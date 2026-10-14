"""v2.7 controller lifecycle: gate codes, terminal outcomes, needs_info, verifier PASS/FAIL,
plan validation records, the post-commit role boundary, process receipts and repair parity.

Offline doubles only: no model, no network, no child process. The controller is the SUT.
"""

import ast
import copy
import hashlib
import json
import sys
from collections import Counter
from pathlib import Path

import pytest
from frozen_v27 import frozen_root

from tools.workflow import __main__ as cli
from tools.workflow import contracts, runner
from tools.workflow.contracts import (
    FAILED_GATES,
    Refused,
    digest,
    expected_plan,
    request_goal,
    validate_goal,
)
from tools.workflow.persistence import read_checkpoint, run_lock, write_checkpoint
from tools.workflow.tests.test_workflow import GOAL, FakeProcesses, FakeRoles, controller, request

WORKFLOW = Path(runner.__file__).resolve().parent
FROZEN = frozen_root()  # verified against FINAL-PINS by tests/frozen_v27.py
CONTRACT = FROZEN / "casesets/cached-onset-e2e-v2_7/EVIDENCE_CONTRACT.json"
# Identity of FINAL-PINS.json itself, as recorded in the input supplement
# (cached-onset-e2e-v2_7-inputs/1, supplements.final_pins_sha256), so a relocated package must
# be byte-identical to the frozen one.
FINAL_PINS_SHA256 = "153f207e89c6ac7786d8203413eeef1e1fce219aef7252d63aced1b5b33d0402"

# Modules in this change's declared file set: every Refused raise must carry a gate here.
DECLARED = {"runner", "contracts", "__main__"}
# The PLAN_VALIDATED reviewer loop is owned by a parallel change and was deliberately not
# edited. Its two raises reach the terminal through the review state's fallback mapping
# (terminal_mapping.source == "state_fallback"). This set may only SHRINK.
PENDING_PROTECTED: set = set()  # reviewer-loop raises are tagged since the claim-pipeline merge
# Untagged raises in modules outside the declared set at this revision (agents.py is declared
# for prompt text only), pinned by (module, message) so that swapping one untagged raise for
# another in the same module still fails the census (W2 review test_runner_lifecycle_v27:198).
# The controller tags them at its call site. This multiset may only SHRINK.
OUTSIDE_INVENTORY = Counter(
    [
        ("admission", "'registered synthetic ' + name + ' changed'"),
        ("admission", "invalid synthetic admission policy"),
        ("admission", "registered synthetic source changed"),
        ("admission", "synthetic admission requires a trusted harness registration"),
        ("admission", "synthetic fixture manifest content hash differs"),
        ("admission", "synthetic fixture manifest disagrees with source"),
        ("admission", "synthetic fixture manifest lineage differs"),
        ("admission", "synthetic fixture manifest requires an integer seed"),
        ("admission", "synthetic fixture manifest source hash differs"),
        ("admission", "synthetic fixture must have honest generator lineage, not fetch history"),
        ("agents", "Claude CLI unavailable"),
        ("agents", "f\"{role} CLI failed: {result['stderr'][-500:]}\""),
        ("agents", "f'invalid {role} envelope: {exc}'"),
        ("agents", "role unavailable or aggregate model budget exhausted"),
        ("catalog", "definition changed; cached-onset/v1 must be reviewed"),
        ("catalog", "empty artifact"),
        ("catalog", "f'invalid CLI contract for {skill}'"),
        ("catalog", "f'links/reparse points are not permitted: {part}'"),
        ("catalog", "f'missing artifact: {path}'"),
        ("catalog", "f'tool is not allowlisted: {skill}'"),
        ("catalog", "network/device paths are outside this profile"),
        ("catalog", "non-execution flag or unresolved placeholder"),
        ("catalog", "only regular artifact files are supported"),
        ("catalog", "runtime core revision differs from the admitted profile"),
        ("catalog", "tool arguments must be a string array"),
        ("evaluation", "build changed during evaluation"),
        ("evaluation", "duplicate case IDs"),
        ("evaluation", "heldout cases must not enter the development harness"),
        (
            "evaluation",
            "implementation harness accepts DEV-ONLY files; heldout custody is external",
        ),
        ("evaluation", "invalid case identifier"),
        ("evaluation", "model evaluation budget must be between zero and five dollars"),
        ("evaluation", "root replacement not supported by this adapter"),
        ("evaluation", "unsupported JSON Patch operation"),
        ("kong_roles", "Kong kit interpreter unavailable"),
        ("kong_roles", "Kong transport requires acknowledging the Not Sensitive data policy"),
        ("kong_roles", "f\"{role} Kong worker failed before sending: {evidence['error_type']}\""),
        ("kong_roles", "f'invalid {role} Kong response: {exc}'"),
        ("kong_roles", "f'price for {name!r} needs finite rates and a cited source'"),
        ("kong_roles", "max_tokens must be an integer in 1..64000"),
        ("kong_roles", "role unavailable or aggregate model budget exhausted"),
        ("kong_roles", "temperature must be null or in 0..1"),
        ("persistence", "run is already owned by another process"),
        ("persistence", "unsupported or damaged checkpoint"),
        ("policy", "invalid action mask"),
        ("policy", "invalid bounded decision scores"),
        ("policy", "stale or masked policy action"),
        ("processes", "aggregate time budget exhausted"),
        ("processes", "available RAM cannot be measured"),
        ("processes", "insufficient free RAM to start a child"),
        ("processes", "reason"),
        ("review_evidence", "invalid review claim fields"),
        ("review_evidence", "invalid review claim identity/text/declaration"),
        ("review_evidence", "required definition source missing"),
        ("review_evidence", "review citation source/hash/quote mismatch"),
        ("review_evidence", "review claim citations missing"),
        ("review_evidence", "review claims missing"),
        ("review_evidence", "review payload binding/source mismatch"),
        ("review_evidence", "review source identity/hash mismatch"),
        ("review_evidence", "review source text missing"),
        ("review_evidence", "review sources missing"),
        ("review_evidence", "review sources missing"),
        ("simulation", "invalid simulation model budget"),
        ("simulation", "unknown simulation scenario"),
    ]
)


# ------------------------------------------------------------------ helpers


class ReceiptRoles(FakeRoles):
    """FakeRoles with input/output hashes, as dispatch evidence binds them."""

    def __init__(self, answers=None, **kwargs):
        super().__init__(**kwargs)
        self.answers = answers or {}
        self.asked = []

    def ask(self, role, payload):
        self.asked.append(role)
        override = self.answers.get(role)
        if override is not None:
            self.receipts.append(
                {"role": role, "session_id": str(len(self.receipts)), "mode": "fixture"}
            )
            answer = override(payload) if callable(override) else copy.deepcopy(override)
        else:
            answer = super().ask(role, payload)
        if self.receipts and self.receipts[-1]["role"] == role:
            self.receipts[-1].update(input_hash=digest(payload), output_hash=digest(answer))
        return answer


class EventProcesses(FakeProcesses):
    """The new Processes interface: on_event receipts and run(..., tool=...)."""

    total_seconds = 1800

    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self.on_event = None
        self.tools = []
        self.serial = 0

    def run(self, argv, cwd, env, tool=None):
        self.serial += 1
        self.tools.append(tool)
        emit = self.on_event or (lambda kind, details: None)
        emit("launch_check", {"serial": self.serial, "free_gib": 9.0})
        emit(
            "process_launch",
            {
                "serial": self.serial,
                "tool": tool,
                "argv": list(argv),
                "pid": 1000 + self.serial,
                "parent_pid": 999,
                "started_monotonic_ns": self.serial,
            },
        )
        result = super().run(argv, cwd, env)
        emit(
            "process_exit",
            {
                "serial": self.serial,
                "pid": 1000 + self.serial,
                "exit_code": result["exit_code"],
                "stdout_bytes": len(result["stdout"]),
                "stderr_bytes": len(result["stderr"]),
                "wall_seconds": 0.0,
                "stop_reason": None,
            },
        )
        return result


def result_json(c):
    return json.loads((c.root / "result.json").read_text(encoding="utf-8"))


def dispatch_events(root):
    return [
        json.loads(p.read_text(encoding="utf-8"))
        for p in sorted(root.glob("dispatch-event-*.json"))
    ]


def bad_plan():
    plan = expected_plan(GOAL)
    plan["steps"][1]["args"]["title"] = "Different title"
    return plan


def tagged(node):
    return any(k.arg == "gate" for k in node.keywords) or len(node.args) >= 2


def refused_raises(path):
    tree = ast.parse(path.read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        if isinstance(node, ast.Raise) and isinstance(node.exc, ast.Call):
            func = node.exc.func
            name = func.id if isinstance(func, ast.Name) else getattr(func, "attr", None)
            if name == "Refused":
                yield node.exc


def message(call):
    if call.args and isinstance(call.args[0], ast.Constant):
        return call.args[0].value
    return ast.unparse(call.args[0]) if call.args else ""


def canonical(text):
    """Render a pinned message in this interpreter's ast.unparse form.

    ast.unparse's quote choice for f-strings changed within 3.12 patch releases (3.12.3 on CI
    renders f'..{d['k']}..', 3.12.13 renders f"..{d['k']}.."), so a pinned source expression is
    re-parsed and re-unparsed on both sides of the census comparison. Two keys still match only
    if they are the identical text or parse to the identical expression; plain-text constant
    messages that are not expressions pass through unchanged.
    """
    try:
        node = ast.parse(text, mode="eval").body
    except SyntaxError:
        return text
    return text if isinstance(node, ast.Constant) else ast.unparse(node)


# ------------------------------------------------------------------ item 1: census + terminal


def test_gate_vocabulary_is_the_frozen_failed_gate_vocabulary():
    pins_bytes = (FROZEN / "FINAL-PINS.json").read_bytes()
    assert hashlib.sha256(pins_bytes).hexdigest() == FINAL_PINS_SHA256
    pins = json.loads(pins_bytes.decode("utf-8"))["files"]
    assert (
        hashlib.sha256(CONTRACT.read_bytes()).hexdigest()
        == pins[CONTRACT.relative_to(FROZEN).as_posix()]
    )
    text = json.loads(CONTRACT.read_text(encoding="utf-8"))["structures"]["failed_gate_vocabulary"]
    frozen = tuple(x.strip() for x in text.rsplit(": ", 1)[1].split(","))
    assert frozen == FAILED_GATES
    assert set(runner.ROLE_GATES.values()) <= set(FAILED_GATES)
    assert set(runner.STATE_GATES.values()) <= set(FAILED_GATES)
    assert set(runner.STATE_GATES) == set(runner.TRANSITIONS) | {"RELEASED"}


def test_static_census_every_refused_raise_carries_a_declared_gate():
    untagged_declared, outside = set(), Counter()
    for path in sorted(WORKFLOW.glob("*.py")):
        module = path.stem
        for call in refused_raises(path):
            for keyword in call.keywords:
                if keyword.arg == "gate" and isinstance(keyword.value, ast.Constant):
                    assert keyword.value.value in FAILED_GATES, (module, message(call))
                if keyword.arg == "outcome" and isinstance(keyword.value, ast.Constant):
                    assert keyword.value.value in contracts.TERMINAL_OUTCOMES[1:]
            if tagged(call):
                continue
            if module in DECLARED:
                untagged_declared.add((module, message(call)))
            else:
                outside[(module, canonical(message(call)))] += 1
    assert untagged_declared == PENDING_PROTECTED
    expected = Counter()
    for (module, text), count in OUTSIDE_INVENTORY.items():
        expected[(module, canonical(text))] += count
    unexpected = outside - expected
    assert not unexpected, f"new untagged raise(s): {sorted(unexpected)}"
    for module in {module for module, _ in outside}:
        assert module in runner.CALL_SITE_GATES, f"no call-site gate documented for {module}"


def test_refused_rejects_undeclared_gate_and_outcome():
    with pytest.raises(ValueError):
        Refused("x", gate="unrelated_tool_failure")
    with pytest.raises(ValueError):
        Refused("x", gate="review", outcome="RELEASED")
    exc = Refused("x")
    contracts.tag(exc, "review")
    assert (exc.gate, exc.gate_source) == ("review", "call_site")
    contracts.tag(exc, "plan_validation")  # a set gate is never silently replaced
    assert exc.gate == "review"


def test_released_terminal_has_null_failed_gate(tmp_path, monkeypatch):
    c = controller(tmp_path, monkeypatch)
    result = c.run()
    expected = {"outcome": "RELEASED", "reason": "released", "failed_gate": None}
    assert result["terminal"] == expected
    stored = result_json(c)
    assert "failed_gate" in stored["terminal"] and stored["terminal"] == expected
    assert read_checkpoint(c.root)["context"]["terminal"]["failed_gate"] is None


@pytest.mark.parametrize(
    ("make", "outcome", "gate", "reason", "source"),
    [
        (
            lambda t, m: controller(t, m, processes=FakeProcesses(fail_onset_check=True)),
            "BLOCKED",
            "gate_evidence",
            "gate_evidence_failed",
            "raise_site",
        ),
        (
            lambda t, m: controller(t, m, processes=FakeProcesses(denied=True)),
            "INCOMPLETE",
            "transition",
            "producing_step_failed",
            "raise_site",
        ),
        (
            lambda t, m: controller(t, m, roles=FakeRoles(omit_verifier_receipt=True)),
            "BLOCKED",
            "verifier",
            "verifier_fail",
            "raise_site",
        ),
    ],
)
def test_refusals_map_to_structured_terminal(
    tmp_path, monkeypatch, make, outcome, gate, reason, source
):
    c = make(tmp_path, monkeypatch)
    with pytest.raises(Refused):
        c.run()
    stored = result_json(c)
    assert stored["terminal"] == {"outcome": outcome, "reason": reason, "failed_gate": gate}
    assert stored["terminal_mapping"]["source"] == source
    assert stored["status"] == "INCOMPLETE" and stored["stop_kind"] == "gate_or_contract_refusal"


def test_source_change_after_binding_is_review_binding(tmp_path, monkeypatch):
    c = controller(tmp_path, monkeypatch)
    c.roles.reviewer_hook = lambda: c.source.write_text("changed")
    with pytest.raises(Refused, match="source changed"):
        c.run()
    assert result_json(c)["terminal"] == {
        "outcome": "BLOCKED",
        "reason": "approval_invalidated",
        "failed_gate": "review_binding",
    }


def test_runtime_failure_is_incomplete_with_documented_fallback(tmp_path, monkeypatch):
    class Outage(FakeRoles):
        def ask(self, role, payload):
            if role == "verifier":
                raise RuntimeError("temporary service outage")
            return super().ask(role, payload)

    c = controller(tmp_path, monkeypatch, roles=Outage())
    with pytest.raises(RuntimeError):
        c.run()
    stored = result_json(c)
    assert stored["terminal"] == {
        "outcome": "INCOMPLETE",
        "reason": "runtime_failure",
        "failed_gate": "interrupted",
    }
    assert stored["terminal_mapping"] == {
        "source": "runtime_fallback",
        "error_type": "RuntimeError",
    }


def test_stale_review_binding_refusal_is_tagged_at_raise_site(tmp_path, monkeypatch):
    roles = ReceiptRoles(
        answers={"reviewer": {"verdict": "APPROVE", "binding_hash": "stale", "findings": []}}
    )
    c = controller(tmp_path, monkeypatch, roles=roles)
    with pytest.raises(Refused, match="review binding or findings invalid"):
        c.run()
    stored = result_json(c)
    assert stored["terminal"]["failed_gate"] == "review_binding"
    assert stored["terminal"]["outcome"] == "BLOCKED"
    assert stored["terminal_mapping"]["source"] == "raise_site"


def test_reviewer_needs_info_verdict_is_a_needs_info_terminal(tmp_path, monkeypatch):
    roles = ReceiptRoles(
        answers={
            "reviewer": lambda p: {
                "verdict": "NEEDS-INFO",
                "binding_hash": p["binding_hash"],
                "findings": ["which window?"],
            }
        }
    )
    c = controller(tmp_path, monkeypatch, roles=roles)
    with pytest.raises(Refused):
        c.run()
    assert result_json(c)["terminal"] == {
        "outcome": "NEEDS_INFO",
        "reason": "needs_info",
        "failed_gate": "review",
    }


def test_repair_exhausted_terminal(tmp_path, monkeypatch):
    roles = ReceiptRoles(answers={"planner": bad_plan()})
    c = controller(tmp_path, monkeypatch, roles=roles)
    c.max_repairs = 0
    with pytest.raises(Refused, match="repair exhausted"):
        c.run()
    assert result_json(c)["terminal"]["failed_gate"] == "repair_exhausted"


def test_resume_refusals_carry_gates(tmp_path, monkeypatch):
    c = controller(tmp_path, monkeypatch)
    c.source.write_text("changed")
    with pytest.raises(Refused) as caught:
        runner.Controller.resume(c.root, FakeProcesses(), FakeRoles())
    assert caught.value.gate == "resume_verification"
    assert caught.value.superseded_gate == "source_check"
    value = read_checkpoint(c.root)
    value["pending"] = {"kind": "tool"}
    runner.write_checkpoint(c.root, value)
    with pytest.raises(Refused) as caught:
        runner.Controller.resume(c.root, FakeProcesses(), FakeRoles())
    assert caught.value.gate == "pending_operation"


# ------------------------------------------------------------------ item 2: needs_info


def test_planner_needs_info_stops_with_no_later_invocation(tmp_path, monkeypatch):
    roles = ReceiptRoles(answers={"planner": {"needs_info": "Is the 2026 scope real?"}})
    processes = FakeProcesses()
    c = controller(tmp_path, monkeypatch, processes=processes, roles=roles)
    result = c.run()
    assert result["status"] == "NEEDS_INFO"
    assert result["terminal"] == {
        "outcome": "NEEDS_INFO",
        "reason": "needs_info",
        "failed_gate": "plan_validation",
    }
    assert c.state == "GOAL_CHECKED"
    assert roles.asked == ["compiler"] * 3 + ["planner"]
    assert len(processes.calls) == 1  # only the pre-plan source check ran
    assert c.context["needs_info"]["role"] == "planner"
    assert "plan_validations" not in c.context and c.plan is None
    # Resuming does not re-ask anything: the stop is final for this request.
    resumed = runner.Controller.resume(c.root, FakeProcesses(), ReceiptRoles())
    again = resumed.run()
    assert again["terminal"]["outcome"] == "NEEDS_INFO"
    assert resumed.roles.asked == [] and resumed.processes.calls == []


def test_repairer_needs_info_after_review_reject_stops_before_rereview(tmp_path, monkeypatch):
    def review(payload):
        verdict = "REJECT" if not payload.get("prior_review") else "APPROVE"
        return {"verdict": verdict, "binding_hash": payload["binding_hash"], "findings": ["x"]}

    roles = ReceiptRoles(
        answers={
            "reviewer": review,
            "repairer": {"needs_info": "Confirm candidates after 2025-12-02?", "response": "-"},
        }
    )
    c = controller(tmp_path, monkeypatch, roles=roles)
    result = c.run()
    assert result["terminal"] == {
        "outcome": "NEEDS_INFO",
        "reason": "needs_info",
        "failed_gate": "review",
    }
    assert roles.asked[-2:] == ["reviewer", "repairer"]
    assert c.state == "PLAN_VALIDATED" and c.repairs_used == 1


def test_repairer_needs_info_after_plan_validation_failure(tmp_path, monkeypatch):
    roles = ReceiptRoles(answers={"planner": bad_plan(), "repairer": {"needs_info": "Title?"}})
    c = controller(tmp_path, monkeypatch, roles=roles)
    result = c.run()
    assert result["terminal"]["failed_gate"] == "plan_validation"
    assert "reviewer" not in roles.asked


@pytest.mark.parametrize(
    ("role", "answer"),
    [
        ("planner", {"needs_info": "q", "plan": expected_plan(GOAL)}),
        ("planner", {"needs_info": ""}),
        ("planner", {"needs_info": 3}),
        ("planner", {"needs_info": "q", "response": "planner has no response field"}),
        ("repairer", {"needs_info": "q", "plan": expected_plan(GOAL), "response": "r"}),
        ("repairer", {"needs_info": "q", "response": 7}),
    ],
)
def test_malformed_needs_info_is_refused(tmp_path, monkeypatch, role, answer):
    answers = {role: answer}
    if role == "repairer":
        answers["planner"] = bad_plan()
    c = controller(tmp_path, monkeypatch, roles=ReceiptRoles(answers=answers))
    with pytest.raises(Refused, match="malformed .* needs_info"):
        c.run()
    assert result_json(c)["terminal"]["failed_gate"] == "plan_validation"
    assert result_json(c)["terminal"]["outcome"] == "BLOCKED"
    assert "needs_info" not in c.context


# ------------------------------------------------------------------ item 3: verifier PASS/FAIL


@pytest.mark.parametrize(
    ("returned", "verdict", "sub_reason"),
    [
        ({"verdict": "FAIL", "sub_reason": "BLOCK"}, "FAIL", "BLOCK"),
        ({"verdict": "FAIL"}, "FAIL", None),
        ({"verdict": "UNVERIFIABLE"}, "FAIL", "UNVERIFIABLE"),
        ({"verdict": "BLOCK"}, "FAIL", "BLOCK"),
    ],
)
def test_verifier_fail_is_recorded_natively(tmp_path, monkeypatch, returned, verdict, sub_reason):
    roles = ReceiptRoles(
        answers={
            "verifier": lambda p: {
                **returned,
                "binding_hash": p["binding_hash"],
                "scope": "data-replay",
            }
        }
    )
    c = controller(tmp_path, monkeypatch, roles=roles)
    with pytest.raises(Refused, match="did not confirm"):
        c.run()
    (record,) = read_checkpoint(c.root)["context"]["verifier_verdicts"]
    assert (record["verdict"], record["sub_reason"]) == (verdict, sub_reason)
    assert result_json(c)["terminal"] == {
        "outcome": "BLOCKED",
        "reason": "verifier_fail",
        "failed_gate": "verifier",
    }


def test_verifier_pass_is_recorded_and_releases(tmp_path, monkeypatch):
    c = controller(tmp_path, monkeypatch)
    assert c.run()["terminal"]["outcome"] == "RELEASED"
    # Updated to follow an intentional change: the record now also keeps the returned
    # binding_hash and scope (W2 review runner.py:1586).
    returned_binding = c.context["verifier_verdicts"][0]["returned_binding_hash"]
    assert isinstance(returned_binding, str) and returned_binding
    assert c.context["verifier_verdicts"] == [
        {
            "occurrence": 1,
            "verdict": "PASS",
            "sub_reason": None,
            "returned_verdict": "PASS",
            "binding_valid": True,
            "scope_valid": True,
            "returned_binding_hash": returned_binding,
            "returned_scope": "data-replay",
        }
    ]


@pytest.mark.parametrize(
    "returned",
    [
        {"verdict": "MAYBE"},
        {"verdict": "PASS", "sub_reason": "BLOCK"},
        {"verdict": "FAIL", "sub_reason": "LATER"},
        {"verdict": "BLOCK", "sub_reason": "UNVERIFIABLE"},
        {"verdict": "PASS", "extra": True},
    ],
)
def test_malformed_verifier_output_is_refused(tmp_path, monkeypatch, returned):
    roles = ReceiptRoles(
        answers={
            "verifier": lambda p: {
                **returned,
                "binding_hash": p["binding_hash"],
                "scope": "data-replay",
            }
        }
    )
    c = controller(tmp_path, monkeypatch, roles=roles)
    with pytest.raises(Refused, match="verifier"):
        c.run()
    assert result_json(c)["terminal"]["failed_gate"] == "verifier"
    assert c.state == "DATA_VERIFIED"


def test_verifier_pass_with_wrong_binding_does_not_release(tmp_path, monkeypatch):
    roles = ReceiptRoles(
        answers={"verifier": {"verdict": "PASS", "binding_hash": "other", "scope": "data-replay"}}
    )
    c = controller(tmp_path, monkeypatch, roles=roles)
    with pytest.raises(Refused, match="did not confirm"):
        c.run()
    assert c.context["verifier_verdicts"][0]["binding_valid"] is False


def test_verifier_record_keeps_the_returned_binding_and_scope(tmp_path, monkeypatch):
    """W2 review runner.py:1586: a FAIL leaves a durable copy of what was returned."""
    returned = {"verdict": "FAIL", "binding_hash": "sha256:" + "0" * 64, "scope": ["odd"]}
    c = controller(tmp_path, monkeypatch, roles=ReceiptRoles(answers={"verifier": returned}))
    with pytest.raises(Refused, match="did not confirm"):
        c.run()
    (record,) = read_checkpoint(c.root)["context"]["verifier_verdicts"]
    assert record["returned_binding_hash"] == "sha256:" + "0" * 64
    assert record["returned_scope"] == ["odd"]
    assert record["binding_valid"] is False and record["scope_valid"] is False


# ------------------------------------------------------------------ item 5: plan records


def test_plan_validation_and_versions_are_persisted(tmp_path, monkeypatch):
    c = controller(tmp_path, monkeypatch)
    c.run()
    context = read_checkpoint(c.root)["context"]
    (validation,) = context["plan_validations"]
    assert validation["plan_hash"] == digest(c.plan)
    assert validation["verdict"] == "PASS" and validation["failed_fields"] == []
    assert validation["parser_accepted"] is True
    assert set(validation["argv"]) == {"onset", "map"}
    assert validation["argv"]["map"][:2] == ["--input", str(c.paths["onset"])]
    assert all(isinstance(t, str) for t in validation["argv"]["onset"])
    assert validation["source"] == {"role": "planner", "occurrence": 1}
    assert context["plan_versions"] == [
        {"plan_hash": digest(c.plan), "role": "planner", "occurrence": 1}
    ]


def test_repair_appends_plan_version_and_failed_validation(tmp_path, monkeypatch):
    roles = ReceiptRoles(
        answers={
            "planner": bad_plan(),
            "repairer": {"plan": expected_plan(GOAL), "response": "Restored the source title."},
        }
    )
    c = controller(tmp_path, monkeypatch, roles=roles)
    assert c.run()["terminal"]["outcome"] == "RELEASED"
    context = read_checkpoint(c.root)["context"]
    failed, passed = context["plan_validations"]
    assert failed["verdict"] == "FAIL" and failed["parser_accepted"] is False
    assert failed["failed_fields"] == ["plan.steps[1].args"]
    assert failed["plan_hash"] == digest(bad_plan())
    assert failed["source"] == {"role": "planner", "occurrence": 1}
    assert passed["verdict"] == "PASS" and passed["source"] == {"role": "repairer", "occurrence": 1}
    assert [v["plan_hash"] for v in context["plan_versions"]] == [
        digest(bad_plan()),
        digest(expected_plan(GOAL)),
    ]


def test_parser_rejection_is_a_failed_validation_record(tmp_path, monkeypatch):
    c = controller(tmp_path, monkeypatch)

    def strict(repo, skill, argv):
        if skill == "plot-onset":
            raise Refused("invalid CLI contract for plot-onset")  # untagged, like catalog.py
        return {}, {}

    monkeypatch.setattr(runner, "preflight", strict)
    c.max_repairs = 0
    with pytest.raises(Refused, match="repair exhausted"):
        c.run()
    (record,) = c.context["plan_validations"]
    assert record["parser_accepted"] is False and record["verdict"] == "FAIL"
    assert record["failed_fields"] == ["plan.steps.argv"]


# ------------------------------------------------------------------ item 6: null title


def data_request(title_entry):
    goal = {**GOAL, "task": "onset_data", "title": None}
    value = request()
    value["constraints"]["task"]["value"] = "onset_data"
    value["constraints"]["title"] = title_entry
    value["authorization"].update(goal_hash=digest(goal), actions=["cached-onset-data"])
    return value, goal


def test_data_profile_accepts_null_title_without_quote():
    value, goal = data_request({"value": None, "message_id": None, "quote": None})
    assert request_goal(value) == goal
    assert validate_goal(goal) == goal


def test_null_title_still_refused_for_map_profile():
    with pytest.raises(Refused, match="title") as caught:
        validate_goal({**GOAL, "title": None})
    assert caught.value.gate == "goal_check"
    value = request()
    value["constraints"]["title"] = {"value": None, "message_id": None, "quote": None}
    with pytest.raises(Refused, match="title"):
        request_goal(value)


@pytest.mark.parametrize(
    "entry",
    [
        {"value": None, "message_id": "scenario", "quote": "not in the message"},
        {"value": None, "message_id": "missing", "quote": None},
        {"value": "A title", "message_id": None, "quote": None},
    ],
)
def test_supplied_or_non_null_attribution_must_still_verify(entry):
    value, _ = data_request(entry)
    if entry["value"] is not None:
        goal = {**GOAL, "task": "onset_data", "title": entry["value"]}
        value["authorization"]["goal_hash"] = digest(goal)
    with pytest.raises(Refused) as caught:
        request_goal(value)
    assert caught.value.gate == "authorization"


def test_null_title_data_run_releases(tmp_path, monkeypatch):
    value, goal = data_request({"value": None, "message_id": None, "quote": None})
    source = tmp_path / "source.zarr"
    source.write_text("unchanged rainfall")
    root = tmp_path / "run"
    root.mkdir()
    monkeypatch.setattr(runner, "build_manifest", lambda repo: {"build": "fixed"})
    monkeypatch.setattr(runner, "preflight", lambda *args: ({}, {}))
    roles = ReceiptRoles(answers={"compiler": goal, "planner": expected_plan(goal)})
    c = runner.Controller(
        Path(runner.__file__).parents[2], value, source, root, FakeProcesses(), roles
    )
    monkeypatch.setattr(c, "inspect_source", lambda: {})
    assert c.run()["terminal"]["outcome"] == "RELEASED"
    assert not c.paths["map"].exists()


# ------------------------------------------------------------------ item 7: post-commit boundary


def test_after_role_committed_observes_durable_cleared_pending(tmp_path, monkeypatch):
    c = controller(tmp_path, monkeypatch, roles=ReceiptRoles())
    seen = []

    def hooks(point, event, ctl):
        if point in {"after_role", "after_role_committed"}:
            durable = read_checkpoint(ctl.root)
            seen.append((point, event["role"], durable["pending"], copy.deepcopy(event)))

    c.hooks = hooks
    c.run()
    roles = [r for p, r, _, _ in seen if p == "after_role"]
    committed = [(r, e) for p, r, _, e in seen if p == "after_role_committed"]
    assert [r for r, _ in committed] == roles
    assert roles[:3] == ["compiler"] * 3
    for (point, _, durable_pending, _event), (next_point, *_rest) in zip(
        seen[::2], seen[1::2], strict=True
    ):
        assert (point, next_point) == ("after_role", "after_role_committed")
        assert durable_pending is not None  # the pre-commit boundary still shows pending
    for _, event in committed:
        assert event["checkpoint_durable"] is True
        assert event["pending_operation"] is None
        assert event["durable"] == {"state": event["checkpoint_state"], "pending": None}
        assert event["checkpoint_state"] == event["state"]


# ------------------------------------------------------------------ item 8: CLI flags


@pytest.mark.parametrize(
    "flags",
    [
        [],
        ["--dispatch-evidence"],
        ["--review-evidence"],
        ["--dispatch-evidence", "--review-evidence"],
    ],
)
def test_cli_exposes_evidence_modes(tmp_path, monkeypatch, capsys, flags):
    captured = {}

    class Recorder:
        def __init__(self, *args, **kwargs):
            captured.update(kwargs)
            self.context = {}

        def run(self):
            return {"terminal": {"outcome": "RELEASED", "reason": "released", "failed_gate": None}}

    monkeypatch.setattr(cli, "Controller", Recorder)
    path = tmp_path / "request.json"
    path.write_text(json.dumps(request()), encoding="utf-8")
    argv = [
        "workflow",
        "run",
        "--request",
        str(path),
        "--source",
        str(tmp_path / "src"),
        "--run-dir",
        str(tmp_path / "run"),
        *flags,
    ]
    monkeypatch.setattr(sys, "argv", argv)
    cli.main()
    assert captured["dispatch_evidence_mode"] is ("--dispatch-evidence" in flags)
    assert captured["review_evidence_mode"] is ("--review-evidence" in flags)


def exhausted_checkpoint(tmp_path, monkeypatch):
    c = controller(tmp_path, monkeypatch)
    value = read_checkpoint(c.root)
    value["elapsed_s"] = value["total_seconds"] + 1
    write_checkpoint(c.root, value)
    monkeypatch.setattr(sys, "argv", ["wf", "resume", "--run-dir", str(c.root)])
    return c


def test_cli_resume_budget_refusal_records_a_terminal(tmp_path, monkeypatch, capsys):
    """W2 review __main__.py:130: the refusal reaches result.json and the status line."""
    c = exhausted_checkpoint(tmp_path, monkeypatch)
    with pytest.raises(SystemExit) as stopped:
        cli.main()
    assert stopped.value.code == 1
    assert isinstance(stopped.value.__cause__, Refused)
    assert stopped.value.__cause__.gate == "budget"
    terminal = result_json(c)["terminal"]
    assert terminal["failed_gate"] == "budget" and terminal["reason"] == "resume_refused"
    assert terminal["outcome"] in {"BLOCKED", "INCOMPLETE"}
    assert read_checkpoint(c.root)["context"]["terminal"]["failed_gate"] == "budget"
    status = json.loads(capsys.readouterr().out.strip().splitlines()[-1])
    assert status["status"] == "INCOMPLETE" and status["terminal"] == terminal


def test_cli_resume_reads_the_checkpoint_under_the_run_lock(tmp_path, monkeypatch, capsys):
    """W2 review __main__.py:127: limits are read from the checkpoint the resume owns."""
    c = exhausted_checkpoint(tmp_path, monkeypatch)
    held, real = [], cli.read_checkpoint

    def spy(root):
        try:
            with run_lock(root):
                held.append(False)  # nobody held the lock: read outside it
        except Refused:
            held.append(True)
        return real(root)

    monkeypatch.setattr(cli, "read_checkpoint", spy)
    with pytest.raises(SystemExit):
        cli.main()
    assert held == [True]
    assert c.root.joinpath("result.json").exists()


# ------------------------------------------------------------------ item 4: process receipts


def test_process_events_join_the_controller_sequence(tmp_path, monkeypatch):
    processes = EventProcesses()
    base = controller(tmp_path, monkeypatch)
    (tmp_path / "evidence-run").mkdir()
    c2 = runner.Controller(
        base.repo,
        base.request,
        base.source,
        tmp_path / "evidence-run",
        processes,
        ReceiptRoles(),
        dispatch_evidence_mode=True,
    )
    monkeypatch.setattr(c2, "inspect_source", lambda: {"test": "metadata fixture"})
    result = c2.run()
    assert result["terminal"]["outcome"] == "RELEASED"
    records = dispatch_events(c2.root)
    assert [r["sequence"] for r in records] == list(range(1, len(records) + 1))
    launches = [r for r in records if r["kind"] == "process_launch"]
    exits = [r for r in records if r["kind"] == "process_exit"]
    checks = [r for r in records if r["kind"] == "launch_check"]
    assert len(launches) == len(exits) == len(checks) == len(processes.calls)
    assert [r["tool"] for r in launches] == processes.tools
    assert processes.tools[:2] == ["check-artifact", "onset-date"]
    assert all(r["resume_phase"] == 0 and r["run_id"] == records[0]["run_id"] for r in records)
    assert {"serial", "argv", "pid", "parent_pid", "started_monotonic_ns"} <= set(launches[0])
    assert records[-1]["kind"] == "terminal"
    assert records[-1]["terminal"]["failed_gate"] is None
    validations = [r for r in records if r["kind"] == "plan_validation"]
    assert validations[0]["validation"]["verdict"] == "PASS"


class SilentProcesses(EventProcesses):
    """Satisfies supports_process_receipts but never calls on_event."""

    def run(self, argv, cwd, env, tool=None):
        self.tools.append(tool)
        return FakeProcesses.run(self, argv, cwd, env)


class NoExitProcesses(EventProcesses):
    """Emits launch_check and process_launch, then drops process_exit."""

    def run(self, argv, cwd, env, tool=None):
        real, kept = self.on_event, []
        self.on_event = lambda kind, details: (
            kept.append(kind) or (real(kind, details) if kind != "process_exit" else None)
        )
        try:
            return super().run(argv, cwd, env, tool=tool)
        finally:
            self.on_event = real


@pytest.mark.parametrize("manager", [SilentProcesses, NoExitProcesses])
def test_process_record_without_receipts_is_refused(tmp_path, monkeypatch, manager):
    """W2 review runner.py:89: the interface check alone let a silent manager release."""
    base = controller(tmp_path, monkeypatch)
    (tmp_path / "ev").mkdir()
    c = runner.Controller(
        base.repo,
        base.request,
        base.source,
        tmp_path / "ev",
        manager(),
        ReceiptRoles(),
        dispatch_evidence_mode=True,
    )
    monkeypatch.setattr(c, "inspect_source", lambda: {"test": "metadata fixture"})
    with pytest.raises(Refused) as caught:
        c.run()
    assert caught.value.gate == "gate_evidence"
    result = result_json(c)
    assert result["terminal"]["outcome"] != "RELEASED"
    assert result["terminal"]["failed_gate"] == "gate_evidence"
    assert result["terminal"]["reason"] == "process_receipts_missing"
    assert dispatch_events(c.root)[-1]["terminal"]["failed_gate"] == "gate_evidence"


def test_signal_stop_event_is_recorded(tmp_path, monkeypatch):
    processes = EventProcesses()
    (tmp_path / "run2").mkdir()
    c = controller(tmp_path, monkeypatch, processes=processes)
    c = runner.Controller(
        c.repo,
        c.request,
        c.source,
        tmp_path / "run2",
        processes,
        ReceiptRoles(),
        dispatch_evidence_mode=True,
    )
    processes.on_event("signal_stop", {"serial": 3, "record": {"pid": 7, "signal": "TERM"}})
    (record,) = dispatch_events(c.root)
    assert record["kind"] == "signal_stop" and record["record"]["pid"] == 7
    with pytest.raises(Refused) as caught:
        processes.on_event("process_exit", {"sequence": 99})
    assert caught.value.gate == "gate_evidence"


def test_old_processes_without_on_event_refuse_dispatch_evidence(tmp_path, monkeypatch):
    c = controller(tmp_path, monkeypatch)
    (tmp_path / "run3").mkdir()
    with pytest.raises(Refused, match="on_event") as caught:
        runner.Controller(
            c.repo,
            c.request,
            c.source,
            tmp_path / "run3",
            FakeProcesses(),
            ReceiptRoles(),
            dispatch_evidence_mode=True,
        )
    assert caught.value.gate == "gate_evidence"


def test_resume_rebinds_process_events_without_double_recording(tmp_path, monkeypatch):
    class Once(ReceiptRoles):
        def ask(self, role, payload):
            if role == "verifier":
                raise RuntimeError("temporary outage")
            return super().ask(role, payload)

    observer = []
    processes = EventProcesses()
    processes.on_event = lambda kind, details: observer.append(kind)
    (tmp_path / "run4").mkdir()
    base = controller(tmp_path, monkeypatch)
    c = runner.Controller(
        base.repo,
        base.request,
        base.source,
        tmp_path / "run4",
        processes,
        Once(),
        dispatch_evidence_mode=True,
    )
    monkeypatch.setattr(c, "inspect_source", lambda: {"test": "metadata fixture"})
    with pytest.raises(RuntimeError):
        c.run()
    before = len(dispatch_events(c.root))
    value = read_checkpoint(c.root)
    value["pending"] = None  # the outage left dispatch uncertain; clear it for this rebind test
    runner.write_checkpoint(c.root, value)
    resumed = runner.Controller.resume(c.root, processes, ReceiptRoles())
    processes.on_event("launch_check", {"serial": 50})
    records = dispatch_events(c.root)
    assert len(records) == before + 2  # resume + exactly one launch_check
    assert records[-1]["resume_phase"] == 1 and records[-1]["kind"] == "launch_check"
    assert observer.count("launch_check") == len(processes.calls) + 1  # caller observer kept
    assert resumed.processes.on_event.controller_owner is resumed


# ------------------------------------------------------------------ item 9: repair parity


def parity(c):
    attempts = c.context.get("repair_attempts", [])
    receipts = sum(r["role"] == "repairer" for r in c.roles.receipts)
    failed = sum(a["status"] == "dispatch_failed" for a in attempts)
    return c.repairs_used == receipts + failed


def test_repair_refused_before_dispatch_does_not_consume_allowance(tmp_path, monkeypatch):
    roles = ReceiptRoles(answers={"planner": bad_plan()})
    c = controller(tmp_path, monkeypatch, roles=roles)

    def hooks(point, event, ctl):
        if point == "before_role" and event["role"] == "repairer":
            raise Refused("injected pre-dispatch refusal", gate="transition")

    c.hooks = hooks
    with pytest.raises(Refused, match="pre-dispatch"):
        c.run()
    assert c.repairs_used == 0
    (attempt,) = read_checkpoint(c.root)["context"]["repair_attempts"]
    assert attempt["status"] == "refused_before_dispatch"
    assert attempt["consumes_allowance"] is False and attempt["trigger_gate"] == "plan_validation"
    assert "repairer" not in roles.asked
    assert parity(c)


def test_dispatched_repairs_are_counted_once(tmp_path, monkeypatch):
    roles = ReceiptRoles(
        answers={
            "planner": bad_plan(),
            "repairer": {"plan": expected_plan(GOAL), "response": "fixed"},
        }
    )
    c = controller(tmp_path, monkeypatch, roles=roles)
    c.run()
    assert c.repairs_used == 1
    assert [a["status"] for a in c.context["repair_attempts"]] == ["returned"]
    assert read_checkpoint(c.root)["repairs_used"] == 1
    assert parity(c)


def test_repair_transport_failure_after_dispatch_consumes_and_balances(tmp_path, monkeypatch):
    class Lost(ReceiptRoles):
        def ask(self, role, payload):
            if role == "repairer":
                raise RuntimeError("lost after dispatch")
            return super().ask(role, payload)

    c = controller(tmp_path, monkeypatch, roles=Lost(answers={"planner": bad_plan()}))
    with pytest.raises(RuntimeError):
        c.run()
    assert c.repairs_used == 1
    assert c.context["repair_attempts"][-1]["status"] == "dispatch_failed"
    assert read_checkpoint(c.root)["repairs_used"] == 1
    assert parity(c)
