"""The three typed decision points and their question templates.

Each decision point maps to ONE typed System-1 question in the TypeSafe ``/v1/systemone``
shape that Laya, Kev and CLM all accept (``noul`` = yes/no probability, ``choice`` = one of
named options). The template text is fixed here, so every backend is asked the same thing
and the log can be compared across backends.

    escalate        noul    "Should the agent stop and ask the human before continuing?"
                            -> choice in {"escalate", "proceed"}, probs over both
    review_verdict  choice  approve / reject a proposed skill pipeline, given the skill
                            rules text in the state
                            -> choice in {"approve", "reject"}
    next_skill      choice  pick one of the RULE-VALID candidate skills (the caller passes
                            only candidates the rules already allow; the shadow never widens
                            the set) -> choice in options
"""
from __future__ import annotations

from typing import Any

DECISION_POINTS = ("escalate", "review_verdict", "next_skill")

ESCALATE_INSTRUCTIONS = (
    "Should the forecasting agent stop and ask the human before continuing? Answer yes when "
    "the request is ambiguous (place, time window, variable or product unclear), when the next "
    "step is costly, irreversible or would publish something, or when the data needed is "
    "missing; answer no when the request is specific and the plan is routine."
)

REVIEW_INSTRUCTIONS = (
    "A proposed skill pipeline and the rules of the skills it uses are given above. "
    "Does the pipeline follow every rule?"
)
REVIEW_CRITERIA = {
    "approve": "The pipeline follows every stated skill rule and answers the request.",
    "reject": "The pipeline breaks at least one stated skill rule, or does not answer the request.",
}

NEXT_SKILL_INSTRUCTIONS = (
    "Given the goal and the pipeline so far, which skill should run next?"
)

ESCALATE_OPTIONS = ("escalate", "proceed")
REVIEW_OPTIONS = tuple(REVIEW_CRITERIA)


def normalize_options(decision_point: str, options: Any) -> dict[str, str | None]:
    """Options as an ordered {key: description-or-None} map, validated per decision point."""
    if decision_point not in DECISION_POINTS:
        raise ValueError(f"unknown decision point {decision_point!r}; expected {DECISION_POINTS}")
    if decision_point == "escalate":
        return {k: None for k in ESCALATE_OPTIONS}
    if decision_point == "review_verdict":
        return dict(REVIEW_CRITERIA)
    if not options:
        raise ValueError("next_skill needs at least one rule-valid candidate skill")
    if isinstance(options, dict):
        return {str(k): (str(v) if v not in (None, "") else None) for k, v in options.items()}
    return {str(k): None for k in options}


def build_question(decision_point: str, options: Any) -> dict[str, Any]:
    """The single typed question (TypeSafe wire shape) for a decision point."""
    opts = normalize_options(decision_point, options)
    if decision_point == "escalate":
        return {"type": "noul", "instructions": ESCALATE_INSTRUCTIONS}
    if decision_point == "review_verdict":
        return {"type": "choice", "instructions": REVIEW_INSTRUCTIONS, "criteria": dict(REVIEW_CRITERIA)}
    return {"type": "choice", "instructions": NEXT_SKILL_INSTRUCTIONS, "criteria": dict(opts)}


def answer_to_result(decision_point: str, answer: dict[str, Any]) -> tuple[str, dict[str, float]]:
    """Map one typed answer back to (choice, probs) in the decision point's own option keys."""
    if decision_point == "escalate":
        if "noul" not in answer:
            raise ValueError(f"expected a noul answer for escalate, got keys {sorted(answer)}")
        p = float(answer["noul"])
        if not 0.0 <= p <= 1.0:
            raise ValueError(f"noul probability out of range: {p}")
        return ("escalate" if p >= 0.5 else "proceed"), {"escalate": p, "proceed": 1.0 - p}
    probs = answer.get("probabilities")
    if not isinstance(probs, dict) or not probs:
        raise ValueError(f"expected a choice answer with probabilities, got keys {sorted(answer)}")
    probs = {str(k): float(v) for k, v in probs.items()}
    choice = str(answer.get("choice") or max(probs, key=probs.__getitem__))
    return choice, probs
