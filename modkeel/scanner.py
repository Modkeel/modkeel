"""Mods folder scanner for Modkeel recommend command."""

import hashlib
import json
import logging
import platform
import re
import zipfile
from pathlib import Path
from typing import List, Optional, Tuple

import toml

from modkeel.loaders import LOADER_PROFILES
from modkeel.models import ScannedMod

logger = logging.getLogger("modkeel")

# Mod IDs that are loader internals or APIs, not user-facing mods
LIBRARY_MOD_IDS = {
    "fabric-api", "fabric-loader", "fabricloader",
    "forge", "neoforge", "minecraft", "java",
    "quilt_loader", "quilted_fabric_api",
    "forgified-fabric-api", "connector",
}

# Filename patterns for library sub-modules
LIBRARY_FILENAME_PATTERNS = [
    re.compile(r"^fabric-api-", re.IGNORECASE),
    re.compile(r"^fabric-.*-v\d+", re.IGNORECASE),
]


def compute_sha1(jar_path: Path) -> str:
    """Compute SHA1 hash of a JAR file for Modrinth lookup."""
    h = hashlib.sha1()
    with open(jar_path, "rb") as f:
        for chunk in iter(lambda: f.read(8192), b""):
            h.update(chunk)
    return h.hexdigest()


def _extract_toml_metadata(
    jar: zipfile.ZipFile, toml_path: str,
) -> Tuple[Optional[str], Optional[str], Optional[str], Optional[str], str]:
    """Extract mod metadata from TOML file (NeoForge/Forge mods.toml).

    Returns (mod_id, mod_name, mod_version, mc_range, loader_type).
    """
    with jar.open(toml_path) as f:
        content = f.read().decode("utf-8")
        mod_info = toml.loads(content)

    mod_id = mod_name = mod_version = None
    if "mods" in mod_info and mod_info["mods"]:
        first = mod_info["mods"][0]
        mod_id = first.get("modId")
        mod_name = first.get("displayName", mod_id)
        mod_version = first.get("version", "unknown")

    mc_range = None
    if "dependencies" in mod_info:
        for _deps_key, dep_list in mod_info["dependencies"].items():
            if isinstance(dep_list, list):
                for dep in dep_list:
                    if dep.get("modId") == "minecraft":
                        mc_range = dep.get("versionRange")
                        break
            if mc_range:
                break

    loader = "neoforge" if "neoforge" in toml_path.lower() else "forge"
    return mod_id, mod_name, mod_version, mc_range, loader


def _extract_json_metadata(
    jar: zipfile.ZipFile, json_path: str,
) -> Tuple[Optional[str], Optional[str], Optional[str], Optional[str], str]:
    """Extract mod metadata from JSON file (Fabric/Quilt).

    Returns (mod_id, mod_name, mod_version, mc_range, loader_type).
    """
    with jar.open(json_path) as f:
        data = json.loads(f.read().decode("utf-8"))

    # Quilt schema
    quilt_loader = data.get("quilt_loader", {})
    if quilt_loader:
        mod_id = quilt_loader.get("id")
        mod_name = quilt_loader.get("metadata", {}).get("name", mod_id)
        mod_version = quilt_loader.get("version", "unknown")
        mc_range = None
        for dep in quilt_loader.get("depends", []):
            if isinstance(dep, dict) and dep.get("id") == "minecraft":
                mc_range = dep.get("versions")
                if isinstance(mc_range, list):
                    mc_range = mc_range[0] if mc_range else None
                break
        return mod_id, mod_name, mod_version, mc_range, "quilt"

    # Fabric schema
    mod_id = data.get("id")
    mod_name = data.get("name", mod_id)
    mod_version = data.get("version", "unknown")
    mc_range = data.get("depends", {}).get("minecraft")
    return mod_id, mod_name, mod_version, mc_range, "fabric"


def _is_library_mod(mod_id: str, jar_filename: str) -> bool:
    """Detect if a mod is a library/API rather than a user-facing mod."""
    if mod_id and mod_id.lower() in LIBRARY_MOD_IDS:
        return True
    for pattern in LIBRARY_FILENAME_PATTERNS:
        if pattern.match(jar_filename):
            return True
    return False


def _scan_single_jar(jar_path: Path, sha1: str) -> Optional[ScannedMod]:
    """Extract metadata from a single JAR file."""
    try:
        with zipfile.ZipFile(jar_path, "r") as jar:
            jar_names = set(jar.namelist())

            for _loader_name, profile in LOADER_PROFILES.items():
                fmt = profile["jar_metadata_format"]
                for metadata_file in profile["jar_metadata_files"]:
                    if metadata_file not in jar_names:
                        continue

                    if fmt == "toml":
                        mod_id, mod_name, mod_ver, mc_range, loader = \
                            _extract_toml_metadata(jar, metadata_file)
                    else:
                        mod_id, mod_name, mod_ver, mc_range, loader = \
                            _extract_json_metadata(jar, metadata_file)

                    if not mod_id:
                        continue

                    # Extract exact MC version from range string
                    declared_mc = None
                    if mc_range:
                        mc_match = re.search(r"(\d+\.\d+(?:\.\d+)?)", str(mc_range))
                        if mc_match:
                            declared_mc = mc_match.group(1)

                    return ScannedMod(
                        jar_path=str(jar_path),
                        jar_filename=jar_path.name,
                        mod_id=mod_id,
                        mod_name=mod_name or mod_id,
                        mod_version=mod_ver or "unknown",
                        declared_loader=loader,
                        declared_mc_range=mc_range,
                        declared_mc_version=declared_mc,
                        sha1_hash=sha1,
                        is_library=_is_library_mod(mod_id, jar_path.name),
                    )

    except zipfile.BadZipFile:
        logger.warning("Invalid JAR file: %s", jar_path.name)
    except Exception as e:
        logger.warning("Error scanning %s: %s", jar_path.name, e)

    return None


def scan_mods_folder(mods_dir: Path) -> List[ScannedMod]:
    """Scan a Minecraft mods directory and extract metadata from all JARs.

    Args:
        mods_dir: Path to the mods/ directory.

    Returns:
        List of ScannedMod objects, one per valid mod JAR found.
    """
    if not mods_dir.exists() or not mods_dir.is_dir():
        raise FileNotFoundError(f"Mods directory not found: {mods_dir}")

    jar_files = sorted(mods_dir.glob("*.jar"))
    if not jar_files:
        return []

    scanned: List[ScannedMod] = []

    for jar_path in jar_files:
        try:
            sha1 = compute_sha1(jar_path)
            mod = _scan_single_jar(jar_path, sha1)
            if mod:
                scanned.append(mod)
        except Exception as e:
            logger.warning("Failed to scan %s: %s", jar_path.name, e)

    return scanned


def detect_mods_folder() -> Optional[Path]:
    """Auto-detect the default Minecraft mods folder for the current OS."""
    system = platform.system()

    candidates: List[Path] = []

    if system == "Windows":
        appdata = Path.home() / "AppData" / "Roaming"
        candidates = [
            appdata / ".minecraft" / "mods",
        ]
    elif system == "Darwin":
        candidates = [
            Path.home() / "Library" / "Application Support" / "minecraft" / "mods",
        ]
    else:  # Linux
        candidates = [
            Path.home() / ".minecraft" / "mods",
        ]

    for path in candidates:
        if path.exists() and path.is_dir():
            return path

    return None
