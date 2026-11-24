"""Health results (IETF health-check draft style) and probes that never raise."""

from __future__ import annotations

import socket
import time
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Iterable
from dataclasses import dataclass, field
from typing import Any

from cli_svckit.contract.envelope import EXIT_FAIL, EXIT_OK, EXIT_WARN
from cli_svckit.procs import pid_alive

STATUSES = ("pass", "warn", "fail")


@dataclass
class CheckResult:
    """The outcome of one probe: a status plus optional measured value and note."""

    status: str
    observed_value: Any = None
    observed_unit: str | None = None
    output: str | None = None

    def to_dict(self) -> dict[str, Any]:
        """Return the wire form (camelCase keys, unset fields omitted)."""
        d: dict[str, Any] = {"status": self.status}
        if self.observed_value is not None:
            d["observedValue"] = self.observed_value
        if self.observed_unit is not None:
            d["observedUnit"] = self.observed_unit
        if self.output is not None:
            d["output"] = self.output
        return d

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> CheckResult:
        """Build from the wire form; unknown keys are ignored."""
        return cls(d.get("status", "fail"), d.get("observedValue"), d.get("observedUnit"), d.get("output"))


@dataclass
class HealthResult:
    """Overall health: status, liveness, readiness and named groups of check results."""

    status: str
    live: bool
    ready: bool
    version: str | None = None
    output: str | None = None
    checks: dict[str, list[CheckResult]] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        """Return the wire form matching ``health.json``."""
        d: dict[str, Any] = {"status": self.status, "live": self.live, "ready": self.ready}
        if self.version is not None:
            d["version"] = self.version
        if self.output is not None:
            d["output"] = self.output
        d["checks"] = {name: [c.to_dict() for c in items] for name, items in self.checks.items()}
        return d

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> HealthResult:
        """Build from the wire form; unknown keys are ignored."""
        checks_in = d.get("checks")
        if not isinstance(checks_in, dict):
            checks_in = {}
        checks = {
            name: [CheckResult.from_dict(c) for c in items if isinstance(c, dict)]
            for name, items in checks_in.items()
            if isinstance(items, list)
        }
        return cls(
            status=d.get("status", "fail"),
            live=bool(d.get("live", False)),
            ready=bool(d.get("ready", False)),
            version=d.get("version"),
            output=d.get("output"),
            checks=checks,
        )


def worst(statuses: Iterable[str]) -> str:
    """Return the worst status (fail > warn > pass); unknown values count as warn; ``pass`` when empty."""
    result = "pass"
    for s in statuses:
        if s == "fail":
            return "fail"
        if s != "pass":
            result = "warn"
    return result


def exit_code(status: str) -> int:
    """Map a health status to a process exit code (pass 0, warn 4, anything else 1)."""
    return {"pass": EXIT_OK, "warn": EXIT_WARN}.get(status, EXIT_FAIL)


class _SameHostRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req: Any, fp: Any, code: int, msg: str, headers: Any, newurl: str) -> Any:
        if urllib.parse.urlsplit(newurl).netloc not in ("", urllib.parse.urlsplit(req.full_url).netloc):
            return None
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def http_probe(url: str, timeout: float = 5.0, expect_status: int = 200) -> CheckResult:
    """GET ``url`` and pass when the status equals ``expect_status``; records latency in ms. Never raises.

    Redirects to another host are not followed, and response bodies are never read or reported.
    """
    start = time.monotonic()
    try:
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), _SameHostRedirect)
        with opener.open(urllib.request.Request(url, method="GET"), timeout=timeout) as resp:
            code = resp.status
    except urllib.error.HTTPError as exc:
        code = exc.code
    except Exception as exc:
        # Only the exception type: messages such as "unknown url type" echo the whole URL (maybe a token).
        return CheckResult("fail", output=type(exc).__name__)
    ms = round((time.monotonic() - start) * 1000, 1)
    if code == expect_status:
        return CheckResult("pass", ms, "ms", f"HTTP {code}")
    return CheckResult("fail", ms, "ms", f"HTTP {code}, expected {expect_status}")


def port_probe(host: str, port: int, timeout: float = 2.0) -> CheckResult:
    """Pass when a TCP connection to ``host:port`` succeeds. Never raises."""
    start = time.monotonic()
    try:
        with socket.create_connection((host, port), timeout=timeout):
            pass
    except Exception as exc:
        return CheckResult("fail", output=f"{type(exc).__name__}: {exc}"[:200])
    return CheckResult("pass", round((time.monotonic() - start) * 1000, 1), "ms", "connected")


def pid_probe(pid: int) -> CheckResult:
    """Pass when process ``pid`` is alive. Never raises."""
    try:
        alive = pid_alive(pid)
    except Exception as exc:
        return CheckResult("fail", output=f"{type(exc).__name__}: {exc}"[:200])
    return CheckResult("pass", output="running") if alive else CheckResult("fail", output="not running")
