"""A moved pack as a new launcher instance, next to the player's old one (never over it).

    new_instance(old, mc_version, loader, jars_dir) -> NewInstance(instance, copied)

`modkeel move` writes the moved JARs to out/mc-<version>/; for a Prism Launcher instance it
then creates "<old name> (<version>)" in the same instances folder, so the pack shows up in
the launcher ready to play:

    instance.cfg    the old one's settings (memory, JVM args, icon...), with a new name and
                    without what belongs to the old instance: play time, its modpack link
                    (ManagedPack*: Prism would "update" it back to the old pack) and a Java
                    path chosen for the old version (Prism picks one for the new version)
    mmc-pack.json   net.minecraft at the target, Fabric's intermediary mappings for
                    Fabric/Quilt, and the loader at a version for it (from Prism's own
                    metadata server, the list its version picker shows); Prism adds the rest
                    (LWJGL) when it loads the instance
    <game>/mods     the moved JARs
    <game>/...      options.txt, config/, resourcepacks/, shaderpacks/ copied; worlds
                    (saves/) are not: opening one on a new version upgrades it for good

Only Prism Launcher (and MultiMC, same format) for now: the Modrinth App and CurseForge keep
their instances in their own database / state, which must not be written behind their back.
Nothing here touches the old instance; a failure leaves no half-made folder.
"""

from __future__ import annotations

import json
import os
import re
import shutil
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Dict, List, Optional

from modkeel import instances as _instances
from modkeel.instances import PRISM_LOADERS, Instance

PRISM_META = "https://meta.prismlauncher.org/v1"
LOADER_UIDS = {loader: uid for uid, loader in PRISM_LOADERS.items()}

# instance.cfg keys that describe the old instance, not settings to carry over
_OLD_ONLY = re.compile(r"^(name|lastLaunchTime|lastTimePlayed|totalTimePlayed|ManagedPack\w*|"
                       r"JavaPath|JavaVersion|JavaArchitecture|JavaRealArchitecture|"
                       r"JavaVendor|JavaSignature|OverrideJavaLocation|"
                       r"linkedInstances|InstanceAccountId)\s*=", re.IGNORECASE)
CARRIED = ("options.txt", "config", "resourcepacks", "shaderpacks")
_UNSTABLE = re.compile(r"alpha|beta|rc|pre|snapshot", re.IGNORECASE)


class NewInstanceError(Exception):
    """The new instance could not be made; the message says why, for the player."""


@dataclass
class NewInstance:
    instance: Instance
    copied: List[str] = field(default_factory=list)   # what was carried from the old game dir


def instance_of(mods_dir: Path, instances: Optional[List[Instance]] = None
                ) -> Optional[Instance]:
    """The launcher instance whose mods folder this is, if any."""
    key = os.path.normcase(os.path.abspath(mods_dir))
    for inst in _instances.find_instances() if instances is None else instances:
        if os.path.normcase(os.path.abspath(inst.mods_dir)) == key:
            return inst
    return None


def _get_json(url: str) -> Optional[Dict]:
    import requests

    try:
        resp = requests.get(url, timeout=15, headers={"User-Agent": "modkeel"})
        return resp.json() if resp.status_code == 200 else None
    except (requests.RequestException, ValueError):
        return None


def loader_version_for(loader: str, mc_version: str,
                       get_json: Optional[Callable[[str], Optional[Dict]]] = None
                       ) -> Optional[str]:
    """The loader version Prism would offer for this Minecraft version: its recommended one,
    else the newest stable, else the newest. A version tied to another Minecraft version
    (NeoForge, Forge) is never picked; Fabric and Quilt loaders run on any."""
    uid = LOADER_UIDS.get(loader)
    index = (get_json or _get_json)(f"{PRISM_META}/{uid}/index.json") if uid else None
    if not index:
        return None
    fits = []
    for v in index.get("versions") or []:
        mc = [r.get("equals") for r in v.get("requires") or [] if r.get("uid") == "net.minecraft"]
        if v.get("version") and (not mc or mc[0] in (None, mc_version)):
            fits.append(v)
    fits.sort(key=lambda v: v.get("releaseTime") or "", reverse=True)
    for pick in ([v for v in fits if v.get("recommended")],
                 [v for v in fits if not _UNSTABLE.search(v["version"])], fits):
        if pick:
            return pick[0]["version"]
    return None


def _cfg_text(old_cfg: Path, name: str) -> str:
    try:
        lines = old_cfg.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        lines = []
    kept = [line for line in lines if not _OLD_ONLY.match(line.strip())]
    if not any(line.strip().startswith("[") for line in kept):
        kept.insert(0, "[General]")
    if not any(line.strip().lower().startswith("instancetype") for line in kept):
        kept.insert(1, "InstanceType=OneSix")
    at = next(i for i, line in enumerate(kept) if line.strip().startswith("[")) + 1
    kept.insert(at, f"name={name}")
    return "\n".join(kept) + "\n"


def _free_folder(base: Path, stem: str) -> Path:
    stem = re.sub(r"[^\w.\- ]", "_", stem).strip() or "instance"
    folder, n = base / stem, 2
    while folder.exists():
        folder, n = base / f"{stem} ({n})", n + 1
    return folder


def _free_name(name: str, taken: List[str]) -> str:
    used = {t.lower() for t in taken}
    candidate, n = name, 2
    while candidate.lower() in used:
        candidate, n = f"{name} {n}", n + 1
    return candidate


def new_instance(old: Instance, mc_version: str, loader: str, jars_dir: Path,
                 loader_version: Optional[str] = None,
                 get_json: Optional[Callable[[str], Optional[Dict]]] = None,
                 instances: Optional[List[Instance]] = None) -> NewInstance:
    """Create the moved pack as a Prism instance beside `old` (see the module docstring)."""
    if old.launcher != "prism":
        raise NewInstanceError("only Prism Launcher instances can be created for now")
    if loader not in LOADER_UIDS:
        raise NewInstanceError(f"unknown loader {loader!r}")
    loader_version = loader_version or loader_version_for(loader, mc_version, get_json)
    if not loader_version:
        raise NewInstanceError(f"no {loader} version for Minecraft {mc_version} in Prism's list")

    src = Path(old.path)
    game_name = Path(old.mods_dir).parent.name or ".minecraft"
    old_game = src / game_name
    taken = [i.name for i in (_instances.find_instances() if instances is None
                               else instances)]
    name = _free_name(f"{old.name} ({mc_version})", taken)
    folder = _free_folder(src.parent, f"{src.name} {mc_version}")
    tmp = src.parent / f".{folder.name}.modkeel-tmp"
    shutil.rmtree(tmp, ignore_errors=True)
    copied = []
    try:
        game = tmp / game_name
        (game / "mods").mkdir(parents=True)
        (tmp / "instance.cfg").write_text(_cfg_text(src / "instance.cfg", name),
                                          encoding="utf-8")
        components = [{"uid": "net.minecraft", "version": mc_version, "important": True}]
        if loader in ("fabric", "quilt"):      # their mappings, as Prism itself writes them
            components.append({"uid": "net.fabricmc.intermediary", "version": mc_version,
                               "dependencyOnly": True})
        components.append({"uid": LOADER_UIDS[loader], "version": loader_version})
        (tmp / "mmc-pack.json").write_text(json.dumps(
            {"components": components, "formatVersion": 1}, indent=4) + "\n", encoding="utf-8")
        for jar in sorted(Path(jars_dir).glob("*.jar")):
            shutil.copy2(jar, game / "mods" / jar.name)
        for item in CARRIED:
            path = old_game / item
            if path.is_dir():
                shutil.copytree(path, game / item)
            elif path.is_file():
                shutil.copy2(path, game / item)
            else:
                continue
            copied.append(item)
        for icon in src.glob("*.png"):         # a custom icon kept next to the instance
            shutil.copy2(icon, tmp / icon.name)
        tmp.rename(folder)                     # appears in the launcher whole, or not at all
    except OSError as e:
        shutil.rmtree(tmp, ignore_errors=True)
        raise NewInstanceError(f"could not write the new instance: {e}") from e
    mods = folder / game_name / "mods"
    return NewInstance(Instance("prism", name, str(folder), str(mods), mc_version, loader,
                                loader_version, sum(1 for _ in mods.glob("*.jar"))), copied)
