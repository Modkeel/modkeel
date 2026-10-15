"""The player's Minecraft instances, found where each launcher keeps them.

    find_instances() -> [Instance(launcher, name, path, mods_dir, mc_version, loader, ...)]

A player picks "my ATM10 pack" from a list instead of looking for a mods folder. Each launcher
stores its instances its own way; everything here only reads:

    Prism Launcher  <data>/instances/<dir>/   instance.cfg (name), mmc-pack.json (components:
                    net.minecraft, the loader's uid), the game in .minecraft/ or minecraft/;
                    <data>/prismlauncher.cfg may move the instances (InstanceDir)
    PolyMC          the same layout in <data>/PolyMC (polymc.cfg); NeoForge's uid is
                    net.neoforged.neoforge. MultiMC is portable (its data is wherever the
                    player unpacked it), so it has no usual place to look
    ATLauncher      <data>/instances/<dir>/instance.json: id (Minecraft), launcher.name,
                    launcher.loaderVersion {type, version}; mods/
    GDLauncher      legacy: <appData>/gdlauncher_next/instances/<name>/config.json (name =
                    folder; loader {loaderType, mcVersion, loaderVersion}); mods/.
                    Carbon: <data>/gdlauncher_carbon/data/instances/<dir>/instance.json
                    (name, game_configuration.version {release, modloaders [{type,
                    version}]}); mods in instance/mods
    FTB App         <home>/.ftba/instances/<dir>/instance.json (Windows: AppData/Local/.ftba):
                    name, mcVersion, modLoader (a version id such as
                    "fabric-loader-<mc>-<loader>", "neoforge-21.1.77", or the Minecraft
                    version for vanilla); mods/
    Modrinth App    <data>/profiles/<path>/   app.db (SQLite, table profiles: name,
                    game_version, mod_loader, mod_loader_version); profile.json in older
                    versions (the launcher was called theseus)
    CurseForge      <home>/curseforge/minecraft/Instances/<dir>/ (Documents/ on macOS)
                    minecraftinstance.json (name, gameVersion, baseModLoader.name such as
                    "neoforge-21.1.77" or "fabric-0.16.5-1.21.1")
    Minecraft       .minecraft/launcher_profiles.json, shared by the official launcher and
                    SKlauncher (its own files in .minecraft/sklauncher/): profiles{name,
                    lastVersionId, gameDir}; the id's versions/<id>/<id>.json names the
                    Minecraft version (inheritsFrom), the id itself the loader:
                    "fabric-loader-0.18.3-1.21.11", "1.21.10-forge-60.1.4", "neoforge-21.1.216".
                    Profiles sharing one game dir (all on .minecraft) are one instance, its
                    version only when they agree; .minecraft/mods without a profile is listed too

Icons, where the launcher keeps one: Prism/PolyMC <data>/icons/<iconKey>.* (IconsDir may move
them; built-in keys have no file), Modrinth App profiles.icon_path (metadata.icon in older
profile.json), CurseForge profileImagePath, ATLauncher instance.png, FTB App .ftbapp/logo, the
official launcher's profile icon when it is a data: URI (the rest name built-in blocks).
icon_data(icon) turns one into a data: URI a screen can show, or None.

A field a launcher does not give (or a file that cannot be read) is None, never a guess; an
instance with no mods folder yet is still listed (mods = 0). Paths come from the platform's
usual places; `home`, `system` and `env` are parameters so tests can build any of them.

    launcher_records(mods_dir) -> {file name: LauncherRecord(source, project_id, name, ...)}

What the launcher itself installed in a mods folder, from its own index: Prism's
mods/.index/<slug>.pw.toml (packwiz format, also *.pw.toml beside the JARs of a packwiz
pack) and CurseForge's minecraftinstance.json (installedAddons). It names the project a JAR
came from even when the file is not on Modrinth (a CurseForge download). A record whose hash
no longer matches the JAR on disk (replaced since) is dropped.
"""

from __future__ import annotations

import base64
import configparser
import hashlib
import json
import os
import platform
import sqlite3
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Dict, Iterable, List, Mapping, Optional, Tuple
from urllib.parse import urlparse

import toml

LAUNCHERS = ("prism", "polymc", "modrinth", "curseforge", "atlauncher", "gdlauncher", "ftb",
             "minecraft", "sklauncher")
LAUNCHER_NAMES = {"prism": "Prism Launcher", "polymc": "PolyMC", "modrinth": "Modrinth App",
                  "curseforge": "CurseForge", "atlauncher": "ATLauncher",
                  "gdlauncher": "GDLauncher", "ftb": "FTB App",
                  "minecraft": "Minecraft Launcher", "sklauncher": "SKlauncher"}

# Prism / MultiMC component uids of each loader
PRISM_LOADERS = {
    "net.fabricmc.fabric-loader": "fabric",
    "org.quiltmc.quilt-loader": "quilt",
    "net.neoforged": "neoforge",
    "net.minecraftforge": "forge",
}
POLYMC_LOADERS = {**PRISM_LOADERS, "net.neoforged.neoforge": "neoforge"}


@dataclass
class Instance:
    """One playable instance: where its mods are and what it runs."""

    launcher: str                    # one of LAUNCHERS
    name: str
    path: str                        # the instance's folder (its game directory's parent)
    mods_dir: str                    # <game dir>/mods, may not exist yet
    mc_version: Optional[str] = None
    loader: Optional[str] = None     # fabric | quilt | neoforge | forge, None = vanilla/unknown
    loader_version: Optional[str] = None
    mods: int = 0                    # JARs in mods_dir
    icon: Optional[str] = None       # the launcher's picture for it: a file, or a data: URI

    def to_dict(self) -> Dict:
        return asdict(self)


def _data_dirs(name: str, home: Path, system: str, env: Mapping[str, str]) -> List[Path]:
    """Where an app named `name` keeps its data on this platform (most usual first)."""
    if system == "Windows":
        appdata = Path(env.get("APPDATA") or home / "AppData" / "Roaming")
        return [appdata / name]
    if system == "Darwin":
        return [home / "Library" / "Application Support" / name]
    data = Path(env.get("XDG_DATA_HOME") or home / ".local" / "share")
    return [data / name]


def _count_jars(mods_dir: Path) -> int:
    try:
        return sum(1 for p in mods_dir.iterdir() if p.suffix == ".jar" and p.is_file())
    except OSError:
        return 0


def _read_json(path: Path) -> Optional[Dict]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


ICON_MAX_BYTES = 512 * 1024
_ICON_TYPES = ((b"\x89PNG\r\n\x1a\n", "image/png"), (b"\xff\xd8\xff", "image/jpeg"),
               (b"GIF87a", "image/gif"), (b"GIF89a", "image/gif"))


def _file(path: Optional[Path]) -> Optional[str]:
    return str(path) if path is not None and path.is_file() else None


def icon_data(icon: Optional[str]) -> Optional[str]:
    """An instance's icon as a data: URI: a file read and typed by its first bytes (PNG, JPEG,
    GIF, WebP), a data: image URI as it is. Anything else, missing or too big gives None."""
    if not icon:
        return None
    if icon.startswith("data:image/"):
        return icon if len(icon) <= ICON_MAX_BYTES * 4 // 3 + 64 else None
    try:
        path = Path(icon)
        if path.stat().st_size > ICON_MAX_BYTES:
            return None
        raw = path.read_bytes()
    except (OSError, ValueError):
        return None
    # FTB App's logo has one byte before the image: look a few bytes in, keep from the image on
    for skip in range(4):
        image = raw[skip:]
        kind = next((t for magic, t in _ICON_TYPES if image.startswith(magic)), None)
        if kind is None and image[:4] == b"RIFF" and image[8:12] == b"WEBP":
            kind = "image/webp"
        if kind:
            return f"data:{kind};base64,{base64.b64encode(image).decode('ascii')}"
    return None


def _read_cfg(path: Path) -> Dict[str, str]:
    """A Qt-style .cfg (Prism): key=value lines, with or without a [General] section."""
    parser = configparser.ConfigParser(interpolation=None, strict=False)
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
        parser.read_string(text if text.lstrip().startswith("[") else "[General]\n" + text)
    except (OSError, configparser.Error):
        return {}
    return {k: v for section in parser.sections() for k, v in parser[section].items()}


# --- Prism Launcher -------------------------------------------------------------------------

def _prism_roots(home: Path, system: str, env: Mapping[str, str]) -> List[Path]:
    roots = _data_dirs("PrismLauncher", home, system, env)
    if system not in ("Windows", "Darwin"):   # the Flatpak keeps its own data
        roots.append(home / ".var" / "app" / "org.prismlauncher.PrismLauncher" / "data"
                     / "PrismLauncher")
    return roots


def _polymc_roots(home: Path, system: str, env: Mapping[str, str]) -> List[Path]:
    roots = _data_dirs("PolyMC", home, system, env)
    if system not in ("Windows", "Darwin"):
        roots.insert(0, roots[0].parent / "polymc")   # the legacy folder wins when present
        roots.append(home / ".var" / "app" / "org.polymc.PolyMC" / "data" / "PolyMC")
    return roots


def _prism_instances_dir(root: Path, cfg_name: str = "prismlauncher.cfg") -> Path:
    custom = _read_cfg(root / cfg_name).get("instancedir")
    if custom:
        p = Path(custom).expanduser()
        return p if p.is_absolute() else root / p
    return root / "instances"


def _prism_icon(icons: Path, key: Optional[str]) -> Optional[str]:
    """The file of a custom icon key; built-in keys ("default", "grass"...) have none."""
    if not key or not icons.is_dir():
        return None
    for ext in (".png", ".jpg", ".jpeg", ".gif", ".webp"):
        found = _file(icons / f"{key}{ext}")
        if found:
            return found
    return None


def _prism(root: Path, launcher: str = "prism",
           cfg_name: str = "prismlauncher.cfg") -> Iterable[Instance]:
    base = _prism_instances_dir(root, cfg_name)
    custom = _read_cfg(root / cfg_name).get("iconsdir")
    icons = Path(custom).expanduser() if custom else root / "icons"
    if not icons.is_absolute():
        icons = root / icons
    if not base.is_dir():
        return
    for folder in sorted(p for p in base.iterdir() if p.is_dir()):
        if not (folder / "instance.cfg").is_file():
            continue                          # _LAUNCHER_TEMP, _MMC_TEMP, groups...
        cfg = _read_cfg(folder / "instance.cfg")
        mc = loader = loader_version = None
        for comp in (_read_json(folder / "mmc-pack.json") or {}).get("components", []):
            uid = comp.get("uid") if isinstance(comp, dict) else None
            if uid == "net.minecraft":
                mc = comp.get("version")
            elif uid in POLYMC_LOADERS:
                loader, loader_version = POLYMC_LOADERS[uid], comp.get("version")
        game = next((folder / d for d in ("minecraft", ".minecraft")
                     if (folder / d).is_dir()), folder / ".minecraft")
        mods = game / "mods"
        yield Instance(launcher, cfg.get("name") or folder.name, str(folder), str(mods),
                       mc, loader, loader_version, _count_jars(mods),
                       _prism_icon(icons, cfg.get("iconkey")))


# --- Modrinth App ---------------------------------------------------------------------------

def _modrinth_roots(home: Path, system: str, env: Mapping[str, str]) -> List[Path]:
    return [*_data_dirs("ModrinthApp", home, system, env),
            *_data_dirs("com.modrinth.theseus", home, system, env)]


def _modrinth_db(root: Path) -> Dict[str, Tuple[Optional[str], ...]]:
    """profiles.path -> (name, game_version, mod_loader, mod_loader_version, icon_path) from
    app.db.

    Opened read-only and immutable: the app may be running and holding its own lock, and we
    must never write to it. An unreadable or differently shaped database gives {}."""
    db = root / "app.db"
    if not db.is_file():
        return {}
    try:
        con = sqlite3.connect(f"{db.as_uri()}?mode=ro&immutable=1", uri=True)
        try:
            columns = {r[1] for r in con.execute("PRAGMA table_info(profiles)")}
            icon = "icon_path" if "icon_path" in columns else "NULL"   # not in every version
            rows = con.execute("SELECT path, name, game_version, mod_loader, mod_loader_version, "
                               f"{icon} FROM profiles").fetchall()
        finally:
            con.close()
    except sqlite3.Error:
        return {}
    return {str(r[0]): tuple(r[1:]) for r in rows}


def _modrinth(root: Path) -> Iterable[Instance]:
    base = root / "profiles"
    if not base.is_dir():
        return
    known = _modrinth_db(root)
    for folder in sorted(p for p in base.iterdir() if p.is_dir()):
        name, mc, loader, loader_version, icon = known.get(folder.name, (None,) * 5)
        if folder.name not in known:          # older app: profile.json in the folder
            data = _read_json(folder / "profile.json")
            if data is None:
                continue                      # not a profile
            meta = data.get("metadata") or {}
            name, mc, icon = meta.get("name"), meta.get("game_version"), meta.get("icon")
            loader, loader_version = meta.get("loader"), (meta.get("loader_version") or {})
            if isinstance(loader_version, dict):
                loader_version = loader_version.get("id")
        loader = None if not loader or loader == "vanilla" else str(loader).lower()
        mods = folder / "mods"
        yield Instance("modrinth", name or folder.name, str(folder), str(mods), mc, loader,
                       loader_version, _count_jars(mods),
                       _file(Path(icon)) if isinstance(icon, str) and icon else None)


# --- CurseForge -----------------------------------------------------------------------------

def _curseforge_roots(home: Path, system: str, env: Mapping[str, str]) -> List[Path]:
    if system == "Darwin":
        return [home / "Documents" / "curseforge" / "minecraft" / "Instances"]
    return [home / "curseforge" / "minecraft" / "Instances"]


def _curseforge_loader(name: Optional[str]) -> Tuple[Optional[str], Optional[str]]:
    """"neoforge-21.1.77" -> (neoforge, 21.1.77); "fabric-0.16.5-1.21.1" -> (fabric, 0.16.5)."""
    if not name or "-" not in name:
        return None, None
    kind, rest = name.split("-", 1)
    kind = kind.lower()
    if kind not in ("fabric", "quilt", "neoforge", "forge"):
        return None, None
    return kind, rest.split("-", 1)[0] if kind in ("fabric", "quilt") else rest


def _curseforge(root: Path) -> Iterable[Instance]:
    if not root.is_dir():
        return
    for folder in sorted(p for p in root.iterdir() if p.is_dir()):
        data = _read_json(folder / "minecraftinstance.json")
        if data is None:
            continue
        loader, loader_version = _curseforge_loader((data.get("baseModLoader") or {}).get("name"))
        mods = folder / "mods"
        image = data.get("profileImagePath")
        yield Instance("curseforge", data.get("name") or folder.name, str(folder), str(mods),
                       data.get("gameVersion"), loader, loader_version, _count_jars(mods),
                       _file(Path(image)) if isinstance(image, str) and image else None)


# --- ATLauncher, GDLauncher, FTB App --------------------------------------------------------

def _loader_kind(kind: object) -> Optional[str]:
    """A launcher's loader type ("Fabric", "neoforge", "LegacyFabric"...) as ours, or None."""
    k = str(kind or "").lower()
    return k if k in ("fabric", "quilt", "neoforge", "forge") else None


def _instance_dirs(base: Path) -> List[Path]:
    try:
        return sorted(p for p in base.iterdir() if p.is_dir())
    except OSError:
        return []


def _atlauncher_roots(home: Path, system: str, env: Mapping[str, str]) -> List[Path]:
    if system == "Windows":
        return _data_dirs("ATLauncher", home, system, env)     # the installer's default
    if system == "Darwin":
        return []                                 # lives inside the .app: no usual place
    return [*_data_dirs("atlauncher", home, system, env),
            home / ".var" / "app" / "com.atlauncher.ATLauncher" / "data"]


def _atlauncher(root: Path) -> Iterable[Instance]:
    for folder in _instance_dirs(root / "instances"):
        data = _read_json(folder / "instance.json")
        if data is None:
            continue
        meta = data.get("launcher") if isinstance(data.get("launcher"), dict) else {}
        lv = meta.get("loaderVersion") if isinstance(meta.get("loaderVersion"), dict) else {}
        loader = _loader_kind(lv.get("type"))
        mods = folder / "mods"
        yield Instance("atlauncher", meta.get("name") or folder.name, str(folder), str(mods),
                       data.get("id"), loader, lv.get("version") if loader else None,
                       _count_jars(mods), _file(folder / "instance.png"))


def _gdlauncher_roots(home: Path, system: str, env: Mapping[str, str]) -> List[Path]:
    """The legacy app's userData (Electron: .config on Linux) and Carbon's runtime folder."""
    if system == "Windows":
        appdata = Path(env.get("APPDATA") or home / "AppData" / "Roaming")
        legacy, carbon = appdata / "gdlauncher_next", appdata / "gdlauncher_carbon"
    elif system == "Darwin":
        support = home / "Library" / "Application Support"
        legacy, carbon = support / "gdlauncher_next", support / "gdlauncher_carbon"
    else:
        legacy = Path(env.get("XDG_CONFIG_HOME") or home / ".config") / "gdlauncher_next"
        carbon = Path(env.get("XDG_DATA_HOME") or home / ".local" / "share") / "gdlauncher_carbon"
    return [legacy, carbon / "data"]


def _gdlauncher(root: Path) -> Iterable[Instance]:
    for folder in _instance_dirs(root / "instances"):
        legacy = _read_json(folder / "config.json")
        if legacy is not None:
            info = legacy.get("loader") if isinstance(legacy.get("loader"), dict) else {}
            loader = _loader_kind(info.get("loaderType"))
            mods = folder / "mods"
            yield Instance("gdlauncher", folder.name, str(folder), str(mods),
                           info.get("mcVersion"), loader,
                           info.get("loaderVersion") if loader else None, _count_jars(mods))
            continue
        carbon = _read_json(folder / "instance.json")
        if carbon is None or "_version" not in carbon:
            continue
        game = carbon.get("game_configuration")
        version = game.get("version") if isinstance(game, dict) else None
        mc = loader = loader_version = None
        if isinstance(version, dict):                # a plain string is a custom version
            mc = version.get("release")
            first = next(iter(version.get("modloaders") or []), None)
            if isinstance(first, dict):
                loader = _loader_kind(first.get("type"))
                loader_version = first.get("version") if loader else None
        mods = folder / "instance" / "mods"
        yield Instance("gdlauncher", carbon.get("name") or folder.name, str(folder), str(mods),
                       mc, loader, loader_version, _count_jars(mods))


def _ftb_roots(home: Path, system: str, env: Mapping[str, str]) -> List[Path]:
    if system == "Windows":
        return [Path(env.get("LOCALAPPDATA") or home / "AppData" / "Local") / ".ftba"]
    if system == "Darwin":
        return [home / "Library" / "Application Support" / ".ftba"]
    return [home / ".ftba"]


def _ftb_loader(mod_loader: object, mc: Optional[str]) -> Tuple[Optional[str], Optional[str]]:
    """FTB's modLoader version id -> (loader, version). Fabric's id puts Minecraft first."""
    vid = str(mod_loader or "")
    if not vid or vid == mc:
        return None, None
    if mc and vid.startswith(f"fabric-loader-{mc}-"):
        return "fabric", vid[len(f"fabric-loader-{mc}-"):] or None
    if vid.startswith("neoforge-"):
        return "neoforge", vid[len("neoforge-"):] or None
    if mc and vid.startswith(f"{mc}-forge-"):
        return "forge", vid[len(f"{mc}-forge-"):] or None
    for kind in ("neoforge", "forge", "fabric"):  # the app's own test; version unknown
        if kind in vid:
            return kind, None
    return None, None


def _ftb(root: Path) -> Iterable[Instance]:
    for folder in _instance_dirs(root / "instances"):
        data = _read_json(folder / "instance.json")
        if data is None:
            continue
        mc = data.get("mcVersion") if isinstance(data.get("mcVersion"), str) else None
        loader, loader_version = _ftb_loader(data.get("modLoader"), mc)
        mods = folder / "mods"
        yield Instance("ftb", data.get("name") or folder.name, str(folder), str(mods),
                       mc, loader, loader_version, _count_jars(mods),
                       _file(folder / ".ftbapp" / "logo"))


# --- The official launcher------------------------------------------------------------------

def _minecraft_dir(home: Path, system: str, env: Mapping[str, str]) -> Path:
    if system == "Windows":
        return Path(env.get("APPDATA") or home / "AppData" / "Roaming") / ".minecraft"
    if system == "Darwin":
        return home / "Library" / "Application Support" / "minecraft"
    return home / ".minecraft"


def _version_id(game: Path, version_id: str) -> Tuple[Optional[str], Optional[str], Optional[str]]:
    """(mc_version, loader, loader_version) of a launcher version id. Minecraft's version comes
    from the installed version JSON (inheritsFrom), else from the id when the id carries it."""
    data = _read_json(game / "versions" / version_id / f"{version_id}.json") or {}
    mc = data.get("inheritsFrom") if isinstance(data.get("inheritsFrom"), str) else None
    parts = version_id.split("-")
    loader = loader_version = None
    if len(parts) >= 4 and parts[0] in ("fabric", "quilt") and parts[1] == "loader":
        loader, loader_version = parts[0], parts[2]
        mc = mc or "-".join(parts[3:])
    elif len(parts) >= 2 and parts[0] == "neoforge":
        loader, loader_version = "neoforge", "-".join(parts[1:])
    elif len(parts) >= 3 and parts[1] == "forge":
        loader, loader_version = "forge", "-".join(parts[2:])
        mc = mc or parts[0]
    elif len(parts) == 1 and data and not data.get("inheritsFrom"):
        mc = data.get("id") if isinstance(data.get("id"), str) else None   # vanilla
    return mc, loader, loader_version


def _minecraft(game: Path) -> Iterable[Instance]:
    """Profiles of launcher_profiles.json, one instance per game dir, then .minecraft itself."""
    launcher = "sklauncher" if (game / "sklauncher").is_dir() else "minecraft"
    profiles = (_read_json(game / "launcher_profiles.json") or {}).get("profiles")
    by_dir: Dict[str, Tuple[Path, List[Tuple[str, Tuple]]]] = {}
    for profile in (profiles.values() if isinstance(profiles, dict) else []):
        if not isinstance(profile, dict) or profile.get("type") in ("latest-release",
                                                                    "latest-snapshot"):
            continue
        version_id = profile.get("lastVersionId")
        folder = Path(profile.get("gameDir") or game)
        info = _version_id(game, version_id) if isinstance(version_id, str) else (None,) * 3
        icon = profile.get("icon")
        by_dir.setdefault(os.path.normcase(str(folder)), (folder, []))[1].append(
            (str(profile.get("name") or ""), info,
             icon if isinstance(icon, str) and icon.startswith("data:image/") else None))
    root = os.path.normcase(str(game))
    if root not in by_dir and (game / "mods").is_dir():  # no mods folder: nobody plays modded
        by_dir[root] = (game, [])
    for key, (folder, entries) in by_dir.items():
        infos = {info for _, info, _ in entries}
        mc, loader, loader_version = infos.pop() if len(infos) == 1 else (None,) * 3
        names = [n for n, _, _ in entries if n]
        icons = {i for _, _, i in entries}
        name = names[0] if len(names) == 1 else (folder.name if key != root else ".minecraft")
        mods = folder / "mods"
        if key != root and not mods.is_dir() and loader is None:
            continue                           # a vanilla profile elsewhere: nothing to mod
        yield Instance(launcher, name, str(folder), str(mods), mc, loader, loader_version,
                       _count_jars(mods), icons.pop() if len(icons) == 1 else None)


# --------------------------------------------------------------------------------------------

def find_instances(home: Optional[Path] = None, system: Optional[str] = None,
                   env: Optional[Mapping[str, str]] = None) -> List[Instance]:
    """Every instance of every launcher found on this machine, launcher by launcher."""
    home = Path(home) if home is not None else Path.home()
    system = system or platform.system()
    env = os.environ if env is None else env
    found: List[Instance] = []
    seen = set()

    def add(instances: Iterable[Instance]) -> None:
        for inst in instances:
            key = os.path.normcase(inst.path)
            if key not in seen:               # Flatpak and native may share a folder
                seen.add(key)
                found.append(inst)

    for root in _prism_roots(home, system, env):
        add(_prism(root))
    for root in _polymc_roots(home, system, env):
        add(_prism(root, "polymc", "polymc.cfg"))
    for root in _modrinth_roots(home, system, env):
        add(_modrinth(root))
    for root in _curseforge_roots(home, system, env):
        add(_curseforge(root))
    for root in _atlauncher_roots(home, system, env):
        add(_atlauncher(root))
    for root in _gdlauncher_roots(home, system, env):
        add(_gdlauncher(root))
    for root in _ftb_roots(home, system, env):
        add(_ftb(root))
    add(_minecraft(_minecraft_dir(home, system, env)))
    return found


def find_instance(query: str, instances: List[Instance]) -> Tuple[Optional[Instance], List[Instance]]:
    """The instance a player named: exact name first (any case), then the only one whose name
    contains it. Returns (match, candidates): no match with several candidates is ambiguous."""
    q = query.strip().lower()
    exact = [i for i in instances if i.name.lower() == q]
    if len(exact) == 1:
        return exact[0], exact
    partial = exact or [i for i in instances if q and q in i.name.lower()]
    return (partial[0] if len(partial) == 1 else None), partial


def mods_dir_of(folder: Path) -> Path:
    """A folder the player gave: a mods folder as is, or an instance/game folder's mods."""
    for sub in ("mods", "minecraft/mods", ".minecraft/mods"):
        if folder.name != "mods" and (folder / sub).is_dir():
            return folder / sub
    return folder


# --- What the launcher installed ------------------------------------------------------------

@dataclass
class LauncherRecord:
    """A launcher's own record of one JAR it installed."""

    source: str                      # "modrinth" | "curseforge": where it was downloaded from
    project_id: str                  # that site's project id (Modrinth "AANobbMI", CF "238222")
    name: Optional[str] = None       # the project's name on that site
    slug: Optional[str] = None       # its slug there, when the record gives one
    hash_format: Optional[str] = None
    hash: Optional[str] = None       # of the file as installed, to tell a replaced JAR


def _packwiz(path: Path) -> Optional[Tuple[str, LauncherRecord]]:
    """One .pw.toml: (file name, record), or None if it names neither site."""
    try:
        data = toml.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError, toml.TomlDecodeError):
        return None
    filename, update = data.get("filename"), data.get("update") or {}
    download = data.get("download") or {}
    if not isinstance(filename, str) or not isinstance(update, dict):
        return None
    slug = path.name[:-len(".pw.toml")]
    for source, key in (("modrinth", "mod-id"), ("curseforge", "project-id")):
        ids = update.get(source)
        if isinstance(ids, dict) and ids.get(key) is not None:
            return filename, LauncherRecord(source, str(ids[key]), data.get("name"), slug,
                                            download.get("hash-format"), download.get("hash"))
    return None


def _curseforge_addons(game: Path) -> Dict[str, LauncherRecord]:
    data = _read_json(game / "minecraftinstance.json") or {}
    records = {}
    for addon in data.get("installedAddons") or []:
        file = addon.get("installedFile") if isinstance(addon, dict) else None
        if not isinstance(file, dict) or addon.get("addonID") is None:
            continue
        filename = file.get("fileNameOnDisk") or file.get("fileName")
        if not filename:
            continue
        # hashes: [{"type": 1, "value": sha1}, {"type": 2, "value": md5}]: CurseForge's HashAlgo
        # (1 sha1, 2 md5), under "type" in the app's file and "algo" in its web API's records
        sha1 = next((h.get("value") for h in file.get("hashes") or []
                     if isinstance(h, dict) and (h.get("type") or h.get("algo")) == 1), None)
        site = urlparse(addon.get("webSiteURL") or "").path.rstrip("/").rsplit("/", 1)[-1]
        records[filename] = LauncherRecord("curseforge", str(addon["addonID"]), addon.get("name"),
                                           site or None, "sha1" if sha1 else None, sha1)
    return records


def _still_installed(jar: Path, record: LauncherRecord) -> bool:
    """The JAR on disk is the one the launcher installed (no hash in the record: trusted)."""
    if not record.hash or not record.hash_format:
        return True
    try:
        digest = hashlib.new(record.hash_format.lower())
        with open(jar, "rb") as f:
            for chunk in iter(lambda: f.read(1 << 16), b""):
                digest.update(chunk)
    except (OSError, ValueError):     # unreadable, or a hash format hashlib does not know
        return True
    return digest.hexdigest() == record.hash.lower()


def launcher_records(mods_dir: Path, only: Optional[Iterable[str]] = None
                     ) -> Dict[str, LauncherRecord]:
    """File name -> the launcher's record of it, for the JARs in mods_dir (or those named in
    `only`, so hashes are computed only for the files that need them). Prism's index wins
    over CurseForge's when both name a file."""
    mods_dir = Path(mods_dir)
    records = _curseforge_addons(mods_dir.parent)
    for path in [*sorted(mods_dir.glob("*.pw.toml")),
                 *sorted((mods_dir / ".index").glob("*.pw.toml"))]:
        found = _packwiz(path)
        if found:
            records[found[0]] = found[1]
    wanted = set(only) if only is not None else None
    return {name: rec for name, rec in records.items()
            if (wanted is None or name in wanted) and (mods_dir / name).is_file()
            and _still_installed(mods_dir / name, rec)}
