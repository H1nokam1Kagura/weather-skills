"""Wave 8 B3b (CAP-DECOY, OI-11 / D5): the analyzer's decoy section and the in-container overlay.

* analyze_trace on synthetic ``strace -ff`` captures (test_analyze_trace.Capture) plus a B3a-style
  harness_status.txt and decoy.trace: harness_observations.decoy OBSERVED / MISSING, the decoy in
  process_tree with its harness parent, the decoy tracer as a harness process.
* project_in_container's overlays after project_run (a stand-in project_run): the decoy copy and
  min_free_ram_while_running_gib = min(sampler, injected in-run readings), launch-check readings
  excluded, the basis note.
* Non-decoy cases: the analyzer's golden no-stop bytes (test_interrupt_broker.GOLDEN_NO_STOP) and
  an untouched projection.
"""

import hashlib
import json
import sys
from pathlib import Path

import pytest

HERE = Path(__file__).resolve().parent
REPO = HERE.parents[1]
sys.path[:0] = [str(HERE), str(HERE.parent), str(REPO)]

import analyze_trace as A  # noqa: E402
from test_analyze_trace import ENTRY, ROOT, Capture, child, start  # noqa: E402
from test_interrupt_broker import GOLDEN_NO_STOP, SCENARIOS, evidence_bytes  # noqa: E402

from adapter_support import evidence_projection  # noqa: E402
from container_harness import project_in_container as pic  # noqa: E402

DECOY, DTRACER = 70, 71
IN_RUN = 1700000000.5  # inside STATUS run_start..run_end (test_analyze_trace)


def sig(ts, name="SIGTERM", si_pid=ROOT, si_uid=1000, code="SI_USER"):
    return (
        f"{ts:.9f} --- {name} {{si_signo={name}, si_code={code}, si_pid={si_pid}, "
        f"si_uid={si_uid}}} ---"
    )


def decoy_status(**over):
    """The decoy_* lines entry.sh (B3a) writes for an FX-DECOY-SPAWN case."""
    s = {
        "decoy_needed": "1",
        "decoy_spawn": "ok",
        "decoy_pid": DECOY,
        "decoy_pgid": DECOY,
        "decoy_sid": DECOY,
        "decoy_uid": 1000,
        "decoy_tracer_pid": DTRACER,
        "decoy_tracer_pgid": ENTRY,
        "decoy_tracer_attached": "1",
        "decoy_alive_after_run": "1",
        "decoy_state_after_run": "S",
        "decoy_tracer_alive_at_run_end": "1",
    }
    s.update(over)
    return {k: v for k, v in s.items() if v is not None}


def decoy_capture(tmp_path, trace_lines=(), kill=(), **status_over):
    cap = Capture(tmp_path)
    start(cap)
    child(cap, ROOT, 101)
    cap.exit(101)
    for pid in kill:
        cap(ROOT, f"kill({pid}, SIGTERM) = 0")
    cap.exit(ROOT)
    cap.status.update(decoy_status(**status_over))
    (cap.dir / "decoy.trace").write_text("".join(f"{x}\n" for x in trace_lines), encoding="utf-8")
    return cap


# ------------------------------------------------------------------ analyzer


def test_observed_decoy_section_and_tree(tmp_path):
    ev = decoy_capture(tmp_path, [sig(IN_RUN), sig(IN_RUN + 10, "SIGINT", si_pid=0)]).write()
    assert ev["harness_observations"]["tracer_status"]["status"] == "OK"
    decoy = ev["harness_observations"]["decoy"]
    assert set(decoy) == {"status", "pid", "alive_after_run", "signals_received"}
    assert decoy["status"] == "OBSERVED" and decoy["pid"] == DECOY
    assert decoy["alive_after_run"] is True
    assert [(s["signal"], s["si_pid"]) for s in decoy["signals_received"]] == [("SIGTERM", ROOT)]
    note = ev["harness_notes"]["decoy_observation"]
    assert note["observation"]["signals_outside_window"] == 1  # the post-run SIGINT
    # Never a gap in ``missing`` (the decoy tracer is not the main trace).
    assert not any("decoy" in k for k in ev["missing"])
    assert "created_paths" in ev["filesystem_snapshot"]


def test_decoy_is_in_process_tree_with_its_harness_parent_unsignalled(tmp_path):
    ev = decoy_capture(tmp_path).write()
    tree = ev["process_tree"]
    assert tree[str(DECOY)] == ENTRY
    assert tree[str(ENTRY)] == 1 and tree["1"] == 0  # harness chain closure
    assert DECOY not in ev["controller_trace"]["signalled_pids"]


def test_decoy_signalled_by_the_controller_keeps_its_harness_parent(tmp_path):
    ev = decoy_capture(tmp_path, [sig(IN_RUN)], kill=[DECOY]).write()
    assert DECOY in ev["controller_trace"]["signalled_pids"]
    assert ev["process_tree"][str(DECOY)] == ENTRY
    assert ev["harness_observations"]["decoy"]["status"] == "OBSERVED"


def test_decoy_tracer_is_a_harness_process(tmp_path):
    # The controller signalling the decoy tracer resolves to the harness process (entry child).
    ev = decoy_capture(tmp_path, kill=[DTRACER]).write()
    procs = ev["detail"]["harness_processes"]
    assert procs[str(DTRACER)] == {"label": A.DECOY_TRACER, "parent": ENTRY}
    assert procs[str(DECOY)] == {"label": "decoy", "parent": ENTRY}
    assert DTRACER in ev["controller_trace"]["signalled_pids"]
    assert ev["process_tree"][str(DTRACER)] == ENTRY


@pytest.mark.parametrize(
    "over,needle",
    [
        ({"decoy_tracer_attached": "0"}, "never attached"),
        ({"decoy_spawn": "failed:spec_exit_2", "decoy_pid": None}, "not spawned"),
        ({"decoy_needed": "error"}, "need check failed"),
        ({"decoy_tracer_alive_at_run_end": "0"}, "incomplete"),
        ({"decoy_alive_after_run": None}, "no alive check"),
    ],
)
def test_missing_decoy_section_carries_the_reason(tmp_path, over, needle):
    ev = decoy_capture(tmp_path, [sig(IN_RUN)], **over).write()
    decoy = ev["harness_observations"]["decoy"]
    assert decoy["status"] == "MISSING" and needle in decoy["reason"], decoy
    assert set(decoy) == {"status", "reason"}
    assert not any("decoy" in k for k in ev["missing"])
    assert "created_paths" in ev["filesystem_snapshot"]


def test_absent_decoy_trace_is_missing(tmp_path):
    cap = decoy_capture(tmp_path)
    (cap.dir / "decoy.trace").unlink()
    decoy = cap.write()["harness_observations"]["decoy"]
    assert decoy["status"] == "MISSING" and "trace file absent" in decoy["reason"]


def test_decoy_pid_that_is_also_a_traced_process_withholds_process_tree(tmp_path):
    ev = decoy_capture(tmp_path, decoy_pid=101).write()
    assert "process_tree" not in ev
    assert "decoy pid 101 is also a traced tree process" in ev["missing"]["process_tree"]


def test_decoy_with_unknown_harness_parent_withholds_process_tree(tmp_path):
    cap = decoy_capture(tmp_path)
    del cap.status["entry_pid"]
    ev = cap.write()
    assert "process_tree" not in ev
    assert "harness parent is unknown" in ev["missing"]["process_tree"]


def test_non_decoy_case_has_no_decoy_section(tmp_path):
    cap = Capture(tmp_path)
    start(cap)
    cap.exit(ROOT)
    ev = cap.write()
    assert "decoy" not in ev["harness_observations"]
    assert "decoy_observation" not in ev["harness_notes"]


@pytest.mark.parametrize("name", sorted(SCENARIOS))
def test_non_decoy_analyzer_bytes_match_the_golden(tmp_path, name):
    assert hashlib.sha256(evidence_bytes(tmp_path, name)).hexdigest() == GOLDEN_NO_STOP[name]


# ------------------------------------------------------------------ in-container overlay

SAMPLER = 7.5


def fake_project_run(sampler):
    def project_run(run_dir, harness):
        assert pic.RAM_READINGS_KEY not in harness  # consumed by the overlay, never project_run's
        observed, missing = {}, []
        if sampler is None:
            missing.append(
                {"path": pic.RAM_FIELD, "capability": "CAP-HARNESS-OBS", "reason": "sampler gap"}
            )
        else:
            observed[pic.RAM_KEY] = sampler
        return {
            "evidence": {"harness_observations": observed},
            "missing": missing,
            "notes": [{"write_capture": {"status": "EXHAUSTIVE"}}],
        }

    return project_run


def run(tmp_path, monkeypatch, *, readings=None, observations=None, sampler=SAMPLER, extra=None):
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    inputs = {"schema": pic.INPUTS_SCHEMA, "case_id": "test", "run_dir": str(run_dir)}
    if readings is not None:
        inputs[pic.RAM_READINGS_KEY] = readings
    inputs.update(extra or {})
    path = tmp_path / "inputs.json"
    path.write_text(json.dumps(inputs), encoding="utf-8")
    obs = tmp_path / "evidence.json"
    obs.write_text(json.dumps(observations or {}), encoding="utf-8")
    monkeypatch.setattr(evidence_projection, "project_run", fake_project_run(sampler))
    out = tmp_path / "projection.json"
    argv = ["project_in_container.py", str(run_dir), str(obs), str(out), "--inputs", str(path)]
    assert pic.main(argv) == 0
    record = json.loads(out.read_text(encoding="utf-8"))
    assert record["status"] == "OK", record.get("traceback")
    return record, out.read_bytes()


def deferred(value, offset=5.0):
    """An e2e_injections deferred record as delivered (B1 field names)."""
    return {
        "reading_form": "deferred",
        "metric": "free_ram_gib",
        "value": value,
        "offset_seconds": 5.0,
        "delivery": "DELIVERED",
        "delivered_value": value,
        "delivered_offset_seconds": offset,
    }


def launch_check(value):
    return {"reading_form": "launch_check", "value": value, "delivery": "DELIVERED"}


def overlay_note(projection):
    key = "min_free_ram_overlay"
    return next(n[key] for n in projection["notes"] if key in n)


def test_injected_readings_is_an_optional_input_key():
    assert pic.RAM_READINGS_KEY == "injected_ram_readings"
    assert pic.RAM_READINGS_KEY in pic.INPUT_KEYS
    assert pic.RAM_READINGS_KEY in pic.OPTIONAL_INPUT_KEYS
    assert pic.RAM_FIELD == A.RAM_FIELD


def test_overlay_takes_the_min_with_injected_in_run_readings(tmp_path, monkeypatch):
    record, _ = run(tmp_path, monkeypatch, readings=[deferred(1.8, 5.02), deferred(2.4)])
    proj = record["projection"]
    assert proj["evidence"]["harness_observations"][pic.RAM_KEY] == 1.8
    note = overlay_note(proj)
    assert note["status"] == "OVERLAID" and note["sampler_gib"] == SAMPLER
    assert note["injected_in_run"] == [
        {"value": 1.8, "offset_seconds": 5.02},
        {"value": 2.4, "offset_seconds": 5.0},
    ]
    assert note["basis"] == pic.RAM_BASIS
    assert "the oracle tests the stop rule, not the host counter" in note["basis"]
    assert "frozen injection schedule" in note["basis"]
    assert "Launch-check readings are excluded" in note["basis"]
    assert record["inputs_status"]["merged"] == [pic.RAM_READINGS_KEY]
    assert record["ram_overlay"] == {"status": "OVERLAID", "sampler_gib": SAMPLER}


def test_a_lower_sampler_minimum_wins(tmp_path, monkeypatch):
    record, _ = run(tmp_path, monkeypatch, readings=[deferred(2.4)], sampler=1.2)
    assert record["projection"]["evidence"]["harness_observations"][pic.RAM_KEY] == 1.2


def test_launch_check_readings_are_excluded(tmp_path, monkeypatch):
    readings = [launch_check(0.5), deferred(1.8)]
    record, _ = run(tmp_path, monkeypatch, readings=readings)
    proj = record["projection"]
    assert proj["evidence"]["harness_observations"][pic.RAM_KEY] == 1.8  # never 0.5
    note = overlay_note(proj)
    why = "launch-check reading (reading_form launch_check)"
    assert note["excluded"] == [{"index": 0, "why": why}]


def test_only_launch_check_or_undelivered_readings_leave_the_sampler(tmp_path, monkeypatch):
    undelivered = {"reading_form": "deferred", "value": 1.0, "delivery": "UNDELIVERED"}
    record, _ = run(tmp_path, monkeypatch, readings=[launch_check(0.5), undelivered])
    proj = record["projection"]
    assert proj["evidence"]["harness_observations"][pic.RAM_KEY] == SAMPLER
    note = overlay_note(proj)
    assert note["status"] == "SAMPLER_ONLY" and len(note["excluded"]) == 2


@pytest.mark.parametrize("readings", [None, []])
def test_no_readings_delivered_is_the_sampler_untouched(tmp_path, monkeypatch, readings):
    record, _ = run(tmp_path, monkeypatch, readings=readings)
    proj = record["projection"]
    assert proj["evidence"]["harness_observations"] == {pic.RAM_KEY: SAMPLER}
    assert not any("min_free_ram_overlay" in n for n in proj["notes"])
    assert "ram_overlay" not in record and "decoy" not in record


def test_sampler_missing_stays_missing_even_with_injected_readings(tmp_path, monkeypatch):
    record, _ = run(tmp_path, monkeypatch, readings=[deferred(1.8)], sampler=None)
    proj = record["projection"]
    assert pic.RAM_KEY not in proj["evidence"]["harness_observations"]
    assert {m["path"]: m["reason"] for m in proj["missing"]}[pic.RAM_FIELD] == "sampler gap"
    assert overlay_note(proj)["status"] == "SAMPLER_MISSING"


def test_malformed_reading_withholds_the_field(tmp_path, monkeypatch):
    bad = {"reading_form": "deferred", "delivery": "DELIVERED", "value": "low"}
    record, _ = run(tmp_path, monkeypatch, readings=[deferred(1.8), bad])
    proj = record["projection"]
    assert pic.RAM_KEY not in proj["evidence"]["harness_observations"]
    gap = next(m for m in proj["missing"] if m["path"] == pic.RAM_FIELD)
    assert gap["capability"] == "CAP-HARNESS-OBS" and gap["reason"].startswith("withheld:")


def test_plain_value_offset_entries_are_accepted():
    in_run, excluded, bad = pic.classify_ram_readings([{"value": 1.9, "offset_seconds": 5}])
    assert in_run == [{"value": 1.9, "offset_seconds": 5}] and excluded == [] and bad is None


def analyzer_section(tmp_path, **over):
    ev = decoy_capture(tmp_path, [sig(IN_RUN)], **over).write()
    return json.loads(json.dumps(ev, default=str))


def test_overlay_copies_an_observed_decoy(tmp_path, monkeypatch):
    observations = analyzer_section(tmp_path / "a")
    record, _ = run(tmp_path, monkeypatch, observations=observations)
    decoy = record["projection"]["evidence"]["harness_observations"]["decoy"]
    assert set(decoy) == {"pid", "alive_after_run", "signals_received"}
    assert decoy["pid"] == DECOY and decoy["alive_after_run"] is True
    assert [s["signal"] for s in decoy["signals_received"]] == ["SIGTERM"]
    assert record["decoy"]["status"] == "OBSERVED"
    assert not any(m["path"] == pic.DECOY_FIELD for m in record["projection"]["missing"])


def test_overlay_lists_a_missing_decoy_with_the_analyzer_reason(tmp_path, monkeypatch):
    observations = analyzer_section(tmp_path / "a", decoy_tracer_attached="0")
    record, _ = run(tmp_path, monkeypatch, observations=observations)
    proj = record["projection"]
    assert "decoy" not in proj["evidence"]["harness_observations"]
    gap = next(m for m in proj["missing"] if m["path"] == pic.DECOY_FIELD)
    assert gap["capability"] == "CAP-DECOY" and "never attached" in gap["reason"]


def test_scheduled_decoy_without_an_analyzer_section_is_missing(tmp_path, monkeypatch):
    fixture = {"entries": [{"fixture_operation": pic.DECOY_FIXTURE_OPERATION, "target": "x"}]}
    record, _ = run(tmp_path, monkeypatch, extra={"fixture_operations": fixture})
    gap = next(m for m in record["projection"]["missing"] if m["path"] == pic.DECOY_FIELD)
    assert gap["reason"] == pic.DECOY_ABSENT_REASON


def test_non_decoy_projection_is_byte_identical(tmp_path, monkeypatch):
    """No decoy section, no decoy scheduled, no readings: the overlays change nothing."""
    cap = Capture(tmp_path / "a")
    start(cap)
    cap.exit(ROOT)
    observations = json.loads(json.dumps(cap.write(), default=str))
    fixture = {"entries": [{"fixture_operation": "FX-SRC-META-ATTR", "target": "source"}]}
    record, raw = run(
        tmp_path, monkeypatch, observations=observations, extra={"fixture_operations": fixture}
    )
    # The record project_in_container wrote before B3b (same keys, same order): the stand-in
    # projection unchanged, no decoy / ram_overlay keys.
    before = {
        "schema": "simkit.harness-projection/1",
        "run_dir": record["run_dir"],
        "observations_path": record["observations_path"],
        "inputs_status": record["inputs_status"],
        "projection": fake_project_run(SAMPLER)(None, {}),
        "status": "OK",
    }
    # main() writes in text mode (CRLF on Windows): compare the LF form.
    lf = raw.replace(b"\r\n", b"\n")
    assert lf == (json.dumps(before, indent=1, default=str) + "\n").encode("utf-8")
