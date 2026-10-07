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
"""

from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Dict, List, Optional, Sequence, Set, Tuple

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
