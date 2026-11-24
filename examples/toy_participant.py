"""A complete stdlib participant for cli-svckit, used in docs and end-to-end tests.

Two modes::

    python toy_participant.py [--state DIR] svc <method> --json     # contract entry point
    python toy_participant.py daemon --lease-dir DIR --state DIR    # watches a lease directory

``svc`` serves describe, health, prepare and complete. ``prepare`` answers ``busy`` while a file named ``busy``
exists in the state directory. ``daemon`` appends ``{"event", "op_id", "t"}`` lines to ``<state>/events.jsonl``
whenever an operation it is interested in starts (``prepare``) or ends (``complete``). Set
``SVCKIT_WATCH_BACKEND`` to ``auto``, ``kqueue``, ``inotify`` or ``poll`` to pick the watcher backend.
"""

from __future__ import annotations

import argparse
import json
import os
import signal
import sys
import tempfile
import threading
import time
from pathlib import Path
from typing import Any

from cli_svckit.contract.server import run
from cli_svckit.coord.lease import Lease, LeaseDir
from cli_svckit.coord.participant import Interest, Quiescer
from cli_svckit.coord.watch import LeaseWatcher

KINDS = ["service.*", "x-example.*"]
SERVICES = ["example-svc"]


def default_state_dir() -> Path:
    """State directory when ``--state`` is not given: ``$EXAMPLE_TOY_STATE`` or a folder in the temp dir."""
    return Path(os.environ.get("EXAMPLE_TOY_STATE") or Path(tempfile.gettempdir()) / "example-toy-participant")


class ToyProvider:
    """The contract methods of the toy tool (see :mod:`cli_svckit.contract.server` for the provider shape)."""

    def __init__(self, state: Path) -> None:
        self.state = state

    def describe(self, params: dict[str, Any]) -> dict[str, Any]:
        """Say who we are and which operations we want to be told about."""
        return {
            "tool": {"name": "example-toy", "version": "0.1.0"},
            "services": [{"name": s} for s in SERVICES],
            "coordination": {
                "kinds": KINDS,
                "services": SERVICES,
                "phases": ["prepare", "complete"],
                "prepare_timeout_s": 30,
                "complete_timeout_s": 30,
                "watches_lease": True,
            },
        }

    def health(self, params: dict[str, Any]) -> dict[str, Any]:
        """Always healthy."""
        return {"status": "pass", "live": True, "ready": True, "checks": {}}

    def prepare(self, params: dict[str, Any]) -> dict[str, Any]:
        """Answer ``busy`` (one interruptible item in flight) while ``<state>/busy`` exists, else ``ready``."""
        op = params.get("op") or {}
        op_id = str(op.get("id", ""))
        if (self.state / "busy").exists():
            item = {"id": "job-1", "kind": "example.job", "what": "example job", "interrupted": True}
            return {"state": "busy", "op_id": op_id, "quiesced": False, "eta_s": 1, "in_flight": [item]}
        return {"state": "ready", "op_id": op_id, "quiesced": True, "in_flight": []}

    def complete(self, params: dict[str, Any]) -> dict[str, Any]:
        """Resume normal work; nothing to recover."""
        return {"state": "resumed", "actions": []}


def run_daemon(lease_dir: Path, state: Path, stop: threading.Event | None = None) -> int:
    """Watch ``lease_dir`` and log prepare/complete events until ``stop`` is set (or SIGINT/SIGTERM arrives)."""
    state.mkdir(parents=True, exist_ok=True)
    events = state / "events.jsonl"
    lock = threading.Lock()

    def log(event: str, lease: Lease) -> None:
        line = json.dumps({"event": event, "op_id": lease.op.id, "t": time.time()})
        with lock, open(events, "a", encoding="utf-8") as fh:
            fh.write(line + "\n")

    ldir = LeaseDir(lease_dir)
    ldir.ensure()
    quiescer = Quiescer(
        ldir,
        Interest(kinds=KINDS, services=SERVICES),
        on_prepare=lambda lease: log("prepare", lease),
        on_complete=lambda lease: log("complete", lease),
        state_file=state / "quiescer.json",
    )
    stop = stop or threading.Event()
    if threading.current_thread() is threading.main_thread():
        for sig in (signal.SIGINT, signal.SIGTERM):
            signal.signal(sig, lambda *_: stop.set())
    quiescer.catch_up()
    watcher = LeaseWatcher(str(lease_dir), quiescer.refresh, backend=os.environ.get("SVCKIT_WATCH_BACKEND") or "auto")
    watcher.start()
    try:
        stop.wait()
    finally:
        watcher.stop()
    return 0


def main(argv: list[str] | None = None) -> int:
    """Entry point: ``svc ...`` runs the contract, ``daemon`` runs the lease watcher."""
    args = list(sys.argv[1:] if argv is None else argv)
    opts = argparse.ArgumentParser(add_help=False)
    opts.add_argument("--state", default=None)
    opts.add_argument("--lease-dir", default=None)
    ns, rest = opts.parse_known_args(args)
    state = Path(ns.state) if ns.state else default_state_dir()
    if rest[:1] == ["svc"]:
        return run(ToyProvider(state), rest[1:])
    if rest[:1] == ["daemon"]:
        if not ns.lease_dir:
            print("daemon needs --lease-dir", file=sys.stderr)
            return 2
        return run_daemon(Path(ns.lease_dir), state)
    print(
        "usage: toy_participant.py [--state DIR] svc <method> --json | daemon --lease-dir DIR --state DIR",
        file=sys.stderr,
    )
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
