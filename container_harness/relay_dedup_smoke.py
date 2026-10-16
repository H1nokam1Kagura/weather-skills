"""Relay DEDUP smoke, run INSIDE the harness container (uid 1000) on the relay-only network.

Started by ``launch.py relay-smoke --client relay_dedup_smoke`` as the container's own command
(never via docker exec). It de-risks the first live case on two points the plain relay smoke
(one ~1 KB verifier call) does not reach:

(a) Kong accepts a REVIEWER-SIZED request: the payload carries the measured reviewer's ~55 KB
    skill documentation (``live-role-payload-sizes.json``) under ``documentation`` and inside
    the review evidence envelope, so ``RelayRoles`` sends it ``dedup-sha256/1``-encoded
    through the production path;
(b) the model RESOLVES ``{"$shared": ...}`` references by doing what a reviewer always does:
    it returns ``binding_hash`` (the reviewer contract, ``agents.PROMPTS["reviewer"]``), and
    the runner refuses any other value (``Controller.accept_review_output``). Here the run's
    binding is reachable ONLY through a reference: there is no top-level ``binding_hash``; the
    identity sits in the review evidence envelope, and that envelope is presented twice
    (``review_evidence`` and the claim-check record's ``presented_review_evidence``), so the
    production encoder writes it once, as a shared block. The binding is derived per run from
    the presented identities and a random nonce, so it cannot be produced without reading
    that block.

Why this shape, not the real one verbatim: in a real reviewer payload ``binding_hash`` is a
64-character string at the top level and in ``review_evidence`` (both far below
``DEDUP_MIN_BYTES``), so it never lands in deduplicated content; only the documentation text
does. Dropping the top-level copy and presenting the envelope twice is the smallest change that
puts the field the reviewer contract already returns inside a shared block. Nothing in the
data asks the model to do anything: the documentation is ordinary technical prose and no
string value addresses the model (checked: ``instruction_terms``).

Prints exactly one JSON object on stdout: the same key-absence (names only) and egress probes
as ``relay_smoke.py`` (reused), the payload/encoding prediction, exactly one ``reviewer`` call
through RelayRoles (no retries) with its redacted receipt, and the reviewer answer (synthetic).
When the relay refuses the answer as invalid, ``answer_diagnostic`` carries the relay's
bounded shape of it (stop_reason, content block types, output tokens, length, empty / fenced,
first and last 300 characters) so the failure is diagnosable from the evidence.

Data policy: the payload is synthetic generated text, Not Sensitive.
"""

import hashlib
import json
import os
import random
import re
import secrets
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
for _path in ("/opt/ctl", str(HERE.parent), str(HERE)):  # container root; repo root; this dir
    if _path not in sys.path:
        sys.path.insert(0, _path)

import relay_smoke  # noqa: E402  (probes + credential-name heuristic, reused unchanged)

from tools.workflow.contracts import digest, plan_version_hash  # noqa: E402
from tools.workflow.relay_roles import (  # noqa: E402
    DEDUP_ENCODING,
    DEFAULT_MAX_BODY,
    SHARED_KEY,
    dedup_encode,
)

SCHEMA = "simkit.relay-dedup-smoke.container/2"
ROLE = "reviewer"
# Measured reviewer documentation block: 55,479 B serialized (live-role-payload-sizes.json).
DOC_NAME = "onset-rolling-window-guide.md"
DOC_TARGET_BYTES = 55_400
REVIEWER_VERDICTS = ("APPROVE", "REJECT", "NEEDS-INFO")  # agents.PROMPTS["reviewer"]
# Kong kit KNOWN_GOOD_BODY_BYTES (relay/runner_kit/scripts/kong_client.py): the worker refuses
# a gateway body above it before sending.
KNOWN_GOOD_GATEWAY_BODY_BYTES = 97_600
SEED = 20261015
ENVELOPE_SCHEMA = "workflow.review-envelope/1"  # review_evidence.SCHEMA
PRESENTED_KEY = "presented_review_evidence"  # where the claim-check record carries the envelope
# Text addressed to the model rather than describing the data. None may occur in any string
# VALUE of the payload (keys are the real contract's schema, e.g. a citation's ``quote``).
INSTRUCTION_TERMS = (
    "copy",
    "quote",
    "character for character",
    "ignore",
    "disregard",
    "verbatim",
    "instruction",
    "you must",
    "respond",
    "reply",
    "echo",
    "repeat",
    "marker",
    "canary",
    "your answer",
    "the reviewer",
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

# ------------------------------------------------------------------ synthetic documentation
# One fixed onset profile, so the guide, the plan and the worked examples agree.
PROFILE = {
    "profile_id": "onset_rolling_v1",
    "window_days": 3,
    "threshold_mm": 20.0,
    "min_rain_days": 2,
    "rain_day_mm": 1.0,
    "lookahead_days": 30,
    "dry_spell_days": 7,
}
TITLE = "# Onset detection with a rolling rainfall window"
CITED_SENTENCE = (
    "The onset date is the first day in the search period on which the rolling window total "
    "reaches the profile threshold and no false start follows."
)
INTRO = (
    "This guide describes how the registered profile onset_rolling_v1 computes the start of "
    "the rainy season from a daily station rainfall series. " + CITED_SENTENCE + " The "
    "procedure is deterministic: the same series and profile always give the same date."
)
SECTION_TITLES = (
    "Input series",
    "Rolling accumulation window",
    "Onset criterion",
    "False-start check",
    "Search period and last eligible date",
    "Missing values and quality flags",
    "Gauge and satellite blending",
    "Units and conversions",
    "Output table",
    "Climatological baseline",
    "Profile parameters",
    "Scope and limitations",
    "Worked examples",
)
PARAGRAPHS = (
    "The rolling accumulation window for profile onset_rolling_v1 spans {w} consecutive days. "
    "A day can be a candidate onset only when the window ending on it is complete, so the "
    "first {w1} days of the search period are never candidates.",
    "Station {st} reports daily rainfall in millimetres. Values below {trace} mm are recorded "
    "as trace amounts and count as dry days for the dry-spell test, but they are still added "
    "to the window total.",
    "The onset threshold is {mm} mm accumulated over the window, with at least {k} rain days "
    "of {r} mm or more. Both conditions are evaluated on the same window.",
    "After a candidate onset the false-start check looks ahead {f} days. If any run of {d} "
    "consecutive dry days occurs inside that period, the candidate is rejected and the search "
    "resumes on the following day.",
    "The search period for the {season} season at station {st} begins on {d1} and ends on "
    "{d2}. The last eligible date is the end of the search period minus the {f}-day "
    "look-ahead, because the false-start check needs complete data after the candidate.",
    "Missing daily values are flagged rather than filled. A window that contains more than "
    "{miss} missing days is treated as incomplete, and a candidate that depends on it is "
    "reported with quality flag {flag}.",
    "Gridded satellite estimates may be blended with gauge records when the profile allows "
    "it. The blend weight for station {st} is {bw}, and the provenance of each day is kept in "
    "the output table.",
    "Dates in the output table use the ISO 8601 calendar format. Day-of-year values count "
    "from the first of January and serve the plotting scale only; they are never onset flags.",
    "The output table has one row per station and season, with the columns station_id, "
    "season, onset_date, onset_doy, window_total_mm, rain_days and quality_flag. An empty "
    "onset_date means no qualifying day was found before the last eligible date.",
    "Unit conversions are applied before any threshold test. Inch-based records from station "
    "{st} are converted at 25.4 mm per inch and rounded to {dp} decimal places.",
    "The climatological baseline uses the reference period {y1} to {y2}. Anomalies are the "
    "onset day-of-year minus the baseline median, so a positive value indicates a late start.",
    "Profile parameters are fixed by the registered template and do not change at run time. "
    "A plan that alters the window length, the threshold or the look-ahead period does not "
    "conform to profile onset_rolling_v1.",
    "Ensemble forecasts are outside the scope of this procedure. Hindcast verification with "
    "lead times of {lead} days is described in a separate guide and plays no part in the "
    "onset date.",
)
EXAMPLE = (
    "Worked example {n}: at station {st} the window ending on {date} totals {tot} mm over {w} "
    "days with {rd} rain days, so the threshold of {mm} mm is {met}. {tail}"
)


def _fields(rng):
    month = rng.randint(3, 6)
    return {
        "w": PROFILE["window_days"],
        "w1": PROFILE["window_days"] - 1,
        "mm": PROFILE["threshold_mm"],
        "k": PROFILE["min_rain_days"],
        "r": PROFILE["rain_day_mm"],
        "f": PROFILE["lookahead_days"],
        "d": PROFILE["dry_spell_days"],
        "st": f"ST-{rng.randint(1, 240):03d}",
        "trace": rng.choice((0.1, 0.2, 0.5)),
        "season": rng.choice(("main", "short")),
        "d1": f"2026-{month:02d}-01",
        "d2": f"2026-{month + 4:02d}-{rng.randint(10, 28):02d}",
        "miss": rng.randint(0, 2),
        "flag": rng.choice(("M1", "M2", "W1")),
        "bw": rng.choice(("0.25", "0.5", "0.75")),
        "dp": rng.randint(1, 2),
        "y1": rng.choice((1981, 1991)),
        "y2": rng.choice((2010, 2020)),
        "lead": rng.choice((10, 15, 30)),
    }


def _example(rng, n):
    f = _fields(rng)
    total = round(rng.uniform(4.0, 48.0), 1)
    rain_days = rng.randint(0, PROFILE["window_days"])
    met = total >= PROFILE["threshold_mm"] and rain_days >= PROFILE["min_rain_days"]
    if not met:
        tail = "The day is not a candidate and the search moves to the next day."
    elif rng.random() < 0.5:
        tail = (
            f"The {f['f']}-day look-ahead holds no run of {f['d']} dry days, so this day is "
            "the onset date."
        )
    else:
        tail = (
            f"The look-ahead holds a run of {f['d']} dry days, so the candidate is a false "
            "start and the search resumes on the following day."
        )
    date = f"2026-{rng.randint(4, 7):02d}-{rng.randint(1, 28):02d}"
    return EXAMPLE.format(
        n=n, date=date, tot=total, rd=rain_days, met="met" if met else "not met", tail=tail, **f
    )


def documentation_text():
    """The shared documentation: ~55 KB of deterministic generated technical prose (headings,
    parameter descriptions, worked examples) about the onset procedure. No double quotes or
    backslashes, so its serialized size is its length plus two."""
    rng = random.Random(SEED)
    parts = [TITLE, INTRO]
    section, example = 0, 0
    while len("\n\n".join(parts).encode("utf-8")) < DOC_TARGET_BYTES:
        title = SECTION_TITLES[section % len(SECTION_TITLES)]
        section += 1
        parts.append(f"## {section}. {title}")
        for _ in range(rng.randint(3, 5)):
            if title == "Worked examples" or rng.random() < 0.25:
                example += 1
                parts.append(_example(rng, example))
            else:
                parts.append(rng.choice(PARAGRAPHS).format(**_fields(rng)))
    return "\n\n".join(parts)


def notes_text(seed, target_bytes):
    """Shorter generated prose of the same kind, for the request and claim-check notes."""
    rng = random.Random(seed)
    parts = []
    while len(" ".join(parts).encode("utf-8")) < target_bytes:
        parts.append(rng.choice(PARAGRAPHS).format(**_fields(rng)))
    return " ".join(parts)


def text_hash(text):  # review_evidence.text_hash (that module is not needed in the container)
    return "sha256:" + hashlib.sha256(text.encode("utf-8")).hexdigest()


# ------------------------------------------------------------------ payload


def make_nonce():
    return secrets.token_hex(16)


def binding_for(identities, nonce):
    """The run's binding: a digest over the presented identities plus a per-run nonce, in the
    controller's bare 64-hex ``contracts.digest`` form. The nonce never enters the payload."""
    return digest({**identities, "run_nonce": nonce})


def build_payload(nonce):
    """A synthetic reviewer payload in the real key order (runner.py ``review_payload``) and
    the real envelope / claim-check shapes, except that the binding is only inside the review
    evidence envelope, which the claim-check record also carries, so it reaches the model only
    through a ``$shared`` reference."""
    doc = documentation_text()
    doc_hash = text_hash(doc)
    source_id = "documentation:" + DOC_NAME
    goal = {
        "task": "onset_date",
        "profile": PROFILE["profile_id"],
        "station": "ST-017",
        "season": "main",
        "dataset": "synthetic-station-series",
    }
    request = {
        "text": "Report the onset date of the main rainy season at station ST-017 under "
        "profile onset_rolling_v1, with the output table described in the guide.",
        "speaker": "synthetic analyst",
        "constraints": [
            {"speaker": "synthetic analyst", "text": "Use the registered profile unchanged."},
            {"speaker": "synthetic analyst", "text": "Report dates in ISO 8601 format."},
        ],
        "notes": notes_text(SEED + 1, 5_200),
    }
    plan = {
        "profile": PROFILE["profile_id"],
        "parameters": {k: v for k, v in PROFILE.items() if k != "profile_id"},
        "steps": [
            {"op": "load_series", "artifact": "synthetic-series-001"},
            {"op": "rolling_sum", "window_days": PROFILE["window_days"]},
            {
                "op": "onset_search",
                "threshold_mm": PROFILE["threshold_mm"],
                "min_rain_days": PROFILE["min_rain_days"],
                "lookahead_days": PROFILE["lookahead_days"],
                "dry_spell_days": PROFILE["dry_spell_days"],
            },
            {"op": "write_table", "artifact": "onset-table"},
        ],
    }
    claims = [
        {
            "claim_id": "c1",
            "text": "The plan reports the first day whose rolling window total reaches the "
            "profile threshold with no false start after it.",
            "citations": [
                {"source_id": source_id, "source_hash": doc_hash, "quote": CITED_SENTENCE}
            ],
            "derivable": False,
        }
    ]
    source = {"id": "synthetic-series-001", "kind": "generated", "rows": 365, "unit": "mm/day"}
    identities = {
        "request_hash": digest(request),
        "goal_hash": digest(goal),
        "plan_hash": plan_version_hash(plan, claims),
        "manifest_hash": digest(plan["steps"]),
        "source_hash": digest(source),
    }
    evidence = {
        "schema": ENVELOPE_SCHEMA,
        **identities,
        "binding_hash": binding_for(identities, nonce),
        "plan_claims": claims,
        "sources": [{"source_id": source_id, "source_hash": doc_hash, "text": doc}],
        "unsupported": ["claim_entailment"],
        "capability": "PRE_REVIEW_CLAIM_GATES",
    }
    claim_check = {
        "index": 0,
        "plan_hash": identities["plan_hash"],
        "manifest_hash": identities["manifest_hash"],
        "source_hash": identities["source_hash"],
        "citation_gate": "PASS",
        "evidence_checker_occurrence": 1,
        "claims": [
            {
                "claim_id": "c1",
                "text": claims[0]["text"],
                "citations": claims[0]["citations"],
                "derivable": False,
                "basis": "checker",
                "citation_verdict": "PASS",
                "checker_verdict": "SUPPORTED",
                "computed": None,
                "verdict": "SUPPORTED",
            }
        ],
        "decision": "PRESENT_FOR_REVIEW",
        "reviewer_occurrence": 1,
        "detail": notes_text(SEED + 2, 1_500),
        PRESENTED_KEY: evidence,
    }
    return {
        "execution_context": {
            "classification": "Not Sensitive (generated text)",
            "source_origin": "synthetic",
            "scenario_dataset": goal["dataset"],
        },
        "request": request,
        "plan": plan,
        "source": source,
        "source_gate": {"status": "PASS", "checked": ["schema", "units", "date range"]},
        "documentation": {DOC_NAME: doc},
        "date_contract": {"start": "2026-03-01", "end": "2026-10-31", "calendar": "gregorian"},
        "preflight": "actual parsers accepted",
        "prior_review": None,
        "repair_response": None,
        "controller_gates": "Mandatory onset check then strict final data replay for every "
        "declared final artifact; no source refetch.",
        "review_evidence": evidence,
        "claim_check": claim_check,
    }


def payload_binding(payload):
    """The binding the reviewer must return: the envelope's, the payload's only copy."""
    return payload["review_evidence"]["binding_hash"]


def _strings(node):
    if isinstance(node, str):
        yield node
    elif isinstance(node, dict):
        for value in node.values():
            yield from _strings(value)
    elif isinstance(node, list):
        for value in node:
            yield from _strings(value)


def instruction_terms(payload):
    """Sorted ``INSTRUCTION_TERMS`` found (whole words, any case) in any string VALUE."""
    found = set()
    for text in _strings(payload):
        low = text.lower()
        for term in INSTRUCTION_TERMS:
            if re.search(r"\b" + re.escape(term) + r"\b", low):
                found.add(term)
    return sorted(found)


def ref_paths(node, ref, path=""):
    """Dotted paths in an encoded value whose value is the reference ``{"$shared": ref}``."""
    if isinstance(node, dict):
        if node == {SHARED_KEY: ref}:
            return [path]
        return [p for k, v in node.items() for p in ref_paths(v, ref, f"{path}.{k}".lstrip("."))]
    if isinstance(node, list):
        return [p for i, v in enumerate(node) for p in ref_paths(v, ref, f"{path}[{i}]")]
    return []


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


def predict(payload):
    """What RelayRoles will send and the relay will render, computed with the production code,
    plus where the binding the reviewer must return occurs."""
    role_prompt, render_model_input = _prompt_and_render()
    binding = payload_binding(payload)
    logical = json.dumps({"role": ROLE, "payload": payload}, allow_nan=False)
    encoded = dedup_encode(payload)
    if encoded is None:
        body, user, shared = logical, json.dumps(payload), {}
    else:
        body = json.dumps({"role": ROLE, **encoded}, allow_nan=False)
        shared = encoded["shared"]
        user = render_model_input(encoded["payload"], shared)
    role_input_value = encoded["payload"] if encoded else payload
    role_input = json.dumps(role_input_value)
    system = role_prompt(ROLE, payload)
    shared_texts = {ref: json.dumps(v) for ref, v in shared.items()}
    holders = [ref for ref, text in shared_texts.items() if binding in text]
    holder = holders[0] if len(holders) == 1 else None
    return {
        "transport_encoding": None if encoded is None else DEDUP_ENCODING,
        "logical_body_bytes": len(logical.encode("utf-8")),
        "payload_bytes": len(json.dumps(payload).encode("utf-8")),
        "request_body_bytes": len(body.encode("utf-8")),
        "request_body_cap": DEFAULT_MAX_BODY,
        "shared_blocks": len(shared),
        "shared_bytes": sum(len(t.encode("utf-8")) for t in shared_texts.values()),
        "model_facing_bytes": len(user.encode("utf-8")),
        "model_facing_hash": "sha256:" + hashlib.sha256(user.encode("utf-8")).hexdigest(),
        "system_prompt_hash": "sha256:" + hashlib.sha256(system.encode("utf-8")).hexdigest(),
        "binding_top_level": "binding_hash" in payload,
        "binding_in_role_input_section": binding in role_input,
        "binding_in_shared_blocks": sum(t.count(binding) for t in shared_texts.values()),
        "binding_in_request_body": body.count(binding),
        "binding_in_model_facing": user.count(binding),
        "binding_block_referenced_at": ref_paths(role_input_value, holder) if holder else [],
        "binding_block_bytes": len(shared_texts[holder].encode("utf-8")) if holder else None,
        "reference_count_in_role_input": role_input.count('{"' + SHARED_KEY + '": '),
        "instruction_terms_checked": list(INSTRUCTION_TERMS),
        "instruction_terms_in_payload": instruction_terms(payload),
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
    payload = build_payload(make_nonce())
    prediction = predict(payload)
    system, user = prediction.pop("_system"), prediction.pop("_user")
    out.update(binding_hash=payload_binding(payload), prediction=prediction)
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
