"""Characterization tests for modkeel.pipeline.Pipeline.

They pin what clone_and_compile / process_repos / generate_report do today, branch by branch,
with every network, git and Gradle call replaced by a double. The point is a safety net for
refactoring the orchestrator: a change that rewires a step differently fails here first.

Doubles are attached to the Pipeline instance (github, validator, modrinth, prebuild, docker)
and the module-level build helpers are patched where pipeline.py looks them up.
"""

from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from modkeel.models import BranchCandidate, CompilationResult, FailureType, ModCompilerConfig
from modkeel.pipeline import Pipeline

REPO = "https://github.com/owner/mod"


def branch(name: str, mc: str = "1.21.10", loader: str = "neoforge",
           sha: str = "") -> BranchCandidate:
    # Each branch has its own head by default; a fork's unchanged copy of an upstream
    # branch shares its SHA (see TestForkCopies).
    b = BranchCandidate(name, sha or f"sha-{name}", "2026-01-01T00:00:00Z")
    b.minecraft_version = mc
    b.loader = loader
    b.loader_version = "64"
    b.is_compatible = True
    return b


@pytest.fixture
def pipeline(tmp_path):
    """A Pipeline whose collaborators are doubles; nothing leaves the process."""
    config = ModCompilerConfig(mc_version="1.21.10", loader="neoforge", loader_version="64",
                               output_dir=str(tmp_path / "out"))
    p = Pipeline(config)
    p.temp_dir = str(tmp_path / "work")
    Path(p.temp_dir).mkdir()

    p.github = MagicMock()
    p.github.get_repo_info.return_value = {"stargazers_count": 1, "forks_count": 0}
    p.github.search_compatible_repos.return_value = []
    p.validator = MagicMock()
    p.validator.filter_branches_by_version_proximity.side_effect = lambda bs: bs
    p.validator.score_branch.return_value = 10
    p.validator.validate_gradle_properties.return_value = (True, "props ok")
    p.validator.validate_build_gradle.return_value = (True, "build ok")
    p.modrinth = MagicMock()
    p.modrinth.check_modrinth.return_value = None
    p.modrinth.is_cross_loader_available.return_value = False
    p.prebuild = MagicMock()
    p.prebuild.filter_branches.side_effect = lambda owner, repo, bs: bs
    p.prebuild.find_prebuilt.return_value = None
    p.docker = MagicMock()
    return p


@pytest.fixture
def build(tmp_path):
    """Patch git clone, Gradle and JAR validation as seen from pipeline.py.

    Yields the mocks; by default the clone succeeds, the build produces a JAR and the JAR
    validates as "TestMod 1.0".
    """
    jar = tmp_path / "built" / "testmod-1.0.jar"
    jar.parent.mkdir()
    jar.write_bytes(b"jar")
    with patch("modkeel.pipeline.subprocess.run") as run, \
            patch("modkeel.pipeline.compile_mod") as compile_mod, \
            patch("modkeel.pipeline.validate_jar") as validate_jar:
        run.return_value = MagicMock(returncode=0, stderr="")
        compile_mod.return_value = (True, jar, "built", FailureType.NONE, [])
        validate_jar.return_value = (True, "TestMod", "1.0", "jar ok")
        yield MagicMock(run=run, compile_mod=compile_mod, validate_jar=validate_jar, jar=jar)


# ---------------------------------------------------------------------------
# Step 0: Modrinth
# ---------------------------------------------------------------------------

class TestModrinthStep:
    def test_modrinth_hit_downloads_and_skips_github(self, pipeline):
        pipeline.modrinth.check_modrinth.return_value = {
            "download_url": "https://cdn/x.jar", "filename": "x.jar",
            "title": "Mod", "version_number": "2.0",
        }
        with patch("modkeel.pipeline.requests.get") as get:
            get.return_value = MagicMock(content=b"bytes")
            result = pipeline.clone_and_compile(REPO)

        assert result.success and result.modrinth_download
        assert result.mod_name == "Mod" and result.mod_version == "2.0"
        assert (pipeline.config.output_dir / "x.jar").read_bytes() == b"bytes"
        pipeline.modrinth.check_modrinth.assert_called_once_with("mod", source_repo="owner/mod")
        pipeline.modrinth.download_modrinth_deps.assert_called_once()
        pipeline.github.get_branches.assert_not_called()

    def test_cross_loader_hit_is_flagged_and_loader_restored(self, pipeline):
        hit = {"download_url": "u", "filename": "f.jar", "title": "Mod",
               "version_number": "1"}
        pipeline.modrinth.is_cross_loader_available.return_value = True
        answers = iter([None, hit])
        seen_loaders = []

        def record(*args, **kwargs):
            seen_loaders.append(pipeline.config.loader)
            return next(answers)
        pipeline.modrinth.check_modrinth.side_effect = record

        with patch("modkeel.pipeline.requests.get") as get:
            get.return_value = MagicMock(content=b"x")
            result = pipeline.clone_and_compile(REPO)

        assert result.success and result.is_cross_loader
        assert seen_loaders == ["neoforge", "fabric"]
        assert pipeline.config.loader == "neoforge"

    def test_modrinth_download_failure_falls_back_to_github(self, pipeline, build):
        pipeline.modrinth.check_modrinth.return_value = {
            "download_url": "u", "filename": "f.jar", "title": "Mod", "version_number": "1"}
        pipeline.github.get_branches.return_value = [branch("main")]
        pipeline.validator.pre_validate_branches.return_value = [branch("main")]
        with patch("modkeel.pipeline.requests.get", side_effect=OSError("down")):
            result = pipeline.clone_and_compile(REPO)
        assert result.success and not result.modrinth_download
        assert result.branch == "main"

    def test_skip_modrinth_and_specific_branch_never_ask_modrinth(self, pipeline, build):
        pipeline.github.get_branches.return_value = [branch("dev")]
        pipeline.validator.pre_validate_branch.return_value = True
        pipeline.clone_and_compile(REPO, specific_branch="dev")
        pipeline.clone_and_compile(REPO, skip_modrinth=True)
        pipeline.modrinth.check_modrinth.assert_not_called()


# ---------------------------------------------------------------------------
# Branch discovery
# ---------------------------------------------------------------------------

class TestBranchDiscovery:
    def test_no_branches_fails(self, pipeline):
        pipeline.github.get_branches.return_value = []
        result = pipeline.clone_and_compile(REPO)
        assert not result.success
        assert result.error == "Could not fetch branches from repository"

    def test_branch_in_url_is_used_and_must_exist(self, pipeline):
        pipeline.github.get_branches.return_value = [branch("main")]
        result = pipeline.clone_and_compile(REPO + "/tree/missing")
        assert not result.success
        assert result.error == "Specified branch 'missing' not found"

    def test_specific_branch_failing_prevalidation(self, pipeline):
        b = branch("dev")
        b.validation_error = "wrong loader"
        pipeline.github.get_branches.return_value = [b]
        pipeline.validator.pre_validate_branch.return_value = False
        result = pipeline.clone_and_compile(REPO, specific_branch="dev")
        assert not result.success
        assert result.error == "Branch 'dev' is not compatible: wrong loader"

    def test_nothing_compatible_without_cross_loader(self, pipeline):
        pipeline.config.cross_loader = False
        pipeline.github.get_branches.return_value = [branch("main")]
        pipeline.validator.pre_validate_branches.return_value = []
        result = pipeline.clone_and_compile(REPO)
        assert not result.success
        assert result.error == ("No compatible branches in original repo or forks for "
                                "MC 1.21.10 + neoforge")
        pipeline.github.search_compatible_repos.assert_called_once()

    def test_nothing_compatible_with_cross_loader_names_the_fallbacks(self, pipeline):
        pipeline.modrinth.is_cross_loader_available.return_value = True
        pipeline.github.get_branches.return_value = [branch("main")]
        pipeline.validator.pre_validate_branches.return_value = []
        result = pipeline.clone_and_compile(REPO)
        assert not result.success
        assert result.error.endswith("(also tried fabric cross-loader fallback)")

    def test_cross_loader_branch_compiles_as_cross_loader(self, pipeline, build):
        pipeline.modrinth.is_cross_loader_available.return_value = True
        pipeline.github.get_branches.return_value = [branch("main")]
        fabric = branch("fabric", loader="fabric")

        def prevalidate(owner, repo, branches, override_loader=None):
            return [fabric] if override_loader == "fabric" else []
        pipeline.validator.pre_validate_branches.side_effect = prevalidate

        result = pipeline.clone_and_compile(REPO)
        assert result.success and result.is_cross_loader
        # loader validation is skipped for the fallback loader's gradle.properties
        pipeline.validator.validate_gradle_properties.assert_called_with(
            Path(pipeline.temp_dir) / "mod", skip_loader_validation=True)

    def test_exact_fork_branch_wins_over_close_upstream(self, pipeline, build):
        pipeline.github.get_branches.side_effect = [
            [branch("old", mc="1.21.9")], [branch("port")]]
        pipeline.validator.pre_validate_branches.side_effect = [
            [branch("old", mc="1.21.9")], [branch("port")]]
        pipeline.validator.analyze_fork_diff.return_value = (True, 5, "clean")
        pipeline.github.search_compatible_repos.return_value = [{
            "fork": {"owner": "alice", "repo": "mod-port", "full_name": "alice/mod-port",
                     "url": "https://github.com/alice/mod-port"},
            "score": 90, "signals": ["recent"], "trust_score": 85}]

        result = pipeline.clone_and_compile(REPO)

        assert result.success and result.branch == "port"
        clone_cmd = build.run.call_args.args[0]
        assert "https://github.com/alice/mod-port.git" in clone_cmd

    def test_fork_diff_bonus_decides_between_equal_branches(self, pipeline, build):
        """Two exact fork branches with the same base score: the cleaner diff is built."""
        pipeline.github.get_branches.side_effect = [
            [branch("old", mc="1.21.9")], [branch("noisy"), branch("clean")]]
        pipeline.validator.pre_validate_branches.side_effect = [
            [branch("old", mc="1.21.9")], [branch("noisy"), branch("clean")]]
        pipeline.validator.analyze_fork_diff.side_effect = (
            lambda o, r, fo, fr, name: (name == "clean", 200 if name == "clean" else 0, "d"))
        pipeline.github.search_compatible_repos.return_value = [{
            "fork": {"owner": "alice", "repo": "mod-port", "full_name": "alice/mod-port",
                     "url": "https://github.com/alice/mod-port"},
            "score": 90, "signals": [], "trust_score": 85}]

        result = pipeline.clone_and_compile(REPO)

        assert result.branch == "clean"

    def test_close_upstream_used_when_forks_have_nothing(self, pipeline, build):
        pipeline.github.get_branches.return_value = [branch("old", mc="1.21.9")]
        pipeline.validator.pre_validate_branches.return_value = [branch("old", mc="1.21.9")]
        result = pipeline.clone_and_compile(REPO)
        assert result.success and result.compiled_mc_version == "1.21.9"

    def test_branches_are_tried_in_score_order(self, pipeline, build):
        low, high = branch("low"), branch("high")
        pipeline.github.get_branches.return_value = [low, high]
        pipeline.validator.pre_validate_branches.return_value = [low, high]
        pipeline.validator.score_branch.side_effect = lambda b: {"low": 1, "high": 9}[b.name]
        result = pipeline.clone_and_compile(REPO)
        assert result.branch == "high"


# ---------------------------------------------------------------------------
# Pre-build gate and prebuilt JARs
# ---------------------------------------------------------------------------

class TestPrebuild:
    def test_gate_filters_branches(self, pipeline, build):
        pipeline.github.get_branches.return_value = [branch("a"), branch("b")]
        pipeline.validator.pre_validate_branches.return_value = [branch("a"), branch("b")]
        pipeline.prebuild.filter_branches.side_effect = lambda o, r, bs: []
        result = pipeline.clone_and_compile(REPO)
        assert not result.success
        assert result.error == "All 0 branches failed:"
        build.run.assert_not_called()

    def test_gate_disabled_is_not_called(self, pipeline, build):
        pipeline.config.prebuild_gate = False
        pipeline.github.get_branches.return_value = [branch("a")]
        pipeline.validator.pre_validate_branches.return_value = [branch("a")]
        pipeline.clone_and_compile(REPO)
        pipeline.prebuild.filter_branches.assert_not_called()

    def test_prebuilt_result_short_circuits_the_build(self, pipeline, build):
        pipeline.github.get_branches.return_value = [branch("a")]
        pipeline.validator.pre_validate_branches.return_value = [branch("a")]
        done = CompilationResult(repo_url=REPO, success=True, branch="a")
        with patch.object(Pipeline, "_try_prebuilt", return_value=done) as tried:
            result = pipeline.clone_and_compile(REPO)
        assert result is done
        tried.assert_called_once()
        build.run.assert_not_called()

    def test_prebuilt_disabled_is_not_tried(self, pipeline, build):
        pipeline.config.use_prebuilt = False
        pipeline.github.get_branches.return_value = [branch("a")]
        pipeline.validator.pre_validate_branches.return_value = [branch("a")]
        with patch.object(Pipeline, "_try_prebuilt") as tried:
            pipeline.clone_and_compile(REPO)
        tried.assert_not_called()


# ---------------------------------------------------------------------------
# Clone, validate, compile
# ---------------------------------------------------------------------------

class TestBuildLoop:
    def setup_branches(self, pipeline, *names):
        bs = [branch(n) for n in names]
        pipeline.github.get_branches.return_value = bs
        pipeline.validator.pre_validate_branches.return_value = bs

    def test_success_copies_jar_and_reports_it(self, pipeline, build):
        self.setup_branches(pipeline, "main")
        result = pipeline.clone_and_compile(REPO)
        dest = pipeline.config.output_dir / "testmod-1.0.jar"
        assert result.success and result.jar_path == str(dest) and dest.exists()
        assert (result.mod_name, result.mod_version, result.branch) == ("TestMod", "1.0", "main")
        assert result.clone_dir == Path(pipeline.temp_dir) / "mod"
        assert build.run.call_args.args[0][:6] == ["git", "clone", "-b", "main", "--depth", "1"]
        build.compile_mod.assert_called_once_with(
            Path(pipeline.temp_dir) / "mod", None, "1.21.10")

    def test_extra_gradle_args_reach_compile_mod(self, pipeline, build):
        self.setup_branches(pipeline, "main")
        pipeline.clone_and_compile(REPO, extra_gradle_args=["--init-script", "x"])
        assert build.compile_mod.call_args.args[1] == ["--init-script", "x"]

    def test_clone_failure_moves_to_next_branch(self, pipeline, build):
        self.setup_branches(pipeline, "first", "second")
        build.run.side_effect = [MagicMock(returncode=1, stderr="boom"),
                                 MagicMock(returncode=0, stderr="")]
        result = pipeline.clone_and_compile(REPO)
        assert result.success and result.branch == "second"

    def test_all_clones_failing_reports_clone_error(self, pipeline, build):
        self.setup_branches(pipeline, "only")
        build.run.return_value = MagicMock(returncode=128, stderr="not found")
        result = pipeline.clone_and_compile(REPO)
        assert not result.success and result.failure_type == FailureType.CLONE_ERROR
        assert result.error == "All 1 branches failed:\n  - only: Clone failed: not found"

    def test_gradle_properties_rejection(self, pipeline, build):
        self.setup_branches(pipeline, "main")
        pipeline.validator.validate_gradle_properties.return_value = (False, "wrong mc")
        result = pipeline.clone_and_compile(REPO)
        assert result.failure_type == FailureType.VALIDATION_ERROR
        build.compile_mod.assert_not_called()

    def test_dependency_failure_keeps_missing_deps_and_clone_dir(self, pipeline, build):
        self.setup_branches(pipeline, "main")
        build.compile_mod.return_value = (
            False, None, "deps", FailureType.DEPENDENCY_RESOLUTION, ["com.x:lib:1"])
        result = pipeline.clone_and_compile(REPO)
        assert result.failure_type == FailureType.DEPENDENCY_RESOLUTION
        assert result.missing_dependencies == ["com.x:lib:1"]
        assert result.clone_dir == Path(pipeline.temp_dir) / "mod"

    def test_invalid_jar_tries_next_branch(self, pipeline, build):
        self.setup_branches(pipeline, "a", "b")
        build.validate_jar.side_effect = [(False, None, None, "bad toml"),
                                          (True, "TestMod", "1.0", "ok")]
        result = pipeline.clone_and_compile(REPO)
        assert result.success and result.branch == "b"

    def test_rejected_jar_is_a_validation_failure(self, pipeline, build):
        self.setup_branches(pipeline, "main")
        build.validate_jar.return_value = (False, None, None, "declares MC 1.20.1")
        result = pipeline.clone_and_compile(REPO)
        assert not result.success
        assert result.failure_type == FailureType.VALIDATION_ERROR
        assert result.error.endswith("main: JAR validation: declares MC 1.20.1")

    def test_failure_type_is_the_last_branch_s(self, pipeline, build):
        """A dependency failure followed by a rejected JAR is not retried with mavenLocal."""
        self.setup_branches(pipeline, "a", "b")
        build.compile_mod.side_effect = [
            (False, None, "deps", FailureType.DEPENDENCY_RESOLUTION, ["lib"]),
            (True, build.jar, "built", FailureType.NONE, []),
        ]
        build.validate_jar.return_value = (False, None, None, "bad")
        result = pipeline.clone_and_compile(REPO)
        assert result.failure_type == FailureType.VALIDATION_ERROR

    def test_unexpected_exception_becomes_a_failed_result(self, pipeline):
        pipeline.github.get_repo_info.side_effect = RuntimeError("kaboom")
        result = pipeline.clone_and_compile(REPO)
        assert not result.success and result.error == "Unexpected error: kaboom"


# ---------------------------------------------------------------------------
# process_repos
# ---------------------------------------------------------------------------

class TestSourceOrder:
    """clone_and_compile walks SOURCE_ORDER: official, official_source (the repo's own
    branches), older_official, fork. These pin the order and the failure kept at the end."""

    OLDER = {"version_number": "6.0.9", "version_type": "release",
             "game_versions": ["1.21.9"], "dependencies": [],
             "files": [{"primary": True, "url": "https://cdn/old.jar",
                        "filename": "old.jar"}]}

    def exact_branch_fails(self, pipeline, build, failure=FailureType.BUILD_ERROR):
        bs = [branch("main")]
        # upstream's branches, then (when a fork is checked) the fork's own commit on main
        pipeline.github.get_branches.side_effect = [bs, [branch("main", sha="fork-own")]]
        pipeline.validator.pre_validate_branches.return_value = bs
        build.compile_mod.return_value = (False, None, "gradle broke", failure, ["dep"])

    def older_build_available(self, pipeline):
        pipeline.modrinth.find_project_by_repo.return_value = {
            "project_id": "p", "slug": "mod", "title": "Mod"}
        pipeline.modrinth.project_versions.return_value = [self.OLDER]
        return [
            patch("modkeel.sources._download",
                  lambda url, dest: dest.write_bytes(b"PK" + b"x" * 20_000)),
            patch("modkeel.sources.validate_jar", return_value=(True, "mod", "6.0.9", "ok")),
            patch("modkeel.sources._linkage_rejection", return_value=None),
        ]

    def test_author_branch_failure_tries_older_official_before_forks(self, pipeline, build):
        self.exact_branch_fails(pipeline, build)
        patches = self.older_build_available(pipeline)
        for p_ in patches:
            p_.start()
        try:
            result = pipeline.clone_and_compile(REPO)
        finally:
            for p_ in patches:
                p_.stop()
        assert result.success and result.modrinth_download
        assert result.source == "older_official"
        assert result.compiled_mc_version == "1.21.9"
        assert "Built for MC 1.21.9" in result.caveat
        assert (pipeline.config.output_dir / "old.jar").exists()
        pipeline.github.search_compatible_repos.assert_not_called()
        assert [line[:1] for line in result.trail] == ["✗", "✗", "✓"]
        assert result.trail[1].startswith("✗ Author's branch: owner/mod branch main")

    def test_strict_never_uses_an_older_build(self, pipeline, build):
        pipeline.config.strict_version = True
        self.exact_branch_fails(pipeline, build)
        pipeline.clone_and_compile(REPO)
        pipeline.modrinth.find_project_by_repo.assert_not_called()
        pipeline.github.search_compatible_repos.assert_called_once()

    def test_mavenlocal_pass_never_uses_an_older_build(self, pipeline, build):
        self.exact_branch_fails(pipeline, build)
        pipeline.clone_and_compile(REPO, skip_modrinth=True)
        pipeline.modrinth.find_project_by_repo.assert_not_called()

    def test_forks_are_searched_after_the_authors_branch_fails(self, pipeline, build):
        self.exact_branch_fails(pipeline, build)
        pipeline.modrinth.find_project_by_repo.return_value = None
        pipeline.github.search_compatible_repos.return_value = [
            {"fork": {"owner": "alice", "repo": "mod", "full_name": "alice/mod",
                      "url": "https://github.com/alice/mod"}, "score": 80, "signals": []}]
        pipeline.validator.analyze_fork_diff.return_value = (True, 0, "clean")
        build.compile_mod.side_effect = [
            (False, None, "gradle broke", FailureType.BUILD_ERROR, []),
            (True, build.jar, "built", FailureType.NONE, []),
        ]
        result = pipeline.clone_and_compile(REPO)
        assert result.success and result.source == "fork"
        assert pipeline.github.search_compatible_repos.call_args.args[:2] == ("owner", "mod")

    def test_dependency_failure_is_kept_for_the_mavenlocal_retry(self, pipeline, build):
        self.exact_branch_fails(pipeline, build, FailureType.DEPENDENCY_RESOLUTION)
        pipeline.modrinth.find_project_by_repo.return_value = None
        result = pipeline.clone_and_compile(REPO)
        assert not result.success
        assert result.failure_type == FailureType.DEPENDENCY_RESOLUTION
        assert result.missing_dependencies == ["dep"]
        assert any(line.startswith("✗ Community fork") for line in result.trail)

    def test_report_shows_source_and_what_was_tried(self, pipeline):
        ok = CompilationResult(repo_url="r1", success=True, mod_name="A", mod_version="1",
                               compiled_mc_version="1.21.9", jar_path="out/a.jar")
        ok.source, ok.caveat = "older_official", "Built for MC 1.21.9."
        bad = CompilationResult(repo_url="r2", success=False, error="nothing")
        bad.trail = ["✗ Official build: no Modrinth build", "✗ Community fork: none"]
        pipeline.results = [ok, bad]
        report = pipeline.generate_report()
        assert "Source: Older official build" in report
        assert "Built for MC 1.21.9." in report
        assert "Tried:\n      ✗ Official build: no Modrinth build" in report

    def test_report_note_after_a_passed_docker_test(self, pipeline):
        ok = CompilationResult(repo_url="r1", success=True, mod_name="A", mod_version="1",
                               compiled_mc_version="1.21.9", jar_path="out/a.jar")
        ok.source = "older_official"
        ok.caveat = "Built for MC 1.21.9. Its metadata allows 1.21.10, test it in game."
        ok.docker_tested, ok.docker_test_passed = True, True
        pipeline.results = [ok]
        report = pipeline.generate_report()
        assert "Built for MC 1.21.9. A headless MC 1.21.10 server booted with it" in report
        assert "Branch: None" not in report


class TestForkCopies:
    """Forks whose branches only copy upstream are skipped before pre-validation."""

    FORK = {"fork": {"owner": "alice", "repo": "mod-1.21.10", "full_name": "alice/mod-1.21.10",
                     "url": "https://github.com/alice/mod-1.21.10"},
            "score": 90, "signals": [], "trust_score": 85}

    def test_unchanged_copy_is_skipped_and_reported(self, pipeline, build):
        upstream = [branch("old", mc="1.21.9")]
        pipeline.github.get_branches.side_effect = [upstream, [branch("old", mc="1.21.9")]]
        pipeline.validator.pre_validate_branches.return_value = upstream
        pipeline.github.search_compatible_repos.return_value = [self.FORK]
        pipeline.modrinth.find_project_by_repo.return_value = None

        pipeline.clone_and_compile(REPO, skip_modrinth=True)

        # upstream pre-validated once; the copy never is (same SHA: no compare call either)
        assert pipeline.validator.pre_validate_branches.call_count == 1
        pipeline.github.commits_ahead.assert_not_called()
        assert pipeline.fork_copies == ["alice/mod-1.21.10"]

    def test_never_pushed_fork_is_skipped_before_listing_branches(self, pipeline, build):
        """NetworkArchitect-sudo/Create_1.21.10 kept a branch upstream deleted: no SHA to
        match, but nobody ever pushed to the fork, so its branches are never listed."""
        fork = {**self.FORK, "fork": {**self.FORK["fork"],
                                      "created_at": "2026-03-01T10:00:00Z",
                                      "pushed_at": "2025-12-07T09:00:00Z"}}
        pipeline.github.get_branches.return_value = [branch("old", mc="1.21.9")]
        pipeline.validator.pre_validate_branches.return_value = [branch("old", mc="1.21.9")]
        pipeline.github.search_compatible_repos.return_value = [fork]
        pipeline.modrinth.find_project_by_repo.return_value = None

        pipeline.clone_and_compile(REPO, skip_modrinth=True)

        assert pipeline.github.get_branches.call_count == 1   # upstream only
        assert pipeline.fork_copies == ["alice/mod-1.21.10"]

    def test_copy_is_named_in_the_trail(self, pipeline, build):
        pipeline.github.get_branches.side_effect = [[branch("main")], [branch("main")]]
        pipeline.validator.pre_validate_branches.return_value = []
        pipeline.github.search_compatible_repos.return_value = [self.FORK]
        pipeline.modrinth.find_project_by_repo.return_value = None

        result = pipeline.clone_and_compile(REPO, skip_modrinth=True)

        assert not result.success
        assert any("1 fork named for it was an unchanged copy of owner/mod" in line
                   for line in result.trail)


class TestProcessRepos:
    def test_dependency_failures_get_a_mavenlocal_second_pass(self, pipeline, tmp_path):
        fail = CompilationResult(repo_url="r1", success=False,
                                 failure_type=FailureType.DEPENDENCY_RESOLUTION)
        ok = CompilationResult(repo_url="r1", success=True)
        calls = []

        def fake(url, extra_gradle_args=None, skip_modrinth=False, **_):
            calls.append((url, extra_gradle_args))
            return fail if len(calls) == 1 else ok

        with patch.object(Pipeline, "clone_and_compile", side_effect=fake), \
                patch("modkeel.pipeline.time.sleep"), \
                patch("modkeel.pipeline.publish_to_maven_local"):
            pipeline.process_repos(["r1"])

        assert calls[0] == ("r1", None)
        assert calls[1][0] == "r1" and calls[1][1][0] == "--init-script"
        assert [r.success for r in pipeline.results] == [True]
        assert not Path(pipeline.temp_dir).exists()

    def test_successful_clone_is_published_to_maven_local(self, pipeline, tmp_path):
        clone = tmp_path / "clone"
        clone.mkdir()
        ok = CompilationResult(repo_url="r", success=True, clone_dir=clone)
        with patch.object(Pipeline, "clone_and_compile", return_value=ok), \
                patch("modkeel.pipeline.time.sleep"), \
                patch("modkeel.pipeline.publish_to_maven_local") as publish:
            pipeline.process_repos(["r"])
        publish.assert_called_once_with(clone)

    def test_crash_in_one_repo_does_not_stop_the_rest(self, pipeline):
        ok = CompilationResult(repo_url="b", success=True)
        with patch.object(Pipeline, "clone_and_compile",
                          side_effect=[RuntimeError("x"), ok]), \
                patch("modkeel.pipeline.time.sleep"):
            pipeline.process_repos(["a", "b"])
        assert [r.success for r in pipeline.results] == [False, True]
        assert pipeline.results[0].error == "Unhandled error: x"

    def test_cross_loader_results_download_bridge_mods(self, pipeline):
        ok = CompilationResult(repo_url="r", success=True, is_cross_loader=True)
        with patch.object(Pipeline, "clone_and_compile", return_value=ok), \
                patch("modkeel.pipeline.time.sleep"):
            pipeline.process_repos(["r"])
        slugs = [c.args[0] for c in pipeline.modrinth.download_modrinth_mod.call_args_list]
        assert slugs == ["connector", "forgified-fabric-api"]

    def test_docker_test_runs_when_enabled(self, pipeline):
        pipeline.config.docker_test = True
        ok = CompilationResult(repo_url="r", success=True)
        with patch.object(Pipeline, "clone_and_compile", return_value=ok), \
                patch("modkeel.pipeline.time.sleep"):
            pipeline.process_repos(["r"])
        pipeline.docker.test_mods_in_docker.assert_called_once_with(pipeline.results)


# ---------------------------------------------------------------------------
# generate_report
# ---------------------------------------------------------------------------

class TestReport:
    def test_report_sections(self, pipeline):
        pipeline.results = [
            CompilationResult(repo_url="ok", success=True, branch="main", mod_name="A",
                              mod_version="1", compiled_mc_version="1.21.10", jar_path="a.jar"),
            CompilationResult(repo_url="close", success=True, branch="b", mod_name="B",
                              mod_version="2", compiled_mc_version="1.21.9"),
            CompilationResult(repo_url="bad", success=False, error="nope",
                              failure_type=FailureType.DEPENDENCY_RESOLUTION,
                              missing_dependencies=["lib"]),
        ]
        report = pipeline.generate_report()
        assert "✅ Successful: 2/3" in report
        assert "❌ Failed: 1/3" in report
        assert "Version: 1.21.10 (exact match)" in report
        assert "Built for 1.21.9 (you're using 1.21.10)" in report
        assert "Unresolved dependencies" in report and "      - lib" in report
        assert "Target: Minecraft 1.21.10 with Neoforge 64" in report
        assert "Mode: LENIENT" in report

    def test_report_without_results(self, pipeline):
        report = pipeline.generate_report()
        assert "✅ Successful: 0/0" in report and "FAILED COMPILATIONS" not in report
