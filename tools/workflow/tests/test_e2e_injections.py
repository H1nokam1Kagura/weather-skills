import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from tools.workflow.e2e_injections import (
    InjectedInterruption,
    InjectionHooks,
    UnsupportedInjection,
    json_patch,
    unsupported_requirements,
)


def spec(operation="json_patch", payload=None, hook="after_role", selector="planner", occurrence=1):
    return {
        "hook": hook,
        "selector": {"role_or_tool_or_stage": selector, "occurrence": occurrence},
        "operation": operation,
        "payload": [] if payload is None else payload,
    }


def hooks(*schedule):
    return InjectionHooks({"id": "test", "injection_schedule": list(schedule)})


def test_patch_strict_and_atomic():
    original = {"a": [1, 2]}
    assert json_patch(original, [{"op": "add", "path": "/a/-", "value": 3}]) == {"a": [1, 2, 3]}
    with pytest.raises(UnsupportedInjection):
        json_patch(original, [{"op": "replace", "path": "/missing", "value": 3}])
    with pytest.raises(UnsupportedInjection):
        json_patch(original, [{"op": "add", "path": "/a/-1", "value": 3}])
    assert original == {"a": [1, 2]}


def test_compiler_identity_and_once():
    h = hooks(spec(payload=[{"op": "replace", "path": "/x", "value": 9}], selector="compiler_2"))
    answers = [{"x": 0} for _ in range(4)]
    for answer in answers:
        h("after_role", {"role": "compiler", "answer": answer}, None)
    assert answers == [{"x": 0}, {"x": 9}, {"x": 0}, {"x": 0}]
    assert h.coverage()["applied"] == [0]


def test_planner_steps_mapping_receipted():
    h = hooks(spec(payload=[{"op": "replace", "path": "/0/args/title", "value": "bad"}]))
    answer = {"steps": [{"args": {"title": "good"}}], "final": ["map"]}
    h("after_role", {"role": "planner", "answer": answer}, None)
    assert answer["steps"][0]["args"]["title"] == "bad"
    assert h.records[0]["adapter_mapping"]


def test_missing_input_channel_is_unsupported_not_applied():
    h = hooks(
        spec(
            hook="before_role",
            selector="compiler_1",
            payload=[{"op": "add", "path": "/source_metadata/hint", "value": "bad"}],
        )
    )
    with pytest.raises(UnsupportedInjection):
        h("before_role", {"role": "compiler", "payload": {"messages": []}}, None)
    assert h.coverage()["applied"] == []
    assert len(h.coverage()["unsupported"]) == 1


def test_artifact_selector_and_multiple_same_boundary_operations():
    schedule = spec(
        hook="after_tool",
        selector="verify-run(map)",
        payload=[
            {"op": "replace", "path": "/artifact", "value": "HARNESS_RESOLVED_ARTIFACT_PATH:onset"}
        ],
    )
    h = hooks(schedule)
    c = SimpleNamespace(paths={"onset": Path("onset.zarr"), "map": Path("onset.png")})
    onset = {"stdout": json.dumps({"artifact": "onset.zarr"})}
    h("after_tool", {"skill": "verify-run", "argv": ["--input", "onset.zarr"], "process": onset}, c)
    result = {"stdout": json.dumps({"artifact": "onset.png"})}
    h("after_tool", {"skill": "verify-run", "argv": ["--input", "onset.png"], "process": result}, c)
    assert json.loads(result["stdout"])["artifact"] == "onset.zarr"
    assert h.coverage()["applied"] == [0]


def test_interrupt_processes_resume_request_before_raising():
    h = hooks(
        spec("interrupt", {"pending_operation": "none"}, "after_transition", "ONSET_CHECKED"),
        spec("request_transition", {"action": "resume"}, "after_transition", "ONSET_CHECKED"),
    )
    with pytest.raises(InjectedInterruption):
        h("after_transition", {"state": "ONSET_CHECKED"}, SimpleNamespace(pending=None))
    assert h.resume_requested
    assert h.coverage()["applied"] == [0, 1]
    h("after_transition", {"state": "ONSET_CHECKED"}, SimpleNamespace(pending=None))


def test_unknown_symbol_never_guessed():
    h = hooks(spec("replace_role_output", {"hash": "HARNESS_HASH:source_previous_snapshot"}))
    with pytest.raises(UnsupportedInjection):
        h("after_role", {"role": "planner", "answer": {}}, None)
    assert h.coverage()["unsupported"]


def test_static_capabilities_and_unreached_not_pass():
    case = {"injection_schedule": [spec("inject_metric", {"metric": "free_ram_gib", "value": 1.8})]}
    assert unsupported_requirements(case)
    assert InjectionHooks(case).coverage()["unreached"] == [0]


def test_outside_fixture_mutation_refused(tmp_path):
    h = InjectionHooks({}, disposable_root=tmp_path / "owned")
    with pytest.raises(UnsupportedInjection):
        h._owned(tmp_path / "other.zarr")


def test_onset_mutation_changes_first_valid_cell_only(tmp_path):
    import numpy as np
    import zarr

    path = tmp_path / "onset.zarr"
    group = zarr.open_group(str(path), mode="w", zarr_format=2)
    missing = np.iinfo("int64").min
    values = np.array([[missing, 0], [1, 2]], dtype="int64")
    array = group.create_array("onset_precip_date", data=values, fill_value=missing)
    array.attrs["units"] = "days since 2025-09-16 00:00:00"
    h = InjectionHooks({}, disposable_root=tmp_path)
    record = {}
    h._fixture(
        {"fixture_operation": "FX-ONSET-CELL-SHIFT", "args": {"days": 1}},
        {},
        SimpleNamespace(paths={"onset": path}),
        record,
    )
    expected = values.copy()
    expected[0, 1] = 1
    np.testing.assert_array_equal(array[:], expected)
    assert record["before_hash"] != record["after_hash"]


def test_error_does_not_become_success():
    h = hooks(
        spec(
            "mutate_fixture",
            {"fixture_operation": "FX-REPORT-RESERIALIZE", "args": {}},
            "after_tool",
            "verify-run",
        )
    )
    with pytest.raises(json.JSONDecodeError):
        h("after_tool", {"skill": "verify-run", "process": {"stdout": "not json"}}, None)
    assert h.coverage()["errors"]
    assert not h.coverage()["applied"]


def test_source_hash_symbol_resolves_against_the_bound_sources():
    """E13-P3-INJ: an injected claim citing HARNESS_SOURCE_HASH:definition.agrhymet-sos-rolling
    resolves through claim_gates.resolve_source_symbol to the controller's bound source, with
    no symbol table passed in (role_substitution passes none)."""
    symbol = "HARNESS_SOURCE_HASH:definition.agrhymet-sos-rolling"
    sources = [
        {"source_id": "definition:agrhymet-sos-rolling", "source_hash": "sha256:" + "a" * 64},
        {"source_id": "documentation:onset-date", "source_hash": "sha256:" + "b" * 64},
    ]
    patch = [{"op": "replace", "path": "/0/args/title", "value": symbol}]
    h = hooks(spec(payload=patch))
    controller = SimpleNamespace(
        paths={}, context={"review_evidence": {"sources": sources}}, root=Path("run")
    )
    answer = {"steps": [{"args": {"title": "good"}}], "final": ["map"]}
    h("after_role", {"role": "planner", "answer": answer}, controller)
    assert answer["steps"][0]["args"]["title"] == "sha256:" + "a" * 64
    assert h.coverage()["applied"] == [0]
    # Before the review sources are bound, the bound reference definition answers.
    bound = {"definition_source": {**sources[0], "path": "x", "symbol": "s"}}
    early = SimpleNamespace(paths={}, context={"review_evidence": bound})
    assert hooks().resolve({"h": symbol}, early) == {"h": "sha256:" + "a" * 64}
    # A symbol naming no bound source, or no controller sources at all, is never delivered.
    with pytest.raises(UnsupportedInjection, match="unresolved trusted symbol"):
        hooks().resolve("HARNESS_SOURCE_HASH:definition.unknown", controller)
    with pytest.raises(UnsupportedInjection, match="unresolved trusted symbol"):
        hooks().resolve(symbol, SimpleNamespace(paths={}))
    # An explicitly supplied symbol still wins.
    explicit = InjectionHooks({"id": "t", "injection_schedule": []}, symbols={symbol: "given"})
    assert explicit.resolve(symbol, controller) == "given"
