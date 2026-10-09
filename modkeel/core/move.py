"""Move a pack to another Minecraft version: a mods folder in, a folder of JARs for the
target out (IDEA-023 "your instances"; the engine's pack entry point, IDEA-028).

    move_pack(MoveRequest(mods_dir, mc_version))

1. Identify every JAR exactly: its SHA-1 is looked up on Modrinth in one request, which
   names the project and version with nothing to guess. For a JAR Modrinth does not know,
   the launcher's own record of what it installed is read (instances.launcher_records):
   a Modrinth project id there is exact too ("launcher"); a CurseForge project gives its
   slug and name to try on Modrinth. JARs left without either are looked up on CurseForge
   by fingerprint in one request ("fingerprint", exact; modkeel/curseforge.py, only while
   Modkeel's CurseForge service is on). Then the name in the JAR's metadata ("name": shown
   as a guess), else the mod is left unknown.
2. Each identified mod goes through the sources for the target (the same Resolver as `get`:
   official build, its CurseForge build, older build that still runs, community fork,
   relaxed range). A mod known only on CurseForge goes through them too, with its CurseForge
   project; the nearest-version proposal (step 4) counts only mods known on Modrinth.
3. A JAR nobody can resolve (not on Modrinth, or no build found) is judged as it is: if the
   player's own file passes the static checks on the target (metadata, linkage, mixins), it
   is reused, since that copy runs there too.
4. When mods are left without a build, the nearest version where more of the pack runs is
   proposed (ChangeTarget, scope "pack"); accepted, the whole move runs there instead.

Everything is written to <output_dir>/mc-<version>/; the player's folder is only read.
With new_instance, a mods folder of a Prism Launcher instance also becomes a new instance
next to it, "<name> (<version>)" (modkeel/newinstance.py); the old one is never touched.
"""

from __future__ import annotations

import shutil
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional, Tuple

from modkeel.core.decisions import Cancel, ChangeTarget, Decide, TokenAsker, check_cancel, \
    safe_default
from modkeel.core.engine import _delivery, _propose
from modkeel.core.events import Delivery, Emitter, Message, ModResolved, PackScanned
from modkeel.core.text import print_event
from modkeel.models import ModCompilerConfig

LOADERS = ("neoforge", "forge", "fabric", "quilt")


@dataclass
class MoveRequest:
    """What `move` asks for: a mods folder and the Minecraft version to move it to."""

    mods_dir: str
    mc_version: str
    loader: Optional[str] = None          # default: the loader most JARs in the folder declare
    loader_version: Optional[str] = None  # needed only to compile a fork
    output_dir: str = "out"
    github_token: Optional[str] = None
    new_instance: bool = False            # also create the pack beside its launcher instance


@dataclass
class MovedMod:
    """One JAR of the folder and what became of it on the target."""

    file: str                             # the JAR's file name in the player's folder
    name: str
    identified_by: Optional[str]          # "hash" | "launcher" (exact), "name" (a guess), None
    slug: Optional[str] = None
    status: str = "missing"               # delivered | reused | missing | unknown
    delivered: Optional[Delivery] = None
    detail: str = ""                      # why it is missing, or what reuse rests on


@dataclass
class MoveResult:
    target: str
    loader: str
    output_dir: Path
    mods: List[MovedMod] = field(default_factory=list)
    retargeted: bool = False
    proposal: object = None               # target.TargetOption, taken or not
    instance: object = None               # instances.Instance created (new_instance)
    instance_note: str = ""               # why none was created, when one was asked for

    @property
    def ready(self) -> int:
        return sum(1 for m in self.mods if m.status in ("delivered", "reused"))


@dataclass
class _Entry:
    jar: Path
    scanned: object                       # scanner.ScannedMod or None (no metadata)
    mod: MovedMod
    project: Optional[Dict] = None
    record: object = None                 # instances.LauncherRecord, when Modrinth lacks the hash
    curseforge: object = None             # curseforge.CfMod, when its CurseForge project is known


def _as_hit(project: Dict) -> Dict:
    """A full project record in the shape the sources read (a search hit's project_id)."""
    return {**project, "project_id": project.get("project_id") or project.get("id")}


def _cf_identity(record, match) -> object:
    """The JAR's CurseForge project: the fingerprint match's, else the launcher's record."""
    from modkeel.curseforge import CfMod

    if match is not None:
        return match.mod
    if record is not None and record.source == "curseforge" and record.project_id.isdigit():
        return CfMod(int(record.project_id), record.slug or "", record.name or "")
    return None


def scan_pack(mods_dir: Path, modrinth, events: Emitter = print_event,
              curseforge=None) -> List[_Entry]:
    """Every JAR in the folder with who it is (step 1 above). `curseforge` defaults to the
    run's shared client (curseforge.default_client)."""
    from modkeel.curseforge import default_client
    from modkeel.instances import LauncherRecord, launcher_records
    from modkeel.scanner import _scan_single_jar, compute_sha1
    from modkeel.sources import identify_mod

    jars = sorted(Path(mods_dir).glob("*.jar"))
    hashes = {jar: compute_sha1(jar) for jar in jars}
    by_hash = modrinth.versions_by_hash(list(hashes.values())) or {}
    projects = modrinth.fetch_projects([v["project_id"] for v in by_hash.values()])
    records = launcher_records(Path(mods_dir), [j.name for j in jars
                                                if hashes[j] not in by_hash])
    # what neither Modrinth nor the launcher names: CurseForge by fingerprint, one request
    by_print = (curseforge or default_client()).match(
        [j for j in jars if hashes[j] not in by_hash and j.name not in records])
    for jar, match in by_print.items():
        records[jar.name] = LauncherRecord("curseforge", str(match.mod.id), match.mod.name,
                                           match.mod.slug)
    entries = []
    for jar in jars:
        scanned = _scan_single_jar(jar, hashes[jar])
        name = scanned.mod_name if scanned else jar.stem
        version = by_hash.get(hashes[jar])
        project = projects.get(version["project_id"]) if version else None
        if project:
            entry = _Entry(jar, scanned, MovedMod(jar.name, project["title"], "hash",
                                                   project["slug"]), _as_hit(project))
        else:
            record = records.get(jar.name)
            if not scanned and record and record.name:
                name = record.name
            cf_mod = _cf_identity(record, by_print.get(jar))
            how = "fingerprint" if jar in by_print else "launcher" if cf_mod else None
            entry = _Entry(jar, scanned, MovedMod(jar.name, name, how), record=record,
                           curseforge=cf_mod)
            found = _by_launcher(record, scanned, modrinth) if record else None
            if not found and scanned:
                full = _by_metadata(scanned, modrinth, identify_mod)
                found = (full, "name") if full else None
            if found:
                full, how = found
                entry.project = _as_hit(full)
                entry.mod = MovedMod(jar.name, full.get("title", name), how, full.get("slug"))
        entries.append(entry)
    events(PackScanned(str(mods_dir), tuple(
        (e.mod.file, e.mod.name, e.mod.slug, e.mod.identified_by) for e in entries)))
    return entries


def _for_loader(project: Dict, scanned) -> bool:
    return (project.get("project_type") == "mod"
            and (not scanned or not scanned.declared_loader
                 or scanned.declared_loader in project.get("loaders", [])))


def _by_launcher(record, scanned, modrinth) -> Optional[Tuple[Dict, str]]:
    """(project, how) from the launcher's record of the JAR. A Modrinth project id names the
    project ("launcher", exact). A CurseForge project is tried on Modrinth by its slug there,
    kept only if the names agree too and it is a mod for the JAR's loader (a guess, "name");
    most mods use the same slug on both sites."""
    from modkeel.modrinth import same_mod_name

    if record.source == "modrinth":
        project = modrinth.fetch_project(record.project_id)
        return (project, "launcher") if project else None
    if not (record.slug and record.name):
        return None
    project = modrinth.fetch_project(record.slug)
    if not project or not _for_loader(project, scanned):
        return None
    if same_mod_name(record.name, project.get("title", "")):
        return project, "name"
    return None


def _by_metadata(scanned, modrinth, identify_mod) -> Optional[Dict]:
    """A JAR Modrinth does not know by hash (repacked, renamed, built locally): its mod id as
    a slug first (ids and slugs usually match: "cloth-config"), then a search by its name.
    A slug hit counts only if it is a mod for the JAR's loader. Still a guess ("name")."""
    for slug in dict.fromkeys([scanned.mod_id, scanned.mod_id.replace("_", "-")]):
        project = modrinth.fetch_project(slug)
        if project and _for_loader(project, scanned):
            return project
    ref = identify_mod(scanned.mod_name or scanned.mod_id, modrinth)
    if ref.project:
        return modrinth.fetch_project(ref.project.get("project_id") or ref.project["slug"]) \
            or ref.project
    return None


def pack_loader(entries: List[_Entry]) -> Optional[str]:
    """The loader most of the folder's JARs declare (libraries count too)."""
    votes = Counter(e.scanned.declared_loader for e in entries
                    if e.scanned and e.scanned.declared_loader in LOADERS)
    return votes.most_common(1)[0][0] if votes else None


def move_pack(request: MoveRequest, events: Emitter = print_event,
              decide: Decide = safe_default, cancelled: Optional[Cancel] = None) -> MoveResult:
    """Move the folder's mods to request.mc_version (or the nearest version accepted)."""
    from modkeel.modrinth import ModrinthClient

    probe_config = ModCompilerConfig(mc_version=request.mc_version, loader="fabric",
                                     loader_version="0", output_dir=request.output_dir)
    modrinth = ModrinthClient(probe_config, events)
    entries = scan_pack(Path(request.mods_dir), modrinth, events)
    loader = (request.loader or pack_loader(entries) or "fabric").lower()
    token_on_demand = TokenAsker(request.github_token, decide, events, cancelled)

    result = _run(entries, request, request.mc_version, loader, False, modrinth, events,
                  token_on_demand, cancelled)
    left = [e for e in entries if e.project is not None
            and next(m for m in result.mods if m.file == e.jar.name).status == "missing"]
    if not left:
        return _as_instance(result, request, events)

    option = _propose([(e.mod.name, e.project["project_id"])
                       for e in entries if e.project is not None],
                      loader, request.mc_version, modrinth, resolved=result.ready,
                      scope="pack", subject="", events=events)
    result.proposal = option
    if option is None or not decide(ChangeTarget(option, request.mc_version, "pack")):
        return _as_instance(result, request, events)
    moved = _run(entries, request, option.mc_version, loader, True, modrinth, events,
                 token_on_demand, cancelled)
    moved.retargeted, moved.proposal = True, option
    return _as_instance(moved, request, events)


def _as_instance(result: MoveResult, request: MoveRequest, events: Emitter) -> MoveResult:
    """The moved pack as a new launcher instance beside the old one, when asked and possible;
    otherwise instance_note says why (the pack is still in result.output_dir)."""
    from modkeel.instances import LAUNCHER_NAMES
    from modkeel.newinstance import NewInstanceError, instance_of, new_instance

    if not request.new_instance:
        return result
    old = instance_of(Path(request.mods_dir))
    if result.ready == 0:
        result.instance_note = "nothing to put in it"
    elif old is None:
        result.instance_note = "this folder is not a launcher instance's"
    else:
        try:
            made = new_instance(old, result.target, result.loader, result.output_dir,
                                request.loader_version if result.target == request.mc_version
                                and request.loader_version != "0" else None)
        except NewInstanceError as e:
            result.instance_note = f"{LAUNCHER_NAMES.get(old.launcher, old.launcher)}: {e}"
        else:
            result.instance = made.instance
            carried = f", with your {', '.join(made.copied)}" if made.copied else ""
            events(Message(f"\nNew {LAUNCHER_NAMES[made.instance.launcher]} instance: "
                           f"{made.instance.name} ({result.loader} "
                           f"{made.instance.loader_version}){carried} -> {made.instance.path}"))
    if result.instance_note:
        events(Message(f"\nNo new instance: {result.instance_note}; "
                       f"the pack is in {result.output_dir}/"))
    return result


def _run(entries: List[_Entry], request: MoveRequest, target: str, loader: str,
         retarget: bool, modrinth, events: Emitter, token_on_demand, cancelled) -> MoveResult:
    """Steps 2 and 3 for one target version."""
    from modkeel.modrinth import ModrinthClient
    from modkeel.resolve import ModRef, ResolveContext, Resolver
    from modkeel.target import fallback_output

    out = fallback_output(request.output_dir, target)
    out.mkdir(parents=True, exist_ok=True)

    def make_config(tok: Optional[str]) -> ModCompilerConfig:
        # "0" marks "no loader version given"; never the player's instance (mods_path)
        return ModCompilerConfig(mc_version=target, loader=loader,
                                 loader_version=request.loader_version or "0",
                                 github_token=tok, output_dir=str(out))

    config = make_config(request.github_token)
    client = ModrinthClient(config, events)   # dependencies for this target, into `out`
    result = MoveResult(target, loader, out, retargeted=retarget)
    for entry in entries:
        check_cancel(cancelled)
        mod = MovedMod(entry.mod.file, entry.mod.name, entry.mod.identified_by, entry.mod.slug)
        if entry.project is not None or entry.curseforge is not None:
            repo = (ModrinthClient.source_repo_of(entry.project) if entry.project is not None
                    else entry.curseforge.github_repo)
            ref = ModRef(query=mod.name, project=entry.project, curseforge=entry.curseforge,
                         source_repo=repo)
            ctx = ResolveContext(config=config, modrinth=client, github_token=token_on_demand,
                                 make_config=make_config, events=events)
            resolution = Resolver().resolve(ref, ctx)
            events(ModResolved(mod.name, target,
                               tuple((s.strategy, s.ok, s.detail) for s in resolution.trail),
                               _delivery(resolution.delivered), retarget))
            if resolution.delivered:
                mod.status, mod.delivered = "delivered", _delivery(resolution.delivered)
            else:
                mod.detail = _missing_detail(entry, resolution.trail)
        if mod.status != "delivered":
            _reuse_if_it_runs(entry, mod, target, config, out, events)
        result.mods.append(mod)
    return result


def _missing_detail(entry: _Entry, trail) -> str:
    """Why no build was found: the last strategy's word, except for a mod known only on
    CurseForge, where the Modrinth strategies have nothing to say (they need its Modrinth
    project) and the CurseForge one does, or there is no CurseForge service to ask."""
    if entry.project is None and entry.curseforge is not None:
        said = [s.detail for s in trail if s.strategy == "curseforge"]
        return said[-1] if said else "on CurseForge only, not on Modrinth"
    return trail[-1].detail if trail else "no build"


def _reuse_if_it_runs(entry: _Entry, mod: MovedMod, target: str, config: ModCompilerConfig,
                      out: Path, events: Emitter) -> None:
    """Step 3: the player's own JAR, kept when it passes the static checks on the target."""
    from modkeel.evidence import Subject, evidence_line, gather

    built_for = entry.scanned.declared_mc_version if entry.scanned else None
    evidence = gather(Subject(entry.jar, target, built_for, mod.name), config,
                      ["metadata", "linkage", "mixins"], required=["metadata", "linkage"],
                      events=events)
    if not evidence.ok:
        if entry.curseforge is not None and entry.project is None:
            # known exactly on CurseForge: what its build search said, then why ours fails
            mod.detail = f"{mod.detail}; your JAR does not run there: {evidence.reason}"
        elif entry.project is None:
            mod.status = "unknown"
            mod.detail = f"not on Modrinth, and your JAR does not run there: {evidence.reason}"
        return
    dest = out / entry.jar.name
    shutil.copy2(entry.jar, dest)
    mod.status = "reused"
    mod.detail = f"your JAR also passes on {target} ({evidence_line(evidence.passed)})"
    mod.delivered = Delivery(dest, mod.name, entry.scanned.mod_version if entry.scanned else "?",
                             "reused", tuple(evidence.passed))
