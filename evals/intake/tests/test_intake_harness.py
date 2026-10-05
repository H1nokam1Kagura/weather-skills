"""Offline tests for the intake eval harness: synthetic outputs only, no model, no network.

uv run python -m pytest evals/intake/tests -q
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

HERE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(HERE))
import claude_shim  # noqa: E402
import run_intake_eval as H  # noqa: E402

UNIT = {
    "id": "u1",
    "cluster": "c1",
    "style": "terse",
    "clarity": "clear",
    "request": "Weekly rainfall map for Kenya for the next month.",
    "truth": {
        "task": "map",
        "variable": "precip",
        "region": "Kenya",
        "relative_time": True,
        "period": "weekly",
        "credentials": False,
        "legacy_cumulative": False,
        "obs_source": "grid",
    },
}


def unit(i: int, clarity="clear", style="terse", **kw) -> dict:
    """Distinct request AND truth per unit (as in the sample): no false duplicate-card defect."""
    regions = ["Kenya", "Senegal", "Ethiopia", "Malawi", "western Kenya", None]
    truth = UNIT["truth"] | {
        "region": regions[i % 6],
        "period": ["weekly", "monthly"][(i // 6) % 2],
        "variable": ["precip", "t2m"][(i // 12) % 2],
        "relative_time": bool((i // 24) % 2 == 0),
    }
    req = f"Request {i}: {truth['period']} {truth['variable']} map, {truth['region']}, next month."
    return (
        UNIT
        | {
            "id": f"u{i}",
            "cluster": f"c{i}",
            "clarity": clarity,
            "style": style,
            "request": req,
            "truth": truth,
        }
        | kw
    )


def good_raw(u=UNIT, kind="oracle") -> dict:
    return H.backend_synthetic(u, kind)


def with_card(raw: dict, **card_over) -> dict:
    card = H.parse_card(raw["text"]) | card_over
    text = raw["text"].split("```json")[0] + "```json\n" + json.dumps(card) + "\n```"
    return raw | {"text": text, "envelope": raw["envelope"] | {"result": text}}


# ---------------------------------------------------------------- premise: truth scores exact ---
def test_sample_truth_as_agent_goal_scores_exact():
    if not H.SAMPLE.exists():
        pytest.skip("requests_sample.jsonl not built (gitignored); run build_sample.py")
    units = H.load_units()
    assert H.premise_audit(units) == []
    # and through the full classify path (oracle card + 3 observed compiles)
    assert all(H.classify(u, good_raw(u))["outcome"] == "exact" for u in units)


def test_truth_schema_matches_scorer_fields():
    if not H.SAMPLE.exists():
        pytest.skip("sample not built")
    for u in H.load_units():
        assert set(H.FIELDS) <= set(u["truth"]), u["id"]
        assert set(u["truth"]) - set(H.FIELDS) == {"credentials"}, u["id"]  # world state, unscored


def test_station_cue_adjusts_truth_and_is_recorded():
    u = unit(
        2,
        clarity="underdetermined",
        request="Forecast vs gauge rainfall for Malawi, weekly.",
        truth=UNIT["truth"] | {"task": "fcst_vs_obs", "region": "Malawi", "relative_time": False},
    )
    t, adj = H.effective_truth(u)
    assert t["obs_source"] == "station" and adj
    assert H.classify(u, good_raw(u))["outcome"] == "exact"


def test_region_canonicalised_both_sides():
    raw = good_raw()
    card = H.parse_card(raw["text"])
    card["goal"]["region"] = "  kenya "
    assert H.classify(UNIT, with_card(raw, goal=card["goal"]))["outcome"] == "exact"


def global_label_unit():
    correction = H.LABEL_CORRECTIONS[0]
    return unit(
        1,
        id=correction["id"],
        request=correction["request"],
        clarity="underdetermined",
        truth=UNIT["truth"] | {"region": None},
    )


def test_audited_global_label_corrected_without_mutating_sample():
    u = global_label_unit()
    assert H.premise_audit([u]) == []
    record = H.classify(u, good_raw(u))
    assert record["outcome"] == "exact"
    assert record["truth_adjusted"][0].startswith("region:None->global")
    assert record["clarity"] == "underdetermined"
    assert u["truth"]["region"] is None
    text, _, _ = H.report([record], "Correction smoke")
    assert "region:None->global" in text


@pytest.mark.parametrize("region", [None, "Kenya"])
def test_global_label_correction_still_catches_wrong_agent_region(region):
    u = global_label_unit()
    raw = good_raw(u)
    goal = H.parse_card(raw["text"])["goal"] | {"region": region}
    record = H.classify(u, with_card(raw, goal=goal))
    assert record["outcome"] == "silent_wrong"


def test_region_unspecified_is_not_globally_relabelled():
    u = unit(1, truth=UNIT["truth"] | {"region": None})
    truth, adjustments = H.effective_truth(u)
    assert truth["region"] is None and adjustments == []


@pytest.mark.parametrize("change", ["request", "truth"])
def test_stale_label_correction_refused(change):
    u = global_label_unit()
    if change == "request":
        u["request"] = "Weekly rainfall map for Kenya."
    else:
        u["truth"] = u["truth"] | {"region": "Kenya"}
    with pytest.raises(ValueError, match="label correction"):
        H.effective_truth(u)


def test_already_corrected_label_is_idempotent():
    u = global_label_unit()
    u["truth"] = u["truth"] | {"region": "global"}
    truth, adjustments = H.effective_truth(u)
    assert truth["region"] == "global" and adjustments == []


def test_controls_have_the_truth_they_claim():
    pos, neg = H.CONTROLS
    assert (
        H.GC.evaluate([{**pos["truth"], "time_window": "relative"}], pos["request"])["exit_code"]
        == 0
    )
    rep = H.GC.evaluate([{**neg["truth"], "time_window": None}], neg["request"])
    assert rep["exit_code"] == 3 and [m["slot"] for m in rep["unresolved"]] == ["period"]


# --------------------------------------------------------- absence is never scored as success ---
ABSENCE = {
    "timeout": lambda r: {
        "timed_out": True,
        "rc": None,
        "text": "",
        "envelope": None,
        "nested": [],
    },
    "rc_nonzero": lambda r: r | {"rc": 1},
    "error_envelope": lambda r: (
        r | {"envelope": r["envelope"] | {"is_error": True, "subtype": "error_max_turns"}}
    ),
    "no_envelope": lambda r: r | {"envelope": None},
    "empty_text": lambda r: r | {"text": ""},
    "unparseable": lambda r: r | {"text": "I made a goal card but forgot the JSON"},
    "unknown_status": lambda r: with_card(r, status="done"),
    "ready_without_goal": lambda r: with_card(
        r, goal=None, status="ready_for_approval", questions=[]
    ),
    "spawn_error": lambda r: {"spawn_error": "No such file"},
}
DEGRADE = {
    "card_single": lambda r: with_card(r, sampling="single"),
    "card_missing_sampling": lambda r: with_card(r, sampling=None),
    "card_samples_1": lambda r: with_card(r, samples=1),
    "no_nested_observed": lambda r: r | {"nested": []},
    "two_nested_ok": lambda r: r | {"nested": r["nested"][:2]},
    "nested_failures": lambda r: (
        r | {"nested": [n | {"rc": 1, "parsed_goal": False} for n in r["nested"]]}
    ),
    "prompt_mismatch": lambda r: (
        r | {"nested": [n | {"system_prompt_sha256": "x"} for n in r["nested"]]}
    ),
    "plugin_from_cache": lambda r: (
        r | {"init": {"plugins": [{"name": "rhiza-forecasting", "path": "C:/elsewhere"}]}}
    ),
}


@pytest.mark.parametrize("name", sorted(ABSENCE))
def test_failures_are_error_never_scored(name):
    for kind in ("oracle", "ask", "perturb"):
        rec = H.classify(UNIT, ABSENCE[name](good_raw(kind=kind)))
        assert rec["outcome"] == "error", (name, kind, rec)
        assert all(rec[k] is None for k in H.METRICS), rec


@pytest.mark.parametrize("name", sorted(DEGRADE))
def test_single_reading_and_nested_failures_are_degraded(name):
    for kind in ("oracle", "ask"):
        rec = H.classify(UNIT, DEGRADE[name](good_raw(kind=kind)))
        assert rec["outcome"] == "degraded", (name, kind, rec)
        assert all(rec[k] is None for k in H.METRICS)
        assert rec["shadow_outcome"] in ("exact", "asked")


def test_redrawn_nested_failure_with_three_ok_is_not_degraded():
    raw = good_raw()
    raw["nested"] = [raw["nested"][0] | {"rc": 1, "parsed_goal": False}] + raw["nested"]
    rec = H.classify(UNIT, raw)
    assert rec["outcome"] == "exact" and rec["nested"]["n_failed"] == 1


def test_plugin_loaded_from_repo_is_fine():
    raw = good_raw() | {"init": {"plugins": [{"name": "rhiza-forecasting", "path": str(H.REPO)}]}}
    assert H.classify(UNIT, raw)["outcome"] == "exact"


def test_metric_sum_over_unscored_record_fails_loudly():
    bad = H.classify(UNIT, ABSENCE["timeout"](good_raw()))
    with pytest.raises(ValueError):
        H._boot([bad], "silent_wrong")


@pytest.mark.parametrize(
    "text,kind",
    [
        ("Invalid API key · Please run /login", "auth"),
        ("OAuth token has expired", "auth"),
        ("Credit balance is too low", "auth"),
        ("API Error: 429 rate_limit_error", "rate_limit"),
        ("Overloaded", "rate_limit"),
        ("API Error: 503 upstream", "transient"),
        ("ECONNRESET", "transient"),
        ("something odd", "other"),
    ],
)
def test_failure_kinds(text, kind):
    assert H.classify_failure(text) == kind


def test_auth_in_envelope_is_auth_error():
    raw = good_raw() | {
        "rc": 1,
        "envelope": {
            "is_error": True,
            "subtype": "success",
            "result": "Invalid API key · Please run /login",
        },
    }
    rec = H.classify(UNIT, raw)
    assert rec["outcome"] == "error" and rec["error_kind"] == "auth"


def test_nested_auth_failure_is_fatal_error_not_degraded():
    raw = good_raw()
    raw["nested"] = [
        n | {"rc": 1, "parsed_goal": False, "stderr_head": "Please run /login"}
        for n in raw["nested"]
    ]
    rec = H.classify(UNIT, with_card(raw, sampling="single"))
    assert rec["outcome"] == "error" and rec["error_kind"] == "auth"


def test_ask_outcomes_and_rule_ask():
    rec = H.classify(UNIT, good_raw(kind="ask"))
    assert (
        rec["outcome"] == "asked" and rec["unnecessary"] == 1 and rec["asked_slots"] == ["period"]
    )
    raw = with_card(
        good_raw(kind="ask"), questions=["goal-obs_source"]
    )  # rule settles: no station cue
    assert H.classify(UNIT, raw)["rule_ask"] == 1


def test_readback_question_is_not_an_ask():
    raw = with_card(good_raw(), questions=[{"kind": "plan_readback"}], approval="plan-readback")
    assert H.classify(UNIT, raw)["outcome"] == "exact"


def test_blocked_and_invalid_goals_are_loud():
    assert (
        H.classify(UNIT, with_card(good_raw(), status="blocked_invalid", goal=None))["outcome"]
        == "loud"
    )
    raw = good_raw()
    card = H.parse_card(raw["text"])
    assert (
        H.classify(UNIT, with_card(raw, goal=card["goal"] | {"task": "nonsense"}))["outcome"]
        == "loud"
    )


def test_card_vs_recheck_inconsistency_is_reported():
    raw = good_raw()  # three agreeing compiles -> recheck exit 0
    rec = H.classify(UNIT, with_card(raw, status="needs_answers", questions=["goal-period"]))
    assert rec["recheck_exit"] == 0 and rec["card_consistent"] is False


# ---------------------------------------------------------------------------------- report ---
def _recs(n_clear=40, n_bad=0, bad_kind="timeout", style="terse"):
    out = []
    for i in range(n_clear):
        u = unit(i, style=style if i % 2 else "formal")
        raw = ABSENCE[bad_kind](good_raw(u)) if i < n_bad else good_raw(u)
        out.append(H.classify(u, raw) | {"rep": 0})
    return out


def test_clean_run_meets_bars():
    _, headline, code = H.report(_recs(), "# t")
    assert headline == "MEETS BARS" and code == H.EXIT_OK


def test_bad_share_over_10pct_is_inconclusive_nonzero():
    txt, headline, code = H.report(_recs(n_bad=5), "# t")  # 5/40 = 12.5%
    assert headline == "INCONCLUSIVE" and code == H.EXIT_INCONCLUSIVE
    assert "error+degraded" in txt


def test_under_10pct_bad_still_decides():
    _, headline, _ = H.report(_recs(n_bad=3), "# t")  # 3/40
    assert headline == "MEETS BARS"


def test_all_error_stratum_is_inconclusive():
    recs = _recs()
    u = unit(99, style="jargon")
    recs.append(H.classify(u, ABSENCE["timeout"](good_raw(u))) | {"rep": 0})
    _, headline, code = H.report(recs, "# t")
    assert headline == "INCONCLUSIVE" and code == H.EXIT_INCONCLUSIVE


def test_aborted_run_exits_3():
    _, headline, code = H.report(_recs(), "# t", aborted="auth failure")
    assert headline == "INCONCLUSIVE" and code == H.EXIT_ABORTED


def test_min_goals_enforced_on_scored_goals():
    _, headline, _ = H.report(_recs(n_clear=12), "# t")
    assert headline.startswith("not decidable")


def test_identical_cards_for_different_requests_is_a_defect():
    recs = _recs()
    for r in recs[:3]:
        r["card_sha"] = "same"
    txt, headline, _ = H.report(recs, "# t")
    assert headline == "INCONCLUSIVE" and "identical card text" in txt


def test_uniform_outcome_is_flagged():
    txt, _, _ = H.report(_recs(), "# t")
    assert "uniform outcome 'exact'" in txt


def test_underdetermined_never_carries_the_verdict():
    recs = _recs() + [
        H.classify(unit(200 + i, clarity="underdetermined"), good_raw(unit(200 + i))) | {"rep": 0}
        for i in range(5)
    ]
    txt, headline, _ = H.report(recs, "# t")
    assert headline == "MEETS BARS" and "no verdict (diagnostic population" in txt


def test_silent_wrong_fails():
    recs = []
    for i in range(40):
        u = unit(i)
        recs.append(H.classify(u, good_raw(u, "perturb" if i < 8 else "oracle")) | {"rep": 0})
    _, headline, _ = H.report(recs, "# t")
    assert headline == "FAILS"


# --------------------------------------------------------------------------- guard / running ---
def _backend_from(seq):
    """Backend returning raws from a list in order (per call)."""
    it = iter(seq)

    def backend(u, rep, attempt):
        return next(it)(u)

    return backend


def test_transient_error_retried_then_succeeds():
    calls = []

    def backend(u, rep, attempt):
        calls.append(attempt)
        if attempt == 0:
            return {
                "rc": 1,
                "envelope": {
                    "is_error": True,
                    "subtype": "success",
                    "result": "API Error: 503",
                    "total_cost_usd": 0.1,
                },
            }
        return good_raw(u)

    rec = H.run_one(UNIT, 0, backend, None)
    assert rec["outcome"] == "exact" and rec["attempts"] == 2 and calls == [0, 1]
    assert rec["cost_spent"] == pytest.approx(0.1)  # failed attempt's spend is counted


def test_rate_limit_exhausts_retries_and_stops_run(tmp_path):
    def backend(u, rep, attempt):
        return {
            "rc": 1,
            "envelope": {"is_error": True, "subtype": "success", "result": "429 rate limit"},
        }

    guard = H.Guard(100, 0.5)
    recs = []
    jobs = [(0, unit(i)) for i in range(10)]
    left = H.run_jobs(jobs, backend, None, guard, 1, H.make_sink(tmp_path / "r.jsonl", recs))
    assert len(recs) == 1 and recs[0]["attempts"] == 3 and left == 9
    assert "rate_limit" in guard.stop_reason


def test_auth_stops_immediately(tmp_path):
    def backend(u, rep, attempt):
        return {
            "rc": 1,
            "envelope": {"is_error": True, "subtype": "success", "result": "Please run /login"},
        }

    guard = H.Guard(100, 0.5)
    recs = []
    left = H.run_jobs(
        [(0, unit(i)) for i in range(5)],
        backend,
        None,
        guard,
        1,
        H.make_sink(tmp_path / "r.jsonl", recs),
    )
    assert len(recs) == 1 and recs[0]["attempts"] == 1 and left == 4


def test_cost_cap_stops_before_overspend(tmp_path):
    def backend(u, rep, attempt):
        r = good_raw(u)
        r["envelope"] = r["envelope"] | {"total_cost_usd": 1.0}
        return r

    guard = H.Guard(3.5, 1.0)
    recs = []
    left = H.run_jobs(
        [(0, unit(i)) for i in range(10)],
        backend,
        None,
        guard,
        1,
        H.make_sink(tmp_path / "r.jsonl", recs),
    )
    assert len(recs) == 3 and left == 7 and guard.spent <= 3.5 and "cost cap" in guard.stop_reason


def test_unknown_cost_is_charged_at_estimate(tmp_path):
    def backend(u, rep, attempt):
        r = good_raw(u)
        r["envelope"] = {k: v for k, v in r["envelope"].items() if k != "total_cost_usd"}
        return r

    guard = H.Guard(100, 2.0)
    recs = []
    H.run_jobs([(0, unit(1))], backend, None, guard, 1, H.make_sink(tmp_path / "r.jsonl", recs))
    assert recs[0]["cost_unknown"] and guard.spent == 2.0


def test_failure_streak_aborts_early(tmp_path):
    def backend(u, rep, attempt):
        return with_card(good_raw(u), sampling="single")

    guard = H.Guard(1000, 0.0)
    recs = []
    left = H.run_jobs(
        [(0, unit(i)) for i in range(30)],
        backend,
        None,
        guard,
        2,
        H.make_sink(tmp_path / "r.jsonl", recs),
    )
    assert left > 0 and "failure streak" in guard.stop_reason


def test_backend_exception_becomes_error_row(tmp_path):
    def backend(u, rep, attempt):
        raise RuntimeError("boom")

    guard = H.Guard(100, 0.0)
    recs = []
    H.run_jobs([(0, unit(1))], backend, None, guard, 1, H.make_sink(tmp_path / "r.jsonl", recs))
    assert recs[0]["outcome"] == "error" and "harness_exception" in recs[0]["errors"][0]


# ------------------------------------------------------------------------------- preflight ---
def test_preflight_passes_on_correct_controls(tmp_path):
    def backend(u, rep, attempt):
        return good_raw(u, "ask" if u["id"] == "control-neg" else "oracle")

    ok, recs, problems = H.preflight(None, backend, H.Guard(3, 0.0), tmp_path)
    assert ok, problems


def test_preflight_fails_when_negative_control_does_not_ask(tmp_path):
    ok, _, problems = H.preflight(
        None, lambda u, r, a: good_raw(u, "oracle"), H.Guard(3, 0.0), tmp_path
    )
    assert not ok and any("control-neg" in p for p in problems)


def test_preflight_fails_on_degraded_positive(tmp_path):
    def backend(u, rep, attempt):
        r = good_raw(u, "ask" if u["id"] == "control-neg" else "oracle")
        return with_card(r, sampling="single") if u["id"] == "control-pos" else r

    ok, _, problems = H.preflight(None, backend, H.Guard(3, 0.0), tmp_path)
    assert not ok and any("control-pos" in p and "degraded" in p for p in problems)


# ------------------------------------------------------------------------------- main / CLI ---
def test_main_synthetic_end_to_end_and_resume_key(tmp_path, monkeypatch):
    if not H.SAMPLE.exists():
        pytest.skip("sample not built")
    out = tmp_path / "run"
    code = H.main(["--backend", "oracle", "--limit", "5", "--out", str(out)])
    assert code == H.EXIT_OK and (out / "report.md").exists() and (out / "manifest.json").exists()
    m = json.loads((out / "manifest.json").read_text("utf-8"))
    for k in (
        "git_sha",
        "agent_file_sha256",
        "compile_prompt_sha256_normalised",
        "sample_sha256",
        "claude_version",
        "run_key",
        "plugin_content_sha256",
    ):
        assert m.get(k), k
    # resume with the same key: nothing re-run
    n_before = len((out / "records.jsonl").read_text("utf-8").splitlines())
    assert H.main(["--backend", "oracle", "--limit", "5", "--resume", str(out)]) == H.EXIT_OK
    assert len((out / "records.jsonl").read_text("utf-8").splitlines()) == n_before
    # tampered key -> refused
    m["run_key"] = "different"
    (out / "manifest.json").write_text(json.dumps(m), "utf-8")
    assert H.main(["--backend", "oracle", "--resume", str(out)]) == H.EXIT_REFUSED


def test_parse_stream():
    lines = [
        {"type": "system", "subtype": "init", "model": "claude-x", "plugins": []},
        {
            "type": "assistant",
            "message": {
                "content": [
                    {"type": "tool_use", "name": "Bash", "input": {"command": "claude -p ..."}}
                ]
            },
        },
        {
            "type": "result",
            "subtype": "success",
            "is_error": False,
            "result": "card",
            "total_cost_usd": 0.5,
        },
    ]
    init, res, tools = H.parse_stream("noise\n" + "\n".join(json.dumps(x) for x in lines))
    assert init["model"] == "claude-x" and res["result"] == "card" and tools[0]["name"] == "Bash"


# ------------------------------------------------------------------------------------ shim ---
FAKE = r"""
import json, sys
args = sys.argv[1:]
if "--fail" in args:
    sys.stderr.write("Please run /login\n"); sys.exit(1)
assert args[-2:] == ["--output-format", "json"], args
print(json.dumps({"type": "result", "subtype": "success", "is_error": False,
                  "result": '{"task": "map", "variable": "precip"}', "total_cost_usd": 0.01,
                  "modelUsage": {"claude-sonnet-x": {}}}))
"""


@pytest.mark.skipif(H.git_bash() is None, reason="Git Bash not available")
def test_shim_end_to_end_through_bash_launcher(tmp_path):
    fake = tmp_path / "fake_claude.py"
    fake.write_text(FAKE, encoding="utf-8")
    shim_dir = claude_shim.install(tmp_path / "shim")
    logs = tmp_path / "nested"
    prompt = H.COMPILE_PROMPT.read_text(encoding="utf-8")
    env = {
        "INTAKE_REAL_CLAUDE": Path(sys.executable).as_posix(),
        "INTAKE_NESTED_DIR": logs.as_posix(),
    }
    import os

    full_env = os.environ | env
    p = subprocess.run(
        [
            H.git_bash(),
            "-c",
            f'"{(shim_dir / "claude").as_posix()}" "{fake.as_posix()}" -p --model sonnet '
            '--system-prompt "$P" "the request"',
        ],
        capture_output=True,
        text=True,
        env=full_env | {"P": prompt.rstrip("\n")},
    )
    assert p.returncode == 0, p.stderr
    assert json.loads(p.stdout) == {"task": "map", "variable": "precip"}  # text-mode equivalent
    (rec,) = [json.loads(f.read_text("utf-8")) for f in logs.glob("*.json")]
    assert rec["system_prompt_sha256"] == H.COMPILE_SHA and rec["parsed_goal"]
    assert (
        rec["models"] == ["claude-sonnet-x"]
        and rec["cost_usd"] == 0.01
        and rec["model_arg"] == "sonnet"
    )
    s = H.nested_summary([rec] * 3)
    assert s["n_ok"] == 3 and s["prompt_ok"] is True
    # a failing nested call is logged with its stderr, and passed through
    p2 = subprocess.run(
        [
            H.git_bash(),
            "-c",
            f'"{(shim_dir / "claude").as_posix()}" "{fake.as_posix()}" --fail -p '
            "--system-prompt x req",
        ],
        capture_output=True,
        text=True,
        env=full_env,
    )
    assert p2.returncode == 1 and "login" in p2.stderr
    fails = [json.loads(f.read_text("utf-8")) for f in logs.glob("*.json")]
    assert any(not r["parsed_goal"] and "login" in r["stderr_head"] for r in fails)
    assert H.nested_summary(fails)["fail_kinds"] == ["auth"]


def test_answer_key_access_is_fatal_error():
    tools = [
        {
            "name": "Read",
            "full": json.dumps(
                {"file_path": "C:/x/weather-skills-demo/evals/intake/requests_sample.jsonl"}
            ),
        }
    ]
    raw = good_raw() | {"answer_key_hits": H.answer_key_hits(tools)}
    rec = H.classify(UNIT, raw)
    assert rec["outcome"] == "error" and rec["error_kind"] == "answer_key"
    assert "answer_key" in H.FATAL


def test_two_copies_of_the_plugin_loaded_is_degraded():
    init = {
        "plugins": [
            {"name": "rhiza-forecasting", "path": str(H.REPO)},
            {"name": "rhiza-forecasting", "path": "C:/cache/rhiza"},
        ]
    }
    assert H.classify(UNIT, good_raw() | {"init": init})["outcome"] == "degraded"


def test_staged_plugin_has_no_eval_tree(tmp_path):
    d = H.stage_plugin(tmp_path / "p")
    assert (d / "agents" / "human-boundary.md").exists() and not (d / "evals").exists()
    assert (d / "skills" / "goal-check" / "references" / "compile_prompt_rhiza.txt").exists()


def test_ctx_paths_are_absolute(tmp_path, monkeypatch):
    # regression: a relative --out made --plugin-dir relative to the agent's temp cwd
    monkeypatch.chdir(tmp_path)
    ctx = H.Ctx(Path("rel_run"), None, 10, sys.executable)
    assert ctx.out.is_absolute() and ctx.plugin_dir.is_absolute() and ctx.shim_dir.is_absolute()


def test_measured_single_reading_fallback_is_degraded_not_exact():
    # Regression from the 2026-10-04 preflight: the recipe was permission-denied, the agent
    # compiled once itself and returned a CORRECT goal with sampling=single. The old scorer
    # counted this as exact / no ask / silent-wrong 0.
    pos = H.CONTROLS[0]
    raw = with_card(good_raw(pos), sampling="single", samples=1, goal_check_exit=0)
    raw = raw | {"nested": [], "envelope": raw["envelope"] | {"permission_denials": [{}] * 4}}
    rec = H.classify(pos, raw)
    assert rec["outcome"] == "degraded" and rec["shadow_outcome"] == "exact"
    assert rec["permission_denials"] == 4 and rec["exact"] is None


def test_staged_plugin_path_does_not_trip_answer_key_guard(tmp_path):
    ctx = H.Ctx(tmp_path / "run", None, 10, sys.executable)
    cmd = (
        f"uv run {ctx.plugin_dir.as_posix()}/skills/goal-check/scripts/goal_check.py --goal g.json"
    )
    assert H.answer_key_hits([{"full": json.dumps({"command": cmd})}]) == []


def test_nested_from_sampler_report_in_tool_result_only():
    import json as _j

    import run_intake_eval as R

    rep = {
        "sampling": "independent",
        "compile_model": "sonnet",
        "compile_prompt_sha256_normalised": R.COMPILE_SHA,
        "compiles": [
            {
                "ok": True,
                "session_id": f"s{i}",
                "cost_usd": 0.01,
                "models": ["m"],
                "goal": {"task": "map"},
                "parsed_goal": True,
            }
            for i in range(3)
        ],
    }
    tool_result = {
        "type": "user",
        "message": {
            "role": "user",
            "content": [{"type": "tool_result", "tool_use_id": "t1", "content": _j.dumps(rep)}],
        },
    }
    # The same JSON written by the agent in its own prose must NOT count.
    prose = {
        "type": "assistant",
        "message": {"role": "assistant", "content": [{"type": "text", "text": _j.dumps(rep)}]},
    }
    only_prose = R.nested_from_sampler_reports(_j.dumps(prose))
    assert only_prose == []
    recs = R.nested_from_sampler_reports(_j.dumps(tool_result))
    s = R.nested_summary(recs)
    assert s["n_compiles"] == 3 and s["n_ok"] == 3 and s["prompt_ok"] is True
