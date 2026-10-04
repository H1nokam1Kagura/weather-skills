"""Correctness tests for decision-packet: the outbound completeness gate, the review page, and
inbound reply parsing. Deterministic; no model, no network."""

import copy
import json

import pytest
from conftest import load_skill, run_skill

REQ = "Compare this season's rain forecast against the station gauges in Malawi."


@pytest.fixture(scope="module")
def dp():
    return load_skill("decision-packet", "decision_packet")


@pytest.fixture(scope="module")
def gc():
    return load_skill("goal-check", "goal_check")


@pytest.fixture()
def packet(gc):
    """A real packet, produced by goal-check from three disagreeing samples."""
    base = {
        "task": "fcst_vs_obs",
        "variable": "precip",
        "region": "Malawi",
        "time_window": "relative",
        "legacy_cumulative": False,
        "obs_source": "station",
    }
    rep = gc.evaluate(
        [base | {"period": "monthly"}, base | {"period": "weekly"}, base | {"period": "monthly"}],
        REQ,
    )
    return copy.deepcopy(rep["questions"][0])


def _run(dp, tmp_path, capsys, packets, *extra):
    argv = []
    for i, p in enumerate(packets):
        f = tmp_path / f"p{i}.json"
        f.write_text(json.dumps(p), encoding="utf-8")
        argv += ["--packet", str(f)]
    code = 0
    try:
        run_skill(dp.decision_packet, *argv, *extra)
    except SystemExit as exc:
        code = exc.code
    cap = capsys.readouterr()
    return code, json.loads(cap.out) if cap.out.strip() else None, cap.err


def test_complete_packet_passes(dp, tmp_path, capsys, packet):
    code, rep, _ = _run(dp, tmp_path, capsys, [packet])
    assert code == 0
    assert rep["ready"] is True and rep["packets"][0]["status"] == "ready"


def _drop(path):
    def f(p):
        obj = p
        *head, last = path
        for k in head:
            obj = obj[k]
        del obj[last]

    return f


def _set(path, value):
    def f(p):
        obj = p
        *head, last = path
        for k in head:
            obj = obj[k]
        obj[last] = value

    return f


@pytest.mark.parametrize(
    "mutate, named",
    [
        (_drop(["question"]), "question"),
        (_set(["question"], "Weekly? Or monthly?"), "exactly one question"),
        (_set(["options"], []), "options"),
        (_drop(["options", 0, "definition"]), "options[0].definition"),
        (_set(["options", 1, "definition"], "Week by week"), "repeats the label"),
        (_drop(["options", 0, "evidence"]), "options[0].evidence"),
        (_drop(["options", 1, "evidence", 0, "source"]), "options[1].evidence[0].source"),
        (_drop(["options", 0, "downstream"]), "options[0].downstream"),
        (_drop(["default"]), "default"),
        (_drop(["default", "if_no_reply"]), "default.if_no_reply"),
        (_set(["default", "option"], "daily"), "default.option"),
        (_set(["checks_run"], []), "checks_run"),
        (_drop(["checks_run", 0, "settled"]), "checks_run[0].settled"),
        (_set(["options", 0, "selected"], True), "nothing pre-selected"),
        (_set(["recommended"], "monthly"), "nothing pre-selected"),
        (_drop(["request"]), "request"),
    ],
)
def test_each_missing_element_blocks_and_is_named(dp, tmp_path, capsys, packet, mutate, named):
    mutate(packet)
    code, rep, err = _run(dp, tmp_path, capsys, [packet])
    assert code == 1, rep
    assert any(named in m for m in rep["packets"][0]["missing"]), rep["packets"][0]["missing"]
    assert "BLOCKED" in err and named in err


def test_rule_decidable_goal_slot_blocks(dp, tmp_path, capsys, packet):
    """Asking station-vs-grid about a request that names no station or gauge is premature."""
    packet["slot"] = "obs_source"
    packet["request"] = "Compare the rain forecast against what was observed in Malawi."
    packet["options"][0]["code"], packet["options"][1]["code"] = "station", "grid"
    code, rep, err = _run(dp, tmp_path, capsys, [packet])
    assert code == 4
    assert "rule settles obs_source" in rep["packets"][0]["rule_decidable"][0]
    assert "do not ask" in err


def test_settled_check_blocks(dp, tmp_path, capsys, packet):
    packet["checks_run"][0]["settled"] = True
    code, rep, _ = _run(dp, tmp_path, capsys, [packet])
    assert code == 4


def test_single_option_blocks(dp, tmp_path, capsys, packet):
    packet["options"] = packet["options"][:1]
    code, rep, _ = _run(dp, tmp_path, capsys, [packet])
    assert code == 4


def test_chat_round_allows_two_questions(dp, tmp_path, capsys, packet):
    ps = [dict(packet, id=f"q{i}") for i in range(3)]
    code, rep, err = _run(dp, tmp_path, capsys, ps)
    assert code == 1 and "round" in rep
    code, rep, _ = _run(dp, tmp_path, capsys, ps, "--channel", "page")
    assert code == 0


def test_html_page_has_nothing_preselected(dp, tmp_path, capsys, packet):
    out = tmp_path / "pages"
    code, rep, _ = _run(dp, tmp_path, capsys, [packet], "--mode", "html", "--html-dir", str(out))
    assert code == 0
    page = (out / "decide_goal-period.html").read_text(encoding="utf-8")
    spec = json.loads((out / "review_spec_goal-period.json").read_text(encoding="utf-8"))
    assert spec["purpose"] == "ground_truth"
    assert not any(it.get("current") for it in spec["items"])
    assert spec["csv"]["header"][-2:] == ["DECISION", "correction_notes"]
    assert spec["defer"]["code"] == "CANT_TELL"
    assert packet["question"] in page and "--decisions" in rep["import"]


def test_html_refuses_incomplete_packet(dp, tmp_path, capsys, packet):
    del packet["options"][0]["definition"]
    out = tmp_path / "pages"
    code, _, _ = _run(dp, tmp_path, capsys, [packet], "--mode", "html", "--html-dir", str(out))
    assert code == 1
    assert not out.exists() or not list(out.iterdir())


@pytest.mark.parametrize("reply", ["2", "weekly", "Week by week", "b", "weekly please"])
def test_import_reply_answers_and_echoes(dp, tmp_path, capsys, packet, reply):
    code, rep, _ = _run(dp, tmp_path, capsys, [packet], "--mode", "import", "--reply", reply)
    assert code == 0
    assert rep["goal_patch"] == {"period": "weekly"}
    assert rep["echo"].startswith("Changed period: (not set) -> Week by week")
    assert rep["updated_goal"]["period"] == "weekly" and rep["still_unresolved"] == []


def test_import_ambiguous_reply_gets_one_clarifying_question(dp, tmp_path, capsys, packet):
    code, rep, _ = _run(dp, tmp_path, capsys, [packet], "--mode", "import", "--reply", "either")
    assert code == 3 and rep["status"] == "ambiguous"
    assert rep["clarifying_question"].count("?") == 1
    packet["clarifications_asked"] = 1
    code, rep, _ = _run(dp, tmp_path, capsys, [packet], "--mode", "import", "--reply", "either")
    assert code == 3 and rep["status"] == "unresolved_after_clarification"
    assert "clarifying_question" not in rep


def test_import_insufficient_information(dp, tmp_path, capsys, packet):
    code, rep, _ = _run(
        dp, tmp_path, capsys, [packet], "--mode", "import", "--reply", "I can't tell from this"
    )
    assert code == 3 and rep["insufficient_information"] is True
    assert rep["next_action"].startswith("improve_packet")


def _csv(tmp_path, decision, note=""):
    f = tmp_path / "decisions.csv"
    f.write_text(
        "﻿packet_id,slot,question,DECISION,correction_notes\r\n"
        f'goal-period,period,"q",{decision},"{note}"\r\n',
        encoding="utf-8",
    )
    return str(f)


def test_import_csv_round_trip(dp, tmp_path, capsys, packet):
    args = ["--mode", "import", "--decisions", _csv(tmp_path, "monthly")]
    code, rep, _ = _run(dp, tmp_path, capsys, [packet], *args)
    assert code == 0 and rep["goal_patch"] == {"period": "monthly"}
    args = ["--mode", "import", "--decisions", _csv(tmp_path, "CANT_TELL", "need last year")]
    code, rep, _ = _run(dp, tmp_path, capsys, [packet], *args)
    assert code == 3 and rep["insufficient_information"] and rep["note"] == "need last year"
    args = ["--mode", "import", "--decisions", _csv(tmp_path, "daily")]
    code, rep, _ = _run(dp, tmp_path, capsys, [packet], *args)
    assert code == 1 and rep["status"] == "not_an_option"


def test_import_inconsistent_answer_is_rejected(dp, tmp_path, capsys, packet):
    packet["goal"]["task"] = "spread_map"  # a spread map takes no period
    code, rep, _ = _run(dp, tmp_path, capsys, [packet], "--mode", "import", "--reply", "1")
    assert code == 1 and rep["status"] == "inconsistent"
