"""Tests for cli_svckit.procs."""

from __future__ import annotations

import os
import subprocess
import sys
import time

import pytest

from cli_svckit import procs

PS = """  1     0     1 /sbin/init
 10     1    10 daemon --flag
 11    10    10 worker one
 12    11    12 grandchild
bad line
 13     1    13
"""


def test_parse_ps_and_tree() -> None:
    table = procs.parse_ps(PS)
    assert [r.pid for r in table] == [1, 10, 11, 12, 13]
    assert table[1].command == "daemon --flag"
    assert table[4].command == ""
    assert [r.pid for r in procs.descendants(10, table)] == [11, 12]
    assert procs.descendants(999, table) == []
    assert [r.pid for r in procs.group_members(10, table)] == [10, 11]


def test_pid_alive_self_and_invalid() -> None:
    assert procs.pid_alive(os.getpid())
    assert not procs.pid_alive(0)
    assert not procs.pid_alive(-5)


def test_reaped_child_not_alive() -> None:
    p = subprocess.Popen([sys.executable, "-c", "pass"])
    p.wait()
    assert not procs.pid_alive(p.pid)


def test_start_time_self_and_missing() -> None:
    t = procs.process_start_time(os.getpid())
    assert t is not None and t <= time.time() + 1
    p = subprocess.Popen([sys.executable, "-c", "pass"])
    p.wait()
    assert procs.process_start_time(p.pid) is None


def test_spawned_tree() -> None:
    code = (
        "import subprocess,sys,time;"
        "subprocess.Popen([sys.executable,'-c','import time;time.sleep(30)']);time.sleep(30)"
    )
    p = subprocess.Popen([sys.executable, "-c", code], start_new_session=True)
    try:
        kids: list[procs.ProcInfo] = []
        for _ in range(100):
            kids = procs.descendants(p.pid)
            if kids:
                break
            time.sleep(0.05)
        assert len(kids) == 1
        assert {r.pid for r in procs.group_members(p.pid)} >= {p.pid, kids[0].pid}
    finally:
        p.kill()
        p.wait()


def test_missing_ps(monkeypatch: pytest.MonkeyPatch) -> None:
    def boom(*a: object, **k: object) -> None:
        raise FileNotFoundError("ps")

    monkeypatch.setitem(sys.modules, "psutil", None)
    monkeypatch.setattr(procs.subprocess, "run", boom)
    assert procs.process_table() == []
    assert procs.process_start_time(os.getpid()) is None
