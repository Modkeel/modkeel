"""Tests for modkeel.scanner module."""

import hashlib
import io
import json
import os
import tempfile
import unittest
import zipfile
from pathlib import Path

import toml

from modkeel.scanner import (
    LIBRARY_MOD_IDS,
    _extract_json_metadata,
    _extract_toml_metadata,
    _is_library_mod,
    compute_sha1,
    detect_mods_folder,
    scan_mods_folder,
)


def _make_jar(tmp_dir: Path, filename: str, metadata: dict,
              metadata_path: str) -> Path:
    """Create a synthetic JAR file with the given metadata."""
    jar_path = tmp_dir / filename
    with zipfile.ZipFile(jar_path, "w") as zf:
        if metadata_path.endswith(".toml"):
            zf.writestr(metadata_path, toml.dumps(metadata))
        else:
            zf.writestr(metadata_path, json.dumps(metadata))
    return jar_path


def _neoforge_toml(mod_id: str = "testmod", display_name: str = "Test Mod",
                   version: str = "1.0.0", mc_range: str = "[1.21,1.22)"):
    """Build a NeoForge mods.toml dict."""
    return {
        "modLoader": "javafml",
        "loaderVersion": "[1,)",
        "mods": [{"modId": mod_id, "displayName": display_name, "version": version}],
        "dependencies": {
            mod_id: [
                {"modId": "minecraft", "type": "required", "versionRange": mc_range},
            ]
        },
    }


def _fabric_json(mod_id: str = "testmod", name: str = "Test Mod",
                 version: str = "1.0.0", mc_range: str = ">=1.21.0"):
    """Build a fabric.mod.json dict."""
    return {
        "schemaVersion": 1,
        "id": mod_id,
        "name": name,
        "version": version,
        "depends": {"minecraft": mc_range, "fabricloader": ">=0.15.0"},
    }


def _quilt_json(mod_id: str = "testmod", name: str = "Test Mod",
                version: str = "1.0.0", mc_versions: str = ">=1.21.0"):
    """Build a quilt.mod.json dict."""
    return {
        "schemaVersion": 1,
        "quilt_loader": {
            "id": mod_id,
            "version": version,
            "metadata": {"name": name},
            "depends": [
                {"id": "minecraft", "versions": mc_versions},
            ],
        },
    }


class TestComputeSha1(unittest.TestCase):
    """Test SHA1 hash computation."""

    def test_sha1_matches_known_value(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "test.jar"
            content = b"hello world"
            path.write_bytes(content)
            expected = hashlib.sha1(content).hexdigest()
            self.assertEqual(compute_sha1(path), expected)

    def test_sha1_different_content(self):
        with tempfile.TemporaryDirectory() as tmp:
            p1 = Path(tmp) / "a.jar"
            p2 = Path(tmp) / "b.jar"
            p1.write_bytes(b"content a")
            p2.write_bytes(b"content b")
            self.assertNotEqual(compute_sha1(p1), compute_sha1(p2))


class TestExtractTomlMetadata(unittest.TestCase):
    """Test TOML metadata extraction from JAR files."""

    def test_neoforge_basic(self):
        with tempfile.TemporaryDirectory() as tmp:
            meta = _neoforge_toml("create", "Create", "6.0.3", "[1.21,1.22)")
            jar_path = _make_jar(
                Path(tmp), "create.jar", meta,
                "META-INF/neoforge.mods.toml",
            )
            with zipfile.ZipFile(jar_path) as jar:
                mod_id, name, ver, mc_range, loader = _extract_toml_metadata(
                    jar, "META-INF/neoforge.mods.toml"
                )
            self.assertEqual(mod_id, "create")
            self.assertEqual(name, "Create")
            self.assertEqual(ver, "6.0.3")
            self.assertEqual(mc_range, "[1.21,1.22)")
            self.assertEqual(loader, "neoforge")

    def test_forge_mods_toml(self):
        with tempfile.TemporaryDirectory() as tmp:
            meta = _neoforge_toml("jei", "JEI", "19.0.0", "[1.21,)")
            jar_path = _make_jar(
                Path(tmp), "jei.jar", meta, "META-INF/mods.toml",
            )
            with zipfile.ZipFile(jar_path) as jar:
                mod_id, name, ver, mc_range, loader = _extract_toml_metadata(
                    jar, "META-INF/mods.toml"
                )
            self.assertEqual(mod_id, "jei")
            self.assertEqual(loader, "forge")

    def test_no_dependencies(self):
        meta = {"mods": [{"modId": "simplemod", "version": "1.0"}]}
        with tempfile.TemporaryDirectory() as tmp:
            jar_path = _make_jar(
                Path(tmp), "simple.jar", meta,
                "META-INF/neoforge.mods.toml",
            )
            with zipfile.ZipFile(jar_path) as jar:
                mod_id, name, ver, mc_range, loader = _extract_toml_metadata(
                    jar, "META-INF/neoforge.mods.toml"
                )
            self.assertEqual(mod_id, "simplemod")
            self.assertIsNone(mc_range)


class TestExtractJsonMetadata(unittest.TestCase):
    """Test JSON metadata extraction from JAR files."""

    def test_fabric_basic(self):
        with tempfile.TemporaryDirectory() as tmp:
            meta = _fabric_json("sodium", "Sodium", "0.6.1", ">=1.21.0")
            jar_path = _make_jar(
                Path(tmp), "sodium.jar", meta, "fabric.mod.json",
            )
            with zipfile.ZipFile(jar_path) as jar:
                mod_id, name, ver, mc_range, loader = _extract_json_metadata(
                    jar, "fabric.mod.json"
                )
            self.assertEqual(mod_id, "sodium")
            self.assertEqual(name, "Sodium")
            self.assertEqual(ver, "0.6.1")
            self.assertEqual(mc_range, ">=1.21.0")
            self.assertEqual(loader, "fabric")

    def test_quilt_basic(self):
        with tempfile.TemporaryDirectory() as tmp:
            meta = _quilt_json("mymod", "My Mod", "2.0.0", ">=1.21.0")
            jar_path = _make_jar(
                Path(tmp), "mymod.jar", meta, "quilt.mod.json",
            )
            with zipfile.ZipFile(jar_path) as jar:
                mod_id, name, ver, mc_range, loader = _extract_json_metadata(
                    jar, "quilt.mod.json"
                )
            self.assertEqual(mod_id, "mymod")
            self.assertEqual(name, "My Mod")
            self.assertEqual(ver, "2.0.0")
            self.assertEqual(mc_range, ">=1.21.0")
            self.assertEqual(loader, "quilt")

    def test_quilt_versions_as_list(self):
        meta = {
            "quilt_loader": {
                "id": "testmod",
                "version": "1.0",
                "metadata": {"name": "Test"},
                "depends": [{"id": "minecraft", "versions": [">=1.21.0", "<1.22"]}],
            },
        }
        with tempfile.TemporaryDirectory() as tmp:
            jar_path = _make_jar(
                Path(tmp), "test.jar", meta, "quilt.mod.json",
            )
            with zipfile.ZipFile(jar_path) as jar:
                _, _, _, mc_range, _ = _extract_json_metadata(
                    jar, "quilt.mod.json"
                )
            self.assertEqual(mc_range, ">=1.21.0")


class TestIsLibraryMod(unittest.TestCase):
    """Test library mod detection."""

    def test_fabric_api_is_library(self):
        self.assertTrue(_is_library_mod("fabric-api", "fabric-api-0.92.jar"))

    def test_minecraft_is_library(self):
        self.assertTrue(_is_library_mod("minecraft", "minecraft.jar"))

    def test_forge_is_library(self):
        self.assertTrue(_is_library_mod("forge", "forge.jar"))

    def test_normal_mod_not_library(self):
        self.assertFalse(_is_library_mod("create", "create-6.0.3.jar"))

    def test_fabric_api_submodule_filename(self):
        self.assertTrue(_is_library_mod("some-id", "fabric-api-base-v0.4.jar"))

    def test_connector_is_library(self):
        self.assertTrue(_is_library_mod("connector", "connector.jar"))


class TestScanModsFolder(unittest.TestCase):
    """Test full mods folder scanning."""

    def test_scan_empty_directory(self):
        with tempfile.TemporaryDirectory() as tmp:
            result = scan_mods_folder(Path(tmp))
            self.assertEqual(result, [])

    def test_scan_nonexistent_directory(self):
        with self.assertRaises(FileNotFoundError):
            scan_mods_folder(Path("/nonexistent/path"))

    def test_scan_neoforge_jar(self):
        with tempfile.TemporaryDirectory() as tmp:
            meta = _neoforge_toml("create", "Create", "6.0.3")
            _make_jar(
                Path(tmp), "create.jar", meta,
                "META-INF/neoforge.mods.toml",
            )
            result = scan_mods_folder(Path(tmp))
            self.assertEqual(len(result), 1)
            self.assertEqual(result[0].mod_id, "create")
            self.assertEqual(result[0].mod_name, "Create")
            self.assertEqual(result[0].declared_loader, "neoforge")
            self.assertFalse(result[0].is_library)

    def test_scan_fabric_jar(self):
        with tempfile.TemporaryDirectory() as tmp:
            meta = _fabric_json("sodium", "Sodium", "0.6.1")
            _make_jar(Path(tmp), "sodium.jar", meta, "fabric.mod.json")
            result = scan_mods_folder(Path(tmp))
            self.assertEqual(len(result), 1)
            self.assertEqual(result[0].mod_id, "sodium")
            self.assertEqual(result[0].declared_loader, "fabric")

    def test_scan_multiple_jars(self):
        with tempfile.TemporaryDirectory() as tmp:
            _make_jar(
                Path(tmp), "create.jar",
                _neoforge_toml("create", "Create"),
                "META-INF/neoforge.mods.toml",
            )
            _make_jar(
                Path(tmp), "jei.jar",
                _neoforge_toml("jei", "JEI"),
                "META-INF/neoforge.mods.toml",
            )
            result = scan_mods_folder(Path(tmp))
            self.assertEqual(len(result), 2)
            mod_ids = {m.mod_id for m in result}
            self.assertEqual(mod_ids, {"create", "jei"})

    def test_scan_skips_corrupt_jar(self):
        with tempfile.TemporaryDirectory() as tmp:
            corrupt = Path(tmp) / "corrupt.jar"
            corrupt.write_bytes(b"not a zip file")
            _make_jar(
                Path(tmp), "good.jar",
                _neoforge_toml("good", "Good Mod"),
                "META-INF/neoforge.mods.toml",
            )
            result = scan_mods_folder(Path(tmp))
            self.assertEqual(len(result), 1)
            self.assertEqual(result[0].mod_id, "good")

    def test_scan_skips_jar_without_metadata(self):
        with tempfile.TemporaryDirectory() as tmp:
            jar_path = Path(tmp) / "empty.jar"
            with zipfile.ZipFile(jar_path, "w") as zf:
                zf.writestr("README.txt", "no metadata here")
            result = scan_mods_folder(Path(tmp))
            self.assertEqual(result, [])

    def test_library_filtered_with_flag(self):
        with tempfile.TemporaryDirectory() as tmp:
            _make_jar(
                Path(tmp), "fabric-api.jar",
                _fabric_json("fabric-api", "Fabric API"),
                "fabric.mod.json",
            )
            result = scan_mods_folder(Path(tmp))
            self.assertEqual(len(result), 1)
            self.assertTrue(result[0].is_library)

    def test_declared_mc_version_extracted(self):
        with tempfile.TemporaryDirectory() as tmp:
            _make_jar(
                Path(tmp), "mod.jar",
                _neoforge_toml(mc_range="[1.21.4,1.22)"),
                "META-INF/neoforge.mods.toml",
            )
            result = scan_mods_folder(Path(tmp))
            self.assertEqual(result[0].declared_mc_version, "1.21.4")

    def test_sha1_hash_populated(self):
        with tempfile.TemporaryDirectory() as tmp:
            _make_jar(
                Path(tmp), "mod.jar",
                _neoforge_toml(),
                "META-INF/neoforge.mods.toml",
            )
            result = scan_mods_folder(Path(tmp))
            self.assertIsNotNone(result[0].sha1_hash)
            self.assertEqual(len(result[0].sha1_hash), 40)  # SHA1 hex length


class TestDetectModsFolder(unittest.TestCase):
    """Test auto-detection of mods folder."""

    def test_returns_path_or_none(self):
        result = detect_mods_folder()
        if result is not None:
            self.assertIsInstance(result, Path)
            self.assertTrue(result.exists())


if __name__ == "__main__":
    unittest.main()
