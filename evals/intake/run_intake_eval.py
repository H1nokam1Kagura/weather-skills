"""HITL effectiveness eval for the human-boundary agent's intake (goal compile + ask policy).

    uv run python evals/intake/run_intake_eval.py --dry-run          # offline; no model call
    uv run python evals/intake/run_intake_eval.py --preflight        # PAID: the 2 controls only
    uv run python evals/intake/run_intake_eval.py --reps 3 --workers 3 --model opus   # full run
    uv run python evals/intake/run_intake_eval.py --resume evals/intake/runs/<dir> ...  # continue
    uv run python evals/intake/run_intake_eval.py --backend replay --replay RUN/records.jsonl

A full run ALWAYS runs the two controls first (a clearly specified request that must compile
exact with no ask; an underdetermined one that must ask) and aborts if they misbehave, unless
--skip-preflight. It refuses a dirty plugin/harness tree (--allow-dirty records and proceeds),
prints a cost/time estimate and refuses when it exceeds --max-cost, checks the spend after every
unit, retries transient failures (max 2), and stops cleanly on an auth or rate-limit failure.

Population: `requests_sample.jsonl` (gitignored; regenerate with build_sample.py), drawn from the
population the clm owner CLEARED: clm runs/d4_distill/train.jsonl (D4 training requests), clarity
from the gpt-oss-120b fidelity audit. NEVER the H7 v2 compile benchmark or any other frozen
test/held-out set (see build_sample.py for the full do-not-use list).

Per unit-run outcome -- exactly one of:
  error         the harness could not get a usable card: timeout, non-zero exit, auth / rate
                limit, agent error envelope, empty or unparseable card, unknown card status, a
                ready card with no goal. NEVER scored: excluded from every rate and counted.
  degraded      a card was produced, but NOT by the measured design (3 independent compiles):
                the card says sampling != independent or samples < 3, fewer than 3 nested compile
                calls succeeded, a nested call used a different compile prompt, or the plugin
                was not loaded from this repo. NEVER scored; counted beside error.
  asked         the card carries a goal question for the person (status needs_answers)
  exact         not asked, and every scored core slot equals the truth
  silent_wrong  not asked, not exact, and the goal passes goal-check: a valid, runnable, WRONG
                workflow -- the key metric (clm H24 bar: upper 90% bound <= 0.02)
  loud          not asked, and the card is blocked or its goal fails goal-check (visible failure)
Secondary flags on asked runs: unnecessary (asked on a CLEAR unit), rule_ask (asked about a slot
a deterministic rule settles for this request).

If error+degraded exceed 10% of a population, or any phrasing-style stratum is entirely
error/degraded, or the run was aborted, or a harness defect is detected (identical cards for
different requests), the verdict is INCONCLUSIVE and the exit code is non-zero.

Scored core slots are clm's (run_h7.py FIELDS): task, variable, region, relative window, period,
legacy archive, and obs_source only for fcst_vs_obs / timeseries (clm D1). `credentials` is in
the truth but is world state, not a compile slot: neither clm nor this scorer compares it.

Populations. CLEAR (both fidelity auditors: the request conveys its goal) carries the verdict.
UNDERDETERMINED is the fidelity audit's `faithful=false`; read by hand (2026-10-04) most of those
requests state their goal completely (21 of 31 are accumulated-archive requests the auditor
flagged) and 5 CONTRADICT their own truth on obs_source (they say "gauge" / "in-situ"; the truth
says grid). So that label does NOT mean "the right behaviour is to ask". It is reported as a
diagnostic population with no verdict; an ask there is neither credited nor penalised, and where
a strong station cue contradicts the truth the scorer uses the request-implied value (the v3rr
station rule's own reading) and records the adjustment.

Exit codes: 0 completed (verdict MEETS / FAILS / smoke), 1 refused before spending (dirty tree,
estimate over cap, config/resume mismatch, missing sample), 2 INCONCLUSIVE, 3 ABORTED (auth,
rate limit, cost cap, early failure streak), 4 PREFLIGHT FAILED.
"""

from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import importlib.util
import json
import os
import platform
import random
import re
import shutil
import statistics
import subprocess
import sys
import tempfile
import threading
import time
from collections import Counter, defaultdict, deque
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait
from pathlib import Path

HERE = Path(__file__).resolve().parent
REPO = HERE.parents[1]
sys.path.insert(0, str(HERE))
import claude_shim  # noqa: E402

SAMPLE = HERE / "requests_sample.jsonl"
SAMPLE_MANIFEST = HERE / "requests_sample.manifest.json"
AGENT = "rhiza-forecasting:human-boundary"
AGENT_FILE = REPO / "agents" / "human-boundary.md"
GC_DIR = REPO / "skills" / "goal-check"
COMPILE_PROMPT = GC_DIR / "references" / "compile_prompt_rhiza.txt"
PLUGIN_DIRS = ["agents", "skills", ".claude-plugin"]
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
MAX_BAD_SHARE = 0.10  # error+degraded share above which a population is INCONCLUSIVE
EARLY_ABORT_AFTER, EARLY_ABORT_SHARE = 10, 0.25  # stop paying for a run that is mostly failures
MAX_RETRIES = 2  # transient / rate-limit retries per unit
DEFAULT_MAX_COST = 100.0
# Conservative per-unit guess used only until a preflight measures one (a unit = 1 agent session
# + >= 3 nested compiles). The preflight replaces it with 1.25 x the dearer control.
DEFAULT_EST_UNIT_COST = 1.50
DEFAULT_EST_UNIT_SECS = 150.0
PROMPT = (
    "Opening request from a person (intake). You are running unattended: the person cannot "
    "reply in this session. Do the intake exactly as your instructions say, finish with the "
    "GOAL CARD (with any questions inside it), and stop.\n\nRequest:\n{request}"
)
ALLOWED = ["Bash(claude -p *)", "Bash(uv run *)", "Bash(cat *)", "Skill", "Read", "Write"]
CARD_STATUSES = {"ready_for_approval", "needs_answers", "blocked_invalid", "blocked_packet"}
STATUS_EXIT = {"ready_for_approval": 0, "needs_answers": 3, "blocked_invalid": 1}
NOT_SCORED = ("error", "degraded")
METRICS = ("asked", "exact", "silent_wrong", "loud", "unnecessary", "rule_ask")
# Session-scoped variables of a PARENT Claude Code session: a child must not inherit them (it
# would believe it is a child/attended session of whoever launched the harness).
STRIP_ENV = (
    "CLAUDECODE",
    "CLAUDE_CODE_SESSION_ID",
    "CLAUDE_CODE_CHILD_SESSION",
    "CLAUDE_CODE_MESSAGING_SOCKET",
    "CLAUDE_CODE_MESSAGING_TOKEN",
    "CLAUDE_CODE_SESSION_ATTENDED",
    "CLAUDE_CODE_ENTRYPOINT",
    "CLAUDE_CODE_EXECPATH",
    "CLAUDE_PID",
)
# Non-secret behaviour-changing variables recorded by VALUE in the stamp (others: names only).
RECORD_ENV = ("CLAUDE_EFFORT", "CLAUDE_CODE_USE_POWERSHELL_TOOL", "CLAUDE_CODE_GIT_BASH_PATH")
EXIT_OK, EXIT_REFUSED, EXIT_INCONCLUSIVE, EXIT_ABORTED, EXIT_PREFLIGHT = 0, 1, 2, 3, 4

# Controls: written for this harness (2026-10-04), not drawn from any clm set. Truth by
# construction: POS states every slot (rainfall needs a period: "weekly totals" gives it); NEG is
# rainfall with no period, which goal-check's required-slot rule turns into a question
# whatever the readings say, so a correct agent MUST ask about `period`.
CONTROLS = [
    {
        "id": "control-pos",
        "cluster": "control-pos",
        "style": "control",
        "clarity": "clear",
        "request": "Please make a map of weekly rainfall totals for Kenya over the next month.",
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
        "expect": {"outcome": "exact"},
    },
    {
        "id": "control-neg",
        "cluster": "control-neg",
        "style": "control",
        "clarity": "underdetermined",
        "request": "Show me a rainfall map for Ethiopia.",
        "truth": {
            "task": "map",
            "variable": "precip",
            "region": "Ethiopia",
            "relative_time": False,
            "period": None,
            "credentials": False,
            "legacy_cumulative": False,
            "obs_source": "grid",
        },
        "expect": {"outcome": "asked", "asked_slot": "period"},
    },
]

_AUTH = re.compile(
    r"invalid api key|please run /login|not logged in|authentication[_ ]error|"
    r"oauth token (has )?expired|\b401\b|unauthori[sz]ed|credit balance is too low|"
    r"organization has been disabled|invalid x-api-key",
    re.I,
)
_RATE = re.compile(r"rate.?limit|\b429\b|overloaded|\b529\b|usage limit|quota exceeded", re.I)
_TRANSIENT = re.compile(
    r"api error: 5\d\d|\b50[0234]\b|econnreset|etimedout|socket hang up|fetch failed|"
    r"network error|connection error|request timed out",
    re.I,
)
RETRYABLE = ("rate_limit", "transient")
FATAL = ("auth", "rate_limit", "answer_key")


def _load_goal_check():
    path = GC_DIR / "scripts" / "goal_check.py"
    spec = importlib.util.spec_from_file_location("eval_goal_check", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


GC = _load_goal_check()
COMPILE_SHA = claude_shim.prompt_sha(COMPILE_PROMPT.read_text(encoding="utf-8"))


def sha256_bytes(b: bytes) -> str:
    return hashlib.sha256(b).hexdigest()


def sha256_file(p: Path) -> str:
    return sha256_bytes(p.read_bytes())


def load_units(path: Path = SAMPLE) -> list[dict]:
    if not path.exists():
        sys.exit(
            f"{path.name} is missing: regenerate it with build_sample.py (module docstring). "
            "The clm compile benchmark is a frozen test set and must never be copied here."
        )
    return [json.loads(x) for x in path.read_text(encoding="utf-8").splitlines() if x.strip()]


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


def effective_truth(unit: dict) -> tuple[dict, list[str]]:
    """Truth as the scorer compares it, plus any request-implied adjustment (see docstring)."""
    t = dict(unit["truth"])
    adj = []
    if (
        t.get("task") in GC.OBS_TASKS
        and t.get("obs_source") == "grid"
        and GC._STATION.search(unit["request"] or "")
    ):
        t["obs_source"] = "station"
        adj.append("obs_source:grid->station (request names stations/gauges/in-situ)")
    t["region"] = GC._canon_region(t.get("region"))
    t["relative_time"] = bool(t.get("relative_time"))
    t["legacy_cumulative"] = bool(t.get("legacy_cumulative"))
    t["obs_source"] = t.get("obs_source") or "grid"
    return t, adj


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


def _goal_questions(card: dict) -> list:
    """An ask = a question about the GOAL; the approval read-back is not an ask."""
    return [
        q
        for q in card.get("questions") or []
        if not (isinstance(q, str) and "readback" in q)
        and not (isinstance(q, dict) and q.get("kind") == "plan_readback")
    ]


def score_card(unit: dict, card: dict) -> dict:
    """Outcome of a usable, non-degraded card against the (effective) truth."""
    truth, _ = effective_truth(unit)
    scored = [f for f in FIELDS if f != "obs_source" or truth["task"] in GC.OBS_TASKS]
    r = {k: 0 for k in METRICS} | {"asked_slots": [], "wrong_fields": [], "gc_exit": None}
    qs = _goal_questions(card)
    status = card.get("status")
    if status == "needs_answers" or qs:
        slots = [q.get("slot") if isinstance(q, dict) else str(q).removeprefix("goal-") for q in qs]
        r |= {
            "outcome": "asked",
            "asked": 1,
            "unnecessary": int(unit["clarity"] == "clear"),
            "asked_slots": slots,
            "rule_ask": int(
                any(
                    GC.rule_settles(s, unit["request"], card.get("goal")) is not None for s in slots
                )
            ),
        }
        return r
    if status in ("blocked_invalid", "blocked_packet") or not isinstance(card.get("goal"), dict):
        return r | {"outcome": "loud", "loud": 1}
    check = GC.evaluate([card["goal"]], unit["request"])
    r["gc_exit"] = check["exit_code"]
    if check["exit_code"] == 1:
        return r | {"outcome": "loud", "loud": 1}
    got = _goal_view(check["goal"])
    wrong = [f for f in scored if got[f] != truth[f]]
    r["wrong_fields"] = [f"{f}:{truth[f]}->{got[f]}" for f in wrong]
    r["outcome"] = "silent_wrong" if wrong else "exact"
    r["silent_wrong"], r["exact"] = int(bool(wrong)), int(not wrong)
    return r


def classify_failure(text: str) -> str:
    text = text or ""
    if _AUTH.search(text):
        return "auth"
    if _RATE.search(text):
        return "rate_limit"
    if _TRANSIENT.search(text):
        return "transient"
    return "other"


def nested_summary(nested: list[dict] | None) -> dict:
    nested = nested or []
    compiles = [n for n in nested if n.get("system_prompt_sha256")]
    ok = [
        n for n in compiles if n.get("rc") == 0 and n.get("parsed_goal") and not n.get("is_error")
    ]
    failed = [n for n in compiles if n not in ok]
    costs = [n.get("cost_usd") for n in nested]
    return {
        "n_calls": len(nested),
        "n_compiles": len(compiles),
        "n_ok": len(ok),
        "n_failed": len(failed),
        "prompt_ok": (
            all(n["system_prompt_sha256"] == COMPILE_SHA for n in compiles) if compiles else None
        ),
        "models": sorted({m for n in nested for m in n.get("models") or []}),
        "model_args": sorted({str(n.get("model_arg")) for n in nested}),
        "cost": round(sum(c for c in costs if c is not None), 6),
        "cost_unknown": sum(1 for c in costs if c is None),
        "fail_kinds": [
            classify_failure((n.get("stderr_head") or "") + " " + (n.get("stdout_head") or ""))
            for n in failed
        ],
        "ok_goals": [n["goal"] for n in ok],
    }


def _plugin_from_repo(init: dict | None, expected: str | None = None) -> bool | None:
    """True if the session loaded our plugin from `expected` (the staged copy); None if the
    init message does not say. A second copy (e.g. the installed cache) loaded alongside counts
    as False: which one supplied the agent is then unknowable."""
    plugins = (init or {}).get("plugins")
    if not plugins:
        return None
    want = os.path.normcase(str(Path(expected or REPO).resolve()))
    ours = [p for p in plugins if isinstance(p, dict) and p.get("name") == AGENT.split(":")[0]]
    if len(ours) != 1:
        return False
    try:
        return os.path.normcase(str(Path(ours[0].get("path", "")).resolve())) == want
    except OSError:
        return False


def classify(unit: dict, raw: dict) -> dict:
    """One unit-run -> exactly one outcome; error / degraded are never scored (all metrics None)."""
    truth, adj = effective_truth(unit)
    text = raw.get("text") or ""
    env = raw.get("envelope")
    init = raw.get("init")
    nest = nested_summary(raw.get("nested"))
    agent_cost = (env or {}).get("total_cost_usd")
    rec = (
        {
            "id": unit["id"],
            "cluster": unit["cluster"],
            "style": unit["style"],
            "clarity": unit["clarity"],
            "truth_adjusted": adj,
            "request_sha": sha256_bytes((unit["request"] or "").encode("utf-8"))[:16],
            "truth_sha": sha256_bytes(json.dumps(unit["truth"], sort_keys=True).encode())[:16],
            "outcome": None,
            "error_kind": None,
            "errors": [],
            "degraded_reasons": [],
            "secs": raw.get("secs"),
            "rc": raw.get("rc"),
            "agent_models": sorted(((env or {}).get("modelUsage") or {}).keys())
            or ([init["model"]] if (init or {}).get("model") else []),
            "nested": {k: v for k, v in nest.items() if k != "ok_goals"},
            "cost_agent": agent_cost,
            "cost_total": (None if agent_cost is None else round(agent_cost + nest["cost"], 6)),
            "cost_unknown": agent_cost is None or nest["cost_unknown"] > 0,
            "permission_denials": len((env or {}).get("permission_denials") or []),
            "card_sha": sha256_bytes(text.strip().encode("utf-8"))[:16] if text.strip() else None,
            "card_status": None,
            "sampling": None,
            "goal_sha": None,
            "recheck_exit": None,
            "card_consistent": None,
        }
        | {k: None for k in METRICS}
        | {"asked_slots": [], "wrong_fields": []}
    )

    errs = []
    if raw.get("spawn_error"):
        errs.append(f"spawn_error:{raw['spawn_error']}")
    if raw.get("timed_out"):
        errs.append("timeout")
    elif env is None and not raw.get("spawn_error"):
        errs.append("no_result_envelope")
    elif env is not None and (env.get("is_error") or env.get("subtype") not in (None, "success")):
        errs.append(f"agent_error:{env.get('subtype')}:{str(env.get('result'))[:160]}")
    if raw.get("rc") not in (0, None) and not raw.get("timed_out"):
        errs.append(f"rc={raw.get('rc')}")
    card = parse_card(text) if text.strip() else None
    if not errs:
        if not text.strip():
            errs.append("empty_result")
        elif card is None:
            errs.append("unparseable_card")
        elif card.get("status") not in CARD_STATUSES:
            errs.append(f"unknown_card_status:{card.get('status')!r}")
        elif card.get("status") == "ready_for_approval" and not isinstance(card.get("goal"), dict):
            errs.append("ready_card_without_goal")
    nested_fatal = [k for k in nest["fail_kinds"] if k in FATAL]
    if nested_fatal:
        errs.append(f"nested_{nested_fatal[0]}")
    if raw.get("answer_key_hits"):
        nested_fatal = ["answer_key"]
        errs.append(f"answer_key_access:{raw['answer_key_hits'][:2]}")
    if errs:
        blob = " ".join(errs) + " " + (raw.get("stderr") or "")[-2000:] + " " + text[-1000:]
        kind = nested_fatal[0] if nested_fatal else classify_failure(blob)
        if "timeout" in errs and kind == "other":
            kind = "timeout"
        return rec | {"outcome": "error", "errors": errs, "error_kind": kind}

    rec["card_status"] = card.get("status")
    rec["sampling"] = card.get("sampling")
    if isinstance(card.get("goal"), dict):
        rec["goal_sha"] = sha256_bytes(json.dumps(card["goal"], sort_keys=True).encode())[:16]
    deg = []
    if card.get("sampling") != "independent":
        deg.append(f"card_sampling={card.get('sampling')!r}")
    if isinstance(card.get("samples"), int) and card["samples"] < 3:
        deg.append(f"card_samples={card['samples']}")
    if raw.get("nested_observed", True):
        if card.get("sampling") == "independent" and nest["n_calls"] == 0:
            deg.append("card_claims_independent_but_no_nested_call_observed")
        if nest["n_ok"] < 3:
            deg.append(f"nested_ok_compiles={nest['n_ok']}")
        if nest["prompt_ok"] is False:
            deg.append("nested_compile_prompt_mismatch")
    if _plugin_from_repo(init, raw.get("plugin_dir")) is False:
        deg.append("plugin_not_loaded_from_the_staged_repo_copy")

    # Independent recompute of what goal-check says on the observed compiles (diagnostic).
    if nest["ok_goals"]:
        rc_ = GC.evaluate(nest["ok_goals"][-3:], unit["request"])
        rec["recheck_exit"] = rc_["exit_code"]
        want = STATUS_EXIT.get(card.get("status"))
        rec["card_consistent"] = None if want is None else (want == rc_["exit_code"])

    sc = score_card(unit, card)
    if deg:
        return rec | {
            "outcome": "degraded",
            "degraded_reasons": deg,
            "shadow_outcome": sc["outcome"],
        }
    return rec | sc


# ---------------------------------------------------------------------------------- backends ---
def stage_plugin(dst: Path) -> Path:
    """Copy ONLY the plugin (agents, skills, manifest) out of the repo: the sample that holds the
    truth lives under evals/, and --plugin-dir <repo> would put it next to the agent."""
    if dst.exists():
        shutil.rmtree(dst)
    for d in PLUGIN_DIRS:
        if (REPO / d).exists():
            shutil.copytree(
                REPO / d, dst / d, ignore=shutil.ignore_patterns("__pycache__", "*.pyc")
            )
    return dst


class Ctx:
    """Everything a backend needs that is fixed for the run."""

    def __init__(
        self,
        out: Path,
        model: str | None,
        timeout: int,
        real_claude: str | None,
        isolate: bool = True,
        unit_budget: float | None = None,
    ):
        self.out = out = Path(out).resolve()
        self.model = model
        self.timeout = timeout
        self.real_claude = real_claude
        self.isolate = isolate
        self.unit_budget = unit_budget
        self.shim_dir = claude_shim.install(out / "_shim") if real_claude else None
        self.plugin_dir = (
            stage_plugin(out / "_plugin" / "rhiza-forecasting") if real_claude else None
        )
        self.sleep = time.sleep


def agent_env(ctx: Ctx, nested_dir: Path) -> dict:
    env = {k: v for k, v in os.environ.items() if k not in STRIP_ENV}
    env["PATH"] = str(ctx.shim_dir) + os.pathsep + env.get("PATH", "")
    env["INTAKE_REAL_CLAUDE"] = ctx.real_claude
    env["INTAKE_NESTED_DIR"] = str(nested_dir)
    return env


_ANSWER_KEY = re.compile(r"evals[/\\]+|requests_sample|weather-skills-demo[/\\]+evals", re.I)


def answer_key_hits(tools: list[dict]) -> list[str]:
    """Tool calls that touch the eval tree (where the truth lives): fatal for the run."""
    return [t["full"][:200] for t in tools if _ANSWER_KEY.search(t.get("full", ""))]


def parse_stream(out: str) -> tuple[dict | None, dict | None, list[dict]]:
    init, result, tools = None, None, []
    for line in (out or "").splitlines():
        line = line.strip()
        if not line.startswith("{"):
            continue
        try:
            m = json.loads(line)
        except json.JSONDecodeError:
            continue
        if m.get("type") == "system" and m.get("subtype") == "init":
            init = m
        elif m.get("type") == "result":
            result = m
        elif m.get("type") == "assistant":
            for c in (m.get("message") or {}).get("content") or []:
                if isinstance(c, dict) and c.get("type") == "tool_use":
                    inp = c.get("input") or {}
                    tools.append(
                        {
                            "name": c.get("name"),
                            "cmd": str(inp.get("command", inp))[:300],
                            "full": json.dumps(inp, ensure_ascii=False),
                        }
                    )
    return init, result, tools


def backend_agent(unit: dict, ctx: Ctx, tag: str) -> dict:
    work = Path(tempfile.mkdtemp(prefix="intake_"))
    keep = ctx.out / "work" / tag
    nested_dir = keep / "nested"
    if nested_dir.exists():
        shutil.rmtree(nested_dir)
    nested_dir.mkdir(parents=True)
    cmd = [
        ctx.real_claude,
        "-p",
        "--plugin-dir",
        str(ctx.plugin_dir),
        "--agent",
        AGENT,
        "--output-format",
        "stream-json",
        "--verbose",
        "--no-session-persistence",
    ]
    if ctx.isolate:  # no user MCP servers, hooks or settings: the run depends only on the stamp
        cmd += ["--strict-mcp-config", "--setting-sources", ""]
    if ctx.unit_budget:
        cmd += ["--max-budget-usd", f"{ctx.unit_budget:.2f}"]
    if ctx.model:
        cmd += ["--model", ctx.model]
    cmd += ["--allowedTools", *ALLOWED]
    t0 = time.time()
    raw = {
        "rc": None,
        "timed_out": False,
        "spawn_error": None,
        "nested_observed": True,
        "plugin_dir": str(ctx.plugin_dir),
    }
    out = err = ""
    try:
        p = subprocess.run(
            cmd,
            input=PROMPT.format(request=unit["request"]),
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            cwd=work,
            timeout=ctx.timeout,
            env=agent_env(ctx, nested_dir),
        )
        out, err, raw["rc"] = p.stdout, p.stderr, p.returncode
    except subprocess.TimeoutExpired as exc:
        raw["timed_out"] = True
        out = (
            exc.stdout.decode("utf-8", "replace")
            if isinstance(exc.stdout, bytes)
            else (exc.stdout or "")
        )
        err = "timeout"
    except OSError as exc:
        raw["spawn_error"] = str(exc)
    raw["secs"] = round(time.time() - t0, 1)
    init, result, tools = parse_stream(out)
    raw["init"] = (
        None
        if init is None
        else {
            k: init.get(k)
            for k in ("model", "plugins", "claude_code_version", "apiKeySource", "permissionMode")
        }
    )
    raw["envelope"] = None if result is None else {k: v for k, v in result.items() if k != "usage"}
    raw["text"] = (result or {}).get("result") or ""
    raw["stderr"] = (err or "")[-4000:]
    raw["answer_key_hits"] = answer_key_hits(tools)
    raw["tool_uses"] = [{k: v for k, v in t.items() if k != "full"} for t in tools]
    raw["nested"] = []
    for f in sorted(nested_dir.glob("*.json")):
        try:
            raw["nested"].append(json.loads(f.read_text(encoding="utf-8")))
        except (OSError, json.JSONDecodeError) as exc:
            raw["nested"].append(
                {
                    "rc": -1,
                    "parsed_goal": False,
                    "stderr_head": f"log: {exc}",
                    "system_prompt_sha256": "unreadable-log",
                }
            )
    shutil.copytree(work, keep, dirs_exist_ok=True)
    (keep / "_transcript.jsonl").write_text(out or "", encoding="utf-8")
    (keep / "_stderr.txt").write_text(err or "", encoding="utf-8")
    (keep / "_card.md").write_text(raw["text"], encoding="utf-8")
    shutil.rmtree(work, ignore_errors=True)
    return raw


def _fake_nested(goal: dict, n: int = 3) -> list[dict]:
    return [
        {
            "rc": 0,
            "parsed_goal": True,
            "goal": goal,
            "system_prompt_sha256": COMPILE_SHA,
            "is_error": False,
            "models": ["synthetic"],
            "cost_usd": 0.0,
            "model_arg": "sonnet",
        }
        for _ in range(n)
    ]


def backend_synthetic(unit: dict, kind: str) -> dict:
    """Offline fakes that exercise the scorer: oracle (truth), perturb (wrong period), ask."""
    t, _ = effective_truth(unit)
    goal = {k: t.get(k) for k in GC.CORE if k != "time_window"}
    goal["time_window"] = "relative" if t["relative_time"] else None
    if kind == "perturb":
        goal["period"] = {"weekly": "monthly", "monthly": "weekly"}.get(goal["period"], "weekly")
        if goal["task"] == "spread_map":  # takes no period: move the area instead
            goal["period"] = None
            goal["region"] = "Senegal" if goal.get("region") != "Senegal" else "Malawi"
    card = {
        "card": "rhiza-goal-card/1",
        "status": "ready_for_approval",
        "sampling": "independent",
        "samples": 3,
        "goal": goal,
        "questions": [],
    }
    if kind == "ask":
        card |= {"status": "needs_answers", "questions": ["goal-period"]}
    text = (
        f"## GOAL CARD\n(synthetic {kind}) You asked: {unit['request']}\n```json\n"
        + json.dumps(card)
        + "\n```"
    )
    return {
        "text": text,
        "rc": 0,
        "secs": 0.0,
        "timed_out": False,
        "nested_observed": True,
        "envelope": {
            "subtype": "success",
            "is_error": False,
            "result": text,
            "total_cost_usd": 0.0,
            "modelUsage": {"synthetic": {}},
        },
        "init": None,
        "nested": _fake_nested(goal),
    }


# ------------------------------------------------------------------------------------ stamp ---
def git(*a: str) -> str:
    p = subprocess.run(["git", "-C", str(REPO), *a], capture_output=True, text=True)
    # rstrip only: porcelain lines start with a status column that may be a space.
    return p.stdout.rstrip() if p.returncode == 0 else f"<git error: {p.stderr.strip()[:200]}>"


def dirty_paths() -> tuple[list[str], list[str]]:
    """(relevant, other): relevant = anything the plugin or this harness reads."""
    rel, other = [], []
    for line in git("status", "--porcelain", "--untracked-files=all").splitlines():
        path = line[3:].strip().strip('"')
        if path.startswith("evals/") and not path.startswith("evals/intake/"):
            other.append(line)
        else:
            rel.append(line)
    return rel, other


def git_bash() -> str | None:
    """The bash Claude Code's Bash tool uses on Windows. NOT `shutil.which("bash")`, which on
    Windows resolves to System32's WSL launcher (measured 2026-10-04: execvpe /bin/bash fails)."""
    cand = os.environ.get("CLAUDE_CODE_GIT_BASH_PATH")
    if cand and Path(cand).exists():
        return cand
    if os.name != "nt":
        return shutil.which("bash")
    g = shutil.which("git")
    cands = [Path(g).resolve().parents[1] / "bin" / "bash.exe"] if g else []
    cands.append(Path(r"C:\Program Files\Git\bin\bash.exe"))
    return next((str(c) for c in cands if c.exists()), None)


def plugin_content_sha() -> str:
    h = hashlib.sha256()
    for d in PLUGIN_DIRS:
        base = REPO / d
        if not base.exists():
            continue
        for p in sorted(base.rglob("*")):
            if p.is_file() and "__pycache__" not in p.parts and p.suffix != ".pyc":
                h.update(p.relative_to(REPO).as_posix().encode() + b"\0")
                h.update(p.read_bytes().replace(b"\r\n", b"\n") + b"\0")
    return h.hexdigest()


def claude_version(real: str | None) -> str:
    if not real:
        return "<claude not found>"
    try:
        return subprocess.run(
            [real, "--version"], capture_output=True, text=True, timeout=60
        ).stdout.strip()
    except (OSError, subprocess.TimeoutExpired) as exc:
        return f"<error: {exc}>"


def build_stamp(args: argparse.Namespace, real: str | None, sample: Path = SAMPLE) -> dict:
    rel, other = dirty_paths()
    sample_sha = sha256_file(sample) if sample.exists() else None
    src = (
        json.loads(SAMPLE_MANIFEST.read_text(encoding="utf-8"))
        if SAMPLE_MANIFEST.exists()
        else None
    )
    stamp = {
        "started_utc": dt.datetime.now(dt.UTC).isoformat(timespec="seconds"),
        "git_sha": git("rev-parse", "HEAD"),
        "git_branch": git("rev-parse", "--abbrev-ref", "HEAD"),
        "git_dirty_relevant": rel,
        "git_dirty_other": other,
        "harness_sha256": sha256_file(Path(__file__)),
        "agent": AGENT,
        "agent_file_sha256": sha256_file(AGENT_FILE),
        "compile_prompt_sha256_normalised": COMPILE_SHA,
        "goal_check_py_sha256": sha256_file(GC_DIR / "scripts" / "goal_check.py"),
        "goal_check_skill_sha256": sha256_file(GC_DIR / "SKILL.md"),
        "plugin_content_sha256": plugin_content_sha(),
        "prompt_template_sha256": sha256_bytes(PROMPT.encode()),
        "allowed_tools": ALLOWED,
        "model_arg": getattr(args, "model", None),
        "sample_file": str(sample),
        "sample_sha256": sample_sha,
        "sample_source": src,
        "sample_matches_its_manifest": (src or {}).get("sample_sha256") == sample_sha
        if src
        else None,
        "claude_cli": real,
        "claude_version": claude_version(real),
        "python": sys.version.split()[0],
        "platform": platform.platform(),
        "env_recorded": {k: os.environ.get(k) for k in RECORD_ENV},
        "env_stripped_for_child": [k for k in STRIP_ENV if k in os.environ],
        "args": {k: v for k, v in vars(args).items()},
        "isolated_session": not getattr(args, "no_isolate", False),
    }
    key_src = {
        k: stamp[k]
        for k in (
            "agent_file_sha256",
            "compile_prompt_sha256_normalised",
            "goal_check_py_sha256",
            "plugin_content_sha256",
            "prompt_template_sha256",
            "allowed_tools",
            "model_arg",
            "claude_version",
            "sample_sha256",
            "isolated_session",
        )
    }
    stamp["run_key"] = sha256_bytes(json.dumps(key_src, sort_keys=True).encode())[:16]
    return stamp


# ----------------------------------------------------------------------------------- report ---
def _boot(recs: list[dict], key: str, n: int = 2000, seed: int = 7) -> tuple[float, float, float]:
    """Mean and a 90% cluster-bootstrap interval (clusters = goal), fixed seed."""
    by = defaultdict(list)
    for r in recs:
        if r[key] is None:
            raise ValueError(f"unscored record {r['id']} reached a metric ({key}): harness bug")
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


def harness_defects(recs: list[dict]) -> tuple[list[str], list[str]]:
    """(defects -> INCONCLUSIVE, flags -> reported). Harness-defect-first."""
    defects, flags = [], []
    by_card = defaultdict(list)
    for r in recs:  # error texts (e.g. the same API error) repeat legitimately: cards only
        if r.get("card_sha") and r["outcome"] != "error":
            by_card[r["card_sha"]].append(r)
    dup_truth = {k: v for k, v in by_card.items() if len({r.get("truth_sha") for r in v}) > 1}
    dup_req = {
        k: v
        for k, v in by_card.items()
        if k not in dup_truth and len({r.get("request_sha") for r in v}) > 1
    }
    if dup_truth:
        ids = sorted({r["id"] for r in next(iter(dup_truth.values()))})[:4]
        defects.append(
            f"identical card text for requests with DIFFERENT truths ({len(dup_truth)} card "
            f"hash(es), e.g. {ids}): the output does not depend on the input "
            "(cached / replayed / constant)"
        )
    if dup_req:
        flags.append(
            f"{len(dup_req)} card text(s) repeated verbatim across different requests with the "
            "same truth (plausible for terse requests; read a few)"
        )
    by_goal = defaultdict(set)
    for r in recs:
        if r.get("goal_sha"):
            by_goal[r["goal_sha"]].add(r.get("truth_sha"))
    shared = [v for v in by_goal.values() if len(v) > 1]
    if shared:
        flags.append(f"{len(shared)} typed goal(s) shared by units with DIFFERENT truths")
    for pop in ("clear", "underdetermined"):
        S = [r for r in recs if r["clarity"] == pop and r["outcome"] not in NOT_SCORED]
        if len(S) >= 10 and len({r["outcome"] for r in S}) == 1:
            flags.append(
                f"uniform outcome '{S[0]['outcome']}' over all {len(S)} scored {pop} unit-runs: "
                "check the harness before believing it"
            )
    return defects, flags


def _pop_section(name: str, R_all: list[dict], primary: bool) -> tuple[list[str], str, list[str]]:
    L, inconclusive = [], []
    bad = [r for r in R_all if r["outcome"] in NOT_SCORED]
    R = [r for r in R_all if r["outcome"] not in NOT_SCORED]
    share = len(bad) / len(R_all)
    if share > MAX_BAD_SHARE:
        inconclusive.append(
            f"{name}: error+degraded {len(bad)}/{len(R_all)} = {share:.1%} > {MAX_BAD_SHARE:.0%}"
        )
    for s in sorted({r["style"] for r in R_all}):
        S = [r for r in R_all if r["style"] == s]
        if all(r["outcome"] in NOT_SCORED for r in S):
            inconclusive.append(f"{name}: stratum style={s} is entirely error/degraded ({len(S)})")
    n_goals = len({r["cluster"] for r in R})
    L += [
        f"## {name}: {len(R_all)} unit-runs ({len(R)} scored, {len(bad)} error/degraded), "
        f"{n_goals} scored goals",
        "",
    ]
    if R:
        sw, ask = _boot(R, "silent_wrong"), _boot(R, "asked")
        na = [r for r in R if not r["asked"]]
        ex = _boot(na, "exact") if na else (float("nan"),) * 3
        rule_asks = sum(r["rule_ask"] for r in R)
        L += [
            f"- silent-wrong (scored unit-runs) {_fmt(sw)}  (bar: upper <= {BARS['silent_wrong_hi']})",
            f"- ask rate {_fmt(ask)}  (bar: point <= {BARS['ask_rate']})",
            f"- exact among non-asked {_fmt(ex)}  (bar: lower >= {BARS['exact_lo']})",
            f"- unnecessary-ask rate {sum(r['unnecessary'] for r in R) / len(R):.3f}; rule-decidable "
            f"asks {rule_asks} (bar 0); loud {sum(r['loud'] for r in R)}",
        ]
    if not primary:
        verdict = "no verdict (diagnostic population; see the definition in the module docstring)"
    elif inconclusive:
        verdict = "INCONCLUSIVE"
    elif n_goals < MIN_GOALS_FOR_VERDICT:
        verdict = f"not decidable (n={n_goals} scored goals < {MIN_GOALS_FOR_VERDICT}; smoke only)"
    elif (
        sw[2] <= BARS["silent_wrong_hi"]
        and ask[0] <= BARS["ask_rate"]
        and ex[1] >= BARS["exact_lo"]
        and rule_asks == 0
    ):
        verdict = "MEETS BARS"
    elif sw[1] > BARS["silent_wrong_hi"] or ask[0] > BARS["ask_rate"] or rule_asks > 0:
        verdict = "FAILS"
    else:
        verdict = "not decidable (intervals straddle the bars)"
    L += [
        f"- verdict (F1 rule): {verdict}",
        "",
        "| style | n | error | degraded | asked | exact | silent-wrong | loud | unnecessary |",
        "|---|---|---|---|---|---|---|---|---|",
    ]
    for s in STYLES + sorted({r["style"] for r in R_all} - set(STYLES)):
        S = [r for r in R_all if r["style"] == s]
        if S:
            c = Counter(r["outcome"] for r in S)
            L.append(
                f"| {s} | {len(S)} | {c['error']} | {c['degraded']} | {c['asked']} | "
                f"{c['exact']} | {c['silent_wrong']} | {c['loud']} | "
                f"{sum(r['unnecessary'] or 0 for r in S)} |"
            )
    adj = [r for r in R_all if r.get("truth_adjusted")]
    if adj:
        c = Counter(r["outcome"] for r in adj)
        L.append(
            f"\nTruth adjusted to the request (station cue vs grid truth): {len(adj)} unit-runs, "
            f"outcomes {dict(c)}"
        )
    L.append("")
    return L, verdict, inconclusive


def report(
    recs: list[dict], header: str, aborted: str | None = None, stamp: dict | None = None
) -> tuple[str, str, int]:
    """Markdown report, headline verdict, exit code."""
    L = [header, ""]
    if stamp:
        L += [
            f"- git {stamp['git_sha'][:12]} ({stamp['git_branch']}); dirty relevant: "
            f"{len(stamp['git_dirty_relevant'])}; run_key {stamp['run_key']}",
            f"- agent {stamp['agent_file_sha256'][:12]}; compile prompt {stamp['compile_prompt_sha256_normalised'][:12]}; "
            f"goal_check {stamp['goal_check_py_sha256'][:12]}; sample {str(stamp['sample_sha256'])[:12]}; "
            f"CLI {stamp['claude_version']}; --model {stamp['model_arg']}",
            "",
        ]
    inconclusive = []
    if aborted:
        inconclusive.append(f"run ABORTED: {aborted}")
    if not recs:
        inconclusive.append("no unit-runs completed")
    defects, flags = harness_defects(recs)
    inconclusive += [f"harness defect: {d}" for d in defects]
    headline = "INCONCLUSIVE"
    for pop, primary in (("clear", True), ("underdetermined", False)):
        R_all = [r for r in recs if r["clarity"] == pop]
        if not R_all:
            if primary:
                inconclusive.append("no CLEAR unit-runs")
            continue
        sec, verdict, inc = _pop_section(
            pop.upper() + (" (primary)" if primary else ""), R_all, primary
        )
        L += sec
        inconclusive += inc
        if primary:
            headline = verdict
    if recs:
        bad_all = sum(r["outcome"] in NOT_SCORED for r in recs) / len(recs)
        if bad_all > MAX_BAD_SHARE:
            inconclusive.append(f"ALL: error+degraded {bad_all:.1%} > {MAX_BAD_SHARE:.0%}")
    if inconclusive:
        headline = "INCONCLUSIVE"
    ek = Counter(r["error_kind"] for r in recs if r["outcome"] == "error")
    dg = Counter(
        d.split("=")[0] for r in recs if r["outcome"] == "degraded" for d in r["degraded_reasons"]
    )
    L += [f"Errors by kind: {dict(ek) or 'none'}", f"Degraded by reason: {dict(dg) or 'none'}"]
    shadow = Counter(r.get("shadow_outcome") for r in recs if r["outcome"] == "degraded")
    if shadow:
        L.append(f"(degraded cards, NOT scored, would have been: {dict(shadow)})")
    incons = [r["id"] for r in recs if r.get("card_consistent") is False]
    ready_needs = [r["id"] for r in recs if r.get("gc_exit") == 3]
    L += [
        f"Card vs independent recheck of the observed compiles: {len(incons)} inconsistent {incons[:6]}",
        f"Ready cards whose own goal still needs an answer (goal-check exit 3): {len(ready_needs)} {ready_needs[:6]}",
        f"Permission denials inside agent sessions: {sum(r.get('permission_denials') or 0 for r in recs)}",
        f"Nested compile failures (redrawn or not): {sum((r.get('nested') or {}).get('n_failed', 0) for r in recs)}",
    ]
    scored = [r for r in recs if r["outcome"] not in NOT_SCORED]
    reps = defaultdict(set)
    for r in scored:
        reps[r["id"]].add(r["outcome"])
    multi = [k for k, v in Counter(r["id"] for r in scored).items() if v > 1]
    if multi:
        L.append(
            f"Repeat stability: {sum(1 for k in multi if len(reps[k]) > 1)}/{len(multi)} units changed outcome across reps."
        )
    errs = Counter(w for r in scored for w in r["wrong_fields"])
    if errs:
        L.append("Top silent errors: " + ", ".join(f"{k} x{v}" for k, v in errs.most_common(8)))
    costs = [r["cost_total"] for r in recs if r.get("cost_total") is not None]
    secs = [r["secs"] for r in recs if r.get("secs")]
    am = Counter(m for r in recs for m in r.get("agent_models") or [])
    nm = Counter(m for r in recs for m in (r.get("nested") or {}).get("models") or [])
    L += [
        "",
        "## Cost / time / models",
        f"- total ${sum(costs):.2f} over {len(costs)} costed unit-runs; per unit mean "
        f"${(statistics.mean(costs) if costs else float('nan')):.3f}, median "
        f"${(statistics.median(costs) if costs else float('nan')):.3f}, max ${max(costs, default=float('nan')):.3f}; "
        f"cost unknown on {sum(1 for r in recs if r.get('cost_unknown'))}",
        f"- seconds per unit: median {(statistics.median(secs) if secs else float('nan')):.0f}, max {max(secs, default=0):.0f}",
        f"- agent-session models: {dict(am)}; nested compile models: {dict(nm)}",
    ]
    if flags:
        L += ["", "## Harness flags (read before believing the numbers)"] + [
            f"- {f}" for f in flags
        ]
    if inconclusive:
        L += ["", "## INCONCLUSIVE because"] + [f"- {x}" for x in inconclusive]
    L += ["", f"# HEADLINE: {headline}"]
    code = (
        EXIT_ABORTED if aborted else (EXIT_INCONCLUSIVE if headline == "INCONCLUSIVE" else EXIT_OK)
    )
    return "\n".join(L), headline, code


# ---------------------------------------------------------------------------------- running ---
class Guard:
    """Spend / failure guard, consulted before every launch and after every unit."""

    def __init__(self, max_cost: float, est_unit: float):
        self.max_cost, self.est_unit = max_cost, est_unit
        self.spent, self.done, self.bad = 0.0, 0, 0
        self.stop_reason: str | None = None
        self.lock = threading.Lock()

    def can_launch(self, inflight: int) -> bool:
        with self.lock:
            if self.stop_reason:
                return False
            if self.spent + (inflight + 1) * self.est_unit > self.max_cost:
                self.stop_reason = (
                    f"cost cap: spent ${self.spent:.2f} + {inflight + 1} x est "
                    f"${self.est_unit:.2f} would exceed --max-cost ${self.max_cost:.2f}"
                )
                return False
            return True

    def record(self, rec: dict) -> None:
        with self.lock:
            c = rec.get("cost_spent")
            self.spent += c if c is not None else self.est_unit  # unknown cost: charge the estimate
            self.done += 1
            self.bad += rec["outcome"] in NOT_SCORED
            if rec.get("error_kind") in FATAL:
                self.stop_reason = self.stop_reason or (
                    f"{rec['error_kind']} failure on {rec['id']}: {rec['errors'][:2]}"
                )
            if self.spent > self.max_cost:
                self.stop_reason = (
                    self.stop_reason or f"cost cap: spent ${self.spent:.2f} > ${self.max_cost:.2f}"
                )
            if self.done >= EARLY_ABORT_AFTER and self.bad / self.done > EARLY_ABORT_SHARE:
                self.stop_reason = self.stop_reason or (
                    f"failure streak: {self.bad}/{self.done} unit-runs error/degraded "
                    f"(> {EARLY_ABORT_SHARE:.0%} after {EARLY_ABORT_AFTER})"
                )


def run_one(unit: dict, rep: int, backend, ctx: Ctx | None) -> dict:
    """One unit-run with transient retries; cost_spent sums every attempt (for the guard)."""
    spent, unknown = 0.0, False
    for attempt in range(MAX_RETRIES + 1):
        raw = backend(unit, rep, attempt)
        rec = classify(unit, raw) | {"rep": rep, "attempt": attempt}
        if rec.get("cost_total") is None:
            unknown = True
        else:
            spent += rec["cost_total"]
        if rec["outcome"] == "error" and rec["error_kind"] in RETRYABLE and attempt < MAX_RETRIES:
            if ctx is not None:
                ctx.sleep(30 * (attempt + 1))
            continue
        break
    rec["attempts"] = attempt + 1
    rec["cost_spent"] = None if unknown and spent == 0 else round(spent, 6)
    rec["raw"] = {
        k: raw.get(k)
        for k in (
            "text",
            "envelope",
            "init",
            "nested",
            "rc",
            "timed_out",
            "spawn_error",
            "secs",
            "nested_observed",
            "tool_uses",
            "plugin_dir",
            "answer_key_hits",
        )
    }
    rec["raw"]["stderr_tail"] = (raw.get("stderr") or "")[-1500:]
    return rec


def run_jobs(jobs: list[tuple[int, dict]], backend, ctx, guard: Guard, workers: int, sink) -> int:
    """Run jobs, never more than the guard allows; returns how many were never launched."""
    pending = deque(jobs)
    inflight = {}
    with ThreadPoolExecutor(max(1, workers)) as ex:
        while pending or inflight:
            while pending and len(inflight) < max(1, workers) and guard.can_launch(len(inflight)):
                rep, u = pending.popleft()
                inflight[ex.submit(run_one, u, rep, backend, ctx)] = (rep, u)
            if not inflight:
                break
            done, _ = wait(list(inflight), return_when=FIRST_COMPLETED)
            for f in done:
                rep, u = inflight.pop(f)
                try:
                    rec = f.result()
                except Exception as exc:  # a harness bug must surface as an ERROR row, not vanish
                    rec = classify(u, {"spawn_error": f"harness_exception:{exc!r}"}) | {
                        "rep": rep,
                        "attempt": 0,
                        "attempts": 1,
                        "cost_spent": None,
                    }
                sink(rec)
                guard.record(rec)
    return len(pending)


def make_sink(path: Path, recs: list[dict]):
    lock = threading.Lock()

    def sink(rec: dict) -> None:
        with lock:
            recs.append(rec)
            with path.open("a", encoding="utf-8") as f:
                f.write(json.dumps(rec, ensure_ascii=False) + "\n")
            print(
                f"[{rec['rep']}] {rec['id']:16s} {rec['style']:14s} {rec['outcome']:12s} "
                f"{(rec['error_kind'] or '') + ' '.join(rec['degraded_reasons'])[:60]:60s} "
                f"{rec.get('secs')}s ${rec.get('cost_spent')} try={rec.get('attempts')}",
                flush=True,
            )

    return sink


def check_controls(recs: list[dict]) -> list[str]:
    """Preflight acceptance: each control must behave exactly as its known truth demands."""
    problems = []
    by = {r["id"]: r for r in recs}
    for c in CONTROLS:
        r = by.get(c["id"])
        if r is None:
            problems.append(f"{c['id']}: did not run")
            continue
        if r["outcome"] != c["expect"]["outcome"]:
            why = r["errors"] or r["degraded_reasons"] or r["wrong_fields"] or r["asked_slots"]
            problems.append(
                f"{c['id']}: expected {c['expect']['outcome']}, got {r['outcome']} ({why})"
            )
        slot = c["expect"].get("asked_slot")
        if slot and r["outcome"] == "asked" and slot not in r["asked_slots"]:
            problems.append(
                f"{c['id']}: asked about {r['asked_slots']}, expected to include {slot!r}"
            )
        n = r.get("nested") or {}
        if n.get("n_ok", 0) < 3 or n.get("prompt_ok") is not True:
            problems.append(f"{c['id']}: nested compiles not as designed: {n}")
    return problems


def preflight(ctx, backend, guard: Guard, out: Path) -> tuple[bool, list[dict], list[str]]:
    pdir = out / "preflight"
    pdir.mkdir(parents=True, exist_ok=True)
    (pdir / "records.jsonl").write_text("", encoding="utf-8")
    recs: list[dict] = []
    left = run_jobs(
        [(0, c) for c in CONTROLS], backend, ctx, guard, 1, make_sink(pdir / "records.jsonl", recs)
    )
    problems = check_controls(recs)
    if left:
        problems.append(f"{left} control(s) never launched: {guard.stop_reason}")
    lines = ["# PREFLIGHT " + ("PASSED" if not problems else "FAILED")]
    for r in recs:
        lines.append(
            f"- {r['id']}: outcome={r['outcome']} asked_slots={r['asked_slots']} "
            f"wrong={r['wrong_fields']} errors={r['errors']} degraded={r['degraded_reasons']} "
            f"sampling={r['sampling']} nested={r['nested']} agent_models={r['agent_models']} "
            f"cost=${r.get('cost_spent')} secs={r.get('secs')} recheck_exit={r.get('recheck_exit')} "
            f"card_consistent={r.get('card_consistent')} permission_denials={r.get('permission_denials')}"
        )
    lines += [f"- PROBLEM: {p}" for p in problems]
    (pdir / "preflight.md").write_text("\n".join(lines) + "\n", encoding="utf-8")
    print("\n".join(lines), flush=True)
    return not problems, recs, problems


# ------------------------------------------------------------------------------------- main ---
def premise_audit(units: list[dict]) -> list[str]:
    """The scorer must score every unit's own truth, phrased as the AGENT would phrase it, exact."""
    problems = []
    for u in units:
        t = u["truth"]
        for tw in ["relative"] if t["relative_time"] else [None, "fixed"]:
            goal = {
                k: t.get(k)
                for k in ("task", "variable", "region", "period", "legacy_cumulative", "obs_source")
            }
            goal["time_window"] = tw
            _, adj = effective_truth(u)
            if adj:
                goal["obs_source"] = "station"
            card = {
                "status": "ready_for_approval",
                "sampling": "independent",
                "samples": 3,
                "goal": goal,
            }
            sc = score_card(u, card)
            if sc["outcome"] != "exact":
                problems.append(
                    f"{u['id']} (time_window={tw}): truth scores {sc['outcome']} {sc['wrong_fields']}"
                )
    return problems


def dry_run(units: list[dict], args) -> int:
    problems = []
    if len(units) < 50:
        problems.append(f"only {len(units)} units")
    present = {u["style"] for u in units}
    if not present <= set(STYLES):
        problems.append(f"unknown phrasing style(s) {sorted(present - set(STYLES))}")
    missing_styles = sorted(set(STYLES) - present)
    if missing_styles:
        print(f"note: phrasing style(s) absent from this population: {missing_styles}")
    if not any(u["clarity"] == "underdetermined" for u in units):
        problems.append("no underdetermined (ambiguous) units")
    for u in units + CONTROLS:
        t = dict(u["truth"])
        rep = GC.evaluate(
            [{**t, "time_window": "relative" if t["relative_time"] else None}], u["request"]
        )
        want = 3 if u["id"] == "control-neg" else 0
        if rep["exit_code"] != want:
            problems.append(
                f"{u['id']}: truth goal gives goal-check exit {rep['exit_code']}, expected {want}"
            )
    problems += premise_audit(units + CONTROLS[:1])
    checks = {}
    for kind in ("oracle", "perturb", "ask"):
        recs = [classify(u, backend_synthetic(u, kind)) for u in units]
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
    # Absence must never score: every failure shape -> error / degraded, never a metric.
    u0 = units[0]
    good = backend_synthetic(u0, "oracle")
    shapes = {
        "timeout": {"timed_out": True, "rc": None, "text": ""},
        "empty": good | {"text": "", "envelope": good["envelope"] | {"result": ""}},
        "rc1": good | {"rc": 1},
        "is_error": good
        | {"envelope": good["envelope"] | {"is_error": True, "subtype": "error_during_execution"}},
        "single": good | {"text": good["text"].replace('"independent"', '"single"')},
        "no_nested": good | {"nested": []},
    }
    for name, raw in shapes.items():
        r = classify(u0, raw)
        if r["outcome"] not in NOT_SCORED or any(r[k] is not None for k in METRICS):
            problems.append(f"absence check: shape {name!r} scored as {r['outcome']}")
    sample_ok = SAMPLE_MANIFEST.exists() and json.loads(SAMPLE_MANIFEST.read_text("utf-8")).get(
        "sample_sha256"
    ) == sha256_file(SAMPLE)
    if not sample_ok:
        problems.append(
            f"{SAMPLE_MANIFEST.name} missing or its sample_sha256 does not match the sample: re-run build_sample.py"
        )
    real = shutil.which("claude")
    if real is None:
        problems.append("`claude` CLI not on PATH (needed for the agent backend)")
    if git_bash() is None:
        problems.append("Git Bash not found: the nested-call observer's launcher cannot be tested")
    stamp = build_stamp(args, real)
    rel = stamp["git_dirty_relevant"]
    n_units = len(units) if not args.limit else min(args.limit, len(units))
    jobs = n_units * args.reps
    est = args.est_unit_cost * (jobs + len(CONTROLS))
    print(
        f"units: {len(units)}; styles: "
        + ", ".join(f"{s}={sum(u['style'] == s for u in units)}" for s in STYLES)
    )
    print(
        f"clarity: clear={sum(u['clarity'] == 'clear' for u in units)} "
        f"underdetermined={sum(u['clarity'] == 'underdetermined' for u in units)}; "
        f"truth adjusted to request: {[u['id'] for u in units if effective_truth(u)[1]]}"
    )
    print("scorer self-test (rates over all units):")
    for k, v in checks.items():
        print(f"  {k:8s} " + " ".join(f"{m}={x:.3f}" for m, x in v.items()))
    print(
        f"premise audit: truth-as-agent-goal scored exact for all {len(units)} units (both "
        "time_window phrasings)"
        if not premise_audit(units)
        else "premise audit: FAILED (see problems)"
    )
    print(
        f"stamp: git {stamp['git_sha'][:12]} run_key {stamp['run_key']} CLI {stamp['claude_version']}; "
        f"dirty relevant paths: {rel or 'none'}"
    )
    print(
        f"estimate for --reps {args.reps}{' --limit ' + str(args.limit) if args.limit else ''}: "
        f"{jobs} unit-runs + {len(CONTROLS)} controls ~ ${est:.0f} at ${args.est_unit_cost:.2f}/unit "
        f"(--max-cost {args.max_cost:.0f}); ~{jobs * DEFAULT_EST_UNIT_SECS / 3600 / max(1, args.workers):.1f} h "
        f"at {args.workers} worker(s)"
    )
    if rel:
        print(
            "note: the tree is dirty in plugin/harness paths; a paid run will refuse without --allow-dirty"
        )
    print(
        "DRY RUN: no model was called."
        + (" PROBLEMS:\n  " + "\n  ".join(problems) if problems else " OK")
    )
    return EXIT_REFUSED if problems else EXIT_OK


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--dry-run", action="store_true", help="validate offline; no model calls")
    ap.add_argument("--preflight", action="store_true", help="PAID: run only the 2 controls")
    ap.add_argument("--skip-preflight", action="store_true", help="do not run the controls first")
    ap.add_argument(
        "--backend", choices=["agent", "replay", "oracle", "perturb", "ask"], default="agent"
    )
    ap.add_argument("--replay", help="records.jsonl from an earlier run (replay backend)")
    ap.add_argument("--resume", help="an earlier run dir: skip its completed (non-error) unit-runs")
    ap.add_argument("--reps", type=int, default=1)
    ap.add_argument("--limit", type=int, default=0, help="first N units only")
    ap.add_argument("--ids", default="", help="comma list of unit ids")
    ap.add_argument(
        "--model", default=None, help="agent-session model (recorded; pin it for a full run)"
    )
    ap.add_argument("--timeout", type=int, default=900, help="per-unit seconds")
    ap.add_argument("--workers", type=int, default=1, help="parallel agent sessions")
    ap.add_argument(
        "--max-cost", type=float, default=DEFAULT_MAX_COST, help="USD, all attempts + controls"
    )
    ap.add_argument("--est-unit-cost", type=float, default=DEFAULT_EST_UNIT_COST)
    ap.add_argument("--allow-dirty", action="store_true", help="run on a dirty tree (recorded)")
    ap.add_argument(
        "--no-isolate",
        action="store_true",
        help="let the agent session load user settings, hooks and MCP servers (not reproducible)",
    )
    ap.add_argument(
        "--unit-budget",
        type=float,
        default=5.0,
        help="USD cap per agent session (--max-budget-usd)",
    )
    ap.add_argument("--out", default=None)
    a = ap.parse_args(argv)
    units = load_units()
    if a.dry_run:
        return dry_run(units, a)
    if a.backend == "replay":
        return replay(a)
    if a.ids:
        want = set(a.ids.split(","))
        units = [u for u in units if u["id"] in want]
        if missing := want - {u["id"] for u in units}:
            print(f"REFUSED: unknown ids {sorted(missing)}")
            return EXIT_REFUSED
    if a.limit:
        units = units[: a.limit]
    real = shutil.which("claude") if a.backend == "agent" else None
    if a.backend == "agent" and real is None:
        print("REFUSED: `claude` CLI not on PATH")
        return EXIT_REFUSED
    # Absolute: the agent runs with a temp cwd, so a relative --plugin-dir / log path would
    # silently point nowhere (measured 2026-10-04: "agent not found" on a relative --out).
    out = Path(
        a.resume or a.out or HERE / "runs" / dt.datetime.now().strftime("%Y%m%d-%H%M%S")
    ).resolve()
    out.mkdir(parents=True, exist_ok=True)
    stamp = build_stamp(a, real)
    if a.backend == "agent":
        if stamp["git_dirty_relevant"] and not a.allow_dirty:
            print(
                "REFUSED: dirty tree in plugin/harness paths (commit, or pass --allow-dirty):\n  "
                + "\n  ".join(stamp["git_dirty_relevant"])
            )
            return EXIT_REFUSED
        if not stamp["sample_matches_its_manifest"] and not a.preflight:
            print(
                f"REFUSED: {SAMPLE_MANIFEST.name} missing or does not match the sample; "
                "re-run build_sample.py so the sample's source shas are stamped"
            )
            return EXIT_REFUSED
    prior: dict[tuple, dict] = {}
    if a.resume:
        mpath = out / "manifest.json"
        if not mpath.exists():
            print(f"REFUSED: {mpath} not found; not a run dir")
            return EXIT_REFUSED
        old = json.loads(mpath.read_text(encoding="utf-8"))
        if old.get("run_key") != stamp["run_key"]:
            print(
                f"REFUSED: resume key mismatch ({old.get('run_key')} != {stamp['run_key']}): the "
                "agent, prompts, plugin, model, CLI or sample changed; start a new run"
            )
            return EXIT_REFUSED
        for line in (
            (out / "records.jsonl").read_text(encoding="utf-8").splitlines()
            if (out / "records.jsonl").exists()
            else []
        ):
            if line.strip():
                r = json.loads(line)
                prior[(r["id"], r["rep"])] = r  # last record per key wins
    stamp["resumed_from"] = len(prior) if a.resume else None
    (out / "manifest.json").write_text(
        json.dumps(stamp, indent=1, ensure_ascii=False), encoding="utf-8"
    )

    ctx = (
        Ctx(out, a.model, a.timeout, real, isolate=not a.no_isolate, unit_budget=a.unit_budget)
        if a.backend == "agent"
        else None
    )
    if a.backend == "agent":

        def backend(u, rep, attempt):
            return backend_agent(u, ctx, f"rep{rep}/{u['id']}/try{attempt}")
    else:

        def backend(u, rep, attempt):
            return backend_synthetic(u, a.backend)

    guard = Guard(a.max_cost, a.est_unit_cost)

    do_preflight = a.preflight or (a.backend == "agent" and not a.skip_preflight)
    if do_preflight:
        print(f"PREFLIGHT: {len(CONTROLS)} control units (max-cost ${a.max_cost:.2f})", flush=True)
        ok, crec, _ = preflight(ctx, backend, guard, out)
        measured = [r["cost_spent"] for r in crec if r.get("cost_spent")]
        if measured:
            guard.est_unit = round(max(measured) * 1.25, 3)
        if not ok:
            print("PREFLIGHT FAILED: the main run was NOT started.")
            return EXIT_PREFLIGHT
        if a.preflight:
            n = len(load_units())
            print(
                f"Measured per-unit estimate ${guard.est_unit:.2f} -> full run ({n} units x 3 reps) "
                f"~ ${guard.est_unit * n * 3:.0f}; preflight spent ${guard.spent:.2f}"
            )
            return EXIT_OK

    jobs = [
        (rep, u)
        for rep in range(a.reps)
        for u in units
        if not (prior.get((u["id"], rep)) and prior[(u["id"], rep)]["outcome"] != "error")
    ]
    est = guard.spent + guard.est_unit * len(jobs)
    print(
        f"ESTIMATE: {len(jobs)} unit-runs x ${guard.est_unit:.2f} + spent ${guard.spent:.2f} = "
        f"${est:.2f} (cap ${a.max_cost:.2f}); ~{len(jobs) * DEFAULT_EST_UNIT_SECS / 3600 / max(1, a.workers):.1f} h",
        flush=True,
    )
    if a.backend == "agent" and est > a.max_cost:
        print("REFUSED: the estimate exceeds --max-cost; raise it deliberately or narrow the run")
        return EXIT_REFUSED
    recs: list[dict] = []
    left = run_jobs(jobs, backend, ctx, guard, a.workers, make_sink(out / "records.jsonl", recs))
    final = {(r["id"], r["rep"]): r for r in prior.values()} | {
        (r["id"], r["rep"]): r for r in recs
    }
    aborted = guard.stop_reason if (left or guard.stop_reason) else None
    if aborted and left:
        aborted += f" ({left} unit-runs never launched)"
    n_units = len({k[0] for k in final})
    rpt, headline, code = report(
        list(final.values()),
        f"# Intake HITL eval: backend {a.backend}, {n_units} units x {a.reps} reps",
        aborted,
        stamp,
    )
    (out / "report.md").write_text(rpt, encoding="utf-8")
    print(rpt)
    print(f"\nrun dir: {out}\nexit {code}")
    return code


def replay(a) -> int:
    lines = Path(a.replay).read_text(encoding="utf-8").splitlines()
    byid = {u["id"]: u for u in load_units() + CONTROLS}
    recs = []
    for r in map(json.loads, filter(None, lines)):
        raw = r.get("raw") or {
            "text": r.get("text", ""),
            "rc": r.get("rc"),
            "nested_observed": False,
            "envelope": {
                "subtype": "success",
                "result": r.get("text", ""),
                "total_cost_usd": r.get("cost_usd"),
            },
        }
        recs.append(classify(byid[r["id"]], raw) | {"rep": r.get("rep", 0)})
    rpt, _, code = report(recs, f"# Intake HITL eval: replay of {a.replay}")
    print(rpt)
    return code


if __name__ == "__main__":
    sys.exit(main())
