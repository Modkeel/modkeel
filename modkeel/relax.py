"""Widen a mod JAR's declared Minecraft range so the loader accepts it on another version.

Used by the relaxed_official source (modkeel/sources.py) for official builds whose bytecode
resolves on the target (linkage check) but whose metadata excludes it: the loader would
refuse them on the declared range alone. Only the Minecraft dependency changes:

  NeoForge / Forge  META-INF/neoforge.mods.toml, META-INF/mods.toml
                    the versionRange of the [[dependencies.<mod>]] block whose modId is
                    "minecraft" becomes a union: "<original>,[<target>]"
  Fabric            fabric.mod.json      depends.minecraft becomes ["<original>", "<target>"]
  Quilt             quilt.mod.json       the "minecraft" entry's versions, the same way

Widening is minimal: every version the author declared stays, and exactly the target is
added (a union in Maven ranges, an any-of list in Fabric and Quilt).

Everything else is copied byte for byte, except JAR signature files (META-INF/*.SF, .RSA,
.DSA, .EC): a signed JAR whose metadata changed fails verification, and an unsigned JAR
loads. A META-INF/modkeel-relaxed.txt entry records the original range and why it changed,
so the modification is visible inside the JAR too.
"""

import json
import re
import zipfile
from dataclasses import dataclass
from pathlib import Path
from typing import List, Optional

TOML_METADATA = ("META-INF/neoforge.mods.toml", "META-INF/mods.toml")
SIGNATURE_SUFFIXES = (".SF", ".RSA", ".DSA", ".EC")
MARKER = "META-INF/modkeel-relaxed.txt"


@dataclass
class Relaxed:
    """What changed: the metadata file and the Minecraft range before and after."""

    metadata_file: str
    old_range: str
    new_range: str


def relax_jar(src: Path, dest: Path, built_for: str, target: str) -> Optional[Relaxed]:
    """Write `dest`: `src` with `target` added to its declared Minecraft range.

    Returns what changed, or None (and writes nothing) when the JAR has no Minecraft
    dependency this module knows how to rewrite.
    """
    with zipfile.ZipFile(src) as jar:
        names = jar.namelist()
        change: Optional[Relaxed] = None
        rewritten = {}
        for name in TOML_METADATA:
            if name in names:
                text = jar.read(name).decode("utf-8")
                result = _relax_toml(text, target)
                if result:
                    rewritten[name], old, new = result
                    change = change or Relaxed(name, old, new)
        for name, relaxer in (("fabric.mod.json", _relax_fabric_json),
                              ("quilt.mod.json", _relax_quilt_json)):
            if name in names and change is None:
                result = relaxer(jar.read(name).decode("utf-8"), target)
                if result:
                    rewritten[name], old, new = result
                    change = Relaxed(name, old, new)
        if change is None:
            return None

        with zipfile.ZipFile(dest, "w", zipfile.ZIP_DEFLATED) as out:
            for info in jar.infolist():
                if _is_signature(info.filename):
                    continue
                data = rewritten.get(info.filename)
                out.writestr(info, data.encode("utf-8") if data is not None
                             else jar.read(info.filename))
            out.writestr(MARKER, (
                "This JAR was modified by Modkeel (relaxed_official).\n"
                f"{change.metadata_file}: Minecraft range {change.old_range} -> "
                f"{change.new_range}\n"
                f"Built for MC {built_for}; its bytecode resolved against MC {target}.\n"
                "Only the declared range changed; the author did not test this version.\n"
            ))
    return change


def _is_signature(name: str) -> bool:
    upper = name.upper()
    return upper.startswith("META-INF/") and upper.count("/") == 1 \
        and upper.endswith(SIGNATURE_SUFFIXES)


_BLOCK = re.compile(r"^\s*\[\[")
_MODID_MC = re.compile(r'^\s*modId\s*=\s*["\']minecraft["\']\s*(?:#.*)?$')
_RANGE = re.compile(r'^(\s*versionRange\s*=\s*)(["\'])(.*?)\2(.*)$')


def _relax_toml(text: str, target: str) -> Optional[tuple]:
    """(new text, old range, new range): the minecraft versionRange plus `,[target]`.

    Line-based, so comments and layout survive: the file is split into [[...]] blocks and
    only the versionRange line of a block that declares modId = "minecraft" changes.
    """
    lines = text.splitlines(keepends=True)
    blocks: List[List[int]] = [[]]
    for i, line in enumerate(lines):
        if _BLOCK.match(line):
            blocks.append([])
        blocks[-1].append(i)
    for block in blocks:
        if not any(_MODID_MC.match(lines[i]) for i in block):
            continue
        for i in block:
            m = _RANGE.match(lines[i].rstrip("\r\n"))
            if m:
                old = m.group(3)
                new_range = f"{old},[{target}]" if old.strip() else f"[{target}]"
                eol = lines[i][len(lines[i].rstrip("\r\n")):]
                lines[i] = f"{m.group(1)}{m.group(2)}{new_range}{m.group(2)}{m.group(4)}{eol}"
                return "".join(lines), old, new_range
    return None


def _plus_target(old, target: str) -> list:
    """An any-of list: what the author declared, then exactly the target."""
    kept = old if isinstance(old, list) else ([old] if old else [])
    return [*kept, target]


def _relax_fabric_json(text: str, target: str) -> Optional[tuple]:
    data = json.loads(text)
    depends = data.get("depends")
    if not isinstance(depends, dict) or "minecraft" not in depends:
        return None
    old = depends["minecraft"]
    depends["minecraft"] = _plus_target(old, target)
    return json.dumps(data, indent=2), json.dumps(old), json.dumps(depends["minecraft"])


def _relax_quilt_json(text: str, target: str) -> Optional[tuple]:
    data = json.loads(text)
    for dep in data.get("quilt_loader", {}).get("depends", []):
        if isinstance(dep, dict) and dep.get("id") == "minecraft":
            old = dep.get("versions", "")
            dep["versions"] = _plus_target(old, target)
            return json.dumps(data, indent=2), json.dumps(old), json.dumps(dep["versions"])
    return None
