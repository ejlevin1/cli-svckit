"""P4 cross-version checks: previous release's toy participant vs the current controller, and the reverse.

Skipped unless ``SVCKIT_PREV_SRC`` points at a checkout of the previous release.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from collections.abc import Callable, Iterator
from pathlib import Path

import pytest

from cli_svckit.contract.client import Participant
from cli_svckit.coord.controller import Operation
from cli_svckit.coord.lease import LeaseDir
from cli_svckit.testing import assert_conforms

PREV_SRC = os.environ.get("SVCKIT_PREV_SRC")
pytestmark = pytest.mark.skipif(not PREV_SRC, reason="SVCKIT_PREV_SRC not set (path to a previous-release checkout)")

ROOT = Path(__file__).resolve().parents[1]
CUR_TOY = ROOT / "examples" / "toy_participant.py"
KIND = "service.restart"
TARGETS = ["example-svc"]

# Runs the PREVIOUS release's controller (PYTHONPATH points at its src) against a toy's ``svc`` hooks.
DRIVER = """
import json, os, sys
import cli_svckit
from cli_svckit.contract.client import Participant
from cli_svckit.coord.controller import Operation
from cli_svckit.coord.lease import LeaseDir

lease_dir, toy, state, cur_src, wrapper = sys.argv[1:6]
# The participant must run on the CURRENT library, not on ours (PYTHONPATH would otherwise leak into it).
env = dict(os.environ, PYTHONPATH=cur_src)
p = Participant("example-toy", [sys.executable, wrapper, toy, "--state", state], timeout_s=10.0, env=env)
op = Operation("service.restart", ["example-svc"], lease_dir=LeaseDir(lease_dir), participants=[p],
               poll_s=0.2, max_wait_s=5, say=lambda _line: None)
with op:
    op.facts["restarted"] = True
print(json.dumps({"op_id": op.op.id, "src": cli_svckit.__file__}))
"""


# Records which library served the participant's ``svc`` hooks, then runs the toy unchanged.
WRAPPER = """
import os, runpy, sys
import cli_svckit
with open(os.environ["SVCKIT_PROBE_FILE"], "a", encoding="utf-8") as fh:
    fh.write(cli_svckit.__file__ + "\\n")
toy = sys.argv[1]
sys.argv = [toy, *sys.argv[2:]]
runpy.run_path(toy, run_name="__main__")
"""


def prev_root() -> Path:
    assert PREV_SRC
    root = Path(PREV_SRC)
    assert (root / "examples" / "toy_participant.py").is_file(), "SVCKIT_PREV_SRC is not a release checkout"
    return root


def env_for(prev: bool) -> dict[str, str]:
    env = dict(os.environ, SVCKIT_WATCH_BACKEND="poll")
    if prev:
        env["PYTHONPATH"] = os.pathsep.join([str(prev_root() / "src"), env.get("PYTHONPATH", "")]).rstrip(os.pathsep)
    return env


def wait_for(cond: Callable[[], bool], timeout: float = 12.0) -> bool:
    end = time.time() + timeout
    while time.time() < end:
        if cond():
            return True
        time.sleep(0.05)
    return cond()


class Daemon:
    """A toy daemon subprocess (from either release) on temp lease and state dirs."""

    def __init__(self, root: Path, toy: Path, prev: bool) -> None:
        self.toy, self.prev = toy, prev
        self.leases = root / "leases"
        self.state = root / "state"
        self.state.mkdir(parents=True)
        self.events = self.state / "events.jsonl"
        self.proc: subprocess.Popen[bytes] | None = None

    def start(self) -> None:
        self.proc = subprocess.Popen(
            [sys.executable, str(self.toy), "--state", str(self.state), "daemon", "--lease-dir", str(self.leases)],
            env=env_for(self.prev),
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        assert wait_for(lambda: (self.state / "quiescer.json").exists() or self.leases.is_dir(), 10)
        time.sleep(0.5)  # let the watcher attach

    def has(self, event: str, op_id: str) -> bool:
        if not self.events.exists():
            return False
        rows = [json.loads(x) for x in self.events.read_text().splitlines() if x.strip()]
        return any(r["event"] == event and r["op_id"] == op_id for r in rows)

    def stop(self) -> None:
        if self.proc is not None and self.proc.poll() is None:
            self.proc.terminate()
            try:
                self.proc.wait(5)
            except subprocess.TimeoutExpired:
                self.proc.kill()
                self.proc.wait()


@pytest.fixture
def make_daemon(tmp_path: Path) -> Iterator[Callable[[Path, bool], Daemon]]:
    made: list[Daemon] = []

    def make(toy: Path, prev: bool) -> Daemon:
        d = Daemon(tmp_path / f"d{len(made)}", toy, prev)
        made.append(d)
        d.start()
        return d

    yield make
    for d in made:
        d.stop()


def test_previous_participant_conforms_to_current_assert_conforms(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    toy = prev_root() / "examples" / "toy_participant.py"
    cmd = [sys.executable, str(toy), "--state", str(tmp_path / "state")]
    # assert_conforms runs the participant as a subprocess: the previous release's own library must serve it.
    monkeypatch.setenv("PYTHONPATH", env_for(True)["PYTHONPATH"])
    assert assert_conforms(cmd, timeout_s=10).ok


def test_current_controller_with_previous_participant(make_daemon: Callable[[Path, bool], Daemon]) -> None:
    toy = prev_root() / "examples" / "toy_participant.py"
    d = make_daemon(toy, True)
    participant = Participant(
        "example-toy", [sys.executable, str(toy), "--state", str(d.state)], timeout_s=10.0, env=env_for(True)
    )
    op = Operation(
        KIND,
        TARGETS,
        lease_dir=LeaseDir(d.leases),
        participants=[participant],
        poll_s=0.2,
        max_wait_s=5,
        say=lambda _line: None,
    )
    with op:
        assert op.op is not None
        assert wait_for(lambda: d.has("prepare", op.op.id))  # type: ignore[union-attr]
        op.facts["restarted"] = True
    assert op.op is not None
    assert wait_for(lambda: d.has("complete", op.op.id)), "complete did not reach the previous daemon"


def test_previous_controller_with_current_participant(
    make_daemon: Callable[[Path, bool], Daemon], tmp_path: Path
) -> None:
    d = make_daemon(CUR_TOY, False)
    driver = tmp_path / "prev_driver.py"
    driver.write_text(DRIVER, encoding="utf-8")
    wrapper = tmp_path / "probe_wrapper.py"
    wrapper.write_text(WRAPPER, encoding="utf-8")
    probe = tmp_path / "probe.txt"
    seen: dict[str, str] = {}

    def run() -> bool:
        proc = subprocess.run(
            [sys.executable, str(driver), str(d.leases), str(CUR_TOY), str(d.state), str(ROOT / "src"), str(wrapper)],
            env=dict(env_for(True), SVCKIT_PROBE_FILE=str(probe)),
            capture_output=True,
            text=True,
            timeout=15,
        )
        assert proc.returncode == 0, proc.stderr[-2000:]
        seen.update(json.loads(proc.stdout.strip().splitlines()[-1]))
        return True

    assert run()
    assert str(prev_root() / "src") in seen["src"], "driver did not use the previous release's library"
    served = probe.read_text(encoding="utf-8").splitlines()
    assert served, "the current participant's svc hooks never ran"
    assert all(line.startswith(str(ROOT / "src")) for line in served), f"hooks served by the wrong library: {served}"
    assert wait_for(lambda: d.has("prepare", seen["op_id"])), "prepare did not reach the current daemon"
    assert wait_for(lambda: d.has("complete", seen["op_id"])), "complete did not reach the current daemon"
