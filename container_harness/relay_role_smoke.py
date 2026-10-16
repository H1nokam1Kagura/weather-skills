"""Relay ROLE smoke, run INSIDE the harness container (uid 1000) on the relay-only network.

Started by ``launch.py relay-smoke --client relay_role_smoke`` as the container's own command
(never via docker exec). It de-risks every LIVE role of the frozen v2.7 live wave before any
live case spends money, on the one point the reviewer-only dedup smoke could not reach: the
CONTROLLER'S OWN ACCEPTANCE of each role's forced-tool answer. Run 38906458002 showed why: the
reviewer's forced-tool schema offered two answer shapes as one union, the model mixed them, the
relay reported SUCCESS, and only the controller would have refused it. The planner and repairer
schemas are open unions too (``tools/workflow/role_schemas.py``).

Which calls (``CALLS``): one per live role and per payload VARIANT the 34 live cases send
(LIVE-WAVE-BRIEF "Case groups" + the frozen ``agent_roles``; every live case runs the
controller with review evidence and claim gates on, ``weather_adapter.run_case``):

* compiler (compiler_1..3 are three asks of one native role with one payload);
* planner with ``review_evidence_request`` (its only live form);
* repairer with ``review_evidence_request`` (``repair`` spreads the planner payload), after a
  rejected review (the frozen E06-P1-INJ reviewer replacement);
* evidence_checker (claim gates on in every live case);
* reviewer with ``review_evidence`` + ``claim_check``, and the same with the planner's optional
  ``rationale`` in the envelope (a different system prompt: ``REVIEW_RATIONALE_SUFFIX``; the
  planner's forced-tool schema offers ``rationale``, so a live planner can put it there);
* verifier;
* shadow_policy (E09-P2 only), with the frozen E09-P2-INJ context note patched in.

Not called: router (no live case asks it: policy mode is ``rules`` or ``shadow_policy``,
``runner.py`` skips the router for both) and rubric_grader (report-side; its model is not
probed here).

How the payloads are built: by the REAL controller. ``capture`` runs ``runner.Controller``
(real constructor, real ``drive``/``_run``/``ask``, real claim gates, preflight and review
capture) over a stub source file, with a scripted role double (``ScriptedRoles``: the frozen
substituted baselines of ``adapter_support.role_substitution`` plus the frozen case answers
above) and records every payload the controller hands to a role. Only the verifier payload is
assembled here (the controller asks it only after executing tools), from the controller's own
parts (``execution_context``, ``read_gate``, the run binding) in ``runner._run``'s key order.

How the answers are judged: by the REAL controller. ``judge`` replays the same scripted run on a
fresh controller with the live answer delivered at that role's occurrence and observes what
the controller does with it: refused (``Refused`` raised, its text and gate recorded),
NEEDS_INFO (the frozen needs_info stop), or accepted (the controller went on to its next role
or transition). The replay must hand the role the byte-identical payload that went live
(``payload_identical``). The planner's refusal is observed on ``accept_planner`` (the run would
otherwise route a refused plan to repair and continue); the shadow decision is the
controller's own ``shadow_decisions`` record (never raises by design); the verifier is judged
by ``Controller.record_verifier`` plus the run's release condition on that record's fields.

Prints exactly one JSON object on stdout: key absence and egress probes (relay_smoke /
relay_dedup_smoke, reused), relay health, then one record per call (prediction, outcome,
answer, redacted receipt, ``schema_errors`` against the forced-tool schema, the controller's
judgement), then the request-cap probe (relay_smoke.cap_probe) that exhausts the relay.

Data policy: the scenario is the frozen synthetic v2.7 reference fixture, Not Sensitive.
"""

import copy
import json
import os
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
# container root and its tests (frozen_v27); repo root and its tests; this dir
_PATHS = ("/opt/ctl", "/opt/ctl/tests", str(HERE.parent), str(HERE.parent / "tests"), str(HERE))
for _path in _PATHS:
    if _path not in sys.path:
        sys.path.insert(0, _path)

import relay_dedup_smoke as dedup  # noqa: E402  (wire view, key absence, refusal fields)
import relay_smoke  # noqa: E402  (probes, cap probe)

SCHEMA = "simkit.relay-role-smoke.container/1"
FROZEN_CASES = "casesets/cached-onset-e2e-v2_7/frozen/agentic-e2e-injections.DEV-ONLY.json"
REJECT_CASE = "DEV-E2E-V2_7-E06-P1-INJ"  # reviewer #1 replaced by a REJECT -> live repairer
SHADOW_NOTES_CASE = "DEV-E2E-V2_7-E09-P2-INJ"  # json_patch of the first shadow question's notes
PAIR = "E06-P1"
# The planner's optional envelope rationale (unpack_plan binds it; the reviewer sees it).
RATIONALE = (
    "The plan keeps the registered two-step profile unchanged. The colour scale ends on "
    "2025-12-02, the last eligible onset date, because each candidate needs 30 complete days "
    "of input and the input ends on 2025-12-31."
)
BUDGET_USD = 2  # the workflow's RELAY_BUDGET; the relay's own guard is authoritative
CALL_TIMEOUT = 240
RECEIPT_KEEP = dedup.RECEIPT_KEEP

# (call id, scenario, native role, occurrence in that scenario's run, variant, why)
CALLS = (
    {
        "call": "compiler",
        "scenario": "reject_then_repair",
        "role": "compiler",
        "occurrence": 1,
        "variant": "goal compilation",
        "why": "compiler_1..3 are live in groups A, A', D(E08), F, H; three asks, one payload",
    },
    {
        "call": "planner",
        "scenario": "reject_then_repair",
        "role": "planner",
        "occurrence": 1,
        "variant": "review_evidence_request",
        "why": "every live case runs review evidence mode, so the planner always gets the "
        "envelope request (live in A, A', B(E15), D(E08), F, H)",
    },
    {
        "call": "evidence_checker",
        "scenario": "reject_then_repair",
        "role": "evidence_checker",
        "occurrence": 1,
        "variant": "claim gates (judged claims of the presented plan version)",
        "why": "claim gates are on in every live case (bound definition source); live in all "
        "groups but E01-P2 and E09",
    },
    {
        "call": "reviewer",
        "scenario": "reject_then_repair",
        "role": "reviewer",
        "occurrence": 1,
        "variant": "review_evidence + claim_check",
        "why": "the reviewer always gets the envelope and the claim-check record live; the "
        "union schema run 38906458002 broke",
    },
    {
        "call": "repairer",
        "scenario": "reject_then_repair",
        "role": "repairer",
        "occurrence": 1,
        "variant": "review_evidence_request, after a rejected review (E06-P1-INJ)",
        "why": "repairer is live in E06, E07, E13; its payload spreads the planner's, so it "
        "always carries review_evidence_request",
    },
    {
        "call": "reviewer_rationale",
        "scenario": "planner_rationale",
        "role": "reviewer",
        "occurrence": 1,
        "variant": "review_evidence (with planner rationale) + claim_check",
        "why": "a live planner may return the optional rationale its schema offers; the "
        "reviewer then gets REVIEW_RATIONALE_SUFFIX and the rationale in the envelope",
    },
    {
        "call": "verifier",
        "scenario": None,
        "role": "verifier",
        "occurrence": 1,
        "variant": "final data-replay gates",
        "why": "verifier is live in every live group",
    },
    {
        "call": "shadow_policy",
        "scenario": "reject_then_repair",
        "role": "shadow_policy",
        "occurrence": 1,
        "variant": "GOAL_CHECKED question with the E09-P2-INJ context note",
        "why": "shadow_policy is live only in E09-P2 (group D), whose first question carries "
        "a patched context note",
    },
)
ROLES = sorted({c["role"] for c in CALLS})
# The relay caps the workflow job passes to relay_ci.sh relay-start (a test pins them): every
# role above, one request per call (then the cap probe exhausts the relay), the live cases'
# per-call output ceiling (planner/repairer keep LIVE_ROLE_MAX_TOKENS 4096), a token cap above
# the summed worst case of every call and a USD 2 budget above the summed worst-case price.
RELAY_CAPS = {
    "RELAY_ROLES": " ".join(ROLES),
    "RELAY_MAX_REQUESTS": str(len(CALLS)),
    "RELAY_MAX_TOKENS": "2048",
    "RELAY_MAX_TOTAL_TOKENS": "500000",
    "RELAY_BUDGET": str(BUDGET_USD),
}


# ------------------------------------------------------------------ frozen inputs


def frozen_inputs():
    """The frozen synthetic scenario: reference goal, source ledger, source metadata,
    evidence template, the E06-P1-INJ reviewer REJECT and the E09-P2-INJ note patch."""
    from case_driver import frozen_root  # verified package; also puts its simkit on sys.path

    root, _ = frozen_root()
    doc = json.loads((root / FROZEN_CASES).read_text(encoding="utf-8"))
    cases = {c["id"]: c for c in doc["cases"]}

    def entry(case_id, role, operation):
        (spec,) = [
            s
            for s in cases[case_id]["injection_schedule"]
            if s["operation"] == operation
            and s["selector"] == {"role_or_tool_or_stage": role, "occurrence": 1}
        ]
        return copy.deepcopy(spec["payload"])

    ref = doc["reference_fixture"]
    return {
        "goal": copy.deepcopy(ref["goal"]),
        "source_ledger": copy.deepcopy(ref["source_ledger"]),
        "source_metadata": copy.deepcopy(ref["source_metadata"]),
        "evidence_template": copy.deepcopy(ref["valid_evidence_template"]),
        "reviewer_reject": entry(REJECT_CASE, "reviewer", "replace_role_output"),
        "shadow_patch": entry(SHADOW_NOTES_CASE, "shadow_policy", "json_patch"),
    }


# ------------------------------------------------------------------ scripted role double


class StopRun(BaseException):  # noqa: N818 - control flow, never an error of the run
    """Raised by the double/hook to end a capture or judge replay at a chosen point. A
    BaseException, so no ``except Exception`` in the controller can absorb it."""

    def __init__(self, at):
        super().__init__(at)
        self.at = at


class _Shadow:
    """The shadow decision a substituted shadow_policy returns (a valid, accepted one)."""

    def bind_receipt(self, receipt):
        pass

    def decide(self, question):
        return {
            "schema": question["schema"],
            "state": question["state"],
            "question_fingerprint": question["question_hash"],
            "action": question["admissible_actions"][0],
            "score": 0.9,
        }


def scripted_roles_class():
    from adapter_support import role_substitution as rs

    class ScriptedRoles(rs.SubstitutedRoles):
        """The frozen substituted double (receipts, sessions, review capture hashes) with a
        script of (role, occurrence) -> answer overrides. Records every payload it is asked.
        ``judged`` (role, occurrence): once that answer was delivered, the controller's next
        role ask ends the replay (``StopRun``): the controller moved on."""

        def __init__(self, goal, script, judged=None):
            case = {
                "id": f"DEV-E2E-V2_7-{PAIR}-INJ",
                "agent_roles": {"substituted": [*rs.NATIVE_ROLE, rs.SHADOW_ROLE]},
            }
            super().__init__(case, goal)
            self.shadow = _Shadow()
            self.script = script
            self.judged = judged
            self.delivered = False
            self.asked = []  # (role, occurrence, payload copy)

        def ask(self, role, payload):
            occurrence = 1 + sum(r == role for r, _, _ in self.asked)
            if self.delivered:
                raise StopRun(f"{role}#{occurrence}")
            self.asked.append((role, occurrence, json.loads(json.dumps(payload))))
            self.current = (role, occurrence)
            value = super().ask(role, payload)
            if (role, occurrence) == self.judged:
                self.delivered = True
            return value

        def baseline(self, role, payload):
            key = getattr(self, "current", None)
            if key in self.script:
                return copy.deepcopy(self.script[key])
            return super().baseline(role, payload)

    return ScriptedRoles


# ------------------------------------------------------------------ the real controller


class _Processes:
    total_seconds = 1800


SCENARIOS = ("reject_then_repair", "planner_rationale")


def scenario_script(name, inputs):
    """(role, occurrence) -> answer, on top of the frozen baselines."""
    if name == "reject_then_repair":
        return {("reviewer", 1): inputs["reviewer_reject"]}
    if name == "planner_rationale":
        from adapter_support import role_substitution as rs

        planner = rs.static_baseline("planner", inputs["goal"], PAIR)
        return {("planner", 1): {**planner, "rationale": RATIONALE}}
    raise KeyError(name)


def new_controller(work, inputs, roles, tag):
    """A real Controller as a live case constructs it (weather_adapter.run_case): seeded
    request, review evidence mode, the pinned reference definition (claim gates on), shadow
    policy mode. The source is a stub file: only its tree hash binds; inspect_source and the
    source check are the two controller methods replaced on the instance, by the frozen
    reference metadata and a source gate that the real ``read_gate`` accepts."""
    from case_driver import DEFINITION_PATH, reference_definition

    from adapter_support.weather_adapter import WeatherAdapter
    from tools.workflow import runner, shadow
    from tools.workflow.contracts import Refused

    work = Path(work)
    repo = Path(runner.__file__).resolve().parents[2]
    source = work / "source.zarr"
    if not source.exists():
        source.write_bytes(b"relay role smoke: stub source; only its hash is bound\n")
    root = Path(tempfile.mkdtemp(prefix=f"run-{tag}-", dir=work))
    seeded = WeatherAdapter().seed_ledger(
        {"request_delta": []}, {"goal": inputs["goal"]}, inputs["source_ledger"]
    )
    hooks = ControllerHooks(inputs)
    c = runner.Controller(
        repo,
        seeded["request"],
        source,
        root,
        _Processes(),
        roles,
        hooks=hooks,
        review_evidence_mode=True,
        definition_source=reference_definition(DEFINITION_PATH),
        policy_mode=shadow.MODE,
    )
    meta = inputs["source_metadata"]
    metadata = {
        "dims": dict(meta["dimensions"]),
        "bounds": list(meta["cell_center_bbox"]),
        "start": inputs["goal"]["start_date"],
        "end": inputs["goal"]["end_date"],
        "units": meta["units"],
        "source": "chirps",
    }
    c.inspect_source = lambda: copy.deepcopy(metadata)

    def check(name, state):
        if name != "source":
            raise Refused("relay role smoke: only the source check is answered")
        gate = {**inputs["evidence_template"]["check_gate"], "artifact": str(c.paths[name])}
        process = {"exit_code": 0, "stdout": json.dumps(gate)}
        return runner.read_gate(process, "check-artifact.gate/1", c.paths[name])

    c.check = check
    return c


class ControllerHooks:
    """The case hooks a live E06/E09 run has, at the controller's own hook points: the
    E09-P2-INJ json_patch of the first shadow question (before_role), and the end of the
    replay at REVIEW_APPROVED (after_transition), before any tool would execute."""

    def __init__(self, inputs):
        self.inputs = inputs
        self.shadow_seen = 0

    def __call__(self, point, event, controller):
        from tools.workflow.e2e_injections import json_patch

        if point == "before_role" and event["role"] == "shadow_policy":
            self.shadow_seen += 1
            if self.shadow_seen == 1:
                payload = event["payload"]
                patched = json_patch(payload, self.inputs["shadow_patch"])
                payload.clear()
                payload.update(patched)
        if point == "after_transition" and event["state"] == "REVIEW_APPROVED":
            raise StopRun("transition:REVIEW_APPROVED")


def drive(c):
    """Run the real controller until StopRun; returns (stop, exception)."""
    try:
        c.drive()
    except StopRun as stop:
        return stop.at, None
    except Exception as exc:  # noqa: BLE001 - the judge records the controller's refusal
        return None, exc
    return "RELEASED", None


def capture(work, inputs=None):
    """Every role payload the real controller builds in the smoke scenarios, by call id."""
    inputs = inputs or frozen_inputs()
    ScriptedRoles = scripted_roles_class()
    payloads, contexts = {}, {}
    for name in SCENARIOS:
        roles = ScriptedRoles(inputs["goal"], scenario_script(name, inputs))
        c = new_controller(work, inputs, roles, f"capture-{name}")
        stop, exc = drive(c)
        if exc is not None or stop != "transition:REVIEW_APPROVED":
            raise RuntimeError(f"capture scenario {name} did not reach review: {stop} {exc!r}")
        for call in CALLS:
            if call["scenario"] != name:
                continue
            hits = [p for r, n, p in roles.asked if (r, n) == (call["role"], call["occurrence"])]
            if len(hits) != 1:
                raise RuntimeError(f"capture: {call['call']} was not asked in {name}")
            payloads[call["call"]] = hits[0]
        contexts[name] = c
    payloads["verifier"] = verifier_payload(contexts["reject_then_repair"], inputs)
    return payloads, contexts


def verifier_payload(c, inputs):
    """The verifier payload in ``runner._run``'s key order, from the controller's own parts:
    the final binding over the review binding, artifacts and replay gates (each gate the real
    ``read_gate`` accepts with replay=True) and ``execution_context``. Synthetic artifacts."""
    from tools.workflow import runner
    from tools.workflow.contracts import digest

    artifacts = dict(c.artifacts)
    gates = {}
    for name in ("onset", "map"):
        artifacts[name] = digest({"synthetic_artifact": name})
        gate = {**inputs["evidence_template"]["replay_gate"], "artifact": str(c.paths[name])}
        process = {"exit_code": 0, "stdout": json.dumps(gate)}
        gates[name] = runner.read_gate(process, "verify-run.gate/1", c.paths[name], replay=True)
    final_binding = digest({"review_binding": c.binding, "artifacts": artifacts, "gates": gates})
    return {
        "binding_hash": final_binding,
        "artifacts": artifacts,
        "gates": gates,
        "execution_context": c.execution_context(),
    }


# ------------------------------------------------------------------ judge

SHADOW_RECORD_KEYS = ("state", "accepted", "action", "score", "reason")
VERIFIER_RECORD_KEYS = ("verdict", "sub_reason", "binding_valid", "scope_valid")


def _refusal(exc):
    return {"error_type": type(exc).__name__, "text": str(exc), "gate": getattr(exc, "gate", None)}


def judge(call, answer, sent_payload, work, inputs, verifier_controller=None):
    """The controller's own decision on ``answer``. Never decides anything itself: it
    replays the real controller with the answer delivered and records what the controller
    did. ``controller_accepts`` is None when the replay could not reach the answer."""
    from tools.workflow.contracts import NeedsInfo, Refused, digest

    record = {"controller_accepts": None, "controller_outcome": None, "controller_refusal": None}
    if call["role"] == "verifier":
        return judge_verifier(answer, sent_payload, verifier_controller, record)
    ScriptedRoles = scripted_roles_class()
    script = dict(scenario_script(call["scenario"], inputs))
    key = (call["role"], call["occurrence"])
    script[key] = answer
    roles = ScriptedRoles(inputs["goal"], script, judged=key)
    c = new_controller(work, inputs, roles, f"judge-{call['call']}")
    planner_outcome = {}
    if call["role"] == "planner":
        # A refused planner plan is routed to repair inside _run, so the refusal is observed
        # on the controller's own accept_planner call (re-raised unchanged).
        accept_planner = c.accept_planner

        def observed(payload):
            try:
                return accept_planner(payload)
            except BaseException as exc:
                planner_outcome["exc"] = exc
                raise

        c.accept_planner = observed
    stop, exc = drive(c)
    hits = [p for r, n, p in roles.asked if (r, n) == key]
    record["payload_identical"] = bool(hits) and digest(hits[0]) == digest(sent_payload)
    record["payload_source"] = "controller_capture"
    record["replay_stop"] = stop
    if not roles.delivered:
        record["controller_outcome"] = "NOT_REACHED"
        if exc is not None:
            record["controller_refusal"] = _refusal(exc)
        return record
    exc = planner_outcome.get("exc", exc)
    if isinstance(exc, StopRun):
        exc = None
    if isinstance(exc, NeedsInfo):
        record.update(controller_accepts=True, controller_outcome="NEEDS_INFO")
        record["needs_info_question"] = exc.question
    elif isinstance(exc, Refused):
        record.update(controller_accepts=False, controller_outcome="REFUSED")
        record["controller_refusal"] = _refusal(exc)
    elif exc is not None:
        record.update(controller_outcome="ERROR", controller_refusal=_refusal(exc))
    elif call["role"] == "shadow_policy":
        decisions = c.context.get("shadow_decisions") or []
        index = call["occurrence"] - 1
        decision = decisions[index] if len(decisions) > index else {}
        accepted = decision.get("accepted")
        record.update(
            controller_accepts=accepted if isinstance(accepted, bool) else None,
            controller_outcome="ACCEPTED" if accepted is True else "REJECTED",
            shadow_decision={k: decision.get(k) for k in SHADOW_RECORD_KEYS},
        )
        if accepted is False:
            record["controller_refusal"] = {"text": decision.get("reason"), "gate": "shadow"}
    else:
        record.update(controller_accepts=True, controller_outcome="ACCEPTED")
    record["judgment"] = judgment(call["role"], c, answer)
    return record


def judge_verifier(answer, sent_payload, c, record):
    """``Controller.record_verifier`` on a real controller, then the run's release condition
    (runner.py ``_run``: PASS and binding_valid and scope_valid) on that record's own fields."""
    from tools.workflow.contracts import Refused

    # Assembled once (verifier_payload); the judged binding is the one that payload sent.
    record.update(payload_identical=True, payload_source="assembled_from_controller_parts")
    try:
        recorded = c.record_verifier(answer, sent_payload["binding_hash"])
    except Refused as exc:
        record.update(controller_accepts=False, controller_outcome="REFUSED")
        record["controller_refusal"] = _refusal(exc)
        return record
    copied = recorded["binding_valid"] and recorded["scope_valid"]
    record.update(
        controller_accepts=bool(copied),
        controller_outcome="ACCEPTED" if copied else "REFUSED",
        verifier_record={k: recorded[k] for k in VERIFIER_RECORD_KEYS},
        judgment={
            "verdict": recorded["verdict"],
            "releases": bool(copied) and recorded["verdict"] == "PASS",
        },
    )
    if not copied:
        record["controller_refusal"] = {
            "text": "independent verification role did not confirm the exact final evidence "
            "(binding_valid / scope_valid false on the controller's verifier record)",
            "gate": "verifier",
        }
    return record


def judgment(role, c, answer):
    """The live answer's judgement as the controller recorded it (diagnostic, never gating)."""
    value = answer if isinstance(answer, dict) else {}
    if role == "reviewer":
        return {"verdict": value.get("verdict"), "findings": len(value.get("findings") or [])}
    if role == "evidence_checker":
        records = c.context["review_evidence"].get("claim_checks") or []
        last = records[-1] if records else {}
        return {
            "verdicts": {v.get("claim_id"): v.get("verdict") for v in value.get("verdicts") or []},
            "claim_check_decision": last.get("decision"),
        }
    if role in ("planner", "repairer"):
        return {"keys": sorted(value), "needs_info": "needs_info" in value}
    if role == "compiler":
        return {"keys": sorted(value)}
    if role == "shadow_policy":
        return {"action": value.get("action"), "score": value.get("score")}
    return None


# ------------------------------------------------------------------ live calls


def predict(role, payload):
    fields, *_ = dedup.wire(role, payload)
    return fields


def run_smoke(roles, work, probes=None, inputs=None):
    """Health, every planned call (no retries) with its controller judgement, then the cap
    probe. ``probes``: a callable returning the egress probe rows (the container passes the
    real relay_smoke.probes; offline tests pass recorded rows: no network)."""
    from tools.workflow import role_schemas
    from tools.workflow.contracts import Refused
    from tools.workflow.relay_roles import RelayOutcomeUnknown

    out = {"relay": {"host": roles.host, "port": roles.port, "run_id": roles.run_id}}
    out["egress_probes"] = probes() if probes is not None else []
    try:
        health = roles.health()
        out["health"] = {k: health.get(k) for k in ("outcome", "run_id", "allowed_roles", "model")}
    except Refused as exc:
        out["health"] = {"outcome": "REFUSED", "reason": str(exc)}
    out["planned_calls"] = [c["call"] for c in CALLS]
    try:
        inputs = inputs or frozen_inputs()
        payloads, contexts = capture(work, inputs)
    except Exception as exc:  # noqa: BLE001 - recorded; no call is made, the report fails it
        out["capture_error"] = {"error_type": type(exc).__name__, "text": str(exc)[:2000]}
        payloads, contexts = {}, {}
    calls = []
    for call in CALLS if payloads else ():
        payload = payloads[call["call"]]
        prediction = predict(call["role"], payload)
        system, user = prediction.pop("_system"), prediction.pop("_user")
        record = {
            **{k: call[k] for k in ("call", "role", "variant", "why")},
            "variant_keys": [k for k in role_schemas.VARIANT_KEYS if k in payload],
            "prediction": prediction,
        }
        try:
            value = roles.ask(call["role"], payload)
        except RelayOutcomeUnknown as exc:
            record.update(outcome="UNKNOWN", evidence=exc.evidence)
        except Refused as exc:
            record.update(dedup.refused_fields(roles, exc))
        else:
            receipt = roles.receipts[-1]
            record.update(
                outcome="SUCCESS",
                answer=value,
                receipt={k: receipt.get(k) for k in RECEIPT_KEEP},
                gateway_body_bytes=dedup.gateway_body_bytes(
                    system, user, receipt.get("settings_sent")
                ),
                schema_errors=role_schemas.schema_errors(
                    role_schemas.answer_schema(call["role"], payload), value
                ),
            )
            try:
                record["controller"] = judge(
                    call, value, payload, work, inputs, contexts["reject_then_repair"]
                )
            except Exception as exc:  # noqa: BLE001 - a harness fault never reads as acceptance
                record["controller"] = {
                    "controller_accepts": None,
                    "controller_outcome": "JUDGE_ERROR",
                    "controller_refusal": _refusal(exc),
                }
        calls.append(record)
    out["calls"] = calls
    # Past a FULL request cap the relay refuses before forwarding (unpaid) and exhausts its
    # guard, so it stops itself; skipped by cap_probe itself when the cap is not full.
    out["cap_probe"] = relay_smoke.cap_probe(
        roles, Refused, role="verifier", payload=payloads.get("verifier")
    )
    out["client_spent_reserved"] = roles.spent
    return out


def main():
    out = {"schema": SCHEMA, "uid": os.getuid(), "payload_classification": "synthetic"}
    out.update(dedup.key_absence())
    targets = json.loads(os.environ.get("SIMKIT_PROBE_TARGETS") or "{}")

    from tools.workflow.relay_roles import RelayRoles

    roles = RelayRoles.from_env(budget=BUDGET_USD, timeout=CALL_TIMEOUT)
    work = Path(tempfile.mkdtemp(prefix="relay-role-smoke-"))
    out.update(
        run_smoke(roles, work, probes=lambda: relay_smoke.probes(targets, roles.host, roles.port))
    )
    print(json.dumps(out))
    return 0


if __name__ == "__main__":
    sys.exit(main())
