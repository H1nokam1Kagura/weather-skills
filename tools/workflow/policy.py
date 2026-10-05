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
