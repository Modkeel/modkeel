"""Questions only the user can answer, asked by the engine through `decide=` (IDEA-028 step 2).

The engine never prompts. When a run reaches a choice that changes what the user gets, it
asks `decide(question)`; the front end answers its own way (the CLI with the countdown,
--fallback and a hidden prompt; the app with a dialog; a script with a fixed policy). A run
started without `decide` gets safe_default for every question: no version change, no token,
so a headless run never blocks and never does more than it was asked.

Only what changes the result is asked (docs/ideas/IDEA-028-engine-core.md, "Principle"):
which sources to try, whether to compile or which checks to run are never questions.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable, ClassVar, Optional


@dataclass(frozen=True)
class Question:
    """Base of every question. `kind` names it on the wire; safe_default answers it."""

    kind: ClassVar[str] = "question"


@dataclass(frozen=True)
class ChangeTarget(Question):
    """Run on another Minecraft version, where more of what was asked runs?

    option: the target.TargetOption proposed (its summary says why). current: the version
    asked for. scope: "mod" (get) or "pack" (compile). Answer: True to run there.
    """

    kind: ClassVar[str] = "change_target"
    option: Any
    current: str
    scope: str = "mod"


@dataclass(frozen=True)
class NeedToken(Question):
    """A GitHub token, needed now (forks are the only source left). Answer: the token, None
    (skip forks), or SIGN_IN: sign in with GitHub right now (ghauth.py, a GitHubCode event
    shows the code; the token is saved for the next runs).

    Asked once per run, at the moment a strategy needs GitHub, never up front.
    """

    kind: ClassVar[str] = "need_token"
    reason: str = "forks"


class SignIn:
    """The NeedToken answer "sign in with GitHub now" (on the wire: {"sign_in": true}).
    open_browser: the engine opens GitHub's page itself (a desktop front end on this machine)."""

    def __init__(self, open_browser: bool = False):
        self.open_browser = open_browser

    def __repr__(self) -> str:
        return f"SignIn(open_browser={self.open_browser})"


SIGN_IN = SignIn()

Decide = Callable[[Question], Any]


def safe_default(question: Question) -> Any:
    """The answer when nobody is asked: keep the version, no token."""
    if isinstance(question, ChangeTarget):
        return False
    return None


class Cancelled(Exception):
    """Raised between steps when the front end's `cancelled()` says the run must stop."""


Cancel = Callable[[], bool]


def never_cancelled() -> bool:
    return False


def check_cancel(cancelled: Optional[Cancel]) -> None:
    """Stop here if the run was cancelled (checked between resolutions and before re-runs)."""
    if cancelled is not None and cancelled():
        raise Cancelled()


class TokenAsker:
    """The fork strategy's token source: `initial`, else NeedToken asked once, when first
    needed (calling it). A SIGN_IN answer runs GitHub's sign-in there; if it fails the run
    goes on without forks (the reason is shown), and a cancel during it cancels the run.
    `current` is the token known so far, without asking."""

    def __init__(self, initial: Optional[str], decide: Decide, events,
                 cancelled: Optional[Cancel] = None):
        self.current: Optional[str] = initial
        self._asked = False
        self._decide, self._events, self._cancelled = decide, events, cancelled

    def __call__(self) -> Optional[str]:
        if not self.current and not self._asked:
            self._asked = True
            answer = self._decide(NeedToken("forks"))
            if isinstance(answer, SignIn):
                from modkeel.core.events import Message
                from modkeel.ghauth import SignInError, sign_in

                try:
                    answer = sign_in(self._events, self._cancelled,
                                     open_browser=answer.open_browser).token
                except SignInError as e:
                    self._events(Message(f"\nGitHub sign-in failed: {e}. Forks skipped."))
                    answer = None
            self.current = answer if isinstance(answer, str) and answer else None
        return self.current
