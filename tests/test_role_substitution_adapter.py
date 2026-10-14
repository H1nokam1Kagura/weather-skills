"""CAP-ROLE-SUBSTITUTE: substituted role doubles + after_role substitution on the real controller.

Frozen pairs run through WeatherAdapter.run_case with the substituted_roles_factory the
container case driver calls: E01-P1-CTL, E02-P1 (CTL/INJ), E04-P1 (CTL/INJ), E13-P1 (CTL/INJ).
The local case keeps the frozen case's id, profile, budget, agent_roles and injection_schedule;
only adapter_requirements is local (CAP-ROLE-SUBSTITUTE stays D4-gated: reviewer null), so the
frozen cases themselves remain NOT_IMPLEMENTED. Tools are the module's stand-in scripts.
"""

import copy
import hashlib
import json
from pathlib import Path
from types import SimpleNamespace

import pytest
from test_evidence_projection_v27 import FROZEN_CASES, FROZEN_CASES_SHA256, MAP_GOAL, Lab

from adapter_support import role_substitution as rs
from adapter_support.weather_adapter import AdapterRefusal, WeatherAdapter
from tools.workflow import runner
from tools.workflow.contracts import digest
from tools.workflow.persistence import read_checkpoint
from tools.workflow.review_evidence import text_hash

PREFIX = "DEV-E2E-V2_7-"


@pytest.fixture(scope="module")
def frozen():
    raw = FROZEN_CASES.read_bytes()
    assert hashlib.sha256(raw).hexdigest() == FROZEN_CASES_SHA256
    return {c["id"]: c for c in json.loads(raw)["cases"]}


def local_case(frozen, short):
    case = frozen[PREFIX + short]
    local = {
        k: copy.deepcopy(case[k])
        for k in ("id", "profile", "request_delta", "budget", "agent_roles", "injection_schedule")
    }
    local["adapter_requirements"] = ["CAP-NOT-IMPLEMENTED"]
    return local


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
        yield Lab(tmp_path_factory.mktemp("rolesub"))


def execute(lab, case):
    adapter = WeatherAdapter(enable_run_case=True)
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
        evidence=evidence,
        report=report,
        run=run,
        checkpoint=read_checkpoint(run),
        roles=built[0],
        root=root,
    )


def role_files(run):
    return [
        json.loads(p.read_text(encoding="utf-8"))
        for p in sorted(run.glob("role-[0-9][0-9][0-9]-*.json"))
    ]


def dispatch_returned(run):
    out = {}
    for path in sorted(run.glob("dispatch-event-*.json")):
        event = json.loads(path.read_text(encoding="utf-8"))
        if event.get("kind") == "dispatch_returned":
            out[event["invocation_id"]] = event["receipt"]
    return out


def assert_receipts_preserved(result):
    """Every receipt is the double's own, bound to the delivered output, identity unchanged."""
    receipts = result.checkpoint["role_receipts"]
    assert receipts and all(r["mode"] == rs.MODE for r in receipts)
    assert receipts == result.roles.receipts
    sessions = [r["session_id"] for r in receipts]
    assert len(set(sessions)) == len(sessions) and all(sessions)
    files = role_files(result.run)
    assert len(files) == len(receipts)
    for record, receipt in zip(files, receipts, strict=True):
        assert record["receipt"]["session_id"] == receipt["session_id"]
        assert record["receipt"]["invocation_id"] == receipt["invocation_id"]
        assert digest(record["output"]) == receipt["output_hash"]
    projected = result.evidence["invocation_receipts"]
    assert [p["session_id"] for p in projected] == sessions
    assert {p["mode"] for p in projected} == {rs.MODE}
    returned = dispatch_returned(result.run)
    for receipt in receipts:
        assert returned[receipt["invocation_id"]]["session_id"] == receipt["session_id"]
        assert returned[receipt["invocation_id"]]["output_hash"] == receipt["output_hash"]


def substituted(receipts, role):
    return [r for r in receipts if r["role"] == role and r.get("substitutions")]


def launch_hashes_present(result):
    """HarnessLaunchObserver records survive the chaining: producing launches carry them."""
    producing = [r for r in result.evidence["process_receipts"] if r.get("producing")]
    return len(producing) == 2 and all(
        r["plan_hash_at_launch"] == r["approval_plan_hash"] for r in producing
    )


def no_producing_process(result):
    return not any(r.get("producing") for r in result.evidence["process_receipts"])


# --------------------------------------------------------------------------- frozen pairs


@pytest.fixture(scope="module")
def e02(lab, frozen):
    return {k: execute(lab, local_case(frozen, f"E02-P1-{k}")) for k in ("CTL", "INJ")}


def test_e02_p1_ctl_releases_with_three_empty_after_role_patches(e02):
    ctl = e02["CTL"]
    assert ctl.evidence["controller_trace"]["final_state"] == "RELEASED"
    assert_receipts_preserved(ctl)
    compilers = substituted(ctl.checkpoint["role_receipts"], "compiler")
    assert [c["substitutions"][0]["schedule_indexes"] for c in compilers] == [[0], [1], [2]]
    for receipt in compilers:
        (sub,) = receipt["substitutions"]
        assert sub["hook"] == "after_role" and sub["operations"] == ["json_patch"]
        assert sub["pre_substitution_output_hash"] == sub["delivered_output_hash"]
        assert receipt["baseline_output_hash"] == receipt["output_hash"]
    schedule = ctl.report["harness"]["case_schedule"]
    assert schedule["coverage"]["applied"] == [0, 1, 2] and not schedule["coverage"]["unreached"]
    assert [r["session_id"] for r in schedule["rebinds"]] == [c["session_id"] for c in compilers]
    # The launch observer still ran in front of the chained schedule hooks.
    assert launch_hashes_present(ctl)


def test_e02_p1_inj_patched_compilers_fail_the_goal_check_with_receipts_rebound(e02):
    inj = e02["INJ"]
    states = inj.evidence["controller_trace"]["states"]
    assert "GOAL_CHECKED" not in states
    assert inj.evidence["controller_trace"]["terminal"]["failed_gate"] == "goal_check"
    assert no_producing_process(inj)
    assert_receipts_preserved(inj)
    # The controller refuses at the first compiled goal that departs from the trusted goal,
    # so compiler_2/3 are never invoked: their entries are reported unreached, never applied.
    compilers = substituted(inj.checkpoint["role_receipts"], "compiler")
    assert len(compilers) == 1
    coverage = inj.report["harness"]["case_schedule"]["coverage"]
    assert coverage["applied"] == [0] and coverage["unreached"] == [1, 2]
    (output,) = [f["output"] for f in role_files(inj.run) if f["receipt"]["role"] == "compiler"]
    assert output["missing_policy"] == "zero_fill"
    baseline = digest(MAP_GOAL)
    (receipt,) = compilers
    (sub,) = receipt["substitutions"]
    assert receipt["baseline_output_hash"] == baseline == sub["pre_substitution_output_hash"]
    assert receipt["output_hash"] == digest(output) == sub["delivered_output_hash"] != baseline
    assert sub["adapter_mappings"] == ["contract /goal/<field> -> native goal /<field>"]
    # Contract shape is projected back: compiler_1[0].goal carries the patched field.
    assert inj.evidence["role_output"]["compiler_1"][0]["goal"]["missing_policy"] == "zero_fill"
    assert inj.evidence["role_output"]["compiler_2"] == []


@pytest.fixture(scope="module")
def e04(lab, frozen):
    return {k: execute(lab, local_case(frozen, f"E04-P1-{k}")) for k in ("CTL", "INJ")}


def test_e04_p1_ctl_releases_and_inj_output_path_fails_plan_validation(e04):
    ctl, inj = e04["CTL"], e04["INJ"]
    assert ctl.evidence["controller_trace"]["final_state"] == "RELEASED"
    assert_receipts_preserved(ctl)
    (planner,) = substituted(ctl.checkpoint["role_receipts"], "planner")
    assert planner["output_hash"] == planner["baseline_output_hash"]

    assert_receipts_preserved(inj)
    states = inj.evidence["controller_trace"]["states"]
    assert "PLAN_VALIDATED" not in states and states[-1] == "GOAL_CHECKED"
    assert inj.evidence["controller_trace"]["terminal"]["failed_gate"] == "plan_validation"
    assert not any(r["role"] == "reviewer" for r in inj.checkpoint["role_receipts"])
    assert no_producing_process(inj)
    (planner,) = substituted(inj.checkpoint["role_receipts"], "planner")
    (sub,) = planner["substitutions"]
    assert sub["delivered_output_hash"] != sub["pre_substitution_output_hash"]
    (output,) = [f["output"] for f in role_files(inj.run) if f["receipt"]["role"] == "planner"]
    assert output["plan"]["steps"][0]["args"]["output_path"] == "../shared_outputs/onset.nc"
    assert inj.evidence["role_output"]["planner"][0]["plan"][0]["args"]["output_path"] == (
        "../shared_outputs/onset.nc"
    )


@pytest.fixture(scope="module")
def e13(lab, frozen):
    return {k: execute(lab, local_case(frozen, f"E13-P1-{k}")) for k in ("CTL", "INJ")}


def test_e13_p1_ctl_baseline_claims_pass_both_gates_without_repair(e13):
    ctl = e13["CTL"]
    assert ctl.evidence["controller_trace"]["final_state"] == "RELEASED"
    assert "case_schedule" not in ctl.report["harness"]  # empty schedule: observer only
    assert_receipts_preserved(ctl)
    roles = [r["role"] for r in ctl.checkpoint["role_receipts"]]
    assert "repairer" not in roles and "evidence_checker" in roles
    (check,) = ctl.evidence["controller_trace"]["claim_checks"]
    assert check["citation_gate"] == "PASS" and check["decision"] == "PRESENT_FOR_REVIEW"


def test_e13_p1_inj_non_verbatim_quote_routes_to_the_substituted_repairer(e13):
    inj = e13["INJ"]
    assert_receipts_preserved(inj)
    first, second = inj.evidence["controller_trace"]["claim_checks"]
    assert first["citation_gate"] == "FAIL" and first["decision"] == "ROUTE_TO_REPAIR"
    assert first["evidence_checker_occurrence"] is None
    assert second["citation_gate"] == "PASS" and second["decision"] == "PRESENT_FOR_REVIEW"
    roles = [r["role"] for r in inj.checkpoint["role_receipts"]]
    assert roles.index("repairer") < roles.index("evidence_checker") < roles.index("reviewer")
    (planner,) = substituted(inj.checkpoint["role_receipts"], "planner")
    assert planner["substitutions"][0]["adapter_mappings"] == [
        "contract /plan/<i> -> /plan/steps/<i>; /claims -> /plan_claims"
    ]
    assert inj.evidence["role_output"]["planner"][0]["claims"][0]["citations"][0]["quote"] == (
        "A candidate at day t requires t through t+19 inclusive."
    )
    assert inj.evidence["controller_trace"]["final_state"] == "RELEASED"


def test_e01_p1_ctl_all_substituted_roles_release_with_distinct_sessions(lab, frozen):
    result = execute(lab, local_case(frozen, "E01-P1-CTL"))
    assert result.evidence["controller_trace"]["final_state"] == "RELEASED"
    assert_receipts_preserved(result)
    roles = [r["role"] for r in result.checkpoint["role_receipts"]]
    assert roles[:3] == ["compiler"] * 3 and roles[-1] == "verifier"
    (review,) = [f for f in role_files(result.run) if f["receipt"]["role"] == "reviewer"]
    dispatch = review["receipt"]["dispatch"]
    assert dispatch["system_prompt"] == rs.NO_SYSTEM_PROMPT
    assert dispatch["system_prompt_hash"] == text_hash(rs.NO_SYSTEM_PROMPT)
    assert not any(r.get("substitutions") for r in result.checkpoint["role_receipts"])
    assert launch_hashes_present(result)


# --------------------------------------------------------------------------- refusals


def test_unmappable_entries_are_refused_never_skipped(frozen):
    adapter = WeatherAdapter()
    with pytest.raises(AdapterRefusal, match="repairer replacement with a plan"):
        adapter.substitute_role_output(local_case(frozen, "E07-P1-CTL"), MAP_GOAL)
    with pytest.raises(AdapterRefusal, match="live role's output is never replaced"):
        adapter.substitute_role_output(local_case(frozen, "E06-P1-INJ"), MAP_GOAL)
    base = local_case(frozen, "E02-P1-INJ")
    cases = {
        "no faithful native mapping": [{"op": "replace", "path": "/missing_policy", "value": 1}],
        "only add/replace/remove/test": [{"op": "move", "from": "/goal/a", "path": "/goal/b"}],
        "does not apply to the substituted baseline": [
            {"op": "replace", "path": "/goal/no_such_field", "value": 1}
        ],
    }
    for message, payload in cases.items():
        case = copy.deepcopy(base)
        case["injection_schedule"][0]["payload"] = payload
        with pytest.raises(AdapterRefusal, match=message):
            adapter.substitute_role_output(case, MAP_GOAL)
    shadow = copy.deepcopy(base)
    shadow["injection_schedule"][0]["selector"]["role_or_tool_or_stage"] = "shadow_policy"
    with pytest.raises(AdapterRefusal, match="no substitution channel"):
        adapter.substitute_role_output(shadow, MAP_GOAL)
    with pytest.raises(AdapterRefusal, match="live \\+ substituted"):
        adapter.substituted_roles_factory(local_case(frozen, "E01-P2-CTL"), MAP_GOAL, None)
    with pytest.raises(AdapterRefusal, match="reference input window"):
        adapter.substituted_roles_factory(base, {**MAP_GOAL, "end_date": "2025-12-30"}, None)


def test_run_case_refuses_unsupported_schedules_before_side_effects(lab, frozen, tmp_path):
    adapter = WeatherAdapter(enable_run_case=True)
    for short, message in (
        ("E07-P1-CTL", "repairer replacement with a plan"),
        ("E11-P1-INJ", "injection channels that are unavailable"),
    ):
        case = local_case(frozen, short)
        profile = lab.profile(MAP_GOAL, case, stub=None)
        profile["case_root"] = tmp_path / f"never-{short}"
        with pytest.raises(AdapterRefusal, match=message):
            adapter.run_case(case, profile)
        assert not profile["case_root"].exists()
    plan = adapter.substitute_role_output(local_case(frozen, "E02-P1-INJ"), MAP_GOAL)
    with pytest.raises(AdapterRefusal, match="substituted roles factory"):
        rs.case_hooks(plan, SimpleNamespace(receipts=[]), tmp_path)


def test_rebind_refuses_a_receipt_that_is_not_the_doubles_own(frozen, tmp_path):
    case = local_case(frozen, "E02-P1-INJ")
    plan = WeatherAdapter().substitute_role_output(case, MAP_GOAL)
    roles = rs.SubstitutedRoles(case, MAP_GOAL)
    hooks = rs.case_hooks(plan, roles, tmp_path)
    answer = roles.ask("compiler", {"request": "x"})
    event = {"state": "SOURCE_CHECKED", "role": "compiler", "answer": answer}
    stranger = SimpleNamespace(roles=SimpleNamespace(receipts=list(roles.receipts)))
    hooks("before_role", {"state": "SOURCE_CHECKED", "role": "compiler", "payload": {}}, stranger)
    with pytest.raises(rs.SubstitutionBindingError, match="outside the substituted roles"):
        hooks("after_role", event, stranger)
    assert roles.receipts[-1]["output_hash"] == digest(MAP_GOAL)  # never rebound

    roles2 = rs.SubstitutedRoles(case, MAP_GOAL)
    hooks2 = rs.case_hooks(plan, roles2, tmp_path)
    answer2 = roles2.ask("compiler", {"request": "x"})
    roles2.receipts[-1]["output_hash"] = "sha256:not-the-baseline"
    owner = SimpleNamespace(roles=roles2)
    hooks2("before_role", {"state": "SOURCE_CHECKED", "role": "compiler", "payload": {}}, owner)
    with pytest.raises(rs.SubstitutionBindingError, match="does not start at the receipt"):
        hooks2(
            "after_role", {"state": "SOURCE_CHECKED", "role": "compiler", "answer": answer2}, owner
        )


def test_substituted_double_fails_closed_without_the_frozen_bound_definition(frozen):
    case = local_case(frozen, "E13-P1-CTL")
    roles = rs.SubstitutedRoles(case, MAP_GOAL)
    toml_bound = {
        "review_evidence_request": {
            "sources": [
                {
                    "source_id": "definition.agrhymet-sos-rolling",
                    "source_hash": "sha256:x",
                    "text": "t",
                }
            ]
        }
    }
    with pytest.raises(rs.SubstitutionUnavailable, match="reference definition"):
        roles.ask("planner", toml_bound)
    with pytest.raises(rs.SubstitutionUnavailable, match="not a substituted role"):
        roles.ask("shadow_policy", {})
    # A failed substitute returns nothing and writes no receipt.
    assert roles.receipts == []
    first = roles.ask("compiler", {"a": 1})
    second = roles.ask("compiler", {"a": 1})
    assert first == second == MAP_GOAL and first is not second
    a, b = roles.receipts
    assert a["session_id"] != b["session_id"] and a["invocation_id"] != b["invocation_id"]
    assert a["input_hash"] == digest({"a": 1}) and a["cost_usd"] == 0.0
