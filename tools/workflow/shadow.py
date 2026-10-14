"""CAP-SHADOW (CP-E14): a shadow policy consulted after four transitions, never applied.

The controller asks role ``shadow_policy`` a fingerprinted question with a state-bound action
mask after GOAL_CHECKED, PLAN_VALIDATED, ONSET_CHECKED and DATA_VERIFIED (policy mode
``shadow_policy``). Every answer becomes one durable record in
``context["shadow_decisions"]`` (EVIDENCE_CONTRACT.md controller_trace.shadow_decisions
{state, accepted, action, score}). An invalid, masked, stale or non-finite answer is recorded
``accepted: false`` with its reason and ignored: the run never aborts because of it and never
changes course because of a valid one. ``authority_granted`` is always false.
"""

import copy
import json
import math

from . import policy
from .contracts import Refused, digest

MODE = "shadow_policy"


def enabled(controller):
    return getattr(controller, "policy_mode", "rules") == MODE


def next_state(controller, state):
    sequence = controller.sequence
    index = sequence.index(state)
    return sequence[index + 1] if index + 1 < len(sequence) else None


def evidence(controller, state):
    last = controller.events[-1] if controller.events else {}
    return {
        "transition_evidence_hash": last.get("evidence_hash"),
        "artifacts": copy.deepcopy(controller.artifacts),
    }


def _recordable(value):
    """Verbatim when JSON-representable; a non-finite number cannot be stored, so None."""
    try:
        json.dumps(value, allow_nan=False)
    except (TypeError, ValueError):
        return None
    return copy.deepcopy(value)


def after_transition(controller, state):
    """Invoke the shadow policy for ``state`` if this run uses it. Never raises for a bad
    answer; controller integrity failures and uncertain dispatches still stop the run."""
    if not enabled(controller) or state not in policy.SHADOW_STATES:
        return None
    question = policy.shadow_question(
        state, next_state(controller, state), controller.binding, evidence(controller, state)
    )
    # Detached: a hook may edit the dispatched payload (e.g. context notes); validation uses
    # the controller's own question.
    held = copy.deepcopy(question)
    controller.integrity()
    answer, reason = None, None
    try:
        answer = controller.ask(MODE, question)
    except Refused as exc:
        # A refusal raised by integrity is never a shadow outcome: re-check, and let it stop.
        controller.integrity()
        if controller.pending is not None:
            raise  # dispatch outcome uncertain: never continue past it
        reason = "role unavailable: " + str(exc)[:200]
    if reason is None:
        reason = policy.shadow_rejection(answer, held)
    returned = answer if isinstance(answer, dict) else {}
    score = returned.get("score")
    if isinstance(score, float) and not math.isfinite(score):
        score = None
    records = controller.context.setdefault("shadow_decisions", [])
    record = {
        "index": len(records),
        "state": state,
        "accepted": reason is None,
        "action": _recordable(returned.get("action")),
        "score": _recordable(score),
        "question_hash": held["question_hash"],
        "reason": reason,
        "authority_granted": False,
        "applied": False,
        "output_hash": _output_hash(answer),
    }
    records.append(record)
    controller.save(f"shadow-decision-{record['index']:03}.json", record)
    controller.dispatch_event("shadow_decision", decision=record)
    controller.checkpoint()
    return record


def _output_hash(answer):
    try:
        return digest(answer)
    except (TypeError, ValueError):
        return None
