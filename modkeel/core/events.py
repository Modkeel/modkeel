"""Typed progress events the engine emits instead of printing (IDEA-028 step 1).

An event carries data, never formatted text (Message is the exception, see below): a front
end decides how to show it. The CLI renders each one as the line it printed before
(modkeel.core.text); the desktop app will render cards and progress bars; a JSON-lines
transport (step 3) will send to_dict() as is.

Events are an API once a front end depends on them: add fields with defaults, never rename
or remove one without bumping PROTOCOL.

Emitter is what every engine entry point takes (`events=`): a callable that receives each
event. The default, modkeel.core.text.print_event, keeps today's terminal output.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Callable, ClassVar, Dict, Optional, Tuple

# Version of the event vocabulary, sent first by the wire format (IDEA-028 step 3).
PROTOCOL = 1


@dataclass(frozen=True)
class Event:
    """Base of every event. `kind` names it on the wire."""

    kind: ClassVar[str] = "event"

    def to_dict(self) -> Dict[str, Any]:
        """JSON-ready form: the kind plus the fields, paths as strings at any depth."""
        return {"kind": self.kind, **_plain(asdict(self))}


def _plain(value: Any) -> Any:
    if isinstance(value, Path):
        return str(value)
    if isinstance(value, dict):
        return {k: _plain(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_plain(v) for v in value]
    return value


@dataclass(frozen=True)
class Message(Event):
    """A progress line not modelled as its own event yet (most of the build pipeline).

    `text` is exactly what the terminal shows, leading blank lines and indentation
    included. Each one is a candidate for a typed event when a front end needs its data.
    """

    kind: ClassVar[str] = "message"
    text: str


@dataclass(frozen=True)
class Downloading(Event):
    """A file is being fetched. purpose "check": an older build fetched to be judged on the
    target (built_for says which Minecraft version it was made for) before any install."""

    kind: ClassVar[str] = "downloading"
    filename: str
    purpose: str = "download"             # "download" | "check"
    built_for: Optional[str] = None


@dataclass(frozen=True)
class Progress(Event):
    """Bytes of a long download so far (the Docker test's loader installer)."""

    kind: ClassVar[str] = "progress"
    done: int
    total: int


@dataclass(frozen=True)
class Saved(Event):
    """A JAR was written: to the output directory, or into the instance (installed)."""

    kind: ClassVar[str] = "saved"
    path: Path
    installed: bool = False


@dataclass(frozen=True)
class CheckRan(Event):
    """An evidence check ran on a JAR (modkeel/evidence.py): status passed/failed/not_run."""

    kind: ClassVar[str] = "check_ran"
    check: str                            # "metadata", "linkage", "mixins", "server_boot"
    status: str
    detail: str


@dataclass(frozen=True)
class RangeRelaxed(Event):
    """relaxed_official widened a JAR's declared Minecraft range to include the target."""

    kind: ClassVar[str] = "range_relaxed"
    metadata_file: str
    old_range: str
    new_range: str


@dataclass(frozen=True)
class ForkChosen(Event):
    """The fork source picked a fork to compile (label: "owner/repo (branch)" and score)."""

    kind: ClassVar[str] = "fork_chosen"
    label: str


@dataclass(frozen=True)
class SourceTried(Event):
    """One step of a mod's resolution trail, emitted as it happens (resolve.Step).

    ok: this strategy delivered the JAR (detail = the candidate); otherwise detail says
    why it had none or why its candidate was rejected.
    """

    kind: ClassVar[str] = "source_tried"
    mod: str
    strategy: str
    ok: bool
    detail: str


@dataclass(frozen=True)
class CarriedOver(Event):
    """The target layer judged a first-run JAR on the new target (target.carry_over)."""

    kind: ClassVar[str] = "carried_over"
    mod: str
    target: str
    from_target: str
    reused: bool
    detail: str                           # the evidence line, or why it was not reused


@dataclass(frozen=True)
class ModIdentified(Event):
    """The mod a request names, as identified on Modrinth (identified=False: it was not;
    related: near misses as (title, slug), never offered as the mod)."""

    kind: ClassVar[str] = "mod_identified"
    query: str
    title: str
    identified: bool
    slug: Optional[str] = None
    source_repo: Optional[str] = None
    related: Tuple[Tuple[str, str], ...] = ()


@dataclass(frozen=True)
class Delivery:
    """What was delivered for a mod (from resolve.Delivered, without strategy internals)."""

    jar_path: Path
    mod_name: str
    mod_version: str
    verb: str
    evidence: Tuple[str, ...] = ()
    caveat: Optional[str] = None
    unverified: Optional[str] = None


@dataclass(frozen=True)
class ModResolved(Event):
    """The source layer finished one mod on one target: its trail, and the delivery if any.

    retarget: this target is a proposed version, not the one asked for. verify_runtime:
    a server boot was asked for (the evidence line says when it did not run).
    """

    kind: ClassVar[str] = "mod_resolved"
    mod: str
    target: str
    trail: Tuple[Tuple[str, bool, str], ...]
    delivered: Optional[Delivery] = None
    retarget: bool = False
    verify_runtime: bool = False


@dataclass(frozen=True)
class PackScanned(Event):
    """A mods folder read and identified (core/move.py): one row per JAR,
    (file, name, Modrinth slug or None,
     identified_by: "hash" | "launcher" | "fingerprint" | "name" | None).
    hash (Modrinth's SHA-1), launcher (the launcher's record names the project) and
    fingerprint (CurseForge's) are exact; name is a guess."""

    kind: ClassVar[str] = "pack_scanned"
    folder: str
    mods: Tuple[Tuple[str, str, Optional[str], Optional[str]], ...]


@dataclass(frozen=True)
class TargetSearch(Event):
    """The target layer looks for a nearer version where more runs (scope: mod | pack)."""

    kind: ClassVar[str] = "target_search"
    scope: str
    subject: str = ""                     # the mod's title for scope "mod"


@dataclass(frozen=True)
class GitHubCode(Event):
    """Signing in with GitHub (ghauth.py): the player opens `url` and enters `code` there."""

    kind: ClassVar[str] = "github_code"
    code: str
    url: str
    expires_in: int                       # seconds the code stays valid

    @property
    def minutes(self) -> int:
        """Whole minutes to show, rounded up: GitHub gives 899 seconds, which reads as 15."""
        return max(1, -(-self.expires_in // 60))


Emitter = Callable[[Event], None]
