"""Moving a pack to another Minecraft version (modkeel/core/move.py, `modkeel move`).

Identification (hash exact, metadata id or name as a guess, unknown), what becomes of each
JAR (delivered, reused when the player's file passes on the target, unknown), the pack-level
version proposal, and the `move` protocol method. Modrinth and the resolver are doubles; the
JARs are tiny real files.
"""

import json
import zipfile
from pathlib import Path
from unittest.mock import patch

import pytest

from modkeel.core.decisions import ChangeTarget, safe_default
from modkeel.core.events import PackScanned
from modkeel.core.move import MoveRequest, pack_loader, move_pack, scan_pack
from modkeel.evidence import Evidence, Outcome
from modkeel.modrinth import ModrinthClient
from modkeel.resolve import Delivered, Resolution, Step


def fabric_jar(folder: Path, name: str, mod_id: str, title: str, mc: str = ">=1.21") -> Path:
    jar = folder / name
    with zipfile.ZipFile(jar, "w") as z:
        z.writestr("fabric.mod.json", json.dumps({
            "schemaVersion": 1, "id": mod_id, "version": "1.0", "name": title,
            "depends": {"minecraft": mc}}))
    return jar


@pytest.fixture
def pack(tmp_path, monkeypatch):
    """sodium.jar (known by hash), cloth.jar (unknown hash, id is a slug), mine.jar (unknown)."""
    monkeypatch.chdir(tmp_path)
    mods = tmp_path / "mods"
    mods.mkdir()
    fabric_jar(mods, "sodium.jar", "sodium", "Sodium")
    fabric_jar(mods, "cloth.jar", "cloth-config", "Cloth Config v15")
    fabric_jar(mods, "mine.jar", "my_private_mod", "My Private Mod")
    from modkeel.scanner import compute_sha1

    sodium_hash = compute_sha1(mods / "sodium.jar")
    projects = {
        "AANobbMI": {"id": "AANobbMI", "slug": "sodium", "title": "Sodium",
                     "project_type": "mod", "loaders": ["fabric"]},
        "9s6osm5g": {"id": "9s6osm5g", "slug": "cloth-config", "title": "Cloth Config API",
                     "project_type": "mod", "loaders": ["fabric", "neoforge"]},
    }
    by_slug = {p["slug"]: p for p in projects.values()}
    with patch.object(ModrinthClient, "versions_by_hash",
                      lambda self, hashes: {sodium_hash: {"project_id": "AANobbMI"}}), \
            patch.object(ModrinthClient, "fetch_projects",
                         lambda self, ids: {i: projects[i] for i in ids if i in projects}), \
            patch.object(ModrinthClient, "fetch_project",
                         lambda self, pid: by_slug.get(pid) or projects.get(pid)), \
            patch.object(ModrinthClient, "find_project", lambda self, q: (None, [])):
        yield mods


def resolving(delivers):
    """Resolver.resolve double: a JAR in the output folder for mods whose title is in
    `delivers`, nothing for the others."""
    def resolve(self, mod, ctx):
        if mod.title in delivers:
            jar = Path(ctx.config.output_dir) / f"{mod.project['slug']}-{ctx.mc_version}.jar"
            jar.write_bytes(b"PK")
            return Resolution(trail=[Step("official", True, mod.title)],
                              delivered=Delivered(jar, mod.title, "2.0"))
        return Resolution(trail=[Step("official", False, f"no build for {ctx.mc_version}")])
    return patch("modkeel.resolve.Resolver.resolve", resolve)


def evidence(ok: bool):
    outcome = Outcome("linkage", "passed" if ok else "failed", "ok" if ok else "3 missing")
    return patch("modkeel.evidence.gather",
                 lambda *a, **k: Evidence(outcomes=[outcome], required=frozenset({"linkage"})))


class TestScan:
    def test_hash_is_exact_metadata_id_is_a_guess_the_rest_unknown(self, pack):
        events = []
        entries = scan_pack(pack, ModrinthClient(_config(), events.append), events.append)
        seen = {e.mod.file: (e.mod.name, e.mod.identified_by, e.mod.slug) for e in entries}
        assert seen == {
            "sodium.jar": ("Sodium", "hash", "sodium"),
            "cloth.jar": ("Cloth Config API", "name", "cloth-config"),
            "mine.jar": ("My Private Mod", None, None),
        }
        scanned = [e for e in events if isinstance(e, PackScanned)]
        assert len(scanned) == 1 and len(scanned[0].mods) == 3
        # the sources read search-hit fields: a full record gets project_id
        assert all(e.project["project_id"] for e in entries if e.project)

    def test_the_launchers_record_identifies_what_the_hash_does_not(self, pack):
        # Prism installed mine.jar from Modrinth (a project id the hash lookup missed) and
        # cloth.jar from CurseForge, whose slug and name match the project on Modrinth
        index = pack / ".index"
        index.mkdir()
        (index / "mine.pw.toml").write_text(
            'name = "Mine"\nfilename = "mine.jar"\n[update.modrinth]\nmod-id = "AANobbMI"\n')
        (index / "cloth-config.pw.toml").write_text(
            'name = "Cloth Config API (Fabric/Forge/NeoForge)"\nfilename = "cloth.jar"\n'
            '[update.curseforge]\nproject-id = 348521\nfile-id = 1\n')
        events = []
        entries = scan_pack(pack, ModrinthClient(_config(), events.append), events.append)
        seen = {e.mod.file: (e.mod.identified_by, e.mod.slug) for e in entries}
        assert seen["mine.jar"] == ("launcher", "sodium")
        assert seen["cloth.jar"] == ("name", "cloth-config")
        line = [e for e in events if isinstance(e, PackScanned)][0]
        from modkeel.core.text import render_text

        assert "1 by the launcher's records" in render_text(line)

    def test_a_curseforge_only_mod_says_so(self, pack):
        (pack.parent / "minecraftinstance.json").write_text(json.dumps({"installedAddons": [
            {"addonID": 7, "name": "Totally Private", "installedFile": {"fileName": "mine.jar"}},
        ]}))
        with resolving({"Sodium", "Cloth Config API"}), evidence(ok=False):
            result = move_pack(MoveRequest(str(pack), "1.21.10"), events=lambda e: None)
        mine = next(m for m in result.mods if m.file == "mine.jar")
        # known exactly (the launcher's record), so missing rather than unknown
        assert (mine.status, mine.identified_by) == ("missing", "launcher")
        assert mine.detail.startswith("on CurseForge only, not on Modrinth; your JAR does not")

    def test_the_pack_loader_is_what_its_jars_declare(self, pack):
        entries = scan_pack(pack, ModrinthClient(_config(), lambda e: None), lambda e: None)
        assert pack_loader(entries) == "fabric"


def _config():
    from modkeel.models import ModCompilerConfig

    return ModCompilerConfig(mc_version="1.21.10", loader="fabric", loader_version="0",
                             output_dir="out")


class TestPort:
    def test_each_jar_ends_delivered_reused_or_unknown(self, pack):
        with resolving({"Sodium", "Cloth Config API"}), evidence(ok=True):
            result = move_pack(MoveRequest(str(pack), "1.21.10"), events=lambda e: None)
        status = {m.file: m.status for m in result.mods}
        assert status == {"sodium.jar": "delivered", "cloth.jar": "delivered",
                          "mine.jar": "reused"}
        assert result.ready == 3 and result.loader == "fabric"
        assert result.output_dir == Path("out/mc-1.21.10")
        assert (result.output_dir / "mine.jar").is_file()       # the player's JAR, copied
        assert (pack / "mine.jar").is_file()                     # and never moved

    def test_an_unknown_jar_that_does_not_pass_is_left_out(self, pack):
        with resolving({"Sodium", "Cloth Config API"}), evidence(ok=False):
            result = move_pack(MoveRequest(str(pack), "1.21.10"), events=lambda e: None)
        mine = next(m for m in result.mods if m.file == "mine.jar")
        assert mine.status == "unknown" and "not on Modrinth" in mine.detail
        assert not (result.output_dir / "mine.jar").exists()

    def test_missing_mods_lead_to_a_pack_proposal_and_a_run_there(self, pack):
        from modkeel.target import TargetOption

        asked = []

        def decide(q):
            asked.append(q)
            return isinstance(q, ChangeTarget)

        option = TargetOption("1.21.1", covered=["Sodium", "Cloth Config API"])
        with resolving({"Sodium"}), evidence(ok=False), \
                patch("modkeel.target.propose_targets", return_value=[option]), \
                patch("modkeel.target.older_build_probe"), \
                patch("modkeel.mappings.release_versions", return_value=()):
            result = move_pack(MoveRequest(str(pack), "1.21.10"), events=lambda e: None,
                               decide=decide)
        change = [q for q in asked if isinstance(q, ChangeTarget)]
        assert change and change[0].scope == "pack" and change[0].current == "1.21.10"
        assert result.retargeted and result.target == "1.21.1"
        assert result.output_dir == Path("out/mc-1.21.1")

    def test_headless_keeps_the_version(self, pack):
        from modkeel.target import TargetOption

        with resolving({"Sodium"}), evidence(ok=False), \
                patch("modkeel.target.propose_targets",
                      return_value=[TargetOption("1.21.1", covered=["Sodium"])]), \
                patch("modkeel.target.older_build_probe"), \
                patch("modkeel.mappings.release_versions", return_value=()):
            result = move_pack(MoveRequest(str(pack), "1.21.10"), events=lambda e: None,
                               decide=safe_default)
        assert not result.retargeted and result.proposal.mc_version == "1.21.1"


class TestWire:
    def test_move_method_runs_and_reports_each_mod(self, pack):
        from modkeel.core.wire import METHODS

        events = []
        with resolving({"Sodium", "Cloth Config API"}), evidence(ok=True):
            out = METHODS["move"]({"mods_dir": str(pack), "mc_version": "1.21.10"},
                                  events.append, safe_default, lambda: False)
        assert json.loads(json.dumps(out))["ready"] == 3
        assert {m["file"]: m["status"] for m in out["mods"]}["mine.jar"] == "reused"

    def test_move_refuses_a_path_that_is_not_a_folder(self, tmp_path):
        from modkeel.core.wire import METHODS

        with pytest.raises(TypeError, match="not a folder"):
            METHODS["move"]({"mods_dir": str(tmp_path / "nope"), "mc_version": "1.21.10"},
                            lambda e: None, safe_default, lambda: False)


class TestNewInstance:
    """move_pack(new_instance=True): the moved pack added to Prism beside the old instance."""

    def prism(self, pack, tmp_path):
        import shutil

        from modkeel.instances import Instance

        game = tmp_path / "instances/ATM/minecraft"
        game.mkdir(parents=True)
        shutil.move(str(pack), str(game / "mods"))
        (game.parent / "instance.cfg").write_text("[General]\nname=All the Mods 10\n")
        return [Instance("prism", "All the Mods 10", str(game.parent), str(game / "mods"),
                         "1.21.1", "fabric", "0.16.0", 3)]

    def test_created_with_what_was_moved(self, pack, tmp_path):
        instances = self.prism(pack, tmp_path)
        events = []
        with resolving({"Sodium", "Cloth Config API"}), evidence(ok=True), \
                patch("modkeel.instances.find_instances", lambda: instances), \
                patch("modkeel.newinstance._get_json",
                      lambda url: {"versions": [{"version": "0.17.2", "recommended": True}]}):
            result = move_pack(MoveRequest(instances[0].mods_dir, "1.21.10",
                                           new_instance=True), events=events.append)
        made = Path(result.instance.path)
        assert made == tmp_path / "instances/ATM 1.21.10" and not result.instance_note
        assert len(list((made / "minecraft/mods").glob("*.jar"))) == 3
        pack = json.loads((made / "mmc-pack.json").read_text())["components"]
        assert [(c["uid"], c["version"]) for c in pack] == [
            ("net.minecraft", "1.21.10"), ("net.fabricmc.intermediary", "1.21.10"),
            ("net.fabricmc.fabric-loader", "0.17.2")]
        assert any("New Prism Launcher instance: All the Mods 10 (1.21.10)"
                   in getattr(e, "text", "") for e in events)

    def test_without_a_loader_version_the_pack_stays_in_out(self, pack, tmp_path):
        instances = self.prism(pack, tmp_path)
        with resolving({"Sodium", "Cloth Config API"}), evidence(ok=True), \
                patch("modkeel.instances.find_instances", lambda: instances), \
                patch("modkeel.newinstance._get_json", lambda url: None):
            result = move_pack(MoveRequest(instances[0].mods_dir, "1.21.10",
                                           new_instance=True), events=lambda e: None)
        assert result.instance is None and "no fabric version" in result.instance_note
        assert result.ready == 3


class TestCommand:
    """`modkeel move` takes a folder or an instance's name; `modkeel instances` lists them."""

    def _instances(self, pack):
        from modkeel.instances import Instance

        return [Instance("prism", "All the Mods 10", str(pack.parent), str(pack), "1.21.1",
                         "neoforge", "21.1.77", 3),
                Instance("prism", "All the Mods 9", "/nowhere", "/nowhere/mods", "1.20.1")]

    def run(self, *args):
        from typer.testing import CliRunner

        from modkeel.cli import app

        return CliRunner().invoke(app, list(args))

    def test_an_instance_name_moves_its_mods_with_its_loader(self, pack):
        seen = {}

        def fake_move(request, **_):
            seen["request"] = request
            from modkeel.core.move import MoveResult
            return MoveResult(request.mc_version, request.loader, Path("out"))

        with patch("modkeel.instances.find_instances", lambda: self._instances(pack)), \
                patch("modkeel.core.move.move_pack", fake_move):
            result = self.run("move", "all the mods 10", "-m", "1.21.10", "--fallback", "never")
        assert seen["request"].mods_dir == str(pack)
        assert seen["request"].loader == "neoforge"        # the instance's, not guessed
        assert seen["request"].new_instance                 # beside it, in the launcher
        assert "Prism Launcher: All the Mods 10 (1.21.1 neoforge)" in result.output

    def test_an_ambiguous_or_unknown_name_is_refused(self, pack):
        with patch("modkeel.instances.find_instances", lambda: self._instances(pack)):
            ambiguous = self.run("move", "All the Mods", "-m", "1.21.10")
            unknown = self.run("move", "Create Above", "-m", "1.21.10")
        assert ambiguous.exit_code == 2 and "several instances match" in ambiguous.output
        assert unknown.exit_code == 2 and "nor an instance's name" in unknown.output

    def test_an_instance_folder_means_its_mods(self, pack, tmp_path):
        seen = {}

        def fake_move(request, **_):
            seen["dir"] = request.mods_dir
            from modkeel.core.move import MoveResult
            return MoveResult(request.mc_version, "fabric", Path("out"))

        with patch("modkeel.core.move.move_pack", fake_move), \
                patch("modkeel.instances.find_instances", lambda: []):
            self.run("move", str(pack.parent), "-m", "1.21.10", "--fallback", "never")
        assert seen["dir"] == str(pack)

    def test_a_new_instance_only_for_a_launchers_folder_and_unless_refused(self, pack):
        seen = []

        def fake_move(request, **_):
            seen.append(request.new_instance)
            from modkeel.core.move import MoveResult
            return MoveResult(request.mc_version, "fabric", Path("out"))

        with patch("modkeel.core.move.move_pack", fake_move):
            with patch("modkeel.instances.find_instances", lambda: []):
                self.run("move", str(pack), "-m", "1.21.10", "--fallback", "never")
            with patch("modkeel.instances.find_instances", lambda: self._instances(pack)):
                self.run("move", str(pack), "-m", "1.21.10", "--fallback", "never")
                self.run("move", "All the Mods 10", "-m", "1.21.10", "--fallback", "never",
                         "--no-new-instance")
        assert seen == [False, True, False]

    def test_instances_lists_and_prints_json(self, pack):
        with patch("modkeel.instances.find_instances", lambda: self._instances(pack)):
            table = self.run("instances")
            listed = self.run("instances", "--json")
        assert "All the Mods 10" in table.output and "neoforge 21.1.77" in table.output
        assert [i["name"] for i in json.loads(listed.output)] == ["All the Mods 10",
                                                                    "All the Mods 9"]
