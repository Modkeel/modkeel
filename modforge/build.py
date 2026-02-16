"""Build and compilation functions for ModForge."""

import json
import logging
import os
import re
import subprocess
import zipfile
from pathlib import Path
from typing import List, Optional, Tuple

import toml

from modforge.models import FailureType
from modforge.version import is_version_in_fabric_range, is_version_in_maven_range

logger = logging.getLogger("modforge")


def classify_build_failure(
    stderr: str, stdout: str
) -> Tuple[FailureType, List[str]]:
    """
    Classify a Gradle build failure by parsing stderr/stdout.
    Returns (failure_type, list_of_missing_dependency_coordinates).
    """
    combined = (stderr or "") + "\n" + (stdout or "")
    missing_deps: List[str] = []

    dep_patterns = [
        r"Could not find\s+([\w.\-]+:[\w.\-]+:[\w.\-+]+)",
        r"Could not resolve\s+([\w.\-]+:[\w.\-]+:[\w.\-+]+)",
        r"Could not resolve all (?:files|dependencies) for configuration",
    ]

    is_dep_failure = False
    for pattern in dep_patterns:
        matches = re.findall(pattern, combined)
        if matches:
            is_dep_failure = True
            if isinstance(matches[0], str) and ":" in matches[0]:
                missing_deps.extend(m.rstrip(".") for m in matches)

    if is_dep_failure:
        seen = set()
        unique_deps = []
        for dep in missing_deps:
            if dep not in seen:
                seen.add(dep)
                unique_deps.append(dep)
        return FailureType.DEPENDENCY_RESOLUTION, unique_deps

    return FailureType.BUILD_ERROR, []


def compile_mod(
    repo_path: Path,
    extra_gradle_args: Optional[List[str]] = None
) -> Tuple[bool, Optional[Path], str, FailureType, List[str]]:
    """
    Compile the mod using Gradle.
    Returns (success, jar_path, message, failure_type, missing_deps)
    """
    print(f"    \U0001f528 Compiling...")

    if os.name == 'nt':
        gradlew = repo_path / "gradlew.bat"
    else:
        gradlew = repo_path / "gradlew"

    if not gradlew.exists():
        return (False, None, "Gradle wrapper not found",
                FailureType.BUILD_ERROR, [])

    if os.name != 'nt':
        os.chmod(gradlew, 0o755)

    # Quick dependency resolution check
    try:
        print(f"    \U0001f50d Checking dependencies...")
        dep_cmd = [
            str(gradlew), "dependencies", "--configuration",
            "compileClasspath", "--no-daemon"
        ]
        if extra_gradle_args:
            dep_cmd.extend(extra_gradle_args)

        dep_result = subprocess.run(
            dep_cmd,
            cwd=repo_path,
            capture_output=True,
            text=True,
            timeout=120
        )
        if dep_result.returncode != 0:
            fail_type, missing = classify_build_failure(
                dep_result.stderr, dep_result.stdout
            )
            if fail_type == FailureType.DEPENDENCY_RESOLUTION:
                print(f"    \u274c Dependency check failed: {', '.join(missing[:3])}")
                return (False, None,
                        f"Dependency check failed: {missing}",
                        fail_type, missing)
            print(f"    \u26a0\ufe0f  Dep check returned error but not dep-related, continuing build...")
    except subprocess.TimeoutExpired:
        print(f"    \u26a0\ufe0f  Dep check timed out, continuing with full build...")
    except Exception as e:
        print(f"    \u26a0\ufe0f  Dep check error ({e}), continuing with full build...")

    try:
        cmd = [str(gradlew), "build", "--no-daemon"]
        if extra_gradle_args:
            cmd.extend(extra_gradle_args)

        result = subprocess.run(
            cmd,
            cwd=repo_path,
            capture_output=True,
            text=True,
            timeout=600
        )

        if result.returncode != 0:
            failure_type, missing_deps = classify_build_failure(
                result.stderr, result.stdout
            )

            stderr_tail = '\n'.join(
                result.stderr.split('\n')[-15:]
            ) if result.stderr else ''
            stdout_tail = '\n'.join(
                result.stdout.split('\n')[-15:]
            ) if result.stdout else ''
            error_detail = f"Gradle build failed (exit code {result.returncode}):\n"
            if stderr_tail.strip():
                error_detail += f"--- stderr ---\n{stderr_tail}\n"
            if stdout_tail.strip():
                error_detail += f"--- stdout ---\n{stdout_tail}"

            if missing_deps:
                error_detail += (
                    f"\n--- missing dependencies ---\n"
                    + "\n".join(f"  {d}" for d in missing_deps)
                )

            return False, None, error_detail, failure_type, missing_deps

        # Find the compiled JAR
        build_libs = repo_path / "build" / "libs"

        if not build_libs.exists():
            return (False, None,
                    "build/libs directory not found after compilation",
                    FailureType.BUILD_ERROR, [])

        exclude_suffixes = (
            '-sources.jar', '-dev.jar', '-javadoc.jar',
            '-slim.jar', '-api.jar'
        )
        jar_files = [
            f for f in build_libs.glob("*.jar")
            if not any(f.name.endswith(s) for s in exclude_suffixes)
        ]

        if not jar_files:
            return (False, None, "No JAR file found in build/libs",
                    FailureType.BUILD_ERROR, [])

        fat_jars = [
            j for j in jar_files
            if j.name.endswith(('-all.jar', '-shadow.jar'))
        ]
        if fat_jars:
            main_jar = max(fat_jars, key=lambda j: j.stat().st_size)
        else:
            main_jar = max(jar_files, key=lambda j: j.stat().st_size)

        return True, main_jar, "Compilation successful", FailureType.NONE, []

    except subprocess.TimeoutExpired:
        return (False, None, "Compilation timeout (>10 minutes)",
                FailureType.TIMEOUT, [])
    except Exception as e:
        return (False, None, f"Compilation error: {e}",
                FailureType.UNKNOWN, [])


def validate_jar(
    jar_path: Path, mc_version: str
) -> Tuple[bool, Optional[str], Optional[str], str]:
    """
    Validate the compiled JAR file.
    Returns (is_valid, mod_name, mod_version, message)
    """
    try:
        with zipfile.ZipFile(jar_path, 'r') as jar:
            toml_path = None
            for name in jar.namelist():
                if name.endswith('mods.toml'):
                    toml_path = name
                    break

            if toml_path:
                with jar.open(toml_path) as f:
                    toml_content = f.read().decode('utf-8')
                    mod_info = toml.loads(toml_content)

                if 'mods' in mod_info and len(mod_info['mods']) > 0:
                    first_mod = mod_info['mods'][0]
                    mod_name = first_mod.get('modId', 'unknown')
                    mod_version = first_mod.get('version', 'unknown')
                else:
                    mod_name = 'unknown'
                    mod_version = 'unknown'

                if 'dependencies' in mod_info:
                    for mod_id, dep_info in mod_info['dependencies'].items():
                        if isinstance(dep_info, list):
                            for dep in dep_info:
                                if dep.get('modId') == 'minecraft':
                                    version_range = dep.get('versionRange', '')
                                    if version_range and not is_version_in_maven_range(mc_version, version_range):
                                        return False, mod_name, mod_version, f"JAR declares incompatible MC version: {version_range}"

            else:
                fabric_path = None
                for name in jar.namelist():
                    if name == 'fabric.mod.json':
                        fabric_path = name
                        break

                if not fabric_path:
                    return False, None, None, "Neither mods.toml nor fabric.mod.json found in JAR"

                with jar.open(fabric_path) as f:
                    fabric_data = json.loads(f.read().decode('utf-8'))

                mod_name = fabric_data.get('id', 'unknown')
                mod_version = fabric_data.get('version', 'unknown')

                depends = fabric_data.get('depends', {})
                mc_range = depends.get('minecraft', '')
                if mc_range and isinstance(mc_range, str):
                    if not is_version_in_fabric_range(mc_version, mc_range):
                        return False, mod_name, mod_version, f"JAR declares incompatible MC version: {mc_range}"

            if jar_path.stat().st_size < 10 * 1024:
                return False, mod_name, mod_version, "JAR file suspiciously small (<10KB)"

            return True, mod_name, mod_version, "JAR validation passed"

    except zipfile.BadZipFile:
        return False, None, None, "Invalid JAR file (corrupted)"
    except Exception as e:
        return False, None, None, f"JAR validation error: {e}"


def create_maven_local_init_script(temp_dir: str) -> Path:
    """Create a Gradle init script that injects mavenLocal()."""
    init_script = Path(temp_dir) / "maven-local-init.gradle"
    init_script.write_text(
        "allprojects {\n"
        "    repositories {\n"
        "        mavenLocal()\n"
        "    }\n"
        "}\n",
        encoding="utf-8"
    )
    return init_script


def publish_to_maven_local(
    repo_path: Path,
    extra_gradle_args: Optional[List[str]] = None
) -> bool:
    """Run publishToMavenLocal on a successfully compiled repo."""
    if os.name == 'nt':
        gradlew = repo_path / "gradlew.bat"
    else:
        gradlew = repo_path / "gradlew"

    if not gradlew.exists():
        return False

    if os.name != 'nt':
        os.chmod(gradlew, 0o755)

    cmd = [str(gradlew), "publishToMavenLocal", "--no-daemon"]
    if extra_gradle_args:
        cmd.extend(extra_gradle_args)

    try:
        result = subprocess.run(
            cmd,
            cwd=repo_path,
            capture_output=True,
            text=True,
            timeout=600
        )
        if result.returncode == 0:
            print(f"    \U0001f4e4 Published to Maven Local (~/.m2/repository/)")
            return True
        else:
            logger.debug(
                "publishToMavenLocal failed (task may not exist): %s",
                result.stderr[:200] if result.stderr else ""
            )
            return False
    except (subprocess.TimeoutExpired, Exception) as e:
        logger.debug("publishToMavenLocal error: %s", e)
        return False
