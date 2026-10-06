"""Loader profiles for Modkeel - single source of truth for loader-specific data."""

import re
from typing import Dict, List, Optional


# ============================================================================
# LOADER PROFILES
# ============================================================================
LOADER_PROFILES: Dict[str, dict] = {
    "neoforge": {
        "display_name": "NeoForge",
        "jar_metadata_files": [
            "META-INF/neoforge.mods.toml",
            "META-INF/mods.toml",
        ],
        "jar_metadata_format": "toml",
        "source_metadata_locations": [
            "neoforge/src/main/resources/META-INF/mods.toml",
            "forge/src/main/resources/META-INF/mods.toml",
            "src/main/resources/META-INF/mods.toml",
            "neoforge/src/main/resources/META-INF/neoforge.mods.toml",
            "src/main/resources/META-INF/neoforge.mods.toml",
        ],
        "source_metadata_format": "toml",
        "gradle_version_patterns": [
            r'neo(?:forge)?_version\s*=\s*["\']?([0-9.]+)["\']?',
        ],
        "gradle_detection_patterns": [
            r'neo(?:forge)?_version\s*=',
        ],
        "version_range_format": "maven",
        "require_loader_version_in_gradle": True,
        "docker_server_type": "NEOFORGE",
        "docker_version_env": "NEOFORGE_VERSION",
        "installer_url_template": (
            "https://maven.neoforged.net/releases/net/neoforged/"
            "neoforge/{version}/neoforge-{version}-installer.jar"
        ),
        "installer_filename_template": "neoforge-{version}-installer.jar",
        "maven_domain": "maven.neoforged.net",
        "cross_loader_fallback": ["fabric"],
        "cross_loader_bridge_mods": {
            "fabric": ["connector", "forgified-fabric-api"],
        },
    },
    "forge": {
        "display_name": "Forge",
        "jar_metadata_files": [
            "META-INF/mods.toml",
        ],
        "jar_metadata_format": "toml",
        "source_metadata_locations": [
            "forge/src/main/resources/META-INF/mods.toml",
            "src/main/resources/META-INF/mods.toml",
        ],
        "source_metadata_format": "toml",
        "gradle_version_patterns": [
            r'forge_version\s*=\s*["\']?([0-9.]+)["\']?',
        ],
        "gradle_detection_patterns": [
            r'forge_version\s*=',
        ],
        "version_range_format": "maven",
        "require_loader_version_in_gradle": False,
        "docker_server_type": "FORGE",
        "docker_version_env": "FORGE_VERSION",
        "installer_url_template": None,
        "installer_filename_template": None,
        "maven_domain": "files.minecraftforge.net",
        "cross_loader_fallback": ["fabric"],
        "cross_loader_bridge_mods": {
            "fabric": ["connector", "forgified-fabric-api"],
        },
    },
    "fabric": {
        "display_name": "Fabric",
        "jar_metadata_files": [
            "fabric.mod.json",
        ],
        "jar_metadata_format": "json",
        "source_metadata_locations": [
            "common/src/main/resources/fabric.mod.json",
            "fabric/src/main/resources/fabric.mod.json",
            "src/main/resources/fabric.mod.json",
        ],
        "source_metadata_format": "json",
        "gradle_version_patterns": [
            r'fabric_(?:loader|api)_version\s*=\s*["\']?([0-9.]+)["\']?',
        ],
        "gradle_detection_patterns": [
            r'fabric_(?:loader|api)_version\s*=',
        ],
        "version_range_format": "fabric",
        "require_loader_version_in_gradle": False,
        "docker_server_type": "FABRIC",
        "docker_version_env": "FABRIC_LOADER_VERSION",
        "installer_url_template": None,
        "installer_filename_template": None,
        "maven_domain": None,
        "cross_loader_fallback": [],
        "cross_loader_bridge_mods": {},
    },
    "quilt": {
        "display_name": "Quilt",
        "jar_metadata_files": [
            "quilt.mod.json",
            "fabric.mod.json",
        ],
        "jar_metadata_format": "json",
        "source_metadata_locations": [
            "src/main/resources/quilt.mod.json",
            "quilt/src/main/resources/quilt.mod.json",
            "src/main/resources/fabric.mod.json",
        ],
        "source_metadata_format": "json",
        "gradle_version_patterns": [
            r'quilt_(?:loader|mappings)_version\s*=\s*["\']?([0-9.]+)["\']?',
        ],
        "gradle_detection_patterns": [
            r'quilt_(?:loader|mappings)_version\s*=',
        ],
        "version_range_format": "fabric",
        "require_loader_version_in_gradle": False,
        "docker_server_type": "FABRIC",
        # Quilt runs as TYPE=FABRIC: a Quilt loader version is not a Fabric one.
        "docker_version_env": None,
        "installer_url_template": None,
        "installer_filename_template": None,
        "maven_domain": None,
        "cross_loader_fallback": [],
        "cross_loader_bridge_mods": {},
    },
}


# ============================================================================
# KNOWN LOADER VERSIONS (for Docker testing / installer download)
# ============================================================================
KNOWN_LOADER_VERSIONS: Dict[str, Dict[str, str]] = {
    "neoforge": {
        "1.21.4": "21.4.156",
        "1.21.3": "21.3.56",
        "1.21.1": "21.1.94",
        "1.21": "21.0.167",
        "1.20.6": "20.6.120",
        "1.20.4": "20.4.263",
    },
}

ALL_LOADERS: List[str] = list(LOADER_PROFILES.keys())


# ============================================================================
# HELPER FUNCTIONS
# ============================================================================

def get_profile(loader: str) -> dict:
    """Get the full profile dict for a loader. Raises KeyError if unknown."""
    loader = loader.lower()
    if loader not in LOADER_PROFILES:
        raise KeyError(
            f"Unknown loader '{loader}'. "
            f"Must be one of: {', '.join(ALL_LOADERS)}"
        )
    return LOADER_PROFILES[loader]


def normalize_loader(loader: str) -> str:
    """Normalize loader name to lowercase, validate it exists."""
    loader = loader.lower()
    if loader not in LOADER_PROFILES:
        raise ValueError(
            f"Unknown loader '{loader}'. "
            f"Must be one of: {', '.join(ALL_LOADERS)}"
        )
    return loader


def get_cross_loader_chain(loader: str) -> List[str]:
    """Get the list of fallback loaders for cross-loader compilation."""
    profile = get_profile(loader)
    return profile.get("cross_loader_fallback", [])


def get_bridge_mods(loader: str, target_loader: str) -> List[str]:
    """Get bridge mod slugs needed to run target_loader mods on loader."""
    profile = get_profile(loader)
    bridge_mods = profile.get("cross_loader_bridge_mods", {})
    return bridge_mods.get(target_loader, [])


def get_known_version(loader: str, mc_version: str) -> Optional[str]:
    """Get the known loader version for a given MC version, or None."""
    loader_versions = KNOWN_LOADER_VERSIONS.get(loader.lower(), {})
    return loader_versions.get(mc_version)


# A loader version the user can mean literally: "21.10.64", "0.19.5", "21.10.62-beta".
# "0" (the sentinel search/get pass when no -lv is given) or a bare build number like
# "64" is not one, so it never reaches an installer URL or the server image.
_EXPLICIT_LOADER_VERSION = re.compile(r"\d+(?:\.\d+)+(?:[-+.][\w.+-]*)?")


def is_explicit_loader_version(version: Optional[str]) -> bool:
    """True when version is a full loader version (dotted), not a sentinel or a fragment."""
    return bool(version) and _EXPLICIT_LOADER_VERSION.fullmatch(version) is not None


def get_docker_version_env(loader: str) -> Optional[str]:
    """itzg/minecraft-server env var that pins this loader's version, or None."""
    return get_profile(loader).get("docker_version_env")


def get_docker_server_type(loader: str) -> str:
    """Get the Docker server type string for itzg/minecraft-server."""
    profile = get_profile(loader)
    return profile["docker_server_type"]


def get_installer_url(loader: str, version: str) -> Optional[str]:
    """Get the installer download URL for a loader version, or None."""
    profile = get_profile(loader)
    template = profile.get("installer_url_template")
    if not template:
        return None
    return template.format(version=version)


def get_installer_filename(loader: str, version: str) -> Optional[str]:
    """Get the installer filename for a loader version, or None."""
    profile = get_profile(loader)
    template = profile.get("installer_filename_template")
    if not template:
        return None
    return template.format(version=version)


def get_maven_domain(loader: str) -> Optional[str]:
    """Get the Maven domain for error messages."""
    profile = get_profile(loader)
    return profile.get("maven_domain")
