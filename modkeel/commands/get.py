"""`modkeel get`: one mod in one step, through the resolver (modkeel/resolve.py).

The mod is identified first (its Modrinth project, never an addon with a similar name), then
the sources run in order: official build for the target, an older official build that still
runs on it, a community fork compiled for it (needs a token and -lv). The path taken is
printed; nothing that isn't the requested mod is ever downloaded.

When nothing runs on the requested version, the target layer (modkeel/target.py) may propose
the nearest Minecraft version with an official build: announced with a countdown in a
terminal, decided by --fallback otherwise, written to <output>/mc-<version>/ and never
installed into --instance.

The flow itself is modkeel.core.engine.get_mod; this command parses the arguments, renders
its events (_GetView) and answers its questions (cli_decide).
"""

from typing import Optional

import typer
from rich.markup import escape
from rich.panel import Panel

from modkeel.commands._shared import (
    BANNER,
    TAGLINE,
    cli_decide,
    console,
    print_evidence,
    print_near_misses,
    print_target_search,
    print_trail,
    require_valid_loader,
    resolve_github_token,
)
from modkeel.config import ModkeelConfig
from modkeel.constants import MODKEEL_VERSION
from modkeel.utils import setup_logging


def get_command(
    query: str = typer.Argument(..., help="Mod name (e.g., 'JEI', 'Create')."),
    mc_version: str = typer.Option(
        ..., "--mc-version", "-m", help="Minecraft version (e.g., 1.21.1)."
    ),
    loader: str = typer.Option(
        "neoforge",
        "--loader",
        "-l",
        help="Mod loader type.",
        case_sensitive=False,
    ),
    loader_version: Optional[str] = typer.Option(
        None,
        "--loader-version",
        "-lv",
        help="Mod loader version (required for fork compilation).",
    ),
    github_token: Optional[str] = typer.Option(
        None,
        "--github-token",
        "-t",
        help="GitHub Personal Access Token.",
    ),
    output_dir: str = typer.Option(
        "out",
        "--output-dir",
        "-o",
        help="Directory for downloaded/compiled JARs.",
    ),
    instance: Optional[str] = typer.Option(
        None,
        "--instance",
        "-i",
        help="Path to Minecraft instance directory.",
    ),
    docker_test: bool = typer.Option(
        False,
        "--docker-test",
        help="Boot a headless server with the result; a JAR that crashes it is rejected "
             "and the next candidate is tried.",
    ),
    fallback: Optional[str] = typer.Option(
        None,
        "--fallback",
        help="When nothing runs on this version, try the nearest version with an official "
             "build: ask (countdown; default in a terminal), auto, never (default otherwise).",
        case_sensitive=False,
    ),
):
    """Find and download/compile a mod in one step."""
    from modkeel.target import FALLBACK_MODES, default_mode

    setup_logging()
    if fallback is not None and fallback.lower() not in FALLBACK_MODES:
        console.print(f"[red]Error:[/red] --fallback must be one of {', '.join(FALLBACK_MODES)}")
        raise typer.Exit(2)

    modkeel_cfg = ModkeelConfig()
    github_token = resolve_github_token(github_token, modkeel_cfg)
    require_valid_loader(loader)

    console.print(
        Panel(
            f"[bold cyan]{BANNER}[/bold cyan]\n\n"
            f"  [bold]v{MODKEEL_VERSION}[/bold] - {TAGLINE}\n\n"
            f"  [bold]Get:[/bold] {query}\n"
            f"  MC {mc_version} | {loader.capitalize()}"
            + (f" {loader_version}" if loader_version else ""),
            title="Modkeel Get",
            border_style="green",
        )
    )

    from modkeel.core.engine import GetRequest, get_mod

    request = GetRequest(query, mc_version, loader, loader_version, output_dir, instance,
                         verify_runtime=docker_test, github_token=github_token)

    def hint(option) -> str:
        return f'modkeel get "{query}" -m {option.mc_version} -l {loader.lower()}'

    decide = cli_decide((fallback or default_mode(console.is_terminal)).lower(), hint,
                        modkeel_cfg)
    result = get_mod(request, events=_GetView(mc_version, loader), decide=decide)

    delivered = result.delivered
    if not result.retargeted:
        if delivered:
            console.print(
                f"\n[green]Done! {escape(delivered.mod_name)} v{escape(delivered.mod_version)} "
                f"{delivered.verb} to {output_dir}/[/green]"
            )
            return
        raise typer.Exit(1)
    if not delivered:
        console.print(f"\n[red]Nothing usable for MC {result.target} either.[/red]")
        raise typer.Exit(1)
    console.print(
        f"\n[green]Done! {escape(delivered.mod_name)} v{escape(delivered.mod_version)} "
        f"for MC {result.target} {delivered.verb} to {result.output_dir}/[/green]"
    )
    if instance:
        console.print(f"[yellow]Not installed into {escape(instance)}: it is for MC "
                      f"{result.target}, not {mc_version}.[/yellow]")


class _GetView:
    """Renders get_mod's events as the get command's output (Rich); the rest as plain lines.

    After the first target's trail: what was delivered, or the near misses and "no build"
    line that precede a version proposal.
    """

    def __init__(self, mc_version: str, loader: str):
        self.mc_version, self.loader = mc_version, loader
        self.identified = None            # the ModIdentified event

    def __call__(self, event) -> None:
        from modkeel.core.events import ModIdentified, ModResolved, TargetSearch
        from modkeel.core.text import print_event

        if isinstance(event, ModIdentified):
            self.identified = event
            if event.identified:
                repo = f" - github.com/{event.source_repo}" if event.source_repo else ""
                console.print(f"\n  Mod: [bold]{escape(event.title)}[/bold] "
                              f"({event.slug}){repo}")
        elif isinstance(event, ModResolved):
            print_trail(event.trail)
            if event.delivered:
                _report_delivery(event.delivered, event.target, event.verify_runtime)
            elif not event.retarget:
                mod = self.identified
                print_near_misses(mod.title, mod.identified, mod.related)
                console.print(
                    f"\n[red]No build of {escape(mod.title)} for MC {self.mc_version} + "
                    f"{self.loader.capitalize()} found.[/red]"
                )
        elif isinstance(event, TargetSearch):
            print_target_search(event)
        else:
            print_event(event)


def _report_delivery(delivered, mc_version: str, docker_test: bool) -> None:
    """Caveat, server test note and evidence line of a delivered JAR."""
    from modkeel.sources import caveat_after_docker

    if delivered.unverified:
        console.print(f"\n[yellow]Server test not run:[/yellow] {escape(delivered.unverified)}")
    if "docker_server" in delivered.evidence and delivered.caveat:
        console.print(f"\n[yellow]Note:[/yellow] "
                      f"{escape(caveat_after_docker(delivered.caveat, mc_version))}")
    elif delivered.caveat:
        console.print(f"\n[yellow]Note:[/yellow] {escape(delivered.caveat)}")
    print_evidence(delivered, docker_requested=docker_test)
