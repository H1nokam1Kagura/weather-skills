"""Run a new, explicitly synthetic demonstration with the actual scientific skills."""

import argparse
import json
from pathlib import Path

from . import PROFILES
from .contracts import register_synthetic
from .controller import Controller, SyntheticRoles
from .fixtures import make_sources, request_for


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--synthetic", action="store_true", required=True)
    parser.add_argument("--profile", choices=PROFILES, required=True)
    parser.add_argument(
        "--run-dir", type=Path, required=True, help="new directory for fixtures and evidence"
    )
    args = parser.parse_args()
    args.run_dir.mkdir(parents=True, exist_ok=False)
    request = request_for(args.profile)
    (args.run_dir / "request.json").write_text(json.dumps(request, indent=2), encoding="utf-8")
    grant = register_synthetic(make_sources(args.run_dir / "sources", request))
    state = Controller(
        Path(__file__).resolve().parents[2],
        request,
        grant,
        args.run_dir / "run",
        SyntheticRoles(request),
    ).run()
    print(
        json.dumps(
            {
                "status": state["status"],
                "production_release": False,
                "checkpoint": str(args.run_dir / "run/checkpoint.json"),
            },
            indent=2,
        )
    )
    return 0 if state["status"] == "SYNTHETIC_COMPLETE" else 1


if __name__ == "__main__":
    raise SystemExit(main())
