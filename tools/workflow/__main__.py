"""Workflow lifecycle commands. The legacy flag-only invocation remains supported."""

import argparse
import json
import sys
from pathlib import Path

from .agents import Roles
from .catalog import build_manifest, safe_path
from .contracts import Refused, request_goal
from .persistence import read_checkpoint, run_lock
from .processes import Processes, free_gib
from .profiles import PROFILES
from .reporting import report
from .runner import Controller


def main():
    parser = argparse.ArgumentParser(description="Controlled scientific workflow lifecycle")
    commands = parser.add_subparsers(dest="command", required=True)
    run = commands.add_parser("run", help="Start a fresh reviewed workflow")
    run.add_argument("--request", type=Path, required=True)
    run.add_argument("--source", type=Path, required=True)
    run.add_argument("--run-dir", type=Path, required=True)
    run.add_argument("--budget-usd", type=float, default=5)
    run.add_argument("--timeout", type=int, default=1800)
    run.add_argument("--repair-limit", type=int, choices=[0, 1, 2], default=1)
    run.add_argument("--policy", choices=["rules", "shadow"], default="rules")
    # D5 CP-A1: the two evidence modes are declared run configuration, not constructor-only.
    # Both persist in the checkpoint, so resume inherits them and takes no flag.
    run.add_argument(
        "--dispatch-evidence",
        action="store_true",
        help="Record controller-sequenced dispatch, process and terminal evidence",
    )
    run.add_argument(
        "--review-evidence",
        action="store_true",
        help="Bind review to versioned plan claims with cited documentation sources",
    )
    # D16 CP-F1: opt-in live roles through the Kong AI Gateway (synthetic data only).
    run.add_argument("--roles", choices=["cli", "kong"], default="cli")
    run.add_argument(
        "--role-model",
        action="append",
        default=[],
        metavar="ROLE=MODEL",
        help="Kong only: per-role model id from the evidenced native roster",
    )
    run.add_argument("--kong-max-tokens", type=int, default=4096)
    run.add_argument(
        "--kong-data-policy-ack",
        action="store_true",
        help="Kong only: confirm every payload is synthetic, Not Sensitive data",
    )
    for name in ("resume", "report", "status"):
        sub = commands.add_parser(name)
        sub.add_argument("--run-dir", type=Path, required=True)
    commands.add_parser("doctor", help="Read-only environment admission checks")
    commands.add_parser("profiles", help="List registered scientific workflows")
    evaluate = commands.add_parser("eval", help="Execute development fault injections")
    evaluate.add_argument("--cases", type=Path, required=True)
    evaluate.add_argument("--run-dir", type=Path, required=True)
    evaluate.add_argument("--model-budget-usd", type=float, default=0)
    simulation = commands.add_parser(
        "simulate", help="Run synthetic weather with lifecycle fault injection"
    )
    simulation.add_argument("--run-dir", type=Path, required=True)
    simulation.add_argument(
        "--scenario",
        choices=["clean", "interrupt", "tamper", "denied", "review-repair"],
        default="clean",
    )
    simulation.add_argument("--task", choices=["onset_map", "onset_data"], default="onset_map")
    simulation.add_argument("--model-budget-usd", type=float, default=0)
    argv = sys.argv[1:]
    if argv and argv[0].startswith("--") and argv[0] not in {"--help", "-h"}:
        argv = ["run", *argv]
    args = parser.parse_args(argv)
    repo = Path(__file__).resolve().parents[2]
    if args.command == "simulate":
        from .simulation import simulate

        print(
            json.dumps(
                simulate(repo, args.run_dir, args.scenario, args.task, args.model_budget_usd),
                indent=2,
            )
        )
        return
    if args.command == "profiles":
        print(json.dumps(PROFILES, indent=2))
        return
    if args.command == "doctor":
        import shutil

        manifest = build_manifest(repo)
        print(
            json.dumps(
                {
                    "runtime": manifest["@runtime"],
                    "claude_cli": shutil.which("claude"),
                    "available_ram_gib": free_gib(),
                    "build_file_count": len(manifest),
                },
                indent=2,
            )
        )
        return
    if args.command == "eval":
        from .evaluation import evaluate

        summary = evaluate(repo, args.cases, args.run_dir, args.model_budget_usd)
        print(json.dumps(summary, indent=2))
        return
    root = safe_path(args.run_dir)
    if args.command == "status":
        value = read_checkpoint(root)
        print(
            json.dumps(
                {
                    k: value[k]
                    for k in ("state", "profile", "pending", "spent", "repairs_used", "resumes")
                },
                indent=2,
            )
        )
        return
    if args.command == "report":
        print(json.dumps(report(root), indent=2))
        return
    if args.command == "run":
        if not 0 < args.budget_usd <= 5 or not 1 <= args.timeout <= 1800:
            parser.error("maximum $5 and 1800 seconds per run")
        request = json.loads(args.request.read_text(encoding="utf-8"))
        request_goal(request)
        root.mkdir(parents=True, exist_ok=False)
        for name in ("processes", "role-context", "scratch"):
            (root / name).mkdir()
        budget, duration, elapsed = args.budget_usd, args.timeout, 0
        transport = kong_transport(parser, args)
    with run_lock(root):
        if args.command == "resume":
            # Read under the lock (W2 review __main__.py:127): limits come from the checkpoint
            # this resume owns. An exhausted budget is refused by Controller.resume, which
            # records the REFUSED decision, the terminal and result.json (gate budget,
            # reason resume_refused) instead of escaping here with no terminal.
            saved = read_checkpoint(root)
            budget, duration, elapsed = saved["budget"], saved["total_seconds"], saved["elapsed_s"]
            transport = recorded_transport(root)
        processes = Processes(root / "processes", total_seconds=duration)
        processes.deadline -= elapsed
        roles = (
            Roles(processes, root / "role-context", budget=budget)
            if transport is None
            else kong_roles(transport, processes, root / "role-context", budget)
        )
        controller = (
            resume_or_exit(root, processes, roles)
            if args.command == "resume"
            else Controller(
                repo,
                request,
                args.source,
                root,
                processes,
                roles,
                max_repairs=args.repair_limit,
                policy_mode=args.policy,
                review_evidence_mode=args.review_evidence,
                dispatch_evidence_mode=args.dispatch_evidence,
            )
        )
        if transport is not None and args.command == "run":
            record_transport(controller, roles)
        try:
            result = controller.run()
        except Exception as exc:
            terminal = controller.context.get("terminal")
            print(
                json.dumps(
                    {
                        "status": "INCOMPLETE",
                        "error": str(exc),
                        "run_dir": str(root),
                        "terminal": terminal,
                    }
                )
            )
            raise SystemExit(1) from exc
        print(json.dumps(result, indent=2))
        if result["terminal"]["outcome"] == "NEEDS_INFO":
            raise SystemExit(2)  # an explicit stop awaiting a human answer, not a release


def resume_or_exit(root, processes, roles):
    """Controller.resume; a refusal prints a status line and exits 1 instead of escaping.

    The terminal printed is the one THIS refusal recorded in result.json (Controller.resume
    writes it for every verified refusal); an older result.json is never reported as it.
    """
    path = root / "result.json"
    before = path.stat().st_mtime_ns if path.exists() else None
    try:
        return Controller.resume(root, processes, roles)
    except Refused as exc:
        after = path.stat().st_mtime_ns if path.exists() else None
        terminal = (
            json.loads(path.read_text(encoding="utf-8")).get("terminal")
            if after not in (None, before)
            else None
        )
        print(
            json.dumps(
                {
                    "status": "INCOMPLETE",
                    "error": str(exc),
                    "run_dir": str(root),
                    "terminal": terminal,
                }
            )
        )
        raise SystemExit(1) from exc


TRANSPORT_RECORD = "role-transport.json"


def kong_transport(parser, args):
    """Validated Kong configuration for a fresh run, or None for the default CLI roles."""
    if args.roles != "kong":
        if args.role_model or args.kong_data_policy_ack:
            parser.error("--role-model and --kong-data-policy-ack require --roles kong")
        return None
    if not args.kong_data_policy_ack:
        parser.error("--roles kong requires --kong-data-policy-ack (synthetic data only)")
    from .kong_roles import role_models

    overrides = {}
    for item in args.role_model:
        role, sep, model = item.partition("=")
        if not sep or not role or not model or role in overrides:
            parser.error(f"--role-model expects a unique ROLE=MODEL, got {item!r}")
        overrides[role] = model
    try:
        models = role_models(overrides)
    except ValueError as exc:
        parser.error(str(exc))
    if not 1 <= args.kong_max_tokens <= 64000:
        parser.error("--kong-max-tokens must be in 1..64000")
    # Spend is untracked by owner ruling: the model budget guard is configured off, and
    # that choice is recorded in run provenance (role-transport.json).
    return {"models": models, "max_tokens": args.kong_max_tokens, "budget_guard": "off"}


def kong_roles(transport, processes, workspace, budget):
    from .kong_roles import KongRoles

    return KongRoles(
        processes,
        workspace,
        budget=budget,
        max_tokens=transport["max_tokens"],
        models=transport["models"],
        budget_guard=transport["budget_guard"],
        prices={},
        data_policy_ack=True,  # given at run start and recorded in role-transport.json
    )


def record_transport(controller, roles):
    """Run provenance: one controller-owned, integrity-hashed record of the live transport."""
    provenance = roles.provenance()
    controller.save(TRANSPORT_RECORD, provenance)
    controller.dispatch_event("role_transport", role_transport=provenance)


def recorded_transport(root):
    """Resume inherits the run's transport; a run without the record used the CLI roles."""
    path = root / TRANSPORT_RECORD
    if not path.exists():
        return None
    value = json.loads(path.read_text(encoding="utf-8"))
    if value.get("schema") != "workflow.role-transport/1" or value.get("transport") != (
        "kong-native-anthropic"
    ):
        raise Refused("recorded role transport is not resumable", gate="resume_verification")
    return {
        "models": value["models"],
        "max_tokens": value["max_tokens"],
        "budget_guard": value["budget_guard"],
    }


if __name__ == "__main__":
    main()
