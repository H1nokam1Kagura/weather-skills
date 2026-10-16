"""CAP-SHADOW: the controller's shadow policy driven by the frozen E09-P1 schedules.

The two frozen cases that need nothing beyond the signed capabilities but CAP-SHADOW
(DEV-E2E-V2_7-E09-P1-CTL and -INJ) run through WeatherAdapter.run_case on the real controller
with their frozen adapter_requirements, agent_roles and injection_schedule verbatim. The
CAP-SHADOW record is signed in a test-local copy of CAPABILITY-PRODUCERS.json; the committed
record is signed too (D4 review at 0f264d2), and the gate test strips it to show it refuses.
Tools are the stand-in scripts of test_evidence_projection_v27.
"""

import copy
import hashlib
import json
from pathlib import Path
from types import SimpleNamespace

import pytest
from test_evidence_projection_v27 import (
    FROZEN_CASES,
    FROZEN_CASES_SHA256,
    MAP_GOAL,
    Lab,
    fails,
    findings,
    resigned,
    score,
)

from adapter_support import evidence_projection as ep
from adapter_support import role_substitution as rs
from adapter_support import shadow_projection as sp
from adapter_support.weather_adapter import AdapterRefusal, WeatherAdapter, load_producers
from tools.workflow import policy, runner
from tools.workflow.contracts import digest
from tools.workflow.persistence import read_checkpoint
from tools.workflow.profiles import stages

PREFIX = "DEV-E2E-V2_7-"
SIGN = {
    "reviewer": "test-local",
    "reviewed_at": "2026-10-14T00:00:00Z",
    "reviewed_commit": "0" * 40,
}
CASE_KEYS = (
    "id",
    "profile",
    "request_delta",
    "budget",
    "agent_roles",
    "injection_schedule",
    "adapter_requirements",
)


@pytest.fixture(scope="module")
def frozen():
    raw = FROZEN_CASES.read_bytes()
    assert hashlib.sha256(raw).hexdigest() == FROZEN_CASES_SHA256
    return json.loads(raw)


def frozen_case(frozen, short):
    (case,) = [c for c in frozen["cases"] if c["id"] == PREFIX + short]
    return case


def run_input(frozen, short):
    """The frozen case's run inputs, verbatim (adapter_requirements included)."""
    return {k: copy.deepcopy(frozen_case(frozen, short)[k]) for k in CASE_KEYS}


def signed():
    """resigned() (the committed file, rereview flags cleared test-locally) with CAP-SHADOW
    signed test-locally. Never written back."""
    return resigned(**{"CAP-SHADOW": SIGN})


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
        mp.setattr(runner, "preflight", lambda *args: ({}, {}))
        yield Lab(tmp_path_factory.mktemp("shadow"))


def execute(lab, case, producers=None):
    adapter = WeatherAdapter(enable_run_case=True, producers=producers or signed())
    profile = lab.profile(MAP_GOAL, case, stub=None)
    built = []

    def factory(run_dir):
        built.append(adapter.substituted_roles_factory(case, MAP_GOAL, run_dir))
        return built[-1]

    profile["roles_factory"] = factory
    evidence = adapter.run_case(case, profile)
    root = Path(profile["case_root"])
    report = json.loads((root / "harness/evidence-projection.json").read_text(encoding="utf-8"))
    run = root / "run"
    return SimpleNamespace(
        evidence=evidence, report=report, run=run, checkpoint=read_checkpoint(run), roles=built[0]
    )


@pytest.fixture(scope="module")
def e09(lab, frozen):
    return {k: execute(lab, run_input(frozen, f"E09-P1-{k}")) for k in ("CTL", "INJ")}


def probe(frozen, short, name):
    (bundle,) = [b for b in frozen_case(frozen, short)["probe_bundles"] if b["name"] == name]
    return bundle["evidence"]


def predicate(frozen, short, assertion_id):
    (a,) = [a for a in frozen_case(frozen, short)["assertions"] if a["id"] == assertion_id]
    return a["predicate"]


def shadow_receipts(result):
    return [r for r in result.checkpoint["role_receipts"] if r["role"] == sp.ROLE]


def tools(result):
    return [r["tool"] for r in result.evidence["process_receipts"]]


def vocabulary():
    sequence = stages("onset_map")
    return {s: f"PROCEED_TO_{sequence[sequence.index(s) + 1]}" for s in policy.SHADOW_STATES}


def assert_shadow_delivery(result):
    """Every shadow invocation is the double's own receipt, bound to the decision the driver
    armed at that state's after_transition emit; no baseline is claimed."""
    receipts = shadow_receipts(result)
    assert len(receipts) == 4 and all(r["mode"] == rs.MODE for r in receipts)
    files = sorted(result.run.glob("role-[0-9][0-9][0-9]-shadow_policy.json"))
    records = [json.loads(p.read_text(encoding="utf-8")) for p in files]
    for k, (receipt, record) in enumerate(zip(receipts, records, strict=True)):
        assert receipt["baseline_output_hash"] is None
        (sub,) = receipt["substitutions"]
        assert sub["hook"] == "after_transition" and sub["schedule_indexes"] == [k]
        assert sub["pre_substitution_output_hash"] is None
        assert sub["delivered_output_hash"] == receipt["output_hash"] == digest(record["output"])
        assert record["input"]["state"] == policy.SHADOW_STATES[k]
        assert record["receipt"]["session_id"] == receipt["session_id"]
    sessions = [r["session_id"] for r in result.checkpoint["role_receipts"]]
    assert len(set(sessions)) == len(sessions)
    coverage = result.report["harness"]["shadow_schedule"]["coverage"]
    assert coverage["applied"] == [0, 1, 2, 3] and coverage["unreached"] == []
    return receipts, records


def test_e09_p1_ctl_four_current_decisions_are_accepted_and_the_run_releases(e09, frozen):
    ctl = e09["CTL"]
    trace = ctl.evidence["controller_trace"]
    assert trace["terminal"]["outcome"] == "RELEASED" and trace["final_state"] == "RELEASED"
    assert trace["states"] == stages("onset_map")
    receipts, records = assert_shadow_delivery(ctl)
    # The controller's own records: four accepted, never applied, no authority.
    native = ctl.checkpoint["context"]["shadow_decisions"]
    assert [d["accepted"] for d in native] == [True] * 4
    assert all(d["reason"] is None and d["authority_granted"] is False for d in native)
    assert [d["action"] for d in native] == [vocabulary()[s] for s in policy.SHADOW_STATES]
    for record in records:
        assert record["output"]["question_fingerprint"] == record["input"]["question_hash"]
    # Contract projection equals the frozen clean-release probe bundle exactly.
    bundle = probe(frozen, "E09-P1-CTL", "clean release")
    assert ctl.evidence["role_output"]["shadow_policy"] == bundle["role_output"]["shadow_policy"]
    projected = [
        {k: d[k] for k in ("state", "accepted", "action", "score")}
        for d in trace["shadow_decisions"]
    ]
    assert projected == bundle["controller_trace"]["shadow_decisions"]
    symbols = ctl.evidence["symbols"]
    for state, label in vocabulary().items():
        assert symbols[f"HARNESS_CANDIDATE_LABEL:{state}.proceed"] == label
        assert symbols[f"HARNESS_CANDIDATE_LABEL:{state}.stop"] == "STOP"
    for decision in native:
        assert (
            symbols[f"HARNESS_DECISION_FINGERPRINT:{decision['state']}"]
            == decision["question_hash"]
        )
    assert not [m for m in ctl.report["missing"] if m["capability"] == sp.CAPABILITY]
    # Frozen scorer: U0 and every run_evidence validator (V25 record lists and the shadow
    # decision reconciliation included) find no FAIL.
    assert score.evaluate(predicate(frozen, "E09-P1-CTL", "U0"), ctl.evidence) == "PASS"
    for name, found in findings(frozen, PREFIX + "E09-P1-CTL", ctl.evidence).items():
        assert fails(found) == [], name


def test_e09_p1_inj_four_malformed_decisions_are_rejected_and_the_path_is_unchanged(e09, frozen):
    ctl, inj = e09["CTL"], e09["INJ"]
    trace = inj.evidence["controller_trace"]
    assert trace["terminal"]["outcome"] == "RELEASED" and trace["states"] == stages("onset_map")
    receipts, records = assert_shadow_delivery(inj)
    native = inj.checkpoint["context"]["shadow_decisions"]
    assert [d["accepted"] for d in native] == [False] * 4
    assert [d["reason"] for d in native] == [
        "stale question fingerprint",
        "action masked at this state",
        "score is not a number",
        "score outside [0, 1]",
    ]
    assert native[1]["action"] == "RELEASE_NOW" and native[2]["score"] == "NaN"
    assert all(d["applied"] is False and d["authority_granted"] is False for d in native)
    # The deterministic path is the control's: same tools in the same order, same states.
    assert (
        tools(inj) == tools(ctl) and trace["states"] == ctl.evidence["controller_trace"]["states"]
    )
    # Entry 0's stale fingerprint names SOURCE_CHECKED, where no shadow question exists: the
    # labelled harness stand-in, recorded on the receipt, never a controller value.
    (stale,) = [
        r for r in receipts[0]["substitutions"][0]["resolutions"] if "FINGERPRINT" in r["symbol"]
    ]
    assert stale["symbol"] == "HARNESS_DECISION_FINGERPRINT:SOURCE_CHECKED"
    assert stale["source"] == "harness_authored_stale_fingerprint"
    assert stale["value"] == digest({"harness_stale_fingerprint": stale["symbol"]})
    assert records[0]["output"]["question_fingerprint"] == stale["value"]
    assert records[0]["output"]["question_fingerprint"] != records[0]["input"]["question_hash"]
    bundle = probe(frozen, "E09-P1-INJ", "all rejected, release")
    assert inj.evidence["role_output"]["shadow_policy"] == bundle["role_output"]["shadow_policy"]
    projected = [
        {k: d[k] for k in ("state", "accepted", "action", "score")}
        for d in trace["shadow_decisions"]
    ]
    assert projected == bundle["controller_trace"]["shadow_decisions"]
    (note,) = [n["shadow_policy"] for n in inj.report["notes"] if "shadow_policy" in n]
    assert [r["symbol"] for r in note["harness_resolutions"]] == [stale["symbol"]]
    for assertion_id in ("C1", "C2", "TS"):
        assert score.evaluate(predicate(frozen, "E09-P1-INJ", assertion_id), inj.evidence) == (
            "PASS"
        ), assertion_id
    for name, found in findings(frozen, PREFIX + "E09-P1-INJ", inj.evidence).items():
        assert fails(found) == [], name


# --------------------------------------------------------------------------- D4 gate / MISSING


def test_cap_shadow_is_declared_only_by_a_signed_record(frozen, lab, tmp_path):
    record = load_producers()["capabilities"]["CAP-SHADOW"]
    assert record["d4_gated"] is True and record["status"] == "PRODUCER_MAPPED"
    assert record["reviewer"] and record["reviewed_at"] and record["reviewed_commit"]
    assert record["missing_fields"] and record["tests"]
    # Re-signed after the live-role wave (D4-REVIEW-LW-C at d6c2809), then flagged
    # rereview_required by the setup-timing fix (weather_adapter.py) and the claim-gate fix
    # (runner.py): undeclared in the committed file until re-signed; the test-local re-signed
    # copy declares it.
    assert record["rereview_required"].strip()
    assert "CAP-SHADOW" not in WeatherAdapter().implemented_capabilities()
    assert "CAP-SHADOW" in WeatherAdapter(producers=resigned()).implemented_capabilities()
    unsigned = resigned(
        **{"CAP-SHADOW": {"reviewer": None, "reviewed_at": None, "reviewed_commit": None}}
    )
    assert "CAP-SHADOW" not in WeatherAdapter(producers=unsigned).implemented_capabilities()
    assert "CAP-SHADOW" in WeatherAdapter(producers=signed()).implemented_capabilities()
    adapter = WeatherAdapter(enable_run_case=True, producers=unsigned)  # signature stripped
    case = run_input(frozen, "E09-P1-CTL")
    case["id"] += "-LOCAL-UNSIGNED"  # the e09 fixture staged the frozen id's source
    profile = lab.profile(MAP_GOAL, case, stub=None)
    profile["case_root"] = tmp_path / "never-E09-P1-CTL"
    with pytest.raises(AdapterRefusal, match="CAP-SHADOW"):
        adapter.run_case(case, profile)
    # A local case that configures the shadow policy without requiring CAP-SHADOW is refused
    # too: the policy mode is never switched on by an undeclared capability.
    case["adapter_requirements"] = ["CAP-NOT-IMPLEMENTED"]
    with pytest.raises(AdapterRefusal, match="CAP-SHADOW is undeclared"):
        adapter.run_case(case, profile)
    assert not profile["case_root"].exists()


def test_drive_shadow_policy_declares_the_controllers_points_and_vocabulary(frozen):
    adapter = WeatherAdapter()
    plan = adapter.drive_shadow_policy(run_input(frozen, "E09-P1-INJ"))
    assert plan["configured"] and plan["policy_mode"] == runner.shadow.MODE
    assert plan["role_mode"] == "substituted"
    assert plan["invocation_points"] == list(policy.SHADOW_STATES)
    assert plan["vocabulary"] == {
        s: {"proceed": p, "stop": "STOP"} for s, p in vocabulary().items()
    }
    assert [e["index"] for e in plan["entries"]] == [0, 1, 2, 3]
    assert (
        sp.schedule_without_shadow(run_input(frozen, "E09-P1-INJ"), plan)["injection_schedule"]
        == []
    )
    # A case naming no shadow role runs the controller's rules mode: nothing is declared.
    plain = adapter.drive_shadow_policy(run_input(frozen, "E01-P1-CTL"))
    assert plain == {"capability": sp.CAPABILITY, "configured": False, "policy_mode": "rules"}
    # E09-P2: a live shadow policy; its before_role note patch is not a shadow entry.
    live = adapter.drive_shadow_policy(run_input(frozen, "E09-P2-INJ"))
    assert live["role_mode"] == "live" and live["entries"] == []
    assert sp.attach(live, None) is None


@pytest.mark.parametrize(
    ("change", "message"),
    [
        (lambda c: c["agent_roles"]["substituted"].remove("shadow_policy"), "names no shadow"),
        (lambda c: c["agent_roles"]["live"].append("shadow_policy"), "both live and substituted"),
        (lambda c: c["injection_schedule"].pop(3), "no decision is scheduled for \\['DATA"),
        (
            lambda c: c["injection_schedule"].append(
                {**c["injection_schedule"][0], "hook": "after_role"}
            ),
            "mixing shadow decision entries",
        ),
        (
            lambda c: c["injection_schedule"][0]["selector"].update(
                role_or_tool_or_stage="SOURCE_CHECKED"
            ),
            "not a shadow invocation point",
        ),
        (lambda c: c["injection_schedule"][0]["payload"].pop("score"), "must carry exactly"),
        (
            lambda c: c["injection_schedule"][0]["payload"].update(action="HARNESS_RUN_DIR"),
            "no shadow mapping at /action",
        ),
        (
            lambda c: c["injection_schedule"][0]["payload"].update(
                action="HARNESS_CANDIDATE_LABEL:PLAN_VALIDATED.proceed"
            ),
            "not a candidate of the GOAL_CHECKED decision",
        ),
        (
            lambda c: c["injection_schedule"][0]["payload"].update(
                action="HARNESS_CANDIDATE_LABEL:GOAL_CHECKED.release"
            ),
            "not a candidate of the GOAL_CHECKED decision",
        ),
        (lambda c: c["injection_schedule"][1]["selector"].update(occurrence=0), "positive"),
        (lambda c: c["injection_schedule"][0].update(id="a", requires=[]), "dependent"),
    ],
)
def test_unmappable_shadow_schedules_are_refused_never_skipped(frozen, change, message):
    case = run_input(frozen, "E09-P1-INJ")
    change(case)
    with pytest.raises(AdapterRefusal, match=message):
        WeatherAdapter().drive_shadow_policy(case)


def question(state="GOAL_CHECKED", rehash=False, **change):
    sequence = stages("onset_map")
    q = policy.shadow_question(state, sequence[sequence.index(state) + 1], "b", {"x": 1})
    q.update(change)
    if rehash:
        q["question_hash"] = sp.question_body_hash(q)
    return q


def armed_driver(frozen):
    plan = sp.shadow_plan(run_input(frozen, "E09-P1-CTL"))
    driver = sp.ShadowDriver(plan)
    driver("after_transition", {"state": "GOAL_CHECKED"}, None)
    return driver


def test_the_double_answers_only_an_armed_bound_controller_question(frozen):
    case = run_input(frozen, "E09-P1-CTL")
    roles = rs.SubstitutedRoles(case, MAP_GOAL)
    with pytest.raises(rs.SubstitutionUnavailable, match="no frozen baseline"):
        roles.ask("shadow_policy", question())  # no driver attached
    sp.attach(sp.shadow_plan(case), roles)
    with pytest.raises(rs.SubstitutionUnavailable, match="no shadow decision is scheduled"):
        roles.ask("shadow_policy", question())  # nothing armed at an after_transition emit
    assert roles.receipts == []  # a failed substitute returns nothing and writes no receipt
    driver = armed_driver(frozen)
    with pytest.raises(rs.SubstitutionUnavailable, match="does not bind its body"):
        driver.decide(question(binding_hash="changed after fingerprinting"))
    with pytest.raises(rs.SubstitutionUnavailable, match="declared vocabulary"):
        driver.decide(question(rehash=True, candidates={"proceed": "X", "stop": "STOP"}))
    with pytest.raises(rs.SubstitutionUnavailable, match="PLAN_VALIDATED question"):
        driver.decide(question("PLAN_VALIDATED"))
    q = question()
    answer = driver.decide(q)
    assert answer == {
        "schema": "workflow.decision/1",
        "state": "GOAL_CHECKED",
        "question_fingerprint": q["question_hash"],
        "action": "PROCEED_TO_PLAN_VALIDATED",
        "score": 0.8,
    }
    with pytest.raises(rs.SubstitutionBindingError, match="does not bind"):
        driver.bind_receipt({"role": "shadow_policy", "output_hash": "x"})
    with pytest.raises(rs.SubstitutionBindingError, match="no shadow decision"):
        driver.bind_receipt({"role": "shadow_policy", "output_hash": digest(answer)})


# --------------------------------------------------------------------------- projection paths


class FakeNative:
    def __init__(self, mode, decisions=None, events=(), finished=True, receipts=(), files=()):
        self.checkpoint = {"policy_mode": mode, "evidence": {}}
        self.context = {} if decisions is None else {"shadow_decisions": decisions}
        self.events = list(events)
        self.receipts = list(receipts)
        self.role_files = list(files)
        self._finished = finished
        self.root = Path(".")

    def kinds(self, kind):
        return [e for e in self.events if e["kind"] == kind]

    def finished(self):
        return self._finished


def projected(native):
    projection = {
        "evidence": {"controller_trace": {}, "role_output": {"shadow_policy": []}, "symbols": {}},
        "missing": [],
        "notes": [],
    }
    sp.project(projection, native)
    return projection


def test_shadow_decisions_are_empty_only_when_the_controller_proves_none_exist():
    # rules mode: the controller asks no shadow question (shadow.enabled is false).
    out = projected(FakeNative("rules"))
    assert out["evidence"]["controller_trace"]["shadow_decisions"] == []
    # shadow mode, finished, no decision event and no invocation: no point was reached.
    out = projected(FakeNative(runner.shadow.MODE))
    assert out["evidence"]["controller_trace"]["shadow_decisions"] == []
    # Anything short of that stays MISSING with its reason, never [].
    for native, reason in (
        (FakeNative(None), "records no policy_mode"),
        (FakeNative(runner.shadow.MODE, finished=False), "recorded no shadow_decisions"),
        (
            FakeNative(runner.shadow.MODE, events=[{"kind": "shadow_decision", "decision": {}}]),
            "recorded no shadow_decisions",
        ),
    ):
        out = projected(native)
        assert "shadow_decisions" not in out["evidence"]["controller_trace"]
        (gap,) = out["missing"]
        assert gap["capability"] == "CAP-SHADOW" and reason in gap["reason"]
    with pytest.raises(ep.EvidenceMappingError, match="outside shadow_policy mode"):
        projected(FakeNative("rules", decisions=[]))


def test_tampered_shadow_records_refuse_the_projection(e09, tmp_path):
    import shutil

    from tools.workflow.persistence import write_checkpoint

    def tampered(edit):
        run = tmp_path / f"run-{len(list(tmp_path.iterdir()))}"
        shutil.copytree(e09["CTL"].run, run)
        checkpoint = read_checkpoint(run)
        checkpoint["root"] = str(run)
        edit(checkpoint)
        write_checkpoint(run, checkpoint)
        return run

    def flip(checkpoint):
        checkpoint["context"]["shadow_decisions"][0]["accepted"] = False

    def drop(checkpoint):
        checkpoint["context"]["shadow_decisions"].pop()

    for edit, message in ((flip, "differs from its native record"), (drop, "dispatch events")):
        with pytest.raises(ep.EvidenceMappingError, match=message):
            ep.project_run(tampered(edit))
    assert ep.project_run(tampered(lambda c: None))["evidence"]["controller_trace"][
        "shadow_decisions"
    ]
