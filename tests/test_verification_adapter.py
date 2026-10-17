"""Admission and observed evidence for the separate verification-profile adapter."""

import json
from pathlib import Path

import numpy as np
import pytest

from adapter_support.verification_adapter import (
    CAPABILITY,
    DIAGNOSTIC,
    VerificationAdapter,
    cases,
    project,
)
from tools.verification_profiles.contracts import register_synthetic
from tools.verification_profiles.controller import Controller, SyntheticRoles
from tools.verification_profiles.evaluation import evaluate_cases
from tools.verification_profiles.fixtures import request_for
from tools.verification_profiles.lifecycle import capture, main, reopen
from tools.verification_profiles.science import paired_summary
from tools.workflow.contracts import Refused

REPO = Path(__file__).resolve().parents[1]


def gap_controller(tmp_path, roles=None):
    request = request_for()
    grant = register_synthetic({"init-20260101": {"unavailable": "synthetic fixture archive gap"}})
    controller = Controller(
        REPO, request, grant, tmp_path / "run", roles or SyntheticRoles(request)
    )
    capture(controller)
    return controller


def test_stale_pause_preserves_current_checkpoint(tmp_path):
    first = gap_controller(tmp_path)
    stale = reopen(REPO, first.root)
    complete = first.run()
    before = (first.root / "checkpoint.json").read_bytes()
    stale.pause()
    assert (first.root / "checkpoint.json").read_bytes() == before
    assert first.run() == complete
    assert stale.pending_pauses()


@pytest.mark.parametrize("action", ["pause", "deadline"])
def test_post_verifier_boundary(tmp_path, action):
    class Roles(SyntheticRoles):
        def ask(self, role, payload):
            answer = super().ask(role, payload)
            if role == "verifier":
                if action == "pause":
                    controller.pause()
                else:
                    controller.started -= 601
            return answer

    controller = gap_controller(tmp_path, Roles(request_for()))
    result = controller.run()
    assert result["status"] == ("PAUSED" if action == "pause" else "BLOCKED")
    assert not result["production_release"]


def test_cli_pause_and_resume_preserve_gates(tmp_path, capsys):
    controller = gap_controller(tmp_path)
    assert main(["pause", "--run-dir", str(controller.root)]) == 0
    assert controller.run()["status"] == "PAUSED"
    assert not controller.state["gates_complete"]
    assert main(["resume", "--run-dir", str(controller.root)]) == 1
    assert main(["resume", "--diagnostic", "--run-dir", str(controller.root)]) == 0
    state = reopen(REPO, controller.root).state
    assert state["status"] == "SYNTHETIC_PARTIAL" and state["gates_complete"]
    assert len(state["role_receipts"]) == 6


def test_projection_has_actual_roles_and_detects_tamper(tmp_path):
    controller = gap_controller(tmp_path)
    controller.run()
    evidence = project(REPO, controller.root)
    assert evidence["status"] == "SYNTHETIC_PARTIAL"
    assert len(evidence["invocation_receipts"]) == 6
    assert evidence["invocation_receipts"][-1]["output"]["verdict"] == "PASS"
    assert evidence["role_mode"] == "substituted"
    assert evidence["artifacts"] == []
    assert evidence["production_release"] is False
    (controller.root / "role-001.json").write_text("tamper", encoding="utf-8")
    with pytest.raises(Refused, match="role evidence changed"):
        project(REPO, controller.root)


def test_default_and_diagnostic_admission_are_separate(tmp_path):
    adapter = VerificationAdapter(REPO, tmp_path)
    assert adapter.implemented_capabilities() == set()
    assert adapter.admission()["status"] == "NOT_IMPLEMENTED"
    diagnostic = VerificationAdapter(REPO, tmp_path, diagnostic=True)
    assert diagnostic.implemented_capabilities() == {DIAGNOSTIC}
    assert CAPABILITY not in diagnostic.implemented_capabilities()
    with pytest.raises(Refused, match="case definition changed"):
        diagnostic.run_case(cases(REPO)[0], {})


@pytest.mark.parametrize("flag", [False, None, "true"])
def test_malformed_d4_record_cannot_skip_signature_check(tmp_path, flag):
    folder = tmp_path / "adapter_support"
    folder.mkdir()
    value = json.loads((REPO / "adapter_support/VERIFICATION-PRODUCERS.json").read_text())
    record = value["capabilities"][CAPABILITY]
    record.update(
        d4_gated=flag,
        reviewer="fake",
        reviewed_at="2026-01-01",
        reviewed_commit="NOT_A_COMMIT",
        rereview_required=None,
    )
    (folder / "VERIFICATION-PRODUCERS.json").write_text(json.dumps(value))
    assert VerificationAdapter(tmp_path).admission()["status"] == "NOT_IMPLEMENTED"


def test_frozen_runner_withholds_unsigned_cases(tmp_path):
    result = evaluate_cases(REPO, tmp_path / "admission")
    assert result["executed"] == 0 and result["not_implemented"] == 19
    assert result["diagnostic_passes"] == 0
    assert all(r["evidence"] is None for r in result["records"])
    assert result["production_release"] is False


@pytest.mark.parametrize("field", ["reviewer", "reviewed_at", "reviewed_commit"])
@pytest.mark.parametrize("value", [123, ["claimed"], "   "])
def test_signature_metadata_must_be_nonblank_strings(tmp_path, monkeypatch, field, value):
    folder = tmp_path / "adapter_support"
    folder.mkdir()
    records = json.loads((REPO / "adapter_support/VERIFICATION-PRODUCERS.json").read_text())
    record = records["capabilities"][CAPABILITY]
    record.update(
        reviewer="reviewer",
        reviewed_at="2026-10-16",
        reviewed_commit="a" * 40,
        rereview_required=None,
    )
    record[field] = value
    (folder / "VERIFICATION-PRODUCERS.json").write_text(json.dumps(records))

    def unexpected_check(*args, **kwargs):
        pytest.fail("malformed signature metadata reached the signature checker")

    monkeypatch.setattr("adapter_support.verification_adapter.check", unexpected_check)
    assert VerificationAdapter(tmp_path).admission()["status"] == "NOT_IMPLEMENTED"


def test_frozen_runner_executes_diagnostic_case(tmp_path):
    result = evaluate_cases(REPO, tmp_path / "diagnostic", ["DIAG-VP-HISTORY-GAP"], diagnostic=True)
    assert result["executed"] == result["diagnostic_passes"] == 1
    assert result["harness_errors"] == 0
    assert result["oracle_status"] == "PROPOSED_UNRATIFIED"
    assert (
        result["records"][0]["evidence"]["projection"]["product_ledger"][0]["status"]
        == "UNAVAILABLE"
    )


def test_unknown_case_creates_no_run(tmp_path):
    with pytest.raises(Refused, match="unknown case"):
        evaluate_cases(REPO, tmp_path / "bad", ["TYPO"], diagnostic=True)
    assert not (tmp_path / "bad").exists()


def test_nonconstant_paired_block_bootstrap_matches_independent_oracle():
    fc = np.array([0.0, 1.0, 3.0, 6.0, 2.0, 5.0]).reshape(6, 1, 1)
    base = np.array([2.0, 0.0, 1.0, 4.0, 0.0, 2.0]).reshape(6, 1, 1)
    config = {"block_windows": 2, "resamples": 20, "seed": 13}
    score = paired_summary(fc, base, "mae", config)
    rng = np.random.default_rng(13)
    delta = (fc - base).ravel()
    replicates = []
    for _ in range(20):
        starts = rng.integers(0, 6, size=3)
        selected = [delta[i % 6] for start in starts for i in (start, start + 1)]
        replicates.append(sum(selected) / 6)
    assert score["uncertainty"]["difference_interval"] == pytest.approx(
        np.quantile(replicates, [0.025, 0.975])
    )
    assert score["difference"] == pytest.approx(delta.mean())


def test_case_index_ids_are_unique_and_sources_are_synthetic():
    registered = cases(REPO)
    assert len({c["id"] for c in registered}) == len(registered)
    assert all(c["adapter_requirements"] == [CAPABILITY] for c in registered)
    record = json.loads((REPO / "adapter_support/VERIFICATION-PRODUCERS.json").read_text())
    assert record["capabilities"][CAPABILITY]["reviewed_commit"] is None
