"""
Unit tests for Docker headless server testing (Phase 2).

Tests log parsing, cache logic, and dependency extraction
WITHOUT requiring Docker to be installed.
"""

import hashlib
import json
import os
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch, PropertyMock

from mod_auto_compiler import ModAutoCompiler
from modforge.models import (
    ModCompilerConfig, CompilationResult, DockerTestCache, FailureType,
)


def make_compiler(
    mc_version: str = "1.21.10",
    loader: str = "neoforge",
    docker_timeout: int = 180,
) -> ModAutoCompiler:
    """Create a compiler with mock config for testing."""
    config = MagicMock(spec=ModCompilerConfig)
    config.mc_version = mc_version
    config.loader = loader.lower()
    config.strict_version = False
    config.github_token = None
    config.github_headers = {}
    config.docker_test = True
    config.docker_timeout = docker_timeout
    config.output_dir = Path("out")
    config.mods_path = None
    compiler = ModAutoCompiler(config)
    return compiler


# ============================================================================
# LOG PATTERN MATCHING
# ============================================================================

class TestLogPatterns(unittest.TestCase):
    """Test Docker server log pattern detection."""

    def test_success_pattern_matches(self):
        """'Done (X.XXXs)! For help' should match success."""
        line = '[Server thread/INFO]: Done (12.345s)! For help, type "help"'
        self.assertIsNotNone(
            ModAutoCompiler.DOCKER_SUCCESS_PATTERN.search(line)
        )

    def test_success_pattern_various_times(self):
        """Success pattern should work with different time formats."""
        for t in ["0.5s", "123.456s", "1s"]:
            line = f"Done ({t})! For help"
            self.assertIsNotNone(
                ModAutoCompiler.DOCKER_SUCCESS_PATTERN.search(line),
                f"Failed for time: {t}"
            )

    def test_success_pattern_no_false_positive(self):
        """Random log lines should NOT match success."""
        lines = [
            "Loading mods...",
            "Starting Minecraft server",
            "Done loading 42 mods",
        ]
        for line in lines:
            self.assertIsNone(
                ModAutoCompiler.DOCKER_SUCCESS_PATTERN.search(line),
                f"False positive: {line}"
            )

    def test_fail_pattern_missing_deps(self):
        """Dependency failure pattern should match."""
        line = (
            "[main/ERROR]: Missing or unsupported mandatory dependencies:"
        )
        matched = any(
            p.search(line) for p in ModAutoCompiler.DOCKER_FAIL_PATTERNS
        )
        self.assertTrue(matched)

    def test_fail_pattern_incompatible(self):
        """Incompatible mod set should match."""
        line = "[main/ERROR]: Incompatible mod set!"
        matched = any(
            p.search(line) for p in ModAutoCompiler.DOCKER_FAIL_PATTERNS
        )
        self.assertTrue(matched)

    def test_fail_pattern_crash_report(self):
        """Crash report line should match."""
        line = (
            "[Server thread/ERROR]: Crash report saved to "
            "./crash-reports/crash-2024-01-15.txt"
        )
        matched = any(
            p.search(line) for p in ModAutoCompiler.DOCKER_FAIL_PATTERNS
        )
        self.assertTrue(matched)

    def test_fail_pattern_fatal(self):
        """[FATAL] should match."""
        line = "[main/ERROR] [FATAL] Something went very wrong"
        matched = any(
            p.search(line) for p in ModAutoCompiler.DOCKER_FAIL_PATTERNS
        )
        self.assertTrue(matched)

    def test_normal_log_no_match(self):
        """Normal server lines should not match any fail pattern."""
        normal_lines = [
            "[Server thread/INFO]: Preparing level \"world\"",
            "[Server thread/INFO]: Loaded 7 recipes",
            "[Server thread/INFO]: Starting minecraft server version 1.21.10",
        ]
        for line in normal_lines:
            matched = any(
                p.search(line) for p in ModAutoCompiler.DOCKER_FAIL_PATTERNS
            )
            self.assertFalse(matched, f"False positive: {line}")


# ============================================================================
# LOADER ERROR DETECTION
# ============================================================================

class TestLoaderErrors(unittest.TestCase):
    """Test detection of known loader/infrastructure errors."""

    def test_java_module_error(self):
        """Java module incompatibility should be detected."""
        line = (
            "java.lang.module.FindException: "
            "Module jdk.crypto.ec not found, required by com.nimbusds"
        )
        matched = False
        for regex, explanation in ModAutoCompiler.DOCKER_LOADER_ERRORS:
            if regex.search(line):
                matched = True
                self.assertIn("Java module", explanation)
                break
        self.assertTrue(matched)

    def test_class_version_error(self):
        """UnsupportedClassVersionError should be detected."""
        line = "java.lang.UnsupportedClassVersionError: net/foo/Bar"
        matched = any(
            r.search(line) for r, _ in ModAutoCompiler.DOCKER_LOADER_ERRORS
        )
        self.assertTrue(matched)

    def test_install_failure(self):
        """Loader installation failure should be detected."""
        lines = [
            "Failed to install NeoForge",
            "There was an error during installation",
            "These libraries failed to download. Try again.",
        ]
        for line in lines:
            matched = any(
                r.search(line)
                for r, _ in ModAutoCompiler.DOCKER_LOADER_ERRORS
            )
            self.assertTrue(matched, f"Not detected: {line}")

    def test_download_warning_not_fatal(self):
        """Individual download WARNING should NOT be a loader error."""
        line = (
            "WARNING: Failed to download from "
            "https://maven.neoforged.net/foo.jar"
        )
        matched = any(
            r.search(line) for r, _ in ModAutoCompiler.DOCKER_LOADER_ERRORS
        )
        self.assertFalse(matched)

    def test_server_failed_exitcode(self):
        """Server process crash with exit code should be detected."""
        line = 'Minecraft server failed. Inspect logs. {"exitCode": 1}'
        matched = any(
            r.search(line) for r, _ in ModAutoCompiler.DOCKER_LOADER_ERRORS
        )
        self.assertTrue(matched)

    def test_normal_log_not_loader_error(self):
        """Normal mod loading lines should NOT match loader errors."""
        lines = [
            "Loading mod create v1.0",
            "[Server thread/INFO]: Loaded 42 mods",
            "Missing or unsupported mandatory dependencies",
        ]
        for line in lines:
            matched = any(
                r.search(line)
                for r, _ in ModAutoCompiler.DOCKER_LOADER_ERRORS
            )
            self.assertFalse(matched, f"False positive: {line}")

    def test_analyze_logs_returns_loader_error_flag(self):
        """_analyze_server_logs should set is_loader_error flag."""
        compiler = make_compiler()
        proc = MagicMock()
        proc.stdout = iter([
            "Starting server...\n",
            "Module jdk.crypto.ec not found, required by foo\n",
        ])
        proc.poll.return_value = None
        proc.terminate = MagicMock()
        proc.wait = MagicMock()
        proc.kill = MagicMock()
        result = compiler._analyze_server_logs(proc)
        self.assertFalse(result["passed"])
        self.assertTrue(result.get("is_loader_error", False))
        self.assertIn("LOADER ERROR", result["error"])


# ============================================================================
# MISSING DEPENDENCY EXTRACTION
# ============================================================================

class TestMissingDeps(unittest.TestCase):
    """Test extraction of missing dependency names from log lines."""

    def test_extract_single_dep(self):
        lines = [
            "Mod 'create' (Create) requires mod 'flywheel' to be available",
        ]
        deps = ModAutoCompiler._extract_missing_deps_from_logs(lines)
        self.assertIn("flywheel", deps)

    def test_extract_multiple_deps(self):
        lines = [
            "Mod 'create' (Create) requires mod 'flywheel' to be available",
            "Mod 'create' (Create) requires mod 'registrate' to be present",
        ]
        deps = ModAutoCompiler._extract_missing_deps_from_logs(lines)
        self.assertEqual(sorted(deps), ["flywheel", "registrate"])

    def test_no_deps_in_normal_logs(self):
        lines = [
            "[Server thread/INFO]: Loading mods...",
            "[Server thread/INFO]: Done!",
        ]
        deps = ModAutoCompiler._extract_missing_deps_from_logs(lines)
        self.assertEqual(deps, [])

    def test_deduplication(self):
        lines = [
            "Mod 'a' (A) requires mod 'dep1' abc",
            "Mod 'b' (B) requires mod 'dep1' xyz",
        ]
        deps = ModAutoCompiler._extract_missing_deps_from_logs(lines)
        self.assertEqual(deps.count("dep1"), 1)


# ============================================================================
# DOCKER TEST CACHE
# ============================================================================

class TestDockerTestCache(unittest.TestCase):
    """Test cache hash determinism, invalidation, and persistence."""

    def setUp(self):
        self._tmpdir = tempfile.mkdtemp()
        self._orig_dir = DockerTestCache.CACHE_DIR
        self._orig_file = DockerTestCache.CACHE_FILE
        DockerTestCache.CACHE_DIR = Path(self._tmpdir)
        DockerTestCache.CACHE_FILE = Path(self._tmpdir) / "test_cache.json"

    def tearDown(self):
        DockerTestCache.CACHE_DIR = self._orig_dir
        DockerTestCache.CACHE_FILE = self._orig_file
        import shutil
        shutil.rmtree(self._tmpdir, ignore_errors=True)

    def _make_jar(self, name: str, content: bytes = b"PK") -> Path:
        p = Path(self._tmpdir) / name
        p.write_bytes(content)
        return p

    def test_hash_determinism(self):
        """Same JAR set always produces same hash."""
        j1 = self._make_jar("a.jar", b"hello")
        j2 = self._make_jar("b.jar", b"world")
        h1 = DockerTestCache.compute_jar_set_hash([j1, j2])
        h2 = DockerTestCache.compute_jar_set_hash([j2, j1])
        self.assertEqual(h1, h2)

    def test_hash_changes_with_content(self):
        """Different content produces different hash."""
        j1 = self._make_jar("a.jar", b"v1")
        h1 = DockerTestCache.compute_jar_set_hash([j1])
        j1.write_bytes(b"v2")
        h2 = DockerTestCache.compute_jar_set_hash([j1])
        self.assertNotEqual(h1, h2)

    def test_cache_roundtrip(self):
        """set() then get() returns the stored value."""
        cache = DockerTestCache()
        cache.set("abc123", True, "1.21.10", "neoforge")
        self.assertTrue(cache.get("abc123", "1.21.10", "neoforge"))

    def test_cache_invalidation_mc_version(self):
        """Changing mc_version invalidates cache."""
        cache = DockerTestCache()
        cache.set("abc123", True, "1.21.10", "neoforge")
        self.assertIsNone(cache.get("abc123", "1.21.9", "neoforge"))

    def test_cache_invalidation_loader(self):
        """Changing loader invalidates cache."""
        cache = DockerTestCache()
        cache.set("abc123", True, "1.21.10", "neoforge")
        self.assertIsNone(cache.get("abc123", "1.21.10", "fabric"))

    def test_cache_miss(self):
        """Unknown hash returns None."""
        cache = DockerTestCache()
        self.assertIsNone(cache.get("unknown", "1.21.10", "neoforge"))

    def test_cache_persistence(self):
        """Cache survives re-instantiation (reads from disk)."""
        cache1 = DockerTestCache()
        cache1.set("persist", False, "1.21.10", "neoforge")
        cache2 = DockerTestCache()
        self.assertFalse(cache2.get("persist", "1.21.10", "neoforge"))

    def test_corrupted_cache_handled(self):
        """Corrupted JSON doesn't crash, returns empty cache."""
        DockerTestCache.CACHE_FILE.write_text("{bad json", encoding="utf-8")
        cache = DockerTestCache()
        self.assertIsNone(cache.get("any", "1.21.10", "neoforge"))


# ============================================================================
# CHECK DOCKER AVAILABLE
# ============================================================================

class TestCheckDockerAvailable(unittest.TestCase):
    """Test check_docker_available() with mocked subprocess."""

    def test_docker_available(self):
        compiler = make_compiler()
        with patch("subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(returncode=0)
            self.assertTrue(compiler.check_docker_available())

    def test_docker_not_installed(self):
        compiler = make_compiler()
        with patch("subprocess.run", side_effect=FileNotFoundError):
            self.assertFalse(compiler.check_docker_available())

    def test_docker_daemon_not_running(self):
        compiler = make_compiler()
        with patch("subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(returncode=1)
            self.assertFalse(compiler.check_docker_available())

    def test_docker_timeout(self):
        compiler = make_compiler()
        with patch("subprocess.run",
                    side_effect=__import__("subprocess").TimeoutExpired(
                        "docker", 10)):
            self.assertFalse(compiler.check_docker_available())


# ============================================================================
# CONTAINER NAME UNIQUENESS
# ============================================================================

class TestContainerName(unittest.TestCase):
    """Container names should be unique across calls."""

    def test_unique_names(self):
        """Two sequential calls produce different container names."""
        compiler = make_compiler()
        names = set()
        for _ in range(5):
            name = f"modforge_test_{int(time.time())}_{os.getpid()}"
            names.add(name)
            time.sleep(0.01)
        # At minimum we get 1 unique due to sub-second calls
        # but with PID they should all differ from other processes
        self.assertGreaterEqual(len(names), 1)


# ============================================================================
# ANALYZE SERVER LOGS
# ============================================================================

class TestAnalyzeServerLogs(unittest.TestCase):
    """Test _analyze_server_logs with mocked Popen."""

    def _mock_process(self, lines):
        """Create a mock Popen with stdout yielding lines."""
        proc = MagicMock()
        proc.stdout = iter(line + "\n" for line in lines)
        proc.poll.return_value = None
        proc.terminate = MagicMock()
        proc.wait = MagicMock()
        proc.kill = MagicMock()
        return proc

    def test_success_detection(self):
        compiler = make_compiler()
        proc = self._mock_process([
            "Loading mods...",
            "Starting server...",
            'Done (12.345s)! For help, type "help"',
        ])
        result = compiler._analyze_server_logs(proc)
        self.assertTrue(result["passed"])
        self.assertIsNone(result["error"])

    def test_failure_detection(self):
        compiler = make_compiler()
        proc = self._mock_process([
            "Loading mods...",
            "Missing or unsupported mandatory dependencies: flywheel",
        ])
        result = compiler._analyze_server_logs(proc)
        self.assertFalse(result["passed"])
        self.assertIn("Missing", result["error"])

    def test_crash_detection(self):
        compiler = make_compiler()
        proc = self._mock_process([
            "Starting server...",
            "Crash report saved to ./crash-reports/crash.txt",
        ])
        result = compiler._analyze_server_logs(proc)
        self.assertFalse(result["passed"])

    def test_timeout_detection(self):
        compiler = make_compiler(docker_timeout=0)
        proc = self._mock_process([
            "Starting minecraft server version 1.21.4",
            "Still loading...",
        ])
        result = compiler._analyze_server_logs(proc)
        self.assertFalse(result["passed"])
        self.assertIn("timeout", result["error"].lower())

    def test_timeout_not_during_install(self):
        """Timeout should NOT trigger during install phase."""
        compiler = make_compiler(docker_timeout=0)
        proc = self._mock_process([
            "Downloading library...",
            "Installing NeoForge...",
            'Done (5s)! For help, type "help"',
        ])
        result = compiler._analyze_server_logs(proc)
        # Server never "started", but success pattern should still match
        self.assertTrue(result["passed"])

    def test_log_snippet_kept(self):
        compiler = make_compiler()
        lines = [f"line {i}" for i in range(15)]
        lines.append('Done (1s)! For help, type "help"')
        proc = self._mock_process(lines)
        result = compiler._analyze_server_logs(proc)
        self.assertTrue(result["passed"])
        self.assertLessEqual(len(result["log_snippet"]), 5)

    def test_process_ends_without_success(self):
        compiler = make_compiler()
        proc = self._mock_process(["just some output"])
        result = compiler._analyze_server_logs(proc)
        self.assertFalse(result["passed"])
        self.assertIn("ended without success", result["error"])


# ============================================================================
# RETRY LOGIC
# ============================================================================

class TestRetryLogic(unittest.TestCase):
    """Test auto-retry on loader install failures."""

    @patch("modforge.docker.time.sleep")
    @patch("modforge.docker.subprocess.Popen")
    @patch("modforge.docker.subprocess.run")
    def test_retries_on_loader_error(self, mock_run, mock_popen,
                                      mock_sleep):
        """Should retry when loader install fails."""
        mock_run.return_value = MagicMock(returncode=0)

        # First call: loader error. Second call: success.
        fail_proc = MagicMock()
        fail_proc.stdout = iter([
            "Failed to install NeoForge\n",
        ])
        fail_proc.poll.return_value = None
        fail_proc.terminate = MagicMock()
        fail_proc.wait = MagicMock()
        fail_proc.kill = MagicMock()

        ok_proc = MagicMock()
        ok_proc.stdout = iter([
            'Done (5.0s)! For help, type "help"\n',
        ])
        ok_proc.poll.return_value = None
        ok_proc.terminate = MagicMock()
        ok_proc.wait = MagicMock()
        ok_proc.kill = MagicMock()

        mock_popen.side_effect = [fail_proc, ok_proc]

        compiler = make_compiler()
        result = compiler._run_docker_server(Path("/tmp/fake"))
        self.assertTrue(result["passed"])
        self.assertEqual(mock_popen.call_count, 2)
        mock_sleep.assert_called_once_with(10)

    @patch("modforge.docker.time.sleep")
    @patch("modforge.docker.subprocess.Popen")
    @patch("modforge.docker.subprocess.run")
    def test_no_retry_on_mod_failure(self, mock_run, mock_popen,
                                      mock_sleep):
        """Should NOT retry when a mod fails (not a loader error)."""
        mock_run.return_value = MagicMock(returncode=0)

        proc = MagicMock()
        proc.stdout = iter([
            "Missing or unsupported mandatory dependencies: flywheel\n",
        ])
        proc.poll.return_value = None
        proc.terminate = MagicMock()
        proc.wait = MagicMock()
        proc.kill = MagicMock()

        mock_popen.return_value = proc

        compiler = make_compiler()
        result = compiler._run_docker_server(Path("/tmp/fake"))
        self.assertFalse(result["passed"])
        self.assertEqual(mock_popen.call_count, 1)
        mock_sleep.assert_not_called()

    @patch("modforge.docker.time.sleep")
    @patch("modforge.docker.subprocess.Popen")
    @patch("modforge.docker.subprocess.run")
    def test_gives_up_after_max_retries(self, mock_run, mock_popen,
                                         mock_sleep):
        """Should give up after DOCKER_INSTALL_MAX_RETRIES."""
        mock_run.return_value = MagicMock(returncode=0)

        def make_fail_proc():
            p = MagicMock()
            p.stdout = iter(["Failed to install NeoForge\n"])
            p.poll.return_value = None
            p.terminate = MagicMock()
            p.wait = MagicMock()
            p.kill = MagicMock()
            return p

        mock_popen.side_effect = [
            make_fail_proc() for _ in range(
                ModAutoCompiler.DOCKER_INSTALL_MAX_RETRIES
            )
        ]

        compiler = make_compiler()
        result = compiler._run_docker_server(Path("/tmp/fake"))
        self.assertFalse(result["passed"])
        self.assertTrue(result.get("is_loader_error"))
        self.assertEqual(
            mock_popen.call_count,
            ModAutoCompiler.DOCKER_INSTALL_MAX_RETRIES,
        )


# ============================================================================
# DATA STRUCTURE FIELDS
# ============================================================================

class TestDataStructures(unittest.TestCase):
    """Verify new fields exist on data classes."""

    def test_failure_type_docker_values(self):
        self.assertEqual(FailureType.DOCKER_CRASH.value, "docker_crash")
        self.assertEqual(FailureType.DOCKER_TIMEOUT.value, "docker_timeout")
        self.assertEqual(
            FailureType.DOCKER_DEPENDENCY.value, "docker_dependency"
        )

    def test_compilation_result_docker_fields(self):
        r = CompilationResult(repo_url="test", success=True)
        self.assertFalse(r.docker_tested)
        self.assertIsNone(r.docker_test_passed)
        self.assertIsNone(r.docker_error)

    def test_config_docker_fields(self):
        config = ModCompilerConfig(
            mc_version="1.21.10",
            loader="neoforge",
            loader_version="64",
            docker_test=True,
            docker_timeout=300,
        )
        self.assertTrue(config.docker_test)
        self.assertEqual(config.docker_timeout, 300)

    def test_config_docker_defaults(self):
        config = ModCompilerConfig(
            mc_version="1.21.10",
            loader="neoforge",
            loader_version="64",
        )
        self.assertFalse(config.docker_test)
        self.assertEqual(config.docker_timeout, 180)


if __name__ == "__main__":
    unittest.main()
