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


def test_reviewer_prompt_states_the_frozen_finding_record_shape():
    # The frozen v2.7 evidence contract types role_output.reviewer[*].findings as records with
    # string fields id, severity and text (DEV-ONLY.json record_lists, read by 66 checks as
    # findings[*].id / .severity / .text) and declares no severity vocabulary. A live reviewer
    # told only "array of grounded concerns" answered {id, severity, category, description}
    # (relay-dedup-smoke run 38859334727), which carries no text.
    from tools.workflow.agents import PROMPTS, role_prompt

    clause = "each an object with string fields id, severity and text"
    assert clause in PROMPTS["reviewer"]
    for payload in ({}, {"review_evidence": {"rationale": "r"}, "claim_check": {}}):
        assert clause in role_prompt("reviewer", payload)
    assert "description" not in PROMPTS["reviewer"]


def test_reviewer_prompt_offers_only_the_frozen_verdict_vocabulary():
    # The frozen v2.7 contract closes role_output.reviewer[*].verdict to APPROVE / REJECT
    # (DEV-ONLY.json record_lists vocabularies); every frozen NEEDS_INFO outcome comes from the
    # planner/repairer needs_info path, never a reviewer verdict. A live reviewer answering
    # NEEDS-INFO would be malformed evidence, so the prompt offers only the closed set. The
    # controller's reviewer NEEDS-INFO handling is unchanged (OI-09 carries the question).
    from tools.workflow.agents import PROMPTS

    assert "verdict (APPROVE/REJECT)" in PROMPTS["reviewer"]
    assert "NEEDS-INFO" not in PROMPTS["reviewer"]
