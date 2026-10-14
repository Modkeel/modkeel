"""Adding a mod into an instance and taking it back (modkeel/core/install.py)."""

import hashlib
import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from modkeel import constants
from modkeel.core import install as inst
from modkeel.core.decisions import ChangeTarget
from modkeel.core.install import AddRequest, InstallRecord, add_to_instance, history, undo
from modkeel.modrinth import ModrinthClient
from modkeel.resolve import Delivered
from tests.test_engine import isolated  # noqa: F401


def jar(folder: Path, name: str, body: str) -> Path:
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / name
    path.write_bytes(body.encode())
    return path


def sha1(body: str) -> str:
    return hashlib.sha1(body.encode()).hexdigest()


def lookup_of(projects):
    """versions_by_hash over {body: project_id}."""
    def lookup(hashes):
        return {sha1(b): {"project_id": p} for b, p in projects.items() if sha1(b) in hashes}
    return lookup


@pytest.fixture
def mods(tmp_path):
    folder = tmp_path / "instance" / "mods"
    folder.mkdir(parents=True)
    return folder


def record(mods):
    return InstallRecord("r1", 1.0, str(mods), "Voxy", "1.21.11", "fabric")


class TestInstall:
    def test_copies_new_files_and_skips_what_the_instance_has(self, mods, tmp_path):
        jar(mods, "fabric-api-0.100.jar", "api-old")
        jar(mods, "sodium.jar", "sodium")
        staged = [jar(tmp_path / "s", "voxy-1.0.jar", "voxy"),
                  jar(tmp_path / "s", "fabric-api-0.120.jar", "api-new"),
                  jar(tmp_path / "s", "sodium-copy.jar", "sodium")]
        rec = inst.install(staged, "voxy-1.0.jar", mods, record(mods),
                           lookup_of({"api-old": "P7dR8mSH", "api-new": "P7dR8mSH",
                                      "voxy": "voxy-id", "sodium": "AANobbMI"}))
        assert rec.added == [{"name": "voxy-1.0.jar", "sha1": sha1("voxy")}]
        assert sorted(rec.skipped) == ["fabric-api-0.120.jar", "sodium-copy.jar"]
        assert rec.replaced == []
        assert sorted(p.name for p in mods.iterdir()) == [
            "fabric-api-0.100.jar", "sodium.jar", "voxy-1.0.jar"]

    def test_the_mod_replaces_its_older_version_kept_for_undo(self, mods, tmp_path):
        jar(mods, "voxy-0.9.jar", "voxy-old")
        staged = [jar(tmp_path / "s", "voxy-1.0.jar", "voxy")]
        rec = inst.install(staged, "voxy-1.0.jar", mods, record(mods),
                           lookup_of({"voxy-old": "voxy-id", "voxy": "voxy-id"}))
        assert rec.replaced == ["voxy-0.9.jar"]
        assert [p.name for p in mods.iterdir()] == ["voxy-1.0.jar"]
        assert (constants.MODKEEL_HOME / "history" / "r1" / "voxy-0.9.jar").read_text() == \
            "voxy-old"

    def test_unknown_files_are_never_matched_by_project(self, mods, tmp_path):
        jar(mods, "a.jar", "a")
        staged = [jar(tmp_path / "s", "b.jar", "b")]
        rec = inst.install(staged, "b.jar", mods, record(mods), lambda hashes: None)
        assert [a["name"] for a in rec.added] == ["b.jar"] and rec.replaced == []


def staged_get(files, title="Voxy"):
    """get_mod that writes `files` ({name: body}) into the request's output folder."""
    def fake(request, events=None, decide=None, cancelled=None):
        out = Path(request.output_dir)
        for name, body in files.items():
            jar(out, name, body)
        main = out / next(iter(files)) if files else None
        return SimpleNamespace(
            mod=SimpleNamespace(title=title, project={"id": request.project_id}),
            target=request.mc_version, output_dir=out, retargeted=False, proposal=None,
            delivered=Delivered(main, title, "1.0") if main else None)
    return fake


def add(mods, **over):
    return AddRequest(**{"project_id": "voxy-id", "title": "Voxy", "mods_dir": str(mods),
                         "mc_version": "1.21.11", "loader": "fabric", **over})


class TestAddAndUndo:
    def test_add_records_and_undo_takes_back_exactly_that(self, mods):
        jar(mods, "voxy-0.9.jar", "voxy-old")
        jar(mods, "sodium.jar", "sodium")
        with patch("modkeel.core.engine.get_mod",
                   staged_get({"voxy-1.0.jar": "voxy", "dep.jar": "dep"})), \
             patch.object(ModrinthClient, "versions_by_hash",
                          lambda self, h: lookup_of({"voxy-old": "v", "voxy": "v"})(h)):
            result = add_to_instance(add(mods), events=lambda e: None)
        assert result.installed and result.reason == ""
        assert sorted(a["name"] for a in result.record.added) == ["dep.jar", "voxy-1.0.jar"]
        assert sorted(p.name for p in mods.iterdir()) == ["dep.jar", "sodium.jar",
                                                          "voxy-1.0.jar"]
        assert not (constants.MODKEEL_HOME / "staging").exists() or \
            not any((constants.MODKEEL_HOME / "staging").iterdir())
        [listed] = history()
        assert listed.id == result.record.id and not listed.undone

        done = undo(listed.id)
        assert done.undone and sorted(done.removed) == ["dep.jar", "voxy-1.0.jar"]
        assert done.restored == ["voxy-0.9.jar"]
        assert sorted(p.name for p in mods.iterdir()) == ["sodium.jar", "voxy-0.9.jar"]
        assert history()[0].undone
        assert undo(listed.id).reason == "already undone"

    def test_undo_leaves_a_file_the_player_changed(self, mods):
        with patch("modkeel.core.engine.get_mod", staged_get({"voxy-1.0.jar": "voxy"})), \
             patch.object(ModrinthClient, "versions_by_hash", lambda self, h: {}):
            result = add_to_instance(add(mods), events=lambda e: None)
        (mods / "voxy-1.0.jar").write_bytes(b"edited")
        done = undo(result.record.id)
        assert done.undone and done.kept == ["voxy-1.0.jar"] and done.removed == []
        assert (mods / "voxy-1.0.jar").exists()

    def test_no_build_installs_nothing_and_never_changes_the_version(self, mods):
        asked, nothing = [], staged_get({})

        def get(request, events=None, decide=None, cancelled=None):
            asked.append(decide(ChangeTarget(None, "1.21.10")))
            return nothing(request, events, decide, cancelled)

        with patch("modkeel.core.engine.get_mod", get):
            result = add_to_instance(add(mods), events=lambda e: None, decide=lambda q: True)
        assert asked == [False]
        assert not result.installed and "no build of Voxy for 1.21.11" in result.reason
        assert list(mods.iterdir()) == [] and history() == []

    def test_a_locked_folder_is_reported_and_the_partial_add_can_be_undone(self, mods):
        real_copy = inst.shutil.copy2
        calls = []

        def copy2(src, dst):
            calls.append(dst)
            if len(calls) == 2:
                raise PermissionError(13, "The process cannot access the file")
            return real_copy(src, dst)

        with patch("modkeel.core.engine.get_mod",
                   staged_get({"a.jar": "a", "b.jar": "b"})), \
             patch.object(ModrinthClient, "versions_by_hash", lambda self, h: {}), \
             patch.object(inst.shutil, "copy2", copy2):
            result = add_to_instance(add(mods), events=lambda e: None)
        assert not result.installed and "is the game running?" in result.reason
        assert [a["name"] for a in result.record.added] == ["a.jar"]
        assert undo(result.record.id).removed == ["a.jar"]
        assert list(mods.iterdir()) == []

    def test_history_skips_broken_lines(self):
        constants.MODKEEL_HOME.mkdir(parents=True, exist_ok=True)
        inst.history_file().write_text(
            "not json\n" + json.dumps({"id": "x", "bogus": 1}) + "\n"
            + json.dumps(InstallRecord("ok", 2.0, "/m", "M", "1.21.1", "fabric").to_dict())
            + "\n", encoding="utf-8")
        assert [r.id for r in history()] == ["ok"]

    def test_unknown_id(self):
        assert undo("nope").reason == "no add with id nope"
