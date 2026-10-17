"""Decoy harness (Wave 8 B3a): need/spec from the frozen package, decoy-trace parsing, entry.sh.

* ``needs`` / ``spec`` read the verified frozen v2.7 package (tests/frozen_v27.py), exactly as the
  container does through case_driver.frozen_root + load_cases.
* Trace parsing runs on synthetic ``strace -e trace=none --timestamps=unix,ns`` text.
* entry.sh is checked statically: with the marked decoy blocks and the one inert ``$DECOY_ENV``
  token removed, it is byte-identical to the pre-B3a script, so every non-decoy case runs the same
  commands (DECOY_ENV is empty for them and the blocks are guarded by the need check).
"""

import copy
import hashlib
import re
import sys
from pathlib import Path

import pytest

HERE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(HERE.parent))

from container_harness import case_driver  # noqa: E402
from container_harness import decoy_harness as D  # noqa: E402

CTL_ID = "DEV-E2E-V2_7-E12-P2-CTL"
INJ_ID = "DEV-E2E-V2_7-E12-P2-INJ"
ENTRY_SH = HERE / "entry.sh"
# sha256 of container_harness/entry.sh at 074fd36 (LF), the last pre-B3a version.
PRE_B3A_ENTRY_SHA256 = "6f76933f8cee117ecfa099c7362bec40da327eb3d43aa01543a08a99ff11dcef"


@pytest.fixture(scope="module")
def frozen_cases():
    root, _ = case_driver.frozen_root()
    cases, _ratified = case_driver.load_cases(root)
    return cases


# ------------------------------------------------------------------ needs / spec


def test_needs_only_the_e12_p2_pair(frozen_cases):
    needed = sorted(c["id"] for c in frozen_cases if D.case_needs(c))
    assert needed == [CTL_ID, INJ_ID]
    assert D.needs(CTL_ID, frozen_cases) and D.needs(INJ_ID, frozen_cases)
    others = [c["id"] for c in frozen_cases if c["id"] not in (CTL_ID, INJ_ID)]
    assert others and not any(D.needs(i, frozen_cases) for i in others)


def test_needs_unknown_case_raises(frozen_cases):
    with pytest.raises(KeyError):
        D.needs("NO-SUCH-CASE", frozen_cases)


@pytest.mark.parametrize("case_id", [CTL_ID, INJ_ID])
def test_spec_is_the_literal_frozen_argv(frozen_cases, case_id):
    case = D.load_case(case_id, frozen_cases)
    argv = D.spec(case)
    assert argv == ["python", "-c", "import time; time.sleep(1800)", "plot-onset-decoy"]
    assert tuple(argv) == D.LITERAL_ARGV


def test_spec_refuses_anything_but_the_literal_argv(frozen_cases):
    case = copy.deepcopy(D.load_case(CTL_ID, frozen_cases))
    entry = D.decoy_entries(case)[0]
    entry["payload"]["args"]["argv"] = [
        "python",
        "-c",
        "import os; os.kill(1, 9)",
        "plot-onset-decoy",
    ]
    with pytest.raises(D.DecoySpecError, match="not the literal"):
        D.spec(case)
    entry["payload"]["args"]["argv"] = ["python", "-c", "x\ny", "plot-onset-decoy"]
    with pytest.raises(D.DecoySpecError, match="newline"):
        D.spec(case)
    case["injection_schedule"].append(copy.deepcopy(entry))
    with pytest.raises(D.DecoySpecError, match="2 FX-DECOY-SPAWN"):
        D.spec(case)
    with pytest.raises(D.DecoySpecError, match="0 FX-DECOY-SPAWN"):
        D.spec({"id": "X", "injection_schedule": []})


def test_case_needs_ignores_other_operations():
    other = {"operation": "mutate_fixture", "payload": {"fixture_operation": "FX-OTHER"}}
    wrong_op = {"operation": "inject_metric", "payload": {"fixture_operation": "FX-DECOY-SPAWN"}}
    assert not D.case_needs({"injection_schedule": [other, wrong_op, "x", {"payload": None}]})
    assert not D.case_needs({})


# ------------------------------------------------------------------ trace parsing

START, END = 1760000010.0, 1760000020.0
PID, TRACER = 4321, 4322


def status(**over):
    s = {
        "run_start": f"{START:.9f}",
        "run_end": f"{END:.9f}",
        "decoy_needed": "1",
        "decoy_spawn": "ok",
        "decoy_pid": str(PID),
        "decoy_pgid": str(PID),
        "decoy_sid": str(PID),
        "decoy_tracer_pid": str(TRACER),
        "decoy_tracer_attached": "1",
        "decoy_alive_after_run": "1",
        "decoy_tracer_alive_at_run_end": "1",
    }
    s.update(over)
    return {k: v for k, v in s.items() if v is not None}


def sig(ts, name="SIGTERM", si_pid=777, si_uid=1000, code="SI_USER"):
    return f"{ts:.9f} --- {name} {{si_signo={name}, si_code={code}, si_pid={si_pid}, si_uid={si_uid}}} ---"


def test_parse_trace_kinds():
    text = "\n".join(
        [
            sig(1760000011.5),
            "4321 " + sig(1760000012.0, "SIGUSR1"),
            f"{1760000013.0:.9f} --- stopped by SIGSTOP ---",
            f"{1760000014.0:.9f} +++ killed by SIGKILL +++",
            f"{1760000015.0:.9f} +++ exited with 3 +++",
            "strace: Process 4321 attached",
            "garbage line",
            "",
        ]
    )
    parsed = D.parse_trace_text(text)
    kinds = [e["kind"] for e in parsed["events"]]
    assert kinds == ["signal", "signal", "stopped", "exit", "exit", "attach"]
    first = parsed["events"][0]
    assert first["signal"] == "SIGTERM" and first["si_pid"] == 777 and first["si_uid"] == 1000
    assert first["si_code"] == "SI_USER"
    assert parsed["events"][1]["tid"] == 4321
    assert parsed["events"][3]["killed_by"] == "SIGKILL"
    assert parsed["events"][4]["exit_code"] == 3
    assert parsed["anomalies"] == ["garbage line"]


def test_signals_inside_and_outside_the_window(tmp_path):
    text = "\n".join(
        [
            sig(START - 5, "SIGHUP", si_pid=1),  # before run_start (e.g. during prepare)
            sig(START + 1, "SIGTERM", si_pid=900),
            sig(START + 2, "SIGCONT", si_pid=901),
            sig(END + 1, "SIGINT", si_pid=0),  # after run_end
        ]
    )
    obs = D.observe(tmp_path, status=status(), trace_text=text)
    assert obs["status"] == D.OBSERVED
    assert obs["pid"] == PID and obs["alive_after_run"] is True
    assert [(e["signal"], e["si_pid"]) for e in obs["signals_received"]] == [
        ("SIGTERM", 900),
        ("SIGCONT", 901),
    ]
    assert obs["signals_outside_window"] == 2
    assert obs["decoy_exit"] is None


def test_no_signals_is_an_observed_empty_list(tmp_path):
    (tmp_path / D.TRACE_NAME).write_text("", encoding="utf-8")
    obs = D.observe(tmp_path, status=status())
    assert obs["status"] == D.OBSERVED and obs["signals_received"] == []


def test_reads_status_and_trace_from_out_dir(tmp_path):
    lines = [f"{k}={v}" for k, v in status().items()] + ["decoy_alive_after_run=0"]  # last wins
    (tmp_path / D.STATUS_NAME).write_text("\n".join(lines) + "\n", encoding="utf-8")
    (tmp_path / D.TRACE_NAME).write_text(sig(START + 3) + "\n", encoding="utf-8")
    obs = D.observe(tmp_path)
    assert obs["status"] == D.OBSERVED and obs["alive_after_run"] is False
    assert len(obs["signals_received"]) == 1


def test_tracer_that_died_is_missing(tmp_path):
    text = sig(START + 1)  # the record stops early: no decoy exit, tracer gone at run_end
    obs = D.observe(tmp_path, status=status(decoy_tracer_alive_at_run_end="0"), trace_text=text)
    assert obs["status"] == D.MISSING
    assert "not attached at run_end" in obs["reason"]
    assert "signals_received" not in obs


def test_decoy_killed_during_run_is_observed_even_though_the_tracer_exited(tmp_path):
    text = "\n".join([sig(START + 1, "SIGKILL"), f"{START + 1:.9f} +++ killed by SIGKILL +++"])
    st = status(decoy_tracer_alive_at_run_end="0", decoy_alive_after_run="0")
    obs = D.observe(tmp_path, status=st, trace_text=text)
    assert obs["status"] == D.OBSERVED
    assert obs["alive_after_run"] is False
    assert obs["decoy_exit"]["killed_by"] == "SIGKILL"
    assert [e["signal"] for e in obs["signals_received"]] == ["SIGKILL"]


def test_missing_trace_file_is_missing_with_reason(tmp_path):
    obs = D.observe(tmp_path, status=status())
    assert obs["status"] == D.MISSING and "decoy trace file absent" in obs["reason"]


@pytest.mark.parametrize(
    "over,needle",
    [
        ({"decoy_needed": None}, "not needed"),
        ({"decoy_needed": "error"}, "need check failed"),
        ({"decoy_spawn": "failed:spec_exit_2", "decoy_pid": None}, "not spawned"),
        ({"decoy_tracer_attached": "0"}, "never attached"),
        ({"run_end": None}, "no run window"),
        ({"decoy_alive_after_run": None}, "no alive check"),
    ],
)
def test_missing_reasons(tmp_path, over, needle):
    obs = D.observe(tmp_path, status=status(**over), trace_text=sig(START + 1))
    assert obs["status"] == D.MISSING and needle in obs["reason"], obs


# ------------------------------------------------------------------ entry.sh

BLOCK_RE = re.compile(r"^[ \t]*# >>> decoy-harness.*?^[ \t]*# <<< decoy-harness\n", re.S | re.M)


def entry_text():
    return ENTRY_SH.read_bytes().replace(b"\r\n", b"\n").decode("utf-8")


def test_entry_sh_non_decoy_path_is_byte_identical_to_pre_b3a():
    text = entry_text()
    assert len(BLOCK_RE.findall(text)) == 3
    stripped = BLOCK_RE.sub("", text).replace("$DECOY_ENV ", "")
    assert hashlib.sha256(stripped.encode("utf-8")).hexdigest() == PRE_B3A_ENTRY_SHA256


def test_entry_sh_decoy_pid_only_on_the_untraced_prepare_line():
    text = entry_text()
    uses = [ln for ln in text.splitlines() if "$DECOY_ENV" in ln]
    assert len(uses) == 1 and "case_driver prepare" in uses[0], uses
    assert "export SIMKIT_DECOY_PID" not in text
    assert text.count("SIMKIT_DECOY_PID") == 1
    # DECOY_ENV starts empty for every case and is set only after a verified spawn.
    blocks = BLOCK_RE.findall(text)
    assert 'DECOY_ENV=""' in blocks[0] and "DECOY=0" in blocks[0]


def test_entry_sh_decoy_ordering():
    text = entry_text()
    stage = text.index("case_driver stage")
    spawn = text.index("exec $AS_RUNNER setsid")
    prepare = text.index("case_driver prepare")
    baseline = text.index('> "$OUT/baseline.txt"')
    tracer = text.index('strace -p "$DPID" -e trace=none -e signal=all --timestamps=unix,ns')
    run_end = text.index('status run_end "$(date +%s.%N)"')
    teardown = text.index("decoy_alive_after_run 1")
    assert stage < spawn < tracer < prepare < baseline < run_end < teardown
    # the need check guards everything; the teardown runs only after a verified spawn
    assert 'if [ "$DECOY_NEED" = 0 ]; then' in text and 'if [ "$DECOY" = 1 ]; then' in text
    for key in ("decoy_pid", "decoy_pgid", "decoy_tracer_pid", "decoy_tracer_alive_at_run_end"):
        assert f"status {key} " in text
        assert key in D.STATUS_KEYS
