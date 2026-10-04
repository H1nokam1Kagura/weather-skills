"""HITL effectiveness eval for the human-boundary agent's intake (goal compile + ask policy).

    uv run python evals/intake/run_intake_eval.py --dry-run                 # offline; no calls
    uv run python evals/intake/run_intake_eval.py --limit 1                 # ONE paid smoke unit
    uv run python evals/intake/run_intake_eval.py --reps 3                  # full: 60 units x 3
    uv run python evals/intake/run_intake_eval.py --backend replay --replay RUN/records.jsonl

Population: `requests_sample.jsonl` -- NOT SHIPPED. The first build drew it from the
clm-weather-skills H7 v2 compile benchmark, which is that research's FROZEN TEST SET; it was
removed (2026-10-04) and must not be re-derived from it. Supply a sample from a source the clm
owner clears (dev split or freshly generated requests), one JSON object per line with
`request`, `style`, `clarity` ("clear" | "underdetermined") and the truth slots.

Per unit-run outcome (an ask is never scored as exact or as silent-wrong; clm F1 rule 4):
  asked         the card carries a question for the person (status needs_answers)
  exact         not asked, and every scored core slot equals the truth
  silent_wrong  not asked, not exact, and the goal passes goal-check: a valid, runnable, WRONG
                workflow -- the key metric (clm H24 bar: <= 0.02)
  loud          not asked and the goal is invalid / the card is unparseable: fails visibly
  unnecessary   asked on a CLEAR unit (the request settles the goal; asking cost the person time)
  rule_ask      asked about a slot a deterministic rule settles for this request (premature)

Scored core slots are clm's: task, variable, region, relative window, period, legacy archive,
and obs_source only for fcst_vs_obs / timeseries (clm D1). The extension slots are read back to
the person, not scored here.
"""

from __future__ import annotations

import argparse
import datetime as dt
import importlib.util
import json
import random
import re
import shutil
import subprocess
import sys
import tempfile
import time
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

HERE = Path(__file__).resolve().parent
REPO = HERE.parents[1]
SAMPLE = HERE / "requests_sample.jsonl"
AGENT = "rhiza-forecasting:human-boundary"
STYLES = ["formal", "terse", "conversational", "jargon", "handwritten_claude"]
FIELDS = [
    "task",
    "variable",
    "region",
    "relative_time",
    "period",
    "legacy_cumulative",
    "obs_source",
]
BARS = {"silent_wrong_hi": 0.02, "ask_rate": 0.15, "exact_lo": 0.90}  # clm H24 / F1 (D47)
MIN_GOALS_FOR_VERDICT = 30  # below this the bootstrap interval is not a verdict, only a smoke
PROMPT = (
    "Opening request from a person (intake). You are running unattended: the person cannot "
    "reply in this session. Do the intake exactly as your instructions say, finish with the "
    "GOAL CARD (with any questions inside it), and stop.\n\nRequest:\n{request}"
)
ALLOWED = ["Bash(claude -p *)", "Bash(uv run *)", "Bash(cat *)", "Skill", "Read", "Write"]


def _load_goal_check():
    path = REPO / "skills" / "goal-check" / "scripts" / "goal_check.py"
    spec = importlib.util.spec_from_file_location("eval_goal_check", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


GC = _load_goal_check()


def load_units() -> list[dict]:
    if not SAMPLE.exists():
        sys.exit(
            f"{SAMPLE.name} is not shipped (see the module docstring): the clm compile benchmark "
            "is a frozen test set and must not be copied here. Supply a cleared sample first."
        )
    return [json.loads(x) for x in SAMPLE.read_text(encoding="utf-8").splitlines() if x.strip()]


# ------------------------------------------------------------------------------------ parse ---
def parse_card(text: str) -> dict | None:
    """The card's typed-goal JSON is the LAST fenced ```json block (agent contract)."""
    blocks = re.findall(r"```json\s*(\{.*?\})\s*```", text or "", re.S)
    for b in reversed(blocks):
        try:
            d = json.loads(b)
        except json.JSONDecodeError:
            continue
        if isinstance(d, dict) and ("goal" in d or "status" in d):
            return d
    return None


def _truth_view(truth: dict) -> dict:
    return {**truth, "region": GC._canon_region(truth.get("region"))}


def _goal_view(goal: dict) -> dict:
    g = GC.normalise(goal)
    return {
        "task": g["task"],
        "variable": g["variable"],
        "region": g["region"],
        "relative_time": g["time_window"] == "relative",
        "period": g["period"],
        "legacy_cumulative": bool(g["legacy_cumulative"]),
        "obs_source": g["obs_source"],
    }


def score_unit(unit: dict, card: dict | None) -> dict:
    truth = _truth_view(unit["truth"])
    scored = [f for f in FIELDS if f != "obs_source" or truth["task"] in GC.OBS_TASKS]
    rec = {
        "id": unit["id"],
        "cluster": unit["cluster"],
        "style": unit["style"],
        "clarity": unit["clarity"],
        "parsed": int(card is not None),
        "asked": 0,
        "exact": 0,
        "silent_wrong": 0,
        "loud": 0,
        "unnecessary": 0,
        "rule_ask": 0,
        "asked_slots": [],
        "wrong_fields": [],
        "sampling": None if card is None else card.get("sampling"),
    }
    if card is None:
        rec["loud"] = 1
        return rec
    # An ask = a question about the GOAL. The approval read-back every card ends with is not an
    # ask (the smoke run's first card listed it under `questions`; the contract now separates
    # `approval`, and this filter keeps older cards scoring correctly).
    qs = [
        q
        for q in card.get("questions") or []
        if not (isinstance(q, str) and "readback" in q)
        and not (isinstance(q, dict) and q.get("kind") == "plan_readback")
    ]
    asked = card.get("status") == "needs_answers" or bool(qs)
    if asked:
        rec["asked"] = 1
        rec["unnecessary"] = int(unit["clarity"] == "clear")
        slots = [q.get("slot") if isinstance(q, dict) else str(q).removeprefix("goal-") for q in qs]
        rec["asked_slots"] = slots
        rec["rule_ask"] = int(
            any(GC.rule_settles(s, unit["request"], card.get("goal")) is not None for s in slots)
        )
        return rec
    goal = card.get("goal")
    if not isinstance(goal, dict):
        rec["loud"] = 1
        return rec
    check = GC.evaluate([goal], unit["request"])
    if check["exit_code"] == 1:
        rec["loud"] = 1
        return rec
    got = _goal_view(check["goal"])
    wrong = [f for f in scored if got[f] != truth[f]]
    rec["wrong_fields"] = [f"{f}:{truth[f]}->{got[f]}" for f in wrong]
    rec["exact"] = int(not wrong)
    rec["silent_wrong"] = int(bool(wrong))
    return rec


# ---------------------------------------------------------------------------------- backends ---
def backend_agent(unit: dict, model: str | None, timeout: int, keep: Path | None) -> dict:
    work = Path(tempfile.mkdtemp(prefix="intake_"))
    cmd = [
        "claude",
        "-p",
        "--plugin-dir",
        str(REPO),
        "--agent",
        AGENT,
        "--output-format",
        "json",
        "--no-session-persistence",
        "--allowedTools",
        *ALLOWED,
    ]
    if model:
        cmd += ["--model", model]
    t0 = time.time()
    try:
        p = subprocess.run(
            cmd,
            input=PROMPT.format(request=unit["request"]),
            capture_output=True,
            text=True,
            encoding="utf-8",
            cwd=work,
            timeout=timeout,
        )
        out, err, rc = p.stdout, p.stderr, p.returncode
    except subprocess.TimeoutExpired:
        out, err, rc = "", "timeout", -1
    secs = time.time() - t0
    try:
        env = json.loads(out)
        text, cost = env.get("result", ""), env.get("total_cost_usd")
    except json.JSONDecodeError:
        text, cost = out, None
    if keep is not None:
        dst = keep / unit["id"]
        shutil.copytree(work, dst, dirs_exist_ok=True)
        (dst / "_card.md").write_text(text or "", encoding="utf-8")
        (dst / "_stderr.txt").write_text(err or "", encoding="utf-8")
    shutil.rmtree(work, ignore_errors=True)
    return {"text": text, "rc": rc, "secs": round(secs, 1), "cost_usd": cost}


def backend_synthetic(unit: dict, kind: str) -> dict:
    """Offline fakes that exercise the scorer: oracle (truth), perturb (wrong period), ask."""
    t = dict(unit["truth"])
    goal = {**t, "time_window": "relative" if t["relative_time"] else None}
    if kind == "perturb":
        goal["period"] = {"weekly": "monthly", "monthly": "weekly"}.get(goal["period"], "weekly")
        if goal["task"] == "spread_map":  # takes no period: move the area instead
            goal["period"] = None
            goal["region"] = "Senegal" if goal.get("region") != "Senegal" else "Malawi"
    card = {"card": "rhiza-goal-card/1", "status": "ready_for_approval", "goal": goal}
    if kind == "ask":
        card = {"status": "needs_answers", "goal": goal, "questions": ["goal-period"]}
    return {"text": "card\n```json\n" + json.dumps(card) + "\n```", "rc": 0, "secs": 0.0}


# ----------------------------------------------------------------------------------- report ---
def _boot(recs: list[dict], key: str, n: int = 2000, seed: int = 7) -> tuple[float, float, float]:
    """Mean and a 90% cluster-bootstrap interval (clusters = goal), fixed seed."""
    by = defaultdict(list)
    for r in recs:
        by[r["cluster"]].append(r[key])
    cl = list(by.values())
    if not cl:
        return (float("nan"),) * 3
    mean = sum(map(sum, cl)) / sum(map(len, cl))
    rng = random.Random(seed)
    stats = []
    for _ in range(n):
        s = [cl[rng.randrange(len(cl))] for _ in cl]
        stats.append(sum(map(sum, s)) / max(1, sum(map(len, s))))
    stats.sort()
    return mean, stats[int(0.05 * n)], stats[int(0.95 * n) - 1]


def _fmt(t):
    return f"{t[0]:.3f} [{t[1]:.3f}, {t[2]:.3f}]"


def report(recs: list[dict], header: str) -> str:
    L = [header, ""]
    for pop, sel in (("CLEAR (primary)", "clear"), ("ALL", None)):
        R = [r for r in recs if sel is None or r["clarity"] == sel]
        if not R:
            continue
        na = [r for r in R if not r["asked"]]
        sw, ex = _boot(R, "silent_wrong"), _boot(na, "exact") if na else (float("nan"),) * 3
        ask = sum(r["asked"] for r in R) / len(R)
        n_goals = len({r["cluster"] for r in R})
        if n_goals < MIN_GOALS_FOR_VERDICT:
            verdict = f"not decidable (n={n_goals} goals < {MIN_GOALS_FOR_VERDICT}; smoke only)"
        elif sw[2] <= BARS["silent_wrong_hi"] and ask <= BARS["ask_rate"] and ex[1] >= 0.90:
            verdict = "MEETS BARS"
        elif sw[1] > BARS["silent_wrong_hi"] or ask > BARS["ask_rate"]:
            verdict = "FAILS"
        else:
            verdict = "not decidable"
        L += [
            f"## {pop}: {len(R)} unit-runs, {len({r['cluster'] for r in R})} goals",
            "",
            f"- silent-wrong (all unit-runs) {_fmt(sw)}  (bar: upper <= {BARS['silent_wrong_hi']})",
            f"- ask rate {ask:.3f}  (bar <= {BARS['ask_rate']})",
            f"- exact among non-asked {_fmt(ex)}  (bar: lower >= {BARS['exact_lo']})",
            f"- unnecessary-ask rate {sum(r['unnecessary'] for r in R) / len(R):.3f}; "
            f"rule-decidable asks {sum(r['rule_ask'] for r in R)}; "
            f"loud {sum(r['loud'] for r in R)}; unparsed {sum(1 - r['parsed'] for r in R)}",
            f"- verdict (F1 rule): {verdict}",
            "",
            "| style | n | exact | silent-wrong | ask | unnecessary ask | loud |",
            "|---|---|---|---|---|---|---|",
        ]
        for s in STYLES:
            S = [r for r in R if r["style"] == s]
            if S:
                n = len(S)
                L.append(
                    f"| {s} | {n} | {sum(r['exact'] for r in S) / n:.3f} | "
                    f"{sum(r['silent_wrong'] for r in S) / n:.3f} | {sum(r['asked'] for r in S) / n:.3f} | "
                    f"{sum(r['unnecessary'] for r in S) / n:.3f} | {sum(r['loud'] for r in S) / n:.3f} |"
                )
        L.append("")
    reps = defaultdict(set)
    for r in recs:
        reps[r["id"]].add((r["asked"], r["exact"], r["silent_wrong"], r["loud"]))
    multi = [k for k in reps if sum(1 for r in recs if r["id"] == k) > 1]
    if multi:
        flips = sum(1 for k in multi if len(reps[k]) > 1)
        L.append(f"Repeat stability: {flips}/{len(multi)} units changed outcome across reps.")
    errs = defaultdict(int)
    for r in recs:
        for w in r["wrong_fields"]:
            errs[w] += 1
    if errs:
        top = sorted(errs.items(), key=lambda kv: -kv[1])[:8]
        L.append("Top silent errors: " + ", ".join(f"{k} x{v}" for k, v in top))
    return "\n".join(L)


# ------------------------------------------------------------------------------------- main ---
def dry_run(units: list[dict]) -> int:
    problems = []
    if len(units) < 50:
        problems.append(f"only {len(units)} units")
    if {u["style"] for u in units} != set(STYLES):
        problems.append("not every phrasing style is present")
    if not any(u["clarity"] == "underdetermined" for u in units):
        problems.append("no underdetermined (ambiguous) units")
    for u in units:
        t = dict(u["truth"])
        rep = GC.evaluate([{**t, "time_window": "relative" if t["relative_time"] else None}], None)
        if rep["exit_code"] != 0:
            problems.append(f"{u['id']}: truth goal does not pass goal-check ({rep['status']})")
    checks = {}
    for kind in ("oracle", "perturb", "ask"):
        recs = [score_unit(u, parse_card(backend_synthetic(u, kind)["text"])) for u in units]
        checks[kind] = {
            k: sum(r[k] for r in recs) / len(recs)
            for k in ("exact", "silent_wrong", "asked", "unnecessary")
        }
    if checks["oracle"]["exact"] != 1.0 or checks["oracle"]["silent_wrong"] != 0.0:
        problems.append(f"scorer: oracle backend not exact: {checks['oracle']}")
    if checks["perturb"]["silent_wrong"] != 1.0:
        problems.append(f"scorer: perturbed goals not all silent-wrong: {checks['perturb']}")
    clear = sum(u["clarity"] == "clear" for u in units) / len(units)
    if checks["ask"]["asked"] != 1.0 or abs(checks["ask"]["unnecessary"] - clear) > 1e-9:
        problems.append(f"scorer: ask backend miscounted: {checks['ask']}")
    if shutil.which("claude") is None:
        problems.append("`claude` CLI not on PATH (needed for the agent backend)")
    print(
        f"units: {len(units)}; styles: "
        + ", ".join(f"{s}={sum(u['style'] == s for u in units)}" for s in STYLES)
    )
    print(
        f"clarity: clear={sum(u['clarity'] == 'clear' for u in units)} "
        f"underdetermined={sum(u['clarity'] == 'underdetermined' for u in units)}"
    )
    print("scorer self-test (rates over all units):")
    for k, v in checks.items():
        print(f"  {k:8s} " + " ".join(f"{m}={x:.3f}" for m, x in v.items()))
    print(
        "agent call per unit: claude -p --plugin-dir <repo> --agent "
        + AGENT
        + " --output-format json ..."
    )
    print("  each unit = 1 agent session + 3 independent compile calls (+ goal-check, no model)")
    print(
        "DRY RUN: no model was called."
        + (" PROBLEMS:\n  " + "\n  ".join(problems) if problems else " OK")
    )
    return 1 if problems else 0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--dry-run", action="store_true", help="validate offline; no model calls")
    ap.add_argument(
        "--backend", choices=["agent", "replay", "oracle", "perturb", "ask"], default="agent"
    )
    ap.add_argument("--replay", help="records.jsonl from an earlier run (replay backend)")
    ap.add_argument("--reps", type=int, default=1)
    ap.add_argument("--limit", type=int, default=0, help="first N units only (smoke: 1)")
    ap.add_argument("--ids", default="", help="comma list of unit ids")
    ap.add_argument("--model", default=None)
    ap.add_argument("--timeout", type=int, default=900)
    ap.add_argument("--workers", type=int, default=1, help="parallel agent sessions")
    ap.add_argument("--out", default=None)
    a = ap.parse_args()
    units = load_units()
    if a.dry_run:
        return dry_run(units)
    if a.ids:
        want = set(a.ids.split(","))
        units = [u for u in units if u["id"] in want]
    if a.limit:
        units = units[: a.limit]
    out = Path(a.out or HERE / "runs" / dt.datetime.now().strftime("%Y%m%d-%H%M%S"))
    out.mkdir(parents=True, exist_ok=True)
    recs = []
    if a.backend == "replay":
        lines = Path(a.replay).read_text(encoding="utf-8").splitlines()
        prev = {(r["id"], r["rep"]): r for r in map(json.loads, filter(None, lines))}
        byid = {u["id"]: u for u in load_units()}
        for (uid, rep), r in prev.items():
            rec = score_unit(byid[uid], parse_card(r.get("text", "")))
            recs.append(rec | {"rep": rep, "text": r.get("text", "")})
        (out / "records.jsonl").write_text(
            "".join(json.dumps(r, ensure_ascii=False) + "\n" for r in recs), encoding="utf-8"
        )
    else:
        jobs = [(rep, u) for rep in range(a.reps) for u in units]

        def run(job):
            rep, u = job
            if a.backend == "agent":
                res = backend_agent(u, a.model, a.timeout, out / "work" / f"rep{rep}")
            else:
                res = backend_synthetic(u, a.backend)
            return score_unit(u, parse_card(res["text"])) | {"rep": rep} | res

        with ThreadPoolExecutor(max(1, a.workers)) as ex:
            for rec in ex.map(run, jobs):
                recs.append(rec)
                print(
                    f"[{rec['rep']}] {rec['id']} {rec['style']:18s} asked={rec['asked']} "
                    f"exact={rec['exact']} sw={rec['silent_wrong']} loud={rec['loud']} "
                    f"{rec.get('secs', 0)}s cost={rec.get('cost_usd')}",
                    flush=True,
                )
                with (out / "records.jsonl").open("a", encoding="utf-8") as f:
                    f.write(json.dumps(rec, ensure_ascii=False) + "\n")
    n_units = len({r["id"] for r in recs})
    n_reps = len({r["rep"] for r in recs})
    rpt = report(recs, f"# Intake HITL eval: backend {a.backend}, {n_units} units x {n_reps} reps")
    (out / "report.md").write_text(rpt, encoding="utf-8")
    print(rpt)
    return 0


if __name__ == "__main__":
    sys.exit(main())
