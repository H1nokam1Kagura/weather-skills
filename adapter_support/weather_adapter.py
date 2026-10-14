"""Observed evidence projection and a gated run_case lifecycle (P8).

A capability is declared only when its method exists AND, for the capabilities this package
adds, CAPABILITY-PRODUCERS.json holds a reviewed capability-to-producer record (owner ruling
D4). run_case is opt-in and refuses any case needing an undeclared capability.
"""

import copy
import json
import re
from pathlib import Path

from simkit.adapter import Adapter

from adapter_support import (
    evidence_projection,
    pre_review_projection,
    role_substitution,
    shadow_projection,
)
from adapter_support import fixture_rereview_projection as fixture_ops
from adapter_support.fixture_staging import stage_fixture
from tools.check_capability_signatures import rereview_flagged
from tools.workflow import review_evidence
from tools.workflow.catalog import safe_path, tree_hash
from tools.workflow.contracts import FIELDS, digest, plan_version_hash, request_goal
from tools.workflow.e2e_injections import InjectionHooks
from tools.workflow.persistence import atomic_json, read_checkpoint
from tools.workflow.profiles import PROFILES, stages
from tools.workflow.runner import Controller

SOURCE_HASH_ALGORITHM = "tools.workflow.catalog.tree_hash"
REPORT_HASH_ALGORITHM = "tools.workflow.review_evidence.text_hash"
SUPPORTED_REPAIR_LIMITS = (0, 1)
PRODUCERS_FILE = Path(__file__).with_name("CAPABILITY-PRODUCERS.json")
# run_case's in-process harness inputs, persisted for the post-trace re-projection.
PROJECTION_INPUTS_SCHEMA = "weather-adapter.evidence-projection-inputs/1"
PROJECTION_INPUTS_FILE = "evidence-projection-inputs.json"
FIXTURE_SCHEMA = "weather-adapter.disposable-fixture/1"
# CAP-LEDGER-SEED quote rule (harness-authored, flagged for review): every non-null goal value
# is attributed to the whole scenario message; a null value is attributed to it only for a
# field the scenario states as a prohibition, else seeded explicitly unattributed (CR-02).
PROHIBITION_FIELDS = ("baseline",)
# CAP-SYMBOLS closed table: exact tokens, or a prefix whose suffix is resolved natively.
SYMBOL_TABLE = (
    "HARNESS_RUN_DIR",
    "HARNESS_SOURCE_CACHE",
    "HARNESS_REQUEST_HASH",
    "HARNESS_REQUEST_HASH:current",
    "HARNESS_MANIFEST_HASH:current",
    "HARNESS_GOAL_HASH:current",
    "HARNESS_GOAL_HASH:authorized",
    "HARNESS_PLAN_HASH:current",
    "HARNESS_HASH:source",
    "HARNESS_HASH:source_pre",
    "HARNESS_HASH:source_expected_after",
    "HARNESS_CONTROLLER_PID",
    "HARNESS_RESOLVED_ARTIFACT_PATH:onset",
    "HARNESS_RESOLVED_ARTIFACT_PATH:map",
    "HARNESS_SOURCE_HASH:" + review_evidence.DEFINITION_SOURCE_ID,
)
SYMBOL_PATTERNS = (
    re.compile(r"HARNESS_PLAN_HASH:v[1-9][0-9]*"),
    re.compile(r"HARNESS_PID:[a-z-]+"),
)


EvidenceMappingError = evidence_projection.EvidenceMappingError


class AdapterRefusal(ValueError):
    """A case input this adapter cannot faithfully apply; never guessed or filled."""


def load_producers(path=PRODUCERS_FILE):
    return json.loads(Path(path).read_text(encoding="utf-8"))


class WeatherAdapter(Adapter):
    name = "weather-observed-review-diagnostic"

    # Extend introspection for frozen capabilities absent from base Simkit's method table.
    # A manifest string is never an implementation; D4-gated ones also need a reviewed record.
    extension_methods = {
        "CAP-BOUND-SOURCES": "capture_bound_sources",
        "CAP-RUN-PROVENANCE": "tag_run_provenance",
        "CAP-SCHEDULE-DEPENDENCIES": "dispatch_dependent_schedule",
        "CAP-REVIEW-APPROVAL-HOOK": "dispatch_after_accepted_review",
        "CAP-EVIDENCE-CHECKER": "invoke_evidence_checker",
        "CAP-PRE-REVIEW-GATES": "record_claim_checks",
        "CAP-OPERATOR-ACTIONS": "record_operator_actions",
        "CAP-REREVIEW": "record_material_changes",
    }

    def __init__(self, enable_run_case=False, producers=None):
        # run_case is opt-in: constructing the adapter never makes cases runnable.
        self.enable_run_case = enable_run_case is True
        self.producers = load_producers() if producers is None else producers

    def d4_declared(self, capability):
        """Owner ruling D4: a reviewed capability-to-producer record, status PRODUCER_MAPPED.
        A signature is a reviewer, a review time and the commit the review read; a name alone
        does not declare (a later code change needs a re-review and a new pin). A record that
        carries a non-empty rereview_required is undeclared until the flag is removed with a
        new signature (D4 re-review G-1); tools/check_capability_signatures.py fails CI when a
        signed record's producer files changed after its reviewed_commit without the flag."""
        record = self.producers["capabilities"].get(capability)
        return (
            isinstance(record, dict)
            and record.get("status") == "PRODUCER_MAPPED"
            and not rereview_flagged(record)
            and all(
                isinstance(record.get(key), str) and bool(record[key].strip())
                for key in ("reviewer", "reviewed_at", "reviewed_commit")
            )
        )

    def implemented_capabilities(self):
        implemented = super().implemented_capabilities()
        for capability, method_name in self.extension_methods.items():
            if callable(getattr(type(self), method_name, None)):
                implemented.add(capability)
        gated = {c for c, r in self.producers["capabilities"].items() if r.get("d4_gated") is True}
        return {c for c in implemented if c not in gated or self.d4_declared(c)}

    def can_run(self):
        if type(self).run_case is not WeatherAdapter.run_case:
            return super().can_run()  # a subclass's own run_case is its own declaration
        return self.enable_run_case

    # ---- P8: evidence projection (CAP-RECEIPTS and the capabilities it carries)

    def capture_evidence(self, run_dir, harness=None):
        """CAP-RECEIPTS: the full projection {evidence, missing, notes} of a finished run."""
        return evidence_projection.project_run(run_dir, harness)

    def resolve_symbol(self, symbol, projection):
        """CAP-SYMBOLS: closed-table resolution from a projection's native symbol values.

        An unknown token is refused; a known token the run did not observe is unresolved
        (refused), never defaulted, and never read from role output.
        """
        known = isinstance(symbol, str) and (
            symbol in SYMBOL_TABLE or any(p.fullmatch(symbol) for p in SYMBOL_PATTERNS)
        )
        if not known:
            raise EvidenceMappingError(f"symbol outside the closed table: {symbol!r}")
        symbols = projection["evidence"]["symbols"]
        if symbol not in symbols:
            raise EvidenceMappingError(f"symbol not observed in this run: {symbol}")
        return symbols[symbol]

    def map_terminal_reason(self, terminal):
        """CAP-REASON-MAP: the controller terminal; an unmapped gate is left out, not guessed."""
        notes = []
        mapped = evidence_projection.map_terminal(terminal, notes)
        return {"terminal": mapped, "notes": notes}

    def map_role_output(self, run_dir, harness=None):
        """CAP-ROLE-OUTPUT-SCHEMA: role_output in the contract shape (plan.steps -> plan,
        plan_claims -> claims, compiler occurrence -> compiler_N, verifier D8 verdict)."""
        return self.capture_evidence(run_dir, harness)["evidence"]["role_output"]

    def capture_bound_sources(self, run_dir, harness=None):
        """CAP-BOUND-SOURCES (D1): one review snapshot per reviewer receipt; failed dispatches
        are reported separately and never become snapshots or receipts."""
        projection = self.capture_evidence(run_dir, harness)
        observations = projection["evidence"]["harness_observations"]
        if "review_snapshots" not in observations:
            raise EvidenceMappingError("review evidence mode was not observed")
        return {
            "review_snapshots": observations["review_snapshots"],
            "review_dispatch_failures": observations["review_dispatch_failures"],
        }

    def invoke_evidence_checker(self, run_dir, harness=None):
        """CAP-EVIDENCE-CHECKER: the controller invokes the separate evidence_checker role
        itself (Controller.claim_check -> ask, after gate 1, before the reviewer). This
        returns its receipts, structured per-claim outputs and observed placement, or MISSING
        when claim gates were not enabled. Live or substituted output arrives through the
        role transport / after_role hooks, never through this method."""
        projection = self.capture_evidence(run_dir, harness)
        return pre_review_projection.evidence_checker_report(
            projection["evidence"], projection["missing"]
        )

    def record_claim_checks(self, run_dir, harness=None):
        """CAP-PRE-REVIEW-GATES: controller_trace.claim_checks (reviewer_occurrence as the
        receipt ordinal) and their event keys, or MISSING. citation_gate/citation_verdict are
        controller labels, never evidence (v2.5 UC-14-resolution)."""
        projection = self.capture_evidence(run_dir, harness)
        trace = projection["evidence"]["controller_trace"]
        if "claim_checks" not in trace:
            return {"status": "MISSING", "reason": self._reason(projection, "claim_checks")}
        prefix = "controller_trace.claim_checks["
        return {
            "claim_checks": trace["claim_checks"],
            "event_seq": {
                k: v for k, v in projection["evidence"]["event_seq"].items() if k.startswith(prefix)
            },
            "missing": [m for m in projection["missing"] if m["path"].startswith(prefix)],
        }

    def record_operator_actions(self, run_dir, harness=None):
        """CAP-OPERATOR-ACTIONS: every operator action the controller accepted or rejected,
        with source and speaker, and their event keys; MISSING when no channel was opened."""
        projection = self.capture_evidence(run_dir, harness)
        trace = projection["evidence"]["controller_trace"]
        if "operator_actions" not in trace:
            return {"status": "MISSING", "reason": self._reason(projection, "operator_actions")}
        prefix = "controller_trace.operator_actions["
        return {
            "operator_actions": trace["operator_actions"],
            "event_seq": {
                k: v for k, v in projection["evidence"]["event_seq"].items() if k.startswith(prefix)
            },
            "missing": [m for m in projection["missing"] if m["path"].startswith(prefix)],
        }

    @staticmethod
    def _reason(projection, name):
        path = f"controller_trace.{name}"
        return "; ".join(m["reason"] for m in projection["missing"] if m["path"] == path)

    def observe_harness(self, run_dir, harness=None):
        """CAP-HARNESS-OBS: the controller's launch checks plus the whole-tree fields the
        container harness observed (``harness["observations_path"]``): max_concurrent_children,
        shell_launches, data_fetch_attempts (trace) and min_free_ram_while_running_gib (harness
        RAM sampler). A field not observed is listed in ``missing`` with its reason, never 0."""
        projection = self.capture_evidence(run_dir, harness)
        observations = projection["evidence"]["harness_observations"]
        out = {"launch_checks": projection["evidence"]["budget_ledger"]["launch_checks"]}
        for key in (
            "max_concurrent_children",
            "shell_launches",
            "data_fetch_attempts",
            "min_free_ram_while_running_gib",
        ):
            if key in observations:
                out[key] = observations[key]
        out["missing"] = [m for m in projection["missing"] if m["capability"] == "CAP-HARNESS-OBS"]
        return out

    def assert_exhaustive_write_capture(self, run_dir, harness=None):
        """CAP-EXHAUSTIVE-WRITES: {status EXHAUSTIVE, created_paths, ...} only when the container
        harness trace is complete (tracer_status OK, no trace-derived field missing) and every
        present artifact is among the created paths; else {status MISSING, reason} with the
        harness's reason. This is what licenses empty_is_exhaustive (EVIDENCE_CONTRACT
        filesystem_snapshot); an empty list is never exhaustive by itself (D4)."""
        projection = self.capture_evidence(run_dir, harness)
        status = next(n["write_capture"] for n in projection["notes"] if "write_capture" in n)
        if status["status"] == "EXHAUSTIVE":
            status = {
                **status,
                "created_paths": projection["evidence"]["filesystem_snapshot"]["created_paths"],
            }
        return status

    def capture_signal_targets(self, run_dir, harness=None):
        """CAP-SIGNAL-CAPTURE: the container harness trace when ``harness["observations_path"]``
        is supplied (checked against the controller's own launches and signal_stop records),
        else native signal_stop records; absence is never []."""
        projection = self.capture_evidence(run_dir, harness)
        evidence = projection["evidence"]
        if "signalled_pids" not in evidence["controller_trace"]:
            reasons = [
                m["reason"]
                for m in projection["missing"]
                if m["path"] == "controller_trace.signalled_pids"
            ]
            return {
                "status": "MISSING",
                "reason": "; ".join(reasons) or "no signal_stop record (D4)",
            }
        out = {"signalled_pids": evidence["controller_trace"]["signalled_pids"]}
        for key, value in (
            (
                "signal_capture_exhaustive",
                evidence["harness_observations"].get("signal_capture_exhaustive"),
            ),
            ("process_tree", evidence.get("process_tree")),
        ):
            if value is not None:
                out[key] = value
        return out

    def tag_run_provenance(self, run_dir, harness_dir, markers, tags=None):
        """CAP-RUN-PROVENANCE (D3): harness tags plus a scan of what the system read.
        ``tags``: the output-tag record when it was not written to harness/output-tags.json."""
        return evidence_projection.run_provenance(run_dir, harness_dir, markers, tags=tags)

    def select_profile(self, case, profile_document):
        """CAP-PROFILE: the case's profile variant, checked against the controller's own."""
        variants = (profile_document or {}).get("variants", {})
        variant = variants.get(case.get("profile")) if isinstance(case, dict) else None
        if not isinstance(variant, dict):
            raise AdapterRefusal("case profile is not a variant of the supplied profile")
        task = variant["goal"].get("task")
        if PROFILES.get(task, {}).get("id") != case["profile"] or stages(task) != variant["states"]:
            raise AdapterRefusal("profile variant differs from the controller's registered profile")
        return copy.deepcopy(variant)

    def seed_ledger(self, case, variant, source_ledger):
        """CAP-LEDGER-SEED: workflow.request/1 from the source ledger plus case.request_delta.

        The constraint values come from the variant goal; quotes follow PROHIBITION_FIELDS and
        are harness-authored (recorded in the derivation, flagged for review). The controller's
        own request_goal validates the result; nothing is coerced (a null title stays null).
        """
        ledger = copy.deepcopy(source_ledger)
        for op in case.get("request_delta") or []:
            if op.get("op") != "replace" or op.get("path") != "/scenario_text":
                raise AdapterRefusal(
                    "only a replace of /scenario_text is a supported request delta"
                )
            ledger["scenario_text"] = op["value"]
        if ledger.get("authorization_speaker") != "user":
            raise AdapterRefusal("only a user-speaker authorization can be seeded")
        goal, text = variant["goal"], ledger["scenario_text"]
        if set(goal) != FIELDS:
            raise AdapterRefusal("variant goal does not carry the controller goal fields")
        constraints, unattributed = {}, []
        for field in sorted(FIELDS):
            value = goal[field]
            if value is None and field not in PROHIBITION_FIELDS:
                constraints[field] = {"value": None, "message_id": None, "quote": None}
                unattributed.append(field)
            else:
                constraints[field] = {"value": value, "message_id": "scenario", "quote": text}
        request = {
            "schema": "workflow.request/1",
            "messages": [
                {"id": "scenario", "speaker": ledger["scenario_speaker"], "text": text},
                {"id": "user", "speaker": "user", "text": ledger["authorization_text"]},
            ],
            "constraints": constraints,
            "authorization": {
                "message_id": "user",
                "quote": ledger["authorization_text"],
                "goal_hash": digest(goal),
                "actions": [PROFILES[goal["task"]]["action"]],
            },
        }
        if request_goal(request) != goal:
            raise AdapterRefusal("seeded request does not compile to the variant goal")
        return {
            "request": request,
            "derivation": {
                "quote_rule": "whole scenario message per attributed field; "
                f"prohibition fields {list(PROHIBITION_FIELDS)}",
                "unattributed_null_fields": unattributed,
                "request_delta_applied": len(case.get("request_delta") or []),
                "flagged_for_review": True,
            },
            "ledger_records": [
                {"id": m["id"], "speaker": m["speaker"], "content_hash": digest(m["text"])}
                for m in request["messages"]
            ],
        }

    def run_case(self, case, profile):
        """CAP-NOT-IMPLEMENTED lifecycle (opt-in). Refuses before any side effect unless every
        capability the case requires is declared; then stages the owned fixture, seeds the
        ledger, runs the real controller in dispatch + review evidence mode, and returns the
        projected evidence. The projection report is persisted beside the harness manifest.

        ``profile`` is the harness mapping: profile_document, source_ledger, case_root,
        source, generator_provenance, presented_fetch_version, definition_source, repo,
        roles_factory(run_dir), processes_factory(run_dir), input_supplement, and optionally:

        * prepared_fixture: the prepare_fixture record of a fixture the harness already
          staged (container case mode stages it untraced, before the trace starts); it is
          re-verified (adopt_prepared_fixture) and nothing is staged here.
        * projection_inputs_channel: a harness-opened text stream. With it, run_case creates
          NO file outside the run directory: the seeded ledger, output tags, projection inputs
          and the in-process projection report leave as one record on the channel
          (persist_projection_inputs) instead of harness/*.json. Frozen v2.7 confines
          filesystem_snapshot.created_paths to HARNESS_RUN_DIR (all_under, every case).

        Without either (in-process route) everything is staged and written as before.
        """
        if not self.enable_run_case:
            raise AdapterRefusal("run_case is disabled; construct with enable_run_case=True")
        if not isinstance(case, dict) or not isinstance(case.get("adapter_requirements"), list):
            raise AdapterRefusal("a case with adapter_requirements is required")
        missing = sorted(set(case["adapter_requirements"]) - self.implemented_capabilities())
        if missing:
            raise AdapterRefusal(f"case requires undeclared capabilities: {missing}")
        variant = self.select_profile(case, profile["profile_document"])
        # CAP-SHADOW: invocation points, vocabulary and the shadow decision entries (delivered
        # by shadow_projection, not InjectionHooks), mapped or refused before any side effect.
        shadow_plan = self.drive_shadow_policy(case)
        if shadow_plan["configured"] and "CAP-SHADOW" not in self.implemented_capabilities():
            raise AdapterRefusal("the case configures the shadow policy; CAP-SHADOW is undeclared")
        scheduled = shadow_projection.schedule_without_shadow(case, shadow_plan)
        # The schedule is mapped or refused here, before any side effect (CAP-ROLE-SUBSTITUTE).
        role_substitution.refuse_unsupported_schedule(scheduled)
        fixture_plan = self.apply_fixture_operation(scheduled, profile["repo"])
        schedule_plan = self.substitute_role_output(scheduled, variant["goal"])
        max_repairs = self.configure_repair(case)
        seeded = self.seed_ledger(case, variant, profile["source_ledger"])
        channel = profile.get("projection_inputs_channel")
        if profile.get("prepared_fixture") is not None:
            prepared = self.adopt_prepared_fixture(
                case, profile["prepared_fixture"], profile.get("case_root")
            )
        else:
            prepared = self.prepare_fixture(
                case,
                profile["case_root"],
                source=profile["source"],
                generator_provenance=profile["generator_provenance"],
                presented_fetch_version=profile["presented_fetch_version"],
            )
        run_dir, harness_dir = Path(prepared["run"]), Path(prepared["harness"])
        # A copy: a before_run FX-LEDGER-APPEND edits seeded["request"] in place (prepare).
        harness_files = {"seeded-ledger.json": copy.deepcopy(seeded)}
        repo, repo_copy = fixture_ops.controller_repo(fixture_plan, profile, prepared)
        if channel is None:
            atomic_json(harness_dir / "seeded-ledger.json", seeded)
        # The controller creates and owns this run-scoped operator ledger (D14/D28); the
        # harness only appends literal records to it (FX-LEDGER-APPEND). It lives in the run
        # directory: the controller creates it, and frozen v2.7 confines created_paths there.
        operator_ledger = run_dir / "operator-ledger.jsonl"
        expected_after, expected_reason = pre_review_projection.expected_source_after(
            case, prepared["source_pre"]["value"]
        )
        processes = profile["processes_factory"](run_dir)
        roles = profile["roles_factory"](run_dir)
        # The case schedule runs chained behind the launch observer, whose records stay intact.
        schedule_hooks = role_substitution.case_hooks(schedule_plan, roles, prepared["case_root"])
        shadow_driver = shadow_projection.attach(shadow_plan, roles)
        observer = evidence_projection.HarnessLaunchObserver(
            chained=shadow_projection.chain(schedule_hooks, shadow_driver)
        )
        if schedule_hooks is not None:
            schedule_hooks.prepare(seeded["request"], prepared["source"])
        controller = Controller(
            repo,
            seeded["request"],
            prepared["source"],
            run_dir,
            processes,
            roles,
            max_repairs=max_repairs,
            hooks=observer,
            review_evidence_mode=True,
            dispatch_evidence_mode=True,
            definition_source=profile["definition_source"],
            operator_ledger_path=operator_ledger,
            policy_mode=shadow_projection.policy_mode(shadow_plan),
        )
        try:
            controller.run()
        except Exception:  # noqa: BLE001 - the controller recorded its own terminal
            pass
        artifacts = [v for k, v in controller.paths.items() if k != "source"]
        tags = evidence_projection.tag_outputs(
            run_dir, harness_dir, artifacts, write=channel is None
        )
        harness_files["output-tags.json"] = tags
        provenance_record = prepared["manifest"]["generator_provenance"]
        markers = [provenance_record["generator"], provenance_record["generator_sha256"]]
        processes_root = getattr(processes, "root", None)
        harness_inputs = {
            "launch_observations": observer.records,
            "processes_root": None if processes_root is None else str(processes_root),
            "source_pre": prepared["source_pre"]["value"],
            "authorized_goal": variant["goal"],
            "run_provenance": self.tag_run_provenance(run_dir, harness_dir, markers, tags=tags),
            "source_expected_after_reason": expected_reason,
        }
        if expected_after is not None:
            harness_inputs["source_expected_after"] = expected_after
        fixture_record = None
        if fixture_plan["entries"]:
            fixture_record = fixture_ops.fixture_report(
                fixture_plan, schedule_hooks, prepared["source_pre"]["value"]
            )
            fixture_ops.harness_inputs_from(fixture_record, harness_inputs)
            expected_after = harness_inputs.get("source_expected_after")
            expected_reason = harness_inputs["source_expected_after_reason"]
        summary = {
            "case_id": case["id"],
            "input_supplement": profile.get("input_supplement"),
            "source_pre": prepared["source_pre"],
            "seeded_ledger": seeded["derivation"],
            "operator_ledger": str(operator_ledger),
            "source_expected_after": {"value": expected_after, "reason": expected_reason},
            "fixture_staged": "pre-staged" if profile.get("prepared_fixture") else "run_case",
        }
        if fixture_record is not None:
            summary["fixture_operations"] = fixture_record
            summary["repo_copy"] = repo_copy
        if schedule_hooks is not None:
            summary["case_schedule"] = schedule_hooks.report()
        if shadow_driver is not None:
            summary["shadow_schedule"] = shadow_driver.report()
        if channel is None:
            # Persisted before projecting, so a post-trace re-projection has them even when
            # the in-process projection refuses.
            self.persist_projection_inputs(case, run_dir, harness_dir, harness_inputs)
            projection = self.capture_evidence(run_dir, harness_inputs)
            projection["harness"] = summary
            atomic_json(harness_dir / "evidence-projection.json", projection)
            return projection["evidence"]
        # Channel route: one record, written whatever the in-process projection does.
        report = None
        try:
            projection = self.capture_evidence(run_dir, harness_inputs)
            projection["harness"] = summary
            report = projection
            return projection["evidence"]
        except Exception as exc:
            report = {"status": "ERROR", "error": repr(exc), "harness": summary}
            raise
        finally:
            harness_files["evidence-projection.json"] = report
            self.persist_projection_inputs(
                case, run_dir, harness_dir, harness_inputs, channel, harness_files=harness_files
            )

    def adopt_prepared_fixture(self, case, prepared, case_root=None):
        """CAP-DISPOSABLE-FIXTURE, pre-staged: accept a prepare_fixture record the harness
        produced earlier (untraced), after re-verifying it. Nothing is created: the layout,
        manifest and staged-source hash must still be exactly what staging recorded, and the
        run directory must exist and be empty. Any difference is a refusal, never a re-stage.
        """
        if not isinstance(prepared, dict) or prepared.get("schema") != FIXTURE_SCHEMA:
            raise AdapterRefusal("prepared fixture has an unknown schema")
        if prepared.get("case_id") != case.get("id"):
            raise AdapterRefusal("prepared fixture belongs to another case")
        root = safe_path(Path(prepared["case_root"]))
        if case_root is not None and safe_path(Path(case_root)) != root:
            raise AdapterRefusal("prepared fixture is not at the profile's case_root")
        layout = {
            "source": root / "source" / "source.zarr",
            "source_directory": root / "source",
            "harness": root / "harness",
            "run": root / "run",
        }
        for key, path in layout.items():
            if safe_path(Path(prepared[key])) != safe_path(path):
                raise AdapterRefusal(f"prepared fixture layout differs: {key}")
        if not layout["run"].is_dir() or any(layout["run"].iterdir()):
            raise AdapterRefusal("prepared run directory must exist and be empty")
        if list(layout["source_directory"].iterdir()) != [layout["source"]]:
            raise AdapterRefusal("prepared source directory must hold only the staged source")
        try:
            manifest = json.loads((layout["harness"] / "manifest.json").read_text("utf-8"))
        except (OSError, ValueError) as exc:
            raise AdapterRefusal("prepared harness manifest is unreadable") from exc
        if manifest != prepared.get("manifest"):
            raise AdapterRefusal("prepared harness manifest differs from the staging record")
        source_pre = prepared.get("source_pre") or {}
        if source_pre.get("algorithm") != SOURCE_HASH_ALGORITHM or tree_hash(
            layout["source"]
        ) != source_pre.get("value"):
            raise AdapterRefusal("staged source changed since staging")
        return copy.deepcopy(prepared)

    def persist_projection_inputs(
        self, case, run_dir, harness_dir, inputs, channel=None, harness_files=None
    ):
        """The in-process harness inputs of run_case, for the post-trace re-projection
        (container_harness/project_in_container.py --inputs).

        ``channel`` (profile["projection_inputs_channel"]): a writable text stream the harness
        opened outside the traced tree (the container entry shell's fd 4 onto /harness-out).
        Writing to an inherited descriptor creates no path, so the record does not appear in
        filesystem_snapshot.created_paths, whose frozen allow-list is HARNESS_RUN_DIR
        (all_under, every v2.7 case); the harness/ directory is outside it. Without a channel
        (in-process route) the record is written atomically to
        harness/evidence-projection-inputs.json. ``harness_files`` (channel route only): the
        harness bookkeeping that would otherwise be harness/<name> files, carried verbatim
        under ``harness_files`` (seeded-ledger.json, output-tags.json,
        evidence-projection.json).
        """
        record = {
            "schema": PROJECTION_INPUTS_SCHEMA,
            "case_id": case["id"],
            "run_dir": str(run_dir),
            **inputs,
        }
        if harness_files is not None:
            record["harness_files"] = harness_files
        if channel is not None:
            channel.write(json.dumps(record, allow_nan=False))
            channel.flush()
            return "channel"
        atomic_json(Path(harness_dir) / PROJECTION_INPUTS_FILE, record)
        return str(Path(harness_dir) / PROJECTION_INPUTS_FILE)

    def apply_fixture_operation(self, case, repo=None):
        """CAP-FIXTURE-OP: every scheduled fixture operation checked (known operation, complete
        frozen arguments, a target the delivery path reaches), or AdapterRefusal before any side
        effect. Delivery is InjectionHooks at the controller's own hook points (run_case);
        definition/runtime-manifest edits run on a disposable repository copy."""
        return fixture_ops.fixture_plan(case, repo)

    def record_material_changes(self, run_dir, harness=None):
        """CAP-REREVIEW: controller_trace.material_changes / source_rechecks and the replacing
        active approval with their event keys, each checked against the controller's files and
        dispatch events; MISSING (listed in ``missing``) for an unfinished run."""
        return fixture_ops.rereview_report(self.capture_evidence(run_dir, harness))

    def substitute_role_output(self, case, goal=None):
        """CAP-ROLE-SUBSTITUTE: the case's after_role role-output entries mapped onto the
        native role outputs (with a dry run against the substituted baseline when ``goal`` is
        given), or AdapterRefusal. Delivery is CaseScheduleHooks at the controller's own
        after_role emit; the substituted invocation keeps its receipt and session ID."""
        return role_substitution.substitution_plan(case, goal)

    def drive_shadow_policy(self, case):
        """CAP-SHADOW: the shadow policy's invocation points and per-state candidate vocabulary
        for the case's profile (the controller's own policy.SHADOW_STATES and
        shadow_candidates), the policy mode run_case sets, and the case's shadow decision
        entries mapped for delivery at after_transition, or AdapterRefusal. The controller
        asks, validates and records every decision itself (tools/workflow/shadow.py)."""
        return shadow_projection.shadow_plan(case)

    def substituted_roles_factory(self, case, goal, run_dir):
        """Driver hook (container_harness/case_driver.py): the Controller roles for a case
        whose agent_roles are all substituted. ``run_dir`` is the controller run directory;
        the double writes nothing there (the controller records every receipt itself)."""
        self.substitute_role_output(case, goal)
        return role_substitution.SubstitutedRoles(case, goal)

    def diagnostic_capability_status(self):
        """Status of the DIRECT diagnostic path (capture_review_diagnostics) only.

        That path reads a live Controller and projects review snapshots without the dispatch
        clock, so it never supplies the full capabilities. Their status as capabilities is the
        CAPABILITY-PRODUCERS.json record, reported beside it (never overridden here).
        """
        records = self.producers["capabilities"]
        out = {}
        for capability, reason in (
            ("CAP-BOUND-SOURCES", "diagnostic path: snapshots only; no approval or claim checks"),
            ("CAP-RECEIPTS", "diagnostic path: no controller-global dispatch clock is read"),
            ("CAP-HARNESS-OBS", "diagnostic path: review projection is not harness observation"),
        ):
            record = records.get(capability, {})
            out[capability] = {
                "status": "NOT_IMPLEMENTED",
                "scope": "capture_review_diagnostics",
                "reasons": [reason],
                "capability_record_status": record.get("status"),
                "capability_record_method": record.get("method"),
            }
        return out

    def controller_goal_hash(self, goal):
        """CAP-CONTROLLER-HASH: the controller's own authorization goal digest.

        The caller passes the reference goal it authorizes; this never reads a
        controller's compiled goal (that is HARNESS_GOAL_HASH:current). NaN and
        other non-JSON values are refused by the controller routine itself.
        """
        if not isinstance(goal, dict):
            raise AdapterRefusal("an explicit goal object is required")
        return digest(goal)

    def configure_repair(self, case):
        """CAP-REPAIR-CONFIG: the Controller(max_repairs=...) value for one case.

        Only the frozen budget field is read. Anything outside the supported
        0/1 limits, including bool, is refused rather than coerced.
        """
        try:
            value = case["budget"]["max_repair_attempts"]
        except (KeyError, TypeError) as exc:
            raise AdapterRefusal("case budget.max_repair_attempts is required") from exc
        if type(value) is not int or value not in SUPPORTED_REPAIR_LIMITS:
            raise AdapterRefusal("max_repair_attempts must be the integer 0 or 1")
        return value

    def prepare_fixture(
        self, case, case_root, *, source, generator_provenance, presented_fetch_version
    ):
        """CAP-DISPOSABLE-FIXTURE: stage one pre-generated synthetic source per case.

        The generator is not imported here: the caller supplies its output and a
        pinned identity (generator, generator_sha256), recorded as caller-stated.
        Layout under a new case_root: source/source.zarr (staged; its parent holds
        nothing else, since verify-run searches source.parent), harness/ (manifest)
        and run/ (fresh, empty). source_pre uses the controller's own tree hash.
        Run provenance and synthetic invisibility are NOT declared here.
        """
        if not isinstance(case, dict) or not isinstance(case.get("id"), str) or not case["id"]:
            raise AdapterRefusal("a case with an explicit id is required")
        provenance = dict(generator_provenance)
        if not re.fullmatch(r"[0-9a-f]{64}", str(provenance.get("generator_sha256", ""))):
            raise AdapterRefusal("a pinned generator_sha256 is required")
        raw = Path(case_root)
        if not raw.is_absolute() or ".." in raw.parts:
            raise AdapterRefusal("case_root must be absolute with no '..' segments")
        root = safe_path(raw)
        if root.exists():
            raise FileExistsError("case_root must be new; reuse is refused")
        for ancestor in root.parents:
            if (ancestor / "harness" / "manifest.json").exists():
                raise AdapterRefusal("case_root lies inside another prepared case root")
        root.mkdir(exist_ok=False)
        source_directory, harness, run = root / "source", root / "harness", root / "run"
        source_directory.mkdir(exist_ok=False)
        staged = source_directory / "source.zarr"
        manifest = stage_fixture(
            source,
            staged,
            harness,
            generator_provenance=provenance,
            presented_fetch_version=presented_fetch_version,
        )
        run.mkdir(exist_ok=False)
        return {
            "schema": FIXTURE_SCHEMA,
            "case_id": case["id"],
            "case_root": str(root),
            "source": str(staged),
            "source_directory": str(source_directory),
            "harness": str(harness),
            "run": str(run),
            "source_pre": {"algorithm": SOURCE_HASH_ALGORITHM, "value": tree_hash(staged)},
            "generator_identity": {
                "generator": manifest["generator_provenance"]["generator"],
                "generator_sha256": manifest["generator_provenance"]["generator_sha256"],
                "stated_by": "caller",
            },
            "manifest": manifest,
            "not_declared": ["CAP-RUN-PROVENANCE"],
        }

    def patch_gate_report(self, hooks, event, controller):
        """CAP-GATE-REPORT-PATCH: deliver after_tool json_patch via InjectionHooks.

        event["process"] is the dict the controller's read_gate later parses, so
        the patched stdout is the delivered report. The raw report stays in the
        process manager's own record; both hashes are returned so evidence can
        name which is which. A schedule whose after_tool entries are anything
        but JSON Patch arrays is refused: delegating would apply them too.
        """
        if not isinstance(hooks, InjectionHooks):
            raise AdapterRefusal("the existing InjectionHooks delivery path is required")
        for spec in hooks.schedule:
            if spec["hook"] == "after_tool" and (
                spec["operation"] != "json_patch" or not isinstance(spec.get("payload"), list)
            ):
                raise AdapterRefusal("after_tool entry is not a gate-report JSON Patch")
        process = event.get("process") if isinstance(event, dict) else None
        if not isinstance(process, dict) or not isinstance(process.get("stdout"), str):
            raise AdapterRefusal("an observed after_tool process with stdout is required")
        raw = review_evidence.text_hash(process["stdout"])
        first = len(hooks.records)
        hooks("after_tool", event, controller)
        new = hooks.records[first:]
        applied = any(r.get("status") == "APPLIED" for r in new)
        return {
            "hash_algorithm": REPORT_HASH_ALGORITHM,
            "raw_report_hash": raw,
            # Only a report an entry was actually applied to counts as delivered.
            "delivered": applied,
            "delivered_report_hash": review_evidence.text_hash(process["stdout"])
            if applied
            else None,
            "raw_report_record": "process manager process record (unpatched stdout)",
            "delivered_report_record": "after_tool process consumed by read_gate",
            "records": json.loads(json.dumps(hooks.records[first:])),
        }

    def dispatch_dependent_schedule(self, hooks):
        """CAP-SCHEDULE-DEPENDENCIES: per-entry dispositions from InjectionHooks.

        Delivery happens at the controller's own hook points; this only returns
        the observed DELIVERED / INACTIVE / NOT_IMPLEMENTED / HARNESS_ERROR record.
        """
        if not isinstance(hooks, InjectionHooks):
            raise AdapterRefusal("the existing InjectionHooks delivery path is required")
        return hooks.dependency_report()

    def dispatch_after_accepted_review(self, hooks):
        """CAP-REVIEW-APPROVAL-HOOK: dispositions of after_review_approval entries.

        The controller emits the hook only after a durably committed accepted
        approval; rejected or uncommitted reviews consume no occurrence.
        """
        report = self.dispatch_dependent_schedule(hooks)
        return {
            "capability": "CAP-REVIEW-APPROVAL-HOOK",
            "accepted_approvals": report["accepted_approvals"],
            "entries": [e for e in report["entries"] if e["hook"] == "after_review_approval"],
        }

    def resolve_review_diagnostic_symbol(self, controller, symbol, reviewer_occurrence):
        """Resolve one observed symbol; this is not the broad CAP-SYMBOLS API."""
        expected = "HARNESS_SOURCE_HASH:" + review_evidence.DEFINITION_SOURCE_ID
        if symbol != expected or type(reviewer_occurrence) is not int:
            raise EvidenceMappingError("unsupported diagnostic symbol or occurrence")
        diagnostic = self.capture_review_diagnostics(controller)
        for snapshot in diagnostic["evidence"]["harness_observations"]["review_snapshots"]:
            if snapshot["reviewer_occurrence"] == reviewer_occurrence:
                for source in snapshot["sources"]:
                    if source["source_id"] == review_evidence.DEFINITION_SOURCE_ID:
                        return source["source_hash"]
        raise EvidenceMappingError("definition source was not observed in that review")

    def capture_review_diagnostics(self, controller):
        """Project only observed fields; never invoke a case, model, or tool.

        Complete returned-review records are required for this diagnostic path.
        A failed attempt is not silently dropped and does not become an invocation
        receipt. No Simkit invocation_receipts stream is synthesized from the
        narrower native receipt schema.
        """
        if not isinstance(controller, Controller):
            raise EvidenceMappingError("a real controller observation is required")
        controller.integrity()
        checkpoint = read_checkpoint(controller.root)
        if checkpoint["context"] != controller.context:
            raise EvidenceMappingError("controller context differs from durable checkpoint")
        context = checkpoint["context"].get("review_evidence")
        if not isinstance(context, dict):
            raise EvidenceMappingError("review evidence mode was not observed")
        original_sources = review_evidence.sources_from_docs(controller.docs())
        if context.get("sources") != original_sources:
            raise EvidenceMappingError("bound sources differ from original reviewed bytes")
        names = sorted(
            name
            for name in checkpoint["evidence"]
            if name.startswith("review-capture-") and name.endswith(".json")
        )
        reviewer_receipts = [
            receipt for receipt in checkpoint["role_receipts"] if receipt["role"] == "reviewer"
        ]
        if not names or len(names) != len(reviewer_receipts):
            raise EvidenceMappingError("complete dispatch/receipt correlation unavailable")
        if context.get("reviewer_occurrence") != len(names):
            raise EvidenceMappingError("review occurrence coverage mismatch")
        snapshots, provenance = [], []
        for occurrence, (name, receipt) in enumerate(zip(names, reviewer_receipts, strict=True), 1):
            if "/" in name or "\\" in name:
                raise EvidenceMappingError("capture path must be a run-local file")
            record = json.loads((controller.root / name).read_text(encoding="utf-8"))
            try:
                snapshot = self._project_record(
                    record, receipt, occurrence, checkpoint, original_sources, context["run_id"]
                )
            except (KeyError, TypeError, IndexError) as exc:
                raise EvidenceMappingError(
                    "required observed capture field missing/malformed"
                ) from exc
            snapshots.append(snapshot)
            provenance.append(
                {
                    "path": name,
                    "native_file_tree_hash": checkpoint["evidence"][name],
                    "run_id": record["run_id"],
                    "reviewer_occurrence": occurrence,
                    "input_hash": record["input_hash"],
                    "stdin_hash": record["stdin_hash"],
                    "accepted_review": record["accepted_review"],
                    "native_receipt": receipt,
                }
            )
        events = checkpoint["events"]
        states = ([events[0]["from"]] + [event["to"] for event in events]) if events else []
        return {
            "kind": "DIRECT_DIAGNOSTIC_NOT_ADMITTED_SIMKIT_CASE",
            "evidence": {
                "harness_observations": {"review_snapshots": snapshots},
                "controller_trace": {"states": states, "final_state": checkpoint["state"]},
            },
            "provenance": provenance,
            "capabilities": self.diagnostic_capability_status(),
            "unsupported_semantics": list(review_evidence.UNSUPPORTED),
        }

    @staticmethod
    def _project_record(record, receipt, occurrence, checkpoint, sources, run_id):
        if (
            record["schema"] != "workflow.review-capture/1"
            or record["role"] != "reviewer"
            or record["run_id"] != run_id
            or type(record["reviewer_occurrence"]) is not int
            or record["reviewer_occurrence"] != occurrence
            or record["dispatch_status"] != "RETURNED"
            or record["receipt"] != receipt
            or type(record["accepted_review"]) is not bool
        ):
            raise EvidenceMappingError("review dispatch identity or receipt mismatch")
        payload, envelope, output = record["payload"], record["review_evidence"], record["output"]
        stdin = record["stdin"]
        if (
            json.dumps(payload) != stdin
            or json.loads(stdin) != payload
            or review_evidence.text_hash(stdin) != record["stdin_hash"]
            or receipt["dispatch"]["stdin_hash"] != record["stdin_hash"]
            or digest(payload) != record["input_hash"]
            or receipt["input_hash"] != record["input_hash"]
            or receipt["output_hash"] != digest(output)
            or not receipt["invocation_id"]
            or not receipt["session_id"]
        ):
            raise EvidenceMappingError("review transport/receipt hash mismatch")
        dispatch = receipt["dispatch"]
        if review_evidence.text_hash(dispatch["system_prompt"]) != dispatch["system_prompt_hash"]:
            raise EvidenceMappingError("review system prompt hash mismatch")
        if (
            payload["review_evidence"] != envelope
            or envelope["schema"] != review_evidence.SCHEMA
            or envelope["sources"] != sources
            or review_evidence.sources_from_docs(payload["documentation"]) != sources
        ):
            raise EvidenceMappingError("review source envelope mismatch")
        review_evidence.validate_claims(envelope["plan_claims"], envelope["sources"])
        goal = request_goal(payload["request"])
        hashes = {
            "request_hash": digest(payload["request"]),
            "goal_hash": digest(goal),
            "plan_hash": plan_version_hash(payload["plan"], envelope["plan_claims"]),
            "manifest_hash": checkpoint["build_hash"],
            "source_hash": checkpoint["source_hash"],
        }
        if hashes["request_hash"] != checkpoint["request_hash"] or any(
            envelope[key] != value for key, value in hashes.items()
        ):
            raise EvidenceMappingError("reviewed binding components mismatch")
        binding = digest(
            {
                "request": hashes["request_hash"],
                "goal": goal,
                "plan": payload["plan"],
                "build": hashes["manifest_hash"],
                "source": hashes["source_hash"],
                "admission": checkpoint["admission_hash"],
                "review_evidence": {
                    "schema": envelope["schema"],
                    "plan_claims": envelope["plan_claims"],
                    "sources": sources,
                },
            }
        )
        if any(
            value != binding
            for value in (payload["binding_hash"], envelope["binding_hash"], output["binding_hash"])
        ):
            raise EvidenceMappingError("reviewed binding mismatch")
        if record["accepted_review"]:
            committed = record["committed_transition"]
            index = committed["index"]
            if type(index) is not int or not 0 <= index < len(checkpoint["events"]):
                raise EvidenceMappingError("committed review index invalid")
            event = checkpoint["events"][index]
            if (
                committed["event"] != event
                or event["to"] != "REVIEW_APPROVED"
                or committed["binding_hash"] != binding
                or output["verdict"] != "APPROVE"
                or event["evidence"] != output
                or event["evidence_hash"] != digest(output)
            ):
                raise EvidenceMappingError("accepted review lacks exact durable transition")
        # Only contract fields observed in the actual input. No support labels,
        # approved_binding, terminal result, seq, phase or empty streams invented.
        return json.loads(
            json.dumps(
                {
                    "reviewer_occurrence": occurrence,
                    **hashes,
                    "plan_claims": envelope["plan_claims"],
                    "sources": envelope["sources"],
                }
            )
        )
