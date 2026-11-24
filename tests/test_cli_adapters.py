"""Tests for the argparse and typer adapters."""

from __future__ import annotations

import argparse
import io
import json
from typing import Any

import pytest

from cli_svckit.contract.argparse_cli import add_svc_subparser, main
from cli_svckit.contract.envelope import EXIT_BUSY, EXIT_OK


class Provider:
    def health(self, params: dict[str, Any]) -> dict[str, Any]:
        return {"status": "pass", "token": "s3"}

    def prepare(self, params: dict[str, Any]) -> dict[str, Any]:
        return {"state": "busy"}


def test_argparse_subparser(monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    parser = argparse.ArgumentParser()
    sub = parser.add_subparsers()
    add_svc_subparser(sub, Provider())
    monkeypatch.setattr("sys.stdin", io.StringIO('{"id":"a1"}'))
    ns = parser.parse_args(["svc", "health", "--json"])
    assert ns.func(ns) == EXIT_OK
    out = json.loads(capsys.readouterr().out)
    assert out["id"] == "a1" and out["result"]["token"] == "***"


def test_argparse_main(monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    monkeypatch.setattr("sys.stdin", io.StringIO(""))
    assert main(Provider(), ["prepare", "--json"]) == EXIT_BUSY
    assert json.loads(capsys.readouterr().out)["result"]["state"] == "busy"


def test_typer_adapter() -> None:
    pytest.importorskip("typer")
    from typer.testing import CliRunner

    from cli_svckit.contract.typer_cli import make_svc_app

    app = make_svc_app(Provider())
    runner = CliRunner()
    res = runner.invoke(app, ["health", "--json"], input='{"id":"t1"}')
    assert res.exit_code == EXIT_OK
    assert json.loads(res.output.strip().splitlines()[-1])["id"] == "t1"
    res = runner.invoke(app, ["prepare", "--json"], input="")
    assert res.exit_code == EXIT_BUSY
    res = runner.invoke(app, ["install", "--json"])
    assert res.exit_code != 0  # not implemented by the provider: no such command
