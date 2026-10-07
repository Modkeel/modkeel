"""Tests for modkeel/relax.py: widening a mod JAR's declared Minecraft range.

JARs are built in tmp_path with the metadata layouts real mods ship (the NeoForge block
mirrors Create 6.0.10's neoforge.mods.toml).
"""

import json
import zipfile

from modkeel.build import validate_jar
from modkeel.relax import MARKER, relax_jar

NEOFORGE_TOML = '''modLoader = "javafml"
loaderVersion = "[1,)"

[[mods]]
modId = "create"
version = "6.0.10"

[[dependencies."create"]]
modId = "neoforge"
type = "required"
versionRange = "[21.1.219,)"
side = "BOTH"

[[dependencies."create"]]
modId = "minecraft"
type = "required"
versionRange = "[1.21.1]"   # keep this comment
side = "BOTH"

# Versions before 1.0 lack the API
[[dependencies."create"]]
modId = "flywheel"
versionRange = "[1.0.0,2.0)"
'''


def make_jar(path, files):
    with zipfile.ZipFile(path, "w") as z:
        for name, data in files.items():
            z.writestr(name, data)
        z.writestr("com/example/Big.class", b"\xca\xfe\xba\xbe" + b"x" * 20_000)
    return path


def read(path, name):
    with zipfile.ZipFile(path) as z:
        return z.read(name).decode()


class TestNeoForge:
    def test_only_the_minecraft_range_changes(self, tmp_path):
        src = make_jar(tmp_path / "create.jar", {"META-INF/neoforge.mods.toml": NEOFORGE_TOML})
        dest = tmp_path / "out.jar"
        change = relax_jar(src, dest, "1.21.1", "1.21.10")
        assert (change.old_range, change.new_range) == ("[1.21.1]", "[1.21.1],[1.21.10]")
        new = read(dest, "META-INF/neoforge.mods.toml")
        assert new == NEOFORGE_TOML.replace('"[1.21.1]"', '"[1.21.1],[1.21.10]"')
        assert validate_jar(src, "1.21.10")[0] is False
        assert validate_jar(dest, "1.21.10")[0] is True
        # what the author declared still holds; nothing in between is added
        assert validate_jar(dest, "1.21.1")[0] is True
        assert validate_jar(dest, "1.21.5")[0] is False

    def test_marker_and_signatures(self, tmp_path):
        src = make_jar(tmp_path / "s.jar", {
            "META-INF/neoforge.mods.toml": NEOFORGE_TOML,
            "META-INF/MANIFEST.MF": "Manifest-Version: 1.0\n",
            "META-INF/SIGNER.SF": "sig", "META-INF/SIGNER.RSA": "sig",
            "META-INF/jarjar/inner.jar": "keep",
        })
        dest = tmp_path / "out.jar"
        relax_jar(src, dest, "1.21.1", "1.21.10")
        with zipfile.ZipFile(dest) as z:
            names = set(z.namelist())
        assert "META-INF/SIGNER.SF" not in names and "META-INF/SIGNER.RSA" not in names
        assert {"META-INF/MANIFEST.MF", "META-INF/jarjar/inner.jar", MARKER} <= names
        assert "[1.21.1] -> [1.21.1],[1.21.10]" in read(dest, MARKER)

    def test_crlf_lines_survive(self, tmp_path):
        crlf = NEOFORGE_TOML.replace("\n", "\r\n")
        src = make_jar(tmp_path / "c.jar", {"META-INF/mods.toml": crlf})
        relax_jar(src, tmp_path / "out.jar", "1.21.1", "1.21.10")
        assert read(tmp_path / "out.jar", "META-INF/mods.toml") == crlf.replace(
            '"[1.21.1]"', '"[1.21.1],[1.21.10]"')

    def test_no_minecraft_dependency(self, tmp_path):
        toml = NEOFORGE_TOML.split('[[dependencies."create"]]\nmodId = "minecraft"')[0]
        src = make_jar(tmp_path / "n.jar", {"META-INF/neoforge.mods.toml": toml})
        assert relax_jar(src, tmp_path / "out.jar", "1.21.1", "1.21.10") is None
        assert not (tmp_path / "out.jar").exists()


class TestFabricAndQuilt:
    def test_fabric(self, tmp_path):
        meta = {"schemaVersion": 1, "id": "hud", "version": "1.3.1",
                "depends": {"fabricloader": ">=0.16", "minecraft": "~26.2"}}
        src = make_jar(tmp_path / "f.jar", {"fabric.mod.json": json.dumps(meta)})
        relax_jar(src, tmp_path / "out.jar", "26.2", "26.3")
        new = json.loads(read(tmp_path / "out.jar", "fabric.mod.json"))
        assert new["depends"] == {"fabricloader": ">=0.16", "minecraft": ["~26.2", "26.3"]}
        assert validate_jar(src, "26.3")[0] is False
        assert validate_jar(tmp_path / "out.jar", "26.3")[0] is True
        assert validate_jar(tmp_path / "out.jar", "26.2")[0] is True

    def test_quilt(self, tmp_path):
        meta = {"schema_version": 1, "quilt_loader": {
            "id": "q", "version": "1", "depends": [{"id": "minecraft", "versions": "1.21.9"}]}}
        src = make_jar(tmp_path / "q.jar", {"quilt.mod.json": json.dumps(meta)})
        relax_jar(src, tmp_path / "out.jar", "1.21.9", "1.21.10")
        new = json.loads(read(tmp_path / "out.jar", "quilt.mod.json"))
        assert new["quilt_loader"]["depends"][0]["versions"] == ["1.21.9", "1.21.10"]
        assert validate_jar(tmp_path / "out.jar", "1.21.10")[0] is True

    def test_fabric_list_is_extended(self, tmp_path):
        meta = {"id": "x", "version": "1", "depends": {"minecraft": ["1.21.8", "1.21.9"]}}
        src = make_jar(tmp_path / "l.jar", {"fabric.mod.json": json.dumps(meta)})
        relax_jar(src, tmp_path / "out.jar", "1.21.9", "1.21.10")
        new = json.loads(read(tmp_path / "out.jar", "fabric.mod.json"))
        assert new["depends"]["minecraft"] == ["1.21.8", "1.21.9", "1.21.10"]


class TestRangeUnions:
    """Maven unions and Fabric any-of lists, as loaders read them."""

    def test_maven_union(self):
        from modkeel.version import is_version_in_maven_range as within
        union = "[1.21.6,1.21.8],[1.21.10]"
        assert within("1.21.7", union) and within("1.21.10", union)
        assert not within("1.21.9", union)
        assert within("1.21.5", "[1.21,1.22)") and within("1.21.1", "[1.21.1]")

    def test_fabric_any_of_list_in_metadata(self, tmp_path):
        meta = {"id": "x", "version": "1", "depends": {"minecraft": ["1.21.8", "1.21.10"]}}
        jar = make_jar(tmp_path / "a.jar", {"fabric.mod.json": json.dumps(meta)})
        assert validate_jar(jar, "1.21.10")[0] is True
        assert validate_jar(jar, "1.21.9")[0] is False
