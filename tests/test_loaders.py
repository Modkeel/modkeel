"""
Tests for modkeel.loaders module.

Tests loader profiles, cross-loader chains, bridge mods, known versions,
and all helper functions.
"""

import unittest

from modkeel.loaders import (
    ALL_LOADERS,
    KNOWN_LOADER_VERSIONS,
    LOADER_PROFILES,
    get_bridge_mods,
    get_cross_loader_chain,
    get_docker_server_type,
    get_installer_filename,
    get_installer_url,
    get_known_version,
    get_maven_domain,
    get_profile,
    normalize_loader,
)


class TestLoaderProfiles(unittest.TestCase):
    """Test that all loader profiles have required keys."""

    REQUIRED_KEYS = [
        "display_name",
        "jar_metadata_files",
        "jar_metadata_format",
        "source_metadata_locations",
        "source_metadata_format",
        "gradle_version_patterns",
        "gradle_detection_patterns",
        "version_range_format",
        "docker_server_type",
        "cross_loader_fallback",
        "cross_loader_bridge_mods",
    ]

    def test_all_loaders_have_profiles(self):
        """Every loader in ALL_LOADERS has a profile."""
        for loader in ALL_LOADERS:
            self.assertIn(loader, LOADER_PROFILES)

    def test_profiles_have_required_keys(self):
        """Every profile has all required keys."""
        for loader, profile in LOADER_PROFILES.items():
            for key in self.REQUIRED_KEYS:
                self.assertIn(
                    key, profile,
                    f"Profile '{loader}' missing key '{key}'"
                )

    def test_four_loaders_exist(self):
        """neoforge, forge, fabric, quilt are all present."""
        expected = {"neoforge", "forge", "fabric", "quilt"}
        self.assertEqual(set(ALL_LOADERS), expected)

    def test_metadata_format_valid(self):
        """jar_metadata_format is either 'toml' or 'json'."""
        for loader, profile in LOADER_PROFILES.items():
            self.assertIn(
                profile["jar_metadata_format"], ("toml", "json"),
                f"Profile '{loader}' has invalid jar_metadata_format"
            )

    def test_version_range_format_valid(self):
        """version_range_format is either 'maven' or 'fabric'."""
        for loader, profile in LOADER_PROFILES.items():
            self.assertIn(
                profile["version_range_format"], ("maven", "fabric"),
                f"Profile '{loader}' has invalid version_range_format"
            )


class TestNormalizeLoader(unittest.TestCase):
    """Test normalize_loader function."""

    def test_lowercase_neoforge(self):
        self.assertEqual(normalize_loader("NeoForge"), "neoforge")

    def test_lowercase_fabric(self):
        self.assertEqual(normalize_loader("FABRIC"), "fabric")

    def test_already_lowercase(self):
        self.assertEqual(normalize_loader("quilt"), "quilt")

    def test_unknown_loader_raises(self):
        with self.assertRaises(ValueError):
            normalize_loader("bukkit")


class TestGetProfile(unittest.TestCase):
    """Test get_profile function."""

    def test_returns_dict(self):
        profile = get_profile("neoforge")
        self.assertIsInstance(profile, dict)

    def test_case_insensitive(self):
        profile = get_profile("NeoForge")
        self.assertEqual(profile["display_name"], "NeoForge")

    def test_unknown_raises_key_error(self):
        with self.assertRaises(KeyError):
            get_profile("spigot")


class TestCrossLoaderChain(unittest.TestCase):
    """Test get_cross_loader_chain function."""

    def test_neoforge_falls_back_to_fabric(self):
        chain = get_cross_loader_chain("neoforge")
        self.assertEqual(chain, ["fabric"])

    def test_forge_falls_back_to_fabric(self):
        chain = get_cross_loader_chain("forge")
        self.assertEqual(chain, ["fabric"])

    def test_fabric_no_fallback(self):
        chain = get_cross_loader_chain("fabric")
        self.assertEqual(chain, [])

    def test_quilt_no_fallback(self):
        chain = get_cross_loader_chain("quilt")
        self.assertEqual(chain, [])


class TestBridgeMods(unittest.TestCase):
    """Test get_bridge_mods function."""

    def test_neoforge_to_fabric_bridge(self):
        mods = get_bridge_mods("neoforge", "fabric")
        self.assertIn("connector", mods)
        self.assertIn("forgified-fabric-api", mods)

    def test_forge_to_fabric_bridge(self):
        mods = get_bridge_mods("forge", "fabric")
        self.assertIn("connector", mods)

    def test_fabric_no_bridge(self):
        mods = get_bridge_mods("fabric", "neoforge")
        self.assertEqual(mods, [])

    def test_nonexistent_target(self):
        mods = get_bridge_mods("neoforge", "quilt")
        self.assertEqual(mods, [])


class TestKnownVersions(unittest.TestCase):
    """Test KNOWN_LOADER_VERSIONS and get_known_version."""

    def test_neoforge_has_versions(self):
        self.assertIn("neoforge", KNOWN_LOADER_VERSIONS)
        self.assertTrue(len(KNOWN_LOADER_VERSIONS["neoforge"]) > 0)

    def test_known_neoforge_version(self):
        version = get_known_version("neoforge", "1.21.4")
        self.assertEqual(version, "21.4.156")

    def test_unknown_mc_version_returns_none(self):
        version = get_known_version("neoforge", "99.99.99")
        self.assertIsNone(version)

    def test_unknown_loader_returns_none(self):
        version = get_known_version("quilt", "1.21.4")
        self.assertIsNone(version)


class TestDockerServerType(unittest.TestCase):
    """Test get_docker_server_type function."""

    def test_neoforge_type(self):
        self.assertEqual(get_docker_server_type("neoforge"), "NEOFORGE")

    def test_forge_type(self):
        self.assertEqual(get_docker_server_type("forge"), "FORGE")

    def test_fabric_type(self):
        self.assertEqual(get_docker_server_type("fabric"), "FABRIC")

    def test_quilt_type(self):
        self.assertEqual(get_docker_server_type("quilt"), "FABRIC")


class TestInstallerUrl(unittest.TestCase):
    """Test get_installer_url and get_installer_filename."""

    def test_neoforge_url(self):
        url = get_installer_url("neoforge", "21.4.156")
        self.assertIn("21.4.156", url)
        self.assertIn("maven.neoforged.net", url)

    def test_fabric_no_installer(self):
        url = get_installer_url("fabric", "0.15.0")
        self.assertIsNone(url)

    def test_neoforge_filename(self):
        fname = get_installer_filename("neoforge", "21.4.156")
        self.assertEqual(fname, "neoforge-21.4.156-installer.jar")

    def test_fabric_no_filename(self):
        fname = get_installer_filename("fabric", "0.15.0")
        self.assertIsNone(fname)


class TestMavenDomain(unittest.TestCase):
    """Test get_maven_domain function."""

    def test_neoforge_domain(self):
        self.assertEqual(get_maven_domain("neoforge"), "maven.neoforged.net")

    def test_fabric_no_domain(self):
        self.assertIsNone(get_maven_domain("fabric"))


if __name__ == "__main__":
    unittest.main()
