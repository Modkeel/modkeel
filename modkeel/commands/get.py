"""`modkeel get`: one mod in one step. Modrinth build if there is one, else compile the best
pre-filtered GitHub fork (needs a token and -lv)."""

from typing import Optional

import typer
from rich.markup import escape
from rich.panel import Panel

from modkeel.commands._shared import (
    BANNER,
    TAGLINE,
    console,
    prefilter_forks,
    require_valid_loader,
    resolve_github_token,
    temp_pipeline,
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
    from modkeel.github import GitHubClient
    from modkeel.modrinth import ModrinthClient
    from modkeel.validation import BranchValidator

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

    # Lookups need no loader version; "0" is a placeholder until the compile step.
    def make_config(token: Optional[str]) -> ModCompilerConfig:
        return ModCompilerConfig(
            mc_version=mc_version,
            loader=loader.lower(),
            loader_version=loader_version or "0",
            github_token=token,
            output_dir=output_dir,
            instance_path=instance,
        )

    # Step 1: Modrinth
    config = make_config(github_token)
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
        console.print(
            "[yellow]Modrinth download failed, trying GitHub forks...[/yellow]"
        )

    # Step 2: GitHub forks (prompt for a token if none was saved)
    if not github_token:
        github_token = resolve_github_token(None, modkeel_cfg, prompt_if_missing=True)
        if github_token:
            config = make_config(github_token)
    if not github_token:
        modrinth_status = (
            f"Modrinth unavailable ({escape(modrinth.last_error)})."
            if modrinth.last_error else "Not found on Modrinth."
        )
        console.print(
            f"\n[red]{modrinth_status}[/red] "
            "Set a GitHub token to search forks:\n"
            "  [bold]modkeel token --set ghp_YOUR_TOKEN[/bold]"
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

    validated_forks = prefilter_forks(github, BranchValidator(github, config), forks)
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

    # Step 3: compile (needs the real loader version)
    if not loader_version:
        console.print(
            "\n[red]Fork found but [bold]-lv LOADER_VERSION[/bold] "
            "is required to compile.[/red]"
        )
        raise typer.Exit(1)

    full_config = ModCompilerConfig(
        mc_version=mc_version,
        loader=loader.lower(),
        loader_version=loader_version,
        github_token=github_token,
        output_dir=output_dir,
        instance_path=instance,
        docker_test=docker_test,
    )

    with temp_pipeline(full_config) as pipeline:
        comp_result = pipeline.clone_and_compile(
            f"https://github.com/{fork_name}",
            specific_branch=best_branch.name,
            skip_modrinth=True,
        )

        if not comp_result.success:
            console.print(f"\n[red]Compilation failed:[/red] {comp_result.error}")
            raise typer.Exit(1)

        if docker_test:
            pipeline.results = [comp_result]
            pipeline.docker.test_mods_in_docker(pipeline.results)
            comp_result = pipeline.results[0]

    console.print(
        f"\n[green]Done! {comp_result.mod_name} "
        f"v{comp_result.mod_version} compiled to "
        f"{output_dir}/[/green]"
    )
