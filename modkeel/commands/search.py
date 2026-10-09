"""`modkeel search`: what the resolver would use for a mod, without fetching it.

Runs the sources of modkeel/resolve.py in find-only mode and shows the first one with
candidates (an official build, older official builds to verify, or pre-filtered forks), with
the path that led there. Interactive runs offer to fetch it; with --no-prompt (or no
terminal) it only reports.
"""

from typing import Optional

import typer
from rich.markup import escape
from rich.panel import Panel
from rich.table import Table

from modkeel.commands._shared import (
    console,
    print_evidence,
    print_related,
    print_trail,
    resolve_github_token,
)
from modkeel.config import ModkeelConfig
from modkeel.models import ModCompilerConfig


def search_command(
    query: str = typer.Argument(
        ..., help="Mod name to search for (e.g., 'Create', 'JEI')."
    ),
    mc_version: str = typer.Option(
        ..., "--mc-version", "-m", help="Minecraft version."
    ),
    loader: str = typer.Option(
        "neoforge",
        "--loader",
        "-l",
        help="Mod loader type.",
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
    loader_version: Optional[str] = typer.Option(
        None,
        "--loader-version",
        "-lv",
        help="Mod loader version (required for compilation).",
    ),
    no_prompt: bool = typer.Option(
        False,
        "--no-prompt",
        help="Skip interactive prompts (for scripts/CI).",
    ),
):
    """Search Modrinth and GitHub for a mod. Offers to download or compile."""
    from modkeel.modrinth import ModrinthClient
    from modkeel.resolve import ResolveContext, Resolver
    from modkeel.sources import identify_mod

    modkeel_cfg = ModkeelConfig()
    github_token = resolve_github_token(github_token, modkeel_cfg)

    console.print(
        Panel(
            f"[bold]Search:[/bold] {query}\nMC {mc_version} | {loader.capitalize()}",
            title="Modkeel Search",
            border_style="cyan",
        )
    )

    # "0" marks "no loader version given": searching doesn't need one, compiling does.
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
    _print_identity(mod)

    ctx = ResolveContext(config=config, modrinth=modrinth,
                         github_token=token_on_demand, make_config=make_config)
    resolver = Resolver()
    resolution = resolver.resolve(mod, ctx, deliver=False)
    print_trail(resolution.trail)

    strategy = resolution.pending_strategy
    if strategy is None:
        print_related(mod)
        if not github_token:
            console.print(
                "\n[dim]Tip: Run 'modkeel token --set TOKEN' to enable "
                "GitHub fork search.[/dim]"
            )
        return

    _print_candidates(strategy, resolution, mc_version)
    if strategy != "official":
        print_related(mod)

    is_interactive = not no_prompt and console.is_terminal
    question = {
        "official": "Download it?",
        "curseforge": "Download it from CurseForge?",
        "older_official": "Download and verify them (nearest first)?",
        "fork": "Compile the best fork?",
    }.get(strategy, "Fetch it?")
    if not is_interactive or (strategy == "fork" and not loader_version):
        if strategy == "fork":
            console.print(
                "\n[dim]  Pre-filtered via gradle.properties. "
                "Use 'modkeel get' with -lv to build, and "
                "'--docker-test' to confirm compatibility.[/dim]"
            )
        return
    if not typer.confirm(f"\n  {question}", default=strategy != "fork"):
        return

    shown = len(resolution.trail)
    resolution = resolver.deliver_pending(resolution, mod, ctx)
    print_trail(resolution.trail[shown:])
    if resolution.delivered:
        d = resolution.delivered
        if d.caveat:
            console.print(f"\n[yellow]Note:[/yellow] {escape(d.caveat)}")
        print_evidence(d)
        console.print(f"\n[green]{d.verb.capitalize()} {escape(d.mod_name)} "
                      f"v{escape(d.mod_version)} to {output_dir}/[/green]")
    else:
        console.print("\n[red]Nothing usable was found.[/red]")


def _print_identity(mod) -> None:
    """Which Modrinth project the query resolved to (or why there is none)."""
    table = Table(title="Modrinth Project")
    table.add_column("Field", style="cyan")
    table.add_column("Value", style="green")
    if mod.project:
        table.add_row("Title", escape(mod.project["title"]))
        table.add_row("Slug", mod.project["slug"])
        table.add_row("Downloads", f"{mod.project.get('downloads', 0):,}")
        if mod.source_repo:
            table.add_row("Source", f"github.com/{mod.source_repo}")
    elif mod.lookup_error:
        table.add_row("Status", f"[red]Modrinth unavailable ({escape(mod.lookup_error)})[/red]")
    else:
        table.add_row("Status", "[yellow]Not found on Modrinth[/yellow]")
    console.print(table)


def _print_candidates(strategy: str, resolution, mc_version: str) -> None:
    """The pending candidates of the first strategy that has any."""
    candidates = resolution.pending_candidates
    if strategy == "official":
        version = candidates[0].data["version"]
        primary = next((f for f in version.get("files", []) if f.get("primary")),
                       (version.get("files") or [{}])[0])
        table = Table(title=f"Official build for MC {mc_version}")
        table.add_column("Field", style="cyan")
        table.add_column("Value", style="green")
        table.add_row("Version", escape(version.get("version_number", "?")))
        table.add_row("Type", version.get("version_type", "release"))
        table.add_row("Size", f"{primary.get('size', 0) / (1024 * 1024):.1f} MB")
        table.add_row("File", escape(primary.get("filename", "?")))
        deps = [d for d in version.get("dependencies", [])
                if d.get("dependency_type") == "required"]
        if deps:
            table.add_row("Dependencies", str(len(deps)))
        console.print(table)
    elif strategy == "curseforge":
        cf_file, cf_mod = candidates[0].data["file"], candidates[0].data["mod"]
        table = Table(title=f"CurseForge build for MC {mc_version}")
        table.add_column("Field", style="cyan")
        table.add_column("Value", style="green")
        table.add_row("Version", escape(cf_file.display))
        table.add_row("Type", cf_file.type)
        table.add_row("File", escape(cf_file.name))
        if cf_file.requires:
            table.add_row("Dependencies", str(len(cf_file.requires)))
        if not cf_file.url:
            table.add_row("Download", f"only from CurseForge: {escape(cf_mod.url or '')}")
        console.print(table)
    elif strategy == "older_official":
        table = Table(title=f"Older official builds to verify on MC {mc_version}")
        table.add_column("Build", style="cyan")
        for c in candidates:
            table.add_row(escape(c.label))
        console.print(table)
        console.print("[dim]  Each is checked before use: its metadata must allow "
                      f"{mc_version} and its bytecode must resolve against it.[/dim]")
    elif strategy == "fork":
        table = Table(title=f"Pre-filtered GitHub Forks ({resolution.pending_note})")
        table.add_column("Repository", style="cyan")
        table.add_column("Branch", style="blue")
        table.add_column("MC Version", style="green")
        table.add_column("Loader", style="yellow")
        for c in candidates:
            fork = c.data["fork"]
            best = fork["_best_branch"]
            table.add_row(fork["fork"]["full_name"], best.name,
                          best.minecraft_version or "?", best.loader or "?")
        console.print(table)
    else:
        for c in candidates:
            console.print(f"  - {escape(c.label)}")
