"""Pieces every `modkeel` subcommand uses: console, banner, token and loader handling,
fork pre-filtering and a Pipeline with a temporary work directory."""

import shutil
import tempfile
from contextlib import contextmanager
from typing import Dict, Iterator, List, Optional

import typer
from rich.console import Console

from modkeel.config import ModkeelConfig
from modkeel.loaders import ALL_LOADERS
from modkeel.models import ModCompilerConfig
from modkeel.pipeline import Pipeline
from modkeel.utils import setup_windows_console

BANNER = r"""    __  ___          ____             __
   /  |/  /___  ____/ / /_____  ___  / /
  / /|_/ / __ \/ __  / //_/ _ \/ _ \/ /
 / /  / / /_/ / /_/ / ,< /  __/  __/ /
/_/  /_/\____/\__,_/_/|_|\___/\___/_/"""

TAGLINE = "Compile the mods Mojang left behind"

# The Windows console must be switched to UTF-8 before Rich binds to it.
setup_windows_console()
console = Console()


def resolve_github_token(
    explicit: Optional[str],
    modkeel_cfg: ModkeelConfig,
    prompt_if_missing: bool = False,
) -> Optional[str]:
    """Resolve GitHub token: CLI flag > saved config > interactive prompt.

    Args:
        explicit: Token passed via -t flag.
        modkeel_cfg: Persistent config.
        prompt_if_missing: If True and no token found, prompt the user
            interactively. Only prompts if running in a terminal.
    """
    if explicit:
        if explicit != modkeel_cfg.github_token:
            modkeel_cfg.github_token = explicit
            console.print("[dim]  GitHub token saved to ~/.modkeel/config.toml[/dim]")
        return explicit
    saved = modkeel_cfg.github_token
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
            modkeel_cfg.github_token = token
            console.print("[green]Token saved to ~/.modkeel/config.toml[/green]")
            return token
    return None


def require_valid_loader(loader: str) -> None:
    """Exit with code 1 and a message unless `loader` is a known loader (any case)."""
    if loader.lower() not in ALL_LOADERS:
        console.print(
            f"[red]Error:[/red] Invalid loader '{loader}'. "
            f"Must be one of: {', '.join(ALL_LOADERS)}"
        )
        raise typer.Exit(1)


def prefilter_forks(github, validator, forks: List[Dict], limit: int = 10) -> List[Dict]:
    """Forks (of the first `limit`) with at least one pre-validated branch.

    Each kept fork gets its best-scoring branch under "_best_branch". Only the GitHub API is
    used (gradle.properties and friends); nothing is cloned.
    """
    validated = []
    for fork in forks[:limit]:
        fork_info = fork["fork"]
        branches = github.get_branches(fork_info["owner"], fork_info["repo"])
        if not branches:
            continue
        compatible = validator.pre_validate_branches(
            fork_info["owner"], fork_info["repo"], branches
        )
        if compatible:
            fork["_best_branch"] = max(compatible, key=lambda b: validator.score_branch(b))
            validated.append(fork)
    return validated


@contextmanager
def temp_pipeline(config: ModCompilerConfig) -> Iterator[Pipeline]:
    """A Pipeline with its own temporary work dir, removed on exit (success or not).

    For one-off builds (search, get); `compile` uses Pipeline.process_repos, which manages
    its own work dir.
    """
    pipeline = Pipeline(config)
    pipeline.temp_dir = tempfile.mkdtemp(prefix="mod_compiler_")
    try:
        yield pipeline
    finally:
        if pipeline.temp_dir:
            shutil.rmtree(pipeline.temp_dir, ignore_errors=True)
