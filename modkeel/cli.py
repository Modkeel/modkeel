"""Typer CLI for Modkeel: the `modkeel` entry point (pyproject: modkeel = modkeel.cli:app).

This module only builds the app and registers the subcommands; each command lives in
modkeel.commands.<name>. The names other code may import from here (console, BANNER,
TAGLINE, resolve_github_token) are re-exported from modkeel.commands._shared.
"""

import typer

from modkeel.commands._shared import BANNER, TAGLINE, console, resolve_github_token
from modkeel.commands.compile import compile_command
from modkeel.commands.get import get_command
from modkeel.commands.move import move_command
from modkeel.commands.recommend import recommend_command
from modkeel.commands.search import search_command
from modkeel.commands.serve import serve_command
from modkeel.commands.status import status_command
from modkeel.commands.token import token_command
from modkeel.constants import MODKEEL_VERSION

__all__ = ["app", "console", "BANNER", "TAGLINE", "resolve_github_token"]

app = typer.Typer(
    name="modkeel",
    help="Minecraft Mod Auto-Compiler - find, compile, and verify unofficial mod forks.",
    add_completion=False,
)


def version_callback(value: bool):
    if value:
        console.print(f"[bold cyan]{BANNER}[/bold cyan]")
        console.print(f"\n  [bold]Modkeel v{MODKEEL_VERSION}[/bold] - {TAGLINE}\n")
        raise typer.Exit()


@app.callback()
def main(
    version: bool = typer.Option(
        False,
        "--version",
        "-V",
        help="Show version and exit.",
        callback=version_callback,
        is_eager=True,
    ),
):
    """Modkeel - Minecraft Mod Auto-Compiler."""


# Order here is the order `modkeel --help` lists them.
app.command(name="compile")(compile_command)
app.command(name="search")(search_command)
app.command(name="get")(get_command)
app.command(name="move")(move_command)
app.command(name="token")(token_command)
app.command(name="status")(status_command)
app.command(name="recommend")(recommend_command)
app.command(name="serve")(serve_command)


if __name__ == "__main__":
    app()
