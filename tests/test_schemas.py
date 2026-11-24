"""The shipped JSON Schemas load, are valid 2020-12, and the examples in docs/contract.md validate against them."""

from __future__ import annotations

import json
import pathlib
import re

import pytest

from cli_svckit.contract import Op, Request, Response, load_schema

ROOT = pathlib.Path(__file__).resolve().parents[1]
DOC = ROOT / "docs" / "contract.md"
SCHEMA_DIR = ROOT / "src" / "cli_svckit" / "contract" / "schemas"
NAMES = ("envelope", "describe", "health", "doctor", "install", "prepare", "complete", "lease")
EXAMPLE = re.compile(r"<!--\s*schema:\s*([a-z_.]+)\s*-->\s*\n```json\n(.*?)\n```", re.S)


def _examples() -> list[tuple[str, str]]:
    return EXAMPLE.findall(DOC.read_text(encoding="utf-8"))


def _registry():
    referencing = pytest.importorskip("referencing")
    resources = [(s["$id"], referencing.Resource.from_contents(s)) for s in map(load_schema, NAMES)]
    return referencing.Registry().with_resources(resources)


def _validator(tag: str):
    jsonschema = pytest.importorskip("jsonschema")
    name, _, part = tag.partition(".")
    base = load_schema(name)["$id"]
    ref = f"{base}#/$defs/{part}" if part else base
    return jsonschema.Draft202012Validator({"$ref": ref}, registry=_registry())


def test_all_eight_schemas_ship_and_load():
    assert sorted(p.stem for p in SCHEMA_DIR.glob("*.json")) == sorted(NAMES)
    for name in NAMES:
        schema = load_schema(name)
        assert schema["$schema"] == "https://json-schema.org/draft/2020-12/schema"
        assert schema["$id"].endswith(f"/{name}.json")
        assert load_schema(f"{name}.json") == schema


def test_load_schema_rejects_unknown_and_path_names():
    with pytest.raises(FileNotFoundError):
        load_schema("nope")
    with pytest.raises(ValueError):
        load_schema("../envelope")


def test_schemas_are_valid_2020_12():
    jsonschema = pytest.importorskip("jsonschema")
    for name in NAMES:
        jsonschema.Draft202012Validator.check_schema(load_schema(name))


def test_schemas_never_forbid_unknown_fields():
    def walk(node):
        if isinstance(node, dict):
            assert node.get("additionalProperties") is not False
            assert "unevaluatedProperties" not in node
            for v in node.values():
                walk(v)
        elif isinstance(node, list):
            for v in node:
                walk(v)

    for name in NAMES:
        walk(load_schema(name))


def test_doc_has_an_example_per_method_and_they_parse():
    tags = [t for t, _ in _examples()]
    for needed in (
        "describe.result",
        "health.result",
        "doctor.params",
        "doctor.result",
        "install.params",
        "install.result",
        "prepare.params",
        "prepare.result",
        "complete.params",
        "complete.result",
        "envelope.request",
        "envelope.response",
        "lease",
    ):
        assert needed in tags, needed
    for tag, body in _examples():
        name, _, part = tag.partition(".")
        schema = load_schema(name)
        assert not part or part in schema["$defs"], tag
        assert isinstance(json.loads(body), dict), tag
    bodies = [json.loads(b) for _, b in _examples()]
    assert any(b.get("state") == "busy" for b in bodies)
    assert any(b.get("ok") is False for b in bodies)


def test_doc_examples_validate():
    pytest.importorskip("jsonschema")
    for tag, body in _examples():
        errors = list(_validator(tag).iter_errors(json.loads(body)))
        assert not errors, f"{tag}: {errors[0].message}"


def test_doc_examples_load_into_dataclasses():
    for tag, body in _examples():
        d = json.loads(body)
        if tag == "envelope.response":
            assert Response.from_dict(d).to_dict()["ok"] == d["ok"]
        elif tag == "envelope.request":
            assert Request.from_dict(d).to_dict() == d
        elif tag == "lease.op":
            assert Op.from_dict(d).to_dict() == d


def test_dataclass_output_validates():
    pytest.importorskip("jsonschema")
    op = Op(id="op-20260101T000000-abcd", kind="restart", targets=["com.example.a"], affected_pids=None)
    assert not list(_validator("lease.op").iter_errors(op.to_dict()))
    req = Request("prepare", {"op": op.to_dict(), "poll": 0}, "r1")
    assert not list(_validator("envelope.request").iter_errors(req.to_dict()))
    assert not list(_validator("prepare.params").iter_errors(req.params))
    ok = Response("r1", True, {"state": "ready", "op_id": op.id, "quiesced": True, "in_flight": []})
    assert not list(_validator("envelope.response").iter_errors(ok.to_dict()))
    assert ok.result is not None
    assert not list(_validator("prepare.result").iter_errors(ok.result))


def test_schemas_reject_obvious_mistakes():
    pytest.importorskip("jsonschema")
    assert list(_validator("envelope.response").iter_errors({"svckit": "1.0", "id": "x", "ok": False}))
    busy_typo = {"state": "waiting", "op_id": "o", "quiesced": False, "in_flight": []}
    assert list(_validator("prepare.result").iter_errors(busy_typo))
    assert list(_validator("install.params").iter_errors({"dry_run": True, "confirm": False, "scope": "everything"}))
