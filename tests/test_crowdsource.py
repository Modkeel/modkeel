"""
Tests for Phase 3: Crowdsource config, report building, HMAC signing,
Java detection, and report submission logic.
"""

import hashlib
import hmac as hmac_lib
import json
import subprocess
import tempfile
from pathlib import Path
from unittest.mock import MagicMock, patch, PropertyMock

import pytest

from mod_auto_compiler import ModAutoCompiler
from modforge.config import ModForgeConfig, prompt_sharing_preference
from modforge.constants import MODFORGE_HMAC_KEY, MODFORGE_VERSION
from modforge.crowdsource import sign_report
from modforge.models import (
    CompilationResult, FailureType, ModCompilerConfig,
)


# ============================================================================
# ModForgeConfig Tests
# ============================================================================
class TestModForgeConfig:
    """Test persistent config loading/saving."""

    def test_creates_config_dir_and_file(self, tmp_path):
        """Config should create ~/.modforge/config.toml on first run."""
        config_file = tmp_path / "config.toml"
        with patch.object(ModForgeConfig, "CONFIG_DIR", tmp_path), \
             patch.object(ModForgeConfig, "CONFIG_FILE", config_file):
            cfg = ModForgeConfig()
            assert config_file.exists()
            assert cfg.client_id  # UUID should be generated
            assert cfg.is_first_run is True

    def test_client_id_persists(self, tmp_path):
        """client_id should stay the same across loads."""
        config_file = tmp_path / "config.toml"
        with patch.object(ModForgeConfig, "CONFIG_DIR", tmp_path), \
             patch.object(ModForgeConfig, "CONFIG_FILE", config_file):
            cfg1 = ModForgeConfig()
            client_id = cfg1.client_id

            cfg2 = ModForgeConfig()
            assert cfg2.client_id == client_id
            assert cfg2.is_first_run is False  # file already exists

    def test_sharing_default_is_ask(self, tmp_path):
        """Default sharing preference should be 'ask'."""
        config_file = tmp_path / "config.toml"
        with patch.object(ModForgeConfig, "CONFIG_DIR", tmp_path), \
             patch.object(ModForgeConfig, "CONFIG_FILE", config_file):
            cfg = ModForgeConfig()
            assert cfg.sharing == "ask"

    def test_sharing_setter_validates(self, tmp_path):
        """Invalid sharing values should raise ValueError."""
        config_file = tmp_path / "config.toml"
        with patch.object(ModForgeConfig, "CONFIG_DIR", tmp_path), \
             patch.object(ModForgeConfig, "CONFIG_FILE", config_file):
            cfg = ModForgeConfig()
            with pytest.raises(ValueError):
                cfg.sharing = "invalid"

    def test_sharing_setter_saves(self, tmp_path):
        """Setting sharing should persist to disk."""
        config_file = tmp_path / "config.toml"
        with patch.object(ModForgeConfig, "CONFIG_DIR", tmp_path), \
             patch.object(ModForgeConfig, "CONFIG_FILE", config_file):
            cfg = ModForgeConfig()
            cfg.sharing = "always"

            cfg2 = ModForgeConfig()
            assert cfg2.sharing == "always"

    def test_mark_prompted(self, tmp_path):
        """mark_prompted should set _prompted flag and persist."""
        config_file = tmp_path / "config.toml"
        with patch.object(ModForgeConfig, "CONFIG_DIR", tmp_path), \
             patch.object(ModForgeConfig, "CONFIG_FILE", config_file):
            cfg = ModForgeConfig()
            assert cfg.was_prompted is False
            cfg.mark_prompted()
            assert cfg.was_prompted is True
            assert cfg.is_first_run is False

            cfg2 = ModForgeConfig()
            assert cfg2.was_prompted is True


# ============================================================================
# prompt_sharing_preference Tests
# ============================================================================
class TestPromptSharingPreference:
    """Test first-run opt-in prompt."""

    def test_always_choice(self, tmp_path):
        """Choosing 'A' should set sharing to 'always'."""
        config_file = tmp_path / "config.toml"
        with patch.object(ModForgeConfig, "CONFIG_DIR", tmp_path), \
             patch.object(ModForgeConfig, "CONFIG_FILE", config_file), \
             patch("builtins.input", return_value="A"):
            cfg = ModForgeConfig()
            result = prompt_sharing_preference(cfg)
            assert result == "always"
            assert cfg.sharing == "always"
            assert cfg.was_prompted is True

    def test_never_choice(self, tmp_path):
        """Choosing 'N' should set sharing to 'never'."""
        config_file = tmp_path / "config.toml"
        with patch.object(ModForgeConfig, "CONFIG_DIR", tmp_path), \
             patch.object(ModForgeConfig, "CONFIG_FILE", config_file), \
             patch("builtins.input", return_value="N"):
            cfg = ModForgeConfig()
            result = prompt_sharing_preference(cfg)
            assert result == "never"
            assert cfg.sharing == "never"

    def test_ask_choice(self, tmp_path):
        """Choosing 'K' should set sharing to 'ask'."""
        config_file = tmp_path / "config.toml"
        with patch.object(ModForgeConfig, "CONFIG_DIR", tmp_path), \
             patch.object(ModForgeConfig, "CONFIG_FILE", config_file), \
             patch("builtins.input", return_value="K"):
            cfg = ModForgeConfig()
            result = prompt_sharing_preference(cfg)
            assert result == "ask"

    def test_eof_defaults_to_ask(self, tmp_path):
        """EOFError (piped input) should default to 'ask'."""
        config_file = tmp_path / "config.toml"
        with patch.object(ModForgeConfig, "CONFIG_DIR", tmp_path), \
             patch.object(ModForgeConfig, "CONFIG_FILE", config_file), \
             patch("builtins.input", side_effect=EOFError):
            cfg = ModForgeConfig()
            result = prompt_sharing_preference(cfg)
            assert result == "ask"


# ============================================================================
# sign_report Tests
# ============================================================================
class TestSignReport:
    """Test HMAC-SHA256 report signing."""

    def test_deterministic_signature(self):
        """Same report should always produce the same signature."""
        report = {
            "mod_name": "Jade",
            "mc_version": "1.21.4",
            "loader": "neoforge",
            "status": "works",
            "client_id": "test-uuid",
        }
        sig1 = sign_report(report)
        sig2 = sign_report(report)
        assert sig1 == sig2
        assert sig1.startswith("hmac-sha256:")

    def test_signature_format(self):
        """Signature should be 'hmac-sha256:<hex>'."""
        report = {"mod_name": "Test", "client_id": "abc"}
        sig = sign_report(report)
        prefix, hex_part = sig.split(":")
        assert prefix == "hmac-sha256"
        assert len(hex_part) == 64  # SHA-256 hex digest

    def test_signature_excludes_signature_field(self):
        """A 'signature' key in the report should be excluded from signing."""
        report = {"mod_name": "Test", "client_id": "abc", "signature": "old"}
        sig = sign_report(report)

        report_clean = {"mod_name": "Test", "client_id": "abc"}
        sig_clean = sign_report(report_clean)
        assert sig == sig_clean

    def test_different_data_different_signature(self):
        """Different reports should produce different signatures."""
        r1 = {"mod_name": "Jade", "client_id": "abc"}
        r2 = {"mod_name": "AppleSkin", "client_id": "abc"}
        assert sign_report(r1) != sign_report(r2)

    def test_manual_hmac_verification(self):
        """Verify signature matches manual HMAC computation."""
        report = {"mod_name": "Test", "mc_version": "1.21.4"}
        serialized = json.dumps(report, sort_keys=True, separators=(",", ":"))
        expected = hmac_lib.new(
            MODFORGE_HMAC_KEY, serialized.encode("utf-8"), hashlib.sha256,
        ).hexdigest()
        sig = sign_report(report)
        assert sig == f"hmac-sha256:{expected}"


# ============================================================================
# _detect_java_version Tests
# ============================================================================
class TestDetectJavaVersion:
    """Test Java version detection from subprocess."""

    def test_parses_standard_version(self):
        """Should parse 'openjdk version "21.0.1"' format."""
        mock_result = MagicMock()
        mock_result.stderr = 'openjdk version "21.0.1" 2023-10-17\n'
        mock_result.stdout = ""
        with patch("subprocess.run", return_value=mock_result):
            version = ModAutoCompiler._detect_java_version()
            assert version == "21.0.1"

    def test_parses_old_format(self):
        """Should parse 'java version "1.8.0_321"' format."""
        mock_result = MagicMock()
        mock_result.stderr = 'java version "1.8.0_321"\n'
        mock_result.stdout = ""
        with patch("subprocess.run", return_value=mock_result):
            version = ModAutoCompiler._detect_java_version()
            assert version == "1.8.0_321"

    def test_handles_no_java(self):
        """Should return 'unknown' if java is not installed."""
        with patch("subprocess.run", side_effect=FileNotFoundError):
            version = ModAutoCompiler._detect_java_version()
            assert version == "unknown"

    def test_handles_timeout(self):
        """Should return 'unknown' on timeout."""
        with patch("subprocess.run", side_effect=subprocess.TimeoutExpired("java", 10)):
            version = ModAutoCompiler._detect_java_version()
            assert version == "unknown"


# ============================================================================
# build_report Tests
# ============================================================================
class TestBuildReport:
    """Test crowdsource report building from CompilationResult."""

    @pytest.fixture
    def compiler(self):
        """Create a minimal ModAutoCompiler with mocked config."""
        config = MagicMock(spec=ModCompilerConfig)
        config.mc_version = "1.21.4"
        config.loader = "neoforge"
        config.loader_version = "64"
        config.github_token = None
        config.headers = {}
        config.output_dir = Path(tempfile.mkdtemp())
        config.instance_path = None
        config.strict_version = False
        config.cross_loader = True
        config.docker_test = False
        config.docker_timeout = 180
        return ModAutoCompiler(config)

    def test_successful_compilation_report(self, compiler, tmp_path):
        """Successfully compiled mod should produce a report with status='compiled'."""
        jar_file = tmp_path / "test-mod.jar"
        jar_file.write_bytes(b"fake jar content")

        result = CompilationResult(
            repo_url="https://github.com/Snownee/Jade",
            success=True,
            branch="1.21.4",
            jar_path=str(jar_file),
            mod_name="Jade",
            mod_version="17.3.0",
        )

        report = compiler.build_report(result)
        assert report is not None
        assert report["mod_name"] == "Jade"
        assert report["mod_version"] == "17.3.0"
        assert report["status"] == "compiled"
        assert report["mc_version"] == "1.21.4"
        assert report["loader"] == "neoforge"
        assert report["loader_version"] == "64"
        assert report["cli_version"] == MODFORGE_VERSION
        assert report["source_repo"] == "Snownee/Jade"
        assert report["jar_hash_sha256"]  # non-empty hash

    def test_docker_passed_report(self, compiler, tmp_path):
        """Docker-tested mod that passed should have status='works'."""
        jar_file = tmp_path / "test.jar"
        jar_file.write_bytes(b"jar")

        result = CompilationResult(
            repo_url="https://github.com/squeek502/appleskin",
            success=True,
            mod_name="AppleSkin",
        )
        result.jar_path = str(jar_file)
        result.docker_tested = True
        result.docker_test_passed = True
        result.docker_load_time_ms = 6018

        report = compiler.build_report(result)
        assert report["status"] == "works"
        assert report["load_time_ms"] == 6018

    def test_docker_failed_report(self, compiler, tmp_path):
        """Docker-tested mod that failed should have status='fails'."""
        jar_file = tmp_path / "test.jar"
        jar_file.write_bytes(b"jar")

        result = CompilationResult(
            repo_url="https://github.com/test/mod",
            success=True,
            mod_name="TestMod",
        )
        result.jar_path = str(jar_file)
        result.docker_tested = True
        result.docker_test_passed = False
        result.docker_error = "Crash on startup"

        report = compiler.build_report(result)
        assert report["status"] == "fails"
        assert "Crash" in report["log_snippet"]

    def test_client_only_returns_none(self, compiler):
        """Client-only (inconclusive) mods should not generate a report."""
        result = CompilationResult(
            repo_url="https://github.com/test/mod",
            success=True,
            mod_name="ClientMod",
        )
        result.docker_tested = True
        result.docker_test_passed = None  # client-only / inconclusive

        report = compiler.build_report(result)
        assert report is None

    def test_failed_compilation_returns_none(self, compiler):
        """Failed compilations should not generate a report."""
        result = CompilationResult(
            repo_url="https://github.com/test/mod",
            success=False,
            error="Build failed",
        )
        report = compiler.build_report(result)
        assert report is None

    def test_jar_hash_computed(self, compiler, tmp_path):
        """JAR hash should match SHA-256 of file contents."""
        content = b"test jar content for hashing"
        jar_file = tmp_path / "hash-test.jar"
        jar_file.write_bytes(content)

        expected_hash = hashlib.sha256(content).hexdigest()

        result = CompilationResult(
            repo_url="https://github.com/test/mod",
            success=True,
            jar_path=str(jar_file),
            mod_name="HashTest",
        )
        report = compiler.build_report(result)
        assert report["jar_hash_sha256"] == expected_hash

    def test_cross_loader_flag(self, compiler, tmp_path):
        """is_cross_loader should propagate to report."""
        jar_file = tmp_path / "test.jar"
        jar_file.write_bytes(b"jar")

        result = CompilationResult(
            repo_url="https://github.com/test/mod",
            success=True,
            jar_path=str(jar_file),
            mod_name="FabricMod",
            is_cross_loader=True,
        )
        report = compiler.build_report(result)
        assert report["is_cross_loader"] is True

    def test_modrinth_download_flag(self, compiler, tmp_path):
        """modrinth_download should propagate to report."""
        jar_file = tmp_path / "test.jar"
        jar_file.write_bytes(b"jar")

        result = CompilationResult(
            repo_url="https://github.com/test/mod",
            success=True,
            jar_path=str(jar_file),
            mod_name="ModrinthMod",
            modrinth_download=True,
        )
        report = compiler.build_report(result)
        assert report["modrinth_download"] is True


# ============================================================================
# submit_reports Tests
# ============================================================================
class TestSubmitReports:
    """Test report submission logic."""

    @pytest.fixture
    def compiler_with_results(self, tmp_path):
        """Compiler with one successful result."""
        config = MagicMock(spec=ModCompilerConfig)
        config.mc_version = "1.21.4"
        config.loader = "neoforge"
        config.loader_version = "64"
        config.github_token = None
        config.headers = {}
        config.output_dir = tmp_path
        config.instance_path = None
        config.strict_version = False
        config.cross_loader = True
        config.docker_test = False
        config.docker_timeout = 180

        compiler = ModAutoCompiler(config)

        jar_file = tmp_path / "test.jar"
        jar_file.write_bytes(b"jar")
        result = CompilationResult(
            repo_url="https://github.com/test/mod",
            success=True,
            jar_path=str(jar_file),
            mod_name="TestMod",
        )
        result.docker_tested = True
        result.docker_test_passed = True
        compiler.results = [result]
        return compiler

    def test_skips_when_sharing_never(self, compiler_with_results, tmp_path):
        """Should not submit when sharing='never'."""
        config_file = tmp_path / "cfg" / "config.toml"
        config_dir = tmp_path / "cfg"
        with patch.object(ModForgeConfig, "CONFIG_DIR", config_dir), \
             patch.object(ModForgeConfig, "CONFIG_FILE", config_file):
            mf_cfg = ModForgeConfig()
            mf_cfg.sharing = "never"

            with patch("requests.post") as mock_post:
                compiler_with_results.submit_reports(mf_cfg)
                mock_post.assert_not_called()

    def test_skips_when_no_api_url(self, compiler_with_results, tmp_path):
        """Should not submit when MODFORGE_API_URL is empty."""
        config_file = tmp_path / "cfg" / "config.toml"
        config_dir = tmp_path / "cfg"
        with patch.object(ModForgeConfig, "CONFIG_DIR", config_dir), \
             patch.object(ModForgeConfig, "CONFIG_FILE", config_file), \
             patch("modforge.crowdsource.MODFORGE_API_URL", ""):
            mf_cfg = ModForgeConfig()
            mf_cfg.sharing = "always"

            with patch("requests.post") as mock_post:
                compiler_with_results.submit_reports(mf_cfg)
                mock_post.assert_not_called()

    def test_submits_when_always(self, compiler_with_results, tmp_path):
        """Should submit when sharing='always' and API URL is set."""
        config_file = tmp_path / "cfg" / "config.toml"
        config_dir = tmp_path / "cfg"
        with patch.object(ModForgeConfig, "CONFIG_DIR", config_dir), \
             patch.object(ModForgeConfig, "CONFIG_FILE", config_file), \
             patch("modforge.crowdsource.MODFORGE_API_URL", "https://api.example.com/submit"):
            mf_cfg = ModForgeConfig()
            mf_cfg.sharing = "always"

            mock_resp = MagicMock()
            mock_resp.status_code = 201
            with patch("requests.post", return_value=mock_resp) as mock_post:
                compiler_with_results.submit_reports(mf_cfg)
                mock_post.assert_called_once()
                call_json = mock_post.call_args.kwargs.get("json") or mock_post.call_args[1].get("json")
                assert call_json["mod_name"] == "TestMod"
                assert call_json["client_id"] == mf_cfg.client_id
                assert call_json["signature"].startswith("hmac-sha256:")

    def test_ask_mode_user_declines(self, compiler_with_results, tmp_path):
        """When sharing='ask' and user says 'n', should not submit."""
        config_file = tmp_path / "cfg" / "config.toml"
        config_dir = tmp_path / "cfg"
        with patch.object(ModForgeConfig, "CONFIG_DIR", config_dir), \
             patch.object(ModForgeConfig, "CONFIG_FILE", config_file), \
             patch("modforge.crowdsource.MODFORGE_API_URL", "https://api.example.com/submit"), \
             patch("builtins.input", return_value="n"):
            mf_cfg = ModForgeConfig()
            mf_cfg.sharing = "ask"

            with patch("requests.post") as mock_post:
                compiler_with_results.submit_reports(mf_cfg)
                mock_post.assert_not_called()

    def test_network_error_silent(self, compiler_with_results, tmp_path):
        """Network errors should be silently ignored."""
        config_file = tmp_path / "cfg" / "config.toml"
        config_dir = tmp_path / "cfg"
        with patch.object(ModForgeConfig, "CONFIG_DIR", config_dir), \
             patch.object(ModForgeConfig, "CONFIG_FILE", config_file), \
             patch("modforge.crowdsource.MODFORGE_API_URL", "https://api.example.com/submit"):
            mf_cfg = ModForgeConfig()
            mf_cfg.sharing = "always"

            with patch("requests.post", side_effect=ConnectionError("no network")):
                # Should not raise
                compiler_with_results.submit_reports(mf_cfg)


import subprocess  # needed for TimeoutExpired in TestDetectJavaVersion
