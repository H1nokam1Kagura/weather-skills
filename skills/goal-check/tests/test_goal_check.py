"""Correctness tests for goal-check (deterministic; no model, no network)."""

import json

import pytest
from conftest import SKILLS_ROOT, load_skill, run_skill


@pytest.fixture(scope="module")
def gc():
    return load_skill("goal-check", "goal_check")


def _goal(**kw):
    g = {
        "task": "map",
        "variable": "t2m",
        "region": None,
        "time_window": None,
        "period": None,
        "legacy_cumulative": False,
        "obs_source": "grid",
    }
    g.update(kw)
    return g


def _run(gc, tmp_path, capsys, goals, request=None):
    argv = []
    for i, g in enumerate(goals):
        p = tmp_path / f"g{i}.json"
        p.write_text(g if isinstance(g, str) else json.dumps(g), encoding="utf-8")
        argv += ["--goal", str(p)]
    if request is not None:
        argv += ["--request", request]
    code = 0
    try:
        run_skill(gc.goal_check, *argv)
    except SystemExit as exc:
        code = exc.code
    return code, json.loads(capsys.readouterr().out)


def test_valid_goal_resolves(gc, tmp_path, capsys):
    g = _goal(task="map", variable="precip", region="Ethiopia", period="weekly")
    code, rep = _run(gc, tmp_path, capsys, [g], "Weekly rainfall map for Ethiopia please.")
    assert code == 0
    assert rep["status"] == "resolved"
    assert rep["goal"]["admin_level"] == "country"
    assert rep["questions"] == []
    assert rep["readback"]["time_resolution"] == "weekly totals"


def test_missing_required_period_needs_human(gc, tmp_path, capsys):
    g = _goal(task="map", variable="precip", region="Kenya")
    code, rep = _run(gc, tmp_path, capsys, [g], "Rain map for Kenya.")
    assert code == 3
    assert [m["slot"] for m in rep["unresolved"]] == ["period"]
    (q,) = rep["questions"]
    assert q["slot"] == "period" and {o["code"] for o in q["options"]} == {"weekly", "monthly"}
    assert q["goal"]["period"] is None


def test_onset_map_resolves_without_rainfall_aggregation(gc, tmp_path, capsys):
    goal = _goal(
        task="onset_map",
        variable="precip",
        region="Kenya OND region",
        time_window="fixed",
        window_start="2025-09-01",
        window_end="2025-12-31",
    )
    code, rep = _run(
        gc,
        tmp_path,
        capsys,
        [goal, goal, goal],
        "Map rainy-season onset dates for Kenya OND region in 2025.",
    )
    assert code == 0
    assert rep["questions"] == []
    assert rep["goal"]["period"] is None
    assert "onset" in rep["readback"]["output"]


@pytest.mark.parametrize("overrides", [{"period": "weekly"}, {"variable": "t2m"}])
def test_onset_map_rejects_wrong_quantity_or_aggregation(gc, tmp_path, capsys, overrides):
    goal = _goal(task="onset_map", variable="precip") | overrides
    code, rep = _run(gc, tmp_path, capsys, [goal], "Map rainy-season onset dates.")
    assert code == 1 and rep["errors"]


def test_missing_task_needs_human(gc, tmp_path, capsys):
    code, rep = _run(gc, tmp_path, capsys, [_goal(task=None)], "Something about temperature.")
    assert code == 3
    assert rep["unresolved"][0]["slot"] == "task"


@pytest.mark.parametrize(
    "bad",
    [
        _goal(variable="wind_speed"),
        _goal(task="spread_map", period="weekly"),
        _goal(task="change_map", time_window="relative"),
        _goal(variable="t2m", legacy_cumulative=True),
        _goal(period="daily"),
    ],
)
def test_invalid_goal_exits_1(gc, tmp_path, capsys, bad):
    code, rep = _run(gc, tmp_path, capsys, [bad], "next month please")
    assert code == 1
    assert rep["status"] == "invalid" and rep["errors"]


def test_unparsable_single_sample_is_invalid(gc, tmp_path, capsys):
    code, rep = _run(gc, tmp_path, capsys, ["I think you want a map"], "a map")
    assert code == 1


def test_disagreeing_samples_need_human(gc, tmp_path, capsys):
    req = "Compare this season's rain forecast against the station gauges in Malawi."
    base = _goal(task="fcst_vs_obs", variable="precip", region="Malawi", time_window="relative")
    samples = [
        base | {"period": "monthly"},
        base | {"period": "weekly"},
        base | {"period": "monthly"},
    ]
    code, rep = _run(gc, tmp_path, capsys, samples, req)
    assert code == 3
    (d,) = rep["disagreements"]
    assert d["slot"] == "period" and d["values"] == {'"monthly"': 2, '"weekly"': 1}
    assert rep["questions"][0]["slot"] == "period"


def test_agreeing_samples_resolve(gc, tmp_path, capsys):
    g = _goal(task="spread_map", variable="sst")
    code, rep = _run(gc, tmp_path, capsys, [g, g, g], "How spread out are the SST members?")
    assert code == 0 and rep["disagreements"] == []


def test_rule_removes_disagreement(gc, tmp_path, capsys):
    """v3rr: with no station cue, 'station' is rewritten to 'grid', so the samples agree."""
    base = _goal(task="fcst_vs_obs", variable="t2m")
    samples = [base | {"obs_source": "station"}, base, base]
    code, rep = _run(gc, tmp_path, capsys, samples, "Forecast versus observed temperature.")
    assert code == 0
    assert rep["goal"]["obs_source"] == "grid"


def test_rule_station_and_relative_cues(gc, tmp_path, capsys):
    g = _goal(task="fcst_vs_obs", variable="t2m", obs_source="station", time_window="relative")
    code, rep = _run(gc, tmp_path, capsys, [g], "Forecast vs observed temperature for 2024.")
    assert code == 0
    applied = {r["slot"]: r for r in rep["rules_applied"]}
    assert applied["obs_source"]["to"] == "grid" and "v3rr" in applied["obs_source"]["source"]
    assert applied["time_window"]["to"] is None and "v3rr" in applied["time_window"]["source"]


def test_defaults_filled_with_cited_source(gc, tmp_path, capsys):
    g = _goal(task="change_map", variable="precip", period="monthly")
    code, rep = _run(gc, tmp_path, capsys, [g], "Monthly precipitation change, future minus past.")
    assert code == 0
    assert rep["goal"]["baseline"] == "1991-2020"
    by_slot = {d["slot"]: d for d in rep["defaults"]}
    assert "WMO" in by_slot["baseline"]["source"]
    assert by_slot["region"]["source"] and by_slot["region"]["why"]
    assert by_slot["output"]["value"] == "change_map"
    assert "1991-2020" in rep["readback"]["baseline"]


def test_invalid_sample_among_three_asks_for_redraw(gc, tmp_path, capsys):
    g = _goal(task="spread_map", variable="sst")
    code, rep = _run(gc, tmp_path, capsys, [g, "not json at all", g], "SST member spread")
    assert code == 3
    assert rep["resample"] is True and rep["questions"] == []
    assert rep["invalid_samples"][0]["index"] == 1


def test_relative_dates_are_never_compiled(gc, tmp_path, capsys):
    g = _goal(
        task="map",
        variable="t2m",
        time_window="relative",
        window_phrase="the last two weeks",
        window_start="2026-09-20",
        window_end="2026-10-04",
    )
    code, rep = _run(gc, tmp_path, capsys, [g], "Temperature map for the last two weeks.")
    assert code == 0
    assert rep["goal"]["window_start"] is None and rep["goal"]["window_end"] is None


def test_at_most_two_questions(gc, tmp_path, capsys):
    a = _goal(task="map", variable="precip", region="Kenya", period="weekly")
    b = _goal(task="bias_map", variable="t2m", region="Malawi", period="monthly")
    code, rep = _run(gc, tmp_path, capsys, [a, b], "something vague")
    assert code == 3
    assert len(rep["questions"]) == 2
    assert rep["queued_questions"]


def test_readback_names_no_skill(gc, tmp_path, capsys):
    skills = {p.name for p in SKILLS_ROOT.iterdir() if (p / "SKILL.md").is_file()}
    g = _goal(task="station_vs_sat", variable="precip", region="western Kenya", period="weekly")
    code, rep = _run(gc, tmp_path, capsys, [g], "Check satellite rain against stations, weekly.")
    assert code == 0
    text = json.dumps(rep["readback"])
    assert not [s for s in skills if s in text]
    assert rep["readback"]["caveats"]  # a non-country place is flagged for the human
