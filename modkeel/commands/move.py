"""`modkeel move`: move a pack to another Minecraft version.

Reads a mods folder (only reads it), identifies each JAR (exactly by its hash on Modrinth,
else by name), gets every mod for the target and writes them to <output>/mc-<version>/.
The flow is modkeel.core.move.move_pack; this command renders its events and answers its
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


def move_command(
    pack: str = typer.Argument(..., help="The pack: its mods folder, its instance folder, or "
                                         "the instance's name (see modkeel instances). "
                                         "Only read."),
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
    from modkeel.core.move import MoveRequest, move_pack
    from modkeel.core.text import print_event
    from modkeel.target import FALLBACK_MODES, default_mode

    setup_logging()
    if fallback is not None and fallback.lower() not in FALLBACK_MODES:
        console.print(f"[red]Error:[/red] --fallback must be one of {', '.join(FALLBACK_MODES)}")
        raise typer.Exit(2)
    mods_dir, instance = _pack_folder(pack)
    if instance is not None:
        loader = loader or instance.loader
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
        shown = f'"{pack}"' if " " in pack else pack
        return f"modkeel move {shown} -m {option.mc_version}"

    decide = cli_decide((fallback or default_mode(console.is_terminal)).lower(), hint,
                        modkeel_cfg)
    result = move_pack(MoveRequest(str(mods_dir), mc_version, loader, loader_version,
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


def _pack_folder(pack: str):
    """(mods folder, instance or None) for what the player typed: a folder (a mods folder,
    or an instance/game folder whose mods/ is used), else an instance's name."""
    from modkeel.instances import LAUNCHER_NAMES, find_instance, find_instances, mods_dir_of

    folder = Path(pack).expanduser()
    if folder.is_dir():
        return mods_dir_of(folder), None
    instance, candidates = find_instance(pack, find_instances())
    if instance is None:
        if candidates:
            names = ", ".join(f'"{escape(i.name)}"' for i in candidates)
            console.print(f"[red]Error:[/red] several instances match {escape(pack)!r}: {names}")
        else:
            console.print(f"[red]Error:[/red] not a folder, nor an instance's name: "
                          f"{escape(pack)} (modkeel instances lists them)")
        raise typer.Exit(2)
    mods_dir = Path(instance.mods_dir)
    if not mods_dir.is_dir():
        console.print(f"[red]Error:[/red] {escape(instance.name)} has no mods folder yet")
        raise typer.Exit(2)
    runs = " ".join(x for x in (instance.mc_version, instance.loader) if x)
    console.print(f"[dim]{LAUNCHER_NAMES[instance.launcher]}: {escape(instance.name)}"
                  f"{f' ({runs})' if runs else ''} -> {escape(str(mods_dir))}[/dim]")
    return mods_dir, instance
