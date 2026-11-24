"""argparse adapter: add ``svc <method>`` to a tool's own argparse CLI."""

from __future__ import annotations

import argparse
import sys
from typing import Any

from cli_svckit.contract.envelope import METHODS
from cli_svckit.contract.server import run


def add_svc_subparser(subparsers: Any, provider: Any, name: str = "svc") -> argparse.ArgumentParser:
    """Add a ``svc <method> [--json] [--request-file PATH]`` command to ``subparsers``.

    The parsed namespace gets a ``func`` that runs the call and returns the exit code.
    """
    svc = subparsers.add_parser(name, help="svckit contract commands")
    svc.add_argument("method", choices=METHODS)
    svc.add_argument("--json", action="store_true", dest="as_json", help="print one JSON line")
    svc.add_argument("--request-file", default=None, help="read the request from this file")

    def func(ns: argparse.Namespace) -> int:
        argv = [ns.method]
        if ns.as_json:
            argv.append("--json")
        if ns.request_file:
            argv += ["--request-file", ns.request_file]
        return run(provider, argv)

    svc.set_defaults(func=func)
    return svc


def main(provider: Any, argv: list[str] | None = None) -> int:
    """Run a standalone ``svc``-style CLI (``<method> [--json] ...``) for ``provider`` and return the exit code."""
    return run(provider, sys.argv[1:] if argv is None else argv)
