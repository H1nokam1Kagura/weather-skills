"""A separate, unratified caseset dispatched by the verified frozen SIMKIT runner.

Closed synthetic fault operations are harness actions, never model-authored code.
The frozen onset case corpus, scorer and ratification ledger are not edited.
"""

import importlib.util
from pathlib import Path

import numpy as np
import xarray as xr

from tools.workflow.contracts import Refused, digest
from tools.workflow.persistence import atomic_json

from . import PROFILES
from .contracts import register_synthetic, require
from .controller import Controller, SyntheticRoles, WorkerDied
from .fixtures import authorize, make_sources, request_for
from .lifecycle import capture, reopen
from .science import Skills


class FaultSkills(Skills):
    def __init__(self, repo, scenario):
        super().__init__(repo)
        self.scenario, self.faults = scenario, 0

    def call(self, skill, argv):
        limit = 2 if self.scenario == "worker_exhausted" else 1
        if self.scenario in ("worker_retry", "worker_exhausted") and self.faults < limit:
            self.faults += 1
            raise WorkerDied("synthetic typed backend-exit injection; not an OS worker kill")
        super().call(skill, argv)
        if self.scenario == "replay_mismatch" and skill == "verify" and self.faults == 0:
            self.faults += 1
            path = Path(argv[argv.index("-o") + 1])
            with xr.open_zarr(path, chunks=None) as ds:
                changed = ds.load().copy(deep=True)
            changed["mae"] = changed.mae + 1
            changed.to_zarr(path, mode="w", consolidated=True)


class FaultRoles(SyntheticRoles):
    def __init__(self, request, scenario):
        super().__init__(request)
        self.scenario = scenario
        self.controller = None

    def ask(self, role, payload):
        answer = super().ask(role, payload)
        if self.scenario == "review_reject" and role == "reviewer":
            answer["verdict"] = "REJECT"
        if self.scenario == "verifier_reject" and role == "verifier":
            answer["verdict"] = "FAIL"
        if self.scenario == "final_pause" and role == "verifier":
            self.controller.pause()
        return answer


def execute_case(repo, root, case):
    from adapter_support.verification_adapter import project

    root.mkdir(parents=True, exist_ok=False)
    scenario = case["scenario"]
    request = request_for(case["profile"], windows=[10])
    if scenario == "all_windows":
        request.update(days=21, windows=[2, 5, 10])
    if scenario == "pause_resume":
        request["windows"] = [2, 10]
    if scenario == "insufficient":
        request["days"] = 10
    if scenario == "threshold":
        request.update(metrics=["brier"], threshold=20, comparator=">")
    if scenario == "mixed_history":
        request["products"].append(
            {"id": "missing-init", "initialization": "2026-01-02", "valid_start": "2026-01-03"}
        )
    authorize(request)

    def mutate(role, data):
        if role != "forecast":
            return
        if scenario == "bounds_mismatch":
            data.time_bounds.values[0, 1] += np.timedelta64(1, "D")
        elif scenario == "grid_mismatch":
            data.coords["latitude"] = [0.0, 1.0]
        elif scenario == "member_missing":
            data.precip.values[0, 0, 0, 0] = np.nan
        elif scenario == "wrong_initialization":
            data.attrs["initialization"] = "2026-02-01"

    if scenario == "history_gap":
        sources = {
            request["products"][0]["id"]: {"unavailable": "synthetic historical archive gap"}
        }
    else:
        fixture_request = dict(request, products=request["products"][:1])
        sources = make_sources(root / "sources", fixture_request, mutate)
        if scenario == "mixed_history":
            sources["missing-init"] = {"unavailable": "synthetic historical archive gap"}
    grant = register_synthetic(sources)
    roles = FaultRoles(request, scenario)
    backend = FaultSkills(repo, scenario)
    pause_sent = False

    def hook(controller):
        nonlocal pause_sent
        if (
            scenario == "pause_resume"
            and not pause_sent
            and controller.state["ledger"][0]["status"] == "VERIFIED"
        ):
            pause_sent = True
            controller.pause()

    controller = Controller(repo, request, grant, root / "run", roles, backend, hook)
    roles.controller = controller
    capture(controller)
    if scenario == "pause_before_review":
        controller.pause()
    state = controller.run()
    observed = {"first_status": state["status"], "backend_faults": backend.faults}
    if scenario == "pause_resume":
        observed["no_auto_resume"] = controller.run() == state
        first = state["ledger"][0]
        controller = reopen(repo, root / "run")
        state = controller.run(resume=True)
        observed["verified_product_preserved"] = state["ledger"][0] == first
    if scenario == "artifact_tamper":
        (Path(state["ledger"][0]["artifact"]) / "scores.json").write_text(
            "synthetic tamper", encoding="utf-8"
        )
    try:
        projection = project(repo, root / "run")
        observed["projection"] = "AVAILABLE"
    except Refused as exc:
        observed.update(projection="UNVERIFIABLE", refusal=str(exc))
        projection = None
    checks = oracle(scenario, state, projection, observed, backend)
    result = {
        "schema": "verification.case-evidence/1",
        "case_id": case["id"],
        "case_hash": digest(case),
        "scenario": scenario,
        "projection": projection,
        "observations": observed,
        "checks": checks,
        "diagnostic_verdict": "PASS" if all(checks.values()) else "FAIL",
        "oracle_status": "PROPOSED_UNRATIFIED",
        "production_release": False,
        "frozen_onset_score_eligible": False,
    }
    atomic_json(root / "case-evidence.json", result)
    return result


def oracle(scenario, state, projection, observed, backend):
    """Independent analytic expectations, not expected values copied from tool output."""
    ledger = state["ledger"]
    expected = {
        "bounds_mismatch": "BLOCKED",
        "grid_mismatch": "BLOCKED",
        "wrong_initialization": "BLOCKED",
        "review_reject": "BLOCKED",
        "verifier_reject": "BLOCKED",
        "worker_exhausted": "BLOCKED",
        "replay_mismatch": "BLOCKED",
        "insufficient": "SYNTHETIC_PARTIAL",
        "history_gap": "SYNTHETIC_PARTIAL",
        "mixed_history": "SYNTHETIC_PARTIAL",
        "pause_before_review": "PAUSED",
        "final_pause": "PAUSED",
    }.get(scenario, "SYNTHETIC_COMPLETE")
    checks = {
        "terminal": state["status"] == expected,
        "no_production_release": not state["production_release"],
    }
    if scenario == "artifact_tamper":
        checks["tamper_refused"] = projection is None and "changed" in observed.get("refusal", "")
        return checks
    checks["projection_available"] = projection is not None
    if scenario in (
        "all_windows",
        "member_missing",
        "threshold",
        "worker_retry",
        "pause_resume",
        "mixed_history",
    ):
        verified_count = 3 if scenario == "all_windows" else 2 if scenario == "pause_resume" else 1
        checks["verified_artifact_count"] = (
            sum(e["status"] == "VERIFIED" for e in ledger) == verified_count
            and projection is not None
            and len(projection["artifacts"]) == verified_count
        )
        for entry in ledger:
            if entry["status"] != "VERIFIED":
                continue
            metric = "brier" if scenario == "threshold" else "mae"
            score = entry["scores"][metric]
            window = entry["window_days"]
            count = (21 if scenario == "all_windows" else 20) // window * 4
            if scenario == "member_missing":
                count -= 1
            label = f"{entry['product']}/{window}"
            checks[label] = (
                score["forecast"] == (1 if metric == "brier" else 0.5 * window)
                and score["baseline"] == 0
                and score["paired_count"] == count
            )
        if scenario == "all_windows":
            checks["every_window"] = [e["window_days"] for e in ledger] == [2, 5, 10]
            checks["tail_reported"] = all(
                a["report"]["dropped_tail_days"] == 21 % a["window_days"]
                for a in projection["artifacts"]
            )
            kinds = [e["kind"] for e in state["events"]]
            checks["probe_precedes_work"] = kinds.index("bounded_probe") < kinds.index(
                "operation_pending"
            )
    if scenario in (
        "bounds_mismatch",
        "grid_mismatch",
        "wrong_initialization",
        "review_reject",
        "pause_before_review",
    ):
        checks["no_scientific_tools"] = not backend.receipts
    if scenario == "pause_before_review":
        checks["no_roles"] = not state["role_receipts"]
    if scenario in ("worker_retry", "worker_exhausted"):
        checks["bounded_retry"] = ledger[0]["attempts"] == 2 and ledger[0]["failures"] == (
            1 if scenario == "worker_retry" else 2
        )
    if scenario == "pause_resume":
        checks.update({k: observed[k] for k in ("no_auto_resume", "verified_product_preserved")})
        checks["first_paused"] = observed["first_status"] == "PAUSED"
    if scenario == "mixed_history":
        checks["gap_retained"] = [e["status"] for e in ledger] == ["VERIFIED", "UNAVAILABLE"]
    if scenario == "history_gap":
        checks["gap_retained"] = (
            all(e["status"] == "UNAVAILABLE" for e in ledger) and not backend.receipts
        )
    if scenario == "replay_mismatch":
        checks["replay_refused"] = "score replay differs" in state.get("reason", "")
    if scenario == "insufficient":
        checks["uncertainty_withheld"] = (
            ledger[0]["scores"]["mae"]["uncertainty"]["status"] == "INSUFFICIENT_SAMPLES"
        )
    return checks


def frozen_runner(repo, destination):
    """Extract only the verified committed package; no frozen source is rewritten."""
    spec = importlib.util.spec_from_file_location(
        "verification_frozen", repo / "tests/frozen_v27.py"
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    root = module.extract_fixture(dest=destination)
    spec = importlib.util.spec_from_file_location(
        "verification_simkit_runner", root / "simkit/runner.py"
    )
    runner = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(runner)
    return runner, module.FINAL_PINS_SHA256


def evaluate_cases(repo, root, only=None, diagnostic=False):
    from adapter_support.verification_adapter import VerificationAdapter, cases

    root = Path(root)
    selected = cases(repo, diagnostic)
    if only:
        wanted = set(only)
        require(wanted <= {c["id"] for c in selected}, "unknown case id; no cases executed")
        selected = [c for c in selected if c["id"] in wanted]
    require(bool(selected), "empty case selection")
    root.mkdir(parents=True, exist_ok=False)
    runner, pin = frozen_runner(repo, root / "frozen-runner")
    records = runner.run(
        selected,
        VerificationAdapter(repo, root / "cases", diagnostic),
        {"id": "verification-suite/v1", "profiles": list(PROFILES)},
    )
    result = {
        "schema": "verification.simkit-run/1",
        "diagnostic": diagnostic,
        "runner_pin": pin,
        "case_set_hash": digest(selected),
        "records": records,
        "executed": sum(r["status"] == "EXECUTED" for r in records),
        "not_implemented": sum(r["status"] == "NOT_IMPLEMENTED" for r in records),
        "harness_errors": sum(r["status"] == "HARNESS_ERROR" for r in records),
        "diagnostic_passes": sum(
            r["status"] == "EXECUTED" and r["evidence"]["diagnostic_verdict"] == "PASS"
            for r in records
        ),
        "oracle_status": "PROPOSED_UNRATIFIED",
        "production_release": False,
        "frozen_onset_score_eligible": False,
    }
    atomic_json(root / "results.json", result)
    return result
