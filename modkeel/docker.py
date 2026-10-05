"""Docker testing for Modkeel."""

import logging
import os
import re
import shutil
import subprocess
import tempfile
import time
from pathlib import Path
from typing import List, Optional

import requests

from modkeel.loaders import (
    KNOWN_LOADER_VERSIONS,
    get_docker_server_type,
    get_installer_filename,
    get_installer_url,
    get_known_version,
    get_maven_domain,
    get_profile,
)
from modkeel.models import CompilationResult, DockerTestCache, ModCompilerConfig
from modkeel.constants import MODKEEL_HOME

logger = logging.getLogger("modkeel")

# ============================================================================
# LOG PATTERNS
# ============================================================================
DOCKER_SUCCESS_PATTERN = re.compile(r"Done \(([\d.]+)s\)! For help")
DOCKER_FAIL_PATTERNS = [
    re.compile(r"Missing or unsupported mandatory dependencies"),
    re.compile(r"Incompatible mod set!"),
    re.compile(r"Crash report saved to"),
    re.compile(r"\[FATAL\]"),
]
DOCKER_DEP_PATTERN = re.compile(
    r"Mod '([^']+)' .* requires .* '([^']+)'"
)

DOCKER_CLIENT_ONLY_PATTERN = re.compile(
    r"invalid dist DEDICATED_SERVER"
)

DOCKER_LOADER_ERRORS = [
    (
        re.compile(r"Module [\w.]+ not found, required by"),
        "Java module incompatibility (wrong JVM version for this loader)"
    ),
    (
        re.compile(r"UnsupportedClassVersionError"),
        "Class file version mismatch (loader needs a different Java version)"
    ),
    (
        re.compile(
            r"Failed to install (?:Neo)?Forge|"
            r"There was an error during installation|"
            r"These libraries failed to download",
            re.IGNORECASE,
        ),
        "Loader installation failed (network issue or corrupt download)"
    ),
    (
        re.compile(r"Could not find or load main class"),
        "Loader installation corrupted (server bootstrap failure)"
    ),
    (
        re.compile(r"Unable to access jarfile"),
        "Loader JAR missing (incomplete server install)"
    ),
    (
        re.compile(r"Minecraft server failed.*exitCode", re.IGNORECASE),
        "Server process crashed during startup (check loader compatibility)"
    ),
]

DOCKER_SERVER_STARTING_PATTERN = re.compile(
    r"Starting minecraft server|ModLauncher running|"
    r"Launching wrapped minecraft"
)

DOCKER_VOLUME_PREFIX = "modkeel_cache"
DOCKER_INSTALL_MAX_RETRIES = 5

# Backwards-compatible alias
NEOFORGE_VERSIONS = KNOWN_LOADER_VERSIONS.get("neoforge", {})


class DockerTester:
    """Encapsulates Docker-based mod testing."""

    def __init__(self, config: ModCompilerConfig):
        self.config = config

    def check_docker_available(self) -> bool:
        """Check if Docker daemon is accessible."""
        try:
            result = subprocess.run(
                ["docker", "info"],
                capture_output=True, text=True, timeout=10
            )
            return result.returncode == 0
        except (FileNotFoundError, subprocess.TimeoutExpired, OSError):
            return False

    def _kill_container(self, name: str) -> None:
        """Best-effort stop and remove a Docker container."""
        try:
            subprocess.run(
                ["docker", "stop", name],
                capture_output=True, timeout=15
            )
        except Exception:
            pass
        try:
            subprocess.run(
                ["docker", "rm", "-f", name],
                capture_output=True, timeout=10
            )
        except Exception:
            pass

    def _get_docker_volume_name(self) -> str:
        """Deterministic volume name based on MC version + loader."""
        tag = (
            f"{self.config.mc_version}_{self.config.loader}"
            .replace(".", "_")
        )
        return f"{DOCKER_VOLUME_PREFIX}_{tag}"

    def _ensure_docker_volume(self) -> str:
        """Create a named Docker volume if it doesn't exist."""
        vol = self._get_docker_volume_name()
        subprocess.run(
            ["docker", "volume", "create", vol],
            capture_output=True, timeout=10,
        )
        return vol

    def _get_loader_cache_dir(self) -> Path:
        """Local cache: ~/.modkeel/loaders/<loader>/<mc_version>/"""
        d = (
            MODKEEL_HOME / "loaders"
            / self.config.loader.lower() / self.config.mc_version
        )
        d.mkdir(parents=True, exist_ok=True)
        return d

    def _is_loader_installed(self, cache_dir: Path) -> bool:
        """Check if a cached loader installation is complete."""
        return (cache_dir / "run.sh").exists() or (
            cache_dir / "run.bat"
        ).exists()

    def _download_installer_with_resume(self, url: str,
                                         dest: Path) -> bool:
        """Download a file with retry + resume (HTTP Range)."""
        max_retries = 10
        for attempt in range(1, max_retries + 1):
            try:
                headers = {}
                mode = "wb"
                existing = 0
                if dest.exists():
                    existing = dest.stat().st_size
                    headers["Range"] = f"bytes={existing}-"
                    mode = "ab"

                resp = requests.get(
                    url, stream=True, timeout=60, headers=headers,
                )
                if resp.status_code == 416:
                    return True
                resp.raise_for_status()

                total_str = resp.headers.get("content-length", "0")
                chunk_total = int(total_str)
                total = existing + chunk_total
                downloaded = existing

                with open(dest, mode) as f:
                    for chunk in resp.iter_content(chunk_size=8192):
                        f.write(chunk)
                        downloaded += len(chunk)
                        if total:
                            pct = downloaded * 100 // total
                            print(
                                f"\r     {downloaded // 1024}KB / "
                                f"{total // 1024}KB ({pct}%)",
                                end="", flush=True,
                            )

                print()
                if downloaded >= total and total > 0:
                    return True

            except Exception as e:
                print(
                    f"\n  \u26a0\ufe0f  Download interrupted (attempt "
                    f"{attempt}/{max_retries}): {e}"
                )
                if attempt < max_retries:
                    wait = min(5 * attempt, 30)
                    print(f"     Resuming in {wait}s...")
                    time.sleep(wait)

        return dest.exists() and dest.stat().st_size > 0

    def _ensure_loader_installed(self) -> Optional[Path]:
        """Ensure the mod loader is installed in a local cache directory."""
        cache_dir = self._get_loader_cache_dir()

        if self._is_loader_installed(cache_dir):
            print(f"  \u2705 Loader cached at {cache_dir}")
            return cache_dir

        loader = self.config.loader.lower()
        profile = get_profile(loader)
        if not profile.get("installer_url_template"):
            return None

        loader_version = get_known_version(loader, self.config.mc_version)
        if not loader_version:
            display = profile["display_name"]
            logger.info(
                "No known %s version for MC %s, "
                "falling back to in-container install",
                display, self.config.mc_version,
            )
            return None

        installer_dir = MODKEEL_HOME / "installers"
        installer_dir.mkdir(parents=True, exist_ok=True)
        installer_fname = get_installer_filename(loader, loader_version)
        installer = installer_dir / installer_fname
        url = get_installer_url(loader, loader_version)

        display = profile["display_name"]
        if not (installer.exists() and installer.stat().st_size > 1_000_000):
            print(
                f"  \U0001f4e5 Downloading {display} {loader_version} installer "
                f"(with retry+resume)..."
            )
            if not self._download_installer_with_resume(url, installer):
                print("  \u274c Could not download installer after retries")
                return None

        install_retries = 10
        for inst_attempt in range(1, install_retries + 1):
            if self._is_loader_installed(cache_dir):
                print(f"  \u2705 {display} installed \u2192 {cache_dir}")
                (cache_dir / "eula.txt").write_text("eula=true\n")
                return cache_dir

            print(
                f"  \U0001f527 Installing {display} {loader_version} "
                f"(attempt {inst_attempt}/{install_retries})..."
            )
            try:
                result = subprocess.run(
                    [
                        "docker", "run", "--rm", "--network=host",
                        "-v",
                        f"{installer.resolve()}:/installer.jar:ro",
                        "-v", f"{cache_dir.resolve()}:/data",
                        "-w", "/data",
                        "--entrypoint", "java",
                        "itzg/minecraft-server:java21",
                        "-jar", "/installer.jar",
                        "--installServer", "/data",
                    ],
                    capture_output=True, text=True, timeout=600,
                )
                if result.returncode == 0:
                    print(f"  \u2705 {display} installed \u2192 {cache_dir}")
                    (cache_dir / "eula.txt").write_text("eula=true\n")
                    return cache_dir

                lines = result.stdout.splitlines()
                ok = sum(
                    1 for line in lines if "Checksum valid" in line
                )
                failed = sum(
                    1 for line in lines
                    if "failed to download" in line.lower()
                )
                print(
                    f"     Libraries: {ok} cached, "
                    f"{failed} failed to download"
                )
                if inst_attempt < install_retries:
                    wait = min(10 * inst_attempt, 60)
                    print(
                        f"     Retrying in {wait}s "
                        f"(progress is saved)..."
                    )
                    time.sleep(wait)

            except subprocess.TimeoutExpired:
                print(
                    f"  \u26a0\ufe0f  Installer timed out "
                    f"(attempt {inst_attempt}/{install_retries})"
                )
                if inst_attempt < install_retries:
                    print("     Retrying (progress is saved)...")
            except Exception as e:
                print(f"  \u26a0\ufe0f  Installer error: {e}")
                return None

        maven = get_maven_domain(loader) or "the download server"
        print(
            f"  \u274c Could not install {display} after "
            f"{install_retries} attempts. "
            f"The {maven} CDN may be down."
        )
        return None

    def _run_docker_server(self, mods_dir: Path) -> dict:
        """Launch a headless Minecraft server in Docker and analyze logs."""
        loader_type = get_docker_server_type(self.config.loader)

        loader_dir = self._ensure_loader_installed()

        if loader_dir and self._is_loader_installed(loader_dir):
            return self._run_docker_server_direct(
                mods_dir, loader_dir,
            )

        return self._run_docker_server_fallback(
            mods_dir, loader_type,
        )

    def _run_docker_server_direct(
        self, mods_dir: Path, loader_dir: Path,
    ) -> dict:
        """Run the server directly from pre-installed loader cache."""
        container_name = (
            f"modkeel_test_{int(time.time())}_{os.getpid()}"
        )

        unix_args = list(
            loader_dir.glob(
                "libraries/net/neoforged/neoforge/*/unix_args.txt"
            )
        )
        if not unix_args:
            return {
                "passed": False,
                "error": "Corrupted loader cache: unix_args.txt "
                         "not found",
                "log_snippet": [],
            }
        args_rel = str(
            unix_args[0].relative_to(loader_dir)
        )

        startup = (
            '#!/bin/sh\n'
            'set -e\n'
            'cd /server\n'
            'ln -s /loader/libraries libraries\n'
            'cp /loader/user_jvm_args.txt .\n'
            'echo "eula=true" > eula.txt\n'
            'printf "server-port=25565\\nquery.port=25565\\n'
            'online-mode=false\\nmax-tick-time=-1\\n" '
            '> server.properties\n'
            'mkdir -p mods\n'
            'cp /mods/*.jar mods/ 2>/dev/null || true\n'
            f'exec java @user_jvm_args.txt @{args_rel} nogui\n'
        )
        startup_path = Path(tempfile.mktemp(
            prefix="modkeel_start_", suffix=".sh",
        ))
        startup_path.write_text(startup)
        startup_path.chmod(0o755)

        cmd = [
            "docker", "run", "--rm",
            "--name", container_name,
            "--tmpfs", "/server",
            "-v", f"{loader_dir.resolve()}:/loader:ro",
            "-v", f"{mods_dir.resolve()}:/mods:ro",
            "-v", f"{startup_path.resolve()}:/start.sh:ro",
            "-w", "/server",
            "--entrypoint", "/bin/sh",
            "itzg/minecraft-server:java21",
            "/start.sh",
        ]

        try:
            process = subprocess.Popen(
                cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
            )
            result = self._analyze_server_logs(process)
        except FileNotFoundError:
            return {
                "passed": False,
                "error": "Docker executable not found",
                "log_snippet": [],
            }
        except Exception as e:
            return {
                "passed": False,
                "error": f"Docker error: {e}",
                "log_snippet": [],
            }
        finally:
            self._kill_container(container_name)
            startup_path.unlink(missing_ok=True)

        return result

    def _run_docker_server_fallback(
        self, mods_dir: Path, loader_type: str,
    ) -> dict:
        """Fallback: use itzg/minecraft-server with Docker volume."""
        volume_name = self._ensure_docker_volume()

        for attempt in range(1, DOCKER_INSTALL_MAX_RETRIES + 1):
            container_name = (
                f"modkeel_test_{int(time.time())}_{os.getpid()}"
            )
            cmd = [
                "docker", "run", "--rm",
                "--name", container_name,
                "-e", "EULA=TRUE",
                "-e", f"TYPE={loader_type}",
                "-e", f"VERSION={self.config.mc_version}",
                "-e", "REMOVE_OLD_MODS=TRUE",
                "-v", f"{mods_dir.resolve()}:/mods:ro",
                "-v", f"{volume_name}:/data",
            ]
            cmd.append("itzg/minecraft-server:java21")

            try:
                process = subprocess.Popen(
                    cmd,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.STDOUT,
                    text=True,
                )
                result = self._analyze_server_logs(process)
            except FileNotFoundError:
                return {
                    "passed": False,
                    "error": "Docker executable not found",
                    "log_snippet": [],
                }
            except Exception as e:
                return {
                    "passed": False,
                    "error": f"Docker error: {e}",
                    "log_snippet": [],
                }
            finally:
                self._kill_container(container_name)

            if not result.get("is_loader_error"):
                return result

            maven = get_maven_domain(self.config.loader) or "the download server"
            if attempt < DOCKER_INSTALL_MAX_RETRIES:
                print(
                    f"  \u26a0\ufe0f  Loader install failed (attempt "
                    f"{attempt}/{DOCKER_INSTALL_MAX_RETRIES}). "
                    f"This is a server-side issue "
                    f"({maven} CDN), not your fault."
                )
                print("     Retrying in 10s...")
                time.sleep(10)
            else:
                print(
                    f"  \u274c Loader install failed after "
                    f"{DOCKER_INSTALL_MAX_RETRIES} attempts. "
                    f"The {loader_type} download server "
                    f"({maven}) is unreliable right now."
                )
                print(
                    "     Try again later, or use a VPN to connect "
                    "through a different region."
                )

        return result

    def _analyze_server_logs(self, process: subprocess.Popen) -> dict:
        """Read server stdout in real time, detect success/failure patterns."""
        server_started = False
        start = None
        recent_lines: List[str] = []
        timeout = self.config.docker_timeout
        absolute_start = time.time()
        absolute_max = 600

        try:
            for raw_line in process.stdout:
                line = raw_line.rstrip("\n")
                recent_lines.append(line)
                if len(recent_lines) > 10:
                    recent_lines.pop(0)

                if (not server_started
                        and DOCKER_SERVER_STARTING_PATTERN.search(line)):
                    server_started = True
                    start = time.time()
                    logger.info(
                        "Docker: server JVM started, timeout begins now"
                    )

                m = DOCKER_SUCCESS_PATTERN.search(line)
                if m:
                    process.terminate()
                    load_ms = int(float(m.group(1)) * 1000)
                    return {
                        "passed": True,
                        "error": None,
                        "log_snippet": recent_lines[-5:],
                        "load_time_ms": load_ms,
                    }

                for regex, explanation in DOCKER_LOADER_ERRORS:
                    if regex.search(line):
                        process.terminate()
                        return {
                            "passed": False,
                            "error": f"[LOADER ERROR] {explanation}",
                            "log_snippet": recent_lines[-5:],
                            "is_loader_error": True,
                        }

                if DOCKER_CLIENT_ONLY_PATTERN.search(line):
                    process.terminate()
                    return {
                        "passed": False,
                        "error": "[CLIENT-ONLY] Mod uses client-side "
                                 "classes, cannot test on headless "
                                 "server",
                        "log_snippet": recent_lines[-5:],
                        "is_client_only": True,
                    }

                for pattern in DOCKER_FAIL_PATTERNS:
                    if pattern.search(line):
                        process.terminate()
                        return {
                            "passed": False,
                            "error": line.strip(),
                            "log_snippet": recent_lines[-5:],
                        }

                if server_started and time.time() - start > timeout:
                    process.terminate()
                    return {
                        "passed": False,
                        "error": (
                            f"Server did not start within "
                            f"{timeout}s timeout"
                        ),
                        "log_snippet": recent_lines[-5:],
                    }

                if time.time() - absolute_start > absolute_max:
                    process.terminate()
                    return {
                        "passed": False,
                        "error": (
                            f"Total Docker time exceeded "
                            f"{absolute_max}s hard limit"
                        ),
                        "log_snippet": recent_lines[-5:],
                    }
        except Exception as e:
            return {
                "passed": False,
                "error": f"Log analysis error: {e}",
                "log_snippet": recent_lines[-5:],
            }
        finally:
            if process.poll() is None:
                process.terminate()
                try:
                    process.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    process.kill()

        all_text = "\n".join(recent_lines)
        for regex, explanation in DOCKER_LOADER_ERRORS:
            if regex.search(all_text):
                return {
                    "passed": False,
                    "error": f"[LOADER ERROR] {explanation}",
                    "log_snippet": recent_lines[-5:],
                    "is_loader_error": True,
                }

        return {
            "passed": False,
            "error": "Server process ended without success signal",
            "log_snippet": recent_lines[-5:],
        }

    @staticmethod
    def _extract_missing_deps_from_logs(
        log_lines: List[str],
    ) -> List[str]:
        """Extract missing dependency mod IDs from server log lines."""
        dep_pattern = re.compile(
            r"Mod '([^']+)' .* requires .* '([^']+)'"
        )
        deps = set()
        for line in log_lines:
            for m in dep_pattern.finditer(line):
                deps.add(m.group(2))
        return sorted(deps)

    def _collect_dep_jars(
        self, results: List[CompilationResult],
        exclude: Optional[set] = None
    ) -> List[Path]:
        """Collect dependency JARs from output_dir that aren't in results."""
        result_jars = {
            Path(r.jar_path).name
            for r in results if r.jar_path
        }
        if exclude:
            result_jars |= exclude
        dep_jars = []
        for jar in self.config.output_dir.glob("*.jar"):
            if jar.name not in result_jars:
                dep_jars.append(jar)
        return dep_jars

    def _test_batch_docker(
        self, jar_paths: List[Path], results: List[CompilationResult]
    ) -> dict:
        """Test all JARs together in a single Docker container."""
        with tempfile.TemporaryDirectory(
            prefix="modkeel_docker_"
        ) as tmp:
            tmp_path = Path(tmp)
            for jar in jar_paths:
                shutil.copy2(jar, tmp_path / jar.name)
            for dep in self._collect_dep_jars(results):
                if not (tmp_path / dep.name).exists():
                    shutil.copy2(dep, tmp_path / dep.name)
            return self._run_docker_server(tmp_path)

    def _test_single_docker(
        self, jar_path: Path, results: List[CompilationResult]
    ) -> dict:
        """Test a single JAR in isolation (with its dependencies)."""
        with tempfile.TemporaryDirectory(
            prefix="modkeel_docker_"
        ) as tmp:
            tmp_path = Path(tmp)
            shutil.copy2(jar_path, tmp_path / jar_path.name)
            for dep in self._collect_dep_jars(
                results, exclude={jar_path.name}
            ):
                shutil.copy2(dep, tmp_path / dep.name)
            return self._run_docker_server(tmp_path)

    def test_mods_in_docker(self, results: List[CompilationResult]) -> None:
        """Docker-test all successfully compiled mods."""
        if not self.check_docker_available():
            print("\n\u26a0\ufe0f  Docker is not available. Skipping Docker tests.")
            print("   Install Docker and ensure the daemon is running "
                  "to enable headless server testing.")
            return

        successful = [r for r in results if r.success and r.jar_path]
        if not successful:
            print("\n\u26a0\ufe0f  No successful mods to Docker-test.")
            return

        jar_paths = [Path(r.jar_path) for r in successful]
        existing_jars = [j for j in jar_paths if j.exists()]
        if not existing_jars:
            print("\n\u26a0\ufe0f  No JAR files found on disk for Docker testing.")
            return

        print(f"\n{'='*80}")
        print(f"\U0001f433 DOCKER TEST: Testing {len(existing_jars)} mod(s) "
              f"in headless Minecraft server")
        print(f"{'='*80}")

        cache = DockerTestCache()
        jar_hash = DockerTestCache.compute_jar_set_hash(existing_jars)
        cached = cache.get(
            jar_hash, self.config.mc_version, self.config.loader
        )
        if cached is not None:
            status = "PASSED" if cached else "FAILED"
            print(f"  \U0001f4cb Cached result found: {status}")
            for r in successful:
                r.docker_tested = True
                r.docker_test_passed = cached
            return

        print(f"  \U0001f504 Batch test: loading all {len(existing_jars)} "
              f"mod(s) into one server...")
        batch = self._test_batch_docker(existing_jars, results)

        if batch["passed"]:
            print("  \u2705 Batch test PASSED \u2014 all mods loaded successfully")
            for r in successful:
                r.docker_tested = True
                r.docker_test_passed = True
                r.docker_load_time_ms = batch.get("load_time_ms")
            cache.set(
                jar_hash, True,
                self.config.mc_version, self.config.loader,
            )
            return

        print(f"  \u274c Batch test FAILED: {batch['error']}")

        if batch.get("is_loader_error"):
            print("  \u26a0\ufe0f  This is a loader/infrastructure error, "
                  "not caused by the mods.")
            print(f"     Snippet: {' | '.join(batch['log_snippet'][-3:])}")
            for r in successful:
                r.docker_tested = True
                r.docker_test_passed = None
                r.docker_error = batch["error"]
            return

        if batch.get("is_client_only") and len(successful) == 1:
            r = successful[0]
            mod_label = r.mod_name or Path(r.jar_path).stem
            r.docker_tested = True
            r.docker_test_passed = None
            r.docker_error = batch["error"]
            print(f"  \u2139\ufe0f  {mod_label} is client-only \u2014 cannot test "
                  f"on headless server")
            return

        print("  \U0001f50d Testing mods individually to isolate failures...")

        for result in successful:
            jar = Path(result.jar_path)
            if not jar.exists():
                continue
            mod_label = result.mod_name or jar.stem
            print(f"\n    \U0001f9ea Testing: {mod_label}...")
            single = self._test_single_docker(jar, results)
            result.docker_tested = True

            if single.get("is_loader_error"):
                result.docker_test_passed = None
                result.docker_error = single["error"]
                print(f"    \u26a0\ufe0f  {mod_label}: INCONCLUSIVE \u2014 "
                      f"{result.docker_error}")
                continue

            if single.get("is_client_only"):
                result.docker_test_passed = None
                result.docker_error = single["error"]
                print(f"    \u2139\ufe0f  {mod_label}: CLIENT-ONLY \u2014 "
                      f"cannot test on headless server")
                continue

            result.docker_test_passed = single["passed"]
            result.docker_load_time_ms = single.get("load_time_ms")
            if single["passed"]:
                print(f"    \u2705 {mod_label}: PASSED")
            else:
                result.docker_error = single["error"]
                missing = self._extract_missing_deps_from_logs(
                    single.get("log_snippet", [])
                )
                if missing:
                    result.docker_error += (
                        f" (missing: {', '.join(missing)})"
                    )
                print(f"    \u274c {mod_label}: FAILED \u2014 {result.docker_error}")
