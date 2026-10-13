"""Opt-in Windows canary for tools/workflow/signal_stop.py on synthetic owned trees.

Every process here is a synthetic python (or cmd.exe) child of this canary. A decoy
outside every tree runs throughout and must never be signalled. Writes
completion-evidence/signal-stop-canary/RESULT.json. This is prototype evidence for
CAP-SIGNAL-CAPTURE feasibility, not SIMKIT scoring evidence.
"""

import argparse
import json
import os
import platform
import signal
import subprocess
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from tools.workflow import signal_stop as ss  # noqa: E402

PYTHON = getattr(sys, "_base_executable", None) or sys.executable
NO_WINDOW = 0x08000000

CHILD = r"""
import os, subprocess, sys, time
from pathlib import Path
mode, d = sys.argv[1], Path(sys.argv[2])
exe, me = sys.executable, os.path.abspath(__file__)
def mark(tag):
    (d / f"{tag}-{os.getpid()}").write_text(str(os.getppid()))
def spawn(m, *a):
    # DETACHED_PROCESS: no shared console, so killing the root's conhost cannot kill
    # an unrecorded descendant collaterally and mask a capture miss.
    return subprocess.Popen([exe, me, m, str(d), *a], creationflags=0x8)
if mode == "leaf":
    mark("leaf"); time.sleep(600)
elif mode == "mid":
    spawn("leaf"); mark("mid"); time.sleep(600)
elif mode == "tree":
    spawn("mid"); spawn("mid"); mark("root"); time.sleep(600)
elif mode == "mid_exit":
    spawn("leaf"); mark("mid"); time.sleep(float(sys.argv[3])); sys.exit(0)
elif mode == "orphan":
    spawn("mid_exit", sys.argv[3]); mark("root"); time.sleep(600)
elif mode == "fast_orphan":
    # cmd.exe lives a few ms: it starts the leaf detached and exits at once.
    subprocess.Popen(["cmd.exe", "/c", "start", "", "/b", exe, me, "leaf", str(d)])
    mark("root"); time.sleep(600)
elif mode == "blip":
    time.sleep(0.03)
elif mode == "spawner":
    mark("root"); i = 0
    while True:
        spawn("leaf" if i % 10 == 0 else "blip"); i += 1; time.sleep(0.005)
"""


def write_child(workdir):
    path = Path(workdir) / "synthetic_child.py"
    path.write_text(CHILD, encoding="utf-8")
    return path


def wait_marks(d, prefix, count, timeout=20.0):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        found = sorted(p for p in Path(d).iterdir() if p.name.startswith(prefix + "-"))
        if len(found) >= count:
            return [int(p.name.split("-")[1]) for p in found]
        time.sleep(0.02)
    raise TimeoutError(f"{count} {prefix} marker(s) not seen in {d}")


def pid_alive(pid, backend=None):
    # Never os.kill(pid, 0): on Windows any non-CTRL signal is TerminateProcess.
    backend = backend or ss.Win32Backend()
    try:
        handle = backend.open(pid)
    except ss.OpenError:
        return False
    try:
        return not backend.is_exited(handle)
    finally:
        backend.close(handle)


def kill_pid(pid):
    try:
        os.kill(pid, signal.SIGTERM)  # TerminateProcess on Windows
    except OSError:
        pass


def start_decoy(workdir):
    d = Path(workdir) / "decoy"
    d.mkdir(exist_ok=True)
    proc = subprocess.Popen(
        [PYTHON, str(write_child(workdir)), "leaf", str(d)], creationflags=NO_WINDOW
    )
    wait_marks(d, "leaf", 1)
    return proc


def run_scenario(
    name,
    mode,
    workdir,
    *,
    job,
    journal_poll,
    settle_s=0.0,
    extra=(),
    wait=None,
    timeout_s=10.0,
    decoy=None,
):
    """Build one synthetic tree, stop it with signal_stop, and summarize the record."""
    d = Path(workdir) / name
    d.mkdir(parents=True, exist_ok=True)
    argv = [PYTHON, str(write_child(workdir)), mode, str(d), *extra]
    census = None
    if job:
        proc, census = ss.launch_in_census_job(argv, creationflags=NO_WINDOW)
    else:
        proc = subprocess.Popen(argv, creationflags=NO_WINDOW)
    journal = ss.AncestryJournal(proc)
    if journal_poll:
        journal.start(journal_poll)
    leaves, survivors = [], None
    try:
        if wait:
            for prefix, count in wait:
                got = wait_marks(d, prefix, count)
                if prefix == "leaf":
                    leaves = got
        if settle_s:
            time.sleep(settle_s)
        record = ss.stop_tree(journal, job=census, timeout_s=timeout_s)
        survivors = [p for p in leaves if pid_alive(p)]  # checked before any cleanup
    finally:
        journal.close()
        if survivors is None:
            survivors = [p for p in leaves if pid_alive(p)]
        for pid in survivors:
            kill_pid(pid)
        if census is not None:
            census.close()
        if proc.poll() is None:
            proc.kill()
        proc.wait(timeout=10)
    decoy_pid = decoy.pid if decoy is not None else None
    attempted = {a["pid"] for a in record["signal_attempts"]}
    return {
        "scenario": name,
        "job": job,
        "journal_poll_s": journal_poll,
        "exhaustive": record["exhaustive"],
        "exhaustive_reasons": record["exhaustive_reasons"],
        "signalled": len(record["signalled_pids"]),
        "results": _count(a["result"] for a in record["signal_attempts"]),
        "all_signalled_recorded_before": all(
            a["sequence"] and a["observed_at_ns"] <= a.get("returned_at_ns", a["observed_at_ns"])
            for a in record["signal_attempts"]
        ),
        "every_signalled_in_tree": all(
            str(p) in record["process_tree"] for p in record["signalled_pids"]
        ),
        "every_signalled_chains_to_controller": all(
            _chains(record, p) for p in record["signalled_pids"]
        ),
        "unproven_pids": record["unproven_pids"],
        "churn_pids": len(record["churn_pids"]),
        "rounds": record["rounds"],
        "leaf_pids_from_markers": leaves,
        "leaves_survived_stop": survivors,
        "decoy_attempted": decoy_pid in attempted if decoy_pid else None,
        "decoy_alive_after": decoy.poll() is None if decoy is not None else None,
        "journal": record["journal"],
        "job_info": {k: v for k, v in record["job"].items() if k != "role"},
        "record": record,
    }


def _count(items):
    out = {}
    for item in items:
        out[item] = out.get(item, 0) + 1
    return out


def _chains(record, pid):
    """The frozen scorer's in_tree walk (score.py _op_all_descendants_of)."""
    tree = {str(k): str(v) for k, v in record["process_tree"].items()}
    root, cur, seen = str(record["controller_pid"]), str(pid), set()
    while True:
        if cur == root:
            return True
        if cur not in tree or cur in seen:
            return False
        seen.add(cur)
        cur = tree[cur]


SCENARIOS = {
    # name: (mode, kwargs)
    "normal_job_journal": (
        "tree",
        dict(job=True, journal_poll=0.02, wait=[("leaf", 2), ("mid", 2), ("root", 1)]),
    ),
    "normal_job_stoptime": (
        "tree",
        dict(job=True, journal_poll=None, wait=[("leaf", 2), ("mid", 2), ("root", 1)]),
    ),
    "normal_no_job_journal": (
        "tree",
        dict(job=False, journal_poll=0.02, wait=[("leaf", 2), ("mid", 2), ("root", 1)]),
    ),
    "orphan_job_journal": (
        "orphan",
        dict(
            job=True,
            journal_poll=0.02,
            extra=("1.0",),
            wait=[("leaf", 1), ("mid", 1)],
            settle_s=1.5,
        ),
    ),
    "orphan_job_stoptime": (
        "orphan",
        dict(
            job=True,
            journal_poll=None,
            extra=("1.0",),
            wait=[("leaf", 1), ("mid", 1)],
            settle_s=1.5,
        ),
    ),
    "orphan_no_job_stoptime": (
        "orphan",
        dict(
            job=False,
            journal_poll=None,
            extra=("1.0",),
            wait=[("leaf", 1), ("mid", 1)],
            settle_s=1.5,
        ),
    ),
    "fast_orphan_job_journal": (
        "fast_orphan",
        dict(job=True, journal_poll=0.02, wait=[("leaf", 1)], settle_s=0.3),
    ),
    # Intermediate lifetime (cmd.exe, ms) shorter than the poll gap: the race window.
    "fast_orphan_job_slow_journal": (
        "fast_orphan",
        dict(job=True, journal_poll=0.5, wait=[("leaf", 1)], settle_s=1.2),
    ),
    "churn_job_journal": (
        "spawner",
        dict(job=True, journal_poll=0.02, wait=[("root", 1)], settle_s=1.0),
    ),
}
REPEATS = {"fast_orphan_job_journal": 10, "fast_orphan_job_slow_journal": 5, "churn_job_journal": 3}


def main(argv=None):
    if os.name != "nt":
        raise SystemExit("Windows only")
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", default=str(Path(__file__).parent / "signal-stop-canary"))
    parser.add_argument("--quick", action="store_true", help="one repeat per scenario")
    args = parser.parse_args(argv)
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    results = []
    with tempfile.TemporaryDirectory(prefix="signal-stop-canary-") as work:
        decoy = start_decoy(work)
        try:
            for name, (mode, kw) in SCENARIOS.items():
                reps = 1 if args.quick else REPEATS.get(name, 1)
                for i in range(reps):
                    label = f"{name}#{i + 1}" if reps > 1 else name
                    r = run_scenario(label.replace("#", "_"), mode, work, decoy=decoy, **kw)
                    r["scenario"] = label
                    results.append(r)
                    print(
                        f"{label}: exhaustive={r['exhaustive']} signalled={r['signalled']} "
                        f"unproven={len(r['unproven_pids'])} survived={r['leaves_survived_stop']}"
                        f" decoy_attempted={r['decoy_attempted']}",
                        flush=True,
                    )
        finally:
            decoy_alive = decoy.poll() is None
            decoy.kill()
            decoy.wait()
    for r in results:  # keep full records only where they are small
        if r["signalled"] > 40:
            r["record"] = {"omitted": f"{r['signalled']} signals; summary above"}
    summary = {}
    for r in results:
        base = r["scenario"].split("#")[0]
        s = summary.setdefault(base, {"runs": 0, "exhaustive_true": 0})
        s["runs"] += 1
        s["exhaustive_true"] += int(r["exhaustive"])
    doc = {
        "schema": "signal-stop-canary/1",
        "generated_utc": ss._utc_now(),
        "host": {"platform": platform.platform(), "python": sys.version.split()[0]},
        "not_evidence_for": "SIMKIT scoring; synthetic owned trees only",
        "decoy_alive_at_end": decoy_alive,
        "decoy_never_attempted": all(r["decoy_attempted"] is False for r in results),
        "summary": summary,
        "runs": results,
    }
    path = out / "RESULT.json"
    path.write_text(json.dumps(doc, indent=1, sort_keys=False) + "\n", encoding="utf-8")
    print(json.dumps(summary, indent=1))
    print(f"wrote {path}")
    return 0 if doc["decoy_alive_at_end"] and doc["decoy_never_attempted"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
