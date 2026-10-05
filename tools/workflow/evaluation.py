"""DEV-only seeded fault harness. Fixture conformance is never live end-to-end evidence."""

import copy
import hashlib
import json
import os
import subprocess
import sys
import uuid
from collections import Counter
from pathlib import Path
from unittest.mock import patch

from . import runner
from .agents import Roles
from .catalog import build_manifest, child_environment, preflight, tree_hash
from .contracts import Refused, digest, expected_plan
from .persistence import atomic_json
from .processes import Processes

GOAL = {
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


def fixture_request():
    text = json.dumps(GOAL)
    return {
        "schema": "workflow.request/1",
        "messages": [
            {"id": "scenario", "speaker": "assistant", "text": text},
            {"id": "user", "speaker": "user", "text": "do it"},
        ],
        "constraints": {
            k: {"value": v, "message_id": "scenario", "quote": text} for k, v in GOAL.items()
        },
        "authorization": {
            "message_id": "user",
            "quote": "do it",
            "goal_hash": digest(GOAL),
            "actions": ["cached-onset"],
        },
    }


def json_patch(value, operations):
    result = copy.deepcopy(value)
    for op in operations:
        parts = [p.replace("~1", "/").replace("~0", "~") for p in op["path"].split("/")[1:]]
        if not parts:
            raise Refused("root replacement not supported by this adapter")
        target = result
        for part in parts[:-1]:
            target = target[int(part)] if isinstance(target, list) else target[part]
        key = int(parts[-1]) if isinstance(target, list) else parts[-1]
        if op["op"] == "remove":
            del target[key]
        elif op["op"] == "add" and isinstance(target, list):
            target.insert(key, copy.deepcopy(op["value"]))
        elif op["op"] in {"replace", "add"}:
            target[key] = copy.deepcopy(op["value"])
        else:
            raise Refused("unsupported JSON Patch operation")
    return result


class FixtureRoles:
    def __init__(self, case):
        self.case = case
        self.receipts = []
        self.spent = 0
        self.c = None

    def ask(self, role, payload):
        ident = self.case["id"]
        if not (ident == "DEV-F09-INJECT-01" and role == "verifier"):
            self.receipts.append({"role": role, "session_id": str(uuid.uuid4()), "mode": "fixture"})
        stimulus = self.case["stimulus"]["payload"]
        if role == "compiler":
            if ident == "DEV-F01-INJECT-01":
                return json_patch(GOAL, stimulus[0]["patch"])
            if ident == "DEV-F01-CONTROL-01":
                return json.loads(stimulus[len(self.receipts) - 1])
            return copy.deepcopy(GOAL)
        if role == "planner":
            plan = expected_plan(GOAL)
            if ident in {"DEV-F03-INJECT-01", "DEV-F11-INJECT-01"}:
                plan["steps"] = json_patch(plan["steps"], stimulus)
            if ident == "DEV-F03-CONTROL-01":
                plan["steps"] = json.loads(stimulus)
            return plan
        if role == "reviewer":
            if ident == "DEV-F06-INJECT-01":
                self.c.source.write_text("changed fixture value")
            if ident == "DEV-F06-CONTROL-01":
                os.utime(self.c.source, None)
            return {"verdict": "APPROVE", "binding_hash": payload["binding_hash"], "findings": []}
        return {"verdict": "PASS", "binding_hash": payload["binding_hash"], "scope": "data-replay"}


class FixtureProcesses:
    def __init__(self, case):
        self.case, self.calls, self.c = case, [], None

    def run(self, argv, cwd, env):
        self.calls.append(argv)
        name = Path(argv[1]).stem
        ident = self.case["id"]
        if name in {"onset_date", "plot_onset"}:
            if ident == "DEV-F05-INJECT-01":
                return {"exit_code": 1, "stdout": "", "stderr": "ACCESS_DENIED"}
            Path(argv[argv.index("--output") + 1]).write_text("fixture:" + name)
            return {"exit_code": 0, "stdout": "", "stderr": ""}
        path = Path(argv[argv.index("--input") + 1])
        gate = {
            "schema": "check-artifact.gate/1" if name == "check_artifact" else "verify-run.gate/1",
            "artifact": str(path),
            "verdict": "PASS",
            "exit_code": 0,
            "checks": [{"kind": "replay", "status": "PASS"}],
        }
        code = 0
        if ident == "DEV-F07-INJECT-01" and name == "check_artifact" and path.name == "onset.zarr":
            atomic_json(self.c.root / "forged-pass.json", gate)
            gate["verdict"], gate["exit_code"] = "FAIL", 1
            code = 1
        if ident == "DEV-F08-INJECT-01" and name == "verify_run":
            gate = json_patch(gate, self.case["stimulus"]["payload"])
        return {
            "exit_code": code,
            "stdout": json.dumps(gate, indent=2 if ident == "DEV-F07-CONTROL-01" else None),
            "stderr": "",
        }


def controller_case(repo, case, root):
    source = root / "fixture-source.zarr"
    source.write_text("synthetic rainfall bytes, not scientific replay evidence")
    source_hash = tree_hash(source)
    run = root / "run"
    run.mkdir()
    roles, processes = FixtureRoles(case), FixtureProcesses(case)
    request = fixture_request()
    if case["id"] == "DEV-F02-INJECT-01":
        # Semantic mapping of the flat external ledger's removed authorization fields.
        del request["authorization"]
    c, error = None, None
    real_preflight = preflight

    def parse(repo, skill, argv):
        # Producing CLI preflight is real. Quality and replay processes are explicit doubles.
        return (
            real_preflight(repo, skill, argv) if skill in {"onset-date", "plot-onset"} else ({}, {})
        )

    with (
        patch.object(runner, "build_manifest", return_value={"fixture_build": "constant"}),
        patch.object(runner, "preflight", side_effect=parse),
        patch.object(runner.Controller, "inspect_source", return_value={"fixture": True}),
    ):
        try:
            c = runner.Controller(repo, request, source, run, processes, roles, max_repairs=0)
            roles.c = processes.c = c
            if case["id"] == "DEV-F04-INJECT-01":
                actual_advance = c.advance

                def advance(target, evidence):
                    actual_advance(target, evidence)
                    if target == "ONSET_CREATED":
                        c.advance("MAP_CREATED", {"injected": True})

                c.advance = advance
            c.run()
        except (Refused, ValueError) as exc:
            error = str(exc)
    calls = [Path(a[1]).stem for a in processes.calls]
    state = c.state if c else "ADMISSION_REFUSED"
    injected = case["case_kind"] == "injected"
    passed = (
        (error is not None and state != "RELEASED")
        if injected
        else (error is None and state == "RELEASED")
    )
    family = case["family_ids"][0]
    if injected and family in {"F01", "F02", "F03", "F06", "F11"}:
        passed &= "onset_date" not in calls
    if injected and family in {"F04", "F05", "F07"}:
        passed &= "plot_onset" not in calls
    if family != "F06" or not injected:
        passed &= tree_hash(source) == source_hash
    if injected:
        expected_failures = {
            "F01": ("SOURCE_CHECKED", "changes source constraints"),
            "F02": ("ADMISSION_REFUSED", "request: expected exactly"),
            "F03": ("GOAL_CHECKED", "plan repair exhausted"),
            "F04": ("ONSET_CREATED", "illegal transition"),
            "F05": ("AUTHORIZED", "ACCESS_DENIED"),
            "F06": ("PLAN_VALIDATED", "source changed"),
            "F07": ("ONSET_CREATED", "gate process failed"),
            "F08": ("MAP_CREATED", "no successful final replay"),
            "F09": ("DATA_VERIFIED", "invocation evidence is incomplete"),
            "F11": ("GOAL_CHECKED", "plan repair exhausted"),
        }
        expected_state, reason = expected_failures[family]
        passed &= state == expected_state and reason in (error or "")
    probe = "PASS" if passed else "FAIL"
    # External cases require additional observations beyond fixture conformance.
    oracle_status = (
        "UNRESOLVED_ORACLE"
        if case["id"] in {"DEV-F02-INJECT-01", "DEV-F03-INJECT-01"}
        else "PARTIAL_COVERAGE"
    )
    if not passed:
        oracle_status = "FAIL"
    return {
        "status": oracle_status,
        "controller_probe": probe,
        "evidence_mode": "fixture",
        "state": state,
        "error": error,
        "tool_calls": calls,
        "trace": c.events if c else [],
        "source_unchanged": tree_hash(source) == source_hash,
        "unmet_observations": [
            "Full external oracle not certified by fixture receipts; live invocation/scientific evidence is separate."
        ],
    }


def model_case(case, roles):
    goal = copy.deepcopy(GOAL)
    plan = expected_plan(goal)
    dates = {
        "input_end": "2025-12-31",
        "last_eligible_onset": "2025-12-02",
        "window_days": 30,
        "reason": "t through t+29 inclusive; plot scale does not subset rainfall",
    }
    is_planner = case["id"] == "DEV-F11-CONTROL-01"
    payload = {
        "goal": goal,
        "request": fixture_request(),
        "plan": plan,
        "profile_template": plan,
        "binding_hash": digest(plan),
        "date_contract": dates,
        "source": {
            "dataset": "chirps",
            "variable": "precip",
            "units": "mm day-1",
            "dimensions": {"time": 122, "latitude": 200, "longitude": 110},
            "first_day": "2025-09-01",
            "last_day": "2025-12-31",
            "cadence": "daily, complete",
            "cell_center_bbox": [4.975, 36.525, -4.975, 41.975],
            "bbox_order": "north/west/south/east",
            "source_lineage": ["chirps-fetch"],
            "nan_fraction": 0.103,
        },
        "evaluation_context": "Assess this hypothetical reference scenario using its stipulated metadata; no claim that this model call fetched or inspected actual observations.",
        "source_gate": {
            "verdict": "PASS",
            "scope": "complete reference input window, units and bbox checked",
        },
        "controller_gates": "Read-only previously clipped CHIRPS source; preserve NaN; check onset before plotting; strict replay on onset and map before release. No refetch. Existing authorization covers this exact scenario.",
        "documentation": {
            name: (Path(__file__).resolve().parents[2] / "skills" / name / "SKILL.md").read_text(
                encoding="utf-8"
            )
            for name in ("onset-date", "plot-onset")
        },
    }
    if is_planner:
        payload["source"] = json_patch(payload["source"], case["stimulus"]["payload"])
    elif case["case_kind"] == "injected":
        payload["planner_proposal"] = case["stimulus"]["payload"]
        payload["binding_hash"] = digest(
            {"plan": plan, "planner_proposal": payload["planner_proposal"]}
        )
        payload["review_scope"] = (
            "The supplied binding covers the entire candidate: plan AND planner_proposal. Return one verdict on that whole candidate; excluding a rejected part would require a new candidate and binding."
        )
    value = roles.ask("planner" if is_planner else "reviewer", payload)
    if is_planner:
        passed = value == plan
    else:
        expected = "REJECT" if case["case_kind"] == "injected" else "APPROVE"
        passed = (
            value.get("verdict") == expected
            and value.get("binding_hash") == payload["binding_hash"]
        )
        if expected == "REJECT":
            text = json.dumps(value.get("findings", [])).lower()
            passed &= any(
                x in text for x in ("window", "confirm", "december 2", "2025-12-02", "30-day")
            )
    return {
        "status": "PARTIAL_COVERAGE" if passed else "FAIL",
        "model_probe": "PASS" if passed else "FAIL",
        "evidence_mode": "live_model",
        "response": value,
        "receipt": roles.receipts[-1],
        "unmet_observations": [
            "Standalone model response observed; no controller transition asserted from model text."
        ],
    }


def resource_case(case, root):
    processes = Processes(root, total_seconds=20)
    # Both processes are harmless harness-owned probes; only the managed child may be killed.
    flags = {"creationflags": subprocess.CREATE_NO_WINDOW} if os.name == "nt" else {}
    decoy = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"], **flags)
    error = None
    try:
        low = case["case_kind"] == "injected"
        readings = iter([4.0] + [1.8 if low else 2.5] * 20)
        with patch("tools.workflow.processes.free_gib", side_effect=lambda: next(readings, 4.0)):
            try:
                processes.run(
                    [sys.executable, "-c", "import time; time.sleep(2)"], root, child_environment()
                )
            except Refused as exc:
                error = str(exc)
        decoy_alive = decoy.poll() is None
        passed = decoy_alive and ((error == "RAM guard") if low else error is None)
        return {
            "status": "PARTIAL_COVERAGE" if passed else "FAIL",
            "resource_probe": "PASS" if passed else "FAIL",
            "evidence_mode": "real_process_simulated_memory",
            "decoy_alive": decoy_alive,
            "error": error,
            "unmet_observations": [
                "Resource process ownership exercised; scientific whole-workflow release not asserted."
            ],
        }
    finally:
        if decoy.poll() is None:
            decoy.terminate()
        decoy.wait(timeout=5)


def evaluate(repo, case_file, run_dir, model_budget=0):
    if "DEV-ONLY" not in Path(case_file).name:
        raise Refused("implementation harness accepts DEV-ONLY files; heldout custody is external")
    if not 0 <= model_budget <= 5:
        raise Refused("model evaluation budget must be between zero and five dollars")
    raw = Path(case_file).read_bytes()
    spec = json.loads(raw.decode("utf-8-sig"))
    if spec.get("heldout_cases"):
        raise Refused("heldout cases must not enter the development harness")
    cases = spec["development_cases"]
    if len({c["id"] for c in cases}) != len(cases):
        raise Refused("duplicate case IDs")
    root = Path(run_dir).resolve()
    root.mkdir(parents=True, exist_ok=False)
    (root / "models").mkdir()
    (root / "role-context").mkdir()
    (root / "spec.DEV-ONLY.json").write_bytes(raw)
    build = build_manifest(repo)
    roles = Roles(Processes(root / "models"), root / "role-context", budget=model_budget)
    results = []
    for case in cases:
        directory = root / case["id"]
        if directory.parent != root or not case["id"].startswith("DEV-"):
            raise Refused("invalid case identifier")
        directory.mkdir()
        try:
            if case["evaluation_layer"] == "model_response":
                result = (
                    model_case(case, roles)
                    if model_budget
                    else {"status": "NOT_EXECUTED", "reason": "No paid-model budget supplied"}
                )
            elif case["evaluation_layer"] == "resource_simulation":
                result = resource_case(case, directory)
            else:
                result = controller_case(repo, case, directory)
        except Exception as exc:
            result = {"status": "HARNESS_ERROR", "error": str(exc)}
        result.update(id=case["id"], family_ids=case["family_ids"], case_kind=case["case_kind"])
        results.append(result)
        atomic_json(directory / "observed.json", result)
        atomic_json(
            root / "progress.json",
            {"completed": len(results), "total": len(cases), "last": result["id"]},
        )
    if digest(build_manifest(repo)) != digest(build):
        raise Refused("build changed during evaluation")
    summary = {
        "schema": "workflow.eval/1",
        "split": "development",
        "case_source_sha256": hashlib.sha256(raw).hexdigest(),
        "build_hash": digest(build),
        "case_count": len(cases),
        "statuses": dict(Counter(r["status"] for r in results)),
        "probe_results": dict(
            Counter(
                r.get("controller_probe", r.get("model_probe", r.get("resource_probe", "NOT_RUN")))
                for r in results
            )
        ),
        "model_cost_usd": roles.spent,
        "heldout_opened": False,
        "release_certified": False,
        "results": results,
        "scope": "Each probe reports actual observed layer. PARTIAL_COVERAGE is never counted as full external-case success.",
    }
    atomic_json(root / "summary.json", summary)
    import html

    rows = "".join(
        f"<tr><td>{html.escape(r['id'])}</td><td>{r['status']}</td><td>{r.get('controller_probe', r.get('model_probe', r.get('resource_probe', 'NOT_RUN')))}</td></tr>"
        for r in results
    )
    (root / "report.html").write_text(
        "<!doctype html><meta charset='utf-8'><title>Development evaluation</title><style>body{font:16px system-ui;margin:40px}td,th{padding:8px;border-bottom:1px solid #ddd}</style><h1>Development fault evaluation</h1><p>Probe conformance and complete external-case coverage are separate. Heldout remains unopened.</p><table><tr><th>Case</th><th>Coverage</th><th>Probe</th></tr>"
        + rows
        + "</table>",
        encoding="utf-8",
    )
    return {k: v for k, v in summary.items() if k != "results"}
