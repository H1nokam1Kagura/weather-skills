"""Durable synthetic-only execution; no production capability is implied by this module."""

import copy
import json
import time
import uuid
from pathlib import Path

from tools.workflow.catalog import safe_path, tree_hash
from tools.workflow.contracts import Refused, digest
from tools.workflow.persistence import atomic_json, run_lock

from .contracts import SyntheticGrant, build_identity, plan, request_hash, require, validate
from .science import Skills, calculate, inspect


class Paused(Exception):
    pass


class WorkerDied(RuntimeError):
    """A backend-observed worker failure, not an error inferred from model prose."""


class SyntheticRoles:
    """Explicit role substitutions. These receipts are never evidence of live independence."""

    mode = "substituted"

    def __init__(self, request):
        self.request = copy.deepcopy(request)
        self.receipts = []

    def ask(self, role, payload):
        self.receipts.append({"role": role, "session_id": str(uuid.uuid4()), "mode": self.mode})
        if role == "compiler":
            return copy.deepcopy(self.request)
        if role == "planner":
            return plan(self.request)
        return {
            "verdict": "APPROVE" if role == "reviewer" else "PASS",
            "binding_hash": payload["binding_hash"],
        }


class Controller:
    """Use only with a trusted synthetic grant and explicitly substituted roles.

    Tools run synchronously in-process with cooperative pause boundaries. Mid-tool OS
    interruption/containment and remote acquisition are NOT implemented by this harness.
    The production onset controller and frozen SIMKIT v2.7 adapter remain unchanged.
    """

    def __init__(self, repo, request, grant, root, roles, backend=None, on_operation=None):
        self.repo, self.root = safe_path(repo), safe_path(root)
        self.request = validate(copy.deepcopy(request))
        require(
            type(grant) is SyntheticGrant,
            "production execution is not enabled; synthetic grant required",
        )
        require(
            isinstance(roles, SyntheticRoles),
            "only explicit synthetic role substitutions are enabled",
        )
        self.grant, self.roles = grant, roles
        grant.verify()
        self.sources = grant.record
        require(
            set(self.sources) == {p["id"] for p in request["products"]},
            "source ledger does not cover every product",
        )
        for records in self.sources.values():
            if "unavailable" in records:
                continue
            for value in records.values():
                source = Path(value["path"])
                require(
                    not self.root.is_relative_to(source) and not source.is_relative_to(self.root),
                    "source and output namespaces must be disjoint",
                )
        self.backend = backend or Skills(self.repo)
        self.on_operation = on_operation
        self.build = build_identity(self.repo)
        self.binding = digest(
            {
                "request": request_hash(request),
                "sources": grant.record,
                "build": self.build,
                "plan": plan(request),
            }
        )
        self.running = False
        if self.root.exists():
            value = json.loads((self.root / "checkpoint.json").read_text(encoding="utf-8"))
            require(digest(value["payload"]) == value["digest"], "checkpoint was changed")
            self.state = value["payload"]
            require(
                self.state["binding_hash"] == self.binding,
                "request, sources or runtime changed on resume",
            )
            self.verify_completed()
        else:
            self.root.mkdir(parents=True)
            self.state = {
                "schema": "verification.checkpoint/1",
                "binding_hash": self.binding,
                "profile": request["profile"],
                "status": "CAPTURED",
                "elapsed_seconds": 0,
                "gates_complete": False,
                "production_release": False,
                "active_since": None,
                "pause_requested": False,
                "pending": None,
                "events": [],
                "role_receipts": [],
                "probes": {},
                "ledger": [
                    dict(p, status="PENDING", attempts=0, failures=0) for p in plan(request)
                ],
            }
            self.save()

    def save(self):
        if self.running:
            self.state["elapsed_seconds"] = self.elapsed_base + time.perf_counter() - self.started
        atomic_json(
            self.root / "checkpoint.json", {"payload": self.state, "digest": digest(self.state)}
        )

    def event(self, kind, **data):
        self.state["events"].append({"sequence": len(self.state["events"]), "kind": kind, **data})
        self.save()

    def pause(self):
        """Publish intent without overwriting the active owner's checkpoint."""
        # Unique immutable records let concurrent callers publish without a shared .tmp
        # file. Only run(), under its lock, consumes intent and writes the checkpoint.
        intent = {"binding_hash": self.binding, "id": str(uuid.uuid4()), "action": "pause"}
        atomic_json(self.root / f"operator-pause-{intent['id']}.json", intent)

    def pending_pauses(self):
        consumed = set(self.state.get("consumed_pauses", []))
        pending = []
        for path in sorted(self.root.glob("operator-pause-*.json")):
            intent = json.loads(path.read_text(encoding="utf-8"))
            require(
                intent.get("binding_hash") == self.binding
                and intent.get("action") == "pause"
                and path.name == f"operator-pause-{intent.get('id')}.json",
                "invalid operator intent",
            )
            if intent["id"] not in consumed:
                pending.append(intent["id"])
        return pending

    def before_operation(self):
        if self.on_operation:
            self.on_operation(self)
        pending = self.pending_pauses()
        if pending:
            self.state["pause_requested"] = True
            self.state.setdefault("consumed_pauses", []).extend(pending)
            self.event("operator_pause", intents=pending, boundary="between_synchronous_operations")
        if self.state["pause_requested"]:
            raise Paused()
        if (
            self.elapsed_base + time.perf_counter() - self.started
            >= self.request["limits"]["max_seconds"]
        ):
            raise Refused("aggregate run time limit exhausted", gate="resource_guard")

    def ask(self, role, payload):
        self.before_operation()
        answer = self.roles.ask(role, copy.deepcopy(payload))
        require(bool(self.roles.receipts), "missing role receipt")
        receipt = self.roles.receipts[-1]
        previous = self.state["role_receipts"]
        require(
            receipt["role"] == role
            and receipt["mode"] == "substituted"
            and receipt["session_id"] not in {p["session_id"] for p in previous},
            "invalid role invocation receipt",
        )
        previous.append(copy.deepcopy(receipt))
        evidence = {"role": role, "input": payload, "output": answer, "receipt": receipt}
        path = self.root / f"role-{len(previous):03}.json"
        atomic_json(path, evidence)
        self.state.setdefault("role_artifacts", []).append(
            {"name": path.name, "hash": tree_hash(path)}
        )
        self.save()
        return answer

    def verify_completed(self):
        for record in self.state.get("role_artifacts", []):
            require(Path(record["name"]).name == record["name"], "invalid role evidence path")
            require(
                tree_hash(self.root / record["name"]) == record["hash"], "role evidence changed"
            )
        for index, entry in enumerate(self.state["ledger"]):
            if entry["status"] != "VERIFIED":
                continue
            expected = self.root / f"product-{index}-attempt-{entry['attempts']}"
            require(entry["artifact"] == str(expected), "checkpoint artifact path changed")
            require(tree_hash(expected) == entry["artifact_hash"], "completed artifact changed")
            replay = self.root / f"replay-{index}-attempt-{entry['attempts']}"
            require(tree_hash(replay) == entry["replay_hash"], "replay evidence changed")

    def gates(self):
        if self.state["gates_complete"]:
            return
        for _ in range(3):
            require(
                self.ask("compiler", {"request": self.request}) == self.request,
                "compiler changed the request",
            )
        require(
            self.ask("planner", {"request": self.request}) == plan(self.request),
            "planner changed the registered steps",
        )
        answer = self.ask(
            "reviewer",
            {"binding_hash": self.binding, "request": self.request, "plan": plan(self.request)},
        )
        require(
            answer == {"verdict": "APPROVE", "binding_hash": self.binding},
            "review did not approve this exact binding",
        )
        self.state["status"] = "APPROVED"
        self.state["gates_complete"] = True
        self.event("review_approved", binding_hash=self.binding)

    def run(self, *, resume=False):
        with run_lock(self.root):
            # Reload under the lock so two constructed instances cannot overwrite a newer run.
            stored = json.loads((self.root / "checkpoint.json").read_text(encoding="utf-8"))
            require(digest(stored["payload"]) == stored["digest"], "checkpoint was changed")
            require(stored["payload"]["binding_hash"] == self.binding, "checkpoint binding changed")
            self.state = stored["payload"]
            if self.state.get("active_since") is not None:
                self.state["elapsed_seconds"] = max(
                    self.state["elapsed_seconds"],
                    self.state.get("elapsed_base", 0)
                    + max(0, time.time() - self.state["active_since"]),
                )
                self.state.update(status="INTERRUPTED", active_since=None)
                self.event("unclean_interruption", uncertain_output_reusable=False)
            elif self.state["status"] in ("RUNNING", "APPROVED"):
                self.state["status"] = "INTERRUPTED"
                self.event("unclean_interruption", uncertain_output_reusable=False)
            self.grant.verify()
            require(build_identity(self.repo) == self.build, "implementation changed")
            self.verify_completed()
            if self.state["status"] in ("SYNTHETIC_COMPLETE", "SYNTHETIC_PARTIAL"):
                return copy.deepcopy(self.state)
            if self.state["status"] in ("PAUSED", "INTERRUPTED", "BLOCKED"):
                if not resume:
                    return copy.deepcopy(self.state)
                # A budget exhaustion or permanent failed gate is not made retryable by resume.
                require(
                    self.state["status"] != "BLOCKED", "blocked run requires a new reviewed request"
                )
                self.state["pause_requested"] = False
                self.state.setdefault("consumed_pauses", []).extend(self.pending_pauses())
                self.event("operator_resume", source_artifact_runtime_checks="PASS")
            self.elapsed_base = self.state["elapsed_seconds"]
            self.started, self.running = time.perf_counter(), True
            self.state.update(active_since=time.time(), elapsed_base=self.elapsed_base)
            self.save()
            try:
                self.before_operation()
                self.gates()
                self.state["status"] = "RUNNING"
                products = {p["id"]: p for p in self.request["products"]}
                cached = {}
                for index, entry in enumerate(self.state["ledger"]):
                    if entry["status"] in ("VERIFIED", "UNAVAILABLE"):
                        continue
                    self.before_operation()
                    records = self.sources[entry["product"]]
                    if "unavailable" in records:
                        entry.update(status="UNAVAILABLE", reason=records["unavailable"])
                        self.event(
                            "unavailable",
                            product=entry["product"],
                            window_days=entry["window_days"],
                        )
                        continue
                    product = products[entry["product"]]
                    if entry["product"] not in cached:
                        self.grant.verify()
                        datasets, probe = inspect(self.request, product, records)
                        cached.clear()  # retain only one initialization's bounded decoded sources
                        cached[entry["product"]] = datasets
                        if entry["product"] not in self.state["probes"]:
                            self.state["probes"][entry["product"]] = probe
                            self.event("bounded_probe", product=entry["product"], probe=probe)
                        # A recorded probe precedes every dependent score; no archive loop is implicit.
                    while entry["status"] != "VERIFIED":
                        self.before_operation()
                        entry["attempts"] += 1
                        directory = self.root / f"product-{index}-attempt-{entry['attempts']}"
                        replay_dir = self.root / f"replay-{index}-attempt-{entry['attempts']}"
                        entry["status"] = "RUNNING"
                        self.state["pending"] = {"index": index, "attempt": entry["attempts"]}
                        self.event("operation_pending", **self.state["pending"])
                        try:
                            report = calculate(
                                self.request,
                                product,
                                entry["window_days"],
                                cached[entry["product"]],
                                directory,
                                self.backend,
                                self.before_operation,
                            )
                            replay = calculate(
                                self.request,
                                product,
                                entry["window_days"],
                                cached[entry["product"]],
                                replay_dir,
                                Skills(self.repo),
                                self.before_operation,
                            )
                            require(
                                report["metrics"] == replay["metrics"]
                                and report["interval_end_exclusive"]
                                == replay["interval_end_exclusive"],
                                "score replay differs",
                            )
                            self.grant.verify()
                            entry.update(
                                status="VERIFIED",
                                artifact=str(directory),
                                artifact_hash=tree_hash(directory),
                                replay_hash=tree_hash(replay_dir),
                                scores=report["metrics"],
                            )
                            self.state["pending"] = None
                            self.event(
                                "product_verified",
                                index=index,
                                replay="paired score replay from registered sources",
                            )
                        except WorkerDied as exc:
                            entry["failures"] += 1
                            self.event(
                                "worker_failure",
                                index=index,
                                failure=type(exc).__name__,
                                failed_output=str(directory),
                            )
                            if entry["failures"] > 1:
                                raise Refused(
                                    "bounded worker retry exhausted", gate="repair_exhausted"
                                ) from exc
                            self.grant.verify()
                            self.event("retry", index=index, new_output_required=True)
                self.verify_completed()
                final_binding = digest({"binding": self.binding, "ledger": self.state["ledger"]})
                answer = self.ask(
                    "verifier", {"binding_hash": final_binding, "ledger": self.state["ledger"]}
                )
                require(
                    answer == {"verdict": "PASS", "binding_hash": final_binding},
                    "final verifier refused",
                )
                self.before_operation()
                complete = all(
                    e["status"] == "VERIFIED"
                    and all(m["uncertainty"]["status"] == "ESTIMATED" for m in e["scores"].values())
                    for e in self.state["ledger"]
                )
                self.state["status"] = "SYNTHETIC_COMPLETE" if complete else "SYNTHETIC_PARTIAL"
                self.state["production_release"] = False
                self.state["limitations"] = [
                    "Substituted roles, not independent live-agent evidence",
                    "Cached synthetic acquisition only; no remote GEFS throughput claim",
                    "Cooperative pause between synchronous operations; no mid-tool OS containment",
                    "No crop maturity or harvest-safety assessment",
                ]
            except Paused:
                self.state["status"] = "PAUSED"
                self.event("paused", pending=self.state["pending"])
            except Refused as exc:
                self.state.update(status="BLOCKED", reason=str(exc))
                self.event("blocked", reason=str(exc))
            except BaseException as exc:
                self.state.update(status="INTERRUPTED", reason=type(exc).__name__)
                self.event("interrupted", reason=type(exc).__name__)
                raise
            finally:
                self.state["elapsed_seconds"] = (
                    self.elapsed_base + time.perf_counter() - self.started
                )
                self.state["active_since"] = None
                self.running = False
                self.save()
            return copy.deepcopy(self.state)
