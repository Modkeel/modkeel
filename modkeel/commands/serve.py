"""`modkeel serve --stdio`: run the engine for another process over JSON lines.

The desktop app (and any front end) starts this and speaks modkeel.core.wire's protocol on
the process's stdin/stdout: requests in; events, questions and results out. Stdout carries
only protocol lines; anything else that writes to stdout while serving (a stray print) goes
to stderr, which a front end can show as a log.
"""

import sys

import typer

from modkeel.commands._shared import console


def serve_command(
    stdio: bool = typer.Option(
        False, "--stdio",
        help="Speak the JSON-lines protocol on stdin/stdout (the only transport for now).",
    ),
):
    """Serve the engine to another program (the Modkeel app) over JSON lines."""
    from modkeel.core.wire import Server

    if not stdio:
        console.print("[red]Error:[/red] choose a transport: --stdio")
        raise typer.Exit(2)
    # Protocol lines are ASCII (wire.encode); the input is read as UTF-8 whatever the
    # platform's default (cp1252 on Windows would garble a mod name with accents), and the
    # log never fails on a character the console cannot show.
    for stream, options in ((sys.stdin, {"encoding": "utf-8"}),
                            (sys.stderr, {"errors": "backslashreplace"})):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(**options)
    protocol_out = sys.stdout
    sys.stdout = sys.stderr
    try:
        Server(sys.stdin, protocol_out).serve()
    finally:
        sys.stdout = protocol_out
