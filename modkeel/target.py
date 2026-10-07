"""Target layer of the resolver: which Minecraft version the whole pack aims at.

Resolution has three layers (modkeel/resolve.py): this one, the sources that produce a JAR
for one mod on one target, and the evidence that says how sure we are it runs. The user
names a target; when some mods of the pack cannot be resolved there, this module proposes
another Minecraft version *for the whole pack*: changing version moves every mod, so it is
never decided per mod.

A version is proposed when more of the pack's mods have an official build there than were
resolved on the requested target (by any source), nearest version first. Official builds
are what Modrinth lists for the loader, so the proposal costs one Modrinth call per mod and
no download.

Changing target changes what the player gets, so it is announced, not silent. The caller
decides with `fallback_decision`:

  never   report the proposal only (the default without a terminal: CI, scripts)
  auto    take it without asking
  ask     a countdown (Enter starts now, n stops, the timeout continues); without a
          terminal it behaves as never

A fallback run writes to <output>/mc-<version>/ and never into --instance: a JAR for
another Minecraft version must not land in a game it does not run on.

A fallback run of a list (compile) first carries over what the first run already resolved
(`carry_over`): a JAR whose metadata allows the new version and whose classes, calls and
mixins still resolve there is copied, not built again. Mods with an official build on the
new version are always resolved again: the author's build for that version beats a JAR
built for another one.
"""

import shutil
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Collection, Dict, Iterable, List, Optional, Sequence, Set, Tuple

from modkeel.models import CompilationResult, ModCompilerConfig

FALLBACK_MODES = ("ask", "auto", "never")
COUNTDOWN_SECONDS = 15


@dataclass
class TargetOption:
    """Another Minecraft version for the pack, and which of its mods have a build there."""

    mc_version: str
    covered: List[str] = field(default_factory=list)
    missing: List[str] = field(default_factory=list)

    @property
    def summary(self) -> str:
        total = len(self.covered) + len(self.missing)
        if total == 1:
            return f"MC {self.mc_version} has an official build of {self.covered[0]}"
        missing = f" (not {', '.join(self.missing)})" if self.missing else ""
        return (f"MC {self.mc_version} has official builds of {len(self.covered)} of the "
                f"{total} mods{missing}")


def _release_like(game_version: str) -> bool:
    return bool(game_version) and all(p.isdigit() for p in game_version.split("."))


def _key(game_version: str) -> Tuple[int, ...]:
    return tuple(int(p) for p in game_version.split("."))


def official_versions(project_id: str, loader: str, modrinth) -> Optional[Set[str]]:
    """Release Minecraft versions the project has a `loader` build for; None if unknown."""
    versions = modrinth.project_versions(project_id, loader)
    if versions is None:
        return None
    return {gv for v in versions for gv in v.get("game_versions", []) if _release_like(gv)}


def propose_targets(mods: Sequence[Tuple[str, Optional[str]]], loader: str, current: str,
                    modrinth, resolved: int, limit: int = 3) -> List[TargetOption]:
    """Other versions where more of `mods` have an official build than `resolved`.

    mods: (title, Modrinth project id or None) for every mod of the pack, resolved or not.
    Best first: most mods covered, then nearest to `current` (counted in versions any of
    the mods was released for), then the older of two equally near versions (the older a
    version, the more mods it has had time to get).
    """
    by_mod: Dict[str, Set[str]] = {}
    for title, project_id in mods:
        found = official_versions(project_id, loader, modrinth) if project_id else None
        by_mod[title] = found or set()
    candidates = set().union(*by_mod.values()) - {current} if by_mod else set()
    if not candidates or not _release_like(current):
        return []
    ladder = sorted(candidates | {current}, key=_key)
    here = ladder.index(current)

    options = []
    for version in candidates:
        covered = [t for t, _ in mods if version in by_mod[t]]
        if len(covered) > resolved:
            options.append(TargetOption(version, covered,
                                        [t for t, _ in mods if t not in covered]))
    options.sort(key=lambda o: (-len(o.covered), abs(ladder.index(o.mc_version) - here),
                                _key(o.mc_version)))
    return options[:limit]


def default_mode(interactive: bool) -> str:
    """ask in a terminal, never without one (scripts and CI never change target silently)."""
    return "ask" if interactive else "never"


def fallback_decision(mode: str, interactive: bool,
                      countdown: Callable[[], bool]) -> bool:
    """Whether to run the fallback target: see the module docstring for each mode."""
    if mode == "auto":
        return True
    if mode == "ask" and interactive:
        return countdown()
    return False


def fallback_output(output_dir: Path, mc_version: str) -> Path:
    """Where a fallback run writes: beside the requested target's files, never mixed in."""
    return Path(output_dir) / f"mc-{mc_version}"


# Sources whose JAR is not carried over to another target: a relaxed JAR's metadata was
# rewritten for the first target (stacking a second guess on it is not evidence), and a
# cross-loader JAR needs the bridge mods the run downloads for its own target.
NOT_CARRIED = ("relaxed_official",)


def carry_over(previous: Iterable[CompilationResult], config: ModCompilerConfig, first_target: str,
               resolve_again: Collection[str]) -> Dict[str, CompilationResult]:
    """First-run results whose JAR also passes on config.mc_version, by repo URL.

    previous: the first run's CompilationResults. resolve_again: repo URLs that must go
    through the sources again (those with an official build on the new target). A JAR is
    judged as older_official judges one: metadata and linkage required, mixins when they
    can be checked, built for the version it was compiled or published for. A carried JAR
    is copied to config.output_dir and comes back as a new result whose trail says so.
    """
    from modkeel.evidence import Subject, evidence_line, gather

    target = config.mc_version
    carried: Dict[str, CompilationResult] = {}
    for r in previous:
        if (not r.success or r.repo_url in resolve_again or not r.jar_path
                or r.is_cross_loader or r.source in NOT_CARRIED
                or not Path(r.jar_path).is_file()):
            continue
        built_for = r.compiled_mc_version or first_target
        name = r.mod_name or r.repo_url
        evidence = gather(Subject(Path(r.jar_path), target, built_for, name), config,
                          ["metadata", "linkage", "mixins"], required=["metadata", "linkage"])
        if not evidence.ok:
            print(f"  ↻ {name}: not reused for MC {target} ({evidence.reason})")
            continue
        out = Path(config.output_dir)
        out.mkdir(parents=True, exist_ok=True)
        dest = out / Path(r.jar_path).name
        shutil.copy2(r.jar_path, dest)
        line = evidence_line(evidence.passed, config.docker_test)
        print(f"  ↻ {name}: the MC {first_target} JAR also passes on {target} ({line})")
        result = CompilationResult(
            repo_url=r.repo_url, success=True, branch=r.branch, jar_path=str(dest),
            mod_name=r.mod_name, mod_version=r.mod_version, compiled_mc_version=built_for,
            modrinth_download=r.modrinth_download)
        result.source = r.source
        result.trail = [f"✓ Reused from the MC {first_target} run: {line}"]
        result.caveat = (f"Built for MC {built_for}. Its metadata allows {target}, every "
                         f"Minecraft class it uses exists in {target}, no method or field it "
                         f"calls was removed or renamed since {built_for} and its mixins "
                         f"still find their targets, but that is a static check: test it "
                         f"in game (or with --docker-test) before relying on it.")
        carried[r.repo_url] = result
    return carried
