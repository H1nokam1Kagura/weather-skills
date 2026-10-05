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
    else:
        saved = read_checkpoint(root)
        budget, duration, elapsed = saved["budget"], saved["total_seconds"], saved["elapsed_s"]
        if elapsed >= duration or saved["spent"] >= budget:
            raise Refused("original run budget exhausted; resume cannot reset limits")
    with run_lock(root):
        processes = Processes(root / "processes", total_seconds=duration)
        processes.deadline -= elapsed
        roles = Roles(processes, root / "role-context", budget=budget)
        controller = (
            Controller.resume(root, processes, roles)
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
            )
        )
        try:
            result = controller.run()
        except Exception as exc:
            print(json.dumps({"status": "INCOMPLETE", "error": str(exc), "run_dir": str(root)}))
            raise SystemExit(1) from exc
        print(json.dumps(result, indent=2))


if __name__ == "__main__":
    main()
