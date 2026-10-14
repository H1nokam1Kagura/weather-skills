"""CAP-SHADOW: declare, drive and project the controller's shadow policy.

Frozen v2.7 capability (casesets/cached-onset-e2e-v2_7/frozen/
agentic-e2e-injections.ADAPTER-REQUIREMENTS.json, CAP-SHADOW, method drive_shadow_policy):
"Declare the shadow policy's invocation points and candidate vocabulary; drive or substitute
its decisions."

The native producer is the controller: ``tools/workflow/shadow.py`` (policy mode
``shadow_policy``) asks role ``shadow_policy`` a fingerprinted question after GOAL_CHECKED,
PLAN_VALIDATED, ONSET_CHECKED and DATA_VERIFIED (``policy.SHADOW_STATES``), validates the
answer itself and records every decision in ``context["shadow_decisions"]`` (plus
shadow-decision-NNN.json and a shadow_decision dispatch event). Nothing here judges a decision.

Three parts:

* ``shadow_plan`` (``WeatherAdapter.drive_shadow_policy``): the invocation points and the
  per-state candidate vocabulary the controller will use for the case's profile, and the
  case's shadow decision entries mapped or refused (``AdapterRefusal``) before any side
  effect. The frozen E09-P1 cases schedule each decision as ``replace_role_output`` at
  ``after_transition`` of the state the decision follows; that stage event is not an output
  channel of InjectionHooks, so these entries are delivered here instead.
* ``ShadowDriver``: the delivery. At the controller's ``after_transition`` emit for state S
  (occurrence k) it arms the entry scheduled there; the substituted ``shadow_policy`` double
  (role_substitution.SubstitutedRoles) then answers the question the controller asks right
  after that emit with the armed decision, its trusted symbols resolved against that very
  question. The double's own receipt records what was delivered and how each symbol was
  resolved. No frozen baseline exists for the shadow policy, so an invocation with nothing
  armed is a dispatch failure (SubstitutionUnavailable), never an invented decision.
* ``project``: controller_trace.shadow_decisions and role_output.shadow_policy in the frozen
  contract vocabulary (EVIDENCE_CONTRACT.md controller_trace.shadow_decisions {state,
  accepted, action, score}; role_output.shadow_policy[*] {schema, state,
  question_fingerprint, action, score}), plus the symbols HARNESS_CANDIDATE_LABEL:<S>.<m> and
  HARNESS_DECISION_FINGERPRINT:<S> from the controller's own questions.
"""

import copy
import json
import re
from collections import Counter

from adapter_support import role_substitution
from tools.workflow import policy, shadow
from tools.workflow.contracts import digest
from tools.workflow.profiles import PROFILES, stages

CAPABILITY = "CAP-SHADOW"
ROLE = shadow.MODE  # the role name and the controller policy mode are both "shadow_policy"
HOOK = "after_transition"
OPERATION = "replace_role_output"
LABEL = re.compile(r"HARNESS_CANDIDATE_LABEL:([A-Z_]+)\.([a-z_]+)")
FINGERPRINT = re.compile(r"HARNESS_DECISION_FINGERPRINT:([A-Z_]+)")
ANY_SYMBOL = re.compile(r"HARNESS_[A-Z_]+")
# Reviewed mapping choice (CAPABILITY-PRODUCERS.json CAP-SHADOW): a fingerprint symbol naming a
# state with no controller shadow question so far in the run (E09-P1-INJ entry 0 names
# SOURCE_CHECKED, which is not an invocation point) has no controller value to deliver.
STALE_FINGERPRINT_RULE_ID = "stale-fingerprint/v1"
STALE_FINGERPRINT_RULE = (
    "HARNESS_DECISION_FINGERPRINT:<S> resolves to the question_hash of the controller's shadow "
    "question at state S: the question being answered when S is its state, else the latest "
    "earlier question at S in this run (a stale fingerprint). When the controller asked no "
    "question at S before this one, no controller fingerprint exists; the harness delivers "
    "digest({'harness_stale_fingerprint': <symbol>}) (controller digest shape), recorded as "
    "source harness_authored_stale_fingerprint on the receipt and mapped back to the symbol "
    "in the projection. It is a stimulus, never evidence: the controller's own validation "
    "decides whether it matches."
)


def _refuse(message):
    role_substitution._refuse(message)


def _detached(value):
    from adapter_support.evidence_projection import _detached as detached

    return detached(value)


def _mapping_error(message):
    from adapter_support.evidence_projection import EvidenceMappingError

    return EvidenceMappingError(message)


def is_shadow_entry(spec):
    return isinstance(spec, dict) and spec.get("hook") == HOOK and spec.get("operation") == OPERATION


def _task_of(case):
    for task, record in PROFILES.items():
        if record["id"] == case.get("profile"):
            return task
    _refuse(f"case profile {case.get('profile')!r} is not a registered controller profile")


def vocabulary_for(task):
    """The controller's per-state candidate vocabulary for one profile (policy.shadow_candidates
    over the profile's state sequence, as shadow.next_state computes it)."""
    sequence = stages(task)
    return {
        state: policy.shadow_candidates(state, sequence[sequence.index(state) + 1])
        for state in policy.SHADOW_STATES
        if state in sequence and sequence.index(state) + 1 < len(sequence)
    }


def _check_payload(where, state, payload, vocabulary):
    if not isinstance(payload, dict) or set(payload) != policy.SHADOW_OUTPUT_FIELDS:
        _refuse(
            f"{where}: a shadow decision replacement must carry exactly "
            f"{sorted(policy.SHADOW_OUTPUT_FIELDS)}"
        )
    for key, value in payload.items():
        if not isinstance(value, str) or not ANY_SYMBOL.search(value):
            continue
        if key == "question_fingerprint" and FINGERPRINT.fullmatch(value):
            continue
        label = LABEL.fullmatch(value) if key == "action" else None
        if label is None:
            _refuse(f"{where}: trusted symbol {value!r} has no shadow mapping at /{key}")
        if label.group(1) != state or label.group(2) not in vocabulary[state]:
            # A label is read from the candidate list of the decision it answers (spec
            # assumption: "read from each decision object's own candidate list").
            _refuse(f"{where}: {value!r} is not a candidate of the {state} decision")


def shadow_plan(case):
    """drive_shadow_policy: {configured, policy_mode, role_mode, invocation_points,
    vocabulary, entries}; AdapterRefusal for any shadow entry that cannot be delivered."""
    if not isinstance(case, dict) or not isinstance(case.get("id"), str) or not case["id"]:
        _refuse("a case with an explicit id is required")
    roles = case.get("agent_roles") or {}
    live, substituted = set(roles.get("live") or []), set(roles.get("substituted") or [])
    schedule = case.get("injection_schedule") or []
    indexed = [(i, s) for i, s in enumerate(schedule) if is_shadow_entry(s)]
    configured = ROLE in live or ROLE in substituted
    if not configured:
        if indexed:
            _refuse("the case schedules shadow decisions but names no shadow_policy role")
        return {"capability": CAPABILITY, "configured": False, "policy_mode": "rules"}
    vocabulary = vocabulary_for(_task_of(case))
    role_mode = "live" if ROLE in live else "substituted"
    if role_mode == "live" and ROLE in substituted:
        _refuse("shadow_policy is both live and substituted in this case")
    if role_mode == "live" and indexed:
        _refuse("a live role's output is never replaced (shadow_policy is live in this case)")
    if indexed and len(indexed) != len(schedule):
        # One InjectionHooks schedule index space: shadow entries are delivered here, so a
        # schedule mixing them with other entries is not mapped (no frozen case mixes them).
        _refuse("a schedule mixing shadow decision entries with other entries is not mapped")
    entries, scheduled = [], set()
    for index, spec in indexed:
        selector = spec.get("selector") if isinstance(spec.get("selector"), dict) else {}
        state, occurrence = selector.get("role_or_tool_or_stage"), selector.get("occurrence", 1)
        where = f"schedule entry {index} ({state}, {OPERATION})"
        if "id" in spec or "requires" in spec:
            _refuse(f"{where}: dependent shadow schedules are not mapped")
        if state not in vocabulary:
            _refuse(f"{where}: {state!r} is not a shadow invocation point of this profile")
        if type(occurrence) is not int or occurrence < 1:
            _refuse(f"{where}: occurrence must be a positive integer")
        if (state, occurrence) in scheduled:
            _refuse(f"{where}: two decisions scheduled for one invocation")
        scheduled.add((state, occurrence))
        _check_payload(where, state, spec.get("payload"), vocabulary)
        entries.append(
            {
                "index": index,
                "hook": HOOK,
                "selector": {"role_or_tool_or_stage": state, "occurrence": occurrence},
                "operation": OPERATION,
                "payload": copy.deepcopy(spec["payload"]),
                "frozen_payload_hash": digest(spec["payload"]),
                "adapter_mapping": "after_transition <S> replace_role_output -> the output of "
                "the shadow_policy invocation the controller makes right after that emit",
            }
        )
    if role_mode == "substituted":
        # No frozen shadow baseline exists: every first invocation needs its scheduled decision.
        uncovered = [s for s in vocabulary if (s, 1) not in scheduled]
        if uncovered:
            _refuse(
                f"substituted shadow_policy has no frozen baseline; no decision is scheduled "
                f"for {uncovered}"
            )
    return {
        "capability": CAPABILITY,
        "configured": True,
        "policy_mode": shadow.MODE,
        "role_mode": role_mode,
        "invocation_points": list(vocabulary),
        "vocabulary": copy.deepcopy(vocabulary),
        "candidate_source": "tools.workflow.policy.shadow_candidates over the profile states",
        "entries": entries,
    }


def schedule_without_shadow(case, plan):
    """The case with the shadow entries this capability delivers removed (InjectionHooks and
    its static findings see only the rest; shadow_plan refuses any mixed schedule)."""
    if not plan.get("entries"):
        return case
    indexes = {e["index"] for e in plan["entries"]}
    out = copy.deepcopy(case)
    out["injection_schedule"] = [
        s for i, s in enumerate(case.get("injection_schedule") or []) if i not in indexes
    ]
    return out


def policy_mode(plan):
    return plan["policy_mode"]


# --------------------------------------------------------------------------- delivery


def question_body_hash(question):
    """policy.shadow_question fingerprints every key except question_hash and context."""
    return digest({k: v for k, v in question.items() if k not in ("question_hash", "context")})


class ShadowDriver:
    """The controller hook (after_transition arming) and the substituted double's decision
    source. One driver per run; attached to the run's SubstitutedRoles as ``roles.shadow``."""

    def __init__(self, plan):
        self.plan = plan
        self.counts = Counter()
        self.armed = None
        self.delivery = None
        self.questions = {}  # state -> latest question_hash answered in this run
        self.records = []
        self.done = set()

    # ---- controller hook
    def __call__(self, point, event, controller):
        if point != HOOK:
            return
        state = event.get("state")
        self.counts[state] += 1
        for entry in self.plan["entries"]:
            selector = entry["selector"]
            if entry["index"] in self.done or selector["role_or_tool_or_stage"] != state:
                continue
            if selector["occurrence"] != self.counts[state]:
                continue
            if self.armed is not None:
                self.armed["record"]["status"] = "UNCONSUMED"
            record = {
                "index": entry["index"],
                "hook": HOOK,
                "state": state,
                "occurrence": self.counts[state],
                "status": "ARMED",
            }
            self.records.append(record)
            self.done.add(entry["index"])
            self.armed = {"entry": entry, "record": record}

    # ---- substituted double (role_substitution.SubstitutedRoles.baseline)
    def decide(self, question):
        Unavailable = role_substitution.SubstitutionUnavailable
        if not isinstance(question, dict) or not isinstance(question.get("state"), str):
            raise Unavailable("the shadow_policy payload is not a controller shadow question")
        state = question["state"]
        if question.get("role") != ROLE or question.get("schema") != policy.SHADOW_SCHEMA:
            raise Unavailable("the shadow_policy payload is not a controller shadow question")
        if question.get("question_hash") != question_body_hash(question):
            raise Unavailable("the shadow question's fingerprint does not bind its body")
        if question.get("candidates") != self.plan["vocabulary"].get(state):
            raise Unavailable(
                f"the controller's {state} candidates differ from the declared vocabulary"
            )
        armed = self.armed
        if armed is None or armed["entry"]["selector"]["role_or_tool_or_stage"] != state:
            raise Unavailable(
                f"no shadow decision is scheduled for this {state} question; a substituted "
                "shadow policy has no frozen baseline"
            )
        self.armed = None
        self.questions[state] = question["question_hash"]
        resolutions = []
        value = {}
        for key, item in armed["entry"]["payload"].items():
            value[key] = self._resolve(key, item, question, resolutions)
        record = armed["record"]
        record.update(question_hash=question["question_hash"], resolutions=resolutions)
        self.delivery = {"entry": armed["entry"], "record": record, "value": copy.deepcopy(value)}
        return value

    def _resolve(self, key, item, question, resolutions):
        if not isinstance(item, str):
            return copy.deepcopy(item)
        label = LABEL.fullmatch(item)
        if key == "action" and label is not None:
            native = question["candidates"][label.group(2)]
            resolutions.append(
                {
                    "path": f"/{key}",
                    "symbol": item,
                    "value": native,
                    "source": f"candidates.{label.group(2)} of the controller's "
                    f"{question['state']} question",
                }
            )
            return native
        fingerprint = FINGERPRINT.fullmatch(item)
        if key == "question_fingerprint" and fingerprint is not None:
            named = fingerprint.group(1)
            if named in self.questions:
                native = self.questions[named]
                source = (
                    "question_hash of the controller question being answered"
                    if named == question["state"]
                    else f"question_hash of the controller's earlier {named} question (stale)"
                )
            else:
                native = digest({"harness_stale_fingerprint": item})
                source = "harness_authored_stale_fingerprint"
            resolutions.append(
                {
                    "path": f"/{key}",
                    "symbol": item,
                    "value": native,
                    "source": source,
                    "rule_id": STALE_FINGERPRINT_RULE_ID,
                }
            )
            return native
        return item

    def bind_receipt(self, receipt):
        """The double's own receipt for the delivered decision: no baseline, one substitution
        record naming the schedule entry, the delivered hash and every symbol resolution."""
        delivery, self.delivery = self.delivery, None
        if delivery is None or receipt.get("role") != ROLE:
            raise role_substitution.SubstitutionBindingError("no shadow decision was delivered")
        delivered = digest(delivery["value"])
        if receipt.get("output_hash") != delivered:
            raise role_substitution.SubstitutionBindingError(
                "the receipt does not bind the delivered shadow decision"
            )
        record = delivery["record"]
        receipt["baseline_output_hash"] = None
        receipt.setdefault("substitutions", []).append(
            {
                "hook": HOOK,
                "schedule_indexes": [delivery["entry"]["index"]],
                "operations": [OPERATION],
                "pre_substitution_output_hash": None,
                "delivered_output_hash": delivered,
                "frozen_payload_hash": delivery["entry"]["frozen_payload_hash"],
                "adapter_mappings": [delivery["entry"]["adapter_mapping"]],
                "resolutions": copy.deepcopy(record["resolutions"]),
            }
        )
        record.update(
            status="APPLIED",
            invocation_id=receipt.get("invocation_id"),
            session_id=receipt.get("session_id"),
            delivered_output_hash=delivered,
        )

    def report(self):
        applied = [r["index"] for r in self.records if r["status"] == "APPLIED"]
        return {
            "capability": CAPABILITY,
            "policy_mode": self.plan["policy_mode"],
            "invocation_points": list(self.plan["invocation_points"]),
            "vocabulary": copy.deepcopy(self.plan["vocabulary"]),
            "entries": [
                {k: copy.deepcopy(v) for k, v in e.items() if k != "payload"}
                for e in self.plan["entries"]
            ],
            "coverage": {
                "scheduled": len(self.plan["entries"]),
                "applied": applied,
                "unreached": [e["index"] for e in self.plan["entries"] if e["index"] not in applied],
                "records": copy.deepcopy(self.records),
            },
            "stale_fingerprint_rule": {
                "rule_id": STALE_FINGERPRINT_RULE_ID,
                "rule": STALE_FINGERPRINT_RULE,
            },
        }


def attach(plan, roles):
    """The run's ShadowDriver bound to its substituted double, or None (no substituted shadow
    policy). A live shadow policy needs the CAP-LIVE-ROLE transport (refused elsewhere)."""
    if not plan.get("configured") or plan.get("role_mode") != "substituted":
        return None
    if not isinstance(roles, role_substitution.SubstitutedRoles) or ROLE not in roles.allowed:
        _refuse("a substituted shadow policy needs this case's substituted roles factory")
    driver = ShadowDriver(plan)
    roles.shadow = driver
    return driver


def chain(*hooks):
    """One Controller hooks callable running each non-None hook in order."""
    present = [h for h in hooks if h is not None]
    if not present:
        return None
    if len(present) == 1:
        return present[0]

    def chained(point, event, controller):
        for hook in present:
            hook(point, event, controller)

    return chained


# --------------------------------------------------------------------------- projection


def _contract_label(value, question):
    if not isinstance(value, str) or question is None:
        return value
    for meaning, label in question["candidates"].items():
        if value == label:
            return f"HARNESS_CANDIDATE_LABEL:{question['state']}.{meaning}"
    return value


def project(projection, native):
    """controller_trace.shadow_decisions, role_output.shadow_policy and the shadow symbols of
    one projected run (evidence_projection.project_run). Tampered or inconsistent native shadow
    records refuse the projection; a list that cannot be read is MISSING with its reason."""
    evidence = projection["evidence"]
    trace = evidence["controller_trace"]
    path = "controller_trace.shadow_decisions"

    def gap(reason):
        projection["missing"].append({"path": path, "capability": CAPABILITY, "reason": reason})

    shadow_records = [
        (receipt, record)
        for receipt, record in zip(native.receipts, native.role_files, strict=True)
        if receipt["role"] == ROLE
    ]
    mode = native.checkpoint.get("policy_mode")
    if mode is None:
        gap("the checkpoint records no policy_mode")
        return
    decisions = native.context.get("shadow_decisions")
    events = native.kinds("shadow_decision")
    if mode != shadow.MODE:
        if shadow_records or decisions is not None or events:
            raise _mapping_error("shadow policy records exist outside shadow_policy mode")
        trace["shadow_decisions"] = []
        projection["notes"].append(
            {
                "shadow_policy": f"not configured (policy_mode {mode!r}): the controller "
                "asks no shadow question, so no shadow decision exists"
            }
        )
        return
    if decisions is None:
        if native.finished() and not events and not shadow_records:
            trace["shadow_decisions"] = []
            projection["notes"].append(
                {"shadow_policy": "shadow_policy mode; the finished run reached no invocation point"}
            )
        else:
            gap("shadow_policy mode but the controller recorded no shadow_decisions list")
        return
    if not isinstance(decisions, list) or len(events) != len(decisions):
        raise _mapping_error("shadow decisions differ from their dispatch events")
    for k, (decision, event) in enumerate(zip(decisions, events, strict=True)):
        name = f"shadow-decision-{k:03}.json"
        if (
            not isinstance(decision, dict)
            or decision.get("index") != k
            or event.get("decision") != decision
            or name not in native.checkpoint["evidence"]
            or _read(native, name) != decision
        ):
            raise _mapping_error(f"shadow decision {k} differs from its native record")
    pairs, j = [], 0
    for decision in decisions:
        record = shadow_records[j][1] if j < len(shadow_records) else None
        question = record.get("input") if record is not None else None
        if (
            isinstance(question, dict)
            and question.get("question_hash") == decision["question_hash"]
            and question.get("state") == decision["state"]
            and digest(record.get("output")) == decision["output_hash"]
        ):
            if question_body_hash(question) != question["question_hash"]:
                raise _mapping_error("a dispatched shadow question differs from its fingerprint")
            pairs.append((decision, shadow_records[j]))
            j += 1
        elif isinstance(decision.get("reason"), str) and decision["reason"].startswith(
            "role unavailable"
        ):
            pairs.append((decision, None))  # the dispatch failed: no receipt, no output
        else:
            raise _mapping_error("a shadow decision has no matching shadow_policy invocation")
    if j != len(shadow_records):
        raise _mapping_error("a shadow_policy invocation has no controller shadow decision")
    by_state = {}
    for _, paired in pairs:
        if paired is not None:
            by_state.setdefault(paired[1]["input"]["state"], set()).add(
                paired[1]["input"]["question_hash"]
            )
    fingerprints = {
        next(iter(hashes)): state for state, hashes in by_state.items() if len(hashes) == 1
    }
    harness = {}
    for receipt, _ in shadow_records:
        for sub in receipt.get("substitutions") or []:
            if sub.get("hook") == HOOK:
                for r in sub.get("resolutions") or []:
                    if r["path"] == "/question_fingerprint":
                        harness[r["value"]] = r
    mapped, outputs, out_decisions = [], [], []
    for k, (decision, paired) in enumerate(pairs):
        question = paired[1]["input"] if paired is not None else None
        projected = _detached(decision)
        projected["action"] = _contract_label(decision.get("action"), question)
        if projected["action"] != decision.get("action"):
            mapped.append({"decision": k, "field": "action", "native": decision["action"]})
        out_decisions.append(projected)
        if paired is None:
            continue
        output = _detached(paired[1]["output"])
        if isinstance(output, dict):
            action = _contract_label(output.get("action"), question)
            if action != output.get("action"):
                mapped.append({"output": len(outputs), "field": "action", "native": output["action"]})
                output["action"] = action
            native_fp = output.get("question_fingerprint")
            if isinstance(native_fp, str) and native_fp in fingerprints:
                output["question_fingerprint"] = (
                    f"HARNESS_DECISION_FINGERPRINT:{fingerprints[native_fp]}"
                )
            elif isinstance(native_fp, str) and native_fp in harness:
                output["question_fingerprint"] = harness[native_fp]["symbol"]
            if output.get("question_fingerprint") != native_fp:
                mapped.append(
                    {"output": len(outputs), "field": "question_fingerprint", "native": native_fp}
                )
        outputs.append(output)
    trace["shadow_decisions"] = out_decisions
    evidence["role_output"][ROLE] = outputs
    symbols = evidence.setdefault("symbols", {})
    for _, paired in pairs:
        if paired is None:
            continue
        question = paired[1]["input"]
        for meaning, label in question["candidates"].items():
            symbols[f"HARNESS_CANDIDATE_LABEL:{question['state']}.{meaning}"] = label
    for question_hash, state in fingerprints.items():
        symbols[f"HARNESS_DECISION_FINGERPRINT:{state}"] = question_hash
    projection["notes"].append(
        {
            "shadow_policy": {
                "policy_mode": mode,
                "invocation_points_reached": [d["state"] for d in decisions],
                "contract_vocabulary": "native candidate labels and question fingerprints are "
                "projected as HARNESS_CANDIDATE_LABEL:<S>.<meaning> / "
                "HARNESS_DECISION_FINGERPRINT:<S> (symbols carries the native values); a "
                "harness-authored stale fingerprint maps back to its symbol from the receipt",
                "mapped": mapped,
                "ambiguous_fingerprint_states": sorted(
                    s for s, hashes in by_state.items() if len(hashes) > 1
                ),
                "harness_resolutions": sorted(
                    (r for r in harness.values() if r["source"].startswith("harness_")),
                    key=lambda r: r["symbol"],
                ),
            }
        }
    )


def _read(native, name):
    try:
        return json.loads((native.root / name).read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise _mapping_error(f"unreadable native record: {name}") from exc
