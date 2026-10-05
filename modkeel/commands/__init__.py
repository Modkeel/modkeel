"""One module per `modkeel` subcommand; modkeel.cli registers them on the Typer app.

Shared pieces (the Rich console, banner, token resolution, loader validation, fork
pre-filtering, a Pipeline with a throwaway work dir) live in modkeel.commands._shared.
"""
