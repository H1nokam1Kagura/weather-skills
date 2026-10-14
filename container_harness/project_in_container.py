"""Untraced, in-container evidence projection of a finished canary run (runs as root, after the
traced tree has exited, in the SAME container, so every path is in the trace's namespace).

usage: python -B project_in_container.py RUN_DIR OBSERVATIONS_JSON OUT_JSON

Frozen score.py compares ``filesystem_snapshot.created_paths`` with ``artifacts.<id>.path`` by
exact string, so projection and scoring must see the container paths, not host copies. The
adapter projection (``adapter_support.evidence_projection.project_run`` with
``{"observations_path": ...}``) is produced by the adapter track; when this image's controller
commit does not carry it yet, the output says UNAVAILABLE and nothing is substituted.
"""

import json
import sys
import traceback


def main(argv):
    run_dir, observations, out = argv[1], argv[2], argv[3]
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
            record.update(
                status="OK", projection=project_run(run_dir, {"observations_path": observations})
            )
        except Exception as exc:  # recorded, never papered over
            record.update(status="ERROR", error=repr(exc), traceback=traceback.format_exc()[-4000:])
    with open(out, "w", encoding="utf-8") as stream:
        json.dump(record, stream, indent=1, default=str)
        stream.write("\n")
    print(json.dumps({k: record.get(k) for k in ("status", "reason", "error")}))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
