from __future__ import annotations

import os
import select
import sys
import threading
import time
from pathlib import Path

import pytest

from cli_svckit.coord.watch import LeaseWatcher

BACKENDS = ["poll"]
if hasattr(select, "kqueue"):
    BACKENDS.append("kqueue")
if sys.platform.startswith("linux"):
    BACKENDS.append("inotify")


class Recorder:
    def __init__(self) -> None:
        self.count = 0
        self.cond = threading.Condition()

    def __call__(self) -> None:
        with self.cond:
            self.count += 1
            self.cond.notify_all()

    def wait_past(self, n: int, timeout: float) -> float | None:
        """Wait until count > n; return seconds waited or None on timeout."""
        t0 = time.monotonic()
        with self.cond:
            ok = self.cond.wait_for(lambda: self.count > n, timeout)
        return time.monotonic() - t0 if ok else None


def make(tmp_path: Path, backend: str, cb=None) -> tuple[LeaseWatcher, Recorder, Path]:
    rec = Recorder()
    d = tmp_path / "leases"
    interval = 0.2 if backend == "poll" else 0.1
    w = LeaseWatcher(d, cb or rec, backend=backend, poll_interval=interval, debounce=0.02)
    return w, rec, d


@pytest.fixture(params=BACKENDS)
def backend(request: pytest.FixtureRequest) -> str:
    return request.param


def test_start_creates_dir_and_fires_once(tmp_path: Path, backend: str) -> None:
    w, rec, d = make(tmp_path, backend)
    w.start()
    try:
        assert d.is_dir()
        assert (d.stat().st_mode & 0o777) == 0o700
        assert w.backend == backend
        assert rec.wait_past(0, 2) is not None
    finally:
        w.stop()


def test_create_replace_delete_fire(tmp_path: Path, backend: str) -> None:
    w, rec, d = make(tmp_path, backend)
    w.start()
    try:
        assert rec.wait_past(0, 2) is not None
        time.sleep(0.3)
        f = d / "op.json"
        for action in ("create", "replace", "delete"):
            n = rec.count
            if action == "create":
                f.write_text("1")
            elif action == "replace":
                tmp = d / "op.json.tmp"
                tmp.write_text("2")
                os.replace(tmp, f)
            else:
                f.unlink()
            assert rec.wait_past(n, 2) is not None, action
            time.sleep(0.3)
    finally:
        w.stop()


def test_latency_p95(tmp_path: Path, backend: str) -> None:
    w, rec, d = make(tmp_path, backend)
    w.start()
    lat: list[float] = []
    try:
        rec.wait_past(0, 2)
        time.sleep(0.3)
        for i in range(20):
            n = rec.count
            tmp = d / "x.tmp"
            tmp.write_text(str(i))
            os.replace(tmp, d / "x.json")
            got = rec.wait_past(n, 2)
            assert got is not None
            lat.append(got)
            time.sleep(0.1)
    finally:
        w.stop()
    lat.sort()
    p95 = lat[int(len(lat) * 0.95) - 1]
    assert p95 < (2.0 if backend == "poll" else 1.0)


def test_dir_deleted_and_recreated(tmp_path: Path, backend: str) -> None:
    w, rec, d = make(tmp_path, backend)
    w.start()
    try:
        rec.wait_past(0, 2)
        time.sleep(0.3)
        (d / "a.json").write_text("1")
        (d / "a.json").unlink()
        d.rmdir()
        time.sleep(0.4)
        n = rec.count
        d.mkdir(mode=0o700)
        assert rec.wait_past(n, 3) is not None
        time.sleep(0.4)
        n = rec.count
        (d / "b.json").write_text("1")
        assert rec.wait_past(n, 2) is not None
    finally:
        w.stop()


def test_callback_exception_keeps_running(tmp_path: Path, backend: str, capsys: pytest.CaptureFixture[str]) -> None:
    calls: list[int] = []
    done = threading.Event()

    def cb() -> None:
        calls.append(1)
        if len(calls) >= 2:
            done.set()
        raise RuntimeError("boom")

    w, _rec, d = make(tmp_path, backend, cb)
    w.start()
    try:
        time.sleep(0.3)
        (d / "a").write_text("1")
        assert done.wait(3)
    finally:
        w.stop()
    assert "boom" in capsys.readouterr().err


def test_stop_is_prompt_idempotent_no_leak(tmp_path: Path, backend: str) -> None:
    before = threading.active_count()
    w, rec, _d = make(tmp_path, backend)
    w2 = LeaseWatcher(tmp_path / "other", rec, backend=backend, poll_interval=30.0)
    for watcher in (w, w2):
        watcher.start()
        watcher.start()  # idempotent
    rec.wait_past(0, 2)
    t0 = time.monotonic()
    for watcher in (w, w2):
        watcher.stop()
        watcher.stop()
    assert time.monotonic() - t0 < 1.0
    assert threading.active_count() == before
    w.start()  # restartable
    w.stop()
    assert threading.active_count() == before


def test_unknown_backend_rejected(tmp_path: Path) -> None:
    with pytest.raises(ValueError):
        LeaseWatcher(tmp_path, lambda: None, backend="nope")


def test_auto_picks_available_backend(tmp_path: Path) -> None:
    w, _rec, _d = make(tmp_path, "auto")
    w.start()
    try:
        assert w.backend in BACKENDS
    finally:
        w.stop()
