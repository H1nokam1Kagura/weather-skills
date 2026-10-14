"""CAP-ROLE-SUBSTITUTE: substituted role doubles and after_role output substitution.

Frozen v2.7 capability (casesets/cached-onset-e2e-v2_7/frozen/
agentic-e2e-injections.ADAPTER-REQUIREMENTS.json, CAP-ROLE-SUBSTITUTE, method
substitute_role_output): "Replace a role's output (or patch it) at after_role, preserving
receipts and session IDs."

Three parts, each with one producer:

* ``SubstitutedRoles`` -- the ``Controller(roles=...)`` double for every role a case lists
  under ``agent_roles.substituted``. It returns the frozen BASELINE output for that role (the
  reference goal, the registered producing plan with the frozen verbatim claims, an approving
  review of the presented binding, SUPPORTED checker verdicts, a PASS verifier) and writes a
  native receipt in the agents.Roles shape: a fresh invocation_id and a distinct session_id
  per invocation, input/output hashes, ``mode: "substituted"``. No model is called.
* ``CaseScheduleHooks`` -- the case's ``injection_schedule`` delivered by the existing
  ``tools.workflow.e2e_injections.InjectionHooks`` at the controller's own hook points. A
  role-output entry (json_patch / replace_role_output at after_role) is applied to the
  answer in place at the controller's pre-commit ``after_role`` emit; the substituted
  invocation's own receipt is then rebound to the delivered output (same receipt object,
  same invocation_id and session_id, ``baseline_output_hash`` kept, ``substitution`` record
  added) BEFORE the controller checks the dispatch receipt binding. A receipt that is not
  this double's own is never rebound: the controller then refuses on its own binding check.
* ``substitution_plan`` -- the static check behind ``substitute_role_output``: every
  role-output entry is mapped from the frozen contract shape onto the native role output, or
  refused (``AdapterRefusal``) before any side effect. Never silently skipped.

Bare-plan wrapping (owner ruling e07_bare_plan_wrapping_2026-10-14): a planner/repairer
replacement whose frozen payload carries ``plan`` as a bare step list (E07-P1 entry 2) is
wrapped by the one rule in ``WRAP_RULE``; every derived field and its source is recorded per
entry and per delivery (receipt ``substitutions[].wrappings``).

CTL and INJ cases use the same double: the baseline outputs, plus whatever the case
schedules (a CTL case may schedule an empty patch or a replacement too, e.g. E02-P1-CTL,
E07-P1-CTL). Every mapping choice is listed in CAPABILITY-PRODUCERS.json for review.
"""

import copy
import json
import re
import uuid

from tools.workflow import review_evidence
from tools.workflow.contracts import FIELDS, digest, expected_plan
from tools.workflow.e2e_injections import (
    InjectionHooks,
    UnsupportedInjection,
    json_patch,
    unsupported_requirements,
)

CAPABILITY = "CAP-ROLE-SUBSTITUTE"
MODE = "substituted"
SUBSTITUTE_ID = "adapter_support.role_substitution/frozen-v2.7-baseline"
ROLE_OUTPUT_OPS = frozenset({"json_patch", "replace_role_output"})
PATCH_OPS = frozenset({"add", "replace", "remove", "test"})
COMPILER_SELECTORS = ("compiler_1", "compiler_2", "compiler_3")
# Frozen agent_roles name -> native Controller role name.
NATIVE_ROLE = {
    **{name: "compiler" for name in COMPILER_SELECTORS},
    "planner": "planner",
    "repairer": "repairer",
    "reviewer": "reviewer",
    "evidence_checker": "evidence_checker",
    "verifier": "verifier",
}
# after_role substitution is mapped for these selectors only. verifier: no frozen case
# substitutes it at after_role and the contract wraps it (role_output.verifier[k].
# returned_output), so no mapping is claimed. shadow_policy/router: no baseline exists.
SUBSTITUTABLE_SELECTORS = frozenset(NATIVE_ROLE) - {"verifier"}
# CAP-SHADOW (adapter_support/shadow_projection.py): a substituted shadow_policy answers with the
# decision its ShadowDriver armed at the controller's after_transition emit (no baseline).
SHADOW_ROLE = "shadow_policy"
# accept_review_output (runner.py) accepts exactly this frozen reviewer schema in review
# evidence mode, so a frozen replacement is already the native shape.
REVIEWER_KEYS = frozenset(
    {"verdict", "approved_binding", "findings", "reason", "verification_waiver"}
)
# Frozen baseline claims: the clean-control planner/repairer claims of the frozen probe
# bundles (DEV-ONLY.json, e.g. DEV-E2E-V2_7-E13-P1-INJ probe_bundles[0].evidence.role_output
# repairer[0].claims), citing the reference definition bound under the colon id (D1).
FROZEN_DEFINITION_SOURCE_ID = review_evidence.REFERENCE_DEFINITION_SOURCE_ID
FROZEN_DEFINITION_SOURCE_HASH = (
    "sha256:3db5ec77bcb91aa6eacb9e7e5d09c385e75461866ac369a9ac940df8ab763aa6"
)
FROZEN_REFERENCE_WINDOW = ("2025-09-01", "2025-12-31")
FROZEN_CLAIMS = (
    {
        "claim_id": "C-WINDOW",
        "text": "Each candidate onset date needs a complete 30-day window inside the input.",
        "quote": "A candidate at day t requires t through t+29 inclusive.",
        "derivable": False,
    },
    {
        "claim_id": "C-LAST-ELIGIBLE",
        "text": "For input 2025-09-01..2025-12-31 the last eligible onset date is 2025-12-02.",
        "quote": "December 2 plus 29 days is December 31.",
        "derivable": True,
        "computes": {"fact": "last_eligible_onset_date", "value": "2025-12-02"},
    },
    {
        "claim_id": "C-PLOT-SCALE",
        "text": "A colour scale from 2025-09-01 to 2025-12-02 does not truncate the rainfall "
        "input.",
        "quote": "It does not truncate the rainfall input.",
        "derivable": False,
    },
)
# Repairer baseline response: the frozen text, verbatim, for every pair whose substituted
# repairer runs unpatched (agent_roles.substituted lists it and no schedule entry replaces it).
# Source: DEV-ONLY.json <INJ case>.probe_bundles[0].evidence.role_output.repairer[0].response.
FROZEN_REPAIRER_RESPONSES = {
    "E13-P1": "Citation for C-WINDOW restated verbatim from the bound definition; science "
    "unchanged.",
    "E13-P3": "C-LAST-ELIGIBLE corrected to 2025-12-02 (30-day window); science unchanged.",
    # Verbatim, including the probe bundle's placeholder plan label "h-plan-1".
    "E15-P1": "Child plan of h-plan-1 after the third material change; science unchanged.",
}
# Harness-authored fallback, used ONLY for a pair with no frozen repairer text (none of the
# frozen substituted-repairer pairs today: E03-P3 and E07-P1 replace the repairer by schedule).
REPAIRER_RESPONSE = (
    "Substituted repair: the registered plan is kept and every claim is restated verbatim "
    "from the bound definition."
)


def repairer_response(pair_id):
    """(text, source): the frozen text for the pair, else the labelled harness fallback."""
    if pair_id in FROZEN_REPAIRER_RESPONSES:
        return FROZEN_REPAIRER_RESPONSES[pair_id], "frozen_probe_bundle"
    return REPAIRER_RESPONSE, "harness_authored_fallback"


def pair_of(case):
    pair = case.get("pair_id") if isinstance(case, dict) else None
    if isinstance(pair, str) and pair:
        return pair
    ident = case.get("id", "") if isinstance(case, dict) else ""
    ident = ident.removeprefix("DEV-E2E-V2_7-")
    return ident.rsplit("-", 1)[0] if ident.endswith(("-CTL", "-INJ")) else ident


# The single documented wrapping rule for a bare plan step list (owner ruling
# e07_bare_plan_wrapping_2026-10-14). Recorded verbatim in every wrapping record and in the
# CAP-ROLE-SUBSTITUTE record of CAPABILITY-PRODUCERS.json.
WRAP_RULE_ID = "bare-plan-wrap/v1"
WRAP_RULE = (
    "A planner/repairer replace_role_output whose frozen payload carries plan as a JSON array "
    "is wrapped into the native envelope of the output it replaces at the controller's "
    "after_role point. For a substituted role that output is the stand-in's baseline for this "
    "invocation (static_baseline(role, goal, pair)); at delivery its hash must equal the "
    "recorded source_output_hash, and the delivered hash the recorded wrapped_output_hash, "
    "else the substitution is refused. The array is delivered verbatim as plan.steps. Every "
    "other key of the typed plan (goal_hash, final) and every other envelope key the replaced "
    "output carries (schema, plan_claims; response or rationale only when the payload does "
    "not supply it) is copied unchanged from the replaced output. Payload keys other than "
    "plan are delivered verbatim and must be native envelope keys of the role (repairer: "
    "response; planner: rationale). No value is invented: a key the replaced output does not "
    "carry is never added, and any other payload shape is refused."
)
WRAP_EXTRA_KEYS = {"planner": frozenset({"rationale"}), "repairer": frozenset({"response"})}
CHECKER_REASONING = "substituted baseline verdict; no model judged this claim"
# Recorded in place of a system prompt: a substituted reviewer is sent none.
NO_SYSTEM_PROMPT = "SUBSTITUTED: no system prompt was sent; output from " + SUBSTITUTE_ID
_INDEX = re.compile(r"^/(\d+|-)(/.*)?$")


class SubstitutionUnavailable(RuntimeError):
    """The substituted double has no frozen baseline for this invocation; nothing returned."""


class SubstitutionBindingError(RuntimeError):
    """A scheduled substitution did not bind to this double's own receipt; nothing rebound."""


def _refusal():
    from adapter_support.weather_adapter import AdapterRefusal

    return AdapterRefusal


def _refuse(message):
    raise _refusal()(message)


def frozen_claims():
    out = []
    for claim in FROZEN_CLAIMS:
        record = {
            "claim_id": claim["claim_id"],
            "text": claim["text"],
            "citations": [
                {
                    "source_id": FROZEN_DEFINITION_SOURCE_ID,
                    "source_hash": FROZEN_DEFINITION_SOURCE_HASH,
                    "quote": claim["quote"],
                }
            ],
            "derivable": claim["derivable"],
        }
        if "computes" in claim:
            record["computes"] = dict(claim["computes"])
        out.append(record)
    return out


def static_baseline(native_role, goal, pair_id=None):
    """Baselines that do not depend on the controller payload (used for the dry run too)."""
    if native_role == "compiler":
        return copy.deepcopy(goal)
    if native_role in {"planner", "repairer"}:
        value = {
            "schema": review_evidence.SCHEMA,
            "plan": expected_plan(goal),
            "plan_claims": frozen_claims(),
        }
        if native_role == "repairer":
            value["response"] = repairer_response(pair_id)[0]
        return value
    return None


def check_goal(goal):
    if not isinstance(goal, dict) or set(goal) != FIELDS:
        _refuse("substituted roles need the reference goal with the controller goal fields")
    if (goal["start_date"], goal["end_date"]) != FROZEN_REFERENCE_WINDOW:
        _refuse("the frozen baseline claims are stated for the reference input window only")


# --------------------------------------------------------------------------- static plan


def _map_paths(operations, mapper, where):
    if not isinstance(operations, list):
        _refuse(f"{where}: json_patch payload must be an array")
    mapped = []
    for op in operations:
        if not isinstance(op, dict) or op.get("op") not in PATCH_OPS:
            _refuse(f"{where}: only add/replace/remove/test patch operations are supported")
        path = op.get("path")
        if not isinstance(path, str) or not path.startswith("/"):
            _refuse(f"{where}: non-root JSON pointer required")
        native = mapper(path)
        if native is None:
            _refuse(f"{where}: no faithful native mapping for contract path {path}")
        mapped.append({**op, "path": native})
    return mapped


def _compiler_path(path):
    # Contract role_output.compiler_N[k] = {"goal": <native goal>} (evidence_projection).
    if path.startswith("/goal/"):
        return path[len("/goal") :]
    return None


def _plan_envelope_path(path):
    # Contract planner/repairer[k].plan is the step list; native "plan" is the typed plan
    # {goal_hash, steps, final}; contract "claims" is the native envelope "plan_claims".
    if path.startswith("/plan/"):
        match = _INDEX.match(path[len("/plan") :])
        return "/plan/steps" + path[len("/plan") :] if match else None
    if path == "/claims" or path.startswith("/claims/"):
        return "/plan_claims" + path[len("/claims") :]
    if path == "/rationale":
        return path
    return None


def wrap_bare_plan(name, payload, goal, pair_id, where):
    """WRAP_RULE: (native envelope, wrapping record) for a bare plan step list."""
    if goal is None:
        _refuse(
            f"{where}: bare-plan wrapping derives the envelope from the stand-in baseline, "
            "which needs the case goal"
        )
    extra = set(payload) - {"plan"}
    if extra - WRAP_EXTRA_KEYS[name]:
        _refuse(
            f"{where}: a {name} bare-plan replacement may carry only "
            f"{sorted(WRAP_EXTRA_KEYS[name])} besides plan; got {sorted(extra)}"
        )
    source = static_baseline(name, goal, pair_id)
    wrapped = copy.deepcopy(source)
    wrapped["plan"]["steps"] = copy.deepcopy(payload["plan"])
    delivered = [{"field": "/plan/steps", "source": "frozen payload /plan (verbatim)"}]
    for key in sorted(extra):
        wrapped[key] = copy.deepcopy(payload[key])
        delivered.append({"field": f"/{key}", "source": f"frozen payload /{key} (verbatim)"})
    derived = [
        {
            "field": f"/plan/{key}",
            "source": f"replaced output /plan/{key}",
            "value": copy.deepcopy(source["plan"][key]),
        }
        for key in sorted(set(source["plan"]) - {"steps"})
    ]
    derived += [
        {"field": f"/{key}", "source": f"replaced output /{key}", "value": copy.deepcopy(value)}
        for key, value in sorted(source.items())
        if key != "plan" and key not in extra
    ]
    record = {
        "rule_id": WRAP_RULE_ID,
        "rule": WRAP_RULE,
        "owner_ruling": "e07_bare_plan_wrapping_2026-10-14",
        "source": f"stand-in baseline of the substituted {name} for this invocation "
        "(the output replaced at after_role)",
        "source_output_hash": digest(source),
        "frozen_payload_hash": digest(payload),
        "wrapped_output_hash": digest(wrapped),
        "delivered_verbatim": delivered,
        "derived_fields": derived,
    }
    return wrapped, record


def _identity_path(keys):
    def mapper(path):
        return path if path.split("/")[1] in keys else None

    return mapper


def _map_entry(index, spec, goal, pair_id):
    selector = spec.get("selector") if isinstance(spec.get("selector"), dict) else {}
    name, occurrence = selector.get("role_or_tool_or_stage"), selector.get("occurrence", 1)
    operation, payload = spec["operation"], spec.get("payload")
    where = f"schedule entry {index} ({name}, {operation})"
    if type(occurrence) is not int or occurrence < 1:
        _refuse(f"{where}: occurrence must be a positive integer")
    if name in COMPILER_SELECTORS and occurrence != 1:
        _refuse(f"{where}: compiler_N names one invocation; occurrence must be 1")
    native = NATIVE_ROLE.get(name)
    mapping = "identity (frozen shape is the native shape)"
    wrapping = None
    if name in COMPILER_SELECTORS:
        if operation == "json_patch":
            payload = _map_paths(payload, _compiler_path, where)
            mapping = "contract /goal/<field> -> native goal /<field>"
        elif isinstance(payload, dict) and set(payload) == {"goal"}:
            payload = copy.deepcopy(payload["goal"])
            mapping = "contract {goal: G} -> native goal G"
        else:
            _refuse(f"{where}: a compiler replacement must be exactly {{goal: ...}}")
    elif name in {"planner", "repairer"}:
        if operation == "json_patch":
            payload = _map_paths(payload, _plan_envelope_path, where)
            mapping = "contract /plan/<i> -> /plan/steps/<i>; /claims -> /plan_claims"
        elif (
            name == "repairer"
            and isinstance(payload, dict)
            and isinstance(payload.get("needs_info"), str)
            and set(payload) <= {"needs_info", "response"}
        ):
            mapping = "identity (frozen repairer needs_info shape is native)"
        elif isinstance(payload, dict) and isinstance(payload.get("plan"), list):
            # Owner ruling e07_bare_plan_wrapping_2026-10-14: the one documented rule.
            payload, wrapping = wrap_bare_plan(name, payload, goal, pair_id, where)
            mapping = f"bare plan step list -> envelope of the replaced output ({WRAP_RULE_ID})"
        else:
            # Any other replacement shape: no faithful native mapping, refused, never invented.
            _refuse(
                f"{where}: a {name} replacement of this shape has no faithful native mapping "
                "(only a bare plan step list is wrapped, by the documented rule)"
            )
    elif name == "reviewer":
        if operation == "json_patch":
            payload = _map_paths(payload, _identity_path(REVIEWER_KEYS), where)
        elif not (
            isinstance(payload, dict) and "verdict" in payload and set(payload) <= REVIEWER_KEYS
        ):
            _refuse(f"{where}: reviewer replacement is outside the frozen reviewer schema")
    elif name == "evidence_checker":
        if operation == "json_patch":
            payload = _map_paths(payload, _identity_path({"plan_hash", "verdicts"}), where)
        elif not (isinstance(payload, dict) and set(payload) == {"plan_hash", "verdicts"}):
            _refuse(f"{where}: evidence_checker replacement must be {{plan_hash, verdicts}}")
    if goal is not None and operation == "json_patch":
        baseline = static_baseline(native, goal, pair_id)
        if baseline is not None:
            try:
                json_patch(baseline, payload)
            except UnsupportedInjection as exc:
                _refuse(f"{where}: patch does not apply to the substituted baseline: {exc}")
    return payload, mapping, wrapping


def substitution_plan(case, goal=None):
    """Map every after_role role-output entry, or refuse; list everything else as delegated.

    Delegated entries (other hooks/operations) belong to other capabilities; they are still
    delivered by the same InjectionHooks, never dropped.
    """
    AdapterRefusal = _refusal()
    if not isinstance(case, dict) or not isinstance(case.get("id"), str) or not case["id"]:
        raise AdapterRefusal("a case with an explicit id is required")
    schedule = case.get("injection_schedule") or []
    if not isinstance(schedule, list):
        raise AdapterRefusal("injection_schedule must be a list")
    roles = case.get("agent_roles") or {}
    substituted = set(roles.get("substituted") or [])
    live = set(roles.get("live") or [])
    if goal is not None:
        check_goal(goal)
    native_schedule, entries, delegated = [], [], []
    for index, spec in enumerate(schedule):
        if not isinstance(spec, dict) or not isinstance(spec.get("operation"), str):
            raise AdapterRefusal(f"schedule entry {index} is malformed")
        if spec["operation"] not in ROLE_OUTPUT_OPS or spec.get("hook") != "after_role":
            native_schedule.append(copy.deepcopy(spec))
            delegated.append(
                {"index": index, "hook": spec.get("hook"), "operation": spec["operation"]}
            )
            continue
        selector = spec.get("selector") if isinstance(spec.get("selector"), dict) else {}
        name = selector.get("role_or_tool_or_stage")
        if name not in SUBSTITUTABLE_SELECTORS:
            raise AdapterRefusal(f"schedule entry {index}: no substitution channel for {name!r}")
        if name not in substituted or name in live:
            raise AdapterRefusal(
                f"schedule entry {index}: {name} is not a substituted-only role in this case; "
                "a live role's output is never replaced"
            )
        payload, mapping, wrapping = _map_entry(index, spec, goal, pair_of(case))
        native_schedule.append({**copy.deepcopy(spec), "payload": payload})
        entry = {
            "index": index,
            "id": spec.get("id"),
            "hook": "after_role",
            "selector": copy.deepcopy(selector),
            "native_role": NATIVE_ROLE[name],
            "operation": spec["operation"],
            "frozen_payload_hash": digest(spec.get("payload")),
            "native_payload_hash": digest(payload),
            "adapter_mapping": mapping,
        }
        if wrapping is not None:
            entry["wrapping"] = wrapping
        entries.append(entry)
    return {
        "capability": CAPABILITY,
        "case_id": case["id"],
        "substituted_roles": sorted(substituted),
        "entries": entries,
        "delegated": delegated,
        "native_case": {"id": case["id"], "injection_schedule": native_schedule},
    }


def refuse_unsupported_schedule(case):
    """InjectionHooks' own static findings refuse a case before any side effect."""
    findings = unsupported_requirements(case)
    if findings:
        _refuse(f"case schedule needs injection channels that are unavailable: {findings}")


# --------------------------------------------------------------------------- role double


class SubstitutedRoles:
    """Controller roles double (agents.Roles attribute contract); no model is called."""

    def __init__(self, case, goal, budget=5):
        AdapterRefusal = _refusal()
        check_goal(goal)
        roles = case.get("agent_roles") if isinstance(case, dict) else None
        if not isinstance(roles, dict) or not roles.get("substituted"):
            raise AdapterRefusal("case lists no substituted roles")
        if roles.get("live"):
            raise AdapterRefusal(
                "live + substituted roles in one run need a combining transport (not built)"
            )
        unknown = sorted(set(roles["substituted"]) - set(NATIVE_ROLE) - {SHADOW_ROLE})
        if unknown:
            raise AdapterRefusal(f"no frozen baseline for substituted roles {unknown}")
        compilers = set(roles["substituted"]) & set(COMPILER_SELECTORS)
        if compilers and compilers != set(COMPILER_SELECTORS):
            raise AdapterRefusal("compilers are substituted all three or not at all")
        self.case_id = case["id"]
        self.pair_id = pair_of(case)
        self.goal = copy.deepcopy(goal)
        self.allowed = frozenset(NATIVE_ROLE.get(r, r) for r in roles["substituted"])
        self.shadow = None  # CAP-SHADOW: shadow_projection.ShadowDriver, attached by run_case
        self.receipts = []
        self.sessions = set()
        self.spent = 0.0
        self.budget = budget
        self.model = MODE
        self.dispatch_context = None
        self.dispatch_observation = None
        self.dispatch_observer = None

    def ask(self, role, payload):
        invocation = str(uuid.uuid4())
        context = getattr(self, "dispatch_context", None)
        if context is not None:
            self.dispatch_observation = {
                "attempt_id": context["attempt_id"],
                "invocation_id": invocation,
                "process_boundary": "NO_PROCESS",
                "transport": SUBSTITUTE_ID,
            }
            self.dispatch_observer(dict(self.dispatch_observation))
        if role not in self.allowed:
            raise SubstitutionUnavailable(
                f"{role} is not a substituted role in case {self.case_id}; nothing returned"
            )
        value = self.baseline(role, payload)
        session = f"substituted-{uuid.uuid4()}"
        while session in self.sessions:  # pragma: no cover - uuid4 collision
            session = f"substituted-{uuid.uuid4()}"
        output_hash = digest(value)
        receipt = {
            "role": role,
            "invocation_id": invocation,
            "session_id": session,
            "input_hash": digest(payload),
            "output_hash": output_hash,
            "baseline_output_hash": output_hash,
            "cost_usd": 0.0,
            "mode": MODE,
            "transport": SUBSTITUTE_ID,
        }
        if role == "reviewer" and "review_evidence" in payload:
            stdin = json.dumps(payload)  # the serializer review_evidence.capture hashes
            receipt["dispatch"] = {
                "stdin_hash": review_evidence.text_hash(stdin),
                "system_prompt": NO_SYSTEM_PROMPT,
                "system_prompt_hash": review_evidence.text_hash(NO_SYSTEM_PROMPT),
            }
        if role == SHADOW_ROLE:
            self.shadow.bind_receipt(receipt)
        if context is not None:
            self.dispatch_observation.update(process_boundary="RETURNED")
        self.sessions.add(session)
        self.receipts.append(receipt)
        return value

    def baseline(self, role, payload):
        if role == "compiler":
            return static_baseline(role, self.goal, self.pair_id)
        if role in {"planner", "repairer"}:
            request = payload.get("review_evidence_request")
            if not isinstance(request, dict):
                raise SubstitutionUnavailable(
                    f"the frozen {role} baseline is the review-evidence envelope; the "
                    "controller sent no review_evidence_request"
                )
            bound = {s.get("source_id"): s for s in request.get("sources") or []}
            source = bound.get(FROZEN_DEFINITION_SOURCE_ID)
            if source is None or source.get("source_hash") != FROZEN_DEFINITION_SOURCE_HASH:
                raise SubstitutionUnavailable(
                    "the frozen baseline claims cite the reference definition "
                    f"{FROZEN_DEFINITION_SOURCE_ID} ({FROZEN_DEFINITION_SOURCE_HASH}); the "
                    "controller bound no such source"
                )
            if any(c["quote"] not in source.get("text", "") for c in FROZEN_CLAIMS):
                raise SubstitutionUnavailable("bound definition text lacks a frozen quote")
            return static_baseline(role, self.goal, self.pair_id)
        if role == "reviewer":
            evidence = payload.get("review_evidence")
            if not isinstance(evidence, dict):
                raise SubstitutionUnavailable(
                    "the frozen reviewer baseline approves a presented review_evidence binding"
                )
            return {
                "verdict": "APPROVE",
                "approved_binding": {
                    "plan_hash": evidence["plan_hash"],
                    "source_hash": evidence["source_hash"],
                },
                "findings": [],
            }
        if role == "evidence_checker":
            return {
                "plan_hash": payload["plan_hash"],
                "verdicts": [
                    {
                        "claim_id": c["claim_id"],
                        "verdict": "SUPPORTED",
                        "reasoning": CHECKER_REASONING,
                    }
                    for c in payload["claims"]
                ],
            }
        if role == "verifier":
            return {
                "verdict": "PASS",
                "binding_hash": payload["binding_hash"],
                "scope": "data-replay",
            }
        if role == SHADOW_ROLE and self.shadow is not None:
            return self.shadow.decide(payload)
        raise SubstitutionUnavailable(f"no frozen baseline for role {role}")


# --------------------------------------------------------------------------- hooks


class CaseScheduleHooks:
    """InjectionHooks over the mapped schedule, plus the after_role receipt rebind."""

    def __init__(self, plan, roles, disposable_root):
        self.plan = plan
        self.roles = roles
        self.injection = InjectionHooks(plan["native_case"], disposable_root=disposable_root)
        self.role_indexes = {e["index"] for e in plan["entries"]}
        self.rebinds = []

    def prepare(self, request, source):
        return self.injection.prepare(request, source)

    def __call__(self, point, event, controller):
        first = len(self.injection.records)
        self.injection(point, event, controller)
        if point != "after_role":
            return
        applied = [
            r
            for r in self.injection.records[first:]
            if r["index"] in self.role_indexes and r["status"] == "APPLIED"
        ]
        if applied:
            self.rebind(event, controller, applied)

    def rebind(self, event, controller, applied):
        roles = getattr(controller, "roles", None)
        receipts = getattr(roles, "receipts", None)
        if roles is not self.roles or not receipts:
            raise SubstitutionBindingError("substitution applied outside the substituted roles")
        receipt = receipts[-1]
        if receipt.get("mode") != MODE or receipt.get("role") != event.get("role"):
            raise SubstitutionBindingError("the last receipt is not this substituted invocation")
        expected = receipt["output_hash"]
        by_index = {e["index"]: e for e in self.plan["entries"]}
        wrappings = []
        for record in applied:
            if record.get("before_hash") != expected:
                raise SubstitutionBindingError("substitution chain does not start at the receipt")
            wrapping = by_index[record["index"]].get("wrapping")
            if wrapping is not None:
                # WRAP_RULE: the derived fields came from exactly the output being replaced.
                if record["before_hash"] != wrapping["source_output_hash"]:
                    raise SubstitutionBindingError(
                        "the replaced output is not the recorded bare-plan wrapping source"
                    )
                if record.get("after_hash") != wrapping["wrapped_output_hash"]:
                    raise SubstitutionBindingError("delivered output is not the recorded wrap")
                wrappings.append({"schedule_index": record["index"], **copy.deepcopy(wrapping)})
            expected = record["after_hash"]
        delivered = digest(event["answer"])
        if delivered != expected:
            raise SubstitutionBindingError("delivered answer differs from the applied substitution")
        identity = (receipt["invocation_id"], receipt["session_id"])
        substitution = {
            "hook": "after_role",
            "schedule_indexes": [r["index"] for r in applied],
            "operations": [r["operation"] for r in applied],
            "pre_substitution_output_hash": receipt["output_hash"],
            "delivered_output_hash": delivered,
            "adapter_mappings": [
                e["adapter_mapping"]
                for i in (r["index"] for r in applied)
                for e in self.plan["entries"]
                if e["index"] == i
            ],
        }
        if wrappings:
            substitution["wrappings"] = wrappings
        receipt["output_hash"] = delivered
        receipt.setdefault("substitutions", []).append(substitution)
        if (receipt["invocation_id"], receipt["session_id"]) != identity:  # pragma: no cover
            raise SubstitutionBindingError("receipt identity changed during rebind")
        self.rebinds.append(
            {"invocation_id": identity[0], "session_id": identity[1], **substitution}
        )

    def report(self):
        return {
            "capability": CAPABILITY,
            "entries": copy.deepcopy(self.plan["entries"]),
            "delegated": copy.deepcopy(self.plan["delegated"]),
            "coverage": self.injection.coverage(),
            "rebinds": copy.deepcopy(self.rebinds),
        }


def case_hooks(plan, roles, disposable_root):
    """None when the case schedules nothing (the run then keeps the observer alone)."""
    if not plan["native_case"]["injection_schedule"]:
        return None
    if plan["entries"] and not (
        isinstance(roles, SubstitutedRoles) and roles.case_id == plan["case_id"]
    ):
        _refuse("scheduled role substitution needs this case's substituted roles factory")
    return CaseScheduleHooks(plan, roles, disposable_root)
