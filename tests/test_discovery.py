from __future__ import annotations

import os
from importlib import metadata

import pytest

from cli_svckit.contract.discovery import participants_from_entry_points, participants_from_registry
from cli_svckit.contract.envelope import SvcError


def test_registry_parsing() -> None:
    ps = participants_from_registry(
        {
            "alpha": {
                "cmd": ["~/bin/example-tool", "--flag"],
                "timeout_seconds": 30,
                "fail_policy": "closed",
                "wait_for": "idle",
                "services": ["com.example.*"],
            },
            "beta": {"cmd": ["beta"]},
            "off": {"cmd": ["x"], "enabled": False},
        }
    )
    assert [p.name for p in ps] == ["alpha", "beta"]
    a, b = ps
    assert a.cmd == [os.path.expanduser("~/bin/example-tool"), "--flag"]
    assert a.timeout_s == 30.0 and a.fail_policy == "closed" and a.wait_for == "idle"
    assert a.services == ["com.example.*"]
    assert b.timeout_s == 120.0 and b.fail_policy == "open" and b.services == []


@pytest.mark.parametrize(
    "entry",
    [
        {},
        {"cmd": "tool"},
        {"cmd": []},
        {"cmd": ["a"], "fail_policy": "maybe"},
        {"cmd": ["a"], "timeout_seconds": -1},
        {"cmd": ["a"], "services": "x"},
        {"cmd": ["a"], "enabled": "yes"},
        "nope",
    ],
)
def test_registry_invalid(entry: object) -> None:
    with pytest.raises(SvcError) as ei:
        participants_from_registry({"bad": entry})  # type: ignore[dict-item]
    assert ei.value.code == "invalid" and ei.value.data == {"participant": "bad"}


def test_entry_points(monkeypatch: pytest.MonkeyPatch) -> None:
    class EP:
        name = "alice"

        def load(self):
            return lambda: {"cmd": ["alice-tool"], "services": ["com.example.alice"]}

    seen: list[str] = []

    def fake(*, group: str):
        seen.append(group)
        return [EP()]

    monkeypatch.setattr(metadata, "entry_points", fake)
    ps = participants_from_entry_points()
    assert seen == ["cli_svckit.participants"]
    assert ps[0].name == "alice" and ps[0].services == ["com.example.alice"]
