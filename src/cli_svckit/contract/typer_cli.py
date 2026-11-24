"""Typer adapter: a ready-made ``svc`` command group (needs the optional ``typer`` extra)."""

from __future__ import annotations

from typing import Any

from cli_svckit.contract.server import implemented_methods, run


def make_svc_app(provider: Any) -> Any:
    """Return a ``typer.Typer`` with one command per method the provider implements.

    Each command takes ``--json`` and ``--request-file`` and exits with the contract exit code.
    """
    import typer

    app = typer.Typer(help="svckit contract commands", add_completion=False, no_args_is_help=True)

    def make(method: str) -> None:
        def command(
            json_out: bool = typer.Option(False, "--json", help="print one JSON line"),
            request_file: str | None = typer.Option(None, "--request-file", help="read the request from this file"),
        ) -> None:
            argv = [method]
            if json_out:
                argv.append("--json")
            if request_file:
                argv += ["--request-file", request_file]
            rc = run(provider, argv)
            raise typer.Exit(rc)

        app.command(name=method, help=f"svc {method}")(command)

    for method in implemented_methods(provider):
        make(method)
    return app
