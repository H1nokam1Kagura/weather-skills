"""Live-role relay usage, spend caps and gateway-fault classification (host, stdlib only).

Library (cases_report.py) and CLI (simkit-cases.yml):

    python container_harness/relay_usage.py caps --out CAPS.json
        reads the case's admission matrix row (JSON) from env MATRIX_ROW; writes CAPS.json and
        prints ``name=value`` lines for $GITHUB_OUTPUT (max_requests, max_total_tokens,
        budget_usd, roles)
    python container_harness/relay_usage.py live-gate ADMISSION.json --live-max-cases N
                                                     --out LIVE-GATE.json
        exit 1 when the run's live selection is not authorized (see live_gate)
    python container_harness/relay_usage.py summarize RELAY_DIR
        prints usage_from_relay_dir(RELAY_DIR) (diagnostic)

Caps (LIVE-WAVE-BRIEF "Spend guard"): admission computes them from the frozen roles. The
matrix fields ``expected_calls``, ``max_requests``, ``max_total_tokens``, ``max_model_usd`` and
``live_roles`` are filled by a later sub-wave (4b, case_driver.py); until a row carries them the
conservative defaults below apply, and every cap records where it came from. The relay enforces
them (sub-wave 1, relay_ci.sh / kong_relay_host.py); this module only chooses and reports them.

Classification (owner ruling live_spend_authorization_2026-10-15.rerun_on_gateway_fault):
  LIVE_TRANSPORT_FAULT  every recorded fault is a transient gateway fault: HTTP 429, a 5xx,
                        a timeout, a connection reset, or an UNKNOWN outcome with no gateway
                        response. Never counted as a failure of the system under test.
  RELAY_GUARD_ABORT     the relay's spend guard stopped the case (``relay_spend_cap`` refusal or
                        a ``spend_cap_reached`` audit event). Deterministic: never rerun.
  None                  no fault, or at least one deterministic fault (4xx other than 429,
                        a payload over the body cap, any other relay refusal, a relay internal
                        error).
"""

import argparse
import json
import os
import re
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path

LIVE_TRANSPORT_FAULT = "LIVE_TRANSPORT_FAULT"
RELAY_GUARD_ABORT = "RELAY_GUARD_ABORT"
LABELS = (LIVE_TRANSPORT_FAULT, RELAY_GUARD_ABORT)

RULING_ID = "OWNER-RULINGS live_spend_authorization_2026-10-15.rerun_on_gateway_fault"
RULING_SUMMARY = (
    "At most ONE re-dispatch of a single case, only when its failure is a recorded transient "
    "gateway fault (HTTP 429, 5xx, timeout, connection reset; relay outcome UNKNOWN with no "
    "gateway response), after waiting at least the gateway's retry-after or 60 s. Never for a "
    "deterministic failure: 4xx other than 429, payload over the body cap, relay_spend_cap / "
    "RELAY_GUARD_ABORT, a scored assertion failure, or a harness error. The original attempt "
    "and the rerun are both kept and reported; the rerun never replaces the first result."
)
MIN_RERUN_WAIT_SECONDS = 60
MAX_ATTEMPTS = 2  # the original plus at most one re-dispatch

# Conservative per-case defaults until the admission matrix carries the computed caps
# (sub-wave 4b). HARD_* are ceilings a matrix value is clamped to.
DEFAULT_MAX_REQUESTS = 24
DEFAULT_MAX_TOTAL_TOKENS = 200_000
DEFAULT_BUDGET_USD = 5
HARD_MAX_REQUESTS = 24
HARD_BUDGET_USD = 5
RUN_TOKEN_CAP = 5_000_000  # run level: admission fails above this sum of per-case token caps
LIVE_MAX_PARALLEL = 3  # shared 500K tokens/min pool (LIVE-WAVE-BRIEF "Cost model")
DEFAULT_MAX_PARALLEL = 8
# Today's runnable live cases are all-live; a row without live_roles gets every relay role.
DEFAULT_LIVE_ROLES = (
    "compiler",
    "planner",
    "repairer",
    "reviewer",
    "evidence_checker",
    "verifier",
    "router",
)
ROLE_NAME = re.compile(r"^[a-z][a-z_]{0,39}$")

# Anthropic list price per MTok (LIVE-WAVE-BRIEF "Receipts"); an estimate, not Kong billing.
LIST_PRICE_PER_MTOK = {"claude-sonnet-4-6": (3.0, 15.0)}
EST_USD_BASIS = "Anthropic list-price estimate over observed usage; Kong billing not observed"

SPEND_CAP_REASON = "relay_spend_cap"
SPEND_CAP_EVENT = "spend_cap_reached"
TIMEOUT_MARKERS = ("timeout", "timedout")
RESET_MARKERS = ("connection", "reset", "remotedisconnected", "readerror", "brokenpipe")


# ------------------------------------------------------------------------------- caps


def _positive_int(value):
    if type(value) is bool:
        return None
    try:
        number = int(value)
    except (TypeError, ValueError):
        return None
    return number if number > 0 and str(value).strip() == str(number) else None


def _positive_number(value):
    if type(value) is bool or not isinstance(value, (int, float, str)):
        return None
    try:
        number = float(value)
    except ValueError:
        return None
    return number if number > 0 and number == number and number != float("inf") else None


def case_caps(row):
    """Relay caps for one live case from its admission matrix row, each with its source."""
    row = row or {}
    source = {}
    requests = _positive_int(row.get("max_requests"))
    expected = _positive_int(row.get("expected_calls"))
    if requests is not None:
        source["max_requests"] = "matrix"
    elif expected is not None:
        requests = min(HARD_MAX_REQUESTS, max(8, 2 * expected))
        source["max_requests"] = "expected_calls"
    else:
        requests = DEFAULT_MAX_REQUESTS
        source["max_requests"] = "default"
    if requests > HARD_MAX_REQUESTS:
        requests = HARD_MAX_REQUESTS
        source["max_requests"] += f" (clamped to {HARD_MAX_REQUESTS})"
    tokens = _positive_int(row.get("max_total_tokens"))
    source["max_total_tokens"] = "matrix" if tokens is not None else "default"
    tokens = tokens if tokens is not None else DEFAULT_MAX_TOTAL_TOKENS
    budget = _positive_number(row.get("max_model_usd"))
    source["budget_usd"] = "matrix" if budget is not None else "default"
    budget = budget if budget is not None else DEFAULT_BUDGET_USD
    if budget > HARD_BUDGET_USD:
        budget = HARD_BUDGET_USD
        source["budget_usd"] += f" (clamped to {HARD_BUDGET_USD})"
    raw_roles = row.get("live_roles")
    if isinstance(raw_roles, str):
        raw_roles = raw_roles.split()
    if raw_roles:
        roles = sorted({str(r) for r in raw_roles})
        bad = [r for r in roles if not ROLE_NAME.match(r)]
        if bad:
            raise ValueError(f"invalid live role names in the matrix row: {bad}")
        source["roles"] = "matrix"
    else:
        roles = list(DEFAULT_LIVE_ROLES)
        source["roles"] = "default"
    return {
        "schema": "simkit.relay-caps/1",
        "case_id": row.get("case_id"),
        "expected_calls": expected,
        "max_requests": requests,
        "max_total_tokens": tokens,
        "budget_usd": budget,
        "roles": roles,
        "source": source,
        "note": "matrix cap fields are filled by sub-wave 4b (case_driver); defaults until then",
    }


def caps_outputs(caps):
    """``name=value`` lines for $GITHUB_OUTPUT (values are ints / a number / role names)."""
    budget = caps["budget_usd"]
    budget = int(budget) if float(budget).is_integer() else budget
    return [
        f"max_requests={caps['max_requests']}",
        f"max_total_tokens={caps['max_total_tokens']}",
        f"budget_usd={budget}",
        f"roles={' '.join(caps['roles'])}",
    ]


def max_parallel(requested, live_count):
    """Case-job max-parallel: the input (default 8); live runs are capped at 3."""
    value = _positive_int(requested) if requested not in (None, "") else DEFAULT_MAX_PARALLEL
    if value is None:
        raise ValueError(f"max_parallel must be a positive integer, got {requested!r}")
    return min(value, LIVE_MAX_PARALLEL) if live_count else value


def live_rows(admission):
    return [r for r in admission.get("matrix") or [] if r.get("role_mode") == "live"]


def live_gate(admission, live_max_cases):
    """Run-level spend authorization for the selected live-role cases (paid Kong calls).

    FAIL when: selector 'live' without an explicit live_max_cases; any live case selected
    (by 'live' or by explicit ids) without one; more live cases selected than it allows; or the
    sum of their per-case token caps above RUN_TOKEN_CAP."""
    rows = live_rows(admission)
    selector = admission.get("selector")
    given = live_max_cases not in (None, "")
    limit = _positive_int(live_max_cases) if given else None
    caps = [case_caps(r) for r in rows]
    tokens = sum(c["max_total_tokens"] for c in caps)
    problems = []
    if given and limit is None:
        problems.append(f"live_max_cases must be a positive integer, got {live_max_cases!r}")
    elif (selector == "live" or rows) and not given:
        problems.append(
            "live-role cases make paid Kong calls: set the workflow input live_max_cases "
            f"(selector {selector!r} selects {len(rows)} live cases)"
        )
    elif limit is not None and len(rows) > limit:
        problems.append(f"{len(rows)} live cases selected, above live_max_cases {limit}")
    if tokens > RUN_TOKEN_CAP:
        problems.append(f"sum of per-case token caps {tokens} is above the run cap {RUN_TOKEN_CAP}")
    return {
        "schema": "simkit.live-gate/1",
        "selector": selector,
        "live_max_cases": limit if limit is not None else (live_max_cases or None),
        "live_selected": len(rows),
        "live_case_ids": [r.get("case_id") for r in rows],
        "sum_max_total_tokens": tokens,
        "run_token_cap": RUN_TOKEN_CAP,
        "max_parallel_live": LIVE_MAX_PARALLEL,
        "status": "FAIL" if problems else "OK",
        "problems": problems,
    }


# ------------------------------------------------------------------------------- audit


def read_audit(path):
    """Audit events from relay-audit.jsonl; None when the file is absent. A line that does not
    parse is kept as an ``unparsed_line`` event (counted, never silently dropped)."""
    path = Path(path)
    if not path.is_file():
        return None
    events = []
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        if not line.strip():
            continue
        try:
            event = json.loads(line)
        except ValueError:
            event = None
        events.append(event if isinstance(event, dict) else {"event": "unparsed_line"})
    return events


def _int(value):
    return value if type(value) is int else None


def _lower(value):
    return str(value or "").lower().replace("_", "").replace(" ", "")


def fault_kind(event):
    """(kind, transient, guard) for one audit event; None when it records no fault."""
    name = event.get("event")
    reason = str(event.get("reason") or "")
    if name == SPEND_CAP_EVENT:
        return "spend_cap_reached", False, True
    if name == "unparsed_line":
        return "unparsed_audit_line", False, False
    if name == "auth_rejected":
        return "auth_rejected", False, False
    if name != "ask":
        return None
    outcome = event.get("outcome")
    if outcome == "SUCCESS":
        return None
    if event.get("reason_code") == SPEND_CAP_REASON or SPEND_CAP_REASON in reason:
        return SPEND_CAP_REASON, False, True
    if outcome == "UNKNOWN":
        status = _int(event.get("status"))  # the GATEWAY's HTTP status, when it answered
        if status == 429:
            return "gateway_429", True, False
        if status is not None and 500 <= status <= 599:
            return "gateway_5xx", True, False
        if status is not None and 400 <= status <= 499:
            return "gateway_4xx", False, False
        if status is not None:
            return "gateway_status_other", False, False
        if reason == "relay internal error":
            return "relay_internal_error", False, False
        error = _lower(event.get("error_type")) + " " + _lower(reason)
        if any(m in error for m in TIMEOUT_MARKERS):
            return "timeout", True, False
        if any(m in error for m in RESET_MARKERS):
            return "connection_reset", True, False
        return "unknown_no_gateway_response", True, False
    http = _int(event.get("http_status"))
    if http == 413 or "body over" in reason:
        return "payload_over_body_cap", False, False
    return "relay_refused", False, False


def _retry_after(event):
    value = event.get("retry_after")
    try:
        seconds = float(value)
    except (TypeError, ValueError):
        return None
    return seconds if seconds >= 0 else None


def faults_of(events):
    out = []
    for event in events:
        kind = fault_kind(event)
        if kind is None:
            continue
        out.append(
            {
                "kind": kind[0],
                "transient": kind[1],
                "guard": kind[2],
                "event": event.get("event"),
                "ts": event.get("ts"),
                "relay_id": event.get("relay_id"),
                "role": event.get("role"),
                "outcome": event.get("outcome"),
                "http_status": event.get("http_status"),
                "gateway_status": event.get("status"),
                "error_type": event.get("error_type"),
                "reason": event.get("reason"),
                "reason_code": event.get("reason_code"),
                "retry_after": _retry_after(event),
            }
        )
    return out


def label_of(faults):
    if any(f["guard"] for f in faults):
        return RELAY_GUARD_ABORT
    if faults and all(f["transient"] for f in faults):
        return LIVE_TRANSPORT_FAULT
    return None


def _sent(event):
    if event.get("event") != "ask":
        return False
    if event.get("outcome") in ("SUCCESS", "UNKNOWN"):
        return True
    return event.get("sent_to_gateway") is True


def _price(model, input_tokens, output_tokens):
    rate = LIST_PRICE_PER_MTOK.get(model)
    if rate is None:
        return None
    return (input_tokens * rate[0] + output_tokens * rate[1]) / 1e6


def usage_from_events(events, caps=None):
    """{requests, input_tokens, output_tokens, est_usd, cap_state, faults[], label, ...}."""
    if events is None:
        return {
            "audit_present": False,
            "requests": None,
            "input_tokens": None,
            "output_tokens": None,
            "est_usd": None,
            "est_usd_basis": EST_USD_BASIS,
            "cap_state": "NO_AUDIT",
            "caps": caps,
            "faults": [],
            "label": None,
        }
    start = next((e for e in events if e.get("event") == "relay_start"), {})
    asks = [e for e in events if e.get("event") == "ask"]
    sent = [e for e in asks if _sent(e)]
    input_tokens = output_tokens = 0
    est = 0.0
    unpriced = unobserved = 0
    reported = [e["cost_usd"] for e in asks if isinstance(e.get("cost_usd"), (int, float))]
    for e in sent:
        usage = e.get("usage") if isinstance(e.get("usage"), dict) else None
        tin = _int((usage or {}).get("input_tokens"))
        tout = _int((usage or {}).get("output_tokens"))
        if tin is None or tout is None:
            unobserved += 1
            continue
        input_tokens += tin
        output_tokens += tout
        cost = _price(e.get("model_observed") or start.get("model"), tin, tout)
        if cost is None:
            unpriced += 1
        else:
            est += cost
    faults = faults_of(events)
    stops = [e.get("reason") for e in events if e.get("event") == "relay_stop"]
    if any(f["guard"] for f in faults):
        cap_state = "SPEND_CAP_REACHED"
    elif "max_requests reached" in stops:
        cap_state = "MAX_REQUESTS_REACHED"
    elif "run_seconds elapsed" in stops:
        cap_state = "RUN_SECONDS_ELAPSED"
    else:
        cap_state = "UNDER_CAP"
    return {
        "audit_present": True,
        "relay_run_id": start.get("run_id"),
        "relay_start_ts": start.get("ts"),
        "model": start.get("model"),
        "asks": len(asks),
        "requests": len(sent),
        "refused_before_send": len(asks) - len(sent),
        "input_tokens": input_tokens,
        "output_tokens": output_tokens,
        "usage_unobserved_requests": unobserved,
        "est_usd": round(est, 6) if not unpriced else None,
        "est_usd_unpriced_requests": unpriced,
        "est_usd_basis": EST_USD_BASIS,
        "relay_reported_cost_usd": round(sum(reported), 6) if reported else None,
        "cap_state": cap_state,
        "relay_stop": stops,
        "caps": caps,
        "faults": faults,
        "label": label_of(faults),
    }


def usage_from_relay_dir(relay_dir):
    """Usage for one case from its copied relay dir (CASE_OUT/relay)."""
    relay_dir = Path(relay_dir)
    caps = None
    caps_path = relay_dir / "relay-caps.json"
    if caps_path.is_file():
        try:
            caps = json.loads(caps_path.read_text(encoding="utf-8"))
        except ValueError:
            caps = None
    return usage_from_events(read_audit(relay_dir / "relay-audit.jsonl"), caps)


# ------------------------------------------------------------------------------- rerun


def parse_ts(value):
    """An audit timestamp as an aware datetime (naive is read as UTC); None if unparseable."""
    try:
        parsed = datetime.fromisoformat(str(value))
    except (TypeError, ValueError):
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)


def rerun_decision(status, label, clean, attempt):
    """(eligible, reason) under RULING_ID for one case attempt."""
    if label == RELAY_GUARD_ABORT:
        return False, "RELAY_GUARD_ABORT: spend-cap abort is deterministic"
    if label != LIVE_TRANSPORT_FAULT:
        return False, "no transient gateway fault recorded (or a deterministic fault too)"
    if status == "HARNESS_ERROR":
        return False, "harness error"
    if status == "EXECUTED" and clean:
        return False, "case passed: nothing to rerun"
    if attempt >= MAX_ATTEMPTS:
        return False, "already re-dispatched once (at most one re-dispatch per case)"
    return True, "transient gateway fault"


def rerun_record(case_id, attempt, usage):
    """The rerun-eligible entry: which faults, and the earliest moment a rerun may start."""
    faults = usage.get("faults") or []
    waits = [f["retry_after"] for f in faults if f.get("retry_after") is not None]
    wait = max([MIN_RERUN_WAIT_SECONDS, *waits])
    times = [t for t in (parse_ts(f.get("ts")) for f in faults) if t is not None]
    last = max(times) if times else None
    return {
        "case_id": case_id,
        "attempt": attempt,
        "fault_kinds": sorted({f["kind"] for f in faults}),
        "last_fault_ts": last.isoformat() if last else None,
        "min_wait_seconds": wait,
        "earliest_rerun_utc": (last + timedelta(seconds=wait)).isoformat() if last else None,
        "ruling": RULING_ID,
    }


# ------------------------------------------------------------------------------- CLI


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = ap.add_subparsers(dest="cmd", required=True)
    c = sub.add_parser("caps")
    c.add_argument("--out", required=True)
    g = sub.add_parser("live-gate")
    g.add_argument("admission")
    g.add_argument("--live-max-cases", default="")
    g.add_argument("--out", required=True)
    s = sub.add_parser("summarize")
    s.add_argument("relay_dir")
    args = ap.parse_args(argv)
    if args.cmd == "caps":
        caps = case_caps(json.loads(os.environ.get("MATRIX_ROW") or "{}"))
        Path(args.out).write_text(json.dumps(caps, indent=1) + "\n", encoding="utf-8")
        print("\n".join(caps_outputs(caps)))
        return 0
    if args.cmd == "live-gate":
        admission = json.loads(Path(args.admission).read_text(encoding="utf-8"))
        gate = live_gate(admission, args.live_max_cases)
        Path(args.out).write_text(json.dumps(gate, indent=1) + "\n", encoding="utf-8")
        print("live gate:", json.dumps(gate))
        return 1 if gate["status"] == "FAIL" else 0
    print(json.dumps(usage_from_relay_dir(args.relay_dir), indent=1))
    return 0


if __name__ == "__main__":
    sys.exit(main())
