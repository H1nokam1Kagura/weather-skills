"""Only direct stub diagnostics and actual Simkit admission refusal; no scoring."""

import copy
import hashlib
import json
import sys
from pathlib import Path

import pytest

# Explicit, test-only import location; no package bridge or Simkit modification.
SIMKIT_ROOT = Path("C:/Users/neilha/.agentbus/simkit-v2_7-closure-20261011")
sys.path.insert(0, str(SIMKIT_ROOT))

from simkit.adapter import Adapter  # noqa: E402 - explicit pinned test/package bootstrap
from simkit.runner import run  # noqa: E402 - explicit pinned test/package bootstrap

from adapter_support.weather_adapter import (  # noqa: E402 - explicit pinned test/package bootstrap
    EvidenceMappingError,
    WeatherAdapter,
)
from tools.workflow.tests.test_review_evidence import (  # noqa: E402 - explicit pinned test/package bootstrap
    ReviewReached,
    make_controller,
)


def accepted_controller(tmp_path, monkeypatch, change=None):
    controller, process = make_controller(tmp_path, monkeypatch, change=change)
    with pytest.raises(ReviewReached):
        controller._run()
    return controller, process


def test_direct_accepted_stub_preserves_observed_review_input(tmp_path, monkeypatch):
    controller, process = accepted_controller(tmp_path, monkeypatch)
    adapter = WeatherAdapter()
    assert isinstance(adapter, Adapter)
    diagnostic = adapter.capture_review_diagnostics(controller)
    assert diagnostic["kind"] == "DIRECT_DIAGNOSTIC_NOT_ADMITTED_SIMKIT_CASE"
    actual = process.calls[-1]["payload"]["review_evidence"]
    snapshots = diagnostic["evidence"]["harness_observations"]["review_snapshots"]
    (snapshot,) = snapshots
    assert snapshot == {
        "reviewer_occurrence": 1,
        **{
            key: actual[key]
            for key in (
                "request_hash",
                "goal_hash",
                "plan_hash",
                "manifest_hash",
                "source_hash",
                "plan_claims",
                "sources",
            )
        },
    }
    assert diagnostic["provenance"][0]["accepted_review"] is True
    assert diagnostic["provenance"][0]["native_receipt"] == controller.roles.receipts[-1]
    assert diagnostic["evidence"]["controller_trace"]["final_state"] == "REVIEW_APPROVED"
    assert "invocation_receipts" not in diagnostic["evidence"]
    assert "terminal" not in diagnostic["evidence"]["controller_trace"]
    assert "claim_checks" not in diagnostic["evidence"]["controller_trace"]
    assert "role_output" not in diagnostic["evidence"]
    assert "CAP-BOUND-SOURCES" not in adapter.implemented_capabilities()
    assert diagnostic["capabilities"]["CAP-BOUND-SOURCES"]["status"] == "NOT_IMPLEMENTED"
    snapshot["sources"][0]["text"] = "mutated return value"
    assert adapter.capture_review_diagnostics(controller)["evidence"] != diagnostic["evidence"]


@pytest.mark.parametrize("change", ["missing_source", "malformed_source", "wrong_binding"])
def test_mapper_refuses_missing_or_malformed_observed_binding(tmp_path, monkeypatch, change):
    controller, _ = accepted_controller(tmp_path, monkeypatch)
    path = controller.root / "review-capture-001.json"
    observed = json.loads(path.read_text())
    if change == "missing_source":
        del observed["review_evidence"]["sources"]
    elif change == "malformed_source":
        observed["review_evidence"]["sources"][0]["source_hash"] = "sha256:wrong"
    else:
        observed["review_evidence"]["binding_hash"] = "wrong"
    # Keep native file/checkpoint hashes coherent to test semantic mapping checks,
    # not merely filesystem tamper detection. No successful receipt is fabricated.
    controller.save(path.name, observed)
    controller.checkpoint()
    with pytest.raises(EvidenceMappingError):
        WeatherAdapter().capture_review_diagnostics(controller)


def test_uncommitted_acceptance_marker_is_not_approval(tmp_path, monkeypatch):
    controller, _ = accepted_controller(tmp_path, monkeypatch)
    path = controller.root / "review-capture-001.json"
    observed = json.loads(path.read_text())
    observed["committed_transition"]["index"] = -1
    controller.save(path.name, observed)
    controller.checkpoint()
    with pytest.raises(EvidenceMappingError, match="index invalid"):
        WeatherAdapter().capture_review_diagnostics(controller)


def test_rejected_and_repaired_reviews_preserve_distinct_actual_bindings(tmp_path, monkeypatch):
    controller, _ = accepted_controller(tmp_path, monkeypatch, change="reject_once")
    diagnostic = WeatherAdapter().capture_review_diagnostics(controller)
    snapshots = diagnostic["evidence"]["harness_observations"]["review_snapshots"]
    assert [s["reviewer_occurrence"] for s in snapshots] == [1, 2]
    assert [p["accepted_review"] for p in diagnostic["provenance"]] == [False, True]
    assert snapshots[0]["plan_claims"] != snapshots[1]["plan_claims"]


def test_failed_dispatch_is_not_silently_dropped_or_given_a_receipt(tmp_path, monkeypatch):
    controller, _ = make_controller(tmp_path, monkeypatch, change="failed_dispatch")
    with pytest.raises(RuntimeError, match="stub dispatch failed"):
        controller._run()
    with pytest.raises(EvidenceMappingError, match="dispatch/receipt correlation unavailable"):
        WeatherAdapter().capture_review_diagnostics(controller)


def test_real_simkit_blocks_required_caps_before_run_case():
    class DispatchSpy(WeatherAdapter):
        calls = 0

        def run_case(self, case, profile):
            self.calls += 1
            raise AssertionError("unsupported case must never dispatch")

    adapter = DispatchSpy()
    required = ["CAP-BOUND-SOURCES", "CAP-RECEIPTS", "CAP-HARNESS-OBS"]
    # Fresh structural local request, not a frozen case or a claimed case pass.
    case = {"id": "LOCAL-CAPABILITY-ADMISSION-NEGATIVE", "adapter_requirements": required}
    (record,) = run([case], adapter, profile={})
    assert record["status"] == "NOT_IMPLEMENTED"
    assert record["missing_capabilities"] == sorted(required)
    assert record["evidence"] is None and adapter.calls == 0
    assert WeatherAdapter().can_run() is False


def test_manifest_only_extension_name_is_not_implemented():
    adapter = WeatherAdapter()
    declared = copy.deepcopy(adapter.extension_methods)
    assert declared["CAP-BOUND-SOURCES"] == "capture_bound_sources"
    assert not hasattr(adapter, "capture_bound_sources")
    assert "CAP-BOUND-SOURCES" not in adapter.implemented_capabilities()
    assert adapter.implemented_capabilities() == {
        "CAP-NOT-IMPLEMENTED",
        "CAP-CONTROLLER-HASH",
        "CAP-REPAIR-CONFIG",
        "CAP-DISPOSABLE-FIXTURE",
        "CAP-GATE-REPORT-PATCH",
        "CAP-SCHEDULE-DEPENDENCIES",
        "CAP-REVIEW-APPROVAL-HOOK",
    }


def test_dependency_dispatch_methods_report_observed_dispositions_only():
    from adapter_support.weather_adapter import AdapterRefusal
    from tools.workflow.e2e_injections import InjectionHooks

    schedule = [
        {
            "id": "approve-1",
            "hook": "after_review_approval",
            "operation": "replace_role_output",
            "selector": "reviewer/1",
            "payload": {},
            "requires": [],
        },
        {
            "id": "after-approve",
            "hook": "after_transition",
            "operation": "replace_role_output",
            "selector": "REVIEW_APPROVED/1",
            "payload": {},
            "requires": ["approve-1"],
        },
    ]
    hooks = InjectionHooks({"injection_schedule": schedule})
    adapter = WeatherAdapter()
    report = adapter.dispatch_dependent_schedule(hooks)
    assert [e["disposition"] for e in report["entries"]] == ["INACTIVE", "INACTIVE"]
    assert {e["reason"] for e in report["entries"]} == {"boundary_not_reached"}
    approval = adapter.dispatch_after_accepted_review(hooks)
    assert [e["id"] for e in approval["entries"]] == ["approve-1"]
    assert approval["accepted_approvals"] == 0
    with pytest.raises(AdapterRefusal):
        adapter.dispatch_dependent_schedule(object())


def test_definition_symbol_resolves_only_exact_reviewed_whole_registry(tmp_path, monkeypatch):
    controller, _ = accepted_controller(tmp_path, monkeypatch)
    adapter = WeatherAdapter()
    raw = (controller.repo / "skills/onset-date/references/onset_definitions.toml").read_bytes()
    symbol = "HARNESS_SOURCE_HASH:definition.agrhymet-sos-rolling"
    assert adapter.resolve_review_diagnostic_symbol(controller, symbol, 1) == (
        "sha256:" + hashlib.sha256(raw).hexdigest()
    )
    with pytest.raises(EvidenceMappingError):
        adapter.resolve_review_diagnostic_symbol(controller, symbol, 2)
    with pytest.raises(EvidenceMappingError):
        adapter.resolve_review_diagnostic_symbol(controller, "HARNESS_SOURCE_HASH:invented", 1)
    assert "CAP-SYMBOLS" not in adapter.implemented_capabilities()
    assert "CAP-BOUND-SOURCES" not in adapter.implemented_capabilities()
