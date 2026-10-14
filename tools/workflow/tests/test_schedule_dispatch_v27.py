"""v2.7 dependent schedule dispatch and the normalized accepted-approval boundary.

Parity: the frozen reference `simulate_dispatch` is imported read-only by pinned path and
driven with the same boundary observations as the dependent dispatcher. Delivery itself is
stubbed in the parity drive because the reference simulates dispatch semantics only.
"""

import copy
import functools
import hashlib
import importlib.util
import json
import sys

import pytest
from frozen_v27 import frozen_root

from tools.workflow import runner
from tools.workflow.contracts import Refused
from tools.workflow.e2e_injections import (
    HarnessDispatchError,
    InjectionHooks,
    InvalidSchedule,
    UnsupportedInjection,
)
from tools.workflow.tests import test_review_evidence as review_tests
from tools.workflow.tests.test_review_evidence import ProcessStub, make_controller

# Explicit, test-only frozen location, verified against its own FINAL-PINS before use.
FROZEN = frozen_root()  # verified against FINAL-PINS by tests/frozen_v27.py
CASESET = FROZEN / "casesets/cached-onset-e2e-v2_7"
SCHEDULE_FIXES = CASESET / "schedule_fixes.py"
DEV_ONLY = CASESET / "frozen/agentic-e2e-injections.DEV-ONLY.json"
DEPENDENT_SUFFIXES = tuple(
    f"{pair}-{kind}"
    for pair in ("E03-P3", "E14-P1", "E14-P2", "E14-P3", "E15-P1", "E15-P2")
    for kind in ("INJ", "CTL")
)
SYMBOLS = {"HARNESS_RUN_ID": "run-parity", "HARNESS_RUN_DIR": "C:/disposable/run-parity"}
# The frozen simulator knows this boundary; this controller has no producer for it.
UNPRODUCED_HOOKS = {"after_operator_notification"}


def pinned(path):
    pins = json.loads((FROZEN / "FINAL-PINS.json").read_text(encoding="utf-8"))["files"]
    actual = hashlib.sha256(path.read_bytes()).hexdigest()
    assert actual == pins[path.relative_to(FROZEN).as_posix()], f"frozen file drifted: {path}"
    return path


@functools.cache
def frozen():
    spec = importlib.util.spec_from_file_location(
        "frozen_schedule_fixes_v2_7", pinned(SCHEDULE_FIXES)
    )
    module = importlib.util.module_from_spec(spec)
    previous, sys.dont_write_bytecode = sys.dont_write_bytecode, True  # never write into frozen
    try:
        spec.loader.exec_module(module)
    finally:
        sys.dont_write_bytecode = previous
    return module


@functools.cache
def frozen_cases():
    data = json.loads(pinned(DEV_ONLY).read_text(encoding="utf-8"))
    return {c["id"]: c for c in data["cases"]}


def dependent_cases():
    cases = frozen_cases()
    selected = {i: c for i, c in cases.items() if i.endswith(DEPENDENT_SUFFIXES)}
    assert len(selected) == 12
    return selected


def entry(name, hook="after_role", target="planner", occurrence=1, requires=(), **extra):
    value = {
        "id": name,
        "requires": list(requires),
        "hook": hook,
        "selector": {"role_or_tool_or_stage": target, "occurrence": occurrence},
        "operation": extra.pop("operation", "request_transition"),
        "payload": extra.pop("payload", {"action": "resume"}),
    }
    if "plan_scope" in extra:
        value["selector"]["plan_scope"] = extra.pop("plan_scope")
    assert not extra
    return value


def dependent(*schedule, **kwargs):
    return InjectionHooks({"id": "dependent", "injection_schedule": list(schedule)}, **kwargs)


def report(hooks):
    return {e["id"]: e for e in hooks.coverage()["dependencies"]["entries"]}


def approval(occurrence=None, **overrides):
    value = {
        "role": "reviewer",
        "target": "reviewer",
        "verdict": "APPROVE",
        "schema_valid": True,
        "binding_valid": True,
        "controller_accepted": True,
        "approval_committed": True,
        "plan_scope": "initial",
        "plan_hash": "plan-initial",
        "initial_plan_hash": "plan-initial",
        "parent_plan_hash": None,
        "binding_hash": "binding-initial",
        "initial_binding_hash": "binding-initial",
        "parent_binding_hash": None,
    }
    if occurrence is not None:
        value["occurrence"] = occurrence
    value.update(overrides)
    return value


# ---------------------------------------------------------------- load-time contract


@pytest.mark.parametrize(
    "schedule",
    [
        [entry("a", requires=["b"]), entry("b")],  # forward
        [entry("a", requires=["a"])],  # self cycle
        [entry("a"), entry("b", requires=["c"]), entry("c", requires=["b"])],  # cycle
        [entry("a"), entry("a")],  # duplicate id
        [entry("", requires=[])],  # empty id
        [entry("a"), entry("b", requires=["a", "a"])],  # duplicate dependency
        [entry("a"), {**entry("b"), "requires": "a"}],  # non-list
        [entry("a"), {k: v for k, v in entry("b").items() if k != "requires"}],  # mixed
        [entry("a", requires=["ghost"])],  # unknown
    ],
)
def test_invalid_dependencies_rejected_at_load(schedule):
    with pytest.raises(InvalidSchedule):
        dependent(*schedule)


def test_every_frozen_dependent_schedule_loads():
    for case in dependent_cases().values():
        InjectionHooks(case, symbols=SYMBOLS)


def test_schedules_without_requires_keep_legacy_coverage_shape():
    legacy = InjectionHooks(
        {
            "injection_schedule": [
                {
                    "hook": "after_role",
                    "selector": {"role_or_tool_or_stage": "planner", "occurrence": 1},
                    "operation": "request_transition",
                    "payload": {"action": "resume"},
                }
            ]
        }
    )
    assert not legacy.dependent
    legacy("after_role", {"role": "planner"}, None)
    assert set(legacy.coverage()) == {
        "scheduled",
        "applied",
        "unsupported",
        "errors",
        "unreached",
        "records",
    }
    assert legacy.coverage()["applied"] == [0]


# ---------------------------------------------------------------- dependency dispatch


def test_undelivered_dependency_is_inactive_and_never_fires_later():
    h = dependent(
        entry("a", "after_transition", "PLAN_VALIDATED"),
        entry("b", "after_role", "reviewer", requires=["a"]),
    )
    h("after_role", {"role": "reviewer"}, None)  # b's boundary before a delivered
    h("after_transition", {"state": "PLAN_VALIDATED"}, None)
    for _ in range(3):
        h("after_role", {"role": "reviewer"}, None)
    entries = report(h)
    assert entries["a"]["disposition"] == "DELIVERED"
    assert entries["b"]["disposition"] == "INACTIVE"
    assert entries["b"]["reason"] == "dependency_undelivered"
    assert entries["b"]["missing"] == ["a"]
    assert [r["id"] for r in h.records] == ["a"]


def test_inactive_dependency_propagates_to_its_dependents():
    h = dependent(
        entry("a", "after_role", "planner", occurrence=2),
        entry("b", "after_role", "reviewer", requires=["a"]),
        entry("c", "after_transition", "AUTHORIZED", requires=["b"]),
    )
    h("after_role", {"role": "planner"}, None)
    h("after_role", {"role": "reviewer"}, None)
    h("after_role", {"role": "planner"}, None)
    h("after_transition", {"state": "AUTHORIZED"}, None)
    entries = report(h)
    assert [entries[n]["disposition"] for n in "abc"] == ["DELIVERED", "INACTIVE", "INACTIVE"]
    assert entries["c"]["missing"] == ["b"]


def test_same_boundary_dependency_later_in_scan_delivers():
    h = dependent(
        entry("a", "after_transition", "AUTHORIZED"),
        entry("b", "after_transition", "AUTHORIZED", requires=["a"]),
    )
    h("after_transition", {"state": "AUTHORIZED"}, None)
    assert [r["id"] for r in h.records] == ["a", "b"]
    assert {e["disposition"] for e in report(h).values()} == {"DELIVERED"}


def test_delivery_error_is_harness_error_and_stops():
    h = dependent(
        entry(
            "a",
            "after_tool",
            "verify-run",
            operation="mutate_fixture",
            payload={"fixture_operation": "FX-REPORT-RESERIALIZE", "args": {}},
        ),
        entry("b", "after_transition", "AUTHORIZED"),  # independent, later boundary
    )
    with pytest.raises(json.JSONDecodeError):
        h("after_tool", {"skill": "verify-run", "process": {"stdout": "not json"}}, None)
    h("after_transition", {"state": "AUTHORIZED"}, None)
    dependencies = h.coverage()["dependencies"]
    entries = report(h)
    assert dependencies["status"] == "HARNESS_ERROR"
    assert entries["a"]["disposition"] == "HARNESS_ERROR"
    assert entries["b"] == {**entries["b"], "disposition": "INACTIVE", "reason": "dispatch_stopped"}
    assert [r["id"] for r in h.records] == ["a"]


def test_unknown_hook_is_not_implemented_not_silently_unmatched():
    h = dependent(
        entry("notify", "after_operator_notification", "operator_notification"),
        entry("after", "after_transition", "AUTHORIZED", requires=["notify"]),
    )
    entries = report(h)
    assert entries["notify"]["disposition"] == "NOT_IMPLEMENTED"
    assert "after_operator_notification" in entries["notify"]["reason"]
    h("after_operator_notification", {"target": "operator_notification"}, None)
    h("after_transition", {"state": "AUTHORIZED"}, None)
    entries = report(h)
    assert entries["notify"]["disposition"] == "NOT_IMPLEMENTED"
    assert entries["after"]["reason"] == "dependency_undelivered"
    assert h.coverage()["dependencies"]["status"] == "NOT_IMPLEMENTED"
    assert h.records == []


def test_unsupported_delivery_is_not_implemented_and_stops():
    h = dependent(
        entry("a", "after_transition", "AUTHORIZED", payload={"action": "replan"}),
        entry("b", "after_transition", "ONSET_CREATED"),
    )
    with pytest.raises(UnsupportedInjection):
        h("after_transition", {"state": "AUTHORIZED"}, None)
    h("after_transition", {"state": "ONSET_CREATED"}, None)
    entries = report(h)
    assert entries["a"]["disposition"] == "NOT_IMPLEMENTED"
    assert entries["b"]["reason"] == "dispatch_stopped"


def test_interrupt_without_observed_durable_null_pending_is_inactive():
    spec = {
        **entry("i", "after_transition", "REVIEW_APPROVED", operation="interrupt"),
        "payload": {"checkpoint_state": "REVIEW_APPROVED", "pending_operation": None},
    }
    # The controller's after_transition event carries no durability or pending evidence.
    h = dependent(spec, entry("j", "after_transition", "REVIEW_APPROVED", requires=["i"]))
    h("after_transition", {"state": "REVIEW_APPROVED"}, None)
    entries = report(h)
    assert entries["i"]["reason"] == "interrupt_precondition_unobserved"
    assert entries["j"]["reason"] == "dependency_undelivered"


def test_pending_operation_null_documents_current_none_check():
    """Recorded contract finding, not resolved here: the operation adapter still requires
    the legacy string 'none', so a frozen null-pending interrupt is refused as unsupported
    even when durability and an explicit null pending operation are observed."""
    payload = {"checkpoint_state": "REVIEW_APPROVED", "pending_operation": None}
    legacy = InjectionHooks(
        {
            "injection_schedule": [
                {
                    "hook": "after_transition",
                    "selector": {"role_or_tool_or_stage": "REVIEW_APPROVED", "occurrence": 1},
                    "operation": "interrupt",
                    "payload": payload,
                }
            ]
        }
    )
    with pytest.raises(UnsupportedInjection):
        legacy("after_transition", {"state": "REVIEW_APPROVED"}, None)
    assert legacy.coverage()["unsupported"]

    h = dependent(
        {
            **entry("i", "after_transition", "REVIEW_APPROVED"),
            "operation": "interrupt",
            "payload": payload,
        }
    )
    observed = {"checkpoint_durable": True, "checkpoint_state": "REVIEW_APPROVED"}
    with pytest.raises(UnsupportedInjection):
        h(
            "after_transition",
            {"state": "REVIEW_APPROVED", **observed, "pending_operation": None},
            None,
        )
    assert report(h)["i"]["disposition"] == "NOT_IMPLEMENTED"


# ---------------------------------------------------------------- approval boundary dispatch


def test_approval_occurrence_counts_only_accepted_approvals():
    h = dependent(
        entry("one", "after_review_approval", "reviewer", 1, plan_scope="initial"),
        entry("two", "after_review_approval", "reviewer", 2, ["one"], plan_scope="initial"),
    )
    h("after_review_approval", approval(verdict="REJECT"), None)
    h("after_review_approval", approval(verdict="NEEDS-INFO"), None)
    h("after_review_approval", approval(approval_committed=False), None)
    h("after_review_approval", approval(binding_valid="true"), None)  # not literally True
    assert h.accepted_approvals == 0
    h("after_review_approval", approval(1), None)
    entries = report(h)
    assert entries["one"] == {**entries["one"], "disposition": "DELIVERED", "occurrence": 1}
    assert entries["two"]["reason"] == "boundary_not_reached"
    assert h.coverage()["dependencies"]["accepted_approvals"] == 1


def test_approval_occurrence_disagreement_is_harness_error():
    h = dependent(entry("one", "after_review_approval", "reviewer", 1))
    with pytest.raises(HarnessDispatchError):
        h("after_review_approval", approval(2), None)
    assert h.coverage()["dependencies"]["status"] == "HARNESS_ERROR"
    assert report(h)["one"]["reason"] == "dispatch_stopped"


def test_child_of_initial_never_matches_without_real_parent_binding():
    child = entry("child", "after_review_approval", "reviewer", 2, plan_scope="child_of_initial")
    for second in (
        approval(2, plan_scope="child_of_initial"),  # bare label, no parent plan
        approval(2, plan_scope="child_of_initial", plan_hash="plan-child"),
        approval(2, plan_scope="child_of_initial", parent_plan_hash="plan-initial"),
        approval(
            2,
            plan_scope="child_of_initial",
            plan_hash="plan-child",
            parent_plan_hash="unrelated",
        ),
        # A parent BINDING is not a parent plan: the identity is the plan.
        approval(
            2,
            plan_scope="child_of_initial",
            binding_hash="binding-child",
            parent_binding_hash="binding-initial",
        ),
    ):
        h = dependent(child)
        h("after_review_approval", approval(1), None)
        h("after_review_approval", second, None)
        assert report(h)["child"]["reason"] == "boundary_not_reached"
    h = dependent(child)
    h("after_review_approval", approval(1), None)
    h(
        "after_review_approval",
        approval(
            2,
            plan_scope="child_of_initial",
            plan_hash="plan-child",
            parent_plan_hash="plan-initial",
        ),
        None,
    )
    assert report(h)["child"]["disposition"] == "DELIVERED"


def test_initial_scope_follows_the_plan_identity_not_the_binding():
    """Frozen: "initial means the first accepted plan identity". A re-bind of the same plan
    (a runtime-manifest bump changes the binding) stays initial; another plan does not."""
    two = entry("two", "after_review_approval", "reviewer", 2, plan_scope="initial")
    h = dependent(two)
    h("after_review_approval", approval(1), None)
    h("after_review_approval", approval(2, binding_hash="binding-rebound"), None)
    assert report(h)["two"]["disposition"] == "DELIVERED"
    h = dependent(two)
    h("after_review_approval", approval(1), None)
    h("after_review_approval", approval(2, plan_hash="plan-other"), None)
    assert report(h)["two"]["reason"] == "boundary_not_reached"
    h = dependent(two)
    h("after_review_approval", approval(1), None)
    h("after_review_approval", approval(2, plan_hash=None), None)  # unobserved plan
    assert report(h)["two"]["reason"] == "boundary_not_reached"


# ---------------------------------------------------------------- frozen parity


def boundary_of(spec):
    s = spec["selector"]
    return (spec["hook"], s["role_or_tool_or_stage"], s["occurrence"], s.get("plan_scope"))


def timeline(schedule):
    found = []
    for spec in schedule:
        if boundary_of(spec) not in found:
            found.append(boundary_of(spec))
    return found


def observe(schedule, bound, **overrides):
    hook, target, occurrence, scope = bound
    value = {"hook": hook, "target": target, "occurrence": occurrence}
    if hook == "after_review_approval":
        value.update(
            verdict="APPROVE",
            schema_valid=True,
            binding_valid=True,
            controller_accepted=True,
            approval_committed=True,
            plan_scope=scope,
        )
    if hook == "after_operator_notification":
        value.update(recorded=True, emitted=True)
    interrupts = [s for s in schedule if s["operation"] == "interrupt" and boundary_of(s) == bound]
    if interrupts:
        value.update(
            checkpoint_durable=True,
            checkpoint_state=interrupts[0]["payload"]["checkpoint_state"],
            pending_operation=None,
        )
    value.update(overrides)
    return value


def build(schedule, bounds, changes=None):
    """Observation list; earlier compiler invocations precede a compiler_N boundary."""
    result = []
    for bound in bounds:
        hook, target = bound[0], bound[1]
        if target.startswith("compiler_"):
            for n in range(1, int(target[-1])):
                result.append({"hook": hook, "target": f"compiler_{n}", "occurrence": 1})
        result.append(observe(schedule, bound, **(changes or {}).get(bound, {})))
    return result


def scenarios(schedule):
    bounds = timeline(schedule)
    yield "full", build(schedule, bounds)
    yield "reversed", build(schedule, bounds[::-1])
    for k in range(len(bounds)):
        yield (
            f"terminal-before-{k}",
            build(schedule, bounds[:k]) + [{"terminal": True}] + build(schedule, bounds[k:]),
        )
        yield f"never-reached-{k}", build(schedule, bounds[:k] + bounds[k + 1 :])
    for spec in schedule:
        changes = {boundary_of(spec): {"delivery_failure": spec["id"]}}
        yield f"delivery-failure-{spec['id']}", build(schedule, bounds, changes)
    for bound in bounds:
        if any(s["operation"] == "interrupt" and boundary_of(s) == bound for s in schedule):
            for label, change in (
                ("pending-none-string", {"pending_operation": "none"}),
                ("not-durable", {"checkpoint_durable": False}),
                ("wrong-checkpoint", {"checkpoint_state": "CAPTURED"}),
            ):
                yield f"{label}-{bound[1]}", build(schedule, bounds, {bound: change})
            missing = build(schedule, bounds)
            for observation in missing:
                if observation["target"] == bound[1] and "pending_operation" in observation:
                    del observation["pending_operation"]
            yield f"pending-missing-{bound[1]}", missing
        if bound[0] == "after_review_approval":
            for label, change in (
                ("rejected", {"verdict": "REJECT"}),
                ("uncommitted", {"approval_committed": False}),
            ):
                observations = build(schedule, bounds)
                index = next(
                    i
                    for i, o in enumerate(observations)
                    if o.get("plan_scope") == bound[3] and o["hook"] == bound[0]
                )
                observations.insert(index, {**observations[index], **change})
                yield f"{label}-before-approval-{bound[2]}", observations


class SyntheticDeliveryFailure(Exception):
    pass


def to_event(observation):
    hook, target = observation["hook"], observation["target"]
    event = {k: v for k, v in observation.items() if k not in {"hook", "target", "occurrence"}}
    if hook in {"before_role", "after_role"}:
        event["role"] = "compiler" if target.startswith("compiler_") else target
    elif hook == "after_transition":
        event["state"] = target
    elif hook == "after_review_approval":
        # Occurrence is the dispatcher's own accepted count, as in the reference.
        event.update(role=target, target=target, initial_plan_hash="plan-initial")
        if observation.get("plan_scope") == "child_of_initial":
            event.update(plan_hash="plan-child", parent_plan_hash="plan-initial")
        else:
            event.update(plan_hash="plan-initial", parent_plan_hash=None)
    else:
        event["target"] = target
    return hook, event


def drive(case, observations):
    hooks = InjectionHooks(case, symbols=SYMBOLS)

    def stub_apply(operation, payload, point, event, controller, record):
        if event.get("delivery_failure") == record["id"]:
            raise SyntheticDeliveryFailure(record["id"])
        return False  # dispatch semantics only; the reference attests no delivery

    hooks._apply = stub_apply
    for observation in observations:
        if observation.get("terminal"):
            break
        hook, event = to_event(observation)
        try:
            hooks(hook, event, None)
        except SyntheticDeliveryFailure:
            pass
    return hooks


def expected_dispositions(result, schedule):
    out = {}
    for spec in schedule:
        name = spec["id"]
        if name in result["delivered"]:
            out[name] = "DELIVERED"
        elif result.get("failed") == name:
            out[name] = "HARNESS_ERROR"
        else:
            out[name] = "INACTIVE"
    return out


@pytest.mark.parametrize("suffix", DEPENDENT_SUFFIXES)
def test_parity_with_frozen_simulate_dispatch(suffix):
    (case,) = [c for i, c in dependent_cases().items() if i.endswith("-" + suffix)]
    schedule = case["injection_schedule"]
    simulate = frozen().simulate_dispatch
    unproduced = {s["id"] for s in schedule if s["hook"] in UNPRODUCED_HOOKS}
    checked = 0
    for label, observations in scenarios(schedule):
        reference = simulate(copy.deepcopy(schedule), copy.deepcopy(observations))
        hooks = drive(case, observations)
        mine = {e["id"]: e["disposition"] for e in hooks.coverage()["dependencies"]["entries"]}
        expected = expected_dispositions(reference, schedule)
        for name in unproduced:  # documented mismatch: no controller producer for the hook
            assert mine[name] == "NOT_IMPLEMENTED", (suffix, label, name)
        compared = {n: d for n, d in mine.items() if n not in unproduced}
        assert compared == {n: d for n, d in expected.items() if n not in unproduced}, (
            suffix,
            label,
        )
        delivered = [r["id"] for r in hooks.records if r["status"] == "APPLIED"]
        assert delivered == [n for n in reference["delivered"] if n not in unproduced]
        occurrences = {r["id"]: r["occurrence"] for r in hooks.records if r["status"] == "APPLIED"}
        assert occurrences == {
            t["id"]: t["occurrence"] for t in reference["trace"] if t["id"] not in unproduced
        }
        status = hooks.coverage()["dependencies"]["status"]
        if reference["status"] == "HARNESS_ERROR" and reference["failed"] not in unproduced:
            assert status == "HARNESS_ERROR"
        else:
            assert status == ("NOT_IMPLEMENTED" if unproduced else "OK")
        checked += 1
    assert checked >= 6


def test_parity_mismatch_is_exactly_the_unproduced_notification_hook():
    mismatched = set()
    for case_id, case in dependent_cases().items():
        schedule = case["injection_schedule"]
        reference = frozen().simulate_dispatch(
            copy.deepcopy(schedule), build(schedule, timeline(schedule))
        )
        mine = report(drive(case, build(schedule, timeline(schedule))))
        for name, disposition in expected_dispositions(reference, schedule).items():
            if mine[name]["disposition"] != disposition:
                mismatched.add((case_id.rsplit("V2_7-", 1)[1], name, disposition))
    assert mismatched == {
        ("E14-P3-INJ", "forged-resume", "DELIVERED"),
        ("E14-P3-CTL", "operator-resume", "DELIVERED"),
    }


def test_real_dispatch_of_frozen_e14_with_controller_shaped_events():
    """Without the stub: controller after_role events carry no durable-checkpoint evidence,
    so interrupt-1 and every dependent stay inactive (contract finding 3)."""
    (case,) = [c for i, c in dependent_cases().items() if i.endswith("E14-P3-CTL")]
    h = InjectionHooks(case, symbols=SYMBOLS)
    for _ in range(3):
        h("after_role", {"role": "compiler", "answer": {}}, None)
    h("before_role", {"role": "planner", "payload": {}}, None)
    h("after_transition", {"state": "REVIEW_APPROVED"}, None)
    h("after_transition", {"state": "AUTHORIZED"}, None)
    entries = report(h)
    assert entries["interrupt-1"]["reason"] == "interrupt_precondition_unobserved"
    for name in ("planner-retry", "interrupt-2", "interrupt-3"):
        assert entries[name]["reason"] == "dependency_undelivered"
    assert entries["operator-resume"]["disposition"] == "NOT_IMPLEMENTED"
    assert h.records == []


# ---------------------------------------------------------------- controller approval emit


class Stop(Exception):
    pass


def run_past_approval(tmp_path, monkeypatch, *, mode=True, change=None, injections=None):
    c, process = make_controller(tmp_path, monkeypatch, mode=mode, change=change)
    seen = []

    def boundary(point, event, controller):
        if point in {"after_transition", "after_review_approval"}:
            seen.append((point, copy.deepcopy(event)))
        if injections is not None:
            injections(point, event, controller)
        if point == "after_transition" and event["state"] == "AUTHORIZED":
            raise Stop

    c.hooks = boundary
    return c, process, seen


def approvals(seen):
    return [e for p, e in seen if p == "after_review_approval"]


@pytest.mark.parametrize("mode", [True, False])
def test_committed_approval_emits_one_normalized_boundary(tmp_path, monkeypatch, mode):
    c, _, seen = run_past_approval(tmp_path, monkeypatch, mode=mode)
    with pytest.raises(Stop):
        c._run()
    (event,) = approvals(seen)
    assert event["occurrence"] == 1
    assert event["plan_scope"] == "initial"
    assert event["binding_hash"] == c.binding == event["initial_binding_hash"]
    assert event["parent_binding_hash"] is None
    assert event["plan_hash"] == c.plan_hash() == event["initial_plan_hash"]
    assert event["parent_plan_hash"] is None
    assert all(
        event[k] is True
        for k in ("schema_valid", "binding_valid", "controller_accepted", "approval_committed")
    )
    assert (event["verdict"], event["target"], event["state"]) == (
        "APPROVE",
        "reviewer",
        "REVIEW_APPROVED",
    )
    points = [(p, e.get("state")) for p, e in seen]
    index = points.index(("after_transition", "REVIEW_APPROVED"))
    assert points[index + 1] == ("after_review_approval", "REVIEW_APPROVED")
    durable = runner.read_checkpoint(c.root)
    assert durable["context"]["accepted_approvals"] == {
        "occurrence": 1,
        "initial_binding_hash": c.binding,
        "initial_plan_hash": c.plan_hash(),
    }
    assert durable["events"][event["transition_index"]]["to"] == "REVIEW_APPROVED"


def test_rejected_then_approved_counts_only_the_accepted_approval(tmp_path, monkeypatch):
    c, process, seen = run_past_approval(tmp_path, monkeypatch, change="reject_once")
    with pytest.raises(Stop):
        c._run()
    reviewer_returns = [r for r in c.roles.receipts if r["role"] == "reviewer"]
    assert len(reviewer_returns) == 2  # raw reviewer returns are never counted
    (event,) = approvals(seen)
    assert event["occurrence"] == 1


def reviewer_stub(review):
    class ReviewerStub(ProcessStub):
        def run(self, command, workspace, environment, stdin):
            result = super().run(command, workspace, environment, stdin)
            payload = json.loads(stdin)
            if "controller_gates" in payload:
                body = json.loads(result["stdout"])
                body["result"] = json.dumps(review(payload))
                result["stdout"] = json.dumps(body)
            return result

    return ReviewerStub


@pytest.mark.parametrize(
    "review",
    [
        lambda p: {"verdict": "NEEDS-INFO", "binding_hash": p["binding_hash"], "findings": []},
        lambda p: {"verdict": "APPROVE", "binding_hash": p["binding_hash"]},  # malformed
        lambda p: {"verdict": "APPROVE", "binding_hash": "stale", "findings": []},
        lambda p: {"verdict": "REJECT", "binding_hash": "stale", "findings": []},
    ],
    ids=["needs-info", "malformed", "stale-binding", "reject-stale"],
)
def test_non_approvals_emit_nothing_and_consume_no_occurrence(tmp_path, monkeypatch, review):
    monkeypatch.setattr(review_tests, "ProcessStub", reviewer_stub(review))
    c, _, seen = run_past_approval(tmp_path, monkeypatch)
    with pytest.raises(Refused):
        c._run()
    assert approvals(seen) == []
    assert "accepted_approvals" not in runner.read_checkpoint(c.root)["context"]


def test_unconfirmed_durable_commit_or_stale_binding_consumes_nothing(tmp_path, monkeypatch):
    c, _, seen = run_past_approval(tmp_path, monkeypatch)
    with pytest.raises(Stop):
        c._run()
    review = c.context["review"]
    counter = copy.deepcopy(runner.read_checkpoint(c.root)["context"]["accepted_approvals"])
    # The durable checkpoint is past REVIEW_APPROVED now, so nothing is re-confirmed.
    assert c.accepted_approval(review) is None
    assert c.accepted_approval({**review, "verdict": "REJECT"}) is None
    c.binding = "stale"
    assert c.accepted_approval(review) is None
    assert runner.read_checkpoint(c.root)["context"]["accepted_approvals"] == counter
    assert len(approvals(seen)) == 1


def test_dispatcher_delivers_approval_hook_at_occurrence_one_only(tmp_path, monkeypatch):
    schedule = [
        entry("approval-1", "after_review_approval", "reviewer", 1, plan_scope="initial"),
        entry(
            "approval-2",
            "after_review_approval",
            "reviewer",
            2,
            ["approval-1"],
            plan_scope="initial",
        ),
        entry(
            "child",
            "after_review_approval",
            "reviewer",
            1,
            ["approval-1"],
            plan_scope="child_of_initial",
        ),
    ]
    hooks = InjectionHooks({"id": "approval", "injection_schedule": schedule})
    c, _, _ = run_past_approval(tmp_path, monkeypatch, change="reject_once", injections=hooks)
    with pytest.raises(Stop):
        c._run()
    entries = report(hooks)
    assert entries["approval-1"] == {
        **entries["approval-1"],
        "disposition": "DELIVERED",
        "occurrence": 1,
    }
    assert entries["approval-2"]["reason"] == "boundary_not_reached"
    assert entries["child"]["reason"] == "boundary_not_reached"
    assert hooks.resume_requested


def test_frozen_e15_bump_matches_occurrence_one_and_is_not_implemented(tmp_path, monkeypatch):
    (case,) = [c for i, c in dependent_cases().items() if i.endswith("E15-P2-CTL")]
    hooks = InjectionHooks(case, symbols=SYMBOLS)
    c, _, _ = run_past_approval(tmp_path, monkeypatch, injections=hooks)
    with pytest.raises(UnsupportedInjection, match="FX-RUNTIME-MANIFEST-BUMP"):
        c._run()
    entries = report(hooks)
    assert entries["bump-1"] == {
        **entries["bump-1"],
        "disposition": "NOT_IMPLEMENTED",
        "occurrence": 1,
    }
    for name in ("bump-2", "bump-3", "operator-replan", "bump-4"):
        assert entries[name]["reason"] == "dispatch_stopped"
