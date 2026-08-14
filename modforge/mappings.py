"""Fetch, cache and index Minecraft mappings.

Phase A supports Mojang official mappings only (``mojmap``). Flavor detection is built out
in full regardless, so Yarn and MCP builds return a clear skip instead of being checked
against the wrong symbol table -- see docs/symbol-check-design.md.

Mojang's mappings carry a EULA that permits modding use but restricts redistribution:
they are downloaded at runtime and cached under the user's home, never vendored.
"""

import logging
import re
from pathlib import Path
from typing import Dict, List, Optional

import requests

from modforge.symbols import SymbolIndex, parse_proguard_mappings

logger = logging.getLogger("modforge")

VERSION_MANIFEST_URL = (
    "https://launchermeta.mojang.com/mc/game/version_manifest_v2.json"
)
CACHE_DIR = Path.home() / ".modforge" / "mappings"

FLAVOR_MOJMAP = "mojmap"
FLAVOR_YARN = "yarn"
FLAVOR_MCP = "mcp"

# Detection is inverted relative to intuition: yarn and mcp are the *marked* cases and
# mojmap is the default. Modern NeoForge builds (ModDevGradle, NeoGradle) never write an
# explicit officialMojangMappings() call -- measuring repos.txt showed an opt-in-based
# detector recognising 1 repository out of 10.
YARN_MARKERS = [
    re.compile(r"net\.fabricmc:yarn:"),
    re.compile(r"yarn_mappings"),
    re.compile(r"yarn\s*=\s*['\"]"),
]
MCP_MARKERS = [
    re.compile(r"mappings\s+channel:\s*['\"]snapshot['\"]"),
    re.compile(r"mappings\s+channel:\s*['\"]stable['\"]"),
]
MOJMAP_MARKERS = [
    re.compile(r"officialMojangMappings"),
    re.compile(r"mappings\s+channel:\s*['\"]official['\"]"),
    re.compile(r"neoforge", re.IGNORECASE),
    re.compile(r"NeoGradle"),
    re.compile(r"net\.neoforged\.moddev"),
    # Parchment renames nothing -- it adds parameter names and javadoc on top of Mojang
    # mappings -- so it implies mojmap for existence checks.
    re.compile(r"parchment", re.IGNORECASE),
]


def detect_flavor(build_scripts: List[str], loader: Optional[str] = None) -> str:
    """Determine which mapping set a mod's source is written against.

    ``build_scripts`` are the raw contents of build.gradle / settings.gradle /
    libs.versions.toml and friends. Yarn wins over mojmap hints because an
    Architectury-style project mentions both.
    """
    joined = "\n".join(s for s in build_scripts if s)

    if any(marker.search(joined) for marker in YARN_MARKERS):
        return FLAVOR_YARN
    if any(marker.search(joined) for marker in MCP_MARKERS):
        return FLAVOR_MCP
    if any(marker.search(joined) for marker in MOJMAP_MARKERS):
        return FLAVOR_MOJMAP

    # Nothing matched. The loader is the last hint available: everything except Fabric
    # and Quilt uses Mojang mappings on modern versions.
    if loader in ("fabric", "quilt"):
        return FLAVOR_YARN
    return FLAVOR_MOJMAP


def resolve_mappings_url(mc_version: str, timeout: int = 15) -> Optional[str]:
    """Resolve the client mappings URL for a Minecraft version, or None."""
    try:
        manifest = requests.get(VERSION_MANIFEST_URL, timeout=timeout).json()
    except (requests.RequestException, ValueError) as e:
        logger.debug("version manifest fetch failed: %s", e)
        return None

    entry = next(
        (v for v in manifest.get("versions", []) if v.get("id") == mc_version), None
    )
    if not entry:
        logger.debug("MC version %s not in manifest", mc_version)
        return None

    try:
        details = requests.get(entry["url"], timeout=timeout).json()
    except (requests.RequestException, ValueError, KeyError) as e:
        logger.debug("version detail fetch failed for %s: %s", mc_version, e)
        return None

    mappings = details.get("downloads", {}).get("client_mappings")
    if not mappings:
        # Versions before 1.14.4 predate published mappings.
        logger.debug("no client_mappings published for %s", mc_version)
        return None

    return mappings.get("url")


def cache_path(mc_version: str, flavor: str) -> Path:
    return CACHE_DIR / f"{flavor}-{mc_version}.json.gz"


def load_index(
    mc_version: str, flavor: str = FLAVOR_MOJMAP, refresh: bool = False
) -> Optional[SymbolIndex]:
    """Return the symbol table for a version, building and caching it on first use.

    Returns None when the flavor is unsupported or the mappings cannot be fetched. The
    caller must treat None as "cannot check", never as "nothing found".
    """
    if flavor != FLAVOR_MOJMAP:
        logger.debug("symbol index unavailable for flavor %s", flavor)
        return None

    path = cache_path(mc_version, flavor)

    if not refresh:
        cached = SymbolIndex.load(path)
        if cached is not None:
            return cached

    url = resolve_mappings_url(mc_version)
    if not url:
        return None

    try:
        text = requests.get(url, timeout=120).text
    except requests.RequestException as e:
        logger.debug("mappings download failed for %s: %s", mc_version, e)
        return None

    index = parse_proguard_mappings(text, mc_version)

    try:
        index.save(path)
    except OSError as e:
        logger.debug("could not cache mappings index: %s", e)

    return index


def prune_cache(keep: Optional[List[str]] = None) -> int:
    """Delete cached indexes, optionally keeping named MC versions. Returns bytes freed."""
    if not CACHE_DIR.exists():
        return 0

    keep = keep or []
    freed = 0

    for path in CACHE_DIR.glob("*.json.gz"):
        if any(f"-{version}.json.gz" in path.name for version in keep):
            continue
        try:
            freed += path.stat().st_size
            path.unlink()
        except OSError:
            pass

    return freed
