"""Projection of the interrupt and resume family (Wave 7, sub-wave C; E10/E11/E14).

evidence_projection.Projection calls these helpers at five points; everything here reads only
what the controller recorded (the checkpoint, its recovery ledger, the dispatch events) or what
the harness observed (``harness["resume_route"]``, written by adapter_support/resume_driver.py).
Nothing is synthesized: a field without an observation stays MISSING (WAVE3-BRIEF rule 1).

Mapping choices, each disclosed in ``notes`` when it is applied (D4 records):

* ``budget_ledger.at_checkpoint`` (EC:budget_ledger, "at_checkpoint {same counters, captured at
  the durable checkpoint} when a checkpoint exists" plus "a snapshot of limits") is the snapshot
  the controller took of the checkpoint consumed by ``controller_trace.resume[0]``
  (``checkpoint_counters``, tools/workflow/recovery.py). A run can hold several interruption
  checkpoints; the contract key is singular, so the first resume's is the one projected.
  Field mapping: AT_CHECKPOINT_MAPPING (the controller's own documented mapping).
* ``symbols.HARNESS_CONTROLLER_PID`` with several controller incarnations (OI-05): the traced
  root, the parent of the forked incarnation 1 and itself incarnation 2. Accepted only when the
  controller's own incarnation pids (initial_state ``controller_pid`` plus every resume
  decision's ``controller_pid``) are exactly the harness-declared ones, each the root or a
  direct child of it, and every launch parent is one of them.
* ``process_receipts[*].output_bytes`` of a process the harness killed (OI-04): the byte count
  the harness observed (st_size of the regular file on fd 1 / fd 2, read while the tree was
  frozen, before SIGKILL), labelled ``output_bytes_source: harness_observed_before_kill``.
  Unobserved stays MISSING, never 0.
* ``harness_observations.onset_partial.present_at_interrupt`` and
  ``symbols.HARNESS_HASH:onset_partial_at_interrupt``: the harness parent's observation of the
  onset artifact after the kill and before anything else ran (mid-process stop only).
"""

from adapter_support.evidence_projection import EvidenceMappingError, _is_count

ROUTE_SCHEMA = "weather-adapter.resume-route/1"
ONSET_PARTIAL_SYMBOL = "HARNESS_HASH:onset_partial_at_interrupt"
OUTPUT_BYTES_SOURCE = "harness_observed_before_kill"
# recovery.checkpoint_counters native name -> EC:budget_ledger.at_checkpoint field.
AT_CHECKPOINT_MAPPING = {
    "consumed_model_usd": ("spent",),
    "elapsed_seconds": ("elapsed_s",),
    "repair_attempts_used": ("repairs_used",),
    "resume_attempts_used": ("recovery_counters", "resume_attempts_used"),
    "automatic_resumes_used": ("recovery_counters", "automatic_resumes_used"),
}
AT_CHECKPOINT_LIMITS = {
    "max_model_usd": ("budget",),
    "max_total_seconds": ("total_seconds",),
    "max_repair_attempts": ("max_repairs",),
    "max_resume_attempts": ("recovery_limits", "max_automatic_resumes"),
}
AT_CHECKPOINT_BASIS = (
    "the checkpoint consumed by controller_trace.resume[0] (its checkpoint_counters snapshot, "
    "taken by the controller before the resume wrote anything)"
)
CONTROLLER_PID_BASIS = (
    "OI-05: HARNESS_CONTROLLER_PID is the traced root, the parent of incarnation 1 (a fork) "
    "and itself incarnation 2"
)


def _number(value):
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def _read(counters, path):
    value = counters
    for key in path:
        if not isinstance(value, dict) or key not in value:
            return None
        value = value[key]
    return value


def route(projection):
    """The harness's stop-and-resume record, or None (a run without the forked route)."""
    record = projection.h.get("resume_route")
    if record is None:
        return None
    if not isinstance(record, dict) or record.get("schema") != ROUTE_SCHEMA:
        raise EvidenceMappingError("harness resume-route record has an unknown schema")
    return record


# ------------------------------------------------------------------ budget_ledger.at_checkpoint


def at_checkpoint(projection):
    """-> (at_checkpoint dict, None) or (None, reason)."""
    resume, reason = projection.recovery_list("resume")
    if resume is None:
        return None, reason
    if not resume:
        return None, "no resume decision consumed an interruption checkpoint"
    counters = resume[0].get("checkpoint_counters") if isinstance(resume[0], dict) else None
    if not isinstance(counters, dict):
        return None, "controller_trace.resume[0] carries no checkpoint_counters snapshot"
    out, limits = {}, {}
    for field, path in AT_CHECKPOINT_MAPPING.items():
        value = _read(counters, path)
        if value is None:
            continue  # not in the snapshot: left out, never filled
        if not _number(value) or value < 0:
            raise EvidenceMappingError(f"checkpoint_counters {'.'.join(path)} is not a count")
        out[field] = value
    for field, path in AT_CHECKPOINT_LIMITS.items():
        value = _read(counters, path)
        if value is None:
            continue
        if not _number(value) or value < 0:
            raise EvidenceMappingError(f"checkpoint_counters {'.'.join(path)} is not a limit")
        limits[field] = value
    out["limits"] = limits
    projection.notes.append(
        {"at_checkpoint": {"basis": AT_CHECKPOINT_BASIS, "state": counters.get("state")}}
    )
    return out, None


# ------------------------------------------------------------------ controller incarnations


def native_incarnations(projection):
    """The controller's own pids, in order: initial_state controller_pid, then each resume
    decision's controller_pid (one per deciding process)."""
    pids = []
    initial = projection.initial_controller_pid()
    if initial is not None:
        pids.append(initial)
    resume, _ = projection.recovery_list("resume")
    for decision in resume or []:
        pid = decision.get("controller_pid") if isinstance(decision, dict) else None
        if _is_count(pid) and pid not in pids:
            pids.append(pid)
    return pids


def _legacy_controller(projection, receipts):
    parents = {r["parent_pid"] for r in receipts}
    controller = parents.pop() if len(parents) == 1 else None
    if not receipts:
        controller = projection.initial_controller_pid()
    return controller


def native_controller(projection, receipts):
    """-> (controller pid or None, {pid string: parent pid} edges to add to the process tree).

    One incarnation: exactly the single-process rule (the sole launch parent, else the
    initial_state pid). Several: the harness-declared traced root (OI-05), after checking it
    against the controller's own records; an inconsistency refuses the projection."""
    pids = native_incarnations(projection)
    if len(pids) <= 1:
        return _legacy_controller(projection, receipts), {}
    record = route(projection)
    declared = record.get("incarnations") if isinstance(record, dict) else None
    if not isinstance(declared, list) or not _is_count(record.get("root_pid")):
        projection.notes.append(
            {"controller_incarnations_unattributed": {"pids": pids, "reason": "no harness record"}}
        )
        return None, {}
    root = record["root_pid"]
    parents = {}
    for entry in declared:
        if not isinstance(entry, dict) or not _is_count(entry.get("pid")):
            raise EvidenceMappingError("harness incarnation record is malformed")
        parents[entry["pid"]] = entry.get("parent_pid")
    for pid in pids:
        if pid not in parents:
            raise EvidenceMappingError(f"controller incarnation pid {pid} not declared by harness")
        if pid != root and parents[pid] != root:
            raise EvidenceMappingError(f"incarnation pid {pid} is not a child of the traced root")
    launch_parents = {r["parent_pid"] for r in receipts}
    if not launch_parents <= set(pids):
        raise EvidenceMappingError("a launch parent is not a controller incarnation")
    projection.notes.append(
        {"controller_incarnations": {"root_pid": root, "pids": pids, "basis": CONTROLLER_PID_BASIS}}
    )
    return root, {str(pid): root for pid in pids if pid != root}


# ------------------------------------------------------------------ killed-process output bytes


def killed_output_bytes(projection, receipts):
    """OI-04: harness-observed byte counts for a launched process the harness killed."""
    record = route(projection)
    kill = record.get("kill") if isinstance(record, dict) else None
    if not isinstance(kill, dict):
        return
    killed = set(kill.get("killed") or [])
    observed = kill.get("output_bytes") or {}
    for receipt in receipts:
        if receipt.get("end") is not None or "output_bytes" in receipt:
            continue
        path = f"process_receipts[serial={receipt['serial']}].output_bytes"
        if receipt["pid"] not in killed:
            continue
        counts = observed.get(str(receipt["pid"]))
        stdout = counts.get("stdout") if isinstance(counts, dict) else None
        stderr = counts.get("stderr") if isinstance(counts, dict) else None
        if not (_is_count(stdout) and _is_count(stderr)):
            for gap in projection.missing:
                if gap["path"] == path:
                    gap["reason"] = (
                        "killed by the harness stop; its output bytes were not observable "
                        "before the kill (never 0)"
                    )
            continue
        receipt["output_bytes"] = stdout + stderr
        receipt["output_bytes_source"] = OUTPUT_BYTES_SOURCE
        projection.missing = [m for m in projection.missing if m["path"] != path]
        projection.notes.append(
            {
                "output_bytes_harness_observed": {
                    "serial": receipt["serial"],
                    "pid": receipt["pid"],
                    "basis": kill.get("output_bytes_basis"),
                    "route": kill.get("route"),
                }
            }
        )


# ------------------------------------------------------------------ onset_partial + symbols


def _midprocess(record):
    stop = record.get("stop") if isinstance(record, dict) else None
    return isinstance(stop, dict) and stop.get("trigger") == "process_launch"


def harness_fields(projection, harness):
    """harness_observations.onset_partial (mid-process stop) and controller_incarnations."""
    record = route(projection)
    if record is None:
        return
    harness["controller_incarnations"] = {
        "source": "harness parent (fork return value and kill record), never the controller",
        "root_pid": record.get("root_pid"),
        "incarnations": record.get("incarnations"),
        "kill": {
            k: (record.get("kill") or {}).get(k)
            for k in ("route", "pid", "killed", "descendants", "ok", "errors")
        },
    }
    if not _midprocess(record):
        return
    partial = record.get("onset_partial")
    if isinstance(partial, dict) and isinstance(partial.get("present_at_interrupt"), bool):
        harness["onset_partial"] = {
            "present_at_interrupt": partial["present_at_interrupt"],
            "observed_by": partial.get("observed_by"),
        }
    else:
        projection.gap(
            "harness_observations.onset_partial",
            "CAP-MIDPROCESS",
            "the harness recorded no onset observation at the interrupt",
        )


def project_symbols(projection, out):
    record = route(projection)
    if not _midprocess(record):
        return
    partial = record.get("onset_partial") or {}
    value = partial.get("content_hash")
    if partial.get("present_at_interrupt") is True and isinstance(value, str) and value:
        out[ONSET_PARTIAL_SYMBOL] = value
    elif partial.get("present_at_interrupt") is True:
        projection.gap(
            f"symbols.{ONSET_PARTIAL_SYMBOL}",
            "CAP-MIDPROCESS",
            "onset was present at the interrupt but its hash was not observable",
        )


# ------------------------------------------------------------------ capability accessors


def trace_report(projection, name):
    """controller_trace.<name> with its event keys and gaps, or MISSING with the reason."""
    trace = projection["evidence"]["controller_trace"]
    path = f"controller_trace.{name}"
    if name not in trace:
        reasons = [m["reason"] for m in projection["missing"] if m["path"] == path]
        return {"status": "MISSING", "reason": "; ".join(reasons) or "not projected"}
    prefix = path + "["
    return {
        name: trace[name],
        "event_seq": {
            k: v for k, v in projection["evidence"]["event_seq"].items() if k.startswith(prefix)
        },
        "missing": [m for m in projection["missing"] if m["path"].startswith(path)],
    }
