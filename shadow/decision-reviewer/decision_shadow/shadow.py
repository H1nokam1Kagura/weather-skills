"""Shadow entry points: run a backend, log the result, never raise, never decide.

    WS_DECISION_SHADOW            off (default) | stub | laya | kev | clm
    WS_DECISION_SHADOW_LOG        JSONL path (default shadow/decision-reviewer/logs/decisions.jsonl)
    WS_DECISION_SHADOW_LOG_TEXT   1 to store raw state text; default stores only its sha256

The log is append-only JSONL with two record kinds:
    {"kind": "decision", "decision_id", "ts", "decision_point", "input_sha256", "options",
     "backend", "model_sha", "status": "ok"|"unavailable", "choice", "probs", "latency_ms",
     ["error"], ["actual"], ["text"]}
    {"kind": "outcome", "decision_id", "ts", "actual"}
An outcome is what the REAL gate decided; the report joins on decision_id (last outcome wins).
"""
from __future__ import annotations

import hashlib
import json
import os
import threading
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from . import backends, templates
from .lock import HERE

ENV_BACKEND = "WS_DECISION_SHADOW"
ENV_LOG = "WS_DECISION_SHADOW_LOG"
ENV_LOG_TEXT = "WS_DECISION_SHADOW_LOG_TEXT"
DEFAULT_LOG = HERE / "logs" / "decisions.jsonl"

_WRITE_LOCK = threading.Lock()


def enabled_backend() -> str | None:
    v = os.environ.get(ENV_BACKEND, "off").strip().lower()
    if v in ("", "off", "0", "false", "none"):
        return None
    return v


def log_path() -> Path:
    return Path(os.environ.get(ENV_LOG) or DEFAULT_LOG)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


def input_hash(decision_point: str, state_text: str, options: Any) -> str:
    canon = json.dumps({"p": decision_point, "s": state_text, "o": options}, sort_keys=True,
                       ensure_ascii=False, default=str)
    return hashlib.sha256(canon.encode("utf-8")).hexdigest()


def _append(record: dict, path: Path | None = None) -> None:
    path = path or log_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    line = json.dumps(record, ensure_ascii=False, sort_keys=True)
    with _WRITE_LOCK, open(path, "a", encoding="utf-8") as f:
        f.write(line + "\n")


def normalize_actual(decision_point: str, actual: Any) -> str:
    """Map the real gate's outcome to the decision point's option keys."""
    a = str(actual).strip()
    if decision_point == "escalate":
        low = a.lower()
        if low in ("escalate", "yes", "true", "1", "ask", "asked"):
            return "escalate"
        if low in ("proceed", "no", "false", "0", "continue"):
            return "proceed"
        raise ValueError(f"escalate outcome must be escalate/proceed (or yes/no), got {actual!r}")
    if decision_point == "review_verdict":
        low = a.lower()
        if low in ("approve", "approved", "pass"):
            return "approve"
        if low in ("reject", "rejected", "fail"):
            return "reject"
        raise ValueError(f"review outcome must be approve/reject, got {actual!r}")
    return a


def shadow_score(decision_point: str, state_text: str, options: Any = None, *,
                 backend: str | None = None, actual: Any = None,
                 decision_id: str | None = None) -> str | None:
    """Score in shadow and log. Returns the decision_id, or None when the shadow is off.

    NEVER raises and NEVER returns a decision: the caller's verdict must not read this.
    Any failure (bad input, backend down, hash mismatch) becomes an `unavailable` record.
    """
    try:
        backend = backend or enabled_backend()
        if backend is None:
            return None
        decision_id = decision_id or uuid.uuid4().hex
        rec: dict[str, Any] = {
            "kind": "decision", "decision_id": decision_id, "ts": _now(),
            "decision_point": decision_point, "backend": backend,
            "input_sha256": input_hash(decision_point, state_text, options),
        }
        try:
            rec["options"] = list(templates.normalize_options(decision_point, options))
        except Exception:  # noqa: BLE001
            rec["options"] = None
        if os.environ.get(ENV_LOG_TEXT) == "1":
            rec["text"] = state_text
        if actual is not None:
            try:
                rec["actual"] = normalize_actual(decision_point, actual)
            except Exception as e:  # noqa: BLE001
                rec["actual_error"] = str(e)
        try:
            out = backends.score(decision_point, state_text, options, backend=backend)
            rec.update(status="ok", choice=out["choice"], probs=out["probs"],
                       latency_ms=out["latency_ms"], model_sha=out["model_sha"])
            if "load_ms" in out:
                rec["load_ms"] = out["load_ms"]
        except Exception as e:  # noqa: BLE001  fail closed: logged, never raised
            rec.update(status="unavailable", choice=None, probs=None, latency_ms=None,
                       model_sha=None, error=f"{type(e).__name__}: {e}")
        _append(rec)
        return decision_id
    except Exception:  # noqa: BLE001  even the logger failing must not touch the real run
        return None


def shadow_score_background(decision_point: str, state_text: str, options: Any = None, *,
                            backend: str | None = None) -> str | None:
    """Fire-and-forget variant: returns the decision_id immediately, scores on a daemon thread."""
    backend = backend or enabled_backend()
    if backend is None:
        return None
    decision_id = uuid.uuid4().hex
    threading.Thread(target=shadow_score, args=(decision_point, state_text, options),
                     kwargs={"backend": backend, "decision_id": decision_id}, daemon=True).start()
    return decision_id


def record_outcome(decision_id: str, actual: Any, decision_point: str | None = None) -> bool:
    """Append the real gate's outcome for a decision. Never raises; returns success."""
    try:
        a = normalize_actual(decision_point, actual) if decision_point else str(actual).strip()
        _append({"kind": "outcome", "decision_id": decision_id, "ts": _now(), "actual": a})
        return True
    except Exception:  # noqa: BLE001
        return False


def read_log(path: Path | None = None) -> list[dict]:
    path = path or log_path()
    if not path.exists():
        return []
    rows = []
    with open(path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                rows.append(json.loads(line))
            except json.JSONDecodeError:
                continue            # a torn line from a crashed writer is skipped, not fatal
    return rows
