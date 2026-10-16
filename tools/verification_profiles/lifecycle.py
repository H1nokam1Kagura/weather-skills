"""Verification-profile dispatch for the main workflow CLI.

Diagnostic execution is explicit. Admission and live release are separate: the
current executor accepts honestly registered synthetic data and substituted roles.
"""

import argparse
import json
from pathlib import Path

from tools.workflow.contracts import Refused, digest
from tools.workflow.persistence import atomic_json

from .contracts import register_synthetic, require, validate
from .controller import Controller, SyntheticRoles


def capture(controller):
    value = {
        "request": controller.request,
        "sources": controller.grant.record,
        "binding_hash": controller.binding,
    }
    atomic_json(controller.root / "capture.json", {"payload": value, "digest": digest(value)})


def reopen(repo, root):
    root = Path(root)
    envelope = json.loads((root / "capture.json").read_text(encoding="utf-8"))
    value = envelope["payload"]
    require(digest(value) == envelope["digest"], "capture changed")
    request = validate(value["request"])
    paths = {
        name: record
        if "unavailable" in record
        else {role: source["path"] for role, source in record.items()}
        for name, record in value["sources"].items()
    }
    grant = register_synthetic(paths)
    require(grant.record == value["sources"], "captured sources changed")
    controller = Controller(repo, request, grant, root, SyntheticRoles(request))
    require(controller.binding == value["binding_hash"], "capture binding changed")
    return controller


def main(argv=None):
    from adapter_support.verification_adapter import VerificationAdapter, project

    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("profiles")
    run = sub.add_parser("run")
    run.add_argument("--request", type=Path, required=True)
    run.add_argument("--sources", type=Path, required=True)
    run.add_argument("--run-dir", type=Path, required=True)
    run.add_argument("--diagnostic", action="store_true")
    for name in ("resume", "pause", "status", "report"):
        command = sub.add_parser(name)
        command.add_argument("--run-dir", type=Path, required=True)
        if name == "resume":
            command.add_argument("--diagnostic", action="store_true")
    evaluate = sub.add_parser("eval")
    evaluate.add_argument("--run-dir", type=Path, required=True)
    evaluate.add_argument("--case", action="append")
    evaluate.add_argument("--diagnostic", action="store_true")
    args = parser.parse_args(argv)
    repo = Path(__file__).resolve().parents[2]
    adapter = VerificationAdapter(repo, diagnostic=getattr(args, "diagnostic", False))
    try:
        if args.command == "profiles":
            result = adapter.profile_status()
        elif args.command == "eval":
            from .evaluation import evaluate_cases

            result = evaluate_cases(repo, args.run_dir, args.case, diagnostic=args.diagnostic)
        elif args.command == "run":
            adapter.require_admission()
            require(not args.run_dir.exists(), "new run directory required")
            request = validate(json.loads(args.request.read_text(encoding="utf-8")))
            sources = json.loads(args.sources.read_text(encoding="utf-8"))
            grant = register_synthetic(sources)
            controller = Controller(repo, request, grant, args.run_dir, SyntheticRoles(request))
            capture(controller)
            controller.run()
            result = project(repo, controller.root)
        else:
            controller = reopen(repo, args.run_dir)
            if args.command == "pause":
                controller.pause()
                result = {
                    "status": "PAUSE_REQUESTED",
                    "binding_hash": controller.binding,
                    "boundary": "between_synchronous_operations",
                    "production_release": False,
                }
            elif args.command == "resume":
                adapter.require_admission()
                controller.run(resume=True)
                result = project(repo, controller.root)
            elif args.command == "report":
                result = project(repo, controller.root)
            else:
                result = {
                    key: controller.state[key]
                    for key in (
                        "status",
                        "profile",
                        "pending",
                        "elapsed_seconds",
                        "production_release",
                    )
                }
        print(json.dumps(result, indent=2, allow_nan=False))
        if args.command == "eval":
            if result["harness_errors"] or result["diagnostic_passes"] < result["executed"]:
                return 1
            if result["not_implemented"]:
                return 2
        return 0 if result.get("status") not in ("BLOCKED", "INTERRUPTED", "HARNESS_ERROR") else 1
    except (Refused, ValueError, OSError, KeyError) as exc:
        print(json.dumps({"status": "REFUSED", "reason": str(exc), "production_release": False}))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
