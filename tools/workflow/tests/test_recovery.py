import copy

import pytest

from tools.workflow import policy, runner
from tools.workflow.contracts import Refused, digest, expected_plan
from tools.workflow.persistence import atomic_json, read_checkpoint, run_lock, write_checkpoint
from tools.workflow.tests.test_workflow import GOAL, FakeProcesses, FakeRoles, controller


def test_resume_after_transient_failure_does_not_repeat_producing_steps(tmp_path, monkeypatch):
    class Once(FakeRoles):
        def ask(self, role, payload):
            if role == "verifier":
                raise RuntimeError("temporary service outage")
            return super().ask(role, payload)

    c = controller(tmp_path, monkeypatch, roles=Once())
    with pytest.raises(RuntimeError):
        c.run()
    assert c.state == "DATA_VERIFIED"
    processes = FakeProcesses()
    resumed = runner.Controller.resume(c.root, processes, FakeRoles())
    assert resumed.run()["status"] == "PASS"
    assert processes.calls == []
    assert resumed.resumes == 1


def test_resume_rejects_changed_source_and_uncertain_operations(tmp_path, monkeypatch):
    c = controller(tmp_path, monkeypatch)
    c.source.write_text("changed")
    with pytest.raises(Refused, match="source changed"):
        runner.Controller.resume(c.root, FakeProcesses(), FakeRoles())
    value = read_checkpoint(c.root)
    value["pending"] = {"kind": "tool"}
    write_checkpoint(c.root, value)
    with pytest.raises(Refused, match="uncertain outcome"):
        runner.Controller.resume(c.root, FakeProcesses(), FakeRoles())


def test_resume_rejects_forged_stage_history(tmp_path, monkeypatch):
    c = controller(tmp_path, monkeypatch)
    value = read_checkpoint(c.root)
    value["state"] = "RELEASED"
    write_checkpoint(c.root, value)
    with pytest.raises(Refused, match="history"):
        runner.Controller.resume(c.root, FakeProcesses(), FakeRoles())


def test_resume_budget_is_enforced_inside_controller(tmp_path, monkeypatch):
    c = controller(tmp_path, monkeypatch)
    value = read_checkpoint(c.root)
    value["elapsed_s"] = value["total_seconds"] + 1
    write_checkpoint(c.root, value)
    with pytest.raises(Refused, match="budget exhausted"):
        runner.Controller.resume(c.root, FakeProcesses(), FakeRoles())


def test_resume_rejects_evidence_file_tampering(tmp_path, monkeypatch):
    c = controller(tmp_path, monkeypatch)
    atomic_json(c.root / "request.json", {"fabricated": True})
    with pytest.raises(Refused, match="evidence changed"):
        runner.Controller.resume(c.root, FakeProcesses(), FakeRoles())


def test_reviewer_repair_is_bounded_and_reviewed_again(tmp_path, monkeypatch):
    class Repair(FakeRoles):
        def ask(self, role, payload):
            if role == "repairer":
                self.receipts.append(
                    {"role": role, "session_id": str(len(self.receipts)), "mode": "fixture"}
                )
                return {
                    "plan": expected_plan(GOAL),
                    "response": "Input ends Dec 31; t+29 implies Dec 2.",
                }
            answer = super().ask(role, payload)
            if (
                role == "reviewer"
                and len([r for r in self.receipts if r["role"] == "reviewer"]) == 1
            ):
                answer["verdict"] = "REJECT"
                answer["findings"] = ["Clarify the two date windows"]
            return answer

    c = controller(tmp_path, monkeypatch, roles=Repair())
    assert c.run()["status"] == "PASS"
    assert c.repairs_used == 1
    assert [r["role"] for r in c.roles.receipts].count("reviewer") == 2


def test_repeated_rejection_cannot_be_shopped_on_resume(tmp_path, monkeypatch):
    class Reject(FakeRoles):
        def ask(self, role, payload):
            if role == "repairer":
                self.receipts.append(
                    {"role": role, "session_id": str(len(self.receipts)), "mode": "fixture"}
                )
                return {"plan": expected_plan(GOAL), "response": "No scope changes"}
            answer = super().ask(role, payload)
            if role == "reviewer":
                answer["verdict"] = "REJECT"
            return answer

    c = controller(tmp_path, monkeypatch, roles=Reject())
    with pytest.raises(Refused, match="repair exhausted"):
        c.run()
    resumed = runner.Controller.resume(c.root, FakeProcesses(), Reject())
    count = len(resumed.roles.receipts)
    with pytest.raises(Refused, match="repair exhausted"):
        resumed.run()
    assert len(resumed.roles.receipts) == count


def test_data_only_profile_has_no_plot_stage(tmp_path, monkeypatch):
    c = controller(tmp_path, monkeypatch)
    goal = {**GOAL, "task": "onset_data"}
    c.request["constraints"]["task"]["value"] = "onset_data"
    c.request["authorization"].update(goal_hash=digest(goal), actions=["cached-onset-data"])
    # Construct a fresh request rather than mutate an admitted controller.
    request = copy.deepcopy(c.request)
    root = tmp_path / "data-run"
    root.mkdir()

    class DataRoles(FakeRoles):
        def ask(self, role, payload):
            answer = super().ask(role, payload)
            return (
                goal if role == "compiler" else expected_plan(goal) if role == "planner" else answer
            )

    c = runner.Controller(c.repo, request, c.source, root, FakeProcesses(), DataRoles())
    monkeypatch.setattr(c, "inspect_source", lambda: {})
    assert c.run()["status"] == "PASS"
    assert not c.paths["map"].exists()
    assert all(e["to"] != "MAP_CREATED" for e in c.events)


def test_action_mask_rejects_stale_or_illegal_selection():
    q = policy.question("AUTHORIZED", "binding", ["EXECUTE", "STOP"], {})
    valid = {"question_hash": q["question_hash"], "action": "EXECUTE", "scores": {}}
    assert policy.validate(valid, q) == valid
    for delta in (
        {"action": "RELEASE"},
        {"question_hash": "stale"},
        {"scores": {"EXECUTE": float("nan")}},
    ):
        with pytest.raises(Refused):
            policy.validate({**valid, **delta}, q)


def test_run_lock_refuses_another_writer_and_releases(tmp_path):
    with run_lock(tmp_path):
        with pytest.raises(Refused, match="already owned"):
            with run_lock(tmp_path):
                pass
    with run_lock(tmp_path):
        pass
