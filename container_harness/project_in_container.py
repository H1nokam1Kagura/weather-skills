"""Untraced, in-container evidence projection of a finished canary/case run (runs as root, after
the traced tree has exited, in the SAME container, so every path is in the trace's namespace).

usage: python -B project_in_container.py RUN_DIR OBSERVATIONS_JSON OUT_JSON [--inputs INPUTS_JSON]

Frozen score.py compares ``filesystem_snapshot.created_paths`` with ``artifacts.<id>.path`` by
exact string, so projection and scoring must see the container paths, not host copies. The
adapter projection is ``adapter_support.evidence_projection.project_run`` with
``{"observations_path": ...}`` plus, in case mode, the harness inputs run_case held in process
(``--inputs``: ``weather-adapter.evidence-projection-inputs/1``, written by
``WeatherAdapter.run_case`` onto the entry shell's fd 4: launch_observations, processes_root,
source_pre, authorized_goal, run_provenance, source_expected_after(_reason) and, for a case with
fixture operations, fixture_operations). Merging them is what keeps the post-trace
projection from losing the launch-time hashes, the source_pre/authorized-goal symbols and the
run provenance the in-process projection had.

Missing stays missing: an absent, unreadable or foreign inputs file is recorded
(``inputs_status``) and the projection runs from the observations alone, so those fields come
out MISSING exactly as they would without the file. An inputs file for another run directory
is refused (status ERROR), never merged. When this image's controller commit has no adapter
projection, the output says UNAVAILABLE and nothing is substituted.
"""

import json
import sys
import traceback
from pathlib import Path

INPUTS_SCHEMA = "weather-adapter.evidence-projection-inputs/1"
INPUT_KEYS = (
    "launch_observations",
    "processes_root",
    "source_pre",
    "authorized_goal",
    "run_provenance",
    # CAP-SYMBOLS HARNESS_HASH:source_expected_after and, when absent, its owner/reason.
    "source_expected_after",
    "source_expected_after_reason",
    # CAP-FIXTURE-OP report (fixture_rereview_projection.fixture_report): the delivery records
    # and HARNESS_HASH:onset_after_mutation for the scored projection (project_fixture_symbols).
    "fixture_operations",
)
# Always written by run_case, except: source_expected_after only when it is known (a case with
# an unapplied source operation leaves it to CAP-FIXTURE-OP) and fixture_operations only when
# the case schedules a fixture operation; neither is then "not carried".
OPTIONAL_INPUT_KEYS = ("source_expected_after", "fixture_operations")


class ForeignInputs(ValueError):
    """The inputs file belongs to another run; merging it would misattribute evidence."""


def load_inputs(path, run_dir):
    """-> (harness keys to merge, inputs_status record). Never raises for absence."""
    if path is None:
        return {}, {"status": "NOT_SUPPLIED"}
    status = {"path": str(path)}
    try:
        text = Path(path).read_text(encoding="utf-8")
    except OSError as exc:
        return {}, {**status, "status": "ABSENT", "reason": repr(exc)}
    if not text.strip():
        return {}, {**status, "status": "ABSENT", "reason": "empty (run_case wrote no inputs)"}
    try:
        record = json.loads(text)
    except ValueError as exc:
        return {}, {**status, "status": "UNREADABLE", "reason": repr(exc)}
    if not isinstance(record, dict) or record.get("schema") != INPUTS_SCHEMA:
        return {}, {**status, "status": "UNREADABLE", "reason": "unknown schema"}
    if Path(str(record.get("run_dir"))).resolve() != Path(run_dir).resolve():
        raise ForeignInputs(f"inputs are for run_dir {record.get('run_dir')!r}, not {run_dir!r}")
    merged = {k: record[k] for k in INPUT_KEYS if k in record}
    return merged, {
        **status,
        "status": "MERGED",
        "case_id": record.get("case_id"),
        "merged": sorted(merged),
        "not_carried": sorted(set(INPUT_KEYS) - set(merged) - set(OPTIONAL_INPUT_KEYS)),
    }


def main(argv):
    args = list(argv[1:])
    inputs = None
    if "--inputs" in args:
        at = args.index("--inputs")
        inputs = args[at + 1]
        del args[at : at + 2]
    run_dir, observations, out = args[0], args[1], args[2]
    record = {
        "schema": "simkit.harness-projection/1",
        "run_dir": run_dir,
        "observations_path": observations,
    }
    try:
        from adapter_support.evidence_projection import project_run
    except ImportError as exc:
        record.update(status="UNAVAILABLE", reason=f"adapter projection not in this commit: {exc}")
    else:
        try:
            harness, record["inputs_status"] = load_inputs(inputs, run_dir)
            harness["observations_path"] = observations
            record.update(status="OK", projection=project_run(run_dir, harness))
        except Exception as exc:  # recorded, never papered over
            record.update(status="ERROR", error=repr(exc), traceback=traceback.format_exc()[-4000:])
    with open(out, "w", encoding="utf-8") as stream:
        json.dump(record, stream, indent=1, default=str)
        stream.write("\n")
    print(
        json.dumps(
            {
                **{k: record.get(k) for k in ("status", "reason", "error")},
                "inputs": (record.get("inputs_status") or {}).get("status"),
            }
        )
    )
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
