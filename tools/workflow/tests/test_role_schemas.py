"""The forced-tool answer schemas are never stricter than the controller.

Property, per role: every answer the controller's OWN acceptance code accepts (run as the
oracle, with a permissive context so only the answer's shape decides) conforms to the role's
schema. Checked over the substituted baseline outputs (adapter_support.role_substitution) and
every frozen v2.7 fixture answer (DEV-ONLY.json probe-bundle role outputs and after_role /
after_transition replace_role_output payloads, mapped to the native shape exactly as the
substitution adapter maps them). Counts are asserted so the property is never vacuous.
"""

import copy
import hashlib
import json
import sys
from types import SimpleNamespace
from unittest import mock

import pytest
from frozen_v27 import frozen_root

SIMKIT_ROOT = frozen_root()  # verified against FINAL-PINS by tests/frozen_v27.py
sys.path.insert(0, str(SIMKIT_ROOT))  # adapter_support imports the pinned simkit package

from adapter_support import role_substitution as rs  # noqa: E402
from adapter_support.weather_adapter import AdapterRefusal  # noqa: E402
from tools.workflow import (  # noqa: E402
    agents,
    claim_gates,
    policy,
    review_evidence,
    role_schemas,
    runner,
)
from tools.workflow.contracts import (  # noqa: E402
    Refused,
    digest,
    expected_plan,
    keys,
    needs_info,
    validate_goal,
    validate_plan,
)
from tools.workflow.role_schemas import (  # noqa: E402
    FROZEN_ONLY,
    answer_schema,
    answer_tool,
    schema_errors,
)

CASES = "casesets/cached-onset-e2e-v2_7/frozen/agentic-e2e-injections.DEV-ONLY.json"
# The frozen reference goal (profile cached-onset/v1), as tests/test_evidence_projection_v27.
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
EVIDENCE = {"review_evidence": {}}
REQUEST = {"review_evidence_request": {}}
LEGACY = {}


@pytest.fixture(scope="module")
def frozen():
    return json.loads((frozen_root() / CASES).read_text(encoding="utf-8"))


# --------------------------------------------------------------------------- oracles
# Each returns True when the controller accepts the answer's shape. Context the answer cannot
# know (the run's binding, plan hash, admissible actions, claim ids) is taken from the answer
# itself, so only shape and vocabulary decide.


def _accepts(call):
    try:
        call()
    except (Refused, KeyError, TypeError, AttributeError, ValueError):
        return False
    return True


def reviewer_accepts(review, mode):
    binding = review.get("binding_hash") if isinstance(review, dict) else None
    controller = SimpleNamespace(binding=binding, context=dict(mode))
    approved = review.get("approved_binding") if isinstance(review, dict) else None
    presented = dict(approved) if isinstance(approved, dict) else {}
    with mock.patch.object(runner.review_evidence, "presented_binding", lambda c: presented):
        return _accepts(lambda: runner.Controller.accept_review_output(controller, review))


def verifier_accepts(value):
    controller = SimpleNamespace(context={}, checkpoint=lambda: None)
    return _accepts(lambda: runner.Controller.record_verifier(controller, value, "b"))


def checker_accepts(output):
    def call():
        ids = {v["claim_id"] for v in output["verdicts"]}
        claim_gates.validate_checker_output(output, output["plan_hash"], [], ids)

    return _accepts(call)


def shadow_accepts(answer):
    if not isinstance(answer, dict):
        return False
    question = {
        "state": answer.get("state"),
        "question_hash": answer.get("question_fingerprint"),
        "admissible_actions": [answer.get("action")],
    }
    return policy.shadow_rejection(answer, question) is None


def router_accepts(answer):
    def call():
        scores = answer["scores"] if isinstance(answer.get("scores"), dict) else {}
        prompt = {
            "question_hash": answer["question_hash"],
            "admissible_actions": [answer["action"], *scores],
        }
        policy.validate(answer, prompt)

    return _accepts(call)


def compiler_accepts(goal):
    return _accepts(lambda: validate_goal(goal))


def planner_accepts(value, mode):
    def call():
        if needs_info(value, "planner") is not None:
            return
        if "review_evidence_request" in mode:
            review_evidence.unpack_plan(value, GOAL, [], claim_gates=True)
        else:
            validate_plan(value, GOAL)

    return _accepts(call)


def repairer_accepts(value, mode):
    def call():
        if needs_info(value, "repairer", allowed=("response",)) is not None:
            return
        fields = {"plan", "response"}
        if "review_evidence_request" in mode:
            fields |= {"plan_claims", "schema"}
        keys(value, fields, "repair")
        if not isinstance(value["response"], str) or len(value["response"]) > 4000:
            raise Refused("invalid bounded repair response")
        validate_plan(value["plan"], GOAL)

    return _accepts(call)


def check(role, mode, answers, oracle):
    """Assert the property over ``answers``; returns how many the controller accepted."""
    schema = answer_schema(role, mode)
    accepted = 0
    for answer in answers:
        if oracle(answer):
            accepted += 1
            assert schema_errors(schema, answer) == [], (role, answer)
    return accepted


# --------------------------------------------------------------------------- fixture answers


def role_outputs(frozen, role):
    for case in frozen["cases"]:
        for bundle in case.get("probe_bundles") or []:
            outputs = ((bundle.get("evidence") or {}).get("role_output") or {}).get(role) or []
            yield from copy.deepcopy(outputs if isinstance(outputs, list) else [outputs])


def replacements(frozen, selector):
    for case in frozen["cases"]:
        for spec in case.get("injection_schedule") or []:
            if (
                spec.get("operation") == "replace_role_output"
                and (spec.get("selector") or {}).get("role_or_tool_or_stage") == selector
            ):
                yield case, copy.deepcopy(spec["payload"])


def native_replacements(frozen, role):
    """after_role replacements in the native shape the substitution adapter delivers."""
    for case in frozen["cases"]:
        try:
            plan = rs.substitution_plan(case, goal=GOAL)
        except AdapterRefusal:
            continue
        schedule = plan["native_case"]["injection_schedule"]  # one entry per frozen spec
        for entry in plan["entries"]:
            if entry["native_role"] == role and entry["operation"] == "replace_role_output":
                yield copy.deepcopy(schedule[entry["index"]]["payload"])


# --------------------------------------------------------------------------- tests


def test_every_prompt_role_has_a_schema_and_a_forced_tool():
    assert set(role_schemas.SCHEMAS) == set(agents.PROMPTS)
    for role in agents.PROMPTS:
        for mode in (LEGACY, EVIDENCE, REQUEST):
            tool = answer_tool(role, mode)
            assert tool["input_schema"]["type"] == "object"  # the API's top-level requirement
            assert not {"anyOf", "oneOf", "allOf"} & set(tool["input_schema"])
            assert role_schemas.tool_choice(tool) == {"type": "tool", "name": tool["name"]}
            assert tool["name"] == f"submit_{role}_answer" and len(tool["name"]) <= 64
            assert role_schemas.request_overhead_bytes(role) >= len(json.dumps(tool))


def test_copied_vocabularies_match_their_sources():
    assert role_schemas.VERIFIER_SUB_REASONS == runner.VERIFIER_SUB_REASONS
    assert role_schemas.CHECKER_VERDICTS == claim_gates.VERDICTS
    assert set(answer_schema("shadow_policy", {})["properties"]) == policy.SHADOW_OUTPUT_FIELDS


def test_answer_text_is_the_digest_serialisation():
    value = {"verdict": "PASS", "scope": "data-replay", "binding_hash": "h", "n": 0.5}
    text = role_schemas.answer_text(value)
    assert agents.parse_role_object(text) == (value, "bare")

    assert hashlib.sha256(text.encode()).hexdigest() == digest(value)
    with pytest.raises(ValueError):
        role_schemas.answer_text({"score": float("nan")})


def test_substituted_baselines_conform():
    roles = rs.SubstitutedRoles.__new__(rs.SubstitutedRoles)
    roles.goal, roles.pair_id, roles.shadow = copy.deepcopy(GOAL), "E13-P1", None
    compiler = rs.static_baseline("compiler", GOAL)
    assert (
        compiler_accepts(compiler) and schema_errors(answer_schema("compiler", {}), compiler) == []
    )
    for role, oracle in (("planner", planner_accepts), ("repairer", repairer_accepts)):
        value = rs.static_baseline(role, GOAL, "E13-P1")
        assert oracle(value, REQUEST) and schema_errors(answer_schema(role, REQUEST), value) == []
    review = roles.baseline("reviewer", {"review_evidence": {"plan_hash": "p", "source_hash": "s"}})
    assert reviewer_accepts(review, EVIDENCE)
    assert schema_errors(answer_schema("reviewer", EVIDENCE), review) == []
    checker = roles.baseline("evidence_checker", {"plan_hash": "p", "claims": rs.frozen_claims()})
    assert checker_accepts(checker)
    assert schema_errors(answer_schema("evidence_checker", {}), checker) == []
    verdict = roles.baseline("verifier", {"binding_hash": "b"})
    assert verifier_accepts(verdict) and schema_errors(answer_schema("verifier", {}), verdict) == []
    plan = expected_plan(GOAL)
    assert planner_accepts(plan, LEGACY) and schema_errors(answer_schema("planner", {}), plan) == []


def test_reviewer_schema_accepts_every_frozen_reviewer_answer(frozen):
    # The frozen reviewer shape is the native one (role_substitution.REVIEWER_KEYS), so every
    # replacement payload counts, whether or not its case substitutes the reviewer.
    answers = [*role_outputs(frozen, "reviewer"), *(p for _, p in replacements(frozen, "reviewer"))]
    accepted = check("reviewer", EVIDENCE, answers, lambda a: reviewer_accepts(a, EVIDENCE))
    assert accepted >= 2000  # 2,048 probe-bundle outputs + 14 replacements (most accepted)


def test_reviewer_evidence_schema_warns_off_the_frozen_only_keys():
    """Run 38906458002: offered the union, the live model mixed the shapes (legacy + reason),
    which the controller refuses. The frozen-only keys stay accepted but each says not to use."""
    mixed = {"verdict": "APPROVE", "binding_hash": "b", "findings": [], "reason": "r"}
    assert not reviewer_accepts(mixed, EVIDENCE)
    properties = answer_schema("reviewer", EVIDENCE)["properties"]
    frozen_only = set(properties) - {"verdict", "binding_hash", "findings"}
    assert frozen_only == {"approved_binding", "reason", "verification_waiver"}
    for key in frozen_only:
        assert properties[key]["description"] == FROZEN_ONLY, key


def test_reviewer_legacy_schema_is_the_exact_legacy_shape():
    schema = answer_schema("reviewer", LEGACY)
    good = {
        "verdict": "REJECT",
        "binding_hash": "b",
        "findings": ["x", {"id": "R1", "severity": "blocking", "text": "t", "extra": 1}],
    }
    assert reviewer_accepts(good, LEGACY) and schema_errors(schema, good) == []
    for bad in ({"verdict": "APPROVE", "findings": []}, {**good, "reason": "r"}):
        assert not reviewer_accepts(bad, LEGACY) and schema_errors(schema, bad)
    # The frozen finding record typing: a record must carry id, severity and text as strings.
    assert schema_errors(schema, {**good, "findings": [{"id": "R1", "description": "d"}]})


def test_evidence_checker_schema_accepts_every_frozen_checker_answer(frozen):
    accepted = check(
        "evidence_checker", {}, role_outputs(frozen, "evidence_checker"), checker_accepts
    )
    assert accepted >= 1000


def test_shadow_schema_accepts_every_accepted_frozen_decision(frozen):
    answers = [*role_outputs(frozen, "shadow_policy")]
    for state in policy.SHADOW_STATES:
        answers += [payload for _, payload in replacements(frozen, state)]
    accepted = check("shadow_policy", {}, answers, shadow_accepts)
    assert accepted >= 100
    # The frozen E09 injections the controller records as NOT accepted stay outside it too.
    for bad in ({"score": "NaN"}, {"score": 1.5}):
        answer = {
            "schema": "workflow.decision/1",
            "state": "S",
            "question_fingerprint": "q",
            "action": "A",
            **bad,
        }
        assert not shadow_accepts(answer) and schema_errors(
            answer_schema("shadow_policy", {}), answer
        )


def test_compiler_schema_accepts_every_frozen_compiled_goal(frozen):
    goals = []
    for selector in rs.COMPILER_SELECTORS:
        goals += [o["goal"] for o in role_outputs(frozen, selector) if isinstance(o, dict)]
    assert check("compiler", {}, goals, compiler_accepts) >= 1000


@pytest.mark.parametrize("mode", [LEGACY, REQUEST], ids=["bare", "review_evidence"])
def test_planner_and_repairer_schemas_accept_every_frozen_answer(frozen, mode):
    planner = [*role_outputs(frozen, "planner"), *native_replacements(frozen, "planner")]
    repairer = [*role_outputs(frozen, "repairer"), *native_replacements(frozen, "repairer")]
    accepted_planner = check("planner", mode, planner, lambda a: planner_accepts(a, mode))
    accepted_repairer = check("repairer", mode, repairer, lambda a: repairer_accepts(a, mode))
    assert (
        accepted_planner >= 1
    )  # a frozen planner needs_info answer (the other asks an empty question)
    assert accepted_repairer >= 39  # the frozen repairer needs_info answers


def test_router_and_verifier_schemas_follow_their_validators():
    router = {"question_hash": "q", "action": "EXECUTE", "scores": {"EXECUTE": 1, "STOP": 0.0}}
    assert router_accepts(router) and schema_errors(answer_schema("router", {}), router) == []
    over = {**router, "scores": {"EXECUTE": 1.5}}
    assert not router_accepts(over) and schema_errors(answer_schema("router", {}), over)
    for value in (
        {"verdict": "PASS", "binding_hash": "b", "scope": "data-replay"},
        {"verdict": "FAIL", "binding_hash": "b", "scope": "data-replay", "sub_reason": "BLOCK"},
        {"verdict": "UNVERIFIABLE", "binding_hash": None, "scope": 3, "sub_reason": None},
    ):
        assert verifier_accepts(value) and schema_errors(answer_schema("verifier", {}), value) == []
    for value in ({"verdict": "PASS"}, {"verdict": "OK", "binding_hash": "b", "scope": "s"}):
        assert not verifier_accepts(value) and schema_errors(answer_schema("verifier", {}), value)


def test_claim_schema_names_the_controller_citation_fields():
    """Run 38922536742: with plan_claims untyped the live planner and repairer wrote
    "verbatim_quote"; both controller validators refuse it, and the schema now does too."""
    good = {"source_id": "s", "source_hash": "h", "quote": "q"}
    claim = {"claim_id": "c1", "text": "t", "citations": [good], "derivable": False}
    wrong = {**claim, "citations": [{"source_id": "s", "source_hash": "h", "verbatim_quote": "q"}]}
    with pytest.raises(Refused, match=r"review citation: expected exactly"):
        claim_gates.validate_claim_shape([wrong])
    for role in ("planner", "repairer"):
        schema = answer_schema(role, REQUEST)
        assert schema_errors(schema, {"plan_claims": [claim]}) == []
        assert schema_errors(schema, {"plan_claims": [{**claim, "computes": "x"}]}) == []
        assert schema_errors(schema, {"plan_claims": [wrong]}), role
        assert schema_errors(schema, {"plan_claims": [{**claim, "derivable": "no"}]}), role
    assert schema_errors(answer_schema("planner", REQUEST), {"claims": [wrong]})
