"""Find participants: from a ``[participants.<name>]`` registry table or from Python entry points."""

from __future__ import annotations

import os
from importlib import metadata
from typing import Any

from cli_svckit.contract.client import Participant
from cli_svckit.contract.envelope import SvcError


def _bad(name: str, msg: str) -> SvcError:
    return SvcError("invalid", f"participant {name!r}: {msg}", data={"participant": name})


def _from_entry(name: str, entry: Any) -> Participant | None:
    """Build one participant from a registry entry; None when disabled."""
    if not isinstance(entry, dict):
        raise _bad(name, "entry must be a table")
    enabled = entry.get("enabled", True)
    if not isinstance(enabled, bool):
        raise _bad(name, "enabled must be a boolean")
    if not enabled:
        return None
    cmd = entry.get("cmd")
    if not isinstance(cmd, list) or not cmd or not all(isinstance(c, str) and c for c in cmd):
        raise _bad(name, "cmd must be a non-empty list of strings")
    kwargs: dict[str, Any] = {}
    if "timeout_seconds" in entry:
        t = entry["timeout_seconds"]
        if isinstance(t, bool) or not isinstance(t, int | float) or t <= 0:
            raise _bad(name, "timeout_seconds must be a positive number")
        kwargs["timeout_s"] = float(t)
    if "fail_policy" in entry:
        if entry["fail_policy"] not in ("open", "closed"):
            raise _bad(name, "fail_policy must be 'open' or 'closed'")
        kwargs["fail_policy"] = entry["fail_policy"]
    if "wait_for" in entry:
        if not isinstance(entry["wait_for"], str):
            raise _bad(name, "wait_for must be a string")
        kwargs["wait_for"] = entry["wait_for"]
    services = entry.get("services", [])
    if not isinstance(services, list) or not all(isinstance(s, str) for s in services):
        raise _bad(name, "services must be a list of strings")
    p = Participant(name, [os.path.expanduser(c) for c in cmd], **kwargs)
    p.services = list(services)
    return p


def participants_from_registry(table: dict[str, Any]) -> list[Participant]:
    """Build participants from a mapping of name -> entry; disabled entries are skipped, bad ones raise."""
    if not isinstance(table, dict):
        raise SvcError("invalid", "participants table must be a mapping")
    out: list[Participant] = []
    for name, entry in table.items():
        p = _from_entry(str(name), entry)
        if p is not None:
            out.append(p)
    return out


def participants_from_entry_points(group: str = "cli_svckit.participants") -> list[Participant]:
    """Load participants from entry points; each yields a registry-style dict (or a callable returning one)."""
    out: list[Participant] = []
    for ep in metadata.entry_points(group=group):
        try:
            value = ep.load()
            if callable(value):
                value = value()
        except Exception as exc:
            raise SvcError("invalid", f"entry point {ep.name!r} failed to load", data={"participant": ep.name}) from exc
        p = _from_entry(ep.name, value)
        if p is not None:
            out.append(p)
    return out
