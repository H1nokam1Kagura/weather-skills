"""Opt-in live role transport through the Gates Foundation Kong AI Gateway (native Anthropic).

Same ``ask(role, payload)`` contract as ``agents.Roles``; the receipt adds the identity the
API itself reports (``model_observed`` is the response ``model`` field, never the request).

Key isolation: this process never reads a key or a ``.env``. Each call launches a short
worker under the kit's own interpreter (``python -I -c WORKER_SOURCE``) with its working
directory at the kit root; the kit's ``kong_client`` loads the consumer key into *that*
process only and the worker prints a key-free JSON envelope.

Outcome semantics (mirrors ``Roles``; UNKNOWN never becomes success):
- ``Refused``: nothing was sent (unknown role, budget, policy, worker failed pre-send), or a
  response came back and is not a valid role answer (cost still reconciled when computable).
- ``KongOutcomeUnknown``: a request may have reached the gateway and no valid answer was
  observed (HTTP error, timeout, crash, unparseable worker output). The budget reservation is
  kept, because the call may have been billed.

Data policy: only Not Sensitive, synthetic content may be sent; the constructor requires an
explicit acknowledgement and the transport is never selected by default.
"""

import json
import math
import os
import sys
import uuid
from pathlib import Path

from .agents import PROMPTS
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

# USD per million tokens, keyed by the OBSERVED model id. Deliberately empty: no price table
# with a citable source is pinned in this repo or the Kong kit, and Kong exposes no per-call
# cost. Callers may pass ``prices={model: {"input_per_mtok", "output_per_mtok", "source"}}``;
# an entry without a ``source`` citation is rejected. Without a price, cost_usd is null with a
# reason, and the pre-call reservation of the remaining ceiling is kept (fail closed).
PINNED_PRICES = {}

# Runs under the kit interpreter. Stdlib + kit only; prints exactly one JSON object.
# "sent" is False only while no request can have left the process.
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
    if request.get("temperature") is not None:
        body["temperature"] = request["temperature"]
    kong_client.check_body_size(body)
    import inspect
    inspect.signature(client.messages.create).bind(**body)  # SDK argument check, pre-send
    out["sdk"] = "anthropic-python " + version("anthropic")
    out["mock"] = kong_client.is_mock_url(cfg.gateway_url)
    started = time.monotonic()
    out["sent"] = True
    try:
        message, info = kong_client.create_message(client, **body)
    except Exception as err:
        out["latency_ms"] = round((time.monotonic() - started) * 1000)
        out["error"] = kong_client.describe_error(err, (cfg.anthropic_key, cfg.unified_key))
        out["error_type"] = type(err).__name__
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


class KongRoles:
    def __init__(
        self,
        processes,
        workspace,
        budget=5,
        model=DEFAULT_MODEL,
        max_tokens=2048,
        temperature=None,
        prices=None,
        *,
        data_policy_ack=False,
        kit_root=KIT_ROOT,
        kit_python=None,
        worker_env=None,
    ):
        if data_policy_ack is not True:
            raise Refused("Kong transport requires acknowledging the Not Sensitive data policy")
        prices = dict(PINNED_PRICES if prices is None else prices)
        for name, entry in prices.items():
            if (
                not isinstance(entry, dict)
                or not isinstance(entry.get("source"), str)
                or not entry["source"].strip()
                or not all(_finite(entry.get(k)) for k in ("input_per_mtok", "output_per_mtok"))
            ):
                raise Refused(f"price for {name!r} needs finite rates and a cited source")
        if type(max_tokens) is not int or not 1 <= max_tokens <= 64000:
            raise Refused("max_tokens must be an integer in 1..64000")
        if temperature is not None and not (_finite(temperature) and temperature <= 1):
            raise Refused("temperature must be null or in 0..1")
        self.processes = processes
        self.workspace = workspace
        self.budget = budget
        self.spent = 0.0
        self.model = model
        self.max_tokens = max_tokens
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

    def _environment(self):
        env = child_environment(Path(self.workspace).parent / "scratch")
        env.update(self.worker_env)
        return env

    def ask(self, role, payload):
        if role not in PROMPTS or self.budget - self.spent < 0.05:
            raise Refused("role unavailable or aggregate model budget exhausted")
        if "review_evidence" in payload or "review_evidence_request" in payload:
            # Roles.ask appends inline prompt suffixes for these; they are not importable,
            # and a copied suffix would let the two transports drift silently.
            raise Refused("review-evidence prompt variants are not available on the Kong transport")
        if not self.kit_python.is_file():
            raise Refused("Kong kit interpreter unavailable")
        invocation = str(uuid.uuid4())
        session = str(uuid.uuid4())
        dispatch_context = getattr(self, "dispatch_context", None)
        if dispatch_context is not None:
            self.dispatch_observation = {
                "attempt_id": dispatch_context["attempt_id"],
                "invocation_id": invocation,
                "process_boundary": "NOT_ENTERED",
            }
        prompt = PROMPTS[role]
        user = json.dumps(payload)
        request = {
            "model": self.model,
            "max_tokens": self.max_tokens,
            "temperature": self.temperature,
            "system": prompt,
            "user": user,
        }
        command = [str(self.kit_python), "-I", "-c", WORKER_SOURCE, str(self.kit_root / "scripts")]
        spent_before = self.spent
        # Reserve the remaining ceiling before launch, exactly as Roles does: an unpriced or
        # unknown outcome cannot create fresh budget, on resume or otherwise.
        self.spent = self.budget
        if dispatch_context is not None:
            self.dispatch_observer(dict(self.dispatch_observation))
            self.dispatch_observation["process_boundary"] = "ENTERED"
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
        }
        if not envelope["sent"]:
            self.spent = spent_before  # provably nothing left the worker
            raise Refused(f"{role} Kong worker failed before sending: {evidence['error_type']}")
        if envelope.get("ok") is not True or result["exit_code"] != 0:
            self._unknown(role, invocation, "gateway call did not return a message", evidence)

        observed = envelope.get("model")
        usage = envelope.get("usage")
        cost, cost_basis = price_usage(observed, usage, self.prices)
        if cost is not None:
            self.spent = spent_before + cost
        try:
            if self.spent > self.budget:
                raise ValueError("aggregate budget exhausted")
            if not isinstance(observed, str) or not observed:
                raise ValueError("response carries no model identity")
            if session in self.sessions:
                raise ValueError("repeated independent session identity")
            value = json.loads(envelope.get("text") or "")
            if not isinstance(value, dict):
                raise ValueError("role output must be an object")
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
                "model_requested": self.model,
                "model_observed": observed,
                "model_match": observed == self.model,
                "settings": {
                    "max_tokens": self.max_tokens,
                    "temperature": self.temperature,  # None: not sent, provider default
                    "system_prompt_hash": text_hash(prompt),
                    "thinking": None,  # not requested; no reasoning setting is claimed
                    "sdk": envelope.get("sdk"),
                },
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
        return value

    def _unknown(self, role, invocation, reason, evidence):
        # Keep the reservation: the request may have been billed.
        record = {
            "role": role,
            "invocation_id": invocation,
            "outcome": "UNKNOWN",
            "reason": reason,
            **evidence,
        }
        self.unknown_outcomes.append(record)
        raise KongOutcomeUnknown(f"{role} Kong outcome UNKNOWN: {reason}", record)


if __name__ == "__main__":  # pragma: no cover - guidance only
    sys.exit("import KongRoles; this module is not a CLI")
