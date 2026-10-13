"""Trusted, fail-closed DEV injection adapters; never an agent capability.

Records distinguish an applied stimulus from an unimplemented or unreached one.
The caller owns oracle evaluation and live/fixture role selection.
"""

import copy
import errno
import json
import re
from collections import Counter
from pathlib import Path
from types import SimpleNamespace

from .catalog import tree_hash
from .contracts import Refused, digest
from .persistence import read_checkpoint


class UnsupportedInjection(RuntimeError):
    """The declared stimulus cannot faithfully reach this implementation."""


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


def unsupported_requirements(case):
    """Static capability findings, not oracle judgments or execution results."""
    findings = []
    for i, spec in enumerate(case.get("injection_schedule", [])):
        op, payload, hook = spec["operation"], spec.get("payload", {}), spec["hook"]
        reason = None
        if op == "inject_metric":
            reason = "resource/budget metric adapter unavailable"
        elif op == "request_transition" and (
            payload.get("action") != "resume" or "budget_override" in payload
        ):
            reason = "transition or budget-override request channel unavailable"
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
        ):
            reason = "midprocess crash adapter unavailable"
        elif op == "mutate_fixture" and payload["fixture_operation"] not in {
            "FX-REPORT-RESERIALIZE",
            "FX-SRC-META-ATTR",
            "FX-SRC-CELL-INCREMENT",
            "FX-ONSET-CELL-SHIFT",
            "FX-TRANSPORT-RETRY",
        }:
            reason = "fixture operation adapter unavailable: " + payload["fixture_operation"]
        elif op not in {
            "json_patch",
            "replace_role_output",
            "interrupt",
            "request_transition",
            "deny_write",
            "mutate_fixture",
        }:
            reason = "unknown operation: " + op
        if reason:
            findings.append({"index": i, "reason": reason})
    return findings


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
    def __init__(self, case, symbols=None, disposable_root=None, notification_boundary=False):
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
        self.initial_binding = None
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
        """initial = first accepted plan identity; child_of_initial needs a real parent binding."""
        scope = selector.get("plan_scope")
        if scope is None:
            return True
        if scope != event.get("plan_scope"):
            return False
        binding = event.get("binding_hash")
        if scope == "initial":
            # An unobserved binding never matches; the first observed one defines "initial".
            if not isinstance(binding, str) or not binding:
                return False
            return self.initial_binding is None or binding == self.initial_binding
        if scope == "child_of_initial":
            return (
                isinstance(self.initial_binding, str)
                and bool(self.initial_binding)
                and event.get("parent_binding_hash") == self.initial_binding
                and isinstance(binding, str)
                and bool(binding)
                and binding != self.initial_binding
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
                self.initial_binding = event.get("binding_hash")
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
            if payload.get("action") == "resume":
                if "budget_override" in payload:
                    raise UnsupportedInjection(
                        "resume budget-override request channel is not implemented"
                    )
                self.resume_requested = True
                return False
            raise UnsupportedInjection("role transition request channel is not implemented")
        if operation == "deny_write":
            args = payload["args"]
            if point != "before_tool" or args.get("denial_source") != "injected_fault":
                raise UnsupportedInjection("unsupported denial boundary")
            raise PermissionError(args.get("errno", 13), args["message"])
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
        """
        if point not in {"after_transition", "after_role_committed"}:
            raise UnsupportedInjection("midprocess hard-crash injection needs a process-start adapter")
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

    def _owned(self, path):
        path = Path(path)
        resolved = path.resolve()
        if self.disposable_root is None or not resolved.is_relative_to(self.disposable_root):
            raise UnsupportedInjection("fixture mutation requires a path inside disposable_root")
        if path.is_symlink() or any(p.is_symlink() for p in path.parents):
            raise UnsupportedInjection("linked fixture mutation refused")
        return resolved

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
