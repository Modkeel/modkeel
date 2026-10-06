"""`modkeel status`: version, saved config, cached loader installs and known loader versions."""

import json

from rich.panel import Panel
from rich.table import Table

from modkeel.commands._shared import console
from modkeel.config import ModkeelConfig
from modkeel.constants import MODKEEL_HOME, MODKEEL_VERSION
from modkeel.loaders import KNOWN_LOADER_VERSIONS, get_profile
from modkeel.models import DockerTestCache


def status_command():
    """Show Modkeel status: version, config, and Docker cache info."""

    console.print(
        Panel(
            f"[bold]Modkeel v{MODKEEL_VERSION}[/bold]",
            title="Status",
            border_style="green",
        )
    )

    # Config info
    config_dir = MODKEEL_HOME
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
        cfg = ModkeelConfig()
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
        loader_table.add_column("Loader Version", style="magenta")
        loader_table.add_column("Status", style="yellow")

        def is_installed(d) -> bool:
            return (d / "run.sh").exists() or (d / "run.bat").exists()

        # Layout: loaders/<loader>/<mc>/<loader_version>/. Older releases installed
        # straight into loaders/<loader>/<mc>/ (no loader version), still listed.
        for loader_dir in sorted(p for p in cache_dir.iterdir() if p.is_dir()):
            for mc_dir in sorted(p for p in loader_dir.iterdir() if p.is_dir()):
                installs = [(mc_dir, "-")] if is_installed(mc_dir) else [
                    (d, d.name) for d in sorted(mc_dir.iterdir()) if d.is_dir()
                ]
                for install_dir, loader_version in installs or [(mc_dir, "-")]:
                    status_str = (
                        "[green]installed[/green]"
                        if is_installed(install_dir)
                        else "[yellow]partial[/yellow]"
                    )
                    loader_table.add_row(
                        loader_dir.name, mc_dir.name, loader_version, status_str,
                    )
        console.print(loader_table)
    else:
        console.print("[dim]No cached loaders found.[/dim]")

    # Docker test cache
    cache_file = config_dir / DockerTestCache.CACHE_FILE.name
    if cache_file.exists():
        try:
            data = json.loads(cache_file.read_text())
            console.print(f"\nDocker test cache: [green]{len(data)} entries[/green]")
        except Exception:
            pass

    # Known loader versions (all loaders that have entries)
    for loader_name, versions in sorted(KNOWN_LOADER_VERSIONS.items()):
        if not versions:
            continue
        display_name = get_profile(loader_name)["display_name"]
        ver_table = Table(title=f"Known {display_name} Versions")
        ver_table.add_column("MC Version", style="cyan")
        ver_table.add_column(f"{display_name} Version", style="green")
        for mc, lv in sorted(versions.items(), reverse=True):
            ver_table.add_row(mc, lv)
        console.print(ver_table)
