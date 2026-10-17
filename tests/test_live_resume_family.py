"""Wave 8, A1 (for E10-P2 / E11-P2): live roles across the stop-and-resume route.

The frozen E10-P2-CTL, E10-P2-INJ and E11-P2-CTL run verbatim through WeatherAdapter.run_case
(ids suffixed -TD for their own case roots) on the harness_stop_controller route of
adapter_support/resume_driver.py, with live roles over the zero-cost fake relay of the live-wave
tests and the live-wave records signed test-locally (signed_producers over resigned()).

Incarnation 1 is the THREAD DOUBLE of tests/test_resume_family_adapter.py (ThreadIncarnation):
NEVER evidence of the real route (one pid, incarnation 1 is never killed); it drives the same
parent-side logic on every platform. The forked route runs these cases on Linux CI through
tests/test_resume_family_adapter.py's pattern; here only what the live roles add is checked:

* incarnation 2 builds fresh roles (a second relay member) and its receipts are appended after
  incarnation 1's, restored from the checkpoint;
* session ids stay distinct across the incarnations;
* ``spent`` is restored from the checkpoint into the new live member (E11-P2: the injected
  1.00 USD reading survives the stop);
* the E10-P2 reviewer split (occurrence 2 substituted with REJECT, frozen as applying only after
  a REFUSED resume) is never routed: a refused resume runs nothing and an accepted one reuses the
  approval (WAVE8 D3);
* the tripwire: a substituted receipt of a split role after an ACCEPTED resume is a harness
  error (IncarnationError), shown by fault injection.

E11-P2-INJ needs the budget-override channel of e2e_injections (A3, another worktree); the A3
hunk of resume_driver is exercised here by simulating that delivery (a LOCAL case, never frozen).
"""

import copy
import json
from pathlib import Path
from types import SimpleNamespace

import pytest
from test_evidence_projection_v27 import score
from test_live_roles_adapter import (
    PREFIX,
    FakeRelay,
    frozen,  # noqa: F401 - module fixture (hash-checked frozen cases)
    goal_of,
    lab,  # noqa: F401 - module fixture (LiveLab with the real skill documentation)
)
from test_live_wave_adapter import local_case, signed_producers
from test_resume_family_adapter import ThreadIncarnation

from adapter_support import live_roles, resume_driver
from adapter_support import role_substitution as rs
from adapter_support.weather_adapter import PROJECTION_INPUTS_FILE, WeatherAdapter
from tools.workflow import e2e_injections, runner
from tools.workflow.persistence import read_checkpoint

TARGETS = ("E10-P2-CTL", "E10-P2-INJ", "E11-P2-CTL")
NATIVE = {
    "E10-P2-CTL": ("RELEASED", None, "ACCEPTED"),
    "E10-P2-INJ": ("BLOCKED", "resume_verification", "REFUSED"),
    "E11-P2-CTL": ("RELEASED", None, "ACCEPTED"),
}
STOP_STATE = {"E10-P2-CTL": "AUTHORIZED", "E10-P2-INJ": "AUTHORIZED", "E11-P2-CTL": "ONSET_CHECKED"}


def run_doubled(lab, case, producers=None):  # noqa: F811 - lab is this module's fixture
    """run_case on the thread-double route; every relay member and roles object is kept."""
    adapter = WeatherAdapter(enable_run_case=True, producers=producers or signed_producers())
    goal = goal_of(case)
    profile = lab.profile(goal, case, stub=None)
    relays, built = [], []

    def relay_factory(run_dir):
        relays.append(
            FakeRelay(case, goal, budget=case["budget"]["max_model_usd"], allow_gateway_mock=True)
        )
        return relays[-1]

    def factory(run_dir):
        built.append(adapter.invoke_live_role(case, goal, run_dir, relay_factory))
        return built[-1]

    profile["roles_factory"] = factory
    root = Path(profile["case_root"])
    evidence = adapter.run_case(case, profile)
    inputs = json.loads((root / "harness" / PROJECTION_INPUTS_FILE).read_text(encoding="utf-8"))
    return SimpleNamespace(
        case=case,
        evidence=evidence,
        route=inputs["resume_route"],
        run=root / "run",
        checkpoint=read_checkpoint(root / "run"),
        relays=relays,
        roles=built,
        adapter=adapter,
    )


def doubled_case(frozen, short, suffix="-TD"):  # noqa: F811
    case = copy.deepcopy(frozen[PREFIX + short])
    case["id"] += suffix  # its own case root; frozen schedule, roles and requirements
    return case


@pytest.fixture(scope="module")
def doubled(lab, frozen):  # noqa: F811
    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(resume_driver, "fork_available", lambda: True)
        mp.setattr(resume_driver, "INCARNATION_ROUTE", ThreadIncarnation)
        return {short: run_doubled(lab, doubled_case(frozen, short)) for short in TARGETS}


# --------------------------------------------------------------------------- static


def test_only_e10_p2_splits_a_role_on_the_stop_route(frozen):  # noqa: F811
    stopped = [c for c in frozen.values() if resume_driver.stop_scheduled(c)]
    split = {c["id"].removeprefix(PREFIX): resume_driver.split_roles(c) for c in stopped}
    assert {s for s, roles in split.items() if roles} == {"E10-P2-CTL", "E10-P2-INJ"}
    assert split["E10-P2-INJ"] == {"reviewer"}


def test_tripwire_flags_only_a_substituted_split_role_receipt(frozen):  # noqa: F811
    e10, e11 = frozen[PREFIX + "E10-P2-CTL"], frozen[PREFIX + "E11-P2-CTL"]
    receipts = [
        {"role": "reviewer", "mode": live_roles.LIVE, "invocation_id": "a"},
        {"role": "verifier", "mode": rs.MODE, "invocation_id": "b"},
        {"role": "reviewer", "mode": rs.MODE, "invocation_id": "c"},
    ]
    assert resume_driver.split_receipts_after_accept(e10, receipts) == [
        {"role": "reviewer", "invocation_id": "c"}
    ]
    assert resume_driver.split_receipts_after_accept(e11, receipts) == []
    assert resume_driver.split_receipts_after_accept(e10, receipts[:2]) == []


# --------------------------------------------------------------------------- the double


def test_controller_decides_the_resume_and_the_frozen_terminal_holds(doubled):
    for short, result in doubled.items():
        context = result.checkpoint["context"]
        outcome, gate, decision = NATIVE[short]
        terminal = context["terminal"]
        assert (terminal["outcome"], terminal["failed_gate"]) == (outcome, gate), short
        (first,) = context["recovery"]["resume"]
        assert first["result"] == decision and first["initiated_by"] == "operator_ledger"
        assert result.route["route"] == "thread-double"
        assert [i["incarnation"] for i in result.route["incarnations"]] == [1, 2]
        assert result.route["stop"]["delivery"] == "STOP_REQUESTED"
        assert first["checkpoint_counters"]["state"] == STOP_STATE[short], short
        assert "budget_override" not in result.route["resume_request"]  # not an override case
    failed = doubled["E10-P2-INJ"].checkpoint["context"]["recovery"]["resume"][0]
    assert any(item.startswith("source") for item in failed["verification"]["failed_items"])


def test_incarnation_two_builds_fresh_live_roles_and_appends_its_receipts(doubled):
    for short, result in doubled.items():
        # One roles object (and one live relay member) per incarnation.
        assert len(result.roles) == len(result.relays) == 2, short
        first, second = result.roles
        assert first is not second and result.relays[0] is not result.relays[1]
        assert isinstance(second, live_roles.CombinedRoles)
        assert second.live_member is result.relays[1]
        at_stop = len(first.receipts)
        receipts = result.checkpoint["role_receipts"]
        # Incarnation 2 continues the restored list: incarnation 1's receipts first, unchanged.
        assert [r["invocation_id"] for r in receipts[:at_stop]] == [
            r["invocation_id"] for r in first.receipts
        ]
        if NATIVE[short][2] == "ACCEPTED":
            added = receipts[at_stop:]
            assert added, short
            assert [r["role"] for r in added] == result.relays[1].calls, short
            assert all(r["mode"] == live_roles.LIVE for r in added), short
        else:
            assert len(receipts) == at_stop and result.relays[1].calls == []  # refused: nothing
        sessions = [r["session_id"] for r in receipts]
        assert len(sessions) == len(set(sessions)), short


def test_spent_is_restored_into_the_new_live_member(doubled):
    for short, result in doubled.items():
        counters = result.checkpoint["context"]["recovery"]["resume"][0]["checkpoint_counters"]
        # The fake relay costs nothing, so the final meter is the checkpointed one.
        assert result.relays[1].spent == counters["spent"] == result.checkpoint["spent"], short
    # E11-P2: the injected 1.00 USD reading was checkpointed and survives into incarnation 2.
    e11 = doubled["E11-P2-CTL"]
    assert e11.checkpoint["context"]["recovery"]["resume"][0]["checkpoint_counters"]["spent"] == 1.0
    assert e11.relays[1].spent == 1.0 and e11.relays[0].spent == 1.0


def test_reviewer_occurrence_two_is_never_routed(doubled):
    for short in ("E10-P2-CTL", "E10-P2-INJ"):
        result = doubled[short]
        routes = [r for roles in result.roles for r in roles.routes if r["role"] == "reviewer"]
        assert [(r["occurrence"], r["route"]) for r in routes] == [(1, live_roles.LIVE)], short
        reviewers = [r for r in result.checkpoint["role_receipts"] if r["role"] == "reviewer"]
        assert [r["mode"] for r in reviewers] == [live_roles.LIVE], short
        (entry,) = [
            e
            for e in result.adapter.substitute_role_output(result.case, goal_of(result.case))[
                "entries"
            ]
            if e["native_role"] == "reviewer"
        ]
        assert entry["selector"]["occurrence"] == 2  # scheduled, mapped, never reached


def test_e11_p2_ctl_frozen_resume_verdict(doubled):
    result = doubled["E11-P2-CTL"]
    predicates = {a["id"]: a["predicate"] for a in result.case["assertions"]}
    assert score.evaluate(predicates["R1"], result.evidence) == "PASS"


# --------------------------------------------------------------------------- fault injection


def test_tripwire_raises_on_a_substituted_split_receipt_after_an_accepted_resume(
    lab,  # noqa: F811
    frozen,  # noqa: F811
    monkeypatch,
):
    """Fault injection, NEVER evidence: incarnation 2's controller is wrapped so that, after
    its real run, a substituted reviewer receipt sits in the shared receipts list, as if the
    split occurrence had been routed after the ACCEPTED resume. run_case must fail as a harness
    error rather than project it."""
    monkeypatch.setattr(resume_driver, "fork_available", lambda: True)
    monkeypatch.setattr(resume_driver, "INCARNATION_ROUTE", ThreadIncarnation)
    real = runner.Controller.request_resume.__func__

    def request_resume(cls, *args, **kwargs):
        controller = real(cls, *args, **kwargs)
        run = controller.run

        def forged_run():
            try:
                return run()
            finally:
                controller.roles.receipts.append(
                    {"role": "reviewer", "mode": rs.MODE, "invocation_id": "forged"}
                )

        controller.run = forged_run
        return controller

    monkeypatch.setattr(runner.Controller, "request_resume", classmethod(request_resume))
    with pytest.raises(resume_driver.IncarnationError, match="ACCEPTED resume"):
        run_doubled(lab, doubled_case(frozen, "E10-P2-CTL", suffix="-TW"))


def test_a3_budget_override_reaches_the_controller_and_is_refused(lab, frozen, monkeypatch):  # noqa: F811
    """The A3 hunk of resume_driver, ahead of the e2e_injections half (another worktree): a
    LOCAL copy of E11-P2-CTL whose deferred request_transition record carries the frozen
    E11-P2-INJ budget_override as ``resume_override`` (simulated delivery). The controller
    refuses it whole with its refusal record; the harness only passes it through."""
    override = frozen[PREFIX + "E11-P2-INJ"]["injection_schedule"][2]["payload"]["budget_override"]
    real = e2e_injections.InjectionHooks.apply_deferred

    def apply_deferred(self, controller, entries=None):
        applied = real(self, controller, entries)
        for record in self.records:
            deferred = record.get("deferred_after_stop") is not None
            if record["operation"] == "request_transition" and deferred:
                record["resume_override"] = copy.deepcopy(override)
        return applied

    monkeypatch.setattr(e2e_injections.InjectionHooks, "apply_deferred", apply_deferred)
    monkeypatch.setattr(resume_driver, "fork_available", lambda: True)
    monkeypatch.setattr(resume_driver, "INCARNATION_ROUTE", ThreadIncarnation)
    case = local_case(frozen, "E11-P2-CTL", suffix="A3-OVERRIDE")
    result = run_doubled(lab, case)
    assert result.route["resume_request"]["budget_override"] == override
    assert result.route["resume_result"] == {"result": "REFUSED", "gate": "budget"}
    context = result.checkpoint["context"]
    (decision,) = context["recovery"]["resume"]
    assert decision["result"] == "REFUSED" and decision["override_requested"] is True
    assert decision["budget_override_applied"] is False
    refusal = decision["refusal_record"]
    assert set(refusal) == {"request", "illegal_fields", "governing_limit"}
    assert refusal["illegal_fields"] == sorted(k for k in override if k != "justification")
    assert context["terminal"]["outcome"] == "BLOCKED"
    assert context["terminal"]["failed_gate"] == "budget"
    assert result.checkpoint["state"] == "ONSET_CHECKED"
    assert result.relays[1].calls == []  # no resumed work
