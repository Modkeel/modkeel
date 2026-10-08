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
from typing import Any, Callable, ClassVar, Dict, Optional

# Version of the event vocabulary, sent first by the wire format (IDEA-028 step 3).
PROTOCOL = 1


@dataclass(frozen=True)
class Event:
    """Base of every event. `kind` names it on the wire."""

    kind: ClassVar[str] = "event"

    def to_dict(self) -> Dict[str, Any]:
        """JSON-ready form: the kind plus the fields, paths as strings."""
        data = {k: (str(v) if isinstance(v, Path) else v) for k, v in asdict(self).items()}
        return {"kind": self.kind, **data}


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


Emitter = Callable[[Event], None]
