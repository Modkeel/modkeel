"""`modkeel token`: save, show (masked by default) or clear the GitHub token."""

from typing import Optional

import typer

from modkeel.commands._shared import console
from modkeel.config import ModkeelConfig


def token_command(
    set_token: Optional[str] = typer.Option(
        None,
        "--set",
        help="Save a GitHub Personal Access Token.",
    ),
    clear: bool = typer.Option(
        False,
        "--clear",
        help="Remove saved token.",
    ),
    show: bool = typer.Option(
        False,
        "--show",
        help="Show the full saved token (unmasked).",
    ),
):
    """Manage saved GitHub Personal Access Token."""
    cfg = ModkeelConfig()

    if set_token:
        cfg.github_token = set_token
        console.print("[green]GitHub token saved.[/green]")
        return

    if clear:
        cfg.github_token = None
        console.print("[green]GitHub token removed.[/green]")
        return

    saved = cfg.github_token
    if not saved:
        console.print(
            "No GitHub token saved.\n\n"
            "  Set one with: [bold]modkeel token --set ghp_YOUR_TOKEN[/bold]\n"
            "  Or pass it:   [bold]modkeel get ... -t ghp_YOUR_TOKEN[/bold] "
            "(auto-saves)"
        )
        return

    if show:
        console.print(f"GitHub token: [bold]{saved}[/bold]")
    else:
        masked = saved[:4] + "****" + saved[-4:] if len(saved) > 8 else "****"
        console.print(f"GitHub token: [bold]{masked}[/bold]")
        console.print("[dim]  Use --show to reveal full token.[/dim]")
