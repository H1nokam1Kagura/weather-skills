"""Controller for cached-onset/v1. No state can be advanced by an agent-written file."""

import json
import sys
import time
from datetime import date, timedelta
from pathlib import Path

from . import policy
from .admission import identity as admission_identity
from .admission import record_for
from .catalog import build_manifest, child_environment, preflight, safe_path, script, tree_hash
from .contracts import (
    Refused,
    digest,
    expected_plan,
    keys,
    request_goal,
    retain_goal,
    validate_plan,
)
from .persistence import atomic_json, read_checkpoint, write_checkpoint
from .profiles import PROFILES, stages

TRANSITIONS = {
    "CAPTURED": "SOURCE_CHECKED",
    "SOURCE_CHECKED": "GOAL_CHECKED",
    "GOAL_CHECKED": "PLAN_VALIDATED",
    "PLAN_VALIDATED": "REVIEW_APPROVED",
    "REVIEW_APPROVED": "AUTHORIZED",
    "AUTHORIZED": "ONSET_CREATED",
    "ONSET_CREATED": "ONSET_CHECKED",
    "ONSET_CHECKED": "MAP_CREATED",
    "MAP_CREATED": "DATA_VERIFIED",
    "DATA_VERIFIED": "FINAL_VERIFIED",
    "FINAL_VERIFIED": "RELEASED",
}


def read_gate(process, schema, artifact, replay=False):
    """Gate text alone cannot authorize: parse process output and cross-check its actual exit."""
    if process["exit_code"] != 0 or process.get("stop_reason"):
        raise Refused("gate process failed or was interrupted")
    try:
        gate = json.loads(process["stdout"])
    except ValueError as exc:
        raise Refused("missing machine-readable gate evidence") from exc
    if not isinstance(gate, dict):
        raise Refused("gate must be an object")
    if (
        gate.get("schema") != schema
        or gate.get("exit_code") != process["exit_code"]
        or gate.get("verdict") != "PASS"
        or not gate.get("checks")
        or Path(gate.get("artifact", "")).resolve() != Path(artifact).resolve()
    ):
        raise Refused("gate identity, schema, scope or verdict mismatch")
    if not isinstance(gate["checks"], list) or any(not isinstance(c, dict) for c in gate["checks"]):
        raise Refused("malformed gate checks")
    if any(c.get("status") in {"FAIL", "BLOCK", "UNVERIFIABLE"} for c in gate["checks"]):
        raise Refused("gate includes an unsuccessful required check")
    if replay and not any(
        c.get("kind") == "replay" and c.get("status") == "PASS" for c in gate["checks"]
    ):
        raise Refused("no successful final replay comparison")
    return gate


class Controller:
    def __init__(
        self,
        repo,
        request,
        source,
        run_dir,
        processes,
        roles,
        max_repairs=1,
        policy_mode="rules",
        hooks=None,
        admission=None,
    ):
        self.repo = safe_path(repo)
        self.request = json.loads(json.dumps(request))
        self.goal = request_goal(self.request)
        self.source = safe_path(source)
        self.admission = admission
        self.admission_record = record_for(admission, self.source)
        self.admission_hash = admission_identity(self.admission_record)
        self.root = safe_path(run_dir)
        if (
            self.root == self.source
            or self.root.is_relative_to(self.source)
            or self.source.is_relative_to(self.root)
        ):
            raise Refused("source and run output namespaces must be disjoint")
        # CLI creates only the run root/process directory. Any output/evidence reuse is refused.
        self.outputs = self.root / "artifacts"
        self.outputs.mkdir(exist_ok=False)
        self.paths = {
            "source": self.source,
            "onset": self.outputs / "onset.zarr",
            "map": self.outputs / "onset.png",
        }
        self.processes, self.roles = processes, roles
        self.profile = PROFILES[self.goal["task"]]["id"]
        self.sequence = stages(self.goal["task"])
        self.context = {}
        self.hooks = hooks
        if hooks is not None:
            self.context["injection_harness"] = True
        self.evidence = {}
        self.pending = None
        self.repairs_used = 0
        self.max_repairs = max_repairs
        self.resumes = 0
        self.policy_mode = policy_mode
        self.elapsed_before = 0
        self.started = time.monotonic()
        self.state = "CAPTURED"
        self.events = []
        self.source_hash = tree_hash(self.source)
        self.build = build_manifest(self.repo)
        self.build_hash = digest(self.build)
        self.request_hash = digest(self.request)
        self.artifacts = {"source": self.source_hash}
        self.binding = None
        self.plan = None
        self.save("request.json", self.request)
        self.save("build.json", self.build)
        self.save("admission.json", self.admission_record)
        self.checkpoint()

    def save(self, name, value):
        atomic_json(self.root / name, value)
        self.evidence[name] = tree_hash(self.root / name)

    def checkpoint(self):
        write_checkpoint(
            self.root,
            {
                "state": self.state,
                "events": self.events,
                "profile": self.profile,
                "admission_record": self.admission_record,
                "admission_hash": self.admission_hash,
                "repo": str(self.repo),
                "root": str(self.root),
                "source": str(self.source),
                "request": self.request,
                "source_hash": self.source_hash,
                "build": self.build,
                "build_hash": self.build_hash,
                "request_hash": self.request_hash,
                "artifacts": self.artifacts,
                "binding": self.binding,
                "plan": self.plan,
                "context": self.context,
                "evidence": self.evidence,
                "pending": self.pending,
                "repairs_used": self.repairs_used,
                "max_repairs": self.max_repairs,
                "resumes": self.resumes,
                "policy_mode": self.policy_mode,
                "role_receipts": self.roles.receipts,
                "spent": self.roles.spent,
                "budget": getattr(self.roles, "budget", 5),
                "model": getattr(self.roles, "model", "fixture"),
                "elapsed_s": self.elapsed_before + time.monotonic() - self.started,
                "total_seconds": getattr(self.processes, "total_seconds", 1800),
            },
        )

    @classmethod
    def resume(cls, root, processes, roles, hooks=None, admission=None):
        root = safe_path(root)
        value = read_checkpoint(root)
        if value["pending"]:
            raise Refused("interrupted operation has uncertain outcome; start a new attempt")
        if value["resumes"] >= 2:
            raise Refused("resume limit exhausted; start a new attempt")
        self = cls.__new__(cls)
        for key in (
            "state",
            "events",
            "profile",
            "request",
            "source_hash",
            "build",
            "build_hash",
            "request_hash",
            "artifacts",
            "binding",
            "plan",
            "context",
            "evidence",
            "pending",
            "repairs_used",
            "max_repairs",
            "policy_mode",
        ):
            setattr(self, key, value[key])
        self.repo, self.root, self.source = map(
            safe_path, (value["repo"], value["root"], value["source"])
        )
        if self.root != root:
            raise Refused("checkpoint belongs to another run directory")
        self.goal = request_goal(self.request)
        self.admission = admission
        self.admission_record = record_for(admission, self.source)
        self.admission_hash = admission_identity(self.admission_record)
        if (
            value.get("admission_record") != self.admission_record
            or value.get("admission_hash") != self.admission_hash
        ):
            raise Refused("resume requires the identical source admission policy and registration")
        self.hooks = hooks
        if self.context.get("injection_harness") and hooks is None:
            raise Refused("injected test run requires its original harness for resume")
        self.sequence = stages(self.goal["task"])
        if self.profile != PROFILES[self.goal["task"]]["id"]:
            raise Refused("checkpoint profile mismatch")
        expected = list(zip(self.sequence, self.sequence[1:], strict=False))
        if self.state not in self.sequence or len(self.events) != self.sequence.index(self.state):
            raise Refused("checkpoint stage history is incomplete")
        for index, event in enumerate(self.events):
            if (event["from"], event["to"]) != expected[index] or digest(
                event["evidence"]
            ) != event["evidence_hash"]:
                raise Refused("checkpoint transition evidence is damaged")
        self.outputs = safe_path(root / "artifacts")
        self.paths = {
            "source": self.source,
            "onset": self.outputs / "onset.zarr",
            "map": self.outputs / "onset.png",
        }
        self.processes, self.roles = processes, roles
        roles.receipts = value["role_receipts"]
        roles.spent = value["spent"]
        roles.budget = value["budget"]
        roles.model = value["model"]
        roles.sessions = {r["session_id"] for r in roles.receipts}
        self.elapsed_before, self.started = value["elapsed_s"], time.monotonic()
        remaining = value["total_seconds"] - self.elapsed_before
        if remaining <= 0 or roles.spent >= roles.budget:
            raise Refused("original run budget exhausted; resume cannot reset limits")
        processes.total_seconds = value["total_seconds"]
        processes.deadline = min(
            getattr(processes, "deadline", float("inf")), time.monotonic() + remaining
        )
        self.resumes = value["resumes"] + 1
        self.integrity()
        self.checkpoint()
        return self

    def advance(self, target, evidence):
        index = self.sequence.index(self.state)
        if index + 1 >= len(self.sequence) or self.sequence[index + 1] != target:
            raise Refused(f"illegal transition: {self.state} -> {target}")
        self.events.append(
            {
                "from": self.state,
                "to": target,
                "evidence_hash": digest(evidence),
                "evidence": json.loads(json.dumps(evidence)),
            }
        )
        self.state = target
        self.checkpoint()
        self.emit("after_transition", {"state": self.state})

    def emit(self, point, event):
        """Trusted Python harness hook, never exposed as an agent tool or CLI permission."""
        if self.hooks is not None:
            self.hooks(point, {"state": self.state, **event}, self)

    def ask(self, role, payload):
        self.integrity()
        self.emit("before_role", {"role": role, "payload": payload})
        before = len(self.roles.receipts)
        self.pending = {"kind": "model", "role": role, "input_hash": digest(payload)}
        self.checkpoint()
        try:
            answer = self.roles.ask(role, payload)
        except Exception:
            self.pending = None
            self.checkpoint()
            raise
        self.pending = None
        self.emit("after_role", {"role": role, "answer": answer})
        if len(self.roles.receipts) != before + 1 or self.roles.receipts[-1]["role"] != role:
            self.checkpoint()
            raise Refused("independent role invocation evidence is incomplete")
        self.save(
            f"role-{len(self.roles.receipts):03}-{role}.json",
            {"input": payload, "output": answer, "receipt": self.roles.receipts[-1]},
        )
        self.checkpoint()
        return answer

    def date_contract(self):
        return {
            "input_window": {"start": self.goal["start_date"], "end": self.goal["end_date"]},
            "required_complete_window_days": 30,
            "eligible_onset_window": {
                "start": self.goal["start_date"],
                "end": (date.fromisoformat(self.goal["end_date"]) - timedelta(days=29)).isoformat(),
            },
            "rule": "Candidate t requires observations through t+29 inclusive. Plot dates are color scale bounds, not input subsetting. The derived last eligible onset date is intentionally earlier than the input end date.",
        }

    def repair(self, payload, failure):
        if self.repairs_used >= self.max_repairs:
            raise Refused("bounded plan repair exhausted: " + failure)
        self.repairs_used += 1
        self.checkpoint()  # a crash cannot reset the retry allowance
        value = self.ask(
            "repairer",
            {
                **payload,
                "failure": failure,
                "prior_plan": self.plan,
                "date_contract": self.date_contract(),
                "instruction": "Repair only within this exact task and profile. Explain a factual correction to review if needed; no changes to authorization or scientific scope.",
            },
        )
        keys(value, {"plan", "response"}, "repair")
        validate_plan(value["plan"], self.goal)
        if not isinstance(value["response"], str) or len(value["response"]) > 4000:
            raise Refused("invalid bounded repair response")
        self.context["repair_response"] = value["response"]
        self.plan = value["plan"]
        self.preflight_plan()
        self.checkpoint()

    def preflight_plan(self):
        validate_plan(self.plan, self.goal)
        for step in self.plan["steps"]:
            argv = [
                "--input",
                str(self.paths[step["input"]]),
                "--output",
                str(self.paths[step["output"]]),
            ]
            for key, value in step["args"].items():
                argv += ["--" + key.replace("_", "-"), value]
            preflight(self.repo, step["skill"], argv)

    def integrity(self):
        if (
            record_for(self.admission, self.source) != self.admission_record
            or admission_identity(self.admission_record) != self.admission_hash
        ):
            raise Refused("source admission policy changed")
        for name, expected in self.evidence.items():
            if tree_hash(self.root / name) != expected:
                raise Refused("controller evidence changed: " + name)
        if tree_hash(self.source) != self.source_hash:
            raise Refused("source changed since admission")
        if digest(build_manifest(self.repo)) != self.build_hash:
            raise Refused("code/documentation changed since admission; review is stale")
        if digest(self.request) != self.request_hash or request_goal(self.request) != self.goal:
            raise Refused("request/authorization changed since admission")
        for name, value in self.artifacts.items():
            if tree_hash(self.paths[name]) != value:
                raise Refused(f"artifact changed since its producing step: {name}")
        if self.binding and self.current_binding() != self.binding:
            raise Refused("plan or review binding changed")

    def current_binding(self):
        return digest(
            {
                "request": self.request_hash,
                "goal": self.goal,
                "plan": self.plan,
                "build": self.build_hash,
                "source": self.source_hash,
                "admission": self.admission_hash,
            }
        )

    def tool(self, skill, argv, allowed_state):
        if self.state != allowed_state:
            raise Refused(f"{skill} cannot run at {self.state}")
        self.integrity()
        parsed, _ = preflight(self.repo, skill, argv)
        # Arguments come exclusively from controller-constructed plans, never raw agent argv.
        command = [sys.executable, str(script(self.repo, skill)), *argv]
        self.emit("before_tool", {"skill": skill, "argv": argv})
        self.integrity()
        self.pending = {"kind": "tool", "skill": skill, "argv": argv}
        self.checkpoint()
        try:
            result = self.processes.run(
                command, self.outputs, child_environment(self.root / "scratch")
            )
        except Exception:
            # The process manager stops owned children before raising. A hard crash leaves pending set.
            self.pending = None
            self.checkpoint()
            raise
        self.pending = None
        self.emit("after_tool", {"skill": skill, "argv": argv, "process": result})
        self.integrity()
        self.save(f"tool-{self.state}-{skill}.json", {"parsed": parsed, "process": result})
        self.checkpoint()
        return result

    def check(self, name, state):
        goal = self.goal
        argv = [
            "--input",
            str(self.paths[name]),
            "--bbox",
            "/".join(map(str, goal["bbox"])),
            "--start-time",
            goal["start_date"],
            "--end-time",
            goal["end_date"],
            "--format",
            "json",
        ]
        if name == "source":
            argv += ["--expect-units", "mm day-1", "--variable", "precip"]
        return read_gate(
            self.tool("check-artifact", argv, state), "check-artifact.gate/1", self.paths[name]
        )

    def inspect_source(self):
        import numpy as np
        import xarray as xr
        from weather_skills_core.provenance import load_history

        with xr.open_zarr(self.source, consolidated=None) as ds:
            if (
                set(ds.sizes) != {"time", "latitude", "longitude"}
                or not 30 <= ds.sizes["time"] <= 370
                or max(ds.sizes["latitude"], ds.sizes["longitude"]) > 500
            ):
                raise Refused("source does not match the bounded daily CHIRPS profile")
            wanted = np.arange(
                np.datetime64(self.goal["start_date"]),
                np.datetime64(self.goal["end_date"]) + np.timedelta64(1, "D"),
            )
            if not np.array_equal(ds.time.values.astype("datetime64[D]"), wanted):
                raise Refused("source dates/cadence do not retain the requested window")
            if "precip" not in ds or set(ds.precip.dims) != {"time", "latitude", "longitude"}:
                raise Refused("source variable/dimensions differ")
            observed = [
                float(ds.latitude.max()),
                float(ds.longitude.min()),
                float(ds.latitude.min()),
                float(ds.longitude.max()),
            ]
            if any(abs(a - b) > 0.051 for a, b in zip(observed, self.goal["bbox"], strict=True)):
                raise Refused("source spatial extent differs from the explicit bbox")
            metadata = {
                "dims": dict(ds.sizes),
                "bounds": observed,
                "start": self.goal["start_date"],
                "end": self.goal["end_date"],
                "units": ds.precip.attrs.get("units"),
                "source": "chirps" if self.admission is None else "synthetic",
            }
            if self.admission is None:
                if (
                    ds.attrs.get("weather_skills_source") != "chirps"
                    or ds.attrs.get("synthetic") in (True, "true")
                    or "simulation_fixture" in ds.attrs
                ):
                    raise Refused(
                        "production admission requires observed CHIRPS, not synthetic data"
                    )
            else:
                self.admission.verify(self.source)
                if (
                    ds.attrs.get("synthetic") != "true"
                    or ds.attrs.get("generator") != self.admission_record["generator_id"]
                ):
                    raise Refused("synthetic registration does not match source identity")
        history = load_history(self.source)
        if self.admission is None and (
            not isinstance(history, list)
            or len(history) != 1
            or history[0].get("skill") != "chirps-fetch"
        ):
            raise Refused("only a direct cached CHIRPS fetch is admitted")
        return metadata

    def docs(self):
        return {
            name: (self.repo / "skills" / name / "SKILL.md").read_text(encoding="utf-8")
            for name in ("onset-date", "plot-onset", "check-artifact", "verify-run")
        }

    def execute_step(self, index, state, target):
        if self.state != state:
            raise Refused("producing step requested out of order")
        validate_plan(self.plan, self.goal)
        step = self.plan["steps"][index]
        output = self.paths[step["output"]]
        if output.exists():
            raise Refused("output exists; never overwrite/reuse a previous attempt")
        argv = ["--input", str(self.paths[step["input"]]), "--output", str(output)]
        for key, value in step["args"].items():
            argv += ["--" + key.replace("_", "-"), value]
        process = self.tool(step["skill"], argv, state)
        if process["exit_code"] != 0:
            raise Refused(f"{step['skill']} failed: {process['stderr'][-500:]}")
        self.artifacts[step["output"]] = tree_hash(output)
        self.advance(target, self.artifacts)

    def run(self):
        try:
            result = self._run()
        except Exception as exc:
            result = {
                "status": "INCOMPLETE",
                "state": self.state,
                "error": str(exc),
                "stop_kind": "gate_or_contract_refusal"
                if isinstance(exc, Refused)
                else "runtime_failure",
                "role_receipts": self.roles.receipts,
                "model_cost_usd": self.roles.spent,
            }
            self.checkpoint()
            atomic_json(self.root / "result.json", result)
            raise
        else:
            atomic_json(self.root / "result.json", result)
            return result
        finally:
            from .reporting import report

            report(self.root)

    def _run(self):
        self.integrity()
        self.emit("before_run", {})
        if self.context.get("clarification_required"):
            raise Refused(
                "review requires user clarification; start a new request after the answer"
            )
        if self.state == "CAPTURED":
            self.context["metadata"] = self.inspect_source()
            self.context["source_gate"] = self.check("source", "CAPTURED")
            self.advance("SOURCE_CHECKED", self.context["source_gate"])
        if self.state == "SOURCE_CHECKED":
            payload = {
                "execution_context": self.execution_context(),
                "messages": self.request["messages"],
                "schema": {k: type(v).__name__ for k, v in self.goal.items()},
                "rules": "task is onset_map or onset_data as explicitly requested; dataset=chirps; variable=precip; missing_policy=preserve_nan; baseline=null when prohibited; ensemble_claim=false for deterministic data; bbox numeric [north,west,south,east]; dates ISO; preserve title exactly.",
            }
            compiles = self.context.setdefault("compiles", [])
            while len(compiles) < 3:
                candidate = self.ask("compiler", payload)
                retain_goal(candidate, self.goal)
                compiles.append(candidate)
                self.checkpoint()
            self.save("compiled-goals.json", compiles)
            self.advance("GOAL_CHECKED", compiles)
        docs = self.docs()
        plan_payload = {
            "execution_context": self.execution_context(),
            "goal": self.goal,
            "goal_hash": digest(self.goal),
            "source": self.context["metadata"],
            "documentation": docs,
            "date_contract": self.date_contract(),
            "profile_template": expected_plan(self.goal),
            "instruction": "Return the exact typed plan this scientific profile permits, preserving every constraint.",
        }
        if self.state == "GOAL_CHECKED":
            if not self.plan:
                self.plan = self.ask("planner", plan_payload)
            try:
                self.preflight_plan()
            except Refused as exc:
                self.repair(plan_payload, str(exc))
            self.binding = self.current_binding()
            self.save("plan.json", {"plan": self.plan, "binding_hash": self.binding})
            self.advance("PLAN_VALIDATED", self.plan)
        if self.state == "PLAN_VALIDATED":
            while True:
                review_payload = {
                    "execution_context": self.execution_context(),
                    "binding_hash": self.binding,
                    "request": self.request,
                    "plan": self.plan,
                    "source": self.context["metadata"],
                    "source_gate": self.context["source_gate"],
                    "documentation": docs,
                    "date_contract": self.date_contract(),
                    "preflight": "actual parsers accepted",
                    "prior_review": self.context.get("rejected_review"),
                    "repair_response": self.context.get("repair_response"),
                    "controller_gates": "Mandatory onset check then strict final data replay for every declared final artifact; no source refetch.",
                }
                if self.context.get("review_retry_required"):
                    self.repair(plan_payload, json.dumps(self.context["rejected_review"]))
                    self.context["review_retry_required"] = False
                    self.binding = self.current_binding()
                    self.save("plan.json", {"plan": self.plan, "binding_hash": self.binding})
                    self.checkpoint()
                    continue
                review = self.ask("reviewer", review_payload)
                keys(review, {"verdict", "binding_hash", "findings"}, "review")
                if review["binding_hash"] != self.binding or not isinstance(
                    review["findings"], list
                ):
                    raise Refused("review binding or findings invalid")
                self.save(f"review-{self.repairs_used}.json", review)
                if review["verdict"] == "APPROVE":
                    self.context["review"] = review
                    break
                if review["verdict"] != "REJECT":
                    self.context["clarification_required"] = review
                    self.checkpoint()
                    raise Refused("review requires user clarification; no automatic scope change")
                self.context["rejected_review"] = review
                self.context["review_retry_required"] = True
                self.checkpoint()
            self.integrity()
            self.advance("REVIEW_APPROVED", review)
        if self.state == "REVIEW_APPROVED":
            self.integrity()
            self.advance("AUTHORIZED", self.request["authorization"])
        if self.state == "AUTHORIZED":
            if self.policy_mode != "rules":
                prompt = policy.question(
                    self.state,
                    self.binding,
                    ["EXECUTE", "STOP"],
                    {"review": self.context["review"]},
                )
                prediction = policy.validate(self.ask("router", prompt), prompt)
                self.save(
                    "policy.json",
                    {
                        "mode": self.policy_mode,
                        "question": prompt,
                        "prediction": prediction,
                        "applied_action": prediction["action"]
                        if self.policy_mode == "model"
                        else "EXECUTE",
                    },
                )
                if self.policy_mode == "model" and prediction["action"] == "STOP":
                    raise Refused("routing policy selected STOP")
            self.execute_step(0, "AUTHORIZED", "ONSET_CREATED")
        if self.state == "ONSET_CREATED":
            self.context["onset_gate"] = self.check("onset", "ONSET_CREATED")
            self.advance("ONSET_CHECKED", self.context["onset_gate"])
        if self.state == "ONSET_CHECKED" and self.goal["task"] == "onset_map":
            self.execute_step(1, "ONSET_CHECKED", "MAP_CREATED")
        before_verify = "MAP_CREATED" if self.goal["task"] == "onset_map" else "ONSET_CHECKED"
        if self.state == before_verify:
            gates = self.context.setdefault("gates", {})
            for name in self.plan["final"]:
                if name in gates:
                    continue
                argv = [
                    "--input",
                    str(self.paths[name]),
                    "--require-replay",
                    "--format",
                    "json",
                    "--search-dir",
                    str(self.source.parent),
                    "--search-dir",
                    str(self.outputs),
                ]
                gates[name] = read_gate(
                    self.tool("verify-run", argv, before_verify),
                    "verify-run.gate/1",
                    self.paths[name],
                    replay=True,
                )
                self.checkpoint()
            self.save("verification.json", gates)
            self.advance("DATA_VERIFIED", gates)
        gates = self.context["gates"]
        final_binding = digest(
            {"review_binding": self.binding, "artifacts": self.artifacts, "gates": gates}
        )
        if self.state == "DATA_VERIFIED":
            verdict = self.ask(
                "verifier",
                {
                    "binding_hash": final_binding,
                    "artifacts": self.artifacts,
                    "gates": gates,
                    "execution_context": self.execution_context(),
                },
            )
            keys(verdict, {"verdict", "binding_hash", "scope"}, "verifier")
            if verdict != {
                "verdict": "PASS",
                "binding_hash": final_binding,
                "scope": "data-replay",
            }:
                raise Refused(
                    "independent verification role did not confirm the exact final evidence"
                )
            self.integrity()
            receipts = self.roles.receipts
            names = [r["role"] for r in receipts]
            if (
                names.count("compiler") < 3
                or "planner" not in names
                or "reviewer" not in names
                or names[-1] != "verifier"
                or len({r["session_id"] for r in receipts}) != len(receipts)
            ):
                raise Refused("independent role invocation evidence is incomplete")
            self.advance("FINAL_VERIFIED", {"verdict": verdict, "receipt": receipts[-1]})
        if self.state == "FINAL_VERIFIED":
            self.integrity()
            self.advance("RELEASED", {"artifacts": self.artifacts, "binding_hash": final_binding})
        return {
            "status": "PASS",
            "state": self.state,
            "profile": self.profile,
            "source_admission": self.admission_record,
            "classification": self.admission_record["classification"],
            "artifacts": {k: str(self.paths[k]) for k in self.plan["final"]},
            "artifact_hashes": self.artifacts,
            "binding_hash": final_binding,
            "role_receipts": self.roles.receipts,
            "model_cost_usd": self.roles.spent,
            "source_unchanged": True,
            "scope": "calculated data replay; pixels and source-observation truth excluded",
        }

    def execution_context(self):
        return {
            "classification": self.admission_record["classification"],
            "admission_hash": self.admission_hash,
            "source_origin": "synthetic" if self.admission is not None else "chirps",
            "scenario_dataset": self.goal["dataset"],
            "instruction": "This is an explicitly authorized synthetic evaluation of the CHIRPS-shaped scenario. Preserve the scenario goal; report evaluation artifacts only, never observations."
            if self.admission is not None
            else "Production cached CHIRPS workflow.",
        }
