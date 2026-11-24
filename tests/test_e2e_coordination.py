"""End-to-end proof: a controller Operation coordinating with the toy participant daemon (a real subprocess)."""

from __future__ import annotations

import json
import os
import statistics
import subprocess
import sys
import threading
import time
from collections.abc import Callable, Iterator
from pathlib import Path

import pytest

from cli_svckit.contract.client import Participant
from cli_svckit.contract.envelope import Op, new_op_id
from cli_svckit.coord.controller import Operation
from cli_svckit.coord.lease import LeaseDir
from cli_svckit.coord.participant import Interest, active_ops

TOY = str(Path(__file__).resolve().parents[1] / "examples" / "toy_participant.py")
INTEREST = Interest(kinds=["service.*"], services=["example-svc"])
KIND = "service.restart"
TARGETS = ["example-svc"]


def wait_for(cond: Callable[[], object], timeout: float = 8.0, step: float = 0.02) -> bool:
    """Poll ``cond`` until it is truthy or ``timeout`` seconds pass."""
    end = time.time() + timeout
    while time.time() < end:
        if cond():
            return True
        time.sleep(step)
    return bool(cond())


class Toy:
    """A toy daemon subprocess plus the paths it uses."""

    def __init__(self, root: Path, backend: str = "auto") -> None:
        self.lease_path = root / "leases"
        self.state = root / "state"
        self.state.mkdir(parents=True, exist_ok=True)
        self.events = self.state / "events.jsonl"
        self.leases = LeaseDir(self.lease_path)
        self.backend = backend
        self.proc: subprocess.Popen[bytes] | None = None

    def participant(self) -> Participant:
        return Participant("example-toy", [sys.executable, TOY, "--state", str(self.state)], timeout_s=20.0)

    def operation(self, **kw: object) -> Operation:
        opts: dict[str, object] = {"poll_s": 0.2, "max_wait_s": 5, "say": lambda _line: None}
        opts.update(kw)
        return Operation(KIND, TARGETS, lease_dir=self.leases, participants=[self.participant()], **opts)  # type: ignore[arg-type]

    def read_events(self) -> list[dict]:
        if not self.events.exists():
            return []
        return [json.loads(x) for x in self.events.read_text().splitlines() if x.strip()]

    def event(self, kind: str, op_id: str) -> dict | None:
        return next((e for e in self.read_events() if e["event"] == kind and e["op_id"] == op_id), None)

    def wait_event(self, kind: str, op_id: str, timeout: float = 8.0) -> dict:
        assert wait_for(lambda: self.event(kind, op_id) is not None, timeout), f"no {kind} event for {op_id}"
        found = self.event(kind, op_id)
        assert found is not None
        return found

    def start(self) -> None:
        env = dict(os.environ, SVCKIT_WATCH_BACKEND=self.backend)
        self.proc = subprocess.Popen(
            [sys.executable, TOY, "--state", str(self.state), "daemon", "--lease-dir", str(self.lease_path)],
            env=env,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        self.warm_up()

    def warm_up(self) -> None:
        """Prove the daemon is watching: announce a throw-away op and wait for its prepare/complete events."""
        now = time.time()
        op = Op(id=new_op_id(), kind=KIND, targets=TARGETS, started_at=now, deadline=now + 30, expires_at=now + 60)
        self.leases.ensure()
        self.leases.create(op, "prepare")
        self.wait_event("prepare", op.id, 10.0)
        self.leases.complete(op.id, "ok", {})
        self.wait_event("complete", op.id, 10.0)

    def stop(self) -> None:
        """Terminate the daemon we started (by handle only)."""
        if self.proc is not None and self.proc.poll() is None:
            self.proc.terminate()
            try:
                self.proc.wait(5)
            except subprocess.TimeoutExpired:
                self.proc.kill()
                self.proc.wait()
        self.proc = None


@pytest.fixture
def make_toy(tmp_path: Path) -> Iterator[Callable[..., Toy]]:
    toys: list[Toy] = []

    def make(backend: str = "auto") -> Toy:
        toy = Toy(tmp_path / f"toy{len(toys)}", backend)
        toys.append(toy)
        toy.start()
        return toy

    yield make
    for toy in toys:
        toy.stop()


@pytest.mark.parametrize("backend", ["auto", "poll"])
def test_prepare_latency(make_toy: Callable[..., Toy], backend: str) -> None:
    toy = make_toy(backend)
    lat: list[float] = []
    for _ in range(10):
        op = toy.operation()
        with op:
            assert op.op is not None
            lat.append(toy.wait_event("prepare", op.op.id)["t"] - op.op.started_at)
    print(
        f"[{backend}] prepare latency s: " + " ".join(f"{x:.3f}" for x in lat)
        + f" | p50={statistics.median(lat):.3f} max={max(lat):.3f}"
    )
    assert max(lat) < 2.0
    if backend == "auto":  # polling waits up to its 1 s interval, so only the hard bound applies there
        assert statistics.median(lat) < 0.5


def test_complete_reaches_daemon(make_toy: Callable[..., Toy]) -> None:
    toy = make_toy()
    op = toy.operation()
    with op:
        assert op.op is not None
        toy.wait_event("prepare", op.op.id)
        op.facts["restarted"] = True
    assert op.op is not None
    toy.wait_event("complete", op.op.id)
    tomb = [t for t in toy.leases.tombstones() if t.op.id == op.op.id]
    assert len(tomb) == 1
    assert tomb[0].outcome == "ok"
    assert tomb[0].facts is not None and tomb[0].facts["restarted"] is True
    assert all(lease.op.id != op.op.id for lease in toy.leases.list_active())


def test_busy_waits_then_proceeds(make_toy: Callable[..., Toy]) -> None:
    toy = make_toy()
    busy = toy.state / "busy"
    busy.touch()
    op = toy.operation(max_wait_s=8)
    timer = threading.Timer(1.0, busy.unlink)
    timer.start()
    t0 = time.time()
    try:
        with op:
            waited = time.time() - t0
            assert op.report is not None
            assert not op.report.capped
    finally:
        timer.cancel()
    assert 0.9 <= waited < 6


def test_busy_cap_proceeds_capped(make_toy: Callable[..., Toy]) -> None:
    toy = make_toy()
    (toy.state / "busy").touch()
    op = toy.operation(max_wait_s=2)
    with op:
        assert op.report is not None
        assert op.report.capped is True
        assert op.report.ready is True
    tomb = [t for t in toy.leases.tombstones() if op.op is not None and t.op.id == op.op.id]
    assert tomb and tomb[0].facts is not None and tomb[0].facts["capped"] is True


def test_controller_crash_lease_expires(make_toy: Callable[..., Toy]) -> None:
    toy = make_toy()
    op = toy.operation(max_wait_s=1, margin_s=2)
    started = op.start()  # the controller "dies" right after announcing: it never completes
    assert started.expires_at - started.started_at == pytest.approx(3.0)
    toy.wait_event("prepare", started.id)
    assert [lease.op.id for lease in active_ops(toy.leases, INTEREST)] == [started.id]
    assert wait_for(lambda: not active_ops(toy.leases, INTEREST), 6.0)
    toy.leases.expire_stale()
    tomb = [t for t in toy.leases.tombstones() if t.op.id == started.id]
    assert len(tomb) == 1
    assert tomb[0].outcome == "expired"


def test_daemon_restart_catches_up_complete(make_toy: Callable[..., Toy]) -> None:
    toy = make_toy()
    op = toy.operation()
    started = op.start()
    toy.wait_event("prepare", started.id)
    toy.stop()  # the daemon goes away mid-operation
    op.finish("ok")  # the op completes while nobody watches
    assert toy.event("complete", started.id) is None
    toy.start()
    toy.wait_event("complete", started.id)
    time.sleep(0.5)  # a duplicate would show up by now
    names = [e["event"] for e in toy.read_events() if e["op_id"] == started.id]
    assert names.count("complete") == 1
    assert names.count("prepare") == 1
