"""Strict handoff contracts; model output never supplies authority or file paths."""

import hashlib
import json
import math
from contextlib import contextmanager
from datetime import date, timedelta

from .profiles import PROFILES

# Frozen v2.7 failed_gate vocabulary (EVIDENCE_CONTRACT.json structures.failed_gate_vocabulary),
# in declared order. CAP-REASON-MAP: every controller refusal carries exactly one of these.
FAILED_GATES = (
    "source_check",
    "goal_check",
    "plan_validation",
    "review",
    "authorization",
    "review_binding",
    "gate_evidence",
    "replay_evidence",
    "verifier",
    "transition",
    "write_denial",
    "resource_guard",
    "budget",
    "repair_exhausted",
    "resume_verification",
    "pending_operation",
    "interrupted",
    "claim_evidence",
)
TERMINAL_OUTCOMES = ("RELEASED", "BLOCKED", "NEEDS_INFO", "INCOMPLETE")
# Gates whose stop leaves the run unfinished rather than decided (as in the frozen terminals).
INCOMPLETE_GATES = frozenset({"interrupted", "pending_operation", "resource_guard"})
# Default terminal reason per gate: the frozen reason token where one is the generic reading.
GATE_REASONS = {
    "goal_check": "goal_check_failed",
    "plan_validation": "plan_validation_failed",
    "repair_exhausted": "repair_exhausted",
    "verifier": "verifier_fail",
    "resume_verification": "resume_refused",
    "pending_operation": "pending_operation_uncertain",
    "write_denial": "write_denied",
    "transition": "transition_rejected",
    "claim_evidence": "claim_evidence_rejected",
    "interrupted": "interrupted",
    "budget": "budget_exhausted",
}


class Refused(ValueError):
    """A controller refusal. ``gate`` is the frozen failed_gate code (CAP-REASON-MAP).

    ``gate=None`` survives only in modules outside the controller's declared set; the
    controller tags those at its call site (``gated``) and records that it did so.
    """

    def __init__(self, message="", gate=None, *, reason=None, outcome=None):
        super().__init__(message)
        if gate is not None and gate not in FAILED_GATES:
            raise ValueError(f"undeclared failed_gate: {gate!r}")
        if outcome is not None and outcome not in TERMINAL_OUTCOMES[1:]:
            raise ValueError(f"undeclared refusal outcome: {outcome!r}")
        self.gate = gate
        self.reason = reason
        self.outcome = outcome
        self.gate_source = "raise_site" if gate is not None else None


class NeedsInfo(Exception):  # noqa: N818 - a declared stop, not an error
    """A role asked for a human answer in the frozen needs_info shape: an explicit stop."""

    def __init__(self, role, question, gate):
        super().__init__(f"{role} needs information: {question}")
        if gate not in FAILED_GATES:
            raise ValueError(f"undeclared failed_gate: {gate!r}")
        self.role, self.question, self.gate = role, question, gate


def tag(exc, gate, source="call_site", override=False):
    """Give an untagged refusal the gate of the controller call site that observed it."""
    if gate not in FAILED_GATES:
        raise ValueError(f"undeclared failed_gate: {gate!r}")
    if isinstance(exc, Refused) and (exc.gate is None or override):
        if override and exc.gate is not None and exc.gate != gate:
            exc.superseded_gate = exc.gate
        exc.gate, exc.gate_source = gate, source
    return exc


@contextmanager
def gated(gate, override=False):
    try:
        yield
    except Refused as exc:
        tag(exc, gate, "override" if override else "call_site", override)
        raise


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, allow_nan=False).encode()).hexdigest()


def plan_version_hash(plan, claims=None):
    """The controller-assigned identity of one plan version (EC v2_5.plan_symbols).

    A plan version is the plan together with the claims envelope it was emitted with, so a
    claims-only repair is a new version with its own hash (claim_checks are one per plan
    version). Without review evidence a plan carries no claims and its version hash is the
    plan's own digest. Every writer and reader of a plan_hash uses this one function.
    """
    if claims is None:
        return digest(plan)
    return digest({"plan": plan, "plan_claims": claims})


FIELDS = {
    "task",
    "bbox",
    "start_date",
    "end_date",
    "dataset",
    "variable",
    "definition_ref",
    "missing_policy",
    "baseline",
    "ensemble_claim",
    "title",
}


def keys(obj, expected, label, gate=None):
    # gate is forwarded from the caller; an untagged call is tagged at the controller call site.
    if not isinstance(obj, dict) or set(obj) != set(expected):
        raise Refused(f"{label}: expected exactly {sorted(expected)}", gate=gate)


def validate_goal(goal):
    keys(goal, FIELDS, "goal", gate="goal_check")
    fixed = {
        "dataset": "chirps",
        "variable": "precip",
        "definition_ref": "agrhymet-sos-rolling",
        "missing_policy": "preserve_nan",
        "baseline": None,
        "ensemble_claim": False,
    }
    if goal["task"] not in PROFILES or any(
        type(goal[k]) is not type(v) or goal[k] != v for k, v in fixed.items()
    ):
        raise Refused("unsupported scientific scope for cached-onset/v1", gate="goal_check")
    box = goal["bbox"]
    if (
        not isinstance(box, list)
        or len(box) != 4
        or any(type(v) not in (int, float) or not math.isfinite(v) for v in box)
    ):
        raise Refused("bbox must be four finite numbers in N/W/S/E order", gate="goal_check")
    n, w, s, e = box
    if not (-90 <= s < n <= 90 and -180 <= w < e <= 180):
        raise Refused("bbox outside supported non-wrapping bounds", gate="goal_check")
    try:
        start, end = date.fromisoformat(goal["start_date"]), date.fromisoformat(goal["end_date"])
    except (ValueError, TypeError) as exc:
        raise Refused("absolute ISO dates required", gate="goal_check") from exc
    if (end - start).days < 29:
        raise Refused("definition requires at least 30 daily observations", gate="goal_check")
    title = goal["title"]
    if title is None and goal["task"] == "onset_data":
        # Ruled UC-04 (CR-02): the data profile renders no figure; its reference goal has a
        # null title. The map profile still requires a bounded plain title.
        return goal
    if not isinstance(title, str) or not 1 <= len(title) <= 100:
        raise Refused("bounded plain title required", gate="goal_check")
    return goal


def request_goal(request):
    """Validate an operator-supplied source ledger, not a model-authored authorization."""
    ledger = "authorization"  # the operator source ledger is what authorizes this exact goal
    keys(request, {"schema", "messages", "constraints", "authorization"}, "request", gate=ledger)
    if request["schema"] != "workflow.request/1":
        raise Refused("unsupported request schema", gate="authorization")
    messages = {}
    for message in request["messages"]:
        keys(message, {"id", "speaker", "text"}, "source message", gate=ledger)
        if message["id"] in messages or message["speaker"] not in {"user", "assistant", "rule"}:
            raise Refused("duplicate message or invalid source attribution", gate="authorization")
        if not isinstance(message["text"], str) or not message["text"].strip():
            raise Refused("source text is required", gate="authorization")
        messages[message["id"]] = message

    def source(record):
        message = messages.get(record["message_id"])
        quote = record["quote"]
        if not message or not isinstance(quote, str) or not quote or quote not in message["text"]:
            raise Refused(
                "source quote does not occur in the attributed message", gate="authorization"
            )
        return message

    keys(request["constraints"], FIELDS, "constraint ledger", gate=ledger)
    goal = {}
    for field, constraint in request["constraints"].items():
        keys(constraint, {"value", "message_id", "quote"}, f"constraint {field}", gate=ledger)
        if not (
            constraint["value"] is None
            and constraint["message_id"] is None
            and constraint["quote"] is None
        ):
            # CR-02: an absent/null field has nothing to quote, so it is seeded as explicitly
            # unattributed (value, message_id and quote all null). Any attribution that IS
            # supplied must still verify; a non-null value always needs its quote.
            source(constraint)
        goal[field] = constraint["value"]
    validate_goal(goal)
    auth = request["authorization"]
    keys(auth, {"message_id", "quote", "goal_hash", "actions"}, "authorization", gate=ledger)
    if source(auth)["speaker"] != "user":
        raise Refused("only an actual user source can authorize execution", gate="authorization")
    if auth["goal_hash"] != digest(goal) or auth["actions"] != [PROFILES[goal["task"]]["action"]]:
        raise Refused(
            "authorization is absent or does not cover this exact goal", gate="authorization"
        )
    return goal


def retain_goal(candidate, trusted_goal):
    validate_goal(candidate)
    if candidate != trusted_goal:
        lost = [k for k in FIELDS if candidate[k] != trusted_goal[k]]
        raise Refused(
            f"compiled goal changes source constraints: {sorted(lost)}", gate="goal_check"
        )


def expected_plan(goal):
    end = (date.fromisoformat(goal["end_date"]) - timedelta(days=29)).isoformat()
    plan = {
        "goal_hash": digest(goal),
        "steps": [
            {
                "id": "onset",
                "skill": "onset-date",
                "input": "source",
                "output": "onset",
                "args": {"definition_ref": goal["definition_ref"], "variable": goal["variable"]},
            },
            {
                "id": "map",
                "skill": "plot-onset",
                "input": "onset",
                "output": "map",
                "args": {"start_date": goal["start_date"], "end_date": end, "title": goal["title"]},
            },
        ],
        "final": ["onset", "map"],
    }
    if goal["task"] == "onset_data":
        plan["steps"] = plan["steps"][:1]
        plan["final"] = ["onset"]
    return plan


def validate_plan(plan, goal):
    # This first profile deliberately has two registered producing steps. New scientific
    # workflows require a reviewed profile, not model-generated extensions to authority.
    if plan != expected_plan(goal):
        raise Refused(
            "plan changes constraints, tool arguments, order or declared final artifacts",
            gate="plan_validation",
        )
    return plan


def plan_failed_fields(plan, goal):
    """Dotted paths at which a plan departs from its profile (empty when it matches)."""
    expected = expected_plan(goal)
    if not isinstance(plan, dict):
        return ["plan"]
    failed = sorted(f"plan.{k}" for k in set(plan) ^ set(expected))
    for key in sorted(set(plan) & set(expected)):
        if key != "steps" and plan[key] != expected[key]:
            failed.append(f"plan.{key}")
    steps, wanted = plan.get("steps"), expected["steps"]
    if "steps" in plan and not isinstance(steps, list):
        failed.append("plan.steps")
    elif isinstance(steps, list):
        if len(steps) != len(wanted):
            failed.append("plan.steps.length")
        for index, step in enumerate(steps[: len(wanted)]):
            target = wanted[index]
            if not isinstance(step, dict):
                failed.append(f"plan.steps[{index}]")
                continue
            missing = object()
            for field in sorted(set(step) | set(target)):
                value, wanted_value = step.get(field, missing), target.get(field, missing)
                if value == wanted_value:
                    continue
                if field == "args" and isinstance(value, dict) and isinstance(wanted_value, dict):
                    # Name each offending argument, not only the args map that holds it.
                    failed += [
                        f"plan.steps[{index}].args.{name}"
                        for name in sorted(set(value) | set(wanted_value), key=str)
                        if value.get(name, missing) != wanted_value.get(name, missing)
                    ]
                else:
                    failed.append(f"plan.steps[{index}].{field}")
    return failed


def needs_info(value, role, allowed=()):
    """Return the question when ``value`` is the frozen needs_info shape, else None.

    Frozen DEV-ONLY role output shapes: planner ``{needs_info: str}`` and repairer
    ``{needs_info: str, response?: str}``; a needs_info output carries no plan. Any other
    object that includes needs_info is malformed and refused, never partially accepted.
    """
    if not isinstance(value, dict) or "needs_info" not in value:
        return None
    question = value["needs_info"]
    extra = set(value) - {"needs_info", *allowed}
    if (
        extra
        or not isinstance(question, str)
        or not question.strip()
        or len(question) > 4000
        or any(
            not isinstance(value[f], str) or len(value[f]) > 4000 for f in set(value) & set(allowed)
        )
    ):
        raise Refused(f"malformed {role} needs_info output", gate="plan_validation")
    return question
