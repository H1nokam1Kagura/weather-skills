"""Untraced pre-run staging inside the container (runs as uid 1000, before the tracer starts).

usage: python /opt/ctl/container_harness/stage_case.py /work/case

1. Re-verifies the pinned generator bytes (supplement cached-onset-e2e-v2_7-inputs/1).
2. Generates the synthetic CHIRPS-identical fixture with the pinned generator
   (seed 20251001; precip.nc in a data dir, manifest + known answers in a separate harness dir).
3. Stages it to Zarr v2 with the UC-12 presentation (adapter_support.fixture_staging), in a
   source dir that holds nothing else.
4. Writes the operator request (tools.workflow.evaluation.fixture_request: the cached-onset/v1
   reference goal, which equals the profile's reference_fixture.goal).

Nothing here is part of the traced controller tree; its writes are in the pre-run baseline.
"""

import hashlib
import json
import re
import sys
from pathlib import Path

CTL = Path("/opt/ctl")
SIMKIT = Path("/opt/simkit")
SEED = 20251001


def sha256(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def main(case):
    case = Path(case)
    sys.dont_write_bytecode = True
    sys.path[:0] = [str(CTL), str(SIMKIT)]
    spec = json.loads(Path("/opt/inputs.json").read_text(encoding="utf-8"))
    pins = {entry["role"]: entry for entry in spec["files"]}
    generator = Path(pins["fixture_generator"]["path"])
    if sha256(generator) != pins["fixture_generator"]["sha256"]:
        raise SystemExit("pinned generator sha256 mismatch at staging")

    from simkit.fixtures import onset_synth
    from simkit.profile import Profile

    if Path(onset_synth.__file__).resolve() != generator.resolve():
        raise SystemExit("imported generator is not the pinned file")
    profile = Profile.load(pins["profile"]["path"])
    case.mkdir(parents=True, exist_ok=False)
    fixture = onset_synth.write(
        onset_synth.generate(profile, seed=SEED),
        str(case / "fixture-nc"),
        str(case / "fixture-harness"),
    )

    from adapter_support.fixture_staging import stage_fixture
    from tools.workflow.contracts import request_goal
    from tools.workflow.evaluation import fixture_request

    skill = (CTL / "skills/chirps-fetch/SKILL.md").read_text(encoding="utf-8")
    version = re.search(r'^\s*version:\s*"([^"]+)"', skill, re.M).group(1)
    (case / "source").mkdir()
    staged = stage_fixture(
        case / "fixture-nc" / "precip.nc",
        case / "source" / "source.zarr",
        case / "harness",
        generator_provenance={
            "synthetic": True,
            "generator": "simkit.fixtures.onset_synth",
            "generator_sha256": pins["fixture_generator"]["sha256"],
            "supplement_id": spec["supplement_id"],
            "seed": SEED,
            "content_sha256": fixture["content_sha256"],
        },
        presented_fetch_version=version,
    )
    request = fixture_request()
    goal = request_goal(request)
    if goal != profile.reference_fixture["goal"]:
        raise SystemExit("fixture request goal differs from the profile reference goal")
    (case / "request.json").write_text(json.dumps(request, indent=1), encoding="utf-8")
    record = {
        "schema": "simkit.harness-stage/1",
        "supplement_id": spec["supplement_id"],
        "generator_sha256": pins["fixture_generator"]["sha256"],
        "profile_sha256": pins["profile"]["sha256"],
        "seed": SEED,
        "fixture_content_sha256": fixture["content_sha256"],
        "presented_fetch_version": version,
        "source": str(case / "source" / "source.zarr"),
        "staged_output_sha256": staged["output"]["sha256"],
        "request": str(case / "request.json"),
        "run_dir": str(case / "run"),
        "goal": goal,
    }
    print(json.dumps(record, indent=1))
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1]))
