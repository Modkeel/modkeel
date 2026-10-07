"""`modkeel get`: one mod in one step, through the resolver (modkeel/resolve.py).

The mod is identified first (its Modrinth project, never an addon with a similar name), then
the sources run in order: official build for the target, an older official build that still
runs on it, a community fork compiled for it (needs a token and -lv). The path taken is
printed; nothing that isn't the requested mod is ever downloaded.

When nothing runs on the requested version, the target layer (modkeel/target.py) may propose
the nearest Minecraft version with an official build: announced with a countdown in a
terminal, decided by --fallback otherwise, written to <output>/mc-<version>/ and never
installed into --instance.
"""

from typing import Optional

import typer
from rich.markup import escape
from rich.panel import Panel

from modkeel.commands._shared import (
    BANNER,
    TAGLINE,
    console,
    offer_target,
    print_evidence,
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
    from modkeel.modrinth import ModrinthClient
    from modkeel.resolve import ResolveContext, Resolver
    from modkeel.sources import identify_mod

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

    # "0" marks "no loader version given": lookups don't need one, compiling does.
    def make_config(token: Optional[str], mc: str = mc_version, out: str = output_dir,
                    lv: str = loader_version or "0",
                    inst: Optional[str] = instance) -> ModCompilerConfig:
        return ModCompilerConfig(
            mc_version=mc,
            loader=loader.lower(),
            loader_version=lv,
            github_token=token,
            output_dir=out,
            instance_path=inst,
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

    def resolve_on(cfg: ModCompilerConfig):
        """Run the resolver for `cfg`'s target; print the trail and what was delivered."""
        ctx = ResolveContext(config=cfg, modrinth=modrinth, github_token=token_on_demand,
                             make_config=lambda t: make_config(
                                 t, cfg.mc_version, str(cfg.output_dir), cfg.loader_version,
                                 str(cfg.instance_path) if cfg.instance_path else None),
                             verify_runtime=docker_test)
        resolution = Resolver().resolve(mod, ctx)
        print_trail(resolution.trail)
        delivered = resolution.delivered
        if delivered:
            _report_delivery(delivered, cfg.mc_version, docker_test)
        return delivered

    delivered = resolve_on(config)
    if delivered:
        console.print(
            f"\n[green]Done! {escape(delivered.mod_name)} v{escape(delivered.mod_version)} "
            f"{delivered.verb} to {output_dir}/[/green]"
        )
        return

    print_related(mod)
    console.print(
        f"\n[red]No build of {escape(mod.title)} for MC {mc_version} + "
        f"{loader.capitalize()} found.[/red]"
    )
    if _fallback(mod, modrinth, resolve_on, make_config, github_token, mc_version, loader,
                 output_dir, instance, fallback or default_mode(console.is_terminal)):
        return
    raise typer.Exit(1)


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


def _fallback(mod, modrinth, resolve_on, make_config, github_token, mc_version: str,
              loader: str, output_dir: str, instance: Optional[str], mode: str) -> bool:
    """The target layer for a pack of one: propose, decide, resolve on the new version.

    True when a JAR for another version was delivered (exit 0: the user let it run).
    """
    from modkeel.mappings import release_versions
    from modkeel.target import fallback_output, older_build_probe, propose_targets

    if not mod.project:
        return False
    console.print(f"\n[dim]Looking for the nearest Minecraft version where "
                  f"{escape(mod.title)} runs...[/dim]")
    options = propose_targets([(mod.title, mod.project.get("project_id"))], loader.lower(),
                              mc_version, modrinth, resolved=0, limit=1,
                              probe=older_build_probe(loader.lower(), modrinth),
                              releases=release_versions())
    if not options:
        return False
    option = options[0]
    hint = f'modkeel get "{mod.query}" -m {option.mc_version} -l {loader.lower()}'
    if not offer_target(option, mode, console.is_terminal, hint):
        return False
    out = fallback_output(output_dir, option.mc_version)
    # A different Minecraft version: never into the instance, and no -lv (it was for the
    # requested version; Docker picks the loader for this one).
    delivered = resolve_on(make_config(github_token, option.mc_version, str(out), "0", None))
    if not delivered:
        console.print(f"\n[red]Nothing usable for MC {option.mc_version} either.[/red]")
        return False
    console.print(
        f"\n[green]Done! {escape(delivered.mod_name)} v{escape(delivered.mod_version)} "
        f"for MC {option.mc_version} {delivered.verb} to {out}/[/green]"
    )
    if instance:
        console.print(f"[yellow]Not installed into {escape(instance)}: it is for MC "
                      f"{option.mc_version}, not {mc_version}.[/yellow]")
    return True
