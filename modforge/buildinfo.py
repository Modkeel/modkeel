"""Extract a branch's build target (Minecraft versions, loaders) from its file tree.

Pure functions: the caller fetches the tree and file contents, this module decides which
files matter and what they declare. Covers the layouts seen in real mods:

- flat ``gradle.properties`` with ``minecraft_version`` / ``mc_version`` / ``minecraftVersion``
- per-platform ``NeoForge/gradle.properties`` (multi-loader)
- Stonecutter ``versions/<mc>-<loader>/gradle.properties`` (one branch, many targets)
- Kotlin build logic: ``val MINECRAFT_VERSION by extra { "1.21.10" }``,
  ``val MINECRAFT_VERSION: String = "1.21.10"`` in buildSrc
- version catalogs (``gradle/libs.versions.toml``)
- ``${key}`` placeholders in metadata, resolved from gradle.properties
"""

import json
import re
from dataclasses import dataclass, field
from typing import Dict, Iterable, List, Optional, Set, Tuple

VERSION = r"\d+\.\d+(?:\.\d+)?"

# A key naming the Minecraft version, in any casing: minecraft_version, mcVersion,
# MINECRAFT_VERSION, minecraft-version. The lookbehind rejects prefixed keys such as
# parchmentMinecraftVersion; the lookahead rejects suffixed ones such as
# minecraftVersionRange.
_MC_KEY = r"(?<![A-Za-z0-9_])(?:minecraft|mc|game)[_\-.]?version(?![A-Za-z0-9_])"

_MC_DECLARATIONS = [
    # key = 1.21.10 / key: "1.21.10" / key = "1.21.10"
    re.compile(_MC_KEY + r"\s*(?::\s*String\s*)?[=:]\s*[\"']?(" + VERSION + r")[\"']?(?![\d.])",
               re.IGNORECASE),
    # val MINECRAFT_VERSION by extra { "1.21.10" }
    re.compile(_MC_KEY + r"\s+by\s+extra\s*\{\s*[\"'](" + VERSION + r")[\"']", re.IGNORECASE),
    # "minecraft_version" to "1.21.10"
    re.compile(r"[\"']" + _MC_KEY + r"[\"']\s+to\s+[\"'](" + VERSION + r")[\"']", re.IGNORECASE),
]

_LOADER_KEYS = {
    "neoforge": re.compile(r"(?<![A-Za-z])neo[_\-.]?forge[_\-.]?version|deps\.neoforge",
                           re.IGNORECASE),
    "forge": re.compile(r"(?<![A-Za-z])(?<!neo)(?<!neo_)forge[_\-.]?version", re.IGNORECASE),
    "fabric": re.compile(r"fabric[_\-.]?(?:loader|api)[_\-.]?version|deps\.fabric",
                         re.IGNORECASE),
    "quilt": re.compile(r"quilt[_\-.]?loader[_\-.]?version", re.IGNORECASE),
}

_METADATA_LOADER = {
    "neoforge.mods.toml": "neoforge",
    "mods.toml": "forge",
    "fabric.mod.json": "fabric",
    "quilt.mod.json": "quilt",
}

_STONECUTTER_DIR = re.compile(r"^versions/([^/]+)/")

_MAX_BUILDSRC_FILES = 6


@dataclass
class BuildInfo:
    """What a branch builds against, as far as its files declare."""

    mc_versions: Set[str] = field(default_factory=set)
    loaders: Set[str] = field(default_factory=set)
    # Minecraft dependency ranges from metadata, placeholders already resolved
    ranges: Dict[str, List[str]] = field(default_factory=dict)  # loader -> ranges
    sources: List[str] = field(default_factory=list)  # files that contributed
    # Stonecutter (mc, loader) pairs; empty when the layout does not pair them
    targets: Set[Tuple[str, str]] = field(default_factory=set)

    def ranges_for(self, loader: str) -> List[str]:
        return self.ranges.get(loader, [])


def select_build_files(paths: Iterable[str]) -> List[str]:
    """Pick the files worth fetching from a recursive tree listing."""
    selected: List[str] = []
    buildsrc: List[str] = []
    for path in paths:
        name = path.rsplit("/", 1)[-1]
        depth = path.count("/")
        if "/test" in path or path.startswith("test"):
            continue
        if name == "gradle.properties" and (depth <= 1 or _STONECUTTER_DIR.match(path)):
            selected.append(path)
        elif name in ("build.gradle.kts", "build.gradle", "settings.gradle.kts",
                      "settings.gradle", "stonecutter.gradle.kts", "stonecutter.gradle") \
                and depth == 0:
            selected.append(path)
        elif path == "gradle/libs.versions.toml":
            selected.append(path)
        elif name in _METADATA_LOADER and "/resources/" in path or \
                name in _METADATA_LOADER and "/templates/" in path:
            selected.append(path)
        elif path.startswith("buildSrc/") and name.endswith((".kt", ".kts", ".properties")):
            buildsrc.append(path)
    # Prefer small, config-like buildSrc files; they carry the constants
    buildsrc.sort(key=lambda p: (not re.search(r"config|constant|version", p, re.I), len(p)))
    return selected + buildsrc[:_MAX_BUILDSRC_FILES]


def loaders_from_tree(paths: Iterable[str]) -> Set[str]:
    """Loaders implied by the layout alone: platform subprojects, metadata files."""
    found: Set[str] = set()
    for path in paths:
        top = path.split("/", 1)[0].lower()
        if top in ("neoforge", "forge", "fabric", "quilt") and "/" in path:
            found.add(top)
        name = path.rsplit("/", 1)[-1]
        if name in _METADATA_LOADER and ("/resources/" in path or "/templates/" in path) \
                and "/test" not in path:
            found.add(_METADATA_LOADER[name])
        match = _STONECUTTER_DIR.match(path)
        if match:
            for loader in ("neoforge", "forge", "fabric", "quilt"):
                if match.group(1).lower().endswith(loader) and not (
                        loader == "forge" and match.group(1).lower().endswith("neoforge")):
                    found.add(loader)
    return found


def parse_properties(content: str) -> Dict[str, str]:
    props: Dict[str, str] = {}
    for line in content.splitlines():
        line = line.strip()
        if not line or line.startswith(("#", "!")):
            continue
        match = re.match(r"([^=:\s]+)\s*[=:]\s*(.*)", line)
        if match:
            props[match.group(1)] = match.group(2).strip()
    return props


def find_mc_versions(content: str) -> Set[str]:
    versions: Set[str] = set()
    for pattern in _MC_DECLARATIONS:
        versions.update(m.group(1) for m in pattern.finditer(content))
    return versions


def find_loaders(content: str) -> Set[str]:
    found = {name for name, pattern in _LOADER_KEYS.items() if pattern.search(content)}
    return found


def resolve_placeholders(value: str, props: Dict[str, str]) -> Optional[str]:
    """Expand ${key} from gradle.properties. None if anything stays unresolved."""
    resolved = re.sub(r"\$\{([^}]+)\}", lambda m: props.get(m.group(1), m.group(0)), value)
    return None if "${" in resolved else resolved


def _metadata_ranges(path: str, content: str) -> List[str]:
    name = path.rsplit("/", 1)[-1]
    if name.endswith(".toml"):
        pattern = (r'\[\[dependencies\.[^\]]+\]\][^\[]*?modId\s*=\s*["\']minecraft["\']'
                   r'[^\[]*?versionRange\s*=\s*["\']([^"\']+)["\']')
        return re.findall(pattern, content, re.DOTALL | re.IGNORECASE)
    try:
        data = json.loads(content)
    except ValueError:
        return []
    ranges: List[str] = []
    dep = data.get("depends", {}).get("minecraft") if isinstance(data, dict) else None
    if isinstance(dep, str):
        ranges.append(dep)
    elif isinstance(dep, list):
        ranges.extend(str(d) for d in dep)
    for qdep in data.get("quilt_loader", {}).get("depends", []) if isinstance(data, dict) else []:
        if isinstance(qdep, dict) and qdep.get("id") == "minecraft" and qdep.get("versions"):
            ranges.append(str(qdep["versions"]))
    return ranges


def extract_build_info(paths: Iterable[str], files: Dict[str, str]) -> BuildInfo:
    """Combine the tree layout and fetched file contents into one BuildInfo."""
    paths = list(paths)
    info = BuildInfo(loaders=loaders_from_tree(paths))

    props: Dict[str, str] = {}
    for path, content in files.items():
        if path.endswith("gradle.properties") and path.count("/") == 0:
            props.update(parse_properties(content))
    for path, content in files.items():
        if path.endswith("gradle.properties") and path.count("/") > 0:
            for key, value in parse_properties(content).items():
                props.setdefault(key, value)

    for path, content in files.items():
        name = path.rsplit("/", 1)[-1]
        if name in _METADATA_LOADER:
            loader = _METADATA_LOADER[name]
            for raw in _metadata_ranges(path, content):
                resolved = resolve_placeholders(raw, props)
                if resolved:
                    info.ranges.setdefault(loader, []).append(resolved)
                    info.sources.append(path)
            continue

        if path == "gradle/libs.versions.toml":
            match = re.search(r"^\s*(?:minecraft|minecraft-version|game-version)\s*=\s*[\"']("
                              + VERSION + r")[\"']", content, re.MULTILINE)
            if match:
                info.mc_versions.add(match.group(1))
                info.sources.append(path)
            info.loaders.update(find_loaders(content))
            continue

        versions = find_mc_versions(content)
        if versions:
            info.mc_versions.update(versions)
            info.sources.append(path)
        info.loaders.update(find_loaders(content))

    # Stonecutter: the directory name is authoritative when the file omits the version
    for path in paths:
        match = _STONECUTTER_DIR.match(path)
        if match:
            dir_version = re.match(VERSION, match.group(1))
            if dir_version:
                info.mc_versions.add(dir_version.group(0))
                suffix = match.group(1)[dir_version.end():].lstrip("-_").lower()
                if suffix in ("neoforge", "forge", "fabric", "quilt"):
                    info.targets.add((dir_version.group(0), suffix))

    return info
