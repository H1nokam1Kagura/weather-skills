"""Strict handoff contracts; model output never supplies authority or file paths."""

import hashlib
import json
import math
from datetime import date, timedelta

from .profiles import PROFILES


class Refused(ValueError):
    pass


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, allow_nan=False).encode()).hexdigest()


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


def keys(obj, expected, label):
    if not isinstance(obj, dict) or set(obj) != set(expected):
        raise Refused(f"{label}: expected exactly {sorted(expected)}")


def validate_goal(goal):
    keys(goal, FIELDS, "goal")
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
        raise Refused("unsupported scientific scope for cached-onset/v1")
    box = goal["bbox"]
    if (
        not isinstance(box, list)
        or len(box) != 4
        or any(type(v) not in (int, float) or not math.isfinite(v) for v in box)
    ):
        raise Refused("bbox must be four finite numbers in N/W/S/E order")
    n, w, s, e = box
    if not (-90 <= s < n <= 90 and -180 <= w < e <= 180):
        raise Refused("bbox outside supported non-wrapping bounds")
    try:
        start, end = date.fromisoformat(goal["start_date"]), date.fromisoformat(goal["end_date"])
    except (ValueError, TypeError) as exc:
        raise Refused("absolute ISO dates required") from exc
    if (end - start).days < 29:
        raise Refused("definition requires at least 30 daily observations")
    if not isinstance(goal["title"], str) or not 1 <= len(goal["title"]) <= 100:
        raise Refused("bounded plain title required")
    return goal


def request_goal(request):
    """Validate an operator-supplied source ledger, not a model-authored authorization."""
    keys(request, {"schema", "messages", "constraints", "authorization"}, "request")
    if request["schema"] != "workflow.request/1":
        raise Refused("unsupported request schema")
    messages = {}
    for message in request["messages"]:
        keys(message, {"id", "speaker", "text"}, "source message")
        if message["id"] in messages or message["speaker"] not in {"user", "assistant", "rule"}:
            raise Refused("duplicate message or invalid source attribution")
        if not isinstance(message["text"], str) or not message["text"].strip():
            raise Refused("source text is required")
        messages[message["id"]] = message

    def source(record):
        message = messages.get(record["message_id"])
        quote = record["quote"]
        if not message or not isinstance(quote, str) or not quote or quote not in message["text"]:
            raise Refused("source quote does not occur in the attributed message")
        return message

    keys(request["constraints"], FIELDS, "constraint ledger")
    goal = {}
    for field, constraint in request["constraints"].items():
        keys(constraint, {"value", "message_id", "quote"}, f"constraint {field}")
        source(constraint)
        goal[field] = constraint["value"]
    validate_goal(goal)
    auth = request["authorization"]
    keys(auth, {"message_id", "quote", "goal_hash", "actions"}, "authorization")
    if source(auth)["speaker"] != "user":
        raise Refused("only an actual user source can authorize execution")
    if auth["goal_hash"] != digest(goal) or auth["actions"] != [PROFILES[goal["task"]]["action"]]:
        raise Refused("authorization is absent or does not cover this exact goal")
    return goal


def retain_goal(candidate, trusted_goal):
    validate_goal(candidate)
    if candidate != trusted_goal:
        lost = [k for k in FIELDS if candidate[k] != trusted_goal[k]]
        raise Refused(f"compiled goal changes source constraints: {sorted(lost)}")


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
        raise Refused("plan changes constraints, tool arguments, order or declared final artifacts")
    return plan
