"""Tests for cli_svckit.health."""

from __future__ import annotations

import http.server
import os
import socket
import subprocess
import sys
import threading
import time
from collections.abc import Iterator

import pytest

from cli_svckit.contract.envelope import load_schema
from cli_svckit.health import (
    CheckResult,
    HealthResult,
    exit_code,
    http_probe,
    pid_probe,
    port_probe,
    worst,
)


class _Handler(http.server.BaseHTTPRequestHandler):
    def do_GET(self) -> None:
        if self.path == "/slow":
            time.sleep(1.5)
        code = {"/bad": 500, "/away": 302}.get(self.path, 200)
        self.send_response(code)
        if code == 302:
            self.send_header("Location", "http://203.0.113.9/x")
        self.send_header("Content-Length", "11" if code != 302 else "0")
        self.end_headers()
        if code != 302:
            self.wfile.write(b"secret-body")

    def log_message(self, *a: object) -> None:
        pass


@pytest.fixture
def server() -> Iterator[str]:
    srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
    srv.daemon_threads = True
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{srv.server_address[1]}"
    srv.shutdown()
    srv.server_close()


def test_http_pass_fail_timeout(server: str) -> None:
    r = http_probe(server + "/ok")
    assert r.status == "pass" and r.observed_unit == "ms" and "secret" not in (r.output or "")
    assert http_probe(server + "/bad").status == "fail"
    assert http_probe(server + "/bad", expect_status=500).status == "pass"
    assert http_probe(server + "/slow", timeout=0.2).status == "fail"


def test_http_cross_host_redirect_not_followed(server: str) -> None:
    r = http_probe(server + "/away", timeout=1)
    assert r.status == "fail" and "302" in (r.output or "")


def test_http_garbage_never_raises() -> None:
    assert http_probe("not a url").status == "fail"
    assert http_probe("http://127.0.0.1:1/", timeout=0.5).status == "fail"


def test_port_probe() -> None:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.listen(1)
    assert port_probe("127.0.0.1", port).status == "pass"
    s.close()
    assert port_probe("127.0.0.1", port).status == "fail"
    assert port_probe("127.0.0.1", 99999).status == "fail"


def test_pid_probe() -> None:
    assert pid_probe(os.getpid()).status == "pass"
    p = subprocess.Popen([sys.executable, "-c", "pass"])
    p.wait()
    assert pid_probe(p.pid).status == "fail"
    assert pid_probe("x").status == "fail"  # type: ignore[arg-type]


def test_worst_and_exit_code() -> None:
    assert worst([]) == "pass"
    assert worst(["pass", "warn"]) == "warn"
    assert worst(["warn", "fail", "pass"]) == "fail"
    assert [exit_code(s) for s in ("pass", "warn", "fail")] == [0, 4, 1]


def test_roundtrip_and_schema() -> None:
    h = HealthResult(
        "warn", True, False, version="1.2.3", checks={"db:latency": [CheckResult("warn", 12.5, "ms", "slow")]}
    )
    d = h.to_dict()
    assert d["checks"]["db:latency"][0] == {
        "status": "warn",
        "observedValue": 12.5,
        "observedUnit": "ms",
        "output": "slow",
    }
    assert HealthResult.from_dict(d) == h
    jsonschema = pytest.importorskip("jsonschema")
    schema = load_schema("health")
    jsonschema.validate(d, {"$ref": "#/$defs/result", "$defs": schema["$defs"]})


def test_http_probe_ignores_env_proxy(server: str, monkeypatch: pytest.MonkeyPatch) -> None:
    for name in ("http_proxy", "HTTP_PROXY"):
        monkeypatch.setenv(name, "http://127.0.0.1:1")
    for name in ("no_proxy", "NO_PROXY"):
        monkeypatch.delenv(name, raising=False)
    assert http_probe(server + "/ok").status == "pass"


def test_http_probe_does_not_echo_url() -> None:
    r = http_probe("nourl?token=abc123")
    assert r.status == "fail" and "abc123" not in (r.output or "")


def test_from_dict_tolerates_non_dict_checks() -> None:
    assert HealthResult.from_dict({"status": "pass", "checks": []}).checks == {}
