"""`modkeel recommend`: scan a mods folder and rank Minecraft version + loader combos by how
many of the installed mods Modrinth has builds for."""

from pathlib import Path
from typing import Optional

import typer
from rich.panel import Panel
from rich.table import Table

from modkeel.commands._shared import BANNER, TAGLINE, console, require_valid_loader
from modkeel.constants import MODKEEL_VERSION


def recommend_command(
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

    from modkeel.recommend import RecommendationEngine
    from modkeel.scanner import detect_mods_folder, scan_mods_folder

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
            f"  [bold]v{MODKEEL_VERSION}[/bold] - {TAGLINE}\n\n"
            f"  Scanned: {mods_path}\n"
            f"  Found: {len(user_mods)} mods"
            + (f" ({lib_count} libraries filtered)" if lib_count else ""),
            title="Modkeel Recommend",
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
        require_valid_loader(loader)
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
                    f"Available on {', '.join(ldr.capitalize() for ldr in alt_loaders)} "
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
        "\n[dim]  Tip: Use 'modkeel compile' to search forks and compile missing mods.\n"
        "       Use '--docker-test' to confirm compatibility.[/dim]\n"
    )
