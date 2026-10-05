"""Harness-only independent synthetic journey; never scores the frozen E2E corpus."""

import argparse
import json
import sys
from pathlib import Path

from .admission import register_synthetic
from .catalog import tree_hash
from .persistence import atomic_json
from .simulation import simulate


def run(repo, simkit, root, scenarios):
    import xarray as xr

    root = Path(root).resolve()
    root.mkdir(parents=True, exist_ok=False)
    sys.dont_write_bytecode = True
    sys.path.insert(0, str(Path(simkit).resolve()))
    from simkit.fixtures import onset_synth
    from simkit.profile import Profile

    profile = Profile.load(str(Path(simkit) / "profiles/cached-onset/profile.json"))
    fixture = root / "fixture"
    onset_synth.write(onset_synth.generate(profile, seed=20251001), str(fixture))
    converted = fixture / "synthetic.zarr"
    with xr.open_dataset(fixture / "precip.nc") as ds:
        ds.to_zarr(converted, mode="w", zarr_format=2, consolidated=True)
        with xr.open_zarr(converted, consolidated=True) as actual:
            xr.testing.assert_identical(ds, actual)
    grant = register_synthetic(converted, fixture / "manifest.json", Path(onset_synth.__file__))
    atomic_json(root / "registration.json", grant.record)
    original = tree_hash(converted)
    results = []
    for scenario in scenarios:
        result = simulate(repo, root / scenario, scenario=scenario, registration=grant)
        results.append(result)
        atomic_json(root / "journeys.json", results)
    summary = {
        "schema": "workflow.synthetic-evaluation/1",
        "scored_corpus": False,
        "source_unchanged": original == tree_hash(converted),
        "conversion_identical": True,
        "model_calls": 0,
        "roles": "fixture",
        "scientific_tools": "real",
        "results": results,
    }
    atomic_json(root / "summary.json", summary)
    return summary


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--simkit-root", type=Path, required=True)
    parser.add_argument("--run-dir", type=Path, required=True)
    parser.add_argument(
        "--scenarios",
        nargs="+",
        default=["clean", "interrupt", "tamper", "denied", "review-repair"],
        choices=["clean", "interrupt", "tamper", "denied", "review-repair"],
    )
    args = parser.parse_args()
    print(
        json.dumps(
            run(
                Path(__file__).resolve().parents[2], args.simkit_root, args.run_dir, args.scenarios
            ),
            indent=2,
        )
    )


if __name__ == "__main__":
    main()
