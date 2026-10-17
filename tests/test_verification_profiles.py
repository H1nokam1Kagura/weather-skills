"""Synthetic process checks, not forecast-skill or independent-agent evidence."""

import json
from pathlib import Path

import numpy as np
import pytest

from tools.verification_profiles.contracts import register_synthetic, validate
from tools.verification_profiles.controller import Controller, SyntheticRoles, WorkerDied
from tools.verification_profiles.fixtures import authorize, make_sources, request_for
from tools.verification_profiles.science import Skills, inspect, paired_summary
from tools.workflow.contracts import Refused, digest
from tools.workflow.persistence import atomic_json

REPO = Path(__file__).resolve().parents[1]


def setup(tmp_path, request=None, mutate=None, backend=None, roles=None, hook=None):
    request = request or request_for(windows=[10])
    grant = register_synthetic(make_sources(tmp_path / "sources", request, mutate))
    controller = Controller(
        REPO,
        request,
        grant,
        tmp_path / "run",
        roles or SyntheticRoles(request),
        backend=backend,
        on_operation=hook,
    )
    return controller


@pytest.mark.parametrize("profile", ["harvest-verification/v1", "gefs-history/v1"])
def test_real_skills_all_windows(tmp_path, profile):
    controller = setup(tmp_path, request_for(profile, days=21))
    result = controller.run()
    assert result["status"] == "SYNTHETIC_COMPLETE", result.get("reason")
    assert result["production_release"] is False
    assert len(result["role_receipts"]) == 6
    assert (
        result["probes"]["init-20260101"]["classification"]
        == "synthetic_probe_not_remote_benchmark"
    )
    kinds = [event["kind"] for event in result["events"]]
    assert kinds.index("bounded_probe") < kinds.index("operation_pending")
    for entry in result["ledger"]:
        w = entry["window_days"]
        score = entry["scores"]["mae"]
        assert score["forecast"] == pytest.approx(0.5 * w)
        assert score["baseline"] == 0
        assert score["paired_count"] == (21 // w) * 4
        assert score["uncertainty"]["difference_interval"] == [0.5 * w] * 2
        report = json.loads((Path(entry["artifact"]) / "scores.json").read_text())
        assert report["dropped_tail_days"] == 21 % w
        assert len(report["tool_receipts"]) == 8
        assert report["crop_readiness"].startswith("NOT_ASSESSED")
    assert controller.run() == result


@pytest.mark.parametrize(
    "fault", ["units", "bounds", "initialization", "grid", "members", "period"]
)
def test_support_refuses_before_scoring(tmp_path, fault):
    def mutate(role, ds):
        if role != "forecast":
            return
        if fault == "units":
            ds.precip.attrs["units"] = "m"
        elif fault == "period":
            ds.precip.attrs["aggregation_period"] = "2 day"
        elif fault == "bounds":
            ds.time_bounds.values[0, 1] += np.timedelta64(1, "D")
        elif fault == "initialization":
            ds.attrs["initialization"] = "2026-01-02"
        elif fault == "grid":
            ds.coords["latitude"] = [0.0, 1.0]
        elif fault == "members":
            ds.coords["number"] = [0, 0, 2]

    controller = setup(tmp_path, mutate=mutate)
    result = controller.run()
    assert result["status"] == "BLOCKED"
    assert not controller.backend.receipts


def test_common_daily_mask(tmp_path):
    def mutate(role, ds):
        if role == "forecast":
            ds.precip.values[0, 0, 0, 0] = np.nan

    controller = setup(tmp_path, mutate=mutate)
    datasets, probe = inspect(
        controller.request,
        controller.request["products"][0],
        controller.grant.record["init-20260101"],
    )
    assert probe["paired_daily_cells"] == 79
    assert np.isnan(datasets["reference"].precip.values[0, 0, 0])
    assert np.isnan(datasets["baseline"].precip.values[0, 0, 0]).all()


def test_request_binding_and_threshold():
    request = request_for()
    request["days"] = 21
    with pytest.raises(Refused, match="authorization"):
        validate(request)
    request.update(metrics=["brier"], threshold=10, comparator=">=")
    with pytest.raises(Refused, match="comparator"):
        validate(authorize(request))


class DyingSkills(Skills):
    def __init__(self, deaths):
        super().__init__(REPO)
        self.deaths = deaths

    def call(self, skill, argv):
        if self.deaths:
            self.deaths -= 1
            raise WorkerDied("injected backend worker exit")
        return super().call(skill, argv)


@pytest.mark.parametrize("deaths,expected", [(1, "SYNTHETIC_COMPLETE"), (2, "BLOCKED")])
def test_bounded_worker_retry(tmp_path, deaths, expected):
    result = setup(tmp_path, backend=DyingSkills(deaths)).run()
    assert result["status"] == expected, result.get("reason")
    assert result["ledger"][0]["attempts"] == 2
    assert result["ledger"][0]["failures"] == deaths
    assert (tmp_path / "run/product-0-attempt-1").exists()
    assert (tmp_path / "run/product-0-attempt-2").exists()


def test_pause_resume_reuses_only_verified(tmp_path):
    def hook(controller):
        if controller.state["ledger"][0]["status"] == "VERIFIED":
            controller.pause()

    controller = setup(tmp_path, request_for(windows=[2, 10]), hook=hook)
    paused = controller.run()
    assert paused["status"] == "PAUSED"
    assert controller.run() == paused
    resumed = Controller(
        REPO,
        controller.request,
        controller.grant,
        controller.root,
        SyntheticRoles(controller.request),
    )
    result = resumed.run(resume=True)
    assert result["status"] == "SYNTHETIC_COMPLETE", result.get("reason")
    assert result["ledger"][0] == paused["ledger"][0]
    assert result["ledger"][1]["attempts"] == 1
    (Path(result["ledger"][0]["artifact"]) / "scores.json").write_text("tampered")
    with pytest.raises(Refused, match="artifact changed"):
        resumed.run()


def test_pause_before_review_does_not_bypass_gates(tmp_path):
    controller = setup(tmp_path)
    controller.pause()
    assert controller.run()["status"] == "PAUSED"
    assert not controller.state["role_receipts"]
    result = controller.run(resume=True)
    assert result["status"] == "SYNTHETIC_COMPLETE"
    assert [r["role"] for r in result["role_receipts"]] == ["compiler"] * 3 + [
        "planner",
        "reviewer",
        "verifier",
    ]


class RejectingRoles(SyntheticRoles):
    def __init__(self, request, target):
        super().__init__(request)
        self.target = target

    def ask(self, role, payload):
        answer = super().ask(role, payload)
        if role == self.target:
            answer["verdict"] = "REJECT"
        return answer


@pytest.mark.parametrize("role", ["reviewer", "verifier"])
def test_failed_gate_cannot_complete(tmp_path, role):
    request = request_for(windows=[10])
    controller = setup(tmp_path, request, roles=RejectingRoles(request, role))
    assert controller.run()["status"] == "BLOCKED"
    with pytest.raises(Refused, match="blocked run"):
        controller.run(resume=True)


def test_unavailable_initialization_explicit(tmp_path):
    request = request_for("gefs-history/v1")
    grant = register_synthetic(
        {"init-20260101": {"unavailable": "synthetic archive gap for requested init"}}
    )
    result = Controller(REPO, request, grant, tmp_path / "run", SyntheticRoles(request)).run()
    assert result["status"] == "SYNTHETIC_PARTIAL"
    assert len(result["ledger"]) == 3
    assert all(e["status"] == "UNAVAILABLE" for e in result["ledger"])
    assert not result["probes"]


def test_insufficient_uncertainty_is_partial(tmp_path):
    result = setup(tmp_path, request_for(days=10, windows=[10])).run()
    assert result["status"] == "SYNTHETIC_PARTIAL"
    assert result["ledger"][0]["scores"]["mae"]["uncertainty"]["status"] == "INSUFFICIENT_SAMPLES"


def test_pooled_rmse_and_paired_mask():
    forecast = np.array([1.0, 9.0, np.nan]).reshape(3, 1, 1)
    baseline = np.array([0.0, 0.0, 100.0]).reshape(3, 1, 1)
    score = paired_summary(
        forecast, baseline, "rmse", {"block_windows": 1, "resamples": 20, "seed": 17}
    )
    assert score["forecast"] == pytest.approx(np.sqrt(5))
    assert score["baseline"] == 0
    assert score["paired_count"] == 2


def test_global_missing_day_excludes_whole_window_and_brier_boundary(tmp_path):
    request = request_for(days=30, windows=[10])
    request.update(metrics=["brier", "rmse", "crps", "bias"], threshold=20, comparator=">")
    authorize(request)

    def mutate(role, ds):
        if role == "forecast":
            ds.precip.values[0] = np.nan

    result = setup(tmp_path, request, mutate=mutate).run()
    assert result["status"] == "SYNTHETIC_COMPLETE", result.get("reason")
    scores = result["ledger"][0]["scores"]
    assert all(s["paired_count"] == 8 for s in scores.values())
    assert scores["brier"]["forecast"] == 1  # reference equals threshold: strictly > is false
    assert scores["brier"]["baseline"] == 0
    for metric in ("rmse", "crps", "bias"):
        assert scores[metric]["forecast"] == pytest.approx(5)
        assert scores[metric]["baseline"] == 0


def test_unclean_checkpoint_requires_resume_and_retains_budget(tmp_path):
    controller = setup(tmp_path)
    controller.state.update(status="RUNNING", elapsed_seconds=600)
    controller.save()
    assert controller.run()["status"] == "INTERRUPTED"
    assert not controller.roles.receipts
    result = controller.run(resume=True)
    assert result["status"] == "BLOCKED"
    assert "time limit" in result["reason"]
    assert not controller.roles.receipts


def test_source_tamper_and_wrong_resume_request(tmp_path):
    controller = setup(tmp_path)
    changed = dict(controller.request, windows=[5])
    authorize(changed)
    with pytest.raises(Refused, match="changed on resume"):
        Controller(REPO, changed, controller.grant, controller.root, SyntheticRoles(changed))
    source = Path(controller.grant.record["init-20260101"]["forecast"]["path"])
    (source / "tampered.txt").write_text("changed")
    with pytest.raises(Refused, match="source changed"):
        controller.run()


def test_checkpoint_digest_rejected(tmp_path):
    controller = setup(tmp_path)
    value = json.loads((controller.root / "checkpoint.json").read_text())
    value["payload"]["status"] = "SYNTHETIC_COMPLETE"
    assert digest(value["payload"]) != value["digest"]
    atomic_json(controller.root / "checkpoint.json", value)
    with pytest.raises(Refused, match="checkpoint was changed"):
        controller.run()
