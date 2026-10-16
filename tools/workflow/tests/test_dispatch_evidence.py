"""Offline native dispatch evidence; no model or child process is launched."""

import json

import pytest

from tools.workflow.agents import Roles
from tools.workflow.contracts import Refused
from tools.workflow.persistence import read_checkpoint
from tools.workflow.runner import Controller
from tools.workflow.tests.test_workflow import request


class Boundary:
    total_seconds = 1800
    # dispatch_evidence_mode requires the on_event / run(tool=...) process-receipt interface.
    on_event = None

    def __init__(self, root, failure=None):
        self.root, self.failure, self.calls = root, failure, 0

    def run(self, argv, cwd, env, stdin, tool=None):
        self.calls += 1
        checkpoint = read_checkpoint(self.root)
        if "dispatch_evidence" in checkpoint["context"]:
            assert checkpoint["pending"]["attempt_id"]
            records = events(self.root)
            assert records[-1]["kind"] == "invocation_prepared"
            assert records[-1]["attempt_id"] == checkpoint["pending"]["attempt_id"]
        if self.failure:
            raise self.failure
        return {
            "exit_code": 0,
            "stderr": "",
            "stdout": json.dumps(
                {
                    "total_cost_usd": 0.1,
                    "session_id": f"session-{self.calls}",
                    "result": '{"ok": true}',
                }
            ),
        }


def events(root):
    return [json.loads(p.read_text()) for p in sorted(root.glob("dispatch-event-*.json"))]


def make(tmp_path, monkeypatch, enabled=True, failure=None):
    monkeypatch.setattr("tools.workflow.runner.build_manifest", lambda _: {"fixture": 1})
    monkeypatch.setattr("tools.workflow.agents.shutil.which", lambda _: "never-executed")
    source = tmp_path / "source"
    source.write_text("fixture")
    root = tmp_path / "run"
    root.mkdir()
    process = Boundary(root, failure)
    roles = Roles(process, root, budget=5)
    return Controller(
        tmp_path, request(), source, root, process, roles, dispatch_evidence_mode=enabled
    )


def test_attempt_is_durable_before_invocation_and_correlates_success(tmp_path, monkeypatch):
    c = make(tmp_path, monkeypatch)
    c.ask("compiler", {"fixture": 1})
    records = events(c.root)
    assert [r["sequence"] for r in records] == [1, 2, 3]
    assert len({r["attempt_id"] for r in records}) == 1
    assert records[1]["invocation_id"] == c.roles.receipts[0]["invocation_id"]
    assert records[0]["occurrence"] == 1
    assert read_checkpoint(c.root)["pending"] is None


def test_resume_continues_global_sequence_and_role_occurrence(tmp_path, monkeypatch):
    c = make(tmp_path, monkeypatch)
    c.ask("compiler", {})
    c.advance("SOURCE_CHECKED", {"fixture": True})
    resumed = Controller.resume(c.root, c.processes, Roles(c.processes, c.root))
    resumed.ask("compiler", {})
    records = events(c.root)
    # The checkpoint has no terminal record, so the resume first records the stop it observed
    # (wave 7: one interruption, observed=checkpoint_without_terminal) on the same clock.
    assert [r["sequence"] for r in records] == list(range(1, 10))
    assert records[4]["kind"] == "interruption"
    assert records[5]["kind"] == "resume"
    assert records[6]["occurrence"] == 2
    assert records[6]["resume_phase"] == 1


@pytest.mark.parametrize("error", [RuntimeError("lost envelope"), KeyboardInterrupt()])
def test_failed_boundary_has_identity_no_fabricated_receipt_and_refuses_resume(
    tmp_path,
    monkeypatch,
    error,
):
    c = make(tmp_path, monkeypatch, failure=error)
    with pytest.raises(type(error)):
        c.ask("compiler", {})
    records = events(c.root)
    failed = records[-1]
    assert failed["outcome"] == "UNKNOWN"
    assert failed["invocation_observation"]["invocation_id"] == records[1]["invocation_id"]
    assert c.roles.receipts == []
    assert "session_id" not in failed and "cost_usd" not in failed
    with pytest.raises(Refused, match="uncertain outcome"):
        Controller.resume(c.root, c.processes, Roles(c.processes, c.root))


def test_legacy_receipts_and_checkpoint_have_no_opt_in_fields(tmp_path, monkeypatch):
    c = make(tmp_path, monkeypatch, enabled=False)
    c.ask("compiler", {})
    assert events(c.root) == []
    assert "dispatch_evidence" not in c.context
    assert "controller_dispatch" not in c.roles.receipts[0]


def test_combined_modes_persist_identical_correlated_receipt(tmp_path, monkeypatch):
    from tools.workflow.tests.test_review_evidence import (
        ReviewReached,
        make_controller,
        snapshots,
    )

    c, _ = make_controller(tmp_path, monkeypatch)
    c.context["dispatch_evidence"] = {
        "schema": "workflow.dispatch-evidence/1",
        "run_id": "combined-test",
        "sequence": 0,
        "occurrences": {},
    }
    c.checkpoint()
    with pytest.raises(ReviewReached):
        c._run()
    (snapshot,) = snapshots(c)
    receipt = c.roles.receipts[-1]
    assert (
        receipt["controller_dispatch"]["attempt_id"] == snapshot["controller_attempt"]["attempt_id"]
    )
    assert snapshot["receipt"] == receipt
    returned = [r for r in events(c.root) if r["kind"] == "dispatch_returned"][-1]
    assert returned["receipt"] == receipt
    (role_file,) = c.root.glob("role-*-reviewer.json")
    assert json.loads(role_file.read_text())["receipt"] == receipt
    assert read_checkpoint(c.root)["role_receipts"][-1] == receipt


@pytest.mark.parametrize("failure_point", ["save", "checkpoint"])
def test_failure_evidence_errors_preserve_primary_exception_and_pending(
    tmp_path,
    monkeypatch,
    failure_point,
):
    primary = RuntimeError("original dispatch failure")
    c = make(tmp_path, monkeypatch, failure=primary)
    original = getattr(c, failure_point)

    def fail_after_dispatch(*args, **kwargs):
        if c.processes.calls:
            raise OSError("evidence unavailable")
        return original(*args, **kwargs)

    monkeypatch.setattr(c, failure_point, fail_after_dispatch)
    with pytest.raises(RuntimeError) as caught:
        c.ask("compiler", {})
    assert caught.value is primary
    assert "Dispatch failure evidence incomplete: OSError" in primary.__notes__
    assert read_checkpoint(c.root)["pending"]["attempt_id"] == c.pending["attempt_id"]
    with pytest.raises(Refused, match="uncertain outcome"):
        Controller.resume(c.root, c.processes, Roles(c.processes, c.root))
