"""The player's Minecraft instances, found where each launcher keeps them.

    find_instances() -> [Instance(launcher, name, path, mods_dir, mc_version, loader, ...)]

A player picks "my ATM10 pack" from a list instead of looking for a mods folder. Each launcher
stores its instances its own way; everything here only reads:

    Prism Launcher  <data>/instances/<dir>/   instance.cfg (name), mmc-pack.json (components:
                    net.minecraft, the loader's uid), the game in .minecraft/ or minecraft/;
                    <data>/prismlauncher.cfg may move the instances (InstanceDir)
    Modrinth App    <data>/profiles/<path>/   app.db (SQLite, table profiles: name,
                    game_version, mod_loader, mod_loader_version); profile.json in older
                    versions (the launcher was called theseus)
    CurseForge      <home>/curseforge/minecraft/Instances/<dir>/ (Documents/ on macOS)
                    minecraftinstance.json (name, gameVersion, baseModLoader.name such as
                    "neoforge-21.1.77" or "fabric-0.16.5-1.21.1")
    Minecraft       the official launcher's .minecraft/mods (no version: it is per profile)

A field a launcher does not give (or a file that cannot be read) is None, never a guess; an
instance with no mods folder yet is still listed (mods = 0). Paths come from the platform's
usual places; `home`, `system` and `env` are parameters so tests can build any of them.
"""

from __future__ import annotations

import configparser
import json
import os
import platform
import sqlite3
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Dict, Iterable, List, Mapping, Optional, Tuple

LAUNCHERS = ("prism", "modrinth", "curseforge", "minecraft")
LAUNCHER_NAMES = {"prism": "Prism Launcher", "modrinth": "Modrinth App",
                  "curseforge": "CurseForge", "minecraft": "Minecraft Launcher"}

# Prism / MultiMC component uids of each loader
PRISM_LOADERS = {
    "net.fabricmc.fabric-loader": "fabric",
    "org.quiltmc.quilt-loader": "quilt",
    "net.neoforged": "neoforge",
    "net.minecraftforge": "forge",
}


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


def _prism_instances_dir(root: Path) -> Path:
    custom = _read_cfg(root / "prismlauncher.cfg").get("instancedir")
    if custom:
        p = Path(custom).expanduser()
        return p if p.is_absolute() else root / p
    return root / "instances"


def _prism(root: Path) -> Iterable[Instance]:
    base = _prism_instances_dir(root)
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
            elif uid in PRISM_LOADERS:
                loader, loader_version = PRISM_LOADERS[uid], comp.get("version")
        game = next((folder / d for d in ("minecraft", ".minecraft")
                     if (folder / d).is_dir()), folder / ".minecraft")
        mods = game / "mods"
        yield Instance("prism", cfg.get("name") or folder.name, str(folder), str(mods),
                       mc, loader, loader_version, _count_jars(mods))


# --- Modrinth App ---------------------------------------------------------------------------

def _modrinth_roots(home: Path, system: str, env: Mapping[str, str]) -> List[Path]:
    return [*_data_dirs("ModrinthApp", home, system, env),
            *_data_dirs("com.modrinth.theseus", home, system, env)]


def _modrinth_db(root: Path) -> Dict[str, Tuple[Optional[str], ...]]:
    """profiles.path -> (name, game_version, mod_loader, mod_loader_version) from app.db.

    Opened read-only and immutable: the app may be running and holding its own lock, and we
    must never write to it. An unreadable or differently shaped database gives {}."""
    db = root / "app.db"
    if not db.is_file():
        return {}
    try:
        con = sqlite3.connect(f"{db.as_uri()}?mode=ro&immutable=1", uri=True)
        try:
            rows = con.execute("SELECT path, name, game_version, mod_loader, mod_loader_version "
                               "FROM profiles").fetchall()
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
        name, mc, loader, loader_version = known.get(folder.name, (None,) * 4)
        if folder.name not in known:          # older app: profile.json in the folder
            data = _read_json(folder / "profile.json")
            if data is None:
                continue                      # not a profile
            meta = data.get("metadata") or {}
            name, mc = meta.get("name"), meta.get("game_version")
            loader, loader_version = meta.get("loader"), (meta.get("loader_version") or {})
            if isinstance(loader_version, dict):
                loader_version = loader_version.get("id")
        loader = None if not loader or loader == "vanilla" else str(loader).lower()
        mods = folder / "mods"
        yield Instance("modrinth", name or folder.name, str(folder), str(mods), mc, loader,
                       loader_version, _count_jars(mods))


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
        yield Instance("curseforge", data.get("name") or folder.name, str(folder), str(mods),
                       data.get("gameVersion"), loader, loader_version, _count_jars(mods))


# --- The official launcher ------------------------------------------------------------------

def _minecraft_dir(home: Path, system: str, env: Mapping[str, str]) -> Path:
    if system == "Windows":
        return Path(env.get("APPDATA") or home / "AppData" / "Roaming") / ".minecraft"
    if system == "Darwin":
        return home / "Library" / "Application Support" / "minecraft"
    return home / ".minecraft"


def _minecraft(game: Path) -> Iterable[Instance]:
    mods = game / "mods"
    if mods.is_dir():                         # no mods folder: nobody plays modded there
        yield Instance("minecraft", ".minecraft", str(game), str(mods), mods=_count_jars(mods))


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
    for root in _modrinth_roots(home, system, env):
        add(_modrinth(root))
    for root in _curseforge_roots(home, system, env):
        add(_curseforge(root))
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
