"""Trusted, fail-closed DEV injection adapters; never an agent capability.

Records distinguish an applied stimulus from an unimplemented or unreached one.
The caller owns oracle evaluation and live/fixture role selection.
"""

import copy
import errno
import json
import os
import re
import tomllib
from collections import Counter
from pathlib import Path
from types import SimpleNamespace

from . import claim_gates, write_boundary
from .catalog import RUNTIME_MANIFEST, tree_hash
from .contracts import Refused, digest
from .operator_ledger import append_record, read_record_ids
from .persistence import read_checkpoint


class UnsupportedInjection(RuntimeError):
    """The declared stimulus cannot faithfully reach this implementation."""


# HARNESS_SOURCE_HASH:<source id>; a source id may hold dots (definition.agrhymet-sos-rolling).
SOURCE_SYMBOL = re.compile(r"HARNESS_SOURCE_HASH:[A-Za-z0-9_-]+(?:\.[A-Za-z0-9_-]+)*")


def bound_source_hash(symbol, controller):
    """Resolve a HARNESS_SOURCE_HASH symbol against the sources the controller bound
    (claim_gates.resolve_source_symbol): the review sources once bound, else the bound
    reference definition. A symbol naming no single bound source cannot be delivered."""
    evidence = (getattr(controller, "context", None) or {}).get("review_evidence") or {}
    sources = evidence.get("sources")
    if not sources and isinstance(evidence.get("definition_source"), dict):
        sources = [evidence["definition_source"]]
    try:
        return claim_gates.resolve_source_symbol(symbol, sources or [])
    except Refused as exc:
        raise UnsupportedInjection(f"unresolved trusted symbol: {symbol} ({exc})") from exc


class InjectedInterruption(RuntimeError):
    """A known, quiescent durable-checkpoint interruption; not a process crash."""


class InjectedConnectionDrop(ConnectionResetError):
    """harness_drop_model_connection, failure_class connection_reset, at a durable boundary.

    A real ConnectionResetError: the controller classifies it by type, never by origin.
    """


class InjectedTimeout(TimeoutError):
    """harness_drop_model_connection, failure_class timeout, at a durable boundary."""


DROP_FAULTS = {"connection_reset": InjectedConnectionDrop, "timeout": InjectedTimeout}
# CP-E06: the controller's native notify-and-wait boundary (recovery.notify). Opt-in for the
# dependent dispatcher until test_schedule_dispatch_v27 (which documents it as unproduced) is
# updated; see InjectionHooks(notification_boundary=...).
NOTIFICATION_HOOK = "after_operator_notification"


class InvalidSchedule(ValueError):
    """A dependent schedule violates the id/requires contract; refused at load."""


class HarnessDispatchError(RuntimeError):
    """The harness itself observed an inconsistent boundary; dispatch stops."""


# Boundaries the controller actually emits. Any other hook is NOT_IMPLEMENTED, never unmatched.
KNOWN_HOOKS = frozenset(
    {
        "before_run",
        "before_role",
        "after_role",
        "after_transition",
        "before_tool",
        "after_tool",
        "after_review_approval",
    }
)
APPROVAL_CONDITIONS = ("schema_valid", "binding_valid", "controller_accepted", "approval_committed")
FIXTURE_OPERATIONS = frozenset(
    {
        "FX-REPORT-RESERIALIZE",
        "FX-SRC-META-ATTR",
        "FX-SRC-CELL-INCREMENT",
        "FX-ONSET-CELL-SHIFT",
        "FX-DEF-PARAM-EDIT",
        "FX-DEF-TOUCH",
        "FX-STDERR-APPEND",
        "FX-RUNTIME-MANIFEST-BUMP",
        "FX-LEDGER-APPEND",
        "FX-OPERATOR-RECORD",
        "FX-TRANSPORT-RETRY",
    }
)
DEFINITIONS_FILE = "skills/onset-date/references/onset_definitions.toml"
# Reviewed adapter mapping: frozen FX-DEF-PARAM-EDIT parameter name -> registry field
# (onset_definitions.toml [definitions.<ref>] trigger.total_mm is the trigger accumulation).
DEFINITION_PARAMETERS = {"trigger_accumulation_mm": ("trigger", "total_mm")}
# Reviewed adapter mapping for CAP-ROLE-INPUT-PATCH (E08-P2): the frozen verifier evidence path
# /evidence/verify-run(<artifact>) is the controller's verifier payload /gates/<artifact>
# (runner.py DATA_VERIFIED: gates keyed by artifact name hold the verify-run gate reports).
VERIFIER_EVIDENCE_PATH = re.compile(r"^/evidence/verify-run\(([A-Za-z0-9_-]+)\)(/.*)?$")


def validate_dependencies(schedule):
    """Unique nonempty ids; each dependency names an earlier entry exactly once."""
    seen = set()
    for index, spec in enumerate(schedule):
        name = spec.get("id") if isinstance(spec, dict) else None
        if not isinstance(name, str) or not name or name in seen:
            raise InvalidSchedule(f"entry {index}: unique nonempty id required")
        requires = spec.get("requires")
        if (
            not isinstance(requires, list)
            or not all(isinstance(r, str) for r in requires)
            or len(requires) != len(set(requires))
        ):
            raise InvalidSchedule(f"{name}: requires must list distinct entry ids")
        unknown = [r for r in requires if r not in seen]
        if unknown:
            raise InvalidSchedule(f"{name}: forward, cyclic or unknown dependency {unknown}")
        seen.add(name)


def unsupported_requirements(case, stop_delivery=False):
    """Static capability findings, not oracle judgments or execution results.

    stop_delivery=True: the caller runs the hooks with a stopper (real kill), so the frozen
    CAP-MIDPROCESS stop form (E11-P1-INJ) is deliverable. Without one it stays refused."""
    findings = []
    for i, spec in enumerate(case.get("injection_schedule", [])):
        op, payload, hook = spec["operation"], spec.get("payload", {}), spec["hook"]
        reason = None
        if op == "inject_metric" and not metered(payload):
            reason = "resource metric adapter unavailable"
        elif op == "request_transition" and "budget_override" in payload:
            reason = "budget-override request channel unavailable"
        elif op == "replace_role_output" and hook == "after_transition":
            reason = "stage event is not a shadow-policy output channel"
        elif op == "interrupt" and not (
            (hook == "after_transition" and payload.get("pending_operation") == "none")
            or (
                hook in {"after_transition", "after_role"}
                and "pending_operation" in payload
                and payload["pending_operation"] is None
                and payload.get("mode") == "harness_drop_model_connection"
            )
            or (stop_delivery and midprocess_stop_form(spec))
        ):
            reason = "midprocess crash adapter unavailable"
        elif op == "mutate_fixture" and payload["fixture_operation"] not in FIXTURE_OPERATIONS:
            reason = "fixture operation adapter unavailable: " + payload["fixture_operation"]
        elif op not in {
            "json_patch",
            "replace_role_output",
            "interrupt",
            "request_transition",
            "deny_write",
            "mutate_fixture",
            "inject_metric",
        }:
            reason = "unknown operation: " + op
        if reason:
            findings.append({"index": i, "reason": reason})
    return findings


def metered(payload):
    """Every reading in an inject_metric payload names a controller budget meter."""
    if not isinstance(payload, dict) or not isinstance(payload.get("also", {}), dict):
        return False
    names = {payload.get("metric"), *payload.get("also", {})}
    return names <= set(write_boundary.METERED) and payload.get("timing") in (None, "immediate")


def json_patch(value, operations):
    """Strict add/replace/remove/test subset; missing parents never materialize."""
    result = copy.deepcopy(value)
    if not isinstance(operations, list):
        raise UnsupportedInjection("JSON Patch must be an array")
    try:
        for op in operations:
            path = op["path"]
            if not isinstance(path, str) or not path.startswith("/"):
                raise UnsupportedInjection("non-root JSON pointer required")
            parts = [x.replace("~1", "/").replace("~0", "~") for x in path[1:].split("/")]
            target = result
            for part in parts[:-1]:
                target = target[int(part)] if isinstance(target, list) else target[part]
            key = parts[-1]
            action = op["op"]
            if isinstance(target, list):
                index = len(target) if key == "-" and action == "add" else int(key)
                if index < 0 or index > len(target) or (action != "add" and index == len(target)):
                    raise UnsupportedInjection("list index out of bounds")
                key = index
            elif not isinstance(target, dict):
                raise UnsupportedInjection("JSON pointer parent is not a container")
            if action == "test":
                if target[key] != op["value"]:
                    raise UnsupportedInjection("JSON Patch test failed")
            elif action == "remove":
                del target[key]
            elif action == "add":
                if isinstance(target, list):
                    target.insert(key, copy.deepcopy(op["value"]))
                else:
                    target[key] = copy.deepcopy(op["value"])
            elif action == "replace":
                target[key]  # require existing member
                target[key] = copy.deepcopy(op["value"])
            else:
                raise UnsupportedInjection(f"unsupported patch operation: {action}")
    except (KeyError, IndexError, ValueError, TypeError) as exc:
        raise UnsupportedInjection(f"patch does not match actual contract: {exc}") from exc
    return result


def replace_in_place(target, value):
    if isinstance(target, dict) and isinstance(value, dict):
        target.clear()
        target.update(copy.deepcopy(value))
    elif isinstance(target, list) and isinstance(value, list):
        target[:] = copy.deepcopy(value)
    else:
        raise UnsupportedInjection("replacement changes root type; hook cannot propagate it")


class InjectionHooks:
    def __init__(
        self, case, symbols=None, disposable_root=None, notification_boundary=False, stopper=None
    ):
        self._init_stop_delivery(stopper)
        self.case = copy.deepcopy(case)
        self.schedule = self.case.get("injection_schedule", [])
        self.symbols = dict(symbols or {})
        self.disposable_root = Path(disposable_root).resolve() if disposable_root else None
        self.records = []
        self.counts = Counter()
        self.done = set()
        self.resume_requested = False
        self.prepared = False
        # CAP-SCHEDULE-DEPENDENCIES: only schedules declaring `requires` use the dependent
        # dispatcher. Schedules without it keep the original dispatch unchanged.
        self.dependent = any(isinstance(s, dict) and "requires" in s for s in self.schedule)
        self.dispositions = {}
        self.accepted_approvals = 0
        self.initial_plan = None
        self.stopped = False
        self.harness_error = None
        self.fault = None  # a classified drop fault raised after the boundary scan
        self.transport_fault = None  # an armed FX-TRANSPORT-RETRY delivery record
        self.notifications = 0
        self.seen_notifications = set()
        known = KNOWN_HOOKS | ({NOTIFICATION_HOOK} if notification_boundary else set())
        if self.dependent:
            validate_dependencies(self.schedule)
            for index, spec in enumerate(self.schedule):
                if spec["hook"] not in known:
                    self.dispositions[index] = {
                        "disposition": "NOT_IMPLEMENTED",
                        "reason": "controller emits no boundary for hook " + spec["hook"],
                    }

    def coverage(self):
        result = {
            "scheduled": len(self.schedule),
            "applied": [r["index"] for r in self.records if r["status"] == "APPLIED"],
            "unsupported": [r for r in self.records if r["status"] == "UNSUPPORTED"],
            "errors": [r for r in self.records if r["status"] == "ERROR"],
            "unreached": [i for i in range(len(self.schedule)) if i not in self.done],
            "records": copy.deepcopy(self.records),
        }
        if self.dependent:
            result["dependencies"] = self.dependency_report()
        return result

    def dependency_report(self):
        """Per-entry DELIVERED / INACTIVE / NOT_IMPLEMENTED / HARNESS_ERROR dispositions.

        An entry whose boundary was never reached is INACTIVE (early stop), never a pass.
        """
        entries = []
        for index, spec in enumerate(self.schedule):
            decided = self.dispositions.get(index)
            if decided is None:
                reason = "dispatch_stopped" if self.stopped else "boundary_not_reached"
                reason = "deferred_after_stop" if self._is_deferred(index) else reason
                decided = {"disposition": "INACTIVE", "reason": reason}
            entries.append({"index": index, "id": spec["id"], "hook": spec["hook"], **decided})
        found = {e["disposition"] for e in entries}
        if self.harness_error:
            found.add("HARNESS_ERROR")
        status = next((s for s in ("HARNESS_ERROR", "NOT_IMPLEMENTED") if s in found), "OK")
        return {
            "capability": "CAP-SCHEDULE-DEPENDENCIES",
            "status": status,
            "harness_error": self.harness_error,
            "accepted_approvals": self.accepted_approvals,
            "entries": entries,
        }

    def resolve(self, value, controller):
        symbols = {**self.symbols}
        if controller is not None:
            for name, path in getattr(controller, "paths", {}).items():
                symbols[f"HARNESS_RESOLVED_ARTIFACT_PATH:{name}"] = str(path)
            if hasattr(controller, "goal"):
                symbols["HARNESS_GOAL_HASH:current"] = digest(controller.goal)
            if hasattr(controller, "root"):
                symbols["HARNESS_RUN_DIR"] = str(controller.root)
        if isinstance(value, dict):
            return {k: self.resolve(v, controller) for k, v in value.items()}
        if isinstance(value, list):
            return [self.resolve(v, controller) for v in value]
        if not isinstance(value, str):
            return value
        for token in SOURCE_SYMBOL.findall(value):
            if token not in symbols:
                symbols[token] = bound_source_hash(token, controller)
        for token in sorted(symbols, key=len, reverse=True):
            value = value.replace(token, str(symbols[token]))
        if re.search(r"HARNESS_[A-Z_]+(?::[A-Za-z0-9_-]+)?", value):
            raise UnsupportedInjection(f"unresolved trusted symbol: {value}")
        return value

    def prepare(self, request, source):
        """Apply declared before-run mutations before Controller captures its binding."""
        if self.prepared:
            raise UnsupportedInjection("prepare must run exactly once")
        self.prepared = True
        controller = SimpleNamespace(
            request=request, source=Path(source), paths={"source": Path(source)}
        )
        self._dispatch("before_run", {}, controller)
        self.records.sort(key=lambda r: r["index"])  # adopt_before_run entries: schedule order
        return request

    def __call__(self, point, event, controller):
        if point == "before_run" and self.prepared:
            return
        self._dispatch(point, event, controller)

    def _matches(self, selector, point, event, controller, count_point=None):
        name = selector["role_or_tool_or_stage"]
        if point == "before_run":
            return name in {"operator_ledger", "source_metadata", "harness"}
        if point in {"before_role", "after_role"}:
            role = event.get("role")
            if name.startswith("compiler_"):
                counted = self.counts[(count_point or point, "compiler")]
                return role == "compiler" and counted == int(name[-1])
            return name == role
        if point == NOTIFICATION_HOOK:
            return name == event.get("target")
        if point == "after_transition":
            return name == event.get("state")
        if point == "after_review_approval":
            return name == event.get("target")
        if point in {"before_tool", "after_tool"}:
            skill, _, suffix = name.partition("(")
            if skill != event.get("skill"):
                return False
            if suffix:
                artifact = suffix.rstrip(")")
                argv = event.get("argv", [])
                return "--input" in argv and str(controller.paths.get(artifact)) == str(
                    argv[argv.index("--input") + 1]
                )
            return True
        return False

    def _occurrence(self, selector, point, identity):
        # Count boundary occurrences once even when multiple operations share it.
        name = selector["role_or_tool_or_stage"]
        if name.startswith("compiler_"):
            return 1
        if "(" in name:
            return self.counts[("selector", point, name)] + 1
        return self.counts[(point, identity)]

    def _count_artifact_selectors(self, point, event, controller):
        for selector_name in {
            s["selector"]["role_or_tool_or_stage"]
            for s in self.schedule
            if s["hook"] == point and "(" in s["selector"]["role_or_tool_or_stage"]
        }:
            if self._matches({"role_or_tool_or_stage": selector_name}, point, event, controller):
                self.counts[("selector", point, selector_name)] += 1

    def _dispatch(self, point, event, controller):
        if self.dependent:
            return self._dispatch_dependent(point, event, controller)
        identity = event.get("role", event.get("skill", event.get("state", "before_run")))
        self.counts[(point, identity)] += 1
        interrupted = False
        for index, spec in enumerate(self.schedule):
            if index in self.done or spec["hook"] != point:
                continue
            selector = spec["selector"]
            if not self._matches(selector, point, event, controller):
                continue
            occurrence = self._occurrence(selector, point, identity)
            if occurrence != selector.get("occurrence", 1):
                continue
            if self._defer(index, spec, point, event, occurrence):
                continue
            record = {
                "index": index,
                "hook": point,
                "selector": selector,
                "operation": spec["operation"],
                "state": event.get("state"),
                "status": "ERROR",
            }
            self.records.append(record)
            self.done.add(index)
            try:
                payload = self.resolve(spec.get("payload"), controller)
                interrupted |= self._apply(
                    spec["operation"], payload, point, event, controller, record
                )
                record["status"] = "APPLIED"
            except UnsupportedInjection as exc:
                record.update(status="UNSUPPORTED", error=str(exc))
                raise
            except PermissionError as exc:
                record.update(status="APPLIED", effect="injected_fault", error=str(exc))
                raise
            except Exception as exc:
                record["error"] = str(exc)
                raise
        self._count_artifact_selectors(point, event, controller)
        if interrupted:
            self._raise_interruption()

    def _raise_interruption(self):
        fault, self.fault = self.fault, None
        if fault is not None:
            raise fault
        if self.stop_ready is not None:
            self._deliver_stop(self.stop_ready)  # never returns
        raise InjectedInterruption(
            "injected quiescent checkpoint interruption; resume requested="
            + str(self.resume_requested)
        )

    @staticmethod
    def _durable(controller):
        """The controller's durable checkpoint, or None when none can be observed."""
        root = getattr(controller, "root", None)
        if root is None or not (Path(root) / "checkpoint.json").is_file():
            return None
        try:
            return read_checkpoint(root)
        except (Refused, OSError, ValueError):
            return None

    def _observation(self, event, controller):
        """CR-03: the interrupt precondition is observed on the durable checkpoint."""
        if "checkpoint_durable" in event:
            return event
        durable = self._durable(controller)
        if durable is None:
            return event
        return {
            **event,
            "checkpoint_durable": True,
            "checkpoint_state": durable["state"],
            "pending_operation": durable["pending"],
        }

    @staticmethod
    def _commits_role_boundary(controller):
        """This controller follows after_role with a post-commit after_role_committed."""
        from .runner import Controller

        return isinstance(controller, Controller)

    def _disarm_transport_fault(self, point):
        armed = self.transport_fault
        if armed is None or armed["delivered"] or point == "before_role":
            return
        processes = armed["processes"]
        if vars(processes).get("run") is armed["wrapper"]:
            del processes.run
        armed["record"]["delivery"] = "UNDELIVERED"
        self.transport_fault = None

    def _decide(self, index, disposition, **details):
        self.dispositions[index] = {"disposition": disposition, **details}

    def _delivered(self, name):
        for index, spec in enumerate(self.schedule):
            if spec["id"] == name:
                return self.dispositions.get(index, {}).get("disposition") == "DELIVERED"
        return False

    def _in_plan_scope(self, selector, event):
        """initial = first accepted plan identity; child_of_initial needs a real parent plan.

        The identity is the plan (event ``plan_hash``, the controller's plan-version hash), not
        the binding: a material change that re-binds the same plan (a runtime-manifest bump)
        keeps it "initial" (frozen schedule contract after_review_approval.plan_scope)."""
        scope = selector.get("plan_scope")
        if scope is None:
            return True
        if scope != event.get("plan_scope"):
            return False
        plan = event.get("plan_hash")
        if scope == "initial":
            # An unobserved plan never matches; the first observed one defines "initial".
            if not isinstance(plan, str) or not plan:
                return False
            return self.initial_plan is None or plan == self.initial_plan
        if scope == "child_of_initial":
            return (
                isinstance(self.initial_plan, str)
                and bool(self.initial_plan)
                and event.get("parent_plan_hash") == self.initial_plan
                and isinstance(plan, str)
                and bool(plan)
                and plan != self.initial_plan
            )
        return False

    @staticmethod
    def _interrupt_observed(spec, event):
        # Durability and an explicitly null pending operation must be observed, never inferred.
        payload = spec.get("payload") or {}
        return (
            event.get("checkpoint_durable") is True
            and event.get("checkpoint_state") == payload.get("checkpoint_state")
            and "pending_operation" in event
            and event["pending_operation"] is None
        )

    def _dispatch_dependent(self, point, event, controller):
        """Scan each exact boundary once in listed order; decide each entry once, never replay."""
        if self.stopped:
            return
        self._disarm_transport_fault(point)
        notification = None
        if point == NOTIFICATION_HOOK:
            # 1-based over native notifications that are recorded AND emitted; a repeated
            # emission of the same native record is counted once.
            native = event.get("notification", {}).get("index")
            if event.get("recorded") is not True or event.get("emitted") is not True:
                return
            if native is not None and native in self.seen_notifications:
                return
            self.seen_notifications.add(native)
            self.notifications += 1
            notification = self.notifications
            if event.get("occurrence", notification) != notification:
                self.stopped = True
                self.harness_error = "notification occurrence disagrees with controller"
                raise HarnessDispatchError(self.harness_error)
        identity = event.get("role", event.get("skill", event.get("state", "before_run")))
        if point == NOTIFICATION_HOOK:
            identity = "operator_notification"
        self.counts[(point, identity)] += 1
        # CR-03: an after_role interrupt is decided at the post-commit role boundary.
        committed = point == "after_role_committed"
        hook_point = "after_role" if committed else point
        defer_role_interrupts = point == "after_role" and self._commits_role_boundary(controller)
        approval = None
        if point == "after_review_approval":
            if event.get("verdict") != "APPROVE" or not all(
                event.get(k) is True for k in APPROVAL_CONDITIONS
            ):
                return  # Not a controller-accepted approval: no boundary, no occurrence.
            self.accepted_approvals += 1
            approval = self.accepted_approvals
            if event.get("occurrence", approval) != approval:
                self.stopped = True
                self.harness_error = "accepted-approval occurrence disagrees with controller"
                raise HarnessDispatchError(self.harness_error)
            if approval == 1:
                self.initial_plan = event.get("plan_hash")
        interrupted = False
        for index, spec in enumerate(self.schedule):
            if index in self.dispositions or spec["hook"] != hook_point:
                continue
            is_interrupt = spec["operation"] == "interrupt"
            if (committed and not is_interrupt) or (defer_role_interrupts and is_interrupt):
                continue
            selector = spec["selector"]
            if not self._matches(selector, hook_point, event, controller, count_point=point):
                continue
            occurrence = approval or notification or self._occurrence(selector, point, identity)
            if occurrence != selector.get("occurrence", 1):
                continue
            if not self._in_plan_scope(selector, event):
                continue
            if self._defer(index, spec, point, event, occurrence):
                continue
            missing = [r for r in spec["requires"] if not self._delivered(r)]
            if missing:
                self._decide(
                    index,
                    "INACTIVE",
                    reason="dependency_undelivered",
                    missing=missing,
                    occurrence=occurrence,
                )
                continue
            if is_interrupt and not self._interrupt_observed(
                spec, self._observation(event, controller)
            ):
                reason = "interrupt_precondition_unobserved"
                self._decide(index, "INACTIVE", reason=reason, occurrence=occurrence)
                continue
            record = {
                "index": index,
                "id": spec["id"],
                "hook": point,
                "selector": selector,
                "operation": spec["operation"],
                "state": event.get("state"),
                "occurrence": occurrence,
                "status": "ERROR",
            }
            self.records.append(record)
            self.done.add(index)
            try:
                payload = self.resolve(spec.get("payload"), controller)
                interrupted |= self._apply(
                    spec["operation"], payload, point, event, controller, record
                )
                record["status"] = "APPLIED"
                self._decide(index, "DELIVERED", occurrence=occurrence)
            except UnsupportedInjection as exc:
                record.update(status="UNSUPPORTED", error=str(exc))
                self._decide(index, "NOT_IMPLEMENTED", reason=str(exc), occurrence=occurrence)
                self.stopped = True
                raise
            except PermissionError as exc:
                record.update(status="APPLIED", effect="injected_fault", error=str(exc))
                self._decide(index, "DELIVERED", occurrence=occurrence, effect="injected_fault")
                raise
            except Exception as exc:
                record["error"] = str(exc)
                self._decide(index, "HARNESS_ERROR", reason=str(exc), occurrence=occurrence)
                self.stopped = True
                raise
        self._count_artifact_selectors(point, event, controller)
        if interrupted:
            self._raise_interruption()

    def _apply(self, operation, payload, point, event, controller, record):
        if operation in {"json_patch", "replace_role_output"}:
            if point == "after_transition":
                raise UnsupportedInjection("stage event is not a shadow-policy output channel")
            if point == "before_run":
                target = controller.request
            elif point == "before_role":
                target = event["payload"]
            elif point == "after_role":
                target = event["answer"]
            elif point == "after_tool":
                process = event["process"]
                if isinstance(payload, dict) and payload.get("stream") == "stderr":
                    process["stderr"] += payload["append"]
                    return False
                try:
                    target = json.loads(process["stdout"])
                except (ValueError, KeyError) as exc:
                    raise UnsupportedInjection("tool stdout is not a gate JSON document") from exc
            else:
                raise UnsupportedInjection("no mutable payload at selected hook")
            if operation == "json_patch" and point == "before_role" and event["role"] == "verifier":
                payload = self._map_verifier_paths(payload, record)
            if operation == "json_patch":
                if (
                    point == "after_role"
                    and event["role"] == "planner"
                    and isinstance(target, dict)
                    and payload
                    and all(re.match(r"/\d+(/|$)", x["path"]) for x in payload)
                ):
                    replacement = copy.deepcopy(target)
                    replacement["steps"] = json_patch(target["steps"], payload)
                    record["adapter_mapping"] = "bare producing-plan array -> typed plan.steps"
                else:
                    replacement = json_patch(target, payload)
            else:
                replacement = payload
            record["before_hash"], record["after_hash"] = digest(target), digest(replacement)
            if point == "after_tool":
                process["stdout"] = json.dumps(replacement)
            else:
                replace_in_place(target, replacement)
            return False
        if operation == "interrupt":
            return self._interrupt(payload, point, event, controller, record)
        if operation == "request_transition":
            if "budget_override" in payload:
                raise UnsupportedInjection(
                    "resume budget-override request channel is not implemented"
                )
            if payload.get("action") == "resume" and "requested_by_role_channel" not in payload:
                self.resume_requested = True
                return False
            # D20 CP-E15: role-channel and non-resume requests go to the controller's own
            # transition interface; the controller records the result, never this adapter.
            submit = getattr(controller, "request_transition", None)
            if submit is None:
                raise UnsupportedInjection("controller exposes no transition request interface")
            observed = submit(payload)
            record["delivered_to"] = "controller.request_transition"
            record["controller_sequence"] = observed.get("sequence")
            return False
        if operation == "deny_write":
            return self._deny_write_routed(payload, point, event, controller, record)
        if operation == "inject_metric":
            return self._inject_metric(payload, controller, record)
        if operation == "mutate_fixture":
            if isinstance(payload, dict) and payload.get("fixture_operation") == (
                "FX-TRANSPORT-RETRY"
            ):
                self._arm_transport_fault(payload["args"], point, event, controller, record)
                return False
            self._fixture(payload, event, controller, record)
            return False
        raise UnsupportedInjection(f"operation not implemented: {operation}")

    def _interrupt(self, payload, point, event, controller, record):
        """Deliver an interrupt only at a durable, nothing-pending boundary (CR-03).

        The legacy E10/E11 literal 'none' and the schedule contract's null both mean the same
        observed condition; null additionally requires reading the durable checkpoint.
        With a stopper, harness_stop_controller is a real stop (_arm_stop / the CAP-MIDPROCESS
        before_tool form armed on the tool's own process_launch, _arm_midprocess_stop).
        """
        if point == "before_tool" and self.stopper is not None and midprocess_payload(payload):
            return self._arm_midprocess_stop(event, controller, record)
        if point not in {"after_transition", "after_role_committed"}:
            raise UnsupportedInjection(
                "midprocess hard-crash injection needs a process-start adapter"
            )
        durable = self._durable(controller)
        wanted = payload.get("checkpoint_state", event.get("state"))
        if payload.get("pending_operation") == "none":
            if durable is not None:
                if durable["pending"] is not None or durable["state"] != event.get("state"):
                    raise UnsupportedInjection("durable checkpoint still records pending work")
            elif getattr(controller, "pending", None):
                raise UnsupportedInjection(
                    "midprocess hard-crash injection needs a process-start adapter"
                )
        elif "pending_operation" in payload and payload["pending_operation"] is None:
            if durable is None or durable["pending"] is not None or durable["state"] != wanted:
                raise UnsupportedInjection(
                    "null pending_operation needs an observed durable checkpoint with nothing "
                    "pending at the scheduled state"
                )
        else:
            raise UnsupportedInjection(
                "midprocess hard-crash injection needs a process-start adapter"
            )
        if durable is not None:
            record["observed_checkpoint"] = {"state": durable["state"], "pending": None}
        if payload.get("mode") == "harness_drop_model_connection":
            fault = DROP_FAULTS.get(payload.get("failure_class"))
            if fault is None:
                raise UnsupportedInjection("unsupported model-connection failure class")
            record["effect"] = "model connection dropped at a durable nothing-pending boundary"
            record["failure_class"] = payload["failure_class"]
            code = errno.ECONNRESET if fault is InjectedConnectionDrop else errno.ETIMEDOUT
            self.fault = fault(code, "harness dropped the model connection")
            return True
        if self.stopper is not None and payload.get("mode") == STOP_MODE:
            return self._arm_stop(event, record)
        record["effect"] = "known quiescent interruption after durable transition"
        return True

    def _arm_transport_fault(self, args, point, event, controller, record):
        """FX-TRANSPORT-RETRY (CP-E13): the next transport call of this role raises one
        ConnectionResetError BEFORE launch. Never synthesizes an envelope or a retry."""
        if (
            point != "before_role"
            or args.get("role") != event.get("role")
            or args.get("failures") != 1
            or args.get("max_retries") != 1
            or args.get("before_write") is not True
            or args.get("failure_class") != "connection_reset"
        ):
            raise UnsupportedInjection("transport retry fault only before a role, pre-write, once")
        processes = getattr(getattr(controller, "roles", None), "processes", None)
        if processes is None or "run" in vars(processes) or self.transport_fault is not None:
            raise UnsupportedInjection("role transport unavailable or already wrapped")
        armed = {"processes": processes, "record": record, "delivered": False}

        def wrapper(*_args, **_kwargs):
            del processes.run  # exactly once: restore the real transport before raising
            armed["delivered"] = True
            record["delivery"] = "DELIVERED"
            self.transport_fault = None
            raise ConnectionResetError(errno.ECONNRESET, "harness pre-write transport reset")

        armed["wrapper"] = wrapper
        processes.run = wrapper
        self.transport_fault = armed
        record.update(
            effect="pre-write transport fault armed for the next role transport call",
            failure_class="connection_reset",
            before_write=True,
            delivery="ARMED",
        )

    @staticmethod
    def _map_verifier_paths(payload, record):
        if not isinstance(payload, list):
            return payload
        mapped, changed = [], False
        for op in payload:
            match = isinstance(op, dict) and isinstance(op.get("path"), str)
            match = match and VERIFIER_EVIDENCE_PATH.match(op["path"])
            if match:
                op = {**op, "path": "/gates/" + match.group(1) + (match.group(2) or "")}
                changed = True
            mapped.append(op)
        if changed:
            record["adapter_mapping"] = "/evidence/verify-run(<artifact>) -> /gates/<artifact>"
        return mapped

    @staticmethod
    def _deny_write(payload, point, event, controller, record):
        """FX-INJECT-PERMISSION-ERROR: arm the controller write boundary for one artifact.
        The fault fires when the controller commits that artifact, never at launch."""
        if not isinstance(payload, dict) or payload.get("fixture_operation") not in (
            None,
            "FX-INJECT-PERMISSION-ERROR",
        ):
            raise UnsupportedInjection("unsupported denial payload")
        args = payload.get("args") or {}
        artifact = args.get("artifact")
        if (
            point not in {"before_tool", "after_transition"}
            or args.get("denial_source") != "injected_fault"
            or args.get("errno", 13) != 13
            or not isinstance(args.get("message"), str)
            or artifact not in getattr(controller, "paths", {})
            or artifact == "source"
            or not hasattr(controller, "context")
        ):
            raise UnsupportedInjection("unsupported denial boundary")
        if point == "before_tool":
            argv = event.get("argv", [])
            output = argv[argv.index("--output") + 1] if "--output" in argv else None
            if output is None or str(controller.paths[artifact]) != str(output):
                raise UnsupportedInjection("selected tool does not write the denied artifact")
        try:
            write_boundary.boundary_for(controller).inject_denial(
                artifact, args.get("errno", 13), args["message"]
            )
        except ValueError as exc:
            raise UnsupportedInjection(str(exc)) from exc
        record.update(
            effect="armed controller write boundary",
            artifact=artifact,
            denial_source="injected_fault",
        )
        return False

    @staticmethod
    def _inject_metric(payload, controller, record):
        """CAP-METRIC budget half: deliver meter readings; the injection flag stays here."""
        if not metered(payload) or not hasattr(controller, "context"):
            raise UnsupportedInjection("metric reading channel unavailable for this payload")
        readings = {payload["metric"]: payload.get("value"), **payload.get("also", {})}
        for metric, value in readings.items():
            if type(value) not in (int, float):
                raise UnsupportedInjection(f"metric reading is not a number: {metric}")
        applied = [write_boundary.meter_reading(controller, m, v) for m, v in readings.items()]
        record.update(
            injected_reading=True,
            readings=[
                {"metric": r["metric"], "value": r["value"], "index": r["index"]} for r in applied
            ],
        )
        return False

    def _owned(self, path):
        path = Path(path)
        resolved = path.resolve()
        if self.disposable_root is None or not resolved.is_relative_to(self.disposable_root):
            raise UnsupportedInjection("fixture mutation requires a path inside disposable_root")
        if path.is_symlink() or any(p.is_symlink() for p in path.parents):
            raise UnsupportedInjection("linked fixture mutation refused")
        return resolved

    def _ledger_fixture(self, operation, args, event, controller, record):
        """Literal attributed records. Delivery confers no acceptance; the controller decides."""
        if operation == "FX-LEDGER-APPEND":
            if args.get("position") != "after_authorization" or args.get("speaker") not in {
                "assistant",
                "user",
            }:
                raise UnsupportedInjection("unsupported FX-LEDGER-APPEND position or speaker")
            if not isinstance(args.get("text"), str) or not args["text"]:
                raise UnsupportedInjection("FX-LEDGER-APPEND needs literal text")
            if record["hook"] == "before_run":
                request = controller.request
                # before_run (P0-REVIEW false_negatives): the request's attributed messages are
                # the native ledger before capture; the controller attributes them itself.
                messages = request["messages"]
                anchor = request["authorization"]["message_id"]
                position = next(i for i, m in enumerate(messages) if m["id"] == anchor) + 1
                ids = {m["id"] for m in messages}
                name = f"ledger-append-{record['index']}"
                if name in ids:
                    raise ValueError(f"duplicate ledger message id: {name}")
                message = {"id": name, "speaker": args["speaker"], "text": args["text"]}
                record["before_hash"] = digest(request)
                messages.insert(position, message)
                record["after_hash"] = digest(request)
                record.update(target="request.messages", message_id=name, assigned_fields=["id"])
                return
            appended = {
                "record_id": f"FX-LEDGER-APPEND:{record.get('id', record['index'])}",
                "speaker": args["speaker"],
                "text": args["text"],
            }
            assigned = ["record_id"]
        else:
            required = ("record_id", "speaker", "action", "run_id", "text")
            if set(args) != set(required) or any(
                not isinstance(args[k], str) or not args[k] for k in required
            ):
                raise UnsupportedInjection("FX-OPERATOR-RECORD needs the five literal fields")
            if args["speaker"] != "user" or args["action"] not in {"answer", "resume", "replan"}:
                raise UnsupportedInjection("FX-OPERATOR-RECORD speaker/action outside contract")
            appended, assigned = {k: args[k] for k in required}, []
        channel = getattr(controller, "context", {}).get("operator_channel")
        if channel is None:
            raise UnsupportedInjection("controller has no operator ledger channel")
        path = self._owned(channel["ledger_path"])
        if appended["record_id"] in read_record_ids(path):
            raise ValueError("duplicate operator ledger record_id: " + appended["record_id"])
        record["before_hash"] = digest(path.read_bytes().decode("utf-8"))
        record["record_hash"] = append_record(path, appended)
        record["after_hash"] = digest(path.read_bytes().decode("utf-8"))
        record.update(
            target="operator_ledger", record_id=appended["record_id"], assigned_fields=assigned
        )

    def _fixture(self, payload, event, controller, record):
        operation, args = payload["fixture_operation"], payload["args"]
        if operation == "FX-REPORT-RESERIALIZE":
            process = event.get("process")
            if process is None:
                raise UnsupportedInjection("report reserialization needs captured process stdout")
            original = process["stdout"]
            parsed = json.loads(original)
            process["stdout"] = json.dumps(dict(reversed(list(parsed.items()))), indent=2)
            record["json_value_equal"] = json.loads(process["stdout"]) == parsed
            record["bytes_differ"] = original != process["stdout"]
            if not record["bytes_differ"]:
                raise UnsupportedInjection("report reserialization did not change bytes")
            return
        if operation in {"FX-LEDGER-APPEND", "FX-OPERATOR-RECORD"}:
            self._ledger_fixture(operation, args, event, controller, record)
            return
        if operation == "FX-STDERR-APPEND":
            return self._stderr_append(args, event, record)
        if operation in {"FX-DEF-PARAM-EDIT", "FX-DEF-TOUCH", "FX-RUNTIME-MANIFEST-BUMP"}:
            try:
                return {
                    "FX-DEF-PARAM-EDIT": self._definition_edit,
                    "FX-DEF-TOUCH": self._definition_touch,
                    "FX-RUNTIME-MANIFEST-BUMP": self._manifest_bump,
                }[operation](args, controller, record)
            except UnsupportedInjection as exc:
                raise UnsupportedInjection(f"{operation}: {exc}") from exc
        if operation not in {"FX-SRC-META-ATTR", "FX-SRC-CELL-INCREMENT", "FX-ONSET-CELL-SHIFT"}:
            raise UnsupportedInjection(f"fixture operation not implemented: {operation}")
        import numpy as np
        import zarr

        path = self._owned(
            controller.paths["onset"] if operation == "FX-ONSET-CELL-SHIFT" else controller.source
        )
        record["before_hash"] = tree_hash(path)
        group = zarr.open_group(str(path), mode="r+")
        if operation == "FX-SRC-META-ATTR":
            group.attrs[args["attribute"]] = args["value"]
            zarr.consolidate_metadata(str(path))
        else:
            variable = args.get("variable", "onset_precip_date")
            if variable not in group:
                raise UnsupportedInjection(f"fixture variable missing: {variable}")
            array = group[variable]
            values = array[:]
            valid = np.isfinite(values)
            if operation == "FX-SRC-CELL-INCREMENT":
                valid &= values > 0
                amount = args["amount_mm"]
            else:
                units = array.attrs.get("units", "")
                if not units.startswith("days since "):
                    raise UnsupportedInjection("onset fixture encoded unit is not days")
                fill = array.fill_value
                if fill is not None:
                    valid &= values != fill
                amount = args["days"]
            selected = np.argwhere(valid)
            if not len(selected):
                raise UnsupportedInjection("fixture has no matching finite cell")
            index = tuple(int(v) for v in selected[0])
            array[index] = values[index] + amount
            record["cell_index"] = list(index)
        record["after_hash"] = tree_hash(path)
        if record["before_hash"] == record["after_hash"]:
            raise UnsupportedInjection("fixture mutation did not alter content")

    @staticmethod
    def _stderr_append(args, event, record):
        """FX-STDERR-APPEND: the literal becomes the last stderr line of this invocation."""
        process = event.get("process")
        if process is None or not isinstance(process.get("stderr"), str):
            raise UnsupportedInjection("stderr append needs a captured tool process")
        if args.get("tool") != event.get("skill") or not isinstance(args.get("text"), str):
            raise UnsupportedInjection("stderr append names another tool or no text")
        before = {"stdout": process.get("stdout"), "exit_code": process.get("exit_code")}
        record["before_hash"] = digest(process["stderr"])
        separator = "\n" if process["stderr"] and not process["stderr"].endswith("\n") else ""
        process["stderr"] += separator + args["text"]
        record["after_hash"] = digest(process["stderr"])
        if {"stdout": process.get("stdout"), "exit_code": process.get("exit_code")} != before:
            raise UnsupportedInjection("stderr append altered stdout or exit code")

    def _repo_file(self, controller, relative):
        repo = getattr(controller, "repo", None)
        if repo is None:
            raise UnsupportedInjection("no controller repository at this boundary")
        path = self._owned(Path(repo) / relative)
        if not path.is_file():
            raise UnsupportedInjection(f"fixture target missing: {relative}")
        return path

    def _definition_edit(self, args, controller, record):
        """FX-DEF-PARAM-EDIT on the disposable repo copy, one field, all others preserved."""
        if args.get("parameter") not in DEFINITION_PARAMETERS:
            raise UnsupportedInjection(f"unmapped definition parameter: {args.get('parameter')}")
        section, field = DEFINITION_PARAMETERS[args["parameter"]]
        ref = args.get("definition_ref")
        path = self._repo_file(controller, DEFINITIONS_FILE)
        text = path.read_bytes().decode("utf-8")
        parsed = tomllib.loads(text)
        try:
            current = parsed["definitions"][ref][section][field]
        except (KeyError, TypeError) as exc:
            raise UnsupportedInjection(
                f"definition field missing: {ref}.{section}.{field}"
            ) from exc
        if type(current) not in (int, float) or current != args.get("from"):
            raise UnsupportedInjection("definition field does not hold the declared from value")
        new = args.get("to")
        if type(new) not in (int, float) or new == current:
            raise UnsupportedInjection("definition edit does not change the value")
        new = float(new) if isinstance(current, float) else new
        lines = text.splitlines(keepends=True)
        header = f"[definitions.{ref}]"
        try:
            start = next(i for i, line in enumerate(lines) if line.strip() == header)
        except StopIteration as exc:
            raise UnsupportedInjection("definition table not in dotted-key form") from exc
        end = next(
            (i for i in range(start + 1, len(lines)) if lines[i].lstrip().startswith("[")),
            len(lines),
        )
        key = re.compile(
            rf"^(\s*{re.escape(section)}\.{re.escape(field)}\s*=\s*)([^#\r\n]*?)(\s*(#.*)?\r?\n?)$"
        )
        hits = [i for i in range(start + 1, end) if key.match(lines[i])]
        if len(hits) != 1:
            raise UnsupportedInjection("definition field is not one dotted-key line")
        match = key.match(lines[hits[0]])
        lines[hits[0]] = match.group(1) + repr(new) + match.group(3)
        edited = "".join(lines)
        expected = copy.deepcopy(parsed)
        expected["definitions"][ref][section][field] = new
        if tomllib.loads(edited) != expected:
            raise UnsupportedInjection("definition edit would change other fields")
        record["before_hash"] = tree_hash(path)
        path.write_bytes(edited.encode("utf-8"))
        record["after_hash"] = tree_hash(path)
        record["adapter_mapping"] = f"{args['parameter']} -> definitions.{ref}.{section}.{field}"
        if record["before_hash"] == record["after_hash"]:
            raise UnsupportedInjection("fixture mutation did not alter content")

    def _definition_touch(self, args, controller, record):
        """FX-DEF-TOUCH: last-write time advanced; content (and its hash) unchanged."""
        delta = args.get("mtime_delta_seconds")
        if type(delta) not in (int, float) or delta <= 0:
            raise UnsupportedInjection("touch needs a positive mtime_delta_seconds")
        path = self._repo_file(controller, DEFINITIONS_FILE)
        info = path.stat()
        record["before_hash"] = tree_hash(path)
        os.utime(path, ns=(info.st_atime_ns, info.st_mtime_ns + int(delta * 1_000_000_000)))
        record["after_hash"] = tree_hash(path)
        record["mtime_delta_seconds"] = (path.stat().st_mtime_ns - info.st_mtime_ns) / 1e9
        if record["before_hash"] != record["after_hash"]:
            raise RuntimeError("touch altered definition content")
        if record["mtime_delta_seconds"] < delta:
            raise UnsupportedInjection("filesystem did not advance the last-write time")

    def _manifest_bump(self, args, controller, record):
        """FX-RUNTIME-MANIFEST-BUMP: the runtime manifest's build_id on the disposable copy."""
        if args.get("field") != "build_id":
            raise UnsupportedInjection("only the runtime manifest build_id can be bumped")
        value = args.get("value")
        path = self._repo_file(controller, RUNTIME_MANIFEST)
        try:
            manifest = json.loads(path.read_text(encoding="utf-8"))
        except ValueError as exc:
            raise UnsupportedInjection("runtime manifest is not JSON") from exc
        if not isinstance(manifest, dict) or not isinstance(manifest.get("build_id"), str):
            raise UnsupportedInjection("runtime manifest has no build_id")
        if not isinstance(value, str) or not value or value == manifest["build_id"]:
            raise UnsupportedInjection("bump value must be a new nonempty build_id")
        record["before_hash"] = tree_hash(path)
        record["build_id_from"] = manifest["build_id"]
        path.write_text(
            json.dumps({**manifest, "build_id": value}, indent=2) + "\n", encoding="utf-8"
        )
        record["after_hash"] = tree_hash(path)
        record["build_id_to"] = value

    # ------------------------------------------------------------ wave 7: stop and resume
    # Interfaces (documented for the adapter's resume driver; see module STOP_SCHEMA /
    # STATE_SCHEMA and tools/workflow/tests/test_interrupt_delivery_v27.py):
    #
    # stopper(packet) -> NoReturn. Called once, at the stop boundary, by the hook itself. In the
    #   real route it sends ``packet`` to the parent, requests the kill and blocks forever (the
    #   process is killed). If it ever returns, the hook raises HarnessDispatchError: nothing
    #   may run past a harness stop. With stopper=None the old InjectedInterruption stays the
    #   unit-level double (and the mid-process form is refused).
    # packet = {"schema": STOP_SCHEMA, "stop": <the stop entry's hook record>,
    #           "deferred": [<deferred entry>...], "state": export_state()}
    # deferred entry = {"index", "id" (None without `requires`), "hook", "selector",
    #   "operation", "occurrence", "boundary_state", "stop_index"}: a schedule entry matched at
    #   the SAME boundary after the stop, NOT applied. The payload is not carried: it is read
    #   from the (digest-checked) schedule and resolved when apply_deferred delivers it.

    def _init_stop_delivery(self, stopper):
        if stopper is not None and not callable(stopper):
            raise TypeError("stopper must be callable")
        self.stopper = stopper
        self.deferred = []  # deferred entries, in schedule order; removed once applied
        self.stop_record = None  # the stop entry's record once a stop was armed or requested
        self.stop_ready = None  # an immediate stop awaiting the end of the boundary scan
        self.midprocess = None  # an armed CAP-MIDPROCESS trigger (transient, never exported)
        self.rebinds = []  # wrapper-owned rebinds round-tripped by export/import_state
        self._defer_boundary = None  # the event object of the boundary being deferred

    def _is_deferred(self, index):
        return any(entry["index"] == index for entry in self.deferred)

    def _defer(self, index, spec, point, event, occurrence):
        """Entries after a stop at the SAME boundary are deferred (returned), never dropped."""
        if self._is_deferred(index):
            return True
        if self._defer_boundary is None or self._defer_boundary is not event:
            return False
        self.deferred.append(
            {
                "index": index,
                "id": spec.get("id"),
                "hook": point,
                "selector": copy.deepcopy(spec["selector"]),
                "operation": spec["operation"],
                "occurrence": occurrence,
                "boundary_state": event.get("state"),
                "stop_index": self.stop_record["index"],
            }
        )
        return True

    def _arm_stop(self, event, record):
        """harness_stop_controller at a durable nothing-pending boundary, with a stopper.

        The preconditions were already checked by _interrupt; the stopper is called once the
        boundary scan has deferred the later entries (_raise_interruption)."""
        if self.stop_record is not None:
            raise UnsupportedInjection("one harness stop per controller incarnation")
        record.update(
            effect="harness stop requested after durable transition",
            mode=STOP_MODE,
            delivery="STOP_REQUESTED",
        )
        self.stop_record = self.stop_ready = record
        self._defer_boundary = event
        return True

    def _arm_midprocess_stop(self, event, controller, record):
        """CAP-MIDPROCESS (E11-P1-INJ): arm at before_tool, fire on that tool's own
        process_launch, after the start receipt is durable and before any exit receipt.

        The trigger wraps the controller's own process-event binding (runner
        bind_process_events): the controller's durable dispatch record, and every observer it
        chains, runs first; the stop follows. Pending work is left exactly as the controller
        recorded it. Only the first process_launch after arming is considered."""
        if self.stop_record is not None or self.midprocess is not None:
            raise UnsupportedInjection("one harness stop per controller incarnation")
        processes = getattr(controller, "processes", None)
        bound = getattr(processes, "on_event", None)
        if getattr(bound, "controller_owner", None) is not controller:
            raise UnsupportedInjection(
                "mid-process stop needs the controller's durable process-event record"
            )
        skill = event.get("skill")
        argv = [str(a) for a in event.get("argv", [])]
        armed = {"processes": processes, "bound": bound}

        def on_event(kind, details):
            bound(kind, details)  # the controller's durable record (and its chain) first
            if kind != "process_launch" or self.midprocess is not armed:
                return
            self._disarm_midprocess()
            launched = [str(a) for a in details.get("argv", [])]
            if details.get("tool") != skill or (argv and launched[-len(argv) :] != argv):
                record.update(delivery="UNDELIVERED", reason="first launch was another process")
                return
            receipt = Path(processes.root) / f"process-{int(details['serial']):03}.start.json"
            if not receipt.is_file():
                record.update(delivery="UNDELIVERED", reason="start receipt is not durable")
                return
            durable = self._durable(controller)
            record.update(
                delivery="STOP_REQUESTED",
                start_receipt={
                    "path": receipt.name,
                    "serial": details["serial"],
                    "pid": details.get("pid"),
                    "tool": skill,
                },
                observed_checkpoint=None
                if durable is None
                else {"state": durable["state"], "pending": durable["pending"]},
            )
            self._deliver_stop(record)

        armed["wrapper"] = on_event
        processes.on_event = on_event
        self.midprocess = armed
        record.update(
            effect="mid-process harness stop armed on the tool's own process_launch",
            mode=STOP_MODE,
            trigger="process_launch",
            tool=skill,
            delivery="ARMED",
        )
        self.stop_record = record
        self._defer_boundary = event
        return False

    def _disarm_midprocess(self):
        armed, self.midprocess = self.midprocess, None
        if armed is not None and armed["processes"].on_event is armed["wrapper"]:
            armed["processes"].on_event = armed["bound"]

    def _deliver_stop(self, record):
        """Hand the state packet to the stopper; nothing may run past this point."""
        self.stop_ready = None
        self._defer_boundary = None
        packet = {
            "schema": STOP_SCHEMA,
            "stop": copy.deepcopy(record),
            "deferred": copy.deepcopy(self.deferred),
            "state": self.export_state(),
        }
        self.stopper(packet)
        self.harness_error = "stopper returned; a harness stop must not return"
        raise HarnessDispatchError(self.harness_error)

    def export_state(self, rebinds=None):
        """JSON-serialisable hook state so a fresh process can continue the schedule.

        Format (STATE_SCHEMA): {"schema", "schedule_digest", "records", "done": [index...],
        "counts": [[key list, n]...], "dispositions": [[index, disposition dict]...],
        "deferred": [deferred entry...], "stop": stop record | None, "resume_requested",
        "prepared", "accepted_approvals", "initial_plan", "stopped", "harness_error",
        "notifications", "seen_notifications": [...], "rebinds": [...]}. ``rebinds`` defaults to
        self.rebinds (a wrapper such as role_substitution.CaseScheduleHooks passes its own).
        Refused while a transient fault or trigger is armed: those are closures, not state."""
        if self.fault is not None or self.transport_fault is not None or self.midprocess:
            raise HarnessDispatchError("cannot export hook state while a fault is armed")
        state = {
            "schema": STATE_SCHEMA,
            "schedule_digest": digest(self.schedule),
            "records": self.records,
            "done": sorted(self.done),
            "counts": sorted(([list(k), n] for k, n in self.counts.items()), key=_json_order),
            "dispositions": sorted([i, d] for i, d in self.dispositions.items()),
            "deferred": self.deferred,
            "stop": self.stop_record,
            "resume_requested": self.resume_requested,
            "prepared": self.prepared,
            "accepted_approvals": self.accepted_approvals,
            "initial_plan": self.initial_plan,
            "stopped": self.stopped,
            "harness_error": self.harness_error,
            "notifications": self.notifications,
            "seen_notifications": sorted(self.seen_notifications, key=_json_order),
            "rebinds": self.rebinds if rebinds is None else rebinds,
        }
        return json.loads(json.dumps(state, allow_nan=False))  # a TypeError is a harness bug

    def import_state(self, state):
        """Continue a schedule exported by export_state, in a fresh hooks object over the same
        case. Returns the rebinds list (a copy) for a wrapper to restore."""
        if not isinstance(state, dict) or state.get("schema") != STATE_SCHEMA:
            raise HarnessDispatchError("not an exported hook state")
        if state.get("schedule_digest") != digest(self.schedule):
            raise HarnessDispatchError("exported hook state belongs to another schedule")
        # Dispositions decided at construction (unknown hooks) are schedule facts, not state.
        if self.records or self.counts or self.done or self.prepared or self.deferred:
            raise HarnessDispatchError("hook state can only be imported into fresh hooks")
        state = json.loads(json.dumps(state))
        self.records = state["records"]
        self.done = set(state["done"])
        self.counts = Counter({tuple(k): n for k, n in state["counts"]})
        self.dispositions = {int(i): d for i, d in state["dispositions"]}
        self.deferred = state["deferred"]
        self.stop_record = state["stop"]
        self.resume_requested = state["resume_requested"]
        self.prepared = state["prepared"]
        self.accepted_approvals = state["accepted_approvals"]
        self.initial_plan = state["initial_plan"]
        self.stopped = state["stopped"]
        self.harness_error = state["harness_error"]
        self.notifications = state["notifications"]
        self.seen_notifications = set(state["seen_notifications"])
        self.rebinds = state["rebinds"]
        return copy.deepcopy(self.rebinds)

    def apply_deferred(self, controller, entries=None):
        """Deliver deferred entries (default: all, in schedule order) after the stop.

        ``controller`` is whatever exposes what the operation needs (the parent has no
        Controller yet: e.g. SimpleNamespace(paths=..., root=...) for FX-ONSET-CELL-SHIFT).
        Each entry is recorded like a dispatched one (plus ``deferred_after_stop``) and leaves
        self.deferred once decided. Returns copies of the new records. Delivery confers no
        acceptance: a resume request is still decided by the controller."""
        wanted = list(self.deferred) if entries is None else list(entries)
        for entry in wanted:
            if entry not in self.deferred:
                raise HarnessDispatchError(f"not a deferred entry: {entry.get('index')}")
        applied = []
        for entry in wanted:
            index = entry["index"]
            spec = self.schedule[index]
            self.deferred.remove(entry)
            if self.dependent:
                missing = [r for r in spec["requires"] if not self._delivered(r)]
                if missing:
                    reason = "dependency_undelivered"
                    occurrence = entry["occurrence"]
                    self._decide(
                        index, "INACTIVE", reason=reason, missing=missing, occurrence=occurrence
                    )
                    continue
            record = {
                "index": index,
                "hook": entry["hook"],
                "selector": copy.deepcopy(entry["selector"]),
                "operation": spec["operation"],
                "state": entry["boundary_state"],
                "occurrence": entry["occurrence"],
                "deferred_after_stop": entry["stop_index"],
                "status": "ERROR",
            }
            if self.dependent:
                record["id"] = spec["id"]
            self.records.append(record)
            self.done.add(index)
            applied.append(record)
            try:
                if spec["operation"] == "interrupt":
                    raise UnsupportedInjection("a second interrupt at a stop boundary")
                payload = self.resolve(spec.get("payload"), controller)
                event = {"state": entry["boundary_state"]}
                self._apply(spec["operation"], payload, entry["hook"], event, controller, record)
                record["status"] = "APPLIED"
                if self.dependent:
                    self._decide(index, "DELIVERED", occurrence=entry["occurrence"])
            except UnsupportedInjection as exc:
                record.update(status="UNSUPPORTED", error=str(exc))
                if self.dependent:
                    self._decide(index, "NOT_IMPLEMENTED", reason=str(exc))
                raise
            except Exception as exc:
                record["error"] = str(exc)
                if self.dependent:
                    self._decide(index, "HARNESS_ERROR", reason=str(exc))
                raise
        return copy.deepcopy(applied)

    def _deny_write_routed(self, payload, point, event, controller, record):
        """E14-P2-INJ (OI-06, by hook point): an after-AUTHORIZED denial whose frozen message
        names HARNESS_RUN_DIR hits the controller's pre-launch run-directory write; every other
        denial (E12-P1-INJ) keeps the commit-time path of _deny_write unchanged."""
        index = record.get("index")
        spec = self.schedule[index] if isinstance(index, int) else {}
        args = ((spec.get("payload") or {}).get("args") or {}) if spec else {}
        message = args.get("message") if spec.get("operation") == "deny_write" else None
        if (
            point == "after_transition"
            and event.get("state") == "AUTHORIZED"
            and isinstance(message, str)
            and RUN_DIR_SYMBOL.search(message)
        ):
            return self._deny_run_dir(payload, controller, record)
        return self._deny_write(payload, point, event, controller, record)

    @staticmethod
    def _deny_run_dir(payload, controller, record):
        """Arm the controller's pre-launch run-directory denial (sub-wave A API).

        Called as write_boundary.inject_run_dir_denial(controller, artifact=, errno=, message=)
        through a module attribute looked up at call time; refused when absent."""
        if not isinstance(payload, dict) or payload.get("fixture_operation") not in (
            None,
            "FX-INJECT-PERMISSION-ERROR",
        ):
            raise UnsupportedInjection("unsupported denial payload")
        args = payload.get("args") or {}
        artifact, root = args.get("artifact"), getattr(controller, "root", None)
        if (
            args.get("denial_source") != "injected_fault"
            or args.get("errno", 13) != 13
            or not isinstance(args.get("message"), str)
            or root is None
            or str(root) not in args["message"]
            or artifact not in getattr(controller, "paths", {})
            or artifact == "source"
            or not hasattr(controller, "context")
        ):
            raise UnsupportedInjection("unsupported run-directory denial boundary")
        inject = getattr(write_boundary, "inject_run_dir_denial", None)
        if inject is None:
            raise UnsupportedInjection(
                "controller exposes no pre-launch run-directory denial "
                "(write_boundary.inject_run_dir_denial)"
            )
        try:
            inject(
                controller, artifact=artifact, errno=args.get("errno", 13), message=args["message"]
            )
        except ValueError as exc:
            raise UnsupportedInjection(str(exc)) from exc
        record.update(
            effect="armed controller pre-launch run-directory denial",
            target="run_dir",
            artifact=artifact,
            denial_source="injected_fault",
            adapter_mapping="Permission denied: HARNESS_RUN_DIR -> pre-launch pending checkpoint",
        )
        return False

    def prestage_before_run(self, indices, source):
        """Fixture preparation: deliver exactly the before_run entries ``indices`` to the
        disposable source copy before the run starts (container case mode: untraced, before the
        trace baseline), on fresh hooks, through the same _dispatch path ``prepare`` uses with
        the same stand-in. The schedule's other before_run entries are masked for this one
        dispatch and stay for ``prepare`` in the run process.

        Returns the delivery state ``adopt_before_run`` installs (JSON): {schema,
        schedule_digest, indices, records, dispositions, stopped, harness_error, raised}. An
        entry that raises is recorded exactly as ``prepare`` records it and the exception is
        carried in ``raised``, for adopt_before_run to raise where prepare would have."""
        if self.records or self.counts or self.done or self.prepared:
            raise HarnessDispatchError("before_run prestaging needs fresh hooks")
        wanted = sorted(set(indices))
        before_run = [i for i, s in enumerate(self.schedule) if s["hook"] == "before_run"]
        if not wanted or any(
            i not in before_run or self.schedule[i]["operation"] != "mutate_fixture" for i in wanted
        ):
            raise HarnessDispatchError("only scheduled before_run mutate_fixture entries prestage")
        masked = [i for i in before_run if i not in wanted]
        masked_ids = {self.schedule[i].get("id") for i in masked} - {None}
        if any(masked_ids & set(self.schedule[i].get("requires") or []) for i in wanted):
            raise HarnessDispatchError("a prestaged entry requires an in-run before_run entry")
        unmask = [i for i in masked if i not in self.done and i not in self.dispositions]
        for index in unmask:
            self.done.add(index)
            if self.dependent:
                self.dispositions[index] = {"disposition": "MASKED"}
        path = Path(source)
        controller = SimpleNamespace(request=None, source=path, paths={"source": path})
        raised = None
        try:
            self._dispatch("before_run", {}, controller)
        except Exception as exc:  # noqa: BLE001 - carried, re-raised by adopt_before_run
            raised = {"type": type(exc).__name__, "message": str(exc)}
        finally:
            for index in unmask:
                self.done.discard(index)
                self.dispositions.pop(index, None)
        for record in self.records:
            record["prestaged"] = True
        state = {
            "schema": PRESTAGE_SCHEMA,
            "schedule_digest": digest(self.schedule),
            "indices": wanted,
            "records": self.records,
            "dispositions": sorted([i, d] for i, d in self.dispositions.items() if i in wanted),
            "stopped": self.stopped,
            "harness_error": self.harness_error,
            "raised": raised,
        }
        return json.loads(json.dumps(state, allow_nan=False))

    def adopt_before_run(self, state):
        """Install a prestage_before_run delivery state on these hooks before ``prepare``: the
        prestaged entries are done (their records and dispositions as delivered), so prepare
        dispatches only the remaining before_run entries and keeps schedule order. A
        prestaged entry that raised is raised here, where prepare would have raised it."""
        if not isinstance(state, dict) or state.get("schema") != PRESTAGE_SCHEMA:
            raise HarnessDispatchError("not a before_run prestage state")
        if state.get("schedule_digest") != digest(self.schedule):
            raise HarnessDispatchError("before_run prestage state belongs to another schedule")
        if self.records or self.counts or self.done or self.prepared:
            raise HarnessDispatchError("a before_run prestage state is adopted into fresh hooks")
        state = json.loads(json.dumps(state))
        indices = set(state["indices"])
        if {r["index"] for r in state["records"]} - indices:
            raise HarnessDispatchError("before_run prestage records outside its indices")
        self.records = state["records"]
        self.done = {r["index"] for r in self.records}
        self.dispositions.update({int(i): d for i, d in state["dispositions"]})
        self.stopped = state["stopped"]
        self.harness_error = state["harness_error"]
        raised = state["raised"]
        if raised is not None:
            kind = {"UnsupportedInjection": UnsupportedInjection, "PermissionError": PermissionError}
            if raised["type"] in kind:
                raise kind[raised["type"]](raised["message"])
            raise HarnessDispatchError(f"{raised['type']}: {raised['message']}")


STOP_MODE = "harness_stop_controller"
STOP_SCHEMA = "e2e-injections.stop/1"
STATE_SCHEMA = "e2e-injections.state/1"
PRESTAGE_SCHEMA = "e2e-injections.before-run-prestage/1"
RUN_DIR_SYMBOL = re.compile(r"HARNESS_RUN_DIR(?![A-Za-z0-9_:])")


def _json_order(value):
    return json.dumps(value, sort_keys=True)


def midprocess_payload(payload):
    """The frozen CAP-MIDPROCESS stop (E11-P1-INJ): after the tool's start receipt is recorded
    and before any exit receipt. Other deferred forms (E12-P2's metric) are not this."""
    if not isinstance(payload, dict):
        return False
    when = payload.get("when")
    return (
        payload.get("mode") == STOP_MODE
        and isinstance(when, str)
        and "start receipt is recorded" in when
        and "before any exit receipt" in when
        and "CAP-MIDPROCESS" in when
    )


def midprocess_stop_form(spec):
    """Static: a before_tool interrupt in the frozen mid-process form."""
    return (
        spec.get("hook") == "before_tool"
        and spec.get("operation") == "interrupt"
        and midprocess_payload(spec.get("payload"))
    )
