"""Docker testing for Modkeel."""

import logging
import os
import re
import shutil
import subprocess
import tempfile
import time
import uuid
from pathlib import Path
from typing import List, Optional

import requests

from modkeel.loaders import (
    KNOWN_LOADER_VERSIONS,
    get_docker_server_type,
    get_docker_version_env,
    get_installer_filename,
    get_installer_url,
    get_known_version,
    get_maven_domain,
    get_profile,
    is_explicit_loader_version,
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

# A client-only mod on a dedicated server: Forge refuses it by dist; NeoForge and Fabric load
# it and it dies reaching for a client class (net.minecraft.client.*), which servers lack.
# Inconclusive rather than failed: the server cannot tell whether it works in a client.
DOCKER_CLIENT_ONLY_PATTERN = re.compile(
    r"invalid dist DEDICATED_SERVER"
    r"|(?:NoClassDefFoundError|ClassNotFoundException):? net[./]minecraft[./]client[./]"
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

# Loader errors that fail the same way on every attempt: retrying them only wastes minutes,
# and they are not the download server's fault.
DOCKER_DETERMINISTIC_LOADER_ERRORS = {
    "Java module incompatibility (wrong JVM version for this loader)",
    "Class file version mismatch (loader needs a different Java version)",
}

# Java runtimes published as itzg/minecraft-server:java<N> images.
DOCKER_JAVA_IMAGES = (8, 11, 16, 17, 21, 25)
DOCKER_IMAGE = "itzg/minecraft-server"


def _java_from_version_number(mc_version: str) -> int:
    """Java a Minecraft version needs, from its number alone (when Mojang's metadata is
    unreachable): 26.x -> 25, 1.20.5+ -> 21, 1.18+ -> 17, 1.17 -> 16, older -> 8.
    Snapshots and other unparsable ids get the newest runtime."""
    try:
        parts = tuple(int(p) for p in mc_version.split("."))
    except ValueError:
        return DOCKER_JAVA_IMAGES[-1]
    if parts[0] >= 26:
        return 25
    if parts >= (1, 20, 5):
        return 21
    if parts >= (1, 18):
        return 17
    if parts >= (1, 17):
        return 16
    return 8


def server_image(mc_version: str) -> str:
    """The itzg/minecraft-server image whose Java runs this Minecraft version.

    Mojang's metadata decides the Java version (fallback: the version number). The exact
    runtime is used when an image exists for it, else the nearest newer one: old loaders
    (Forge on Java 8) break on newer runtimes, new game jars refuse older ones.
    """
    from modkeel.mappings import java_major_version

    java = java_major_version(mc_version) or _java_from_version_number(mc_version)
    tag = next((j for j in DOCKER_JAVA_IMAGES if j >= java), DOCKER_JAVA_IMAGES[-1])
    return f"{DOCKER_IMAGE}:java{tag}"


DOCKER_SERVER_STARTING_PATTERN = re.compile(
    r"Starting minecraft server|ModLauncher running|"
    r"Launching wrapped minecraft"
)

DOCKER_VOLUME_PREFIX = "modkeel_cache"
DOCKER_INSTALL_MAX_RETRIES = 5

# Backwards-compatible alias
NEOFORGE_VERSIONS = KNOWN_LOADER_VERSIONS.get("neoforge", {})


def _loader_error(explanation: str, recent_lines: List[str]) -> dict:
    """Result for a loader/infrastructure failure; deterministic ones are not retried."""
    return {
        "passed": False,
        "error": f"[LOADER ERROR] {explanation}",
        "log_snippet": recent_lines[-5:],
        "is_loader_error": True,
        "retryable": explanation not in DOCKER_DETERMINISTIC_LOADER_ERRORS,
    }


def _container_name() -> str:
    """A Docker container name no other test run can hold.

    Time + PID alone repeat within one second, e.g. the fallback's retry loop starting a new
    container right after one that timed out and could not be removed, which makes
    `docker run --name` fail with "name already in use". The random suffix rules that out.
    """
    return f"modkeel_test_{int(time.time())}_{os.getpid()}_{uuid.uuid4().hex[:8]}"


class DockerTester:
    """Encapsulates Docker-based mod testing."""

    def __init__(self, config: ModCompilerConfig):
        self.config = config

    def _loader_version(self) -> Optional[str]:
        """Loader version the test server runs, used by every cache and container below.

        The user's -lv wins when it is a full version, so the server matches the target
        the mods were built for. Otherwise the known version for this MC release, and
        None when there is none: the fallback image then installs its latest loader.
        """
        requested = getattr(self.config, "loader_version", None)
        if is_explicit_loader_version(requested):
            return requested
        return get_known_version(self.config.loader, self.config.mc_version)

    def _describe_loader(self) -> str:
        """'NeoForge 21.10.64' or 'NeoForge (latest)', for log lines."""
        display = get_profile(self.config.loader)["display_name"]
        return f"{display} {self._loader_version() or '(latest)'}"

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
        """Deterministic volume name: MC version + loader + loader version.

        One volume per loader version, so switching -lv does not make the image
        reinstall over (or reuse) another version's server files.
        """
        tag = re.sub(
            r"[^A-Za-z0-9_-]", "_",
            f"{self.config.mc_version}_{self.config.loader}"
            f"_{self._loader_version() or 'latest'}",
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

    def _get_loader_cache_dir(self, loader_version: str) -> Path:
        """Local cache: ~/.modkeel/loaders/<loader>/<mc_version>/<loader_version>/

        Keyed by loader version too: a server installed for one -lv is never reused
        for another.
        """
        d = (
            MODKEEL_HOME / "loaders"
            / self.config.loader.lower() / self.config.mc_version
            / loader_version
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
        loader = self.config.loader.lower()
        profile = get_profile(loader)
        if not profile.get("installer_url_template"):
            return None

        loader_version = self._loader_version()
        if not loader_version:
            display = profile["display_name"]
            logger.info(
                "No known %s version for MC %s, "
                "falling back to in-container install",
                display, self.config.mc_version,
            )
            return None

        cache_dir = self._get_loader_cache_dir(loader_version)
        if self._is_loader_installed(cache_dir):
            print(f"  \u2705 Loader cached at {cache_dir}")
            return cache_dir

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
                        server_image(self.config.mc_version),
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
        container_name = _container_name()

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
            server_image(self.config.mc_version),
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
            container_name = _container_name()
            cmd = [
                "docker", "run", "--rm",
                "--name", container_name,
                "-e", "EULA=TRUE",
                "-e", f"TYPE={loader_type}",
                "-e", f"VERSION={self.config.mc_version}",
            ]
            # Pin the loader version when there is one, or the image installs its latest
            version_env = get_docker_version_env(self.config.loader)
            loader_version = self._loader_version()
            if version_env and loader_version:
                cmd += ["-e", f"{version_env}={loader_version}"]
            cmd += [
                "-e", "REMOVE_OLD_MODS=TRUE",
                "-v", f"{mods_dir.resolve()}:/mods:ro",
                "-v", f"{volume_name}:/data",
            ]
            cmd.append(server_image(self.config.mc_version))

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

            if not result.get("is_loader_error") or not result.get("retryable", True):
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
                        return _loader_error(explanation, recent_lines)

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
                return _loader_error(explanation, recent_lines)

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
        requested = getattr(self.config, "loader_version", None)
        if requested and requested != "0" and not is_explicit_loader_version(requested):
            print(f"  \u26a0\ufe0f  Loader version '{requested}' is not a full version; "
                  f"testing on {self._describe_loader()}")
        print(f"  Server: Minecraft {self.config.mc_version} + {self._describe_loader()}")

        cache = DockerTestCache()
        jar_hash = DockerTestCache.compute_jar_set_hash(existing_jars)
        cached = cache.get(
            jar_hash, self.config.mc_version, self.config.loader,
            self._loader_version(),
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
                self._loader_version(),
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
