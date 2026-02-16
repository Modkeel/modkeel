"""Typer CLI for ModForge."""

import logging
import sys
from pathlib import Path
from typing import Optional

import typer
from rich.console import Console
from rich.panel import Panel
from rich.table import Table

from modforge.config import ModForgeConfig, prompt_sharing_preference
from modforge.constants import MODFORGE_VERSION
from modforge.loaders import ALL_LOADERS, KNOWN_LOADER_VERSIONS
from modforge.models import CompilationResult, FailureType, ModCompilerConfig
from modforge.pipeline import Pipeline
from modforge.utils import setup_logging, setup_windows_console

BANNER = r"""    __  ___          ______
   /  |/  /___  ____/ / __/___  _________ ____
  / /|_/ / __ \/ __  / /_/ __ \/ ___/ __ `/ _ \
 / /  / / /_/ / /_/ / __/ /_/ / /  / /_/ /  __/
/_/  /_/\____/\__,_/_/  \____/_/   \__, /\___/
                                  /____/"""

TAGLINE = "Compile the mods Mojang left behind"

setup_windows_console()
console = Console()
app = typer.Typer(
    name="modforge",
    help="Minecraft Mod Auto-Compiler - find, compile, and verify unofficial mod forks.",
    add_completion=False,
)


def version_callback(value: bool):
    if value:
        console.print(f"[bold cyan]{BANNER}[/bold cyan]")
        console.print(f"\n  [bold]ModForge v{MODFORGE_VERSION}[/bold] - {TAGLINE}\n")
        raise typer.Exit()


@app.callback()
def main(
    version: bool = typer.Option(
        False, "--version", "-V",
        help="Show version and exit.",
        callback=version_callback,
        is_eager=True,
    ),
):
    """ModForge - Minecraft Mod Auto-Compiler."""


@app.command()
def compile(
    repos_file: Path = typer.Argument(
        ..., help="Text file containing GitHub repository URLs (one per line).",
        exists=True, readable=True,
    ),
    mc_version: str = typer.Option(
        ..., "--mc-version", "-m", help="Minecraft version (e.g., 1.21.10)."
    ),
    loader: str = typer.Option(
        ..., "--loader", "-l", help="Mod loader type.",
        case_sensitive=False,
    ),
    loader_version: str = typer.Option(
        ..., "--loader-version", "-lv", help="Mod loader version (e.g., 64)."
    ),
    instance: Optional[str] = typer.Option(
        None, "--instance", "-i",
        help="Path to Minecraft instance directory.",
    ),
    output_dir: str = typer.Option(
        "out", "--output-dir", "-o",
        help="Directory for compiled JARs.",
    ),
    github_token: Optional[str] = typer.Option(
        None, "--github-token", "-t",
        help="GitHub Personal Access Token.",
    ),
    strict: bool = typer.Option(
        False, "--strict",
        help="Require exact Minecraft version match.",
    ),
    no_cross_loader: bool = typer.Option(
        False, "--no-cross-loader",
        help="Disable Fabric fallback via Sinytra Connector.",
    ),
    output_report: Optional[str] = typer.Option(
        None, "--output-report",
        help="Path to save the compilation report.",
    ),
    log_file: Optional[str] = typer.Option(
        None, "--log-file",
        help="Path to write a log file.",
    ),
    docker_test: bool = typer.Option(
        False, "--docker-test",
        help="Test compiled mods in a headless Docker Minecraft server.",
    ),
    docker_timeout: int = typer.Option(
        180, "--docker-timeout",
        help="Seconds to wait for Docker server startup.",
    ),
    no_share: bool = typer.Option(
        False, "--no-share",
        help="Skip anonymous data sharing for this run.",
    ),
):
    """Compile mods from a list of GitHub repositories."""
    setup_logging(log_file)

    # Validate loader
    if loader.lower() not in ALL_LOADERS:
        console.print(
            f"[red]Error:[/red] Invalid loader '{loader}'. "
            f"Must be one of: {', '.join(ALL_LOADERS)}"
        )
        raise typer.Exit(1)

    # Read repository URLs
    with open(repos_file, 'r', encoding='utf-8') as f:
        repo_urls = [
            line.strip() for line in f
            if line.strip() and not line.startswith('#')
        ]

    if not repo_urls:
        console.print(f"[red]Error:[/red] No repository URLs found in {repos_file}")
        raise typer.Exit(1)

    console.print(
        Panel(
            f"[bold cyan]{BANNER}[/bold cyan]\n\n"
            f"  [bold]v{MODFORGE_VERSION}[/bold] - {TAGLINE}\n\n"
            f"  MC {mc_version} | {loader.capitalize()} {loader_version}\n"
            f"  {len(repo_urls)} repositories loaded from {repos_file}",
            title="ModForge",
            border_style="blue",
        )
    )

    # Load persistent config
    modforge_cfg = ModForgeConfig()
    if modforge_cfg.is_first_run and not modforge_cfg.was_prompted:
        prompt_sharing_preference(modforge_cfg)

    # Create configuration
    try:
        config = ModCompilerConfig(
            mc_version=mc_version,
            loader=loader.lower(),
            loader_version=loader_version,
            instance_path=instance,
            github_token=github_token,
            strict_version=strict,
            output_dir=output_dir,
            cross_loader=not no_cross_loader,
            docker_test=docker_test,
            docker_timeout=docker_timeout,
        )
    except ValueError as e:
        console.print(f"[red]Configuration error:[/red] {e}")
        raise typer.Exit(1)

    # Run pipeline
    pipeline = Pipeline(config)
    pipeline.process_repos(repo_urls)

    # Generate and display report
    report = pipeline.generate_report()
    print(report)

    # Save report if requested
    if output_report:
        with open(output_report, 'w', encoding='utf-8') as f:
            f.write(report)
        console.print(f"\nReport saved to: {output_report}")

    # Submit anonymous crowdsource reports
    if not no_share:
        try:
            from modforge.crowdsource import submit_reports
            from modforge.github import parse_repo_url
            submit_reports(
                pipeline.results, modforge_cfg,
                mc_version, loader.lower(), loader_version,
                parse_repo_url,
            )
        except Exception as e:
            logging.getLogger("modforge").debug(
                "Crowdsource submission error: %s", e
            )

    # Exit with error code if all mods failed
    if pipeline.results and not any(r.success for r in pipeline.results):
        raise typer.Exit(1)


@app.command()
def search(
    query: str = typer.Argument(
        ..., help="Mod name to search for (e.g., 'Create', 'JEI')."
    ),
    mc_version: str = typer.Option(
        ..., "--mc-version", "-m", help="Minecraft version."
    ),
    loader: str = typer.Option(
        "neoforge", "--loader", "-l", help="Mod loader type.",
    ),
    github_token: Optional[str] = typer.Option(
        None, "--github-token", "-t",
        help="GitHub Personal Access Token.",
    ),
):
    """Search Modrinth and GitHub for a mod without compiling."""
    from modforge.github import GitHubClient
    from modforge.modrinth import ModrinthClient

    console.print(
        Panel(
            f"[bold]Search:[/bold] {query}\n"
            f"MC {mc_version} | {loader.capitalize()}",
            title="ModForge Search",
            border_style="cyan",
        )
    )

    # Minimal config for search
    config = ModCompilerConfig(
        mc_version=mc_version,
        loader=loader.lower(),
        loader_version="0",
        github_token=github_token,
        output_dir="out",
    )

    # Modrinth search
    modrinth = ModrinthClient(config)
    result = modrinth.check_modrinth(query)

    table = Table(title="Modrinth Results")
    table.add_column("Field", style="cyan")
    table.add_column("Value", style="green")

    if result:
        table.add_row("Title", result["title"])
        table.add_row("Slug", result["slug"])
        table.add_row("Version", result["version_number"])
        table.add_row("Type", result["version_type"])
        size_mb = result["file_size"] / (1024 * 1024)
        table.add_row("Size", f"{size_mb:.1f} MB")
        table.add_row("Downloads", f"{result['downloads']:,}")
        table.add_row("File", result["filename"])
        if result.get("required_deps"):
            table.add_row("Dependencies", str(len(result["required_deps"])))
    else:
        table.add_row("Status", "[yellow]Not found on Modrinth[/yellow]")

    console.print(table)

    # GitHub fork search with pre-filtering
    if github_token:
        from modforge.validation import BranchValidator

        console.print("\n[bold]GitHub Forks:[/bold]")
        github = GitHubClient(config)
        forks = github.search_compatible_repos(query, query, False)

        if forks:
            validator = BranchValidator(github, config)
            validated_forks = []

            for fork in forks[:10]:
                fork_info = fork["fork"]
                fork_owner = fork_info["owner"]
                fork_repo = fork_info["repo"]

                branches = github.get_branches(fork_owner, fork_repo)
                if not branches:
                    continue

                compatible = validator.pre_validate_branches(
                    fork_owner, fork_repo, branches
                )
                if compatible:
                    best = max(compatible, key=lambda b: validator.score_branch(b))
                    fork["_best_branch"] = best
                    validated_forks.append(fork)

            total_found = len(forks)
            passed = len(validated_forks)

            fork_table = Table(
                title=f"Pre-filtered GitHub Forks ({passed} of {total_found} passed)"
            )
            fork_table.add_column("Repository", style="cyan")
            fork_table.add_column("Branch", style="blue")
            fork_table.add_column("MC Version", style="green")
            fork_table.add_column("Loader", style="yellow")
            fork_table.add_column("Score", style="green", justify="right")

            for fork in validated_forks:
                best = fork["_best_branch"]
                fork_table.add_row(
                    fork["fork"]["full_name"],
                    best.name,
                    best.minecraft_version or "?",
                    best.loader or "?",
                    str(fork["score"]),
                )

            console.print(fork_table)

            if validated_forks:
                console.print(
                    "\n[dim]  Pre-filtered via gradle.properties. "
                    "Use 'modforge compile' to build and "
                    "'--docker-test' to confirm compatibility.[/dim]"
                )
            else:
                console.print(
                    "[yellow]No forks passed pre-filtering "
                    f"for MC {mc_version} + {loader}.[/yellow]"
                )
        else:
            console.print("[yellow]No GitHub forks found.[/yellow]")
    else:
        console.print(
            "\n[dim]Tip: Use --github-token to also search GitHub forks.[/dim]"
        )


@app.command()
def status():
    """Show ModForge status: version, config, and Docker cache info."""
    from modforge.models import DockerTestCache

    console.print(
        Panel(
            f"[bold]ModForge v{MODFORGE_VERSION}[/bold]",
            title="Status",
            border_style="green",
        )
    )

    # Config info
    config_dir = Path.home() / ".modforge"
    config_file = config_dir / "config.toml"

    table = Table(title="Configuration")
    table.add_column("Setting", style="cyan")
    table.add_column("Value", style="green")

    table.add_row("Config directory", str(config_dir))
    table.add_row(
        "Config file",
        str(config_file) if config_file.exists() else "[dim]not created[/dim]",
    )

    if config_file.exists():
        cfg = ModForgeConfig()
        table.add_row("Client ID", cfg.client_id[:8] + "...")
        table.add_row("Data sharing", str(cfg.sharing))

    console.print(table)

    # Docker cache
    cache_dir = config_dir / "loaders"
    if cache_dir.exists():
        loader_table = Table(title="Cached Loaders")
        loader_table.add_column("Loader", style="cyan")
        loader_table.add_column("MC Version", style="green")
        loader_table.add_column("Status", style="yellow")

        for loader_dir in sorted(cache_dir.iterdir()):
            if loader_dir.is_dir():
                for version_dir in sorted(loader_dir.iterdir()):
                    if version_dir.is_dir():
                        has_run = (
                            (version_dir / "run.sh").exists()
                            or (version_dir / "run.bat").exists()
                        )
                        status_str = (
                            "[green]installed[/green]"
                            if has_run
                            else "[yellow]partial[/yellow]"
                        )
                        loader_table.add_row(
                            loader_dir.name,
                            version_dir.name,
                            status_str,
                        )
        console.print(loader_table)
    else:
        console.print("[dim]No cached loaders found.[/dim]")

    # Docker test cache
    cache_file = config_dir / "docker_cache.json"
    if cache_file.exists():
        import json
        try:
            data = json.loads(cache_file.read_text())
            console.print(
                f"\nDocker test cache: [green]{len(data)} entries[/green]"
            )
        except Exception:
            pass

    # Known loader versions (all loaders that have entries)
    for loader_name, versions in sorted(KNOWN_LOADER_VERSIONS.items()):
        if not versions:
            continue
        from modforge.loaders import get_profile
        display_name = get_profile(loader_name)["display_name"]
        ver_table = Table(title=f"Known {display_name} Versions")
        ver_table.add_column("MC Version", style="cyan")
        ver_table.add_column(f"{display_name} Version", style="green")
        for mc, lv in sorted(versions.items(), reverse=True):
            ver_table.add_row(mc, lv)
        console.print(ver_table)


if __name__ == "__main__":
    app()
