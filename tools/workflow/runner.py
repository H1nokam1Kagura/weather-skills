"""Controller for cached-onset/v1. No state can be advanced by an agent-written file."""

import functools
import inspect
import json
import sys
import time
import uuid
from datetime import date, timedelta
from pathlib import Path

from . import (
    claim_gates,
    operator_ledger,
    policy,
    process_receipts,
    recovery,
    review_evidence,
    shadow,
    trace_records,
    write_boundary,
)
from .admission import identity as admission_identity
from .admission import record_for
from .catalog import (
    build_manifest,
    child_environment,
    confined_process,
    preflight,
    safe_path,
    script,
    tree_hash,
)
from .contracts import (
    GATE_REASONS,
    INCOMPLETE_GATES,
    NeedsInfo,
    Refused,
    digest,
    expected_plan,
    gated,
    keys,
    needs_info,
    plan_failed_fields,
    plan_version_hash,
    request_goal,
    tag,
    validate_plan,
)
from .persistence import atomic_json, read_checkpoint, write_checkpoint
from .profiles import PROFILES, stages

# CAP-REASON-MAP, call-site half. A refusal raised inside a role transport (agents.py,
# kong_roles.py, or a custom Roles) without its own gate is tagged with the gate the role serves:
# the role's output could not be obtained or was not acceptable, so that gate failed.
# Exception (owner ruling role_cli_resource_gate_2026-10-14): a role CLI process the process
# manager refused or stopped for its RAM floor or wall-time guard arrives already tagged
# resource_guard at the raise site (processes.py) and keeps it; tag() never replaces a set gate.
ROLE_GATES = {
    "compiler": "goal_check",
    "planner": "plan_validation",
    "repairer": "plan_validation",
    "reviewer": "review",
    "verifier": "verifier",
    "router": "transition",
}
# Last-resort mapping for a refusal that reached the terminal with no gate at all (a raise
# site outside this module that no call site tagged). The terminal records
# terminal_mapping.source == "state_fallback" so the inference is never silent.
STATE_GATES = {
    "CAPTURED": "source_check",
    "SOURCE_CHECKED": "goal_check",
    "GOAL_CHECKED": "plan_validation",
    "PLAN_VALIDATED": "review",
    "REVIEW_APPROVED": "authorization",
    "AUTHORIZED": "transition",
    "ONSET_CREATED": "gate_evidence",
    "ONSET_CHECKED": "transition",
    "MAP_CREATED": "replay_evidence",
    "DATA_VERIFIED": "verifier",
    "FINAL_VERIFIED": "transition",
    "RELEASED": "transition",
}
# Modules outside the controller's file set whose raises carry no gate yet, and the controller
# call-site gate applied to them (census-checked in test_runner_lifecycle_v27).
CALL_SITE_GATES = {
    "agents": "ROLE_GATES by role (Controller.ask)",
    "kong_roles": "ROLE_GATES by role (Controller.ask)",
    "processes": (
        "resource_guard (Controller.tool); RAM/time guard refusals and stops carry "
        "resource_guard from the raise site for role CLIs too; the output-budget stop of a "
        "role CLI via ROLE_GATES"
    ),
    "catalog": "plan_validation for preflight, gate_evidence for artifact hashes",
    "admission": "source_check (inspect_source, integrity)",
    "review_evidence": "review_binding (capture), claim_evidence (plan envelope), else state",
    "policy": "transition (router decision)",
    "persistence": "resume_verification (resume) / CLI lock",
    "evaluation": "not reached from Controller",
    "simulation": "not reached from Controller",
}
VERIFIER_SUB_REASONS = ("BLOCK", "UNVERIFIABLE")
PROCESS_EVENT_RESERVED = {"schema", "run_id", "sequence", "resume_phase", "kind"}


def supports_process_receipts(processes):
    """The process manager must emit on_event receipts and accept run(..., tool=...)."""
    if not hasattr(processes, "on_event"):
        return False
    try:
        parameters = inspect.signature(processes.run).parameters
    except (TypeError, ValueError):
        return False
    return "tool" in parameters or any(
        p.kind is inspect.Parameter.VAR_KEYWORD for p in parameters.values()
    )


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


# CAP-REREVIEW (CP-E09): approved-binding objects a re-review may follow (EVIDENCE_CONTRACT.md
# v2_4.material_changes object vocabulary request|goal|plan|manifest|source), the states an
# approval is active in, and the re-review cap per plan (UC-05: "if >2 then re-plan").
APPROVED_STATES = ("REVIEW_APPROVED", "AUTHORIZED")
MAX_REREVIEWS_PER_PLAN = 2
CURRENT_CLAIMS = object()  # record_plan_validation: hash with the current plan's claims


class ReturnToReview(Refused):
    """An approved binding changed before anything was produced. Raised by integrity()
    without mutating state; Controller.run returns the run to PLAN_VALIDATED. Anywhere else
    (e.g. resume verification) it is an ordinary review_binding refusal."""

    def __init__(self, message, changes):
        super().__init__(message, gate="review_binding", reason="approval_invalidated")
        self.changes = changes


def read_gate(process, schema, artifact, replay=False):
    """Gate text alone cannot authorize: parse process output and cross-check its actual exit."""
    code = "replay_evidence" if replay else "gate_evidence"
    if process["exit_code"] != 0 or process.get("stop_reason"):
        raise Refused("gate process failed or was interrupted", gate=code)
    try:
        gate = json.loads(process["stdout"])
    except ValueError as exc:
        raise Refused("missing machine-readable gate evidence", gate=code) from exc
    if not isinstance(gate, dict):
        raise Refused("gate must be an object", gate=code)
    if (
        gate.get("schema") != schema
        or gate.get("exit_code") != process["exit_code"]
        or gate.get("verdict") != "PASS"
        or not gate.get("checks")
        or Path(gate.get("artifact", "")).resolve() != Path(artifact).resolve()
    ):
        raise Refused("gate identity, schema, scope or verdict mismatch", gate=code)
    if not isinstance(gate["checks"], list) or any(not isinstance(c, dict) for c in gate["checks"]):
        raise Refused("malformed gate checks", gate=code)
    if any(c.get("status") in {"FAIL", "BLOCK", "UNVERIFIABLE"} for c in gate["checks"]):
        raise Refused("gate includes an unsuccessful required check", gate=code)
    if replay and not any(
        c.get("kind") == "replay" and c.get("status") == "PASS" for c in gate["checks"]
    ):
        raise Refused("no successful final replay comparison", gate="replay_evidence")
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
        review_evidence_mode=False,
        dispatch_evidence_mode=False,
        definition_source=None,
        operator_ledger_path=None,
        operator_run_id=None,
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
            raise Refused("source and run output namespaces must be disjoint", gate="write_denial")
        if dispatch_evidence_mode and not supports_process_receipts(processes):
            # Never silently omit process receipts from an evidence-mode run.
            raise Refused(
                "dispatch_evidence_mode requires a process manager that emits on_event "
                "receipts and accepts run(..., tool=...)",
                gate="gate_evidence",
            )
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
        if dispatch_evidence_mode:
            self.context["dispatch_evidence"] = {
                "schema": "workflow.dispatch-evidence/1",
                "run_id": str(uuid.uuid4()),
                "sequence": 0,
                "occurrences": {},
            }
        if review_evidence_mode:
            self.context["review_evidence"] = {
                "schema": review_evidence.SCHEMA,
                "run_id": str(uuid.uuid4()),
                "reviewer_occurrence": 0,
            }
            if definition_source is not None:
                # D1/D10: an explicitly supplied reference definition enables claim gates.
                self.context["review_evidence"]["definition_source"] = (
                    claim_gates.bind_definition_source(definition_source)
                )
                self.context["review_evidence"]["claim_checks"] = []
        elif definition_source is not None:
            raise Refused("claim gates require review evidence mode", gate="claim_evidence")
        self.bind_process_events()
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
        # D14/D28: optional attributed operator ledger, created by this run and read only at
        # wait boundaries (consult_operator_ledger). Absent => no operator_actions at all.
        operator_ledger.open_channel(self, operator_ledger_path, operator_run_id)
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
    def request_resume(cls, root, processes, roles, request, hooks=None, admission=None):
        """Resume-request API: requester attribution and requested overrides (D18).

        Every request gets a durable decision record (controller_trace.resume[*]) before
        this returns the resumed controller or raises the refusal.
        """
        return cls.resume(root, processes, roles, hooks=hooks, admission=admission, request=request)

    @classmethod
    def resume(
        cls, root, processes, roles, hooks=None, admission=None, request=None, supervisor=None
    ):
        """Itemized re-verification within the original limits; one decision per request.

        request None is the legacy unattributed resume (initiated_by null, capped at 2 as
        before). Automatic resumes need the run loop's supervisor capability and are capped
        at recovery.MAX_AUTOMATIC_RESUMES; attributed operator-ledger resumes are uncapped
        (UC-16/R15) but pass the same verification within the original limits.
        """
        root = safe_path(root)
        with gated("resume_verification"):
            value = read_checkpoint(root)  # unreadable: no durable place for a decision
        attribution, override, problems = recovery.parse_request(request, supervisor)
        book = recovery.ledger(value["context"])
        # checkpoint_counters + controller_pid; an observed stop is recorded once (wave 7).
        decision, interruption = recovery.open_decision(root, value, request, attribution, override)
        verify = "resume_verification"
        checked, failed, refusals = [], [], []

        def fail(item, refusal):
            failed.append(item)
            refusals.append(refusal)

        initiated = attribution["initiated_by"]
        checked.append("attribution")
        for problem in problems:
            fail(problem, Refused("resume request attribution invalid: " + problem, gate=verify))
        checked.append("pending_operation")
        if value["pending"]:
            fail(
                "pending_operation:" + str(recovery.pending_name(value["pending"])),
                Refused(
                    "interrupted operation has uncertain outcome; start a new attempt",
                    gate="pending_operation",
                ),
            )
        checked.append("resume_limit")
        exhausted = Refused(
            "resume limit exhausted; start a new attempt",
            gate="interrupted",
            reason="repeated_interruption",
        )
        if initiated == "automatic":
            if book["counters"]["automatic_resumes_used"] >= recovery.MAX_AUTOMATIC_RESUMES:
                fail("automatic_resume_limit", exhausted)
            if interruption is None or not interruption["transient"]:
                fail(
                    "interruption:not_transient",
                    Refused("automatic resume needs a transient interruption", gate=verify),
                )
        # Legacy cap for an unattributed resume, applied below unless the operator channel
        # binds an accepted user resume record to it (operator resumes are uncapped).
        legacy_capped = initiated is None and value["resumes"] >= 2
        self = cls.__new__(cls)
        self.resumes = value["resumes"]
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
        checked.append("run_directory")
        if self.root != root:
            fail(
                "run_directory",
                Refused("checkpoint belongs to another run directory", gate=verify),
            )
        checked.append("goal")
        try:
            with gated(verify, override=True):
                self.goal = request_goal(self.request)
        except Refused as exc:
            fail("goal", exc)
            self.goal = None
        self.admission = admission
        checked.append("admission_policy")
        try:
            with gated(verify):
                self.admission_record = record_for(admission, self.source)
                self.admission_hash = admission_identity(self.admission_record)
        except Refused as exc:
            fail("admission_policy", exc)
            self.admission_record = value.get("admission_record")
            self.admission_hash = value.get("admission_hash")
        else:
            if (
                value.get("admission_record") != self.admission_record
                or value.get("admission_hash") != self.admission_hash
            ):
                fail(
                    "admission_policy",
                    Refused(
                        "resume requires the identical source admission policy and registration",
                        gate=verify,
                    ),
                )
        self.hooks = hooks
        checked.append("harness")
        if self.context.get("injection_harness") and hooks is None:
            fail(
                "harness",
                Refused("injected test run requires its original harness for resume", gate=verify),
            )
        checked.append("history")
        if self.goal is not None:
            self.sequence = stages(self.goal["task"])
            expected = list(zip(self.sequence, self.sequence[1:], strict=False))
            if self.profile != PROFILES[self.goal["task"]]["id"]:
                fail("profile", Refused("checkpoint profile mismatch", gate=verify))
            elif self.state not in self.sequence or len(self.events) != self.sequence.index(
                self.state
            ):
                fail("history", Refused("checkpoint stage history is incomplete", gate=verify))
            elif any(
                (event["from"], event["to"]) != expected[index]
                or digest(event["evidence"]) != event["evidence_hash"]
                for index, event in enumerate(self.events)
            ):
                fail("history", Refused("checkpoint transition evidence is damaged", gate=verify))
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
        checked.append("budget")
        if remaining <= 0 or roles.spent >= roles.budget:
            fail(
                "budget",
                Refused(
                    "original run budget exhausted; resume cannot reset limits",
                    gate="budget",
                    reason="resume_refused",
                ),
            )
        if self.goal is not None:
            # Itemized integrity (never parsed from exception text).
            verification = recovery.verify(self)
            checked += verification["checked_items"]
            for item, refusal in zip(
                verification["failed_items"], verification["refusals"], strict=True
            ):
                fail(item, tag(refusal, verify, "override", override=True))
        checked.append("process_receipts")
        if "dispatch_evidence" in self.context and not supports_process_receipts(processes):
            fail(
                "process_receipts",
                Refused(
                    "dispatch_evidence_mode resume requires a process manager that emits "
                    "on_event receipts and accepts run(..., tool=...)",
                    gate="gate_evidence",
                ),
            )
        # Attribution binds pass or fail: a REFUSED operator resume keeps its ledger record.
        attributable = not problems and "run_directory" not in failed
        if attributable and initiated == "operator_ledger":
            checked.append("operator_record")
            bound, problem = recovery.check_operator_record(self, attribution["operator_record_id"])
            if problem:
                fail(problem, Refused("no accepted operator resume record", gate=verify))
            else:
                decision["operator_record_id"] = bound
        elif attributable and initiated is None and override is None:
            # An unattributed request becomes an operator_ledger resume only when the operator
            # channel hands out an accepted user resume record for it (P-01).
            bound, _ = recovery.operator_authorization(self)
            if bound is not None:
                decision["initiated_by"] = initiated = "operator_ledger"
                decision["operator_record_id"] = bound
                checked.append("operator_record")
        elif not failed and initiated == "evidenced_repair":
            checked.append("repair_binding")
            problem = recovery.check_repair_binding(self.context, interruption)
            if problem:
                fail(problem, Refused("no evidenced mechanical repair to resume", gate=verify))
        if legacy_capped and initiated is None:
            fail("resume_limit", exhausted)
        decision["verification"] = {
            "verdict": "FAIL" if failed else "PASS",
            "checked_items": checked,
            "failed_items": failed,
        }
        if override is not None:
            # UC-15: an override request is refused outright, never partially applied.
            refusals.insert(
                0,
                Refused(
                    "resume cannot reset counters or change limits: " + ", ".join(sorted(override)),
                    gate="budget",
                    reason="resume_refused",
                ),
            )
        if refusals:
            recovery.record_refused(root, value, decision, refusals[0], request)
            raise refusals[0]
        processes.total_seconds = value["total_seconds"]
        processes.deadline = min(
            getattr(processes, "deadline", float("inf")), time.monotonic() + remaining
        )
        self.resumes = value["resumes"] + 1
        self.bind_process_events()
        recovery.record_accepted(self, decision, request)  # resume event + checkpoint
        return self

    def dispatch_event(self, kind, **details):
        """One controller-owned sequence for opt-in dispatch and transition evidence."""
        context = self.context.get("dispatch_evidence")
        if context is None:
            return None
        context["sequence"] += 1
        record = {
            "schema": context["schema"],
            "run_id": context["run_id"],
            "sequence": context["sequence"],
            "resume_phase": self.resumes,
            "kind": kind,
            **details,
        }
        self.save(f"dispatch-event-{context['sequence']:06}.json", record)
        return record

    def bind_process_events(self):
        """RECEIPTS process half: route Processes on_event into the controller sequence."""
        if "dispatch_evidence" not in self.context:
            return
        previous = getattr(self.processes, "on_event", None)
        if hasattr(previous, "controller_owner"):
            # Unwrap an earlier controller's binding (e.g. the pre-resume controller) so no
            # event is recorded twice or into a stale sequence; keep any caller observer.
            previous = previous.controller_previous

        def on_event(kind, details):
            self.process_event(kind, details)
            if previous is not None:
                previous(kind, details)

        on_event.controller_owner = self
        on_event.controller_previous = previous
        self.processes.on_event = on_event

    def process_event(self, kind, details):
        """Record launch_check / process_launch / process_exit / signal_stop verbatim."""
        if not isinstance(kind, str) or not kind or not isinstance(details, dict):
            raise Refused("malformed process event", gate="gate_evidence")
        clash = PROCESS_EVENT_RESERVED & set(details)
        if clash:
            raise Refused(
                f"process event overrides controller fields: {sorted(clash)}", gate="gate_evidence"
            )
        record = self.dispatch_event(kind, **json.loads(json.dumps(details)))
        window = getattr(self, "process_window", None)
        if window is not None:  # Controller.tool verifies these at process completion
            window.append((kind, json.loads(json.dumps(details))))
        return record

    def capture_invocation(self, observation):
        if observation["attempt_id"] != self.pending["attempt_id"]:
            raise Refused(
                "dispatch attempt correlation mismatch",
                gate=ROLE_GATES.get(self.pending.get("role"), "transition"),
            )
        self.dispatch_event("invocation_prepared", **observation)
        self.checkpoint()

    def advance(self, target, evidence):
        index = self.sequence.index(self.state)
        if index + 1 >= len(self.sequence) or self.sequence[index + 1] != target:
            raise Refused(f"illegal transition: {self.state} -> {target}", gate="transition")
        self.events.append(
            {
                "from": self.state,
                "to": target,
                "evidence_hash": digest(evidence),
                "evidence": json.loads(json.dumps(evidence)),
            }
        )
        self.state = target
        self.dispatch_event("transition", transition=self.events[-1])
        self.checkpoint()
        if target == "REVIEW_APPROVED" and "review_evidence" in self.context:
            # Acceptance must refer to a transition already durably committed.
            evidence_context = self.context["review_evidence"]
            name = evidence_context["last_capture"]
            snapshot = json.loads((self.root / name).read_text(encoding="utf-8"))
            snapshot["accepted_review"] = True
            snapshot["committed_transition"] = {
                "index": len(self.events) - 1,
                "event": json.loads(json.dumps(self.events[-1])),
                "binding_hash": self.binding,
            }
            self.save(name, snapshot)
            self.checkpoint()
        if target == "REVIEW_APPROVED":
            self.issue_active_approval()
        self.emit("after_transition", {"state": self.state})
        if target == "REVIEW_APPROVED" and self.hooks is not None:
            approval = self.accepted_approval(evidence)
            if approval is not None:
                self.emit("after_review_approval", approval)
        shadow.after_transition(self, target)

    def issue_active_approval(self):
        """The approval the run now acts under; a re-reviewed approval replaces it durably
        (EVIDENCE_CONTRACT.md controller_trace.active_approval)."""
        reviewers = [r for r in self.roles.receipts if r.get("role") == "reviewer"]
        approval = {
            "approval_id": str(uuid.uuid4()),
            "verdict": "APPROVE",
            "reviewer_occurrence": len(reviewers),
            "reviewer_session_id": reviewers[-1].get("session_id") if reviewers else None,
            "request_hash": self.request_hash,
            "goal_hash": digest(self.goal),
            "plan_hash": self.plan_hash(),
            "manifest_hash": self.build_hash,
            "source_hash": self.source_hash,
            "binding_hash": self.binding,
            "transition_index": len(self.events) - 1,
        }
        superseded = self.context.setdefault("superseded_approvals", [])
        previous = self.context.pop("active_approval", None)
        if previous is not None:
            superseded.append(previous)
        if superseded:
            approval["replaces"] = superseded[-1]["approval_id"]
        event = self.dispatch_event("active_approval", approval=approval)
        approval["sequence"] = event["sequence"] if event else None
        self.context["active_approval"] = approval
        self.save(f"active-approval-{len(superseded):03}.json", approval)
        self.checkpoint()

    def accepted_approval(self, review):
        """Normalized accepted-approval boundary, confirmed against the durable checkpoint.

        Only a committed APPROVE bound to the current plan consumes an occurrence; raw
        reviewer returns and repeated state entries are never counted.
        """
        value = read_checkpoint(self.root)
        events = value["events"]
        if (
            value["state"] != "REVIEW_APPROVED"
            or not events
            or events[-1]["to"] != "REVIEW_APPROVED"
            or events[-1]["evidence"] != review
            or events[-1]["evidence_hash"] != digest(review)
            or value["binding"] != self.binding
            or not isinstance(review, dict)
            or review.get("verdict") != "APPROVE"
            or review.get("binding_hash") != self.binding
        ):
            return None
        counter = self.context.setdefault(
            "accepted_approvals",
            {"occurrence": 0, "initial_binding_hash": None, "initial_plan_hash": None},
        )
        plan_hash = self.plan_hash()
        counter["occurrence"] += 1
        if counter["occurrence"] == 1:
            counter["initial_binding_hash"] = self.binding
            counter["initial_plan_hash"] = plan_hash
        self.checkpoint()
        initial = counter["initial_binding_hash"]
        initial_plan = counter.get("initial_plan_hash")  # absent in a pre-change checkpoint
        return {
            "role": "reviewer",
            "target": "reviewer",
            "occurrence": counter["occurrence"],
            "verdict": "APPROVE",
            "schema_valid": True,
            "binding_valid": True,
            "controller_accepted": True,
            "approval_committed": True,
            # plan_scope is the plan identity (frozen schedule contract: "initial means the
            # first accepted plan identity"), the plan-version hash, not the binding: a
            # runtime-manifest or source change re-binds the same plan and stays "initial".
            # No parent-plan binding exists in this controller, so never child_of_initial.
            "plan_scope": "initial" if plan_hash == initial_plan else None,
            "plan_hash": plan_hash,
            "initial_plan_hash": initial_plan,
            "parent_plan_hash": None,
            "binding_hash": self.binding,
            "initial_binding_hash": initial,
            "parent_binding_hash": None,
            "transition_index": len(events) - 1,
        }

    def emit(self, point, event):
        """Trusted Python harness hook, never exposed as an agent tool or CLI permission."""
        if self.hooks is not None:
            self.hooks(point, {"state": self.state, **event}, self)

    def ask(self, role, payload):
        self.integrity()
        self.emit("before_role", {"role": role, "payload": payload})
        captured = None
        if role == "reviewer" and "review_evidence" in self.context:
            self.integrity()
            payload = json.loads(json.dumps(payload))
            with gated("review_binding"):
                captured = review_evidence.capture(self, payload)
        before = len(self.roles.receipts)
        self.pending = {"kind": "model", "role": role, "input_hash": digest(payload)}
        if role == "repairer" and getattr(self, "repair_trigger", None) is not None:
            # UC-16: the allowance is consumed at the dispatch boundary, durably in the same
            # checkpoint that records the pending repairer invocation (crash-safe).
            self.repairs_used += 1
            self.context.setdefault("repair_attempts", []).append(
                {
                    "attempt": self.repairs_used,
                    "status": "dispatched",
                    "trigger_gate": self.repair_trigger,
                    "consumes_allowance": True,
                }
            )
            self.repair_trigger = None
        attempt = None
        context = self.context.get("dispatch_evidence")
        if context is not None:
            occurrence = context["occurrences"].get(role, 0) + 1
            context["occurrences"][role] = occurrence
            attempt = self.dispatch_event(
                "dispatch_attempt",
                attempt_id=str(uuid.uuid4()),
                role=role,
                occurrence=occurrence,
                input_hash=digest(payload),
                state=self.state,
            )
            self.pending["attempt_id"] = attempt["attempt_id"]
            # Native Roles consumes this context; custom roles may leave correlation unknown.
            self.roles.dispatch_context = dict(attempt)
            self.roles.dispatch_observation = None
            self.roles.dispatch_observer = self.capture_invocation
            if captured:
                captured[1]["controller_attempt"] = dict(attempt)
                self.save(captured[0], captured[1])
        self.checkpoint()
        try:
            answer = self.roles.ask(role, payload)
        except BaseException as exc:
            # Untagged role failures take the role's gate; a raise-site gate (resource_guard
            # from the process manager's RAM/time guard) is kept as raised.
            tag(exc, ROLE_GATES.get(role, "transition"))
            if attempt is None and not isinstance(exc, Exception):
                raise  # Preserve legacy interruption/checkpoint behavior.
            try:
                if attempt is not None:
                    observed = getattr(self.roles, "dispatch_observation", None)
                    self.dispatch_event(
                        "dispatch_failed",
                        attempt_id=attempt["attempt_id"],
                        error_type=type(exc).__name__,
                        outcome="UNKNOWN",
                        invocation_observation=observed,
                    )
                if captured:
                    name, snapshot = captured
                    snapshot["dispatch_status"] = "FAILED"
                    if attempt is not None:
                        snapshot["invocation_observation"] = observed
                    self.save(name, snapshot)
                if attempt is None:
                    self.pending = None
                self.checkpoint()
            except BaseException as observation_error:
                try:
                    exc.add_note(
                        "Dispatch failure evidence incomplete: " + type(observation_error).__name__
                    )
                except BaseException:
                    pass
            raise
        finally:
            if attempt is not None:
                self.roles.dispatch_context = None
                self.roles.dispatch_observer = None
        if attempt is None:
            self.pending = None
        role_gate = ROLE_GATES.get(role, "transition")
        self.emit("after_role", {"role": role, "answer": answer})
        if len(self.roles.receipts) != before + 1 or self.roles.receipts[-1]["role"] != role:
            self.checkpoint()
            raise Refused("independent role invocation evidence is incomplete", gate=role_gate)
        if attempt is not None:
            receipt = self.roles.receipts[-1]
            if receipt.get("input_hash") != attempt["input_hash"] or receipt.get(
                "output_hash"
            ) != digest(answer):
                raise Refused("dispatch receipt binding mismatch", gate=role_gate)
            receipt["controller_dispatch"] = {
                "attempt_id": attempt["attempt_id"],
                "sequence": context["sequence"] + 1,
                "occurrence": attempt["occurrence"],
                "resume_phase": self.resumes,
            }
            self.dispatch_event(
                "dispatch_returned",
                attempt_id=attempt["attempt_id"],
                invocation_id=receipt.get("invocation_id"),
                receipt=receipt,
            )
        if captured:
            name, snapshot = captured
            receipt = self.roles.receipts[-1]
            if (
                receipt.get("input_hash") != snapshot["input_hash"]
                or receipt.get("output_hash") != digest(answer)
                or digest(payload) != snapshot["input_hash"]
                or receipt.get("dispatch", {}).get("stdin_hash") != snapshot["stdin_hash"]
                or not receipt.get("invocation_id")
                or not receipt.get("session_id")
            ):
                raise Refused("review dispatch receipt binding mismatch", gate="review_binding")
            snapshot["dispatch_status"] = "RETURNED"
            snapshot["receipt"] = json.loads(json.dumps(receipt))
            snapshot["output"] = json.loads(json.dumps(answer))
            self.save(name, snapshot)
            self.context["review_evidence"]["last_capture"] = name
        self.save(
            f"role-{len(self.roles.receipts):03}-{role}.json",
            {"input": payload, "output": answer, "receipt": self.roles.receipts[-1]},
        )
        self.pending = None
        self.checkpoint()
        if self.hooks is not None:
            # CR-03 (ruled: the frozen text is an observation requirement): a post-commit role
            # boundary, fired only after the durable checkpoint has cleared pending. The
            # pre-commit after_role emit above is unchanged.
            durable = read_checkpoint(self.root)
            self.emit(
                "after_role_committed",
                {
                    "role": role,
                    "answer": json.loads(json.dumps(answer)),
                    "receipt": json.loads(json.dumps(self.roles.receipts[-1])),
                    "checkpoint_durable": True,
                    "checkpoint_state": durable["state"],
                    "pending_operation": durable["pending"],
                    "durable": {"state": durable["state"], "pending": durable["pending"]},
                },
            )
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

    def repair(self, payload, failure, prior_plan=None):
        """Bounded repair. UC-16 parity: the allowance is consumed only when the repairer is
        actually dispatched (Controller.ask, durably with its pending record). A repair refused
        before dispatch is recorded as refused_before_dispatch and consumes nothing, so
        repairs_used == repairer receipts + dispatched attempts that returned no receipt.

        ``prior_plan`` names the refused candidate when it was never accepted as self.plan
        (a planner plan refused by validation while its envelope was unpacked)."""
        trigger = "review" if self.state == "PLAN_VALIDATED" else "plan_validation"
        if self.repairs_used >= self.max_repairs:
            raise Refused("bounded plan repair exhausted: " + failure, gate="repair_exhausted")
        attempts = self.context.setdefault("repair_attempts", [])
        dispatched = len(attempts)
        receipts = sum(r.get("role") == "repairer" for r in self.roles.receipts)
        self.repair_trigger = trigger
        try:
            value = self.ask(
                "repairer",
                {
                    **payload,
                    "failure": failure,
                    "prior_plan": self.plan if prior_plan is None else prior_plan,
                    "date_contract": self.date_contract(),
                    "instruction": "Repair only within this exact task and profile. Explain a factual correction to review if needed; no changes to authorization or scientific scope. If the request cannot be repaired without a human answer, return only needs_info (your question) and an optional response, with no plan.",
                },
            )
        except Exception as exc:
            try:
                if len(attempts) == dispatched:
                    attempts.append(
                        {
                            "attempt": None,
                            "status": "refused_before_dispatch",
                            "trigger_gate": trigger,
                            "consumes_allowance": False,
                            "error_type": type(exc).__name__,
                            "failed_gate": getattr(exc, "gate", None),
                        }
                    )
                elif sum(r.get("role") == "repairer" for r in self.roles.receipts) == receipts:
                    attempts[-1]["status"] = "dispatch_failed"
                self.checkpoint()
            except Exception as evidence_error:
                exc.add_note("Repair attempt record incomplete: " + type(evidence_error).__name__)
            raise
        finally:
            self.repair_trigger = None
        attempts[-1]["status"] = "returned"
        question = needs_info(value, "repairer", allowed=("response",))
        if question is not None:
            self.stop_needs_info("repairer", question, trigger, value, payload)
            return  # D30: an accepted operator answer re-planned; the new plan is preflighted
        repair_fields = {"plan", "response"}
        if "review_evidence" in self.context:
            repair_fields |= {"plan_claims", "schema"}
        keys(value, repair_fields, "repair", gate="plan_validation")
        if not isinstance(value["response"], str) or len(value["response"]) > 4000:
            raise Refused("invalid bounded repair response", gate="plan_validation")
        self.context["repair_response"] = value["response"]
        try:
            validate_plan(value["plan"], self.goal)
        except Refused:
            self.record_plan_validation(
                value["plan"],
                "repairer",
                parser_accepted=False,
                claims=self.candidate_claims(value),
            )
            raise
        with gated("claim_evidence"):
            if "review_evidence" in self.context:
                self.accept_plan({k: value[k] for k in ("schema", "plan", "plan_claims")})
            else:
                self.plan = value["plan"]
        self.record_plan_version("repairer")
        self.preflight_plan()
        self.checkpoint()

    def accept_planner(self, payload):
        """Planner dispatch: the frozen needs_info shape stops the run; a plan is accepted."""
        answer = self.ask("planner", payload)
        question = needs_info(answer, "planner")
        if question is not None:
            self.stop_needs_info("planner", question, "plan_validation", answer, payload)
            return  # D30: an accepted operator answer re-planned
        try:
            with gated("claim_evidence"):
                self.accept_plan(answer)
        except Refused as exc:
            refused = self.record_refused_plan(answer, "planner")
            if refused is not None:
                exc.refused_plan = refused  # a plan validation refusal: repairable (run loop)
            raise
        self.record_plan_version("planner")

    def record_refused_plan(self, value, role):
        """A candidate plan refused while its envelope was unpacked (review_evidence.unpack_plan
        runs validate_plan before preflight_plan could record it): record its FAIL validation,
        once, so plan_validations names the refused plan as the repairer path does. A refusal
        of anything but the plan itself (envelope shape, claims) is not a plan validation.
        Returns the refused plan when its validation was recorded, else None."""
        plan = value.get("plan") if isinstance(value, dict) else None
        if "review_evidence" not in self.context or plan is None:
            return None
        try:
            validate_plan(plan, self.goal)
        except Refused:
            self.record_plan_validation(
                plan, role, parser_accepted=False, claims=self.candidate_claims(value)
            )
            return plan
        return None

    def stop_needs_info(self, role, question, gate, answer, planner_payload=None):
        """D9 CP-B3: an explicit NEEDS_INFO stop; no further role or process is started.

        D26/D30: the stop is a wait boundary. The operator ledger is consulted after the question
        is durable; only an accepted user answer (recorded as replan) continues the run, by
        re-invoking the planner within the original limits. Otherwise the stop stands.
        """
        occurrence = sum(r.get("role") == role for r in self.roles.receipts)
        self.context["needs_info"] = {
            "role": role,
            "occurrence": occurrence,
            "question": question,
            "failed_gate": gate,
            "output_hash": digest(answer),
        }
        self.checkpoint()
        allowed = ("replan",) if planner_payload is not None else ()
        for decision in self.consult_operator_ledger("needs_info", allowed):
            if decision["accepted"] and decision["action"] == "replan":
                return self.operator_replan(decision, planner_payload, role)
        raise NeedsInfo(role, question, gate)

    def consult_operator_ledger(self, boundary, allowed=None):
        """Wait-boundary read of the operator ledger (needs_info, operator_notification,
        before_resume, re_review). Returns this consultation's durable decisions."""
        return operator_ledger.consult(self, boundary, allowed)

    def operator_resume_authorization(self):
        """Before any resume: the accepted user resume record_id to bind, None if the ledger
        was consulted and accepted none, or False if no ledger was opened for this run."""
        if self.context.get("operator_channel") is None:
            return False
        return operator_ledger.take_accepted_resume(self)

    def request_transition(self, request):
        """D20 CP-E15: record a transition request ACCEPTED/REJECTED; never calls advance()."""
        return operator_ledger.submit_transition_request(self, request)

    def operator_replan(self, decision, planner_payload, role):
        """An accepted replan: the planner again, same goal, budget, deadline and repairs."""
        stop = self.context.pop("needs_info")
        self.context.setdefault("answered_needs_info", []).append(
            {**stop, "record_id": decision["record_id"]}
        )
        self.checkpoint()
        payload = {
            **planner_payload,
            "operator_answer": {
                "record_id": decision["record_id"],
                "question": stop["question"],
                "text": decision["decision"]["text"],
            },
        }
        self.accept_planner(payload)
        if role == "repairer":
            self.preflight_plan()
        self.checkpoint()

    def plan_source(self, role):
        return {"role": role, "occurrence": sum(r.get("role") == role for r in self.roles.receipts)}

    def plan_claims(self):
        """The claims envelope bound to the current plan (None without review evidence)."""
        evidence = self.context.get("review_evidence")
        return evidence.get("plan_claims") if evidence is not None else None

    def plan_hash(self):
        """The current plan version's hash: the plan plus its claims (plan_version_hash)."""
        return plan_version_hash(self.plan, self.plan_claims())

    def candidate_claims(self, value):
        """The claims a candidate planner/repairer envelope carries, as accept_plan binds them."""
        if "review_evidence" not in self.context or not isinstance(value, dict):
            return None
        return value.get("plan_claims", value.get("claims"))

    def record_plan_version(self, role):
        self.context["plan_source"] = self.plan_source(role)
        self.context.setdefault("plan_versions", []).append(
            {"plan_hash": self.plan_hash(), **self.context["plan_source"]}
        )
        self.checkpoint()

    def plan_argv(self, plan):
        """Controller-constructed token lists per step id, as validated against the parsers."""
        argv = {}
        for index, step in enumerate(plan.get("steps", []) if isinstance(plan, dict) else []):
            try:
                tokens = [
                    "--input",
                    str(self.paths[step["input"]]),
                    "--output",
                    str(self.paths[step["output"]]),
                ]
                for key, value in step["args"].items():
                    tokens += ["--" + key.replace("_", "-"), value]
            except (KeyError, TypeError, AttributeError):
                tokens = None  # not constructible: recorded as such, never invented
            step_id = step.get("id") if isinstance(step, dict) else None
            argv[step_id if isinstance(step_id, str) else f"#{index}"] = tokens
        return argv

    def record_plan_validation(
        self, plan, role, parser_accepted, failed_fields=None, claims=CURRENT_CLAIMS
    ):
        """One plan_validations record and its dispatch event. ``claims`` defaults to the current
        plan's claims; a candidate that was never accepted passes the claims it carried."""
        failed = plan_failed_fields(plan, self.goal) if failed_fields is None else failed_fields
        if not parser_accepted and not failed:
            failed = ["plan.steps.argv"]
        if claims is CURRENT_CLAIMS:
            claims = self.plan_claims()
        try:
            plan_hash = plan_version_hash(plan, claims)
        except (TypeError, ValueError):
            plan_hash = None
        record = {
            "plan_hash": plan_hash,
            "argv": self.plan_argv(plan),
            "parser_accepted": bool(parser_accepted),
            "verdict": "PASS" if parser_accepted and not failed else "FAIL",
            "failed_fields": failed,
            "source": self.plan_source(role),
        }
        self.context.setdefault("plan_validations", []).append(record)
        self.dispatch_event("plan_validation", validation=record)
        self.checkpoint()
        return record

    def preflight_plan(self):
        role = self.context.get("plan_source", {}).get("role", "planner")
        try:
            validate_plan(self.plan, self.goal)
        except Refused:
            self.record_plan_validation(self.plan, role, parser_accepted=False)
            raise
        argv = self.plan_argv(self.plan)
        try:
            for step in self.plan["steps"]:
                with gated("plan_validation"):
                    preflight(self.repo, step["skill"], argv[step["id"]])
        except Refused:
            self.record_plan_validation(self.plan, role, parser_accepted=False)
            raise
        self.record_plan_validation(self.plan, role, parser_accepted=True, failed_fields=[])

    def integrity(self):
        """Raise the first failed item of the itemized verification (recovery.verify).

        Inputs bound into an approval invalidate it when they change (review_binding);
        before any binding exists the same change fails the check that admitted it.
        CAP-REREVIEW: while an approval is active and nothing has been produced
        (rereview_open), a change to an approved object is followed instead: collected and
        raised as ReturnToReview (re-review) by rereview_integrity().
        """
        if self.rereview_open():
            self.rereview_integrity()
            return
        refusals = recovery.verify(self, stop_at_first=True)["refusals"]
        if refusals:
            raise refusals[0]

    def rereview_integrity(self):
        """integrity() while re-review is open (an approval is bound, nothing produced).

        Same checks, order and gate codes as recovery.verify; source, manifest, request and
        plan changes are collected and raised together as ReturnToReview. A goal change is
        recorded and refused, never re-reviewed.
        """
        changes = []
        with gated("source_check"):
            admission_changed = (
                record_for(self.admission, self.source) != self.admission_record
                or admission_identity(self.admission_record) != self.admission_hash
            )
        if admission_changed:
            raise Refused(
                "source admission policy changed",
                gate="review_binding",
                reason="approval_invalidated",
            )
        for name, expected in self.evidence.items():
            with gated("gate_evidence"):
                observed = tree_hash(self.root / name)
            if observed != expected:
                raise Refused(
                    "controller evidence changed: " + name,
                    gate="gate_evidence",
                    reason="artifact_identity_mismatch",
                )
        with gated("source_check"):
            source_now = tree_hash(self.source)
        if source_now != self.source_hash:
            changes.append({"object": "source", "to_hash": source_now})
        with gated("review_binding"):
            build_now = build_manifest(self.repo)
        if digest(build_now) != self.build_hash:
            changes.append({"object": "manifest", "to_hash": digest(build_now), "build": build_now})
        with gated("authorization"):
            request_now = digest(self.request)
            try:
                goal_changed = request_goal(self.request) != self.goal
            except Refused:
                if request_now != self.request_hash:
                    self.record_material_change("request", request_now)
                raise
        if request_now != self.request_hash or goal_changed:
            # Recorded either way; a goal change is never re-reviewed (it needs new
            # compiled authority, not a new look at the same plan).
            self.record_material_change("request", request_now)
            if goal_changed:
                self.record_material_change("goal", digest(request_goal(self.request)))
                raise Refused(
                    "request/authorization changed since admission",
                    gate="review_binding",
                    reason="approval_invalidated",
                )
            changes.append({"object": "request", "to_hash": request_now, "recorded": True})
        for name, value in self.artifacts.items():
            if name == "source" and any(c["object"] == "source" for c in changes):
                continue  # the same change, followed above
            with gated("gate_evidence"):
                observed = tree_hash(self.paths[name])
            if observed != value:
                raise Refused(
                    f"artifact changed since its producing step: {name}",
                    gate="gate_evidence",
                    reason="artifact_identity_mismatch",
                )
        if self.context["active_approval"]["plan_hash"] != self.plan_hash():
            changes.append({"object": "plan", "to_hash": self.plan_hash()})
        if changes:
            raise ReturnToReview(
                "approved binding changed: " + ", ".join(c["object"] for c in changes), changes
            )
        if self.binding and self.current_binding() != self.binding:
            raise Refused(
                "plan or review binding changed", gate="review_binding", reason="stale_binding"
            )

    def rereview_open(self):
        """An approval is active and no producing step has started or left output."""
        approval = self.context.get("active_approval") or {}
        return (
            self.state in APPROVED_STATES
            and bool(self.binding)
            and self.pending is None
            and set(self.artifacts) == {"source"}
            and not any(Path(p).exists() for n, p in self.paths.items() if n != "source")
            and approval.get("binding_hash") == self.binding
        )

    def record_material_change(self, kind, to_hash):
        records = self.context.setdefault("material_changes", [])
        record = {"index": len(records), "object": kind, "to_hash": to_hash, "state": self.state}
        event = self.dispatch_event("material_change", change=record)
        record["sequence"] = event["sequence"] if event else None
        records.append(record)
        self.save(f"material-change-{record['index']:03}.json", record)
        self.checkpoint()
        return record

    def return_to_review(self, change):
        """CAP-REREVIEW: record each change, re-check a changed source, withdraw the approval
        and return to PLAN_VALIDATED under the same repair and resume budgets. The re-review
        itself is the ordinary PLAN_VALIDATED loop (claim check, reviewer, repair)."""
        for item in change.changes:
            if not item.get("recorded"):
                self.record_material_change(item["object"], item["to_hash"])
        # A request change was recorded where it was detected, just before the others.
        changed = self.context["material_changes"][-len(change.changes) :]
        indexes = [r["index"] for r in changed]
        plan_hash = self.plan_hash()
        # Re-review wait boundary: an accepted attributed operator replan (operator channel,
        # when this controller has one) returns to planning instead; a new plan starts its
        # own re-reviews (UC-05). Same repair and resume budgets either way.
        # consult_operator_ledger returns this consultation's decisions (a list, empty when no
        # channel exists); a lone decision dict is treated as a one-item consultation. Only an
        # accepted (literally True) replan decision counts.
        consult = getattr(self, "consult_operator_ledger", None)
        decisions = consult("re_review", allowed=("replan",)) if callable(consult) else None
        if isinstance(decisions, dict):
            decisions = [decisions]
        decision = next(
            (
                d
                for d in decisions or ()
                if isinstance(d, dict) and d.get("accepted") is True and d.get("action") == "replan"
            ),
            None,
        )
        replan = decision is not None
        done = [r for r in self.context.get("rereviews", []) if r["plan_hash"] == plan_hash]
        if not replan and len(done) >= MAX_REREVIEWS_PER_PLAN:
            # UC-05: a third re-review of one plan is not allowed. No re-plan is invented
            # here: stop at the binding check, still at the approved state.
            raise Refused(
                "re-review cap reached for this plan; a new plan and approval are required",
                gate="review_binding",
                reason="approval_invalidated",
            )
        withdrawn, target = [], "GOAL_CHECKED" if replan else "PLAN_VALIDATED"
        withdraw = APPROVED_STATES + (("PLAN_VALIDATED",) if replan else ())
        while self.events and self.events[-1]["to"] in withdraw:
            withdrawn.append(self.events.pop())
        if not self.events or self.events[-1]["to"] != target:
            raise Refused("re-review cannot return to " + target, gate="review_binding")
        self.state = target
        approval = self.context.pop("active_approval", None)
        if approval is not None:
            approval["invalidated_by"] = indexes
            self.context.setdefault("superseded_approvals", []).append(approval)
        self.context.pop("review", None)
        for item in change.changes:
            if item["object"] == "manifest":
                self.build, self.build_hash = item["build"], item["to_hash"]
                self.save(f"build-{indexes[-1]:03}.json", self.build)
            elif item["object"] == "source":
                self.source_hash = self.artifacts["source"] = item["to_hash"]
            elif item["object"] == "request":
                self.request_hash = item["to_hash"]
        if replan:
            self.plan, self.binding = None, None  # GOAL_CHECKED asks the planner again
        else:
            self.binding = self.current_binding()
        rereviews = self.context.setdefault("rereviews", [])
        rereview = {
            "index": len(rereviews),
            "plan_hash": plan_hash,
            "material_changes": indexes,
            "withdrawn_states": [e["to"] for e in reversed(withdrawn)],
            "binding_hash": self.binding,
            "returned_to": target,
            "operator_decision": decision if replan else None,
        }
        rereviews.append(rereview)
        self.dispatch_event("return_to_review", rereview=rereview)
        self.checkpoint()
        if any(item["object"] == "source" for item in change.changes):
            self.recheck_source()
        if replan:
            self.checkpoint()
            return
        self.preflight_plan()  # revalidate the plan against the current manifest
        self.save("plan.json", {"plan": self.plan, "binding_hash": self.binding})
        self.checkpoint()

    def recheck_source(self):
        """UC-05 "new data new check": the source gate runs again before any re-review."""
        records = self.context.setdefault("source_rechecks", [])
        record = {"index": len(records), "source_hash": self.source_hash, "verdict": "FAIL"}
        try:
            self.context["metadata"] = self.inspect_source()
            self.context["source_gate"] = self.check("source", self.state)
            record["verdict"] = "PASS"
        except Refused as exc:
            tag(exc, "source_check", override=True)
            raise
        finally:
            event = self.dispatch_event("source_recheck", recheck=record)
            record["sequence"] = event["sequence"] if event else None
            records.append(record)
            self.save(f"source-recheck-{record['index']:03}.json", record)
            self.checkpoint()

    def current_binding(self):
        bound = {
            "request": self.request_hash,
            "goal": self.goal,
            "plan": self.plan,
            "build": self.build_hash,
            "source": self.source_hash,
            "admission": self.admission_hash,
        }
        if "review_evidence" in self.context:
            evidence = self.context["review_evidence"]
            bound["review_evidence"] = {
                "schema": evidence["schema"],
                "plan_claims": evidence["plan_claims"],
                "sources": evidence["sources"],
            }
            if "rationale" in evidence:
                bound["review_evidence"]["rationale"] = evidence["rationale"]
        return digest(bound)

    def claim_gates_enabled(self):
        return "claim_checks" in self.context.get("review_evidence", {})

    def claim_check(self):
        """Gate 1, derivable computation, then gate 2; one durable record per plan version.

        The record is written (and a dispatch event emitted) before any reviewer receipt.
        A resumed run reuses the record for the identical binding instead of re-gating.
        """
        evidence = self.context["review_evidence"]
        records = evidence["claim_checks"]
        plan_hash = self.plan_hash()
        if records and evidence.get("claim_check_binding") == self.binding:
            record = records[-1]
            if record["decision"] == "ROUTE_TO_REPAIR" or (
                record["reviewer_occurrence"] == evidence["reviewer_occurrence"] + 1
            ):
                return record
        if claim_gates.presentations(records, plan_hash) >= claim_gates.MAX_PRESENTATIONS_PER_PLAN:
            # Refused before gating, so no checker session is spent on a 4th presentation.
            self.checkpoint()
            raise Refused("re-review cap reached: plan already presented 3 times", gate="review")
        claims, sources = evidence["plan_claims"], evidence["sources"]
        contract = self.date_contract()
        index = len(records)
        args = (index, plan_hash, self.build_hash, self.source_hash, claims, sources, contract)
        gate = claim_gates.build_record(*args)
        occurrence, ruled = None, {}
        judged = [c["claim_id"] for c in claims if not c["derivable"]]
        if gate["citation_gate"] == "PASS" and judged:
            output = self.ask(
                "evidence_checker",
                claim_gates.checker_payload(plan_hash, self.binding, claims, sources),
            )
            ruled = claim_gates.validate_checker_output(
                output, plan_hash, judged, {c["claim_id"] for c in claims}
            )
            occurrence = sum(1 for r in self.roles.receipts if r["role"] == "evidence_checker")
            receipt = self.roles.receipts[-1]
            receipt["verdict"] = claim_gates.worst(ruled.values())
            receipt["occurrence"] = occurrence
        record = claim_gates.build_record(*args, occurrence, ruled)
        if record["decision"] == "PRESENT_FOR_REVIEW":
            record["reviewer_occurrence"] = evidence["reviewer_occurrence"] + 1
        records.append(record)
        evidence["claim_check_binding"] = self.binding
        self.save(f"claim-check-{index:03}.json", record)
        self.dispatch_event(
            "claim_check",
            index=index,
            plan_hash=plan_hash,
            decision=record["decision"],
            record_hash=claim_gates.record_hash(record),
        )
        self.checkpoint()
        return record

    def accept_review_output(self, review):
        """Exact legacy shape, or the frozen UC-03 shape bound by a review capture.

        Returns the transition evidence. A frozen-shape output is normalized to the
        controller's binding, with the reviewer's own output kept verbatim beside it.
        """
        if isinstance(review, dict) and set(review) == {"verdict", "binding_hash", "findings"}:
            if review["binding_hash"] != self.binding or not isinstance(review["findings"], list):
                raise Refused("review binding or findings invalid", gate="review_binding")
            return review
        frozen = {"verdict", "approved_binding", "findings", "reason", "verification_waiver"}
        if (
            "review_evidence" not in self.context
            or not isinstance(review, dict)
            or "verdict" not in review
            or not set(review) <= frozen
        ):
            keys(review, {"verdict", "binding_hash", "findings"}, "review")
        if (
            review["verdict"] not in {"APPROVE", "REJECT"}
            or not isinstance(review.get("findings", []), list)
            or not isinstance(review.get("reason", ""), str)
            or not isinstance(review.get("verification_waiver", {}), dict)
        ):
            raise Refused("review output outside the frozen reviewer schema", gate="review")
        if "approved_binding" in review:
            approved = review["approved_binding"]
            presented = review_evidence.presented_binding(self)
            if (
                not isinstance(approved, dict)
                or not {"plan_hash", "source_hash"} <= set(approved)
                or any(k not in presented or presented[k] != v for k, v in approved.items())
            ):
                # review_binding: an approval of anything but the presented binding is void.
                raise Refused(
                    "review_binding: approved_binding differs from presented binding",
                    gate="review_binding",
                )
        # A verification waiver is recorded verbatim but never relaxes a controller gate.
        return {
            "verdict": review["verdict"],
            "binding_hash": self.binding,
            "findings": review.get("findings", []),
            "reviewer_output": json.loads(json.dumps(review)),
        }

    def accept_plan(self, value):
        if "review_evidence" not in self.context:
            self.plan = value
            return
        evidence = self.context["review_evidence"]
        parsed = review_evidence.unpack_plan(
            value, self.goal, evidence["sources"], claim_gates="claim_checks" in evidence
        )
        self.plan = parsed["plan"]
        evidence["plan_claims"] = parsed["plan_claims"]
        # A plan version's rationale is its own; never inherit a previous version's.
        evidence.pop("rationale", None)
        if "rationale" in parsed:
            evidence["rationale"] = parsed["rationale"]

    def tool(self, skill, argv, allowed_state, argv_gate="plan_validation"):
        if self.state != allowed_state:
            raise Refused(f"{skill} cannot run at {self.state}", gate="transition")
        self.integrity()
        with gated(argv_gate):
            parsed, _ = preflight(self.repo, skill, argv)
            # Arguments come exclusively from controller-constructed plans, never raw agent argv.
            command = [sys.executable, str(script(self.repo, skill)), *argv]
        self.emit("before_tool", {"skill": skill, "argv": argv})
        self.integrity()
        self.pending = {"kind": "tool", "skill": skill, "argv": argv}
        write_boundary.pending_checkpoint(self, skill, argv)  # PermissionError: pending cleared
        receipts = {"tool": skill} if "dispatch_evidence" in self.context else {}
        self.process_window = [] if receipts else None
        try:
            # Process-manager refusals are its RAM / wall-time / output guards.
            with gated("resource_guard"):
                result = self.processes.run(
                    command, self.outputs, child_environment(self.root / "scratch"), **receipts
                )
        except Exception:
            # The process manager stops owned children before raising. A hard crash leaves pending set.
            self.pending = None
            self.process_window = None
            self.checkpoint()
            raise
        self.pending = None
        window, self.process_window = self.process_window, None
        if window is not None:
            process_receipts.verify(window, result)  # gate_evidence: receipts, not interface
        self.emit("after_tool", {"skill": skill, "argv": argv, "process": result})
        self.integrity()
        self.save(f"tool-{self.state}-{skill}.json", {"parsed": parsed, "process": result})
        artifact = write_boundary.artifact_for_output(self, argv)
        if artifact is not None and result["exit_code"] == 0 and not result.get("stop_reason"):
            # CAP-DENY-WRITE: the controller commits the produced artifact (write boundary).
            write_boundary.commit(self, artifact, skill)
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
            self.tool("check-artifact", argv, state, argv_gate="gate_evidence"),
            "check-artifact.gate/1",
            self.paths[name],
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
                raise Refused(
                    "source does not match the bounded daily CHIRPS profile", gate="source_check"
                )
            wanted = np.arange(
                np.datetime64(self.goal["start_date"]),
                np.datetime64(self.goal["end_date"]) + np.timedelta64(1, "D"),
            )
            if not np.array_equal(ds.time.values.astype("datetime64[D]"), wanted):
                raise Refused(
                    "source dates/cadence do not retain the requested window", gate="source_check"
                )
            if "precip" not in ds or set(ds.precip.dims) != {"time", "latitude", "longitude"}:
                raise Refused("source variable/dimensions differ", gate="source_check")
            observed = [
                float(ds.latitude.max()),
                float(ds.longitude.min()),
                float(ds.latitude.min()),
                float(ds.longitude.max()),
            ]
            if any(abs(a - b) > 0.051 for a, b in zip(observed, self.goal["bbox"], strict=True)):
                raise Refused(
                    "source spatial extent differs from the explicit bbox", gate="source_check"
                )
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
                        "production admission requires observed CHIRPS, not synthetic data",
                        gate="source_check",
                    )
            else:
                with gated("source_check"):
                    self.admission.verify(self.source)
                if (
                    ds.attrs.get("synthetic") != "true"
                    or ds.attrs.get("generator") != self.admission_record["generator_id"]
                ):
                    raise Refused(
                        "synthetic registration does not match source identity",
                        gate="source_check",
                    )
        history = load_history(self.source)
        if self.admission is None and (
            not isinstance(history, list)
            or len(history) != 1
            or history[0].get("skill") != "chirps-fetch"
        ):
            raise Refused("only a direct cached CHIRPS fetch is admitted", gate="source_check")
        return metadata

    def docs(self):
        docs = {
            name: (
                (self.repo / "skills" / name / "SKILL.md").read_bytes().decode("utf-8")
                if "review_evidence" in self.context
                else (self.repo / "skills" / name / "SKILL.md").read_text(encoding="utf-8")
            )
            for name in ("onset-date", "plot-onset", "check-artifact", "verify-run")
        }
        evidence = self.context.get("review_evidence", {})
        if "definition_source" in evidence:
            bound = evidence["definition_source"]
            text = claim_gates.read_definition_text(bound["path"])
            if review_evidence.text_hash(text) != bound["source_hash"]:
                raise Refused(
                    "bound definition source changed since admission", gate="claim_evidence"
                )
            if "definition-source.json" not in self.evidence:
                self.save("definition-source.json", bound)
            docs[bound["source_id"]] = text
        elif "review_evidence" in self.context:
            path = self.repo / "skills/onset-date/references/onset_definitions.toml"
            try:
                # Preserve the whole authoritative document, including newlines.
                text = path.read_bytes().decode("utf-8")
            except (OSError, UnicodeDecodeError) as exc:
                raise Refused(
                    "required definition source unavailable", gate="claim_evidence"
                ) from exc
            if not text:
                raise Refused("required definition source empty", gate="claim_evidence")
            docs[review_evidence.DEFINITION_SOURCE_ID] = text
        return docs

    def execute_step(self, index, state, target):
        if self.state != state:
            raise Refused("producing step requested out of order", gate="transition")
        validate_plan(self.plan, self.goal)
        step = self.plan["steps"][index]
        output = self.paths[step["output"]]
        if output.exists():
            raise Refused(
                "output exists; never overwrite/reuse a previous attempt", gate="write_denial"
            )
        argv = ["--input", str(self.paths[step["input"]]), "--output", str(output)]
        for key, value in step["args"].items():
            argv += ["--" + key.replace("_", "-"), value]
        process = self.tool(step["skill"], argv, state)
        if process["exit_code"] != 0:
            # No faithful gate exists for a failed producing tool (exit status alone does not
            # say whether a write was denied): INCOMPLETE at the transition it could not make.
            raise Refused(
                f"{step['skill']} failed: {process['stderr'][-500:]}",
                gate="transition",
                reason="producing_step_failed",
                outcome="INCOMPLETE",
            )
        self.artifacts[step["output"]] = tree_hash(output)
        self.advance(target, self.artifacts)

    def terminal_for(self, exc):
        """CAP-REASON-MAP: map a stop onto {outcome, reason, failed_gate} plus provenance.

        failed_gate is null iff RELEASED. A refusal with no gate is mapped from the state it
        stopped in and marked state_fallback; a non-refusal failure is INCOMPLETE and maps to
        pending_operation when an operation was in flight, else interrupted (runtime_fallback).
        """
        if isinstance(exc, NeedsInfo):
            terminal = {"outcome": "NEEDS_INFO", "reason": "needs_info", "failed_gate": exc.gate}
            return terminal, {"source": "raise_site", "role": exc.role}
        if isinstance(exc, Refused):
            gate, source = exc.gate, exc.gate_source
            if gate is None:
                gate, source = STATE_GATES[self.state], "state_fallback"
            outcome = exc.outcome or ("INCOMPLETE" if gate in INCOMPLETE_GATES else "BLOCKED")
            reason = exc.reason or GATE_REASONS.get(gate, gate + "_failed")
            if gate == "review" and self.context.get("clarification_required"):
                # The controller recorded a reviewer NEEDS-INFO verdict before refusing.
                outcome, reason = "NEEDS_INFO", "needs_info"
            mapping = {"source": source}
            if getattr(exc, "superseded_gate", None):
                mapping["superseded_gate"] = exc.superseded_gate
            terminal = {"outcome": outcome, "reason": reason, "failed_gate": gate}
            if getattr(exc, "denial_source", None):
                terminal["denial_source"] = exc.denial_source  # write boundary (CAP-DENY-WRITE)
            return terminal, mapping
        pending = self.pending is not None
        terminal = {
            "outcome": "INCOMPLETE",
            "reason": "pending_operation_uncertain" if pending else "runtime_failure",
            "failed_gate": "pending_operation" if pending else "interrupted",
        }
        return terminal, {"source": "runtime_fallback", "error_type": type(exc).__name__}

    def finish(self, result, terminal, mapping):
        result["terminal"] = terminal
        result["terminal_mapping"] = mapping
        self.context["terminal"] = {**terminal, "mapping": mapping}
        self.dispatch_event("terminal", terminal=terminal, terminal_mapping=mapping)
        self.checkpoint()
        atomic_json(self.root / "result.json", result)

    def run(self):
        # Controller-native confinement: the controller process's own HOME-derived caches and
        # temp files live under run/scratch for the whole run, like its children's.
        with confined_process(self.root / "scratch"):
            return self._unconfined_run()

    def _unconfined_run(self):
        try:
            trace_records.record_initial_state(self)  # event_seq for the initial state
            result = self._supervised_run()
        except NeedsInfo as stop:
            terminal, mapping = self.terminal_for(stop)
            result = {
                "status": "NEEDS_INFO",
                "state": self.state,
                "needs_info": self.context.get("needs_info"),
                "role_receipts": self.roles.receipts,
                "model_cost_usd": self.roles.spent,
            }
            self.finish(result, terminal, mapping)
            return result
        except Exception as exc:
            terminal, mapping = self.terminal_for(exc)
            stop = recovery.ledger(self.context)["stop"]
            if stop is not None:
                # Notify-and-wait (UC-15): no further state, invocation or process follows.
                mapping["stop"] = dict(stop)
            result = {
                "status": "INCOMPLETE",
                "state": self.state,
                "error": str(exc),
                "stop_kind": "gate_or_contract_refusal"
                if isinstance(exc, Refused)
                else (stop or {}).get("kind", "runtime_failure"),
                "role_receipts": self.roles.receipts,
                "model_cost_usd": self.roles.spent,
            }
            try:
                self.finish(result, terminal, mapping)
            except Exception as evidence_error:
                exc.add_note("Terminal evidence incomplete: " + type(evidence_error).__name__)
                atomic_json(self.root / "result.json", result)
            raise
        else:
            terminal = {"outcome": "RELEASED", "reason": "released", "failed_gate": None}
            self.finish(result, terminal, {"source": "released"})
            return result
        finally:
            from .reporting import report

            report(self.root)

    def _supervised_run(self):
        """Automatic-resume supervisor (D23): classified interruptions are recorded at failure
        time; transient ones with a durable null pending are resumed, re-verified, at most
        recovery.MAX_AUTOMATIC_RESUMES times; repeated interruption or an exhausted budget is
        diagnosed, mechanically repaired only when evidenced and authority-preserving, else
        notified, and the run stops and waits."""
        self.bind_recovery()
        while True:
            try:
                # drive() follows ReturnToReview (a Refused) itself; anything else it raises
                # reaches the recovery handling below exactly as _run() would.
                return self.drive()
            except (NeedsInfo, Refused):
                raise
            except Exception as exc:
                resumed = self.recover(exc)
                if resumed is None:
                    raise
                # Continue as the durably re-verified controller, in this same object.
                self.__dict__.update(resumed.__dict__)
                # A resumed controller starts its own _run (before_run included), as a fresh
                # resume() would; a re-review re-entry flag does not survive the resume.
                self.__dict__.pop("reentered", None)
                self.bind_process_events()
                self.bind_recovery()

    def bind_recovery(self):
        recovery.ledger(self.context)
        try:
            self.roles.transport_retry_recorder = functools.partial(
                recovery.record_transport_retry, self
            )
        except AttributeError:
            pass  # a role transport without attributes simply gets no transport retry

    def recover(self, exc):
        """Record the interruption, then resume / repair / notify. None means stop now."""
        try:
            interruption = recovery.record_interruption(self, exc)
        except Exception as evidence_error:
            exc.add_note("Interruption record incomplete: " + type(evidence_error).__name__)
            return None
        if recovery.automatic_eligible(self, interruption):
            request = {"initiated_by": "automatic"}
            try:
                return type(self).resume(
                    self.root,
                    self.processes,
                    self.roles,
                    hooks=self.hooks,
                    admission=self.admission,
                    request=request,
                    supervisor=recovery.SUPERVISOR,
                )
            except Refused as refusal:
                # The refusal and its terminal are durable; adopt them before stopping.
                durable = read_checkpoint(self.root)
                self.context, self.evidence = durable["context"], durable["evidence"]
                raise refusal from exc
        if not recovery.needs_diagnosis(self, interruption):
            return None
        diagnosis = recovery.diagnose(self)
        repair = recovery.execute_repair(self, diagnosis["index"])
        if repair is not None and repair["result"] == "SUCCESS":
            try:
                return type(self).resume(
                    self.root,
                    self.processes,
                    self.roles,
                    hooks=self.hooks,
                    admission=self.admission,
                    request={"initiated_by": "evidenced_repair"},
                )
            except Refused:
                durable = read_checkpoint(self.root)
                self.context, self.evidence = durable["context"], durable["evidence"]
        try:
            recovery.notify(self, diagnosis)
        except Exception as hook_error:
            exc.add_note("Operator notification hook failed: " + type(hook_error).__name__)
        return None

    def drive(self):
        """_run, returning to PLAN_VALIDATED after each followed material change."""
        while True:
            try:
                return self._run()
            except ReturnToReview as change:
                self.return_to_review(change)
                self.reentered = True

    def _run(self):
        self.integrity()
        if not getattr(self, "reentered", False):
            self.emit("before_run", {})
        stop = self.context.get("needs_info")
        if stop:
            # A recorded NEEDS_INFO is final for this request: no role or process runs again.
            raise NeedsInfo(stop["role"], stop["question"], stop["failed_gate"])
        if self.context.get("clarification_required"):
            raise Refused(
                "review requires user clarification; start a new request after the answer",
                gate="review",
                reason="needs_info",
                outcome="NEEDS_INFO",
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
                trace_records.retain_compiled_goal(self, candidate)  # goal_check record
                compiles.append(candidate)
                self.checkpoint()
            self.save("compiled-goals.json", compiles)
            self.advance("GOAL_CHECKED", compiles)
        docs = self.docs()
        if "review_evidence" in self.context:
            evidence = self.context["review_evidence"]
            sources = review_evidence.sources_from_docs(docs)
            if "sources" in evidence and evidence["sources"] != sources:
                raise Refused("bound review sources changed", gate="review_binding")
            evidence["sources"] = sources
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
        if "review_evidence" in self.context:
            plan_payload["review_evidence_request"] = {
                "schema": review_evidence.SCHEMA,
                "sources": self.context["review_evidence"]["sources"],
                "instruction": "Return schema, plan (the unchanged typed plan), and nonempty "
                "plan_claims. Each claim contains claim_id, text, citations (source_id, "
                "source_hash, verbatim quote), and your boolean derivable declaration. "
                "Use only these bound documentation sources; never invent a source. "
                "Declarations and citations are not controller support verdicts. "
                "For a repair also include response; retain all envelope fields.",
            }
        if self.state == "GOAL_CHECKED":
            repaired = False
            if not self.plan:
                try:
                    self.accept_planner(plan_payload)
                except Refused as exc:
                    # A planner plan refused by validation (recorded by record_refused_plan) may
                    # trigger repair like a preflight failure, but only within the allowance:
                    # otherwise the original refusal stands (failed_gate plan_validation).
                    # Envelope and claim refusals are not plan validations and are re-raised.
                    refused = getattr(exc, "refused_plan", None)
                    if refused is None or self.repairs_used >= self.max_repairs:
                        raise
                    self.repair(plan_payload, str(exc), prior_plan=refused)
                    repaired = True
            if not repaired:
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
                if "review_evidence" in self.context:
                    review_payload["review_evidence"] = review_evidence.envelope(self)
                if self.context.get("review_retry_required"):
                    self.repair(plan_payload, json.dumps(self.context["rejected_review"]))
                    self.context["review_retry_required"] = False
                    self.binding = self.current_binding()
                    self.save("plan.json", {"plan": self.plan, "binding_hash": self.binding})
                    self.checkpoint()
                    continue
                if self.claim_gates_enabled():
                    record = self.claim_check()
                    if record["decision"] == "ROUTE_TO_REPAIR":
                        # Never presented: repair within max_repairs, then gate the new version.
                        self.repair(plan_payload, claim_gates.repair_failure(record))
                        self.binding = self.current_binding()
                        self.save("plan.json", {"plan": self.plan, "binding_hash": self.binding})
                        self.checkpoint()
                        continue
                    # A detached copy: a hook editing the payload can never edit the record.
                    review_payload["claim_check"] = json.loads(json.dumps(record))
                review = self.ask("reviewer", review_payload)
                review = self.accept_review_output(review)
                self.save(f"review-{self.repairs_used}.json", review)
                if review["verdict"] == "APPROVE":
                    self.context["review"] = review
                    break
                if review["verdict"] != "REJECT":
                    self.context["clarification_required"] = review
                    self.checkpoint()
                    raise Refused(
                        "review requires user clarification; no automatic scope change",
                        gate="review",
                    )
                self.context["rejected_review"] = review
                self.context["review_retry_required"] = True
                self.checkpoint()
            self.integrity()
            self.advance("REVIEW_APPROVED", review)
        if self.state == "REVIEW_APPROVED":
            self.integrity()
            self.advance("AUTHORIZED", self.request["authorization"])
        if self.state == "AUTHORIZED":
            if self.policy_mode not in ("rules", shadow.MODE):
                prompt = self.policy_question(
                    self.state,
                    self.binding,
                    ["EXECUTE", "STOP"],
                    {"review": self.context["review"]},
                )
                answer = self.ask("router", prompt)
                with gated("transition"):
                    prediction = policy.validate(answer, prompt)
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
                    raise Refused(
                        "routing policy selected STOP", gate="transition", reason="shadow_stop"
                    )
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
                    self.tool("verify-run", argv, before_verify, argv_gate="replay_evidence"),
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
            recorded = self.record_verifier(verdict, final_binding)
            if not (
                recorded["verdict"] == "PASS"
                and recorded["binding_valid"]
                and recorded["scope_valid"]
            ):
                raise Refused(
                    "independent verification role did not confirm the exact final evidence",
                    gate="verifier",
                    reason="verifier_fail",
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
                raise Refused("independent role invocation evidence is incomplete", gate="verifier")
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

    def policy_question(self, *args):
        with gated("transition"):
            return policy.question(*args)

    def record_verifier(self, value, binding):
        """D8 CP-B2: record PASS/FAIL natively; BLOCK/UNVERIFIABLE survive as sub_reason."""
        required = {"verdict", "binding_hash", "scope"}
        if (
            not isinstance(value, dict)
            or not required <= set(value)
            or set(value) - required - {"sub_reason"}
        ):
            raise Refused(
                "verifier: expected verdict, binding_hash, scope and optional sub_reason",
                gate="verifier",
            )
        raw, sub = value["verdict"], value.get("sub_reason")
        if raw in VERIFIER_SUB_REASONS and sub in (None, raw):
            verdict, sub = "FAIL", raw  # the native gate vocabulary, recorded as a sub-reason
        elif raw == "PASS" and sub is None:
            verdict = "PASS"
        elif raw == "FAIL" and (sub is None or sub in VERIFIER_SUB_REASONS):
            verdict = "FAIL"
        else:
            raise Refused(
                "verifier verdict outside PASS/FAIL (sub_reason BLOCK/UNVERIFIABLE)",
                gate="verifier",
            )
        records = self.context.setdefault("verifier_verdicts", [])
        record = {
            "occurrence": len(records) + 1,
            "verdict": verdict,
            "sub_reason": sub,
            "returned_verdict": raw,
            "binding_valid": value["binding_hash"] == binding,
            "scope_valid": value["scope"] == "data-replay",
            # What the verifier actually returned, kept durably (W2 review runner.py:1586):
            # a FAIL refusal otherwise leaves only the booleans behind.
            "returned_binding_hash": json.loads(json.dumps(value["binding_hash"])),
            "returned_scope": json.loads(json.dumps(value["scope"])),
        }
        records.append(record)
        self.checkpoint()
        return record

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
