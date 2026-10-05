"""sample_goals: one-command independent sampling. No model, no network: compiles are faked."""

import json
import threading
import time

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


def test_scope_retries_empty_then_compiles(sg, monkeypatch, capsys):
    _fake(
        monkeypatch,
        sg,
        [ok(None), ok({"in_scope": True, "unsupported": []}), ok(GOAL), ok(GOAL), ok(GOAL)],
    )
    code, rep = _run(sg, capsys, "--scope-profile", "d66")
    assert code == 0 and rep["samples_ok"] == 3
    assert len(rep["scope_check"]["attempts"]) == 2


def test_out_of_scope_stops_before_compilation(sg, monkeypatch, capsys):
    _fake(monkeypatch, sg, [ok({"in_scope": False, "unsupported": ["task"]})])
    code, rep = _run(sg, capsys, "--scope-profile", "d66")
    assert code == 6 and rep["samples_ok"] == 0
    assert rep["scope_check"]["status"] == "out_of_scope"


@pytest.mark.parametrize(
    "value",
    [
        None,
        {"in_scope": "false", "unsupported": []},
        {"in_scope": True, "unsupported": ["time"]},
        {"in_scope": False, "unsupported": []},
    ],
)
def test_invalid_scope_exhausts_two_retries_without_compiling(sg, monkeypatch, capsys, value):
    _fake(monkeypatch, sg, [ok(value)] * 3)
    code, rep = _run(sg, capsys, "--scope-profile", "d66")
    assert code == 4 and rep["sampling"] == "not_started"
    assert len(rep["scope_check"]["attempts"]) == 3
    assert rep["scope_check"]["decision"] is None


def test_scope_missing_details_reach_existing_clarification(sg, monkeypatch, capsys):
    incomplete = {**GOAL, "time_window": None, "period": None}
    _fake(monkeypatch, sg, [ok({"in_scope": True, "unsupported": []})] + [ok(incomplete)] * 3)
    code, rep = _run(sg, capsys, "--scope-profile", "d66")
    assert code == 3 and rep["scope_check"]["status"] == "in_scope"


def test_scope_prompt_is_frozen(sg):
    import hashlib

    assert hashlib.sha256(sg.SCOPE_PROMPT_FILE.read_text(encoding="utf-8").encode("utf-8")).hexdigest() == (
        "649041311d18f997fb446b39a3b09a6ecfeb4479a3a567c0e4fc8342acc84b89"
    )


def test_report_carries_per_compile_evidence(sg, monkeypatch, capsys):
    rec = {**ok(GOAL), "session_id": "s1", "cost_usd": 0.01, "models": ["claude-sonnet-5-5"]}
    _fake(monkeypatch, sg, [rec, rec, rec])
    code, rep = _run(sg, capsys)
    assert len(rep["compiles"]) == 3 and rep["compiles"][0]["session_id"] == "s1"
    assert rep["compiles"][0]["goal"] == GOAL and rep["compile_prompt_sha256_normalised"]


def test_default_sampling_limits_concurrency_without_reusing_calls(sg, monkeypatch, capsys):
    active = 0
    peak = 0
    calls = 0
    lock = threading.Lock()

    def compile_once(*args):
        nonlocal active, peak, calls
        with lock:
            active += 1
            calls += 1
            session = str(calls)
            peak = max(peak, active)
        time.sleep(0.01)
        with lock:
            active -= 1
        return {**ok(GOAL), "session_id": session}

    monkeypatch.setattr(sg, "_compile", compile_once)
    code, rep = _run(sg, capsys)
    assert code == 0 and calls == 3 and peak == 1
    assert rep["workers"] == 1 and rep["sampling"] == "independent"
    assert len({c["session_id"] for c in rep["compiles"]}) == 3


@pytest.mark.parametrize("workers", ["0", "4"])
def test_invalid_workers_refused_before_model_call(sg, monkeypatch, workers):
    def unexpected(*args):
        pytest.fail("invalid worker count must not start a compile")

    monkeypatch.setattr(sg, "_compile", unexpected)
    with pytest.raises(SystemExit) as exc:
        run_skill(sg.sample_goals, "--request", REQ, "--workers", workers)
    assert exc.value.code != 0
