"""Controller side of the svckit contract: call a participant tool and read its answer."""

from __future__ import annotations

import contextlib
import json
import os
import re
import subprocess
import time
import uuid
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from cli_svckit.contract.envelope import (
    CONTRACT_VERSION,
    Error,
    Request,
    Response,
    SvcError,
    parse_stdout,
)

KILL_GRACE_S = 5.0
_VERSION_RE = re.compile(r"^(\d+)\.(\d+|x)$")


@dataclass
class CallResult:
    """What one call to a participant produced. Never raised: failures are described here."""

    participant: str
    method: str
    ok: bool
    response: Response | None
    error: Error | None
    rc: int | None
    duration_s: float
    timed_out: bool


def _caller() -> dict[str, Any]:
    """Return the ``caller`` object: ``SVCKIT_CALLER`` (JSON object) if valid, else the library default."""
    raw = os.environ.get("SVCKIT_CALLER")
    if raw:
        try:
            value = json.loads(raw)
        except ValueError:
            value = None
        if isinstance(value, dict):
            return value
    return {"tool": "cli-svckit"}


def _terminate(proc: subprocess.Popen[str]) -> None:
    """Stop a child: SIGTERM, then SIGKILL after a short grace period; always reaps it."""
    with contextlib.suppress(OSError):
        proc.terminate()
    try:
        proc.communicate(timeout=KILL_GRACE_S)
    except subprocess.TimeoutExpired:
        with contextlib.suppress(OSError):
            proc.kill()
        with contextlib.suppress(Exception):
            proc.communicate(timeout=KILL_GRACE_S)


class Participant:
    """A tool that speaks the contract: ``cmd + ["svc", <method>, "--json"]`` with the request on stdin."""

    def __init__(
        self,
        name: str,
        cmd: list[str],
        *,
        timeout_s: float = 120.0,
        fail_policy: str = "open",
        wait_for: str = "interrupted",
        env: Mapping[str, str] | None = None,
        drop_env_prefixes: tuple[str, ...] = ("HERMES_KANBAN_",),
    ) -> None:
        if fail_policy not in ("open", "closed"):
            raise SvcError("invalid", "fail_policy must be 'open' or 'closed'", data={"participant": name})
        self.name = name
        self.cmd = list(cmd)
        self.timeout_s = timeout_s
        self.fail_policy = fail_policy
        self.wait_for = wait_for
        self.env = None if env is None else dict(env)
        self.drop_env_prefixes = tuple(drop_env_prefixes)
        self.services: list[str] = []

    def _clean_env(self) -> dict[str, str]:
        """Return the child environment: ``env`` (or ours) minus keys starting with a dropped prefix."""
        base = dict(os.environ if self.env is None else self.env)
        if not self.drop_env_prefixes:
            return base
        return {k: v for k, v in base.items() if not k.startswith(self.drop_env_prefixes)}

    def call(self, method: str, params: dict[str, Any] | None = None, *, timeout_s: float | None = None) -> CallResult:
        """Run one method and return what happened. Timeouts kill the child; participant faults never raise."""
        timeout = self.timeout_s if timeout_s is None else timeout_s
        request = Request(
            method=method,
            params=dict(params or {}),
            id=uuid.uuid4().hex,
            caller=_caller(),
            deadline_ms=int(timeout * 1000),
        )
        argv = [*self.cmd, "svc", method, "--json"]
        start = time.monotonic()

        def finish(
            ok: bool, response: Response | None, error: Error | None, rc: int | None, timed_out: bool = False
        ) -> CallResult:
            return CallResult(self.name, method, ok, response, error, rc, time.monotonic() - start, timed_out)

        try:
            proc = subprocess.Popen(
                argv,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                env=self._clean_env(),
                start_new_session=True,
            )
        except (OSError, ValueError) as exc:
            return finish(False, None, Error("internal", f"cannot run participant: {type(exc).__name__}"), None)
        try:
            stdout, _stderr = proc.communicate(request.to_json() + "\n", timeout=timeout)
        except subprocess.TimeoutExpired:
            _terminate(proc)
            return finish(False, None, Error("timeout", f"no answer in {timeout:g}s"), proc.returncode, True)
        except BaseException:
            _terminate(proc)
            raise
        rc = proc.returncode
        try:
            response = parse_stdout(stdout or "")
        except SvcError:
            return finish(False, None, Error("invalid", f"unparseable output (rc={rc})"), rc)
        return finish(response.ok, response, response.error, rc)

    def describe(self, cache: DescribeCache | None = None) -> dict[str, Any] | None:
        """Return the ``describe`` result (or None on failure), using ``cache`` when it is still valid."""
        if cache is not None:
            return cache.describe(self)
        return self._describe_now()

    def _describe_now(self) -> dict[str, Any] | None:
        """Ask the tool for ``describe`` and return its result object, or None."""
        res = self.call("describe", timeout_s=min(self.timeout_s, 30.0))
        if res.ok and res.response is not None and isinstance(res.response.result, dict):
            return res.response.result
        return None

    def probe_version(self) -> str | None:
        """Cheap version probe: run ``cmd --version`` and return the last word of its first line, or None."""
        try:
            done = subprocess.run(
                [*self.cmd, "--version"],
                capture_output=True,
                text=True,
                timeout=10.0,
                env=self._clean_env(),
                stdin=subprocess.DEVNULL,
            )
        except (OSError, subprocess.SubprocessError):
            return None
        if done.returncode != 0:
            return None
        lines = done.stdout.strip().splitlines()
        words = lines[0].split() if lines else []
        return words[-1] if words else None


def effective_ready(result: CallResult, fail_policy: str | None = None) -> bool | None:
    """Interpret a ``prepare`` result: True ready, False busy, None when the method is not applicable.

    A participant's own answer decides (``result.ok`` with ``ready``, or exit code 3 meaning busy). Errors and timeouts
    follow ``fail_policy`` (``open``: counts as ready, ``closed``: counts as busy); ``unsupported`` is not applicable.
    """
    policy = fail_policy or "open"
    if result.ok and result.response is not None:
        data = result.response.result or {}
        ready = data.get("ready")
        if isinstance(ready, bool):
            return ready
        return result.rc != 3
    error = result.error
    if error is not None and error.code == "unsupported":
        return None
    return policy != "closed"


class DescribeCache:
    """JSON file cache of ``describe`` results keyed by participant name, command and tool version."""

    def __init__(
        self,
        path: str | os.PathLike[str],
        *,
        max_age_s: float = 3600.0,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self.path = Path(path)
        self.max_age_s = max_age_s
        self.clock = clock

    def _load(self) -> dict[str, Any]:
        try:
            data = json.loads(self.path.read_text())
        except (OSError, ValueError):
            return {}
        return data if isinstance(data, dict) else {}

    def _save(self, data: dict[str, Any]) -> None:
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.path.with_name(self.path.name + f".{os.getpid()}.tmp")
            tmp.write_text(json.dumps(data))
            os.replace(tmp, self.path)
        except OSError:
            pass

    @staticmethod
    def _key(p: Participant) -> str:
        return json.dumps([p.name, p.cmd])

    def describe(self, p: Participant) -> dict[str, Any] | None:
        """Return the cached describe result when valid, else fetch, store and return a fresh one."""
        data = self._load()
        key = self._key(p)
        entry = data.get(key)
        now = self.clock()
        if isinstance(entry, dict) and isinstance(entry.get("result"), dict):
            age = now - float(entry.get("at", 0.0))
            if 0 <= age <= self.max_age_s:
                return entry["result"]
            version = p.probe_version()
            if version is not None and version == entry.get("version"):
                entry["at"] = now
                data[key] = entry
                self._save(data)
                return entry["result"]
        result = p._describe_now()
        if result is None:
            return None
        tool = result.get("tool")
        version = tool.get("version") if isinstance(tool, dict) else None
        if not isinstance(version, str):
            version = p.probe_version()
        data[key] = {"at": now, "version": version, "result": result}
        self._save(data)
        return result


def negotiate(contract: dict[str, Any]) -> str:
    """Check our contract version is within the participant's ``[min, max]`` (max may be ``1.x``); returns it."""

    def parse(value: Any, allow_x: bool) -> tuple[int, float]:
        m = _VERSION_RE.match(str(value))
        if not m or (m.group(2) == "x" and not allow_x):
            raise SvcError("incompatible", f"bad contract version {value!r}")
        return int(m.group(1)), (float("inf") if m.group(2) == "x" else int(m.group(2)))

    if not isinstance(contract, dict) or "min" not in contract or "max" not in contract:
        raise SvcError("incompatible", "participant did not report a contract range")
    ours = parse(CONTRACT_VERSION, False)
    lo = parse(contract["min"], False)
    hi = parse(contract["max"], True)
    if not (lo <= ours <= hi):
        raise SvcError(
            "incompatible",
            f"contract {CONTRACT_VERSION} is outside {contract['min']}..{contract['max']}",
            data={"ours": CONTRACT_VERSION, "min": contract["min"], "max": contract["max"]},
        )
    return CONTRACT_VERSION
