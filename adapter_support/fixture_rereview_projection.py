"""CAP-FIXTURE-OP (apply_fixture_operation) and CAP-REREVIEW (record_material_changes).

CAP-FIXTURE-OP: the frozen fixture operations (casesets/cached-onset-e2e-v2_7 DEV-ONLY
``fixture_operations``) are delivered by tools/workflow/e2e_injections.InjectionHooks
(``mutate_fixture`` -> ``_apply`` -> ``_fixture`` / ``_ledger_fixture`` / ``_arm_transport_fault``)
at the controller's own hook points, chained behind the harness launch observer by run_case.
This module adds what the delivery path does not do itself:

* ``fixture_plan``: a static, side-effect-free check of every scheduled fixture operation
  (known operation, complete arguments, the target the delivery path can actually reach),
  refused before anything is staged;
* ``stage_repo_copy`` / ``adopt_repo_copy``: the disposable copy of the controller repository
  the definition and runtime-manifest operations edit (frozen targets "disposable copy of the
  definition" / "disposable copy of the runtime manifest the approval binds"); the delivery
  path refuses any edit outside its disposable root, so the controller must run on the copy;
* ``fixture_report``: the delivery records of a finished run and the harness symbols the
  frozen postconditions name: HARNESS_HASH:source_expected_after, HARNESS_HASH:source_pre (FX-
  SRC-META-ATTR) and HARNESS_HASH:onset_after_mutation (FX-ONSET-CELL-SHIFT). A symbol the run
  did not observe stays MISSING with its reason.

CAP-REREVIEW (EVIDENCE_CONTRACT v2_4.material_changes): controller_trace.material_changes and
controller_trace.source_rechecks are the controller's own records (runner.Controller.
record_material_change / recheck_source), each checked against its evidence file and dispatch
event; the superseded-approval chain behind controller_trace.active_approval is checked; and
``transition_history`` rebuilds the state history from the controller's own record of every
return to review (the ``return_to_review`` dispatch event, ``context.rereviews``), which removes
withdrawn transitions from the checkpoint but not their dispatch events (review finding X3).
"""

import copy
import json
import shutil
from pathlib import Path

from adapter_support.evidence_projection import EvidenceMappingError
from tools.workflow.catalog import RUNTIME_MANIFEST, safe_path, tree_hash
from tools.workflow.contracts import digest
from tools.workflow.e2e_injections import DEFINITIONS_FILE, FIXTURE_OPERATIONS, InjectionHooks

FIXTURE_CAPABILITY = "CAP-FIXTURE-OP"
REREVIEW_CAPABILITY = "CAP-REREVIEW"
REPORT_SCHEMA = "weather-adapter.fixture-operations/1"
REPO_COPY_SCHEMA = "weather-adapter.disposable-repo-copy/1"
REPO_COPY_DIR = "repo"

# Reviewed classification of every operation the delivery path implements: what it writes
# (frozen fixture_operations[*].target) and where, in case mode, that write lands.
#   request: the captured request's messages, in memory before the controller captures it
#   operator_ledger: run/operator-ledger.jsonl (created by the controller, appended in place)
#   process_stream: a captured tool stdout/stderr in memory (never a file)
#   role_transport: one transport call (never a file)
#   repo_copy: an existing file of the disposable repository copy, rewritten in place
#   source: the staged source Zarr under source/ (zarr writes <name>.<uuid>.partial, renames)
#   run_artifact: the onset Zarr under run/artifacts/ (same temp-file pattern, inside run/)
FIXTURE_TARGETS = {
    "FX-LEDGER-APPEND": "request_or_operator_ledger",
    "FX-OPERATOR-RECORD": "operator_ledger",
    "FX-REPORT-RESERIALIZE": "process_stream",
    "FX-STDERR-APPEND": "process_stream",
    "FX-TRANSPORT-RETRY": "role_transport",
    "FX-DEF-PARAM-EDIT": "repo_copy",
    "FX-DEF-TOUCH": "repo_copy",
    "FX-RUNTIME-MANIFEST-BUMP": "repo_copy",
    "FX-SRC-META-ATTR": "source",
    "FX-SRC-CELL-INCREMENT": "source",
    "FX-ONSET-CELL-SHIFT": "run_artifact",
}
# Operations whose delivery creates new entries outside HARNESS_RUN_DIR (frozen U12/all_under) in
# the traced controller process, unless delivered at before_run (prestage_before_run, untraced).
CREATES_OUTSIDE_RUN_DIR = {
    "FX-SRC-META-ATTR": "zarr 3 writes source/source.zarr/<key>.<uuid>.partial then renames "
    "(attrs and consolidated metadata): created paths under source/, outside HARNESS_RUN_DIR",
    "FX-SRC-CELL-INCREMENT": "zarr 3 writes source/source.zarr/<chunk>.<uuid>.partial then "
    "renames: created paths under source/, outside HARNESS_RUN_DIR",
}
# The argument set each frozen operation carries (DEV-ONLY fixture_operations[*].arguments; the
# same key set in every frozen use). "Complete arguments": exactly these keys, nothing defaulted.
REQUIRED_ARGS = {
    "FX-LEDGER-APPEND": {"speaker", "position", "text"},
    "FX-OPERATOR-RECORD": {"record_id", "speaker", "action", "run_id", "text"},
    "FX-REPORT-RESERIALIZE": {"report", "member_order", "indent"},
    "FX-STDERR-APPEND": {"tool", "occurrence", "text"},
    "FX-TRANSPORT-RETRY": {
        "role",
        "occurrence",
        "failure_class",
        "failures",
        "max_retries",
        "before_write",
    },
    "FX-DEF-PARAM-EDIT": {"definition_ref", "parameter", "from", "to"},
    "FX-DEF-TOUCH": {"definition_ref", "mtime_delta_seconds"},
    "FX-RUNTIME-MANIFEST-BUMP": {"field", "value"},
    "FX-SRC-META-ATTR": {"attribute", "value"},
    "FX-SRC-CELL-INCREMENT": {"variable", "amount_mm"},
    "FX-ONSET-CELL-SHIFT": {"artifact", "days"},
}
# What the controller's repository copy must hold for the copy to be the binding's twin: the
# files catalog.build_manifest hashes, plus the whole skills tree (scripts the controller runs).
REPO_COPY_TREES = (("skills", "**/*"), ("registry", "*.toml"), ("agents", "*.md"))
REPO_COPY_TREES += (("tools/workflow", "*.py"),)
REPO_COPY_FILES = ("pyproject.toml", RUNTIME_MANIFEST)


def _refusal():
    from adapter_support.weather_adapter import AdapterRefusal

    return AdapterRefusal


def _refuse(message):
    raise _refusal()(message)


# --------------------------------------------------------------------------- static plan


def fixture_entries(case):
    """(index, spec) for every scheduled mutate_fixture entry."""
    schedule = case.get("injection_schedule") if isinstance(case, dict) else None
    return [
        (i, s)
        for i, s in enumerate(schedule if isinstance(schedule, list) else [])
        if isinstance(s, dict) and s.get("operation") == "mutate_fixture"
    ]


def _check_args(index, operation, spec, args):
    where = f"schedule entry {index} ({operation})"
    if set(args) != REQUIRED_ARGS[operation]:
        _refuse(
            f"{where}: arguments {sorted(args)} are not the complete frozen set "
            f"{sorted(REQUIRED_ARGS[operation])}"
        )
    selector = spec.get("selector") if isinstance(spec.get("selector"), dict) else {}
    if operation == "FX-REPORT-RESERIALIZE" and (
        args["member_order"] != "reverse"
        or args["indent"] != 2
        or args["report"] != selector.get("role_or_tool_or_stage")
        or spec.get("hook") != "after_tool"
    ):
        _refuse(f"{where}: only a reverse-order, indent-2 reserialization of the selected report")
    if operation == "FX-STDERR-APPEND" and (
        args["occurrence"] != selector.get("occurrence", 1) or spec.get("hook") != "after_tool"
    ):
        _refuse(f"{where}: the stderr occurrence must be the selected tool occurrence")
    if operation == "FX-ONSET-CELL-SHIFT" and args["artifact"] != "onset":
        _refuse(f"{where}: the delivery path shifts the onset artifact only")


def fixture_plan(case, repo=None):
    """CAP-FIXTURE-OP static mapping of every scheduled fixture operation, or AdapterRefusal.

    No side effect. ``repo`` is the controller repository the copy would be taken from; an
    operation on a file it does not carry is refused (nothing is seeded)."""
    if not isinstance(case, dict) or not isinstance(case.get("id"), str) or not case["id"]:
        _refuse("a case with an explicit id is required")
    entries, needs_repo = [], False
    for index, spec in fixture_entries(case):
        payload = spec.get("payload")
        if not isinstance(payload, dict) or not isinstance(payload.get("args"), dict):
            _refuse(f"schedule entry {index}: a fixture operation needs a payload with args")
        operation = payload.get("fixture_operation")
        if operation not in FIXTURE_TARGETS:
            _refuse(f"schedule entry {index}: fixture operation unavailable: {operation!r}")
        args = payload["args"]
        _check_args(index, operation, spec, args)
        target = FIXTURE_TARGETS[operation]
        if target == "repo_copy":
            needs_repo = True
            relative = (
                RUNTIME_MANIFEST if operation == "FX-RUNTIME-MANIFEST-BUMP" else (DEFINITIONS_FILE)
            )
            if repo is not None and not (Path(repo) / relative).is_file():
                _refuse(
                    f"schedule entry {index} ({operation}): the controller repository carries "
                    f"no {relative}; there is nothing to edit and none is seeded"
                )
        if target == "request_or_operator_ledger":
            target = "request" if spec.get("hook") == "before_run" else "operator_ledger"
        entries.append(
            {
                "index": index,
                "id": spec.get("id"),
                "hook": spec.get("hook"),
                "selector": copy.deepcopy(spec.get("selector")),
                "fixture_operation": operation,
                "args_hash": digest(args),
                "target": target,
                "creates_outside_run_dir": CREATES_OUTSIDE_RUN_DIR.get(operation),
            }
        )
    return {
        "capability": FIXTURE_CAPABILITY,
        "case_id": case["id"],
        "entries": entries,
        "needs_repo_copy": needs_repo,
        "delivery": "tools/workflow/e2e_injections.InjectionHooks (mutate_fixture) at the "
        "controller's own hook points, chained behind the harness launch observer",
    }


# --------------------------------------------------------------------------- repository copy


def repo_files(repo):
    """Relative posix paths of the files a repository copy carries (see REPO_COPY_TREES)."""
    repo = Path(repo)
    found = set()
    for base, pattern in REPO_COPY_TREES:
        for path in (repo / base).glob(pattern):
            if path.is_symlink():
                _refuse(f"repository copy refuses a link: {path}")
            if path.is_file() and "__pycache__" not in path.parts:
                found.add(path.relative_to(repo).as_posix())
    for name in REPO_COPY_FILES:
        if (repo / name).is_file():
            found.add(name)
    return sorted(found)


def _manifest_hash(repo):
    from tools.workflow import runner  # the controller's own binding routine (patchable seam)

    return digest(runner.build_manifest(repo))


def stage_repo_copy(repo, case_root):
    """The disposable copy of the controller repository under case_root/repo.

    The copy is the binding's twin: its build manifest must hash exactly as the source
    repository's, else the copy is refused (removed is never attempted; the case root is the
    harness's disposable directory). Run this untraced in container case mode: it creates
    every copied path outside HARNESS_RUN_DIR."""
    source = safe_path(Path(repo))
    root = safe_path(Path(case_root))
    target = root / REPO_COPY_DIR
    if target.exists():
        _refuse("the disposable repository copy already exists; reuse is refused")
    files = repo_files(source)
    for relative in files:
        destination = target / relative
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(source / relative, destination)
    record = {
        "schema": REPO_COPY_SCHEMA,
        "source_repo": str(source),
        "repo": str(target),
        "files": {relative: tree_hash(target / relative) for relative in files},
        "source_manifest_hash": _manifest_hash(source),
        "copy_manifest_hash": _manifest_hash(target),
    }
    if record["copy_manifest_hash"] != record["source_manifest_hash"]:
        _refuse("the repository copy does not reproduce the source build manifest")
    return record


def adopt_repo_copy(record, case_root):
    """A repository copy the harness staged earlier (untraced), re-verified, never re-staged."""
    if not isinstance(record, dict) or record.get("schema") != REPO_COPY_SCHEMA:
        _refuse("prepared repository copy has an unknown schema")
    target = safe_path(Path(case_root)) / REPO_COPY_DIR
    if safe_path(Path(record.get("repo", ""))) != target:
        _refuse("prepared repository copy is not at case_root/repo")
    files = record.get("files")
    if not isinstance(files, dict) or repo_files(target) != sorted(files):
        _refuse("prepared repository copy holds other files than staging recorded")
    for relative, value in files.items():
        if tree_hash(target / relative) != value:
            _refuse(f"prepared repository copy changed since staging: {relative}")
    if _manifest_hash(target) != record.get("copy_manifest_hash"):
        _refuse("prepared repository copy no longer reproduces its staged build manifest")
    return copy.deepcopy(record)


def controller_repo(plan, profile, prepared):
    """(repo the controller runs on, repository-copy record or None).

    No repository operation scheduled: the profile's repo, untouched. Otherwise the disposable
    copy: adopted from profile["prepared_repo"] when the harness staged it, staged here on the
    in-process route, and refused in pre-staged case mode without one (staging it inside the
    traced run_case would create paths outside HARNESS_RUN_DIR)."""
    if not plan["needs_repo_copy"]:
        return profile["repo"], None
    if profile.get("prepared_repo") is not None:
        record = adopt_repo_copy(profile["prepared_repo"], prepared["case_root"])
    elif profile.get("prepared_fixture") is not None:
        _refuse(
            "pre-staged case mode needs the disposable repository copy staged untraced with "
            "the fixture (profile['prepared_repo']); staging it here would create paths "
            "outside HARNESS_RUN_DIR"
        )
    else:
        record = stage_repo_copy(profile["repo"], prepared["case_root"])
    return Path(record["repo"]), record


# --------------------------------------------------------------------------- run report

_DELIVERY_KEYS = (
    "status",
    "hook",
    "state",
    "occurrence",
    "before_hash",
    "after_hash",
    "error",
    "effect",
    "cell_index",
    "json_value_equal",
    "bytes_differ",
    "adapter_mapping",
    "mtime_delta_seconds",
    "build_id_from",
    "build_id_to",
    "message_id",
    "record_id",
    "record_hash",
    "assigned_fields",
    "delivery",
    "prestaged",
)


def fixture_report(plan, schedule_hooks, source_pre):
    """The delivery record of each planned fixture operation and the harness symbols the
    frozen postconditions name. ``schedule_hooks``: run_case's CaseScheduleHooks."""
    injection = getattr(schedule_hooks, "injection", None)
    records = {r["index"]: r for r in (injection.records if injection is not None else [])}
    dispositions = getattr(injection, "dispositions", {}) if injection is not None else {}
    entries = []
    for planned in plan["entries"]:
        record = records.get(planned["index"])
        entry = copy.deepcopy(planned)
        if record is None:
            disposition = dispositions.get(planned["index"]) or {}
            entry["status"] = "UNREACHED"
            entry["reason"] = disposition.get("reason", "boundary not reached")
        else:
            entry.update({k: copy.deepcopy(record[k]) for k in _DELIVERY_KEYS if k in record})
            if "target" in record:  # the delivery path's own name for what it wrote
                entry["delivered_target"] = record["target"]
        entries.append(entry)
    symbols, missing, notes = {}, [], []
    source_ops = [e for e in entries if e["target"] == "source"]
    if not source_ops:
        symbols["HARNESS_HASH:source_expected_after"] = source_pre
        notes.append(
            "source_expected_after = source_pre: no scheduled fixture operation targets the "
            "source cache (FIXTURE_TARGETS)"
        )
    elif all(e["status"] == "APPLIED" for e in source_ops):
        symbols["HARNESS_HASH:source_expected_after"] = source_ops[-1]["after_hash"]
        notes.append("source_expected_after = after_hash of the last applied source operation")
    else:
        missing.append(
            {
                "symbol": "HARNESS_HASH:source_expected_after",
                "reason": "CAP-FIXTURE-OP: a scheduled source operation was not applied ("
                + ", ".join(f"{e['index']}:{e['status']}" for e in source_ops)
                + "); its postcondition hash was never observed",
            }
        )
    meta = [e for e in source_ops if e["fixture_operation"] == "FX-SRC-META-ATTR"]
    if meta:
        # Frozen postcondition: "content hash recomputed and recorded as HARNESS_HASH:source_pre
        # before CAPTURED" (a before_run mutation precedes the controller's capture).
        if all(e["status"] == "APPLIED" and e["hook"] == "before_run" for e in meta):
            symbols["HARNESS_HASH:source_pre"] = meta[-1]["after_hash"]
        else:
            missing.append(
                {
                    "symbol": "HARNESS_HASH:source_pre",
                    "reason": "CAP-FIXTURE-OP: FX-SRC-META-ATTR was not applied before the run",
                }
            )
    shifts = [e for e in entries if e["fixture_operation"] == "FX-ONSET-CELL-SHIFT"]
    if shifts:
        applied = [e for e in shifts if e["status"] == "APPLIED"]
        if len(applied) == len(shifts):
            symbols["HARNESS_HASH:onset_after_mutation"] = applied[-1]["after_hash"]
        else:
            missing.append(
                {
                    "symbol": "HARNESS_HASH:onset_after_mutation",
                    "reason": "CAP-FIXTURE-OP: FX-ONSET-CELL-SHIFT was not applied",
                }
            )
    return {
        "schema": REPORT_SCHEMA,
        "capability": FIXTURE_CAPABILITY,
        "entries": entries,
        "symbols": symbols,
        "missing": missing,
        "notes": notes,
        "outside_run_dir": [
            {"index": e["index"], "operation": e["fixture_operation"], "why": why}
            for e in entries
            if (why := e["creates_outside_run_dir"])
            and e["status"] == "APPLIED"
            and not e.get("prestaged")  # applied in fixture preparation, before the run
        ],
    }


def harness_inputs_from(report, inputs):
    """Fold the report's harness symbols into run_case's projection inputs (in place).

    The keys are the ones the scored in-container projection already merges
    (container_harness/project_in_container.INPUT_KEYS) plus ``fixture_operations``."""
    inputs["fixture_operations"] = report
    symbols = report["symbols"]
    if "HARNESS_HASH:source_pre" in symbols:
        inputs["source_pre"] = symbols["HARNESS_HASH:source_pre"]
    if "HARNESS_HASH:source_expected_after" in symbols:
        inputs["source_expected_after"] = symbols["HARNESS_HASH:source_expected_after"]
        inputs["source_expected_after_reason"] = "; ".join(report["notes"])
    else:
        inputs.pop("source_expected_after", None)
        inputs["source_expected_after_reason"] = next(
            m["reason"]
            for m in report["missing"]
            if m["symbol"] == "HARNESS_HASH:source_expected_after"
        )
    return inputs


def project_fixture_symbols(projection, out):
    """Projection.symbols: HARNESS_HASH:onset_after_mutation from the harness report, or a
    CAP-FIXTURE-OP gap when the case scheduled the operation and it was not observed."""
    report = projection.h.get("fixture_operations")
    if not isinstance(report, dict):
        return
    if report.get("schema") != REPORT_SCHEMA:
        raise EvidenceMappingError("fixture-operations report has an unknown schema")
    value = report["symbols"].get("HARNESS_HASH:onset_after_mutation")
    if isinstance(value, str):
        out["HARNESS_HASH:onset_after_mutation"] = value
    for gap in report["missing"]:
        if gap["symbol"] in {"HARNESS_HASH:onset_after_mutation", "HARNESS_HASH:source_pre"}:
            projection.gap(f"symbols.{gap['symbol']}", FIXTURE_CAPABILITY, gap["reason"])
    if report["entries"]:
        projection.notes.append(
            {
                "fixture_operations": [
                    {
                        k: e.get(k)
                        for k in ("index", "fixture_operation", "target", "status", "after_hash")
                    }
                    for e in report["entries"]
                ]
            }
        )


# --------------------------------------------------------------------------- CAP-REREVIEW

# controller_trace list -> (dispatch event kind, event key, evidence file prefix): the records
# runner.Controller.record_material_change / recheck_source write (EC v2_4.material_changes).
REREVIEW_LISTS = {
    "material_changes": ("material_change", "change", "material-change"),
    "source_rechecks": ("source_recheck", "recheck", "source-recheck"),
}


def transition_history(native):
    """(surviving transition events, withdrawn transitions) of a run, from the dispatch clock.

    Controller.return_to_review pops the withdrawn transitions from the checkpoint history but
    their transition dispatch events stay (they happened). Each return is the controller's own
    ``return_to_review`` event carrying the rereview record (withdrawn_states, returned_to),
    which must equal context.rereviews[index]; replaying the clock with those withdrawals must
    give exactly the checkpoint history, else the projection is refused."""
    rereviews = native.context.get("rereviews", [])
    if not isinstance(rereviews, list):
        raise EvidenceMappingError("native rereviews is not a list")
    history, withdrawn = [], []
    for event in native.events:
        if event["kind"] == "transition":
            history.append(event)
            continue
        if event["kind"] != "return_to_review":
            continue
        record = event.get("rereview")
        index = record.get("index") if isinstance(record, dict) else None
        if type(index) is not int or not 0 <= index < len(rereviews) or rereviews[index] != record:
            raise EvidenceMappingError("return_to_review event differs from context.rereviews")
        states = record.get("withdrawn_states")
        count = len(states) if isinstance(states, list) else 0
        if not count or count > len(history):
            raise EvidenceMappingError("return_to_review withdraws no recorded transition")
        removed = history[-count:]
        if [e["transition"]["to"] for e in removed] != states:
            raise EvidenceMappingError("withdrawn states are not the latest transitions")
        del history[-count:]
        if not history or history[-1]["transition"]["to"] != record.get("returned_to"):
            raise EvidenceMappingError("return_to_review did not return to its recorded state")
        withdrawn.append(
            {
                "rereview_index": index,
                "return_sequence": event["sequence"],
                "withdrawn": [
                    {"state": e["transition"]["to"], "sequence": e["sequence"]} for e in removed
                ],
                "returned_to": record["returned_to"],
            }
        )
    if len(withdrawn) != len(rereviews):
        raise EvidenceMappingError("a recorded re-review has no return_to_review event")
    if [e["transition"] for e in history] != native.checkpoint["events"]:
        raise EvidenceMappingError("transition events differ from the checkpoint history")
    return history, withdrawn


def rereview_lists(projection, trace):
    """controller_trace.material_changes / source_rechecks, checked; event keys [index=<i>].

    A list the controller holds is projected after each record matches its evidence file and
    its dispatch event. No list: [] for a finished run whose gap-free clock has no such event
    (the controller emits one per detection), MISSING for an unfinished run."""
    native = projection.n
    for name, (kind, key, prefix) in REREVIEW_LISTS.items():
        records = native.context.get(name)
        events = native.kinds(kind)
        if records is None:
            if events:
                raise EvidenceMappingError(f"{kind} events without the native {name} list")
            if native.finished():
                trace[name] = []
                projection.notes.append(
                    {name: f"[]: no {kind} event in the finished, gap-free dispatch sequence"}
                )
            else:
                projection.gap(
                    f"controller_trace.{name}",
                    REREVIEW_CAPABILITY,
                    "the run did not finish: later changes could still be recorded",
                )
            continue
        if not isinstance(records, list) or len(records) != len(events):
            raise EvidenceMappingError(f"native {name} differs from its {kind} events")
        for i, (record, event) in enumerate(zip(records, events, strict=True)):
            body = {k: v for k, v in record.items() if k != "sequence"}
            if (
                record.get("index") != i
                or record.get("sequence") != event["sequence"]
                or event.get(key) != body
                or projection.read_native(f"{prefix}-{i:03}.json") != record
            ):
                raise EvidenceMappingError(f"native {name}[{i}] differs from its file or event")
            projection.seq[f"controller_trace.{name}[index={i}]"] = event["sequence"]
        trace[name] = json.loads(json.dumps(records))
    superseded_chain(projection, trace)


def superseded_chain(projection, trace):
    """A re-reviewed approval replaces the active one (EC v2_4.material_changes): every
    superseded approval is its issued file, each invalidation names recorded material changes,
    and the current approval replaces the last superseded one."""
    native = projection.n
    superseded = native.context.get("superseded_approvals", [])
    if not isinstance(superseded, list):
        raise EvidenceMappingError("native superseded_approvals is not a list")
    changes = {r.get("index") for r in trace.get("material_changes") or []}
    for i, approval in enumerate(superseded):
        issued = {k: v for k, v in approval.items() if k != "invalidated_by"}
        if projection.read_native(f"active-approval-{i:03}.json") != issued:
            raise EvidenceMappingError(f"superseded approval {i} differs from its issued file")
        if not set(approval.get("invalidated_by", [])) <= changes:
            raise EvidenceMappingError(f"superseded approval {i} names unrecorded changes")
    current = native.context.get("active_approval")
    if (
        superseded
        and current is not None
        and current.get("replaces") != superseded[-1].get("approval_id")
    ):
        raise EvidenceMappingError("the active approval does not replace the last superseded one")
    rereviews = native.context.get("rereviews", [])
    if superseded or rereviews:
        projection.notes.append(
            {
                "rereview": {
                    "rereviews": json.loads(json.dumps(rereviews)),
                    "superseded_approvals": [
                        {k: a.get(k) for k in ("approval_id", "sequence", "invalidated_by")}
                        for a in superseded
                    ],
                }
            }
        )


def rereview_report(projection):
    """WeatherAdapter.record_material_changes: the CAP-REREVIEW fields of one projection."""
    evidence = projection["evidence"]
    trace = evidence["controller_trace"]
    prefixes = tuple(f"controller_trace.{name}[" for name in REREVIEW_LISTS)
    out = {name: trace[name] for name in REREVIEW_LISTS if name in trace}
    if "active_approval" in trace:
        out["active_approval"] = trace["active_approval"]
    out["event_seq"] = {
        k: v
        for k, v in evidence["event_seq"].items()
        if k.startswith(prefixes) or k == "controller_trace.active_approval"
    }
    out["missing"] = [
        m
        for m in projection["missing"]
        if m["path"] in {f"controller_trace.{n}" for n in REREVIEW_LISTS}
    ]
    for note in projection["notes"]:
        for key in ("rereview", "withdrawn_transitions"):
            if key in note:
                out[key] = note[key]
    return out


# --------------------------------------------------------------------------- before_run setup

BEFORE_RUN_SCHEMA = "weather-adapter.before-run-fixture/1"
# Targets of a before_run fixture operation that exist before the run starts: the staged source
# copy. A before_run request edit (FX-LEDGER-APPEND) stays in the run process: its target is
# the seeded request object there, and it writes no file.
PRESTAGED_TARGETS = frozenset({"source"})


def prestaged_entries(plan):
    """The fixture plan's before_run entries that fixture preparation applies."""
    return [
        e for e in plan["entries"] if e["hook"] == "before_run" and e["target"] in PRESTAGED_TARGETS
    ]


def prestage_before_run(plan, native_case, prepared):
    """Fixture preparation for the case's before_run fixture operations (frozen hook
    before_run, target the disposable source copy): every such entry applied to the staged
    source BEFORE the run starts, through InjectionHooks.prestage_before_run (the delivery
    path ``prepare`` uses). Container case mode runs this untraced in ``case_driver prepare``,
    before the trace baseline, so its writes (zarr <key>.<uuid>.partial temps, renamed) are
    not paths the traced run created; the in-process route runs it in run_case.

    ``native_case``: run_case's schedule (substitution_plan native_case), whose indices the
    plan carries. -> the record (prepared["before_run_fixture"]) or None when the case
    schedules no such entry. A delivery that raised is carried in the record, never hidden:
    adopt_before_run raises it in run_case, before the controller starts."""
    entries = prestaged_entries(plan)
    if not entries:
        return None
    source = Path(prepared["source"])
    hooks = InjectionHooks(native_case, disposable_root=prepared["case_root"])
    delivery = hooks.prestage_before_run([e["index"] for e in entries], source)
    return {
        "schema": BEFORE_RUN_SCHEMA,
        "case_id": plan["case_id"],
        "source": str(source),
        "source_pre": prepared["source_pre"]["value"],
        "source_after": tree_hash(source),
        "delivery": delivery,
    }


def adopt_before_run(record, plan, injection, prepared):
    """run_case, before ``prepare``: install the fixture-preparation delivery on the run's
    InjectionHooks (so the report carries the records exactly as delivered and prepare does
    not deliver them again), after re-verifying it against the staged source. Refused when
    the case schedules such an entry and none was prepared: applying it in the run would
    create paths outside HARNESS_RUN_DIR in the traced process."""
    entries = prestaged_entries(plan)
    if not entries:
        if record is not None:
            _refuse("a before_run fixture record for a case that schedules no such operation")
        return None
    if record is None:
        _refuse(
            "the case's before_run fixture operations must be applied in fixture preparation "
            "(case_driver prepare, before the trace); applying them in the run would create "
            "paths outside HARNESS_RUN_DIR"
        )
    if not isinstance(record, dict) or record.get("schema") != BEFORE_RUN_SCHEMA:
        _refuse("before_run fixture record has an unknown schema")
    if record.get("case_id") != plan["case_id"]:
        _refuse("before_run fixture record belongs to another case")
    source = Path(prepared["source"])
    if safe_path(Path(record.get("source", ""))) != safe_path(source):
        _refuse("before_run fixture record is not for the staged source")
    if record.get("source_pre") != prepared["source_pre"]["value"]:
        _refuse("before_run fixture record does not start from the staged source")
    delivery = record.get("delivery") or {}
    if delivery.get("indices") != [e["index"] for e in entries]:
        _refuse("before_run fixture record delivers other entries than the case schedules")
    if tree_hash(source) != record.get("source_after"):
        _refuse("staged source changed since the before_run fixture operations")
    applied = [r for r in delivery.get("records") or [] if r.get("status") == "APPLIED"]
    if applied and delivery.get("raised") is None and (
        applied[0].get("before_hash") != record["source_pre"]
        or applied[-1].get("after_hash") != record["source_after"]
    ):
        _refuse("before_run fixture record hashes do not chain from source_pre to the source")
    injection.adopt_before_run(delivery)
    return copy.deepcopy(record)


def _self_check():
    if set(FIXTURE_TARGETS) != set(FIXTURE_OPERATIONS) or set(REQUIRED_ARGS) != set(
        FIXTURE_TARGETS
    ):
        raise RuntimeError("fixture classification differs from the delivery path's operations")


_self_check()
