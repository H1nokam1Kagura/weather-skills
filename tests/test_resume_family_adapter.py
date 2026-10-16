"""Wave 7, sub-wave C (adapter): the interrupt and resume family on the real controller.

The frozen cases E10-P1 (CTL/INJ), E11-P1 (CTL/INJ), E14-P1 (CTL/INJ) and E14-P2-INJ run
through WeatherAdapter.run_case with their FROZEN adapter_requirements, admitted by a
test-local copy of the producer file in which the nine wave-7 records are signed and the
records flagged rereview_required are re-signed (the committed file carries the nine unsigned
and the flags: only the coordinator signs, after the batch D4 review). Roles are
the substituted doubles the container case driver uses; tools are stand-in scripts whose
onset-date writes a small real zarr (FX-ONSET-CELL-SHIFT mutates it) after a short delay (so
the mid-process stop lands while it runs). Frozen assertions are scored with the pinned
frozen scorer.

Routes:

* E14 (automatic resumes, classified drops, notify-and-wait): the single in-process
  controller, exactly as before; runs everywhere.
* E10/E11 (harness_stop_controller): the forked route of adapter_support/resume_driver.py,
  in-process (no interrupt broker: the parent stops incarnation 1 itself). It needs os.fork,
  so those tests are SKIPPED where fork is unavailable (Windows) and run on Linux CI.
* A THREAD DOUBLE of incarnation 1 (``ThreadIncarnation``, below) drives the same parent-side
  logic (stop packet, deferred entries, the harness resume record, request_resume as
  incarnation 2, projection, frozen scoring) on every platform. Both incarnations share one
  pid and the blocked thread is never killed (only the launched tool process is terminated),
  so it is NEVER evidence of the real route; it checks the harness logic around it.

Frozen assertions that need the container route (whole-tree concurrency, shell launches,
data fetches, running RAM, created paths) are NOT_OBSERVED/FAIL here, as for every in-process
case; they are excluded by name (CONTAINER_ONLY) and never by verdict.
"""

import copy
import hashlib
import json
import os
import queue
import signal
import subprocess
import threading
import time
from pathlib import Path
from types import SimpleNamespace

import pytest
from test_evidence_projection_v27 import (
    CATALOG,
    MAP_GOAL,
    SIGNED_ADMITTED,
    TOOL_SCRIPT,
    Lab,
    resigned,
    score,
)
from test_role_substitution_adapter import PREFIX, frozen  # noqa: F401 - shared fixture

from adapter_support import evidence_projection as ep
from adapter_support import resume_driver, resume_projection
from adapter_support.weather_adapter import (
    PROJECTION_INPUTS_FILE,
    AdapterRefusal,
    WeatherAdapter,
)
from tools.workflow import runner
from tools.workflow.persistence import read_checkpoint

WAVE7 = (
    "CAP-INTERRUPT",
    "CAP-RESUME",
    "CAP-RESUME-ATTRIBUTION",
    "CAP-MIDPROCESS",
    "CAP-AUTO-RESUME",
    "CAP-DIAGNOSIS",
    "CAP-OPERATOR-NOTIFY",
    "CAP-MECHANICAL-REPAIR",
    "CAP-DENY-WRITE",
)
SIGN = {"reviewer": "test-local", "reviewed_at": "2026-10-15T00:00:00Z", "reviewed_commit": "0"}
FORK = pytest.mark.skipif(
    not resume_driver.fork_available(), reason="os.fork unavailable: the forked route runs on Linux"
)
TARGETS = (
    "E10-P1-CTL",
    "E10-P1-INJ",
    "E11-P1-CTL",
    "E11-P1-INJ",
    "E14-P1-CTL",
    "E14-P1-INJ",
    "E14-P2-INJ",
)
STOPPED = TARGETS[:4]
E14 = TARGETS[4:]
# Assertions that read only container-route harness fields (no whole-tree observer, RAM sampler
# or write capture in process). Excluded by id, never by verdict.
CONTAINER_ONLY = {
    "V22-SHELL-FETCH",
    "V23-CONCURRENCY",
    "V24-EVIDENCE",
    "V25-RUNNING-RAM",
    "V26-PROCESS-OWNERSHIP",
    "RC10",
    "U1",
    "U6",
    "U8",
    "U11",
}
ONSET_DELAY_S = 1.5
ONSET_TOOL = """
if name == "onset_date":
    import time
    import numpy as np
    import zarr
    time.sleep(ONSET_DELAY_S)
    out = pathlib.Path(value("--output"))
    group = zarr.open_group(str(out), mode="w", zarr_format=2)
    missing = np.iinfo("int64").min
    values = np.array([[missing, 0], [1, 2]], dtype="int64")
    array = group.create_array("onset_precip_date", data=values, fill_value=missing)
    array.attrs["units"] = "days since 2025-09-16 00:00:00"
elif name == "plot_onset":""".replace("ONSET_DELAY_S", repr(ONSET_DELAY_S))


def zarr_tool_script():
    head, sep, tail = TOOL_SCRIPT.partition('if name == "onset_date":')
    assert sep
    body, sep, rest = tail.partition('elif name == "plot_onset":')
    assert sep
    return head + ONSET_TOOL.lstrip("\n") + rest


@pytest.fixture(scope="module")
def lab(tmp_path_factory):
    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(
            runner,
            "build_manifest",
            lambda base: {
                str(p.relative_to(base)): hashlib.sha256(p.read_bytes()).hexdigest()
                for p in sorted(Path(base).rglob("*.md"))
            },
        )
        mp.setattr(runner, "preflight", lambda *args: ({}, {}))
        built = Lab(tmp_path_factory.mktemp("rf"))
        for skill, (script, _) in CATALOG.items():
            path = built.repo / "skills" / skill / "scripts" / script
            path.write_text(zarr_tool_script(), encoding="utf-8")
        yield built


def signed(*caps):
    """resigned() plus test-local signatures on ``caps`` (default: all nine wave-7 records,
    committed unsigned). Never written back: CAPABILITY-PRODUCERS.json is the coordinator's."""
    producers = resigned()
    for cap in caps or WAVE7:
        record = producers["capabilities"].setdefault(
            cap, {"d4_gated": True, "status": "PRODUCER_MAPPED"}
        )
        record.update(SIGN)
    return producers


def execute(lab, case, producers=None):  # noqa: F811 - lab is this module's fixture
    adapter = WeatherAdapter(enable_run_case=True, producers=producers or signed())
    profile = lab.profile(MAP_GOAL, case, stub=None)

    def factory(run_dir):
        return adapter.substituted_roles_factory(case, MAP_GOAL, run_dir)

    profile["roles_factory"] = factory
    root = Path(profile["case_root"])
    evidence = report = refusal = None
    try:
        evidence = adapter.run_case(case, profile)
    except ep.EvidenceMappingError as exc:
        # The projection refused; run_case persisted its inputs first, so the native records
        # and the harness route record stay inspectable. Kept, never hidden.
        refusal = str(exc)
    else:
        report = json.loads((root / "harness/evidence-projection.json").read_text("utf-8"))
    inputs = json.loads((root / "harness" / PROJECTION_INPUTS_FILE).read_text(encoding="utf-8"))
    return SimpleNamespace(
        case=case,
        evidence=evidence,
        report=report,
        refusal=refusal,
        route=inputs.get("resume_route"),
        # The harness inputs run_case projected with (persisted record minus its envelope).
        harness={k: v for k, v in inputs.items() if k not in ("schema", "case_id", "run_dir")},
        adapter=adapter,
        run=root / "run",
        checkpoint=read_checkpoint(root / "run"),
    )


def native_trace(result):
    """What the controller itself recorded (checkpoint), independent of the projection."""
    context = result.checkpoint["context"]
    return SimpleNamespace(
        terminal={k: context["terminal"][k] for k in ("outcome", "reason", "failed_gate")},
        state=result.checkpoint["state"],
        resume=context["recovery"]["resume"],
        interruptions=context["recovery"]["interruptions"],
        run_id=context["operator_channel"]["run_id"],
        ledger=Path(context["operator_channel"]["ledger_path"]),
    )


def scored(result):
    return score.score_case(
        result.case, {"status": "EXECUTED", "evidence": result.evidence}, allow_unratified=True
    )


def verdicts(result, *ids):
    predicates = {a["id"]: a["predicate"] for a in result.case["assertions"]}
    return {i: score.evaluate(predicates[i], result.evidence) for i in ids}


def case_assertions(result):
    """Every frozen assertion the in-process route can decide (CONTAINER_ONLY excluded)."""
    return [a["id"] for a in result.case["assertions"] if a["id"] not in CONTAINER_ONLY]


def missing(report, prefix):
    return [m for m in report["missing"] if m["path"].startswith(prefix)]


def notes(report, key):
    return [n[key] for n in report["notes"] if key in n]


def admitted(adapter, cases):
    implemented = adapter.implemented_capabilities()
    return {
        c["id"].removeprefix(PREFIX)
        for c in cases
        if not set(c["adapter_requirements"]) - implemented
        and not resume_driver.implied_missing(c, implemented)
    }


# --------------------------------------------------------------------------- incarnation double


class ThreadIncarnation:
    """NEVER evidence. Incarnation 1 as a thread of the test process: the stopper hands the
    packet over a queue and blocks forever (the thread is leaked, daemon); the "kill"
    terminates only the tool process a mid-process stop left running, and reaps nothing."""

    route = "thread-double"

    def __init__(self):
        self.pid = os.getpid()
        self.read_fd = None
        self.threads_at_fork = None
        self.exit = None
        self.inbox = queue.Queue()
        self.message = None

    def start(self, channel, run, close_fds):
        self.threads_at_fork = threading.active_count()
        never = threading.Event()
        channel.send = self.inbox.put
        channel.block = never.wait
        channel.failed = lambda exc: self.inbox.put(self._error(exc))

        def body():
            try:
                self.inbox.put(run())
            except BaseException as exc:  # noqa: BLE001 - reported like the forked child
                self.inbox.put(self._error(exc))

        threading.Thread(target=body, daemon=True, name="incarnation-1-double").start()

    def _error(self, exc):
        return {
            "schema": resume_driver.MESSAGE_SCHEMA,
            "kind": "error",
            "pid": self.pid,
            "error": repr(exc),
        }

    def receive(self, timeout=None):
        try:
            self.message = json.loads(json.dumps(self.inbox.get(timeout=600)))
        except queue.Empty:
            return None, "timeout waiting for the incarnation double"
        return self.message, None

    def kill(self, broker):
        errors = ["thread double: incarnation 1 is not killed (never evidence)"]
        receipt = ((self.message or {}).get("packet") or {}).get("stop", {}).get("start_receipt")
        if isinstance(receipt, dict) and isinstance(receipt.get("pid"), int):
            pid = receipt["pid"]
            if os.name == "nt":
                # The tool pid can be a venv launcher whose interpreter is its child: the tree.
                done = subprocess.run(
                    ["taskkill", "/PID", str(pid), "/T", "/F"], capture_output=True, check=False
                )
                if done.returncode:
                    errors.append(f"taskkill {pid}: exit {done.returncode}")
            else:
                try:
                    os.kill(pid, signal.SIGKILL)
                except OSError as exc:
                    errors.append(f"tool pid {pid}: {exc}")
            time.sleep(0.5)
        return {
            "route": self.route,
            "pid": self.pid,
            "ok": False,
            "descendants": [],
            "killed": [],
            "output_bytes": {},
            "errors": errors,
        }

    def reap(self, kill_on_timeout=True):
        self.exit = {"reaped": False, "double": True}
        return self.exit


@pytest.fixture(scope="module")
def doubled(lab, frozen):  # noqa: F811
    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(resume_driver, "fork_available", lambda: True)
        mp.setattr(resume_driver, "INCARNATION_ROUTE", ThreadIncarnation)
        out = {}
        for short in STOPPED:
            case = copy.deepcopy(frozen[PREFIX + short])
            case["id"] += "-TD"  # thread double: its own case root (short: Windows MAX_PATH)
            out[short] = execute(lab, case)
        return out


@pytest.fixture(scope="module")
def forked(lab, frozen):  # noqa: F811
    if not resume_driver.fork_available():
        pytest.skip("os.fork unavailable: the forked route runs on Linux")
    return {s: execute(lab, copy.deepcopy(frozen[PREFIX + s])) for s in STOPPED}


@pytest.fixture(scope="module")
def e14(lab, frozen):  # noqa: F811
    return {s: execute(lab, copy.deepcopy(frozen[PREFIX + s])) for s in E14}


# --------------------------------------------------------------------------- admission (D21)


def test_resigned_producers_keep_24_admitted_and_the_signed_records_add_the_seven(frozen):  # noqa: F811
    cases = list(frozen.values())
    assert len(cases) == 66
    committed = WeatherAdapter()
    assert not set(WAVE7) & committed.implemented_capabilities()
    # The committed file: the nine wave-7 records unsigned and the flagged records undeclared
    # until the batch re-review, so it admits exactly SIGNED_ADMITTED (none at this commit).
    assert {PREFIX + c for c in admitted(committed, cases)} == SIGNED_ADMITTED
    # Re-signed (flags cleared, test-local): the 24 cases admitted before wave 7.
    resigned_adapter = WeatherAdapter(producers=resigned())
    assert not set(WAVE7) & resigned_adapter.implemented_capabilities()
    before = admitted(resigned_adapter, cases)
    assert len(before) == 24
    # D21 is stricter, never looser: requirements alone admit the same 24 today.
    implemented = resigned_adapter.implemented_capabilities()
    assert before == {
        c["id"].removeprefix(PREFIX)
        for c in cases
        if not set(c["adapter_requirements"]) - implemented
    }
    after = admitted(WeatherAdapter(producers=signed()), cases)
    assert after - before == set(TARGETS) and before <= after
    assert "E14-P2-CTL" not in after  # CAP-OWNED-STALE-LOCK-SETUP is not built (D31)


def test_implied_capabilities_follow_the_frozen_method_table(frozen):  # noqa: F811
    implied = {
        s: sorted({e["capability"] for e in resume_driver.implied_capabilities(frozen[PREFIX + s])})
        for s in (*TARGETS, "E12-P1-INJ")
    }
    assert implied["E10-P1-INJ"] == ["CAP-FIXTURE-OP", "CAP-INTERRUPT", "CAP-TRANSITION-REQUEST"]
    assert implied["E11-P1-INJ"] == ["CAP-INTERRUPT", "CAP-MIDPROCESS", "CAP-TRANSITION-REQUEST"]
    assert implied["E14-P2-INJ"] == ["CAP-DENY-WRITE", "CAP-INTERRUPT"]
    assert implied["E12-P1-INJ"] == ["CAP-DENY-WRITE", "CAP-FIXTURE-OP"]


def test_an_undeclared_implied_capability_refuses_before_side_effects(lab, frozen, tmp_path):  # noqa: F811
    """E14-P2-INJ lists no CAP-DENY-WRITE requirement, but its scheduled deny_write implies it
    (D21 / CR-05): with every other record signed the case is still refused."""
    case = copy.deepcopy(frozen[PREFIX + "E14-P2-INJ"])
    assert "CAP-DENY-WRITE" not in case["adapter_requirements"]
    case["id"] += "-REFUSED"  # its own generated source; frozen schedule and requirements
    profile = lab.profile(MAP_GOAL, case, stub=None)
    profile["case_root"] = tmp_path / "never"
    producers = signed(*(c for c in WAVE7 if c != "CAP-DENY-WRITE"))
    with pytest.raises(
        AdapterRefusal, match=r"implies undeclared capabilities: \['CAP-DENY-WRITE'\]"
    ):
        WeatherAdapter(enable_run_case=True, producers=producers).run_case(case, profile)
    assert not profile["case_root"].exists()


def test_unsigned_wave7_records_and_e14_p2_ctl_stay_refused(lab, frozen, tmp_path):  # noqa: F811
    for short, producers, message in (
        ("E10-P1-CTL", None, "undeclared capabilities"),
        ("E11-P1-INJ", None, "undeclared capabilities"),
        ("E14-P1-CTL", None, "undeclared capabilities"),
        ("E14-P2-CTL", signed(), r"undeclared capabilities: \['CAP-OWNED-STALE-LOCK-SETUP'\]"),
    ):
        case = copy.deepcopy(frozen[PREFIX + short])
        case["id"] += "-UNSIGNED"  # its own generated source; frozen schedule and requirements
        profile = lab.profile(MAP_GOAL, case, stub=None)
        profile["case_root"] = tmp_path / f"never-{short}"
        adapter = WeatherAdapter(enable_run_case=True, producers=producers)
        with pytest.raises(AdapterRefusal, match=message):
            adapter.run_case(case, profile)
        assert not profile["case_root"].exists()


def test_without_fork_the_stop_route_is_refused_before_side_effects(
    lab,
    frozen,  # noqa: F811
    tmp_path,
    monkeypatch,
):
    monkeypatch.setattr(resume_driver, "fork_available", lambda: False)
    for short, message in (
        ("E10-P1-CTL", "needs a real stop of a forked controller incarnation"),
        ("E11-P1-INJ", "injection channels that are unavailable"),  # no stopper: no mid-process
    ):
        case = copy.deepcopy(frozen[PREFIX + short])
        case["id"] += "-NO-FORK"  # its own generated source; frozen schedule and requirements
        profile = lab.profile(MAP_GOAL, case, stub=None)
        profile["case_root"] = tmp_path / f"never-{short}"
        with pytest.raises(AdapterRefusal, match=message):
            WeatherAdapter(enable_run_case=True, producers=signed()).run_case(case, profile)
        assert not profile["case_root"].exists()


def test_stop_delivery_is_requested_only_with_a_stopper(frozen, monkeypatch):  # noqa: F811
    e11 = frozen[PREFIX + "E11-P1-INJ"]
    for available in (True, False):
        monkeypatch.setattr(resume_driver, "fork_available", lambda a=available: a)
        assert resume_driver.stop_delivery(e11) is available
        assert resume_driver.stop_delivery(frozen[PREFIX + "E14-P1-INJ"]) is False
    assert resume_driver.stop_entries(frozen[PREFIX + "E10-P1-INJ"]) == [0]
    assert resume_driver.stop_entries(frozen[PREFIX + "E14-P1-INJ"]) == []


# --------------------------------------------------------------------------- E14, in process


def test_e14_p1_ctl_two_automatic_resumes_release_with_at_checkpoint(e14):
    result = e14["E14-P1-CTL"]
    trace = result.evidence["controller_trace"]
    assert trace["terminal"]["outcome"] == "RELEASED" and trace["final_state"] == "RELEASED"
    assert result.route is None  # no harness stop: the single in-process controller
    assert [(d["initiated_by"], d["result"]) for d in trace["resume"]] == [
        ("automatic", "ACCEPTED"),
        ("automatic", "ACCEPTED"),
    ]
    counters = result.checkpoint["context"]["recovery"]["resume"][0]["checkpoint_counters"]
    assert result.evidence["budget_ledger"]["at_checkpoint"] == {
        "consumed_model_usd": counters["spent"],
        "elapsed_seconds": counters["elapsed_s"],
        "repair_attempts_used": counters["repairs_used"],
        "resume_attempts_used": counters["recovery_counters"]["resume_attempts_used"],
        "automatic_resumes_used": counters["recovery_counters"]["automatic_resumes_used"],
        "limits": {
            "max_model_usd": counters["budget"],
            "max_total_seconds": counters["total_seconds"],
            "max_repair_attempts": counters["max_repairs"],
            "max_resume_attempts": counters["recovery_limits"]["max_automatic_resumes"],
        },
    }
    assert (
        notes(result.report, "at_checkpoint")[0]["basis"] == resume_projection.AT_CHECKPOINT_BASIS
    )
    attribution = result.adapter.attribute_resumes(result.run, result.harness)
    assert attribution["automatic_resumes_used"] == 2
    assert [a["initiated_by"] for a in attribution["attribution"]] == ["automatic", "automatic"]
    assert scored(result)["terminal"] == "PASS"
    assert verdicts(result, "R1") == {"R1": "PASS"}


def test_e14_compile_reasked_after_resume_is_compiler_3_occurrence_2(e14):
    """The after_role(compiler_3) drop lands after the receipt is durable and before the
    controller retains the compile, so the resumed controller asks a fourth compiler: it is
    projected as compiler_3 occurrence 2 (post_resume), never dropped or renumbered. Frozen U5
    (count(compiler_3) == 1) then FAILs: a controller behaviour, reported, not hidden."""
    result = e14["E14-P1-CTL"]
    compilers = [
        (r["role"], r["occurrence"], r["phase"])
        for r in result.evidence["invocation_receipts"]
        if r["role"].startswith("compiler_")
    ]
    assert compilers == [
        ("compiler_1", 1, "pre_interrupt"),
        ("compiler_2", 1, "pre_interrupt"),
        ("compiler_3", 1, "pre_interrupt"),
        ("compiler_3", 2, "post_resume"),
    ]
    assert len(result.evidence["role_output"]["compiler_3"]) == 2
    (slots,) = notes(result.report, "compiler_slots_after_resume")
    assert [s["role"] for s in slots["receipts"]] == [c[0] for c in compilers]
    assert verdicts(result, "U5") == {"U5": "FAIL"}


def test_e14_p1_inj_third_drop_diagnoses_notifies_and_waits(e14):
    result = e14["E14-P1-INJ"]
    trace = result.evidence["controller_trace"]
    assert trace["terminal"] == {
        "outcome": "INCOMPLETE",
        "reason": "runtime_failure",
        "failed_gate": "interrupted",
    }
    assert [i["failure_class"] for i in trace["interruptions"]] == [
        "connection_reset",
        "timeout",
        "connection_reset",
    ]
    adapter, run, harness = result.adapter, result.run, result.harness
    assert len(adapter.record_diagnosis(run, harness)["diagnoses"]) >= 1
    notified = adapter.record_operator_notification(run, harness)["operator_notifications"]
    assert len(notified[0]["stop_record"]["failure_classes"]) == 3
    auto = adapter.inject_intermittent_interruption(run, harness)
    assert len(auto["interruptions"]) == 3 and len(auto["automatic_resumes"]) == 2
    assert adapter.record_mechanical_repair(run, harness)["mechanical_repairs"] == []
    assert "at_checkpoint" in result.evidence["budget_ledger"]
    assert scored(result)["terminal"] == "PASS"
    decided = verdicts(result, *case_assertions(result))
    assert {k: v for k, v in decided.items() if v != "PASS"} == {}


def test_e14_p2_inj_pre_launch_denial_is_interrupted_with_no_producing_launch(e14):
    result = e14["E14-P2-INJ"]
    trace = result.evidence["controller_trace"]
    assert trace["terminal"]["outcome"] == "INCOMPLETE"
    assert trace["terminal"]["failed_gate"] == "interrupted"
    assert trace["final_state"] == "AUTHORIZED"
    denials = result.adapter.deny_write(result.run, result.harness)
    (denial,) = denials["run_dir_denials"]
    assert denial["boundary"] == "pending_checkpoint" and denial["artifact"] == "onset"
    assert denial["denial_source"] == "injected_fault"
    assert (
        result.adapter.record_mechanical_repair(result.run, result.harness)["mechanical_repairs"]
        == []
    )
    assert not [p for p in result.evidence["process_receipts"] if p.get("producing")]
    assert scored(result)["terminal"] == "PASS"
    decided = verdicts(result, *case_assertions(result))
    assert {k: v for k, v in decided.items() if v != "PASS"} == {}


def test_e14_runs_without_a_stop_report_missing_for_the_stop_only_methods(e14):
    result = e14["E14-P1-CTL"]
    assert result.adapter.arm_midprocess_trigger(result.run, result.harness)["status"] == "MISSING"
    interrupted = result.adapter.interrupt_controller(result.run, result.harness)
    assert len(interrupted["interruptions"]) == 2 and "harness_stop" not in interrupted
    resumed = result.adapter.request_resume(result.run, result.harness)
    assert len(resumed["resume"]) == 2 and "at_checkpoint" in resumed
    assert result.adapter.deny_write(result.run, result.harness)["status"] == "MISSING"


# --------------------------------------------------------------------------- stop + resume

# The resume entry's schedule index, which names the harness-authored operator record.
RESUME_ENTRY = {"E10-P1-CTL": 1, "E10-P1-INJ": 2, "E11-P1-CTL": 1, "E11-P1-INJ": 1}
NATIVE_TERMINAL = {
    "E10-P1-CTL": ("RELEASED", None, "ACCEPTED"),
    "E10-P1-INJ": ("BLOCKED", "resume_verification", "REFUSED"),
    "E11-P1-CTL": ("RELEASED", None, "ACCEPTED"),
    "E11-P1-INJ": ("INCOMPLETE", "pending_operation", "REFUSED"),
}
# Controller-side defects (sub-wave A files, outside this sub-wave) that keep the stopped cases
# from projecting/scoring. Strict: the test must start passing (and the mark be removed) once
# they are fixed.
CONTROLLER_DEFECTS = (
    "controller defects outside sub-wave C: (1) recovery.record_accepted/record_refused append "
    "a resume transition request to context.transition_requests without the per-record "
    "transition-request-NNN.json the transition projection verifies against; (2) a resume "
    "from a checkpoint older than the last dispatch event overwrites the killed incarnation's "
    "launch_check/process_launch events (E11-P1-INJ loses its onset-date process receipt)"
)


def check_native_route(results, real):
    """What the controller and the harness recorded on the stop-and-resume route, read from
    the checkpoint and the harness route record (independent of the projection)."""
    for short, result in results.items():
        native, route = native_trace(result), result.route
        outcome, gate, decision = NATIVE_TERMINAL[short]
        assert (native.terminal["outcome"], native.terminal["failed_gate"]) == (outcome, gate)
        assert route["schema"] == resume_projection.ROUTE_SCHEMA
        assert [i["incarnation"] for i in route["incarnations"]] == [1, 2], short
        pids = [i["pid"] for i in route["incarnations"]]
        if real:
            assert pids[0] != route["root_pid"] == pids[1], short
            assert route["incarnations"][0]["parent_pid"] == route["root_pid"]
            assert route["exit"]["reaped"] is True and route["exit"]["signaled"] is True
            assert route["kill"]["ok"] is True and pids[0] in route["kill"]["killed"]
        # The harness-authored resume record, appended to the controller's own ledger.
        index = RESUME_ENTRY[short]
        record = route["operator_resume_record"]
        assert record["authored_by"] == "harness (disclosed)"
        expected = resume_driver.harness_resume_record(index, native.run_id)
        assert {k: record[k] for k in expected} == expected
        (line,) = native.ledger.read_bytes().split(b"\n")[:-1]
        assert json.loads(line) == expected
        assert route["resume_request"] == {
            "action": "resume",
            "initiated_by": "operator_ledger",
            "requested_by": "operator",
            "run_id": native.run_id,
        }
        # The controller decided it: one decision, attributed even when refused, consuming the
        # observed stop (controller_killed) and carrying its checkpoint snapshot.
        (first,) = native.resume
        assert first["result"] == decision, short
        assert first["initiated_by"] == "operator_ledger"
        assert first["operator_record_id"] == expected["record_id"]
        assert first["controller_pid"] == route["root_pid"]
        (killed,) = native.interruptions
        assert killed["failure_class"] == "controller_killed"
        assert killed["observed"] == "checkpoint_without_terminal"
        assert first["interruption_index"] == killed["index"]
        assert first["checkpoint_counters"]["state"] == killed["at_state"]
        stop = route["stop"]
        assert stop["mode"] == "harness_stop_controller"
        assert stop["delivery"] == "STOP_REQUESTED"
    # Deferred at the stop boundary, applied by the parent in schedule order (B's records).
    for short, operations in (
        ("E10-P1-CTL", [(1, "request_transition")]),
        ("E10-P1-INJ", [(1, "mutate_fixture"), (2, "request_transition")]),
        ("E11-P1-CTL", [(1, "request_transition")]),
        ("E11-P1-INJ", [(1, "request_transition")]),
    ):
        applied = results[short].route["deferred_applied"]
        assert [(r["index"], r["operation"]) for r in applied] == operations, short
        assert all(r["status"] == "APPLIED" and r["deferred_after_stop"] == 0 for r in applied)
    failed = native_trace(results["E10-P1-INJ"]).resume[0]["verification"]["failed_items"]
    assert "artifact:onset" in failed
    e11i = results["E11-P1-INJ"].route
    assert e11i["stop"]["trigger"] == "process_launch"
    assert e11i["stop"]["start_receipt"]["tool"] == "onset-date"
    assert e11i["stop"]["observed_checkpoint"]["pending"]["skill"] == "onset-date"
    assert isinstance(e11i["onset_partial"]["present_at_interrupt"], bool)
    for short in ("E10-P1-CTL", "E10-P1-INJ", "E11-P1-CTL"):
        assert "onset_partial" not in results[short].route  # not a mid-process stop


def check_projected(results):
    """The projection of the stopped cases, scored with the frozen scorer."""
    for short, result in results.items():
        assert result.refusal is None, f"{short}: {result.refusal}"
        trace = result.evidence["controller_trace"]
        assert trace["resume"][0]["initiated_by"] == "operator_ledger"
        assert "at_checkpoint" in result.evidence["budget_ledger"]
        assert scored(result)["terminal"] == "PASS", short
        decided = verdicts(result, *case_assertions(result))
        assert {k: v for k, v in decided.items() if v != "PASS"} == {}, short
    e11i = results["E11-P1-INJ"]
    stop = e11i.adapter.arm_midprocess_trigger(e11i.run, e11i.harness)
    assert stop["stop"]["trigger"] == "process_launch"
    symbols = e11i.evidence["symbols"]
    if stop["onset_partial"]["present_at_interrupt"]:
        assert symbols[resume_projection.ONSET_PARTIAL_SYMBOL]
    else:
        assert resume_projection.ONSET_PARTIAL_SYMBOL not in symbols


def test_thread_double_drives_the_parent_logic_and_the_controller_decides(doubled):
    """NEVER evidence: one pid, incarnation 1 never killed. Checks the harness side."""
    check_native_route(doubled, real=False)
    for result in doubled.values():
        assert result.route["route"] == "thread-double"
        assert result.route["kill"]["ok"] is False  # the double never claims a kill


@pytest.mark.xfail(strict=True, raises=AssertionError, reason=CONTROLLER_DEFECTS)
def test_thread_double_projects_and_scores_the_frozen_outcomes(doubled):
    check_projected(doubled)


@FORK
def test_forked_route_kills_incarnation_1_and_resumes_as_incarnation_2(forked):
    check_native_route(forked, real=True)
    for result in forked.values():
        assert result.route["route"] == "fork"
        assert result.route["kill"]["route"] == "in_process"
        assert result.route["kill"]["output_bytes_basis"]


@FORK
@pytest.mark.xfail(strict=True, raises=AssertionError, reason=CONTROLLER_DEFECTS)
def test_forked_route_projects_and_scores_the_frozen_outcomes(forked):
    check_projected(forked)
    for result in forked.values():
        (note,) = notes(result.report, "controller_incarnations")
        assert note["root_pid"] == result.route["root_pid"]
        child = str(result.route["incarnations"][0]["pid"])
        assert result.evidence["process_tree"][child] == result.route["root_pid"]
    # OI-04: the killed onset-date receipt carries the harness-observed byte count, else
    # stays MISSING (never 0).
    e11i = forked["E11-P1-INJ"]
    (receipt,) = [p for p in e11i.evidence["process_receipts"] if p["tool"] == "onset-date"]
    if "output_bytes" in receipt:
        assert receipt["output_bytes_source"] == resume_projection.OUTPUT_BYTES_SOURCE
    else:
        assert missing(e11i.report, f"process_receipts[serial={receipt['serial']}].output_bytes")


def test_stop_route_methods_need_the_projection(doubled):
    """interrupt_controller / request_resume / attribute_resumes read the projection; while it
    refuses (CONTROLLER_DEFECTS) they raise, never return a partial answer."""
    e10i = doubled["E10-P1-INJ"]
    if e10i.refusal is None:  # once the controller defects are fixed
        stopped = e10i.adapter.interrupt_controller(e10i.run, e10i.harness)
        assert stopped["interruptions"][0]["failure_class"] == "controller_killed"
        assert stopped["harness_stop"]["incarnations"] == e10i.route["incarnations"]
        (decision,) = e10i.adapter.attribute_resumes(e10i.run, e10i.harness)["attribution"]
        assert decision["result"] == "REFUSED" and decision["initiated_by"] == "operator_ledger"
        assert decision["operator_record_id"] == "HARNESS-OPERATOR-RESUME-2"
        return
    for method in (
        e10i.adapter.interrupt_controller,
        e10i.adapter.request_resume,
        e10i.adapter.attribute_resumes,
    ):
        with pytest.raises(ep.EvidenceMappingError):
            method(e10i.run, e10i.harness)
    # deny_write reads the controller's own context only: no denial was recorded.
    assert e10i.adapter.deny_write(e10i.run, e10i.harness)["status"] == "MISSING"


# --------------------------------------------------------------------------- projection units


def projection_double(route=None, receipts=(), compiles=None, interruptions=()):
    return SimpleNamespace(
        h={} if route is None else {"resume_route": route},
        missing=[],
        notes=[],
        n=SimpleNamespace(receipts=list(receipts), context={"compiles": compiles}),
        recovery_list=lambda name: (list(interruptions), None),
    )


def test_killed_output_bytes_are_harness_observed_or_missing_never_zero():
    route = {
        "schema": resume_projection.ROUTE_SCHEMA,
        "kill": {"killed": [41, 42], "output_bytes": {"41": {"stdout": 7, "stderr": 3}}},
    }
    projection = projection_double(route)
    receipts = [
        {"serial": 1, "pid": 41, "end": None},
        {"serial": 2, "pid": 42, "end": None},
        {"serial": 3, "pid": 43, "end": None},
    ]
    projection.missing = [
        {
            "path": f"process_receipts[serial={s}].output_bytes",
            "capability": "CAP-RECEIPTS",
            "reason": "no exit receipt",
        }
        for s in (1, 2, 3)
    ]
    resume_projection.killed_output_bytes(projection, receipts)
    assert receipts[0]["output_bytes"] == 10
    assert receipts[0]["output_bytes_source"] == resume_projection.OUTPUT_BYTES_SOURCE
    assert "output_bytes" not in receipts[1] and "output_bytes" not in receipts[2]
    reasons = {m["path"]: m["reason"] for m in projection.missing}
    assert "process_receipts[serial=1].output_bytes" not in reasons
    assert "never 0" in reasons["process_receipts[serial=2].output_bytes"]
    assert reasons["process_receipts[serial=3].output_bytes"] == "no exit receipt"  # not killed


def test_compiler_slots_refuse_what_the_controller_did_not_record():
    def compiler(phase):
        return {"role": "compiler", "controller_dispatch": {"resume_phase": phase}}

    reasked = [compiler(0), compiler(0), compiler(0), compiler(1)]
    projection = projection_double(receipts=reasked, compiles=[{}, {}, {}], interruptions=[{}])
    roles = [resume_projection.compiler_role(projection, i, i + 1) for i in range(4)]
    assert roles == [("compiler_1", 1), ("compiler_2", 1), ("compiler_3", 1), ("compiler_3", 2)]
    for bad in (
        projection_double(receipts=reasked, compiles=[{}, {}, {}], interruptions=[]),
        projection_double(receipts=reasked, compiles=[{}, {}], interruptions=[{}]),
        projection_double(receipts=[compiler(0)] * 4, compiles=[{}, {}, {}], interruptions=[{}]),
        projection_double(receipts=reasked[::-1], compiles=[{}, {}, {}], interruptions=[{}]),
    ):
        with pytest.raises(ep.EvidenceMappingError):
            resume_projection.compiler_role(bad, 0, 1)
    plain = projection_double(receipts=reasked[:3], compiles=None)
    assert resume_projection.compiler_role(plain, 2, 3) == ("compiler_3", 1)


def test_only_the_notification_outbox_is_a_nested_run_local_name():
    assert resume_projection.outbox_record("notifications/notification-000.json")
    for name in (
        "notifications/../checkpoint.json",
        "notifications\\notification-000.json",
        "other/notification-000.json",
        "notifications/notification-0.json",
    ):
        assert not resume_projection.outbox_record(name)


def test_several_incarnations_need_the_harness_record_and_a_consistent_tree():
    def projection(route, resume_pids, initial=10):
        double = projection_double(route)
        double.initial_controller_pid = lambda: initial
        double.recovery_list = lambda name: (
            [{"controller_pid": p} for p in resume_pids],
            None,
        )
        return double

    route = {
        "schema": resume_projection.ROUTE_SCHEMA,
        "root_pid": 10,
        "incarnations": [
            {"incarnation": 1, "pid": 11, "parent_pid": 10},
            {"incarnation": 2, "pid": 10, "parent_pid": 1},
        ],
    }
    # incarnation 1 decided nothing; its launches and the root's are accepted.
    double = projection(route, [10], initial=11)
    receipts = [{"parent_pid": 11}, {"parent_pid": 10}]
    assert resume_projection.native_controller(double, receipts) == (10, {"11": 10})
    with pytest.raises(ep.EvidenceMappingError, match="not declared"):
        resume_projection.native_controller(projection(route, [12], initial=11), receipts)
    with pytest.raises(ep.EvidenceMappingError, match="launch parent"):
        resume_projection.native_controller(double, [{"parent_pid": 99}])
    unattributed = projection(None, [10], initial=11)
    assert resume_projection.native_controller(unattributed, receipts) == (None, {})
    # One incarnation keeps the single-process rule exactly.
    assert resume_projection.native_controller(projection(None, [10]), [{"parent_pid": 10}]) == (
        10,
        {},
    )


def test_close_fds_names_only_the_descriptors_the_parent_owns():
    channel = SimpleNamespace(fileno=lambda: 7)
    broker = SimpleNamespace(request_fd=8, ack_fd=9)
    assert resume_driver._close_fds({"projection_inputs_channel": channel}) == [7]
    assert resume_driver._close_fds({"interrupt_broker": broker}) == [8, 9]
    import io

    assert resume_driver._close_fds({"projection_inputs_channel": io.StringIO()}) == []
