"""Relay DEDUP smoke, run INSIDE the harness container (uid 1000) on the relay-only network.

Started by ``launch.py relay-smoke --client relay_dedup_smoke`` as the container's own command
(never via docker exec). It de-risks the first live case on two points the plain relay smoke
(one ~1 KB verifier call) does not reach:

(a) Kong accepts a REVIEWER-SIZED request: the payload mirrors the measured reviewer payload
    (``live-role-payload-sizes.json``: ~123 KB logical, ~55 KB of skill documentation carried
    twice, under ``documentation`` and ``review_evidence.sources``), so ``RelayRoles`` sends it
    ``dedup-sha256/1``-encoded (~67 KB) through the production path;
(b) the model RESOLVES ``{"$shared": ...}`` references: a random canary token sits ONLY inside
    the shared documentation block (it occurs nowhere in the role-input JSON section of the
    model-facing message), and the reviewer is asked to quote it in ``findings``. The reviewer
    output schema is unchanged (exactly verdict / binding_hash / findings), so the canary rides
    in a schema-valid field.

Prints exactly one JSON object on stdout: the same key-absence (names only) and egress probes
as ``relay_smoke.py`` (reused), the payload/encoding prediction, exactly one ``reviewer`` call
through RelayRoles (no retries) with its redacted receipt, and the reviewer answer (synthetic).
When the relay refuses the answer as invalid, ``answer_diagnostic`` carries the relay's
bounded shape of it (stop_reason, content block types, output tokens, length, empty / fenced,
first and last 300 characters) so the failure is diagnosable from the evidence.

Data policy: the payload is synthetic generated text, Not Sensitive. The canary is a fresh
random token per run, so a model cannot produce it without reading the shared block.
"""

import hashlib
import json
import os
import random
import secrets
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
for _path in ("/opt/ctl", str(HERE.parent), str(HERE)):  # container root; repo root; this dir
    if _path not in sys.path:
        sys.path.insert(0, _path)

import relay_smoke  # noqa: E402  (probes + credential-name heuristic, reused unchanged)

from tools.workflow.relay_roles import (  # noqa: E402
    DEDUP_ENCODING,
    DEFAULT_MAX_BODY,
    SHARED_KEY,
    dedup_encode,
)

SCHEMA = "simkit.relay-dedup-smoke.container/1"
ROLE = "reviewer"
# Measured reviewer documentation block: 55,479 B serialized (live-role-payload-sizes.json).
DOC_NAME = "synthetic-onset-skill-guide.md"
DOC_TARGET_BYTES = 55_400
CANARY_FRACTION = 0.62  # the marker paragraph sits ~62% into the shared block
CANARY_PREFIX = "Transport integrity marker:"
CANARY_FINDING_PREFIX = "integrity marker: "
REVIEWER_VERDICTS = ("APPROVE", "REJECT", "NEEDS-INFO")  # agents.PROMPTS["reviewer"]
# Kong kit KNOWN_GOOD_BODY_BYTES (relay/runner_kit/scripts/kong_client.py): the worker refuses
# a gateway body above it before sending.
KNOWN_GOOD_GATEWAY_BODY_BYTES = 97_600
SEED = 20261015
BINDING_HASH = "sha256:" + hashlib.sha256(b"simkit relay-dedup-smoke synthetic binding").hexdigest()
INSTRUCTION = (
    "SYNTHETIC TRANSPORT CHECK (Not Sensitive, generated text). Review the plan normally. In "
    "addition: the bound documentation source (documentation and review_evidence.sources[0].text,"
    " which may be written as a shared reference) contains exactly one sentence that begins '"
    + CANARY_PREFIX
    + "'. The FIRST element of findings MUST be the string '"
    + CANARY_FINDING_PREFIX
    + "' followed by that marker token copied exactly, character for character. If you cannot "
    "find it, the first finding must be '" + CANARY_FINDING_PREFIX + "NOT FOUND'. Keep findings "
    "to at most three short strings."
)
RECEIPT_KEEP = (
    *relay_smoke.RECEIPT_KEEP,
    "settings",
    "settings_sent",
    "sent_keys",
    "output_form",
    "transport_encoding",
    "transport_bytes",
)

WORDS = (
    "forecast rolling window onset rainfall threshold station district season dekad cumulative "
    "millimetre sowing advisory farmer extension calendar anomaly climatology baseline gauge "
    "satellite estimate blend quality flag missing value interpolation tolerance profile "
    "registered template contract source binding review planner verifier controller gate "
    "replay artifact manifest checksum deterministic eligible date retreat false start dry "
    "spell length criterion agronomic definition crop maize sorghum millet soil moisture "
    "evapotranspiration reference period percentile tercile probability ensemble member lead "
    "time skill score hindcast verification tolerance window boundary inclusive exclusive "
    "metadata attribution constraint immutable goal request schema field unit conversion "
    "output table summary narrative caveat limitation assumption documented example procedure "
    "step input parameter default override precedence validation refusal record audit"
).split()


def _sentence(rng):
    words = [rng.choice(WORDS) for _ in range(rng.randint(9, 18))]
    words[0] = words[0].capitalize()
    return " ".join(words) + "."


def _paragraph(rng):
    return " ".join(_sentence(rng) for _ in range(rng.randint(3, 6)))


def synthetic_text(seed, target_bytes, section_title="Section"):
    """Deterministic generated prose (no quotes, no backslashes) of at least target_bytes."""
    rng = random.Random(seed)
    parts, size, section = [], 0, 0
    while size < target_bytes:
        if len(parts) % 6 == 0:
            section += 1
            parts.append(f"## {section_title} {section}: {rng.choice(WORDS)} {rng.choice(WORDS)}")
        parts.append(_paragraph(rng))
        size = len("\n\n".join(parts).encode("utf-8"))
    return parts


def make_canary():
    return "canary-" + secrets.token_hex(12)


def documentation_text(canary):
    """The shared block: ~55 KB of generated prose with the canary sentence ~62% in."""
    parts = synthetic_text(SEED, DOC_TARGET_BYTES, "Guide section")
    total, running, at = len("\n\n".join(parts)), 0, len(parts)
    for i, part in enumerate(parts):
        running += len(part) + 2
        if running >= CANARY_FRACTION * total:
            at = i + 1
            break
    marker = f"{CANARY_PREFIX} {canary}. It identifies this generated document only."
    return "\n\n".join([*parts[:at], marker, *parts[at:]])


def build_payload(canary):
    """A synthetic reviewer payload shaped like the measured one (keys and sizes)."""
    doc = documentation_text(canary)
    doc_hash = "sha256:" + hashlib.sha256(doc.encode("utf-8")).hexdigest()
    notes = "\n\n".join(synthetic_text(SEED + 1, 5_200, "Request note"))
    checks = "\n\n".join(synthetic_text(SEED + 2, 1_500, "Claim check"))
    claims_note = " ".join(synthetic_text(SEED + 3, 1_800, "Claim"))
    source_id = "documentation:" + DOC_NAME
    return {
        "execution_context": {
            "mode": "synthetic-transport-check",
            "data_classification": "Not Sensitive (generated text)",
            "run": "relay-dedup-smoke",
            "controller": "simkit relay dedup smoke",
        },
        "binding_hash": BINDING_HASH,
        "request": {
            "instruction": INSTRUCTION,
            "goal": "Report the onset date of a synthetic station series under the profile.",
            "notes": notes,
        },
        "plan": {
            "profile": "synthetic_onset_v1",
            "steps": ["load synthetic series", "apply rolling window", "report first date"],
        },
        "source": {"id": "synthetic-series-001", "kind": "generated", "rows": 365},
        "source_gate": {"status": "PASS", "checked": ["schema", "units", "date range"]},
        "documentation": {DOC_NAME: doc},
        "date_contract": {"start": "2026-01-01", "end": "2026-12-31", "calendar": "gregorian"},
        "preflight": {"status": "PASS"},
        "prior_review": None,
        "repair_response": None,
        "controller_gates": {"plan_validation": "PASS", "source_gate": "PASS"},
        "review_evidence": {
            "schema": "synthetic-review-evidence/1",
            "binding_hash": BINDING_HASH,
            "plan_claims": [
                {
                    "claim_id": "c1",
                    "text": "The plan applies the documented rolling window.",
                    "citations": [{"source_id": source_id, "quote": "rolling window"}],
                    "note": claims_note,
                }
            ],
            "sources": [{"source_id": source_id, "source_hash": doc_hash, "text": doc}],
            "capability": "PARTIAL_IDENTITY_CAPTURE_ONLY",
        },
        "claim_check": {"status": "RECORDED", "authority": "none", "detail": checks},
    }


def _prompt_and_render():
    # Imported lazily: the relay module pulls in the controller's stdlib-only workflow modules.
    from relay.kong_relay_host import render_model_input
    from tools.workflow.agents import role_prompt

    return role_prompt, render_model_input


def gateway_body_bytes(system, user, settings_sent):
    """Size of the body the kit worker builds (kong_roles.WORKER_SOURCE): the echoed
    non-content settings plus system and the single user message, ``json.dumps``-measured as
    ``kong_client.check_body_size`` measures it."""
    body = dict(settings_sent or {})
    body["system"] = system
    body["messages"] = [{"role": "user", "content": user}]
    return len(json.dumps(body).encode("utf-8"))


def predict(payload, canary):
    """What RelayRoles will send and the relay will render, computed with the production code."""
    role_prompt, render_model_input = _prompt_and_render()
    logical = json.dumps({"role": ROLE, "payload": payload}, allow_nan=False)
    encoded = dedup_encode(payload)
    if encoded is None:
        body, user, shared = logical, json.dumps(payload), {}
    else:
        body = json.dumps({"role": ROLE, **encoded}, allow_nan=False)
        shared = encoded["shared"]
        user = render_model_input(encoded["payload"], shared)
    role_input = json.dumps(encoded["payload"] if encoded else payload)
    system = role_prompt(ROLE, payload)
    shared_texts = [json.dumps(v) for v in shared.values()]
    return {
        "transport_encoding": None if encoded is None else DEDUP_ENCODING,
        "logical_body_bytes": len(logical.encode("utf-8")),
        "payload_bytes": len(json.dumps(payload).encode("utf-8")),
        "request_body_bytes": len(body.encode("utf-8")),
        "request_body_cap": DEFAULT_MAX_BODY,
        "shared_blocks": len(shared),
        "shared_bytes": sum(len(t.encode("utf-8")) for t in shared_texts),
        "model_facing_bytes": len(user.encode("utf-8")),
        "model_facing_hash": "sha256:" + hashlib.sha256(user.encode("utf-8")).hexdigest(),
        "system_prompt_hash": "sha256:" + hashlib.sha256(system.encode("utf-8")).hexdigest(),
        "canary_sha256": hashlib.sha256(canary.encode("utf-8")).hexdigest(),
        "canary_in_role_input_section": canary in role_input,
        "canary_in_shared_blocks": sum(t.count(canary) for t in shared_texts),
        "canary_in_request_body": body.count(canary),
        "canary_in_model_facing": user.count(canary),
        "canary_offset_in_shared_block": shared_texts[0].find(canary) if shared_texts else None,
        "reference_count_in_role_input": role_input.count('{"' + SHARED_KEY + '": '),
        # temperature unknown until the worker echoes settings_sent; bound with a placeholder
        "gateway_body_bytes_bound": gateway_body_bytes(
            system,
            user,
            {"model": "claude-sonnet-4-6", "max_tokens": 64000, "extra_body": {"temperature": 0}},
        ),
        "gateway_body_known_good": KNOWN_GOOD_GATEWAY_BODY_BYTES,
        "_system": system,
        "_user": user,
    }


def key_absence():
    """Container-side key absence, names only (same rules as relay_smoke.main)."""
    names = sorted(os.environ)
    found = []
    for top, dirs, files in os.walk("/"):
        if top == "/":
            dirs[:] = [d for d in dirs if d not in ("proc", "sys", "dev")]
        for name in files:
            low = name.lower()
            if low == ".env" or low.endswith(".env") or "kong" in low:
                found.append(os.path.join(top, name))
    return {
        "env_names": names,
        "credential_like_env_names": [
            n
            for n in names
            if any(m in n.upper() for m in relay_smoke.CREDENTIAL_NAME_MARKERS)
            and n not in relay_smoke.ALLOWED_RELAY_NAMES
        ],
        "kong_named_files": sorted(found),
        "credential_like_files": sorted(
            f
            for f in found
            if not f.startswith("/opt/ctl/") and relay_smoke.credential_like_name(f)
        ),
    }


def refused_fields(roles, exc):
    """The container record of a refused call. ``answer_diagnostic`` is the relay's bounded
    shape of an answer that did not validate (stop_reason, content block types, output tokens,
    length, empty / fenced, head / tail of this synthetic answer); null for other refusals."""
    refusal = getattr(roles, "last_refusal", None) or {}
    return {
        "outcome": "REFUSED",
        "reason": str(exc),
        "gate": exc.gate,
        "relay_id": refusal.get("relay_id"),
        "reason_code": refusal.get("reason_code"),
        "answer_diagnostic": refusal.get("answer_diagnostic"),
    }


def main():
    out = {"schema": SCHEMA, "uid": os.getuid(), "payload_classification": "synthetic"}
    out.update(key_absence())
    targets = json.loads(os.environ.get("SIMKIT_PROBE_TARGETS") or "{}")

    from tools.workflow.contracts import Refused
    from tools.workflow.relay_roles import RelayOutcomeUnknown, RelayRoles

    roles = RelayRoles.from_env(budget=1, timeout=240)
    out["relay"] = {"host": roles.host, "port": roles.port, "run_id": roles.run_id}
    out["egress_probes"] = relay_smoke.probes(targets, roles.host, roles.port)
    try:
        health = roles.health()
        out["health"] = {k: health.get(k) for k in ("outcome", "run_id", "allowed_roles", "model")}
    except Refused as exc:
        out["health"] = {"outcome": "REFUSED", "reason": str(exc)}
    canary = make_canary()
    payload = build_payload(canary)
    prediction = predict(payload, canary)
    system, user = prediction.pop("_system"), prediction.pop("_user")
    out.update(canary=canary, binding_hash=BINDING_HASH, prediction=prediction)
    try:
        value = roles.ask(ROLE, payload)
    except RelayOutcomeUnknown as exc:
        out.update(outcome="UNKNOWN", evidence=exc.evidence)
    except Refused as exc:
        out.update(refused_fields(roles, exc))
    else:
        receipt = roles.receipts[-1]
        out.update(
            outcome="SUCCESS",
            answer=value,
            receipt={k: receipt.get(k) for k in RECEIPT_KEEP},
            gateway_body_bytes=gateway_body_bytes(system, user, receipt.get("settings_sent")),
            gateway_body_basis="reconstructed from the predicted system prompt and model-facing "
            "message plus the worker-echoed settings_sent; exact when the receipt's "
            "model_facing_hash and system_prompt_hash equal the prediction",
        )
    out["client_spent_reserved"] = roles.spent
    print(json.dumps(out))
    return 0


if __name__ == "__main__":
    sys.exit(main())
