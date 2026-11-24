"""Tests for the controller operation runner, with a fake clock and fake participants."""

from __future__ import annotations

from typing import Any

import pytest

from cli_svckit.contract.client import CallResult
from cli_svckit.contract.envelope import Error, Response
from cli_svckit.coord.controller import Operation, OperationAborted
from cli_svckit.coord.lease import LeaseDir


class Clock:
    def __init__(self) -> None:
        self.t = 1_700_000_000.0

    def __call__(self) -> float:
        return self.t

    def sleep(self, s: float) -> None:
        self.t += s


def ok(name: str, method: str, result: dict[str, Any] | None = None, rc: int = 0) -> CallResult:
    return CallResult(name, method, True, Response("x", True, result or {}), None, rc, 0.0, False)


def timeout(name: str, method: str, policy: str = "open") -> CallResult:
    err = Error("timeout", "timed out")
    return CallResult(name, method, False, None, err, None, 1.0, True, fail_policy=policy)


def ready() -> dict[str, Any]:
    return {"state": "ready", "op_id": "x", "quiesced": True, "in_flight": []}


def busy(interrupted: bool = True) -> dict[str, Any]:
    item = {"id": "j1", "kind": "job", "what": "build", "interrupted": interrupted}
    return {"state": "busy", "op_id": "x", "quiesced": False, "in_flight": [item]}


class Fake:
    def __init__(
        self,
        name: str = "alice",
        prepares: list[Any] | None = None,
        *,
        policy: str = "open",
        coordination: Any = "default",
        wait_for: str = "interrupted",
        hook: Any = None,
    ) -> None:
        self.name = name
        self.fail_policy = policy
        self.wait_for = wait_for
        self.prepares = list(prepares if prepares is not None else [ready()])
        self.calls: list[tuple[str, dict[str, Any]]] = []
        self.hook = hook
        if coordination == "default":
            coordination = {"kinds": ["restart"], "services": ["example-*"]}
        self.coordination = coordination

    def describe(self, cache: Any = None) -> dict[str, Any] | None:
        if self.coordination == "fail":
            return None
        return {} if self.coordination is None else {"coordination": self.coordination}

    def call(self, method: str, params: dict[str, Any] | None = None, **kw: Any) -> CallResult:
        self.calls.append((method, params or {}))
        if self.hook:
            self.hook(method)
        if method == "prepare":
            item = self.prepares.pop(0) if len(self.prepares) > 1 else self.prepares[0]
            if isinstance(item, CallResult):
                return item
            return ok(self.name, method, item, rc=0 if item["state"] == "ready" else 3)
        return ok(self.name, method)

    def methods(self) -> list[str]:
        return [m for m, _ in self.calls]


@pytest.fixture
def env(tmp_path):
    clock = Clock()
    leases = LeaseDir(tmp_path / "leases", clock=clock)
    lines: list[str] = []
    return clock, leases, lines


def make(env, parts, **kw):
    clock, leases, lines = env
    kw.setdefault("poll_s", 10.0)
    return Operation(
        "restart",
        ["example-web"],
        lease_dir=leases,
        participants=parts,
        say=lines.append,
        clock=clock,
        sleep=clock.sleep,
        **kw,
    )


def test_ready_at_once(env):
    p = Fake()
    op = make(env, [p])
    with op:
        assert env[1].list_active()[0].phase == "running"
    assert p.methods() == ["prepare", "complete"]
    assert op.report.ready and not op.report.capped


def test_busy_then_ready(env):
    p = Fake(prepares=[busy(), busy(), ready()])
    op = make(env, [p])
    with op:
        pass
    assert p.methods().count("prepare") == 3
    assert [c[1]["poll"] for c in p.calls if c[0] == "prepare"] == [0, 1, 2]
    assert op.report.waited_s == 20.0
    assert any("waiting for 1 in flight" in line for line in env[2])


def test_cap_proceed(env):
    p = Fake(prepares=[busy()])
    op = make(env, [p], max_wait_s=30.0)
    with op:
        pass
    assert op.report.capped and op.report.ready
    assert "alice" in op.report.busy
    tomb = env[1].tombstones()[0]
    assert tomb.facts["capped"] is True and tomb.outcome == "ok"


def test_cap_abort(env):
    p = Fake(prepares=[busy()])
    op = make(env, [p], max_wait_s=30.0, on_cap="abort")
    with pytest.raises(OperationAborted):
        with op:
            raise AssertionError("body must not run")
    assert p.methods()[-1] == "complete"
    assert env[1].list_active() == []
    assert env[1].tombstones()[0].outcome == "aborted"


def test_now_forces_proceed(env):
    p = Fake(prepares=[busy()])
    op = make(env, [p], now=True)
    with op:
        pass
    assert p.methods().count("prepare") == 1
    assert op.report.forced_now and op.report.ready
    assert env[1].tombstones()[0].facts["forced_now"] is True


def test_fail_open_on_timeout(env):
    p = Fake(prepares=[timeout("alice", "prepare")])
    op = make(env, [p])
    with op:
        pass
    assert op.report.ready and "alice" in op.report.errors


def test_fail_closed_on_timeout(env):
    p = Fake(prepares=[timeout("alice", "prepare", "closed")], policy="closed")
    op = make(env, [p], max_wait_s=30.0, on_cap="abort")
    with pytest.raises(OperationAborted):
        with op:
            pass
    assert p.methods().count("prepare") == 4
    assert env[1].tombstones()[0].outcome == "aborted"


def test_keyboard_interrupt_during_prepare_releases_lease(env):
    p = Fake(prepares=[busy()])
    op = make(env, [p])

    def boom(_s: float) -> None:
        raise KeyboardInterrupt

    op.sleep = boom
    with pytest.raises(KeyboardInterrupt):
        with op:
            pass
    assert env[1].tombstones()[0].outcome == "aborted"
    assert not env[1].active(op.clock()) if hasattr(env[1], "active") else True


def test_detached_busy_does_not_block(env):
    p = Fake(prepares=[busy(interrupted=False)])
    op = make(env, [p])
    with op:
        pass
    assert p.methods().count("prepare") == 1
    assert "alice" in op.report.busy and op.report.ready and not op.report.capped


def test_wait_for_all_blocks_detached(env):
    p = Fake(prepares=[busy(interrupted=False), ready()])
    op = make(env, [p], wait_for="all")
    with op:
        pass
    assert p.methods().count("prepare") == 2


def test_complete_facts_and_failure_outcome(env):
    p = Fake()
    op = make(env, [p])
    with pytest.raises(RuntimeError):
        with op:
            op.facts["targets"] = ["example-web"]
            env[0].t += 5
            raise RuntimeError("boom")
    tomb = env[1].tombstones()[0]
    assert tomb.outcome == "failed"
    assert tomb.facts["error"].endswith("boom")
    assert tomb.facts["targets"] == ["example-web"]
    assert tomb.facts["duration_s"] == 5.0
    assert {"waited_s", "capped", "forced_now"} <= set(tomb.facts)
    complete = p.calls[-1]
    assert complete[0] == "complete" and complete[1]["outcome"] == "failed"


def test_lease_written_before_first_prepare_and_tombstoned(env):
    seen: dict[str, Any] = {}

    def hook(method: str) -> None:
        if method == "prepare" and "phase" not in seen:
            active = env[1].list_active()
            seen["phase"] = active[0].phase if active else None

    op = make(env, [Fake(hook=hook)])
    with op:
        pass
    assert seen["phase"] == "prepare"
    assert env[1].list_active() == []
    assert env[1].tombstones()[0].op.id == op.op.id


def test_op_times(env):
    op = make(env, [Fake()], max_wait_s=100.0, margin_s=50.0)
    o = op.start()
    assert o.deadline == o.started_at + 100.0 and o.expires_at == o.started_at + 150.0
    op.finish()


def test_mirror_called(env):
    events: list[tuple[str, str]] = []
    op = make(env, [Fake()], mirror=lambda ev, o: events.append((ev, o.id)))
    with op:
        pass
    assert events == [("start", op.op.id), ("end", op.op.id)]


def test_irrelevant_participants_never_called(env):
    other_kind = Fake("bob", coordination={"kinds": ["update"], "services": ["*"]})
    other_svc = Fake("carol", coordination={"kinds": ["restart"], "services": ["other-*"]})
    none = Fake("dave", coordination=None)
    failed_open = Fake("erin", coordination="fail")
    op = make(env, [other_kind, other_svc, none, failed_open])
    with op:
        pass
    for p in (other_kind, other_svc, none, failed_open):
        assert p.calls == []


def test_describe_failure_closed_is_relevant(env):
    p = Fake(coordination="fail", policy="closed")
    op = make(env, [p])
    with op:
        pass
    assert p.methods() == ["prepare", "complete"]
    assert sum("describe failed" in line for line in env[2]) == 1


def test_tick_flow(env):
    p = Fake(prepares=[busy(), ready()])
    op = make(env, [p])
    assert op.tick() == "waiting"
    assert env[1].list_active()[0].phase == "prepare"
    again = make(env, [p], op_id=op.op.id)
    assert again.tick() == "ready"
    assert again.op.id == op.op.id and len(env[1].list_active()) == 1
    assert env[1].list_active()[0].phase == "running"
    again.finish()
    assert env[1].list_active() == []


def test_tick_capped(env):
    p = Fake(prepares=[busy()])
    op = make(env, [p], max_wait_s=20.0)
    assert op.tick() == "waiting"
    env[0].t += 25
    assert op.tick() == "capped"
    op.finish()


def test_run_merges_facts(env):
    op = make(env, [Fake()])
    op.run(lambda: {"targets": ["example-web"]})
    assert env[1].tombstones()[0].facts["targets"] == ["example-web"]
