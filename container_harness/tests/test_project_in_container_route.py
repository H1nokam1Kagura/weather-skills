"""Wave 7 container-route gap: the harness resume-route record reaches the SCORED in-container
projection (project_in_container.INPUT_KEYS), offline (no docker, no fork).

project_run is replaced by a stand-in that calls the REAL resume_projection helpers on the
harness inputs it is handed (harness_fields, killed_output_bytes, native_controller,
project_symbols) and applies the same observed-controller rule as
Projection.observed_signal_capture, so these tests check what project_in_container passes
through and what it discloses or withholds afterwards; never evidence of a real stop.
"""

import json
import sys
from pathlib import Path

import pytest

HERE = Path(__file__).resolve().parent
REPO = HERE.parents[1]
sys.path[:0] = [str(REPO), str(REPO / "tests")]

from adapter_support import evidence_projection  # noqa: E402
from adapter_support import resume_projection as rsp  # noqa: E402
from container_harness import analyze_trace as A  # noqa: E402
from container_harness import interrupt_broker as B  # noqa: E402
from container_harness import project_in_container as pic  # noqa: E402

ENTRY, ROOT, INC1, TOOL = 50, 100, 101, 102
DEFAULT_GAP = "no process_exit event: byte counts unobserved"


def route_record(kill_route="in_process", output_bytes=None):
    return {
        "schema": rsp.ROUTE_SCHEMA,
        "route": "fork",
        "root_pid": ROOT,
        "incarnations": [
            {"incarnation": 1, "pid": INC1, "parent_pid": ROOT},
            {"incarnation": 2, "pid": ROOT, "parent_pid": ENTRY},
        ],
        "kill": {
            "route": kill_route,
            "pid": INC1,
            "ok": True,
            "verified": True,
            "descendants": [TOOL],
            "killed": [INC1, TOOL],
            "output_bytes": output_bytes
            if output_bytes is not None
            else {
                str(INC1): {"stdout": None, "stderr": None},
                str(TOOL): {"stdout": 12, "stderr": 3},
            },
            "stop_index": 0,
            "errors": [],
            "output_bytes_basis": B.OUTPUT_BYTES_BASIS,
        },
        "stop": {"trigger": "process_launch"},
        "onset_partial": {
            "present_at_interrupt": True,
            "observed_by": "harness parent, after the kill of incarnation 1 and before any resume",
            "content_hash": "onset-partial-hash",
        },
    }


def broker_section(**stop):
    entry = {
        "request_id": "r1",
        "pid": INC1,
        "verified": True,
        "ok": True,
        "descendants": [TOOL],
        "killed": [INC1, TOOL],
        "output_bytes": {
            str(INC1): {"stdout": None, "stderr": None},
            str(TOOL): {"stdout": 12, "stderr": 3},
        },
        "trace_confirmed": True,
        "trace_confirmed_kills": [INC1, TOOL],
        "trace_unconfirmed_kills": [],
    }
    entry.update(stop)
    return {"source": "analyzer", "status": "OK", "stops": [entry], "incarnation_pids": [INC1]}


class StandIn:
    """The parts of evidence_projection.Projection the resume_projection helpers read."""

    def __init__(self, harness):
        self.h = harness
        self.notes, self.missing = [], []

    def gap(self, path, capability, reason):
        self.missing.append({"path": path, "capability": capability, "reason": reason})

    def initial_controller_pid(self):
        return INC1

    def recovery_list(self, name):
        return ([{"controller_pid": ROOT}] if name == "resume" else []), None


def fake_project_run(seen):
    def project_run(run_dir, harness):
        seen.update(harness)
        projection = StandIn(harness)
        receipts = [{"serial": 1, "pid": TOOL, "parent_pid": INC1, "end": None}]
        projection.gap("process_receipts[serial=1].output_bytes", "CAP-RECEIPTS", DEFAULT_GAP)
        rsp.killed_output_bytes(projection, receipts)
        observed = {}
        # load_harness_observations' merge of the analyzer count (the field B-HO-1 is about).
        try:
            analyzed = json.loads(Path(harness["observations_path"]).read_text(encoding="utf-8"))
        except (OSError, ValueError):
            analyzed = {}
        count = (analyzed.get("harness_observations") or {}).get("max_concurrent_children")
        if count is not None:
            observed["max_concurrent_children"] = count
        rsp.harness_fields(projection, observed)
        native, _ = rsp.native_controller(projection, receipts)
        analyzer_pid = ROOT  # what the container analyzer reports as the traced root
        if native is not None and native != analyzer_pid:
            raise evidence_projection.EvidenceMappingError("controller pid differs")
        symbols = {"HARNESS_CONTROLLER_PID": analyzer_pid}
        rsp.project_symbols(projection, symbols)
        return {
            "evidence": {
                "process_receipts": receipts,
                "harness_observations": observed,
                "symbols": symbols,
            },
            "missing": projection.missing,
            "notes": projection.notes,
        }

    return project_run


def run(tmp_path, monkeypatch, *, route=None, observations=None):
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    inputs = {"schema": pic.INPUTS_SCHEMA, "case_id": "test", "run_dir": str(run_dir)}
    if route is not None:
        inputs["resume_route"] = route
    path = tmp_path / "inputs.json"
    path.write_text(json.dumps(inputs), encoding="utf-8")
    obs = tmp_path / "evidence.json"
    obs.write_text(json.dumps(observations or {}), encoding="utf-8")
    seen = {}
    monkeypatch.setattr(evidence_projection, "project_run", fake_project_run(seen))
    out = tmp_path / "projection.json"
    argv = ["project_in_container.py", str(run_dir), str(obs), str(out), "--inputs", str(path)]
    assert pic.main(argv) == 0
    record = json.loads(out.read_text(encoding="utf-8"))
    assert record["status"] == "OK", record.get("traceback")
    return record, seen


def gaps(projection):
    return {m["path"]: m["reason"] for m in projection["missing"]}


def test_route_record_is_an_optional_input_key():
    assert pic.ROUTE_KEY == "resume_route" and pic.ROUTE_KEY in pic.INPUT_KEYS
    assert pic.ROUTE_KEY in pic.OPTIONAL_INPUT_KEYS  # only harness_stop_controller cases
    assert pic.BROKER_EVIDENCE_KEY == A.BROKER_EVIDENCE_KEY == B.EVIDENCE_KEY


def test_route_record_passes_through_to_the_scored_projection(tmp_path, monkeypatch):
    route = route_record()
    record, seen = run(tmp_path, monkeypatch, route=route)
    assert seen["resume_route"] == route
    assert "resume_route" in record["inputs_status"]["merged"]
    assert "resume_route" not in record["inputs_status"]["not_carried"]
    assert record["kill_facts"]["status"] == "AS_RECORDED"  # in_process: the parent's own /proc
    evidence = record["projection"]["evidence"]
    incarnations = evidence["harness_observations"]["controller_incarnations"]
    assert incarnations["root_pid"] == ROOT and incarnations["kill"]["killed"] == [INC1, TOOL]
    assert evidence["harness_observations"]["onset_partial"]["present_at_interrupt"] is True
    assert evidence["symbols"]["HARNESS_CONTROLLER_PID"] == ROOT  # incarnation-checked
    assert evidence["symbols"][rsp.ONSET_PARTIAL_SYMBOL] == "onset-partial-hash"
    (receipt,) = evidence["process_receipts"]
    assert receipt["output_bytes"] == 15
    assert receipt["output_bytes_source"] == rsp.OUTPUT_BYTES_SOURCE
    assert "process_receipts[serial=1].output_bytes" not in gaps(record["projection"])
    assert any("controller_incarnations" in n for n in record["projection"]["notes"])
    assert "harness_interrupt_broker" not in record["projection"]  # not the broker route


def test_inconsistent_route_record_refuses_the_projection(tmp_path, monkeypatch):
    route = route_record()
    route["incarnations"][0]["parent_pid"] = 7  # incarnation 1 not a child of the traced root
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    path = tmp_path / "inputs.json"
    inputs = {"schema": pic.INPUTS_SCHEMA, "run_dir": str(run_dir), "resume_route": route}
    path.write_text(json.dumps(inputs), encoding="utf-8")
    monkeypatch.setattr(evidence_projection, "project_run", fake_project_run({}))
    out = tmp_path / "projection.json"
    argv = ["p", str(run_dir), str(tmp_path / "absent.json"), str(out), "--inputs", str(path)]
    assert pic.main(argv) == 0
    record = json.loads(out.read_text(encoding="utf-8"))
    assert record["status"] == "ERROR" and "not a child of the traced root" in record["error"]
    assert "projection" not in record and record["inputs_status"]["status"] == "MERGED"


@pytest.mark.parametrize("broker_seen", [False, True])
def test_absent_route_record_leaves_the_stop_fields_missing_never_filled(
    tmp_path, monkeypatch, broker_seen
):
    observations = {pic.BROKER_EVIDENCE_KEY: broker_section()} if broker_seen else None
    record, seen = run(tmp_path, monkeypatch, observations=observations)
    assert "resume_route" not in seen and "kill_facts" not in record
    projection = record["projection"]
    evidence = projection["evidence"]
    missing = gaps(projection)
    for field in ("controller_incarnations", "onset_partial"):
        assert field not in evidence["harness_observations"]
        assert missing[f"harness_observations.{field}"] == pic.ROUTE_ABSENT_REASON
    # Two incarnations, no harness record: the analyzer's pid was never cross-checked.
    assert "HARNESS_CONTROLLER_PID" not in evidence["symbols"]
    assert (
        "incarnation cross-check (OI-05) could not run"
        in (missing["symbols.HARNESS_CONTROLLER_PID"])
    )
    assert rsp.ONSET_PARTIAL_SYMBOL not in evidence["symbols"]
    (receipt,) = evidence["process_receipts"]
    assert "output_bytes" not in receipt  # never 0, never the broker's count
    assert missing["process_receipts[serial=1].output_bytes"] == DEFAULT_GAP
    (note,) = [n["resume_route_absent"] for n in projection["notes"] if "resume_route_absent" in n]
    assert note["reason"] == pic.ROUTE_ABSENT_REASON
    assert (pic.BROKER_EVIDENCE_KEY in projection) is broker_seen


def test_broker_route_takes_the_trace_confirmed_broker_record(tmp_path, monkeypatch):
    relayed = {str(TOOL): {"stdout": 99, "stderr": 0}}  # the ack as relayed: never counted
    route = route_record(kill_route=pic.BROKER_KILL_ROUTE, output_bytes=relayed)
    observations = {pic.BROKER_EVIDENCE_KEY: broker_section()}
    record, seen = run(tmp_path, monkeypatch, route=route, observations=observations)
    kill = seen["resume_route"]["kill"]
    assert (
        kill["facts_source"] == pic.BROKER_SOURCE and kill["ack_facts"]["output_bytes"] == relayed
    )
    assert kill["killed"] == [INC1, TOOL]
    facts = record["kill_facts"]
    assert facts["status"] == "CONFIRMED" and facts["ack_differs"] == ["output_bytes"]
    projection = record["projection"]
    (receipt,) = projection["evidence"]["process_receipts"]
    assert receipt["output_bytes"] == 15  # broker record 12 + 3, not the relayed 99
    assert projection[pic.BROKER_EVIDENCE_KEY] == observations[pic.BROKER_EVIDENCE_KEY]
    assert {"interrupt_broker_kill_facts": facts} in projection["notes"]
    assert route["kill"]["output_bytes"] == relayed  # the input record itself is not edited


def test_broker_route_counts_only_trace_confirmed_kills(tmp_path, monkeypatch):
    route = route_record(kill_route=pic.BROKER_KILL_ROUTE)
    section = broker_section(trace_confirmed_kills=[INC1], trace_unconfirmed_kills=[TOOL])
    record, _ = run(
        tmp_path, monkeypatch, route=route, observations={"harness_interrupt_broker": section}
    )
    facts = record["kill_facts"]
    assert facts["status"] == "CONFIRMED" and facts["unconfirmed_kills"] == [TOOL]
    assert facts["withheld"] == [TOOL]
    (receipt,) = record["projection"]["evidence"]["process_receipts"]
    assert "output_bytes" not in receipt
    gap = gaps(record["projection"])["process_receipts[serial=1].output_bytes"]
    assert "the trace does not confirm the broker's kill of pids [102]" in gap


@pytest.mark.parametrize(
    ("section", "reason"),
    [
        (None, "carried no harness_interrupt_broker section"),
        (
            {"status": "UNAVAILABLE", "reason": "interrupt broker record absent", "stops": []},
            "status UNAVAILABLE: interrupt broker record absent",
        ),
        (broker_section(trace_confirmed=False), "not confirmed by the trace"),
        (broker_section(pid=999), "0 matching stops"),
    ],
)
def test_broker_route_unconfirmed_withholds_the_byte_counts(tmp_path, monkeypatch, section, reason):
    route = route_record(kill_route=pic.BROKER_KILL_ROUTE)
    observations = {pic.BROKER_EVIDENCE_KEY: section} if section is not None else {}
    record, seen = run(tmp_path, monkeypatch, route=route, observations=observations)
    facts = record["kill_facts"]
    assert facts["status"] == "UNCONFIRMED" and reason in facts["reason"]
    assert facts["withheld"] == [INC1, TOOL]
    assert seen["resume_route"]["kill"]["output_bytes"] == {}
    projection = record["projection"]
    (receipt,) = projection["evidence"]["process_receipts"]
    assert "output_bytes" not in receipt
    gap = gaps(projection)["process_receipts[serial=1].output_bytes"]
    assert gap.startswith("killed by the harness interrupt broker; byte counts withheld")
    assert reason in gap and gap.endswith("(never 0)")
    # The route record still carries the incarnations: the controller pid stays checked.
    assert projection["evidence"]["symbols"]["HARNESS_CONTROLLER_PID"] == ROOT


FALLBACK = (
    "SIMKIT_INTERRUPT_BROKER was set but no broker channel was usable: the harness parent "
    "killed incarnation 1 itself (route in_process), not the broker"
)


@pytest.mark.parametrize("disclosed", [False, True])
def test_in_process_fallback_kill_in_the_container_withholds_concurrency(
    tmp_path, monkeypatch, disclosed
):
    """D4 w7 B-HO-1: the kill fell back to the traced root (route in_process) and the analyzer
    still reported a root-only count; the scored projection must not keep it."""
    route = route_record()  # kill.route in_process
    if disclosed:
        route["kill"]["broker_fallback"] = FALLBACK
    observations = {
        "harness_observations": {"max_concurrent_children": 1},  # root-only: the fork is 1
        pic.BROKER_EVIDENCE_KEY: {"status": "OK", "stops": [], "incarnation_pids": []},
    }
    record, _ = run(tmp_path, monkeypatch, route=route, observations=observations)
    facts = record["kill_facts"]
    assert facts["status"] == "AS_RECORDED" and facts["kill_route"] == "in_process"
    assert (facts.get("broker_fallback") == FALLBACK) is disclosed
    projection = record["projection"]
    assert "max_concurrent_children" not in projection["evidence"]["harness_observations"]
    (gap,) = [m for m in projection["missing"] if m["path"] == pic.CONCURRENCY_FIELD]
    assert gap["capability"] == "CAP-HARNESS-OBS"
    assert gap["reason"].startswith("withheld: " + pic.CONCURRENCY_WITHHELD)
    assert "kill_facts AS_RECORDED: kill route in_process" in gap["reason"]
    assert (FALLBACK in gap["reason"]) is disclosed
    # The harness parent's own /proc counts are still used as recorded (OI-04).
    (receipt,) = projection["evidence"]["process_receipts"]
    assert receipt["output_bytes"] == 15


def test_unconfirmed_broker_stop_withholds_concurrency(tmp_path, monkeypatch):
    route = route_record(kill_route=pic.BROKER_KILL_ROUTE)
    observations = {
        "harness_observations": {"max_concurrent_children": 1},
        pic.BROKER_EVIDENCE_KEY: broker_section(trace_confirmed=False),
    }
    record, _ = run(tmp_path, monkeypatch, route=route, observations=observations)
    projection = record["projection"]
    assert "max_concurrent_children" not in projection["evidence"]["harness_observations"]
    assert "not confirmed by the trace" in gaps(projection)[pic.CONCURRENCY_FIELD]


def test_confirmed_broker_stop_keeps_the_analyzer_concurrency(tmp_path, monkeypatch):
    route = route_record(kill_route=pic.BROKER_KILL_ROUTE)
    observations = {
        "harness_observations": {"max_concurrent_children": 2},  # over confirmed incarnations
        pic.BROKER_EVIDENCE_KEY: broker_section(),
    }
    record, _ = run(tmp_path, monkeypatch, route=route, observations=observations)
    projection = record["projection"]
    assert record["kill_facts"]["status"] == "CONFIRMED"
    assert projection["evidence"]["harness_observations"]["max_concurrent_children"] == 2
    assert pic.CONCURRENCY_FIELD not in gaps(projection)


def test_a_case_without_a_stop_is_untouched(tmp_path, monkeypatch):
    def single(run_dir, harness):
        return {
            "evidence": {"symbols": {"HARNESS_CONTROLLER_PID": ROOT}},
            "missing": [],
            "notes": [],
        }

    run_dir = tmp_path / "run"
    run_dir.mkdir()
    monkeypatch.setattr(evidence_projection, "project_run", single)
    out = tmp_path / "projection.json"
    assert pic.main(["p", str(run_dir), str(tmp_path / "absent.json"), str(out)]) == 0
    record = json.loads(out.read_text(encoding="utf-8"))
    assert record["status"] == "OK" and "kill_facts" not in record
    assert record["projection"] == {
        "evidence": {"symbols": {"HARNESS_CONTROLLER_PID": ROOT}},
        "missing": [],
        "notes": [],
    }
