"""Wave 7, sub-wave A (controller): the interrupt and resume family, gaps 2 to 5.

Offline doubles only: no model, no network, no real child process. The controller is the SUT.

Interfaces sub-waves B (injection delivery) and C (adapter) call
-------------------------------------------------------------------

1. Pre-launch run-directory denial (E14-P2-INJ; OI-06 distinguishes it from E12-P1-INJ by hook
   point). Arm it from a trusted Python hook, after AUTHORIZED and before the producing tool::

       write_boundary.boundary_for(controller).inject_run_dir_denial(
           artifact,          # run artifact name the producing tool writes, e.g. "onset"
           errno=13,          # only EACCES is accepted (ValueError otherwise)
           message="Permission denied: <resolved HARNESS_RUN_DIR>",
       )

   One-shot, one per artifact (ValueError on a second arm). It fires on the durable
   pending-operation checkpoint inside Controller.tool() (write_boundary.pending_checkpoint),
   before processes.run, as ``PermissionError(errno, message, "<run>/checkpoint.json")`` with
   ``injected_fault = True``. Controller.tool() clears ``self.pending`` and re-raises the
   PermissionError (never WriteDenied); the run loop records a write_denied interruption,
   diagnoses it (fix_widens_authority true), repairs nothing, notifies and waits: terminal
   INCOMPLETE / failed_gate interrupted at AUTHORIZED with zero producing launches. The denial
   is also recorded as ``context.run_dir_denials[*]`` {boundary: "pending_checkpoint", tool,
   artifact, state, errno, message, denial_source} with a ``run_dir_denial`` dispatch event.
   The commit-time denial (``inject_denial``, E12-P1-INJ) is unchanged: the tool launches
   once, then BLOCKED / write_denial.

2. Fields every resume decision (controller_trace.resume[*], context.recovery.resume[*]) now
   carries, accepted or refused:

   - ``checkpoint_counters``: snapshot of the consumed checkpoint, taken before the resume
     writes anything: {state, spent, elapsed_s, repairs_used, resumes, budget, total_seconds,
     max_repairs, recovery_counters{automatic_resumes_used, operator_resumes_used,
     evidenced_repair_resumes_used, unattributed_resumes_used, resume_attempts_used},
     recovery_limits{max_automatic_resumes}}. Source for budget_ledger.at_checkpoint (map:
     spent->consumed_model_usd, elapsed_s->elapsed_seconds, repairs_used->
     repair_attempts_used, recovery_counters.resume_attempts_used->resume_attempts_used,
     budget->limits.max_model_usd, total_seconds->limits.max_total_seconds,
     max_repairs->limits.max_repair_attempts, recovery_limits.max_automatic_resumes->
     limits.max_resume_attempts). Use the one consumed by resume[0].
   - ``controller_pid``: os.getpid() of the process that made the decision (one per
     incarnation; the set of incarnation PIDs for the controller-PID check).
   - ``initiated_by`` / ``operator_record_id`` are bound even when the decision is REFUSED: an
     operator_ledger request (or an unattributed request the operator channel hands an
     accepted user resume record) names that record, so V24-RESUME-OPERATOR can match it.

   Observed stop: a resume whose checkpoint has no terminal record and no unanswered
   interruption first records ONE interruption {index, failure_class: "controller_killed",
   at_state, pending_operation, transient: false, error_type: null, observed:
   "checkpoint_without_terminal", observed_by_pid, sequence}; the decision's
   ``interruption_index`` names it. Never when a terminal exists; never twice (it is durable
   before the decision, so a crash mid-resume leaves it unanswered and the next resume binds
   it); never for a checkpoint whose run had not started (no transition, role receipt or
   dispatch event). controller_killed is an inference: disclose it.

3. Fresh-process resume (incarnation 2): construct NO controller beforehand; call::

       controller = runner.Controller.request_resume(
           run_root, processes, roles,
           {"action": "resume", "initiated_by": "operator_ledger", "requested_by": "operator",
            "run_id": "<HARNESS_RUN_ID>"},  # operator_record_id optional: the channel binds it
           hooks=hooks, admission=admission,
       )

   It reads only ``run_root/checkpoint.json``; the operator channel (opened by incarnation 1
   with operator_ledger_path / operator_run_id) is consulted at before_resume and hands out
   the earliest accepted, unconsumed user resume record. A refusal raises Refused after the
   REFUSED decision, a REJECTED transition request (requested_by operator) and the terminal
   are durable; an acceptance returns the controller, whose run() continues the run.
"""

import json
import os

import pytest

from tools.workflow import operator_ledger, recovery, runner, write_boundary
from tools.workflow.contracts import Refused
from tools.workflow.e2e_injections import InjectionHooks
from tools.workflow.persistence import read_checkpoint, write_checkpoint
from tools.workflow.tests.test_recovery_v27 import book, faults, interrupted_run
from tools.workflow.tests.test_recovery_v27 import make as make_controller
from tools.workflow.tests.test_rereview_shadow_v27 import deny_onset_case, tools_run
from tools.workflow.tests.test_runner_lifecycle_v27 import (
    EventProcesses,
    ReceiptRoles,
    dispatch_events,
    result_json,
)
from tools.workflow.tests.test_workflow import controller as plain_controller

RUN_ID = "run-w7"


class Killed(BaseException):
    """Stands in for SIGKILL: no handler in the controller runs, so no terminal is written."""


def kill_at(point, key):
    def hook(p, event, ctl):
        if p == point and (event.get("state") == key or event.get("skill") == key):
            if not getattr(hook, "fired", False):
                hook.fired = True
                raise Killed

    return hook


class KillDuring(EventProcesses):
    """The controller is killed while ``tool`` runs: the pending checkpoint stays durable."""

    def __init__(self, tool, **kwargs):
        super().__init__(**kwargs)
        self.kill_tool = tool

    def run(self, argv, cwd, env, tool=None):
        if tool == self.kill_tool:
            raise Killed
        return super().run(argv, cwd, env, tool=tool)


def with_ledger(tmp_path, monkeypatch, hooks=None, processes=None):
    """A dispatch-evidence controller with the native operator channel (incarnation 1)."""
    base = plain_controller(tmp_path, monkeypatch)  # patches build_manifest / preflight
    root = tmp_path / "evidence-run"
    root.mkdir()
    c = runner.Controller(
        base.repo,
        base.request,
        base.source,
        root,
        processes or EventProcesses(),
        ReceiptRoles(),
        hooks=hooks,
        dispatch_evidence_mode=True,
        operator_ledger_path=tmp_path / "operator-ledger.jsonl",
        operator_run_id=RUN_ID,
    )
    monkeypatch.setattr(c, "inspect_source", lambda: {"test": "metadata fixture"})
    return c


def user_resume(tmp_path, record_id="OPR-RESUME-1"):
    operator_ledger.append_record(
        tmp_path / "operator-ledger.jsonl",
        {
            "record_id": record_id,
            "speaker": "user",
            "text": "resume the run",
            "action": "resume",
            "run_id": RUN_ID,
        },
    )


def interruptions(c):
    return book(c)["interruptions"]


def accepted_operator_actions(c):
    actions = read_checkpoint(c.root)["context"]["operator_actions"]
    return [a["record_id"] for a in actions if a["action"] == "resume" and a["accepted"]]


# ------------------------------------------------------------------ gap 2: attribution


@pytest.mark.parametrize(
    "request_body",
    [
        {"initiated_by": "operator_ledger", "requested_by": "operator", "run_id": RUN_ID},
        {"action": "resume", "requested_by": "operator", "run_id": RUN_ID},  # unattributed
    ],
)
def test_refused_drift_resume_binds_the_accepted_operator_record(
    tmp_path, monkeypatch, request_body
):
    c = with_ledger(tmp_path, monkeypatch, hooks=kill_at("after_transition", "ONSET_CHECKED"))
    with pytest.raises(Killed):
        c.run()
    c.paths["onset"].write_text("drifted onset")  # FX-ONSET-CELL-SHIFT stand-in
    user_resume(tmp_path)
    with pytest.raises(Refused) as caught:
        runner.Controller.request_resume(
            c.root, EventProcesses(), ReceiptRoles(), request_body, hooks=c.hooks
        )
    assert caught.value.gate == "resume_verification"
    (decision,) = book(c)["resume"]
    assert decision["result"] == "REFUSED"
    assert decision["verification"]["failed_items"] == ["artifact:onset"]
    assert isinstance(decision["initiated_by"], str)
    assert decision["initiated_by"] == "operator_ledger"
    assert decision["operator_record_id"] == "OPR-RESUME-1"
    assert "operator_record" in decision["verification"]["checked_items"]
    assert accepted_operator_actions(c) == ["OPR-RESUME-1"]  # V24-RESUME-OPERATOR: exactly 1
    (transition,) = read_checkpoint(c.root)["context"]["transition_requests"]
    assert transition["result"] == "REJECTED" and transition["reasons"] == ["artifact:onset"]
    assert result_json(c)["terminal"]["failed_gate"] == "resume_verification"


def test_refused_pending_resume_binds_the_accepted_operator_record(tmp_path, monkeypatch):
    processes = KillDuring("onset-date")
    c = with_ledger(tmp_path, monkeypatch, processes=processes)
    with pytest.raises(Killed):
        c.run()
    assert read_checkpoint(c.root)["pending"]["skill"] == "onset-date"
    user_resume(tmp_path)
    request = {"initiated_by": "operator_ledger", "requested_by": "operator", "run_id": RUN_ID}
    after = EventProcesses()
    with pytest.raises(Refused) as caught:
        runner.Controller.request_resume(c.root, after, ReceiptRoles(), request)
    assert caught.value.gate == "pending_operation"
    (decision,) = book(c)["resume"]
    assert decision["result"] == "REFUSED"
    assert "pending_operation:onset-date" in decision["verification"]["failed_items"]
    assert decision["initiated_by"] == "operator_ledger"
    assert decision["operator_record_id"] == "OPR-RESUME-1"
    assert accepted_operator_actions(c) == ["OPR-RESUME-1"]
    (stop,) = interruptions(c)
    assert stop["failure_class"] == "controller_killed"
    assert stop["pending_operation"] == "onset-date" and stop["at_state"] == "AUTHORIZED"
    assert decision["interruption_index"] == stop["index"]
    assert after.calls == []  # nothing launched after the refusal


def test_refused_resume_without_an_accepted_record_stays_unbound(tmp_path, monkeypatch):
    c = with_ledger(tmp_path, monkeypatch, hooks=kill_at("after_transition", "ONSET_CHECKED"))
    with pytest.raises(Killed):
        c.run()
    c.paths["onset"].write_text("drifted onset")
    request = {"initiated_by": "operator_ledger", "requested_by": "operator"}
    with pytest.raises(Refused):
        runner.Controller.request_resume(
            c.root, EventProcesses(), ReceiptRoles(), request, hooks=c.hooks
        )
    (decision,) = book(c)["resume"]
    assert decision["operator_record_id"] is None
    assert decision["verification"]["failed_items"] == [
        "artifact:onset",
        "operator_record:none_accepted",
    ]


def test_invalid_attribution_never_binds_an_operator_record(tmp_path, monkeypatch):
    c = with_ledger(tmp_path, monkeypatch, hooks=kill_at("after_transition", "ONSET_CHECKED"))
    with pytest.raises(Killed):
        c.run()
    user_resume(tmp_path)
    request = {"initiated_by": "somebody", "requested_by": "operator"}
    with pytest.raises(Refused):
        runner.Controller.request_resume(
            c.root, EventProcesses(), ReceiptRoles(), request, hooks=c.hooks
        )
    (decision,) = book(c)["resume"]
    assert decision["initiated_by"] is None and decision["operator_record_id"] is None
    assert "attribution:unknown_initiator" in decision["verification"]["failed_items"]


# ------------------------------------------------------------------ gap 3: checkpoint counters


def expected_counters(value):
    ledger = value["context"]["recovery"]
    return {
        "state": value["state"],
        "spent": value["spent"],
        "elapsed_s": value["elapsed_s"],
        "repairs_used": value["repairs_used"],
        "resumes": value["resumes"],
        "budget": value["budget"],
        "total_seconds": value["total_seconds"],
        "max_repairs": value["max_repairs"],
        "recovery_counters": ledger["counters"],
        "recovery_limits": ledger["limits"],
    }


def test_checkpoint_counters_equal_the_consumed_checkpoint_and_limits(tmp_path, monkeypatch):
    c = interrupted_run(tmp_path, monkeypatch)
    value = read_checkpoint(c.root)
    value["spent"], value["repairs_used"] = 1.25, 1  # distinct, nonzero counters
    value["context"]["recovery"]["counters"]["automatic_resumes_used"] = 1
    write_checkpoint(c.root, value)
    expected = expected_counters(read_checkpoint(c.root))
    resumed = runner.Controller.resume(c.root, EventProcesses(), ReceiptRoles(), hooks=c.hooks)
    (decision,) = recovery.ledger(resumed.context)["resume"]
    assert decision["result"] == "ACCEPTED"
    assert decision["checkpoint_counters"] == expected
    assert decision["checkpoint_counters"]["recovery_limits"] == {
        "max_automatic_resumes": recovery.MAX_AUTOMATIC_RESUMES
    }
    assert decision["controller_pid"] == os.getpid()
    # A snapshot, not a live view: the accepted resume advanced the counters afterwards.
    counters = recovery.ledger(resumed.context)["counters"]
    assert counters["resume_attempts_used"] == 1
    assert decision["checkpoint_counters"]["recovery_counters"]["resume_attempts_used"] == 0
    assert resumed.resumes == decision["checkpoint_counters"]["resumes"] + 1
    (event,) = [r for r in dispatch_events(c.root) if r["kind"] == "resume"]
    assert event["record"]["checkpoint_counters"] == expected


def test_refused_decision_carries_checkpoint_counters_and_pid(tmp_path, monkeypatch):
    c = interrupted_run(tmp_path, monkeypatch)
    expected = expected_counters(read_checkpoint(c.root))
    c.paths["onset"].write_text("drifted onset")
    with pytest.raises(Refused):
        runner.Controller.request_resume(
            c.root, EventProcesses(), ReceiptRoles(), {"requested_by": "operator"}, c.hooks
        )
    (decision,) = book(c)["resume"]
    assert decision["result"] == "REFUSED"
    assert decision["checkpoint_counters"] == expected
    assert decision["controller_pid"] == os.getpid()


def test_automatic_resumes_snapshot_each_consumed_checkpoint(tmp_path, monkeypatch):
    plan = {
        ("after_transition", "GOAL_CHECKED"): ConnectionResetError,
        ("after_transition", "REVIEW_APPROVED"): TimeoutError,
    }
    c = make_controller(tmp_path, monkeypatch, hooks=faults(plan))
    assert c.run()["terminal"]["outcome"] == "RELEASED"
    first, second = book(c)["resume"]
    assert [d["checkpoint_counters"]["state"] for d in (first, second)] == [
        "GOAL_CHECKED",
        "REVIEW_APPROVED",
    ]
    assert first["checkpoint_counters"]["recovery_counters"]["automatic_resumes_used"] == 0
    assert second["checkpoint_counters"]["recovery_counters"]["automatic_resumes_used"] == 1
    assert first["checkpoint_counters"]["resumes"] == 0
    assert second["checkpoint_counters"]["resumes"] == 1
    assert {first["controller_pid"], second["controller_pid"]} == {os.getpid()}
    # No observed stop: each automatic resume answers the interruption the loop recorded.
    assert [i["failure_class"] for i in interruptions(c)] == ["connection_reset", "timeout"]


# ------------------------------------------------------------------ gap 4: observed stop


def test_resume_after_a_kill_records_exactly_one_observed_stop(tmp_path, monkeypatch):
    c = make_controller(tmp_path, monkeypatch, hooks=kill_at("after_transition", "ONSET_CHECKED"))
    with pytest.raises(Killed):
        c.run()
    durable = read_checkpoint(c.root)
    assert durable["context"].get("terminal") is None and interruptions(c) == []
    resumed = runner.Controller.resume(c.root, EventProcesses(), ReceiptRoles(), hooks=c.hooks)
    ledger = recovery.ledger(resumed.context)
    (stop,) = ledger["interruptions"]
    assert stop == {
        "index": 0,
        "failure_class": "controller_killed",
        "at_state": "ONSET_CHECKED",
        "pending_operation": None,
        "pending_record": None,
        "transient": False,
        "error_type": None,
        "observed": "checkpoint_without_terminal",
        "observed_by_pid": os.getpid(),
        "sequence": stop["sequence"],
    }
    (decision,) = ledger["resume"]
    assert decision["interruption_index"] == 0 and decision["result"] == "ACCEPTED"
    events = dispatch_events(c.root)
    kinds = [e["kind"] for e in events if e["sequence"] >= stop["sequence"]]
    assert kinds[:2] == ["interruption", "resume"]
    assert resumed.run()["terminal"]["outcome"] == "RELEASED"
    assert [i["failure_class"] for i in book(c)["interruptions"]] == ["controller_killed"]


def test_crash_between_observed_stop_and_decision_never_duplicates(tmp_path, monkeypatch):
    c = make_controller(tmp_path, monkeypatch, hooks=kill_at("after_transition", "ONSET_CHECKED"))
    with pytest.raises(Killed):
        c.run()
    original = recovery.new_decision

    def crash(*args, **kwargs):
        raise Killed  # the resuming process dies after the observed stop is durable

    monkeypatch.setattr(recovery, "new_decision", crash)
    with pytest.raises(Killed):
        runner.Controller.resume(c.root, EventProcesses(), ReceiptRoles(), hooks=c.hooks)
    monkeypatch.setattr(recovery, "new_decision", original)
    assert len(interruptions(c)) == 1 and book(c)["resume"] == []
    resumed = runner.Controller.resume(c.root, EventProcesses(), ReceiptRoles(), hooks=c.hooks)
    ledger = recovery.ledger(resumed.context)
    assert len(ledger["interruptions"]) == 1
    assert ledger["resume"][0]["interruption_index"] == 0


def test_no_observed_stop_when_a_terminal_exists(tmp_path, monkeypatch):
    c = interrupted_run(tmp_path, monkeypatch)  # RuntimeError: interruption + terminal
    assert read_checkpoint(c.root)["context"]["terminal"]["outcome"] == "INCOMPLETE"
    before = [dict(i) for i in interruptions(c)]
    resumed = runner.Controller.resume(c.root, EventProcesses(), ReceiptRoles(), hooks=c.hooks)
    assert recovery.ledger(resumed.context)["interruptions"] == before
    assert "controller_killed" not in json.dumps(before)


def test_no_second_observed_stop_after_a_refused_resume(tmp_path, monkeypatch):
    c = make_controller(tmp_path, monkeypatch, hooks=kill_at("after_transition", "ONSET_CHECKED"))
    with pytest.raises(Killed):
        c.run()
    c.paths["onset"].write_text("drifted onset")
    for _ in range(2):
        with pytest.raises(Refused):
            runner.Controller.request_resume(
                c.root, EventProcesses(), ReceiptRoles(), {"requested_by": "operator"}, c.hooks
            )
    assert [i["failure_class"] for i in interruptions(c)] == ["controller_killed"]
    assert [d["interruption_index"] for d in book(c)["resume"]] == [0, None]


def test_no_observed_stop_for_a_run_that_never_started(tmp_path, monkeypatch):
    c = make_controller(tmp_path, monkeypatch)
    assert recovery.run_started(read_checkpoint(c.root)) is False
    runner.Controller.resume(c.root, EventProcesses(), ReceiptRoles())
    assert interruptions(c) == []


# ------------------------------------------------------------------ gap 5: pre-launch denial


def run_dir_denial_hooks(message="Permission denied: HARNESS_RUN_DIR"):
    """E14-P2-INJ shape: two transient interrupts, then the run-directory denial armed after
    AUTHORIZED (the delivery sub-wave B routes the frozen deny_write to)."""
    inner = faults(
        {
            ("after_transition", "GOAL_CHECKED"): ConnectionResetError,
            ("after_transition", "REVIEW_APPROVED"): TimeoutError,
        }
    )

    def hook(point, event, ctl):
        if point == "after_transition" and event.get("state") == "AUTHORIZED":
            if not getattr(hook, "armed", False):
                hook.armed = True
                write_boundary.boundary_for(ctl).inject_run_dir_denial("onset", 13, message)
        inner(point, event, ctl)

    return hook


def test_pre_launch_run_dir_denial_is_an_interruption_and_waits(tmp_path, monkeypatch):
    processes = EventProcesses()
    c = make_controller(tmp_path, monkeypatch, hooks=run_dir_denial_hooks(), processes=processes)
    with pytest.raises(PermissionError) as caught:
        c.run()
    assert type(caught.value) is PermissionError  # never WriteDenied
    assert c.pending is None and read_checkpoint(c.root)["pending"] is None
    ledger = book(c)
    assert [i["failure_class"] for i in ledger["interruptions"]] == [
        "connection_reset",
        "timeout",
        "write_denied",
    ]
    denied = ledger["interruptions"][2]
    assert denied["at_state"] == "AUTHORIZED" and denied["pending_operation"] is None
    assert denied["errno"] == 13
    assert denied["filename"] == str(c.root / "checkpoint.json")
    (diagnosis,) = ledger["diagnoses"]
    assert diagnosis["cause"] == "write_denied"
    assert diagnosis["mechanical"] is True and diagnosis["evidenced"] is True
    assert diagnosis["fix_widens_authority"] is True
    assert ledger["mechanical_repairs"] == []
    assert not [r for r in ledger["resume"] if r["initiated_by"] == "evidenced_repair"]
    (note,) = ledger["operator_notifications"]
    assert note["stop_record"]["failure_classes"][-1] == "write_denied"
    stored = result_json(c)
    assert stored["terminal"] == {
        "outcome": "INCOMPLETE",
        "reason": "runtime_failure",
        "failed_gate": "interrupted",
    }
    assert stored["stop_kind"] == "notify_and_wait"
    assert c.state == "AUTHORIZED"
    assert "onset-date" not in processes.tools  # zero producing launches
    assert not any(
        r["kind"] == "process_launch" and r.get("tool") == "onset-date"
        for r in dispatch_events(c.root)
    )
    (denial,) = read_checkpoint(c.root)["context"]["run_dir_denials"]
    assert denial["boundary"] == "pending_checkpoint" and denial["tool"] == "onset-date"
    assert denial["artifact"] == "onset" and denial["denial_source"] == "injected_fault"
    assert denial["errno"] == 13 and denial["state"] == "AUTHORIZED"
    assert "write_denials" not in read_checkpoint(c.root)["context"]  # no commit-time denial
    assert c.write_boundary.run_dir_armed == {} and c.write_boundary.armed == {}
    (event,) = [r for r in dispatch_events(c.root) if r["kind"] == "run_dir_denial"]
    assert event["denial"] == denial
    assert ledger["interruptions"][1]["sequence"] < event["sequence"] < denied["sequence"]


def test_host_permission_error_on_the_pending_checkpoint_clears_pending(tmp_path, monkeypatch):
    c = make_controller(tmp_path, monkeypatch)
    checkpoint = c.checkpoint

    def deny(self=None):
        if c.pending is not None and c.pending.get("skill") == "onset-date":
            raise PermissionError(13, "Access is denied")
        checkpoint()

    monkeypatch.setattr(c, "checkpoint", deny)
    with pytest.raises(PermissionError):
        c.run()
    assert c.pending is None
    assert c.context["run_dir_denials"][0]["denial_source"] == "host_acl"
    assert "onset-date" not in c.processes.tools


def test_run_dir_denial_arming_is_validated_and_independent():
    boundary = write_boundary.Boundary()
    boundary.inject_run_dir_denial("onset", 13, "Permission denied: run")
    with pytest.raises(ValueError):
        boundary.inject_run_dir_denial("onset")
    with pytest.raises(ValueError):
        boundary.inject_run_dir_denial("map", 1, "x")
    with pytest.raises(ValueError):
        boundary.inject_run_dir_denial("", 13, "x")
    boundary.inject_denial("onset")  # the commit-time boundary is a separate one-shot
    assert set(boundary.armed) == set(boundary.run_dir_armed) == {"onset"}


def test_run_dir_denial_for_another_artifact_does_not_fire(tmp_path, monkeypatch):
    c = make_controller(tmp_path, monkeypatch)
    write_boundary.boundary_for(c).inject_run_dir_denial("map", 13, "Permission denied: run")
    hooks_seen = []

    def hook(point, event, ctl):
        if point == "before_tool":
            hooks_seen.append(event["skill"])
        if point == "after_transition" and event["state"] == "ONSET_CREATED":
            raise Killed  # stop after onset-date ran: map never reached

    c.hooks = hook
    with pytest.raises(Killed):
        c.run()
    assert "onset-date" in c.processes.tools
    assert set(c.write_boundary.run_dir_armed) == {"map"}  # still armed, never fired
    assert "run_dir_denials" not in c.context


# ------------------------------------------------------------------ E12-P1-INJ regression


def test_commit_time_denial_is_unchanged(tmp_path, monkeypatch):
    c = plain_controller(tmp_path, monkeypatch, roles=ReceiptRoles())
    hooks = InjectionHooks(deny_onset_case())
    c.hooks = hooks
    with pytest.raises(write_boundary.WriteDenied):
        c.run()
    assert result_json(c)["terminal"] == {
        "outcome": "BLOCKED",
        "reason": "write_denied",
        "failed_gate": "write_denial",
        "denial_source": "injected_fault",
    }
    assert c.state == "AUTHORIZED"
    assert tools_run(c.processes) == ["check_artifact", "onset_date"]  # launched once
    (denial,) = c.context["write_denials"]
    assert denial["denial_source"] == "injected_fault"
    assert c.write_boundary.armed == {} and c.write_boundary.run_dir_armed == {}
    assert "run_dir_denials" not in c.context
    assert recovery.ledger(c.context)["interruptions"] == []  # a Refused, never recovered


# ------------------------------------------------------------------ wave 7f controller defects


def verifiable_transition_requests(root):
    """Every transition request is verifiable the way the projection verifies it: its own
    transition-request-NNN.json (hashed into the checkpoint evidence) equals the checkpoint
    record, and its transition_request dispatch event equals the record minus event_seq."""
    value = read_checkpoint(root)
    records = value["context"]["transition_requests"]
    events = [e for e in dispatch_events(root) if e["kind"] == "transition_request"]
    assert len(events) == len(records)
    for number, (event, record) in enumerate(zip(events, records, strict=True), 1):
        assert record["sequence"] == number
        name = f"transition-request-{number:03}.json"
        assert json.loads((root / name).read_text(encoding="utf-8")) == record
        assert value["evidence"][name] == recovery.tree_hash(root / name)
        assert event["sequence"] == record["event_seq"]
        assert event["transition_request"] == {k: v for k, v in record.items() if k != "event_seq"}
    return records


def gap_free_dispatch_sequence(root):
    """The projection's dispatch-event invariant: 1..N, each recorded, N == the sequence."""
    value = read_checkpoint(root)
    context = value["context"]["dispatch_evidence"]
    events = dispatch_events(root)
    assert [e["sequence"] for e in events] == list(range(1, len(events) + 1))
    for event in events:
        name = f"dispatch-event-{event['sequence']:06}.json"
        assert value["evidence"][name] == recovery.tree_hash(root / name)
        assert event["run_id"] == context["run_id"] and event["schema"] == context["schema"]
    assert len(events) == context["sequence"]
    return events


def test_refused_then_accepted_resume_requests_are_verifiable(tmp_path, monkeypatch):
    """Defect 1: a resume transition request gets the per-record evidence file every other
    transition request gets (operator_ledger.submit_transition_request), refused or accepted."""
    c = with_ledger(tmp_path, monkeypatch, hooks=kill_at("after_transition", "ONSET_CHECKED"))
    with pytest.raises(Killed):
        c.run()
    onset = c.paths["onset"].read_bytes()
    c.paths["onset"].write_text("drifted onset")
    user_resume(tmp_path, "OPR-RESUME-1")
    request = {"initiated_by": "operator_ledger", "requested_by": "operator", "run_id": RUN_ID}
    with pytest.raises(Refused):
        runner.Controller.request_resume(c.root, EventProcesses(), ReceiptRoles(), request, c.hooks)
    (refused,) = verifiable_transition_requests(c.root)
    assert refused["result"] == "REJECTED" and refused["reasons"] == ["artifact:onset"]
    gap_free_dispatch_sequence(c.root)
    c.paths["onset"].write_bytes(onset)  # the drift is undone; a second record authorizes
    user_resume(tmp_path, "OPR-RESUME-2")
    resumed = runner.Controller.request_resume(
        c.root, EventProcesses(), ReceiptRoles(), request, c.hooks
    )
    first, accepted = verifiable_transition_requests(c.root)
    assert first == refused
    assert accepted["result"] == "ACCEPTED" and accepted["requested_via"] == "resume_request"
    assert [d["result"] for d in book(c)["resume"]] == ["REFUSED", "ACCEPTED"]
    gap_free_dispatch_sequence(c.root)
    # The resumed run re-verifies its own evidence (the new files included) and releases.
    assert resumed.run()["terminal"]["outcome"] == "RELEASED"
    assert len(verifiable_transition_requests(c.root)) == 2
    gap_free_dispatch_sequence(c.root)


class KillAfterLaunch(EventProcesses):
    """The controller is killed while ``tool`` runs, AFTER its launch_check and process_launch
    events are durable and before process_exit (the E11-P1-INJ mid-process stop)."""

    def __init__(self, tool, **kwargs):
        super().__init__(**kwargs)
        self.kill_tool = tool

    def run(self, argv, cwd, env, tool=None):
        if tool != self.kill_tool:
            return super().run(argv, cwd, env, tool=tool)
        self.serial += 1
        self.tools.append(tool)
        self.on_event("launch_check", {"serial": self.serial, "free_gib": 9.0})
        self.on_event(
            "process_launch",
            {
                "serial": self.serial,
                "tool": tool,
                "argv": list(argv),
                "pid": 1000 + self.serial,
                "parent_pid": 999,
                "started_monotonic_ns": self.serial,
            },
        )
        raise Killed


def on_disk(root):
    return {p.name: p.read_bytes() for p in root.glob("dispatch-event-*.json")}


def test_resume_keeps_events_written_after_the_checkpoint(tmp_path, monkeypatch):
    """Defect 2: the killed incarnation's launch_check / process_launch events, written after
    its last checkpoint, survive the resume, which continues after the highest on disk."""
    c = with_ledger(tmp_path, monkeypatch, processes=KillAfterLaunch("onset-date"))
    with pytest.raises(Killed):
        c.run()
    recorded = read_checkpoint(c.root)["context"]["dispatch_evidence"]["sequence"]
    before = on_disk(c.root)
    assert len(before) == recorded + 2  # launch_check + process_launch beyond the checkpoint
    killed = [
        json.loads(before[f"dispatch-event-{n:06}.json"]) for n in (recorded + 1, recorded + 2)
    ]
    assert [e["kind"] for e in killed] == ["launch_check", "process_launch"]
    assert killed[1]["tool"] == "onset-date"
    user_resume(tmp_path)
    request = {"initiated_by": "operator_ledger", "requested_by": "operator", "run_id": RUN_ID}
    with pytest.raises(Refused) as caught:
        runner.Controller.request_resume(c.root, EventProcesses(), ReceiptRoles(), request)
    assert caught.value.gate == "pending_operation"
    after = on_disk(c.root)
    assert {name: after[name] for name in before} == before  # never overwritten
    events = gap_free_dispatch_sequence(c.root)
    new = [e for e in events if e["sequence"] > len(before)]
    kinds = [e["kind"] for e in new]
    assert kinds[0] == "interruption" and {"resume", "transition_request"} <= set(kinds)
    (launch,) = [e for e in events if e["kind"] == "process_launch" and e["tool"] == "onset-date"]
    assert launch["sequence"] == recorded + 2
    assert not [
        e for e in events if e["kind"] == "process_exit" and e["serial"] == launch["serial"]
    ]
    (stop,) = interruptions(c)
    assert stop["failure_class"] == "controller_killed" and stop["sequence"] == len(before) + 1
    verifiable_transition_requests(c.root)


def test_accepted_resume_continues_after_unrecorded_events(tmp_path, monkeypatch):
    """The same on an ACCEPTED resume: events the killed controller wrote after its last
    checkpoint are kept, and the resumed run's sequence continues after them to RELEASED."""
    c = make_controller(tmp_path, monkeypatch, hooks=kill_at("after_transition", "ONSET_CHECKED"))
    with pytest.raises(Killed):
        c.run()
    # The killed controller's own dispatch_event, never followed by a checkpoint: exactly what a
    # kill between dispatch_event and the next checkpoint leaves on disk.
    c.dispatch_event("launch_check", serial=99, free_gib=9.0)
    before = on_disk(c.root)
    recorded = read_checkpoint(c.root)["context"]["dispatch_evidence"]["sequence"]
    assert len(before) == recorded + 1
    resumed = runner.Controller.resume(c.root, EventProcesses(), ReceiptRoles(), hooks=c.hooks)
    assert recovery.ledger(resumed.context)["resume"][-1]["result"] == "ACCEPTED"
    assert resumed.run()["terminal"]["outcome"] == "RELEASED"
    after = on_disk(c.root)
    assert {name: after[name] for name in before} == before
    events = gap_free_dispatch_sequence(c.root)
    assert events[recorded]["kind"] == "launch_check" and events[recorded]["serial"] == 99
    assert events[recorded + 1]["kind"] == "interruption"


def test_unverifiable_event_beyond_the_checkpoint_is_kept_but_never_adopted(tmp_path, monkeypatch):
    """A file that is not the controller's own event is not vouched for (left unrecorded, so a
    projection refuses it), and still never overwritten: the sequence skips past it."""
    c = make_controller(tmp_path, monkeypatch, hooks=kill_at("after_transition", "ONSET_CHECKED"))
    with pytest.raises(Killed):
        c.run()
    recorded = read_checkpoint(c.root)["context"]["dispatch_evidence"]["sequence"]
    foreign = c.root / f"dispatch-event-{recorded + 1:06}.json"
    foreign.write_text(json.dumps({"sequence": recorded + 1, "run_id": "other", "kind": "x"}))
    data = foreign.read_bytes()
    resumed = runner.Controller.resume(c.root, EventProcesses(), ReceiptRoles(), hooks=c.hooks)
    assert foreign.read_bytes() == data
    assert foreign.name not in read_checkpoint(c.root)["evidence"]
    (stop,) = recovery.ledger(resumed.context)["interruptions"]
    assert stop["sequence"] == recorded + 2
