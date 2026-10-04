"""Pluggable shadow backends behind one interface.

    score(question_type, state_text, options) -> {choice, probs, latency_ms, backend, model_sha}

A backend may RAISE (``BackendUnavailable`` or anything else); the caller in ``shadow.py``
turns every exception into a logged ``unavailable`` record. Nothing here can block, change or
delay the real verdict, because nothing here is consulted by the real gate.

    stub  deterministic heuristic, stdlib only, offline. The baseline every model must beat.
    laya  convaiinnovations/laya (Apache-2.0, ModernBERT-large + decision head), local CPU,
          weights pinned by revision + sha256 in laya.lock.json. Needs `laya` (PyPI, by
          Convai Innovations) importable: run under `uv run --no-project --with laya==0.3.26`.
    kev   jaredpalmer/kev-0.8b served by `python -m kev.serve` (TypeSafe /v1/systemone).
          Client only, UNTESTED against a real server; weights are NOT downloaded here.
          Configure KEV_URL (+ optional KEV_API_KEY). No URL -> unavailable.
    clm   Contrastive-LM CLM-v0.1-8B served by `clm-serve` (TypeSafe /v1/systemone; vLLM +
          Qwen3-8B encoder, needs a GPU). Client only. Configure CLM_URL (+ optional
          CLM_API_KEY). No URL -> unavailable.
"""
from __future__ import annotations

import json
import os
import time
import urllib.error
import urllib.request
from typing import Any, Callable

from . import templates
from .lock import HashMismatch, cache_dir, read_lock, verify_cache

BACKENDS = ("stub", "laya", "kev", "clm")
STUB_VERSION = "stub-heuristic-v1"

# Revisions registered by the clm research (D56). Recorded on every kev log line; the kev
# server reports what it actually loaded via /v1/models and that wins when present.
KEV_REPO = "jaredpalmer/kev-0.8b"
KEV_REVISION = "bf75a6a8848ea6960ff2ed108d9ed44c2941174f"
CLM_REPO = "Contrastive-LM/CLM-v0.1-8B"


class BackendUnavailable(RuntimeError):
    """The backend cannot answer (not configured, not installed, endpoint down). Fail closed."""


# --------------------------------------------------------------------------- stub
_ESCALATE_CUES = (
    "ambiguous", "unclear", "not sure", "unsure", "either", " or ", "which one", "?",
    "delete", "overwrite", "publish", "send", "public", "cost", "paid", "credential",
    "missing", "no data", "unknown", "somewhere", "around", "recently",
)
_REJECT_CUES = ("violat", "breaks rule", "not allowed", "missing input", "error", "fails",
                "deaccumulate after fetch", "plot before convert-to-totals")


def _stub(decision_point: str, state_text: str, options: Any) -> tuple[str, dict[str, float], str]:
    text = f" {state_text.lower()} "
    if decision_point == "escalate":
        hits = sum(1 for cue in _ESCALATE_CUES if cue in text)
        p = min(0.95, 0.05 + 0.15 * hits)
        return ("escalate" if p >= 0.5 else "proceed"), {"escalate": p, "proceed": 1.0 - p}, STUB_VERSION
    if decision_point == "review_verdict":
        hits = sum(1 for cue in _REJECT_CUES if cue in text)
        p_rej = min(0.9, 0.2 + 0.35 * hits)
        probs = {"approve": 1.0 - p_rej, "reject": p_rej}
        return max(probs, key=probs.__getitem__), probs, STUB_VERSION
    # next_skill: the "alphabetically first valid skill" rule. The clm research found it
    # scores 0.669 top-1 on held-out task types, above every small generative model tested.
    keys = sorted(templates.normalize_options(decision_point, options))
    first = keys[0]
    rest = (0.4 / (len(keys) - 1)) if len(keys) > 1 else 0.0
    probs = {k: (0.6 if len(keys) > 1 else 1.0) if k == first else rest for k in keys}
    return first, probs, STUB_VERSION


# --------------------------------------------------------------------------- laya
_LAYA_AGENT: dict[str, Any] = {}


def _laya_prepare() -> float:
    """Verify hashes and load the model once per process. Returns load ms (0 when cached)."""
    lock = read_lock()
    directory = cache_dir(lock)
    key = str(directory)
    if key in _LAYA_AGENT:
        return 0.0
    t0 = time.perf_counter()
    verify_cache(lock, directory)                # raises HashMismatch -> refuse to load
    # Belt and braces: the load must never reach the Hub (the verified local copy is the model).
    os.environ["HF_HUB_OFFLINE"] = "1"
    os.environ["TRANSFORMERS_OFFLINE"] = "1"
    try:
        import laya  # noqa: PLC0415  (heavy; only when this backend is selected)
    except ImportError as e:
        raise BackendUnavailable(
            "laya package not importable; run under `uv run --no-project --with laya==0.3.26`") from e
    # A path containing a separator is treated by laya.load as a local checkpoint
    # directory, so nothing is fetched from the Hub at load time.
    _LAYA_AGENT[key] = laya.load(str(directory), device="cpu")
    return (time.perf_counter() - t0) * 1000.0


def _laya(decision_point: str, state_text: str, options: Any) -> tuple[str, dict[str, float], str]:
    lock = read_lock()
    _laya_prepare()
    agent = _LAYA_AGENT[str(cache_dir(lock))]
    q = templates.build_question(decision_point, options)
    result = agent.predict(state_text, {"decision": q})
    choice, probs = templates.answer_to_result(decision_point, result["answers"]["decision"])
    return choice, probs, lock["revision"]


def laya_verify_only() -> str:
    """Hash-check the cache against the lock without importing laya. Returns the revision."""
    lock = read_lock()
    verify_cache(lock, cache_dir(lock))
    return lock["revision"]


# --------------------------------------------------------------------------- TypeSafe HTTP (kev, clm)
def _post_json(url: str, body: dict, api_key: str | None, timeout: float) -> dict:
    req = urllib.request.Request(url, data=json.dumps(body).encode("utf-8"), method="POST",
                                 headers={"content-type": "application/json"})
    if api_key:
        req.add_header("Authorization", f"Bearer {api_key}")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:  # noqa: S310 (configured URL)
            return json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        # 503/504 here is the scale-to-zero cold start of a serving endpoint: unavailable, not fatal.
        raise BackendUnavailable(f"HTTP {e.code} from {url}") from e
    except (urllib.error.URLError, TimeoutError, OSError) as e:
        raise BackendUnavailable(f"endpoint unreachable: {e}") from e


def _typesafe(env_prefix: str, default_model: str, model_sha: str) -> Callable:
    def call(decision_point: str, state_text: str, options: Any) -> tuple[str, dict[str, float], str]:
        base = os.environ.get(f"{env_prefix}_URL", "").strip()
        if not base:
            raise BackendUnavailable(f"{env_prefix}_URL is not set; the {env_prefix.lower()} backend "
                                     "is a client to an externally served endpoint")
        api_key = os.environ.get(f"{env_prefix}_API_KEY") or None      # read, never logged
        timeout = float(os.environ.get(f"{env_prefix}_TIMEOUT_S", "5"))
        model = os.environ.get(f"{env_prefix}_MODEL", default_model)
        body = {"state": state_text, "model": model,
                "questions": {"decision": templates.build_question(decision_point, options)}}
        out = _post_json(base.rstrip("/") + "/v1/systemone", body, api_key, timeout)
        try:
            answer = out["answers"]["decision"]
        except (KeyError, TypeError) as e:
            raise BackendUnavailable(f"malformed /v1/systemone response: keys {sorted(out) if isinstance(out, dict) else type(out)}") from e
        choice, probs = templates.answer_to_result(decision_point, answer)
        return choice, probs, os.environ.get(f"{env_prefix}_MODEL_SHA", model_sha)
    return call


_kev = _typesafe("KEV", "kev-latest", f"{KEV_REPO}@{KEV_REVISION}")
_clm = _typesafe("CLM", "clm-latest", f"{CLM_REPO}@unpinned-served")

_IMPL = {"stub": _stub, "laya": _laya, "kev": _kev, "clm": _clm}
_PREPARE = {"laya": _laya_prepare}


def score(question_type: str, state_text: str, options: Any = None, backend: str = "stub") -> dict:
    """The one interface. Raises on failure; shadow.py turns that into an `unavailable` log line."""
    if backend not in _IMPL:
        raise BackendUnavailable(f"unknown backend {backend!r}; expected {BACKENDS}")
    templates.normalize_options(question_type, options)        # validates the decision point
    # One-time model load is reported separately so latency_ms is the per-decision cost.
    load_ms = _PREPARE[backend]() if backend in _PREPARE else 0.0
    t0 = time.perf_counter()
    choice, probs, model_sha = _IMPL[backend](question_type, state_text, options)
    latency_ms = (time.perf_counter() - t0) * 1000.0
    out = {"choice": choice, "probs": {k: round(float(v), 6) for k, v in probs.items()},
           "latency_ms": round(latency_ms, 2), "backend": backend, "model_sha": model_sha}
    if load_ms:
        out["load_ms"] = round(load_ms, 1)
    return out


__all__ = ["BACKENDS", "BackendUnavailable", "HashMismatch", "score", "laya_verify_only"]
