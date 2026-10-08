"""The engine's interface for front ends: the CLI today, the desktop app and a service later.

Step 1 of docs/ideas/IDEA-028-engine-core.md: the engine reports progress as typed events
(modkeel.core.events) instead of printing, and modkeel.core.text renders them as the lines
the CLI has always printed. Requests, decisions and resolve_pack come in the next steps.
"""

from modkeel.core.events import Emitter, Event
from modkeel.core.text import print_event, render_text

__all__ = ["Emitter", "Event", "print_event", "render_text"]
