"""Tests for the participant helpers and the Quiescer (temp dirs and a fake clock only)."""

from __future__ import annotations

import json
import threading

import pytest

from cli_svckit.contract.envelope import Op
from cli_svckit.coord.lease import LeaseDir
from cli_svckit.coord.participant import Interest, Quiescer, active_ops


class FakeClock:
    def __init__(self, t: float = 1000.0) -> None:
        self.t = t

    def __call__(self) -> float:
        return self.t


def make_op(op_id: str, *, kind: str = "update", targets: list[str] | None = None, expires_at: float = 5000.0) -> Op:
    return Op(
        id=op_id,
        kind=kind,
        targets=targets or ["com.example.worker"],
        started_at=1000.0,
        deadline=1300.0,
        expires_at=expires_at,
    )


@pytest.fixture
def clock() -> FakeClock:
    return FakeClock()


@pytest.fixture
def ld(tmp_path, clock) -> LeaseDir:
    d = LeaseDir(tmp_path / "leases", clock=clock)
    d.ensure()
    return d


class Recorder:
    def __init__(self) -> None:
        self.events: list[tuple[str, str]] = []

    def prep(self, lease) -> None:
        self.events.append(("prepare", lease.op.id))

    def done(self, lease) -> None:
        self.events.append(("complete", lease.op.id))


def make_q(ld, clock, rec, interest=None, **kw) -> Quiescer:
    interest = interest or Interest(kinds=["update", "restart*"], services=["com.example.*"])
    return Quiescer(ld, interest, rec.prep, rec.done, clock=clock, **kw)


def test_interest_globs():
    i = Interest(kinds=["update", "restart*"], services=["com.example.w*"])
    assert i.matches(make_op("op-1", kind="restart-all"))
    assert i.matches(make_op("op-1", targets=["com.other.x", "com.example.worker"]))
    assert not i.matches(make_op("op-1", kind="reinstall"))
    assert not i.matches(make_op("op-1", targets=["com.example.api"]))
    assert not i.matches(make_op("op-1", targets=["COM.EXAMPLE.WORKER"]))


def test_active_ops_filters(ld, clock):
    ld.create(make_op("op-a"))
    ld.create(make_op("op-b", targets=["com.other.svc"]))
    ld.create(make_op("op-c", kind="backup"))
    interest = Interest(kinds=["update"], services=["com.example.*"])
    assert [le.op.id for le in active_ops(ld, interest)] == ["op-a"]
    assert active_ops(ld, interest, now=6000.0) == []


def test_refresh_fires_once(ld, clock):
    rec = Recorder()
    q = make_q(ld, clock, rec)
    q.refresh()
    assert rec.events == []
    clock.t = 1001.0
    ld.create(make_op("op-a"))
    q.refresh()
    q.refresh()
    assert rec.events == [("prepare", "op-a")]
    ld.set_phase("op-a", "running")
    q.refresh()
    assert rec.events == [("prepare", "op-a")]
    clock.t = 1002.0
    ld.complete("op-a", "ok", None)
    q.refresh()
    q.refresh()
    assert rec.events == [("prepare", "op-a"), ("complete", "op-a")]


def test_overlapping_ops(ld, clock):
    rec = Recorder()
    q = make_q(ld, clock, rec)
    clock.t = 1001.0
    ld.create(make_op("op-a"))
    ld.create(make_op("op-b"))
    q.refresh()
    ld.complete("op-a", "ok", None)
    q.refresh()
    assert rec.events == [("prepare", "op-a"), ("prepare", "op-b"), ("complete", "op-a")]
    assert [le.op.id for le in active_ops(ld, q.interest)] == ["op-b"]
    assert q.prepared == {"op-b"}


def test_came_and_went_between_refreshes(ld, clock):
    rec = Recorder()
    q = make_q(ld, clock, rec)
    clock.t = 1001.0
    ld.create(make_op("op-fast"))
    ld.complete("op-fast", "ok", None)
    q.refresh()
    assert rec.events == [("prepare", "op-fast"), ("complete", "op-fast")]


def test_old_tombstones_do_not_fire(ld, clock):
    ld.create(make_op("op-old"))
    ld.complete("op-old", "ok", None)
    clock.t = 1100.0
    rec = Recorder()
    q = make_q(ld, clock, rec)
    q.catch_up()
    assert rec.events == []


def test_non_matching_ignored(ld, clock):
    rec = Recorder()
    q = make_q(ld, clock, rec)
    clock.t = 1001.0
    ld.create(make_op("op-x", targets=["com.other.svc"]))
    ld.complete("op-x", "ok", None)
    q.refresh()
    assert rec.events == []


def test_expired_lease_fires_complete(ld, clock):
    rec = Recorder()
    q = make_q(ld, clock, rec)
    seen = []
    q.on_complete = lambda lease: seen.append(lease.outcome)
    ld.create(make_op("op-a", expires_at=1100.0))
    q.refresh()
    clock.t = 1100.0
    q.refresh()
    assert rec.events == [("prepare", "op-a")] and seen == ["expired"]
    ld.expire_stale()
    q.refresh()
    assert seen == ["expired"]


def test_callback_exceptions_do_not_stop_others(ld, clock, capsys):
    calls = []

    def prep(lease):
        calls.append(lease.op.id)
        if lease.op.id == "op-a":
            raise RuntimeError("boom")

    q = Quiescer(ld, Interest(["*"], ["*"]), prep, lambda le: None, clock=clock)
    ld.create(make_op("op-a"))
    ld.create(make_op("op-b"))
    q.refresh()
    assert calls == ["op-a", "op-b"]
    assert "RuntimeError: boom" in capsys.readouterr().err
    q.refresh()
    assert calls == ["op-a", "op-b"]


def test_refire_safety_across_instances(ld, clock, tmp_path):
    state = tmp_path / "state" / "quiescer.json"
    rec1 = Recorder()
    q1 = make_q(ld, clock, rec1, state_file=state)
    clock.t = 1001.0
    ld.create(make_op("op-a"))
    ld.create(make_op("op-b"))
    q1.catch_up()
    clock.t = 1002.0
    ld.complete("op-a", "ok", None)
    q1.refresh()
    assert rec1.events == [("prepare", "op-a"), ("prepare", "op-b"), ("complete", "op-a")]
    saved = json.loads(state.read_text())
    assert saved["prepared"] == ["op-b"] and saved["completed"] == ["op-a"]

    # Daemon restarts: nothing refires; op-b completing while down fires complete on catch-up.
    clock.t = 1003.0
    ld.complete("op-b", "ok", None)
    clock.t = 1004.0
    ld.create(make_op("op-c"))
    ld.complete("op-c", "failed", None)
    rec2 = Recorder()
    q2 = make_q(ld, clock, rec2, state_file=state)
    q2.catch_up()
    assert rec2.events == [("complete", "op-b"), ("prepare", "op-c"), ("complete", "op-c")]
    rec3 = Recorder()
    q3 = make_q(ld, clock, rec3, state_file=state)
    q3.catch_up()
    q3.refresh()
    assert rec3.events == []


def test_corrupt_state_file_starts_fresh(ld, clock, tmp_path, capsys):
    state = tmp_path / "q.json"
    state.write_text("{oops")
    rec = Recorder()
    q = make_q(ld, clock, rec, state_file=state)
    ld.create(make_op("op-a"))
    q.catch_up()
    assert rec.events == [("prepare", "op-a")]
    assert "ignoring quiescer state" in capsys.readouterr().err
    assert json.loads(state.read_text())["prepared"] == ["op-a"]


def test_pruned_tombstone_forgotten(ld, clock, tmp_path):
    rec = Recorder()
    q = make_q(ld, clock, rec)
    clock.t = 1001.0
    ld.create(make_op("op-a"))
    q.refresh()
    ld.complete("op-a", "ok", None)
    q.refresh()
    assert q.completed == {"op-a"}
    clock.t = 1001.0 + 86401.0
    ld.prune_tombstones()
    q.refresh()
    assert q.completed == frozenset()
    assert len(rec.events) == 2


def test_refresh_thread_safe(ld, clock):
    rec = Recorder()
    q = make_q(ld, clock, rec)
    for i in range(5):
        ld.create(make_op(f"op-{i}"))
    threads = [threading.Thread(target=q.refresh) for _ in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert sorted(rec.events) == [("prepare", f"op-{i}") for i in range(5)]
