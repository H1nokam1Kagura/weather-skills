"""relay_usage.py: caps, the live spend gate and gateway-fault classification (offline).

Pins the owner ruling live_spend_authorization_2026-10-15.rerun_on_gateway_fault: only a
recorded transient gateway fault (429, 5xx, timeout, reset, UNKNOWN with no gateway response)
is LIVE_TRANSPORT_FAULT and rerun-eligible; a spend-cap abort, any other 4xx, an oversized
payload, a relay refusal, a harness error and a scored failure without such a fault never are.
"""

import json
import sys
from pathlib import Path

import pytest

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))

import relay_usage as ru  # noqa: E402

TS = "2026-10-15T12:00:00+00:00"


def ask(outcome="SUCCESS", **fields):
    event = {"schema": "simkit-kong-relay-audit/1", "ts": TS, "event": "ask", "outcome": outcome}
    if outcome == "SUCCESS":
        event.update(
            http_status=200,
            model_observed="claude-sonnet-4-6",
            usage={"input_tokens": 1000, "output_tokens": 200},
        )
    event.update(fields)
    return event


def unknown(status=None, error_type=None, reason="gateway call did not return a message", **kw):
    return ask(
        "UNKNOWN", http_status=502, status=status, error_type=error_type, reason=reason, **kw
    )


def refused(http_status, reason, **kw):
    kw.setdefault("sent_to_gateway", False)
    return ask("REFUSED", http_status=http_status, reason=reason, **kw)


START = {"event": "relay_start", "ts": TS, "run_id": "relay-run-1", "model": "claude-sonnet-4-6"}


# ------------------------------------------------------------------ classification


@pytest.mark.parametrize(
    ("event", "kind", "transient"),
    [
        (unknown(status=429), "gateway_429", True),
        (unknown(status=500), "gateway_5xx", True),
        (unknown(status=503), "gateway_5xx", True),
        (unknown(error_type="APITimeoutError"), "timeout", True),
        (unknown(error_type="ReadTimeout"), "timeout", True),
        (unknown(error_type="APIConnectionError"), "connection_reset", True),
        (unknown(error_type="ConnectionResetError"), "connection_reset", True),
        (unknown(reason="unparseable worker output"), "unknown_no_gateway_response", True),
        (unknown(status=400), "gateway_4xx", False),
        (unknown(status=401), "gateway_4xx", False),
        (unknown(status=413), "gateway_4xx", False),
        (
            unknown(reason="relay internal error", error_type="KeyError"),
            "relay_internal_error",
            False,
        ),
        (refused(413, "body over the 65536-byte cap"), "payload_over_body_cap", False),
        (refused(403, "role not allowed"), "relay_refused", False),
        (refused(422, "invalid reviewer Kong response: x"), "relay_refused", False),
        ({"event": "auth_rejected", "outcome": "REFUSED"}, "auth_rejected", False),
    ],
)
def test_fault_kinds(event, kind, transient):
    assert ru.fault_kind(event) == (kind, transient, False)


def test_spend_cap_is_a_guard_abort_never_transient():
    for event in (
        refused(429, "over the request cap", reason_code="relay_spend_cap"),
        refused(422, "relay_spend_cap: token cap"),
        {"event": "spend_cap_reached", "ts": TS},
    ):
        kind = ru.fault_kind(event)
        assert kind[1] is False and kind[2] is True, event


def test_success_and_lifecycle_events_are_not_faults():
    for event in (ask(), START, {"event": "relay_stop", "reason": "max_requests reached"}):
        assert ru.fault_kind(event) is None


def label(events):
    return ru.usage_from_events(events)["label"]


def test_case_labels():
    assert label([START, ask(), ask()]) is None
    assert label([START, ask(), unknown(status=429)]) == ru.LIVE_TRANSPORT_FAULT
    assert label([START, unknown(status=502), unknown(error_type="APITimeoutError")]) == (
        ru.LIVE_TRANSPORT_FAULT
    )
    # a deterministic fault beside a transient one withholds the transport label
    assert label([START, unknown(status=429), unknown(status=400)]) is None
    assert label([START, refused(413, "body over the cap")]) is None
    # the guard wins over everything
    guard = refused(422, "x", reason_code="relay_spend_cap")
    assert label([START, unknown(status=429), guard]) == ru.RELAY_GUARD_ABORT
    assert label([START, {"event": "spend_cap_reached"}]) == ru.RELAY_GUARD_ABORT


# ------------------------------------------------------------------ usage


def test_usage_counts_sent_requests_tokens_and_list_price():
    events = [
        START,
        ask(),
        ask(usage={"input_tokens": 3000, "output_tokens": 800}),
        unknown(status=503),  # sent, usage unobserved
        refused(403, "role not allowed"),  # never sent
        refused(422, "invalid reviewer Kong response", sent_to_gateway=True),
    ]
    u = ru.usage_from_events(events, caps={"max_requests": 24})
    assert u["audit_present"] is True
    assert (u["asks"], u["requests"], u["refused_before_send"]) == (5, 4, 1)
    assert (u["input_tokens"], u["output_tokens"]) == (4000, 1000)
    assert u["usage_unobserved_requests"] == 2
    assert u["est_usd"] == pytest.approx((4000 * 3 + 1000 * 15) / 1e6)
    assert "Kong billing not observed" in u["est_usd_basis"]
    assert u["cap_state"] == "UNDER_CAP" and u["caps"] == {"max_requests": 24}
    assert [f["kind"] for f in u["faults"]] == ["gateway_5xx", "relay_refused", "relay_refused"]


def test_unpriced_model_makes_the_estimate_null_not_zero():
    u = ru.usage_from_events([START, ask(model_observed="other-model")])
    assert u["est_usd"] is None and u["est_usd_unpriced_requests"] == 1
    assert u["input_tokens"] == 1000


def test_cap_states():
    assert ru.usage_from_events(None)["cap_state"] == "NO_AUDIT"
    assert ru.usage_from_events(None)["requests"] is None  # absent is never zero
    stop = {"event": "relay_stop", "reason": "max_requests reached"}
    assert ru.usage_from_events([START, ask(), stop])["cap_state"] == "MAX_REQUESTS_REACHED"
    cap = {"event": "spend_cap_reached"}
    assert ru.usage_from_events([START, ask(), cap])["cap_state"] == "SPEND_CAP_REACHED"


def test_a_call_past_the_request_cap_is_a_guard_abort():
    # The relay keeps serving past max_requests and refuses the next call before forwarding.
    events = [
        START,
        ask(),
        {"event": "spend_cap_reached", "cap": "max_requests", "ts": TS},
        refused(
            402, "spend cap max_requests reached; nothing forwarded", reason_code="relay_spend_cap"
        ),
        {"event": "relay_stop", "reason": "spend cap reached: max_requests"},
    ]
    u = ru.usage_from_events(events)
    assert u["label"] == ru.RELAY_GUARD_ABORT and u["cap_state"] == "SPEND_CAP_REACHED"
    assert (u["requests"], u["refused_before_send"]) == (1, 1)


def test_relay_dir_reading_keeps_unparsed_lines(tmp_path):
    lines = [json.dumps(START), "{not json", json.dumps(ask())]
    (tmp_path / "relay-audit.jsonl").write_text("\n".join(lines) + "\n", encoding="utf-8")
    (tmp_path / "relay-caps.json").write_text(json.dumps({"max_requests": 9}), encoding="utf-8")
    u = ru.usage_from_relay_dir(tmp_path)
    assert u["requests"] == 1 and u["caps"] == {"max_requests": 9}
    assert [f["kind"] for f in u["faults"]] == ["unparsed_audit_line"] and u["label"] is None
    assert ru.usage_from_relay_dir(tmp_path / "absent")["cap_state"] == "NO_AUDIT"


# ------------------------------------------------------------------ rerun


def test_rerun_decision_follows_the_ruling():
    t, g = ru.LIVE_TRANSPORT_FAULT, ru.RELAY_GUARD_ABORT
    assert ru.rerun_decision("EXECUTED", t, False, 1) == (True, "transient gateway fault")
    assert ru.rerun_decision("NOT_EXECUTED", t, False, 1)[0] is True
    assert ru.rerun_decision("EXECUTED", g, False, 1)[0] is False  # spend-cap abort
    assert ru.rerun_decision("EXECUTED", None, False, 1)[0] is False  # assertion failure only
    assert ru.rerun_decision("HARNESS_ERROR", t, False, 1)[0] is False
    assert ru.rerun_decision("EXECUTED", t, True, 1)[0] is False  # passed anyway
    assert ru.rerun_decision("EXECUTED", t, False, 2)[0] is False  # at most one re-dispatch


def test_rerun_record_waits_at_least_60s_or_the_retry_after():
    u = ru.usage_from_events([START, unknown(status=429, ts="2026-10-15T12:05:00+00:00")])
    r = ru.rerun_record("C1", 1, u)
    assert r["min_wait_seconds"] == 60 and r["fault_kinds"] == ["gateway_429"]
    assert r["earliest_rerun_utc"] == "2026-10-15T12:06:00+00:00"
    assert r["ruling"] == ru.RULING_ID
    u = ru.usage_from_events([START, unknown(status=429, retry_after="120")])
    assert ru.rerun_record("C1", 1, u)["min_wait_seconds"] == 120


# ------------------------------------------------------------------ caps + gates


def test_caps_default_when_the_matrix_row_has_no_cap_fields():
    caps = ru.case_caps({"case_id": "X-CTL", "role_mode": "live"})
    assert (caps["max_requests"], caps["max_total_tokens"], caps["budget_usd"]) == (24, 200000, 5)
    assert caps["roles"] == list(ru.DEFAULT_LIVE_ROLES)
    assert set(caps["source"].values()) == {"default"}
    assert ru.caps_outputs(caps) == [
        "max_requests=24",
        "max_total_tokens=200000",
        "budget_usd=5",
        "roles=" + " ".join(ru.DEFAULT_LIVE_ROLES),
    ]


def test_caps_from_the_matrix_row():
    caps = ru.case_caps({"expected_calls": 7, "live_roles": ["verifier", "compiler"]})
    assert caps["max_requests"] == 14 and caps["source"]["max_requests"] == "expected_calls"
    assert ru.case_caps({"expected_calls": 2})["max_requests"] == 8
    assert ru.case_caps({"expected_calls": 30})["max_requests"] == 24
    assert caps["roles"] == ["compiler", "verifier"] and caps["source"]["roles"] == "matrix"
    caps = ru.case_caps({"max_requests": 40, "max_total_tokens": 90000, "max_model_usd": 9})
    assert caps["max_requests"] == 24 and "clamped" in caps["source"]["max_requests"]
    assert caps["max_total_tokens"] == 90000 and caps["budget_usd"] == 5
    assert ru.case_caps({"live_roles": "planner reviewer"})["roles"] == ["planner", "reviewer"]
    with pytest.raises(ValueError):
        ru.case_caps({"live_roles": ["verifier; rm -rf /"]})


def test_caps_cli_reads_the_matrix_row_from_env(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("MATRIX_ROW", json.dumps({"case_id": "X", "expected_calls": 5}))
    out = tmp_path / "caps.json"
    assert ru.main(["caps", "--out", str(out)]) == 0
    assert "max_requests=10" in capsys.readouterr().out.splitlines()
    assert json.loads(out.read_text())["case_id"] == "X"


def test_max_parallel_live_capped_offline_unchanged():
    assert ru.max_parallel(None, 0) == 8 and ru.max_parallel("", 0) == 8
    assert ru.max_parallel("12", 0) == 12
    assert ru.max_parallel("8", 2) == 3 and ru.max_parallel("", 1) == 3
    assert ru.max_parallel("2", 5) == 2
    with pytest.raises(ValueError):
        ru.max_parallel("abc", 0)


def adm(selector, live=0, offline=1, **row):
    matrix = [{"case_id": f"L{i}", "role_mode": "live", **row} for i in range(live)]
    matrix += [{"case_id": f"O{i}", "role_mode": "substituted"} for i in range(offline)]
    return {"selector": selector, "matrix": matrix}


def test_live_gate():
    gate = ru.live_gate
    assert gate(adm("substituted"), "")["status"] == "OK"  # offline: no input needed
    assert gate(adm("admission", offline=0), "")["status"] == "OK"
    assert gate(adm("live", live=0, offline=0), "")["status"] == "FAIL"  # 'live' always needs it
    assert gate(adm("live", live=2), "")["status"] == "FAIL"
    assert gate(adm("L0,L1", live=2), "")["status"] == "FAIL"  # explicit ids select paid cases
    assert gate(adm("live", live=2), "2")["status"] == "OK"
    g = gate(adm("live", live=3), "2")
    assert g["status"] == "FAIL" and "above live_max_cases 2" in g["problems"][0]
    assert gate(adm("live", live=1), "two")["status"] == "FAIL"
    assert gate(adm("live", live=1), "0")["status"] == "FAIL"
    # run token cap: 26 cases at the 200000 default is 5.2M > 5M
    g = gate(adm("live", live=26), "30")
    assert g["status"] == "FAIL" and g["sum_max_total_tokens"] == 5_200_000
    assert gate(adm("live", live=25), "30")["status"] == "OK"
    assert gate(adm("live", live=2, max_total_tokens=3_000_000), "5")["status"] == "FAIL"


def test_live_gate_cli_exit_status(tmp_path, capsys):
    p = tmp_path / "ADMISSION.json"
    p.write_text(json.dumps(adm("live", live=1)), encoding="utf-8")
    out = tmp_path / "gate.json"
    assert ru.main(["live-gate", str(p), "--live-max-cases", "", "--out", str(out)]) == 1
    assert json.loads(out.read_text())["status"] == "FAIL"
    assert ru.main(["live-gate", str(p), "--live-max-cases", "1", "--out", str(out)]) == 0


# ------------------------------------------------------------------ rerun gate (admission)

EARLIEST = "2026-10-15T12:01:00+00:00"
AFTER = ru.parse_ts("2026-10-15T12:05:00+00:00")
BEFORE = ru.parse_ts("2026-10-15T12:00:30+00:00")


def prior(eligible=("L0",), attempts=None, earliest=EARLIEST):
    attempts = attempts or {}
    return {
        "run_url": "https://example.invalid/runs/1",
        "cases": [
            {"case_id": cid, "attempt": n, "attempts": [{"attempt": i + 1} for i in range(n)]}
            for cid, n in attempts.items()
        ],
        "live": {
            "rerun_eligible": [
                {"case_id": cid, "attempt": 1, "earliest_rerun_utc": earliest} for cid in eligible
            ],
        },
    }


def test_rerun_gate_passes_an_eligible_case_after_its_earliest_time():
    g = ru.rerun_gate(adm("L0", live=1, offline=0), prior(attempts={"L0": 1}), "1", AFTER)
    assert g["status"] == "OK" and g["problems"] == [] and g["selected"] == ["L0"]
    assert g["cases"][0]["prior_attempts"] == 1 and g["original_results_present"] is True


@pytest.mark.parametrize(
    ("admission", "prior_results", "now", "reason"),
    [
        (adm("L0,L1", live=2, offline=0), prior(), AFTER, "L1: not on the original run's"),
        (
            adm("L0", live=1, offline=0),
            prior(attempts={"L0": 2}),
            AFTER,
            "L0: already re-dispatched in the original (2 attempts",
        ),
        (adm("L0", live=1, offline=0), prior(), BEFORE, "L0: too early: earliest rerun"),
        (adm("L0", live=1, offline=0), prior(earliest=None), AFTER, "no earliest rerun time"),
        (adm("L0", live=1, offline=0), None, AFTER, "no readable simkit-results RESULTS.json"),
        (adm("L0", live=0, offline=0), prior(), AFTER, "selects no case"),
    ],
    ids=["not-eligible", "second-rerun", "too-early", "no-earliest", "missing-original", "none"],
)
def test_rerun_gate_refuses(admission, prior_results, now, reason):
    g = ru.rerun_gate(admission, prior_results, "1", now)
    assert g["status"] == "FAIL"
    assert any(reason in p for p in g["problems"]), g["problems"]


def test_rerun_gate_counts_attempts_from_the_live_section_too():
    p = prior()
    p["live"]["attempts_by_case"] = {"L0": [{"attempt": 1}, {"attempt": 2}]}
    g = ru.rerun_gate(adm("L0", live=1, offline=0), p, "1", AFTER)
    assert g["status"] == "FAIL" and g["cases"][0]["prior_attempts"] == 2


@pytest.mark.parametrize(
    "content",
    ["{not json", "[]", '"RESULTS"', ""],
    ids=["corrupt", "json-list", "json-string", "empty"],
)
def test_rerun_gate_refuses_an_unreadable_original_record_fail_closed(tmp_path, content):
    a = tmp_path / "ADMISSION.json"
    a.write_text(json.dumps(adm("L0", live=1, offline=0)), encoding="utf-8")
    r = tmp_path / "RESULTS.json"
    r.write_text(content, encoding="utf-8")
    out = tmp_path / "RERUN-GATE.json"
    argv = ["rerun-gate", str(a), "--prior-results", str(r), "--rerun-of-run-id", "9"]
    assert ru.main([*argv, "--out", str(out), "--now", AFTER.isoformat()]) == 1
    rec = json.loads(out.read_text(encoding="utf-8"))
    assert rec["status"] == "FAIL" and rec["original_results_present"] is False
    assert any("no readable simkit-results RESULTS.json" in p for p in rec["problems"])


def test_rerun_gate_refuses_an_unreadable_admission(tmp_path):
    g = ru.rerun_gate(None, prior(attempts={"L0": 1}), "1", AFTER)
    assert g["status"] == "FAIL" and g["selected"] == []
    assert any("ADMISSION.json absent or unreadable" in p for p in g["problems"])
    a = tmp_path / "ADMISSION.json"
    a.write_text("{not json", encoding="utf-8")
    r = tmp_path / "RESULTS.json"
    r.write_text(json.dumps(prior(attempts={"L0": 1})), encoding="utf-8")
    argv = ["rerun-gate", str(a), "--prior-results", str(r), "--rerun-of-run-id", "9"]
    assert ru.main([*argv, "--out", str(tmp_path / "g.json"), "--now", AFTER.isoformat()]) == 1


def test_rerun_gate_cli(tmp_path, capsys):
    a = tmp_path / "ADMISSION.json"
    a.write_text(json.dumps(adm("L0", live=1, offline=0)), encoding="utf-8")
    r = tmp_path / "prior" / "RESULTS.json"
    out = tmp_path / "out" / "RERUN-GATE.json"
    base = ["rerun-gate", str(a), "--prior-results", str(r), "--rerun-of-run-id", "77"]
    # the original's results are missing: refused, with the reason recorded and printed
    assert ru.main([*base, "--out", str(out), "--now", AFTER.isoformat()]) == 1
    rec = json.loads(out.read_text(encoding="utf-8"))
    assert rec["status"] == "FAIL" and rec["original_results_present"] is False
    assert rec["rerun_of_run_id"] == "77" and "Refused" in capsys.readouterr().out
    r.parent.mkdir()
    r.write_text(json.dumps(prior()), encoding="utf-8")
    assert ru.main([*base, "--out", str(out), "--now", BEFORE.isoformat()]) == 1
    assert ru.main([*base, "--out", str(out), "--now", AFTER.isoformat()]) == 0
    assert json.loads(out.read_text(encoding="utf-8"))["status"] == "OK"
    assert "Rerun gate: OK" in capsys.readouterr().out
