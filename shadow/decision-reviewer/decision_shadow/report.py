"""Measure the shadow against what the real gate did.

Per decision point and backend: decisions logged, unavailable, n with a known outcome,
agreement with the actual outcome. For `escalate`, additionally the clm E2 bar
(REPORT_2026-10-02.md, E2): flag the top 20% of decisions by shadow p(escalate) and require

    recall  >= 0.70   share of ACTUAL escalations that fall inside the flagged 20%
    kept_err <= 0.05  share of the unflagged 80% where the real gate escalated anyway

Ties at the cut are broken by log order (earlier first), so the cut is exactly ceil(0.2 n).
Small samples are labelled "advisory only; insufficient n" whatever the verdict says.
"""

from __future__ import annotations

import math
from collections import defaultdict
from typing import Any

from .shadow import normalize_actual

TOP_SHARE = 0.20
RECALL_BAR = 0.70
KEPT_ERR_BAR = 0.05
MIN_N = 30  # decisions with a known outcome
MIN_POSITIVES = 10  # actual escalations, for the recall estimate to mean anything


def join(records: list[dict]) -> list[dict]:
    """Decision records with `actual` filled from the last outcome record (or inline actual)."""
    outcomes: dict[str, Any] = {}
    for r in records:
        if r.get("kind") == "outcome":
            outcomes[r["decision_id"]] = r.get("actual")
    out = []
    for r in records:
        if r.get("kind") != "decision":
            continue
        r = dict(r)
        raw = outcomes.get(r["decision_id"], r.get("actual"))
        if raw is not None:
            try:
                r["actual"] = normalize_actual(r["decision_point"], raw)
            except ValueError:
                r["actual"] = None
        out.append(r)
    return out


def escalate_bar(rows: list[dict]) -> dict[str, Any]:
    """rows: ok escalate decisions with a known actual, in log order."""
    n = len(rows)
    positives = sum(1 for r in rows if r["actual"] == "escalate")
    if n == 0:
        return {"n": 0, "k": 0, "positives": 0, "recall": None, "kept_err": None, "verdict": "n/a"}
    k = math.ceil(TOP_SHARE * n)
    order = sorted(range(n), key=lambda i: (-float(rows[i]["probs"]["escalate"]), i))
    flagged = set(order[:k])
    caught = sum(1 for i in flagged if rows[i]["actual"] == "escalate")
    kept = [i for i in range(n) if i not in flagged]
    kept_err = (
        (sum(1 for i in kept if rows[i]["actual"] == "escalate") / len(kept)) if kept else 0.0
    )
    recall = (caught / positives) if positives else None
    # Ceiling a PERFECT detector reaches at this base rate (clm D58 premise finding: E2's bar was
    # unreachable for any trigger because the base error exceeded the 20% escalation budget).
    best_recall = min(1.0, k / positives) if positives else None
    best_kept_err = (max(0, positives - k) / (n - k)) if (positives and n > k) else 0.0
    reachable = (
        best_recall is not None and best_recall >= RECALL_BAR and best_kept_err <= KEPT_ERR_BAR
    )
    if recall is None:
        verdict = "n/a (no actual escalations)"
    elif not reachable:
        verdict = "n/a (bar unreachable at this base rate; even a perfect detector fails)"
    else:
        verdict = "PASS" if (recall >= RECALL_BAR and kept_err <= KEPT_ERR_BAR) else "FAIL"
    return {
        "n": n,
        "k": k,
        "positives": positives,
        "caught": caught,
        "recall": recall,
        "kept_err": kept_err,
        "best_recall": best_recall,
        "best_kept_err": best_kept_err,
        "verdict": verdict,
    }


def summarize(records: list[dict]) -> dict[tuple[str, str], dict[str, Any]]:
    groups: dict[tuple[str, str], list[dict]] = defaultdict(list)
    for r in join(records):
        groups[(r["decision_point"], r["backend"])].append(r)
    summary = {}
    for key, rows in sorted(groups.items()):
        ok = [r for r in rows if r.get("status") == "ok"]
        scored = [r for r in ok if r.get("actual") is not None]
        agree = sum(1 for r in scored if r.get("choice") == r["actual"])
        lat = sorted(float(r["latency_ms"]) for r in ok if r.get("latency_ms") is not None)
        s: dict[str, Any] = {
            "logged": len(rows),
            "unavailable": len(rows) - len(ok),
            "n": len(scored),
            "agreement": (agree / len(scored)) if scored else None,
            "latency_p50_ms": lat[len(lat) // 2] if lat else None,
        }
        if key[0] == "escalate":
            s["bar"] = escalate_bar(scored)
            enough = len(scored) >= MIN_N and s["bar"]["positives"] >= MIN_POSITIVES
        else:
            enough = len(scored) >= MIN_N
        s["insufficient_n"] = not enough
        summary[key] = s
    return summary


def _fmt(x: Any, pct: bool = False) -> str:
    if x is None:
        return "-"
    return f"{100 * x:.1f}%" if pct else (f"{x:.1f}" if isinstance(x, float) else str(x))


def render(records: list[dict]) -> str:
    summary = summarize(records)
    if not summary:
        return "No shadow decisions logged. (Shadow is off unless WS_DECISION_SHADOW is set.)\n"
    lines = ["Decision shadow report -- advisory only; the shadow never changed a verdict.", ""]
    hdr = f"{'decision point':<16}{'backend':<8}{'logged':>7}{'unavail':>8}{'n':>5}{'agree':>8}{'p50 ms':>9}"
    lines += [hdr, "-" * len(hdr)]
    for (point, backend), s in summary.items():
        lines.append(
            f"{point:<16}{backend:<8}{s['logged']:>7}{s['unavailable']:>8}{s['n']:>5}"
            f"{_fmt(s['agreement'], True):>8}{_fmt(s['latency_p50_ms']):>9}"
        )
    lines.append("")
    for (point, backend), s in summary.items():
        if point != "escalate":
            continue
        b = s["bar"]
        lines.append(
            f"escalate / {backend}: clm E2 bar = catch >= {RECALL_BAR:.0%} of actual escalations "
            f"in the top {TOP_SHARE:.0%} by shadow p, with <= {KEPT_ERR_BAR:.0%} error on the kept {1 - TOP_SHARE:.0%}"
        )
        lines.append(
            f"  n={b['n']}  flagged={b['k']}  actual escalations={b['positives']}  "
            f"caught={b.get('caught', 0)}  recall={_fmt(b['recall'], True)}  "
            f"kept error={_fmt(b['kept_err'], True)}  -> {b['verdict']}"
        )
        lines.append(
            f"  perfect-detector ceiling: recall={_fmt(b.get('best_recall'), True)}  "
            f"kept error={_fmt(b.get('best_kept_err'), True)}"
        )
    lines.append("")
    lines.append(
        "This E2-style recall bar is a diagnostic. The bar of record is clm D58: the tiered "
        "system at <=20% escalation must be no worse than the big model alone (lower 90% bound "
        ">= -0.02). It needs a big-model-alone arm and is measured offline in clm, not here. "
        "Baseline to beat there: B0a, a 34-feature CPU logistic head (system 0.788 vs Sonnet "
        "4.6 0.763; synthetic workflows)."
    )
    for (point, backend), s in summary.items():
        if s["insufficient_n"]:
            need = f"n>={MIN_N}" + (
                f" and >={MIN_POSITIVES} actual escalations" if point == "escalate" else ""
            )
            lines.append(
                f"{point} / {backend}: advisory only; insufficient n (have n={s['n']}, need {need})"
            )
    return "\n".join(lines) + "\n"
