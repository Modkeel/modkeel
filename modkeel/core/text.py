"""Terminal rendering of engine events: the exact lines the CLI printed before events existed.

print_event is the default Emitter everywhere, so a caller that passes no `events=` sees
the same output as always. Events the terminal never showed live (SourceTried: the CLI
prints the whole trail at the end) render to None and print nothing.
"""

from __future__ import annotations

from typing import Optional

from modkeel.core.events import (
    CarriedOver,
    CheckRan,
    Downloading,
    Event,
    ForkChosen,
    GitHubCode,
    Message,
    ModIdentified,
    ModResolved,
    PackScanned,
    Progress,
    RangeRelaxed,
    Saved,
    SourceTried,
    TargetSearch,
)

CHECK_LABELS = {"metadata": "Metadata", "linkage": "Linkage", "mixins": "Mixins",
                "server_boot": "Server boot"}


def render_text(event: Event) -> Optional[str]:
    """The line(s) for one event, or None when the terminal shows nothing for it."""
    if isinstance(event, Message):
        return event.text
    if isinstance(event, Downloading):
        if event.purpose == "check":
            return f"    \U0001f4e5 Checking {event.filename} (built for MC {event.built_for})..."
        return f"    \U0001f4e5 Downloading {event.filename}..."
    if isinstance(event, Progress):
        return (f"\r     {event.done // 1024}KB / {event.total // 1024}KB "
                f"({event.done * 100 // event.total}%)")
    if isinstance(event, Saved):
        return f"    \U0001f4be {'Installed' if event.installed else 'Saved'}: {event.path}"
    if isinstance(event, CheckRan):
        label = CHECK_LABELS.get(event.check, event.check)
        mark = {"passed": "✓", "not_run": "\U0001f50e"}.get(event.status, "⚠️ ")
        return f"    {mark} {label}: {event.detail}"
    if isinstance(event, RangeRelaxed):
        return (f"    ✏️  Relaxed {event.metadata_file}: MC {event.old_range} -> "
                f"{event.new_range}")
    if isinstance(event, ForkChosen):
        return f"\n  Best fork: {event.label}"
    if isinstance(event, CarriedOver):
        if event.reused:
            return (f"  ↻ {event.mod}: the MC {event.from_target} JAR also passes on "
                    f"{event.target} ({event.detail})")
        return f"  ↻ {event.mod}: not reused for MC {event.target} ({event.detail})"
    if isinstance(event, SourceTried):
        return None
    if isinstance(event, ModIdentified):
        if not event.identified:
            return None
        repo = f" - github.com/{event.source_repo}" if event.source_repo else ""
        return f"\n  Mod: {event.title} ({event.slug}){repo}"
    if isinstance(event, ModResolved):
        if not event.trail:
            return None
        lines = ["\nTried:"] + [f"  {'✓' if ok else '✗'} {strategy}: {detail}"
                                 for strategy, ok, detail in event.trail]
        return "\n".join(lines)
    if isinstance(event, PackScanned):
        exact = sum(1 for m in event.mods if m[3] == "hash")
        launcher = sum(1 for m in event.mods if m[3] == "launcher")
        on_cf = sum(1 for m in event.mods if m[3] == "fingerprint")
        guessed = sum(1 for m in event.mods if m[3] == "name")
        unknown = len(event.mods) - exact - launcher - on_cf - guessed
        by_launcher = f"{launcher} by the launcher's records, " if launcher else ""
        by_cf = f"{on_cf} by CurseForge's fingerprint, " if on_cf else ""
        return (f"\n{len(event.mods)} JARs in {event.folder}: {exact} identified by their hash, "
                f"{by_launcher}{by_cf}{guessed} by name, {unknown} unknown")
    if isinstance(event, GitHubCode):
        return (f"\nSign in with GitHub: open {event.url} and enter the code {event.code}"
                f" (valid {event.minutes} minutes)")
    if isinstance(event, TargetSearch):
        what = (f"{event.subject} runs" if event.scope == "mod"
                else "more of these mods run")
        return f"\nLooking for the nearest Minecraft version where {what}..."
    return None


def print_event(event: Event) -> None:
    """Default Emitter: print the event's terminal line, if it has one."""
    line = render_text(event)
    if line is None:
        return
    if isinstance(event, Progress):
        # redrawn in place: no newline until the download ends
        print(line, end="", flush=True)
    else:
        print(line)
