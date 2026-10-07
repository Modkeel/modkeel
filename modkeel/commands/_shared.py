"""Pieces every `modkeel` subcommand uses: console, banner, token and loader handling, and
how the resolver's outcome is shown (trail, near misses, evidence line)."""

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
    """The path the resolver took: one line per strategy or candidate, ✓ for the one used."""
    if not trail:
        return
    console.print("\n[bold]Tried:[/bold]")
    for step in trail:
        mark = "[green]✓[/green]" if step.ok else "[red]✗[/red]"
        console.print(f"  {mark} {strategy_label(step.strategy):<22} {escape(step.detail)}")


def print_related(mod, limit: int = 5) -> None:
    """Near misses on Modrinth (addons, ports, similar names), never offered as the mod."""
    others = [h for h in mod.related if h.get("title")][:limit]
    if not others:
        return
    heading = (f"Related on Modrinth (not {escape(mod.title)})" if mod.project
               else "Did you mean")
    names = ", ".join(f"{escape(h['title'])} ({h.get('slug', '?')})" for h in others)
    console.print(f"\n[dim]{heading}: {names}[/dim]")


def print_evidence(delivered, docker_requested: bool = False) -> None:
    """The evidence line for a delivered JAR: what it passed, and what was not run."""
    from modkeel.evidence import evidence_line

    if delivered.evidence:
        console.print(f"[dim]Evidence: "
                      f"{escape(evidence_line(delivered.evidence, docker_requested))}[/dim]")
