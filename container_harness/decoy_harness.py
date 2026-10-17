"""Decoy harness: the FX-DECOY-SPAWN process for E12-P2, spawned and traced OUTSIDE the run tree.

Wave 8, Build B part B3a (WAVE8-BRIEF.md). Only for frozen v2.7 cases whose ``injection_schedule``
carries a ``mutate_fixture`` entry with ``fixture_operation == "FX-DECOY-SPAWN"`` (E12-P2-CTL and
E12-P2-INJ). Every other case runs exactly as before: ``entry.sh`` asks ``needs`` and, on "no",
writes nothing, starts nothing and changes no command line.

Process model (decision D7)
---------------------------
``entry.sh`` (root), in the case branch between ``stage`` and ``prepare`` and BEFORE the
filesystem baseline:

1. ``needs CASE_ID`` (exit 0 = needed, 1 = not needed, 2 = error), as uid 1000.
2. ``spec CASE_ID`` prints the frozen argv, one element per line (``spec`` refuses any element that
   holds a newline or NUL, and any argv other than ``LITERAL_ARGV``).
3. ``$AS_RUNNER setsid <literal argv> &``: the decoy runs as uid 1000 in its own session; its
   parent is the entry shell (a harness process, never a child of strace).
4. A root ``strace -p <decoy pid> -e trace=none -e signal=all --timestamps=unix,ns -o
   $OUT/decoy.trace`` records every signal delivered to the decoy. The tracer is a second harness
   process, a child of the entry shell.
5. ``SIMKIT_DECOY_PID`` is passed ONLY on the untraced ``prepare`` command line; the traced run's
   environment never carries it.
6. After ``run_end``: the alive check, the tracer liveness check, detach the tracer (SIGINT), then
   kill the decoy.

harness_status.txt lines written by ``entry.sh`` (``STATUS_KEYS``)
-----------------------------------------------------------------
``decoy_needed`` (1 | error; absent for every other case), ``decoy_spawn`` (ok | failed:<why>),
``decoy_pid``, ``decoy_pgid``, ``decoy_sid``, ``decoy_uid``, ``decoy_tracer_pid``,
``decoy_tracer_pgid``, ``decoy_tracer_attached`` (1 | 0), and after ``run_end``:
``decoy_alive_after_run`` (1 | 0), ``decoy_state_after_run`` (the /proc state letter or
``gone``), ``decoy_tracer_alive_at_run_end`` (1 | 0, and 1 only when the decoy's TracerPid still
names the tracer), ``decoy_tracer_exit``, ``decoy_kill_exit``.

Files in ``$OUT``: ``decoy.trace`` (strace -o), ``decoy_tracer.stderr``, ``decoy.stdout``,
``decoy_needs.stdout``, ``decoy_spec.stderr``.

``observe(out_dir)`` turns those into ``harness_observations.decoy``: ``{status: OBSERVED, pid,
alive_after_run, signals_received, ...}`` or ``{status: MISSING, reason}``. A tracer that was
gone at ``run_end`` while the decoy was still alive makes ``signals_received`` unprovable, so the
observation is MISSING, never ``[]``.
"""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path

FIXTURE_OPERATION = "FX-DECOY-SPAWN"
# The frozen FX-DECOY-SPAWN argv (both E12-P2 cases). ``spec`` reads the argv from the frozen case
# and refuses anything that is not exactly this; it is restated here only as that check.
LITERAL_ARGV = ("python", "-c", "import time; time.sleep(1800)", "plot-onset-decoy")
ENV_VAR = "SIMKIT_DECOY_PID"
TRACE_NAME = "decoy.trace"
STATUS_NAME = "harness_status.txt"
STATUS_KEYS = (
    "decoy_needed",
    "decoy_spawn",
    "decoy_pid",
    "decoy_pgid",
    "decoy_sid",
    "decoy_uid",
    "decoy_tracer_pid",
    "decoy_tracer_pgid",
    "decoy_tracer_attached",
    "decoy_alive_after_run",
    "decoy_state_after_run",
    "decoy_tracer_alive_at_run_end",
    "decoy_tracer_exit",
    "decoy_kill_exit",
)
OBSERVED, MISSING = "OBSERVED", "MISSING"


class DecoySpecError(ValueError):
    """The frozen case's FX-DECOY-SPAWN entry is absent, duplicated or not the literal argv."""


# ------------------------------------------------------------------ frozen case


def decoy_entries(case):
    """The case's FX-DECOY-SPAWN schedule entries (mutate_fixture payloads)."""
    out = []
    for entry in case.get("injection_schedule") or []:
        if not isinstance(entry, dict) or entry.get("operation") != "mutate_fixture":
            continue
        payload = entry.get("payload")
        if isinstance(payload, dict) and payload.get("fixture_operation") == FIXTURE_OPERATION:
            out.append(entry)
    return out


def case_needs(case):
    return bool(decoy_entries(case))


def spec(case):
    """-> the literal argv list of the case's single FX-DECOY-SPAWN entry (DecoySpecError else)."""
    entries = decoy_entries(case)
    if len(entries) != 1:
        raise DecoySpecError(
            f"{case.get('id')}: {len(entries)} {FIXTURE_OPERATION} entries (exactly 1 required)"
        )
    args = entries[0]["payload"].get("args")
    argv = args.get("argv") if isinstance(args, dict) else None
    if not isinstance(argv, list) or not all(isinstance(a, str) and a for a in argv):
        raise DecoySpecError(f"{case.get('id')}: argv is not a list of non-empty strings: {argv!r}")
    if any(("\n" in a) or ("\0" in a) for a in argv):
        raise DecoySpecError(f"{case.get('id')}: an argv element holds a newline or NUL")
    if tuple(argv) != LITERAL_ARGV:
        raise DecoySpecError(f"{case.get('id')}: argv {argv!r} is not the literal {LITERAL_ARGV!r}")
    return list(argv)


def load_case(case_id, cases=None):
    """The effective frozen case (the same load as interrupt_broker.needs and case_driver)."""
    if cases is None:
        from container_harness import case_driver

        root, _ = case_driver.frozen_root()
        cases, _ratified = case_driver.load_cases(root)
    for c in cases:
        if c["id"] == case_id:
            return c
    raise KeyError(f"no frozen case {case_id!r}")


def needs(case_id, cases=None):
    return case_needs(load_case(case_id, cases))


# ------------------------------------------------------------------ trace parsing

_TS = r"(?P<ts>\d+(?:\.\d+)?)"
_PID = r"(?:(?:\[pid\s+)?(?P<pid>\d+)\]?\s+)?"
_SIGNAL_RE = re.compile(
    rf"^{_PID}{_TS}\s+---\s+(?P<sig>SIG[A-Z0-9+]+)\s+(?P<info>\{{.*\}})\s+---\s*$"
)
_STOPPED_RE = re.compile(rf"^{_PID}{_TS}\s+---\s+stopped by\s+(?P<sig>SIG[A-Z0-9+]+)\s+---\s*$")
_EXIT_RE = re.compile(
    rf"^{_PID}{_TS}\s+\+\+\+\s+(?:(?P<exited>exited with (?P<code>-?\d+))|"
    rf"(?:killed by (?P<ksig>SIG[A-Z0-9+]+)(?P<core> \(core dumped\))?))\s+\+\+\+\s*$"
)
_ATTACH_RE = re.compile(
    rf"^{_PID}(?:{_TS}\s+)?(?:strace: )?Process (?P<apid>\d+) (?P<what>attached|detached)\b"
)
_INFO_FIELD_RE = re.compile(r"(si_signo|si_code|si_pid|si_uid|si_status)=([^,}]+)")


def _maybe_int(v):
    try:
        return int(v)
    except (TypeError, ValueError):
        return v


def parse_trace_text(text):
    """-> {events: [...], anomalies: [...]} from ``strace -e trace=none --timestamps=unix,ns``.

    Event kinds: ``signal`` (a delivery, with the siginfo fields), ``stopped`` (group-stop),
    ``exit`` (``+++ exited with N +++`` / ``+++ killed by SIG +++``), ``attach`` / ``detach``.
    Any other non-blank line is an anomaly (kept verbatim, never dropped silently)."""
    events, anomalies = [], []
    for raw in text.splitlines():
        line = raw.rstrip("\r")
        if not line.strip():
            continue
        m = _SIGNAL_RE.match(line)
        if m:
            info = {k: _maybe_int(v.strip()) for k, v in _INFO_FIELD_RE.findall(m["info"])}
            ev = {"kind": "signal", "ts": float(m["ts"]), "signal": m["sig"]}
            ev.update(info)
            if m["pid"]:
                ev["tid"] = int(m["pid"])
            events.append(ev)
            continue
        m = _STOPPED_RE.match(line)
        if m:
            events.append({"kind": "stopped", "ts": float(m["ts"]), "signal": m["sig"]})
            continue
        m = _EXIT_RE.match(line)
        if m:
            ev = {"kind": "exit", "ts": float(m["ts"])}
            if m["exited"]:
                ev["exit_code"] = int(m["code"])
            else:
                ev["killed_by"] = m["ksig"]
                ev["core_dumped"] = bool(m["core"])
            events.append(ev)
            continue
        m = _ATTACH_RE.match(line)
        if m:
            events.append(
                {
                    "kind": "attach" if m["what"] == "attached" else "detach",
                    "ts": float(m["ts"]) if m["ts"] else None,
                    "pid": int(m["apid"]),
                }
            )
            continue
        anomalies.append(line)
    return {"events": events, "anomalies": anomalies}


def in_window(ev, start, end):
    ts = ev.get("ts")
    return ts is not None and start <= ts <= end


# ------------------------------------------------------------------ observation


def read_status(path):
    """KEY=VALUE lines, last value wins (the analyzer's read_status semantics)."""
    status = {}
    path = Path(path)
    if path.exists():
        for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
            if "=" in line:
                k, v = line.split("=", 1)
                status[k.strip()] = v.strip()
    return status


def _int(v):
    try:
        return int(v)
    except (TypeError, ValueError):
        return None


def _float(v):
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def _missing(reason, **extra):
    out = {"status": MISSING, "reason": reason}
    out.update(extra)
    return out


def observe(out_dir, status=None, trace_text=None):
    """-> ``harness_observations.decoy`` from ``$OUT`` (or explicit status / trace text).

    OBSERVED: ``{pid, pgid, alive_after_run, signals_received, signals_outside_window,
    decoy_exit, tracer_complete_basis, anomalies}``. MISSING: ``{reason}``, never an empty list
    standing in for an unobserved window."""
    out_dir = Path(out_dir)
    if status is None:
        status = read_status(out_dir / STATUS_NAME)
    needed = status.get("decoy_needed")
    if needed is None:
        return _missing("decoy not needed for this case (no decoy_needed status line)")
    if needed != "1":
        return _missing(f"decoy need check failed (decoy_needed={needed})")
    spawn = status.get("decoy_spawn")
    pid = _int(status.get("decoy_pid"))
    if spawn != "ok" or pid is None:
        return _missing(
            f"decoy was not spawned (decoy_spawn={spawn}, decoy_pid={status.get('decoy_pid')})"
        )
    base = {
        "pid": pid,
        "pgid": _int(status.get("decoy_pgid")),
        "sid": _int(status.get("decoy_sid")),
    }
    alive = status.get("decoy_alive_after_run")
    alive_after_run = {"1": True, "0": False}.get(alive)
    start, end = _float(status.get("run_start")), _float(status.get("run_end"))
    if start is None or end is None:
        return _missing("run_start/run_end not both recorded: no run window", **base)
    if status.get("decoy_tracer_attached") != "1":
        return _missing(
            f"the decoy tracer never attached (decoy_tracer_attached="
            f"{status.get('decoy_tracer_attached')})",
            **base,
        )
    if trace_text is None:
        tpath = out_dir / TRACE_NAME
        if not tpath.is_file():
            return _missing(f"decoy trace file absent: {tpath.name}", **base)
        trace_text = tpath.read_text(encoding="utf-8", errors="replace")
    parsed = parse_trace_text(trace_text)
    events = parsed["events"]
    exits = [e for e in events if e["kind"] == "exit"]
    decoy_exit = exits[0] if exits else None
    tracer_at_end = status.get("decoy_tracer_alive_at_run_end")
    if tracer_at_end == "1":
        basis = "tracer attached to the decoy at run_end"
    elif decoy_exit is not None and decoy_exit["ts"] <= end:
        # strace exits once its only tracee is gone: the trace is complete up to the decoy's death.
        basis = "decoy exited during the run; the trace ends with its exit record"
    else:
        return _missing(
            f"the decoy tracer was not attached at run_end (decoy_tracer_alive_at_run_end="
            f"{tracer_at_end}) and the trace holds no decoy exit before run_end: the signal record "
            f"for the run window is incomplete",
            **base,
        )
    if alive_after_run is None:
        return _missing(f"no alive check after run_end (decoy_alive_after_run={alive})", **base)
    signals = [e for e in events if e["kind"] in ("signal", "stopped")]
    inside = [e for e in signals if in_window(e, start, end)]
    outside = [e for e in signals if not in_window(e, start, end)]
    out = {"status": OBSERVED}
    out.update(base)
    out.update(
        {
            "alive_after_run": alive_after_run,
            "signals_received": inside,
            "signals_outside_window": len(outside),
            "decoy_exit": decoy_exit
            if decoy_exit is not None and in_window(decoy_exit, start, end)
            else None,
            "tracer_complete_basis": basis,
            "run_window": [start, end],
            "anomalies": parsed["anomalies"],
        }
    )
    return out


# ------------------------------------------------------------------ CLI


def main(argv):
    import argparse

    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = ap.add_subparsers(dest="cmd", required=True)
    for name in ("needs", "spec"):
        p = sub.add_parser(name)
        p.add_argument("case_id")
    o = sub.add_parser("observe")
    o.add_argument("out_dir")
    args = ap.parse_args(argv[1:])
    if args.cmd == "observe":
        print(json.dumps(observe(args.out_dir), indent=2, sort_keys=True))
        return 0
    try:
        case = load_case(args.case_id)
        if args.cmd == "needs":
            n = len(decoy_entries(case))
            print(json.dumps({"case_id": args.case_id, "fx_decoy_spawn_entries": n}))
            return 0 if n else 1
        # One element per line, LF only (entry.sh compares it to /proc/<pid>/cmdline, NUL -> LF).
        sys.stdout.buffer.write(("\n".join(spec(case)) + "\n").encode("utf-8"))
        sys.stdout.flush()
        return 0
    except Exception as exc:  # noqa: BLE001
        # spec's stdout is the argv itself, so its error goes to stderr (decoy_spec.stderr).
        err = sys.stderr if args.cmd == "spec" else sys.stdout
        print(
            json.dumps({"case_id": args.case_id, "error": f"{type(exc).__name__}: {exc}"}), file=err
        )
        return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv))
