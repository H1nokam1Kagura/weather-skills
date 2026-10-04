"""Offline tests for the reviewer-eval harness: scorer, outcome classes and run guards.

Every CLI response here is synthetic stream-json; nothing touches the network.

    uv run python -m pytest evals/reviewer/test_run_reviewer_eval.py -q
"""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
_spec = importlib.util.spec_from_file_location("run_reviewer_eval", HERE / "run_reviewer_eval.py")
ev = importlib.util.module_from_spec(_spec)
sys.modules["run_reviewer_eval"] = ev
_spec.loader.exec_module(ev)

CASES = [c for _, c in ev.load_cases(ev.CASES_DIR)]
CONTROL = next(c for c in CASES if c["id"] == ev.DEFAULT_CONTROL_CASE)


# ---------------------------------------------------------------------------
# synthetic CLI output
# ---------------------------------------------------------------------------


def card(verdict: str, extra: str = "") -> str:
    return (
        f"VERDICT: {verdict}\nREVIEWED: plan\n\nFINDINGS:\n"
        "1. step: 1\n   rule: skills/clip-region/SKILL.md: \"`--bbox` — `N/W/S/E` in decimal degrees.\"\n"
        f"   severity: BLOCKER\n   fix: swap west and east {extra}\n\nNEEDED: none\nOPEN QUESTIONS: none\n"
        "SUMMARY: done"
    )


def stream(text: str, cost=0.4, model="claude-opus-4-7", tools=(), is_error=False,
           subtype="success", agents=None, tool_results=()) -> str:
    ev_ = [{"type": "system", "subtype": "init", "model": model,
            "agents": agents if agents is not None else ["rhiza-forecasting:reviewer"]}]
    for name, inp in tools:
        ev_.append({"type": "assistant", "message": {"content": [
            {"type": "tool_use", "id": "t1", "name": name, "input": inp}]}})
    for content in tool_results:
        ev_.append({"type": "user", "message": {"content": [
            {"type": "tool_result", "tool_use_id": "t1", "content": content}]}})
    ev_.append({"type": "result", "subtype": subtype, "is_error": is_error, "result": text,
                "total_cost_usd": cost, "modelUsage": {model: {"costUSD": cost}}, "num_turns": 2})
    return "\n".join(json.dumps(e) for e in ev_)


def is_defect(prompt: str) -> bool:
    return any(c["defect"]["submission"].rstrip() in prompt for c in CASES)


def good_reviewer(cmd, prompt, timeout):
    """A well-behaved reviewer: REJECT defects, APPROVE clean twins, unique replies."""
    tag = str(abs(hash(prompt)))
    v = "REJECT" if is_defect(prompt) else "APPROVE"
    return 0, stream(card(v, tag)), "", False, 1.0


class Recorder:
    def __init__(self, fn):
        self.fn, self.calls = fn, 0

    def __call__(self, cmd, prompt, timeout):
        self.calls += 1
        return self.fn(cmd, prompt, timeout)


def run_main(tmp_path, argv, invoke, logs=None):
    logs = [] if logs is None else logs
    code = ev.main(
        ["--out", str(tmp_path / "out"), *argv],
        invoke=invoke,
        sleep=lambda s: None,
        stamp_fn=lambda a, h, c: {"agent_file_sha256": h},
        dirty_fn=lambda p: [],
        stage_fn=lambda src, root: src,
        log=logs.append,
    )
    return code, logs


# ---------------------------------------------------------------------------
# classification: absence is never success
# ---------------------------------------------------------------------------


def test_cli_exit_error_is_ERROR_not_miss():
    res = ev.classify_cli(1, "", "boom: something broke")
    assert res["error_kind"] in ("no_result", "cli_error")
    assert ev.outcome_for("defect", res.get("verdict"), res["error_kind"]) == ev.ERROR
    assert ev.outcome_for("clean", res.get("verdict"), res["error_kind"]) == ev.ERROR


def test_is_error_with_exit_zero_is_ERROR():
    res = ev.classify_cli(0, stream(card("APPROVE"), is_error=True, subtype="error_during_execution"), "")
    assert res["error_kind"] == "cli_error"


def test_unparseable_reply_is_ERROR():
    res = ev.classify_cli(0, stream("I think this looks fine overall."), "")
    assert res["error_kind"] == "unparseable"
    assert ev.outcome_for("clean", None, res["error_kind"]) == ev.ERROR


def test_empty_reply_and_non_json_are_ERROR():
    assert ev.classify_cli(0, stream(""), "")["error_kind"] == "empty_reply"
    assert ev.classify_cli(0, "not json at all", "")["error_kind"] == "no_result"


def test_ambiguous_verdict_is_ERROR_and_template_line_ignored():
    assert ev.parse_verdict("VERDICT: APPROVE | REJECT | NEEDS-INFO\n\nVERDICT: REJECT")[0] == "REJECT"
    assert ev.parse_verdict("VERDICT: APPROVE\n...\nVERDICT: REJECT") == (None, "ambiguous")
    assert ev.parse_verdict("**VERDICT:** NEEDS-INFO")[0] == "NEEDS-INFO"


def test_timeout_is_ERROR():
    assert ev.classify_cli(None, "", "", timed_out=True)["error_kind"] == "timeout"


def test_auth_error_is_fatal():
    res = ev.classify_cli(1, "", "Invalid API key · Please run /login")
    assert res["error_kind"] == "auth" and res["fatal"]


def test_401_inside_a_successful_card_is_not_auth():
    res = ev.classify_cli(0, stream(card("APPROVE", "HTTP 401 mentioned in passing")), "")
    assert res["error_kind"] is None and res["verdict"] == "APPROVE"


def test_leakage_in_transcript_is_fatal():
    out = stream(card("REJECT"), tools=[("Read", {"file_path": "C:/x/weather-skills-demo/evals/reviewer/cases/05.yaml"})])
    res = ev.classify_cli(0, out, "")
    assert res["error_kind"] == "leakage" and res["fatal"]
    out2 = stream(card("REJECT"), tool_results=["expected_verdict: REJECT"])
    assert ev.classify_cli(0, out2, "")["error_kind"] == "leakage"


def test_located_requires_findings_section():
    assert ev.located(card("REJECT"), ["west"])
    assert not ev.located("VERDICT: REJECT\nthe west and east are swapped", ["west"])


# ---------------------------------------------------------------------------
# summary: denominators, verdict, sanity flags
# ---------------------------------------------------------------------------


def _rec(case, variant, outcome, rep=1, sha=None, kind=None):
    verdict = {ev.CATCH: "REJECT", ev.FALSE_ALARM: "REJECT", ev.MISS: "APPROVE",
               ev.APPROVE_OK: "APPROVE", ev.NEEDS_INFO: "NEEDS-INFO"}.get(outcome)
    return {"case": case, "defect_class": "x", "variant": variant, "rep": rep, "outcome": outcome,
            "verdict": verdict, "located": outcome == ev.CATCH, "error_kind": kind,
            "response_sha": sha or f"{case}{variant}{rep}", "cost_usd": 0.4, "cost_known": True}


def _args(**kw):
    a = ev.build_parser().parse_args([])
    for k, v in kw.items():
        setattr(a, k, v)
    return a


def test_errors_excluded_from_denominators_and_make_run_inconclusive():
    recs = []
    for case in ("a", "b", "c"):
        for rep in (1, 2, 3):
            recs.append(_rec(case, "defect", ev.CATCH, rep))
            recs.append(_rec(case, "clean", ev.APPROVE_OK, rep))
    recs[1] = _rec("a", "clean", ev.ERROR, 1, kind="timeout")
    recs[3] = _rec("a", "clean", ev.ERROR, 2, kind="timeout")
    report, verdict, code = ev.summarize(recs, len(recs), {}, None, _args())
    assert "false-alarm rate: 0/7" in report  # 2 errored clean runs are NOT clean passes
    assert "catch rate: 9/9" in report
    assert verdict == "INCONCLUSIVE" and code == ev.EXIT_INCONCLUSIVE  # 2/18 = 11% > 10%


def test_all_reps_errored_cell_is_inconclusive_even_under_share():
    recs = [_rec(c, v, ev.CATCH if v == "defect" else ev.APPROVE_OK, r)
            for c in "abcdefghij" for v in ("defect", "clean") for r in (1, 2, 3)]
    recs = [r for r in recs if not (r["case"] == "a" and r["variant"] == "defect")]
    recs += [_rec("a", "defect", ev.ERROR, r, kind="cli_error") for r in (1, 2, 3)]  # 3/60 = 5%
    _, verdict, _ = ev.summarize(recs, len(recs), {}, None, _args())
    assert verdict == "INCONCLUSIVE"


def test_missing_planned_runs_is_inconclusive():
    recs = [_rec("a", "defect", ev.CATCH, r) for r in (1, 2, 3)] + [_rec("b", "defect", ev.MISS, r) for r in (1, 2, 3)]
    _, verdict, _ = ev.summarize(recs, 10, {}, None, _args())
    assert verdict == "INCONCLUSIVE"


def test_uniform_outcome_flag():
    recs = [_rec(c, v, ev.CATCH if v == "defect" else ev.FALSE_ALARM, r)
            for c in "abc" for v in ("defect", "clean") for r in (1, 2, 3)]
    flags = ev.sanity_flags(recs)
    assert any("UNIFORM VERDICT" in m and severe for m, severe in flags)
    _, verdict, _ = ev.summarize(recs, len(recs), {}, None, _args())
    assert verdict == "INCONCLUSIVE"


def test_identical_reply_across_cases_flag():
    recs = [_rec("a", "defect", ev.CATCH, 1, sha="same"), _rec("b", "defect", ev.CATCH, 1, sha="same"),
            _rec("a", "clean", ev.APPROVE_OK, 1), _rec("b", "clean", ev.APPROVE_OK, 1)]
    assert any("IDENTICAL REPLY" in m and severe for m, severe in ev.sanity_flags(recs))


def test_no_per_case_call_below_three_reps():
    recs = [_rec(c, v, ev.CATCH if v == "defect" else ev.APPROVE_OK, r)
            for c in "ab" for v in ("defect", "clean") for r in (1, 2)]
    report, _, _ = ev.summarize(recs, len(recs), {}, None, _args())
    assert "no call (n=2<3)" in report and "CAUGHT" not in report


# ---------------------------------------------------------------------------
# run guards through main(): resume key, cost ceiling, auth stop, retries, preflight
# ---------------------------------------------------------------------------


def test_resume_key_changes_with_agent_hash():
    k1 = ev.run_key("c", "defect", 1, "a" * 64, "c" * 64)
    k2 = ev.run_key("c", "defect", 1, "b" * 64, "c" * 64)
    assert k1 != k2
    assert ev.run_key("c", "defect", 1, "a" * 64, "d" * 64) != k1


def test_resume_skips_completed_and_forks_on_agent_change(tmp_path, monkeypatch):
    inv = Recorder(good_reviewer)
    argv = ["--case", "omitted-clip", "--n", "1"]
    code, _ = run_main(tmp_path, argv, inv)
    assert code == ev.EXIT_OK and inv.calls == 4  # 2 preflight + 2 main
    inv2 = Recorder(good_reviewer)
    code, logs = run_main(tmp_path, argv, inv2)
    assert code == ev.EXIT_OK and inv2.calls == 0  # preflight reused, runs resumed
    # A changed agent file must fork new keys: nothing reused, nothing mixed.
    real_sha = ev._sha
    monkeypatch.setattr(ev, "_sha", lambda t: real_sha((t if isinstance(t, bytes) else t.encode()) + b"v2"))
    inv3 = Recorder(good_reviewer)
    code, logs = run_main(tmp_path, argv, inv3)
    assert code == ev.EXIT_OK and inv3.calls == 4


def test_cost_ceiling_aborts_with_partial_results(tmp_path):
    # The estimate ($0.10/run x 24 = $2.40) clears the $3.50 ceiling, but each run really
    # costs $1.00: the ceiling must be enforced on MEASURED spend after every run.
    def pricey(c, p, t):
        v = "REJECT" if is_defect(p) else "APPROVE"
        return 0, stream(card(v, str(hash(p))), cost=1.0), "", False, 1.0

    inv = Recorder(pricey)
    code, logs = run_main(
        tmp_path, ["--skip-preflight", "--n", "1", "--max-cost", "3.5", "--est-cost-per-run", "0.1"], inv
    )
    assert code == ev.EXIT_ABORTED
    assert inv.calls == 4  # $4 spent >= $3.50 after run 4: stop, 20 runs never started
    summary = (tmp_path / "out" / "summary.md").read_text(encoding="utf-8")
    assert "ABORTED" in summary and "COST CEILING" in summary and "DO NOT CITE" in summary
    assert len(list((tmp_path / "out" / "runs").glob("*.json"))) == 4
    # Resume after raising the ceiling: the 4 finished runs are not paid for again.
    inv2 = Recorder(pricey)
    code, _ = run_main(tmp_path, ["--skip-preflight", "--n", "1", "--max-cost", "100", "--est-cost-per-run", "0.1"], inv2)
    assert code == ev.EXIT_OK and inv2.calls == 2 * len(CASES) - 4


def test_estimate_over_ceiling_refuses_before_spending(tmp_path):
    inv = Recorder(good_reviewer)
    code, _ = run_main(tmp_path, ["--skip-preflight", "--max-cost", "1", "--est-cost-per-run", "0.5"], inv)
    assert code == ev.EXIT_ABORTED and inv.calls == 0


def test_auth_error_stops_run(tmp_path):
    inv = Recorder(lambda c, p, t: (1, "", "OAuth token has expired. Please run /login", False, 0.5))
    code, logs = run_main(tmp_path, ["--skip-preflight", "--n", "3"], inv)
    assert code == ev.EXIT_ABORTED
    assert inv.calls == 1
    assert any("FATAL AUTH" in str(line) for line in logs)


def test_transient_errors_retry_then_succeed(tmp_path):
    seq = iter([(1, "", "API Error: 529 overloaded_error", False, 1.0)] * 2)

    def flaky(c, p, t):
        return next(seq, None) or good_reviewer(c, p, t)

    inv = Recorder(flaky)
    code, _ = run_main(tmp_path, ["--skip-preflight", "--case", "omitted-clip", "--variant", "defect", "--n", "1"], inv)
    assert inv.calls == 3
    rec = json.loads(next((tmp_path / "out" / "runs").glob("*.json")).read_text(encoding="utf-8"))
    assert rec["outcome"] == ev.CATCH and len(rec["attempts"]) == 3


def test_rate_limit_exhausted_is_fatal(tmp_path):
    inv = Recorder(lambda c, p, t: (1, "", "429 rate_limit_error", False, 1.0))
    code, _ = run_main(tmp_path, ["--skip-preflight", "--n", "1"], inv)
    assert code == ev.EXIT_ABORTED and inv.calls == 3  # 1 + 2 retries, then stop


def test_consecutive_errors_stop_the_run(tmp_path):
    inv = Recorder(lambda c, p, t: (0, stream("no card here"), "", False, 1.0))
    code, _ = run_main(tmp_path, ["--skip-preflight", "--n", "1", "--max-consecutive-errors", "3"], inv)
    assert code == ev.EXIT_ABORTED and inv.calls == 3


def test_wrong_model_means_agent_not_loaded(tmp_path):
    inv = Recorder(lambda c, p, t: (0, stream(card("REJECT", str(hash(p))), model="claude-sonnet-4-6"), "", False, 1.0))
    code, logs = run_main(tmp_path, ["--skip-preflight", "--n", "1"], inv)
    assert code == ev.EXIT_ABORTED and inv.calls == 1


def test_preflight_failure_blocks_full_run(tmp_path):
    inv = Recorder(lambda c, p, t: (0, stream(card("APPROVE", str(hash(p)))), "", False, 1.0))  # misses the defect
    code, logs = run_main(tmp_path, ["--n", "1"], inv)
    assert code == ev.EXIT_PREFLIGHT and inv.calls == 2
    pf = json.loads((tmp_path / "out" / "preflight.json").read_text(encoding="utf-8"))
    assert not pf["passed"] and any("positive control" in p for p in pf["problems"])
    assert not (tmp_path / "out" / "summary.md").exists()


def test_preflight_only_mode(tmp_path):
    inv = Recorder(good_reviewer)
    code, _ = run_main(tmp_path, ["--preflight"], inv)
    assert code == ev.EXIT_OK and inv.calls == 2


def test_dirty_tree_refused(tmp_path):
    code = ev.main(["--out", str(tmp_path / "o"), "--skip-preflight"], invoke=good_reviewer,
                   stamp_fn=lambda *a: {}, dirty_fn=lambda p: [" M agents/reviewer.md"],
                   stage_fn=lambda s, r: s, log=lambda *a: None)
    assert code == ev.EXIT_REFUSED


def test_atomic_write_leaves_no_temp(tmp_path):
    ev.atomic_write(tmp_path / "x" / "r.json", json.dumps({"a": 1}))
    assert [p.name for p in (tmp_path / "x").iterdir()] == ["r.json"]


def test_workers_produce_one_valid_file_per_run(tmp_path):
    inv = Recorder(good_reviewer)
    code, _ = run_main(tmp_path, ["--skip-preflight", "--n", "1", "--workers", "4"], inv)
    files = list((tmp_path / "out" / "runs").glob("*.json"))
    assert code == ev.EXIT_OK and len(files) == 2 * len(CASES)
    assert all(json.loads(f.read_text(encoding="utf-8"))["outcome"] for f in files)


# ---------------------------------------------------------------------------
# dry-run premise checks
# ---------------------------------------------------------------------------


def test_real_cases_pass_dry_run():
    assert ev.dry_run(ev.load_cases(ev.CASES_DIR), ev.REPO, quiet=True) == 0


def test_twin_diff_too_large_or_off_target_is_rejected():
    import copy

    big = copy.deepcopy(CONTROL)
    big["clean"]["submission"] = "\n".join(f"{i}. step-{i}" for i in range(1, 12))
    errs = ev.validate_case(Path("x.yaml"), big, ev.REPO)
    assert any("twin diff too large" in e for e in errs)

    off = copy.deepcopy(CONTROL)
    off["defect"]["expected_step"] = 9
    assert any("expected_step 9" in e for e in ev.validate_case(Path("x.yaml"), off, ev.REPO))

    noexp = copy.deepcopy(CONTROL)
    del noexp["clean"]["expected_verdict"]
    assert any("expected_verdict" in e for e in ev.validate_case(Path("x.yaml"), noexp, ev.REPO))


def test_prompt_never_contains_answer_key():
    for c in CASES:
        for v in ("defect", "clean"):
            assert not ev.LEAK_CONTENT_RE.search(ev.build_prompt(c, v, ev.REPO, True))
