"""ONE live synthetic probe of KongRoles through the Kong AI Gateway (native Anthropic route).

Payload is synthetic and Not Sensitive (three invented manifest lines). Exactly one call, no
retry. Writes only redacted metadata to RESULT.json: outcome, model identity, usage, request
id, via, latency and hashes. Never reads or writes a key or .env; the kit worker loads it.

Run from the repo root:  python -B completion-evidence/kong-live-probe/probe.py
"""

import json
import sys
import tempfile
import time
from datetime import UTC, datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from tools.workflow.contracts import Refused  # noqa: E402
from tools.workflow.kong_roles import KongOutcomeUnknown, KongRoles  # noqa: E402
from tools.workflow.processes import Processes  # noqa: E402

PAYLOAD = {
    "binding_hash": "synthetic-binding-0001",
    "artifact_manifest": [
        "gate-a exit 0 verdict PASS",
        "gate-b exit 0 verdict PASS",
        "final replay comparison PASS",
    ],
}
FIELDS = ("model", "stop_reason", "usage", "via", "request_id", "latency_ms", "sdk", "mock")


def main():
    out = Path(__file__).with_name("RESULT.json")
    scratch = Path(tempfile.mkdtemp(prefix="kong-probe-"))
    (scratch / "proc").mkdir()
    roles = KongRoles(
        Processes(scratch / "proc", timeout=120, total_seconds=180),
        scratch / "ctx",
        budget=1,
        max_tokens=128,
        prices={},  # no cited price table is pinned; cost stays null with a reason
        data_policy_ack=True,
    )
    result = {
        "schema": "kong-live-probe/1",
        "generated_utc": datetime.now(UTC).isoformat(),
        "transport": "kong-native-anthropic",
        "role": "verifier",
        "payload_classification": "synthetic, Not Sensitive",
        "model_requested": roles.model,
        "settings_requested": {"max_tokens": roles.max_tokens, "temperature": roles.temperature},
        "attempts": 1,
    }
    started = time.monotonic()
    try:
        roles.ask("verifier", PAYLOAD)
        result["outcome"] = "SUCCESS"
    except KongOutcomeUnknown as exc:
        result.update(outcome="UNKNOWN", evidence=exc.evidence)
    except Refused as exc:
        result.update(outcome="REFUSED", reason=str(exc)[:300])
    result["wall_ms"] = round((time.monotonic() - started) * 1000)
    records = sorted((scratch / "proc").glob("process-*.json"))
    if records:  # worker envelope: keep metadata only, never the model text
        worker = json.loads(json.loads(records[-1].read_text())["stdout"] or "{}")
        result["worker"] = {k: worker.get(k) for k in ("sent", "ok", *FIELDS)}
        if isinstance(worker.get("error"), dict):
            result["worker"]["error"] = {
                k: worker["error"].get(k) for k in ("status", "via", "request_id", "hint")
            }
        result["worker"]["text_is_json_object"] = _is_object(worker.get("text"))
    if roles.receipts:
        receipt = dict(roles.receipts[0])
        result["receipt"] = receipt
    result["model_cost_usd_spent"] = roles.spent
    out.write_text(json.dumps(result, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({"outcome": result["outcome"], "result": str(out)}))


def _is_object(text):
    try:
        return isinstance(json.loads(text or ""), dict)
    except ValueError:
        return False


if __name__ == "__main__":
    main()
