"""`modkeel get`: one mod in one step, through the resolver (modkeel/resolve.py).

The mod is identified first (its Modrinth project, never an addon with a similar name), then
the sources run in order: official build for the target, an older official build that still
runs on it, a community fork compiled for it (needs a token and -lv). The path taken is
printed; nothing that isn't the requested mod is ever downloaded.
"""

from typing import Optional

import typer
from rich.markup import escape
from rich.panel import Panel

from modkeel.commands._shared import (
    BANNER,
    TAGLINE,
    console,
    docker_test_delivered,
    print_related,
    print_trail,
    require_valid_loader,
    resolve_github_token,
)
from modkeel.config import ModkeelConfig
from modkeel.constants import MODKEEL_VERSION
from modkeel.models import ModCompilerConfig
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
        help="Test compiled mod in Docker after compilation.",
    ),
):
    """Find and download/compile a mod in one step."""
    from modkeel.modrinth import ModrinthClient
    from modkeel.resolve import ResolveContext, Resolver
    from modkeel.sources import caveat_after_docker, identify_mod

    setup_logging()

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

    # "0" marks "no loader version given": lookups don't need one, compiling does.
    def make_config(token: Optional[str]) -> ModCompilerConfig:
        return ModCompilerConfig(
            mc_version=mc_version,
            loader=loader.lower(),
            loader_version=loader_version or "0",
            github_token=token,
            output_dir=output_dir,
            instance_path=instance,
        )

    def token_on_demand() -> Optional[str]:
        """The fork strategy calls this; a token is asked for only when forks are needed."""
        nonlocal github_token
        if not github_token:
            github_token = resolve_github_token(None, modkeel_cfg, prompt_if_missing=True)
        return github_token

    config = make_config(github_token)
    modrinth = ModrinthClient(config)
    mod = identify_mod(query, modrinth)
    if mod.project:
        repo = f" - github.com/{mod.source_repo}" if mod.source_repo else ""
        console.print(f"\n  Mod: [bold]{escape(mod.title)}[/bold] "
                      f"({mod.project['slug']}){repo}")

    ctx = ResolveContext(config=config, modrinth=modrinth,
                         github_token=token_on_demand, make_config=make_config)
    resolution = Resolver().resolve(mod, ctx)
    print_trail(resolution.trail)

    delivered = resolution.delivered
    if not delivered:
        print_related(mod)
        console.print(
            f"\n[red]No build of {escape(mod.title)} for MC {mc_version} + "
            f"{loader.capitalize()} found.[/red]"
        )
        raise typer.Exit(1)

    if docker_test:
        docker_test_delivered(delivered, make_config(github_token))

    if "docker_server" in delivered.evidence and delivered.caveat:
        console.print(f"\n[yellow]Note:[/yellow] "
                      f"{escape(caveat_after_docker(delivered.caveat, mc_version))}")
    elif delivered.caveat:
        console.print(f"\n[yellow]Note:[/yellow] {escape(delivered.caveat)}")
    console.print(
        f"\n[green]Done! {escape(delivered.mod_name)} v{escape(delivered.mod_version)} "
        f"{delivered.verb} to {output_dir}/[/green]"
    )
