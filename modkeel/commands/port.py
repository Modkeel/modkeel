"""`modkeel port`: move a pack to another Minecraft version.

Reads a mods folder (only reads it), identifies each JAR (exactly by its hash on Modrinth,
else by name), gets every mod for the target and writes them to <output>/mc-<version>/.
The flow is modkeel.core.port.port_pack; this command renders its events and answers its
questions like `get` does.
"""

from pathlib import Path
from typing import Optional

import typer
from rich.markup import escape

from modkeel.commands._shared import (
    cli_decide,
    console,
    print_target_search,
    print_trail,
    require_valid_loader,
    resolve_github_token,
)
from modkeel.config import ModkeelConfig
from modkeel.utils import setup_logging

STATUS = {"delivered": "[green]✓[/green]", "reused": "[green]↻[/green]",
          "missing": "[red]✗[/red]", "unknown": "[yellow]?[/yellow]"}


def port_command(
    mods_dir: Path = typer.Argument(..., help="The pack's mods folder (only read)."),
    mc_version: str = typer.Option(..., "--mc-version", "-m",
                                   help="Minecraft version to move the pack to."),
    loader: Optional[str] = typer.Option(None, "--loader", "-l",
                                         help="Default: the loader the folder's mods use."),
    loader_version: Optional[str] = typer.Option(None, "--loader-version", "-lv",
                                                 help="Needed only to compile forks."),
    github_token: Optional[str] = typer.Option(None, "--github-token", "-t"),
    output_dir: str = typer.Option("out", "--output-dir", "-o",
                                   help="Where mc-<version>/ is written."),
    fallback: Optional[str] = typer.Option(
        None, "--fallback",
        help="When mods are left without a build, try the nearest version where more of the "
             "pack runs: ask (countdown; default in a terminal), auto, never.",
        case_sensitive=False),
):
    """Move a pack (a mods folder) to another Minecraft version."""
    from modkeel.core.events import ModResolved, PackScanned, TargetSearch
    from modkeel.core.port import PortRequest, port_pack
    from modkeel.core.text import print_event
    from modkeel.target import FALLBACK_MODES, default_mode

    setup_logging()
    if fallback is not None and fallback.lower() not in FALLBACK_MODES:
        console.print(f"[red]Error:[/red] --fallback must be one of {', '.join(FALLBACK_MODES)}")
        raise typer.Exit(2)
    if not mods_dir.is_dir():
        console.print(f"[red]Error:[/red] not a folder: {escape(str(mods_dir))}")
        raise typer.Exit(2)
    if loader:
        require_valid_loader(loader)
    modkeel_cfg = ModkeelConfig()
    token = resolve_github_token(github_token, modkeel_cfg)

    def view(event) -> None:
        if isinstance(event, ModResolved):
            console.print(f"\n[bold]{escape(event.mod)}[/bold] [dim]MC {event.target}[/dim]")
            print_trail(event.trail)
        elif isinstance(event, TargetSearch):
            print_target_search(event)
        elif isinstance(event, PackScanned):
            print_event(event)
        else:
            print_event(event)

    def hint(option) -> str:
        return f"modkeel port {mods_dir} -m {option.mc_version}"

    decide = cli_decide((fallback or default_mode(console.is_terminal)).lower(), hint,
                        modkeel_cfg)
    result = port_pack(PortRequest(str(mods_dir), mc_version, loader, loader_version,
                                   output_dir, token), events=view, decide=decide)

    console.print(f"\n[bold]MC {result.target} ({result.loader}): {result.ready} of "
                  f"{len(result.mods)} ready in {escape(str(result.output_dir))}/[/bold]")
    for m in result.mods:
        how = "" if m.identified_by == "hash" else \
            " [dim](guessed by name)[/dim]" if m.identified_by == "name" else ""
        line = f"  {STATUS[m.status]} {escape(m.name)}{how}"
        if m.status != "delivered" and m.detail:
            line += f" [dim]- {escape(m.detail)}[/dim]"
        console.print(line)
    if result.ready == 0:
        raise typer.Exit(1)
