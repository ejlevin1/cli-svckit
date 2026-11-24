"""Doctor: run independent diagnostic checks concurrently, each with its own deadline."""

from __future__ import annotations

import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from cli_svckit.contract.envelope import EXIT_FAIL, EXIT_OK, EXIT_WARN


@dataclass
class DoctorItem:
    """What one check found: ``ok``, ``warn``, ``fail`` or ``skip``, a detail line and an optional fix."""

    status: str
    detail: str
    fix: dict[str, Any] | None = None


@dataclass
class Check:
    """A named diagnostic: ``fn`` returns a :class:`DoctorItem` and must finish within ``deadline_s``."""

    id: str
    title: str
    fn: Callable[[], DoctorItem]
    area: str = "general"
    deadline_s: float = 10.0


@dataclass
class CheckOutcome:
    """A check together with its result."""

    check: Check
    item: DoctorItem

    def to_dict(self) -> dict[str, Any]:
        """Return the wire form matching ``doctor.json``."""
        d: dict[str, Any] = {
            "id": self.check.id,
            "title": self.check.title,
            "area": self.check.area,
            "status": self.item.status,
            "detail": self.item.detail,
        }
        if self.item.fix is not None:
            d["fix"] = self.item.fix
        return d


@dataclass
class DoctorReport:
    """All check outcomes in the order the checks were given."""

    outcomes: list[CheckOutcome] = field(default_factory=list)

    @property
    def status(self) -> str:
        """``fail`` if any check failed, else ``warn`` if any warned, else ``ok`` (skips do not count)."""
        statuses = {o.item.status for o in self.outcomes}
        if statuses - {"ok", "warn", "skip"}:  # "fail" and anything unknown: fail closed
            return "fail"
        return "warn" if "warn" in statuses else "ok"

    def to_dict(self) -> dict[str, Any]:
        """Return the wire form matching ``doctor.json``."""
        return {"status": self.status, "checks": [o.to_dict() for o in self.outcomes]}

    def exit_code(self) -> int:
        """Exit code for the aggregate status (ok 0, warn 4, fail 1)."""
        return {"ok": EXIT_OK, "warn": EXIT_WARN}.get(self.status, EXIT_FAIL)


def _run_one(check: Check, box: list[DoctorItem], done: threading.Event) -> None:
    try:
        item = check.fn()
        if not isinstance(item, DoctorItem):
            item = DoctorItem("fail", f"check returned {type(item).__name__}, expected DoctorItem")
        if item.status not in ("ok", "warn", "fail", "skip"):
            item = DoctorItem("fail", f"invalid status {item.status!r}")
        box.append(item)
    except BaseException as exc:
        box.append(DoctorItem("fail", f"{type(exc).__name__}: {exc}"))
    finally:
        done.set()


def run_checks(
    checks: list[Check],
    *,
    max_workers: int = 8,
    clock: Callable[[], float] = time.monotonic,
    sleep: Callable[[float], None] = time.sleep,
) -> DoctorReport:
    """Run ``checks`` with at most ``max_workers`` in flight; a check that overruns its deadline fails.

    Each check runs in a daemon thread. An overrunning thread is abandoned (it cannot be killed) and its slot is
    freed. Exceptions become ``fail`` items with the exception type and message, never a traceback.
    """
    checks = list(checks)
    results: list[DoctorItem | None] = [None] * len(checks)
    pending = list(range(len(checks)))
    running: dict[int, tuple[float, threading.Event, list[DoctorItem]]] = {}
    while pending or running:
        while pending and len(running) < max(1, max_workers):
            i = pending.pop(0)
            done = threading.Event()
            box: list[DoctorItem] = []
            threading.Thread(target=_run_one, args=(checks[i], box, done), daemon=True).start()
            running[i] = (clock() + checks[i].deadline_s, done, box)
        sleep(0.005)
        now = clock()
        for i, (deadline, done, box) in list(running.items()):
            if done.is_set():
                results[i] = box[0]
            elif now >= deadline:
                results[i] = DoctorItem("fail", f"timed out after {checks[i].deadline_s:g}s")
            else:
                continue
            del running[i]
    return DoctorReport([CheckOutcome(c, r) for c, r in zip(checks, results, strict=True) if r is not None])
