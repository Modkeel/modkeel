"""Pieces every `modkeel` subcommand uses: console, banner, token and loader handling, and
how the resolver's outcome is shown (trail, near misses, evidence line)."""

import sys
import time
from typing import Optional

import typer
from rich.console import Console
from rich.markup import escape

from modkeel.config import ModkeelConfig
from modkeel.loaders import ALL_LOADERS
# Moved to modkeel/sources.py (the fork strategy uses them); re-exported for callers.
from modkeel.sources import prefilter_forks, strategy_label, temp_pipeline  # noqa: F401
from modkeel.utils import setup_windows_console

BANNER = r"""    __  ___          ____             __
   /  |/  /___  ____/ / /_____  ___  / /
  / /|_/ / __ \/ __  / //_/ _ \/ _ \/ /
 / /  / / /_/ / /_/ / ,< /  __/  __/ /
/_/  /_/\____/\__,_/_/|_|\___/\___/_/"""

TAGLINE = "Compile the mods Mojang left behind"

# The Windows console must be switched to UTF-8 before Rich binds to it.
setup_windows_console()
console = Console()


def resolve_github_token(
    explicit: Optional[str],
    modkeel_cfg: ModkeelConfig,
    prompt_if_missing: bool = False,
) -> Optional[str]:
    """Resolve GitHub token: CLI flag > saved config > interactive prompt.

    Args:
        explicit: Token passed via -t flag.
        modkeel_cfg: Persistent config.
        prompt_if_missing: If True and no token found, prompt the user
            interactively. Only prompts if running in a terminal.
    """
    if explicit:
        if explicit != modkeel_cfg.github_token:
            modkeel_cfg.github_token = explicit
            console.print("[dim]  GitHub token saved to ~/.modkeel/config.toml[/dim]")
        return explicit
    saved = modkeel_cfg.github_token
    if saved:
        console.print("[dim]  Using saved GitHub token.[/dim]")
        return saved
    if prompt_if_missing and console.is_terminal:
        console.print(
            "\n[yellow]No GitHub token found.[/yellow] "
            "A token is needed to search GitHub forks.\n"
            "  Create one at: [bold]https://github.com/settings/tokens[/bold]\n"
            "  Scopes needed: [dim]none (public repo access only)[/dim]\n"
        )
        token = typer.prompt("  GitHub token", hide_input=True, default="")
        if token:
            modkeel_cfg.github_token = token
            console.print("[green]Token saved to ~/.modkeel/config.toml[/green]")
            return token
    return None


def require_valid_loader(loader: str) -> None:
    """Exit with code 1 and a message unless `loader` is a known loader (any case)."""
    if loader.lower() not in ALL_LOADERS:
        console.print(
            f"[red]Error:[/red] Invalid loader '{loader}'. "
            f"Must be one of: {', '.join(ALL_LOADERS)}"
        )
        raise typer.Exit(1)


def print_trail(trail) -> None:
    """The path the resolver took: one line per strategy or candidate, ✓ for the one used.

    Steps are resolve.Step objects or (strategy, ok, detail) tuples (ModResolved.trail).
    """
    if not trail:
        return
    console.print("\n[bold]Tried:[/bold]")
    for step in trail:
        strategy, ok, detail = ((step.strategy, step.ok, step.detail)
                                if hasattr(step, "strategy") else step)
        mark = "[green]✓[/green]" if ok else "[red]✗[/red]"
        console.print(f"  {mark} {strategy_label(strategy):<22} {escape(detail)}")


def print_related(mod, limit: int = 5) -> None:
    """Near misses on Modrinth (addons, ports, similar names), never offered as the mod."""
    print_near_misses(mod.title, mod.project is not None,
                      [(h["title"], h.get("slug", "?")) for h in mod.related if h.get("title")],
                      limit)


def print_near_misses(title: str, identified: bool, related, limit: int = 5) -> None:
    """print_related from (title, slug) pairs (what the ModIdentified event carries)."""
    others = list(related)[:limit]
    if not others:
        return
    heading = f"Related on Modrinth (not {escape(title)})" if identified else "Did you mean"
    names = ", ".join(f"{escape(t)} ({slug})" for t, slug in others)
    console.print(f"\n[dim]{heading}: {names}[/dim]")


def print_evidence(delivered, docker_requested: bool = False) -> None:
    """The evidence line for a delivered JAR: what it passed, and what was not run."""
    from modkeel.evidence import evidence_line

    if delivered.evidence:
        console.print(f"[dim]Evidence: "
                      f"{escape(evidence_line(delivered.evidence, docker_requested))}[/dim]")


def countdown(message: str, seconds: int) -> bool:
    """A cancellable countdown: True when it runs out or Enter is pressed, False on "n".

    "<message> in 15s - press n to stop, Enter to start now", redrawn every second. Reads
    stdin without blocking: select() on POSIX, msvcrt on Windows (whose select() only works
    on sockets).
    """
    deadline = time.monotonic() + seconds
    while True:
        left = int(deadline - time.monotonic() + 0.999)
        if left <= 0:
            console.print()
            return True
        console.print(f"\r{message} in {left}s - press n to stop, Enter to start now ",
                      end="", highlight=False)
        answer = _read_key(timeout=1.0)
        if answer is not None:
            console.print()
            return not answer.strip().lower().startswith("n")


def _read_key(timeout: float) -> Optional[str]:
    """A line (POSIX) or a key (Windows) typed within `timeout` seconds, else None."""
    if sys.platform == "win32":
        import msvcrt
        end = time.monotonic() + timeout
        while time.monotonic() < end:
            if msvcrt.kbhit():
                return msvcrt.getwch()
            time.sleep(0.05)
        return None
    import select
    ready, _, _ = select.select([sys.stdin], [], [], timeout)
    return sys.stdin.readline() if ready else None


def offer_target(option, mode: str, interactive: bool, retry_hint: str) -> bool:
    """Announce another Minecraft version for the pack and decide (target.fallback_decision).

    Never silent: the proposal is printed in every mode, with the command to run it by hand
    when it is not taken.
    """
    from modkeel.target import COUNTDOWN_SECONDS, fallback_decision

    console.print(f"\n[bold]{escape(option.summary)}.[/bold]")
    take = fallback_decision(
        mode, interactive,
        lambda: countdown(f"Searching MC {option.mc_version}", COUNTDOWN_SECONDS))
    if not take:
        console.print(f"[dim]Not searched. To try it: {escape(retry_hint)}[/dim]")
    return take


def cli_decide(mode: str, retry_hint, modkeel_cfg: ModkeelConfig):
    """How the CLI answers the engine's questions (modkeel/core/decisions.py).

    ChangeTarget: announced, then the countdown / --fallback (offer_target), with
    retry_hint(option) as the command to run it by hand. NeedToken: the saved token or a
    hidden prompt in a terminal. Anything else: the engine's safe default.
    """
    from modkeel.core.decisions import ChangeTarget, NeedToken, safe_default

    def decide(question):
        if isinstance(question, ChangeTarget):
            return offer_target(question.option, mode, console.is_terminal,
                                retry_hint(question.option))
        if isinstance(question, NeedToken):
            return resolve_github_token(None, modkeel_cfg, prompt_if_missing=True)
        return safe_default(question)

    return decide


def print_target_search(event) -> None:
    """The TargetSearch event as the CLI shows it."""
    what = (f"{escape(event.subject)} runs" if event.scope == "mod"
            else "more of these mods run")
    console.print(f"\n[dim]Looking for the nearest Minecraft version where {what}...[/dim]")
