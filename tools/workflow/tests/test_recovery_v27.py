"""v2.7 recovery semantics: interruption ledger, automatic resume, resume decisions and
attribution, diagnosis, mechanical-repair framework, operator notification lifecycle and
hook, same-invocation transport retry (D18, D23, D29 parts, D31).

Offline doubles only: no model, no network, no real child process. The controller is the SUT;
the frozen v2.7 E14 schedules are read-only inputs driven through the real hook adapter.
"""

import copy
import functools
import hashlib
import json
import uuid
from pathlib import Path
from types import SimpleNamespace

import pytest

from tools.workflow import agents, kong_roles, recovery, runner
from tools.workflow.contracts import Refused
from tools.workflow.e2e_injections import (
    InjectedConnectionDrop,
    InjectionHooks,
    UnsupportedInjection,
)
from tools.workflow.persistence import read_checkpoint, write_checkpoint
from tools.workflow.tests.test_runner_lifecycle_v27 import (
    EventProcesses,
    ReceiptRoles,
    dispatch_events,
    result_json,
)
from tools.workflow.tests.test_workflow import GOAL, controller

FROZEN = Path(
    "C:/Users/neilha/.agentbus/simkit-controller-snowyowl/completion-evidence/frozen-baseline"
)
DEV_ONLY = FROZEN / "casesets/cached-onset-e2e-v2_7/frozen/agentic-e2e-injections.DEV-ONLY.json"
SYMBOLS = {"HARNESS_RUN_ID": "run-recovery", "HARNESS_RUN_DIR": "C:/disposable/run-recovery"}


@functools.cache
def frozen_cases():
    pins = json.loads((FROZEN / "FINAL-PINS.json").read_text(encoding="utf-8"))["files"]
    actual = hashlib.sha256(DEV_ONLY.read_bytes()).hexdigest()
    assert actual == pins[DEV_ONLY.relative_to(FROZEN).as_posix()], "frozen file drifted"
    data = json.loads(DEV_ONLY.read_text(encoding="utf-8"))
    return {c["id"].rsplit("V2_7-", 1)[1]: c for c in data["cases"]}


def make(tmp_path, monkeypatch, hooks=None, roles=None, processes=None, name="evidence-run"):
    base = controller(tmp_path, monkeypatch)  # patches build_manifest / preflight
    root = tmp_path / name
    root.mkdir()
    c = runner.Controller(
        base.repo,
        base.request,
        base.source,
        root,
        processes or EventProcesses(),
        roles or ReceiptRoles(),
        hooks=hooks,
        dispatch_evidence_mode=True,
    )
    monkeypatch.setattr(c, "inspect_source", lambda: {"test": "metadata fixture"})
    return c


def book(c):
    return read_checkpoint(c.root)["context"]["recovery"]


def faults(plan):
    """Hook raising each planned exception once at (point, state-or-role)."""
    fired = set()

    def hook(point, event, ctl):
        key = (point, event.get("role") or event.get("state"))
        if key in plan and key not in fired:
            fired.add(key)
            raise plan[key]()

    return hook


def interrupted_run(tmp_path, monkeypatch, state="ONSET_CHECKED", error=RuntimeError):
    c = make(tmp_path, monkeypatch, hooks=faults({("after_transition", state): error}))
    with pytest.raises(error):
        c.run()
    return c


def seq(records, kind):
    return [r["sequence"] for r in records if r["kind"] == kind]


# ------------------------------------------------------------------ CAP-AUTO-RESUME / E14-P1


def test_e14_p1_inj_two_automatic_resumes_then_diagnose_notify_and_wait(tmp_path, monkeypatch):
    hooks = InjectionHooks(frozen_cases()["E14-P1-INJ"], symbols=SYMBOLS)
    processes = EventProcesses()
    c = make(tmp_path, monkeypatch, hooks=hooks, processes=processes)
    with pytest.raises(InjectedConnectionDrop):
        c.run()
    ledger = book(c)
    assert [i["failure_class"] for i in ledger["interruptions"]] == [
        "connection_reset",
        "timeout",
        "connection_reset",
    ]
    assert [i["at_state"] for i in ledger["interruptions"]] == [
        "SOURCE_CHECKED",
        "REVIEW_APPROVED",
        "AUTHORIZED",
    ]
    assert all(i["pending_operation"] is None for i in ledger["interruptions"])
    resumes = ledger["resume"]
    assert [(r["initiated_by"], r["result"], r["interruption_index"]) for r in resumes] == [
        ("automatic", "ACCEPTED", 0),
        ("automatic", "ACCEPTED", 1),
    ]
    for r in resumes:
        assert r["verification"]["verdict"] == "PASS"
        assert r["verification"]["failed_items"] == []
        assert set(recovery.REQUIRED_ITEMS) <= set(r["verification"]["checked_items"])
        assert r["budget_override_applied"] is False and r["override_requested"] is False
    assert ledger["counters"]["automatic_resumes_used"] == 2
    (diagnosis,) = ledger["diagnoses"]
    assert diagnosis["interruption_indexes"] == [0, 1, 2]
    assert diagnosis["fix_widens_authority"] is True and diagnosis["mechanical"] is False
    assert ledger["mechanical_repairs"] == []
    (note,) = ledger["operator_notifications"]
    assert note["stop_record"]["failure_classes"] == [
        "connection_reset",
        "timeout",
        "connection_reset",
    ]
    assert note["recorded"] is True and note["emitted"] is True
    assert note["diagnosis_index"] == diagnosis["index"]
    terminal = result_json(c)["terminal"]
    assert terminal["outcome"] == "INCOMPLETE" and terminal["failed_gate"] == "interrupted"
    assert result_json(c)["stop_kind"] == "notify_and_wait"
    assert c.state == "AUTHORIZED"
    assert "onset_date" not in json.dumps(processes.calls)
    records = dispatch_events(c.root)
    assert ledger["interruptions"][2]["sequence"] < diagnosis["sequence"] < note["sequence"]
    assert note["sequence"] < seq(records, "terminal")[-1]
    after = [r["kind"] for r in records if r["sequence"] > note["sequence"]]
    forbidden = {"transition", "dispatch_attempt", "process_launch", "launch_check"}
    assert not forbidden & set(after)
    entries = {e["id"]: e["disposition"] for e in hooks.coverage()["dependencies"]["entries"]}
    assert entries == dict.fromkeys(("interrupt-1", "interrupt-2", "interrupt-3"), "DELIVERED")


def test_e14_p1_ctl_two_automatic_resumes_then_release(tmp_path, monkeypatch):
    hooks = InjectionHooks(frozen_cases()["E14-P1-CTL"], symbols=SYMBOLS)
    processes = EventProcesses()
    c = make(tmp_path, monkeypatch, hooks=hooks, processes=processes)
    assert c.run()["terminal"]["outcome"] == "RELEASED"
    ledger = book(c)
    assert ledger["counters"]["automatic_resumes_used"] == 2
    assert [r["result"] for r in ledger["resume"]] == ["ACCEPTED", "ACCEPTED"]
    assert sum("onset_date" in json.dumps(call) for call in processes.calls) == 1
    assert ledger["diagnoses"] == [] and ledger["operator_notifications"] == []


def test_durable_pending_blocks_automatic_resume(tmp_path, monkeypatch):
    class Drop(ReceiptRoles):
        def ask(self, role, payload):
            if role == "planner":
                raise ConnectionResetError("dropped mid-dispatch")
            return super().ask(role, payload)

    c = make(tmp_path, monkeypatch, roles=Drop())
    with pytest.raises(ConnectionResetError):
        c.run()
    ledger = book(c)
    (interruption,) = ledger["interruptions"]
    assert interruption["failure_class"] == "connection_reset"
    assert interruption["pending_operation"] == "planner"
    assert ledger["resume"] == []
    assert result_json(c)["terminal"]["failed_gate"] == "pending_operation"


def test_non_transient_class_is_not_auto_resumed(tmp_path, monkeypatch):
    c = interrupted_run(tmp_path, monkeypatch, state="REVIEW_APPROVED")
    ledger = book(c)
    assert [i["failure_class"] for i in ledger["interruptions"]] == ["unclassified"]
    assert ledger["resume"] == [] and ledger["diagnoses"] == []
    assert result_json(c)["terminal"]["failed_gate"] == "interrupted"


def test_crash_between_interruption_and_resume_keeps_counters(tmp_path, monkeypatch):
    plan = {
        ("after_transition", "GOAL_CHECKED"): ConnectionResetError,
        ("after_transition", "REVIEW_APPROVED"): TimeoutError,
    }
    c = make(tmp_path, monkeypatch, hooks=faults(plan))
    original_descriptor = runner.Controller.__dict__["resume"]
    original = original_descriptor.__func__
    calls = []

    def crash_on_second(cls, *args, **kwargs):
        calls.append(kwargs.get("request"))
        if len(calls) == 2:
            raise KeyboardInterrupt  # the process dies after the interruption is durable
        return original(cls, *args, **kwargs)

    monkeypatch.setattr(runner.Controller, "resume", classmethod(crash_on_second))
    with pytest.raises(KeyboardInterrupt):
        c.run()
    monkeypatch.setattr(runner.Controller, "resume", original_descriptor)
    ledger = book(c)
    assert len(ledger["interruptions"]) == 2
    assert ledger["counters"]["automatic_resumes_used"] == 1  # never reset by the crash
    resumed = runner.Controller.resume(c.root, EventProcesses(), ReceiptRoles(), hooks=c.hooks)
    ledger = recovery.ledger(resumed.context)
    assert ledger["counters"]["automatic_resumes_used"] == 1
    assert ledger["counters"]["resume_attempts_used"] == 2
    assert ledger["resume"][-1]["interruption_index"] == 1


def test_after_role_before_commit_keeps_interrupt_inactive(tmp_path, monkeypatch):
    class NoThirdReceipt(ReceiptRoles):
        def ask(self, role, payload):
            answer = super().ask(role, payload)
            if role == "compiler" and self.asked.count("compiler") == 3:
                self.receipts.pop()
            return answer

    hooks = InjectionHooks(frozen_cases()["E14-P1-INJ"], symbols=SYMBOLS)
    c = make(tmp_path, monkeypatch, hooks=hooks, roles=NoThirdReceipt())
    with pytest.raises(Refused, match="invocation evidence is incomplete"):
        c.run()
    entries = {e["id"]: e for e in hooks.coverage()["dependencies"]["entries"]}
    assert entries["interrupt-1"]["disposition"] == "INACTIVE"
    assert entries["interrupt-1"]["reason"] == "boundary_not_reached"
    assert hooks.records == [] and book(c)["interruptions"] == []


def test_pre_commit_after_role_never_forced_on_a_live_controller(tmp_path, monkeypatch):
    spec = {
        "id": "i",
        "requires": [],
        "hook": "after_role",
        "selector": {"role_or_tool_or_stage": "planner", "occurrence": 1},
        "operation": "interrupt",
        "payload": {
            "mode": "harness_drop_model_connection",
            "failure_class": "connection_reset",
            "checkpoint_state": "GOAL_CHECKED",
            "pending_operation": None,
        },
    }
    c = make(tmp_path, monkeypatch)
    h = InjectionHooks({"injection_schedule": [spec]})
    h("after_role", {"role": "planner", "state": "GOAL_CHECKED"}, c)  # pre-commit: deferred
    assert h.dispositions == {} and h.records == []


# ------------------------------------------------------------------ CAP-RESUME / ATTRIBUTION


def test_override_request_refused_with_refusal_record(tmp_path, monkeypatch):
    c = interrupted_run(tmp_path, monkeypatch)
    processes = EventProcesses()
    request = {
        "action": "resume",
        "requested_by": "operator",
        "budget_override": {
            "consumed_model_usd": 0,
            "elapsed_seconds": 0,
            "repair_attempts_used": 0,
            "resume_attempts_used": 0,
            "justification": "Fresh process after interruption; counters restart.",
        },
    }
    with pytest.raises(Refused) as caught:
        runner.Controller.request_resume(c.root, processes, ReceiptRoles(), request, hooks=c.hooks)
    assert caught.value.gate == "budget"
    (decision,) = book(c)["resume"]
    assert decision["result"] == "REFUSED"
    assert decision["override_requested"] is True
    assert decision["budget_override_applied"] is False
    assert decision["refusal_record"]["request"] == request
    assert decision["refusal_record"]["illegal_fields"] == [
        "consumed_model_usd",
        "elapsed_seconds",
        "repair_attempts_used",
        "resume_attempts_used",
    ]
    assert decision["refusal_record"]["governing_limit"]
    assert decision["interruption_index"] == 0
    assert processes.calls == []
    assert read_checkpoint(c.root)["context"]["terminal"]["outcome"] != "RELEASED"


def test_pending_unknown_refused_and_nothing_follows(tmp_path, monkeypatch):
    c = make(tmp_path, monkeypatch)
    c.pending = {"kind": "tool", "skill": "onset-date", "argv": []}
    c.checkpoint()
    before = len(dispatch_events(c.root))
    processes = EventProcesses()
    with pytest.raises(Refused) as caught:
        runner.Controller.request_resume(
            c.root, processes, ReceiptRoles(), {"requested_by": "operator"}
        )
    assert caught.value.gate == "pending_operation"
    (decision,) = book(c)["resume"]
    assert decision["result"] == "REFUSED"
    assert "pending_operation:onset-date" in decision["verification"]["failed_items"]
    assert decision["verification"]["verdict"] == "FAIL"
    kinds = [r["kind"] for r in dispatch_events(c.root)[before:]]
    # An operator-requested resume also records its REJECTED transition request (E10/E11).
    assert kinds == ["resume", "transition_request", "terminal"]
    (transition,) = read_checkpoint(c.root)["context"]["transition_requests"]
    assert transition["result"] == "REJECTED"
    assert transition["reasons"] == ["pending_operation:onset-date"]
    assert processes.calls == []


def test_artifact_drift_refused_with_itemized_failure_and_never_releases(tmp_path, monkeypatch):
    c = interrupted_run(tmp_path, monkeypatch)
    c.paths["onset"].write_text("drifted onset")
    for _ in range(2):
        with pytest.raises(Refused) as caught:
            runner.Controller.request_resume(
                c.root, EventProcesses(), ReceiptRoles(), {"requested_by": "operator"}, c.hooks
            )
        assert caught.value.gate == "resume_verification"
    first = book(c)["resume"][0]
    assert first["result"] == "REFUSED"
    assert first["verification"]["failed_items"] == ["artifact:onset"]
    assert "artifacts" in first["verification"]["checked_items"]
    assert read_checkpoint(c.root)["state"] == "ONSET_CHECKED"


def test_accepted_resume_carries_full_checked_items_and_sequence(tmp_path, monkeypatch):
    c = interrupted_run(tmp_path, monkeypatch)
    resumed = runner.Controller.resume(c.root, EventProcesses(), ReceiptRoles(), hooks=c.hooks)
    (decision,) = recovery.ledger(resumed.context)["resume"]
    assert decision["result"] == "ACCEPTED" and decision["initiated_by"] is None
    assert set(recovery.REQUIRED_ITEMS) <= set(decision["verification"]["checked_items"])
    (event,) = [r for r in dispatch_events(c.root) if r["kind"] == "resume"]
    assert event["sequence"] == decision["sequence"]
    assert resumed.run()["terminal"]["outcome"] == "RELEASED"


def test_automatic_resume_cannot_be_requested_from_outside(tmp_path, monkeypatch):
    c = interrupted_run(tmp_path, monkeypatch)
    # Only the run loop's supervisor may initiate an automatic resume; a forged one is refused.
    with pytest.raises(Refused):
        runner.Controller.request_resume(
            c.root, EventProcesses(), ReceiptRoles(), {"initiated_by": "automatic"}, c.hooks
        )
    decision = book(c)["resume"][-1]
    assert decision["result"] == "REFUSED"
    assert "attribution:automatic_not_supervised" in decision["verification"]["failed_items"]


def test_evidenced_repair_without_success_repair_is_refused(tmp_path, monkeypatch):
    c = interrupted_run(tmp_path, monkeypatch)
    with pytest.raises(Refused):
        runner.Controller.request_resume(
            c.root, EventProcesses(), ReceiptRoles(), {"initiated_by": "evidenced_repair"}, c.hooks
        )
    decision = book(c)["resume"][-1]
    assert decision["result"] == "REFUSED"
    assert decision["verification"]["failed_items"] == ["repair_binding:no_diagnosis"]


def authorizing(record_ids):
    """Stand-in for the operator channel's operator_resume_authorization(): hands each
    accepted user resume record out once, then None."""
    queue = list(record_ids)
    calls = []

    def authorize(self):
        calls.append(self.state)
        return queue.pop(0) if queue else None

    authorize.calls = calls
    return authorize


def test_operator_resume_requires_an_accepted_ledger_record(tmp_path, monkeypatch):
    c = interrupted_run(tmp_path, monkeypatch)
    request = {"initiated_by": "operator_ledger", "operator_record_id": "OPR-RESUME-1"}
    with pytest.raises(Refused):  # no operator channel on this controller: none accepted
        runner.Controller.request_resume(c.root, EventProcesses(), ReceiptRoles(), request, c.hooks)
    assert book(c)["resume"][-1]["verification"]["failed_items"] == [
        "operator_record:ledger_unavailable"
    ]
    authorize = authorizing([])
    monkeypatch.setattr(
        runner.Controller, "operator_resume_authorization", authorize, raising=False
    )
    with pytest.raises(Refused):
        runner.Controller.request_resume(c.root, EventProcesses(), ReceiptRoles(), request, c.hooks)
    assert book(c)["resume"][-1]["verification"]["failed_items"] == [
        "operator_record:none_accepted"
    ]
    monkeypatch.setattr(
        runner.Controller,
        "operator_resume_authorization",
        authorizing(["OPR-OTHER"]),
        raising=False,
    )
    with pytest.raises(Refused):
        runner.Controller.request_resume(c.root, EventProcesses(), ReceiptRoles(), request, c.hooks)
    decision = book(c)["resume"][-1]
    assert decision["verification"]["failed_items"] == ["operator_record:OPR-RESUME-1"]
    assert decision["result"] == "REFUSED"


def test_operator_resumes_are_uncapped_but_reverified(tmp_path, monkeypatch):
    c = interrupted_run(tmp_path, monkeypatch)
    value = read_checkpoint(c.root)
    value["context"]["recovery"]["counters"]["automatic_resumes_used"] = 2
    value["resumes"] = 2  # the legacy cap would refuse an unattributed resume here
    write_checkpoint(c.root, value)
    monkeypatch.setattr(
        runner.Controller,
        "operator_resume_authorization",
        authorizing(["OPR-RESUME-1"]),
        raising=False,
    )
    request = {"initiated_by": "operator_ledger", "operator_record_id": "OPR-RESUME-1"}
    resumed = runner.Controller.request_resume(
        c.root, EventProcesses(), ReceiptRoles(), request, c.hooks
    )
    ledger = recovery.ledger(resumed.context)
    assert ledger["resume"][-1]["result"] == "ACCEPTED"
    assert ledger["resume"][-1]["operator_record_id"] == "OPR-RESUME-1"
    assert ledger["counters"]["operator_resumes_used"] == 1
    assert ledger["counters"]["automatic_resumes_used"] == 2
    # Same verification within the original limits: an exhausted budget still refuses.
    value = read_checkpoint(c.root)
    value["elapsed_s"] = value["total_seconds"] + 1
    write_checkpoint(c.root, value)
    monkeypatch.setattr(
        runner.Controller,
        "operator_resume_authorization",
        authorizing(["OPR-RESUME-2"]),
        raising=False,
    )
    request = {"initiated_by": "operator_ledger", "operator_record_id": "OPR-RESUME-2"}
    with pytest.raises(Refused, match="budget exhausted"):
        runner.Controller.request_resume(c.root, EventProcesses(), ReceiptRoles(), request, c.hooks)
    # The legacy cap still binds an unattributed resume once no operator record is handed out
    # (it is itemized after verification, so the exhausted budget is the first refusal here).
    with pytest.raises(Refused, match="budget exhausted"):
        runner.Controller.resume(c.root, EventProcesses(), ReceiptRoles(), hooks=c.hooks)
    failed = book(c)["resume"][-1]["verification"]["failed_items"]
    assert "budget" in failed and "resume_limit" in failed


def test_unattributed_resume_binds_an_accepted_operator_record(tmp_path, monkeypatch):
    c = interrupted_run(tmp_path, monkeypatch)
    authorize = authorizing(["OPR-RESUME-1"])
    monkeypatch.setattr(
        runner.Controller, "operator_resume_authorization", authorize, raising=False
    )
    request = {"action": "resume", "requested_by": "operator", "run_id": "run-x"}
    resumed = runner.Controller.request_resume(
        c.root, EventProcesses(), ReceiptRoles(), request, c.hooks
    )
    decision = recovery.ledger(resumed.context)["resume"][-1]
    assert decision["initiated_by"] == "operator_ledger"
    assert decision["operator_record_id"] == "OPR-RESUME-1"
    assert decision["requested_by"] == "operator"
    (transition,) = resumed.context["transition_requests"]
    assert transition["request"] == "resume" and transition["result"] == "ACCEPTED"
    assert transition["requested_by"] == "operator" and transition["run_id"] == "run-x"
    assert isinstance(transition["event_seq"], int)


def test_operator_resume_request_refusal_is_a_rejected_transition_request(tmp_path, monkeypatch):
    c = interrupted_run(tmp_path, monkeypatch)
    c.paths["onset"].write_text("drifted onset")
    request = {"action": "resume", "requested_by": "operator"}
    with pytest.raises(Refused):
        runner.Controller.request_resume(c.root, EventProcesses(), ReceiptRoles(), request, c.hooks)
    (transition,) = read_checkpoint(c.root)["context"]["transition_requests"]
    assert transition["result"] == "REJECTED"
    assert transition["reasons"] == ["artifact:onset"]
    assert transition["from_state"] == "ONSET_CHECKED"


def test_notify_and_wait_consults_the_operator_ledger_after_emission(tmp_path, monkeypatch):
    seen = []

    def consult(self, boundary, allowed=None):
        notes = read_checkpoint(self.root)["context"]["recovery"]["operator_notifications"]
        seen.append((boundary, notes[-1]["recorded"], notes[-1]["emitted"]))
        return []

    monkeypatch.setattr(runner.Controller, "consult_operator_ledger", consult, raising=False)
    plan = {
        ("after_transition", "GOAL_CHECKED"): ConnectionResetError,
        ("after_transition", "REVIEW_APPROVED"): TimeoutError,
        ("after_transition", "AUTHORIZED"): ConnectionResetError,
    }
    c = make(tmp_path, monkeypatch, hooks=faults(plan))
    with pytest.raises(ConnectionResetError):
        c.run()
    assert seen == [("operator_notification", True, True)]


def test_integrity_is_itemized_and_raises_the_first_failure(tmp_path, monkeypatch):
    c = interrupted_run(tmp_path, monkeypatch)
    c.paths["onset"].write_text("drifted")
    (c.root / "request.json").write_text("{}")
    result = recovery.verify(c)
    assert result["failed_items"] == ["evidence:request.json", "artifact:onset"]
    assert set(recovery.INTEGRITY_ITEMS) <= set(result["checked_items"])
    with pytest.raises(Refused, match="evidence changed: request.json"):
        c.integrity()


# ------------------------------------------------------------------ CAP-DIAGNOSIS / REPAIR


def permission_plan():
    return {
        ("after_transition", "GOAL_CHECKED"): ConnectionResetError,
        ("after_transition", "REVIEW_APPROVED"): TimeoutError,
        ("after_transition", "AUTHORIZED"): lambda: PermissionError(13, "denied", "C:/out"),
    }


def test_permission_denied_diagnosis_widens_authority_and_is_not_repaired(tmp_path, monkeypatch):
    c = make(tmp_path, monkeypatch, hooks=faults(permission_plan()))
    with pytest.raises(PermissionError):
        c.run()
    ledger = book(c)
    (diagnosis,) = ledger["diagnoses"]
    assert diagnosis["cause"] == "write_denied"
    assert diagnosis["mechanical"] is True and diagnosis["evidenced"] is True
    assert diagnosis["fix_widens_authority"] is True
    assert any("errno=13" in e for e in diagnosis["evidence"])
    assert set(diagnosis["interruption_indexes"]) <= {i["index"] for i in ledger["interruptions"]}
    assert ledger["mechanical_repairs"] == []
    assert not [r for r in ledger["resume"] if r["initiated_by"] == "evidenced_repair"]
    assert len(ledger["operator_notifications"]) == 1


def test_blank_evidence_is_never_evidenced_and_cannot_drive_repair():
    blank = {
        "mechanical": True,
        "evidenced": True,
        "fix_widens_authority": False,
        "evidence": [" "],
    }
    assert recovery.evidenced_mechanical(blank) is False
    assert recovery.evidenced_mechanical({**blank, "evidence": []}) is False


def test_superseding_non_mechanical_diagnosis_blocks_evidenced_repair():
    context = {"recovery": recovery.new_ledger()}
    ledger = context["recovery"]
    ledger["interruptions"].append({"index": 0, "failure_class": "write_denied"})
    good = {
        "index": 0,
        "interruption_indexes": [0],
        "mechanical": True,
        "evidenced": True,
        "evidence": ["fact"],
        "fix_action": "fix",
        "fix_widens_authority": False,
    }
    ledger["diagnoses"].append(good)
    ledger["mechanical_repairs"].append(
        {
            "index": 0,
            "diagnosis_index": 0,
            "action": "fix",
            "widens_authority": False,
            "result": "SUCCESS",
        }
    )
    interruption = ledger["interruptions"][0]
    assert recovery.check_repair_binding(context, interruption) is None
    ledger["diagnoses"].append({**good, "index": 1, "mechanical": False})
    assert (
        recovery.check_repair_binding(context, interruption)
        == "repair_binding:diagnosis_not_mechanical"
    )


def test_repair_vocabulary_is_empty_under_the_kernel_only_lock():
    # D31: no fault this SUT can exhibit has an authority-preserving machine fix; the
    # stale-lock fixture (CAP-OWNED-STALE-LOCK-SETUP) is not implemented.
    assert recovery.REPAIR_ACTIONS == {}
    assert "lock_stale" not in recovery.DIAGNOSES


@pytest.mark.parametrize("outcome", ["SUCCESS", "FAIL"])
def test_repair_framework_success_resumes_and_failure_notifies(tmp_path, monkeypatch, outcome):
    rule = {
        "cause": "write_denied",
        "mechanical": True,
        "fix_action": "test_only_fix",
        "fix_widens_authority": False,
    }
    monkeypatch.setitem(recovery.DIAGNOSES, "write_denied", rule)
    monkeypatch.setitem(recovery.REPAIR_ACTIONS, "test_only_fix", lambda c, d: outcome)
    c = make(tmp_path, monkeypatch, hooks=faults(permission_plan()))
    if outcome == "SUCCESS":
        assert c.run()["terminal"]["outcome"] == "RELEASED"
    else:
        with pytest.raises(PermissionError):
            c.run()
    ledger = book(c)
    (repair,) = ledger["mechanical_repairs"]
    assert repair["result"] == outcome and repair["widens_authority"] is False
    assert repair["diagnosis_index"] == ledger["diagnoses"][-1]["index"]
    evidenced = [r for r in ledger["resume"] if r["initiated_by"] == "evidenced_repair"]
    if outcome == "SUCCESS":
        assert [r["result"] for r in evidenced] == ["ACCEPTED"]
        assert evidenced[0]["interruption_index"] == 2
        assert ledger["operator_notifications"] == []
    else:
        assert evidenced == []
        assert len(ledger["operator_notifications"]) == 1


def test_repair_of_a_non_latest_diagnosis_is_rejected(tmp_path, monkeypatch):
    c = make(tmp_path, monkeypatch)
    ledger = recovery.ledger(c.context)
    ledger["diagnoses"] += [{"index": 0}, {"index": 1}]
    with pytest.raises(Refused, match="latest diagnosis"):
        recovery.execute_repair(c, 0)


def test_widening_fix_executes_no_repair(tmp_path, monkeypatch):
    monkeypatch.setitem(recovery.REPAIR_ACTIONS, "grant_write_access", lambda c, d: "SUCCESS")
    c = make(tmp_path, monkeypatch, hooks=faults(permission_plan()))
    with pytest.raises(PermissionError):
        c.run()
    assert book(c)["mechanical_repairs"] == []


# ------------------------------------------------------------------ CAP-OPERATOR-NOTIFY / HOOK


def test_notification_hook_fires_once_after_durable_recorded_and_emitted(tmp_path, monkeypatch):
    seen = []
    inner = faults(
        {
            ("after_transition", "GOAL_CHECKED"): ConnectionResetError,
            ("after_transition", "REVIEW_APPROVED"): TimeoutError,
            ("after_transition", "AUTHORIZED"): ConnectionResetError,
        }
    )

    def hook(point, event, ctl):
        if point == "after_operator_notification":
            durable = read_checkpoint(ctl.root)["context"]["recovery"]["operator_notifications"]
            seen.append((copy.deepcopy(event), copy.deepcopy(durable)))
        inner(point, event, ctl)

    c = make(tmp_path, monkeypatch, hooks=hook)
    with pytest.raises(ConnectionResetError):
        c.run()
    ((event, durable),) = seen
    assert event["occurrence"] == 1 and event["recorded"] is True and event["emitted"] is True
    assert durable[-1]["emitted"] is True and durable[-1]["recorded"] is True
    assert (c.root / durable[-1]["delivery"]["path"]).is_file()
    # A duplicate notify for the same diagnosis is not double-counted.
    diagnosis = recovery.ledger(c.context)["diagnoses"][-1]
    recovery.notify(c, diagnosis)
    assert len(book(c)["operator_notifications"]) == 1 and len(seen) == 1


def test_notification_delivery_failure_is_not_emitted_and_hook_does_not_fire(tmp_path, monkeypatch):
    seen = []
    inner = faults(
        {
            ("after_transition", "GOAL_CHECKED"): ConnectionResetError,
            ("after_transition", "REVIEW_APPROVED"): TimeoutError,
            ("after_transition", "AUTHORIZED"): TimeoutError,
        }
    )

    def hook(point, event, ctl):
        if point == "after_operator_notification":
            seen.append(event)
        inner(point, event, ctl)

    c = make(tmp_path, monkeypatch, hooks=hook)
    (c.root / recovery.NOTIFICATION_DIR).write_text("occupied: outbox cannot be created")
    with pytest.raises(TimeoutError):
        c.run()
    (note,) = book(c)["operator_notifications"]
    assert note["recorded"] is True and note["emitted"] is False
    assert note["delivery"]["status"] == "FAILED"
    assert seen == []


def notification_spec(name="notify"):
    return {
        "id": name,
        "requires": [],
        "hook": "after_operator_notification",
        "selector": {"role_or_tool_or_stage": "operator_notification", "occurrence": 1},
        "operation": "request_transition",
        "payload": {"action": "resume"},
    }


def test_hook_adapter_counts_only_recorded_and_emitted_native_notifications():
    h = InjectionHooks({"injection_schedule": [notification_spec()]}, notification_boundary=True)
    base = {"target": "operator_notification", "notification": {"index": 0}}
    h("after_operator_notification", {**base, "recorded": True, "emitted": False}, None)
    assert h.dispositions == {}
    h("after_operator_notification", {**base, "recorded": True, "emitted": True}, None)
    h("after_operator_notification", {**base, "recorded": True, "emitted": True}, None)
    (entry,) = h.coverage()["dependencies"]["entries"]
    assert entry["disposition"] == "DELIVERED" and entry["occurrence"] == 1
    assert h.notifications == 1 and h.resume_requested


def test_hook_adapter_default_keeps_notification_hook_unproduced():
    h = InjectionHooks({"injection_schedule": [notification_spec()]})
    (entry,) = h.coverage()["dependencies"]["entries"]
    assert entry["disposition"] == "NOT_IMPLEMENTED"


def test_null_pending_interrupt_refused_when_durable_pending_is_set(tmp_path, monkeypatch):
    c = make(tmp_path, monkeypatch)
    c.pending = {"kind": "model", "role": "planner"}
    c.checkpoint()
    h = InjectionHooks({})
    payload = {
        "mode": "harness_drop_model_connection",
        "failure_class": "connection_reset",
        "checkpoint_state": "CAPTURED",
        "pending_operation": None,
    }
    with pytest.raises(UnsupportedInjection):
        h._interrupt(payload, "after_transition", {"state": "CAPTURED"}, c, {})
    c.pending = None
    c.checkpoint()
    record = {}
    assert h._interrupt(payload, "after_transition", {"state": "CAPTURED"}, c, record) is True
    assert isinstance(h.fault, ConnectionResetError)
    assert record["observed_checkpoint"] == {"state": "CAPTURED", "pending": None}


# ------------------------------------------------------------------ CAP-TRANSPORT-RETRY


class RoleTransport(EventProcesses):
    """A role transport double: serial moves only when a child would be launched."""

    def __init__(self, faults=(), **kwargs):
        super().__init__(**kwargs)
        self.faults = list(faults)  # "pre" (before launch) or "post" (after launch)
        self.role_calls = 0

    def run(self, argv, cwd, env, stdin="", tool=None):
        if tool is not None or not stdin:
            return super().run(argv, cwd, env, tool=tool)
        self.role_calls += 1
        fault = self.faults.pop(0) if self.faults else None
        if fault == "pre":
            raise ConnectionResetError("reset before launch")
        self.serial += 1
        if fault == "post":
            raise ConnectionResetError("reset after launch")
        envelope = {
            "total_cost_usd": 0.01,
            "session_id": str(uuid.uuid4()),
            "result": json.dumps(GOAL),
        }
        return {"exit_code": 0, "stdout": json.dumps(envelope), "stderr": ""}


def role_controller(tmp_path, monkeypatch, faults):
    monkeypatch.setattr(agents.shutil, "which", lambda name: "claude-test-double")
    processes = RoleTransport(faults)
    roles = agents.Roles(processes, tmp_path / "role-context")
    c = make(tmp_path, monkeypatch, roles=roles, processes=processes)
    c.bind_recovery()
    return c, processes


def test_pre_write_fault_is_retried_once_inside_the_same_attempt(tmp_path, monkeypatch):
    c, processes = role_controller(tmp_path, monkeypatch, ["pre"])
    assert c.ask("compiler", {"messages": []}) == GOAL
    (retry,) = book(c)["transport_retries"]
    attempt = [r for r in dispatch_events(c.root) if r["kind"] == "dispatch_attempt"][-1]
    assert retry["step"] == "compiler" and retry["attempt"] == 1
    assert retry["before_write"] is True
    assert retry["attempt_id"] == attempt["attempt_id"] and retry["occurrence"] == 1
    assert retry["invocation_id"] == c.roles.receipts[-1]["invocation_id"]
    assert isinstance(retry["sequence"], int) and retry["sequence"] > attempt["sequence"]
    assert processes.role_calls == 2 and len(c.roles.receipts) == 1


def test_fault_after_launch_is_not_retried(tmp_path, monkeypatch):
    c, processes = role_controller(tmp_path, monkeypatch, ["post"])
    with pytest.raises(ConnectionResetError):
        c.ask("compiler", {"messages": []})
    assert book(c)["transport_retries"] == []
    assert processes.role_calls == 1 and c.roles.receipts == []


def test_second_fault_gets_no_second_retry_and_no_receipt(tmp_path, monkeypatch):
    c, processes = role_controller(tmp_path, monkeypatch, ["pre", "pre"])
    with pytest.raises(ConnectionResetError):
        c.ask("compiler", {"messages": []})
    assert len(book(c)["transport_retries"]) == 1
    assert processes.role_calls == 2 and c.roles.receipts == []


def test_no_transport_retry_after_release_or_without_a_recorder(tmp_path, monkeypatch):
    c, _ = role_controller(tmp_path, monkeypatch, ["pre"])
    c.state = "RELEASED"
    with pytest.raises(ConnectionResetError):
        c.ask("compiler", {"messages": []})
    assert recovery.ledger(c.context)["transport_retries"] == []
    (tmp_path / "second").mkdir()
    c2, _ = role_controller(tmp_path / "second", monkeypatch, ["pre"])
    c2.roles.transport_retry_recorder = None
    with pytest.raises(ConnectionResetError):
        c2.ask("compiler", {"messages": []})


def test_kong_transport_retries_a_pre_write_fault_once(tmp_path, monkeypatch):
    class KongTransport(RoleTransport):
        def run(self, argv, cwd, env, stdin="", tool=None):
            self.role_calls += 1
            if self.faults:
                self.faults.pop(0)
                raise ConnectionResetError("reset before launch")
            self.serial += 1
            envelope = {"sent": True, "ok": True, "model": "m", "text": json.dumps(GOAL)}
            return {"exit_code": 0, "stdout": json.dumps(envelope), "stderr": ""}

    processes = KongTransport(["pre"])
    kit = tmp_path / "kit"
    kit.mkdir()
    python = kit / "python.exe"
    python.write_text("")
    roles = kong_roles.KongRoles(
        processes, tmp_path, data_policy_ack=True, kit_root=kit, kit_python=python, model="m"
    )
    c = make(tmp_path, monkeypatch, roles=roles, processes=processes)
    c.bind_recovery()
    assert c.ask("compiler", {"messages": []}) == GOAL
    assert len(book(c)["transport_retries"]) == 1 and processes.role_calls == 2


def test_transport_retry_setup_injects_once_before_launch():
    class Transport:
        serial = 0

        def run(self, *args, **kwargs):
            self.serial += 1
            return {"exit_code": 0}

    processes = Transport()
    ctl = SimpleNamespace(roles=SimpleNamespace(processes=processes))
    h = InjectionHooks({})
    args = {
        "role": "planner",
        "occurrence": 1,
        "failure_class": "connection_reset",
        "failures": 1,
        "max_retries": 1,
        "before_write": True,
    }
    record = {}
    with pytest.raises(UnsupportedInjection):
        h._arm_transport_fault(args, "after_role", {"role": "planner"}, ctl, {})
    h._arm_transport_fault(args, "before_role", {"role": "planner"}, ctl, record)
    with pytest.raises(ConnectionResetError):
        processes.run()
    assert processes.serial == 0 and record["delivery"] == "DELIVERED"
    assert processes.run() == {"exit_code": 0} and processes.serial == 1
    stale = {}
    h._arm_transport_fault(args, "before_role", {"role": "planner"}, ctl, stale)
    h._disarm_transport_fault("before_tool")  # never left armed past its boundary
    assert stale["delivery"] == "UNDELIVERED" and "run" not in vars(processes)
