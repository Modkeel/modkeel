"""`modkeel compile`: build every repository in a list file through the full pipeline.

The repositories are a pack: when some of them cannot be resolved on the requested version,
the target layer (modkeel/target.py) may propose another Minecraft version for all of them
(never with --strict), and re-run the whole list there into <output>/mc-<version>/.
"""

import logging
from pathlib import Path
from typing import Optional

import typer
from rich.panel import Panel

from modkeel.commands._shared import (
    BANNER,
    TAGLINE,
    cli_decide,
    console,
    print_target_search,
    require_valid_loader,
    resolve_github_token,
)
from modkeel.config import ModkeelConfig, prompt_sharing_preference
from modkeel.constants import MODKEEL_API_URL, MODKEEL_VERSION
from modkeel.models import ModCompilerConfig
from modkeel.pipeline import Pipeline
from modkeel.utils import setup_logging


def compile_command(
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
    fallback: Optional[str] = typer.Option(
        None,
        "--fallback",
        help="When some repos fail, try the nearest version where more of them have an "
             "official build: ask (countdown; default in a terminal), auto, never "
             "(default otherwise). Never with --strict.",
        case_sensitive=False,
    ),
):
    """Compile mods from a list of GitHub repositories."""
    from modkeel.target import FALLBACK_MODES

    setup_logging(log_file)
    require_valid_loader(loader)
    if fallback is not None and fallback.lower() not in FALLBACK_MODES:
        console.print(f"[red]Error:[/red] --fallback must be one of {', '.join(FALLBACK_MODES)}")
        raise typer.Exit(2)

    # One URL per line; blank lines and # comments are skipped.
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
            f"  [bold]v{MODKEEL_VERSION}[/bold] - {TAGLINE}\n\n"
            f"  MC {mc_version} | {loader.capitalize()} {loader_version}\n"
            f"  {len(repo_urls)} repositories loaded from {repos_file}",
            title="Modkeel",
            border_style="blue",
        )
    )

    modkeel_cfg = ModkeelConfig()
    github_token = resolve_github_token(
        github_token, modkeel_cfg, prompt_if_missing=True
    )
    if MODKEEL_API_URL and modkeel_cfg.is_first_run and not modkeel_cfg.was_prompted:
        prompt_sharing_preference(modkeel_cfg)

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

    pipeline = Pipeline(config)
    pipeline.process_repos(repo_urls)

    report = pipeline.generate_report()
    print(report)

    if output_report:
        with open(output_report, "w", encoding="utf-8") as f:
            f.write(report)
        console.print(f"\nReport saved to: {output_report}")

    # Anonymous crowdsource reports; never fails the run (submit_reports is a no-op while
    # MODKEEL_API_URL is empty).
    if not no_share:
        try:
            from modkeel.crowdsource import submit_reports
            from modkeel.github import parse_repo_url

            submit_reports(
                pipeline.results,
                modkeel_cfg,
                mc_version,
                loader.lower(),
                loader_version,
                parse_repo_url,
            )
        except Exception as e:
            logging.getLogger("modkeel").debug("Crowdsource submission error: %s", e)

    fallback_built = False
    if not strict and any(not r.success for r in pipeline.results):
        fallback_built = _pack_fallback(repo_urls, pipeline, config, repos_file, fallback,
                                        modkeel_cfg)

    # Exit code 1 only when every repository failed (scripts can tell "nothing built").
    if pipeline.results and not any(r.success for r in pipeline.results) \
            and not fallback_built:
        raise typer.Exit(1)


def _pack_fallback(repo_urls, pipeline: Pipeline, config: ModCompilerConfig,
                   repos_file: Path, fallback: Optional[str], modkeel_cfg) -> bool:
    """The target layer for compile (modkeel.core.engine.retarget_pack), shown the CLI's way.

    True when the run on the proposed version built at least one mod.
    """
    from modkeel.core.engine import retarget_pack
    from modkeel.core.events import TargetSearch
    from modkeel.core.text import print_event
    from modkeel.target import default_mode

    def view(event) -> None:
        if isinstance(event, TargetSearch):
            print_target_search(event)
        else:
            print_event(event)

    def hint(option) -> str:
        return f"modkeel compile {repos_file} -m {option.mc_version} -l {config.loader}"

    mode = (fallback or default_mode(console.is_terminal)).lower()
    run = retarget_pack(repo_urls, pipeline.results, config, events=view,
                        decide=cli_decide(mode, hint, modkeel_cfg))
    if run is None:
        return False
    print(run.report)
    out = run.config.output_dir
    reused = f" ({len(run.carried)} reused from the MC {config.mc_version} run)" \
        if run.carried else ""
    console.print(f"\n[green]MC {run.option.mc_version}: {run.built} of {len(repo_urls)} "
                  f"built to {out}/{reused}[/green]")
    if config.instance_path:
        console.print(f"[yellow]Not installed into {config.instance_path}: these are for MC "
                      f"{run.option.mc_version}, not {config.mc_version}.[/yellow]")
    return run.built > 0
