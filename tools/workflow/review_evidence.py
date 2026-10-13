"""Opt-in citation identity binding, not a claim-support or entailment gate."""

import hashlib
import json

from .contracts import Refused, digest, keys, validate_plan

SCHEMA = "workflow.review-envelope/1"
DEFINITION_SOURCE_ID = "definition.agrhymet-sos-rolling"
UNSUPPORTED = ["claim_entailment", "derivable_claim_computation", "claim_gate_verdict"]


def text_hash(text):
    return "sha256:" + hashlib.sha256(text.encode("utf-8")).hexdigest()


def sources_from_docs(docs):
    if not isinstance(docs, dict) or not docs:
        raise Refused("review sources missing")
    if DEFINITION_SOURCE_ID not in docs:
        raise Refused("required definition source missing")
    sources = []
    for name, text in sorted(docs.items()):
        if not isinstance(name, str) or not isinstance(text, str) or not text:
            raise Refused("review source text missing")
        # This logical ID denotes the ENTIRE authoritative UTF-8 registry,
        # not an extracted entry or a semantic-support assertion.
        source_id = name if name == DEFINITION_SOURCE_ID else "documentation:" + name
        sources.append({"source_id": source_id,
                        "source_hash": text_hash(text), "text": text})
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
        if (not isinstance(identity, str) or not identity or identity in registry
                or not isinstance(text, str) or not text
                or source["source_hash"] != text_hash(text)):
            raise Refused("review source identity/hash mismatch")
        registry[identity] = source
    seen = set()
    for claim in claims:
        # derivable is the planner's declaration, not a controller verdict.
        required = {"claim_id", "text", "citations", "derivable"}
        if not isinstance(claim, dict) or set(claim) not in (required, required | {"computes"}):
            raise Refused("invalid review claim fields")
        identity = claim["claim_id"]
        if (not isinstance(identity, str) or not identity or identity in seen
                or not isinstance(claim["text"], str) or not claim["text"]
                or type(claim["derivable"]) is not bool):
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
            if (source is None or citation["source_hash"] != source["source_hash"]
                    or not isinstance(quote, str) or not quote or quote not in source["text"]):
                raise Refused("review citation source/hash/quote mismatch")


def unpack_plan(value, goal, sources):
    keys(value, {"schema", "plan", "plan_claims"}, "review plan envelope")
    if value["schema"] != SCHEMA:
        raise Refused("unsupported review plan envelope")
    validate_plan(value["plan"], goal)
    validate_claims(value["plan_claims"], sources)
    return json.loads(json.dumps(value))


def envelope(controller):
    evidence = controller.context["review_evidence"]
    validate_claims(evidence["plan_claims"], evidence["sources"])
    return json.loads(json.dumps({
        "schema": SCHEMA,
        "request_hash": controller.request_hash,
        "goal_hash": digest(controller.goal),
        "plan_hash": digest(controller.plan),
        "manifest_hash": controller.build_hash,
        "source_hash": controller.source_hash,
        "binding_hash": controller.binding,
        "plan_claims": evidence["plan_claims"],
        "sources": evidence["sources"],
        "unsupported": UNSUPPORTED,
        "capability": "PARTIAL_IDENTITY_CAPTURE_ONLY",
    }))


def capture(controller, payload):
    """Called after hooks, at dispatch; do not repair or fill missing payload fields."""
    evidence = controller.context["review_evidence"]
    if (payload.get("review_evidence") != envelope(controller)
            or payload.get("binding_hash") != controller.binding
            or payload.get("plan") != controller.plan
            or payload.get("request") != controller.request
            or sources_from_docs(payload.get("documentation")) != evidence["sources"]):
        raise Refused("review payload binding/source mismatch")
    evidence["reviewer_occurrence"] += 1
    occurrence = evidence["reviewer_occurrence"]
    # Same serializer as Roles.ask. This is a detached object sent to that boundary.
    stdin = json.dumps(payload)
    snapshot = {
        "schema": "workflow.review-capture/1",
        "run_id": evidence["run_id"], "role": "reviewer",
        "reviewer_occurrence": occurrence,
        "payload": json.loads(stdin), "stdin": stdin,
        "stdin_hash": text_hash(stdin), "input_hash": digest(payload),
        "review_evidence": envelope(controller),
        "dispatch_status": "ATTEMPTED", "accepted_review": False,
        "full_simkit_capability": "NOT_IMPLEMENTED",
    }
    name = f"review-capture-{occurrence:03}.json"
    controller.save(name, snapshot)
    return name, snapshot
