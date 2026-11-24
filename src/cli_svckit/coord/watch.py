"""Directory watcher used by participant daemons to notice lease changes quickly.

``LeaseWatcher`` watches a *directory* (never a single file, because atomic replace swaps inodes) and calls a
callback whenever something in it changes. It uses kqueue on macOS/BSD, inotify (via ctypes) on Linux and falls
back to periodic snapshots everywhere else.
"""

from __future__ import annotations

import contextlib
import ctypes
import os
import select
import stat
import sys
import threading
import time
from collections.abc import Callable

__all__ = ["LeaseWatcher"]

# inotify constants (linux/inotify.h)
_IN_ATTRIB = 0x00000004
_IN_CLOSE_WRITE = 0x00000008
_IN_MOVED_FROM = 0x00000040
_IN_MOVED_TO = 0x00000080
_IN_CREATE = 0x00000100
_IN_DELETE = 0x00000200
_IN_DELETE_SELF = 0x00000400
_IN_MOVE_SELF = 0x00000800
_IN_IGNORED = 0x00008000
_IN_NONBLOCK = os.O_NONBLOCK
_IN_CLOEXEC = getattr(os, "O_CLOEXEC", 0x80000)
_IN_MASK = (
    _IN_CREATE | _IN_DELETE | _IN_MOVED_TO | _IN_MOVED_FROM | _IN_CLOSE_WRITE | _IN_ATTRIB | _IN_DELETE_SELF
    | _IN_MOVE_SELF
)  # fmt: skip
_IN_GONE = _IN_DELETE_SELF | _IN_MOVE_SELF | _IN_IGNORED

# Wait results shared by all backends.
_EVENT = "event"
_GONE = "gone"
_STOP = "stop"
_TIMEOUT = "timeout"


class _Backend:
    """One way of waiting for directory changes. ``wait`` returns _EVENT, _GONE, _STOP or _TIMEOUT."""

    name = ""

    def __init__(self, path: str, stop_fd: int, poll_interval: float) -> None:
        self.path = path
        self.stop_fd = stop_fd
        self.poll_interval = poll_interval

    def arm(self) -> bool:
        """Start watching the directory; False when it does not exist (yet)."""
        raise NotImplementedError

    def wait(self, timeout: float | None) -> str:
        """Block until something happens or ``timeout`` seconds pass."""
        raise NotImplementedError

    def disarm(self) -> None:
        """Release the per-directory resources (the watcher may arm again later)."""

    def close(self) -> None:
        """Release everything."""
        self.disarm()


class _KqueueBackend(_Backend):
    name = "kqueue"

    def __init__(self, path: str, stop_fd: int, poll_interval: float) -> None:
        super().__init__(path, stop_fd, poll_interval)
        self._kq = select.kqueue()
        self._dir_fd: int | None = None
        stop_ev = select.kevent(
            stop_fd, select.KQ_FILTER_READ, select.KQ_EV_ADD | select.KQ_EV_ENABLE | select.KQ_EV_CLEAR
        )
        self._kq.control([stop_ev], 0, 0)

    def arm(self) -> bool:
        flags = getattr(os, "O_EVTONLY", None) or getattr(os, "O_RDONLY")  # noqa: B009
        try:
            fd = os.open(self.path, flags | getattr(os, "O_CLOEXEC", 0))
        except OSError:
            return False
        try:
            if not stat.S_ISDIR(os.fstat(fd).st_mode):
                os.close(fd)
                return False
            fflags = (
                select.KQ_NOTE_WRITE | select.KQ_NOTE_DELETE | select.KQ_NOTE_RENAME | select.KQ_NOTE_EXTEND
                | select.KQ_NOTE_ATTRIB | select.KQ_NOTE_REVOKE
            )  # fmt: skip
            ev = select.kevent(
                fd, select.KQ_FILTER_VNODE, select.KQ_EV_ADD | select.KQ_EV_ENABLE | select.KQ_EV_CLEAR, fflags
            )
            self._kq.control([ev], 0, 0)
        except OSError:
            with contextlib.suppress(OSError):
                os.close(fd)
            return False
        self._dir_fd = fd
        return True

    def wait(self, timeout: float | None) -> str:
        events = self._kq.control(None, 8, timeout)
        if not events:
            return _TIMEOUT
        result = _EVENT
        for ev in events:
            if ev.ident == self.stop_fd:
                return _STOP
            if ev.fflags & (select.KQ_NOTE_DELETE | select.KQ_NOTE_RENAME | select.KQ_NOTE_REVOKE):
                result = _GONE
        return result

    def disarm(self) -> None:
        if self._dir_fd is not None:
            with contextlib.suppress(OSError):
                os.close(self._dir_fd)  # closing removes the kevent
            self._dir_fd = None

    def close(self) -> None:
        self.disarm()
        with contextlib.suppress(OSError):
            self._kq.close()


class _InotifyBackend(_Backend):
    name = "inotify"

    def __init__(self, path: str, stop_fd: int, poll_interval: float) -> None:
        super().__init__(path, stop_fd, poll_interval)
        libc = ctypes.CDLL(None, use_errno=True)
        self._add_watch = libc.inotify_add_watch
        self._add_watch.argtypes = [ctypes.c_int, ctypes.c_char_p, ctypes.c_uint32]
        self._add_watch.restype = ctypes.c_int
        init = libc.inotify_init1
        init.argtypes = [ctypes.c_int]
        init.restype = ctypes.c_int
        fd = init(_IN_NONBLOCK | _IN_CLOEXEC)
        if fd < 0:
            raise OSError(ctypes.get_errno(), "inotify_init1 failed")
        self._fd = fd
        self._armed = False

    def arm(self) -> bool:
        # Drain stale events from a previous (now gone) watch.
        self._drain()
        wd = self._add_watch(self._fd, os.fsencode(self.path), _IN_MASK)
        self._armed = wd >= 0
        return self._armed

    def _drain(self) -> int:
        """Read all pending inotify events and return the OR of their masks."""
        mask_or = 0
        while True:
            try:
                data = os.read(self._fd, 65536)
            except BlockingIOError:
                break
            except OSError:
                break
            if not data:
                break
            pos = 0
            while pos + 16 <= len(data):
                _wd = int.from_bytes(data[pos : pos + 4], sys.byteorder, signed=True)
                mask = int.from_bytes(data[pos + 4 : pos + 8], sys.byteorder)
                length = int.from_bytes(data[pos + 12 : pos + 16], sys.byteorder)
                mask_or |= mask
                pos += 16 + length
        return mask_or

    def wait(self, timeout: float | None) -> str:
        ready, _, _ = select.select([self._fd, self.stop_fd], [], [], timeout)
        if self.stop_fd in ready:
            return _STOP
        if self._fd in ready:
            mask = self._drain()
            if mask & _IN_GONE:
                return _GONE
            return _EVENT
        return _TIMEOUT

    def disarm(self) -> None:
        self._armed = False

    def close(self) -> None:
        with contextlib.suppress(OSError):
            os.close(self._fd)


class _PollBackend(_Backend):
    name = "poll"

    def __init__(self, path: str, stop_fd: int, poll_interval: float) -> None:
        super().__init__(path, stop_fd, poll_interval)
        self._snap: dict[str, tuple[int, int, int]] = {}

    def _snapshot(self) -> dict[str, tuple[int, int, int]]:
        snap: dict[str, tuple[int, int, int]] = {}
        try:
            with os.scandir(self.path) as it:
                for entry in it:
                    try:
                        st = entry.stat(follow_symlinks=False)
                    except OSError:
                        continue
                    snap[entry.name] = (st.st_mtime_ns, st.st_size, st.st_ino)
            st = os.stat(self.path)
            snap["."] = (st.st_mtime_ns, 0, st.st_ino)
        except OSError:
            return {}
        return snap

    def arm(self) -> bool:
        self._snap = self._snapshot()
        return True

    def wait(self, timeout: float | None) -> str:
        interval = self.poll_interval if timeout is None else min(timeout, self.poll_interval)
        ready, _, _ = select.select([self.stop_fd], [], [], interval)
        if ready:
            return _STOP
        snap = self._snapshot()
        if snap != self._snap:
            self._snap = snap
            return _EVENT
        return _TIMEOUT


_BACKENDS: dict[str, type[_Backend]] = {"kqueue": _KqueueBackend, "inotify": _InotifyBackend, "poll": _PollBackend}


def _auto_candidates() -> list[str]:
    """Backend names to try for ``backend="auto"``, best first."""
    names = []
    if hasattr(select, "kqueue"):
        names.append("kqueue")
    elif sys.platform.startswith("linux"):
        names.append("inotify")
    names.append("poll")
    return names


class LeaseWatcher:
    """Call ``callback()`` once at start and after every (debounced) change in the directory ``path``.

    ``backend`` is ``"auto"``, ``"kqueue"``, ``"inotify"`` or ``"poll"``; after ``start()`` the ``backend``
    attribute names the one in use. The callback runs on a daemon thread; exceptions are logged to stderr and
    do not stop the watcher. If the directory is deleted or renamed away the watcher waits for it to reappear
    (checking every ``poll_interval`` seconds), re-arms and fires the callback.
    """

    def __init__(
        self,
        path: str | os.PathLike[str],
        callback: Callable[[], None],
        *,
        backend: str = "auto",
        poll_interval: float = 1.0,
        debounce: float = 0.05,
    ) -> None:
        if backend != "auto" and backend not in _BACKENDS:
            raise ValueError(f"unknown watch backend {backend!r}")
        self.path = os.fspath(path)
        self._callback = callback
        self._requested = backend
        self._poll_interval = poll_interval
        self._debounce = debounce
        self.backend: str = backend
        self._lock = threading.Lock()
        self._thread: threading.Thread | None = None
        self._stop_r = -1
        self._stop_w = -1
        self._impl: _Backend | None = None
        self._abandoned = False

    def start(self) -> None:
        """Create the directory if needed, start watching and fire the callback once. Idempotent."""
        with self._lock:
            if self._thread is not None and self._thread.is_alive():
                return
            self._abandoned = False
            os.makedirs(self.path, mode=0o700, exist_ok=True)
            self._stop_r, self._stop_w = os.pipe()
            for fd in (self._stop_r, self._stop_w):
                os.set_blocking(fd, False)
            impl = self._make_backend()
            self._impl = impl
            self.backend = impl.name
            impl.arm()
            thread = threading.Thread(target=self._run, args=(impl,), name="cli-svckit-lease-watcher", daemon=True)
            self._thread = thread
            thread.start()

    def stop(self) -> None:
        """Stop the thread (within about a second) and close all descriptors. Idempotent."""
        with self._lock:
            thread = self._thread
            if thread is None:
                return
            with contextlib.suppress(OSError):
                os.write(self._stop_w, b"x")
            if thread is not threading.current_thread():
                thread.join(1.0)
            if thread.is_alive():
                self._abandoned = True  # callback still running; the thread cleans up when it exits
                return
            self._cleanup(self._impl)

    def _cleanup(self, impl: _Backend | None) -> None:
        if impl is not None:
            impl.close()
        for fd in (self._stop_r, self._stop_w):
            if fd >= 0:
                with contextlib.suppress(OSError):
                    os.close(fd)
        self._stop_r = self._stop_w = -1
        self._impl = None
        self._thread = None

    def _make_backend(self) -> _Backend:
        if self._requested != "auto":
            return _BACKENDS[self._requested](self.path, self._stop_r, self._poll_interval)
        last: Exception | None = None
        for name in _auto_candidates():
            try:
                return _BACKENDS[name](self.path, self._stop_r, self._poll_interval)
            except (OSError, AttributeError, ValueError) as exc:
                last = exc
        raise OSError(f"no watch backend available: {last}")

    def _fire(self) -> None:
        try:
            self._callback()
        except Exception as exc:  # noqa: BLE001 - the watcher must survive callback bugs
            print(f"cli-svckit: lease watcher callback failed: {exc!r}", file=sys.stderr)

    def _wait_for_dir(self, impl: _Backend) -> bool:
        """Wait until the directory exists and is armed again; False if stop was requested."""
        impl.disarm()
        while True:
            if impl.arm():
                return True
            ready, _, _ = select.select([self._stop_r], [], [], self._poll_interval)
            if ready:
                return False

    def _run(self, impl: _Backend) -> None:
        try:
            self._loop(impl)
        finally:
            if self._abandoned:  # stop() gave up waiting for a slow callback
                self._cleanup(impl)

    def _loop(self, impl: _Backend) -> None:
        self._fire()
        while True:
            res = impl.wait(None if impl.name != "poll" else self._poll_interval)
            if res == _STOP:
                return
            if res == _TIMEOUT:
                continue
            # Coalesce a burst of events into one callback.
            deadline = time.monotonic() + max(self._debounce * 10, 0.5)
            while res != _STOP:
                if res == _GONE:
                    if not self._wait_for_dir(impl):
                        return
                    res = _EVENT
                    break
                remaining = min(self._debounce, deadline - time.monotonic())
                if remaining <= 0:
                    break
                res = impl.wait(remaining)
                if res == _TIMEOUT:
                    break
            if res == _STOP:
                return
            self._fire()
