"""`modkeel login`: sign in with GitHub in the browser instead of making a token by hand.

The OAuth device flow (modkeel/ghauth.py): Modkeel shows a code, the player enters it on
github.com/login/device, and the token is saved where `modkeel token` keeps it. It reads
public data only, under the player's own GitHub rate limit (5000 requests/hour).
"""

import webbrowser

import typer
from rich.markup import escape

from modkeel.commands._shared import console


def login_command(
    no_browser: bool = typer.Option(False, "--no-browser",
                                    help="Only print the page and the code."),
):
    """Sign in with GitHub (searching forks needs it); the token is saved for every run."""
    from modkeel.core.events import GitHubCode
    from modkeel.ghauth import SignInError, sign_in

    def show(event) -> None:
        if isinstance(event, GitHubCode):
            console.print(f"\nOpen [bold]{escape(event.url)}[/bold] and enter the code\n\n"
                          f"    [bold cyan]{escape(event.code)}[/bold cyan]\n\n"
                          f"[dim]Valid {event.expires_in // 60} minutes. Waiting for GitHub "
                          "(Ctrl+C to stop)...[/dim]")
            if not no_browser and console.is_terminal:
                try:
                    webbrowser.open(event.url)
                except Exception:   # no browser here: the printed page is enough
                    pass

    try:
        done = sign_in(show)
    except SignInError as e:
        console.print(f"[red]GitHub sign-in failed:[/red] {escape(str(e))}")
        raise typer.Exit(1)
    who = f" as [bold]{escape(done.user)}[/bold]" if done.user else ""
    console.print(f"[green]Signed in with GitHub{who}.[/green] "
                  "[dim]Token saved to ~/.modkeel/config.toml (modkeel token --clear removes "
                  "it; revoke it on GitHub under Settings > Applications).[/dim]")
