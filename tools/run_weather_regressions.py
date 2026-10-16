"""Run the synthetic weather tool regression suite and retain timing evidence."""

import argparse
import hashlib
import importlib.metadata
import json
import platform
import subprocess
import sys
import time
import xml.etree.ElementTree as ET
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SUITES = [
    "skills/dynamical-fetch/tests",
    "skills/aggregate-temporal/tests",
    "skills/convert-to-totals/tests",
    "skills/verify/tests",
]
RECOVERY_SUITE = "tools/workflow/tests/test_recovery.py"


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--output-dir",
        type=Path,
        required=True,
        help="New directory for XML, log, source hashes and timing summary.",
    )
    args = parser.parse_args()
    output = args.output_dir.resolve()
    output.mkdir(parents=True, exist_ok=False)
    xml_path = output / "tests.xml"
    suites = list(SUITES)
    unavailable = []
    if (ROOT / RECOVERY_SUITE).is_file():
        suites.append(RECOVERY_SUITE)
    else:
        unavailable.append(RECOVERY_SUITE)
    command = [
        sys.executable,
        "-m",
        "pytest",
        *suites,
        "-q",
        "-p",
        "no:cacheprovider",
        "--durations=10",
        f"--junitxml={xml_path}",
        f"--basetemp={output / 'tmp'}",
    ]
    paths = {
        Path(__file__),
        ROOT / "tests/conftest.py",
        ROOT / "pyproject.toml",
        ROOT / "agents/forecaster.md",
    }
    for directory in ("dynamical-fetch", "aggregate-temporal", "convert-to-totals", "verify"):
        paths.update((ROOT / "skills" / directory).rglob("*.py"))
    paths.update((ROOT / "tools/workflow").rglob("*.py"))
    hashes = {
        str(p.relative_to(ROOT)).replace("\\", "/"): hashlib.sha256(p.read_bytes()).hexdigest()
        for p in sorted(paths)
    }
    versions = {}
    for name in ("pytest", "xarray", "zarr", "numpy", "weather-skills-core", "dask", "xskillscore"):
        try:
            versions[name] = importlib.metadata.version(name)
        except importlib.metadata.PackageNotFoundError:
            versions[name] = None
    start = time.perf_counter()
    with (output / "pytest.log").open("w", encoding="utf-8") as log:
        result = subprocess.run(
            command, cwd=ROOT, stdout=log, stderr=subprocess.STDOUT, check=False
        )
    cases = []
    if xml_path.exists():
        for test in ET.parse(xml_path).findall(".//testcase"):
            skipped = test.find("skipped")
            status = (
                "skipped"
                if skipped is not None
                else "failed"
                if test.find("failure") is not None or test.find("error") is not None
                else "passed"
            )
            cases.append(
                {
                    "name": test.get("classname", "") + "." + test.get("name", ""),
                    "seconds": float(test.get("time", "0")),
                    "status": status,
                    "skip_reason": skipped.get("message") if skipped is not None else None,
                }
            )
    report = {
        "kind": "synthetic_component_and_pipeline_regression",
        "command": command,
        "exit_code": result.returncode,
        "unavailable_suites": unavailable,
        "wall_seconds": time.perf_counter() - start,
        "python": platform.python_version(),
        "packages": versions,
        "source_sha256": hashes,
        "counts": {
            s: sum(c["status"] == s for c in cases) for s in ("passed", "failed", "skipped")
        },
        "slowest_tests": sorted(cases, key=lambda c: c["seconds"], reverse=True)[:10],
        "skipped": [c for c in cases if c["status"] == "skipped"],
        "limits": [
            "Synthetic data; catalog network is mocked.",
            "Recovery uses fake roles/processes, not a hosted agent.",
            "Timings are local test costs, not archive download benchmarks.",
        ],
    }
    (output / "summary.json").write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    print(
        json.dumps(
            {
                "exit_code": result.returncode,
                "counts": report["counts"],
                "report": str(output / "summary.json"),
            }
        )
    )
    return result.returncode


if __name__ == "__main__":
    raise SystemExit(main())
