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

from modforge.loaders import LOADER_PROFILES
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


_EXCLUDED_JAR_SUFFIXES = ('-sources.jar', '-dev.jar', '-javadoc.jar', '-slim.jar', '-api.jar')
_MOD_METADATA = ('META-INF/neoforge.mods.toml', 'META-INF/mods.toml', 'fabric.mod.json',
                 'quilt.mod.json')


def _is_mod_jar(jar: Path) -> bool:
    try:
        with zipfile.ZipFile(jar) as z:
            names = set(z.namelist())
    except (zipfile.BadZipFile, OSError):
        return False
    return any(m in names for m in _MOD_METADATA)


def find_output_jars(repo_path: Path, max_depth: int = 3) -> List[Path]:
    """Built JARs from the root and subprojects (multi-loader builds put them in
    ``neoforge/build/libs`` and similar). Jars carrying mod metadata win; when none
    do, fall back to every candidate so single-module builds keep working."""
    candidates = []
    for depth in range(max_depth + 1):
        pattern = "/".join(["*"] * depth + ["build", "libs", "*.jar"])
        for jar in repo_path.glob(pattern):
            rel = jar.relative_to(repo_path).parts
            if rel[0] == "buildSrc" or jar.name.endswith(_EXCLUDED_JAR_SUFFIXES):
                continue
            candidates.append(jar)
    mod_jars = [j for j in candidates if _is_mod_jar(j)]
    return mod_jars or candidates


# The lines that say why a build failed; Gradle's own tail only says that it did
_KEY_ERROR = re.compile(r"\berror:|\[ERROR\]|^\s*ERROR\b|Caused by:|cannot find symbol"
                        r"|What went wrong|OutOfMemoryError|Not enough memory", re.I)
_ANSI = re.compile(r"\x1b\[[0-9;]*m")


def key_error_lines(output: str, limit: int = 12) -> List[str]:
    """First distinct error lines of a build's output, colour codes stripped."""
    out: List[str] = []
    lines = [ln.strip() for ln in _ANSI.sub("", output).splitlines()]
    for i, line in enumerate(lines):
        if not line or not _KEY_ERROR.search(line):
            continue
        if line.startswith("* What went wrong"):
            # Gradle puts the cause on the next line
            line = next((ln for ln in lines[i + 1:] if ln), line)
        if line not in out:
            out.append(line[:300])
            if len(out) >= limit:
                break
    return out


GRADLE_DISTS = Path.home() / ".modforge" / "gradle"
_WRAPPER_URL = re.compile(r"^distributionUrl\s*=\s*(\S+)", re.M)
_LOADER_DIR_ORDER = ("fabric", "neoforge", "forge", "quilt")


def _gradle_script(root: Path) -> Path:
    return root / ("gradlew.bat" if os.name == 'nt' else "gradlew")


def ensure_gradle_distribution(url: str) -> Optional[Path]:
    """Download and unpack a Gradle distribution once; return its gradle executable."""
    import requests  # local: nothing else in build.py touches the network

    name = url.rsplit("/", 1)[-1].removesuffix(".zip")
    home = GRADLE_DISTS / name
    exe_name = "gradle.bat" if os.name == 'nt' else "gradle"
    found = list(home.glob(f"*/bin/{exe_name}")) if home.exists() else []
    if found:
        return found[0]
    home.mkdir(parents=True, exist_ok=True)
    archive = home / "dist.zip"
    try:
        with requests.get(url, stream=True, timeout=300) as r:
            r.raise_for_status()
            with open(archive, "wb") as f:
                for chunk in r.iter_content(1 << 16):
                    f.write(chunk)
        with zipfile.ZipFile(archive) as z:
            z.extractall(home)
    except Exception as e:  # noqa: BLE001 - the caller reports a missing wrapper
        logger.debug(f"Gradle distribution download failed ({url}): {e}")
        return None
    finally:
        archive.unlink(missing_ok=True)
    found = list(home.glob(f"*/bin/{exe_name}"))
    if found and os.name != 'nt':
        os.chmod(found[0], 0o755)
    return found[0] if found else None


def resolve_gradle(repo_path: Path, loader: Optional[str] = None
                   ) -> Tuple[Optional[Path], Optional[Path]]:
    """(build root, gradle executable) for a repo.

    The wrapper script wins. A repo that ships only gradle-wrapper.properties gets the
    distribution it names. Repos whose loaders are separate Gradle projects in subfolders
    (``Fabric/``, ``NeoForge/``) build the folder of ``loader``, else the first known one.
    """
    script = _gradle_script(repo_path)
    if script.exists():
        if os.name != 'nt':
            os.chmod(script, 0o755)
        return repo_path, script
    props = repo_path / "gradle" / "wrapper" / "gradle-wrapper.properties"
    has_settings = any((repo_path / f).exists()
                       for f in ("settings.gradle", "settings.gradle.kts",
                                 "build.gradle", "build.gradle.kts"))
    if props.exists() and has_settings:
        m = _WRAPPER_URL.search(props.read_text(encoding="utf-8", errors="replace"))
        if m:
            exe = ensure_gradle_distribution(m.group(1).replace("\\:", ":"))
            if exe:
                return repo_path, exe
    subs = {d.name.lower(): d for d in repo_path.iterdir()
            if d.is_dir() and (_gradle_script(d).exists()
                               or (d / "gradle" / "wrapper" / "gradle-wrapper.properties")
                               .exists())}
    for name in ([loader.lower()] if loader else []) + list(_LOADER_DIR_ORDER):
        if name in subs:
            return resolve_gradle(subs[name])
    return None, None


def loader_subproject(root: Path, loader: str) -> Optional[str]:
    """Gradle path of a multi-loader repo's subproject for ``loader`` (``:Fabric``), if any."""
    for d in root.iterdir():
        if d.is_dir() and d.name.lower() == loader.lower() and \
                any((d / f).exists() for f in ("build.gradle", "build.gradle.kts")):
            return f":{d.name}"
    return None


def build_tasks(root: Path, mc_version: Optional[str], loader: Optional[str] = None) -> List[str]:
    """Tasks to build. Stonecutter builds every version it knows, and a multi-loader
    build fails as a whole when one loader breaks: narrow to the target (and ``loader``)."""
    sub = loader_subproject(root, loader) if loader else None
    if sub:
        return [f"{sub}:build"]
    versions = root / "versions"
    stonecutter = any((root / f).exists()
                      for f in ("stonecutter.gradle.kts", "stonecutter.gradle"))
    if not (mc_version and stonecutter and versions.is_dir()):
        return ["build"]
    names = [d.name for d in versions.iterdir()
             if d.is_dir() and (d.name == mc_version or d.name.startswith(mc_version + "-"))]
    return [f":{n}:build" for n in sorted(names)] or ["build"]


def select_main_jar(jar_files: List[Path], mc_version: Optional[str] = None) -> Path:
    """Fat jar, else the largest. Jars that validate for ``mc_version`` go first, so a
    multi-version build does not hand back another version's jar."""
    if mc_version:
        jar_files = [j for j in jar_files if validate_jar(j, mc_version)[0]] or jar_files
    fat_jars = [j for j in jar_files if j.name.endswith(('-all.jar', '-shadow.jar'))]
    return max(fat_jars or jar_files, key=lambda j: j.stat().st_size)


def compile_mod(
    repo_path: Path,
    extra_gradle_args: Optional[List[str]] = None,
    mc_version: Optional[str] = None,
    loader: Optional[str] = None,
) -> Tuple[bool, Optional[Path], str, FailureType, List[str]]:
    """
    Compile the mod using Gradle.
    Returns (success, jar_path, message, failure_type, missing_deps)
    """
    print(f"    \U0001f528 Compiling...")

    root, gradlew = resolve_gradle(repo_path, loader)
    if not gradlew:
        return (False, None, "Gradle wrapper not found",
                FailureType.BUILD_ERROR, [])
    repo_path = root

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
        cmd = [str(gradlew), *build_tasks(repo_path, mc_version, loader), "--no-daemon"]
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
            causes = key_error_lines(result.stdout + "\n" + result.stderr)
            if causes:
                error_detail += "--- errors ---\n" + "\n".join(causes) + "\n"
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

        jar_files = find_output_jars(repo_path)

        if not jar_files:
            return (False, None, "No mod JAR found in any build/libs",
                    FailureType.BUILD_ERROR, [])

        main_jar = select_main_jar(jar_files, mc_version)

        return True, main_jar, "Compilation successful", FailureType.NONE, []

    except subprocess.TimeoutExpired:
        return (False, None, "Compilation timeout (>10 minutes)",
                FailureType.TIMEOUT, [])
    except Exception as e:
        return (False, None, f"Compilation error: {e}",
                FailureType.UNKNOWN, [])


def _validate_toml_jar(
    jar: zipfile.ZipFile, toml_path: str, mc_version: str
) -> Tuple[bool, Optional[str], Optional[str], str]:
    """Validate a TOML-based mod JAR (NeoForge/Forge mods.toml)."""
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

    return True, mod_name, mod_version, "JAR validation passed"


def _validate_json_jar(
    jar: zipfile.ZipFile, json_path: str, mc_version: str
) -> Tuple[bool, Optional[str], Optional[str], str]:
    """Validate a JSON-based mod JAR (Fabric/Quilt)."""
    with jar.open(json_path) as f:
        data = json.loads(f.read().decode('utf-8'))

    # Quilt schema: quilt_loader.id, quilt_loader.version
    quilt_loader = data.get('quilt_loader', {})
    if quilt_loader:
        mod_name = quilt_loader.get('id', 'unknown')
        mod_version_val = quilt_loader.get('version', 'unknown')
        for dep in quilt_loader.get('depends', []):
            if isinstance(dep, dict) and dep.get('id') == 'minecraft':
                versions = dep.get('versions', '')
                if versions and isinstance(versions, str):
                    if not is_version_in_fabric_range(mc_version, versions):
                        return False, mod_name, mod_version_val, f"JAR declares incompatible MC version: {versions}"
        return True, mod_name, mod_version_val, "JAR validation passed"

    # Fabric schema: id, version, depends.minecraft
    mod_name = data.get('id', 'unknown')
    mod_version_val = data.get('version', 'unknown')

    depends = data.get('depends', {})
    mc_range = depends.get('minecraft', '')
    if mc_range and isinstance(mc_range, str):
        if not is_version_in_fabric_range(mc_version, mc_range):
            return False, mod_name, mod_version_val, f"JAR declares incompatible MC version: {mc_range}"

    return True, mod_name, mod_version_val, "JAR validation passed"


def validate_jar(
    jar_path: Path, mc_version: str
) -> Tuple[bool, Optional[str], Optional[str], str]:
    """
    Validate the compiled JAR file.
    Returns (is_valid, mod_name, mod_version, message)
    """
    try:
        with zipfile.ZipFile(jar_path, 'r') as jar:
            jar_names = set(jar.namelist())

            # Try each loader profile's metadata files
            for loader_name, profile in LOADER_PROFILES.items():
                fmt = profile["jar_metadata_format"]
                for metadata_file in profile["jar_metadata_files"]:
                    if metadata_file in jar_names:
                        if fmt == "toml":
                            valid, name, ver, msg = _validate_toml_jar(
                                jar, metadata_file, mc_version
                            )
                        else:
                            valid, name, ver, msg = _validate_json_jar(
                                jar, metadata_file, mc_version
                            )
                        if not valid:
                            return valid, name, ver, msg
                        if jar_path.stat().st_size < 10 * 1024:
                            return False, name, ver, "JAR file suspiciously small (<10KB)"
                        return True, name, ver, msg

            return False, None, None, "No recognized mod metadata found in JAR"

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
    root, gradlew = resolve_gradle(repo_path)
    if not gradlew:
        return False
    repo_path = root

    # Release builds often GPG-sign their publications; a local publish needs no signature
    # and would otherwise fail with "no configured signatory".
    no_signing = repo_path / ".modforge-no-signing.gradle"
    no_signing.write_text(
        "allprojects {\n"
        "    tasks.withType(Sign).configureEach { enabled = false }\n"
        "}\n",
        encoding="utf-8"
    )

    cmd = [str(gradlew), "publishToMavenLocal", "--no-daemon", "-x", "test",
           "--init-script", str(no_signing)]
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
