"""Read-only implementation assessment plus disposable independent-fixture probes.

Never scores cases, changes their oracles, or grants synthetic production lineage.
"""

import argparse
import hashlib
import json
import sys
from pathlib import Path

from .catalog import child_environment, tree_hash
from .e2e_contracts import admit_corpus
from .e2e_injections import unsupported_requirements
from .persistence import atomic_json
from .processes import Processes
from .runner import Controller


def inspect(repo, simkit_root, root):
    repo, simkit_root, root = map(lambda p: Path(p).resolve(), (repo, simkit_root, root))
    root.mkdir(parents=True, exist_ok=False)
    corpus_dir = repo / "evals/controller/fixtures/e2e-20261005"
    dataset = corpus_dir / "agentic-e2e-injections.DEV-ONLY.json"
    requirements = corpus_dir / "agentic-e2e-injections.ADAPTER-REQUIREMENTS.json"
    expected = {
        dataset: "9198f60612770b98e131419b4baca4489c441acf80c898746154906b4d23f054",
        requirements: "a2f49b843ca8741d1055d0c9e8f352989ea3db79e58a8252b341275928054118",
    }
    for path, sha in expected.items():
        if hashlib.sha256(path.read_bytes()).hexdigest() != sha:
            raise ValueError("Frozen input hash mismatch: " + str(path))
    admission = admit_corpus(
        dataset, requirements, repo / "evals/controller/fixtures/seeded-agentic-eval.DEV-ONLY.json"
    )
    atomic_json(root / "corpus-admission.json", admission)
    sys.dont_write_bytecode = True
    sys.path.insert(0, str(simkit_root))
    from simkit.fixtures import onset_synth
    from simkit.profile import Profile

    profile = Profile.load(str(simkit_root / "profiles/cached-onset/profile.json"))
    fixture = root / "fixture"
    manifest = onset_synth.write(onset_synth.generate(profile, seed=20251001), str(fixture))
    source = fixture / "precip.nc"
    source_hash = tree_hash(source)
    corpus = json.loads(dataset.read_text(encoding="utf-8"))
    goal = corpus["reference_fixture"]["goal"]
    probe = Controller.__new__(Controller)
    probe.admission = None
    probe.goal = goal
    probes = []
    for label, path in [("original_netcdf", source)]:
        probe.source = path
        try:
            result = probe.inspect_source()
            probes.append({"probe": label, "accepted": True, "metadata": result})
        except Exception as exc:
            probes.append(
                {
                    "probe": label,
                    "accepted": False,
                    "error_type": type(exc).__name__,
                    "error": str(exc),
                }
            )
    # Separate format compatibility from the lineage policy. Preserve every source attr.
    import xarray as xr

    converted = fixture / "honestly-labelled.zarr"
    with xr.open_dataset(source) as ds:
        ds.to_zarr(converted, mode="w", zarr_format=2, consolidated=True)
    probe.source = converted
    try:
        result = probe.inspect_source()
        probes.append(
            {"probe": "zarr_format_only_conversion", "accepted": True, "metadata": result}
        )
    except Exception as exc:
        probes.append(
            {
                "probe": "zarr_format_only_conversion",
                "accepted": False,
                "error_type": type(exc).__name__,
                "error": str(exc),
            }
        )
    atomic_json(root / "source-probes.json", probes)
    (root / "processes").mkdir()
    processes = Processes(root / "processes", total_seconds=180)
    command = [
        sys.executable,
        str(repo / "skills/onset-date/scripts/onset_date.py"),
        "--input",
        str(source),
        "--output",
        str(root / "probe-onset.zarr"),
        "--definition-ref",
        "agrhymet-sos-rolling",
        "--variable",
        "precip",
    ]
    try:
        tool = processes.run(command, root, child_environment(root / "scratch"))
    except Exception as exc:
        tool = {"exit_code": None, "error_type": type(exc).__name__, "error": str(exc)}
    atomic_json(root / "scientific-tool-probe.json", tool)
    converted_command = list(command)
    converted_command[converted_command.index("--input") + 1] = str(converted)
    converted_command[converted_command.index("--output") + 1] = str(root / "converted-onset.zarr")
    try:
        converted_tool = processes.run(converted_command, root, child_environment(root / "scratch"))
    except Exception as exc:
        converted_tool = {"exit_code": None, "error_type": type(exc).__name__, "error": str(exc)}
    atomic_json(root / "converted-tool-probe.json", converted_tool)
    blocked = not any(p["accepted"] for p in probes)
    report = {
        "schema": "workflow.e2e-admission-probe/1",
        "scored": False,
        "implementation_repo": str(repo),
        "simkit_root": str(simkit_root),
        "frozen_hashes": {str(k): v for k, v in expected.items()},
        "fixture": manifest,
        "source_probes": probes,
        "scientific_tool_exit_code": tool["exit_code"],
        "converted_scientific_tool_exit_code": converted_tool["exit_code"],
        "original_fixture_unchanged": tree_hash(source) == source_hash,
        "UC-12": {
            "blocked": blocked,
            "original_accepted": probes[0]["accepted"],
            "converted_accepted": probes[1]["accepted"],
            "lineage_relabelled": False,
            "implementation_policy": "Zarr, weather_skills_source=chirps, exactly one chirps-fetch history entry",
        },
        "UC-09": "Compiler input contains messages, schema and rules only; no source metadata. Frozen E02-P2 input patch targets an absent input channel. AMD-010/011 require human review, not automatic application.",
        "case_readiness": [
            {
                "id": c["id"],
                "status": "BLOCKED_FIXTURE_ADMISSION"
                if blocked
                else "REQUIRES_ADAPTER_AND_ORACLE_REVIEW",
                "additional_unsupported_injections": unsupported_requirements(c),
            }
            for c in corpus["cases"]
        ],
        "heldout_opened": False,
        "candidate_cases_executed": 0,
    }
    atomic_json(root / "admission-probe.json", report)
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--simkit-root", type=Path, required=True)
    parser.add_argument("--run-dir", type=Path, required=True)
    args = parser.parse_args()
    result = inspect(Path(__file__).resolve().parents[2], args.simkit_root, args.run_dir)
    print(
        json.dumps(
            {
                k: result[k]
                for k in (
                    "UC-12",
                    "UC-09",
                    "scientific_tool_exit_code",
                    "original_fixture_unchanged",
                    "candidate_cases_executed",
                )
            },
            indent=2,
        )
    )


if __name__ == "__main__":
    main()
