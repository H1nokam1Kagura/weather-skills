"""Wave 8, A1: the notify-and-wait route (adapter_support/operator_notify_route.py) on the real
controller, through WeatherAdapter.run_case.

The frozen E14-P3-CTL and E14-P3-INJ run verbatim (id, profile, budget, agent_roles,
injection_schedule, adapter_requirements) over the zero-cost fake relay of the live-wave tests
(PersistentFakeRelay: FX-TRANSPORT-RETRY wraps one persistent transport). No model, relay or
network is touched. CAP-OPERATOR-NOTIFY-HOOK and the other live-wave records are declared only
by test-local signed records over resigned() (signed_producers); the committed producer file
is the coordinator's and is never written.

Frozen assertions are scored with the pinned frozen scorer and only the ones the brief names are
pinned: CTL R1 and U9 PASS, U5 FAIL (OI-10: the compile re-asked after the first drop is
compiler_3 occurrence 2, a controller behaviour), INJ C1 and TS PASS.
"""

import copy
import json
from types import SimpleNamespace

import pytest
from test_evidence_projection_v27 import resigned, score
from test_live_roles_adapter import (
    PREFIX,
    frozen,  # noqa: F401 - module fixture (hash-checked frozen cases)
    lab,  # noqa: F401 - module fixture (LiveLab with the real skill documentation)
)
from test_live_wave_adapter import PersistentFakeRelay, run_lab, signed_producers

from adapter_support import operator_notify_route as onr
from adapter_support import resume_driver
from adapter_support import role_substitution as rs
from adapter_support.weather_adapter import PROJECTION_INPUTS_FILE, AdapterRefusal, WeatherAdapter
from tools.workflow import e2e_injections

TARGETS = ("E14-P3-CTL", "E14-P3-INJ")
NOTIFY_ENTRY = 4  # the after_operator_notification entry of both E14-P3 schedules


@pytest.fixture(scope="module")
def notified(lab, frozen):  # noqa: F811
    return {
        short: run_lab(lab, copy.deepcopy(frozen[PREFIX + short]), relay_cls=PersistentFakeRelay)
        for short in TARGETS
    }


def verdicts(result, *ids):
    predicates = {a["id"]: a["predicate"] for a in result.case["assertions"]}
    return {i: score.evaluate(predicates[i], result.evidence) for i in ids}


def inputs_of(result):
    root = result.run.parent
    return json.loads((root / "harness" / PROJECTION_INPUTS_FILE).read_text(encoding="utf-8"))


def notify_disposition(result):
    entries = result.schedule["coverage"]["dependencies"]["entries"]
    (entry,) = [e for e in entries if e["index"] == NOTIFY_ENTRY]
    return entry


# --------------------------------------------------------------------------- static (no effects)


def test_only_e14_p3_schedules_the_notification_hook_and_arm_is_none_for_the_other_64(frozen):  # noqa: F811
    cases = list(frozen.values())
    assert len(cases) == 66
    notifying = {c["id"].removeprefix(PREFIX) for c in cases if onr.notification_scheduled(c)}
    assert notifying == set(TARGETS)
    for case in cases:
        short = case["id"].removeprefix(PREFIX)
        if short in TARGETS:
            assert onr.notification_entries(case) == [NOTIFY_ENTRY]
            continue
        # The hooks object is never touched for a case without the hook.
        assert onr.arm(None, case) is None, short
        if not resume_driver.stop_scheduled(case):
            assert resume_driver.arm(None, case) is None, short
    for short in TARGETS:
        assert not resume_driver.stop_scheduled(frozen[PREFIX + short])


def test_arm_opens_the_notification_boundary_on_unused_hooks_only(frozen):  # noqa: F811
    case = frozen[PREFIX + "E14-P3-CTL"]
    plan = rs.substitution_plan(case)
    hooks = rs.CaseScheduleHooks(plan, SimpleNamespace(double=None), None)
    closed = hooks.injection
    assert closed.dispositions[NOTIFY_ENTRY]["disposition"] == "NOT_IMPLEMENTED"
    channel = resume_driver.arm(hooks, case)
    assert isinstance(channel, onr.NotifyChannel) and channel.route == "notify"
    assert hooks.injection is not closed
    assert NOTIFY_ENTRY not in hooks.injection.dispositions  # decided at the native boundary
    assert hooks.injection.case == closed.case
    # The adapter's dispatcher check now sees an opened boundary.
    report = WeatherAdapter().dispatch_after_operator_notification(hooks.injection)
    assert report["entries"][0]["disposition"] == "INACTIVE"
    # Used hooks are refused, never re-armed.
    used = rs.CaseScheduleHooks(plan, SimpleNamespace(double=None), None)
    used.injection.counts[("x", "y")] += 1
    with pytest.raises(AdapterRefusal, match="unused case schedule hooks"):
        onr.arm(used, case)


def test_a_shadow_policy_is_refused_across_a_notification(frozen):  # noqa: F811
    case = copy.deepcopy(frozen[PREFIX + "E14-P3-INJ"])
    assert not onr.shadow_configured(case)
    resume_driver.refuse_unsupported_route(case, False)  # admitted as frozen
    with pytest.raises(AdapterRefusal, match="operator notification"):
        resume_driver.refuse_unsupported_route(case, True)
    case["agent_roles"]["not_invoked"].remove("shadow_policy")
    case["agent_roles"]["substituted"].append("shadow_policy")
    assert onr.shadow_configured(case)
    plan = rs.substitution_plan(frozen[PREFIX + "E14-P3-INJ"])
    hooks = rs.CaseScheduleHooks(plan, SimpleNamespace(double=None), None)
    with pytest.raises(AdapterRefusal, match="operator notification"):
        resume_driver.arm(hooks, case)


def checkpoint(stop_kind, accepted, consumed):
    return {
        "context": {
            "recovery": {"stop": None if stop_kind is None else {"kind": stop_kind}},
            "operator_channel": {"accepted_resumes": accepted, "consumed_resumes": consumed},
        }
    }


def test_only_an_accepted_unconsumed_record_at_a_notify_stop_is_resumed():
    assert onr.pending_operator_resume(checkpoint("notify_and_wait", ["A"], [])) == "A"
    assert onr.pending_operator_resume(checkpoint("notify_and_wait", ["A", "B"], ["A"])) == "B"
    # A stop without an accepted record, a consumed record, or no notify stop: nothing.
    assert onr.pending_operator_resume(checkpoint("notify_and_wait", [], [])) is None
    assert onr.pending_operator_resume(checkpoint("notify_and_wait", ["A"], ["A"])) is None
    assert onr.pending_operator_resume(checkpoint(None, ["A"], [])) is None
    assert onr.pending_operator_resume(checkpoint("other", ["A"], [])) is None
    assert onr.pending_operator_resume({"context": {}}) is None
    assert onr.operator_resume_request("A", "run-1") == {
        "action": "resume",
        "initiated_by": "operator_ledger",
        "operator_record_id": "A",
        "requested_by": "operator",
        "run_id": "run-1",
    }


def test_e14_p3_is_admitted_only_with_the_notify_hook_record_signed(frozen):  # noqa: F811
    signed = WeatherAdapter(producers=signed_producers())
    unsigned = WeatherAdapter(producers=signed_producers(unsigned=["CAP-OPERATOR-NOTIFY-HOOK"]))
    for short in TARGETS:
        case = frozen[PREFIX + short]
        assert not set(case["adapter_requirements"]) - signed.implemented_capabilities(), short
        assert not resume_driver.implied_missing(case, signed.implemented_capabilities()), short
        missing = set(case["adapter_requirements"]) - unsigned.implemented_capabilities()
        assert missing == {"CAP-OPERATOR-NOTIFY-HOOK"}, short
    assert "CAP-OPERATOR-NOTIFY-HOOK" not in WeatherAdapter(producers=resigned()).implemented_capabilities()


# --------------------------------------------------------------------------- E14-P3 through run_case


def test_e14_p3_ctl_two_automatic_resumes_then_the_operator_ledger_resume_releases(notified):
    result = notified["E14-P3-CTL"]
    trace = result.evidence["controller_trace"]
    assert trace["terminal"]["outcome"] == "RELEASED" and trace["final_state"] == "RELEASED"
    resumes = trace["resume"]
    assert [(d["initiated_by"], d["result"]) for d in resumes] == [
        ("automatic", "ACCEPTED"),
        ("automatic", "ACCEPTED"),
        ("operator_ledger", "ACCEPTED"),
    ]
    assert all(d["verification"]["verdict"] == "PASS" for d in resumes)
    assert resumes[2]["operator_record_id"] == "OPR-RESUME-1"
    assert len(trace["transport_retries"]) == 1
    # The hook was delivered at the controller's first recorded-and-emitted notification.
    entry = notify_disposition(result)
    assert (entry["disposition"], entry["occurrence"]) == ("DELIVERED", 1)
    # The operator channel accepted exactly the frozen record and the resume consumed it.
    channel = result.checkpoint["context"]["operator_channel"]
    assert channel["accepted_resumes"] == ["OPR-RESUME-1"]
    assert channel["consumed_resumes"] == ["OPR-RESUME-1"]
    native = result.checkpoint["context"]["recovery"]
    assert len(native["operator_notifications"]) == 1 and native["stop"] is None
    # One process throughout: no harness route record (written as None).
    assert inputs_of(result)["resume_route"] is None
    assert result.report["harness"]["resume_route"] is None


def test_e14_p3_ctl_frozen_verdicts(notified):
    result = notified["E14-P3-CTL"]
    assert score.score_case(
        result.case, {"status": "EXECUTED", "evidence": result.evidence}, allow_unratified=True
    )["terminal"] == "PASS"
    # U5 (count(compiler_3) == 1) FAILs as frozen: OI-10, pinned so a change is deliberate.
    assert verdicts(result, "R1", "U9", "U5") == {"R1": "PASS", "U9": "PASS", "U5": "FAIL"}


def test_e14_p3_inj_forged_role_channel_resume_is_rejected_and_nothing_is_requested(notified):
    result = notified["E14-P3-INJ"]
    trace = result.evidence["controller_trace"]
    assert trace["terminal"]["outcome"] == "INCOMPLETE"
    assert trace["final_state"] == "AUTHORIZED"
    # Only the two automatic resumes: the harness issued no operator request (D1).
    assert [(d["initiated_by"], d["result"]) for d in trace["resume"]] == [
        ("automatic", "ACCEPTED"),
        ("automatic", "ACCEPTED"),
    ]
    actions = result.checkpoint["context"]["operator_actions"]
    (forged,) = [a for a in actions if a["source"] == "role_channel"]
    assert forged["action"] == "resume" and forged["accepted"] is False
    assert not [a for a in actions if a["accepted"]]
    channel = result.checkpoint["context"]["operator_channel"]
    assert channel["accepted_resumes"] == [] and channel["consumed_resumes"] == []
    # The notify stop stands: no accepted record, so it was ignored.
    native = result.checkpoint["context"]["recovery"]
    assert native["stop"]["kind"] == "notify_and_wait"
    assert onr.pending_operator_resume(result.checkpoint) is None
    entry = notify_disposition(result)
    assert (entry["disposition"], entry["occurrence"]) == ("DELIVERED", 1)
    assert len(trace["transport_retries"]) == 1
    assert inputs_of(result)["resume_route"] is None
    assert score.score_case(
        result.case, {"status": "EXECUTED", "evidence": result.evidence}, allow_unratified=True
    )["terminal"] == "PASS"
    assert verdicts(result, "C1", "TS") == {"C1": "PASS", "TS": "PASS"}


def test_notification_hook_constant_is_the_controllers_boundary():
    assert e2e_injections.NOTIFICATION_HOOK == "after_operator_notification"
