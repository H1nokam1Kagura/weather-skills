"""Opt-in citation identity binding. Claim gates (claim_gates.py) are a separate, opt-in layer.

Without a supplied reference definition source this module keeps its original behaviour:
identity and verbatim checks that refuse, never a recorded gate verdict.
"""

import hashlib
import json

from .contracts import Refused, digest, keys, validate_plan

SCHEMA = "workflow.review-envelope/1"
DEFINITION_SOURCE_ID = "definition.agrhymet-sos-rolling"
# Owner ruling D1 (CR-01): the reference definition text is bound under the colon id, and
# the dot-form symbol resolves to that source's hash. The dot id stays the legacy binding
# of the definitions TOML when no reference definition source is supplied.
REFERENCE_DEFINITION_SOURCE_ID = "definition:agrhymet-sos-rolling"
SYMBOL_ALIASES = {DEFINITION_SOURCE_ID: REFERENCE_DEFINITION_SOURCE_ID}
UNSUPPORTED = ["claim_entailment", "derivable_claim_computation", "claim_gate_verdict"]
CLAIM_GATE_UNSUPPORTED = ["claim_entailment"]
DEFINITION_IDS = (DEFINITION_SOURCE_ID, REFERENCE_DEFINITION_SOURCE_ID)


def text_hash(text):
    return "sha256:" + hashlib.sha256(text.encode("utf-8")).hexdigest()


def sources_from_docs(docs):
    if not isinstance(docs, dict) or not docs:
        raise Refused("review sources missing")
    # Exactly one definition binding: never the TOML and the reference text side by side.
    if sum(identity in docs for identity in DEFINITION_IDS) != 1:
        raise Refused("required definition source missing")
    sources = []
    for name, text in sorted(docs.items()):
        if not isinstance(name, str) or not isinstance(text, str) or not text:
            raise Refused("review source text missing")
        # This logical ID denotes the ENTIRE authoritative UTF-8 document,
        # not an extracted entry or a semantic-support assertion.
        source_id = name if name in DEFINITION_IDS else "documentation:" + name
        sources.append({"source_id": source_id, "source_hash": text_hash(text), "text": text})
    return sources


def validate_claims(claims, sources):
    """Check identity and verbatim citation only; never infer semantic support."""
    if not isinstance(claims, list) or not claims:
        raise Refused("review claims missing")
    if not isinstance(sources, list) or not sources:
        raise Refused("review sources missing")
    registry = {}
    for source in sources:
        keys(source, {"source_id", "source_hash", "text"}, "review source")
        identity, text = source["source_id"], source["text"]
        if (
            not isinstance(identity, str)
            or not identity
            or identity in registry
            or not isinstance(text, str)
            or not text
            or source["source_hash"] != text_hash(text)
        ):
            raise Refused("review source identity/hash mismatch")
        registry[identity] = source
    seen = set()
    for claim in claims:
        # derivable is the planner's declaration, not a controller verdict.
        required = {"claim_id", "text", "citations", "derivable"}
        if not isinstance(claim, dict) or set(claim) not in (required, required | {"computes"}):
            raise Refused("invalid review claim fields")
        identity = claim["claim_id"]
        if (
            not isinstance(identity, str)
            or not identity
            or identity in seen
            or not isinstance(claim["text"], str)
            or not claim["text"]
            or type(claim["derivable"]) is not bool
        ):
            raise Refused("invalid review claim identity/text/declaration")
        seen.add(identity)
        citations = claim["citations"]
        if not isinstance(citations, list) or not citations:
            raise Refused("review claim citations missing")
        for citation in citations:
            keys(citation, {"source_id", "source_hash", "quote"}, "review citation")
            source_id = citation["source_id"]
            source = registry.get(source_id) if isinstance(source_id, str) else None
            quote = citation["quote"]
            if (
                source is None
                or citation["source_hash"] != source["source_hash"]
                or not isinstance(quote, str)
                or not quote
                or quote not in source["text"]
            ):
                raise Refused("review citation source/hash/quote mismatch")


def unpack_plan(value, goal, sources, claim_gates=False):
    """Accept the versioned envelope; rationale (D19) is optional and bound when present.

    With claim gates the frozen planner key 'claims' is also accepted (CAP-ROLE-OUTPUT-
    SCHEMA), and citation resolution is NOT checked here: gate 1 records it instead.
    """
    if not isinstance(value, dict):
        raise Refused("review plan envelope must be an object", gate="plan_validation")
    fields = set(value) - {"rationale"}
    if fields == {"schema", "plan", "plan_claims"}:
        if value["schema"] != SCHEMA:
            raise Refused("unsupported review plan envelope", gate="plan_validation")
        claims = value["plan_claims"]
    elif claim_gates and fields == {"plan", "claims"}:
        claims = value["claims"]
    else:
        raise Refused("review plan envelope: unexpected or missing fields", gate="plan_validation")
    if "rationale" in value and (
        not isinstance(value["rationale"], str) or len(value["rationale"]) > 4000
    ):
        raise Refused("invalid bounded planner rationale", gate="plan_validation")
    validate_plan(value["plan"], goal)
    if claim_gates:
        from .claim_gates import validate_claim_shape

        validate_claim_shape(claims)
    else:
        validate_claims(claims, sources)
    parsed = {"schema": SCHEMA, "plan": value["plan"], "plan_claims": claims}
    if "rationale" in value:
        parsed["rationale"] = value["rationale"]
    return json.loads(json.dumps(parsed))


def envelope(controller):
    evidence = controller.context["review_evidence"]
    gated = "claim_checks" in evidence
    if not gated:
        validate_claims(evidence["plan_claims"], evidence["sources"])
    value = {
        "schema": SCHEMA,
        "request_hash": controller.request_hash,
        "goal_hash": digest(controller.goal),
        "plan_hash": digest(controller.plan),
        "manifest_hash": controller.build_hash,
        "source_hash": controller.source_hash,
        "binding_hash": controller.binding,
        "plan_claims": evidence["plan_claims"],
        "sources": evidence["sources"],
        "unsupported": CLAIM_GATE_UNSUPPORTED if gated else UNSUPPORTED,
        "capability": "PRE_REVIEW_CLAIM_GATES" if gated else "PARTIAL_IDENTITY_CAPTURE_ONLY",
    }
    if "rationale" in evidence:
        # The reviewer sees exactly the planner rationale that the binding hash covers.
        value["rationale"] = evidence["rationale"]
    return json.loads(json.dumps(value))


def presented_binding(controller):
    """The binding a frozen-shape reviewer's approved_binding must agree with (UC-03)."""
    value = envelope(controller)
    return {
        k: value[k]
        for k in (
            "request_hash",
            "goal_hash",
            "plan_hash",
            "manifest_hash",
            "source_hash",
            "binding_hash",
        )
    }


def presented_claim_check(evidence):
    """The claim-check record presented with this review, or None without claim gates."""
    records = evidence.get("claim_checks")
    if not records:
        return None
    record = records[-1]
    if (
        record["decision"] != "PRESENT_FOR_REVIEW"
        or record["reviewer_occurrence"] != evidence["reviewer_occurrence"] + 1
    ):
        raise Refused(
            "no claim check presents this plan version to the reviewer", gate="claim_evidence"
        )
    return record


def capture(controller, payload):
    """Called after hooks, at dispatch; do not repair or fill missing payload fields."""
    evidence = controller.context["review_evidence"]
    if (
        payload.get("review_evidence") != envelope(controller)
        or payload.get("binding_hash") != controller.binding
        or payload.get("plan") != controller.plan
        or payload.get("request") != controller.request
        or sources_from_docs(payload.get("documentation")) != evidence["sources"]
        or payload.get("claim_check") != presented_claim_check(evidence)
    ):
        raise Refused("review payload binding/source mismatch")
    evidence["reviewer_occurrence"] += 1
    occurrence = evidence["reviewer_occurrence"]
    # Same serializer as Roles.ask. This is a detached object sent to that boundary.
    stdin = json.dumps(payload)
    snapshot = {
        "schema": "workflow.review-capture/1",
        "run_id": evidence["run_id"],
        "role": "reviewer",
        "reviewer_occurrence": occurrence,
        "payload": json.loads(stdin),
        "stdin": stdin,
        "stdin_hash": text_hash(stdin),
        "input_hash": digest(payload),
        "review_evidence": envelope(controller),
        "dispatch_status": "ATTEMPTED",
        "accepted_review": False,
        "full_simkit_capability": "NOT_IMPLEMENTED",
    }
    name = f"review-capture-{occurrence:03}.json"
    controller.save(name, snapshot)
    return name, snapshot
