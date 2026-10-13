import json

import pytest

from tools.workflow.agents import Roles
from tools.workflow.contracts import Refused


class Process:
    def __init__(self, envelope):
        self.envelope = envelope
        self.calls = []

    def run(self, argv, cwd, env, stdin):
        self.calls.append(argv)
        return {"exit_code": 0, "stdout": json.dumps(self.envelope), "stderr": ""}


def role(monkeypatch, tmp_path, **changes):
    monkeypatch.setattr("tools.workflow.agents.shutil.which", lambda _: "claude.exe")
    process = Process(
        {
            "total_cost_usd": 0.1,
            "session_id": "independent-session",
            "result": '{"verdict":"PASS"}',
            **changes,
        }
    )
    return Roles(process, tmp_path, budget=1), process


def test_roles_have_no_tools_and_no_shared_session(monkeypatch, tmp_path):
    agent, process = role(monkeypatch, tmp_path)
    agent.ask("verifier", {})
    command = process.calls[0]
    assert command[command.index("--tools") + 1] == ""
    assert "--strict-mcp-config" in command and "--no-session-persistence" in command
    with pytest.raises(Refused, match="repeated independent session"):
        agent.ask("verifier", {})


@pytest.mark.parametrize(
    "changes",
    [
        {"is_error": True},
        {"total_cost_usd": 1.1},
        {"total_cost_usd": float("nan")},
        {"result": 'Verdict:\n```json\n{"verdict":"PASS"}\n```'},
        {"result": "[]"},
        {"session_id": ""},
    ],
)
def test_invalid_model_envelopes_cannot_be_receipts(monkeypatch, tmp_path, changes):
    agent, _ = role(monkeypatch, tmp_path, **changes)
    with pytest.raises(Refused):
        agent.ask("verifier", {})
    assert agent.receipts == []
