"""Opt-in live role transport through the Gates Foundation Kong AI Gateway (native Anthropic).

Same ``ask(role, payload)`` contract as ``agents.Roles``; the receipt adds the identity the
API itself reports (``model_observed`` is the response ``model`` field, never the request) and
the exact non-content settings the worker sent (``settings_sent``, echoed by the worker from
the request body it built).

Shared with ``agents.Roles`` so the transports cannot drift:
- the system prompt, including every opt-in review-evidence suffix (``agents.role_prompt``);
- the output rule (``agents.parse_role_object``, recorded as ``output_normalization``):
  exactly one JSON object, optionally wrapped in exactly one ```json fence, nothing else.
  Assistant prefill was not chosen: it is unprobed on this route and the probed model
  generation is not known to accept it.

Key isolation: this process never reads a key or a ``.env``. Each call launches a short
worker under the kit's own interpreter (``python -I -c WORKER_SOURCE``) with its working
directory at the kit root; the kit's ``kong_client`` loads the consumer key into *that*
process only and the worker prints a key-free JSON envelope.

Per-role models (D16 / CP-F1). Ids may only come from ``NATIVE_ROSTER`` (what the gateway is
evidenced to serve on this route). Every role below is judgment-measured by at least one
frozen live case, so no role is downgraded by default; ``claude-sonnet-4-6`` is also the
only model PROBED on the native route. Overrides are explicit (``models=`` /
``--role-model``) and recorded in every receipt and in run provenance.

| Role | Default | Rationale |
|---|---|---|
| compiler | claude-sonnet-4-6 | Mechanical schema copy, but E02-P2 measures resistance to a post-authorization zero_fill nudge and E03-P2 to impersonated scope widening |
| planner | claude-sonnet-4-6 | Scientific judgment (rolling window, last eligible date); E06/E13 claims |
| repairer | claude-sonnet-4-6 | Must fix the genuine finding and refuse tempting scope expansion (E07-P2) |
| reviewer | claude-sonnet-4-6 | Core judgment: scope drift, stale approvals, false rationale (E04-E06) |
| evidence_checker | claude-sonnet-4-6 | Entailment of omitted confirmation clauses (E13-P2); not a simple-claim role |
| verifier | claude-sonnet-4-6 | Mostly mechanical, but E08-P2 plants a FAIL in the verifier's own evidence copy |
| router | claude-sonnet-4-6 | Shadow policy must resist a quoted plausible-but-wrong action (E09-P2) |

No cheaper model is available on this route today: the cheap candidate ``claude-haiku-4-5``
was refused by the gateway's native-route model validator in the 2026-10-13 wiring smoke
(``NATIVE_REJECTED``). A cheaper default for compiler/verifier/router needs a model the native
route accepts, a probe, and a held-out comparison on the cases named above.

Budget (D16; spend is untracked by owner ruling):
- ``budget_guard="on"`` (default): a cited price table prices the OBSERVED model; the remaining
  ceiling is reserved before launch and reconciled to the priced cost of the observed usage, so
  a priced run makes as many calls as its budget allows. An unpriced or unknown outcome keeps
  the reservation (fail closed): without a price only one call per run is possible.
  ``PINNED_PRICES`` carries the Anthropic list price for ``claude-sonnet-4-6``; every receipt
  priced from it says ``cost_basis: "Anthropic list-price estimate; Kong billing not observed"``.
- ``budget_guard="off"``: no price table may be given; nothing is reserved; every receipt
  records ``cost_usd: null`` with ``cost_basis: "untracked"``. ``spent`` therefore stays at its
  checkpointed value and is NOT a measure of Kong spend; run provenance says so
  (``provenance()``). The workflow CLI selects this for ``--roles kong``.

Settings the live relay sets (``relay/kong_relay_host.py``; the defaults here are unchanged):
- ``role_max_tokens``: per-role output ceilings (``LIVE_ROLE_MAX_TOKENS``: planner and repairer
  4096, every other role ``max_tokens``); the receipt's ``settings.max_tokens`` is the role's.
- ``temperature``: sent when not null. The pinned SDK has no typed ``temperature`` argument,
  so the worker sends it through the SDK's ``extra_body`` and ``settings_sent`` echoes exactly
  that. Temperature 0 lowers variance; it does not make a model deterministic.
- ``session_source="response_id"``: the receipt's ``session_id`` is the provider's response id
  (observed after the call), and a response without one is refused.
- ``ask(..., model_input=...)``: the relay's model-facing rendering of a deduplicated
  (``dedup-sha256/1``) request replaces the user message; nothing else changes.

Outcome semantics (mirrors ``Roles``; UNKNOWN never becomes success):
- ``Refused``: nothing was sent (unknown role, budget, policy, worker failed pre-send), or a
  response came back and is not a valid role answer (cost still reconciled when computable).
- ``KongOutcomeUnknown``: a request may have reached the gateway and no valid answer was
  observed (HTTP error, timeout, crash, unparseable worker output). With the guard on the
  budget reservation is kept, because the call may have been billed.

No retries: the worker builds a client with the kit defaults and this transport never re-asks.

Data policy: only Not Sensitive, synthetic content may be sent; the constructor requires an
explicit acknowledgement and the transport is never selected by default.
"""

import json
import math
import os
import sys
import time
import uuid
from email.utils import parsedate_to_datetime
from pathlib import Path

from .agents import OUTPUT_NORMALIZATION, PROMPTS, parse_role_object, role_prompt
from .catalog import child_environment
from .contracts import Refused, digest
from .recovery import transport_call
from .review_evidence import text_hash

KIT_ROOT = Path(
    os.environ.get(
        "KONG_KIT_ROOT", r"C:/Users/neilha/gf-agent-publish-env/.claude/skills/kong-ai-gateway"
    )
)
KIT_PYTHON = KIT_ROOT / ".venv" / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
TRANSPORT = "kong-native-anthropic"
DEFAULT_MODEL = "claude-sonnet-4-6"  # PROBED OK on /v2/anthropic/v1/messages (kit, 2026-10-06)

# Model ids the native route is OBSERVED to serve (kong-ai-gateway references/probe-log.md,
# plus this repo's completion-evidence/kong-live-probe). Nothing else is accepted. The kit's
# CODE-only native ids (ET fallback roster: claude-sonnet-5, claude-opus-4-8, claude-opus-4-7)
# are deliberately absent until a live probe shows the gateway accepts them.
NATIVE_ROSTER = {
    "claude-sonnet-4-6": "PROBED native: kit small_text PASS 2026-10-06 (run fc4dfd14); "
    "role calls SUCCESS 2026-10-13 (WIRING-RESULT.json)",
}
# Ids the native route refused live. Kept so a refusal names its evidence.
NATIVE_REJECTED = {
    "claude-haiku-4-5": "HTTP 400 'property model validation failed: matches none of the enum "
    "values' on /v2/anthropic, 2026-10-13, request f2c338128953ca05ed84677558b20f2f "
    "(WIRING-RESULT.json shape C); routable on the Unified route only",
}
ROLE_MODELS = {role: DEFAULT_MODEL for role in PROMPTS}
BUDGET_GUARDS = ("on", "off")

# USD per million tokens, keyed by the OBSERVED model id. Kong exposes no per-call cost, so
# this is an ESTIMATE at the provider's public list price, never an observed bill: ``source``
# is what every priced receipt records as ``cost_basis``. Callers may pass their own
# ``prices={model: {"input_per_mtok", "output_per_mtok", "source"}}``; an entry without a
# ``source`` is rejected. Without a price, cost_usd is null with a reason, and (guard on) the
# pre-call reservation of the remaining ceiling is kept. Rates: LIVE-WAVE-BRIEF.md (frozen
# v2.7 plan, "Receipts > Pricing", 2026-10-15): claude-sonnet-4-6 at $3 input / $15 output per
# million tokens, Anthropic list price. Cache-token rates are not pinned (price_usage refuses).
COST_BASIS = "Anthropic list-price estimate; Kong billing not observed"
PINNED_PRICES = {
    "claude-sonnet-4-6": {
        "input_per_mtok": 3.0,
        "output_per_mtok": 15.0,
        "source": COST_BASIS,
        "citation": "Anthropic public list price for claude-sonnet-4-6 ($3 / $15 per MTok), "
        "pinned by LIVE-WAVE-BRIEF.md (completion-20261012) on 2026-10-15",
    },
}
# The smallest remaining budget a call may start with (the Roles guard's floor).
MIN_HEADROOM_USD = 0.05
# Per-role output ceilings for live runs (LIVE-WAVE-BRIEF.md, sub-wave 1): the planner and the
# repairer write whole plans; every other role answers a short verdict object.
DEFAULT_MAX_TOKENS = 2048
LIVE_ROLE_MAX_TOKENS = {"planner": 4096, "repairer": 4096}
# Where the session id comes from. "uuid": minted here before the call (the historical
# default, kept for the direct ``--roles kong`` path). "response_id": the provider's own
# response id, OBSERVED after the call; a response without one is refused (live relay).
SESSION_SOURCES = ("uuid", "response_id")

# Runs under the kit interpreter. Stdlib + kit only; prints exactly one JSON object.
# "sent" is False only while no request can have left the process. "sent_settings" echoes
# every non-content field of the body actually passed to the SDK.
WORKER_SOURCE = r"""
import json, sys, time
out = {"sent": False}
try:
    request = json.loads(sys.stdin.read())
    sys.path.insert(0, sys.argv[1])
    import kong_client
    from importlib.metadata import version
    cfg = kong_client.KongConfig.from_env()
    client = kong_client.anthropic_client(cfg)
    body = {"model": request["model"], "max_tokens": request["max_tokens"],
            "system": request["system"],
            "messages": [{"role": "user", "content": request["user"]}]}
    import inspect
    if request.get("temperature") is not None:
        if "temperature" in inspect.signature(client.messages.create).parameters:
            body["temperature"] = request["temperature"]
        else:
            # The pinned SDK (anthropic-python 1.13) has no typed temperature parameter; the
            # Messages API field goes through the SDK's own extra_body passthrough.
            body["extra_body"] = {"temperature": request["temperature"]}
    kong_client.check_body_size(body)
    inspect.signature(client.messages.create).bind(**body)  # SDK argument check, pre-send
    out["sdk"] = "anthropic-python " + version("anthropic")
    out["mock"] = kong_client.is_mock_url(cfg.gateway_url)
    out["sent_settings"] = {k: v for k, v in body.items() if k not in ("system", "messages")}
    out["sent_keys"] = sorted(body)
    started = time.monotonic()
    out["sent"] = True
    try:
        message, info = kong_client.create_message(client, **body)
    except Exception as err:
        out["latency_ms"] = round((time.monotonic() - started) * 1000)
        out["error"] = kong_client.describe_error(err, (cfg.anthropic_key, cfg.unified_key))
        out["error_type"] = type(err).__name__
        try:  # a header value (seconds or a date), never content: the rerun ruling's wait
            headers = getattr(getattr(err, "response", None), "headers", None)
            out["retry_after"] = kong_client.rate_info(headers).get("retry_after")
        except Exception:
            out["retry_after"] = None
    else:
        out["latency_ms"] = round((time.monotonic() - started) * 1000)
        usage = message.usage.model_dump() if message.usage is not None else None
        out.update(ok=True, model=message.model, response_id=message.id,
                   stop_reason=message.stop_reason, usage=usage,
                   text=kong_client.text_of(message), via=info.get("via"),
                   request_id=info.get("request_id"))
except Exception as err:
    out["error_type"] = type(err).__name__
    try:
        out["error"] = kong_client.describe_error(err)
    except Exception:
        out["error"] = {"message": "worker failed before a sanitizer was available"}
print(json.dumps(out))
"""


class KongOutcomeUnknown(RuntimeError):
    """A request may have been sent and no valid answer was observed."""

    outcome = "UNKNOWN"

    def __init__(self, message, evidence=None):
        super().__init__(message)
        self.evidence = evidence or {}


def _finite(value):
    return type(value) in (int, float) and math.isfinite(value) and value >= 0


def retry_after_seconds(value, now=None):
    """A Retry-After header value as non-negative seconds (float), or None. The header is
    either delta-seconds or an HTTP date (RFC 9110 10.2.3); anything else is not observed."""
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        seconds = float(value.strip())
    except ValueError:
        try:
            when = parsedate_to_datetime(value.strip())
        except (TypeError, ValueError):
            return None
        if when is None or when.tzinfo is None:
            return None
        seconds = when.timestamp() - (time.time() if now is None else now)
    return max(0.0, seconds) if math.isfinite(seconds) else None


def price_usage(model, usage, prices):
    """Return (cost_usd, basis). Cost is None with a reason unless a cited price applies."""
    entry = prices.get(model) if isinstance(model, str) else None
    if entry is None:
        return None, f"no pinned cited price for observed model {model!r}"
    if not isinstance(usage, dict):
        return None, "usage missing from response"
    if any(usage.get(k) for k in ("cache_creation_input_tokens", "cache_read_input_tokens")):
        return None, "cache token pricing is not pinned"
    tokens = usage.get("input_tokens"), usage.get("output_tokens")
    if not all(type(t) is int and t >= 0 for t in tokens):
        return None, "usage token counts missing or invalid"
    cost = (tokens[0] * entry["input_per_mtok"] + tokens[1] * entry["output_per_mtok"]) / 1e6
    return cost, entry["source"]


def role_models(overrides=None, default=DEFAULT_MODEL):
    """The full per-role model map; ValueError on an unknown role or an unevidenced model id."""

    def check(model, where):
        if model not in NATIVE_ROSTER:
            refused = NATIVE_REJECTED.get(model)
            detail = f" (refused live: {refused})" if refused else ""
            raise ValueError(f"model {model!r} {where}is not on the observed native roster{detail}")

    check(default, "")
    models = {role: default for role in PROMPTS}
    for role, model in dict(overrides or {}).items():
        if role not in PROMPTS:
            raise ValueError(f"unknown role {role!r} in model map")
        check(model, f"for {role} ")
        models[role] = model
    return models


class KongRoles:
    def __init__(
        self,
        processes,
        workspace,
        budget=5,
        model=DEFAULT_MODEL,
        max_tokens=DEFAULT_MAX_TOKENS,
        temperature=None,
        prices=None,
        *,
        models=None,
        role_max_tokens=None,
        session_source="uuid",
        budget_guard="on",
        data_policy_ack=False,
        kit_root=KIT_ROOT,
        kit_python=None,
        worker_env=None,
    ):
        if data_policy_ack is not True:
            raise Refused("Kong transport requires acknowledging the Not Sensitive data policy")
        if budget_guard not in BUDGET_GUARDS:
            raise ValueError("budget_guard must be 'on' or 'off'")
        if prices is None:  # the pinned table prices a guarded run; an untracked one has none
            prices = PINNED_PRICES if budget_guard == "on" else {}
        prices = dict(prices)
        for name, entry in prices.items():
            if (
                not isinstance(entry, dict)
                or not isinstance(entry.get("source"), str)
                or not entry["source"].strip()
                or not all(_finite(entry.get(k)) for k in ("input_per_mtok", "output_per_mtok"))
            ):
                raise Refused(f"price for {name!r} needs finite rates and a cited source")
        if budget_guard == "off" and prices:
            raise ValueError("budget_guard 'off' means untracked spend; give no price table")
        if type(max_tokens) is not int or not 1 <= max_tokens <= 64000:
            raise Refused("max_tokens must be an integer in 1..64000")
        if temperature is not None and not (_finite(temperature) and temperature <= 1):
            raise Refused("temperature must be null or in 0..1")
        if session_source not in SESSION_SOURCES:
            raise ValueError(f"session_source must be one of {SESSION_SOURCES}")
        per_role = dict(role_max_tokens or {})
        for role, tokens in per_role.items():
            if role not in PROMPTS:
                raise ValueError(f"unknown role {role!r} in max_tokens map")
            if type(tokens) is not int or not 1 <= tokens <= 64000:
                raise ValueError(f"max_tokens for {role} must be an integer in 1..64000")
        self.processes = processes
        self.workspace = workspace
        self.budget = budget
        self.spent = 0.0
        self.model = model  # default id; Controller checkpoints and restores this field
        self.models = role_models(models, model)
        self.budget_guard = budget_guard
        self.max_tokens = max_tokens  # every role without its own entry below
        self.role_max_tokens = per_role
        self.session_source = session_source
        self.temperature = temperature
        self.prices = prices
        self.kit_root = Path(kit_root)
        self.kit_python = (
            Path(kit_python) if kit_python else (self.kit_root / KIT_PYTHON.relative_to(KIT_ROOT))
        )
        self.worker_env = dict(worker_env or {})
        self.receipts = []
        self.sessions = set()
        self.unknown_outcomes = []
        # Spend view of the latest ask, read by the host relay's spend guard: whether a request
        # can have left (True / False / None = unknown), the observed usage and priced cost.
        self.last_call = None

    def max_tokens_for(self, role):
        return self.role_max_tokens.get(role, self.max_tokens)

    def provenance(self):
        """Run-level transport configuration, recorded once per run by the workflow CLI."""
        return {
            "schema": "workflow.role-transport/1",
            "transport": TRANSPORT,
            "mode": "live",
            "data_policy": "Not Sensitive synthetic content only (acknowledged)",
            "default_model": self.model,
            "models": dict(self.models),
            "model_evidence": {m: NATIVE_ROSTER[m] for m in sorted(set(self.models.values()))},
            "max_tokens": self.max_tokens,
            "role_max_tokens": dict(self.role_max_tokens),
            "temperature": self.temperature,
            "session_source": self.session_source,
            "thinking": None,
            "retries": 0,
            "output_normalization": OUTPUT_NORMALIZATION,
            "budget_guard": self.budget_guard,
            "cost_tracking": "priced" if self.budget_guard == "on" else "untracked",
            "spent_field_semantics": (
                "reconciled priced cost plus kept reservations"
                if self.budget_guard == "on"
                else "not a measure of Kong spend: calls are untracked and reserve nothing"
            ),
            "budget_usd": self.budget,
        }

    def _environment(self):
        env = child_environment(Path(self.workspace).parent / "scratch")
        env.update(self.worker_env)
        return env

    def ask(self, role, payload, *, model_input=None):
        """``model_input``: the live relay's model-facing rendering of a ``dedup-sha256/1``
        request (``relay/kong_relay_host.py``), sent as the user message instead of
        ``json.dumps(payload)``. The relay has verified it is a lossless encoding of
        ``payload``; ``input_hash`` and ``stdin_hash`` stay those of the logical payload text
        (the controller binds them), and the relay records the model-facing hash and size."""
        self.last_call = {"role": role, "sent": False, "usage": None, "cost_usd": None}
        if model_input is not None and (not isinstance(model_input, str) or not model_input):
            raise Refused("model_input must be a non-empty string")
        if role not in PROMPTS or self.budget - self.spent < MIN_HEADROOM_USD:
            raise Refused("role unavailable or aggregate model budget exhausted")
        if not self.kit_python.is_file():
            raise Refused("Kong kit interpreter unavailable")
        guarded = self.budget_guard == "on"
        model = self.models.get(role, self.model)
        max_tokens = self.max_tokens_for(role)
        invocation = str(uuid.uuid4())
        session = str(uuid.uuid4()) if self.session_source == "uuid" else None
        self.last_call.update(invocation_id=invocation, model_requested=model)
        dispatch_context = getattr(self, "dispatch_context", None)
        if dispatch_context is not None:
            self.dispatch_observation = {
                "attempt_id": dispatch_context["attempt_id"],
                "invocation_id": invocation,
                "process_boundary": "NOT_ENTERED",
            }
        prompt = role_prompt(role, payload)
        user = json.dumps(payload)  # same serializer as Roles.ask and review_evidence.capture
        request = {
            "model": model,
            "max_tokens": max_tokens,
            "temperature": self.temperature,
            "system": prompt,
            "user": user if model_input is None else model_input,
        }
        command = [str(self.kit_python), "-I", "-c", WORKER_SOURCE, str(self.kit_root / "scripts")]
        spent_before = self.spent
        if guarded:
            # Reserve the remaining ceiling before launch, exactly as Roles does: an unpriced
            # or unknown outcome cannot create fresh budget, on resume or otherwise.
            self.spent = self.budget
        if dispatch_context is not None:
            self.dispatch_observer(dict(self.dispatch_observation))
            self.dispatch_observation["process_boundary"] = "ENTERED"
        serial_before = getattr(self.processes, "serial", None)
        self.last_call["sent"] = None  # from here a request may leave the worker
        try:
            # CAP-TRANSPORT-RETRY: a classified pre-write fault (nothing launched, so nothing
            # sent) is retried once inside this attempt; anything else stays UNKNOWN.
            result = transport_call(
                self,
                role,
                lambda: self.processes.run(
                    command, self.kit_root, self._environment(), json.dumps(request)
                ),
                invocation,
            )
        except Exception as exc:
            if isinstance(exc, Refused) and exc.gate == "resource_guard":
                self._resource_guard(role, invocation, exc, serial_before, spent_before)
            self._unknown(role, invocation, f"worker process failed: {type(exc).__name__}", {})
        if dispatch_context is not None:
            self.dispatch_observation.update(
                process_boundary="RETURNED", exit_code=result["exit_code"]
            )
        try:
            envelope = json.loads(result["stdout"])
            if not isinstance(envelope, dict) or type(envelope.get("sent")) is not bool:
                raise ValueError("not a worker envelope")
        except (ValueError, KeyError, TypeError):
            self._unknown(role, invocation, "unparseable worker output", {})
        error = envelope.get("error") if isinstance(envelope.get("error"), dict) else {}
        evidence = {
            "status": error.get("status"),
            "via": error.get("via"),
            "request_id": error.get("request_id"),
            "error_type": envelope.get("error_type"),
            "hint": error.get("hint"),
            "retry_after": retry_after_seconds(envelope.get("retry_after")),
            "model_requested": model,
        }
        # What the worker echoed it sent (temperature included), for the relay audit line.
        self.last_call.update(
            settings_sent=envelope.get("sent_settings"), sent_keys=envelope.get("sent_keys")
        )
        if not envelope["sent"]:
            self.last_call["sent"] = False
            self.spent = spent_before  # provably nothing left the worker
            raise Refused(f"{role} Kong worker failed before sending: {evidence['error_type']}")
        self.last_call.update(sent=True, status=evidence["status"])  # the worker says it sent
        if envelope.get("ok") is not True or result["exit_code"] != 0:
            self._unknown(role, invocation, "gateway call did not return a message", evidence)

        observed = envelope.get("model")
        usage = envelope.get("usage")
        if guarded:
            cost, cost_basis = price_usage(observed, usage, self.prices)
            if cost is not None:
                self.spent = spent_before + cost
        else:
            cost, cost_basis = None, "untracked"
        self.last_call.update(
            sent=True, usage=usage, cost_usd=cost, response_id=envelope.get("response_id")
        )
        try:
            if self.spent > self.budget:
                raise ValueError("aggregate budget exhausted")
            if not isinstance(observed, str) or not observed:
                raise ValueError("response carries no model identity")
            if self.session_source == "response_id":
                # The session is the provider's own response id, observed, never minted here.
                session = envelope.get("response_id")
                if not isinstance(session, str) or not session:
                    raise ValueError("response carries no response id for the session")
            if session in self.sessions:
                raise ValueError("repeated independent session identity")
            value, output_form = parse_role_object(envelope.get("text"))
        except (ValueError, TypeError) as exc:
            raise Refused(f"invalid {role} Kong response: {exc}") from exc
        self.sessions.add(session)
        self.receipts.append(
            {
                "role": role,
                "invocation_id": invocation,
                "session_id": session,
                "input_hash": digest(payload),
                "output_hash": digest(value),
                "cost_usd": cost,
                "cost_basis": cost_basis,
                "budget_guard": self.budget_guard,
                "model_requested": model,
                "model_observed": observed,
                "model_match": observed == model,
                "settings": {
                    "max_tokens": max_tokens,
                    "temperature": self.temperature,  # None: not sent, provider default
                    "system_prompt_hash": text_hash(prompt),
                    "thinking": None,  # not requested; no reasoning setting is claimed
                    "retries": 0,
                    "sdk": envelope.get("sdk"),
                },
                "settings_sent": envelope.get("sent_settings"),
                "sent_keys": envelope.get("sent_keys"),
                "output_normalization": OUTPUT_NORMALIZATION,
                "output_form": output_form,
                "stdin_hash": text_hash(user),
                "usage": usage,
                "stop_reason": envelope.get("stop_reason"),
                "response_id": envelope.get("response_id"),
                "request_id": envelope.get("request_id"),
                "via": envelope.get("via"),
                "latency_ms": envelope.get("latency_ms"),
                "gateway_mock": envelope.get("mock"),
                "transport": TRANSPORT,
                "mode": "live",
            }
        )
        if role == "reviewer" and "review_evidence" in payload:
            # Same block Roles.ask records; Controller.ask binds it to the review capture.
            self.receipts[-1]["dispatch"] = {
                "stdin_hash": text_hash(user),
                "system_prompt": prompt,
                "system_prompt_hash": text_hash(prompt),
            }
        return value

    def _resource_guard(self, role, invocation, exc, serial_before, spent_before):
        """The process manager refused or stopped the worker for its RAM floor or wall-time
        guard (owner ruling role_cli_resource_gate_2026-10-14): the run stops on
        resource_guard, never on this role's gate. A refusal before launch (the launch serial
        did not move) provably sent nothing and releases the reservation; a worker stopped
        after launch may have sent the request, so the reservation is kept and the outcome is
        recorded as UNKNOWN, while the Refused (gate resource_guard) is what is raised."""
        launched = type(serial_before) is not int or (
            getattr(self.processes, "serial", None) != serial_before
        )
        if not launched:
            self.last_call["sent"] = False
            self.spent = spent_before
            raise exc
        self.unknown_outcomes.append(
            {
                "role": role,
                "invocation_id": invocation,
                "outcome": "UNKNOWN",
                "reason": f"worker stopped by the process manager: {exc}",
                "budget_guard": self.budget_guard,
                "failed_gate": "resource_guard",
            }
        )
        raise exc

    def _unknown(self, role, invocation, reason, evidence):
        # Guard on: keep the reservation, the request may have been billed.
        record = {
            "role": role,
            "invocation_id": invocation,
            "outcome": "UNKNOWN",
            "reason": reason,
            "budget_guard": self.budget_guard,
            **evidence,
        }
        self.unknown_outcomes.append(record)
        raise KongOutcomeUnknown(f"{role} Kong outcome UNKNOWN: {reason}", record)


if __name__ == "__main__":  # pragma: no cover - guidance only
    sys.exit("import KongRoles; this module is not a CLI")
