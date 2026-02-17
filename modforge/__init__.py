"""
ModForge - Minecraft Mod Auto-Compiler

Automatically detects, compiles, and installs mods from GitHub repositories
for specific Minecraft versions and mod loaders.
"""

from modforge.constants import MODFORGE_VERSION
from modforge.loaders import (  # noqa: F401
    ALL_LOADERS,
    KNOWN_LOADER_VERSIONS,
    LOADER_PROFILES,
    get_bridge_mods,
    get_cross_loader_chain,
    get_docker_server_type,
    get_installer_url,
    get_known_version,
    get_maven_domain,
    get_profile,
    normalize_loader,
)

from modforge.recommend import RecommendationEngine  # noqa: F401
from modforge.scanner import detect_mods_folder, scan_mods_folder  # noqa: F401

__version__ = MODFORGE_VERSION
