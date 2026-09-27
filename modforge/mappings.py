"""Fetch, cache and index Minecraft mappings.

Phase A supports Mojang official mappings only (``mojmap``). Flavor detection is built out
in full regardless, so Yarn and MCP builds return a clear skip instead of being checked
against the wrong symbol table -- see docs/symbol-check-design.md.

Mojang's mappings carry a EULA that permits modding use but restricts redistribution:
they are downloaded at runtime and cached under the user's home, never vendored.
"""

import logging
import re
import zipfile
from pathlib import Path
from typing import Dict, List, Optional

import requests

from modforge.linkage import ClassFileError, parse_class_members
from modforge.symbols import SymbolIndex, is_candidate, parse_proguard_mappings

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


def _param_count(descriptor: str) -> int:
    """Number of parameters in a JVM method descriptor such as (ILjava/lang/String;[J)V."""
    params = descriptor[1:descriptor.index(")")]
    count, i = 0, 0
    while i < len(params):
        while params[i] == "[":
            i += 1
        i = params.index(";", i) + 1 if params[i] == "L" else i + 1
        count += 1
    return count


def index_from_jar(jar_path: Path, mc_version: str) -> SymbolIndex:
    """Build a SymbolIndex from an unobfuscated client JAR (Minecraft 26.1 and later).

    From 26.1 Mojang ships the game with its real names and stops publishing mappings, so
    the class files themselves are the symbol table.
    """
    index = SymbolIndex(flavor=FLAVOR_MOJMAP, mc_version=mc_version)
    with zipfile.ZipFile(jar_path) as jar:
        for entry in jar.namelist():
            if not entry.endswith(".class"):
                continue
            fqcn = entry[:-6].replace("/", ".")
            if not is_candidate(fqcn):
                continue
            try:
                _name, fields, methods = parse_class_members(jar.read(entry))
            except ClassFileError as e:
                logger.debug("skipping %s: %s", entry, e)
                continue
            index.classes.add(fqcn)
            for name, _desc in fields:
                index.fields.setdefault(fqcn, set()).add(name)
            for name, desc in methods:
                if name in ("<init>", "<clinit>"):
                    continue
                index.methods.setdefault(fqcn, set()).add((name, _param_count(desc)))
                index.descriptors.setdefault(fqcn, set()).add(f"{name}{desc}")
    return index


def _version_details(mc_version: str, timeout: int = 15) -> Optional[Dict]:
    try:
        manifest = requests.get(VERSION_MANIFEST_URL, timeout=timeout).json()
        entry = next(
            (v for v in manifest.get("versions", []) if v.get("id") == mc_version), None
        )
        return requests.get(entry["url"], timeout=timeout).json() if entry else None
    except (requests.RequestException, ValueError, KeyError, TypeError) as e:
        logger.debug("version detail fetch failed for %s: %s", mc_version, e)
        return None


def _index_from_client_jar(mc_version: str) -> Optional[SymbolIndex]:
    details = _version_details(mc_version)
    client = (details or {}).get("downloads", {}).get("client")
    if not client or (details or {}).get("downloads", {}).get("client_mappings"):
        return None  # unknown version, or obfuscated (use the mappings instead)
    jar_path = CACHE_DIR / f"client-{mc_version}.jar"
    try:
        jar_path.parent.mkdir(parents=True, exist_ok=True)
        with requests.get(client["url"], timeout=300, stream=True) as r:
            r.raise_for_status()
            with open(jar_path, "wb") as f:
                for chunk in r.iter_content(1 << 20):
                    f.write(chunk)
        return index_from_jar(jar_path, mc_version)
    except (requests.RequestException, OSError, zipfile.BadZipFile) as e:
        logger.debug("client jar index failed for %s: %s", mc_version, e)
        return None
    finally:
        jar_path.unlink(missing_ok=True)


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
    path = cache_path(mc_version, FLAVOR_MOJMAP)

    if not refresh:
        cached = SymbolIndex.load(path)
        if cached is not None:
            # Only unobfuscated versions are cached without mappings; there every
            # flavor sees Mojang's names.
            if flavor == FLAVOR_MOJMAP or cached.flavor == "unobfuscated":
                return cached

    url = resolve_mappings_url(mc_version)
    if not url:
        index = _index_from_client_jar(mc_version)
        if index is None:
            return None
        index.flavor = "unobfuscated"
        try:
            index.save(path)
        except OSError as e:
            logger.debug("could not cache jar index: %s", e)
        return index

    if flavor != FLAVOR_MOJMAP:
        logger.debug("symbol index unavailable for flavor %s", flavor)
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
