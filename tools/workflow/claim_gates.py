"""Pre-review claim gates (EVIDENCE_CONTRACT v2_4.claim_checks / v2_4.evidence_checker).

Gate 1 is deterministic: every claim cites a bound, hashed source with a verbatim
(whitespace-normalised) quote. Derivable claims are computed by the controller from a
CLOSED registry of facts; an unregistered fact is never SUPPORTED. Gate 2 is the separate
evidence_checker role for non-derivable claims; its verdict is never authority over a
derivable claim. Nothing here infers semantic entailment.
"""

import json
from pathlib import Path

from .contracts import Refused, digest, keys
from .review_evidence import REFERENCE_DEFINITION_SOURCE_ID, SYMBOL_ALIASES, text_hash

VERDICTS = ("SUPPORTED", "INSUFFICIENT", "CONTRADICTED")
ORDER = {v: i for i, v in enumerate(VERDICTS)}
MAX_PRESENTATIONS_PER_PLAN = 3
CLAIM_FIELDS = {"claim_id", "text", "citations", "derivable"}


def worst(verdicts):
    """Receipt verdict: SUPPORTED only when every ruled claim is SUPPORTED."""
    verdicts = list(verdicts)
    if any(v not in ORDER for v in verdicts):
        raise Refused(
            "evidence-checker verdict outside the closed vocabulary", gate="claim_evidence"
        )
    return max(verdicts, key=ORDER.__getitem__) if verdicts else "SUPPORTED"


# --------------------------------------------------------------------------- bound sources


def bind_definition_source(path):
    """Read an explicitly supplied, reviewed definition source; record its identity.

    The controller never locates this text itself (and never reads the frozen case
    package): the harness or operator passes the file, and its hash is bound here.
    """
    resolved = Path(path).resolve()
    text = read_definition_text(resolved)
    return {
        "source_id": REFERENCE_DEFINITION_SOURCE_ID,
        "symbol": next(k for k, v in SYMBOL_ALIASES.items() if v == REFERENCE_DEFINITION_SOURCE_ID),
        "path": str(resolved),
        "source_hash": text_hash(text),
        "byte_length": len(text.encode("utf-8")),
    }


def read_definition_text(path):
    try:
        # Bytes, not text mode: newline translation would change the bound hash.
        text = Path(path).read_bytes().decode("utf-8")
    except (OSError, UnicodeDecodeError) as exc:
        raise Refused("required definition source unavailable", gate="claim_evidence") from exc
    if not text.strip():
        raise Refused("required definition source empty", gate="claim_evidence")
    return text


def resolve_source_symbol(symbol, sources):
    """Resolve HARNESS_SOURCE_HASH:<id> (or a bare id) to a bound source's hash.

    The dot-form symbol names the colon-form bound source (owner ruling D1 / CR-01).
    """
    if not isinstance(symbol, str) or not symbol:
        raise Refused("source symbol missing", gate="claim_evidence")
    name = symbol.split(":", 1)[1] if symbol.startswith("HARNESS_SOURCE_HASH:") else symbol
    source_id = SYMBOL_ALIASES.get(name, name)
    matches = [s for s in sources if s.get("source_id") == source_id]
    if len(matches) != 1:
        raise Refused(
            f"source symbol does not name exactly one bound source: {symbol}", gate="claim_evidence"
        )
    return matches[0]["source_hash"]


def registry(sources):
    if not isinstance(sources, list) or not sources:
        raise Refused("review sources missing", gate="claim_evidence")
    bound = {}
    for source in sources:
        keys(source, {"source_id", "source_hash", "text"}, "review source")
        identity, text = source["source_id"], source["text"]
        if (
            not isinstance(identity, str)
            or not identity
            or identity in bound
            or not isinstance(text, str)
            or not text
            or source["source_hash"] != text_hash(text)
        ):
            raise Refused("review source identity/hash mismatch", gate="claim_evidence")
        bound[identity] = source
    return bound


# --------------------------------------------------------------------------- claim shape


def validate_claim_shape(claims):
    """Structural validation only. Citation resolution is gate 1's recorded verdict.

    A claim that cannot be represented as a typed contract record is refused, because a
    record with a null or wrongly typed field is malformed evidence (v2_5.record_lists).
    """
    if not isinstance(claims, list) or not claims:
        raise Refused("review claims missing", gate="claim_evidence")
    seen = set()
    for claim in claims:
        if not isinstance(claim, dict) or set(claim) not in (
            CLAIM_FIELDS,
            CLAIM_FIELDS | {"computes"},
        ):
            raise Refused("invalid review claim fields", gate="claim_evidence")
        identity = claim["claim_id"]
        if (
            not isinstance(identity, str)
            or not identity.strip()
            or identity in seen
            or not isinstance(claim["text"], str)
            or not claim["text"].strip()
            or type(claim["derivable"]) is not bool
        ):
            raise Refused("invalid review claim identity/text/declaration", gate="claim_evidence")
        seen.add(identity)
        if not isinstance(claim["citations"], list):
            raise Refused("review claim citations must be a list", gate="claim_evidence")
        for citation in claim["citations"]:
            keys(citation, {"source_id", "source_hash", "quote"}, "review citation")
            if not all(isinstance(citation[k], str) for k in citation):
                raise Refused("review citation fields must be strings", gate="claim_evidence")
    return claims


# --------------------------------------------------------------------------- gate 1


def _ws(text):
    return " ".join(text.split())


def citation_verdict(claim, bound):
    """PASS only when every citation names a bound source, its hash and a verbatim quote.

    A non-derivable claim needs at least one citation. A derivable claim may carry none: the
    controller computes it from its own state, and the frozen contract requires a citation only
    on non-derivable claims (any citation a derivable claim does carry must still resolve).
    """
    if not claim["citations"]:
        return "PASS" if claim["derivable"] else "FAIL"
    for citation in claim["citations"]:
        source = bound.get(citation["source_id"])
        quote = citation["quote"]
        if (
            source is None
            or citation["source_hash"] != source["source_hash"]
            or not quote.strip()
            or _ws(quote) not in _ws(source["text"])
        ):
            return "FAIL"
    return "PASS"


# --------------------------------------------------------------------------- derivable facts


# Every producer reads ONLY the controller's own bound state -- the date contract it derives
# from the trusted goal, the trusted goal itself (Controller.goal, which every retained
# compiled goal must equal), and the validated plan (Controller.plan) -- and returns
# {fact name: value}. None of them ever reads a claim: a claim names a fact and states a
# value, and the controller compares that value with its own.


def _last_eligible_onset_date(date_contract, goal, plan):
    if not isinstance(date_contract, dict):
        return {}
    return {"last_eligible_onset_date": date_contract["eligible_onset_window"]["end"]}


def _goal_fields(date_contract, goal, plan):
    """goal.<field>: the value of that field of the controller's goal."""
    if not isinstance(goal, dict):
        return {}
    return {f"goal.{field}": value for field, value in goal.items()}


def _plan_step_args(date_contract, goal, plan):
    """plan.<step id>.<argument>: that argument's value in the controller's plan."""
    steps = plan.get("steps") if isinstance(plan, dict) else None
    facts = {}
    for step in steps if isinstance(steps, list) else ():
        if (
            not isinstance(step, dict)
            or not isinstance(step.get("id"), str)
            or not isinstance(step.get("args"), dict)
        ):
            continue
        for arg, value in step["args"].items():
            facts[f"plan.{step['id']}.{arg}"] = value
    return facts


# CLOSED registry. Adding a fact is a reviewed code change, never a model declaration.
DERIVABLE_FACTS = {
    "last_eligible_onset_date": _last_eligible_onset_date,
    "goal_field": _goal_fields,
    "plan_step_arg": _plan_step_args,
}


def derivable_facts(date_contract, goal, plan):
    """{fact name: controller value} for every registered fact the bound state defines."""
    facts = {}
    for producer in DERIVABLE_FACTS.values():
        for name, value in producer(date_contract, goal, plan).items():
            if name in facts:
                raise Refused(f"derivable fact defined twice: {name}", gate="claim_evidence")
            facts[name] = value
    # Detached: a comparison can never alias (or later edit) the controller's own records.
    return json.loads(json.dumps(facts))


def compute(claim, date_contract, goal, plan):
    """MATCH / MISMATCH from the controller's own computation, or None when not computable.

    None (no computes block, or a fact outside the closed registry) can never yield
    SUPPORTED: the claim's verdict becomes INSUFFICIENT and the plan routes to repair.
    """
    spec = claim.get("computes")
    if not isinstance(spec, dict) or set(spec) != {"fact", "value"}:
        return None
    facts = derivable_facts(date_contract, goal, plan)
    if not isinstance(spec["fact"], str) or spec["fact"] not in facts:
        return None
    expected = facts[spec["fact"]]
    value = spec["value"]
    return "MATCH" if type(value) is type(expected) and value == expected else "MISMATCH"


# --------------------------------------------------------------------------- gate 2


def checker_payload(plan_hash, binding_hash, claims, sources):
    judged = [
        {k: c[k] for k in ("claim_id", "text", "citations")} for c in claims if not c["derivable"]
    ]
    value = {
        "plan_hash": plan_hash,
        "binding_hash": binding_hash,
        "claims": judged,
        "sources": sources,
        "instruction": "Judge whether each listed claim is supported by the bound sources it "
        "cites. Return ONLY JSON with plan_hash (copy it) and verdicts: one "
        "{claim_id, verdict, reasoning} per listed claim, verdict SUPPORTED, INSUFFICIENT "
        "or CONTRADICTED. Your verdict is not authority and cannot approve the plan.",
    }
    # Detached: a hook editing the payload can never edit the bound claims or sources.
    return json.loads(json.dumps(value))


def validate_checker_output(output, plan_hash, judged_ids, carried_ids):
    """Return {claim_id: verdict}. Every judged claim must be ruled; never an uncarried claim."""
    keys(output, {"plan_hash", "verdicts"}, "evidence_checker")
    if output["plan_hash"] != plan_hash:
        raise Refused(
            "evidence_checker output is bound to a stale or different plan_hash",
            gate="claim_evidence",
        )
    verdicts = output["verdicts"]
    if not isinstance(verdicts, list):
        raise Refused("evidence_checker verdicts must be a list", gate="claim_evidence")
    ruled = {}
    for item in verdicts:
        keys(item, {"claim_id", "verdict", "reasoning"}, "evidence_checker verdict")
        claim_id = item["claim_id"]
        if not isinstance(claim_id, str) or claim_id not in carried_ids:
            raise Refused(
                "evidence_checker ruled a claim the claim check does not carry",
                gate="claim_evidence",
            )
        if claim_id in ruled:
            raise Refused("evidence_checker ruled a claim twice", gate="claim_evidence")
        if item["verdict"] not in ORDER or not isinstance(item["reasoning"], str):
            raise Refused(
                "evidence_checker verdict outside the closed vocabulary", gate="claim_evidence"
            )
        ruled[claim_id] = item["verdict"]
    if set(judged_ids) - set(ruled):
        raise Refused("evidence_checker did not rule every judged claim", gate="claim_evidence")
    return ruled


# --------------------------------------------------------------------------- record


def build_record(
    index,
    plan_hash,
    manifest_hash,
    source_hash,
    claims,
    sources,
    date_contract,
    goal,
    plan,
    checker_occurrence=None,
    checker_verdicts=None,
):
    """One controller_trace.claim_checks record, exactly the v2_4 field set.

    date_contract, goal and plan are the controller's own bound state (Controller.date_contract(),
    Controller.goal, Controller.plan); derivable claims are computed against them only.
    """
    bound = registry(sources)
    checker_verdicts = checker_verdicts or {}
    if checker_occurrence is None and checker_verdicts:
        raise Refused("checker verdicts without a checker occurrence", gate="claim_evidence")
    per_claim = []
    for claim in claims:
        citation = citation_verdict(claim, bound)
        checker = checker_verdicts.get(claim["claim_id"])
        if claim["derivable"]:
            computed = compute(claim, date_contract, goal, plan)
            verdict = {"MATCH": "SUPPORTED", "MISMATCH": "CONTRADICTED"}.get(
                computed, "INSUFFICIENT"
            )
            basis = "computation"
        else:
            computed, basis = None, "checker"
            verdict = checker if checker is not None else "INSUFFICIENT"
        per_claim.append(
            {
                "claim_id": claim["claim_id"],
                "text": claim["text"],
                "citations": json.loads(json.dumps(claim["citations"])),
                "derivable": claim["derivable"],
                "basis": basis,
                "citation_verdict": citation,
                "checker_verdict": checker,
                "computed": computed,
                "verdict": verdict,
            }
        )
    gate = "PASS" if all(c["citation_verdict"] == "PASS" for c in per_claim) else "FAIL"
    if gate == "FAIL" and checker_occurrence is not None:
        raise Refused(
            "no evidence checker may run after a citation gate failure", gate="claim_evidence"
        )
    # A plan is presented only after an evidence-checker pass (V24-PRE-REVIEW-GATES requires a
    # checker occurrence and a SUPPORTED checker receipt for every presented plan): a claim set
    # with no checker-judged claim routes to repair, however well its computed facts match. The
    # receipt is the worst of ALL rulings, so a ruling on a derivable claim the checker was not
    # sent also blocks presentation; it never makes that claim's own verdict.
    present = (
        gate == "PASS"
        and checker_occurrence is not None
        and all(v == "SUPPORTED" for v in checker_verdicts.values())
        and all(c["verdict"] == "SUPPORTED" for c in per_claim)
    )
    return {
        "index": index,
        "plan_hash": plan_hash,
        "manifest_hash": manifest_hash,
        "source_hash": source_hash,
        "citation_gate": gate,
        "evidence_checker_occurrence": checker_occurrence,
        "claims": per_claim,
        "decision": "PRESENT_FOR_REVIEW" if present else "ROUTE_TO_REPAIR",
        "reviewer_occurrence": None,
    }


def presentations(records, plan_hash):
    return sum(
        1 for r in records if r["plan_hash"] == plan_hash and r["decision"] == "PRESENT_FOR_REVIEW"
    )


def repair_failure(record):
    """The bounded, factual finding handed to the repairer for a routed plan version."""
    finding = {
        "claim_check_index": record["index"],
        "citation_gate": record["citation_gate"],
        "failed_claims": [
            {k: c[k] for k in ("claim_id", "citation_verdict", "computed", "verdict", "basis")}
            for c in record["claims"]
            if c["verdict"] != "SUPPORTED" or c["citation_verdict"] != "PASS"
        ],
        "instruction": "Correct the claims or citations; quotes must be verbatim from a "
        "bound source and derivable facts must match the controller's computation.",
    }
    if not any(not c["derivable"] for c in record["claims"]):
        # Only on this route, so every other finding keeps its exact bytes.
        finding["checker_judged_claims"] = 0
        finding["instruction"] += (
            " Every claim is derivable, so no evidence checker ran and the plan cannot be "
            "presented: add at least one derivable false claim whose cited quotes state its fact."
        )
    overruled = [
        c["claim_id"]
        for c in record["claims"]
        if c["derivable"] and c["checker_verdict"] not in (None, "SUPPORTED")
    ]
    if overruled:
        # Only on this route, likewise: the checker ruled a derivable claim it was not sent.
        finding["checker_ruled_derivable_claims"] = overruled
        finding["instruction"] += (
            " The evidence checker ruled derivable claims it was not asked to judge, and its "
            "receipt must be SUPPORTED before the plan is presented: restate or remove them."
        )
    return json.dumps(finding, sort_keys=True)


def record_hash(record):
    return digest(record)
