"""Characterization tests for the Typer CLI (modkeel.cli).

Each command runs through Typer's CliRunner with its collaborators replaced at the CLASS level
(ModrinthClient.check_modrinth, Pipeline.process_repos, ...). Patching classes instead of
names in modkeel.cli keeps these tests valid when commands move to other modules: what they
pin is the command's behavior (arguments, exit codes, what it tells the user, what it asks
the pipeline to do), not where the code lives.

Under CliRunner stdout is not a terminal, so every interactive prompt is skipped, which is
the non-interactive behavior scripts and CI rely on.
"""

import sys
from pathlib import Path
from unittest.mock import patch

import pytest
from typer.testing import CliRunner

from modkeel.cli import app
from modkeel.config import ModkeelConfig
from modkeel.evidence import PASSED, Outcome
from modkeel.github import GitHubClient
from modkeel.models import BranchCandidate, CompilationResult
from modkeel.modrinth import ModrinthClient
from modkeel.pipeline import Pipeline
from modkeel.validation import BranchValidator

runner = CliRunner()

MODRINTH_HIT = {
    "slug": "jei", "title": "Just Enough Items", "version_number": "19.0",
    "version_type": "release", "file_size": 2 * 1024 * 1024, "downloads": 1234,
    "filename": "jei.jar", "required_deps": ["dep"], "download_url": "u",
}

LINKAGE_OK = Outcome("linkage", PASSED, "ok")
MIXINS_OK = Outcome("mixins", PASSED, "ok")


def set_everywhere(monkeypatch, name, value):
    """Replace a module-level name in every loaded modkeel module that defines it."""
    for mod_name, mod in list(sys.modules.items()):
        if mod_name.startswith("modkeel") and mod is not None and hasattr(mod, name):
            monkeypatch.setattr(mod, name, value)


@pytest.fixture(autouse=True)
def isolated(monkeypatch, tmp_path):
    """Config, caches and output go to tmp_path; the console is wide enough not to wrap."""
    home = tmp_path / "home"
    home.mkdir()
    set_everywhere(monkeypatch, "MODKEEL_HOME", home)
    monkeypatch.setattr(ModkeelConfig, "CONFIG_DIR", home)
    monkeypatch.setattr(ModkeelConfig, "CONFIG_FILE", home / "config.toml")
    monkeypatch.setattr(ModkeelConfig, "LEGACY_FILE", tmp_path / "legacy.toml")
    for mod_name, mod in list(sys.modules.items()):
        console = getattr(mod, "console", None) if mod_name.startswith("modkeel") else None
        if console is not None and hasattr(console, "width"):
            monkeypatch.setattr(console, "width", 200)
    monkeypatch.chdir(tmp_path)
    return home


def invoke(*args):
    return runner.invoke(app, list(args))


def fork(name: str, score: int = 80) -> dict:
    owner, repo = name.split("/")
    return {"fork": {"owner": owner, "repo": repo, "full_name": name,
                     "url": f"https://github.com/{name}"}, "score": score, "signals": []}


def candidate(name: str = "port", mc: str = "1.21.10") -> BranchCandidate:
    b = BranchCandidate(name, "sha", "2026-01-01T00:00:00Z")
    b.minecraft_version, b.loader = mc, "neoforge"
    return b


# ---------------------------------------------------------------------------
# global
# ---------------------------------------------------------------------------

def test_version_flag():
    result = invoke("--version")
    assert result.exit_code == 0
    assert "Modkeel v" in result.output and "Compile the mods Mojang left behind" in result.output


# ---------------------------------------------------------------------------
# compile
# ---------------------------------------------------------------------------

class TestCompile:
    @pytest.fixture
    def repos(self, tmp_path):
        f = tmp_path / "repos.txt"
        f.write_text("# comment\nhttps://github.com/a/b\n\nhttps://github.com/c/d\n")
        return f

    @pytest.fixture
    def pipeline_run(self):
        """Record what the command asks the pipeline to do; every repo succeeds."""
        seen = {}

        def process(self, urls):
            seen["config"], seen["urls"] = self.config, urls
            self.results = [CompilationResult(repo_url=u, success=True) for u in urls]

        with patch.object(Pipeline, "process_repos", process), \
                patch.object(Pipeline, "generate_report", return_value="THE REPORT"), \
                patch("modkeel.crowdsource.submit_reports") as submit:
            seen["submit"] = submit
            yield seen

    def test_passes_options_to_the_pipeline(self, repos, pipeline_run):
        result = invoke("compile", str(repos), "-m", "1.21.10", "-l", "NeoForge", "-lv", "64",
                        "-t", "tok", "--strict", "--no-cross-loader", "--docker-test",
                        "--no-prebuild-gate", "--no-prebuilt", "--no-symbol-check")
        assert result.exit_code == 0, result.output
        cfg = pipeline_run["config"]
        assert pipeline_run["urls"] == ["https://github.com/a/b", "https://github.com/c/d"]
        assert (cfg.mc_version, cfg.loader, cfg.loader_version) == ("1.21.10", "neoforge", "64")
        assert cfg.github_token == "tok" and cfg.strict_version and cfg.docker_test
        assert not (cfg.cross_loader or cfg.prebuild_gate or cfg.use_prebuilt
                    or cfg.symbol_check)
        assert "THE REPORT" in result.output

    def test_defaults(self, repos, pipeline_run):
        result = invoke("compile", str(repos), "-m", "1.21.10", "-l", "fabric", "-lv", "0.16")
        assert result.exit_code == 0, result.output
        cfg = pipeline_run["config"]
        assert cfg.cross_loader and cfg.prebuild_gate and cfg.use_prebuilt and cfg.symbol_check
        assert not cfg.strict_version and not cfg.docker_test and cfg.github_token is None
        assert cfg.output_dir == Path("out")

    def test_token_flag_is_saved_for_next_time(self, repos, pipeline_run, isolated):
        invoke("compile", str(repos), "-m", "1.21.10", "-l", "neoforge", "-lv", "64",
               "-t", "ghp_saved")
        assert ModkeelConfig().github_token == "ghp_saved"

    def test_report_file_and_sharing(self, repos, pipeline_run, tmp_path):
        out = tmp_path / "report.txt"
        invoke("compile", str(repos), "-m", "1.21.10", "-l", "neoforge", "-lv", "64",
               "--output-report", str(out))
        assert out.read_text() == "THE REPORT"
        pipeline_run["submit"].assert_called_once()

    def test_no_share_skips_submission(self, repos, pipeline_run):
        invoke("compile", str(repos), "-m", "1.21.10", "-l", "neoforge", "-lv", "64",
               "--no-share")
        pipeline_run["submit"].assert_not_called()

    def test_invalid_loader(self, repos):
        result = invoke("compile", str(repos), "-m", "1.21.10", "-l", "rift", "-lv", "1")
        assert result.exit_code == 1 and "Invalid loader 'rift'" in result.output

    def test_empty_repo_list(self, tmp_path):
        empty = tmp_path / "empty.txt"
        empty.write_text("# only comments\n")
        result = invoke("compile", str(empty), "-m", "1.21.10", "-l", "neoforge", "-lv", "64")
        assert result.exit_code == 1 and "No repository URLs found" in result.output

    def test_missing_instance_is_a_configuration_error(self, repos):
        result = invoke("compile", str(repos), "-m", "1.21.10", "-l", "neoforge", "-lv", "64",
                        "-i", "/does/not/exist")
        assert result.exit_code == 1 and "Configuration error" in result.output

    def test_all_failed_exits_1(self, repos):
        def process(self, urls):
            self.results = [CompilationResult(repo_url=u, success=False) for u in urls]
        with patch.object(Pipeline, "process_repos", process), \
                patch.object(Pipeline, "generate_report", return_value=""), \
                patch("modkeel.crowdsource.submit_reports"):
            result = invoke("compile", str(repos), "-m", "1.21.10", "-l", "neoforge",
                            "-lv", "64")
        assert result.exit_code == 1


# ---------------------------------------------------------------------------
# search / get: both go through the resolver (modkeel/resolve.py, modkeel/sources.py).
# Modrinth is replaced at the ModrinthClient methods the sources call.
# ---------------------------------------------------------------------------

JEI = {"project_id": "jei-id", "slug": "jei", "title": "Just Enough Items", "downloads": 1234}
CREATE = {"project_id": "create-id", "slug": "create", "title": "Create",
          "downloads": 27_000_000}
TFMG = {"project_id": "tfmg-id", "slug": "create-tfmg",
        "title": "Create: The Factory Must Grow"}


def mr_version(number: str, mcs, filename: str = "jei.jar", deps=("dep",)) -> dict:
    return {"version_number": number, "version_type": "release", "game_versions": list(mcs),
            "files": [{"primary": True, "url": f"https://cdn/{filename}",
                       "filename": filename, "size": 2 * 1024 * 1024}],
            "dependencies": [{"dependency_type": "required", "project_id": d} for d in deps]}


def modrinth(project=JEI, related=(), exact=(), all_versions=(), error=None,
             source_url=None):
    """Patch the Modrinth calls of identify_mod and the official/older sources.

    exact: versions for the target game version; all_versions: every version (older_official).
    """
    def find_project(self, query):
        self.last_error = error
        return (None if error else project), list(related)

    def project_versions(self, project_id, loader, game_version=None):
        self.last_error = None
        return list(exact) if game_version else list(all_versions)

    return [
        patch.object(ModrinthClient, "find_project", find_project),
        patch.object(ModrinthClient, "fetch_project",
                     return_value={"source_url": source_url} if source_url else {}),
        patch.object(ModrinthClient, "project_versions", project_versions),
    ]


class patched:
    """Enter several patch() context managers at once (and expose their mocks by name)."""

    def __init__(self, *patches, **named):
        self.patches = list(patches) + list(named.values())
        self.names = list(named)

    def __enter__(self):
        entered = [p.__enter__() for p in self.patches]
        return dict(zip(self.names, entered[len(entered) - len(self.names):]))

    def __exit__(self, *exc):
        for p in reversed(self.patches):
            p.__exit__(*exc)


def fake_download(url, dest):
    dest.write_bytes(b"PK" + b"x" * 20_000)


class TestSearch:
    def test_official_build_shows_it_and_skips_github(self):
        with patched(*modrinth(exact=[mr_version("19.0", ["1.21.1"])]),
                     gh=patch.object(GitHubClient, "search_compatible_repos")) as m:
            result = invoke("search", "JEI", "-m", "1.21.1", "-t", "tok")
        assert result.exit_code == 0
        for text in ("Just Enough Items", "19.0", "2.0 MB", "1,234", "jei.jar"):
            assert text in result.output
        m["gh"].assert_not_called()

    def test_not_found_without_token_gives_a_tip(self):
        with patched(*modrinth(project=None)):
            result = invoke("search", "Nope", "-m", "1.21.1")
        assert result.exit_code == 0
        assert "Not found on Modrinth" in result.output
        assert "modkeel token --set TOKEN" in result.output

    def test_modrinth_unavailable_is_not_reported_as_not_found(self):
        with patched(*modrinth(error="timed out")):
            result = invoke("search", "JEI", "-m", "1.21.1")
        assert "Modrinth unavailable (timed out)" in result.output
        assert "Not found on Modrinth" not in result.output

    def test_forks_are_prefiltered_with_a_token(self):
        with patched(*modrinth(project=None),
                     patch.object(GitHubClient, "search_compatible_repos",
                                  return_value=[fork("alice/jei"), fork("bob/jei")]),
                     patch.object(GitHubClient, "get_branches",
                                  side_effect=[[candidate()], []]),
                     patch.object(BranchValidator, "pre_validate_branches",
                                  return_value=[candidate("port-1.21.1", mc="1.21.1")]),
                     patch.object(BranchValidator, "score_branch", return_value=5)):
            result = invoke("search", "JEI", "-m", "1.21.1", "-t", "tok")
        assert result.exit_code == 0, result.output
        assert "(1 of 2 passed)" in result.output
        assert "alice/jei" in result.output and "port-1.21.1" in result.output
        assert "bob/jei" not in result.output

    def test_no_forks(self):
        with patched(*modrinth(project=None),
                     patch.object(GitHubClient, "search_compatible_repos", return_value=[])):
            result = invoke("search", "JEI", "-m", "1.21.1", "-t", "tok")
        assert "No GitHub forks found" in result.output

    def test_forks_of_the_mods_own_repo(self):
        """When Modrinth names the source repo, forks of that repo are searched."""
        with patched(*modrinth(project=CREATE,
                               source_url="https://github.com/Creators-of-Create/Create"),
                     gh=patch.object(GitHubClient, "search_compatible_repos",
                                     return_value=[])) as m:
            invoke("search", "Create", "-m", "1.21.10", "-t", "tok")
        m["gh"].assert_called_once_with("Creators-of-Create", "Create", False)

    def test_addon_is_never_the_result(self):
        """Create has no 1.21.10 build; its addon does. The addon is listed, not offered."""
        with patched(*modrinth(project=CREATE, related=[TFMG],
                               all_versions=[mr_version("6.0.6", ["1.21.1"])])):
            result = invoke("search", "Create", "-m", "1.21.10")
        assert "Create has no NeoForge build for MC 1.21.10" in result.output
        assert "6.0.6 for MC 1.21.1" in result.output       # older build, to verify
        assert "Related on Modrinth (not Create)" in result.output
        assert "Create: The Factory Must Grow" in result.output


class TestGet:
    def test_official_download(self):
        with patched(*modrinth(exact=[mr_version("19.0", ["1.21.1"])]),
                     patch("modkeel.sources._download", fake_download),
                     deps=patch.object(ModrinthClient, "download_modrinth_deps",
                                       return_value=[])) as m:
            result = invoke("get", "JEI", "-m", "1.21.1")
        assert result.exit_code == 0, result.output
        assert "Done! Just Enough Items v19.0 downloaded to out/" in result.output
        assert Path("out/jei.jar").exists()
        m["deps"].assert_called_once_with({"required_deps": ["dep"]})

    def test_not_on_modrinth_and_no_token(self):
        with patched(*modrinth(project=None)):
            result = invoke("get", "JEI", "-m", "1.21.1")
        assert result.exit_code == 1
        assert "Not found on Modrinth" in result.output
        assert "modkeel token --set" in result.output

    def test_modrinth_unavailable_and_no_token(self):
        with patched(*modrinth(error="HTTP 503")):
            result = invoke("get", "JEI", "-m", "1.21.1")
        assert result.exit_code == 1 and "Modrinth unavailable (HTTP 503)" in result.output

    def test_nothing_anywhere(self):
        with patched(*modrinth(project=None),
                     patch.object(GitHubClient, "search_compatible_repos", return_value=[])):
            result = invoke("get", "JEI", "-m", "1.21.1", "-t", "tok")
        assert result.exit_code == 1
        assert "No GitHub forks found" in result.output
        assert "No build of JEI for MC 1.21.1 + Neoforge found." in result.output

    def test_fork_found_but_no_loader_version(self):
        with patched(*modrinth(project=None),
                     patch.object(GitHubClient, "search_compatible_repos",
                                  return_value=[fork("alice/jei")]),
                     patch.object(GitHubClient, "get_branches", return_value=[candidate()]),
                     patch.object(BranchValidator, "pre_validate_branches",
                                  return_value=[candidate()]),
                     patch.object(BranchValidator, "score_branch", return_value=1)):
            result = invoke("get", "JEI", "-m", "1.21.10", "-t", "tok")
        assert result.exit_code == 1
        assert "Best fork: alice/jei branch port" in result.output
        assert "-lv LOADER_VERSION" in result.output

    @pytest.mark.parametrize("success", [True, False])
    def test_compiles_the_best_fork(self, success):
        seen = {}

        def clone(self, url, specific_branch=None, extra_gradle_args=None,
                  skip_modrinth=False):
            seen.update(url=url, branch=specific_branch, skip=skip_modrinth,
                        temp=self.temp_dir)
            return CompilationResult(repo_url=url, success=success, mod_name="JEI",
                                     mod_version="1", error="gradle broke")

        with patched(*modrinth(project=None),
                     patch.object(GitHubClient, "search_compatible_repos",
                                  return_value=[fork("alice/jei")]),
                     patch.object(GitHubClient, "get_branches", return_value=[candidate()]),
                     patch.object(BranchValidator, "pre_validate_branches",
                                  return_value=[candidate()]),
                     patch.object(BranchValidator, "score_branch", return_value=1),
                     patch.object(Pipeline, "clone_and_compile", clone)):
            result = invoke("get", "JEI", "-m", "1.21.10", "-lv", "64", "-t", "tok")

        assert seen["url"] == "https://github.com/alice/jei"
        assert seen["branch"] == "port" and seen["skip"] is True
        assert not Path(seen["temp"]).exists()  # temp dir cleaned up either way
        if success:
            assert result.exit_code == 0 and "Done! JEI v1 compiled to out/" in result.output
        else:
            assert result.exit_code == 1
            assert "compilation failed: gradle broke" in result.output

    def test_addon_is_never_downloaded(self):
        """Regression: `get Create -m 1.21.10` used to download Create: TFMG."""
        downloads = []
        with patched(*modrinth(project=CREATE, related=[TFMG],
                               all_versions=[mr_version("6.0.6", ["1.21.1"],
                                                        filename="create.jar")]),
                     patch("modkeel.sources._download",
                           lambda url, dest: downloads.append(url) or fake_download(url, dest)),
                     patch("modkeel.sources.validate_jar",
                           return_value=(False, "create", "6.0.6",
                                         "JAR declares incompatible MC version: [1.21.1]")),
                     patch("modkeel.evidence.validate_jar",
                           return_value=(False, "create", "6.0.6",
                                         "JAR declares incompatible MC version: [1.21.1]"))):
            result = invoke("get", "Create", "-m", "1.21.10")
        assert result.exit_code == 1
        assert downloads == ["https://cdn/create.jar"]   # checked, never the addon
        assert "its metadata only allows MC: [1.21.1]" in result.output
        assert "Related on Modrinth (not Create)" in result.output
        assert not list(Path("out").glob("*.jar"))

    def test_older_build_that_runs_is_delivered_with_a_note(self):
        with patched(*modrinth(project=CREATE,
                               all_versions=[mr_version("6.0.9", ["1.21.9"],
                                                        filename="create.jar", deps=())]),
                     patch("modkeel.sources._download", fake_download),
                     patch("modkeel.sources.validate_jar",
                           return_value=(True, "create", "6.0.9", "ok")),
                     patch("modkeel.evidence.validate_jar",
                           return_value=(True, "create", "6.0.9", "ok")),
                     patch("modkeel.evidence.check_linkage", return_value=LINKAGE_OK),
                     patch("modkeel.evidence.check_mixins", return_value=MIXINS_OK)):
            result = invoke("get", "Create", "-m", "1.21.10")
        assert result.exit_code == 0, result.output
        assert "Older official build" in result.output
        assert "Built for MC 1.21.9" in result.output
        assert "Evidence: metadata ✓ · linkage ✓ · mixins ✓ · server boot not run" in result.output
        assert "Done! Create v6.0.9 downloaded to out/" in result.output
        assert Path("out/create.jar").exists()

    def test_docker_test_covers_any_delivered_jar(self):
        seen = {}

        def docker(self, results):
            seen["jars"] = [Path(r.jar_path).name for r in results]
            results[0].docker_test_passed = True

        with patched(*modrinth(exact=[mr_version("19.0", ["1.21.1"])]),
                     patch("modkeel.sources._download", fake_download),
                     patch.object(ModrinthClient, "download_modrinth_deps",
                                  return_value=[Path("out/dep.jar")]),
                     patch("modkeel.docker.DockerTester.check_docker_available", return_value=True),
                     patch("modkeel.docker.DockerTester.test_mods_in_docker", docker)):
            result = invoke("get", "JEI", "-m", "1.21.1", "--docker-test")
        assert result.exit_code == 0, result.output
        assert seen["jars"] == ["jei.jar", "dep.jar"]

    def test_note_reflects_a_passed_docker_test(self):
        def docker(self, results):
            results[0].docker_test_passed = True

        with patched(*modrinth(project=CREATE,
                               all_versions=[mr_version("6.0.9", ["1.21.9"],
                                                        filename="create.jar", deps=())]),
                     patch("modkeel.sources._download", fake_download),
                     patch("modkeel.sources.validate_jar",
                           return_value=(True, "create", "6.0.9", "ok")),
                     patch("modkeel.evidence.validate_jar",
                           return_value=(True, "create", "6.0.9", "ok")),
                     patch("modkeel.evidence.check_linkage", return_value=LINKAGE_OK),
                     patch("modkeel.evidence.check_mixins", return_value=MIXINS_OK),
                     patch("modkeel.docker.DockerTester.check_docker_available", return_value=True),
                     patch("modkeel.docker.DockerTester.test_mods_in_docker", docker)):
            result = invoke("get", "Create", "-m", "1.21.10", "--docker-test")
        assert "Built for MC 1.21.9. A headless MC 1.21.10 server booted with it" in result.output
        assert "before relying on it" not in result.output
        assert "Evidence: metadata ✓ · linkage ✓ · mixins ✓ · server boot ✓" in result.output

    def test_invalid_loader(self):
        result = invoke("get", "JEI", "-m", "1.21.1", "-l", "rift")
        assert result.exit_code == 1 and "Invalid loader 'rift'" in result.output


# ---------------------------------------------------------------------------
# token / status / recommend
# ---------------------------------------------------------------------------

class TestToken:
    def test_lifecycle(self):
        assert "No GitHub token saved." in invoke("token").output
        assert "GitHub token saved." in invoke("token", "--set", "ghp_abcdefghij").output
        masked = invoke("token").output
        assert "ghp_****ghij" in masked and "ghp_abcdefghij" not in masked
        assert "ghp_abcdefghij" in invoke("token", "--show").output
        assert "GitHub token removed." in invoke("token", "--clear").output
        assert "No GitHub token saved." in invoke("token").output

    def test_short_token_is_fully_masked(self):
        invoke("token", "--set", "short")
        assert "GitHub token: ****" in invoke("token").output


class TestStatus:
    def test_fresh_install(self):
        result = invoke("status")
        assert result.exit_code == 0
        assert "not created" in result.output
        assert "No cached loaders found." in result.output
        assert "Known NeoForge Versions" in result.output

    def test_with_config_and_cached_loader(self, isolated):
        invoke("token", "--set", "ghp_abcdefghij")
        (isolated / "loaders" / "neoforge" / "1.21.1").mkdir(parents=True)
        (isolated / "loaders" / "neoforge" / "1.21.1" / "run.sh").write_text("")
        result = invoke("status")
        assert "ghp_****ghij" in result.output
        assert "installed" in result.output

    def test_cached_loader_per_loader_version(self, isolated):
        install = isolated / "loaders" / "neoforge" / "1.21.10" / "21.10.64"
        install.mkdir(parents=True)
        (install / "run.sh").write_text("")
        (isolated / "loaders" / "neoforge" / "1.21.10" / "21.10.63").mkdir()
        result = invoke("status")
        assert "21.10.64" in result.output and "installed" in result.output
        assert "partial" in result.output

    def test_docker_test_cache_count(self, isolated):
        (isolated / "docker_test_cache.json").write_text('{"a": {}, "b": {}}')
        assert "2 entries" in invoke("status").output


class TestRecommend:
    def test_missing_directory(self, tmp_path):
        result = invoke("recommend", "-d", str(tmp_path / "nope"))
        assert result.exit_code == 1 and "Mods directory not found" in result.output

    def test_empty_directory(self, tmp_path):
        (tmp_path / "mods").mkdir()
        result = invoke("recommend", "-d", str(tmp_path / "mods"))
        assert result.exit_code == 0 and "No mod JARs found" in result.output

    def test_no_autodetected_folder(self):
        with patch("modkeel.scanner.detect_mods_folder", return_value=None):
            result = invoke("recommend")
        assert result.exit_code == 1 and "Could not auto-detect mods folder" in result.output
