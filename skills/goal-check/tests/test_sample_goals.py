"""sample_goals: one-command independent sampling. No model, no network: compiles are faked."""

import json

import pytest
from conftest import load_skill, run_skill

GOAL = {
    "task": "map",
    "variable": "precip",
    "region": "Kenya",
    "time_window": "relative",
    "period": "weekly",
    "legacy_cumulative": False,
    "obs_source": "grid",
}
REQ = "Weekly rainfall map for Kenya for the next month."


@pytest.fixture()
def sg(monkeypatch):
    mod = load_skill("goal-check", "sample_goals")
    monkeypatch.setattr(mod.shutil, "which", lambda name: "claude")
    return mod


def _fake(monkeypatch, sg, results):
    it = iter(results)
    monkeypatch.setattr(sg, "_compile", lambda *a, **k: next(it))


def _run(sg, capsys, *argv):
    with pytest.raises(SystemExit) as exc:
        run_skill(sg.sample_goals, "--request", REQ, *argv)
    return exc.value.code, json.loads(capsys.readouterr().out)


def ok(goal):
    return {"ok": True, "error": None, "goal": goal}


def fail(msg="exit 1: boom"):
    return {"ok": False, "error": msg, "goal": None}


def test_three_agreeing_compiles_are_independent_and_resolved(sg, monkeypatch, capsys):
    _fake(monkeypatch, sg, [ok(GOAL), ok(GOAL), ok(GOAL)])
    code, rep = _run(sg, capsys)
    assert code == 0 and rep["sampling"] == "independent" and rep["samples_ok"] == 3
    assert rep["compile_prompt_sha256"]


def test_disagreement_needs_human(sg, monkeypatch, capsys):
    _fake(monkeypatch, sg, [ok(GOAL), ok(GOAL), ok({**GOAL, "period": "monthly"})])
    code, rep = _run(sg, capsys)
    assert code == 3 and rep["sampling"] == "independent" and rep["disagreements"]


def test_partial_failure_is_degraded_and_loud(sg, monkeypatch, capsys):
    _fake(monkeypatch, sg, [ok(GOAL), fail(), ok(GOAL)])
    code, rep = _run(sg, capsys)
    assert code == 5 and rep["sampling"] == "degraded" and rep["samples_ok"] == 2
    assert rep["compile_failures"] == ["exit 1: boom"]


def test_allow_degraded_returns_goal_check_code_but_still_reports(sg, monkeypatch, capsys):
    _fake(monkeypatch, sg, [ok(GOAL), fail(), ok(GOAL)])
    code, rep = _run(sg, capsys, "--allow-degraded")
    assert code == 0 and rep["sampling"] == "degraded"


def test_all_failed_is_unavailable(sg, monkeypatch, capsys):
    _fake(monkeypatch, sg, [fail(), fail("timeout after 180s"), fail()])
    code, rep = _run(sg, capsys)
    assert code == 4 and rep["sampling"] == "unavailable" and rep["samples_ok"] == 0


def test_no_cli_is_unavailable(sg, monkeypatch, capsys):
    monkeypatch.setattr(sg.shutil, "which", lambda name: None)
    code, rep = _run(sg, capsys)
    assert code == 4 and rep["reason"] == "claude CLI not on PATH"


def test_unparsable_reply_is_an_invalid_sample_not_a_degradation(sg, monkeypatch, capsys):
    _fake(monkeypatch, sg, [ok(GOAL), ok(None), ok(GOAL)])
    code, rep = _run(sg, capsys)
    assert rep["sampling"] == "independent" and code == 3 and rep["resample"] is True


def test_extract_json_finds_object_in_chatter(sg):
    assert sg._extract_json('Sure! {"task": "map"} done') == {"task": "map"}
    assert sg._extract_json("no json here") is None
