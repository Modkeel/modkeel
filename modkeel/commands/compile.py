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
    console,
    offer_target,
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
        fallback_built = _pack_fallback(repo_urls, pipeline, config, repos_file, fallback)

    # Exit code 1 only when every repository failed (scripts can tell "nothing built").
    if pipeline.results and not any(r.success for r in pipeline.results) \
            and not fallback_built:
        raise typer.Exit(1)


def _pack_fallback(repo_urls, pipeline: Pipeline, config: ModCompilerConfig,
                   repos_file: Path, fallback: Optional[str]) -> bool:
    """The target layer for compile: propose a version for the whole list, decide, re-run.

    The pack's mods are the repos' Modrinth projects (repos without one count as missing
    everywhere). True when the fallback run built at least one mod.
    """
    from modkeel.github import parse_repo_url
    from modkeel.modrinth import ModrinthClient
    from modkeel.sources import identify_repo
    from modkeel.mappings import release_versions
    from modkeel.target import (
        carry_over,
        default_mode,
        fallback_output,
        older_build_probe,
        propose_targets,
    )

    modrinth = ModrinthClient(config)
    mods = []          # (title, Modrinth project id), parallel to repo_urls
    for url in repo_urls:
        try:
            owner, repo, _ = parse_repo_url(url)
        except ValueError:
            mods.append((url, None))
            continue
        ref = identify_repo(owner, repo, modrinth)
        mods.append((repo, ref.project.get("project_id") if ref.project else None))
    resolved = sum(1 for r in pipeline.results if r.success)
    console.print("\n[dim]Looking for the nearest Minecraft version where more of these "
                  "mods run...[/dim]")
    options = propose_targets(mods, config.loader, config.mc_version, modrinth, resolved,
                              limit=1, probe=older_build_probe(config.loader, modrinth),
                              releases=release_versions())
    if not options:
        return False
    option = options[0]
    hint = f"modkeel compile {repos_file} -m {option.mc_version} -l {config.loader}"
    mode = (fallback or default_mode(console.is_terminal)).lower()
    if not offer_target(option, mode, console.is_terminal, hint):
        return False

    # Same run on the new version: own output folder, never the instance, no -lv (it named
    # a loader build for the requested version).
    out = fallback_output(config.output_dir, option.mc_version)
    retry = ModCompilerConfig(
        mc_version=option.mc_version, loader=config.loader, loader_version="0",
        instance_path=None, github_token=config.github_token, strict_version=False,
        output_dir=str(out), cross_loader=config.cross_loader, docker_test=config.docker_test,
        docker_timeout=config.docker_timeout, prebuild_gate=config.prebuild_gate,
        use_prebuilt=config.use_prebuilt, symbol_check=config.symbol_check)
    # What the first run resolved is reused when it also passes there, except for mods
    # with an official build on the new version (that build wins): no repo is built twice
    # for nothing.
    official_there = {url for url, (title, _) in zip(repo_urls, mods)
                      if title in option.covered}
    carried = carry_over(pipeline.results, retry, config.mc_version, official_there)
    second = Pipeline(retry)
    second.process_repos(repo_urls, resolved=carried)
    print(second.generate_report())
    built = sum(1 for r in second.results if r.success)
    reused = f" ({len(carried)} reused from the MC {config.mc_version} run)" if carried else ""
    console.print(f"\n[green]MC {option.mc_version}: {built} of {len(repo_urls)} built "
                  f"to {out}/{reused}[/green]")
    if config.instance_path:
        console.print(f"[yellow]Not installed into {config.instance_path}: these are for MC "
                      f"{option.mc_version}, not {config.mc_version}.[/yellow]")
    return built > 0
