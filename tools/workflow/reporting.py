"""Report observed receipts without treating fixture evidence as live verification."""

import html
import json
from pathlib import Path

from .persistence import atomic_json, read_checkpoint


def report(root):
    root = Path(root)
    result_path = root / "result.json"
    result = json.loads(result_path.read_text(encoding="utf-8")) if result_path.exists() else {}
    checkpoint = read_checkpoint(root)
    processes = [
        json.loads(p.read_text(encoding="utf-8"))
        for p in sorted((root / "processes").glob("process-*.json"))
    ]
    roles = checkpoint.get("role_receipts", [])
    fixture = any(r.get("mode") == "fixture" for r in roles)
    summary = {
        "schema": "workflow.report/1",
        "status": result.get("status", "INTERRUPTED"),
        "state": checkpoint["state"],
        "profile": checkpoint["profile"],
        "source_admission": checkpoint.get("admission_record", {"policy": "legacy-unrecorded"}),
        "evidence_mode": "fixture" if fixture else "live",
        "error": result.get("error"),
        "stop_kind": result.get("stop_kind"),
        "source_unchanged_at_last_check": result.get("source_unchanged"),
        "model_cost_usd": checkpoint.get("spent", 0),
        "elapsed_s": checkpoint.get("elapsed_s", 0),
        "process_count": len(processes),
        "role_count": len(roles),
        "minimum_free_gib": min(
            (p["min_free_gib"] for p in processes if "min_free_gib" in p), default=None
        ),
        "repairs_used": checkpoint.get("repairs_used", 0),
        "resumes": checkpoint.get("resumes", 0),
        "pending_operation": checkpoint.get("pending"),
        "stages": checkpoint["events"],
        "artifacts": result.get("artifacts", {}),
        "scope": result.get("scope", "not released"),
        "next_action": "Inspect released artifacts"
        if result.get("status") == "PASS"
        else "Inspect failure evidence; resume only if eligible, otherwise create a new reviewed attempt",
    }
    atomic_json(root / "report.json", summary)
    rows = "".join(
        f"<tr><th>{html.escape(k)}</th><td>{html.escape(str(v))}</td></tr>"
        for k, v in summary.items()
        if k not in {"stages", "artifacts"}
    )
    steps = "".join(
        f"<li>{html.escape(e['from'])} → {html.escape(e['to'])}</li>" for e in summary["stages"]
    )
    (root / "report.html").write_text(
        "<!doctype html><html lang='en'><meta charset='utf-8'><title>Workflow run report</title>"
        "<style>body{font:16px system-ui;max-width:1000px;margin:40px auto;padding:16px}"
        "th,td{text-align:left;vertical-align:top;padding:8px;border-bottom:1px solid #ddd}"
        "td{overflow-wrap:anywhere}th{min-width:220px}</style><h1>Workflow run report</h1>"
        f"<table>{rows}</table><h2>Observed transitions</h2><ol>{steps}</ol></html>",
        encoding="utf-8",
    )
    return summary
