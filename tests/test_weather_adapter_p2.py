"""P2 adapter capabilities: pure local, no model, no network, no case scoring."""

import collections
import copy
import hashlib
import json
import math
import sys
from pathlib import Path
from types import SimpleNamespace

import numpy as np
import pytest
import xarray as xr

# Explicit, test-only import location; no package bridge or Simkit modification.
SIMKIT_ROOT = Path("C:/Users/neilha/.agentbus/simkit-v2_7-closure-20261011")
sys.path.insert(0, str(SIMKIT_ROOT))

from simkit.adapter import CAPABILITY_METHODS  # noqa: E402 - explicit pinned test bootstrap

from adapter_support.weather_adapter import (  # noqa: E402 - explicit pinned test bootstrap
    SOURCE_HASH_ALGORITHM,
    AdapterRefusal,
    WeatherAdapter,
)
from tools.workflow.catalog import tree_hash  # noqa: E402 - explicit pinned test bootstrap
from tools.workflow.contracts import Refused, digest  # noqa: E402 - explicit pinned test bootstrap
from tools.workflow.e2e_injections import (  # noqa: E402 - explicit pinned test bootstrap
    InjectionHooks,
    UnsupportedInjection,
)
from tools.workflow.runner import (  # noqa: E402 - explicit pinned test bootstrap
    Controller,
    read_gate,
)

FROZEN_CASES = (
    SIMKIT_ROOT / "casesets/cached-onset-e2e-v2_7/frozen/agentic-e2e-injections.DEV-ONLY.json"
)
# FINAL-PINS.json entry for the frozen DEV-ONLY case set.
FROZEN_CASES_SHA256 = "9c3ea347454c76410e2e73adfb24aabca70018f1e7c3fd6bf4a4f11bfac241b0"
IMPLEMENTED = {
    "CAP-NOT-IMPLEMENTED",
    "CAP-CONTROLLER-HASH",
    "CAP-REPAIR-CONFIG",
    "CAP-DISPOSABLE-FIXTURE",
    "CAP-GATE-REPORT-PATCH",
    "CAP-SCHEDULE-DEPENDENCIES",
    "CAP-REVIEW-APPROVAL-HOOK",
}


@pytest.fixture(scope="module")
def frozen():
    raw = FROZEN_CASES.read_bytes()
    assert hashlib.sha256(raw).hexdigest() == FROZEN_CASES_SHA256
    return json.loads(raw)


def case_by_id(frozen, case_id):
    (case,) = [c for c in frozen["cases"] if c["id"] == case_id]
    return copy.deepcopy(case)


# ---- introspection


def test_implemented_capabilities_gain_exactly_the_p2_set():
    implemented = WeatherAdapter().implemented_capabilities()
    assert implemented == IMPLEMENTED
    for absent in ("CAP-SIGNAL-CAPTURE", "CAP-RUN-PROVENANCE", "CAP-PROFILE", "CAP-BOUND-SOURCES"):
        assert absent not in implemented
    frozen_table = {c for c in IMPLEMENTED if c in CAPABILITY_METHODS} - {"CAP-NOT-IMPLEMENTED"}
    assert {CAPABILITY_METHODS[c] for c in frozen_table} == {
        "controller_goal_hash",
        "configure_repair",
        "prepare_fixture",
        "patch_gate_report",
    }
    # Capabilities absent from the frozen table are admitted only via extension_methods.
    assert IMPLEMENTED - frozen_table - {"CAP-NOT-IMPLEMENTED"} == {
        "CAP-SCHEDULE-DEPENDENCIES",
        "CAP-REVIEW-APPROVAL-HOOK",
    }
    assert not hasattr(WeatherAdapter, "capture_bound_sources")
    assert WeatherAdapter().can_run() is False


# ---- CAP-CONTROLLER-HASH


def test_goal_hash_is_the_controller_digest_and_key_order_invariant(frozen):
    goal = frozen["reference_fixture"]["goal"]
    adapter = WeatherAdapter()
    assert adapter.controller_goal_hash(goal) == digest(goal)
    reordered = dict(reversed(list(goal.items())))
    assert list(reordered) != list(goal)
    assert adapter.controller_goal_hash(reordered) == digest(goal)
    expected = hashlib.sha256(
        json.dumps(goal, sort_keys=True, allow_nan=False).encode()
    ).hexdigest()
    assert adapter.controller_goal_hash(goal) == expected


def test_goal_hash_refuses_nan_and_non_goal_inputs(frozen):
    goal = copy.deepcopy(frozen["reference_fixture"]["goal"])
    goal["bbox"][0] = math.nan
    with pytest.raises(ValueError):
        WeatherAdapter().controller_goal_hash(goal)
    controller = SimpleNamespace(goal=frozen["reference_fixture"]["goal"])
    for value in (controller, None, "goal", [goal]):
        with pytest.raises(AdapterRefusal):
            WeatherAdapter().controller_goal_hash(value)


def test_authorized_goal_hash_is_not_the_compiled_goal_hash(frozen):
    authorized = frozen["reference_fixture"]["goal"]
    compiled = {**authorized, "title": "a compiler-broadened title"}
    current = InjectionHooks({}).resolve(
        "HARNESS_GOAL_HASH:current", SimpleNamespace(goal=compiled)
    )
    assert current == digest(compiled)
    assert WeatherAdapter().controller_goal_hash(authorized) != current


# ---- CAP-REPAIR-CONFIG


def test_repair_limit_read_from_every_frozen_case_budget(frozen):
    adapter = WeatherAdapter()
    limits = [adapter.configure_repair(case) for case in frozen["cases"]]
    assert limits == [case["budget"]["max_repair_attempts"] for case in frozen["cases"]]
    assert collections.Counter(limits) == {0: 48, 1: 18}
    assert all(type(value) is int for value in limits)


@pytest.mark.parametrize("value", [True, False, 2, -1, 1.0, "1", None])
def test_unsupported_repair_limits_refused(frozen, value):
    case = copy.deepcopy(frozen["cases"][0])
    case["budget"]["max_repair_attempts"] = value
    with pytest.raises(AdapterRefusal):
        WeatherAdapter().configure_repair(case)


@pytest.mark.parametrize("case", [{}, {"budget": {}}, {"budget": None}, None, []])
def test_missing_repair_limit_refused(case):
    with pytest.raises(AdapterRefusal):
        WeatherAdapter().configure_repair(case)


# ---- CAP-DISPOSABLE-FIXTURE


@pytest.fixture
def source(tmp_path):
    """Pre-generated synthetic source, outside every case root."""
    values = np.arange(30 * 2 * 2, dtype="float32").reshape(30, 2, 2) / 10
    values[:, 1, 1] = np.nan
    ds = xr.Dataset(
        {"precip": (("time", "latitude", "longitude"), values, {"units": "mm day-1"})},
        coords={
            "time": np.arange("2026-01-01", "2026-01-31", dtype="datetime64[D]").astype(
                "datetime64[ns]"
            ),
            "latitude": np.array([1.05, 1.0]),
            "longitude": np.array([30.0, 30.05]),
        },
        attrs={"dataset": "chirps", "source_lineage": "chirps-fetch"},
    )
    directory = tmp_path / "generated"
    directory.mkdir()
    path = directory / "fixture.nc"
    ds.to_netcdf(path, engine="scipy")
    return path


GENERATOR = {
    "synthetic": True,
    "generator": "unit-test-grid/1",
    "generator_sha256": hashlib.sha256(b"unit-test-grid/1").hexdigest(),
    "seed": 17,
}


def prepare(source, case_root, case_id="DEV-E2E-V2_7-E01-P1-INJ", adapter=None, **overrides):
    args = dict(source=source, generator_provenance=GENERATOR, presented_fetch_version="0.0.2")
    args.update(overrides)
    return (adapter or WeatherAdapter()).prepare_fixture({"id": case_id}, case_root, **args)


def test_fixture_layout_disjoint_fresh_and_hash_named(source, tmp_path):
    before = source.read_bytes()
    prepared = prepare(source, tmp_path / "case-a")
    root = Path(prepared["case_root"])
    assert sorted(p.name for p in root.iterdir()) == ["harness", "run", "source"]
    assert Path(prepared["source"]).parent == Path(prepared["source_directory"])
    assert [p.name for p in Path(prepared["source_directory"]).iterdir()] == ["source.zarr"]
    assert list(Path(prepared["run"]).iterdir()) == []
    assert [p.name for p in Path(prepared["harness"]).iterdir()] == ["manifest.json"]
    assert prepared["source_pre"] == {
        "algorithm": SOURCE_HASH_ALGORITHM,
        "value": tree_hash(prepared["source"]),
    }
    assert SOURCE_HASH_ALGORITHM == f"{tree_hash.__module__}.{tree_hash.__name__}"
    assert prepared["source_pre"]["value"] == tree_hash(prepared["source"])
    manifest = json.loads((Path(prepared["harness"]) / "manifest.json").read_text())
    assert manifest == prepared["manifest"]
    assert manifest["source"]["sha256"] == hashlib.sha256(before).hexdigest()
    assert prepared["generator_identity"] == {
        "generator": GENERATOR["generator"],
        "generator_sha256": GENERATOR["generator_sha256"],
        "stated_by": "caller",
    }
    assert prepared["not_declared"] == ["CAP-RUN-PROVENANCE"]
    assert source.read_bytes() == before


def test_source_hash_stable_across_identical_cases(source, tmp_path):
    first = prepare(source, tmp_path / "case-a")
    second = prepare(source, tmp_path / "case-b", case_id="DEV-E2E-V2_7-E01-P1-CTL")
    assert first["source_pre"]["value"] == second["source_pre"]["value"]
    assert first["source"] != second["source"]
    assert tree_hash(first["source"]) == first["source_pre"]["value"]


def test_two_cases_cannot_share_a_root_and_reuse_is_refused(source, tmp_path):
    adapter = WeatherAdapter()
    first = prepare(source, tmp_path / "case-a", adapter=adapter)
    snapshot = {str(p): p.read_bytes() for p in Path(first["case_root"]).rglob("*") if p.is_file()}
    with pytest.raises(FileExistsError):
        prepare(source, tmp_path / "case-a", case_id="OTHER", adapter=adapter)
    with pytest.raises(AdapterRefusal, match="inside another"):
        prepare(source, tmp_path / "case-a/run/nested", case_id="OTHER", adapter=adapter)
    with pytest.raises(AdapterRefusal, match="inside another"):
        prepare(source, tmp_path / "case-a/source/nested", case_id="OTHER", adapter=adapter)
    assert snapshot == {
        str(p): p.read_bytes() for p in Path(first["case_root"]).rglob("*") if p.is_file()
    }


def test_existing_empty_root_is_refused_unchanged(source, tmp_path):
    (tmp_path / "case-a").mkdir()
    with pytest.raises(FileExistsError):
        prepare(source, tmp_path / "case-a")
    assert list((tmp_path / "case-a").iterdir()) == []


@pytest.mark.parametrize("root", ["case-a/../case-b", "relative-case"])
def test_dotdot_and_relative_roots_refused(source, tmp_path, root):
    candidate = tmp_path / root if ".." in root else Path(root)
    with pytest.raises(AdapterRefusal):
        prepare(source, candidate)
    assert not (tmp_path / "case-b").exists() and not Path(root).exists()


@pytest.mark.skipif(sys.platform != "win32", reason="junction is a Windows reparse point")
def test_junction_escape_refused(source, tmp_path):
    import _winapi

    real = tmp_path / "elsewhere"
    real.mkdir()
    _winapi.CreateJunction(str(real), str(tmp_path / "link"))
    with pytest.raises(Refused, match="reparse"):
        prepare(source, tmp_path / "link/case-a")
    assert list(real.iterdir()) == []


def test_symlink_escape_refused(source, tmp_path):
    real = tmp_path / "elsewhere"
    real.mkdir()
    try:
        (tmp_path / "link").symlink_to(real, target_is_directory=True)
    except OSError as exc:  # unprivileged Windows cannot create symlinks
        pytest.skip(f"symlink creation unavailable: {exc}")
    with pytest.raises(Refused):
        prepare(source, tmp_path / "link/case-a")
    assert list(real.iterdir()) == []


@pytest.mark.parametrize(
    "provenance",
    [
        {**GENERATOR, "generator_sha256": "not-a-hash"},
        {k: v for k, v in GENERATOR.items() if k != "generator_sha256"},
        {**GENERATOR, "synthetic": False},
        {**GENERATOR, "generator": ""},
    ],
)
def test_untruthful_or_unpinned_generator_refused(source, tmp_path, provenance):
    with pytest.raises(ValueError):
        prepare(source, tmp_path / "case-a", generator_provenance=provenance)
    assert not (tmp_path / "case-a/source/source.zarr").exists()


@pytest.mark.parametrize("case", [{}, {"id": ""}, None])
def test_case_identity_required(source, tmp_path, case):
    with pytest.raises(AdapterRefusal):
        WeatherAdapter().prepare_fixture(
            case,
            tmp_path / "case-a",
            source=source,
            generator_provenance=GENERATOR,
            presented_fetch_version="0.0.2",
        )
    assert not (tmp_path / "case-a").exists()


def test_prepared_fixture_accepted_by_real_source_inspector(source, tmp_path):
    prepared = prepare(source, tmp_path / "case-a")
    controller = object.__new__(Controller)
    controller.source = Path(prepared["source"])
    controller.admission = None
    controller.goal = {
        "start_date": "2026-01-01",
        "end_date": "2026-01-30",
        "bbox": [1.05, 30.0, 1.0, 30.05],
    }
    metadata = controller.inspect_source()
    assert metadata["source"] == "chirps"
    assert metadata["units"] == "mm day-1"
    assert tree_hash(prepared["source"]) == prepared["source_pre"]["value"]


# ---- CAP-GATE-REPORT-PATCH


def gate_controller(tmp_path):
    outputs = tmp_path / "run/artifacts"
    return SimpleNamespace(
        paths={
            "source": tmp_path / "source/source.zarr",
            "onset": outputs / "onset.zarr",
            "map": outputs / "onset.png",
        },
        goal={"task": "onset_map"},
        root=tmp_path / "run",
    )


def verify_event(controller, name, stdout=None):
    report = {
        "schema": "verify-run.gate/1",
        "exit_code": 0,
        "verdict": "PASS",
        "checks": [{"kind": "replay", "status": "PASS"}],
        "artifact": str(controller.paths[name]),
    }
    process = {
        "exit_code": 0,
        "stdout": json.dumps(report) if stdout is None else stdout,
        "stderr": "",
    }
    argv = ["--input", str(controller.paths[name]), "--require-replay", "--format", "json"]
    return {"state": "MAP_CREATED", "skill": "verify-run", "argv": argv, "process": process}


def test_gate_report_patch_delivers_only_selected_report(frozen, tmp_path):
    case = case_by_id(frozen, "DEV-E2E-V2_7-E08-P1-INJ")
    hooks, controller, adapter = InjectionHooks(case), gate_controller(tmp_path), WeatherAdapter()

    onset = verify_event(controller, "onset")
    untouched = onset["process"]["stdout"]
    result = adapter.patch_gate_report(hooks, onset, controller)
    assert onset["process"]["stdout"] == untouched
    assert result["records"] == []
    assert result["delivered"] is False and result["delivered_report_hash"] is None
    read_gate(onset["process"], "verify-run.gate/1", controller.paths["onset"], replay=True)

    selected = verify_event(controller, "map")
    raw = copy.deepcopy(selected["process"])
    read_gate(raw, "verify-run.gate/1", controller.paths["map"], replay=True)
    result = adapter.patch_gate_report(hooks, selected, controller)
    delivered = json.loads(selected["process"]["stdout"])
    assert delivered["artifact"] == str(controller.paths["onset"])
    (record,) = result["records"]
    assert record["index"] == 0 and record["status"] == "APPLIED"
    assert record["before_hash"] == digest(json.loads(raw["stdout"]))
    assert record["after_hash"] == digest(delivered)
    assert (
        result["raw_report_hash"] == "sha256:" + hashlib.sha256(raw["stdout"].encode()).hexdigest()
    )
    assert result["delivered_report_hash"] == (
        "sha256:" + hashlib.sha256(selected["process"]["stdout"].encode()).hexdigest()
    )
    assert result["raw_report_hash"] != result["delivered_report_hash"]
    with pytest.raises(Refused, match="identity"):
        read_gate(selected["process"], "verify-run.gate/1", controller.paths["map"], replay=True)

    again = verify_event(controller, "map")
    unchanged = again["process"]["stdout"]
    assert adapter.patch_gate_report(hooks, again, controller)["records"] == []
    assert again["process"]["stdout"] == unchanged
    assert hooks.coverage()["applied"] == [0] and hooks.coverage()["unreached"] == []


def test_non_json_report_is_unsupported_not_patched(frozen, tmp_path):
    hooks = InjectionHooks(case_by_id(frozen, "DEV-E2E-V2_7-E08-P1-INJ"))
    controller = gate_controller(tmp_path)
    event = verify_event(controller, "map", stdout="not a gate document")
    with pytest.raises(UnsupportedInjection, match="not a gate JSON"):
        WeatherAdapter().patch_gate_report(hooks, event, controller)
    assert event["process"]["stdout"] == "not a gate document"
    (record,) = hooks.coverage()["unsupported"]
    assert record["index"] == 0


@pytest.mark.parametrize("case_id", ["DEV-E2E-V2_7-E08-P1-CTL", "DEV-E2E-V2_7-E01-P1-INJ"])
def test_non_patch_after_tool_entries_refused_before_delegation(frozen, tmp_path, case_id):
    hooks = InjectionHooks(case_by_id(frozen, case_id))
    controller = gate_controller(tmp_path)
    event = verify_event(controller, "map")
    before = event["process"]["stdout"]
    with pytest.raises(AdapterRefusal, match="not a gate-report JSON Patch"):
        WeatherAdapter().patch_gate_report(hooks, event, controller)
    assert event["process"]["stdout"] == before and hooks.records == []


def test_gate_report_patch_requires_real_hooks_and_observed_process(frozen, tmp_path):
    controller = gate_controller(tmp_path)
    adapter = WeatherAdapter()
    with pytest.raises(AdapterRefusal, match="InjectionHooks"):
        adapter.patch_gate_report(lambda *a: None, verify_event(controller, "map"), controller)
    hooks = InjectionHooks(case_by_id(frozen, "DEV-E2E-V2_7-E08-P1-INJ"))
    for event in ({"skill": "verify-run"}, {"process": {"stdout": None}}, None):
        with pytest.raises(AdapterRefusal, match="observed after_tool process"):
            adapter.patch_gate_report(hooks, event, controller)
    assert hooks.records == []
