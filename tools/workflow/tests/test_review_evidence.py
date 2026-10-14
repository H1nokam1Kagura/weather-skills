"""Real controller and Roles, with only a deterministic local process stub."""

import copy
import hashlib
import json
from pathlib import Path

import pytest

from tools.workflow import agents, review_evidence, runner
from tools.workflow.contracts import Refused, digest, expected_plan, plan_version_hash
from tools.workflow.tests.test_workflow import GOAL, request


class ReviewReached(Exception):
    pass


class ProcessStub:
    total_seconds = 1800

    def __init__(self, mode, change=None):
        self.calls = []
        self.mode, self.change = mode, change

    def run(self, command, workspace, environment, stdin):
        # Controller scientific operations are stubbed separately; no executable launches.
        assert command[0] == "never-launched-claude"
        payload = json.loads(stdin)
        self.calls.append({"stdin": stdin, "payload": payload, "command": command})
        if "schema" in payload:  # compiler input
            value = copy.deepcopy(GOAL)
        elif "profile_template" in payload:
            value = expected_plan(GOAL)
            if self.mode:
                source = payload["review_evidence_request"]["sources"][0]
                value = {
                    "schema": review_evidence.SCHEMA,
                    "plan": value,
                    "plan_claims": [
                        {
                            "claim_id": "planner-emitted-claim",
                            "text": source["text"].splitlines()[0],
                            "derivable": False,
                            "citations": [
                                {
                                    "source_id": source["source_id"],
                                    "source_hash": source["source_hash"],
                                    "quote": source["text"].splitlines()[0],
                                }
                            ],
                        }
                    ],
                }
                if self.change == "missing_claims":
                    del value["plan_claims"]
                if self.change == "missing_source":
                    value["plan_claims"][0]["citations"][0]["source_id"] = "absent"
                if self.change == "wrong_source_hash":
                    value["plan_claims"][0]["citations"][0]["source_hash"] = "sha256:bad"
                if "failure" in payload:
                    value["plan_claims"][0]["claim_id"] += "-repaired"
                    value["response"] = "Retained typed plan and resubmitted explicit claim."
        else:
            if self.change == "failed_dispatch":
                raise RuntimeError("stub dispatch failed")
            value = {"verdict": "APPROVE", "binding_hash": payload["binding_hash"], "findings": []}
            if self.change == "wrong_binding":
                value["binding_hash"] = "wrong"
            if self.change == "reject_once" and not payload.get("prior_review"):
                value["verdict"] = "REJECT"
        return {
            "exit_code": 0,
            "stderr": "",
            "stdout": json.dumps(
                {
                    "session_id": "stub-session-" + str(len(self.calls)),
                    "total_cost_usd": 0.0,
                    "result": json.dumps(value),
                }
            ),
        }


def make_controller(tmp_path, monkeypatch, mode=True, change=None, hook=None):
    repo = tmp_path / "repo"
    for name in ("onset-date", "plot-onset", "check-artifact", "verify-run"):
        directory = repo / "skills" / name
        directory.mkdir(parents=True)
        (directory / "SKILL.md").write_bytes(
            ("Explicit documentation for " + name + ": café.\r\nSecond line.\r\n").encode()
        )
    registry = repo / "skills/onset-date/references/onset_definitions.toml"
    registry.parent.mkdir()
    original = (
        Path(__file__).resolve().parents[3] / "skills/onset-date/references/onset_definitions.toml"
    )
    registry.write_bytes(original.read_bytes())
    source = tmp_path / "source.zarr"
    source.write_bytes(b"stub source only; not scientific data")
    root = tmp_path / "run"
    root.mkdir()
    monkeypatch.setattr(
        runner,
        "build_manifest",
        lambda base: {
            str(p.relative_to(base)): hashlib.sha256(p.read_bytes()).hexdigest()
            for p in sorted(Path(base).rglob("SKILL.md"))
        },
    )
    monkeypatch.setattr(runner, "preflight", lambda *args: ({}, {}))
    monkeypatch.setattr(agents.shutil, "which", lambda _: "never-launched-claude")
    process = ProcessStub(mode, change)
    roles = agents.Roles(process, root, budget=1)

    def boundary(point, event, controller):
        if hook:
            hook(point, event, controller)
        if point == "after_transition" and event["state"] == "REVIEW_APPROVED":
            raise ReviewReached

    c = runner.Controller(
        repo, request(), source, root, process, roles, hooks=boundary, review_evidence_mode=mode
    )
    monkeypatch.setattr(c, "inspect_source", lambda: {"stub_metadata": True})
    monkeypatch.setattr(c, "check", lambda *args: {"stub_check": True})
    return c, process


def snapshots(c):
    return [json.loads(p.read_text()) for p in sorted(c.root.glob("review-capture-*.json"))]


def test_actual_dispatch_captures_original_reviewed_bytes_and_binding(tmp_path, monkeypatch):
    c, process = make_controller(tmp_path, monkeypatch)
    with pytest.raises(ReviewReached):
        c._run()
    (snapshot,) = snapshots(c)
    actual = process.calls[-1]
    assert snapshot["stdin"] == actual["stdin"]
    assert snapshot["stdin_hash"] == review_evidence.text_hash(actual["stdin"])
    assert snapshot["input_hash"] == digest(actual["payload"])
    assert snapshot["accepted_review"] is True
    committed = snapshot["committed_transition"]
    assert committed["event"] == c.events[committed["index"]]
    assert committed["event"]["to"] == "REVIEW_APPROVED"
    assert committed["binding_hash"] == c.binding
    assert snapshot["reviewer_occurrence"] == 1
    assert snapshot["receipt"]["session_id"] == "stub-session-5"
    assert snapshot["receipt"]["invocation_id"]
    prompt = actual["command"][actual["command"].index("--system-prompt") + 1]
    assert snapshot["receipt"]["dispatch"]["system_prompt"] == prompt
    assert snapshot["receipt"]["dispatch"]["system_prompt_hash"] == review_evidence.text_hash(
        prompt
    )
    envelope = snapshot["review_evidence"]
    assert envelope["binding_hash"] == c.current_binding()
    # The reviewed plan version is the plan plus the claims emitted with it.
    assert envelope["plan_hash"] == plan_version_hash(expected_plan(GOAL), envelope["plan_claims"])
    assert envelope["plan_hash"] != digest(expected_plan(GOAL))
    assert envelope["plan_claims"] == actual["payload"]["review_evidence"]["plan_claims"]
    for source in envelope["sources"]:
        if source["source_id"] == review_evidence.DEFINITION_SOURCE_ID:
            raw = (c.repo / "skills/onset-date/references/onset_definitions.toml").read_bytes()
        else:
            name = source["source_id"].split(":", 1)[1]
            raw = (c.repo / "skills" / name / "SKILL.md").read_bytes()
        assert source["text"].encode("utf-8") == raw
        assert source["source_hash"] == "sha256:" + hashlib.sha256(raw).hexdigest()
        if source["source_id"] != review_evidence.DEFINITION_SOURCE_ID:
            assert "\r\n" in source["text"]
    assert envelope["unsupported"] == review_evidence.UNSUPPORTED
    assert snapshot["full_simkit_capability"] == "NOT_IMPLEMENTED"


def test_full_original_registry_is_delivered_before_planning_and_review(tmp_path, monkeypatch):
    c, process = make_controller(tmp_path, monkeypatch)
    raw = (c.repo / "skills/onset-date/references/onset_definitions.toml").read_bytes()
    with pytest.raises(ReviewReached):
        c._run()
    planner = process.calls[3]["payload"]
    reviewer = process.calls[4]["payload"]
    source_id = review_evidence.DEFINITION_SOURCE_ID
    (planner_source,) = [
        s for s in planner["review_evidence_request"]["sources"] if s["source_id"] == source_id
    ]
    (reviewer_source,) = [
        s for s in reviewer["review_evidence"]["sources"] if s["source_id"] == source_id
    ]
    assert planner_source == reviewer_source
    assert reviewer_source["text"].encode("utf-8") == raw
    assert reviewer_source["source_hash"] == "sha256:" + hashlib.sha256(raw).hexdigest()
    assert planner["documentation"][source_id].encode("utf-8") == raw
    assert reviewer["documentation"][source_id].encode("utf-8") == raw


@pytest.mark.parametrize("fault", ["missing", "unreadable"])
def test_missing_or_unreadable_registry_cannot_reach_planner(tmp_path, monkeypatch, fault):
    c, process = make_controller(tmp_path, monkeypatch)
    registry = c.repo / "skills/onset-date/references/onset_definitions.toml"
    original_read = Path.read_bytes

    def unavailable(path):
        if path == registry:
            if fault == "missing":
                raise FileNotFoundError("stub missing registry")
            raise PermissionError("stub unreadable registry")
        return original_read(path)

    monkeypatch.setattr(Path, "read_bytes", unavailable)
    with pytest.raises(Refused, match="required definition source unavailable"):
        c._run()
    assert len(process.calls) == 3  # compiler only; no planner/reviewer dispatch
    assert snapshots(c) == []


@pytest.mark.parametrize(
    "change", ["wrong_binding", "missing_claims", "missing_source", "wrong_source_hash"]
)
def test_invalid_identity_cannot_gain_accepted_capture(tmp_path, monkeypatch, change):
    c, _ = make_controller(tmp_path, monkeypatch, change=change)
    with pytest.raises(Refused):
        c._run()
    assert not any(s["accepted_review"] for s in snapshots(c))
    assert c.state != "REVIEW_APPROVED"


@pytest.mark.parametrize("change", ["remove_source", "change_source", "binding"])
def test_mutating_hook_cannot_substitute_review_evidence(tmp_path, monkeypatch, change):
    def mutate(point, event, controller):
        if point == "before_role" and event["role"] == "reviewer":
            evidence = event["payload"]["review_evidence"]
            if change == "remove_source":
                evidence["sources"].pop()
            elif change == "change_source":
                evidence["sources"][0]["text"] = "different text"
                evidence["sources"][0]["source_hash"] = review_evidence.text_hash("different text")
            else:
                evidence["binding_hash"] = "wrong"

    c, process = make_controller(tmp_path, monkeypatch, hook=mutate)
    with pytest.raises(Refused, match="binding/source mismatch"):
        c._run()
    assert len(process.calls) == 4
    assert snapshots(c) == []


def test_failed_dispatch_has_attempt_without_success_receipt(tmp_path, monkeypatch):
    c, _ = make_controller(tmp_path, monkeypatch, change="failed_dispatch")
    with pytest.raises(RuntimeError, match="stub dispatch failed"):
        c._run()
    (snapshot,) = snapshots(c)
    assert snapshot["dispatch_status"] == "FAILED"
    assert snapshot["accepted_review"] is False
    assert "receipt" not in snapshot


def test_after_role_cannot_forge_an_approved_answer(tmp_path, monkeypatch):
    def mutate(point, event, controller):
        if point == "after_role" and event["role"] == "reviewer":
            event["answer"]["findings"] = ["forged after dispatch"]

    c, _ = make_controller(tmp_path, monkeypatch, hook=mutate)
    with pytest.raises(Refused, match="receipt binding mismatch"):
        c._run()
    assert not any(s["accepted_review"] for s in snapshots(c))


def test_checkpoint_failure_cannot_create_accepted_review_capture(tmp_path, monkeypatch):
    c, _ = make_controller(tmp_path, monkeypatch)
    checkpoint = c.checkpoint

    def fail_commit():
        if c.state == "REVIEW_APPROVED":
            raise OSError("stub checkpoint failure")
        checkpoint()

    monkeypatch.setattr(c, "checkpoint", fail_commit)
    with pytest.raises(OSError, match="checkpoint failure"):
        c._run()
    (snapshot,) = snapshots(c)
    assert snapshot["accepted_review"] is False
    assert "committed_transition" not in snapshot
    assert json.loads((c.root / "checkpoint.json").read_text())["state"] == "PLAN_VALIDATED"


def test_repaired_claims_receive_fresh_binding_and_occurrence(tmp_path, monkeypatch):
    c, _ = make_controller(tmp_path, monkeypatch, change="reject_once")
    with pytest.raises(ReviewReached):
        c._run()
    first, second = snapshots(c)
    assert [first["reviewer_occurrence"], second["reviewer_occurrence"]] == [1, 2]
    assert first["run_id"] == second["run_id"]
    assert first["accepted_review"] is False
    assert second["accepted_review"] is True
    assert first["review_evidence"]["binding_hash"] != second["review_evidence"]["binding_hash"]
    assert first["review_evidence"]["plan_claims"][0]["claim_id"] == "planner-emitted-claim"
    assert second["review_evidence"]["plan_claims"][0]["claim_id"].endswith("-repaired")
    c.integrity()


def test_legacy_plan_and_binding_unchanged(tmp_path, monkeypatch):
    c, process = make_controller(tmp_path, monkeypatch, mode=False)
    with pytest.raises(ReviewReached):
        c._run()
    assert c.plan == expected_plan(GOAL)
    assert snapshots(c) == []
    assert "review_evidence" not in process.calls[-1]["payload"]
    assert "review_evidence_request" not in process.calls[-2]["payload"]
    assert c.binding == digest(
        {
            "request": c.request_hash,
            "goal": c.goal,
            "plan": c.plan,
            "build": c.build_hash,
            "source": c.source_hash,
            "admission": c.admission_hash,
        }
    )


def test_resume_retains_run_and_occurrence_and_refuses_changed_capture(tmp_path, monkeypatch):
    c, _ = make_controller(tmp_path, monkeypatch)
    with pytest.raises(ReviewReached):
        c._run()
    resumed = runner.Controller.resume(c.root, c.processes, c.roles, hooks=c.hooks)
    assert resumed.context["review_evidence"] == c.context["review_evidence"]
    path = next(c.root.glob("review-capture-*.json"))
    path.write_text("{}")
    with pytest.raises(Refused, match="controller evidence changed"):
        runner.Controller.resume(c.root, c.processes, c.roles, hooks=c.hooks)
