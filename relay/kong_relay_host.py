"""Host-side Kong relay for containerised controller runs (one relay process = one run).

Owner ruling ``container_route_ruling_2026-10-13.live_roles``: live role calls go from the
container to this relay; the Kong key never enters the container.

Key isolation: this process never reads a key or a ``.env``. Each forwarded call goes through
the existing ``tools.workflow.kong_roles.KongRoles``, which launches the Kong kit worker under
the kit interpreter; the kit loads the consumer key into *that* worker process only and hands
back a key-free envelope. The container authenticates to this relay with a per-run random
token generated here (``secrets.token_urlsafe``). That token is not a Kong credential.

Controls:
- binds 127.0.0.1 by default; extra ``--bind`` addresses must be specific (no wildcard).
  On Rancher Desktop (networking tunnel) ``host.docker.internal`` resolves inside containers
  to 192.168.127.254, which the host-switch forwards to the host's loopback, so 127.0.0.1 is
  both sufficient and the narrowest bind.
- every request needs ``Authorization: Bearer <run token>`` and ``X-Simkit-Run: <run id>``;
- role allow-list (explicit ``--allow-role``, each must be a KongRoles prompt role);
- request body cap (default 88,000 bytes, hard max 90,000: under the Kong kit's
  KNOWN_GOOD_BODY_BYTES 97,600, the largest body observed to pass; the kit worker re-checks
  the full gateway body, system prompt included, before anything is sent);
- transport encoding ``dedup-sha256/1`` (``tools/workflow/relay_roles.py``): the cap applies to
  the ENCODED body. The relay decodes and verifies it (``dedup_decode``: every shared value
  hashes to its reference, every reference resolves, and the decoded payload matches both
  declared original hashes) or refuses with ``reason_code: dedup_mismatch`` (DETERMINISTIC,
  ``sent_to_gateway: false``). KongRoles gets the DECODED payload, so ``input_hash``,
  ``stdin_hash``, the system prompt and the reviewer ``dispatch`` binding are those of the
  controller payload; only the user message is the model-facing rendering
  (``render_model_input``: the role input JSON with references kept, then each shared value
  exactly once, after a preamble that explains references). The system prompt is NOT changed,
  so the controller's review binding still holds. The receipt and the audit ask line record
  ``transport_encoding`` and ``transport_bytes`` (request body, payload, model-facing sizes
  and the model-facing hash). Every ask line also records ``settings_sent`` / ``sent_keys``
  (temperature included) as the worker echoed them;
- one request in flight per run (a concurrent request is refused, never queued);
- spend guard (``SpendGuard``, LIVE-WAVE-BRIEF "Spend guard"): per-run ``--max-requests``,
  ``--max-total-tokens`` and a priced ``--budget`` (USD, list-price estimate). A call that
  could exceed a cap at its WORST case is refused BEFORE forwarding (``sent_to_gateway:
  false``, ``reason_code: relay_spend_cap``); observed usage is charged after each call and an
  UNKNOWN outcome is charged at its worst case. The first cap hit makes the guard EXHAUSTED
  (sticky), writes one ``spend_cap_reached`` audit event and stops the relay;
- no retries: one relay request is at most one KongRoles call, and the worker runs with
  ``KONG_SDK_MAX_RETRIES=0``;
- append-only JSONL audit log with redacted metadata only (status, via, request id, model,
  usage, sent settings, hashes, sizes, spend totals, fault labels). Never the payload, the answer text, the
  run token or any key.

Two clients (``--client``), same KongRoles worker, receipt, redaction and UNKNOWN semantics:

- ``kit`` (default, laptop): run under the Kong kit's interpreter; the kit worker loads the key
  from the kit's own ``.env`` into the worker only::

    <kit>/.venv/Scripts/python.exe -B relay/kong_relay_host.py --ack-not-sensitive \
        --allow-role verifier --audit-log <dir>/relay-audit.jsonl \
        --token-file <dir>/relay.token --ready-file <dir>/relay-ready.json

- ``runner`` (GitHub runner, no kit): the worker imports
  ``relay/runner_kit/scripts/kong_client.py`` under this interpreter (pinned SDK:
  ``relay/runner_kit/requirements.txt``) and reads ``KONG_ANTHROPIC_KEY`` from the environment
  this relay was started with (a workflow secret scoped to the relay's step). This process only
  checks that the variable is present; it never reads, stores or logs its value. The worker
  inherits it.

Data policy: only Not Sensitive, synthetic content may be relayed (``--ack-not-sensitive``).
"""

import argparse
import hmac
import ipaddress
import json
import math
import os
import secrets
import sys
import tempfile
import threading
import time
import uuid
from datetime import UTC, datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from tools.workflow.agents import PROMPTS, role_prompt  # noqa: E402
from tools.workflow.contracts import Refused  # noqa: E402
from tools.workflow.kong_roles import (  # noqa: E402
    DEFAULT_MAX_TOKENS,
    KIT_PYTHON,
    LIVE_ROLE_MAX_TOKENS,
    MIN_HEADROOM_USD,
    PINNED_PRICES,
    KongOutcomeUnknown,
    KongRoles,
)
from tools.workflow.relay_roles import (  # noqa: E402
    ASK_PATH,
    DEDUP_ENCODING,
    DEDUP_MISMATCH,
    DEFAULT_MAX_BODY,
    ENCODED_KEYS,
    HEALTH_PATH,
    PLAIN_KEYS,
    SHARED_KEY,
    SPEND_CAP_CODE,
    UNKNOWN_EVIDENCE,
    DedupMismatch,
    classify_fault,
    dedup_decode,
)
from tools.workflow.review_evidence import text_hash  # noqa: E402

RUNNER_KIT = ROOT / "relay" / "runner_kit"
RUNNER_SDK = {"anthropic": "1.13.0"}  # relay/runner_kit/requirements.txt
KEY_ENV = "KONG_ANTHROPIC_KEY"
HARD_MAX_BODY = 90_000  # under the kit's KNOWN_GOOD_BODY_BYTES (97,600) with prompt headroom
SCHEMA = "simkit-kong-relay-audit/1"
# Worst-case input bound: a byte-level tokenizer emits at most one token per UTF-8 byte of
# text, plus the message framing (role markers, system block). 64 is a generous framing bound.
INPUT_FRAMING_TOKENS = 64
# Every opt-in prompt suffix at once: the longest system prompt a role can be sent.
_ALL_SUFFIXES = {
    "review_evidence_request": True,
    "review_evidence": {"rationale": True},
    "claim_check": True,
}
# Cap defaults (env first, then these); container_harness/relay_ci.sh passes all three.
CAP_ENV = {
    "max_requests": ("RELAY_MAX_REQUESTS", 24),  # LIVE-WAVE-BRIEF: min(24, max(8, 2 x expected))
    "max_total_tokens": ("RELAY_MAX_TOTAL_TOKENS", 1_000_000),
    "budget": ("RELAY_BUDGET", 5.0),  # the frozen per-case max_model_usd
}
RECEIPT_AUDIT = (
    "invocation_id",
    "session_id",
    "input_hash",
    "output_hash",
    "cost_usd",
    "cost_basis",
    "model_requested",
    "model_observed",
    "model_match",
    "usage",
    "stop_reason",
    "response_id",
    "request_id",
    "via",
    "latency_ms",
    "gateway_mock",
)


def _now():
    return datetime.now(UTC).isoformat()


def _tokens(value):
    return value if type(value) is int and value >= 0 else None


class AuditLog:
    """Append-only JSONL; one flushed line per event. Callers pass redacted fields only."""

    def __init__(self, path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.lock = threading.Lock()

    def write(self, event, **fields):
        line = json.dumps({"schema": SCHEMA, "ts": _now(), "event": event, **fields})
        with self.lock, self.path.open("a", encoding="utf-8") as handle:
            handle.write(line + "\n")
            handle.flush()


class SpendGuard:
    """Per-run spend caps, enforced before forwarding; observed usage charged after.

    ``requests`` counts calls that may have reached the gateway (a KongRoles refusal that
    provably sent nothing is not counted). Token totals are observed usage; a call without
    observed usage (UNKNOWN) is charged its worst case. USD is an Anthropic list-price
    ESTIMATE (``PINNED_PRICES``), never an observed Kong bill. A call whose model has no price
    is charged the whole remaining budget (fail closed)."""

    OPEN, EXHAUSTED = "OPEN", "EXHAUSTED"

    def __init__(self, *, budget_usd, max_requests=None, max_total_tokens=None):
        if not (type(budget_usd) in (int, float) and math.isfinite(budget_usd) and budget_usd > 0):
            raise ValueError("budget_usd must be a positive finite number")
        for name, cap in (("max_requests", max_requests), ("max_total_tokens", max_total_tokens)):
            if cap is not None and (type(cap) is not int or cap < 1):
                raise ValueError(f"{name} must be a positive integer or None")
        self.budget_usd = float(budget_usd)
        self.max_requests = max_requests
        self.max_total_tokens = max_total_tokens
        self.state = self.OPEN
        self.exhausted_by = None
        self.requests = 0
        self.input_tokens = 0
        self.output_tokens = 0
        self.charged_tokens = 0  # observed tokens + worst-case charges
        self.estimated_usd = 0.0
        self.worst_case_charges = 0
        self.refused_calls = 0

    def caps(self):
        return {
            "max_requests": self.max_requests,
            "max_total_tokens": self.max_total_tokens,
            "budget_usd": self.budget_usd,
        }

    def snapshot(self):
        return {
            "state": self.state,
            "exhausted_by": self.exhausted_by,
            "requests": self.requests,
            "input_tokens": self.input_tokens,
            "output_tokens": self.output_tokens,
            "charged_tokens": self.charged_tokens,
            "estimated_usd": round(self.estimated_usd, 6),
            "cost_basis": "Anthropic list-price estimate; Kong billing not observed",
            "worst_case_charges": self.worst_case_charges,
            "refused_by_cap": self.refused_calls,
            "caps": self.caps(),
        }

    def admit(self, worst_tokens, worst_usd, roles_headroom_usd):
        """The cap this call would exceed at its worst case, or None to admit it."""
        if self.state == self.EXHAUSTED:
            return self.exhausted_by
        if self.max_requests is not None and self.requests + 1 > self.max_requests:
            return "max_requests"
        if (
            self.max_total_tokens is not None
            and self.charged_tokens + worst_tokens > self.max_total_tokens
        ):
            return "max_total_tokens"
        need = MIN_HEADROOM_USD if worst_usd is None else max(worst_usd, MIN_HEADROOM_USD)
        if self.estimated_usd + need > self.budget_usd or roles_headroom_usd < need:
            return "budget"
        return None

    def exhaust(self, cap):
        """Go EXHAUSTED (sticky). True only on the first cap hit."""
        self.refused_calls += 1
        if self.state == self.EXHAUSTED:
            return False
        self.state, self.exhausted_by = self.EXHAUSTED, cap
        return True

    def charge(self, *, sent, usage, cost_usd, worst_tokens, worst_usd):
        """Charge one call. ``sent`` False: nothing left, nothing charged. Returns the charge."""
        if sent is False:
            return {"charged_usd": 0.0, "charged_tokens": 0, "basis": "not_sent"}
        self.requests += 1
        usage = usage if isinstance(usage, dict) else {}
        observed = [
            _tokens(usage.get(k))
            for k in (
                "input_tokens",
                "output_tokens",
                "cache_creation_input_tokens",
                "cache_read_input_tokens",
            )
        ]
        if observed[0] is not None and observed[1] is not None:
            tokens = sum(t or 0 for t in observed)
            self.input_tokens += observed[0] + (observed[2] or 0) + (observed[3] or 0)
            self.output_tokens += observed[1]
            token_basis = "observed"
        else:
            tokens = worst_tokens
            token_basis = "worst_case"
        if type(cost_usd) in (int, float) and math.isfinite(cost_usd) and cost_usd >= 0:
            usd, usd_basis = float(cost_usd), "observed_usage_priced"
        elif worst_usd is not None and token_basis == "worst_case":
            usd, usd_basis = worst_usd, "worst_case"
        else:  # observed but unpriced (or no price at all): fail closed
            usd, usd_basis = max(0.0, self.budget_usd - self.estimated_usd), "unpriced_remaining"
        if "worst_case" in (token_basis, usd_basis):
            self.worst_case_charges += 1
        self.charged_tokens += tokens
        self.estimated_usd += usd
        if self.max_total_tokens is not None and self.charged_tokens > self.max_total_tokens:
            self.exhaust("max_total_tokens")
        elif self.estimated_usd > self.budget_usd:
            self.exhaust("budget")
        return {
            "charged_usd": round(usd, 6),
            "charged_tokens": tokens,
            "basis": usd_basis if usd_basis != "observed_usage_priced" else token_basis,
        }


MODEL_INPUT_PREAMBLE = (
    "TRANSPORT ENCODING " + DEDUP_ENCODING + ". This message is your complete role input. "
    "To fit the gateway's request size, every large JSON value that occurred more than once "
    "in it is written ONCE, in the SHARED DOCUMENTS section below, and each place it "
    'occurred holds a reference object {"' + SHARED_KEY + '": "<sha256>"}. Read every '
    "reference exactly as if the referenced JSON value were written in full at that "
    "position; it is the same value at every position that references it. Nothing was "
    "removed, shortened or summarised. Each shared block is the JSON value itself (a JSON "
    "string literal for text) and may contain further references. Your answer rules are "
    "unchanged."
)


def render_model_input(encoded_payload, shared):
    """The model-facing user message for a verified ``dedup-sha256/1`` request: the role
    input JSON with references kept, then each shared value exactly once. Deterministic."""
    parts = [
        MODEL_INPUT_PREAMBLE,
        "",
        "=== ROLE INPUT (JSON) ===",
        json.dumps(encoded_payload),
        "=== END ROLE INPUT ===",
        "",
        f"=== SHARED DOCUMENTS (referenced by {SHARED_KEY} sha256; {len(shared)} blocks) ===",
    ]
    for ref, value in shared.items():
        parts += [
            f"--- {SHARED_KEY} {ref} ---",
            json.dumps(value),
            f"--- end {SHARED_KEY} {ref} ---",
        ]
    parts.append("=== END SHARED DOCUMENTS ===")
    return "\n".join(parts)


def wire_view(request, body, payload):
    """How the payload travelled: (transport_encoding, model_input or None, transport_bytes).
    ``request`` is already verified (``dedup_decode``) when it is encoded."""
    logical = json.dumps(payload)
    if set(request) == PLAIN_KEYS:
        encoding, model_input, user, shared = None, None, logical, {}
    else:
        encoding, shared = DEDUP_ENCODING, request["shared"]
        model_input = user = render_model_input(request["payload"], shared)
    sizes = {
        "request_body_bytes": len(body),
        "payload_bytes": len(logical.encode("utf-8")),
        "model_facing_bytes": len(user.encode("utf-8")),
        "model_facing_hash": text_hash(user),
        "shared_blocks": len(shared),
        "shared_bytes": sum(len(json.dumps(v).encode("utf-8")) for v in shared.values()),
    }
    return encoding, model_input, sizes


def worst_case(roles, role, payload, user=None):
    """Upper bound of one call: (tokens, usd or None when the requested model is unpriced).
    ``user``: the model-facing user message when it is not ``json.dumps(payload)``."""
    model = roles.models.get(role, roles.model)
    max_tokens = roles.max_tokens_for(role)
    prompt = role_prompt(role, _ALL_SUFFIXES)  # the longest system prompt this role can get
    user = json.dumps(payload) if user is None else user
    tokens_in = len(prompt.encode("utf-8")) + len(user.encode("utf-8")) + INPUT_FRAMING_TOKENS
    entry = roles.prices.get(model)
    usd = None
    if entry is not None:
        usd = (tokens_in * entry["input_per_mtok"] + max_tokens * entry["output_per_mtok"]) / 1e6
    return {"tokens": tokens_in + max_tokens, "usd": usd, "max_tokens": max_tokens}


class Relay:
    """Transport-free relay logic: authenticate, admit, guard spend, forward once, audit."""

    def __init__(
        self,
        roles,
        *,
        run_id,
        token,
        allowed_roles,
        audit,
        max_body=DEFAULT_MAX_BODY,
        guard=None,
    ):
        if not isinstance(token, str) or len(token) < 32:
            raise ValueError("run token must be at least 32 characters")
        allowed = frozenset(allowed_roles)
        if not allowed or not allowed <= set(PROMPTS):
            raise ValueError(f"allowed roles must be a non-empty subset of {sorted(PROMPTS)}")
        if type(max_body) is not int or not 1 <= max_body <= HARD_MAX_BODY:
            raise ValueError(f"max_body must be an integer in 1..{HARD_MAX_BODY}")
        self.roles = roles
        self.run_id = run_id
        self._token = token
        self.allowed_roles = allowed
        self.audit = audit
        self.max_body = max_body
        # Without explicit caps the roles' own budget is still enforced as the priced cap.
        self.guard = guard or SpendGuard(budget_usd=getattr(roles, "budget", 5))
        self.in_flight = threading.Lock()
        self.asks = 0
        self.asks_done = threading.Event()
        self.max_asks = None

    def authenticate(self, headers):
        presented = headers.get("Authorization") or ""
        scheme, _, value = presented.partition(" ")
        token_ok = scheme == "Bearer" and hmac.compare_digest(
            value.encode("utf-8", "replace"), self._token.encode()
        )
        run_ok = hmac.compare_digest(
            (headers.get("X-Simkit-Run") or "").encode("utf-8", "replace"), self.run_id.encode()
        )
        return token_ok and run_ok

    def health(self):
        return 200, {
            "outcome": "OK",
            "run_id": self.run_id,
            "allowed_roles": sorted(self.allowed_roles),
            "model": getattr(self.roles, "model", None),
            "max_body_bytes": self.max_body,
            "in_flight": self.in_flight.locked(),
            "spend": self.guard.snapshot(),
        }

    def refuse(self, status, relay_id, reason, remote, event="ask", reason_code=None, **fields):
        relay_id = relay_id or str(uuid.uuid4())  # every definite refusal is identifiable
        self.audit.write(
            event,
            run_id=self.run_id,
            relay_id=relay_id,
            remote=remote,
            http_status=status,
            outcome="REFUSED",
            sent_to_gateway=False,
            reason=reason,
            reason_code=reason_code,
            **classify_fault(kind=reason_code or "relay_refused"),
            **fields,
        )
        return status, {
            "outcome": "REFUSED",
            "relay_id": relay_id,
            "sent_to_gateway": False,
            "billed_usd": None,
            "reason": reason,
            "reason_code": reason_code,
        }

    def ask(self, body, remote):
        relay_id = str(uuid.uuid4())
        size = {"body_bytes": len(body)}
        try:
            request = json.loads(body)
            if not isinstance(request, dict) or set(request) not in (PLAIN_KEYS, ENCODED_KEYS):
                raise ValueError("body must be exactly {role, payload} or its encoded form")
        except ValueError:
            return self.refuse(
                400, relay_id, "malformed relay request", remote, reason_code="malformed", **size
            )
        role = request["role"]
        if not isinstance(role, str) or role not in self.allowed_roles:
            # Name the role only when it is a known prompt role: never echo arbitrary input.
            shown = role if role in PROMPTS else None
            return self.refuse(
                403,
                relay_id,
                "role not allowed",
                remote,
                reason_code="role_not_allowed",
                role=shown,
                **size,
            )
        payload = request["payload"]
        if set(request) == ENCODED_KEYS:
            try:
                payload = dedup_decode(request)
            except DedupMismatch as exc:
                # Deterministic and nothing forwarded: the body is not the payload it declares.
                return self.refuse(
                    400,
                    relay_id,
                    f"transport encoding does not verify: {exc}",
                    remote,
                    reason_code=DEDUP_MISMATCH,
                    role=role,
                    transport_encoding=request.get("transport_encoding")
                    if request.get("transport_encoding") == DEDUP_ENCODING
                    else None,
                    **size,
                )
        if not self.in_flight.acquire(blocking=False):
            return self.refuse(
                409,
                relay_id,
                "a request is already in flight",
                remote,
                reason_code="busy",
                role=role,
            )
        try:
            return self._forward(
                relay_id, role, payload, remote, size, wire_view(request, body, payload)
            )
        finally:
            self.asks += 1
            self.in_flight.release()

    def responded(self):
        """Called after a response is fully written: stop once max_asks were forwarded or the
        spend guard is EXHAUSTED."""
        if self.guard.state == SpendGuard.EXHAUSTED or (
            self.max_asks is not None and self.asks >= self.max_asks
        ):
            self.asks_done.set()

    def _cap_refusal(self, relay_id, role, cap, remote, size, worst):
        first = self.guard.exhaust(cap)
        if first:
            self.audit.write(
                "spend_cap_reached",
                kind="spend_cap_reached",
                run_id=self.run_id,
                relay_id=relay_id,
                role=role,
                cap=cap,
                worst_case_tokens=worst["tokens"],
                worst_case_usd=worst["usd"],
                spend=self.guard.snapshot(),
            )
        reason = f"spend cap {cap} reached; nothing forwarded"
        return self.refuse(
            402,
            relay_id,
            reason,
            remote,
            reason_code=SPEND_CAP_CODE,
            role=role,
            cap=cap,
            spend=self.guard.snapshot(),
            **size,
        )

    def _charge(self, worst, fallback_sent):
        last = getattr(self.roles, "last_call", None)
        last = last if isinstance(last, dict) else {}
        sent = last.get("sent", fallback_sent)
        if sent not in (True, False, None):
            sent = None
        charge = self.guard.charge(
            sent=sent,
            usage=last.get("usage"),
            cost_usd=last.get("cost_usd"),
            worst_tokens=worst["tokens"],
            worst_usd=worst["usd"],
        )
        return sent, last, charge

    def _sent_settings(self):
        """The non-content settings the worker echoed it sent (temperature included), so a
        temperature-0 run is provable per call; null when no request body was built."""
        last = getattr(self.roles, "last_call", None)
        last = last if isinstance(last, dict) else {}
        return {
            "temperature_requested": getattr(self.roles, "temperature", None),
            "settings_sent": last.get("settings_sent"),
            "sent_keys": last.get("sent_keys"),
        }

    def _forward(self, relay_id, role, payload, remote, size, wire):
        spent_before = self.roles.spent
        encoding, model_input, transport_bytes = wire
        base = {
            "run_id": self.run_id,
            "relay_id": relay_id,
            "remote": remote,
            "role": role,
            "transport_encoding": encoding,
            "transport_bytes": transport_bytes,
        }
        try:
            worst = worst_case(self.roles, role, payload, model_input)
        except (TypeError, ValueError):
            return self.refuse(
                400, relay_id, "payload is not JSON", remote, reason_code="malformed", **size
            )
        cap = self.guard.admit(worst["tokens"], worst["usd"], self.roles.budget - spent_before)
        if cap is not None:
            return self._cap_refusal(relay_id, role, cap, remote, size, worst)
        started = time.monotonic()
        try:
            if model_input is None:
                value = self.roles.ask(role, payload)
            else:
                value = self.roles.ask(role, payload, model_input=model_input)
        except KongOutcomeUnknown as exc:
            _, _, charge = self._charge(worst, None)
            evidence = {k: exc.evidence.get(k) for k in UNKNOWN_EVIDENCE}
            evidence.update(classify_fault(evidence["status"], evidence["error_type"]))
            self.audit.write(
                "ask",
                **base,
                **size,
                **self._sent_settings(),
                http_status=502,
                outcome="UNKNOWN",
                gateway_status=evidence["status"],
                invocation_id=exc.evidence.get("invocation_id"),
                wall_ms=round((time.monotonic() - started) * 1000),
                **evidence,
                charge=charge,
                spend=self.guard.snapshot(),
            )
            return 502, {"outcome": "UNKNOWN", "relay_id": relay_id, "evidence": evidence}
        except Refused as exc:
            # KongRoles restores its reservation only when provably nothing was sent.
            inferred = self.roles.spent != spent_before
            sent, last, charge = self._charge(worst, inferred)
            sent = True if sent is None else sent  # may have left: never claim it did not
            cost = last.get("cost_usd")
            billed = cost if sent and type(cost) in (int, float) else None
            reason = str(exc)
            kind = "kong_pre_send" if not sent else "invalid_answer"
            if exc.gate == "resource_guard":
                kind = "resource_guard"
            self.audit.write(
                "ask",
                **base,
                **size,
                **self._sent_settings(),
                http_status=422,
                outcome="REFUSED",
                sent_to_gateway=sent,
                billed_usd=billed,
                reason=reason,
                reason_code=kind,
                **classify_fault(kind=kind),
                wall_ms=round((time.monotonic() - started) * 1000),
                charge=charge,
                spend=self.guard.snapshot(),
            )
            return 422, {
                "outcome": "REFUSED",
                "relay_id": relay_id,
                "sent_to_gateway": sent,
                "billed_usd": billed,
                "reason": reason,
                "reason_code": kind,
            }
        except Exception as exc:  # anything else: the call may have left, never a success
            _, _, charge = self._charge(worst, None)
            evidence = {k: None for k in UNKNOWN_EVIDENCE}
            evidence.update(error_type=type(exc).__name__, reason="relay internal error")
            evidence.update(classify_fault(kind="harness_error"))
            self.audit.write(
                "ask",
                **base,
                **size,
                **self._sent_settings(),
                http_status=500,
                outcome="UNKNOWN",
                **evidence,
                charge=charge,
                spend=self.guard.snapshot(),
            )
            return 500, {"outcome": "UNKNOWN", "relay_id": relay_id, "evidence": evidence}
        _, _, charge = self._charge(worst, True)
        receipt = dict(self.roles.receipts[-1])
        receipt.update(
            relay_id=relay_id, transport_encoding=encoding, transport_bytes=transport_bytes
        )
        self.audit.write(
            "ask",
            **base,
            **size,
            **self._sent_settings(),
            http_status=200,
            outcome="SUCCESS",
            wall_ms=round((time.monotonic() - started) * 1000),
            **{k: receipt.get(k) for k in RECEIPT_AUDIT},
            charge=charge,
            spend=self.guard.snapshot(),
        )
        return 200, {"outcome": "SUCCESS", "relay_id": relay_id, "value": value, "receipt": receipt}


class Handler(BaseHTTPRequestHandler):
    relay = None  # set per server class
    server_version = "simkit-kong-relay/1"
    sys_version = ""

    def log_message(self, format, *args):  # noqa: A002 - stdlib signature
        pass  # the audit log is the only record; no default request logging

    def _send(self, status, value):
        data = json.dumps(value).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Connection", "close")
        self.end_headers()
        self.wfile.write(data)
        self.close_connection = True

    def _remote(self):
        return self.client_address[0]

    def _authorized(self):
        if self.relay.authenticate(self.headers):
            return True
        # The presented credentials are never recorded.
        self._send(
            *self.relay.refuse(
                401,
                None,
                "unauthorized",
                self._remote(),
                event="auth_rejected",
                reason_code="unauthorized",
                path=self.path,
            )
        )
        return False

    def do_GET(self):  # noqa: N802 - stdlib name
        if self.path != HEALTH_PATH:
            self._send(404, {"outcome": "REFUSED", "relay_id": None, "reason": "not found"})
            return
        if self._authorized():
            self._send(*self.relay.health())

    def do_POST(self):  # noqa: N802 - stdlib name
        relay = self.relay
        if self.path != ASK_PATH:
            self._send(404, {"outcome": "REFUSED", "relay_id": None, "reason": "not found"})
            return
        if not self._authorized():
            return
        remote = self._remote()
        if self.headers.get("Transfer-Encoding"):
            reason = "chunked bodies are not accepted"
            self._send(*relay.refuse(411, None, reason, remote, reason_code="chunked"))
            return
        if (self.headers.get("Content-Type") or "").split(";")[0].strip() != "application/json":
            reason = "content type must be JSON"
            self._send(*relay.refuse(415, None, reason, remote, reason_code="content_type"))
            return
        try:
            length = int(self.headers.get("Content-Length", ""))
        except ValueError:
            reason = "content length required"
            self._send(*relay.refuse(411, None, reason, remote, reason_code="length_required"))
            return
        if not 0 <= length <= relay.max_body:
            # Not read: the oversized body never enters this process.
            reason = f"body over the {relay.max_body}-byte cap"
            self._send(
                *relay.refuse(413, None, reason, remote, reason_code="body_cap", body_bytes=length)
            )
            return
        self._send(*relay.ask(self.rfile.read(length), remote))
        relay.responded()


def validate_binds(binds):
    out = []
    for bind in binds:
        address = ipaddress.ip_address(bind)  # raises on a name or empty string
        if address.is_unspecified:
            raise ValueError(f"wildcard bind {bind!r} is refused; name a specific interface")
        out.append(str(address))
    return out


def serve(relay, binds, port):
    """Start one server per bind address on the same port; return the servers."""
    handler = type("RelayHandler", (Handler,), {"relay": relay})
    servers = []
    try:
        for bind in validate_binds(binds):
            server = ThreadingHTTPServer((bind, port), handler)
            server.daemon_threads = True
            port = server.server_address[1]  # first bind picks the port if 0
            servers.append(server)
    except BaseException:
        for server in servers:
            server.server_close()
        raise
    for server in servers:
        threading.Thread(target=server.serve_forever, daemon=True).start()
    return servers


def write_secret_file(path, text):
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as handle:
        handle.write(text)


def client_paths(client, environ=None, executable=None):
    """(kit_root, kit_python) for ``--client``; ValueError when this host cannot serve it."""
    environ = os.environ if environ is None else environ
    executable = Path(executable or sys.executable)
    if client == "kit":
        if executable.resolve() != KIT_PYTHON.resolve():
            raise ValueError(f"run under the Kong kit interpreter ({KIT_PYTHON}), not {executable}")
        return None, None  # KongRoles defaults: the kit root and its interpreter
    if client != "runner":
        raise ValueError(f"unknown client {client!r}")
    if not environ.get(KEY_ENV):  # presence only; the value is never kept or logged
        raise ValueError(f"--client runner needs {KEY_ENV} in the relay's environment")
    from importlib.metadata import PackageNotFoundError, version

    for package, pinned in RUNNER_SDK.items():
        try:
            found = version(package)
        except PackageNotFoundError:
            found = None
        if found != pinned:
            raise ValueError(f"--client runner needs {package}=={pinned}, found {found}")
    return RUNNER_KIT, executable


def build_roles(args, kit_root=None, kit_python=None):
    from tools.workflow.processes import Processes

    scratch = Path(tempfile.mkdtemp(prefix="simkit-relay-"))
    (scratch / "proc").mkdir()
    return KongRoles(
        Processes(scratch / "proc", timeout=args.call_seconds, total_seconds=args.run_seconds),
        scratch / "ctx",
        budget=args.budget,
        model=args.model,
        max_tokens=args.max_tokens,
        temperature=args.temperature,
        prices=PINNED_PRICES,  # cited list price: every call is reconciled, not reserved whole
        role_max_tokens=args.role_max_tokens,
        session_source="response_id",  # the provider's id, observed after the call
        data_policy_ack=True,
        worker_env={"KONG_SDK_MAX_RETRIES": "0"},  # no retries, overriding any .env value
        **({"kit_root": kit_root, "kit_python": kit_python} if kit_root else {}),
    )


def build_guard(args):
    return SpendGuard(
        budget_usd=args.budget,
        max_requests=args.max_requests,
        max_total_tokens=args.max_total_tokens,
    )


def _role_tokens(text):
    role, sep, value = text.partition("=")
    if not sep or role not in PROMPTS:
        raise argparse.ArgumentTypeError(f"expected ROLE=N with ROLE in {sorted(PROMPTS)}")
    try:
        return role, int(value)
    except ValueError:
        raise argparse.ArgumentTypeError(f"max_tokens for {role} must be an integer") from None


def _temperature(text):
    if text.lower() == "none":
        return None
    value = float(text)
    if not (math.isfinite(value) and 0 <= value <= 1):
        raise argparse.ArgumentTypeError("temperature must be in 0..1 or 'none'")
    return value


def cap_defaults(environ=None):
    """Cap defaults from RELAY_MAX_REQUESTS / RELAY_MAX_TOTAL_TOKENS / RELAY_BUDGET."""
    environ = os.environ if environ is None else environ
    out = {}
    for dest, (name, default) in CAP_ENV.items():
        raw = environ.get(name)
        if raw in (None, ""):
            out[dest] = default
            continue
        try:
            out[dest] = float(raw) if dest == "budget" else int(raw)
        except ValueError:
            raise ValueError(f"{name} must be a number, got {raw!r}") from None
    return out


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.split("\n", 1)[0])
    parser.add_argument("--ack-not-sensitive", action="store_true", required=True)
    parser.add_argument("--allow-role", action="append", required=True, choices=sorted(PROMPTS))
    parser.add_argument("--bind", action="append", default=None)
    parser.add_argument("--port", type=int, default=0)
    parser.add_argument("--audit-log", type=Path, required=True)
    parser.add_argument("--token-file", type=Path, required=True)
    parser.add_argument("--ready-file", type=Path, required=True)
    parser.add_argument("--max-body", type=int, default=DEFAULT_MAX_BODY)
    try:
        caps = cap_defaults()
    except ValueError as exc:
        parser.error(str(exc))
    parser.add_argument("--max-requests", type=int, default=caps["max_requests"])
    parser.add_argument("--max-total-tokens", type=int, default=caps["max_total_tokens"])
    parser.add_argument("--budget", type=float, default=caps["budget"])
    parser.add_argument("--run-seconds", type=int, default=1800)
    parser.add_argument("--call-seconds", type=int, default=120)
    parser.add_argument("--model", default="claude-sonnet-4-6")
    parser.add_argument("--max-tokens", type=int, default=DEFAULT_MAX_TOKENS)
    parser.add_argument("--role-max-tokens", action="append", type=_role_tokens, default=None)
    parser.add_argument("--temperature", type=_temperature, default=0.0)
    parser.add_argument("--client", choices=("kit", "runner"), default="kit")
    args = parser.parse_args(argv)
    args.role_max_tokens = (
        dict(LIVE_ROLE_MAX_TOKENS) if args.role_max_tokens is None else dict(args.role_max_tokens)
    )
    try:
        kit_root, kit_python = client_paths(args.client)
        guard = build_guard(args)
    except ValueError as exc:
        parser.error(str(exc))
    run_id = "relay-run-" + uuid.uuid4().hex[:12]
    token = secrets.token_urlsafe(32)
    audit = AuditLog(args.audit_log)
    roles = build_roles(args, kit_root, kit_python)
    relay = Relay(
        roles,
        run_id=run_id,
        token=token,
        allowed_roles=args.allow_role,
        audit=audit,
        max_body=args.max_body,
        guard=guard,
    )
    relay.max_asks = args.max_requests  # stop once the request cap was answered
    binds = args.bind or ["127.0.0.1"]
    servers = serve(relay, binds, args.port)
    port = servers[0].server_address[1]
    write_secret_file(args.token_file, token)
    ready = {
        "run_id": run_id,
        "port": port,
        "binds": [s.server_address[0] for s in servers],
        "allowed_roles": sorted(relay.allowed_roles),
        "model": args.model,
        "max_body_bytes": relay.max_body,
        "max_requests": args.max_requests,
        "caps": guard.caps(),
        "max_tokens": args.max_tokens,
        "role_max_tokens": dict(roles.role_max_tokens),
        "temperature": args.temperature,
        "prices": {
            m: {k: e[k] for k in ("input_per_mtok", "output_per_mtok", "source")}
            for m, e in roles.prices.items()
        },
        "client": args.client,
        "pid": os.getpid(),
    }
    audit.write("relay_start", **ready, python=sys.version.split()[0])
    args.ready_file.write_text(json.dumps(ready), encoding="utf-8")
    print(json.dumps({"ready": ready}), flush=True)
    reason = "run_seconds elapsed"
    try:
        if relay.asks_done.wait(args.run_seconds):
            if guard.state == SpendGuard.EXHAUSTED:
                reason = f"spend cap reached: {guard.exhausted_by}"
            else:
                reason = "max_requests reached"
    except KeyboardInterrupt:
        reason = "interrupted"
    finally:
        for server in servers:
            server.shutdown()
            server.server_close()
        spend = guard.snapshot()
        audit.write(
            "relay_stop",
            run_id=run_id,
            reason=reason,
            asks=relay.asks,
            requests=spend["requests"],
            input_tokens=spend["input_tokens"],
            output_tokens=spend["output_tokens"],
            estimated_usd=spend["estimated_usd"],
            cap_state=spend["state"],
            spend=spend,
        )
    return 0


if __name__ == "__main__":
    sys.exit(main())
