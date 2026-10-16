"""Per-role answer schemas for live roles answered through a forced tool (structured output).

Why: two live reviewer calls with the same model, temperature 0 and payload class answered in
different FORMATS (bare JSON once; ~6.8 KB of prose with a trailing ```json fence the next time,
despite "Output ONLY JSON"), so the strict output rule (``agents.parse_role_object``) refused
the second as invalid_answer. The parser's strictness is a safety property and stays. Instead
the live Kong transport (``kong_roles.KongRoles``) sends ONE tool whose ``input_schema`` is the
role's answer schema below, with ``tool_choice {"type": "tool", "name": <that tool>}``
(Anthropic Messages API forced tool use), and takes the answer from the ``tool_use`` block's
``input``. That input is serialised with ``answer_text`` and handed to the unchanged parser,
so the controller still parses and validates exactly as before.

Rule for every schema: NEVER STRICTER THAN THE CONTROLLER. A schema only shapes the answer; the
controller's own checks (runner.py, claim_gates.py, policy.py, contracts.py) decide. So each
schema requires only what the controller requires of every answer it accepts for that role and
payload, and leaves open whatever the controller leaves open:

| Role | Controller acceptance (source) | Schema |
|---|---|---|
| compiler | ``contracts.retain_goal``: exactly the goal ``FIELDS`` | object, required FIELDS, no other keys, values untyped |
| planner | a plan (``expected_plan`` shape), the review envelope (``review_evidence.unpack_plan``: schema/plan/plan_claims[/rationale], or plan/claims under claim gates), or ``{needs_info}`` (``contracts.needs_info``) | object, nothing required (the alternatives share no key), named properties typed only where every alternative agrees |
| repairer | ``{plan, response}`` (+ ``plan_claims``, ``schema`` with review evidence) or ``{needs_info[, response]}`` (``Controller.repair``) | as planner |
| reviewer | legacy exact ``{verdict, binding_hash, findings}``; with review evidence also the frozen UC-03 shape (``verdict`` + any of approved_binding / findings / reason / verification_waiver) (``Controller.accept_review_output``) | legacy: all three required, no other keys; review evidence: only ``verdict`` required, the union of both shapes' keys |
| evidence_checker | exactly ``{plan_hash, verdicts[{claim_id, verdict, reasoning}]}`` (``claim_gates.validate_checker_output``) | the same, closed vocabulary |
| verifier | ``{verdict, binding_hash, scope[, sub_reason]}``, verdict PASS/FAIL or the native BLOCK/UNVERIFIABLE (``Controller.record_verifier``) | the same; binding_hash and scope untyped (recorded, then compared) |
| router | exactly ``{question_hash, action, scores}``, scores numbers in 0..1 (``policy.validate``) | the same |
| shadow_policy | an ACCEPTED decision is exactly ``SHADOW_OUTPUT_FIELDS`` with a numeric score in 0..1 (``policy.shadow_rejection``) | the same |
| rubric_grader | report-side only, never the controller: exactly ``{verdict, rationale, evidence_quotes}``, verdict PASS/FAIL/UNDECIDED (``container_harness/rubric_grader.check_answer``) | the same |

Reviewer findings follow the frozen v2.7 contract's record typing (DEV-ONLY.json record_lists:
``role_output.reviewer[*].findings`` = a string, or a record with string ``id``, ``severity``,
``text``; extra keys allowed, no severity vocabulary), which ``agents.PROMPTS["reviewer"]``
states. The reviewer verdict is the frozen closed set APPROVE/REJECT the prompt offers.

The Messages API does not validate a (non-``strict``) tool input against its schema: the schema
guides the model; the controller remains the only judge. Tests pin every schema against the
substituted baseline outputs and the frozen fixture answers (tools/workflow/tests/
test_role_schemas.py), with the controller's own acceptance code as the oracle.

Stdlib only (the container's smoke imports it).
"""

import hashlib
import json

from .claim_gates import VERDICTS as CHECKER_VERDICTS
from .contracts import FIELDS
from .policy import SHADOW_OUTPUT_FIELDS, SHADOW_SCHEMA
from .review_evidence import SCHEMA as REVIEW_ENVELOPE_SCHEMA

STRUCTURED_OUTPUT = "forced_tool"
ANSWER_SOURCE = "tool_use.input"
TOOL_PREFIX = "submit_"
TOOL_SUFFIX = "_answer"
REVIEWER_VERDICTS = ("APPROVE", "REJECT")  # agents.PROMPTS["reviewer"]; frozen closed set
REVIEWER_FINDING_FIELDS = ("id", "severity", "text")
# Copy of runner.VERIFIER_SUB_REASONS (runner is not importable in the container; a parity
# test pins equality).
VERIFIER_SUB_REASONS = ("BLOCK", "UNVERIFIABLE")
VERIFIER_VERDICTS = ("PASS", "FAIL", *VERIFIER_SUB_REASONS)
# Payload keys that select a schema variant (as agents.role_prompt selects prompt suffixes).
VARIANT_KEYS = ("review_evidence", "review_evidence_request")

ANY = {}  # an untyped value: the controller checks it, the schema does not
STRING = {"type": "string"}
OBJECT = {"type": "object"}
ARRAY = {"type": "array"}
UNIT = {"type": "number", "minimum": 0, "maximum": 1}


def _object(properties, required=(), closed=False):
    schema = {"type": "object", "properties": properties}
    if required:
        schema["required"] = list(required)
    if closed:
        schema["additionalProperties"] = False
    return schema


FINDING = {
    "anyOf": [
        STRING,
        _object({k: STRING for k in REVIEWER_FINDING_FIELDS}, REVIEWER_FINDING_FIELDS),
    ]
}
FINDINGS = {"type": "array", "items": FINDING}
FROZEN_ONLY = (
    "Do not use. Only for the native frozen reviewer shape, which has no binding_hash; your "
    "answer is exactly verdict, binding_hash and findings, and adding this key makes it invalid."
)
NEEDS_INFO = {"type": "string", "description": "your question, only when no answer is possible"}


def _reviewer(payload):
    legacy = {
        "verdict": {"type": "string", "enum": list(REVIEWER_VERDICTS)},
        "binding_hash": {"type": "string", "description": "copy the provided identity"},
        "findings": FINDINGS,
    }
    if "review_evidence" not in payload:
        return _object(legacy, sorted(legacy), closed=True)
    # The frozen UC-03 keys stay accepted (never stricter than the controller), but the live
    # prompt asks for exactly the legacy three, and the controller refuses a MIX of the two
    # shapes. Run 38906458002: the model added "reason" beside binding_hash because the schema
    # offered it. Each frozen-only key therefore says so; no top-level anyOf (API limit).
    frozen = {
        "approved_binding": {
            **_object({"plan_hash": STRING, "source_hash": STRING}),
            "description": FROZEN_ONLY,
        },
        "reason": {**STRING, "description": FROZEN_ONLY},
        "verification_waiver": {**OBJECT, "description": FROZEN_ONLY},
    }
    return _object({**legacy, **frozen}, ["verdict"], closed=True)


def _planner(payload):
    if "review_evidence_request" in payload:
        properties = {
            "schema": {"type": "string", "enum": [REVIEW_ENVELOPE_SCHEMA]},
            "plan": OBJECT,
            "plan_claims": ARRAY,
            "claims": ARRAY,
            "rationale": STRING,
        }
    else:  # the bare typed plan (contracts.expected_plan)
        properties = {"goal_hash": STRING, "steps": ARRAY, "final": ARRAY}
    return _object({**properties, "needs_info": NEEDS_INFO})


def _repairer(payload):
    properties = {"plan": OBJECT, "response": STRING}
    if "review_evidence_request" in payload:
        properties.update(
            schema={"type": "string", "enum": [REVIEW_ENVELOPE_SCHEMA]}, plan_claims=ARRAY
        )
    return _object({**properties, "needs_info": NEEDS_INFO})


def _evidence_checker(payload):
    verdict = _object(
        {
            "claim_id": STRING,
            "verdict": {"type": "string", "enum": list(CHECKER_VERDICTS)},
            "reasoning": STRING,
        },
        ("claim_id", "verdict", "reasoning"),
        closed=True,
    )
    return _object(
        {"plan_hash": STRING, "verdicts": {"type": "array", "items": verdict}},
        ("plan_hash", "verdicts"),
        closed=True,
    )


def _verifier(payload):
    return _object(
        {
            "verdict": {"type": "string", "enum": list(VERIFIER_VERDICTS)},
            "sub_reason": {"enum": [*VERIFIER_SUB_REASONS, None]},
            "binding_hash": ANY,
            "scope": ANY,
        },
        ("verdict", "binding_hash", "scope"),
        closed=True,
    )


def _router(payload):
    return _object(
        {
            "question_hash": STRING,
            "action": STRING,
            "scores": {"type": "object", "additionalProperties": UNIT},
        },
        ("question_hash", "action", "scores"),
        closed=True,
    )


def _shadow_policy(payload):
    properties = {
        "schema": {"type": "string", "enum": [SHADOW_SCHEMA]},
        "state": STRING,
        "question_fingerprint": STRING,
        "action": STRING,
        "score": UNIT,
    }
    if set(properties) != SHADOW_OUTPUT_FIELDS:  # pragma: no cover - drift guard
        raise RuntimeError("shadow answer schema drifted from policy.SHADOW_OUTPUT_FIELDS")
    return _object(properties, sorted(properties), closed=True)


GRADER_VERDICTS = ("PASS", "FAIL", "UNDECIDED")


def _rubric_grader(payload):
    # Report-side evaluator (container_harness/rubric_grader.py), not a controller role. Its
    # acceptance is rubric_grader.check_answer, which uses exactly this schema, so the
    # "never stricter than the controller" rule holds by construction.
    return _object(
        {
            "verdict": {"type": "string", "enum": list(GRADER_VERDICTS)},
            "rationale": STRING,
            "evidence_quotes": {
                "type": "array",
                "items": STRING,
                "description": "verbatim excerpts of graded_output, copied exactly",
            },
        },
        ("verdict", "rationale", "evidence_quotes"),
        closed=True,
    )


SCHEMAS = {
    "compiler": lambda payload: _object({k: ANY for k in sorted(FIELDS)}, sorted(FIELDS), True),
    "planner": _planner,
    "repairer": _repairer,
    "reviewer": _reviewer,
    "evidence_checker": _evidence_checker,
    "verifier": _verifier,
    "router": _router,
    "shadow_policy": _shadow_policy,
    "rubric_grader": _rubric_grader,
}


def answer_schema(role, payload):
    """The answer JSON schema for one invocation; KeyError for a role without one."""
    return SCHEMAS[role](payload if isinstance(payload, dict) else {})


def tool_name(role):
    return TOOL_PREFIX + role + TOOL_SUFFIX


def answer_tool(role, payload):
    """The single tool definition sent for one invocation (Messages API ``tools[0]``)."""
    return {
        "name": tool_name(role),
        "description": f"Submit your complete {role} answer. The tool input IS the answer: the "
        "JSON object your instructions describe, and nothing else.",
        "input_schema": answer_schema(role, payload),
    }


def tool_choice(tool):
    """Forced use of exactly that tool."""
    return {"type": "tool", "name": tool["name"]}


def tool_hash(tool):
    return "sha256:" + hashlib.sha256(json.dumps(tool, sort_keys=True).encode()).hexdigest()


def answer_text(value):
    """The exact text handed to the controller's parser for a tool answer: ``contracts.digest``'s
    own serialisation, so the receipt's ``output_hash`` (digest of the parsed answer) is the
    sha256 of this very text."""
    return json.dumps(value, sort_keys=True, allow_nan=False)


def request_overhead_bytes(role):
    """Upper bound of the bytes ``tools`` + ``tool_choice`` add to a request body for ``role``
    (the largest schema variant), for the relay's worst-case input bound."""
    variants = [{}, {k: {} for k in VARIANT_KEYS}]
    return max(
        len(json.dumps({"tools": [t], "tool_choice": tool_choice(t)}).encode("utf-8"))
        for t in (answer_tool(role, v) for v in variants)
    )


# --------------------------------------------------------------------------- validation
# The JSON Schema subset these schemas use (type, enum, properties, required,
# additionalProperties, items, anyOf, minimum, maximum). Tests use it to prove a schema accepts
# every answer the controller accepts; the transport never validates with it.

_TYPES = {
    "object": lambda v: isinstance(v, dict),
    "array": lambda v: isinstance(v, list),
    "string": lambda v: isinstance(v, str),
    "number": lambda v: type(v) in (int, float),
    "boolean": lambda v: type(v) is bool,
    "null": lambda v: v is None,
}


def schema_errors(schema, value, path="$"):
    """Errors of ``value`` against ``schema`` (subset above); [] when it conforms."""
    if "anyOf" in schema:
        if not any(not schema_errors(s, value, path) for s in schema["anyOf"]):
            return [f"{path}: matches no anyOf branch"]
        return []
    kind = schema.get("type")
    if kind is not None and not _TYPES[kind](value):
        return [f"{path}: not {kind}"]
    if "enum" in schema and value not in schema["enum"]:
        return [f"{path}: {value!r} not in {schema['enum']!r}"]
    errors = []
    if kind == "number":
        if "minimum" in schema and value < schema["minimum"]:
            errors.append(f"{path}: below {schema['minimum']}")
        if "maximum" in schema and value > schema["maximum"]:
            errors.append(f"{path}: above {schema['maximum']}")
    if kind == "object":
        properties = schema.get("properties", {})
        errors += [f"{path}: missing {k}" for k in schema.get("required", []) if k not in value]
        extra = schema.get("additionalProperties", True)
        for key, item in value.items():
            if key in properties:
                errors += schema_errors(properties[key], item, f"{path}.{key}")
            elif extra is False:
                errors.append(f"{path}: unexpected key {key}")
            elif isinstance(extra, dict):
                errors += schema_errors(extra, item, f"{path}.{key}")
    if kind == "array" and "items" in schema:
        for k, item in enumerate(value):
            errors += schema_errors(schema["items"], item, f"{path}[{k}]")
    return errors
