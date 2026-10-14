"""Add a mod into a launcher instance, with a record that undoes exactly that.

`get` delivers into an output folder; the app's "Add" puts the mod in the player's instance.
The run goes to a staging folder first (MODKEEL_HOME/staging/<id>), so nothing reaches the
instance until the mod and its dependencies are all there, then the files are copied in:

- a file the instance already has (same SHA-1) is skipped;
- a dependency the instance already has (same Modrinth project, another file) is skipped:
  the pack's own copy stays;
- the mod itself, when the instance has another version of it, replaces it; the old file
  is kept in MODKEEL_HOME/history/<id>/ so undo can put it back.

Each add appends a record to MODKEEL_HOME/history.jsonl; undo appends one too, so the file
is the whole story. Undo removes only files whose hash still matches what was added: a file
the player changed or replaced since is left alone and named in the result.

A version change has no place here: the instance runs the version it runs. When there is no
build for it, the add ends without installing and the result carries the proposal.
"""

from __future__ import annotations

import hashlib
import json
import shutil
import time
import uuid
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Callable, Dict, List, Optional

from modkeel import constants
from modkeel.core.decisions import ChangeTarget, Decide, safe_default
from modkeel.core.events import Emitter
from modkeel.core.text import print_event


def history_file() -> Path:
    return constants.MODKEEL_HOME / "history.jsonl"


def _sha1(path: Path) -> str:
    h = hashlib.sha1()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


@dataclass
class AddRequest:
    """One mod, picked by Modrinth project id, into one instance's mods folder."""

    project_id: str
    title: str                            # label for events and history (the search hit's)
    mods_dir: str
    mc_version: str
    loader: str
    loader_version: Optional[str] = None  # needed only to compile a fork
    github_token: Optional[str] = None


@dataclass
class InstallRecord:
    """What one add did to one mods folder; history.jsonl holds one per line."""

    id: str
    time: float
    mods_dir: str
    mod: str
    mc_version: str
    loader: str
    added: List[Dict[str, str]] = field(default_factory=list)   # {"name", "sha1"}
    replaced: List[str] = field(default_factory=list)          # old files, kept in history/<id>/
    skipped: List[str] = field(default_factory=list)           # staged files already covered
    undone: bool = False

    def to_dict(self) -> Dict:
        return asdict(self)


@dataclass
class AddResult:
    installed: bool
    reason: str                           # why not, when not installed
    record: Optional[InstallRecord] = None
    get: object = None                    # engine.GetResult of the staged run


def _identities(jars: List[Path], sha1s: Dict[Path, str], lookup) -> Dict[Path, Optional[str]]:
    """Each JAR's Modrinth project id by hash (one request), None when unknown."""
    known = lookup(sorted(set(sha1s[j] for j in jars))) or {}
    return {j: (known.get(sha1s[j]) or {}).get("project_id") for j in jars}


def install(staged: List[Path], main: str, mods_dir: Path, record: InstallRecord,
            lookup: Callable[[List[str]], Optional[Dict[str, Dict]]]) -> InstallRecord:
    """Copy the staged JARs into `mods_dir` by the rules above, filling `record`.

    `main` is the delivered mod's file name; `lookup` is versions_by_hash (sha1 -> version).
    Raises OSError when the folder cannot be written (a running game holds its files on
    Windows); what was copied before that is in the record.
    """
    existing = sorted(p for p in mods_dir.glob("*.jar") if p.is_file())
    sha1s = {p: _sha1(p) for p in [*existing, *staged]}
    projects = _identities([*existing, *staged], sha1s, lookup)
    have_sha1 = {sha1s[p] for p in existing}
    backup = constants.MODKEEL_HOME / "history" / record.id

    for jar in staged:
        if sha1s[jar] in have_sha1:
            record.skipped.append(jar.name)
            continue
        project = projects[jar]
        same_project = [p for p in existing if project and projects[p] == project]
        if same_project and jar.name != main:
            record.skipped.append(jar.name)     # a dependency the pack already has
            continue
        old = [*same_project, *([mods_dir / jar.name] if (mods_dir / jar.name).exists() else [])]
        for path in dict.fromkeys(old):
            backup.mkdir(parents=True, exist_ok=True)
            shutil.move(str(path), str(backup / path.name))
            record.replaced.append(path.name)
        shutil.copy2(jar, mods_dir / jar.name)
        record.added.append({"name": jar.name, "sha1": sha1s[jar]})
    return record


def _append(entry: Dict) -> None:
    history_file().parent.mkdir(parents=True, exist_ok=True)
    with open(history_file(), "a", encoding="utf-8") as f:
        f.write(json.dumps(entry, ensure_ascii=True) + "\n")


def history() -> List[InstallRecord]:
    """Every add, newest first, with undone set from the undo lines."""
    if not history_file().exists():
        return []
    records: Dict[str, InstallRecord] = {}
    undone = set()
    for line in history_file().read_text(encoding="utf-8").splitlines():
        try:
            entry = json.loads(line)
        except ValueError:
            continue
        if "undo" in entry:
            undone.add(entry["undo"])
        elif "id" in entry:
            try:
                records[entry["id"]] = InstallRecord(**entry)
            except TypeError:
                continue
    for rid in undone & records.keys():
        records[rid].undone = True
    return sorted(records.values(), key=lambda r: r.time, reverse=True)


def add_to_instance(request: AddRequest, events: Emitter = print_event,
                    decide: Decide = safe_default, cancelled=None) -> AddResult:
    """Get the mod for the instance's version into staging, then install it with a record."""
    from modkeel.core.engine import GetRequest, get_mod
    from modkeel.modrinth import ModrinthClient
    from modkeel.models import ModCompilerConfig

    mods_dir = Path(request.mods_dir)
    if not mods_dir.is_dir():
        raise ValueError(f"not a folder: {mods_dir}")
    rid = time.strftime("%Y%m%d-%H%M%S-") + uuid.uuid4().hex[:6]
    staging = constants.MODKEEL_HOME / "staging" / rid

    def keep_version(question):
        # the instance's version is fixed: never move the run to another one
        return False if isinstance(question, ChangeTarget) else decide(question)

    try:
        got = get_mod(GetRequest(
            query=request.title, mc_version=request.mc_version, loader=request.loader,
            loader_version=request.loader_version, output_dir=str(staging), instance=None,
            github_token=request.github_token, project_id=request.project_id),
            events=events, decide=keep_version, cancelled=cancelled)
        if not got.delivered:
            return AddResult(False, f"no build of {request.title} for "
                                    f"{request.mc_version} found", get=got)
        staged = sorted(p for p in staging.rglob("*.jar") if p.is_file())
        record = InstallRecord(rid, time.time(), str(mods_dir), got.mod.title,
                               request.mc_version, request.loader.lower())
        client = ModrinthClient(ModCompilerConfig(
            mc_version=request.mc_version, loader=request.loader, loader_version="0",
            output_dir=str(staging)), events)
        try:
            install(staged, Path(got.delivered.jar_path).name, mods_dir, record,
                    client.versions_by_hash)
        except OSError as e:
            reason = f"could not write to {mods_dir}: {e.strerror or e}; is the game running?"
            if record.added or record.replaced:
                _append(record.to_dict())       # undo can still take back the partial add
            return AddResult(False, reason, record if record.added else None, got)
        _append(record.to_dict())
        return AddResult(True, "", record, got)
    finally:
        shutil.rmtree(staging, ignore_errors=True)


@dataclass
class UndoResult:
    undone: bool
    removed: List[str] = field(default_factory=list)
    restored: List[str] = field(default_factory=list)
    kept: List[str] = field(default_factory=list)    # changed since the add: left alone
    reason: str = ""


def undo(record_id: str) -> UndoResult:
    """Take back one add: its files out (if unchanged), the files it replaced back in."""
    record = next((r for r in history() if r.id == record_id), None)
    if record is None:
        return UndoResult(False, reason=f"no add with id {record_id}")
    if record.undone:
        return UndoResult(False, reason="already undone")
    mods_dir = Path(record.mods_dir)
    result = UndoResult(True)
    try:
        for added in record.added:
            path = mods_dir / added["name"]
            if path.exists() and _sha1(path) == added["sha1"]:
                path.unlink()
                result.removed.append(added["name"])
            elif path.exists():
                result.kept.append(added["name"])
        backup = constants.MODKEEL_HOME / "history" / record.id
        for name in record.replaced:
            source, dest = backup / name, mods_dir / name
            if source.exists() and not dest.exists():
                shutil.move(str(source), str(dest))
                result.restored.append(name)
    except OSError as e:
        result.undone = False
        result.reason = f"could not write to {mods_dir}: {e.strerror or e}; is the game running?"
    if result.undone:
        _append({"undo": record.id, "time": time.time()})
    return result
