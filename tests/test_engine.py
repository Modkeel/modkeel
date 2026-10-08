"""The engine's entry points without the CLI (modkeel/core/engine.py, IDEA-028 step 2).

What a front end sees: events instead of output, questions through `decide` with safe
defaults when nobody answers, a cancel hook between steps. The CLI's own behaviour on top of
these is covered in test_cli.
"""

import json
import sys
from pathlib import Path
from unittest.mock import patch

import pytest

from modkeel.core.decisions import Cancelled, ChangeTarget, NeedToken, safe_default
from modkeel.core.engine import GetRequest, get_mod, retarget_pack
from modkeel.core.events import ModIdentified, ModResolved, TargetSearch
from modkeel.models import CompilationResult, ModCompilerConfig
from modkeel.pipeline import Pipeline
from modkeel.modrinth import ModrinthClient
from tests.test_cli import CREATE, LINKAGE_OK, fake_download, modrinth, mr_version, patched


@pytest.fixture(autouse=True)
def isolated(monkeypatch, tmp_path):
    """Caches and output in tmp_path, as in test_cli."""
    home = tmp_path / "home"
    home.mkdir()
    for name, mod in list(sys.modules.items()):
        if name.startswith("modkeel") and mod is not None and hasattr(mod, "MODKEEL_HOME"):
            monkeypatch.setattr(mod, "MODKEEL_HOME", home)
    monkeypatch.chdir(tmp_path)


def by_version(builds):
    def project_versions(self, project_id, loader, game_version=None):
        self.last_error = None
        if game_version:
            return list(builds.get(game_version, []))
        return [v for vs in builds.values() for v in vs]
    return patch.object(ModrinthClient, "project_versions", project_versions)


def create_only_on_1_21_1():
    """Create has an official build for 1.21.1 only; it is refused on 1.21.10."""
    old = mr_version("6.0.6", ["1.21.1"], filename="create.jar", deps=())
    refused = (False, "create", "6.0.6", "JAR declares incompatible MC version: [1.21.1]")
    return patched(*modrinth(project=CREATE)[:2], by_version({"1.21.1": [old]}),
                   patch("modkeel.sources._download", fake_download),
                   patch("modkeel.sources.validate_jar", return_value=refused),
                   patch("modkeel.evidence.validate_jar", return_value=refused),
                   patch("modkeel.evidence.check_linkage", return_value=LINKAGE_OK),
                   patch("modkeel.relax.relax_jar", return_value=None))


REQUEST = GetRequest("Create", "1.21.10", "neoforge", instance=None)


class TestGetMod:
    def test_headless_run_keeps_the_version_and_prints_nothing(self, capsys):
        events, questions = [], []

        def decide(q):
            questions.append(q)
            return safe_default(q)

        with create_only_on_1_21_1():
            result = get_mod(REQUEST, events=events.append, decide=decide)

        assert result.delivered is None and not result.retargeted
        assert result.proposal.mc_version == "1.21.1"
        # forks were the last source: the token was asked for, then the version change
        assert [type(q) for q in questions] == [NeedToken, ChangeTarget]
        assert questions[1].current == "1.21.10" and questions[1].scope == "mod"
        kinds = [type(e) for e in events]
        assert kinds.index(ModIdentified) < kinds.index(ModResolved) < kinds.index(TargetSearch)
        assert capsys.readouterr().out == ""
        assert not Path("out/mc-1.21.1").exists()

    def test_an_accepted_proposal_runs_there_and_never_into_the_instance(self, tmp_path):
        instance = tmp_path / "inst"
        (instance / "mods").mkdir(parents=True)
        events = []
        request = GetRequest("Create", "1.21.10", "neoforge", loader_version="21.10.64",
                             instance=str(instance))
        with create_only_on_1_21_1():
            result = get_mod(request, events=events.append, decide=lambda q: True)

        assert result.retargeted and result.target == "1.21.1"
        assert result.output_dir == Path("out/mc-1.21.1")
        assert result.delivered.mod_version == "6.0.6"
        assert Path("out/mc-1.21.1/create.jar").exists()
        assert not (instance / "mods" / "create.jar").exists()
        resolved = [e for e in events if isinstance(e, ModResolved)]
        assert [(e.target, e.retarget, e.delivered is not None) for e in resolved] == [
            ("1.21.10", False, False), ("1.21.1", True, True)]

    def test_dependencies_on_the_new_version_come_for_it_and_land_with_it(self):
        """A retarget run fetches required dependencies for its own version and folder
        (they used to come for the version asked for, into the first run's folder)."""
        seen = []

        def deps(self, info):
            seen.append((self.config.mc_version, str(self.config.output_dir)))
            return []

        with create_only_on_1_21_1(), \
                patch.object(ModrinthClient, "download_modrinth_deps", deps):
            result = get_mod(REQUEST, events=lambda e: None, decide=lambda q: True)
        assert result.retargeted and seen == [("1.21.1", "out/mc-1.21.1")]

    def test_events_cross_a_process_boundary_as_json(self):
        events = []
        with create_only_on_1_21_1():
            get_mod(REQUEST, events=events.append, decide=lambda q: True)
        for e in events:
            assert json.loads(json.dumps(e.to_dict()))["kind"] == e.kind

    def test_the_token_is_asked_once_and_only_when_forks_are_needed(self):
        asked = []

        def decide(q):
            asked.append(q)
            return None if isinstance(q, NeedToken) else False

        # not on Modrinth at all: forks are the only source, and they need a token
        with patched(*modrinth(project=None)):
            result = get_mod(GetRequest("nothing", "1.21.10", "neoforge"),
                             events=lambda e: None, decide=decide)
        assert result.delivered is None
        assert [type(q) for q in asked] == [NeedToken]

    def test_an_official_build_never_asks_for_a_token(self):
        asked = []
        exact = [mr_version("6.0.8", ["1.21.10"], filename="create.jar", deps=())]
        with patched(*modrinth(project=CREATE, exact=exact),
                     patch("modkeel.sources._download", fake_download)):
            result = get_mod(REQUEST, events=lambda e: None, decide=asked.append)
        assert result.delivered and not asked

    def test_cancelled_before_resolving(self):
        with create_only_on_1_21_1(), pytest.raises(Cancelled):
            get_mod(REQUEST, events=lambda e: None, cancelled=lambda: True)


class TestRetargetPack:
    URLS = ["https://github.com/a/b", "https://github.com/c/d"]

    def run(self, decide, carry=None):
        runs = []

        def process(self, urls, resolved=None):
            runs.append((self.config.mc_version, resolved))
            self.results = [CompilationResult(repo_url=u, success=True) for u in urls]

        def by_repo(self, owner, repo):
            return {"project_id": f"{repo}-id", "slug": repo, "title": repo}

        def versions(self, project_id, loader, game_version=None):
            return [{"game_versions": ["1.21.1"]}]

        first = [CompilationResult(repo_url=u, success=False) for u in self.URLS]
        config = ModCompilerConfig(mc_version="1.21.10", loader="neoforge",
                                   loader_version="21.10.64", output_dir="out")
        events = []
        with patch.object(Pipeline, "process_repos", process), \
                patch.object(Pipeline, "generate_report", return_value="REPORT"), \
                patch.object(ModrinthClient, "find_project_by_repo", by_repo), \
                patch.object(ModrinthClient, "project_versions", versions), \
                patch("modkeel.target.carry_over", carry or (lambda *a, **k: {})):
            run = retarget_pack(self.URLS, first, config, events=events.append, decide=decide)
        return run, runs, events

    def test_kept_version_runs_nothing(self):
        run, runs, events = self.run(safe_default)
        assert run is None and runs == []
        assert any(isinstance(e, TargetSearch) and e.scope == "pack" for e in events)

    def test_accepted_version_reruns_the_list_there(self):
        seen = {}

        def carry(previous, config, first_target, resolve_again, events=None):
            seen.update(events=events, target=config.mc_version)
            return {}

        run, runs, _ = self.run(lambda q: isinstance(q, ChangeTarget), carry=carry)
        assert runs == [("1.21.1", {})]
        assert run.built == 2 and run.report == "REPORT"
        assert run.config.output_dir == Path("out/mc-1.21.1")
        assert run.config.loader_version == "0" and run.config.mods_path is None
        assert seen["target"] == "1.21.1" and seen["events"] is not None
