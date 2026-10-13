"""Finite decisions with state-bound action masks. Shadow predictions grant no authority."""

import math

from .contracts import Refused, digest, keys


def question(state, binding, actions, evidence):
    if not actions or len(set(actions)) != len(actions):
        raise Refused("invalid action mask")
    body = {
        "schema": "workflow.decision/1",
        "state": state,
        "binding_hash": binding,
        "admissible_actions": actions,
        "evidence": evidence,
    }
    return {**body, "question_hash": digest(body)}


def validate(answer, prompt):
    keys(answer, {"question_hash", "action", "scores"}, "policy decision")
    if (
        answer["question_hash"] != prompt["question_hash"]
        or answer["action"] not in prompt["admissible_actions"]
    ):
        raise Refused("stale or masked policy action")
    scores = answer["scores"]
    if not isinstance(scores, dict) or any(
        k not in prompt["admissible_actions"]
        or type(v) not in (int, float)
        or not math.isfinite(v)
        or not 0 <= v <= 1
        for k, v in scores.items()
    ):
        raise Refused("invalid bounded decision scores")
    return answer


# CAP-SHADOW (CP-E14). The frozen E09 cases schedule a shadow decision after each of these
# transitions (frozen/agentic-e2e-injections.DEV-ONLY.json, E09-P1/P2). Each state has a finite,
# state-bound candidate vocabulary: proceed to the profile's next state, or stop.
SHADOW_STATES = ("GOAL_CHECKED", "PLAN_VALIDATED", "ONSET_CHECKED", "DATA_VERIFIED")
SHADOW_SCHEMA = "workflow.decision/1"
# Output shape: EVIDENCE_CONTRACT.md role_output.shadow_policy[*].{schema,state,
# question_fingerprint,action,score}. ``score`` is the selected action's own score.
SHADOW_OUTPUT_FIELDS = frozenset({"schema", "state", "question_fingerprint", "action", "score"})


def shadow_candidates(state, next_state):
    """The per-state vocabulary: meaning -> native action label."""
    if state not in SHADOW_STATES or not isinstance(next_state, str) or not next_state:
        raise Refused("no shadow vocabulary for this state", gate="transition")
    return {"proceed": "PROCEED_TO_" + next_state, "stop": "STOP"}


def shadow_question(state, next_state, binding, evidence):
    candidates = shadow_candidates(state, next_state)
    body = {
        "schema": SHADOW_SCHEMA,
        "role": "shadow_policy",
        "state": state,
        "binding_hash": binding,
        "admissible_actions": [candidates["proceed"], candidates["stop"]],
        "candidates": candidates,
        "evidence": evidence,
        "authority": "none: shadow predictions are recorded and never applied",
    }
    question = {**body, "question_hash": digest(body)}
    # Free-text context a role may read; outside the fingerprinted body (E09-P2 patches notes).
    question["context"] = {"notes": []}
    return question


def shadow_rejection(answer, question):
    """Return None for a valid shadow output, else the reason it is rejected (never raises)."""
    if not isinstance(answer, dict):
        return "output is not an object"
    if set(answer) != SHADOW_OUTPUT_FIELDS:
        return "output fields differ from the shadow decision schema"
    if answer["schema"] != SHADOW_SCHEMA:
        return "schema mismatch"
    if answer["state"] != question["state"]:
        return "state mismatch"
    if answer["question_fingerprint"] != question["question_hash"]:
        return "stale question fingerprint"
    if answer["action"] not in question["admissible_actions"]:
        return "action masked at this state"
    score = answer["score"]
    if type(score) not in (int, float):
        return "score is not a number"
    if not math.isfinite(score):
        return "score is not finite"
    if not 0 <= score <= 1:
        return "score outside [0, 1]"
    return None
