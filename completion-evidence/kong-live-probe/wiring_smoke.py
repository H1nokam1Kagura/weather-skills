"""ONE live synthetic Kong call per role shape changed by the D16 wiring. No retries.

Shapes: (A) plain verifier on the probed model, the shape whose fenced answer was refused by
the first probe; (B) reviewer with the shared review-evidence + rationale + claim_check prompt
suffixes; (C) planner with review_evidence_request on the cheap candidate claude-haiku-4-5
(native-route routability of that id is CODE-only before this call).

Payloads are synthetic and Not Sensitive. Budget guard off (spend untracked by owner ruling).
Writes only redacted metadata to WIRING-RESULT.json: outcome, model identity, settings sent,
usage, request id, via, latency, hashes and the output's top-level keys. Never model text,
never a key or .env (the kit worker loads the key in its own process).

Run from the repo root:  python -B completion-evidence/kong-live-probe/wiring_smoke.py
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

SHAPES = [
    {
        "shape": "A-verifier-plain",
        "role": "verifier",
        "model": "claude-sonnet-4-6",
        "payload": {
            "binding_hash": "synthetic-binding-0001",
            "artifact_manifest": [
                "gate-a exit 0 verdict PASS",
                "gate-b exit 0 verdict PASS",
                "final replay comparison PASS",
            ],
        },
    },
    {
        "shape": "B-reviewer-review-evidence",
        "role": "reviewer",
        "model": "claude-sonnet-4-6",
        "payload": {
            "binding_hash": "synthetic-binding-0002",
            "plan": {"steps": ["onset-date", "plot-onset"], "start_date": "2025-10-01"},
            "constraints": {"start_date": "2025-10-01", "end_date": "2025-12-31"},
            "review_evidence": {
                "rationale": "Synthetic: the plan keeps the requested start date.",
                "plan_claims": [{"claim_id": "C-1", "text": "start_date is 2025-10-01"}],
            },
            "claim_check": {"citations": [], "derivable": {"start_date": "2025-10-01"}},
        },
    },
    {
        "shape": "C-planner-review-evidence-request",
        "role": "planner",
        "model": "claude-haiku-4-5",
        "payload": {
            "goal": {"task": "onset_data", "start_date": "2025-10-01", "end_date": "2025-12-31"},
            "tools": ["onset-date"],
            "output_schema": {"steps": "list of tool ids"},
            "review_evidence_request": {"version": 1, "emit": ["plan_claims"]},
        },
    },
]
RECEIPT_KEEP = (
    "role",
    "invocation_id",
    "session_id",
    "input_hash",
    "output_hash",
    "cost_usd",
    "cost_basis",
    "budget_guard",
    "model_requested",
    "model_observed",
    "model_match",
    "settings",
    "settings_sent",
    "sent_keys",
    "output_normalization",
    "output_form",
    "stdin_hash",
    "usage",
    "stop_reason",
    "response_id",
    "request_id",
    "via",
    "latency_ms",
    "gateway_mock",
    "transport",
    "mode",
)


def run_shape(spec, scratch):
    proc = scratch / spec["shape"] / "proc"
    proc.mkdir(parents=True)
    roles = KongRoles(
        Processes(proc, timeout=120, total_seconds=180),
        scratch / spec["shape"] / "ctx",
        budget=1,
        max_tokens=1024,
        models={spec["role"]: spec["model"]},
        budget_guard="off",
        prices={},
        data_policy_ack=True,
    )
    row = {
        "shape": spec["shape"],
        "role": spec["role"],
        "model_requested": spec["model"],
        "payload_classification": "synthetic, Not Sensitive",
        "attempts": 1,
    }
    started = time.monotonic()
    try:
        answer = roles.ask(spec["role"], spec["payload"])
        row["outcome"] = "SUCCESS"
        row["output_top_level_keys"] = sorted(answer)
    except KongOutcomeUnknown as exc:
        row.update(outcome="UNKNOWN", evidence=exc.evidence)
    except Refused as exc:
        row.update(outcome="REFUSED", reason=str(exc)[:300])
    except Exception as exc:  # network drop or other local failure: never retried
        row.update(outcome="UNKNOWN", reason=f"local {type(exc).__name__}")
    row["wall_ms"] = round((time.monotonic() - started) * 1000)
    records = sorted(proc.glob("process-*.json"))
    if records:  # worker envelope: metadata only, never the model text
        worker = json.loads(json.loads(records[-1].read_text())["stdout"] or "{}")
        fields = ("sent", "ok", "model", "stop_reason", "via", "request_id", "latency_ms")
        row["worker"] = {k: worker.get(k) for k in fields}
        if isinstance(worker.get("error"), dict):
            row["worker"]["error"] = {
                k: worker["error"].get(k) for k in ("status", "via", "request_id", "hint")
            }
        text = worker.get("text")
        row["worker"]["text_starts_with_fence"] = isinstance(text, str) and text.lstrip().startswith(
            "```"
        )
    if roles.receipts:
        receipt = roles.receipts[0]
        row["receipt"] = {k: receipt.get(k) for k in RECEIPT_KEEP}
        if "dispatch" in receipt:
            row["receipt"]["dispatch"] = {
                "stdin_hash": receipt["dispatch"]["stdin_hash"],
                "system_prompt_hash": receipt["dispatch"]["system_prompt_hash"],
            }
    row["spent_after"] = roles.spent
    return row, roles.provenance()


def main():
    out = Path(__file__).with_name("WIRING-RESULT.json")
    scratch = Path(tempfile.mkdtemp(prefix="kong-wiring-"))
    result = {
        "schema": "kong-wiring-smoke/1",
        "generated_utc": datetime.now(UTC).isoformat(),
        "transport": "kong-native-anthropic",
        "retries": 0,
        "shapes": [],
    }
    for spec in SHAPES:
        row, provenance = run_shape(spec, scratch)
        row["provenance"] = provenance
        result["shapes"].append(row)
    out.write_text(json.dumps(result, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({"outcomes": [r["outcome"] for r in result["shapes"]], "result": str(out)}))


if __name__ == "__main__":
    main()
