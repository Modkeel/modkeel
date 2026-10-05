"""`modkeel search`: look a mod up on Modrinth, then in pre-filtered GitHub forks.

Interactive runs offer to download the Modrinth build or compile the best fork; with
--no-prompt (or no terminal) it only reports.
"""

from typing import Optional

import typer
from rich.markup import escape
from rich.panel import Panel
from rich.table import Table

from modkeel.commands._shared import (
    console,
    prefilter_forks,
    resolve_github_token,
    temp_pipeline,
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
    from modkeel.github import GitHubClient
    from modkeel.modrinth import ModrinthClient

    modkeel_cfg = ModkeelConfig()
    github_token = resolve_github_token(github_token, modkeel_cfg)

    console.print(
        Panel(
            f"[bold]Search:[/bold] {query}\nMC {mc_version} | {loader.capitalize()}",
            title="Modkeel Search",
            border_style="cyan",
        )
    )

    # Searching needs no loader version; "0" is a placeholder until a compile is requested.
    def make_config(token: Optional[str]) -> ModCompilerConfig:
        return ModCompilerConfig(
            mc_version=mc_version,
            loader=loader.lower(),
            loader_version=loader_version or "0",
            github_token=token,
            output_dir=output_dir,
            instance_path=instance,
        )

    config = make_config(github_token)

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

    # Not found, or Modrinth could not be asked: say which, so a network problem
    # is not mistaken for "this mod has no build for this version".
    if modrinth.last_error:
        reason = escape(modrinth.last_error)
        table.add_row("Status", f"[red]Modrinth unavailable ({reason})[/red]")
    else:
        table.add_row("Status", "[yellow]Not found on Modrinth[/yellow]")
    console.print(table)

    # The fork search needs a token; ask for one now if none was saved.
    if not github_token:
        github_token = resolve_github_token(None, modkeel_cfg, prompt_if_missing=True)
        if github_token:
            config = make_config(github_token)

    if not github_token:
        console.print(
            "\n[dim]Tip: Run 'modkeel token --set TOKEN' to enable "
            "GitHub fork search.[/dim]"
        )
        return

    from modkeel.validation import BranchValidator

    console.print("\n[bold]GitHub Forks:[/bold]")
    github = GitHubClient(config)
    forks = github.search_compatible_repos(query, query, False)
    if not forks:
        console.print("[yellow]No GitHub forks found.[/yellow]")
        return

    validated_forks = prefilter_forks(github, BranchValidator(github, config), forks)

    fork_table = Table(
        title=f"Pre-filtered GitHub Forks ({len(validated_forks)} of {len(forks)} passed)"
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

    if not validated_forks:
        console.print(
            "[yellow]No forks passed pre-filtering "
            f"for MC {mc_version} + {loader}.[/yellow]"
        )
        return

    # Offer compilation if interactive and a loader version was given
    is_interactive = not no_prompt and console.is_terminal
    if is_interactive and loader_version:
        if typer.confirm("\n  Compile best fork?", default=False):
            _compile_best_fork(validated_forks[0], make_config(github_token), output_dir)
            return

    console.print(
        "\n[dim]  Pre-filtered via gradle.properties. "
        "Use 'modkeel compile' to build and "
        "'--docker-test' to confirm compatibility.[/dim]"
    )


def _compile_best_fork(best_fork: dict, config: ModCompilerConfig, output_dir: str) -> None:
    """Build the fork's pre-selected branch (Modrinth already checked) and report it."""
    repo_url = f"https://github.com/{best_fork['fork']['full_name']}"
    with temp_pipeline(config) as pipeline:
        comp_result = pipeline.clone_and_compile(
            repo_url,
            specific_branch=best_fork["_best_branch"].name,
            skip_modrinth=True,
        )
    if comp_result.success:
        console.print(
            f"\n[green]Compiled {comp_result.mod_name} "
            f"v{comp_result.mod_version} to "
            f"{output_dir}/[/green]"
        )
    else:
        console.print(f"\n[red]Compilation failed:[/red] {comp_result.error}")
