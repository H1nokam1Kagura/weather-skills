"""Report math on synthetic logs with known answers."""

import math

import pytest
from decision_shadow import report


def esc(i, p, actual, backend="stub", status="ok"):
    return {
        "kind": "decision",
        "decision_id": f"d{i}",
        "decision_point": "escalate",
        "backend": backend,
        "status": status,
        "choice": None if status != "ok" else ("escalate" if p >= 0.5 else "proceed"),
        "probs": None if status != "ok" else {"escalate": p, "proceed": 1 - p},
        "latency_ms": None if status != "ok" else 1.0,
        "actual": actual,
    }


def test_top20_catches_all_pass_but_insufficient_n():
    probs = [0.95, 0.9, 0.4, 0.35, 0.3, 0.25, 0.2, 0.15, 0.1, 0.05]
    rows = [esc(i, p, "escalate" if i < 2 else "proceed") for i, p in enumerate(probs)]
    s = report.summarize(rows)[("escalate", "stub")]
    b = s["bar"]
    assert (b["n"], b["k"], b["positives"], b["caught"]) == (10, 2, 2, 2)
    assert b["recall"] == 1.0 and b["kept_err"] == 0.0 and b["verdict"] == "PASS"
    assert s["agreement"] == 1.0 and s["insufficient_n"] is True
    text = report.render(rows)
    assert "-> PASS" in text and "escalate / stub: advisory only; insufficient n" in text


def test_missed_escalation_fails_both_bars():
    probs = [0.95, 0.9, 0.4, 0.35, 0.3, 0.25, 0.2, 0.15, 0.1, 0.05]
    actual = [
        "escalate",
        "proceed",
        "proceed",
        "proceed",
        "proceed",
        "proceed",
        "proceed",
        "proceed",
        "proceed",
        "escalate",
    ]
    b = report.escalate_bar(
        [esc(i, p, a) for i, (p, a) in enumerate(zip(probs, actual, strict=True))]
    )
    assert b["caught"] == 1 and b["recall"] == 0.5
    assert b["kept_err"] == pytest.approx(1 / 8) and b["verdict"] == "FAIL"


def test_perfect_detector_at_high_base_rate_is_unreachable_not_fail():
    # 50 decisions, 15 actual escalations: the top 10 are ALL escalations (a perfect detector),
    # yet recall is 10/15 = 0.667 < 0.70. That is the clm D58 premise finding: the bar, not the
    # detector, fails. Previously reported as FAIL.
    rows = [esc(i, 1 - i / 100, "escalate" if i < 15 else "proceed") for i in range(50)]
    b = report.escalate_bar(rows)
    assert b["k"] == 10 and b["caught"] == 10
    assert b["recall"] == pytest.approx(10 / 15) and b["kept_err"] == pytest.approx(5 / 40)
    assert b["verdict"].startswith("n/a (bar unreachable")


def test_reachable_bar_still_fails_a_bad_detector():
    # 50 decisions, 7 escalations (reachable), but the shadow ranks them last.
    rows = [esc(i, i / 100, "escalate" if i < 7 else "proceed") for i in range(50)]
    assert report.escalate_bar(rows)["verdict"] == "FAIL"


def test_sufficient_n_pass_has_no_advisory_line():
    rows = [esc(i, 1 - i / 100, "escalate" if i < 10 else "proceed") for i in range(50)]
    s = report.summarize(rows)[("escalate", "stub")]
    assert s["bar"]["verdict"] == "PASS" and s["insufficient_n"] is False
    assert "insufficient n" not in report.render(rows)


def test_cut_is_ceil_and_ties_break_by_log_order():
    rows = [esc(i, 0.5, "escalate" if i in (0, 6) else "proceed") for i in range(7)]
    b = report.escalate_bar(rows)
    assert b["k"] == math.ceil(0.2 * 7) == 2  # flags d0, d1 (log order)
    assert b["caught"] == 1 and b["recall"] == 0.5 and b["kept_err"] == pytest.approx(1 / 5)


def test_no_actual_escalations_is_na():
    rows = [esc(i, 0.1, "proceed") for i in range(5)]
    assert report.escalate_bar(rows)["verdict"].startswith("n/a")


def test_outcome_record_overrides_inline_and_unavailable_excluded():
    rows = [
        esc(0, 0.9, "proceed"),
        esc(1, 0.1, None),
        esc(2, 0.0, "escalate", status="unavailable"),
        {"kind": "outcome", "decision_id": "d0", "actual": "yes"},
        {"kind": "outcome", "decision_id": "d1", "actual": "no"},
    ]
    s = report.summarize(rows)[("escalate", "stub")]
    assert (s["logged"], s["unavailable"], s["n"]) == (3, 1, 2)
    assert s["agreement"] == 1.0  # d0 escalate==escalate, d1 proceed==proceed


def test_agreement_for_choice_points_and_backends_kept_apart():
    rows = []
    for i, (choice, actual, backend) in enumerate(
        [
            ("approve", "approve", "stub"),
            ("approve", "reject", "stub"),
            ("reject", "reject", "laya"),
        ]
    ):
        rows.append(
            {
                "kind": "decision",
                "decision_id": f"r{i}",
                "decision_point": "review_verdict",
                "backend": backend,
                "status": "ok",
                "choice": choice,
                "probs": {choice: 0.8},
                "latency_ms": 2.0,
                "actual": actual,
            }
        )
    rows.append(
        {
            "kind": "decision",
            "decision_id": "n0",
            "decision_point": "next_skill",
            "backend": "stub",
            "status": "ok",
            "choice": "clip-region",
            "probs": {"clip-region": 0.5},
            "latency_ms": 0.1,
        }
    )
    rows.append({"kind": "outcome", "decision_id": "n0", "actual": "select"})
    s = report.summarize(rows)
    assert s[("review_verdict", "stub")]["agreement"] == 0.5
    assert s[("review_verdict", "laya")]["agreement"] == 1.0
    assert s[("next_skill", "stub")]["agreement"] == 0.0


def test_empty_log():
    assert "No shadow decisions logged" in report.render([])


def test_unreachable_bar_is_not_reported_as_fail():
    # 50 decisions, 20 actual escalations (40% base rate): a 20% flag budget (10) cannot reach
    # 70% recall even with a perfect detector, so the verdict must say so instead of FAIL.
    rows = [esc(i, 1.0 - i / 100, "escalate" if i < 20 else "proceed") for i in range(50)]
    b = report.escalate_bar(rows)
    assert b["best_recall"] == 0.5
    assert b["verdict"].startswith("n/a (bar unreachable")
