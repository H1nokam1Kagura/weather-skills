"""CLI: score | record-outcome | report | fetch-laya | verify-laya.

`score` ALWAYS exits 0 and by default prints only the decision_id, never the shadow's
choice, so a calling agent cannot accidentally act on it. `--show` prints the logged record
(for demos and the smoke test, not for agents).
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import uuid
from pathlib import Path

from . import report as report_mod
from .shadow import enabled_backend, read_log, record_outcome, shadow_score

ENTRY = Path(__file__).resolve().parent.parent / "ds.py"


def _options(args) -> object:
    if args.options_json:
        return json.loads(args.options_json)
    return args.option or None


def _state(args) -> str:
    if args.state is not None:
        return args.state
    if args.state_file == "-":
        return sys.stdin.read()
    if args.state_file:
        return Path(args.state_file).read_text(encoding="utf-8")
    return ""


def cmd_score(args) -> int:
    try:
        backend = args.backend or enabled_backend()
        if backend is None:
            print(json.dumps({"decision_id": None, "shadow": "off"}))
            return 0
        state = _state(args)
        options = _options(args)
        if args.background:
            # Detached child does the work; this process returns at once (never delays the run).
            did = uuid.uuid4().hex
            cmd = [sys.executable, str(ENTRY), "score", "--point", args.point, "--backend", backend,
                   "--decision-id", did, "--state-file", "-"]
            if args.options_json or args.option:
                cmd += ["--options-json", json.dumps(options)]
            if args.actual is not None:
                cmd += ["--actual", args.actual]
            flags = 0
            if os.name == "nt":
                flags = subprocess.CREATE_NEW_PROCESS_GROUP | subprocess.DETACHED_PROCESS  # type: ignore[attr-defined]
            p = subprocess.Popen(cmd, stdin=subprocess.PIPE, stdout=subprocess.DEVNULL,
                                 stderr=subprocess.DEVNULL, creationflags=flags)
            p.stdin.write(state.encode("utf-8"))
            p.stdin.close()
            print(json.dumps({"decision_id": did, "shadow": backend, "mode": "background"}))
            return 0
        did = shadow_score(args.point, state, options, backend=backend, actual=args.actual,
                           decision_id=args.decision_id)
        if args.show and did:
            rec = next((r for r in reversed(read_log()) if r.get("decision_id") == did), None)
            print(json.dumps(rec, indent=2))
        else:
            print(json.dumps({"decision_id": did, "shadow": backend}))
    except Exception as e:  # noqa: BLE001  the shadow never fails the caller
        print(json.dumps({"decision_id": None, "shadow": "error", "error": str(e)}))
    return 0


def cmd_record(args) -> int:
    ok = record_outcome(args.id, args.actual, args.point)
    print(json.dumps({"recorded": ok, "decision_id": args.id}))
    return 0


def cmd_report(args) -> int:
    rows = read_log(Path(args.log) if args.log else None)
    sys.stdout.write(report_mod.render(rows))
    return 0


def cmd_fetch(args) -> int:
    from .fetch import fetch
    fetch(write_lock=args.write_lock, force=args.force)
    return 0


def cmd_verify(args) -> int:
    from .backends import laya_verify_only
    print(json.dumps({"verified": True, "revision": laya_verify_only()}))
    return 0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="ds.py", description="Shadow-only decision reviewer (advisory, never decides)")
    sub = ap.add_subparsers(dest="cmd", required=True)

    s = sub.add_parser("score", help="score one decision in shadow and log it")
    s.add_argument("--point", required=True, choices=["escalate", "review_verdict", "next_skill"])
    g = s.add_mutually_exclusive_group()
    g.add_argument("--state", help="state text (prefer --state-file - to keep it out of argv)")
    g.add_argument("--state-file", help="path, or - for stdin")
    s.add_argument("--option", action="append", help="candidate (repeat); next_skill only")
    s.add_argument("--options-json", help='JSON list or {"skill": "description"} map')
    s.add_argument("--backend", choices=["stub", "laya", "kev", "clm"], help="override WS_DECISION_SHADOW")
    s.add_argument("--actual", help="the real gate's outcome, if already known")
    s.add_argument("--decision-id", help=argparse.SUPPRESS)
    s.add_argument("--background", action="store_true", help="return immediately; score in a detached child")
    s.add_argument("--show", action="store_true", help="print the logged record (demo only)")
    s.set_defaults(fn=cmd_score)

    r = sub.add_parser("record-outcome", help="record what the real gate decided")
    r.add_argument("--id", required=True)
    r.add_argument("--actual", required=True)
    r.add_argument("--point", choices=["escalate", "review_verdict", "next_skill"])
    r.set_defaults(fn=cmd_record)

    rp = sub.add_parser("report", help="agreement + clm E2 escalation bar")
    rp.add_argument("--log")
    rp.set_defaults(fn=cmd_report)

    f = sub.add_parser("fetch-laya", help="download the pinned official Laya checkpoint")
    f.add_argument("--write-lock", action="store_true")
    f.add_argument("--force", action="store_true")
    f.set_defaults(fn=cmd_fetch)

    v = sub.add_parser("verify-laya", help="hash-check the cached Laya files against the lock")
    v.set_defaults(fn=cmd_verify)

    args = ap.parse_args(argv)
    return args.fn(args)
