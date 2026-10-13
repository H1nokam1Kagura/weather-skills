"""P8 evidence projection on synthetic finished runs, validated by the frozen v2.7 scorer.

The real Controller runs in a temp directory: a staged synthetic CHIRPS-identical fixture
(adapter prepare_fixture), the real source inspector, real Roles over a deterministic role CLI
stub, and the real process manager launching tiny local stand-in tool scripts. No model,
network or paid call. The frozen run_evidence operands are read from the pinned DEV-ONLY case
file; the frozen score.py validates the projected evidence.
"""

import copy
import hashlib
import json
import sys
from pathlib import Path
from types import SimpleNamespace

import numpy as np
import pytest
import xarray as xr

SIMKIT_ROOT = Path("C:/Users/neilha/.agentbus/simkit-v2_7-closure-20261011")
sys.path.insert(0, str(SIMKIT_ROOT))

from simkit import score  # noqa: E402 - explicit pinned test bootstrap
from simkit.adapter import Adapter  # noqa: E402 - explicit pinned test bootstrap
from simkit.runner import run as simkit_run  # noqa: E402 - explicit pinned test bootstrap

from adapter_support import evidence_projection as ep  # noqa: E402
from adapter_support.weather_adapter import AdapterRefusal, WeatherAdapter  # noqa: E402
from tools.workflow import agents, runner  # noqa: E402
from tools.workflow.catalog import CATALOG  # noqa: E402
from tools.workflow.contracts import digest, expected_plan  # noqa: E402
from tools.workflow.processes import Processes  # noqa: E402
from tools.workflow.tests.test_claim_gates_v27 import (  # noqa: E402
    FROZEN_SOURCE_HASH,
    FROZEN_VERBATIM_QUOTES,
    REFERENCE_DEFINITION,
    citation,
    role_of,
)

FROZEN_CASES = (
    SIMKIT_ROOT / "casesets/cached-onset-e2e-v2_7/frozen/agentic-e2e-injections.DEV-ONLY.json"
)
FROZEN_CASES_SHA256 = "9c3ea347454c76410e2e73adfb24aabca70018f1e7c3fd6bf4a4f11bfac241b0"
GENERATOR = {
    "synthetic": True,
    "generator": "evidence-projection-test-grid/1",
    "generator_sha256": hashlib.sha256(b"evidence-projection-test-grid/1").hexdigest(),
    "seed": 23,
}
# The frozen reference goal (profile cached-onset/v1); a tiny 2x2 synthetic grid spans its bbox.
MAP_GOAL = {
    "task": "onset_map",
    "bbox": [5, 36.5, -5, 42],
    "start_date": "2025-09-01",
    "end_date": "2025-12-31",
    "dataset": "chirps",
    "variable": "precip",
    "definition_ref": "agrhymet-sos-rolling",
    "missing_policy": "preserve_nan",
    "baseline": None,
    "ensemble_claim": False,
    "title": "Eastern Kenya: 2025 short rains",
}
DATA_GOAL = {**MAP_GOAL, "task": "onset_data", "title": None}
TOOL_SCRIPT = '''
import json, pathlib, sys
argv = sys.argv[1:]
name = pathlib.Path(sys.argv[0]).stem
def value(flag):
    return argv[argv.index(flag) + 1]
if name == "onset_date":
    out = pathlib.Path(value("--output"))
    out.mkdir()
    (out / "values.bin").write_bytes(b"synthetic onset stand-in")
elif name == "plot_onset":
    pathlib.Path(value("--output")).write_bytes(b"synthetic map stand-in")
else:
    checks = [{"status": "PASS"}] if name == "check_artifact" else [{"kind": "replay", "status": "PASS"}]
    schema = "check-artifact.gate/1" if name == "check_artifact" else "verify-run.gate/1"
    print(json.dumps({"schema": schema, "artifact": value("--input"), "verdict": "PASS",
                      "exit_code": 0, "checks": checks}))
'''


@pytest.fixture(scope="module")
def frozen():
    raw = FROZEN_CASES.read_bytes()
    assert hashlib.sha256(raw).hexdigest() == FROZEN_CASES_SHA256
    return json.loads(raw)


def operands(frozen, case_id):
    (case,) = [c for c in frozen["cases"] if c["id"] == case_id]
    return {
        a["id"]: a["predicate"]["operand"]
        for a in case["assertions"]
        if isinstance(a.get("predicate"), dict) and a["predicate"].get("operator") == "run_evidence"
    }


def findings(frozen, case_id, evidence):
    return {
        name: score.run_evidence_findings(spec, evidence)
        for name, spec in operands(frozen, case_id).items()
    }


def fails(found):
    return [m for s, m in found if s == "FAIL"]


# --------------------------------------------------------------------------- run harness


def last_eligible(goal):
    return str(np.datetime64(goal["end_date"]) - np.timedelta64(29, "D"))


def claims(goal):
    return [
        {
            "claim_id": "C-WINDOW",
            "text": "Each candidate onset date needs a complete 30-day window inside the input.",
            "citations": [citation(FROZEN_VERBATIM_QUOTES[0])],
            "derivable": False,
        },
        {
            "claim_id": "C-LAST-ELIGIBLE",
            "text": "The last eligible onset date is the input end date minus 29 days.",
            "citations": [citation(FROZEN_VERBATIM_QUOTES[1])],
            "derivable": True,
            "computes": {"fact": "last_eligible_onset_date", "value": last_eligible(goal)},
        },
    ]


class RoleCli:
    """Deterministic role CLI behind the real agents.Roles (no model is launched)."""

    total_seconds = 1800

    def __init__(self, goal, planner=None, reviewer=None, checker=None, fail_roles=()):
        self.goal, self.calls = goal, []
        self.planner, self.reviewer, self.checker = planner, reviewer, checker
        self.fail_roles = set(fail_roles)

    def run(self, command, workspace, environment, stdin):
        role = role_of(command)
        payload = json.loads(stdin)
        self.calls.append(role)
        if role in self.fail_roles:
            return {"exit_code": 1, "stderr": "stub transport failure", "stdout": ""}
        if role == "compiler":
            value = copy.deepcopy(self.goal)
        elif role == "planner":
            value = (self.planner or self.default_planner)(payload)
        elif role == "evidence_checker":
            value = (self.checker or self.default_checker)(payload)
        elif role == "reviewer":
            value = (self.reviewer or self.default_reviewer)(payload)
        elif role == "verifier":
            value = {"verdict": "PASS", "binding_hash": payload["binding_hash"], "scope": "data-replay"}
        else:
            raise AssertionError("unexpected role " + role)
        envelope = {
            "session_id": f"stub-session-{len(self.calls)}",
            "total_cost_usd": 0.0,
            "result": json.dumps(value),
        }
        return {"exit_code": 0, "stderr": "", "stdout": json.dumps(envelope)}

    def default_planner(self, payload):
        return {
            "plan": expected_plan(self.goal),
            "claims": claims(self.goal),
            "rationale": "The registered profile plan.",
        }

    def default_checker(self, payload):
        return {
            "plan_hash": payload["plan_hash"],
            "verdicts": [
                {"claim_id": c["claim_id"], "verdict": "SUPPORTED", "reasoning": "verbatim"}
                for c in payload["claims"]
            ],
        }

    def default_reviewer(self, payload):
        evidence = payload["review_evidence"]
        return {
            "verdict": "APPROVE",
            "approved_binding": {k: evidence[k] for k in ("plan_hash", "source_hash")},
            "findings": [],
        }


def write_source(directory, goal):
    days = (np.datetime64(goal["end_date"]) - np.datetime64(goal["start_date"])).astype(int) + 1
    values = np.arange(days * 4, dtype="float32").reshape(days, 2, 2) / 10
    values[:, 1, 1] = np.nan
    north, west, south, east = goal["bbox"]
    ds = xr.Dataset(
        {"precip": (("time", "latitude", "longitude"), values, {"units": "mm day-1"})},
        coords={
            "time": np.arange(
                np.datetime64(goal["start_date"]),
                np.datetime64(goal["end_date"]) + np.timedelta64(1, "D"),
            ).astype("datetime64[ns]"),
            "latitude": np.array([north, south]),
            "longitude": np.array([west, east]),
        },
        attrs={"dataset": "chirps", "source_lineage": "chirps-fetch"},
    )
    directory.mkdir()
    path = directory / "fixture.nc"
    ds.to_netcdf(path, engine="scipy")
    return path


def write_repo(root):
    for skill, (script, _) in CATALOG.items():
        directory = root / "skills" / skill
        (directory / "scripts").mkdir(parents=True)
        (directory / "SKILL.md").write_bytes(f"Documentation for {skill}.\n".encode())
        (directory / "scripts" / script).write_text(TOOL_SCRIPT, encoding="utf-8")
    return root


def profile_document(goal):
    variant = {
        "goal": goal,
        "states": runner.stages(goal["task"]),
        "final_artifacts": expected_plan(goal)["final"],
        "request_delta": [],
    }
    return {"variants": {runner.PROFILES[goal["task"]]["id"]: variant}}


def ledger(goal):
    text = (
        f"Synthetic scenario for {goal['task']} within {goal['bbox']} from {goal['start_date']} "
        f"through {goal['end_date']}. Do not compare with a historical normal."
    )
    return {
        "scenario_speaker": "assistant",
        "scenario_text": text,
        "authorization_speaker": "user",
        "authorization_text": "do it",
        "authorization_context": "synthetic test authorization",
    }


def test_case(goal, requirements=("CAP-NOT-IMPLEMENTED",), max_repairs=0, case_id="LOCAL-P8"):
    return {
        "id": case_id,
        "profile": runner.PROFILES[goal["task"]]["id"],
        "request_delta": [],
        "budget": {"max_repair_attempts": max_repairs},
        "adapter_requirements": list(requirements),
    }


test_case.__test__ = False  # a helper, not a test


class Lab:
    """One temp repo, definition source and patched controller seams for the module."""

    def __init__(self, root):
        self.root = root
        self.repo = write_repo(root / "repo")
        self.definition = root / "definition" / "agrhymet-sos-rolling.reference.txt"
        self.definition.parent.mkdir()
        self.definition.write_bytes(REFERENCE_DEFINITION.encode("utf-8"))

    def profile(self, goal, case, stub, scenario=None):
        source_ledger = ledger(goal)
        if scenario is not None:
            source_ledger["scenario_text"] = scenario
        return {
            "profile_document": profile_document(goal),
            "source_ledger": source_ledger,
            "case_root": self.root / f"case-{case['id']}",
            "source": write_source(self.root / f"generated-{case['id']}", goal),
            "generator_provenance": GENERATOR,
            "presented_fetch_version": "0.0.2",
            "definition_source": self.definition,
            "repo": self.repo,
            "roles_factory": lambda run_dir: agents.Roles(stub, run_dir, budget=5),
            "processes_factory": lambda run_dir: Processes(run_dir / "processes"),
            "input_supplement": {"supplement_id": "test-only", "sha256": None},
        }

    def execute(self, goal, case, scenario=None, **cli):
        stub = RoleCli(goal, **cli)
        profile = self.profile(goal, case, stub, scenario)
        evidence = WeatherAdapter(enable_run_case=True).run_case(case, profile)
        harness = Path(profile["case_root"]) / "harness"
        report = json.loads((harness / "evidence-projection.json").read_text(encoding="utf-8"))
        return SimpleNamespace(
            evidence=evidence,
            report=report,
            stub=stub,
            run=Path(profile["case_root"]) / "run",
            harness=harness,
        )


@pytest.fixture(scope="module")
def lab(tmp_path_factory):
    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(
            runner,
            "build_manifest",
            lambda base: {
                str(p.relative_to(base)): hashlib.sha256(p.read_bytes()).hexdigest()
                for p in sorted(Path(base).rglob("*.md"))
            },
        )
        # Parser preflight is the real CLI contract elsewhere; the stand-in tools have none.
        mp.setattr(runner, "preflight", lambda *args: ({}, {}))
        mp.setattr(agents.shutil, "which", lambda _: "never-launched-claude")
        yield Lab(tmp_path_factory.mktemp("p8"))


@pytest.fixture(scope="module")
def released(lab):
    return lab.execute(MAP_GOAL, test_case(MAP_GOAL, case_id="RELEASED-MAP"))


@pytest.fixture(scope="module")
def data_run(lab):
    return lab.execute(DATA_GOAL, test_case(DATA_GOAL, case_id="RELEASED-DATA"))


def non_verbatim_planner(goal):
    def planner(payload):
        value = RoleCli(goal).default_planner(payload)
        value["claims"][0]["citations"] = [
            citation("A candidate at day t requires t through t+19 inclusive.")
        ]
        return value

    return planner


@pytest.fixture(scope="module")
def blocked(lab):
    return lab.execute(
        MAP_GOAL,
        test_case(MAP_GOAL, case_id="BLOCKED-CLAIM"),
        planner=non_verbatim_planner(MAP_GOAL),
    )


@pytest.fixture(scope="module")
def needs_info(lab):
    # The scenario carries the generator identity: the system can see the synthetic tag.
    scenario = ledger(MAP_GOAL)["scenario_text"] + " Generated by " + GENERATOR["generator"] + "."
    return lab.execute(
        MAP_GOAL,
        test_case(MAP_GOAL, case_id="NEEDS-INFO"),
        scenario=scenario,
        planner=lambda payload: {"needs_info": "Which season is intended?"},
    )


@pytest.fixture(scope="module")
def failed_review(lab):
    return lab.execute(
        MAP_GOAL, test_case(MAP_GOAL, case_id="FAILED-REVIEW"), fail_roles={"reviewer"}
    )


# Known MISSING findings of the frozen V24 validator on a released run, each with its owner:
# whole-tree harness observation (CAP-HARNESS-OBS), resume split and limit (CAP-AUTO-RESUME /
# CAP-RESUME), operator actions (CAP-OPERATOR-ACTIONS), exhaustive writes (not declared), and the
# initial-state clock (CAP-RECEIPTS: the controller emits no event for CAPTURED).
V24_RELEASED_MISSING = {
    "count field harness_observations.max_concurrent_children missing",
    "count field harness_observations.shell_launches missing",
    "count field harness_observations.data_fetch_attempts missing",
    "count field budget_ledger.final.automatic_resumes_used missing",
    "count field budget_ledger.final.limits.max_resume_attempts missing",
    "list stream controller_trace.resume missing",
    "list stream controller_trace.operator_actions missing",
    "list stream filesystem_snapshot.created_paths missing",
    "no event clock for entered state CAPTURED",
}


# --------------------------------------------------------------------------- released runs


def test_released_map_run_passes_frozen_v25_validators_and_v24_has_no_fail(released, frozen):
    trace = released.evidence["controller_trace"]
    assert trace["final_state"] == "RELEASED"
    assert trace["terminal"] == {"outcome": "RELEASED", "reason": "released", "failed_gate": None}
    result = findings(frozen, "DEV-E2E-V2_7-E02-P1-INJ", released.evidence)
    assert result["V25-EVIDENCE"] == []
    assert result["V25-PRE-REVIEW-RESOLVED"] == []
    assert fails(result["V24-EVIDENCE"]) == []
    assert {m for _, m in result["V24-EVIDENCE"]} == V24_RELEASED_MISSING


def test_released_projection_maps_native_records_without_filling(released):
    ev = released.evidence
    roles = [(r["role"], r["occurrence"]) for r in ev["invocation_receipts"]]
    assert roles == [
        ("compiler_1", 1),
        ("compiler_2", 1),
        ("compiler_3", 1),
        ("planner", 1),
        ("evidence_checker", 1),
        ("reviewer", 1),
        ("verifier", 1),
    ]
    seqs = [r["seq"] for r in ev["invocation_receipts"]]
    assert seqs == sorted(seqs) and len(set(seqs)) == len(seqs)
    assert {r["phase"] for r in ev["invocation_receipts"]} == {"pre_interrupt"}
    assert [r["verdict"] for r in ev["invocation_receipts"] if "verdict" in r] == [
        "SUPPORTED",
        "APPROVE",
        "PASS",
    ]
    # role_output: zero-based per role, empty for named roles never invoked.
    assert ev["role_output"]["repairer"] == [] and ev["role_output"]["shadow_policy"] == []
    assert ev["role_output"]["compiler_2"][0]["goal"] == MAP_GOAL
    planner = ev["role_output"]["planner"][0]
    assert planner["plan"] == expected_plan(MAP_GOAL)["steps"]
    assert planner["controller_plan"] == expected_plan(MAP_GOAL)
    # Process receipts: the validated CLI argv, on the dispatch clock, owned by the controller.
    procs = ev["process_receipts"]
    assert [(p["tool"], p["artifact"]) for p in procs] == [
        ("check-artifact", "source"),
        ("onset-date", "onset"),
        ("check-artifact", "onset"),
        ("plot-onset", "map"),
        ("verify-run", "onset"),
        ("verify-run", "map"),
    ]
    validation = ev["controller_trace"]["plan_validations"][-1]
    by_tool = {p["tool"]: p for p in procs}
    assert by_tool["onset-date"]["argv"] == validation["argv"]["onset"]
    assert by_tool["plot-onset"]["argv"] == validation["argv"]["map"]
    assert all(p["parent_pid"] == ev["symbols"]["HARNESS_CONTROLLER_PID"] for p in procs)
    assert all(p["start"] < p["end"] for p in procs)
    for producing in (by_tool["onset-date"], by_tool["plot-onset"]):
        assert producing["producing"] is True
        assert producing["approval_plan_hash"] == producing["plan_hash_at_launch"]
        assert producing["approval_source_hash"] == producing["source_hash_at_launch"]
    budget = ev["budget_ledger"]
    assert budget["final"]["combined_process_output_bytes"] == sum(p["output_bytes"] for p in procs)
    launched = [c["tool"] for c in budget["launch_checks"] if c["launched"]]
    assert launched == [p["tool"] for p in procs]
    assert "at_checkpoint" not in budget
    # Gates bound to the artifact the harness hashed after the gate ran.
    assert set(ev["gate_reports"]) == {
        "check-artifact(source)",
        "check-artifact(onset)",
        "verify-run(onset)",
        "verify-run(map)",
    }
    onset = ev["artifacts"]["onset"]
    assert onset["present"] is True
    gate = ev["gate_reports"]["check-artifact(onset)"]
    assert gate["artifact_content_hash"] == onset["content_hash"]
    symbols = ev["symbols"]
    assert symbols["HARNESS_HASH:source"] == symbols["HARNESS_HASH:source_pre"]
    assert ev["filesystem_snapshot"] == {
        "source_cache_hash_after": symbols["HARNESS_HASH:source_pre"]
    }
    assert symbols["HARNESS_GOAL_HASH:authorized"] == digest(MAP_GOAL)
    assert symbols["HARNESS_PLAN_HASH:v1"] == symbols["HARNESS_PLAN_HASH:current"]
    assert symbols["HARNESS_SOURCE_HASH:definition.agrhymet-sos-rolling"] == FROZEN_SOURCE_HASH
    assert symbols["HARNESS_RESOLVED_ARTIFACT_PATH:onset"] == onset["path"]
    # Absent producers stay absent (never []), and every gap names its owner.
    trace = ev["controller_trace"]
    for name in ("operator_actions", "resume", "transition_requests", "interruptions"):
        assert name not in trace
    assert "signalled_pids" not in trace
    assert "signal_capture_exhaustive" not in ev["harness_observations"]
    assert "created_paths" not in ev["filesystem_snapshot"]
    owners = {m["path"]: m["capability"] for m in released.report["missing"]}
    assert owners["filesystem_snapshot.created_paths"] == "CAP-EXHAUSTIVE-WRITES"
    assert owners["controller_trace.signalled_pids"] == "CAP-SIGNAL-CAPTURE"
    assert owners["harness_observations.max_concurrent_children"] == "CAP-HARNESS-OBS"
    # Proven absences only for keys a finished, gap-free sequence rules out.
    assert ev["event_seq"]["_absent"] == []
    clocks = [v for k, v in ev["event_seq"].items() if k != "_absent"]
    assert ev["event_seq"]["controller_trace.terminal"] == max(clocks)


def test_released_run_provenance_follows_ruling_d3(released):
    provenance = released.evidence["harness_observations"]["run_provenance"]
    manifest = (released.harness / "manifest.json").read_bytes()
    assert provenance["tag"] == "synthetic"
    assert provenance["manifest_hash"] == "sha256:" + hashlib.sha256(manifest).hexdigest()
    assert provenance["outputs_tagged"] is True
    assert provenance["visible_to_system"] is False
    assert "consequence of owner ruling D3" in provenance["admission_note"]
    tags = json.loads((released.harness / "output-tags.json").read_text())
    assert len(tags["outputs"]) == 3  # result.json, onset, map; a harness-only file


def test_review_snapshots_one_per_reviewer_receipt_with_d1_source(released):
    (snapshot,) = released.evidence["harness_observations"]["review_snapshots"]
    assert snapshot["reviewer_occurrence"] == 1
    sources = {s["source_id"]: s for s in snapshot["sources"]}
    assert sources["definition:agrhymet-sos-rolling"]["source_hash"] == FROZEN_SOURCE_HASH
    assert released.evidence["harness_observations"]["review_dispatch_failures"] == []
    approval = released.evidence["controller_trace"]["active_approval"]
    assert approval["reviewer_occurrence"] == 1
    assert approval["plan_hash"] == snapshot["plan_hash"]


def test_data_profile_run_with_null_title_releases_without_a_map(data_run, frozen):
    ev = data_run.evidence
    trace = ev["controller_trace"]
    assert trace["final_state"] == "RELEASED"
    assert "MAP_CREATED" not in trace["states"]
    assert set(ev["artifacts"]) == {"onset"}
    assert ev["role_output"]["compiler_1"][0]["goal"]["title"] is None
    seeded = json.loads((data_run.harness / "seeded-ledger.json").read_text())
    assert seeded["derivation"]["unattributed_null_fields"] == ["title"]
    assert seeded["request"]["constraints"]["title"] == {
        "value": None,
        "message_id": None,
        "quote": None,
    }
    result = findings(frozen, "DEV-E2E-V2_7-E01-P2-INJ", ev)
    assert result["V25-EVIDENCE"] == [] and result["V25-PRE-REVIEW-RESOLVED"] == []
    assert fails(result["V24-EVIDENCE"]) == []
    assert "process_receipts[tool=plot-onset].start" in ev["event_seq"]["_absent"]


# --------------------------------------------------------------------------- stopped runs


def test_claim_gate_block_projects_a_routed_record_and_no_review(blocked, frozen):
    ev = blocked.evidence
    trace = ev["controller_trace"]
    assert trace["terminal"]["failed_gate"] == "repair_exhausted"
    assert trace["terminal"]["outcome"] == "BLOCKED"
    (record,) = trace["claim_checks"]
    assert record["citation_gate"] == "FAIL" and record["decision"] == "ROUTE_TO_REPAIR"
    assert record["evidence_checker_occurrence"] is None
    assert ev["role_output"]["reviewer"] == [] and ev["role_output"]["evidence_checker"] == []
    assert ev["harness_observations"]["review_snapshots"] == []
    assert "active_approval" not in trace
    assert [p["tool"] for p in ev["process_receipts"]] == ["check-artifact"]
    assert "controller_trace.states[RELEASED]" in ev["event_seq"]["_absent"]
    for name, found in findings(frozen, "DEV-E2E-V2_7-E13-P1-INJ", ev).items():
        assert fails(found) == [], name


def test_needs_info_stop_and_visible_synthetic_marker(needs_info, frozen):
    ev = needs_info.evidence
    trace = ev["controller_trace"]
    assert trace["terminal"] == {
        "outcome": "NEEDS_INFO",
        "reason": "needs_info",
        "failed_gate": "plan_validation",
    }
    assert trace["plan_validations"] == []
    assert ev["role_output"]["planner"] == [{"needs_info": "Which season is intended?"}]
    provenance = ev["harness_observations"]["run_provenance"]
    assert provenance["visible_to_system"] is True
    assert GENERATOR["generator"] in provenance["scan"]["found"]
    for name, found in findings(frozen, "DEV-E2E-V2_7-E03-P3-INJ", ev).items():
        assert fails(found) == [], name


def test_failed_reviewer_dispatch_is_reported_never_a_snapshot_or_receipt(failed_review, frozen):
    ev = failed_review.evidence
    observations = ev["harness_observations"]
    assert observations["review_snapshots"] == []
    (failure,) = observations["review_dispatch_failures"]
    assert failure["dispatch_status"] == "FAILED"
    assert ev["role_output"]["reviewer"] == []
    assert not any(r["role"] == "reviewer" for r in ev["invocation_receipts"])
    assert ev["controller_trace"]["terminal"]["failed_gate"] == "review"
    found = findings(frozen, "DEV-E2E-V2_7-E02-P1-INJ", ev)["V25-PRE-REVIEW-RESOLVED"]
    assert found == [
        (
            "MISSING",
            "claim_checks[0] presented to reviewer occurrence 1, which has no bound review "
            "snapshot",
        )
    ]


# --------------------------------------------------------------------------- refusals


class Tamper:
    """Edit one native file in place; the original bytes are restored on exit."""

    def __init__(self, path):
        self.path, self.original = Path(path), Path(path).read_bytes()

    def __enter__(self):
        return self.path

    def __exit__(self, *exc):
        self.path.write_bytes(self.original)


def test_tampered_or_inconsistent_native_evidence_refuses_projection(released):
    run = released.run
    adapter = WeatherAdapter()
    adapter.capture_evidence(run, {"processes_root": run / "processes"})  # intact: projects
    with Tamper(run / "dispatch-event-000005.json") as path:
        record = json.loads(path.read_text())
        record["resume_phase"] = 7
        path.write_text(json.dumps(record))
        with pytest.raises(ep.EvidenceMappingError, match="changed after recording"):
            adapter.capture_evidence(run)
    extra = run / "dispatch-event-999999.json"
    extra.write_text("{}")
    try:
        with pytest.raises(ep.EvidenceMappingError, match="gap or duplicate"):
            adapter.capture_evidence(run)
    finally:
        extra.unlink()
    with Tamper(run / "processes" / "process-002.start.json") as path:
        record = json.loads(path.read_text())
        record["pid"] = record["pid"] + 1
        path.write_text(json.dumps(record))
        with pytest.raises(ep.EvidenceMappingError, match="start receipt differs"):
            adapter.capture_evidence(run, {"processes_root": run / "processes"})
    with Tamper(run / "result.json") as path:
        record = json.loads(path.read_text())
        record["terminal"]["failed_gate"] = "review"
        path.write_text(json.dumps(record))
        with pytest.raises(ep.EvidenceMappingError, match="terminal differs"):
            adapter.capture_evidence(run)


def test_pending_records_light_up_from_their_frozen_native_shape(released):
    from tools.workflow.persistence import read_checkpoint, write_checkpoint

    run = released.run
    with Tamper(run / "checkpoint.json"):
        payload = read_checkpoint(run)
        action = {
            "record_id": "op-1",
            "source": "operator_ledger",
            "speaker": "user",
            "action": "resume",
            "accepted": True,
            "sequence": 3,
        }
        payload["context"]["operator_actions"] = [action]
        payload["context"]["resume"] = []
        write_checkpoint(run, payload)
        ev = WeatherAdapter().capture_evidence(run)["evidence"]
        assert ev["controller_trace"]["operator_actions"] == [action]
        assert ev["controller_trace"]["resume"] == []
        assert ev["event_seq"]["controller_trace.operator_actions[record_id=op-1]"] == 3
        assert "interruptions" not in ev["controller_trace"]


def test_run_without_dispatch_evidence_is_refused(tmp_path):
    from tools.workflow.persistence import write_checkpoint

    run = tmp_path / "run"
    run.mkdir()
    payload = {"state": "CAPTURED", "events": [], "root": str(run), "context": {}, "evidence": {}}
    write_checkpoint(run, payload)
    with pytest.raises(ep.EvidenceMappingError, match="dispatch evidence mode"):
        ep.project_run(run)


# --------------------------------------------------------------------------- run_case and D4


def test_run_case_is_opt_in_and_refuses_undeclared_capabilities_before_side_effects(
    tmp_path, frozen
):
    case = copy.deepcopy(frozen["cases"][0])
    with pytest.raises(AdapterRefusal, match="disabled"):
        WeatherAdapter().run_case(case, {})
    profile = {"case_root": tmp_path / "never-created"}
    with pytest.raises(AdapterRefusal, match="undeclared capabilities"):
        WeatherAdapter(enable_run_case=True).run_case(case, profile)
    assert not profile["case_root"].exists()
    assert WeatherAdapter().can_run() is False
    assert WeatherAdapter(enable_run_case=True).can_run() is True
    assert WeatherAdapter(enable_run_case="yes").can_run() is False


def test_every_frozen_case_stays_not_implemented_under_the_real_runner(frozen):
    adapter = WeatherAdapter(enable_run_case=True)
    records = simkit_run(frozen["cases"], adapter, profile={})
    assert len(records) == 66
    assert {r["status"] for r in records} == {"NOT_IMPLEMENTED"}
    assert all("run_case" not in r["missing_capabilities"] for r in records)
    assert all("CAP-EXHAUSTIVE-WRITES" in r["missing_capabilities"] for r in records)
    assert all(r["evidence"] is None for r in records)


def producers_with(**changes):
    path = Path(ep.__file__).with_name("CAPABILITY-PRODUCERS.json")
    producers = json.loads(path.read_text(encoding="utf-8"))
    for capability, fields in changes.items():
        producers["capabilities"][capability].update(fields)
    return producers


def test_d4_gate_declares_only_reviewed_producer_mapped_records():
    base = WeatherAdapter()
    records = base.producers["capabilities"]
    gated = {c for c, r in records.items() if r.get("d4_gated") is True}
    assert gated and not (gated & base.implemented_capabilities())
    for capability in gated:
        record = records[capability]
        assert record["reviewer"] is None
        assert record["evidence_fields"] and record["tests"]
        if record["status"] != "NOT_DECLARED":
            assert record["producers"]
            assert getattr(WeatherAdapter, record["method"]) is not getattr(
                Adapter, record["method"], None
            )
    reviewed = WeatherAdapter(producers=producers_with(**{"CAP-REASON-MAP": {"reviewer": "lead"}}))
    assert "CAP-REASON-MAP" in reviewed.implemented_capabilities()
    partial = WeatherAdapter(producers=producers_with(**{"CAP-HARNESS-OBS": {"reviewer": "lead"}}))
    assert "CAP-HARNESS-OBS" not in partial.implemented_capabilities()  # status PARTIAL
    assert records["CAP-EXHAUSTIVE-WRITES"]["status"] == "NOT_DECLARED"
    assert "CAP-EXHAUSTIVE-WRITES" not in base.implemented_capabilities()


def test_symbol_resolver_is_a_closed_native_table(released):
    adapter = WeatherAdapter()
    projection = adapter.capture_evidence(released.run)
    assert adapter.resolve_symbol("HARNESS_RUN_DIR", projection) == str(released.run.resolve())
    plot = next(p for p in projection["evidence"]["process_receipts"] if p["tool"] == "plot-onset")
    assert adapter.resolve_symbol("HARNESS_PID:plot-onset", projection) == plot["pid"]
    for unresolved in ("HARNESS_PLAN_HASH:v2", "HARNESS_GOAL_HASH:authorized"):
        with pytest.raises(ep.EvidenceMappingError, match="not observed"):
            adapter.resolve_symbol(unresolved, projection)  # no second plan, no harness goal
    for unknown in ("HARNESS_INVENTED", "HARNESS_CANDIDATE_LABEL:GOAL_CHECKED.proceed"):
        with pytest.raises(ep.EvidenceMappingError, match="closed table"):
            adapter.resolve_symbol(unknown, projection)


def test_terminal_mapping_never_guesses_an_unmapped_gate():
    adapter = WeatherAdapter()
    mapped = adapter.map_terminal_reason({"outcome": "BLOCKED", "reason": "x", "failed_gate": "review"})
    assert mapped["terminal"]["failed_gate"] == "review"
    unmapped = adapter.map_terminal_reason({"outcome": "BLOCKED", "reason": "x", "failed_gate": "lint"})
    assert "failed_gate" not in unmapped["terminal"]
    assert unmapped["notes"] == [{"unmapped_failed_gate": "lint"}]
    released = adapter.map_terminal_reason(
        {"outcome": "RELEASED", "reason": "released", "failed_gate": None}
    )
    assert released["terminal"]["failed_gate"] is None
    with pytest.raises(ep.EvidenceMappingError):
        adapter.map_terminal_reason({"outcome": "DONE", "reason": "x", "failed_gate": None})


def signal_record(signalled, exhaustive, reasons=()):
    return {
        "schema": "signal-stop/1",
        "signalled_pids": signalled,
        "process_tree": {str(p): 1 for p in signalled},
        "exhaustive": exhaustive,
        "exhaustive_reasons": list(reasons),
    }


def test_signal_projection_never_turns_absence_into_exhaustive_evidence():
    assert ep.project_signal_records([]) is None
    one = ep.project_signal_records([signal_record([11], True)])
    assert one["signalled_pids"] == [11] and one["signal_capture_exhaustive"] is True
    mixed = ep.project_signal_records(
        [signal_record([11], True), signal_record([], False, ["empty signal list"])]
    )
    assert mixed["signal_capture_exhaustive"] is False
    with pytest.raises(ep.EvidenceMappingError):
        ep.project_signal_records([{**signal_record([1], True), "schema": "other"}])


def test_ledger_seed_applies_the_delta_once_and_refuses_unsupported_inputs():
    adapter = WeatherAdapter()
    variant = profile_document(DATA_GOAL)["variants"]["cached-onset-data/v1"]
    delta = [{"op": "replace", "path": "/scenario_text", "value": "Compute onset data only."}]
    case = {**test_case(DATA_GOAL), "request_delta": delta}
    seeded = adapter.seed_ledger(case, variant, ledger(DATA_GOAL))
    assert seeded["request"]["messages"][0]["text"] == "Compute onset data only."
    assert seeded["derivation"]["request_delta_applied"] == 1
    assert seeded["request"]["authorization"]["goal_hash"] == digest(DATA_GOAL)
    bad = {**case, "request_delta": [{"op": "add", "path": "/extra", "value": "x"}]}
    with pytest.raises(AdapterRefusal):
        adapter.seed_ledger(bad, variant, ledger(DATA_GOAL))
    assistant = {**ledger(DATA_GOAL), "authorization_speaker": "assistant"}
    with pytest.raises(AdapterRefusal):
        adapter.seed_ledger(case, variant, assistant)


def test_profile_selection_is_checked_against_the_controller_profile():
    adapter = WeatherAdapter()
    document = profile_document(MAP_GOAL)
    assert adapter.select_profile(test_case(MAP_GOAL), document)["goal"] == MAP_GOAL
    wrong = copy.deepcopy(document)
    wrong["variants"]["cached-onset/v1"]["states"] = runner.stages("onset_data")
    with pytest.raises(AdapterRefusal):
        adapter.select_profile(test_case(MAP_GOAL), wrong)
    with pytest.raises(AdapterRefusal):
        adapter.select_profile(test_case(DATA_GOAL), document)


def test_zero_collected_outputs_are_never_tagged(released, tmp_path):
    harness = tmp_path / "harness"
    harness.mkdir()
    (harness / "manifest.json").write_text("{}")
    (harness / "output-tags.json").write_text(json.dumps({"outputs": []}))
    provenance = ep.run_provenance(released.run, harness, [GENERATOR["generator"]])
    assert provenance["outputs_tagged"] is False
