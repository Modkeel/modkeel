"""Tests for the pre-build gate (Level 0 prebuilt discovery + Level 1 static checks)."""

from unittest.mock import MagicMock, patch

import pytest
import requests

from modkeel.models import BranchCandidate, ModCompilerConfig
from modkeel.prebuild import (
    DEAD_MAVEN_HOSTS,
    CIStatus,
    PreBuildGate,
    PreBuildVerdict,
    PrebuiltFinder,
    StaticBuildCheck,
    check_maven_host_alive,
    extract_repository_urls,
    gradle_supports_jdk,
    parse_gradle_version,
)


@pytest.fixture
def config():
    return ModCompilerConfig(
        mc_version="1.21.10",
        loader="neoforge",
        loader_version="64",
        output_dir="/tmp/mf-test",
    )


@pytest.fixture
def github(config):
    client = MagicMock()
    client.config = config
    client.get_file_from_repo.return_value = None
    return client


# ── Gradle version parsing ───────────────────────────────────────────────────


class TestParseGradleVersion:
    def test_parses_standard_distribution_url(self):
        url = "https\\://services.gradle.org/distributions/gradle-8.5-bin.zip"
        assert parse_gradle_version(url) == (8, 5)

    def test_parses_all_distribution(self):
        url = "https://services.gradle.org/distributions/gradle-7.6.1-all.zip"
        assert parse_gradle_version(url) == (7, 6)

    def test_returns_none_without_version(self):
        assert parse_gradle_version("https://example.com/gradle-bin.zip") is None


class TestGradleSupportsJdk:
    def test_gradle_8_5_runs_on_jdk_21(self):
        supported, _ = gradle_supports_jdk((8, 5), 21)
        assert supported

    def test_gradle_7_0_fails_on_jdk_21(self):
        supported, reason = gradle_supports_jdk((7, 0), 21)
        assert not supported
        assert "JDK 21" in reason

    def test_gradle_6_9_fails_on_jdk_17(self):
        supported, reason = gradle_supports_jdk((6, 9), 17)
        assert not supported
        assert "8" not in reason.split("needs")[0] or "Gradle 6.9" in reason

    def test_gradle_6_9_runs_on_jdk_8(self):
        supported, _ = gradle_supports_jdk((6, 9), 8)
        assert supported

    def test_unknown_future_jdk_inherits_newest_requirement(self):
        # JDK 99 is not in the table; it must not silently pass an ancient Gradle.
        supported, _ = gradle_supports_jdk((5, 0), 99)
        assert not supported


# ── Repository URL extraction ────────────────────────────────────────────────


class TestExtractRepositoryUrls:
    def test_extracts_groovy_maven_block(self):
        script = """
        repositories {
            maven { url = 'https://maven.neoforged.net/releases' }
            maven { url "https://maven.blamejared.com" }
        }
        """
        urls = extract_repository_urls(script)
        assert "https://maven.neoforged.net/releases" in urls
        assert "https://maven.blamejared.com" in urls

    def test_extracts_kotlin_dsl_form(self):
        script = 'repositories { maven("https://maven.parchmentmc.org") }'
        assert extract_repository_urls(script) == ["https://maven.parchmentmc.org"]

    def test_skips_github_and_doc_urls(self):
        script = """
        // see https://github.com/foo/bar
        maven { url = 'https://maven.example.org' }
        """
        assert extract_repository_urls(script) == ["https://maven.example.org"]

    def test_deduplicates(self):
        script = """
        maven { url = 'https://a.example.org' }
        maven { url = 'https://a.example.org' }
        """
        assert extract_repository_urls(script) == ["https://a.example.org"]


# ── Maven host liveness ──────────────────────────────────────────────────────


class TestCheckMavenHostAlive:
    def test_known_dead_host_rejected_without_network(self):
        alive, reason = check_maven_host_alive("https://jcenter.bintray.com/")
        assert not alive
        assert "JCenter" in reason

    def test_subdomain_of_dead_host_rejected(self):
        alive, _ = check_maven_host_alive("https://cdn.dl.bintray.com/foo")
        assert not alive

    def test_dead_hosts_table_is_lowercase(self):
        for host in DEAD_MAVEN_HOSTS:
            assert host == host.lower()

    @patch("modkeel.prebuild.requests.head")
    def test_404_on_root_is_not_dead(self, mock_head):
        mock_head.return_value = MagicMock(status_code=404)
        alive, _ = check_maven_host_alive("https://maven.example.org")
        assert alive

    @patch("modkeel.prebuild.requests.head")
    def test_connection_error_is_dead(self, mock_head):
        mock_head.side_effect = requests.ConnectionError()
        alive, reason = check_maven_host_alive("https://gone.example.org")
        assert not alive
        assert "unreachable" in reason

    @patch("modkeel.prebuild.requests.head")
    def test_410_is_dead(self, mock_head):
        mock_head.return_value = MagicMock(status_code=410)
        alive, _ = check_maven_host_alive("https://gone.example.org")
        assert not alive

    @patch("modkeel.prebuild.requests.head")
    def test_timeout_is_inconclusive_not_dead(self, mock_head):
        mock_head.side_effect = requests.Timeout()
        alive, _ = check_maven_host_alive("https://slow.example.org")
        assert alive


# ── Level 1 static checks ────────────────────────────────────────────────────


class TestStaticBuildCheck:
    def test_missing_wrapper_is_blocking(self, github):
        def files(owner, repo, branch, path):
            return "apply plugin: 'java'" if path == "build.gradle" else None

        github.get_file_from_repo.side_effect = files
        checker = StaticBuildCheck(github, jdk_major=21)
        verdict = checker.check("owner", "repo", "main")
        assert verdict.will_fail
        assert any("wrapper" in b for b in verdict.blocking)

    def test_unreachable_branch_is_not_blocking(self, github):
        # Every file fetch returns None: the branch is gone, not wrapper-less.
        checker = StaticBuildCheck(github, jdk_major=21)
        verdict = checker.check("owner", "repo", "does-not-exist")
        assert not verdict.will_fail
        assert any("no readable build files" in w for w in verdict.warnings)

    def test_old_gradle_on_modern_jdk_is_blocking(self, github):
        def files(owner, repo, branch, path):
            if "gradle-wrapper" in path:
                return "distributionUrl=https\\://services.gradle.org/distributions/gradle-6.8-bin.zip"
            return None

        github.get_file_from_repo.side_effect = files
        checker = StaticBuildCheck(github, jdk_major=21)
        verdict = checker.check("owner", "repo", "main")
        assert verdict.will_fail
        assert any("Gradle 6.8" in b for b in verdict.blocking)

    def test_modern_gradle_passes(self, github):
        def files(owner, repo, branch, path):
            if "gradle-wrapper" in path:
                return "distributionUrl=https\\://services.gradle.org/distributions/gradle-8.10-bin.zip"
            return None

        github.get_file_from_repo.side_effect = files
        checker = StaticBuildCheck(github, jdk_major=21)
        verdict = checker.check("owner", "repo", "main")
        assert not verdict.will_fail

    def test_dead_maven_repo_is_blocking(self, github):
        def files(owner, repo, branch, path):
            if "gradle-wrapper" in path:
                return "distributionUrl=https\\://services.gradle.org/distributions/gradle-8.10-bin.zip"
            if path == "build.gradle":
                return "repositories { maven { url = 'https://jcenter.bintray.com' } }"
            return None

        github.get_file_from_repo.side_effect = files
        checker = StaticBuildCheck(github, jdk_major=21)
        verdict = checker.check("owner", "repo", "main")
        assert verdict.will_fail
        assert any("dead Maven" in b for b in verdict.blocking)

    def test_unknown_jdk_downgrades_to_warning(self, github):
        def files(owner, repo, branch, path):
            if "gradle-wrapper" in path:
                return "distributionUrl=https\\://services.gradle.org/distributions/gradle-4.10-bin.zip"
            return None

        github.get_file_from_repo.side_effect = files
        checker = StaticBuildCheck(github, jdk_major=None)
        verdict = checker.check("owner", "repo", "main")
        assert not verdict.will_fail
        assert verdict.warnings

    def test_legacy_forgegradle_warns_on_modern_jdk(self, github):
        def files(owner, repo, branch, path):
            if "gradle-wrapper" in path:
                return "distributionUrl=https\\://services.gradle.org/distributions/gradle-8.10-bin.zip"
            if path == "build.gradle":
                return "classpath 'net.minecraftforge.gradle:ForgeGradle:3.0.190'"
            return None

        github.get_file_from_repo.side_effect = files
        checker = StaticBuildCheck(github, jdk_major=21)
        verdict = checker.check("owner", "repo", "main")
        assert any("ForgeGradle" in w for w in verdict.warnings)


# ── CI status scoring ────────────────────────────────────────────────────────


class TestCIStatus:
    def test_green_ci_boosts_score(self):
        assert CIStatus(has_workflows=True, last_conclusion="success").score_delta > 0

    def test_red_ci_penalises_score(self):
        assert CIStatus(has_workflows=True, last_conclusion="failure").score_delta < 0

    def test_no_workflows_penalises_slightly(self):
        status = CIStatus(has_workflows=False)
        assert -50 < status.score_delta < 0

    def test_configured_but_unrun_is_neutral(self):
        assert CIStatus(has_workflows=True).score_delta == 0


# ── Level 0 prebuilt discovery ───────────────────────────────────────────────


class TestPrebuiltFinder:
    def _finder(self, github, payloads):
        finder = PrebuiltFinder(github)
        finder._get = lambda url, timeout=10: next(
            (v for k, v in payloads.items() if k in url), None
        )
        return finder

    def test_finds_release_jar(self, github):
        finder = self._finder(
            github,
            {
                "/releases": [
                    {
                        "tag_name": "v1.21.10",
                        "assets": [
                            {
                                "name": "mod-1.21.10.jar",
                                "size": 500000,
                                "browser_download_url": "https://example.org/mod.jar",
                            }
                        ],
                    }
                ]
            },
        )
        artifact = finder.find_release_jar("owner", "repo", "1.21.10")
        assert artifact is not None
        assert artifact.source == "release"
        assert artifact.name == "mod-1.21.10.jar"

    def test_skips_sources_and_javadoc_jars(self, github):
        finder = self._finder(
            github,
            {
                "/releases": [
                    {
                        "tag_name": "v1.21.10",
                        "assets": [
                            {
                                "name": "mod-1.21.10-sources.jar",
                                "size": 900000,
                                "browser_download_url": "https://example.org/s.jar",
                            },
                            {
                                "name": "mod-1.21.10.jar",
                                "size": 500000,
                                "browser_download_url": "https://example.org/m.jar",
                            },
                        ],
                    }
                ]
            },
        )
        artifact = finder.find_release_jar("owner", "repo", "1.21.10")
        assert artifact.name == "mod-1.21.10.jar"

    def test_filters_by_mc_version(self, github):
        finder = self._finder(
            github,
            {
                "/releases": [
                    {
                        "tag_name": "v1.20.1",
                        "assets": [
                            {
                                "name": "mod-1.20.1.jar",
                                "size": 500000,
                                "browser_download_url": "https://example.org/m.jar",
                            }
                        ],
                    }
                ]
            },
        )
        assert finder.find_release_jar("owner", "repo", "1.21.10") is None

    def test_ignores_draft_releases(self, github):
        finder = self._finder(
            github,
            {
                "/releases": [
                    {
                        "draft": True,
                        "tag_name": "v1.21.10",
                        "assets": [
                            {
                                "name": "mod-1.21.10.jar",
                                "size": 1,
                                "browser_download_url": "u",
                            }
                        ],
                    }
                ]
            },
        )
        assert finder.find_release_jar("owner", "repo", "1.21.10") is None

    def test_no_releases_returns_none(self, github):
        finder = self._finder(github, {})
        assert finder.find_release_jar("owner", "repo") is None

    def test_ci_status_success(self, github):
        finder = self._finder(
            github,
            {
                "/actions/workflows": {"total_count": 2},
                "/actions/runs": {
                    "workflow_runs": [
                        {"conclusion": "success", "html_url": "https://example.org/run"}
                    ]
                },
            },
        )
        status = finder.get_ci_status("owner", "repo", "main")
        assert status.last_conclusion == "success"
        assert status.has_workflows

    def test_ci_status_no_workflows(self, github):
        finder = self._finder(github, {"/actions/workflows": {"total_count": 0}})
        status = finder.get_ci_status("owner", "repo", "main")
        assert not status.has_workflows
        assert status.score_delta < 0

    def test_ci_status_skips_cancelled_runs(self, github):
        finder = self._finder(
            github,
            {
                "/actions/workflows": {"total_count": 1},
                "/actions/runs": {
                    "workflow_runs": [
                        {"conclusion": "cancelled"},
                        {"conclusion": "failure", "html_url": "u"},
                    ]
                },
            },
        )
        status = finder.get_ci_status("owner", "repo", "main")
        assert status.last_conclusion == "failure"

    def test_expired_ci_artifacts_ignored(self, github):
        finder = self._finder(
            github,
            {
                "/actions/runs?branch": {"workflow_runs": [{"id": 1}]},
                "/artifacts": {"artifacts": [{"name": "build", "expired": True}]},
            },
        )
        assert finder.find_ci_artifact("owner", "repo", "main") is None


# ── Gate orchestration ───────────────────────────────────────────────────────


def _branch(name, score=100):
    branch = BranchCandidate(name=name, commit_sha="abc", commit_date="")
    branch.score = score
    branch.minecraft_version = "1.21.10"
    return branch


class TestPreBuildGate:
    def test_drops_branch_with_blocking_failure(self, github):
        gate = PreBuildGate(github, jdk_major=21)
        good, bad = _branch("good"), _branch("bad")

        gate.checker.check = lambda o, r, b: (
            PreBuildVerdict(blocking=["dead Maven repository"])
            if b == "bad"
            else PreBuildVerdict(checks_run=3)
        )
        gate.finder.get_ci_status = lambda o, r, b: CIStatus(has_workflows=True)

        survivors = gate.filter_branches("owner", "repo", [good, bad], verbose=False)
        assert [b.name for b in survivors] == ["good"]

    def test_never_drops_every_branch(self, github):
        gate = PreBuildGate(github, jdk_major=21)
        branches = [_branch("a"), _branch("b")]

        gate.checker.check = lambda o, r, b: PreBuildVerdict(blocking=["boom"])

        survivors = gate.filter_branches("owner", "repo", branches, verbose=False)
        assert len(survivors) == 2

    def test_green_ci_reorders_to_front(self, github):
        gate = PreBuildGate(github, jdk_major=21)
        low, high = _branch("green", score=50), _branch("plain", score=100)

        gate.checker.check = lambda o, r, b: PreBuildVerdict(checks_run=3)
        gate.finder.get_ci_status = lambda o, r, b: (
            CIStatus(has_workflows=True, last_conclusion="success")
            if b == "green"
            else CIStatus(has_workflows=True)
        )

        survivors = gate.filter_branches("owner", "repo", [low, high], verbose=False)
        assert survivors[0].name == "green"

    def test_empty_input_returns_empty(self, github):
        gate = PreBuildGate(github, jdk_major=21)
        assert gate.filter_branches("owner", "repo", [], verbose=False) == []

    def test_find_prebuilt_prefers_release_over_ci(self, github):
        gate = PreBuildGate(github, jdk_major=21)
        gate.finder.find_release_jar = lambda o, r, v=None: "RELEASE"
        gate.finder.find_ci_artifact = lambda o, r, b: "CI"
        assert gate.find_prebuilt("owner", "repo", "main", "1.21.10") == "RELEASE"

    def test_find_prebuilt_falls_back_to_ci(self, github):
        gate = PreBuildGate(github, jdk_major=21)
        gate.finder.find_release_jar = lambda o, r, v=None: None
        gate.finder.find_ci_artifact = lambda o, r, b: "CI"
        assert gate.find_prebuilt("owner", "repo", "main", "1.21.10") == "CI"
