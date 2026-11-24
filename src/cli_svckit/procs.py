"""Process helpers: liveness, start time, and the process table (parent/group relations).

Everything here returns ``None`` or an empty list instead of raising when the platform tool (``ps``) is missing
or fails. ``psutil`` is used when it can be imported, otherwise ``ps`` output is parsed.
"""

from __future__ import annotations

import os
import subprocess
import time
from dataclasses import dataclass


@dataclass(frozen=True)
class ProcInfo:
    """One row of the process table."""

    pid: int
    ppid: int
    pgid: int
    command: str


def _ps_field(pid: int, field: str) -> str | None:
    try:
        out = subprocess.run(
            ["ps", "-o", f"{field}=", "-p", str(pid)], capture_output=True, text=True, timeout=5, check=False
        )
    except (OSError, subprocess.SubprocessError):
        return None
    text = out.stdout.strip()
    return text if out.returncode == 0 and text else None


def pid_alive(pid: int) -> bool:
    """Return True when a process with this pid exists (a zombie counts as gone when ``ps`` can tell)."""
    if not isinstance(pid, int) or isinstance(pid, bool) or pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return False
    state = _ps_field(pid, "stat")
    return not (state is not None and state.startswith("Z"))


def process_start_time(pid: int) -> float | None:
    """Return the process start time as a UNIX timestamp, or None when unknown or the process is gone."""
    try:
        import psutil  # type: ignore[import-not-found,unused-ignore]
    except ImportError:
        psutil = None
    if psutil is not None:
        try:
            return float(psutil.Process(pid).create_time())
        except Exception:
            return None
    text = _ps_field(pid, "lstart")
    if text is None:
        return None
    try:
        # lstart looks like "Mon Jan  2 15:04:05 2006" in the local time zone.
        return float(time.mktime(time.strptime(text, "%a %b %d %H:%M:%S %Y")))
    except (ValueError, OverflowError):
        return None


def parse_ps(text: str) -> list[ProcInfo]:
    """Parse ``ps -axo pid=,ppid=,pgid=,command=`` output; malformed lines are skipped."""
    rows: list[ProcInfo] = []
    for line in text.splitlines():
        parts = line.split(None, 3)
        if len(parts) < 3:
            continue
        try:
            pid, ppid, pgid = int(parts[0]), int(parts[1]), int(parts[2])
        except ValueError:
            continue
        rows.append(ProcInfo(pid, ppid, pgid, parts[3].strip() if len(parts) > 3 else ""))
    return rows


def process_table() -> list[ProcInfo]:
    """Return all visible processes, or an empty list when they cannot be listed."""
    try:
        import psutil  # type: ignore[import-not-found,unused-ignore]
    except ImportError:
        psutil = None
    if psutil is not None:
        try:
            rows = []
            for p in psutil.process_iter(["pid", "ppid", "cmdline"]):
                info = p.info
                try:
                    pgid = os.getpgid(info["pid"])
                except OSError:
                    continue
                rows.append(ProcInfo(info["pid"], info["ppid"] or 0, pgid, " ".join(info.get("cmdline") or [])))
            if rows:
                return rows
        except Exception:
            pass
    try:
        out = subprocess.run(
            ["ps", "-axo", "pid=,ppid=,pgid=,command="], capture_output=True, text=True, timeout=10, check=False
        )
    except (OSError, subprocess.SubprocessError):
        return []
    return parse_ps(out.stdout) if out.returncode == 0 else []


def descendants(pid: int, table: list[ProcInfo] | None = None) -> list[ProcInfo]:
    """Return every child, grandchild, ... of ``pid`` (not ``pid`` itself), breadth first."""
    rows = process_table() if table is None else table
    children: dict[int, list[ProcInfo]] = {}
    for row in rows:
        children.setdefault(row.ppid, []).append(row)
    found: list[ProcInfo] = []
    seen = {pid}
    queue = [pid]
    while queue:
        for child in children.get(queue.pop(0), []):
            if child.pid not in seen:
                seen.add(child.pid)
                found.append(child)
                queue.append(child.pid)
    return found


def group_members(pgid: int, table: list[ProcInfo] | None = None) -> list[ProcInfo]:
    """Return every process in the process group ``pgid``."""
    rows = process_table() if table is None else table
    return [row for row in rows if row.pgid == pgid]
