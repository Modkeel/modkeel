"""Launcher instances (modkeel/instances.py): each launcher's layout on each platform, what
is read from it, what is left None, and naming an instance.

The launchers' folders are built in tmp_path as each one writes them; `home`, `system` and
`env` stand in for the machine.
"""

import hashlib
import json
import sqlite3
from pathlib import Path

from modkeel.instances import Instance, find_instance, find_instances, launcher_records, \
    mods_dir_of


def jars(mods: Path, n: int) -> None:
    mods.mkdir(parents=True, exist_ok=True)
    for i in range(n):
        (mods / f"mod{i}.jar").write_bytes(b"PK")
    (mods / "notes.txt").write_text("not a mod")


def prism_instance(base: Path, folder: str, name: str, mc: str, loader_uid: str = "",
                   loader_version: str = "", game: str = ".minecraft", mods: int = 0) -> Path:
    inst = base / folder
    inst.mkdir(parents=True)
    (inst / "instance.cfg").write_text(f"[General]\nInstanceType=OneSix\nname={name}\n")
    components = [{"uid": "net.minecraft", "version": mc}]
    if loader_uid:
        components.append({"uid": loader_uid, "version": loader_version})
    (inst / "mmc-pack.json").write_text(json.dumps({"components": components,
                                                    "formatVersion": 1}))
    jars(inst / game / "mods", mods)
    return inst


def by_name(instances):
    return {i.name: i for i in instances}


class TestPrism:
    def test_linux_instances_with_their_version_loader_and_mods(self, tmp_path):
        base = tmp_path / ".local/share/PrismLauncher/instances"
        prism_instance(base, "ATM", "All the Mods 10", "1.21.1", "net.neoforged", "21.1.77",
                       game="minecraft", mods=3)
        prism_instance(base, "Fab", "Fabric fun", "1.21.10", "net.fabricmc.fabric-loader",
                       "0.17.2", mods=2)
        prism_instance(base, "Van", "Vanilla", "1.21.10")
        (base / "_LAUNCHER_TEMP").mkdir()     # Prism's own folders are not instances
        found = by_name(find_instances(tmp_path, "Linux", {}))
        assert set(found) == {"All the Mods 10", "Fabric fun", "Vanilla"}
        atm = found["All the Mods 10"]
        assert (atm.launcher, atm.mc_version, atm.loader, atm.loader_version, atm.mods) == \
            ("prism", "1.21.1", "neoforge", "21.1.77", 3)
        assert atm.mods_dir == str(base / "ATM" / "minecraft" / "mods")
        assert found["Fabric fun"].loader == "fabric" and found["Fabric fun"].mods == 2
        assert found["Vanilla"].loader is None and found["Vanilla"].mods == 0

    def test_a_custom_instances_folder(self, tmp_path):
        root = tmp_path / "AppData/Roaming/PrismLauncher"
        root.mkdir(parents=True)
        elsewhere = tmp_path / "games/prism"
        (root / "prismlauncher.cfg").write_text(f"InstanceDir={elsewhere}\n")
        prism_instance(elsewhere, "x", "Moved", "1.20.1", "net.minecraftforge", "47.3.0")
        found = find_instances(tmp_path, "Windows", {})
        assert [(i.name, i.loader) for i in found] == [("Moved", "forge")]

    def test_flatpak(self, tmp_path):
        base = tmp_path / ".var/app/org.prismlauncher.PrismLauncher/data/PrismLauncher/instances"
        prism_instance(base, "f", "From Flatpak", "1.21.1")
        assert [i.name for i in find_instances(tmp_path, "Linux", {})] == ["From Flatpak"]


class TestModrinth:
    def test_profiles_named_by_the_app_database(self, tmp_path):
        root = tmp_path / "Library/Application Support/ModrinthApp"
        jars(root / "profiles/cobblemon/mods", 4)
        (root / "profiles/plain").mkdir(parents=True)
        con = sqlite3.connect(root / "app.db")
        con.execute("CREATE TABLE profiles (path TEXT PRIMARY KEY, install_stage TEXT, "
                    "name TEXT, game_version TEXT, mod_loader TEXT, mod_loader_version TEXT)")
        con.executemany("INSERT INTO profiles VALUES (?, 'installed', ?, ?, ?, ?)", [
            ("cobblemon", "Cobblemon Official", "1.21.1", "fabric", "0.16.14"),
            ("plain", "Just vanilla", "1.21.10", "vanilla", None),
        ])
        con.commit()
        con.close()
        found = by_name(find_instances(tmp_path, "Darwin", {}))
        cob = found["Cobblemon Official"]
        assert (cob.launcher, cob.mc_version, cob.loader, cob.loader_version, cob.mods) == \
            ("modrinth", "1.21.1", "fabric", "0.16.14", 4)
        assert found["Just vanilla"].loader is None

    def test_older_app_profile_json(self, tmp_path):
        prof = tmp_path / ".local/share/com.modrinth.theseus/profiles/old"
        prof.mkdir(parents=True)
        (prof / "profile.json").write_text(json.dumps({"metadata": {
            "name": "Old pack", "game_version": "1.20.1", "loader": "forge",
            "loader_version": {"id": "47.2.0"}}}))
        (prof.parent / "stray").mkdir()          # no profile.json, no database: skipped
        found = find_instances(tmp_path, "Linux", {})
        assert [(i.name, i.mc_version, i.loader, i.loader_version) for i in found] == \
            [("Old pack", "1.20.1", "forge", "47.2.0")]

    def test_an_unreadable_database_falls_back_to_the_folders(self, tmp_path):
        root = tmp_path / ".local/share/ModrinthApp"
        (root / "profiles/p").mkdir(parents=True)
        (root / "profiles/p/profile.json").write_text(json.dumps({"metadata": {"name": "P"}}))
        (root / "app.db").write_bytes(b"not a database")
        assert [i.name for i in find_instances(tmp_path, "Linux", {})] == ["P"]


class TestCurseForge:
    def test_windows_instances(self, tmp_path):
        base = tmp_path / "curseforge/minecraft/Instances"
        for folder, name, mc, loader in [
            ("ATM9", "All the Mods 9", "1.20.1", "forge-47.3.0"),
            ("Fab", "Fabulously Optimized", "1.21.1", "fabric-0.16.5-1.21.1"),
            ("Neo", "Neo pack", "1.21.1", "neoforge-21.1.77"),
        ]:
            (base / folder).mkdir(parents=True)
            (base / folder / "minecraftinstance.json").write_text(json.dumps({
                "name": name, "gameVersion": mc, "baseModLoader": {"name": loader}}))
        jars(base / "Neo/mods", 5)
        found = by_name(find_instances(tmp_path, "Windows", {"APPDATA": str(tmp_path / "x")}))
        assert {n: (i.loader, i.loader_version) for n, i in found.items()} == {
            "All the Mods 9": ("forge", "47.3.0"),
            "Fabulously Optimized": ("fabric", "0.16.5"),
            "Neo pack": ("neoforge", "21.1.77"),
        }
        assert found["Neo pack"].mods == 5 and found["Neo pack"].launcher == "curseforge"

    def test_macos_keeps_them_in_documents(self, tmp_path):
        inst = tmp_path / "Documents/curseforge/minecraft/Instances/A"
        inst.mkdir(parents=True)
        (inst / "minecraftinstance.json").write_text(json.dumps({"name": "A",
                                                                 "gameVersion": "1.21.1"}))
        found = find_instances(tmp_path, "Darwin", {})
        assert [(i.name, i.loader) for i in found] == [("A", None)]


class TestOfficialLauncher:
    def test_dot_minecraft_with_mods(self, tmp_path):
        jars(tmp_path / "AppData/Roaming/.minecraft/mods", 2)
        found = find_instances(tmp_path, "Windows", {})
        assert [(i.launcher, i.mods, i.mc_version) for i in found] == [("minecraft", 2, None)]

    def test_without_a_mods_folder_it_is_not_listed(self, tmp_path):
        (tmp_path / ".minecraft").mkdir()
        assert find_instances(tmp_path, "Linux", {}) == []


def test_nothing_installed(tmp_path):
    assert find_instances(tmp_path, "Linux", {}) == []


class TestNaming:
    instances = [Instance("prism", n, f"/i/{n}", f"/i/{n}/mods")
                 for n in ("All the Mods 10", "All the Mods 9", "Fabric fun")]

    def test_exact_name_any_case(self):
        match, _ = find_instance("all the mods 9", self.instances)
        assert match.name == "All the Mods 9"

    def test_a_unique_part_of_the_name(self):
        assert find_instance("fabric", self.instances)[0].name == "Fabric fun"

    def test_ambiguous_gives_the_candidates(self):
        match, candidates = find_instance("all the mods", self.instances)
        assert match is None and len(candidates) == 2

    def test_unknown(self):
        assert find_instance("create", self.instances) == (None, [])


def test_a_game_folder_means_its_mods(tmp_path):
    (tmp_path / "inst/minecraft/mods").mkdir(parents=True)
    assert mods_dir_of(tmp_path / "inst") == tmp_path / "inst/minecraft/mods"
    assert mods_dir_of(tmp_path / "inst/minecraft/mods") == tmp_path / "inst/minecraft/mods"
    (tmp_path / "plain").mkdir()
    assert mods_dir_of(tmp_path / "plain") == tmp_path / "plain"


class TestLauncherRecords:
    """What the launcher says it installed: Prism's .index, packwiz, CurseForge's addons."""

    @staticmethod
    def pw(folder: Path, slug: str, filename: str, update: str, hash_line: str = "") -> None:
        folder.mkdir(parents=True, exist_ok=True)
        (folder / f"{slug}.pw.toml").write_text(
            f'name = "{slug.title()}"\nfilename = "{filename}"\nside = "both"\n\n'
            f'[download]\n{hash_line}\n\n{update}\n')

    def test_prism_index_names_the_project_on_either_site(self, tmp_path):
        mods = tmp_path / "minecraft/mods"
        jars(mods, 3)
        sha512 = hashlib.sha512(b"PK").hexdigest()
        self.pw(mods / ".index", "sodium", "mod0.jar",
                '[update.modrinth]\nmod-id = "AANobbMI"\nversion = "x"',
                f'hash-format = "sha512"\nhash = "{sha512}"')
        self.pw(mods / ".index", "jei", "mod1.jar",
                "[update.curseforge]\nfile-id = 5\nproject-id = 238222")
        self.pw(mods / ".index", "gone", "removed.jar", '[update.modrinth]\nmod-id = "x"')
        found = launcher_records(mods)
        assert {f: (r.source, r.project_id, r.slug) for f, r in found.items()} == {
            "mod0.jar": ("modrinth", "AANobbMI", "sodium"),
            "mod1.jar": ("curseforge", "238222", "jei"),
        }

    def test_the_files_prism_writes(self, tmp_path):
        # Prism serialises with toml++: literal strings, sorted keys, its x-prismlauncher-*
        # fields, dependencies as an array of tables; CurseForge entries carry sha1, md5 or
        # murmur2 (no hashlib name: the record is trusted) and no URL
        mods = tmp_path / "mods"
        jars(mods, 2)
        sha512 = hashlib.sha512(b"PK").hexdigest()
        (mods / ".index").mkdir()
        (mods / ".index/sodium.pw.toml").write_text(f"""filename = 'mod0.jar'
name = 'Sodium'
side = 'both'
x-prismlauncher-loaders = [ 'fabric', 'quilt' ]
x-prismlauncher-lock-update = false
x-prismlauncher-mc-versions = [ '1.21.6' ]
x-prismlauncher-release-type = 'release'
x-prismlauncher-version-number = 'mc1.21.6-0.6.13-fabric'

[[x-prismlauncher-dependencies]]
addonId = 'P7dR8mSH'
type = 'required'

[download]
hash = '{sha512}'
hash-format = 'sha512'
mode = 'url'
url = 'https://cdn.modrinth.com/data/AANobbMI/versions/x/sodium.jar'

[update.modrinth]
mod-id = 'AANobbMI'
version = 'x'
""")
        (mods / ".index/jei.pw.toml").write_text("""filename = 'mod1.jar'
name = 'Just Enough Items (JEI)'
side = 'both'
x-prismlauncher-dependencies = []
x-prismlauncher-loaders = [ 'neoforge' ]

[download]
hash = '1234567890'
hash-format = 'murmur2'
mode = 'metadata:curseforge'
url = ''

[update.curseforge]
file-id = 5101366
project-id = 238222
""")
        found = launcher_records(mods)
        assert {f: (r.source, r.project_id, r.name) for f, r in found.items()} == {
            "mod0.jar": ("modrinth", "AANobbMI", "Sodium"),
            "mod1.jar": ("curseforge", "238222", "Just Enough Items (JEI)"),
        }

    def test_a_replaced_jar_loses_its_record(self, tmp_path):
        jars(tmp_path / "mods", 1)
        self.pw(tmp_path / "mods/.index", "s", "mod0.jar", '[update.modrinth]\nmod-id = "A"',
                'hash-format = "sha1"\nhash = "0000"')
        assert launcher_records(tmp_path / "mods") == {}

    def test_curseforge_installed_addons(self, tmp_path):
        jars(tmp_path / "mods", 2)
        (tmp_path / "minecraftinstance.json").write_text(json.dumps({"installedAddons": [
            {"addonID": 238222, "name": "Just Enough Items (JEI)",
             "webSiteURL": "https://www.curseforge.com/minecraft/mc-mods/jei",
             "installedFile": {"fileNameOnDisk": "mod0.jar", "hashes": [
                 {"type": 1, "value": hashlib.sha1(b"PK").hexdigest()}]}},
            {"addonID": 1, "name": "Changed", "installedFile": {
                "fileName": "mod1.jar", "hashes": [{"type": 1, "value": "ff"}]}},
            {"name": "broken entry"},
        ]}))
        found = launcher_records(tmp_path / "mods")
        assert list(found) == ["mod0.jar"]
        assert (found["mod0.jar"].name, found["mod0.jar"].slug) == \
            ("Just Enough Items (JEI)", "jei")

    def test_packwiz_files_beside_the_jars_and_only_the_files_asked(self, tmp_path):
        jars(tmp_path / "mods", 2)
        self.pw(tmp_path / "mods", "a", "mod0.jar", '[update.modrinth]\nmod-id = "A"')
        self.pw(tmp_path / "mods", "b", "mod1.jar", '[update.modrinth]\nmod-id = "B"')
        (tmp_path / "mods/bad.pw.toml").write_text("not = [toml")
        assert list(launcher_records(tmp_path / "mods", ["mod1.jar"])) == ["mod1.jar"]

    def test_no_launcher_files(self, tmp_path):
        jars(tmp_path / "mods", 1)
        assert launcher_records(tmp_path / "mods") == {}
