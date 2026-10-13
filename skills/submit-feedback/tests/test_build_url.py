"""Correctness tests for submit-feedback."""

import pytest
from conftest import load_skill, run_skill


@pytest.fixture(scope="module")
def submit_feedback():
    return load_skill("submit-feedback", "build_url").submit_feedback


def test_build_url_prints_github_link(capsys, submit_feedback):
    run_skill(submit_feedback, "--title", "Plot bug", "--body", "Heatmap axis labels overlap.")

    out = capsys.readouterr().out
    assert "github.com/rhiza-research/forecasting-skills/issues/new" in out
    assert "title=Plot%20bug" in out


def test_overlong_body_exits_1(submit_feedback):
    body = "x" * 10000
    with pytest.raises(SystemExit) as exc:
        run_skill(submit_feedback, "--title", "Long body", "--body", body)
    assert exc.value.code == 1


def test_repo_flag_targets_allow_listed_repo(capsys, submit_feedback):
    run_skill(submit_feedback, "--title", "t", "--body", "b", "--repo", "weather-skills-plotting")
    assert "github.com/rhiza-research/weather-skills-plotting/issues/new" in capsys.readouterr().out


def test_repo_flag_rejects_other_repos(submit_feedback):
    with pytest.raises(SystemExit) as exc:
        run_skill(submit_feedback, "--title", "t", "--body", "b", "--repo", "someone/else")
    assert exc.value.code == 2
