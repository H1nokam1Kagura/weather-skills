"""Controller-owned boundaries: the artifact commit write and the external budget meter.

CAP-DENY-WRITE (CP-E17). A producing tool writes its output; the artifact only becomes a run
artifact when the controller commits it: it verifies the output lies in the run's artifact
namespace, hashes it and durably writes a commit record. That write is the controller write
boundary for one artifact (frozen fixture FX-INJECT-PERMISSION-ERROR target). A
PermissionError there is a terminal BLOCKED stop with failed_gate ``write_denial``; its
``denial_source`` is ``injected_fault`` when the trusted harness armed the fault and
``host_acl`` when the operating system refused (EVIDENCE_CONTRACT.md controller_trace.terminal
{..., denial_source?}). Host ACLs are never changed to produce a denial, an armed fault fires
exactly once, and nothing retries or redirects the write.

CAP-METRIC (CP-E18, budget half). An external meter may report consumption the controller
cannot observe itself (consumed_model_usd, elapsed_seconds). A reading is a monotone floor: it
can raise the controller's accounting, never lower it, so no reading can reset a budget. Repair
attempts are counted by the controller alone; a repair reading is recorded and reconciled,
never applied. Each reading is a durable controller record with no marker of where it came
from (the harness keeps its own injected-reading record).
"""

import errno as errno_codes
import math
import time
from pathlib import Path

from .catalog import tree_hash
from .contracts import Refused

INJECTED, HOST = "injected_fault", "host_acl"


class WriteDenied(Refused):
    def __init__(self, message, denial_source):
        super().__init__(message, gate="write_denial", reason="write_denied", outcome="BLOCKED")
        self.denial_source = denial_source


class Boundary:
    """Per-controller, in-memory: an armed fault is harness state, consumed on first use."""

    def __init__(self):
        self.armed = {}

    def inject_denial(self, artifact, errno=errno_codes.EACCES, message="Permission denied"):
        """Trusted harness channel only (Python hooks); never an agent or CLI capability."""
        if not isinstance(artifact, str) or not artifact or artifact in self.armed:
            raise ValueError("one armed denial per named artifact")
        if errno != errno_codes.EACCES or not isinstance(message, str):
            raise ValueError("injected denial must be PermissionError(errno 13, message)")
        self.armed[artifact] = {"errno": errno, "message": message}

    def write(self, controller, artifact, name, record):
        fault = self.armed.pop(artifact, None)
        if fault is not None:
            error = PermissionError(fault["errno"], fault["message"])
            error.injected_fault = True
            raise error
        controller.save(name, record)


def boundary_for(controller):
    boundary = getattr(controller, "write_boundary", None)
    if boundary is None:
        boundary = controller.write_boundary = Boundary()
    return boundary


def artifact_for_output(controller, argv):
    """The run artifact a producing argv writes, or None when it writes none."""
    if "--output" not in argv:
        return None
    output = Path(argv[argv.index("--output") + 1])
    for name, path in controller.paths.items():
        if name != "source" and Path(path) == output:
            return name
    raise Refused("producing output outside the run artifact namespace", gate="write_denial")


def commit(controller, artifact, skill):
    """Commit one produced artifact through the controller write boundary."""
    path = Path(controller.paths[artifact])
    if not path.resolve().is_relative_to(Path(controller.outputs).resolve()):
        raise Refused("artifact outside the run artifact namespace", gate="write_denial")
    content_hash = tree_hash(path)
    record = {
        "schema": "workflow.artifact-commit/1",
        "artifact": artifact,
        "path": str(path),
        "content_hash": content_hash,
        "tool": skill,
        "state": controller.state,
    }
    try:
        boundary_for(controller).write(
            controller, artifact, f"artifact-commit-{artifact}.json", record
        )
    except PermissionError as exc:
        source = INJECTED if getattr(exc, "injected_fault", False) else HOST
        denial = {
            "artifact": artifact,
            "tool": skill,
            "state": controller.state,
            "errno": exc.errno,
            "message": str(exc),
            "denial_source": source,
        }
        controller.context.setdefault("write_denials", []).append(denial)
        controller.dispatch_event("write_denial", denial=denial)
        controller.checkpoint()
        raise WriteDenied(f"controller write denied for {artifact}: {exc}", source) from exc
    commits = controller.context.setdefault("artifact_commits", [])
    commits.append(record)
    controller.dispatch_event("artifact_commit", commit=record)
    return content_hash


# ---------------------------------------------------------------- budget meter (CAP-METRIC)

METERED = ("consumed_model_usd", "elapsed_seconds", "repair_attempts_used")


def _native(controller, metric):
    if metric == "consumed_model_usd":
        return controller.roles.spent
    if metric == "elapsed_seconds":
        return controller.elapsed_before + time.monotonic() - controller.started
    return controller.repairs_used


def meter_reading(controller, metric, value):
    """Apply one external meter reading as a monotone floor and record it durably."""
    if metric not in METERED:
        raise Refused(f"no meter for {metric}", gate="budget")
    if (
        type(value) not in (int, float)
        or not math.isfinite(value)
        or value < 0
        or (metric == "repair_attempts_used" and type(value) is not int)
    ):
        raise Refused(f"invalid meter reading for {metric}", gate="budget")
    before = _native(controller, metric)
    applied = metric != "repair_attempts_used" and value > before
    if applied and metric == "consumed_model_usd":
        controller.roles.spent = value
    elif applied:
        delta = value - before
        controller.elapsed_before += delta
        deadline = getattr(controller.processes, "deadline", None)
        if isinstance(deadline, (int, float)) and math.isfinite(deadline):
            controller.processes.deadline = deadline - delta
    readings = controller.context.setdefault("budget_readings", [])
    record = {
        "index": len(readings),
        "metric": metric,
        "value": value,
        "native_before": before,
        "effective": _native(controller, metric),
        "applied": applied,
        "reconciled": value == before if metric == "repair_attempts_used" else value <= before,
        "state": controller.state,
    }
    readings.append(record)
    controller.save(f"budget-reading-{record['index']:03}.json", record)
    controller.dispatch_event("budget_reading", reading=record)
    controller.checkpoint()
    return record
