"""Controller side of lease coordination: announce an operation, wait for participants to quiesce, then resume them.

An :class:`Operation` is a context manager. Entering it writes a lease (so participant daemons see it), asks every
relevant participant to ``prepare`` until all are ready (or a cap is hit), and moves the lease to ``running``.
Leaving it tells the participants ``complete`` and tombstones the lease.
"""

from __future__ import annotations

import time
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from cli_svckit.contract.client import CallResult
from cli_svckit.contract.envelope import Error, Op, SvcError, matches, new_op_id
from cli_svckit.coord.lease import LeaseDir

__all__ = ["Operation", "OperationAborted", "PrepareReport"]


class OperationAborted(Exception):
    """Raised when participants did not become ready in time and the operation was told to abort at the cap."""

    def __init__(self, message: str, report: PrepareReport | None = None) -> None:
        super().__init__(message)
        self.report = report


@dataclass
class PrepareReport:
    """Outcome of the prepare phase: ``ready`` to go on, ``capped`` (cap reached), ``forced_now``, and who was busy."""

    ready: bool
    capped: bool = False
    forced_now: bool = False
    waited_s: float = 0.0
    busy: dict[str, dict[str, Any]] = field(default_factory=dict)
    errors: dict[str, Error] = field(default_factory=dict)


class Operation:
    """One controller operation (restart, update, ...) coordinated with participants through a lease directory.

    ``max_wait_s`` is how long to wait for participants, ``margin_s`` extra time before the lease counts as stale,
    ``poll_s`` the pause between prepare polls. ``on_cap`` is ``"proceed"`` (go on anyway) or ``"abort"``. ``now``
    asks for one prepare call and no waiting. ``mirror(event, op)`` is called with ``"start"``/``"end"`` (for legacy
    drain files). ``say`` receives progress lines. ``facts`` is a dict the caller fills; it is stored on the lease.
    """

    def __init__(
        self,
        kind: str,
        targets: list[str],
        *,
        lease_dir: LeaseDir,
        participants: list[Any],
        max_wait_s: float = 900.0,
        margin_s: float = 1200.0,
        poll_s: float = 15.0,
        now: bool = False,
        reason: str = "",
        by: dict[str, Any] | None = None,
        affected_pids: list[int] | None = None,
        wait_for: str = "interrupted",
        on_cap: str = "proceed",
        op_id: str | None = None,
        describe_cache: Any = None,
        mirror: Callable[[str, Op], None] | None = None,
        say: Callable[[str], None] = print,
        clock: Callable[[], float] = time.time,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        if on_cap not in ("proceed", "abort"):
            raise SvcError("invalid", "on_cap must be 'proceed' or 'abort'")
        self.kind = kind
        self.targets = list(targets)
        self.lease_dir = lease_dir
        self.participants = list(participants)
        self.max_wait_s = max_wait_s
        self.margin_s = margin_s
        self.poll_s = poll_s
        self.now = now
        self.reason = reason
        self.by = dict(by or {})
        self.affected_pids = None if affected_pids is None else list(affected_pids)
        self.wait_for = wait_for
        self.on_cap = on_cap
        self.op_id = op_id
        self.describe_cache = describe_cache
        self.mirror = mirror
        self.say = say
        self.clock = clock
        self.sleep = sleep
        self.facts: dict[str, Any] = {}
        self.op: Op | None = None
        self.report: PrepareReport | None = None
        self._relevant: list[Any] | None = None
        self._polls = 0
        self._started = False
        self._finished = False
        self._said: set[str] = set()

    # -- relevance ---------------------------------------------------------------------------------------

    def _say_once(self, line: str) -> None:
        if line not in self._said:
            self._said.add(line)
            self.say(line)

    def relevant(self) -> list[Any]:
        """Return the participants whose ``coordination`` covers this kind and any target (computed once).

        No coordination means not relevant. A participant whose describe fails is relevant only with
        ``fail_policy == "closed"`` (a note is printed once).
        """
        if self._relevant is not None:
            return self._relevant
        found: list[Any] = []
        for p in self.participants:
            desc = p.describe(self.describe_cache)
            if desc is None:
                if p.fail_policy == "closed":
                    self._say_once(f"{p.name}: describe failed; treating as relevant (fail closed)")
                    found.append(p)
                else:
                    self._say_once(f"{p.name}: describe failed; skipping (fail open)")
                continue
            coord = desc.get("coordination") if isinstance(desc, dict) else None
            if not isinstance(coord, dict):
                continue
            kinds = [str(k) for k in coord.get("kinds") or []]
            services = [str(s) for s in coord.get("services") or []]
            if not matches(self.kind, kinds):
                continue
            if services and not any(matches(t, services) for t in self.targets):
                continue
            found.append(p)
        self._relevant = found
        return found

    # -- lifecycle ---------------------------------------------------------------------------------------

    def start(self) -> Op:
        """Build the op and write its lease (phase prepare) before any prepare call; then call ``mirror("start")``.

        When ``op_id`` names an existing active lease, that lease's op is reused instead.
        """
        if self.op is not None:
            return self.op
        existing = self.lease_dir.get(self.op_id) if self.op_id else None
        if existing is not None and existing.phase != "complete" and existing.is_active(self.clock()):
            self.op = existing.op
            self._started = True
            return self.op
        started = self.clock()
        op = Op(
            id=self.op_id or new_op_id(self.clock),
            kind=self.kind,
            targets=list(self.targets),
            reason=self.reason,
            by=dict(self.by),
            started_at=started,
            deadline=started + self.max_wait_s,
            expires_at=started + self.max_wait_s + self.margin_s,
            affected_pids=self.affected_pids,
            wait_for=self.wait_for,
            now=self.now,
        )
        self.lease_dir.ensure()
        self.lease_dir.create(op, "prepare")
        self.op = op
        self.op_id = op.id
        self._started = True
        if self.mirror is not None:
            self.mirror("start", op)
        return op

    def _poll(self, op: Op, poll: int) -> PrepareReport:
        """Call ``prepare`` once on every relevant participant and sum up who is still blocking."""
        report = PrepareReport(ready=True)
        for p in self.relevant():
            result: CallResult = p.call("prepare", {"op": op.to_dict(), "poll": poll})
            if not result.ok and result.error is not None:
                report.errors[p.name] = result.error
            if not self._participant_ready(p, op, result, report):
                report.ready = False
        return report

    def _participant_ready(self, p: Any, op: Op, result: CallResult, report: PrepareReport) -> bool:
        """Decide whether one participant's answer lets the operation go on (recording busy details)."""
        policy = getattr(p, "fail_policy", "open")
        data: dict[str, Any] | None = None
        if result.ok and result.response is not None:
            data = result.response.result or {}
        if data is None:
            error = result.error
            if error is not None and error.code == "unsupported":
                return True
            return policy != "closed"
        if "state" in data:
            ready = data.get("state") == "ready"
        elif isinstance(data.get("ready"), bool):
            ready = bool(data["ready"])
        else:
            ready = result.rc != 3
        if ready:
            return True
        report.busy[p.name] = data
        wait_all = (getattr(p, "wait_for", "interrupted") == "all") or op.wait_for == "all"
        in_flight = [i for i in data.get("in_flight") or [] if isinstance(i, dict)]
        if wait_all:
            return False
        # Busy only blocks when something in flight would be interrupted by the operation.
        return not any(i.get("interrupted") is True for i in in_flight)

    def _progress(self, report: PrepareReport) -> None:
        parts: list[str] = []
        count = 0
        for name, data in report.busy.items():
            for item in data.get("in_flight") or []:
                if isinstance(item, dict) and item.get("interrupted") is False and self.wait_for != "all":
                    continue
                count += 1
                if isinstance(item, dict):
                    parts.append(f"{name}:{item.get('what') or item.get('id') or '?'}")
        detail = ", ".join(parts)
        if report.busy:
            self.say(f"waiting for {count} in flight: {detail}" if count else f"waiting for {', '.join(report.busy)}")
        else:
            self.say("waiting for participants")

    def prepare(self) -> PrepareReport:
        """Poll relevant participants until all are ready, the cap is hit, or ``now`` forces one call.

        At the cap, ``on_cap="proceed"`` returns ready with ``capped=True``; ``"abort"`` returns ``ready=False``.
        """
        op = self.start()
        t0 = op.started_at or self.clock()
        if self.now or op.now:
            report = self._poll(op, 0)
            report.ready = True
            report.forced_now = True
            report.waited_s = self.clock() - t0
            self.report = report
            return report
        poll = 0
        while True:
            report = self._poll(op, poll)
            poll += 1
            report.waited_s = self.clock() - t0
            if report.ready:
                break
            if self.clock() >= op.deadline:
                report.capped = True
                report.ready = self.on_cap == "proceed"
                break
            self._progress(report)
            self.sleep(max(0.0, min(self.poll_s, op.deadline - self.clock())))
        self.report = report
        return report

    def tick(self) -> str:
        """One step for periodic jobs: ``"waiting"``, ``"ready"`` or ``"capped"``.

        The first tick creates the lease; later ticks (also from another process, via ``op_id``) reuse it. Each tick
        does one prepare poll. On ``ready`` the lease moves to ``running``. ``capped`` means the cap was hit: with
        ``on_cap="proceed"`` the lease moves to ``running`` (``report.ready`` True); with ``"abort"`` it stays in
        ``prepare`` and ``report.ready`` is False.
        """
        op = self.start()
        report = self._poll(op, self._polls)
        self._polls += 1
        report.waited_s = self.clock() - op.started_at
        if report.ready or op.now or self.now:
            report.forced_now = bool(self.now or op.now)
            report.ready = True
            state = "ready"
        elif self.clock() >= op.deadline:
            report.capped = True
            report.ready = self.on_cap == "proceed"
            state = "capped"
        else:
            self.report = report
            return "waiting"
        self.report = report
        if report.ready:
            self.lease_dir.set_phase(op.id, "running")
        return state

    def __enter__(self) -> Operation:
        try:
            report = self.prepare()
            if not report.ready:
                self._finish("aborted", f"participants not ready after {report.waited_s:.0f}s")
                raise OperationAborted("participants did not become ready before the cap", report)
            assert self.op is not None
            self.lease_dir.set_phase(self.op.id, "running")
        except OperationAborted:
            raise
        except BaseException as e:
            # Python never calls __exit__ when __enter__ fails, so release the lease here.
            if self._started and not self._finished:
                self._finish("aborted", f"{type(e).__name__}: {e}" if str(e) else type(e).__name__)
            raise
        return self

    def __exit__(self, exc_type: Any, exc: BaseException | None, tb: Any) -> None:
        if exc is not None:
            self._finish("failed", f"{exc_type.__name__}: {exc}" if str(exc) else exc_type.__name__)
        else:
            self._finish("ok", None)
        return None

    def finish(self, outcome: str = "ok", error: str | None = None) -> None:
        """End the operation (for ``tick`` users): complete participants, tombstone the lease, call mirror."""
        self._finish(outcome, error)

    def _finish(self, outcome: str, error: str | None) -> None:
        if self._finished or self.op is None:
            return
        self._finished = True
        op = self.op
        report = self.report
        facts = dict(self.facts)
        facts["duration_s"] = self.clock() - op.started_at
        facts["waited_s"] = report.waited_s if report else 0.0
        facts["capped"] = bool(report and report.capped)
        facts["forced_now"] = bool(report and report.forced_now)
        if error:
            facts["error"] = error
        for p in self.relevant():
            try:
                res = p.call("complete", {"op": op.to_dict(), "outcome": outcome, "facts": facts})
                if not res.ok:
                    detail = res.error.message if res.error else "failed"
                    self.say(f"{p.name}: complete failed: {detail}")
            except Exception as exc:  # reported, never raised
                self.say(f"{p.name}: complete raised: {exc}")
        try:
            self.lease_dir.complete(op.id, outcome, facts)
        except SvcError as exc:
            self.say(f"lease complete failed: {exc}")
        if self.mirror is not None:
            try:
                self.mirror("end", op)
            except Exception as exc:
                self.say(f"mirror end failed: {exc}")

    def run(self, action: Callable[[], dict[str, Any] | None]) -> None:
        """Run ``action`` inside the operation; its returned dict (if any) is merged into ``facts``."""
        with self:
            self.facts.update(action() or {})
