"""Tests for cli_svckit.doctor."""

from __future__ import annotations

import time

from cli_svckit.doctor import Check, DoctorItem, run_checks


def ok() -> DoctorItem:
    return DoctorItem("ok", "fine")


def slow() -> DoctorItem:
    time.sleep(3)
    return ok()


def test_aggregate_and_order() -> None:
    checks = [
        Check("a", "A", ok),
        Check("b", "B", lambda: DoctorItem("warn", "meh", {"hint": "do x"}), area="net"),
        Check("c", "C", lambda: DoctorItem("skip", "n/a")),
    ]
    rep = run_checks(checks)
    assert rep.status == "warn" and rep.exit_code() == 4
    d = rep.to_dict()
    assert [c["id"] for c in d["checks"]] == ["a", "b", "c"]
    assert d["checks"][1]["fix"] == {"hint": "do x"} and d["checks"][1]["area"] == "net"


def test_all_ok_and_empty() -> None:
    assert run_checks([Check("a", "A", ok)]).exit_code() == 0
    assert run_checks([]).status == "ok"


def test_exception_is_fail_without_traceback() -> None:
    def boom() -> DoctorItem:
        raise ValueError("bad thing")

    rep = run_checks([Check("a", "A", boom)])
    assert rep.status == "fail" and rep.exit_code() == 1
    assert rep.outcomes[0].item.detail == "ValueError: bad thing"


def test_deadline_enforced() -> None:
    start = time.monotonic()
    rep = run_checks([Check("slow", "S", slow, deadline_s=0.2), Check("b", "B", ok)])
    assert time.monotonic() - start < 2
    assert rep.outcomes[0].item.status == "fail"
    assert rep.outcomes[0].item.detail == "timed out after 0.2s"
    assert rep.outcomes[1].item.status == "ok"


def test_timed_out_check_frees_slot() -> None:
    start = time.monotonic()
    rep = run_checks([Check("slow", "S", slow, deadline_s=0.1), Check("b", "B", ok)], max_workers=1)
    assert time.monotonic() - start < 2
    assert [o.item.status for o in rep.outcomes] == ["fail", "ok"]
