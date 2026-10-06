"""Source layer of the resolver: one ordered list of ways to get a mod's JAR for a target.

Resolution has three layers, of which this module is the middle one:

  target layer    which Minecraft version + loader the pack aims at   (not built yet)
  source layer    strategies that produce a JAR for one mod on that target   (this module)
  evidence layer  checks that say how sure we are it runs   (linkage/Docker, used ad hoc now)

A strategy has two steps. `find` is cheap (API lookups) and lists candidates, or says why
there are none. `deliver` is costly (download, verify, compile) and either puts a JAR in the
output directory or rejects the candidate with a reason. The Resolver walks SOURCE_ORDER
(modkeel/sources.py), tries each strategy's candidates in turn, stops at the first JAR, and
records every step so the CLI can show the path it took.

Adding a way to find JARs means one SourceStrategy subclass and one entry in SOURCE_ORDER.
"""

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Sequence, Union

from modkeel.models import ModCompilerConfig


@dataclass
class ModRef:
    """The mod the user asked for, identified once and shared by every strategy."""

    query: str
    project: Optional[Dict] = None        # Modrinth project record, when identified
    source_repo: Optional[str] = None     # "owner/repo" on GitHub, from the project
    related: List[Dict] = field(default_factory=list)  # near misses: addons, ports...
    lookup_error: Optional[str] = None    # why Modrinth could not be asked, if it could not

    @property
    def title(self) -> str:
        return self.project["title"] if self.project else self.query


@dataclass
class ResolveContext:
    """What strategies need besides the mod: the target and the clients to reach it.

    github_token is a callable so the prompt for a token (interactive runs only) happens
    when a strategy actually needs GitHub, not up front.
    """

    config: ModCompilerConfig
    modrinth: "object"
    github_token: Callable[[], Optional[str]] = lambda: None
    # Same target as `config`, with the given GitHub token (GitHub clients read it from
    # the config they are built with). Defaults to `config` itself.
    make_config: Optional[Callable[[Optional[str]], ModCompilerConfig]] = None

    def config_with_token(self, token: Optional[str]) -> ModCompilerConfig:
        return self.make_config(token) if self.make_config else self.config

    @property
    def mc_version(self) -> str:
        return self.config.mc_version

    @property
    def loader(self) -> str:
        return self.config.loader

    @property
    def loader_version(self) -> Optional[str]:
        """The -lv the user gave, or None ("0" is the placeholder for "not given")."""
        lv = self.config.loader_version
        return lv if lv and lv != "0" else None


@dataclass
class Candidate:
    """Something a strategy can try to deliver. `data` is private to that strategy."""

    label: str
    data: Dict = field(default_factory=dict)


@dataclass
class Found:
    """Result of `find`: candidates, or a note saying why there are none."""

    candidates: List[Candidate] = field(default_factory=list)
    note: str = ""
    # Strategy-specific detail for callers that need more than the note (the pipeline's
    # CompilationResult for a failed plan). Collected in Resolution.payloads.
    payload: Any = None


@dataclass
class Delivered:
    """A JAR in the output directory and what we know about it.

    evidence lists the checks it passed, weakest first ("published", "metadata",
    "linkage", "built"); caveat is shown to the user when the JAR is not an official
    build for the exact target.
    """

    jar_path: Path
    mod_name: str
    mod_version: str
    verb: str = "downloaded"              # "downloaded" or "compiled", for the final line
    evidence: List[str] = field(default_factory=list)
    caveat: Optional[str] = None
    dependencies: List[Path] = field(default_factory=list)  # required JARs fetched with it
    payload: Any = None                   # strategy-specific result (see Found.payload)


@dataclass
class Rejected:
    """A candidate that did not make it, and why (shown in the trail)."""

    reason: str
    payload: Any = None                   # strategy-specific result (see Found.payload)


@dataclass
class Step:
    """One line of the trail: a strategy, whether it produced the JAR, and the detail."""

    strategy: str
    ok: bool
    detail: str


class SourceStrategy:
    """Base class for a way to get a mod's JAR for the target. See module docstring."""

    name: str = ""
    label: str = ""
    # Most candidates deliver() is tried on (None: all). find() may list more, for display.
    max_attempts: Optional[int] = None

    def find(self, mod: ModRef, ctx: ResolveContext) -> Found:
        raise NotImplementedError

    def deliver(self, candidate: Candidate, mod: ModRef,
                ctx: ResolveContext) -> Union[Delivered, Rejected]:
        raise NotImplementedError


@dataclass
class Resolution:
    """Outcome of a resolve: the JAR (if any), the trail, and what is left to try.

    After a find-only run, `pending` is the first strategy with candidates and those
    candidates, so a later `deliver_pending` does not repeat the (possibly slow) lookup.
    """

    delivered: Optional[Delivered] = None
    trail: List[Step] = field(default_factory=list)
    pending: Optional[tuple] = None       # (strategy index, [Candidate], strategy name)
    pending_note: str = ""                # the pending strategy's Found.note
    payloads: List[Any] = field(default_factory=list)  # every non-None payload, in order

    @property
    def pending_strategy(self) -> Optional[str]:
        return None if self.pending is None else self.pending[2]

    @property
    def pending_candidates(self) -> List[Candidate]:
        return [] if self.pending is None else self.pending[1]


class Resolver:
    """Walks the strategies in order. See module docstring."""

    def __init__(self, strategies: Optional[Sequence[SourceStrategy]] = None):
        if strategies is None:
            from modkeel.sources import default_strategies
            strategies = default_strategies()
        self.strategies = list(strategies)

    def resolve(self, mod: ModRef, ctx: ResolveContext, deliver: bool = True) -> Resolution:
        """Find (and with deliver=True, fetch) the mod's JAR for ctx's target.

        Find-only runs stop at the first strategy with candidates and leave them pending.
        """
        return self._run(mod, ctx, Resolution(), start=0, deliver=deliver)

    def deliver_pending(self, resolution: Resolution, mod: ModRef,
                        ctx: ResolveContext) -> Resolution:
        """Continue a find-only resolution: deliver its pending candidates, then go on."""
        if resolution.pending is None:
            return resolution
        index, candidates, _ = resolution.pending
        resolution.pending = None
        if self._deliver_all(self.strategies[index], candidates, mod, ctx, resolution):
            return resolution
        return self._run(mod, ctx, resolution, start=index + 1, deliver=True)

    def _run(self, mod: ModRef, ctx: ResolveContext, resolution: Resolution,
             start: int, deliver: bool) -> Resolution:
        for index in range(start, len(self.strategies)):
            strategy = self.strategies[index]
            found = strategy.find(mod, ctx)
            if found.payload is not None:
                resolution.payloads.append(found.payload)
            if not found.candidates:
                resolution.trail.append(Step(strategy.name, False, found.note))
                continue
            if not deliver:
                resolution.pending = (index, found.candidates, strategy.name)
                resolution.pending_note = found.note
                return resolution
            if self._deliver_all(strategy, found.candidates, mod, ctx, resolution):
                return resolution
        return resolution

    @staticmethod
    def _deliver_all(strategy: SourceStrategy, candidates: List[Candidate], mod: ModRef,
                     ctx: ResolveContext, resolution: Resolution) -> bool:
        for candidate in candidates[:strategy.max_attempts]:
            outcome = strategy.deliver(candidate, mod, ctx)
            if outcome.payload is not None:
                resolution.payloads.append(outcome.payload)
            if isinstance(outcome, Delivered):
                resolution.delivered = outcome
                resolution.trail.append(Step(strategy.name, True, candidate.label))
                return True
            resolution.trail.append(
                Step(strategy.name, False, f"{candidate.label}: {outcome.reason}")
            )
        return False

