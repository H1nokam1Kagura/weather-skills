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

Stop-and-resume cases (harness_stop_controller, Wave 7): run_case also writes ``resume_route``,
the harness route record of adapter_support/resume_driver.py (``weather-adapter.resume-route/1``:
root_pid, incarnations, kill, stop, onset_partial, ...). It is merged like the other inputs, so
the scored projection carries harness_observations.controller_incarnations, onset_partial (or
its MISSING entry), the harness-observed output_bytes of a killed process and the incarnation
cross-check of symbols.HARNESS_CONTROLLER_PID (adapter_support/resume_projection.py).

Kill facts (``broker_kill_facts``): when the route record says the kill went through the
interrupt broker (``kill.route == "interrupt_broker"``), the facts it carries are the broker's
ack as relayed through the traced root. The observed source is the broker's own record as the
analyzer read it and checked against the trace (evidence key ``harness_interrupt_broker``,
analyze_trace.apply_interrupt_broker). The scored projection uses that stop's trace-confirmed
kills and their byte counts; the relayed ack is kept beside them (``ack_facts``) and never
used for a count. When the analyzer cannot confirm the stop (section absent or UNAVAILABLE,
no single matching stop, stop not trace_confirmed) the byte counts are withheld: those
receipts stay MISSING with that reason, never filled. The analyzer section is copied into the
scored projection (``harness_interrupt_broker``, beside ``evidence``: it is no contract field)
and the choice is disclosed in ``notes`` and in this record's ``kill_facts``. Any other kill
route (in_process: the harness parent's own /proc observation) is used as recorded.

Absent route record on a stop case (the run has several controller incarnations, or the
analyzer saw the broker engaged): nothing is filled. harness_observations.controller_incarnations
and harness_observations.onset_partial are listed MISSING with the reason, and an analyzer
HARNESS_CONTROLLER_PID that the incarnation check could not test is withheld (MISSING).

Missing stays missing: an absent, unreadable or foreign inputs file is recorded
(``inputs_status``) and the projection runs from the observations alone, so those fields come
out MISSING exactly as they would without the file. An inputs file for another run directory
is refused (status ERROR), never merged. When this image's controller commit has no adapter
projection, the output says UNAVAILABLE and nothing is substituted.
"""

import copy
import json
import sys
import traceback
from pathlib import Path

INPUTS_SCHEMA = "weather-adapter.evidence-projection-inputs/1"
ROUTE_KEY = "resume_route"  # resume_projection.ROUTE_SCHEMA record (resume_driver.drive)
BROKER_EVIDENCE_KEY = "harness_interrupt_broker"  # analyze_trace.BROKER_EVIDENCE_KEY
BROKER_KILL_ROUTE = "interrupt_broker"  # resume_driver.ForkedIncarnation.kill
BROKER_SOURCE = (
    "harness_interrupt_broker: the broker's own record ($OUT/interrupt_broker.json, root, "
    "untraced) as analyze_trace read it, stop and kills confirmed by the trace"
)
ROUTE_ABSENT_REASON = (
    "the harness resume-route record (run_case resume_route) was not carried into the "
    "in-container projection"
)
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
    # CAP-INTERRUPT / CAP-MIDPROCESS: the harness stop-and-resume route record
    # (resume_projection.route): controller incarnations, kill, stop, onset_partial.
    ROUTE_KEY,
)
# Always written by run_case, except: source_expected_after only when it is known (a case with
# an unapplied source operation leaves it to CAP-FIXTURE-OP), fixture_operations only when the
# case schedules a fixture operation and resume_route only for a harness_stop_controller case;
# none is then "not carried".
OPTIONAL_INPUT_KEYS = ("source_expected_after", "fixture_operations", ROUTE_KEY)


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


def _read_observations(path):
    try:
        record = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError):
        return None
    return record if isinstance(record, dict) else None


def _broker_stop(section, kill):
    """-> (the analyzer's stop for this kill, None) or (None, reason)."""
    if not isinstance(section, dict):
        return None, f"the analyzer carried no {BROKER_EVIDENCE_KEY} section"
    if section.get("status") not in ("OK", "UNCONFIRMED"):
        return None, (
            f"analyzer {BROKER_EVIDENCE_KEY} status {section.get('status')}: "
            f"{section.get('reason') or 'no confirmed stop'}"
        )
    stops = [s for s in section.get("stops") or [] if isinstance(s, dict)]
    index = kill.get("stop_index")
    if isinstance(index, int) and not isinstance(index, bool) and 0 <= index < len(stops):
        candidates = [stops[index]] if stops[index].get("pid") == kill.get("pid") else []
    else:
        candidates = [s for s in stops if s.get("pid") == kill.get("pid")]
    if len(candidates) != 1:
        return None, (
            f"the broker record holds {len(candidates)} matching stops for pid "
            f"{kill.get('pid')} (stop_index {index!r})"
        )
    stop = candidates[0]
    if stop.get("trace_confirmed") is not True:
        return None, f"the broker's stop of pid {kill.get('pid')} is not confirmed by the trace"
    return stop, None


def broker_kill_facts(route, observations):
    """-> (route record for the projection, kill_facts disclosure). Never fills a value.

    A broker-route kill takes its killed pids and byte counts from the analyzer's
    trace-confirmed broker stop. Unconfirmed, the byte counts are withheld: ``withheld`` lists
    the killed pids whose receipts stay MISSING with ``reason`` (apply_route_disclosures)."""
    kill = route.get("kill") if isinstance(route, dict) else None
    if not isinstance(kill, dict):
        return route, {"source": "resume_route", "status": "NO_KILL_RECORD"}
    if kill.get("route") != BROKER_KILL_ROUTE:
        return route, {
            "source": f"resume_route.kill (route {kill.get('route')}: the harness parent's own "
            "observation)",
            "status": "AS_RECORDED",
        }
    stop, reason = _broker_stop((observations or {}).get(BROKER_EVIDENCE_KEY), kill)
    route = copy.deepcopy(route)
    out = route["kill"]
    relayed = {k: out.get(k) for k in ("ok", "verified", "descendants", "killed", "output_bytes")}
    out["ack_facts"] = relayed  # disclosed beside the facts, never used for a count
    if stop is None:
        out["output_bytes"] = {}
        out["facts_source"] = "withheld: " + reason
        return route, {
            "source": BROKER_SOURCE,
            "status": "UNCONFIRMED",
            "reason": reason,
            "withheld": sorted(p for p in relayed["killed"] or [] if isinstance(p, int)),
        }
    confirmed = sorted(p for p in stop.get("trace_confirmed_kills") or [] if isinstance(p, int))
    observed = stop.get("output_bytes") if isinstance(stop.get("output_bytes"), dict) else {}
    out.update(
        ok=stop.get("ok"),
        verified=stop.get("verified"),
        descendants=list(stop.get("descendants") or []),
        killed=confirmed,
        output_bytes={
            str(p): {n: observed[str(p)].get(n) for n in ("stdout", "stderr")}
            for p in confirmed
            if isinstance(observed.get(str(p)), dict)
        },
        facts_source=BROKER_SOURCE,
    )
    unconfirmed = sorted(
        {p for p in (stop.get("killed") or []) + (relayed["killed"] or []) if isinstance(p, int)}
        - set(confirmed)
    )
    facts = {
        "source": BROKER_SOURCE,
        "status": "CONFIRMED",
        "stop_request_id": stop.get("request_id"),
        "killed": confirmed,
        "unconfirmed_kills": unconfirmed,
        "ack_differs": sorted(k for k in ("killed", "output_bytes") if relayed[k] != out[k]),
    }
    if unconfirmed:
        facts["withheld"] = unconfirmed
        facts["reason"] = f"the trace does not confirm the broker's kill of pids {unconfirmed}"
    return route, facts


def _gap(projection, path, capability, reason):
    """Replace any gap on ``path`` with this one (the projection's own list)."""
    projection["missing"] = [m for m in projection["missing"] if m.get("path") != path]
    projection["missing"].append({"path": path, "capability": capability, "reason": reason})


def apply_route_disclosures(projection, harness, observations, kill_facts):
    """After project_run: disclose the kill-fact source (and copy the analyzer's broker
    section); list withheld byte counts, and on a stop case without a route record its fields,
    MISSING with the reason. Never fills a value."""
    section = (observations or {}).get(BROKER_EVIDENCE_KEY)
    if kill_facts is not None:
        evidence = projection["evidence"]
        projection["notes"].append({"interrupt_broker_kill_facts": kill_facts})
        if kill_facts["source"] == BROKER_SOURCE:
            projection[BROKER_EVIDENCE_KEY] = section
        for receipt in evidence.get("process_receipts") or []:
            if receipt.get("pid") in kill_facts.get("withheld", ()) and receipt.get("end") is None:
                _gap(
                    projection,
                    f"process_receipts[serial={receipt['serial']}].output_bytes",
                    "CAP-MIDPROCESS",
                    "killed by the harness interrupt broker; byte counts withheld because "
                    + kill_facts["reason"]
                    + " (never 0)",
                )
        return
    if ROUTE_KEY in harness:
        return
    unattributed = any(
        "controller_incarnations_unattributed" in n for n in projection.get("notes") or []
    )
    if not unattributed and not isinstance(section, dict):
        return  # not a stop case: nothing here applies
    evidence = projection["evidence"]
    projection["notes"].append(
        {
            "resume_route_absent": {
                "reason": ROUTE_ABSENT_REASON,
                "detected_by": "several controller incarnations"
                if unattributed
                else f"the analyzer recorded {BROKER_EVIDENCE_KEY}",
            }
        }
    )
    if isinstance(section, dict):
        projection[BROKER_EVIDENCE_KEY] = section
    observed = evidence.get("harness_observations") or {}
    for field, capability in (
        ("controller_incarnations", "CAP-INTERRUPT"),
        ("onset_partial", "CAP-MIDPROCESS"),
    ):
        if field not in observed:
            _gap(projection, f"harness_observations.{field}", capability, ROUTE_ABSENT_REASON)
    symbols = evidence.get("symbols") or {}
    if unattributed and "HARNESS_CONTROLLER_PID" in symbols:
        del symbols["HARNESS_CONTROLLER_PID"]
        _gap(
            projection,
            "symbols.HARNESS_CONTROLLER_PID",
            "CAP-SYMBOLS",
            "withheld: several controller incarnations and "
            + ROUTE_ABSENT_REASON
            + ", so the incarnation cross-check (OI-05) could not run",
        )


def project(run_dir, observations, inputs, record):
    """Fill ``record`` with inputs_status, kill_facts (route record carried) and projection,
    each as soon as it is known (an error later keeps what was recorded)."""
    from adapter_support.evidence_projection import project_run

    harness, record["inputs_status"] = load_inputs(inputs, run_dir)
    harness["observations_path"] = observations
    analyzed = _read_observations(observations)
    kill_facts = None
    if ROUTE_KEY in harness:
        harness[ROUTE_KEY], kill_facts = broker_kill_facts(harness[ROUTE_KEY], analyzed)
        record["kill_facts"] = kill_facts
    projection = project_run(run_dir, harness)
    apply_route_disclosures(projection, harness, analyzed, kill_facts)
    record["projection"] = projection
    return record


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
        from adapter_support.evidence_projection import project_run  # noqa: F401
    except ImportError as exc:
        record.update(status="UNAVAILABLE", reason=f"adapter projection not in this commit: {exc}")
    else:
        try:
            project(run_dir, observations, inputs, record)
            record["status"] = "OK"
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
                "kill_facts": (record.get("kill_facts") or {}).get("status"),
            }
        )
    )
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
