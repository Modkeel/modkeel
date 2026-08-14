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


def resolve_github_token(
    explicit: Optional[str],
    modforge_cfg: ModForgeConfig,
    prompt_if_missing: bool = False,
) -> Optional[str]:
    """Resolve GitHub token: CLI flag > saved config > interactive prompt.

    Args:
        explicit: Token passed via -t flag.
        modforge_cfg: Persistent config.
        prompt_if_missing: If True and no token found, prompt the user
            interactively. Only prompts if running in a terminal.
    """
    if explicit:
        if explicit != modforge_cfg.github_token:
            modforge_cfg.github_token = explicit
            console.print("[dim]  GitHub token saved to ~/.modforge/config.toml[/dim]")
        return explicit
    saved = modforge_cfg.github_token
    if saved:
        console.print("[dim]  Using saved GitHub token.[/dim]")
        return saved
    if prompt_if_missing and console.is_terminal:
        console.print(
            "\n[yellow]No GitHub token found.[/yellow] "
            "A token is needed to search GitHub forks.\n"
            "  Create one at: [bold]https://github.com/settings/tokens[/bold]\n"
            "  Scopes needed: [dim]none (public repo access only)[/dim]\n"
        )
        token = typer.prompt("  GitHub token", hide_input=True, default="")
        if token:
            modforge_cfg.github_token = token
            console.print("[green]Token saved to ~/.modforge/config.toml[/green]")
            return token
    return None


def version_callback(value: bool):
    if value:
        console.print(f"[bold cyan]{BANNER}[/bold cyan]")
        console.print(f"\n  [bold]ModForge v{MODFORGE_VERSION}[/bold] - {TAGLINE}\n")
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
    """ModForge - Minecraft Mod Auto-Compiler."""


@app.command()
def compile(
    repos_file: Path = typer.Argument(
        ...,
        help="Text file containing GitHub repository URLs (one per line).",
        exists=True,
        readable=True,
    ),
    mc_version: str = typer.Option(
        ..., "--mc-version", "-m", help="Minecraft version (e.g., 1.21.10)."
    ),
    loader: str = typer.Option(
        ...,
        "--loader",
        "-l",
        help="Mod loader type.",
        case_sensitive=False,
    ),
    loader_version: str = typer.Option(
        ..., "--loader-version", "-lv", help="Mod loader version (e.g., 64)."
    ),
    instance: Optional[str] = typer.Option(
        None,
        "--instance",
        "-i",
        help="Path to Minecraft instance directory.",
    ),
    output_dir: str = typer.Option(
        "out",
        "--output-dir",
        "-o",
        help="Directory for compiled JARs.",
    ),
    github_token: Optional[str] = typer.Option(
        None,
        "--github-token",
        "-t",
        help="GitHub Personal Access Token.",
    ),
    strict: bool = typer.Option(
        False,
        "--strict",
        help="Require exact Minecraft version match.",
    ),
    no_cross_loader: bool = typer.Option(
        False,
        "--no-cross-loader",
        help="Disable Fabric fallback via Sinytra Connector.",
    ),
    output_report: Optional[str] = typer.Option(
        None,
        "--output-report",
        help="Path to save the compilation report.",
    ),
    log_file: Optional[str] = typer.Option(
        None,
        "--log-file",
        help="Path to write a log file.",
    ),
    docker_test: bool = typer.Option(
        False,
        "--docker-test",
        help="Test compiled mods in a headless Docker Minecraft server.",
    ),
    docker_timeout: int = typer.Option(
        180,
        "--docker-timeout",
        help="Seconds to wait for Docker server startup.",
    ),
    no_share: bool = typer.Option(
        False,
        "--no-share",
        help="Skip anonymous data sharing for this run.",
    ),
    no_prebuild_gate: bool = typer.Option(
        False,
        "--no-prebuild-gate",
        help="Do not skip branches with deterministic build failures.",
    ),
    no_prebuilt: bool = typer.Option(
        False,
        "--no-prebuilt",
        help="Always compile, even when a published JAR already exists.",
    ),
    no_symbol_check: bool = typer.Option(
        False, "--no-symbol-check",
        help="Skip verifying Minecraft symbols against official mappings.",
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
    with open(repos_file, "r", encoding="utf-8") as f:
        repo_urls = [
            line.strip() for line in f if line.strip() and not line.startswith("#")
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

    # Load persistent config + resolve token
    modforge_cfg = ModForgeConfig()
    github_token = resolve_github_token(
        github_token, modforge_cfg, prompt_if_missing=True
    )
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
            prebuild_gate=not no_prebuild_gate,
            use_prebuilt=not no_prebuilt,
            symbol_check=not no_symbol_check,
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
        with open(output_report, "w", encoding="utf-8") as f:
            f.write(report)
        console.print(f"\nReport saved to: {output_report}")

    # Submit anonymous crowdsource reports
    if not no_share:
        try:
            from modforge.crowdsource import submit_reports
            from modforge.github import parse_repo_url

            submit_reports(
                pipeline.results,
                modforge_cfg,
                mc_version,
                loader.lower(),
                loader_version,
                parse_repo_url,
            )
        except Exception as e:
            logging.getLogger("modforge").debug("Crowdsource submission error: %s", e)

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
    from modforge.github import GitHubClient
    from modforge.modrinth import ModrinthClient

    # Resolve token
    modforge_cfg = ModForgeConfig()
    github_token = resolve_github_token(github_token, modforge_cfg)

    console.print(
        Panel(
            f"[bold]Search:[/bold] {query}\nMC {mc_version} | {loader.capitalize()}",
            title="ModForge Search",
            border_style="cyan",
        )
    )

    # Minimal config for search (use loader_version="0" as placeholder)
    config = ModCompilerConfig(
        mc_version=mc_version,
        loader=loader.lower(),
        loader_version=loader_version or "0",
        github_token=github_token,
        output_dir=output_dir,
        instance_path=instance,
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
        console.print(table)

        # Mod is on Modrinth -- skip fork search, offer download
        is_interactive = not no_prompt and console.is_terminal
        if is_interactive:
            download = typer.confirm(
                "\n  Available on Modrinth. Download?", default=True
            )
            if download:
                jar = modrinth.download_modrinth_mod(
                    result["slug"], mc_version, loader.lower()
                )
                if jar:
                    modrinth.download_modrinth_deps(result)
                    console.print(f"\n[green]Downloaded to {output_dir}/[/green]")
                else:
                    console.print("[red]Download failed.[/red]")
        return

    # Not found on Modrinth
    table.add_row("Status", "[yellow]Not found on Modrinth[/yellow]")
    console.print(table)

    # Prompt for token if missing (only needed for fork search)
    if not github_token:
        github_token = resolve_github_token(None, modforge_cfg, prompt_if_missing=True)
        if github_token:
            # Rebuild config with token
            config = ModCompilerConfig(
                mc_version=mc_version,
                loader=loader.lower(),
                loader_version=loader_version or "0",
                github_token=github_token,
                output_dir=output_dir,
                instance_path=instance,
            )

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
                # Offer compilation if interactive and loader_version provided
                is_interactive = not no_prompt and console.is_terminal
                if is_interactive and loader_version:
                    compile_it = typer.confirm("\n  Compile best fork?", default=False)
                    if compile_it:
                        best_fork = validated_forks[0]
                        best_branch = best_fork["_best_branch"]
                        fork_name = best_fork["fork"]["full_name"]
                        repo_url = f"https://github.com/{fork_name}"

                        full_config = ModCompilerConfig(
                            mc_version=mc_version,
                            loader=loader.lower(),
                            loader_version=loader_version,
                            github_token=github_token,
                            output_dir=output_dir,
                            instance_path=instance,
                        )
                        pipeline = Pipeline(full_config)
                        pipeline.temp_dir = __import__("tempfile").mkdtemp(
                            prefix="mod_compiler_"
                        )
                        try:
                            comp_result = pipeline.clone_and_compile(
                                repo_url,
                                specific_branch=best_branch.name,
                                skip_modrinth=True,
                            )
                            if comp_result.success:
                                console.print(
                                    f"\n[green]Compiled {comp_result.mod_name} "
                                    f"v{comp_result.mod_version} to "
                                    f"{output_dir}/[/green]"
                                )
                            else:
                                console.print(
                                    f"\n[red]Compilation failed:[/red] "
                                    f"{comp_result.error}"
                                )
                        finally:
                            import shutil

                            if pipeline.temp_dir:
                                shutil.rmtree(pipeline.temp_dir, ignore_errors=True)
                        return

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
            "\n[dim]Tip: Run 'modforge token --set TOKEN' to enable "
            "GitHub fork search.[/dim]"
        )


@app.command()
def get(
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
    import shutil
    import tempfile

    from modforge.github import GitHubClient
    from modforge.modrinth import ModrinthClient
    from modforge.validation import BranchValidator

    setup_logging()

    # Resolve token
    modforge_cfg = ModForgeConfig()
    github_token = resolve_github_token(github_token, modforge_cfg)

    # Validate loader
    if loader.lower() not in ALL_LOADERS:
        console.print(
            f"[red]Error:[/red] Invalid loader '{loader}'. "
            f"Must be one of: {', '.join(ALL_LOADERS)}"
        )
        raise typer.Exit(1)

    console.print(
        Panel(
            f"[bold cyan]{BANNER}[/bold cyan]\n\n"
            f"  [bold]v{MODFORGE_VERSION}[/bold] - {TAGLINE}\n\n"
            f"  [bold]Get:[/bold] {query}\n"
            f"  MC {mc_version} | {loader.capitalize()}"
            + (f" {loader_version}" if loader_version else ""),
            title="ModForge Get",
            border_style="green",
        )
    )

    # Step 1: Check Modrinth
    config = ModCompilerConfig(
        mc_version=mc_version,
        loader=loader.lower(),
        loader_version=loader_version or "0",
        github_token=github_token,
        output_dir=output_dir,
        instance_path=instance,
    )

    modrinth = ModrinthClient(config)
    result = modrinth.check_modrinth(query)

    if result:
        jar = modrinth.download_modrinth_mod(result["slug"], mc_version, loader.lower())
        if jar:
            modrinth.download_modrinth_deps(result)
            console.print(
                f"\n[green]Done! {result['title']} "
                f"v{result['version_number']} downloaded to "
                f"{output_dir}/[/green]"
            )
            return
        else:
            console.print(
                "[yellow]Modrinth download failed, trying GitHub forks...[/yellow]"
            )

    # Step 2: Search GitHub forks (prompt for token if missing)
    if not github_token:
        github_token = resolve_github_token(None, modforge_cfg, prompt_if_missing=True)
        if github_token:
            config = ModCompilerConfig(
                mc_version=mc_version,
                loader=loader.lower(),
                loader_version=loader_version or "0",
                github_token=github_token,
                output_dir=output_dir,
                instance_path=instance,
            )
    if not github_token:
        console.print(
            "\n[red]Not found on Modrinth.[/red] "
            "Set a GitHub token to search forks:\n"
            "  [bold]modforge token --set ghp_YOUR_TOKEN[/bold]"
        )
        raise typer.Exit(1)

    console.print("\n[bold]Searching GitHub forks...[/bold]")
    github = GitHubClient(config)
    forks = github.search_compatible_repos(query, query, False)

    if not forks:
        console.print(
            f"[red]Not found anywhere.[/red] No Modrinth results and "
            f"no GitHub forks for '{query}'."
        )
        raise typer.Exit(1)

    # Pre-filter forks
    validator = BranchValidator(github, config)
    validated_forks = []

    for fork in forks[:10]:
        fork_info = fork["fork"]
        fork_owner = fork_info["owner"]
        fork_repo = fork_info["repo"]

        branches = github.get_branches(fork_owner, fork_repo)
        if not branches:
            continue

        compatible = validator.pre_validate_branches(fork_owner, fork_repo, branches)
        if compatible:
            best = max(compatible, key=lambda b: validator.score_branch(b))
            fork["_best_branch"] = best
            validated_forks.append(fork)

    if not validated_forks:
        console.print(
            f"[red]No compatible forks found[/red] for MC {mc_version} + {loader}."
        )
        raise typer.Exit(1)

    best_fork = validated_forks[0]
    best_branch = best_fork["_best_branch"]
    fork_name = best_fork["fork"]["full_name"]

    console.print(
        f"\n  Best fork: [cyan]{fork_name}[/cyan] "
        f"branch [blue]{best_branch.name}[/blue] "
        f"(MC {best_branch.minecraft_version or '?'})"
    )

    # Step 3: Compile
    if not loader_version:
        console.print(
            "\n[red]Fork found but [bold]-lv LOADER_VERSION[/bold] "
            "is required to compile.[/red]"
        )
        raise typer.Exit(1)

    # Rebuild config with real loader_version for compilation
    full_config = ModCompilerConfig(
        mc_version=mc_version,
        loader=loader.lower(),
        loader_version=loader_version,
        github_token=github_token,
        output_dir=output_dir,
        instance_path=instance,
        docker_test=docker_test,
    )

    repo_url = f"https://github.com/{fork_name}"
    pipeline = Pipeline(full_config)
    pipeline.temp_dir = tempfile.mkdtemp(prefix="mod_compiler_")

    try:
        comp_result = pipeline.clone_and_compile(
            repo_url,
            specific_branch=best_branch.name,
            skip_modrinth=True,
        )

        if comp_result.success:
            if docker_test:
                pipeline.results = [comp_result]
                pipeline.docker.test_mods_in_docker(pipeline.results)
                comp_result = pipeline.results[0]

            console.print(
                f"\n[green]Done! {comp_result.mod_name} "
                f"v{comp_result.mod_version} compiled to "
                f"{output_dir}/[/green]"
            )
        else:
            console.print(f"\n[red]Compilation failed:[/red] {comp_result.error}")
            raise typer.Exit(1)
    finally:
        if pipeline.temp_dir:
            shutil.rmtree(pipeline.temp_dir, ignore_errors=True)


@app.command()
def token(
    set_token: Optional[str] = typer.Option(
        None,
        "--set",
        help="Save a GitHub Personal Access Token.",
    ),
    clear: bool = typer.Option(
        False,
        "--clear",
        help="Remove saved token.",
    ),
    show: bool = typer.Option(
        False,
        "--show",
        help="Show the full saved token (unmasked).",
    ),
):
    """Manage saved GitHub Personal Access Token."""
    cfg = ModForgeConfig()

    if set_token:
        cfg.github_token = set_token
        console.print("[green]GitHub token saved.[/green]")
        return

    if clear:
        cfg.github_token = None
        console.print("[green]GitHub token removed.[/green]")
        return

    saved = cfg.github_token
    if not saved:
        console.print(
            "No GitHub token saved.\n\n"
            "  Set one with: [bold]modforge token --set ghp_YOUR_TOKEN[/bold]\n"
            "  Or pass it:   [bold]modforge get ... -t ghp_YOUR_TOKEN[/bold] "
            "(auto-saves)"
        )
        return

    if show:
        console.print(f"GitHub token: [bold]{saved}[/bold]")
    else:
        masked = saved[:4] + "****" + saved[-4:] if len(saved) > 8 else "****"
        console.print(f"GitHub token: [bold]{masked}[/bold]")
        console.print("[dim]  Use --show to reveal full token.[/dim]")


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
        saved_token = cfg.github_token
        if saved_token:
            masked = (
                saved_token[:4] + "****" + saved_token[-4:]
                if len(saved_token) > 8
                else "****"
            )
            table.add_row("GitHub token", masked)
        else:
            table.add_row("GitHub token", "[dim]not set[/dim]")

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
                        has_run = (version_dir / "run.sh").exists() or (
                            version_dir / "run.bat"
                        ).exists()
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
            console.print(f"\nDocker test cache: [green]{len(data)} entries[/green]")
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


@app.command()
def recommend(
    mods_dir: Optional[str] = typer.Option(
        None,
        "--mods-dir",
        "-d",
        help="Path to Minecraft mods folder. Auto-detects if not set.",
    ),
    mc_version: Optional[str] = typer.Option(
        None,
        "--mc-version",
        "-m",
        help="Filter to MC version family (e.g., 1.21).",
    ),
    loader: Optional[str] = typer.Option(
        None,
        "--loader",
        "-l",
        help="Filter to a specific loader.",
    ),
    top: int = typer.Option(
        5,
        "--top",
        "-n",
        help="Number of top recommendations to show.",
    ),
):
    """Scan installed mods and recommend the best MC version + loader."""
    from rich.progress import Progress, SpinnerColumn, TextColumn

    from modforge.recommend import RecommendationEngine
    from modforge.scanner import detect_mods_folder, scan_mods_folder

    # Resolve mods directory
    if mods_dir:
        mods_path = Path(mods_dir)
    else:
        mods_path = detect_mods_folder()
        if not mods_path:
            console.print(
                "[red]Error:[/red] Could not auto-detect mods folder. "
                "Use --mods-dir to specify the path."
            )
            raise typer.Exit(1)

    if not mods_path.exists() or not mods_path.is_dir():
        console.print(f"[red]Error:[/red] Mods directory not found: {mods_path}")
        raise typer.Exit(1)

    # Scan mods
    console.print(f"\n[bold]Scanning:[/bold] {mods_path}")
    try:
        all_mods = scan_mods_folder(mods_path)
    except FileNotFoundError as e:
        console.print(f"[red]Error:[/red] {e}")
        raise typer.Exit(1)

    if not all_mods:
        console.print("[yellow]No mod JARs found in the directory.[/yellow]")
        raise typer.Exit(0)

    user_mods = [m for m in all_mods if not m.is_library]
    lib_count = len(all_mods) - len(user_mods)

    console.print(
        Panel(
            f"[bold cyan]{BANNER}[/bold cyan]\n\n"
            f"  [bold]v{MODFORGE_VERSION}[/bold] - {TAGLINE}\n\n"
            f"  Scanned: {mods_path}\n"
            f"  Found: {len(user_mods)} mods"
            + (f" ({lib_count} libraries filtered)" if lib_count else ""),
            title="ModForge Recommend",
            border_style="cyan",
        )
    )

    if not user_mods:
        console.print("[yellow]No user mods found (only libraries).[/yellow]")
        raise typer.Exit(0)

    # Show scanned mods
    scan_table = Table(title="Scanned Mods")
    scan_table.add_column("#", style="dim", justify="right")
    scan_table.add_column("Mod", style="cyan")
    scan_table.add_column("Version", style="green")
    scan_table.add_column("Loader", style="yellow")
    scan_table.add_column("MC Range", style="blue")
    for i, mod in enumerate(user_mods, 1):
        scan_table.add_row(
            str(i),
            mod.mod_name,
            mod.mod_version,
            mod.declared_loader or "?",
            mod.declared_mc_range or "?",
        )
    console.print(scan_table)

    # Determine loaders to check
    loaders_to_check = None
    if loader:
        if loader.lower() not in ALL_LOADERS:
            console.print(
                f"[red]Error:[/red] Invalid loader '{loader}'. "
                f"Must be one of: {', '.join(ALL_LOADERS)}"
            )
            raise typer.Exit(1)
        loaders_to_check = [loader.lower()]

    # Run recommendation engine with progress
    engine = RecommendationEngine(
        scanned_mods=all_mods,
        loaders=loaders_to_check,
    )

    with Progress(
        SpinnerColumn(),
        TextColumn("[bold blue]{task.description}"),
        TextColumn("[dim]{task.fields[detail]}"),
        console=console,
    ) as progress:
        task = progress.add_task(
            "Identifying mods on Modrinth...", total=None, detail=""
        )
        hash_matches = engine.identify_mods_by_hash()
        matched_count = len(hash_matches)
        progress.update(
            task,
            description="Identifying mods on Modrinth...",
            detail=f"{matched_count} matched by hash",
        )

        def resolve_progress(current: int, total: int, name: str) -> None:
            progress.update(
                task,
                description=f"Resolving mods ({current}/{total})...",
                detail=name,
            )

        engine._progress = resolve_progress
        engine.resolve_modrinth_slugs(hash_matches)

        found_count = sum(
            1 for a in engine.availability.values() if a.found_on_modrinth
        )

        def version_progress(current: int, total: int, name: str) -> None:
            progress.update(
                task,
                description=f"Fetching versions ({current}/{total})...",
                detail=name,
            )

        engine._progress = version_progress
        engine.fetch_all_versions()

        progress.update(task, description="Building recommendations...", detail="")
        recommendations = engine.build_recommendations(mc_version_filter=mc_version)

    # Modrinth identification summary
    unknown_count = len(user_mods) - found_count
    console.print(
        f"\n  Identified on Modrinth: [green]{found_count}[/green]/{len(user_mods)}"
    )
    if unknown_count > 0:
        unknown_ids = [
            a.mod_name for a in engine.availability.values() if not a.found_on_modrinth
        ]
        console.print(f"  Not found: [yellow]{', '.join(unknown_ids)}[/yellow]")

    if not recommendations:
        console.print(
            "\n[yellow]No recommendations available. "
            "Mods may not be on Modrinth.[/yellow]"
        )
        raise typer.Exit(0)

    # Top recommendations table
    show_count = min(top, len(recommendations))
    rec_table = Table(title=f"\nTop {show_count} Recommendations")
    rec_table.add_column("#", style="dim", justify="right")
    rec_table.add_column("MC Version", style="cyan")
    rec_table.add_column("Loader", style="yellow")
    rec_table.add_column("Available", style="green", justify="right")
    rec_table.add_column("Missing", style="red", justify="right")
    rec_table.add_column("Unknown", style="dim", justify="right")
    rec_table.add_column("Coverage", style="bold green", justify="right")

    for i, rec in enumerate(recommendations[:show_count], 1):
        total_mods = len(user_mods)
        rec_table.add_row(
            str(i),
            rec.mc_version,
            rec.loader.capitalize(),
            f"{len(rec.available_mods)}/{total_mods}",
            str(len(rec.missing_mods)),
            str(len(rec.unknown_mods)),
            f"{rec.coverage_pct}%",
        )

    console.print(rec_table)

    # Detail for best recommendation: missing mods
    best = recommendations[0]
    if best.missing_mods:
        detail_table = Table(
            title=f"\nMissing for {best.mc_version} + {best.loader.capitalize()}"
        )
        detail_table.add_column("Mod", style="cyan")
        detail_table.add_column("Status", style="yellow")

        for mod_id in best.missing_mods:
            avail = engine.availability.get(mod_id)
            if not avail:
                detail_table.add_row(mod_id, "Not on Modrinth")
                continue

            # Find what combos this mod IS available for
            alt_loaders = set()
            alt_versions = set()
            for mc, ldr in avail.available_combos:
                if mc == best.mc_version and ldr != best.loader:
                    alt_loaders.add(ldr)
                if ldr == best.loader and mc != best.mc_version:
                    alt_versions.add(mc)

            if alt_loaders:
                detail_table.add_row(
                    avail.mod_name,
                    f"Available on {', '.join(l.capitalize() for l in alt_loaders)} "
                    f"{best.mc_version}",
                )
            elif alt_versions:
                latest = sorted(
                    alt_versions,
                    key=lambda v: [int(x) for x in v.split(".") if x.isdigit()],
                    reverse=True,
                )[0]
                detail_table.add_row(
                    avail.mod_name,
                    f"Available for {best.loader.capitalize()} {latest}",
                )
            else:
                detail_table.add_row(avail.mod_name, "No compatible version found")

        console.print(detail_table)

    if best.unknown_mods:
        console.print(
            f"\n  [dim]Unknown mods ({len(best.unknown_mods)}): "
            f"{', '.join(best.unknown_mods)}[/dim]"
        )

    console.print(
        "\n[dim]  Tip: Use 'modforge compile' to search forks and compile missing mods.\n"
        "       Use '--docker-test' to confirm compatibility.[/dim]\n"
    )


if __name__ == "__main__":
    app()
