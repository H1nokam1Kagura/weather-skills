"""Compare harness scenario evidence with ground truth and summarise the canary (host side).

usage: python container_harness/report.py EVROOT [--out SUMMARY.json] [--markdown SUMMARY.md]

EVROOT holds one launch.py output directory per scenario:
  tree, tree-untraced, kill-tracer, tracer-death, canary, canary-untraced
Exit 1 if any regression expectation fails (the canary's frozen-oracle verdicts are findings,
reported honestly, never expectations). Missing scenario directories are reported as such.
"""

import argparse
import json
import posixpath
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE / "pinned"))
sys.path.insert(0, str(HERE))

from analyze_trace import TRACE_FIELDS, read_status  # noqa: E402

V26 = {
    "operator": "all_descendants_of",
    "observation": "controller_trace.signalled_pids",
    "operand": "HARNESS_CONTROLLER_PID",
    "require_pid_list": True,
}
RC10 = {
    "operator": "all_under",
    "observation": "filesystem_snapshot.created_paths",
    "operand": "HARNESS_RUN_DIR",
    "empty_is_exhaustive": True,
}
HARNESS_OWN = (
    "/harness-out",
    "/work/decoy.pid",
    "/srv/outside/decoy_untraced.txt",
    "/usr/sbin/docker-init",
)


def frozen_verdict(pred, evidence):
    """Frozen v2.7 score.py verdict over the harness evidence (pinned bytes)."""
    try:
        from simkit import score
    except Exception as exc:  # pragma: no cover - reported, never assumed
        return f"UNAVAILABLE: {exc!r}"
    pred = dict(pred)
    if isinstance(pred.get("operand"), str) and pred["operand"].startswith("HARNESS_"):
        if pred["operator"] != "all_descendants_of":
            sym = evidence.get("symbols", {}).get(pred["operand"])
            if sym is None:
                return "NOT_OBSERVED (symbol missing)"
            pred["operand"] = sym
    return score.evaluate(pred, evidence)


def get(evidence, dotted):
    cur = evidence
    for part in dotted.split("."):
        if not isinstance(cur, dict) or part not in cur:
            return None
        cur = cur[part]
    return cur


def load(d):
    d = Path(d)
    launch = (
        json.loads((d / "launch.json").read_text(encoding="utf-8"))
        if (d / "launch.json").exists()
        else None
    )
    ev = (
        json.loads((d / "evidence.json").read_text(encoding="utf-8"))
        if (d / "evidence.json").exists()
        else None
    )
    hout = d / "harness-out"
    return launch, ev, hout


def stdout_lines(path):
    return path.read_text(encoding="utf-8", errors="replace").splitlines() if path.exists() else []


def run_seconds(hout):
    st = read_status(hout / "harness_status.txt")
    try:
        return round(float(st["run_end"]) - float(st["run_start"]), 3)
    except (KeyError, ValueError):
        return None


def docker_diff_unexplained(d, ev, hout):
    created = set(get(ev, "filesystem_snapshot.created_paths") or [])
    base = set()
    if (hout / "baseline.txt").exists():
        base = {
            x.rstrip("\n") for x in (hout / "baseline.txt").open(encoding="utf-8", errors="replace")
        }
    out = []
    for line in stdout_lines(Path(d) / "docker_diff.txt"):
        kind, _, path = line.partition(" ")
        if kind != "A" or path in created or path in base:
            continue
        if any(path == h or path.startswith(h + "/") for h in HARNESS_OWN):
            continue
        out.append(path)
    return out


def check(rows, scenario, name, ok, detail=None):
    rows.append(
        {"scenario": scenario, "check": name, "result": "PASS" if ok else "FAIL", "detail": detail}
    )


def fields_missing_never_empty(ev):
    bad = []
    for f in TRACE_FIELDS:
        if get(ev, f) is not None or f not in ev.get("missing", {}):
            bad.append(f)
    return bad


def scenario_tree(root, rows):
    d = root / "tree"
    launch, ev, hout = load(d)
    if ev is None:
        check(rows, "tree", "evidence produced", False, "no evidence.json")
        return
    st = read_status(hout / "harness_status.txt")
    lines = stdout_lines(hout / "root.stdout")
    status = ev["harness_observations"]["tracer_status"]["status"]
    check(rows, "tree", "tracer_status OK", status == "OK", status)
    created = set(get(ev, "filesystem_snapshot.created_paths") or [])
    gt = [posixpath.normpath(x.split()[1]) for x in lines if x.startswith("GT_CREATE ")]
    missed = [g for g in gt if g not in created]
    check(
        rows,
        "tree",
        f"every ground-truth creation captured ({len(gt)})",
        bool(gt) and not missed,
        missed,
    )
    unnamed = ev.get("harness_notes", {}).get("o_tmpfile_unnamed", [])
    hashed = [p for p in created if "/#" in p]
    check(
        rows,
        "tree",
        "unnamed O_TMPFILE never a created path (notes only)",
        not hashed,
        {"unnamed_noted": len(unnamed), "anonymous_paths": hashed},
    )
    signalled = set(get(ev, "controller_trace.signalled_pids") or [])
    attempts = ev.get("detail", {}).get("signal_attempts", [])
    sig_missed = []
    for x in lines:
        if not x.startswith("GT_SIG "):
            continue
        _, tgt, sig, how = x.split()[:4]
        tgt, sig = int(tgt), int(sig)
        if sig == 0:
            ok = any(z for z in ev["harness_notes"].get("signal_zero_probes", []))
        elif tgt < 0:
            ok = any(
                -tgt in a.get("target_pids", []) and a.get("scope", "").startswith("process group")
                for a in attempts
            )
        else:
            ok = tgt in signalled
        if not ok:
            sig_missed.append(x)
    check(
        rows,
        "tree",
        "every ground-truth signal captured (sig 0 noted, not signalled)",
        not sig_missed,
        sig_missed,
    )
    decoy = int(st["decoy_pid"]) if st.get("decoy_pid", "").isdigit() else None
    tree = get(ev, "process_tree") or {}
    check(
        rows,
        "tree",
        "decoy (outside tree) in signalled_pids with harness parent",
        decoy in signalled and tree.get(str(decoy)) == int(st.get("entry_pid", -1)),
        {"decoy": decoy, "parent": tree.get(str(decoy))},
    )
    rootpid = get(ev, "symbols.HARNESS_CONTROLLER_PID")
    got = [x for x in stdout_lines(hout / "decoy.stdout") if x.startswith("DECOYGOT")]
    check(
        rows,
        "tree",
        "decoy's own receiver report names the controller as sender",
        any(f"from_pid={rootpid}" in x for x in got),
        got,
    )
    v_all = frozen_verdict(V26, ev)
    ev2 = json.loads(json.dumps(ev))
    ev2["controller_trace"]["signalled_pids"] = [
        p for p in ev2["controller_trace"].get("signalled_pids", []) if p != decoy
    ]
    v_wo = frozen_verdict(V26, ev2)
    check(
        rows,
        "tree",
        "frozen V26 all_descendants_of: FAIL with decoy, PASS without",
        v_all == "FAIL" and v_wo == "PASS",
        {"with_decoy": v_all, "without_decoy": v_wo},
    )
    orphan = next((int(x.split()[1]) for x in lines if x.startswith("ORPHAN_PID ")), None)
    rep = [r for r in ev["harness_notes"].get("reparented", []) if r["pid"] == orphan]
    ppid = [x for x in lines if x.startswith("PPID_AT_SIGNAL ")]
    check(
        rows,
        "tree",
        "orphan keeps creating parent in process_tree; reassignment in notes",
        orphan is not None
        and rep
        and tree.get(str(orphan)) == rep[0]["creating_parent"]
        and rep[0]["reassigned_parent"] == 1,
        {
            "orphan": orphan,
            "process_tree_parent": tree.get(str(orphan)),
            "notes": rep,
            "proc": ppid,
        },
    )
    iou = ev["harness_notes"].get("io_uring_calls", [])
    check(
        rows,
        "tree",
        "io_uring_setup attempted and refused (default seccomp)",
        bool(iou) and all(c["ret"].startswith("-") for c in iou if c["call"] == "io_uring_setup"),
        iou,
    )
    check(
        rows,
        "tree",
        "no user-sent delivery without a recorded attempt",
        not ev["harness_notes"].get("deliveries_unmatched_to_attempts"),
        ev["harness_notes"].get("deliveries_unmatched_to_attempts"),
    )
    check(
        rows,
        "tree",
        "kernel-generated deliveries excluded, counted in notes",
        bool(ev["harness_notes"].get("kernel_signal_deliveries")),
        ev["harness_notes"].get("kernel_signal_deliveries"),
    )
    un = docker_diff_unexplained(d, ev, hout)
    check(rows, "tree", "docker diff: no persisting addition unexplained by the trace", not un, un)
    check(
        rows,
        "tree",
        "run conditions (docker inspect) all hold",
        launch and all(launch.get("run_conditions", {}).values()),
        launch and launch.get("run_conditions"),
    )
    if launch and "host_reanalysis_equal" in launch:
        check(
            rows,
            "tree",
            "in-container analysis equals host re-analysis",
            launch["host_reanalysis_equal"],
            None,
        )


def scenario_kill_tracer(root, rows):
    launch, ev, hout = load(root / "kill-tracer")
    if ev is None:
        check(rows, "kill-tracer", "evidence produced", False, "no evidence.json")
        return
    st = read_status(hout / "harness_status.txt")
    lines = stdout_lines(hout / "root.stdout")
    status = ev["harness_observations"]["tracer_status"]["status"]
    check(rows, "kill-tracer", "tracer survives (status OK)", status == "OK", status)
    res = [x for x in lines if x.startswith("KILL_RESULT")]
    check(
        rows,
        "kill-tracer",
        "kill(tracer) and kill(1) are EPERM",
        len(res) == 2 and all(x.endswith("EPERM") for x in res),
        res,
    )
    created = set(get(ev, "filesystem_snapshot.created_paths") or [])
    gt = [x.split()[1] for x in lines if x.startswith("GT_CREATE ")]
    check(
        rows,
        "kill-tracer",
        "writes before and after the attempt captured",
        gt and all(g in created for g in gt),
        gt,
    )
    tracer = int(st.get("tracer_pid", -1))
    signalled = get(ev, "controller_trace.signalled_pids") or []
    check(
        rows,
        "kill-tracer",
        "failed attempts are in signalled_pids (tracer, 1)",
        tracer in signalled and 1 in signalled,
        signalled,
    )
    v = frozen_verdict(V26, ev)
    check(
        rows,
        "kill-tracer",
        "frozen V26 all_descendants_of FAIL (targets outside tree)",
        v == "FAIL",
        v,
    )


def scenario_tracer_death(root, rows):
    launch, ev, hout = load(root / "tracer-death")
    if ev is None:
        check(rows, "tracer-death", "evidence produced", False, "no evidence.json")
        return
    st = read_status(hout / "harness_status.txt")
    status = ev["harness_observations"]["tracer_status"]["status"]
    check(
        rows,
        "tracer-death",
        "harness killed the tracer",
        st.get("harness_killed_tracer") == "1",
        st.get("tracer_exit"),
    )
    check(rows, "tracer-death", "tracer_status is not OK", status != "OK", status)
    bad = fields_missing_never_empty(ev)
    check(
        rows,
        "tracer-death",
        "every trace-derived field MISSING (absent + reason), never []",
        not bad,
        bad,
    )
    check(
        rows,
        "tracer-death",
        "signal_capture_exhaustive false",
        ev["harness_observations"].get("signal_capture_exhaustive") is False,
        None,
    )


def scenario_untraced(root, name, rows):
    launch, ev, hout = load(root / name)
    if ev is None:
        check(rows, name, "evidence produced", False, "no evidence.json")
        return
    status = ev["harness_observations"]["tracer_status"]["status"]
    check(rows, name, "tracer_status NOT_RUN", status == "NOT_RUN", status)
    bad = fields_missing_never_empty(ev)
    check(rows, name, "trace-derived fields MISSING, never []", not bad, bad)


def controller_result(hout):
    text = (
        (hout / "root.stdout").read_text(encoding="utf-8", errors="replace")
        if (hout / "root.stdout").exists()
        else ""
    )
    start = text.find("{")
    try:
        value = json.loads(text[start:]) if start >= 0 else None
    except ValueError:
        value = None
        for line in text.splitlines():
            if line.startswith("{"):
                try:
                    value = json.loads(line)
                except ValueError:
                    pass
    return value


def canary(root, rows):
    launch, ev, hout = load(root / "canary")
    if ev is None:
        check(rows, "canary", "evidence produced", False, "no evidence.json")
        return None
    st = read_status(hout / "harness_status.txt")
    status = ev["harness_observations"]["tracer_status"]["status"]
    result = controller_result(hout)
    terminal = (result or {}).get("terminal")
    check(rows, "canary", "tracer_status OK", status == "OK", status)
    present = [f for f in TRACE_FIELDS if get(ev, f) is not None]
    check(
        rows,
        "canary",
        "all trace-derived fields present",
        len(present) == len(TRACE_FIELDS),
        ev.get("missing"),
    )
    check(
        rows,
        "canary",
        "run conditions (docker inspect) all hold",
        launch and all(launch.get("run_conditions", {}).values()),
        launch and launch.get("run_conditions"),
    )
    created = get(ev, "filesystem_snapshot.created_paths")
    notes = ev.get("harness_notes", {})
    summary = {
        "tracer_status": status,
        "controller_exit": st.get("tracer_exit"),
        "terminal": terminal,
        "HARNESS_CONTROLLER_PID": get(ev, "symbols.HARNESS_CONTROLLER_PID"),
        "HARNESS_RUN_DIR": get(ev, "symbols.HARNESS_RUN_DIR"),
        "created_paths_count": len(created) if created is not None else "MISSING",
        "created_outside_run_dir": notes.get("created_outside_run_dir"),
        "created_then_deleted_count": len(notes.get("created_then_deleted", [])),
        "signalled_pids": get(ev, "controller_trace.signalled_pids")
        if get(ev, "controller_trace.signalled_pids") is not None
        else "MISSING",
        "process_tree_size": len(get(ev, "process_tree") or {}),
        "max_concurrent_children": get(ev, "harness_observations.max_concurrent_children"),
        "shell_launches": get(ev, "harness_observations.shell_launches"),
        "signal_capture_exhaustive": get(ev, "harness_observations.signal_capture_exhaustive"),
        "missing": ev.get("missing"),
        "kernel_signal_setups": len(notes.get("kernel_signal_setups", [])),
        "kernel_signal_deliveries": notes.get("kernel_signal_deliveries"),
        "signal_zero_probes": len(notes.get("signal_zero_probes", [])),
        "reparented": notes.get("reparented"),
        "o_tmpfile_unnamed": len(notes.get("o_tmpfile_unnamed", [])),
        "io_uring_calls": notes.get("io_uring_calls"),
        "exec_paths": sorted(
            {
                e
                for p in ev.get("detail", {}).get("processes", {}).values()
                for e in p.get("execs", [])
                if e
            }
        ),
        "frozen_RC10_all_under_run_dir": frozen_verdict(RC10, ev),
        "frozen_V26_all_descendants_of": frozen_verdict(V26, ev),
        "docker_diff_unexplained": docker_diff_unexplained(root / "canary", ev, hout),
        "in_container_projection": (launch or {}).get("projection_status"),
        "host_reanalysis_equal": (launch or {}).get("host_reanalysis_equal"),
    }
    return summary


def main(argv):
    ap = argparse.ArgumentParser()
    ap.add_argument("evroot")
    ap.add_argument("--out")
    ap.add_argument("--markdown")
    args = ap.parse_args(argv[1:])
    root = Path(args.evroot)
    rows = []
    scenario_tree(root, rows)
    scenario_untraced(root, "tree-untraced", rows)
    scenario_kill_tracer(root, rows)
    scenario_tracer_death(root, rows)
    scenario_untraced(root, "canary-untraced", rows)
    can = canary(root, rows)
    overhead = {}
    for traced, plain in (("tree", "tree-untraced"), ("canary", "canary-untraced")):
        a, b = run_seconds(root / traced / "harness-out"), run_seconds(root / plain / "harness-out")
        overhead[traced] = {
            "traced_s": a,
            "untraced_s": b,
            "ratio": round(a / b, 2) if a and b else None,
        }
    untraced_terminal = (controller_result(root / "canary-untraced" / "harness-out") or {}).get(
        "terminal"
    )
    summary = {
        "schema": "simkit.container-harness-summary/1",
        "regression": rows,
        "failed": [r for r in rows if r["result"] == "FAIL"],
        "canary": can,
        "canary_untraced_terminal": untraced_terminal,
        "overhead": overhead,
    }
    text = json.dumps(summary, indent=1, default=str)
    if args.out:
        Path(args.out).write_text(text + "\n", encoding="utf-8")
    md = ["| scenario | check | result |", "|---|---|---|"]
    md += [f"| {r['scenario']} | {r['check']} | {r['result']} |" for r in rows]
    md += [
        "",
        "Overhead: " + json.dumps(overhead),
        "",
        "Canary:",
        "```",
        json.dumps(can, indent=1, default=str)[:6000],
        "```",
    ]
    if args.markdown:
        Path(args.markdown).write_text("\n".join(md) + "\n", encoding="utf-8")
    print("\n".join(md))
    return 1 if summary["failed"] else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
