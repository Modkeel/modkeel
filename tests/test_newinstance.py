"""A moved pack as a new Prism Launcher instance (modkeel/newinstance.py): what is written,
what is carried from the old instance and what is not, the loader version picked from
Prism's metadata, and the old instance left exactly as it was.

The instances folder is built in tmp_path as Prism writes it; Prism's metadata is a double.
"""

import json
from pathlib import Path

import pytest

from modkeel.instances import Instance, find_instances
from modkeel.newinstance import NewInstanceError, instance_of, loader_version_for, new_instance

NEOFORGE_INDEX = {"versions": [
    {"version": "21.10.64", "releaseTime": "2025-11-02", "recommended": False,
     "requires": [{"uid": "net.minecraft", "equals": "1.21.10"}]},
    {"version": "21.10.70-beta", "releaseTime": "2025-12-01", "recommended": False,
     "requires": [{"uid": "net.minecraft", "equals": "1.21.10"}]},
    {"version": "21.1.200", "releaseTime": "2026-01-01", "recommended": True,
     "requires": [{"uid": "net.minecraft", "equals": "1.21.1"}]},
]}
FABRIC_INDEX = {"versions": [
    {"version": "0.17.2", "releaseTime": "2025-10-01", "recommended": True,
     "requires": [{"uid": "net.fabricmc.intermediary"}]},
    {"version": "0.17.3", "releaseTime": "2025-11-01", "recommended": False,
     "requires": [{"uid": "net.fabricmc.intermediary"}]},
]}


def meta(url):
    return {"net.neoforged": NEOFORGE_INDEX,
            "net.fabricmc.fabric-loader": FABRIC_INDEX}.get(url.split("/")[-2])


def snapshot(folder: Path):
    return {str(p.relative_to(folder)): p.read_bytes() for p in folder.rglob("*") if p.is_file()}


@pytest.fixture
def prism(tmp_path):
    """A Prism instance "All the Mods 10" on 1.21.1 NeoForge, with settings, configs and a
    world; and the moved JARs in out/mc-1.21.10."""
    base = tmp_path / ".local/share/PrismLauncher/instances"
    old = base / "ATM10"
    game = old / "minecraft"
    (game / "mods/.index").mkdir(parents=True)
    (game / "mods/old.jar").write_bytes(b"old")
    (game / "mods/.index/old.pw.toml").write_text("filename = 'old.jar'\n")
    (game / "config").mkdir()
    (game / "config/jei.toml").write_text("x = 1\n")
    (game / "saves/World").mkdir(parents=True)
    (game / "options.txt").write_text("fov:0.5\n")
    (old / "instance.cfg").write_text(
        "[General]\nConfigVersion=1.2\nInstanceType=OneSix\nname=All the Mods 10\n"
        "iconKey=atm\nMaxMemAlloc=8192\nOverrideMemory=true\ntotalTimePlayed=99999\n"
        "ManagedPack=true\nManagedPackID=abc\nManagedPackType=modrinth\n"
        "OverrideJavaLocation=true\nJavaPath=/usr/lib/jvm/java-21/bin/java\n")
    (old / "mmc-pack.json").write_text(json.dumps({"components": [
        {"uid": "org.lwjgl3", "version": "3.3.3", "dependencyOnly": True},
        {"uid": "net.minecraft", "version": "1.21.1", "important": True},
        {"uid": "net.neoforged", "version": "21.1.77"}], "formatVersion": 1}))
    (old / "atm.png").write_bytes(b"icon")
    out = tmp_path / "out/mc-1.21.10"
    out.mkdir(parents=True)
    for name in ("a.jar", "b.jar"):
        (out / name).write_bytes(b"PK")
    instances = find_instances(tmp_path, "Linux", {})
    return instances[0], out, instances


class TestLoaderVersion:
    def test_neoforge_for_that_minecraft_stable_first(self):
        assert loader_version_for("neoforge", "1.21.10", meta) == "21.10.64"

    def test_fabric_runs_on_any_version_recommended_first(self):
        assert loader_version_for("fabric", "1.21.10", meta) == "0.17.2"

    def test_none_for_a_version_without_builds_or_no_metadata(self):
        assert loader_version_for("neoforge", "1.22", meta) is None
        assert loader_version_for("neoforge", "1.21.10", lambda url: None) is None


class TestNewInstance:
    def test_beside_the_old_one_ready_to_play(self, prism):
        old, out, instances = prism
        before = snapshot(Path(old.path))
        made = new_instance(old, "1.21.10", "neoforge", out, get_json=meta, instances=instances)
        new = Path(made.instance.path)
        assert new.parent == Path(old.path).parent and new.name == "ATM10 1.21.10"
        assert made.instance.name == "All the Mods 10 (1.21.10)"
        assert sorted(p.name for p in (new / "minecraft/mods").iterdir()) == ["a.jar", "b.jar"]
        pack = json.loads((new / "mmc-pack.json").read_text())["components"]
        assert [(c["uid"], c["version"]) for c in pack] == [
            ("net.minecraft", "1.21.10"), ("net.neoforged", "21.10.64")]
        assert snapshot(Path(old.path)) == before           # the old instance untouched
        # and Prism reads it back as an instance of the target
        again = {i.name: i for i in find_instances(new.parents[4], "Linux", {})}
        got = again["All the Mods 10 (1.21.10)"]
        assert (got.mc_version, got.loader, got.mods) == ("1.21.10", "neoforge", 2)

    def test_settings_kept_and_what_belongs_to_the_old_instance_dropped(self, prism):
        old, out, instances = prism
        new = Path(new_instance(old, "1.21.10", "neoforge", out, get_json=meta,
                                instances=instances).instance.path)
        cfg = (new / "instance.cfg").read_text()
        assert "name=All the Mods 10 (1.21.10)" in cfg
        assert "MaxMemAlloc=8192" in cfg and "iconKey=atm" in cfg
        for gone in ("totalTimePlayed", "ManagedPack", "JavaPath", "OverrideJavaLocation",
                     "name=All the Mods 10\n"):
            assert gone not in cfg
        assert (new / "atm.png").read_bytes() == b"icon"

    def test_configs_and_options_carried_worlds_and_old_index_not(self, prism):
        old, out, instances = prism
        made = new_instance(old, "1.21.10", "neoforge", out, get_json=meta,
                            instances=instances)
        game = Path(made.instance.path) / "minecraft"
        assert made.copied == ["options.txt", "config"]
        assert (game / "config/jei.toml").is_file() and (game / "options.txt").is_file()
        assert not (game / "saves").exists() and not (game / "mods/.index").exists()

    def test_twice_gives_a_second_instance_never_overwrites(self, prism):
        old, out, instances = prism
        first = new_instance(old, "1.21.10", "neoforge", out, get_json=meta,
                             instances=instances).instance
        second = new_instance(old, "1.21.10", "neoforge", out, get_json=meta,
                              instances=[*instances, first]).instance
        assert first.path != second.path and second.name == "All the Mods 10 (1.21.10) 2"

    def test_no_loader_version_means_no_folder_left(self, prism):
        old, out, instances = prism
        base = Path(old.path).parent
        with pytest.raises(NewInstanceError, match="no neoforge version"):
            new_instance(old, "1.22", "neoforge", out, get_json=meta, instances=instances)
        assert [p.name for p in base.iterdir()] == ["ATM10"]

    def test_only_prism_for_now(self, tmp_path):
        other = Instance("modrinth", "P", str(tmp_path), str(tmp_path / "mods"))
        with pytest.raises(NewInstanceError, match="only Prism"):
            new_instance(other, "1.21.10", "fabric", tmp_path, get_json=meta, instances=[])


def test_the_instance_a_mods_folder_belongs_to(prism):
    old, _, instances = prism
    assert instance_of(Path(old.mods_dir), instances) is old
    assert instance_of(Path(old.path), instances) is None
