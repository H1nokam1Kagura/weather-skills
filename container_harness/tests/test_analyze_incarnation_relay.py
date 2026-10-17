"""Wave 8 A2 (decision D4): relay attribution for a forked controller incarnation.

Incarnation 1 of a harness_stop_controller case is a fork of the traced root, so its role calls
to the declared relay endpoint come from its own pid. They are role transport only when the
interrupt broker declared that incarnation and the trace confirms it; otherwise
data_fetch_attempts is MISSING, never 0 and never a fetch. Synthetic strace text only (no docker).
"""

import sys
from pathlib import Path

import pytest

HERE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(HERE.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import analyze_trace as A  # noqa: E402
from test_analyze_trace import RELAY, ROOT, Capture, child, start  # noqa: E402
from test_interrupt_broker import (  # noqa: E402
    BROKER_PID,
    STOP_STATUS,
    broker_record,
    ts_now,
    write_record,
)

INC, TOOL, TOOL2 = 101, 102, 104
RELAY_CONNECT = (
    "connect(3<socket:[77]>, {sa_family=AF_INET, sin_port=htons(18080), "
    'sin_addr=inet_addr("172.30.0.1")}, 16) = 0'
)
OTHER_CONNECT = (
    "connect(4<socket:[78]>, {sa_family=AF_INET, sin_port=htons(443), "
    'sin_addr=inet_addr("93.184.216.34")}, 16) = -1 ENETUNREACH (Network is unreachable)'
)


def relay_capture(
    tmp_path,
    *,
    inc_lines=(RELAY_CONNECT, RELAY_CONNECT),
    tool_lines=(),
    tool2_lines=(),
    incarnation_killed=True,
):
    """Root 100 connects to the relay, forks incarnation 1 (101), which makes its own role calls
    (inc_lines) and runs a tool (102, tool_lines); the broker (outside the tree) freezes and
    SIGKILLs 101 and 102. Incarnation 2 runs in the root: another relay connect, then a direct
    tool child (104, tool2_lines)."""
    cap = start(Capture(tmp_path))
    cap.status.update(STOP_STATUS)
    cap.status["relay_endpoint"] = RELAY
    cap(ROOT, RELAY_CONNECT)
    child(cap, ROOT, INC, vfork=False)  # os.fork: incarnation 1
    for line in inc_lines:
        cap(INC, line)
    child(cap, INC, TOOL)
    cap(TOOL, 'execve("/usr/bin/cdo", ["cdo"], 0x7f /* 9 vars */) = 0')
    for line in tool_lines:
        cap(TOOL, line)
    requested_at = ts_now(cap) + 500
    for pid in (INC, TOOL):
        cap(
            pid,
            f"--- SIGSTOP {{si_signo=SIGSTOP, si_code=SI_USER, si_pid={BROKER_PID}, si_uid=0}} ---",
        )
        cap(pid, "--- stopped by SIGSTOP ---")
    cap(TOOL, "+++ killed by SIGKILL +++")
    if incarnation_killed:
        cap(INC, "+++ killed by SIGKILL +++")
    else:
        cap.exit(INC)
    cap(ROOT, RELAY_CONNECT)  # incarnation 2, in-process
    child(cap, ROOT, TOOL2)
    for line in tool2_lines:
        cap(TOOL2, line)
    cap.exit(TOOL2)
    cap.exit(ROOT)
    return cap, requested_at


def record(requested_at, **stop):
    stop.setdefault("descendants", [TOOL])
    stop.setdefault("stopped", [INC, TOOL])
    stop.setdefault("killed", [INC, TOOL])
    stop.setdefault(
        "output_bytes",
        {str(INC): {"stdout": None, "stderr": None}, str(TOOL): {"stdout": 0, "stderr": 0}},
    )
    return broker_record(requested_at, **stop)


def pids(entries):
    return [e["pid"] for e in entries]


def test_confirmed_incarnation_relay_connects_are_role_transport_not_fetches(tmp_path):
    cap, requested_at = relay_capture(tmp_path)
    write_record(cap, record(requested_at))
    ev = cap.write()
    assert ev["missing"] == {}
    assert ev[A.BROKER_EVIDENCE_KEY]["status"] == "OK"
    assert ev[A.BROKER_EVIDENCE_KEY]["incarnation_pids"] == [INC]
    assert ev["harness_observations"]["data_fetch_attempts"] == 0
    notes = ev["harness_notes"]
    assert pids(notes["role_transport_connects"]) == [ROOT, INC, INC, ROOT]  # time order
    assert notes["network_attempts"] == []
    assert notes["incarnation_role_transport_connects"] == 2
    assert "unconfirmed_incarnation_relay_connects" not in notes
    assert "trace-confirmed incarnation" in notes["semantics"]["data_fetch_attempts"]


def test_the_same_trace_without_the_fix_would_count_the_role_calls_as_fetches(tmp_path):
    """Why A2 exists: without broker engagement the fork's relay calls are data fetches."""
    cap, _ = relay_capture(tmp_path)
    for key in STOP_STATUS:
        cap.status.pop(key)
    ev = cap.write()
    assert ev["harness_observations"]["data_fetch_attempts"] == 2
    assert pids(ev["harness_notes"]["network_attempts"]) == [INC, INC]


@pytest.mark.parametrize(
    "setup,needle",
    [
        (lambda cap, req: None, "record absent"),
        (lambda cap, req: write_record(cap, record(req, pid=TOOL2)), "not confirmed"),
        (lambda cap, req: write_record(cap, record(req + 10**12)), "not confirmed"),
        (lambda cap, req: write_record(cap, record(req, verified=False, killed=[])), "refused"),
        (lambda cap, req: write_record(cap, {**record(req), "stops": []}), A.NO_STOP_REASON),
    ],
)
def test_unconfirmed_incarnation_reaching_the_relay_makes_fetches_missing(tmp_path, setup, needle):
    cap, requested_at = relay_capture(tmp_path)
    setup(cap, requested_at)
    ev = cap.write()
    assert needle in ev["missing"][A.CONCURRENCY_FIELD]
    assert "data_fetch_attempts" not in ev["harness_observations"]  # absent, never 0
    reason = ev["missing"][A.FETCH_FIELD]
    assert f"[{INC}]" in reason and "trace-confirmed incarnation" in reason
    notes = ev["harness_notes"]
    # never a fetch, never role transport: listed on their own
    assert pids(notes["unconfirmed_incarnation_relay_connects"]) == [INC, INC]
    assert notes["network_attempts"] == []
    assert pids(notes["role_transport_connects"]) == [ROOT, ROOT]


def test_declared_incarnation_the_trace_shows_exiting_normally_makes_fetches_missing(tmp_path):
    cap, requested_at = relay_capture(tmp_path, incarnation_killed=False)
    write_record(cap, record(requested_at))
    ev = cap.write()
    assert ev[A.BROKER_EVIDENCE_KEY]["stops"][0]["trace_confirmed"] is False
    assert "data_fetch_attempts" not in ev["harness_observations"]
    assert f"[{INC}]" in ev["missing"][A.FETCH_FIELD]


def test_tool_under_the_incarnation_reaching_the_relay_is_still_a_fetch(tmp_path):
    cap, requested_at = relay_capture(tmp_path, tool_lines=(RELAY_CONNECT,))
    write_record(cap, record(requested_at))
    ev = cap.write()
    assert ev["missing"] == {}
    assert ev["harness_observations"]["data_fetch_attempts"] == 1
    notes = ev["harness_notes"]
    assert pids(notes["network_attempts"]) == [TOOL]
    assert notes["network_attempts"][0]["destination"] == RELAY
    assert pids(notes["role_transport_connects"]) == [ROOT, INC, INC, ROOT]


def test_incarnation_non_relay_destination_is_still_a_fetch(tmp_path):
    cap, requested_at = relay_capture(tmp_path, inc_lines=(RELAY_CONNECT, OTHER_CONNECT))
    write_record(cap, record(requested_at))
    ev = cap.write()
    assert ev["missing"] == {}
    assert ev["harness_observations"]["data_fetch_attempts"] == 1
    notes = ev["harness_notes"]
    assert [(a["pid"], a["destination"]) for a in notes["network_attempts"]] == [
        (INC, "93.184.216.34:443")
    ]
    assert pids(notes["role_transport_connects"]) == [ROOT, INC, ROOT]


def test_tool_under_the_incarnation_non_relay_destination_is_still_a_fetch(tmp_path):
    cap, requested_at = relay_capture(tmp_path, tool_lines=(OTHER_CONNECT,))
    write_record(cap, record(requested_at))
    ev = cap.write()
    assert ev["missing"] == {}
    assert ev["harness_observations"]["data_fetch_attempts"] == 1
    assert pids(ev["harness_notes"]["network_attempts"]) == [TOOL]


def test_a_root_child_that_is_no_confirmed_incarnation_reaching_the_relay_is_missing(tmp_path):
    """D4: a direct child of the root that the broker did not declare (here incarnation 2's
    tool 104) could be an unconfirmed incarnation, so its relay connect is undecidable."""
    cap, requested_at = relay_capture(tmp_path, tool2_lines=(RELAY_CONNECT,))
    write_record(cap, record(requested_at))
    ev = cap.write()
    assert ev["harness_observations"]["max_concurrent_children"] == 1  # concurrency unaffected
    assert "data_fetch_attempts" not in ev["harness_observations"]
    assert f"[{TOOL2}]" in ev["missing"][A.FETCH_FIELD]
    assert pids(ev["harness_notes"]["unconfirmed_incarnation_relay_connects"]) == [TOOL2]


def test_root_relay_connects_are_unchanged_by_the_broker_section(tmp_path):
    cap, requested_at = relay_capture(tmp_path / "broker")
    write_record(cap, record(requested_at))
    with_broker = cap.write()["harness_notes"]["role_transport_connects"]
    plain, _ = relay_capture(tmp_path / "plain")
    for key in STOP_STATUS:
        plain.status.pop(key)
    without = plain.write()["harness_notes"]["role_transport_connects"]
    assert [e for e in with_broker if e["pid"] == ROOT] == without
    assert pids(without) == [ROOT, ROOT]


def test_a_non_network_fetch_gap_keeps_its_own_reason(tmp_path):
    cap, requested_at = relay_capture(tmp_path)
    cap.status.pop("trace_network")
    write_record(cap, record(requested_at))
    ev = cap.write()
    assert ev["missing"][A.FETCH_FIELD] == "network syscalls were not in the traced set"
    assert pids(ev["harness_notes"]["role_transport_connects"]) == [ROOT, INC, INC, ROOT]
