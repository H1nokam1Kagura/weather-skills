"""cases_report.py: per-case statuses and honesty rules over synthetic artifacts (no docker).

Uses the frozen v2.7 package from the committed fixture (tests/frozen_v27.py) for the guarded
scorer's undecided records and summarize(); nothing here executes a case.
"""

import json
import sys
from pathlib import Path

import pytest

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))

import cases_report  # noqa: E402

CAPS = ["CAP-NOT-IMPLEMENTED"]


def row(cid, kind, admitted=True, disposition="RUNNABLE", selected=True, missing=()):
    return {
        "case_id": cid,
        "pair_id": cid.rsplit("-", 1)[0],
        "kind": kind,
        "family_id": "EX",
        "execution_mode": "controller_conformance",
        "role_mode": "substituted",
        "admitted": admitted,
        "missing_capabilities": list(missing),
        "disposition": disposition if admitted else "NOT_IMPLEMENTED",
        "selected": selected and admitted,
        "oracle_status": "PROPOSED",
        "required_capabilities": CAPS,
    }


def admission(rows, selector="test"):
    return {
        "schema": "simkit.gha-admission/1",
        "adapter": "test",
        "selector": selector,
        "implemented_capabilities": CAPS,
        "counts": {"admitted": sum(r["admitted"] for r in rows)},
        "missing_capability_counts": {"CAP-X": 1},
        "missing_capability_reasons": {"CAP-X": "NO_ADAPTER_METHOD"},
        "cases": rows,
    }


def artifact(tmp, cid, launch=None, score=None):
    d = tmp / "cases" / cid
    d.mkdir(parents=True)
    rec = {
        "entry_args": ["case", cid],
        "harness_status": {"case_id": cid},
        "container_exit_code": 0,
        "tracer_status": "OK",
        "run_conditions": {"a": True},
    }
    rec.update(launch or {})
    (d / "launch.json").write_text(json.dumps(rec), encoding="utf-8")
    if score is not None:
        (d / "score.json").write_text(json.dumps(score), encoding="utf-8")
    return d


def executed_score(cid, dims):
    return {
        "case_id": cid,
        "run_status": "EXECUTED",
        "scored": {
            "case_id": cid,
            "kind": "control",
            "run_status": "EXECUTED",
            "oracle_status": "UNRATIFIED_DEV",
            "dimensions": dims,
            "assertions": {},
            "terminal": "NOT_OBSERVED",
        },
    }


def by_id(res):
    return {e["case_id"]: e for e in res["cases"]}


def test_statuses_are_distinct_and_never_inferred(tmp_path):
    rows = [
        row("P1-CTL", "control", admitted=False, missing=["CAP-X"]),
        row("P1-INJ", "injection", disposition="BLOCKED_LIVE_ROLE_TRANSPORT"),
        row("P2-CTL", "control"),  # selected, no artifact at all
        row("P2-INJ", "injection", selected=False),
        row("P3-CTL", "control"),
        row("P3-INJ", "injection"),
        row("P4-CTL", "control"),
        row("P4-INJ", "injection"),
    ]
    dims = {
        "judgment": "PASS",
        "containment": "NOT_OBSERVED",
        "recovery": "NOT_APPLICABLE",
        "utility": "FAIL",
    }
    artifact(tmp_path, "P3-CTL", score=executed_score("P3-CTL", dims))
    artifact(
        tmp_path,
        "P3-INJ",
        score={"case_id": "P3-INJ", "run_status": "NOT_EXECUTED", "demoted": "no checkpoint"},
    )
    artifact(tmp_path, "P4-CTL", launch={"docker_start_rc": "timeout"})
    artifact(
        tmp_path,
        "P4-INJ",
        score={
            "case_id": "P4-INJ",
            "run_status": "HARNESS_ERROR",
            "harness_error_source": "projection_UNAVAILABLE",
        },
    )
    res = cases_report.build(admission(rows), tmp_path / "cases")
    got = by_id(res)
    assert got["P1-CTL"]["status"] == "NOT_IMPLEMENTED"
    assert got["P1-CTL"]["missing_capabilities"] == ["CAP-X"]
    assert got["P1-INJ"]["status"] == "BLOCKED_LIVE_ROLE_TRANSPORT"
    assert got["P2-CTL"] == {
        **got["P2-CTL"],
        "status": "HARNESS_ERROR",
        "harness_error_source": "no_case_artifact",
    }
    assert got["P2-INJ"]["status"] == "ADMITTED_NOT_SELECTED"
    assert got["P3-CTL"]["status"] == "EXECUTED" and got["P3-CTL"]["dimensions"] == dims
    assert got["P3-INJ"]["status"] == "NOT_EXECUTED" and got["P3-INJ"]["demoted"]
    assert got["P4-CTL"]["harness_error_source"] == "container_timeout"
    assert got["P4-INJ"]["harness_error_source"] == "projection_UNAVAILABLE"
    assert res["oracles"] == "PROPOSED" and "DIAGNOSTIC" in res["scoring"]
    assert res["gs1"] == "NOT_MET"
    # Only the executed case can carry decided dimensions into the frozen summary.
    assert res["frozen_summary"]["run_status_counts"]["EXECUTED"] == 1
    assert res["counts"]["by_status"]["HARNESS_ERROR"] == 3
    pairs = {p["pair_id"]: p for p in res["pairs"]}
    assert pairs["P3"]["both_executed"] is False
    md = cases_report.markdown(res)
    assert md.splitlines()[2].startswith("> Oracles are PROPOSED")


def test_missing_score_is_harness_error_not_executed(tmp_path):
    rows = [row("P5-CTL", "control"), row("P5-INJ", "injection", admitted=False, missing=["X"])]
    artifact(tmp_path, "P5-CTL", score=None)
    res = cases_report.build(admission(rows), tmp_path / "cases")
    assert by_id(res)["P5-CTL"]["status"] == "HARNESS_ERROR"
    assert by_id(res)["P5-CTL"]["harness_error_source"] == "no_score"
    assert "EXECUTED" not in res["counts"]["by_status"]


def test_entry_abort_is_harness_error(tmp_path):
    rows = [row("P6-CTL", "control")]
    artifact(
        tmp_path,
        "P6-CTL",
        launch={"harness_status": {"case_id": "P6-CTL", "abort": "staging_failed"}},
    )
    res = cases_report.build(admission(rows), tmp_path / "cases")
    assert by_id(res)["P6-CTL"]["harness_error_source"] == "entry:staging_failed"


def test_runner_harness_error_names_its_source_root_cause_and_text(tmp_path):
    """A HARNESS_ERROR in the runner record itself is never 'unspecified': its exception type,
    the controller's root error type (run-copy/result.json) and the error text reach the
    score record and the RESULTS.md table."""
    import case_driver

    out = tmp_path / "harness-out"
    (out / "run-copy").mkdir(parents=True)
    record = {
        "status": "HARNESS_ERROR",
        "error": "EvidenceMappingError: role files do not correspond one-to-one to receipts",
    }
    (out / "run-copy" / "result.json").write_text(
        json.dumps(
            {
                "status": "INCOMPLETE",
                "error": "unresolved trusted symbol: HARNESS_SOURCE_HASH:definition.x",
                "terminal_mapping": {
                    "source": "runtime_fallback",
                    "error_type": "UnsupportedInjection",
                },
            }
        ),
        encoding="utf-8",
    )
    record_path = out / "runner_record.json"
    assert case_driver.runner_error_source(record, record_path) == {
        "harness_error_source": "runner:EvidenceMappingError",
        "root_error_type": "UnsupportedInjection",
        "root_error": "unresolved trusted symbol: HARNESS_SOURCE_HASH:definition.x",
    }
    # The driver's own record keeps its source; no result.json means no root fields.
    own = {
        "status": "HARNESS_ERROR",
        "error": "case_driver: OSError: x",
        "harness_error_source": "case_driver",
    }
    assert case_driver.runner_error_source(own, tmp_path / "none" / "r.json") == {
        "harness_error_source": "case_driver"
    }
    assert (
        case_driver.runner_error_source({"error": None}, record_path)["harness_error_source"]
        == "runner"
    )

    rows = [row("P7-INJ", "injection")]
    artifact(
        tmp_path,
        "P7-INJ",
        score={
            "case_id": "P7-INJ",
            "run_status": "HARNESS_ERROR",
            "record_error": record["error"] + " | with a pipe",
            **case_driver.runner_error_source(record, record_path),
        },
    )
    res = cases_report.build(admission(rows), tmp_path / "cases")
    got = by_id(res)["P7-INJ"]
    assert got["harness_error_source"] == "runner:EvidenceMappingError"
    assert got["root_error_type"] == "UnsupportedInjection"
    assert got["root_error"].startswith("unresolved trusted symbol")
    (line,) = [x for x in cases_report.markdown(res).splitlines() if x.startswith("| P7-INJ ")]
    assert "runner:EvidenceMappingError" in line
    assert "role files do not correspond" in line and "with a pipe" in line
    assert "root UnsupportedInjection: unresolved trusted symbol" in line
    # the error text's own pipe is escaped: the row stays 6 cells (5 + the labelled
    # counterfactual column, n/a for a case that did not execute)
    # counterfactual and rubric-grader cells: both n/a for a HARNESS_ERROR
    assert line.count(" | ") == 6 and line.endswith(" | n/a | n/a |")


def test_execution_gate_by_selector():
    """'admission' / 'relay-smoke' / push 'smoke' select nothing by design; every other selector
    is meant to execute cases, so none selected or fewer EXECUTED than selected is a FAIL."""
    gate = cases_report.execution_gate
    for sel in ("admission", "relay-smoke", "smoke"):
        g = gate(sel, 0, 0)
        assert g["status"] == "NOTHING_SELECTED_BY_DESIGN" and g["expects_execution"] is False
        assert g["message"] == (
            f"NOTHING WILL EXECUTE: selector '{sel}' selects 0 cases; "
            "use 'substituted' for runnable offline cases"
        )
    assert gate("smoke", 2, 1)["status"] == "SHORTFALL_NOT_ENFORCED"
    for sel in ("substituted", "live", "A-CTL,A-INJ"):
        assert gate(sel, 0)["status"] == "FAIL"  # admission time
        assert gate(sel, 0, 0)["status"] == "FAIL"
        assert "NOTHING WILL EXECUTE" in gate(sel, 0, 0)["message"]
        assert gate(sel, 3)["status"] == "OK"
        g = gate(sel, 3, 2)
        assert g["status"] == "FAIL" and g["message"] == "only 2 of 3 selected cases EXECUTED"
        assert gate(sel, 3, 3)["status"] == "OK"
    a = admission([row("Q-CTL", "control", selected=False)], selector="substituted")
    a["counts"]["selected"] = 0
    assert cases_report.admission_gate(a)["status"] == "FAIL"
    a["selector"] = "admission"
    assert cases_report.admission_gate(a)["status"] == "NOTHING_SELECTED_BY_DESIGN"


def write_results(tmp_path, res):
    p = tmp_path / "RESULTS.json"
    p.write_text(json.dumps(res), encoding="utf-8")
    return p


def test_admission_selector_with_nothing_selected_still_succeeds(tmp_path):
    rows = [row("Q1-CTL", "control", selected=False), row("Q1-INJ", "injection", selected=False)]
    res = cases_report.build(admission(rows, selector="admission"), tmp_path / "cases")
    g = res["execution_gate"]
    assert (g["status"], g["selected"], g["executed"]) == ("NOTHING_SELECTED_BY_DESIGN", 0, 0)
    md = cases_report.markdown(res).splitlines()
    assert md[2].startswith("> Oracles are PROPOSED")
    assert f"> **NOTHING_SELECTED_BY_DESIGN: {g['message']}**" in md
    assert cases_report.main(["x", "gate", str(write_results(tmp_path, res))]) == 0


def test_substituted_selector_with_nothing_selected_fails_the_run(tmp_path):
    rows = [row("Q2-CTL", "control", selected=False)]
    res = cases_report.build(admission(rows, selector="substituted"), tmp_path / "cases")
    g = res["execution_gate"]
    assert g["status"] == "FAIL" and "NOTHING WILL EXECUTE: selector 'substituted'" in g["message"]
    assert f"> **RUN FAILS (execution gate): {g['message']}**" in cases_report.markdown(res)
    assert cases_report.main(["x", "gate", str(write_results(tmp_path, res))]) == 1


def test_selected_case_that_did_not_execute_fails_the_run(tmp_path):
    rows = [row("Q3-CTL", "control"), row("Q3-INJ", "injection")]
    dims = dict.fromkeys(cases_report.DIMENSIONS, "PASS")
    artifact(tmp_path, "Q3-CTL", score=executed_score("Q3-CTL", dims))
    artifact(tmp_path, "Q3-INJ", score=None)  # HARNESS_ERROR no_score
    res = cases_report.build(admission(rows, selector="Q3-CTL,Q3-INJ"), tmp_path / "cases")
    g = res["execution_gate"]
    assert (g["status"], g["selected"], g["executed"]) == ("FAIL", 2, 1)
    assert cases_report.main(["x", "gate", str(write_results(tmp_path, res))]) == 1
    (tmp_path / "cases" / "Q3-INJ" / "score.json").write_text(
        json.dumps(executed_score("Q3-INJ", dims)), encoding="utf-8"
    )
    res = cases_report.build(admission(rows, selector="Q3-CTL,Q3-INJ"), tmp_path / "cases")
    assert res["execution_gate"]["status"] == "OK"
    assert not [x for x in cases_report.markdown(res).splitlines() if "execution gate" in x]
    assert cases_report.main(["x", "gate", str(write_results(tmp_path, res))]) == 0


def test_absent_results_fail_the_gate(tmp_path):
    assert cases_report.main(["x", "gate", str(tmp_path / "none.json")]) == 1


# ------------------------------------------------------------------ live relay usage / reruns

TS = "2026-10-15T12:00:00+00:00"
START = {"event": "relay_start", "ts": TS, "run_id": "r", "model": "claude-sonnet-4-6"}
OK_ASK = {
    "event": "ask",
    "ts": TS,
    "outcome": "SUCCESS",
    "http_status": 200,
    "model_observed": "claude-sonnet-4-6",
    "usage": {"input_tokens": 1000, "output_tokens": 100},
}


def gw_unknown(status=None, ts=TS):
    return {"event": "ask", "ts": ts, "outcome": "UNKNOWN", "http_status": 502, "status": status}


GUARD = {"event": "spend_cap_reached", "ts": TS}
LIVE_KEYS = {"relay_usage", "relay_outcome_label", "attempts", "attempt"}
CLEAN = dict.fromkeys(cases_report.DIMENSIONS, "PASS")
FAILED = {**CLEAN, "judgment": "FAIL"}


def live_row(cid, kind, **kw):
    return {**row(cid, kind, **kw), "role_mode": "live"}


def scored(cid, dims, terminal):
    s = executed_score(cid, dims)
    s["scored"]["terminal"] = terminal
    return s


def relay_audit(art, events, start=START):
    d = art / "relay"
    d.mkdir()
    lines = [json.dumps(e) for e in ([start] if start else []) + events]
    (d / "relay-audit.jsonl").write_text("\n".join(lines) + "\n", encoding="utf-8")
    (d / "relay-caps.json").write_text(json.dumps({"max_requests": 24}), encoding="utf-8")


def live_run(tmp_path):
    rows = [
        live_row("L1-CTL", "control"),
        live_row("L1-INJ", "injection"),
        live_row("L2-CTL", "control"),
        live_row("L2-INJ", "injection"),
        live_row("L3-CTL", "control"),
        live_row("L3-INJ", "injection"),  # selected, no artifact at all
        row("O1-CTL", "control"),  # an offline case in the same run
    ]
    art = artifact(tmp_path, "L1-CTL", score=scored("L1-CTL", FAILED, "FAIL"))
    relay_audit(art, [OK_ASK, gw_unknown(429)])
    art = artifact(tmp_path, "L1-INJ", score=scored("L1-INJ", FAILED, "FAIL"))
    relay_audit(art, [OK_ASK, OK_ASK, GUARD])
    art = artifact(tmp_path, "L2-CTL", score=scored("L2-CTL", CLEAN, "PASS"))
    relay_audit(art, [OK_ASK, OK_ASK])
    art = artifact(
        tmp_path,
        "L2-INJ",
        score={"case_id": "L2-INJ", "run_status": "HARNESS_ERROR", "harness_error_source": "x"},
    )
    relay_audit(art, [gw_unknown(503)])
    art = artifact(tmp_path, "L3-CTL", score=scored("L3-CTL", FAILED, "FAIL"))
    relay_audit(art, [gw_unknown(400)])
    artifact(tmp_path, "O1-CTL", score=scored("O1-CTL", CLEAN, "PASS"))
    return cases_report.build(admission(rows, selector="live"), tmp_path / "cases", "RUN-1")


def test_live_cases_carry_relay_usage_labels_and_the_rerun_eligible_list(tmp_path):
    res = live_run(tmp_path)
    got = by_id(res)
    assert not LIVE_KEYS & set(got["O1-CTL"])  # offline case in a live run: untouched
    lab = {cid: got[cid]["relay_outcome_label"] for cid in got if cid.startswith("L")}
    assert lab == {
        "L1-CTL": "LIVE_TRANSPORT_FAULT",
        "L1-INJ": "RELAY_GUARD_ABORT",
        "L2-CTL": None,
        "L2-INJ": "LIVE_TRANSPORT_FAULT",
        "L3-CTL": None,  # 400 is deterministic
        "L3-INJ": None,
    }
    u = got["L1-CTL"]["relay_usage"]
    assert (u["requests"], u["input_tokens"], u["output_tokens"]) == (2, 1000, 100)
    assert u["est_usd"] == pytest.approx((1000 * 3 + 100 * 15) / 1e6)
    assert got["L1-INJ"]["relay_usage"]["cap_state"] == "SPEND_CAP_REACHED"
    assert got["L3-INJ"]["status"] == "HARNESS_ERROR"
    assert got["L3-INJ"]["relay_usage"]["cap_state"] == "NO_AUDIT"
    assert got["L3-INJ"]["relay_usage"]["requests"] is None
    assert got["L1-CTL"]["attempts"] == [
        {
            "attempt": 1,
            "run_url": "RUN-1",
            "status": "EXECUTED",
            "relay_outcome_label": "LIVE_TRANSPORT_FAULT",
            "relay_usage": {
                "requests": 2,
                "input_tokens": 1000,
                "output_tokens": 100,
                "est_usd": u["est_usd"],
                "cap_state": "UNDER_CAP",
            },
            "fault_kinds": ["gateway_429"],
            "dimensions": FAILED,
            "terminal": "FAIL",
        }
    ]
    live = res["live"]
    assert live["ruling"].startswith("OWNER-RULINGS live_spend_authorization_2026-10-15")
    assert live["labels"] == {
        "LIVE_TRANSPORT_FAULT": ["L1-CTL", "L2-INJ"],
        "RELAY_GUARD_ABORT": ["L1-INJ"],
    }
    # only the transient fault on a non-harness-error, non-clean case
    assert [r["case_id"] for r in live["rerun_eligible"]] == ["L1-CTL"]
    assert live["rerun_eligible"][0]["min_wait_seconds"] == 60
    assert live["rerun_eligible"][0]["earliest_rerun_utc"] == "2026-10-15T12:01:00+00:00"
    ne = live["rerun_not_eligible"]
    assert set(ne) == {"L1-INJ", "L2-INJ", "L3-CTL"}
    assert "RELAY_GUARD_ABORT" in ne["L1-INJ"] and ne["L2-INJ"] == "harness error"
    t = live["totals"]
    assert (t["cases"], t["cases_with_audit"], t["requests"]) == (6, 5, 8)
    assert t["input_tokens"] == 5000 and t["est_usd_complete"] is False  # L3-INJ: no audit
    # the frozen headline is untouched: counts and the execution gate are as before
    assert res["counts"]["by_status"] == {"EXECUTED": 5, "HARNESS_ERROR": 2}
    assert res["execution_gate"]["status"] == "FAIL"  # 5 of 7 selected executed

    md = cases_report.markdown(res)
    lines = md.splitlines()
    (l1,) = [x for x in lines if x.startswith("| L1-CTL | controller_conformance")]
    assert "relay LIVE_TRANSPORT_FAULT" in l1 and l1.count(" | ") == 6
    assert "## Live relay usage (relay audit; list-price estimate, Kong billing not observed)" in md
    header = (
        "| case | attempt | status | requests | input tokens | output tokens | est. USD "
        "| cap state | relay label | faults |"
    )
    assert header in lines
    assert (
        "| L1-CTL | 1 | EXECUTED | 2 | 1000 | 100 | 0.0045 | UNDER_CAP | LIVE_TRANSPORT_FAULT" in md
    )
    l1inj = "| L1-INJ | 1 | EXECUTED | 2 | 2000 | 200 | 0.0090 | SPEND_CAP_REACHED "
    assert l1inj + "| RELAY_GUARD_ABORT | spend_cap_reached |" in lines
    assert "### Rerun-eligible (OWNER-RULINGS live_spend_authorization_2026-10-15" in md
    assert "| L1-CTL | 1 | gateway_429 | 60 | 2026-10-15T12:01:00+00:00 |" in lines
    assert "- LIVE_TRANSPORT_FAULT: L1-CTL, L2-INJ" in lines
    assert "- RELAY_GUARD_ABORT: L1-INJ" in lines


def test_a_rerun_is_reported_alongside_the_original_never_replacing_it(tmp_path):
    original = json.loads(json.dumps(live_run(tmp_path / "a"), default=str))
    rows = [
        live_row("L1-CTL", "control"),
        live_row("L1-INJ", "injection"),  # NOT rerun-eligible in the original
        row("O1-CTL", "control", selected=False),
    ]
    late = {**START, "ts": "2026-10-15T12:10:00+00:00"}
    early = {**START, "ts": "2026-10-15T12:00:30+00:00"}
    art = artifact(tmp_path / "b", "L1-CTL", score=scored("L1-CTL", CLEAN, "PASS"))
    relay_audit(art, [OK_ASK], start=late)
    art = artifact(tmp_path / "b", "L1-INJ", score=scored("L1-INJ", CLEAN, "PASS"))
    relay_audit(art, [OK_ASK], start=early)
    res = cases_report.build(
        admission(rows, selector="L1-CTL,L1-INJ"), tmp_path / "b" / "cases", "RUN-2", original
    )
    got = by_id(res)["L1-CTL"]
    first = by_id(original)["L1-CTL"]["attempts"][0]
    assert got["attempt"] == 2 and len(got["attempts"]) == 2
    assert got["attempts"][0] == first  # the original attempt, unchanged
    second = got["attempts"][1]
    assert (second["attempt"], second["run_url"], second["status"]) == (2, "RUN-2", "EXECUTED")
    assert second["rerun_of_run_url"] == "RUN-1"
    assert second["authorized_by_prior_rerun_eligible"] is True
    live = res["live"]
    assert live["rerun_eligible"] == []  # never a second re-dispatch
    assert live["reruns_not_authorized_by_prior"] == ["L1-INJ"]
    assert live["reruns_before_earliest_time"] == []  # L1-CTL started after 12:01
    assert set(live["attempts_by_case"]) == {"L1-CTL", "L1-INJ"}
    md = cases_report.markdown(res)
    assert "| L1-CTL | 1 | EXECUTED | 2 |" in md and "| L1-CTL | 2 | EXECUTED | 1 |" in md
    assert "attempt 2 (rerun; earlier attempts under Live relay usage)" in md
    assert "> **Reruns NOT listed rerun-eligible by the prior run: L1-INJ**" in md
    # a rerun that started before the ruling's wait is flagged
    relay = tmp_path / "b" / "cases" / "L1-CTL" / "relay" / "relay-audit.jsonl"
    relay.write_text(json.dumps(early) + "\n" + json.dumps(OK_ASK) + "\n", encoding="utf-8")
    res = cases_report.build(
        admission(rows, selector="L1-CTL,L1-INJ"), tmp_path / "b" / "cases", "RUN-2", original
    )
    assert res["live"]["reruns_before_earliest_time"] == ["L1-CTL"]


def test_prior_results_that_cannot_be_read_refuse_the_report(tmp_path):
    a = tmp_path / "ADMISSION.json"
    a.write_text(json.dumps(admission([row("Z-CTL", "control")])), encoding="utf-8")
    argv = ["x", str(a), str(tmp_path), "--out", str(tmp_path / "R.json")]
    argv += ["--markdown", str(tmp_path / "R.md"), "--prior-results", str(tmp_path / "no.json")]
    with pytest.raises(SystemExit, match="prior-results"):
        cases_report.main(argv)


# The RESULTS.json top-level keys before the live sub-wave; an offline run adds only "live".
PRE_LIVE_KEYS = {
    "schema",
    "header",
    "oracles",
    "scoring",
    "run_url",
    "gs1",
    "frozen_final_pins_sha256",
    "frozen_root_used_for_summary",
    "adapter",
    "implemented_capabilities",
    "selector",
    "execution_gate",
    "counts",
    "top_missing_capabilities",
    "missing_capability_reasons",
    "frozen_summary",
    cases_report.wcf.JSON_KEY,
    "pairs",
    "cases",
}


def test_offline_report_is_unchanged_apart_from_an_empty_live_section(tmp_path):
    rows = [
        row("V1-CTL", "control"),
        row("V1-INJ", "injection"),
        row("V2-CTL", "control", admitted=False, missing=["CAP-X"]),
        row("V2-INJ", "injection", selected=False),
        {**row("V3-CTL", "control"), "role_mode": "fixture"},
    ]
    artifact(tmp_path, "V1-CTL", score=scored("V1-CTL", CLEAN, "PASS"))
    artifact(tmp_path, "V1-INJ", score=None)
    art = artifact(tmp_path, "V3-CTL", score=scored("V3-CTL", FAILED, "FAIL"))
    relay_audit(art, [gw_unknown(429)])  # a stray relay dir on an offline case is ignored
    res = cases_report.build(admission(rows, selector="substituted"), tmp_path / "cases")
    # sub-wave 8 adds the rubric grader block; with no rubric case it is empty and silent
    assert set(res) == PRE_LIVE_KEYS | {"live", "rubric_grader"}
    assert res["rubric_grader"]["cases"] == 0 and res["rubric_grader"]["assertions"] == []
    for e in res["cases"]:
        assert not LIVE_KEYS & set(e), e["case_id"]
    live = res["live"]
    assert live["totals"]["cases"] == 0 and live["totals"]["requests"] == 0
    assert live["labels"] == {"LIVE_TRANSPORT_FAULT": [], "RELAY_GUARD_ABORT": []}
    assert live["rerun_eligible"] == [] and live["rerun_not_eligible"] == {}
    assert live["attempts_by_case"] == {} and live["prior_run_url"] is None
    md = cases_report.markdown(res)
    assert md == cases_report.markdown({k: v for k, v in res.items() if k != "live"})
    assert "relay" not in md.lower() and "rerun" not in md.lower()
    assert res["execution_gate"]["status"] == "FAIL"  # gate behaviour unchanged (2 of 3 ran)


def test_workflow_runs_both_execution_gates_after_the_uploads():
    text = (cases_report.REPO / ".github" / "workflows" / "simkit-cases.yml").read_text(
        encoding="utf-8"
    )
    admission_job = text.split("\n  admission:\n", 1)[1].split("\n  case:\n", 1)[0]
    assert "cases_report.admission_gate(a)" in admission_job
    assert admission_job.index("r.admission_gate(") > admission_job.index("name: simkit-admission")
    report_job = text.split("\n  report:\n", 1)[1]
    gate = "container_harness/cases_report.py gate results/RESULTS.json"
    assert gate in report_job
    assert report_job.index(gate) > report_job.index("name: simkit-results")
