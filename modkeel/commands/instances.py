"""`modkeel instances`: the player's launcher instances (modkeel/instances.py), so a pack can
be named instead of looked for: `modkeel move "All the Mods 10" -m 1.21.10`."""

import json

import typer
from rich.markup import escape
from rich.table import Table

from modkeel.commands._shared import console


def instances_command(
    as_json: bool = typer.Option(False, "--json", help="One JSON list, for scripts."),
):
    """List the Minecraft instances of Prism, Modrinth App, CurseForge and .minecraft."""
    from modkeel.instances import LAUNCHER_NAMES, find_instances

    found = find_instances()
    if as_json:
        print(json.dumps([i.to_dict() for i in found], indent=2))
        return
    if not found:
        console.print("No instances found (Prism Launcher, Modrinth App, CurseForge, "
                      ".minecraft/mods). Give `modkeel move` a mods folder instead.")
        return
    table = Table(title="Instances")
    for column in ("Name", "Minecraft", "Loader", "Mods", "Launcher"):
        table.add_column(column)
    for i in found:
        loader = " ".join(x for x in (i.loader, i.loader_version) if x) or "[dim]-[/dim]"
        table.add_row(escape(i.name), i.mc_version or "[dim]?[/dim]", loader, str(i.mods),
                      LAUNCHER_NAMES[i.launcher])
    console.print(table)
    console.print('[dim]Move one: modkeel move "<name>" -m <version>[/dim]')
