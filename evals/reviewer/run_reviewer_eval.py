"""Planted-defect benchmark for the weather-skills reviewer agent.

Each case file in ``cases/`` holds one defect and its clean twin (the same
pipeline without the defect), the defect class, the expected verdicts, and the
SKILL.md clause(s) that make the defect a defect.

    # offline: validate every case file (schema, twins, premise quotes, twin diff size)
    uv run python evals/reviewer/run_reviewer_eval.py --dry-run

    # paid, 2 runs: positive + negative control only
    uv run python evals/reviewer/run_reviewer_eval.py --preflight --out evals/reviewer/results/<dir>

    # paid, full run (runs the preflight first unless a passing one is in --out)
    uv run python evals/reviewer/run_reviewer_eval.py --n 3 --out evals/reviewer/results/<dir>

The live backend is headless Claude Code with a STAGED COPY of this checkout
(everything except ``evals/``) loaded as the plugin:

    claude -p --agent rhiza-forecasting:reviewer --plugin-dir <staged copy>
           --output-format stream-json --verbose ...

The prompt goes on stdin. Each run starts in an empty temporary directory with
file tools limited to Read/Grep/Glob/Skill and every permission prompt
auto-denied. The answer key is not in the staged plugin, and every run's full
transcript is scanned for any touch of an ``evals/`` path or an answer-key
field: one hit aborts the whole run (exit 4).

Every run gets exactly one outcome:

  defect twin   CATCH (REJECT) | MISS (APPROVE) | NEEDS_INFO | ERROR
  clean twin    APPROVE_OK     | FALSE_ALARM (REJECT) | NEEDS_INFO | ERROR

ERROR covers timeouts, CLI failures, ``is_error`` results, empty or
unparseable or ambiguous replies. An ERROR is never a catch, a miss, a clean
pass or a false alarm: it is excluded from every rate's denominator and
reported per case. The run's verdict is INCONCLUSIVE (exit 3) when errors
exceed ``--max-error-share`` of runs, when any case/variant cell has every rep
errored, when planned runs are missing, or when a harness sanity check fires
(uniform verdicts, identical replies across different cases).

Exit codes: 0 COMPLETE, 1 refused (invalid cases, dirty tree), 2 usage,
3 INCONCLUSIVE, 4 ABORTED (auth/quota, leakage, cost ceiling, wrong model,
consecutive errors, interrupt), 5 PREFLIGHT FAILED.
"""

from __future__ import annotations

import argparse
import concurrent.futures as cf
import datetime as dt
import difflib
import hashlib
import json
import math
import os
import random
import re
import shutil
import subprocess
import sys
import tempfile
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

import yaml

HERE = Path(__file__).resolve().parent
REPO = HERE.parents[1]
CASES_DIR = HERE / "cases"
VERDICTS = ("APPROVE", "REJECT", "NEEDS-INFO")
REQUIRED = ("id", "defect_class", "kind", "skills", "task", "context", "premise", "defect", "clean")
# Fields that are answer key or author notes: never sent to the reviewer.
HIDDEN = ("premise", "defect_class", "expected_verdict", "expected_step", "match_any", "note")
# A card's VERDICT line. The template line "VERDICT: APPROVE | REJECT | NEEDS-INFO" is
# excluded by the negative lookahead on "|".
VERDICT_LINE_RE = re.compile(
    r"^[\s>*_#`-]*VERDICT\s*:?\s*[*_`]*\s*(APPROVE|REJECT|NEEDS[-_ ]INFO)\b(?![^\n]*\|)",
    re.IGNORECASE | re.MULTILINE,
)

EXIT_OK, EXIT_REFUSED, EXIT_USAGE, EXIT_INCONCLUSIVE, EXIT_ABORTED, EXIT_PREFLIGHT = 0, 1, 2, 3, 4, 5

# Outcome classes.
CATCH, MISS, NEEDS_INFO, APPROVE_OK, FALSE_ALARM, ERROR = (
    "CATCH",
    "MISS",
    "NEEDS_INFO",
    "APPROVE_OK",
    "FALSE_ALARM",
    "ERROR",
)

# Twin-diff sanity: a clean twin may differ from its defect twin in at most this many
# lines and hunks (after stripping plan step numbers), unless the case overrides it.
TWIN_DIFF_MAX_LINES = 6
TWIN_DIFF_MAX_HUNKS = 2

DEFAULT_CONTROL_CASE = "bbox-west-east-swapped"
DIRTY_GUARD_PATHS = ("evals/reviewer", "agents", "skills", ".claude-plugin")

# CLI failure classification. Scanned ONLY when the run already failed (non-zero exit,
# is_error, no result event), so a review card that happens to say "401" is not an error.
AUTH_PATTERNS = [
    r"invalid api key",
    r"invalid x-api-key",
    r"authentication[_ ]error",
    r"please run /login",
    r"not logged in",
    r"oauth token (?:has )?(?:expired|been revoked)",
    r"token (?:has )?expired",
    r"\b401\b",
    r"\b403\b",
    r"unauthori[sz]ed",
    r"credit balance is too low",
    r"organization (?:has been )?disabled",
]
QUOTA_PATTERNS = [r"usage limit", r"limit reached", r"quota exceeded", r"resets? at \d"]
RATE_LIMIT_PATTERNS = [r"\b429\b", r"rate[_ ]limit"]
TRANSIENT_PATTERNS = [
    r"overloaded",
    r"\b529\b",
    r"\b50[0234]\b",
    r"internal server error",
    r"econnreset|etimedout|econnrefused|socket hang up",
    r"connection (?:error|reset|refused)",
    r"fetch failed",
    r"network error",
]
# Transcript leakage: any tool call whose input names an evals/ path or a case file, or any
# tool result / reply carrying an answer-key field name.
LEAK_INPUT_RE = re.compile(r"evals[\\/]+|reviewer[\\/]+cases|run_reviewer_eval", re.IGNORECASE)
LEAK_CONTENT_RE = re.compile(r"expected_verdict|match_any|expected_step|injected_rationale")


# ---------------------------------------------------------------------------
# Case loading and offline validation
# ---------------------------------------------------------------------------


def _norm(text: str) -> str:
    return " ".join(text.split())


def _sha(text: str | bytes) -> str:
    data = text.encode("utf-8") if isinstance(text, str) else text
    return hashlib.sha256(data).hexdigest()


def load_cases(cases_dir: Path) -> list[tuple[Path, dict]]:
    out = []
    for path in sorted(cases_dir.glob("*.yaml")):
        try:
            with path.open(encoding="utf-8") as fh:
                out.append((path, yaml.safe_load(fh)))
        except yaml.YAMLError as exc:
            out.append((path, f"YAML parse error: {exc}"))
    return out


def _submission_lines(text: str) -> list[str]:
    """Submission lines with plan step numbers stripped, so renumbering is not a diff."""
    return [re.sub(r"^\s*\d+\.\s+", "", ln).rstrip() for ln in str(text).strip("\n").splitlines()]


def twin_diff(defect_sub: str, clean_sub: str) -> dict:
    """Size and location of the defect/clean difference (1-based defect-side lines)."""
    a, b = _submission_lines(defect_sub), _submission_lines(clean_sub)
    ops = difflib.SequenceMatcher(a=a, b=b, autojunk=False).get_opcodes()
    hunks = [(i1 + 1, max(i1 + 1, i2), j2 - j1) for tag, i1, i2, j1, j2 in ops if tag != "equal"]
    changed = sum(
        max(i2 - i1, j2 - j1) for tag, i1, i2, j1, j2 in ops if tag != "equal"
    )
    return {"changed_lines": changed, "hunks": hunks}


def _expected_step_number(step) -> int | None:
    m = re.search(r"\d+", str(step)) if step is not None else None
    return int(m.group(0)) if m else None


def validate_case(path: Path, case: dict, repo: Path) -> list[str]:
    """Every problem with one case file; empty when it is valid."""
    errs = []
    if isinstance(case, str):
        return [f"{path.name}: {case}"]
    if not isinstance(case, dict):
        return [f"{path.name}: not a mapping"]
    for key in REQUIRED:
        if key not in case:
            errs.append(f"missing key {key!r}")
    if errs:
        return [f"{path.name}: {e}" for e in errs]

    if case["kind"] not in ("plan", "code"):
        errs.append(f"kind must be plan or code, got {case['kind']!r}")
    skills = case["skills"]
    if not isinstance(skills, list) or not skills:
        errs.append("skills must be a non-empty list")
        skills = []
    for name in skills:
        if not (repo / "skills" / name / "SKILL.md").is_file():
            errs.append(f"skills: no skills/{name}/SKILL.md in this checkout")

    # Premise audit: every quoted clause must exist verbatim in the cited file.
    premise = case["premise"] if isinstance(case["premise"], list) else []
    quotes = [p for p in premise if isinstance(p, dict) and "quote" in p]
    if not quotes:
        errs.append("premise: at least one {file, quote} entry is required")
    for entry in quotes:
        target = repo / entry.get("file", "")
        if not target.is_file():
            errs.append(f"premise: file not found {entry.get('file')!r}")
            continue
        if _norm(entry["quote"]) not in _norm(target.read_text(encoding="utf-8")):
            errs.append(f"premise: quote not found verbatim in {entry['file']}: {entry['quote']!r}")
    if not any(isinstance(p, dict) and p.get("why") for p in premise):
        errs.append("premise: a 'why' explanation is required")

    for variant, verdict in (("defect", "REJECT"), ("clean", "APPROVE")):
        block = case[variant]
        if not isinstance(block, dict) or not str(block.get("submission", "")).strip():
            errs.append(f"{variant}: submission is required")
            continue
        if block.get("expected_verdict") != verdict:
            errs.append(f"{variant}: expected_verdict must be {verdict}")
    defect, clean = case["defect"], case["clean"]
    if isinstance(defect, dict) and isinstance(clean, dict):
        d_sub, c_sub = str(defect.get("submission", "")), str(clean.get("submission", ""))
        if _norm(d_sub) == _norm(c_sub):
            errs.append("defect and clean submissions are identical (not a twin)")
        else:
            # Twin sanity: the twins differ ONLY around the planted defect.
            diff = twin_diff(d_sub, c_sub)
            max_lines = int(case.get("twin_diff_max_lines", TWIN_DIFF_MAX_LINES))
            if diff["changed_lines"] > max_lines:
                errs.append(
                    f"twin diff too large: {diff['changed_lines']} changed lines > {max_lines} "
                    "(the clean twin must differ only in the planted-defect region; set "
                    "twin_diff_max_lines with a note if this is deliberate)"
                )
            if len(diff["hunks"]) > TWIN_DIFF_MAX_HUNKS:
                errs.append(
                    f"twin diff has {len(diff['hunks'])} separate hunks > {TWIN_DIFF_MAX_HUNKS}"
                )
            step = _expected_step_number(defect.get("expected_step"))
            if step is None:
                errs.append("defect: expected_step (a step or line number) is required")
            elif not any(lo - 1 <= step <= hi + 1 for lo, hi, _ in diff["hunks"]):
                errs.append(
                    f"defect: expected_step {step} is not inside or next to any twin-diff hunk "
                    f"{[(lo, hi) for lo, hi, _ in diff['hunks']]}: the planted defect is not "
                    "where the twins differ"
                )
        patterns = defect.get("match_any") or []
        if not patterns:
            errs.append("defect: match_any patterns are required for located-catch scoring")
        for pat in patterns:
            try:
                re.compile(pat)
            except re.error as exc:
                errs.append(f"defect: bad match_any regex {pat!r}: {exc}")
        for key in ("injected_rationale",):
            if key in clean:
                errs.append(f"clean: {key} belongs on the defect variant only")
    return [f"{path.name}: {e}" for e in errs]


def dry_run(cases: list[tuple[Path, dict]], repo: Path, quiet: bool = False) -> int:
    problems, ids = [], {}
    for path, case in cases:
        problems += validate_case(path, case, repo)
        cid = case.get("id") if isinstance(case, dict) else None
        if cid in ids:
            problems.append(f"{path.name}: duplicate id {cid!r} (also {ids[cid]})")
        ids[cid] = path.name
    if not quiet:
        print(f"{'case':<36} {'kind':<5} {'defect class':<42} {'diff':<5} premise")
        for _path, case in cases:
            if not isinstance(case, dict):
                continue
            quotes = [p for p in case.get("premise", []) if isinstance(p, dict) and "quote" in p]
            files = ", ".join(sorted({p.get("file", "?").split("/")[1] for p in quotes}))
            try:
                d = twin_diff(case["defect"]["submission"], case["clean"]["submission"])
                dtxt = f"{d['changed_lines']}L/{len(d['hunks'])}h"
            except (KeyError, TypeError):
                dtxt = "?"
            print(
                f"{case.get('id', '?'):<36} {case.get('kind', '?'):<5} "
                f"{case.get('defect_class', '?'):<42} {dtxt:<5} {files}"
            )
    n_cases = len(cases)
    if not quiet:
        print(f"\n{n_cases} cases ({2 * n_cases} variants: {n_cases} defect + {n_cases} clean twins)")
    if n_cases < 10:
        problems.append(f"only {n_cases} cases; the benchmark needs at least 10")
    if problems:
        print("\nINVALID:")
        for p in problems:
            print(f"  - {p}")
        return 1
    if not quiet:
        print(
            "all case files valid; every premise quote found verbatim in its SKILL.md; every twin "
            f"pair differs in <= {TWIN_DIFF_MAX_LINES} lines / {TWIN_DIFF_MAX_HUNKS} hunks around "
            "its expected_step; every variant has its expected verdict"
        )
    return 0


# ---------------------------------------------------------------------------
# Prompt construction (answer key never included)
# ---------------------------------------------------------------------------


def build_prompt(case: dict, variant: str, repo: Path, inline_skills: bool) -> str:
    block = case[variant]
    label = (
        "PROPOSED PLAN (not yet run)"
        if case["kind"] == "plan"
        else "MODEL-WRITTEN CODE (not yet run)"
    )
    parts = [
        f"Review request: {case['kind']}.",
        "",
        "TASK:",
        case["task"].strip(),
        "",
        "INPUT FACTS:",
        case["context"].strip(),
        "",
        f"{label}:",
        "```",
        block["submission"].rstrip(),
        "```",
    ]
    if block.get("injected_rationale"):
        parts += ["", "AUTHOR'S NOTE:", block["injected_rationale"].strip()]
    if inline_skills:
        parts += ["", "SKILL TEXTS (verbatim SKILL.md of each skill named above):"]
        for name in case["skills"]:
            text = (repo / "skills" / name / "SKILL.md").read_text(encoding="utf-8")
            parts += ["", f"===== skills/{name}/SKILL.md =====", text.rstrip()]
    parts += ["", "Return your review card."]
    prompt = "\n".join(parts)
    # Belt and braces: the answer key must never reach the prompt.
    if LEAK_CONTENT_RE.search(prompt):
        raise RuntimeError(f"answer-key field name in the prompt for {case['id']}/{variant}")
    return prompt


# ---------------------------------------------------------------------------
# CLI output parsing and classification (pure; unit-tested with synthetic output)
# ---------------------------------------------------------------------------


def parse_events(stdout: str) -> tuple[list[dict], int]:
    """stream-json lines (or a single json object) -> (events, n_unparseable_lines)."""
    events, bad = [], 0
    text = (stdout or "").strip()
    if not text:
        return [], 0
    try:
        obj = json.loads(text)
        return ([obj] if isinstance(obj, dict) else [e for e in obj if isinstance(e, dict)]), 0
    except json.JSONDecodeError:
        pass
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            obj = json.loads(line)
        except json.JSONDecodeError:
            bad += 1
            continue
        if isinstance(obj, dict):
            events.append(obj)
    return events, bad


def _content_text(content) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "\n".join(_content_text(c.get("text", c.get("content", ""))) if isinstance(c, dict) else str(c) for c in content)
    if isinstance(content, dict):
        return _content_text(content.get("text", content.get("content", "")))
    return "" if content is None else str(content)


def transcript_tools(events: list[dict]) -> tuple[list[dict], list[str]]:
    """(tool_use calls, tool_result texts) from a stream-json transcript."""
    uses, results = [], []
    for ev in events:
        msg = ev.get("message") if isinstance(ev.get("message"), dict) else None
        if not msg:
            continue
        for block in msg.get("content") or []:
            if not isinstance(block, dict):
                continue
            if block.get("type") == "tool_use":
                uses.append({"name": block.get("name"), "input": block.get("input")})
            elif block.get("type") == "tool_result":
                results.append(_content_text(block.get("content")))
    return uses, results


def leakage_hits(events: list[dict], reply: str) -> list[str]:
    """Every sign the agent touched the eval directory or saw the answer key."""
    hits = []
    uses, results = transcript_tools(events)
    for u in uses:
        blob = json.dumps(u.get("input"), ensure_ascii=False)
        if LEAK_INPUT_RE.search(blob) or LEAK_CONTENT_RE.search(blob):
            hits.append(f"tool {u.get('name')} input touches eval files: {blob[:200]}")
    for r in results:
        m = LEAK_CONTENT_RE.search(r)
        if m:
            hits.append(f"tool result contains answer-key field {m.group(0)!r}")
    m = LEAK_CONTENT_RE.search(reply or "")
    if m:
        hits.append(f"reply contains answer-key field {m.group(0)!r}")
    return hits


def _any(patterns: list[str], text: str) -> bool:
    return any(re.search(p, text, re.IGNORECASE) for p in patterns)


def classify_cli(rc: int | None, stdout: str, stderr: str, timed_out: bool = False) -> dict:
    """One CLI invocation -> a run result. ``error_kind`` set means the run is an ERROR.

    error kinds: timeout, auth, quota, rate_limit, transient, cli_error, no_result,
    empty_reply, unparseable, ambiguous, leakage.  ``fatal`` stops the whole run;
    ``transient`` makes it eligible for retry.
    """
    events, bad_lines = parse_events(stdout)
    result = next((e for e in reversed(events) if e.get("type") == "result"), None)
    init = next(
        (e for e in events if e.get("type") == "system" and e.get("subtype") == "init"), None
    )
    reply = (result or {}).get("result") or ""
    if not isinstance(reply, str):
        reply = json.dumps(reply)
    model_usage = (result or {}).get("modelUsage") or {}
    res = {
        "text": reply,
        "cost_usd": (result or {}).get("total_cost_usd"),
        "models_used": sorted(model_usage) if isinstance(model_usage, dict) else [],
        "init_model": (init or {}).get("model"),
        "init_agents": (init or {}).get("agents"),
        "init_plugins": (init or {}).get("plugins"),
        "subtype": (result or {}).get("subtype"),
        "is_error": (result or {}).get("is_error"),
        "num_turns": (result or {}).get("num_turns"),
        "permission_denials": (result or {}).get("permission_denials"),
        "returncode": rc,
        "unparseable_stream_lines": bad_lines,
        "error_kind": None,
        "error": None,
        "fatal": False,
        "transient": False,
    }
    uses, _ = transcript_tools(events)
    res["tool_calls"] = [
        {"name": u.get("name"), "input": json.dumps(u.get("input"), ensure_ascii=False)[:300]}
        for u in uses
    ]

    def fail(kind: str, msg: str, fatal: bool = False, transient: bool = False) -> dict:
        res.update(error_kind=kind, error=msg[:1500], fatal=fatal, transient=transient)
        return res

    if timed_out:
        return fail("timeout", "run exceeded the per-run timeout")

    failed = (
        rc not in (0, None)
        or result is None
        or bool(res["is_error"])
        or (res["subtype"] not in (None, "success"))
    )
    if failed:
        blob = "\n".join([stderr or "", (stdout or "")[-4000:], reply])
        if _any(QUOTA_PATTERNS, blob):
            return fail("quota", f"usage/quota limit: {blob.strip()[-600:]}", fatal=True)
        if _any(AUTH_PATTERNS, blob):
            return fail("auth", f"authentication failure: {blob.strip()[-600:]}", fatal=True)
        if _any(RATE_LIMIT_PATTERNS, blob):
            return fail("rate_limit", f"rate limited: {blob.strip()[-600:]}", transient=True)
        if _any(TRANSIENT_PATTERNS, blob):
            return fail("transient", f"transient API error: {blob.strip()[-600:]}", transient=True)
        if result is None:
            return fail("no_result", f"no result event (exit {rc}): {blob.strip()[-600:]}")
        return fail(
            "cli_error",
            f"exit {rc}, is_error={res['is_error']}, subtype={res['subtype']}: "
            f"{blob.strip()[-600:]}",
        )

    hits = leakage_hits(events, reply)
    if hits:
        res["leakage"] = hits
        return fail("leakage", "; ".join(hits), fatal=True)
    if not reply.strip():
        return fail("empty_reply", "the agent returned an empty reply")
    verdict, why = parse_verdict(reply)
    if verdict is None:
        return fail(why, f"no single VERDICT line in the reply ({why})")
    res["verdict"] = verdict
    return res


def parse_verdict(text: str) -> tuple[str | None, str]:
    """(verdict, '') or (None, 'unparseable' | 'ambiguous')."""
    found = {m.group(1).upper().replace("_", "-").replace(" ", "-") for m in VERDICT_LINE_RE.finditer(text or "")}
    if not found:
        return None, "unparseable"
    if len(found) > 1:
        return None, "ambiguous"
    return found.pop(), ""


def findings_text(text: str) -> str | None:
    """The FINDINGS section of a card, or None when the card has none."""
    m = re.search(r"FINDINGS\s*:(.*?)(?:\n[\s*#]*NEEDED\s*:|\n[\s*#]*OPEN QUESTIONS\s*:|\Z)", text or "", re.S)
    return m.group(1) if m else None


def located(text: str, patterns: list[str]) -> bool:
    body = findings_text(text)
    if body is None:  # no FINDINGS section: cannot credit a located catch
        return False
    return any(re.search(p, body, re.IGNORECASE) for p in patterns)


RULE_RE = re.compile(r"rule:\s*skills/([\w-]+)/SKILL\.md:?(.*)")
QUOTE_RE = re.compile(r"\"([^\"]{8,})\"")


def citation_check(text: str, repo: Path) -> tuple[int, int]:
    """(verbatim, total) for the quoted SKILL.md lines on the card's ``rule:`` lines."""
    ok = total = 0
    for name, rest in RULE_RE.findall(findings_text(text) or ""):
        path = repo / "skills" / name / "SKILL.md"
        source = _norm(path.read_text(encoding="utf-8")) if path.is_file() else ""
        for quote in QUOTE_RE.findall(rest):
            total += 1
            ok += bool(source) and _norm(quote) in source
    return ok, total


def outcome_for(variant: str, verdict: str | None, error_kind: str | None) -> str:
    if error_kind or verdict is None:
        return ERROR
    if verdict == "NEEDS-INFO":
        return NEEDS_INFO
    if variant == "defect":
        return CATCH if verdict == "REJECT" else MISS
    return FALSE_ALARM if verdict == "REJECT" else APPROVE_OK


def response_hash(text: str) -> str:
    return _sha(_norm(text or ""))[:16]


# ---------------------------------------------------------------------------
# Statistics
# ---------------------------------------------------------------------------


def wilson(k: int, n: int, z: float = 1.96) -> tuple[float, float] | None:
    if n == 0:
        return None
    p = k / n
    denom = 1 + z * z / n
    centre = (p + z * z / (2 * n)) / denom
    half = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / denom
    return max(0.0, centre - half), min(1.0, centre + half)


def fmt_ci(ci: tuple[float, float] | None) -> str:
    return "-" if ci is None else f"{ci[0]:.2f}-{ci[1]:.2f}"


def cluster_bootstrap(groups: list[tuple[int, int]], iters: int = 2000, seed: int = 7):
    """95% CI of pooled k/n, resampling CASES (reps of one case are not independent)."""
    groups = [g for g in groups if g[1] > 0]
    if len(groups) < 2:
        return None
    rng = random.Random(seed)
    stats = []
    for _ in range(iters):
        draw = [groups[rng.randrange(len(groups))] for _ in groups]
        n = sum(g[1] for g in draw)
        stats.append(sum(g[0] for g in draw) / n)
    stats.sort()
    return stats[int(0.025 * iters)], stats[int(0.975 * iters) - 1]


# ---------------------------------------------------------------------------
# Run bookkeeping: keys, atomic writes, stamp
# ---------------------------------------------------------------------------


def run_key(case_id: str, variant: str, rep: int, agent_hash: str, config_hash: str, prefix: str = "") -> str:
    """Resume key. A changed agent file or prompt/config forks a NEW key, so results from
    two agent versions can never be silently mixed in one results dir."""
    return f"{prefix}{case_id}__{variant}__r{rep}__a{agent_hash[:12]}__c{config_hash[:12]}"


def atomic_write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(text)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def load_records(runs_dir: Path) -> dict[str, dict]:
    out = {}
    if not runs_dir.is_dir():
        return out
    for p in sorted(runs_dir.glob("*.json")):
        try:
            rec = json.loads(p.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue  # a torn file cannot exist (atomic writes); skip anything foreign
        if isinstance(rec, dict) and rec.get("key"):
            out[rec["key"]] = rec
    return out


def _git(repo: Path, *args: str) -> str | None:
    try:
        proc = subprocess.run(
            ["git", "-C", str(repo), *args], capture_output=True, text=True, encoding="utf-8", timeout=60
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    return proc.stdout if proc.returncode == 0 else None


def git_dirty(repo: Path) -> list[str] | None:
    """Uncommitted paths under the guarded dirs; None if git could not tell."""
    out = _git(repo, "status", "--porcelain", "--", *DIRTY_GUARD_PATHS)
    if out is None:
        return None
    return [ln for ln in out.splitlines() if ln.strip()]


def agent_file(plugin_dir: Path, agent: str) -> Path:
    return plugin_dir / "agents" / f"{agent.split(':')[-1]}.md"


def agent_model_pin(text: str) -> str | None:
    m = re.match(r"---\s*\n(.*?)\n---", text, re.S)
    if not m:
        return None
    fm = yaml.safe_load(m.group(1)) or {}
    return str(fm.get("model")) if fm.get("model") else None


def claude_exe() -> str | None:
    return shutil.which("claude")


def collect_stamp(args, agent_hash: str, config_hash: str) -> dict:
    exe = claude_exe()
    cli_version = None
    if exe:
        try:
            cli_version = subprocess.run(
                [exe, "--version"], capture_output=True, text=True, encoding="utf-8", timeout=60
            ).stdout.strip()
        except (OSError, subprocess.TimeoutExpired):
            cli_version = None
    manifest = args.plugin_dir / ".claude-plugin" / "plugin.json"
    try:
        plugin_json = json.loads(manifest.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        plugin_json = {}
    return {
        "timestamp_utc": dt.datetime.now(dt.UTC).isoformat(timespec="seconds"),
        "git_sha": (_git(args.plugin_dir, "rev-parse", "HEAD") or "").strip() or None,
        "git_branch": (_git(args.plugin_dir, "rev-parse", "--abbrev-ref", "HEAD") or "").strip() or None,
        "dirty_guarded_paths": git_dirty(args.plugin_dir),
        "plugin_name": plugin_json.get("name"),
        "plugin_version": plugin_json.get("version") or "unversioned (no version in plugin.json)",
        "plugin_json_sha256": _sha(manifest.read_bytes()) if manifest.is_file() else None,
        "agent": args.agent,
        "agent_file_sha256": agent_hash,
        "config_sha256": config_hash,
        "model_override": args.model,
        "claude_cli": exe,
        "claude_cli_version": cli_version,
        "python": sys.version.split()[0],
    }


# ---------------------------------------------------------------------------
# Live backend
# ---------------------------------------------------------------------------


def stage_plugin(src: Path, dst_root: Path) -> Path:
    """Copy the plugin WITHOUT evals/ (the answer key) and without VCS/venv clutter.

    Uses git's file list (tracked + untracked-not-ignored) when available, so only real
    plugin content is staged; then proves no answer-key field survived the copy.
    """
    dst = dst_root / "plugin"
    listing = _git(src, "ls-files", "-co", "--exclude-standard", "-z")
    excluded_top = {"evals", ".git", ".venv", "node_modules", ".pytest_cache", ".ruff_cache"}
    if listing is not None:
        for rel in filter(None, listing.split("\0")):
            parts = Path(rel).parts
            if not parts or parts[0] in excluded_top:
                continue
            s = src / rel
            if not s.is_file():
                continue
            d = dst / rel
            d.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(s, d)
    else:
        shutil.copytree(
            src,
            dst,
            ignore=lambda d, names: [n for n in names if Path(d) == src and n in excluded_top]
            + [n for n in names if n == "__pycache__"],
        )
    if (dst / "evals").exists():
        raise RuntimeError("staging failed: evals/ present in the staged plugin")
    for p in dst.rglob("*"):
        if p.is_file() and p.stat().st_size < 2_000_000:
            try:
                text = p.read_text(encoding="utf-8")
            except (UnicodeDecodeError, OSError):
                continue
            if "expected_verdict" in text or "match_any" in text:
                raise RuntimeError(f"staging failed: answer-key field found in {p}")
    return dst


def build_cmd(args, plugin_dir: Path) -> list[str]:
    exe = claude_exe()
    if not exe:
        raise SystemExit("claude CLI not found on PATH; the live eval needs Claude Code installed")
    cmd = [
        exe,
        "-p",
        "--agent",
        args.agent,
        "--plugin-dir",
        str(plugin_dir),
        "--output-format",
        "stream-json",
        "--verbose",
        "--no-session-persistence",
        "--permission-prompts",
        "none",
        "--allowedTools",
        "Read Grep Glob Skill",
        "--max-budget-usd",
        f"{args.max_run_cost:.2f}",
    ]
    if args.model:
        cmd += ["--model", args.model]
    return cmd


def invoke_cli(cmd: list[str], prompt: str, timeout: int) -> tuple[int | None, str, str, bool, float]:
    """Run one headless CLI call in an empty temp dir -> (rc, stdout, stderr, timed_out, secs)."""
    started = time.monotonic()
    with tempfile.TemporaryDirectory(prefix="reviewer-eval-") as workdir:
        try:
            proc = subprocess.run(
                cmd,
                input=prompt,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                cwd=workdir,
                timeout=timeout,
            )
        except subprocess.TimeoutExpired as exc:
            out = exc.stdout.decode("utf-8", "replace") if isinstance(exc.stdout, bytes) else (exc.stdout or "")
            return None, out, "", True, round(time.monotonic() - started, 1)
        except OSError as exc:
            return 127, "", f"could not start claude CLI: {exc}", False, round(time.monotonic() - started, 1)
    return proc.returncode, proc.stdout, proc.stderr, False, round(time.monotonic() - started, 1)


@dataclass
class Ctx:
    args: argparse.Namespace
    out_dir: Path
    cmd: list[str]
    plugin_dir: Path
    agent_hash: str
    config_hash: str
    model_pin: str | None
    invoke: Callable = invoke_cli
    sleep: Callable = time.sleep
    est_cost: float = 0.45
    spent: float = 0.0
    log: Callable = print
    calls: int = 0


def make_jobs(cases: list[dict], variants, n: int, ctx: Ctx, prefix: str = "", reps=None) -> list[dict]:
    jobs = []
    for case in cases:
        for variant in variants:
            prompt = build_prompt(case, variant, ctx.plugin_dir, not ctx.args.no_inline_skills)
            cfg = _sha(prompt + "\0" + (ctx.args.model or "") + "\0" + ctx.config_hash)
            for rep in reps or range(1, n + 1):
                jobs.append(
                    {
                        "key": run_key(case["id"], variant, rep, ctx.agent_hash, cfg, prefix),
                        "case": case["id"],
                        "defect_class": case["defect_class"],
                        "variant": variant,
                        "rep": rep,
                        "prompt": prompt,
                        "prompt_sha256": _sha(prompt),
                        "expected": case[variant]["expected_verdict"],
                        "match_any": case["defect"]["match_any"],
                    }
                )
    return jobs


def run_one(job: dict, ctx: Ctx) -> dict:
    """Run one job with transient-error retries; never raises for a CLI failure."""
    attempts, cost_total, cost_known, seconds = [], 0.0, True, 0.0
    res = None
    for attempt in range(ctx.args.retries + 1):
        ctx.calls += 1
        rc, stdout, stderr, timed_out, secs = ctx.invoke(ctx.cmd, job["prompt"], ctx.args.timeout)
        seconds += secs or 0.0
        res = classify_cli(rc, stdout, stderr, timed_out)
        res["_stdout"] = stdout
        c = res.get("cost_usd")
        if isinstance(c, (int, float)):
            cost_total += float(c)
        elif not res.get("error_kind"):
            cost_known = False
        attempts.append({"attempt": attempt + 1, "error_kind": res.get("error_kind"), "cost_usd": c, "seconds": secs})
        if res.get("transient") and attempt < ctx.args.retries:
            wait = ctx.args.backoff * (3**attempt)
            ctx.log(f"    transient {res['error_kind']} on {job['key']}; retry in {wait:.0f}s")
            ctx.sleep(wait)
            continue
        break
    if res.get("transient"):  # retries exhausted
        if res["error_kind"] == "rate_limit":
            res["fatal"] = True
            res["error"] = f"rate limited after {len(attempts)} attempts: " + (res.get("error") or "")
    # Model check: the agent pins a model; a run on a different family means the agent
    # was not loaded (the CLI fell back to the default session).
    agents = res.get("init_agents")
    short = ctx.args.agent.split(":")[-1]
    if not res.get("error_kind") and isinstance(agents, list) and agents and not any(
        short in (a if isinstance(a, str) else json.dumps(a)) for a in agents
    ):
        res.update(
            error_kind="agent_not_loaded",
            error=f"{ctx.args.agent} is not among the session's agents {agents[:20]}",
            fatal=True,
        )
    if not res.get("error_kind") and ctx.model_pin and not ctx.args.model:
        family = ctx.model_pin.lower()
        if family in ("opus", "sonnet", "haiku") and not any(family in m.lower() for m in res["models_used"]):
            res.update(
                error_kind="wrong_model",
                error=f"agent pins {ctx.model_pin} but the run used {res['models_used']}: "
                "the agent was probably not loaded",
                fatal=True,
            )
    rec = {k: v for k, v in job.items() if k not in ("prompt", "match_any")}
    stdout = res.pop("_stdout", "")
    rec.update(res)
    rec["attempts"] = attempts
    rec["seconds"] = round(seconds, 1)
    rec["cost_usd"] = round(cost_total, 6) if (cost_known or cost_total) else None
    rec["cost_known"] = cost_known
    rec["verdict"] = res.get("verdict") if not res.get("error_kind") else None
    rec["outcome"] = outcome_for(job["variant"], rec["verdict"], rec.get("error_kind"))
    text = res.get("text", "")
    rec["located"] = bool(
        rec["outcome"] == CATCH and located(text, job["match_any"])
    )
    rec["quotes_verbatim"], rec["quotes_total"] = citation_check(text, ctx.plugin_dir)
    rec["response_sha"] = response_hash(text) if text.strip() else None
    rec["agent_sha256"] = ctx.agent_hash
    rec["finished_utc"] = dt.datetime.now(dt.UTC).isoformat(timespec="seconds")
    atomic_write(ctx.out_dir / "transcripts" / f"{job['key']}.jsonl", stdout or "")
    atomic_write(ctx.out_dir / "runs" / f"{job['key']}.json", json.dumps(rec, indent=1))
    return rec


def execute(jobs: list[dict], ctx: Ctx) -> tuple[list[dict], str | None]:
    """Run jobs (resuming completed keys). Returns (records for these jobs, abort reason)."""
    existing = load_records(ctx.out_dir / "runs")
    done = {k: r for k, r in existing.items() if r.get("outcome") and r["outcome"] != ERROR}
    pending = [j for j in jobs if j["key"] not in done]
    if len(jobs) != len(pending):
        ctx.log(f"resume: {len(jobs) - len(pending)} of {len(jobs)} runs already complete in {ctx.out_dir}")
    records = {j["key"]: done[j["key"]] for j in jobs if j["key"] in done}
    abort = None
    consecutive_errors = 0
    workers = max(1, ctx.args.workers)
    in_flight: dict[cf.Future, dict] = {}
    queue = list(pending)
    with cf.ThreadPoolExecutor(max_workers=workers) as pool:
        try:
            while (queue or in_flight) :
                while queue and not abort and len(in_flight) < workers:
                    projected = ctx.spent + (len(in_flight) + 1) * ctx.est_cost
                    if projected > ctx.args.max_cost:
                        abort = (
                            f"COST CEILING: spent ${ctx.spent:.2f}; the next run (est "
                            f"${ctx.est_cost:.2f}) would pass --max-cost ${ctx.args.max_cost:.2f}"
                        )
                        break
                    job = queue.pop(0)
                    in_flight[pool.submit(run_one, job, ctx)] = job
                if not in_flight:
                    break
                finished, _ = cf.wait(in_flight, return_when=cf.FIRST_COMPLETED)
                for fut in finished:
                    job = in_flight.pop(fut)
                    try:
                        rec = fut.result()
                    except Exception as exc:  # a harness bug must stop the run, loudly
                        abort = abort or f"HARNESS EXCEPTION on {job['key']}: {exc!r}"
                        continue
                    records[job["key"]] = rec
                    cost = rec.get("cost_usd")
                    ctx.spent += cost if isinstance(cost, (int, float)) and rec.get("cost_known") else (
                        ctx.est_cost if rec["outcome"] != ERROR else (cost or 0.0)
                    )
                    ctx.log(
                        f"{job['case']:<36} {job['variant']:<6} rep {job['rep']}: {rec['outcome']:<11}"
                        f" (expected {job['expected']}){'  located' if rec['located'] else ''}"
                        f"  {rec.get('seconds', '?')}s"
                        + (f"  ${cost:.4f}" if isinstance(cost, (int, float)) else "  $?")
                        + f"  [spent ${ctx.spent:.2f}/{ctx.args.max_cost:.2f}]"
                        + (f"  [{rec['error_kind']}: {(rec.get('error') or '')[:140]}]" if rec.get("error_kind") else "")
                    )
                    consecutive_errors = consecutive_errors + 1 if rec["outcome"] == ERROR else 0
                    if rec.get("fatal") and not abort:
                        abort = f"FATAL {rec['error_kind'].upper()}: {rec.get('error')}"
                    elif consecutive_errors >= ctx.args.max_consecutive_errors and not abort:
                        abort = (
                            f"{consecutive_errors} consecutive ERROR runs (last: {rec['error_kind']}): "
                            "stopping rather than burning the remaining cases"
                        )
                    elif ctx.spent >= ctx.args.max_cost and not abort:
                        abort = f"COST CEILING: spent ${ctx.spent:.2f} >= --max-cost ${ctx.args.max_cost:.2f}"
                if abort:
                    queue.clear()
        except KeyboardInterrupt:
            abort = "INTERRUPTED by user"
            queue.clear()
            for fut in in_flight:
                fut.cancel()
    return [records[j["key"]] for j in jobs if j["key"] in records], abort


# ---------------------------------------------------------------------------
# Summary, sanity checks, run verdict
# ---------------------------------------------------------------------------


def sanity_flags(records: list[dict]) -> list[tuple[str, bool]]:
    """(message, severe). Severe flags make the run INCONCLUSIVE (harness-defect-first)."""
    flags = []
    valid = [r for r in records if r["outcome"] != ERROR]
    verdicts = {r["verdict"] for r in valid}
    variants = {r["variant"] for r in valid}
    cases = {r["case"] for r in valid}
    if len(valid) >= 4 and len(cases) >= 2 and len(verdicts) == 1:
        both = variants == {"defect", "clean"}
        flags.append(
            (
                f"UNIFORM VERDICT: all {len(valid)} valid runs across {len(cases)} cases answered "
                f"{verdicts.pop()} -- harness suspect until explained",
                both and len(valid) >= 6,
            )
        )
    by_hash: dict[str, set] = {}
    for r in valid:
        if r.get("response_sha"):
            by_hash.setdefault(r["response_sha"], set()).add((r["case"], r["variant"]))
    for h, cells in by_hash.items():
        if len(cells) > 1:
            flags.append(
                (f"IDENTICAL REPLY {h} for different case/variants {sorted(cells)} -- harness suspect", True)
            )
    for cid in sorted(cases):
        rs = [r for r in valid if r["case"] == cid]
        d = {r["response_sha"] for r in rs if r["variant"] == "defect"}
        c = {r["response_sha"] for r in rs if r["variant"] == "clean"}
        if d and c and d & c:
            flags.append((f"IDENTICAL REPLY to defect and clean twin of {cid} -- harness suspect", True))
    return flags


def summarize(records: list[dict], planned: int, stamp: dict, abort: str | None, args) -> tuple[str, str, int]:
    """(markdown report, run verdict, exit code)."""
    lines = [
        "# Reviewer planted-defect eval",
        "",
        "## Reproducibility stamp",
        "",
        "```json",
        json.dumps(stamp, indent=1),
        "```",
        "",
        "## Per case",
        "",
        "Outcome counts are k/n over NON-ERROR runs; ERROR runs are never in a denominator. "
        f"A per-case call needs >= {args.min_reps_for_call} valid reps.",
        "",
        "| case | defect class | defect: catch / located / valid | defect call | clean: false alarm / valid | clean call | NEEDS-INFO | ERROR (kinds) |",
        "|---|---|---|---|---|---|---|---|",
    ]
    cases = list(dict.fromkeys(r["case"] for r in records))
    tot = {"d_n": 0, "d_c": 0, "d_l": 0, "c_n": 0, "c_fa": 0, "ni": 0, "err": 0}
    d_groups, l_groups, c_groups = [], [], []
    all_errored_cells = []

    def call(k: int, n: int, good: str, bad: str) -> str:
        if n < args.min_reps_for_call:
            return f"no call (n={n}<{args.min_reps_for_call})"
        if k == n:
            return good
        if k == 0:
            return bad
        return f"MIXED {k}/{n}"

    for cid in cases:
        rs = [r for r in records if r["case"] == cid]
        d_all = [r for r in rs if r["variant"] == "defect"]
        c_all = [r for r in rs if r["variant"] == "clean"]
        d = [r for r in d_all if r["outcome"] != ERROR]
        c = [r for r in c_all if r["outcome"] != ERROR]
        for name, cell_all, cell_valid in (("defect", d_all, d), ("clean", c_all, c)):
            if cell_all and not cell_valid:
                all_errored_cells.append(f"{cid}/{name}")
        d_c = sum(r["outcome"] == CATCH for r in d)
        d_l = sum(bool(r["located"]) for r in d)
        c_fa = sum(r["outcome"] == FALSE_ALARM for r in c)
        ni = sum(r["outcome"] == NEEDS_INFO for r in rs)
        errs = [r for r in rs if r["outcome"] == ERROR]
        kinds = ", ".join(f"{k}x{sum(r.get('error_kind') == k for r in errs)}" for k in sorted({r.get("error_kind") for r in errs}))
        tot["d_n"] += len(d)
        tot["d_c"] += d_c
        tot["d_l"] += d_l
        tot["c_n"] += len(c)
        tot["c_fa"] += c_fa
        tot["ni"] += ni
        tot["err"] += len(errs)
        d_groups.append((d_c, len(d)))
        l_groups.append((d_l, len(d)))
        c_groups.append((c_fa, len(c)))
        lines.append(
            f"| {cid} | {rs[0]['defect_class']} | "
            f"{f'{d_c} / {d_l} / {len(d)}' if d_all else '-'} | "
            f"{call(d_c, len(d), 'CAUGHT', 'MISSED') if d_all else '-'} | "
            f"{f'{c_fa} / {len(c)}' if c_all else '-'} | "
            f"{call(len(c) - c_fa, len(c), 'CLEAN', 'FALSE ALARM') if c_all else '-'} | "
            f"{ni} | {len(errs)}{f' ({kinds})' if kinds else ''} |"
        )

    n_total = len(records)
    err_share = tot["err"] / n_total if n_total else 1.0
    lines += [
        "",
        "## Pooled (valid runs only)",
        "",
        f"- catch rate: {tot['d_c']}/{tot['d_n']} (Wilson 95% {fmt_ci(wilson(tot['d_c'], tot['d_n']))}; "
        f"case-bootstrap 95% {fmt_ci(cluster_bootstrap(d_groups))})",
        f"- located catch: {tot['d_l']}/{tot['d_n']} (Wilson 95% {fmt_ci(wilson(tot['d_l'], tot['d_n']))}; "
        f"case-bootstrap 95% {fmt_ci(cluster_bootstrap(l_groups))})",
        f"- false-alarm rate: {tot['c_fa']}/{tot['c_n']} (Wilson 95% {fmt_ci(wilson(tot['c_fa'], tot['c_n']))}; "
        f"case-bootstrap 95% {fmt_ci(cluster_bootstrap(c_groups))})",
        f"- NEEDS-INFO: {tot['ni']} (in the denominators: a valid answer that is neither a catch nor a false alarm)",
        f"- ERROR: {tot['err']}/{n_total} runs ({err_share:.0%}); planned {planned}, recorded {n_total}",
        f"- cited SKILL.md quotes found verbatim: "
        f"{sum(r.get('quotes_verbatim', 0) for r in records)}/{sum(r.get('quotes_total', 0) for r in records)}",
        f"- spend: ${sum((r.get('cost_usd') or 0) for r in records):.2f} recorded"
        + (f" ({sum(not r.get('cost_known', True) for r in records)} runs with unknown cost)" if any(not r.get("cost_known", True) for r in records) else ""),
        f"- models used: {sorted({m for r in records for m in (r.get('models_used') or [])})}",
        "",
        "Wilson treats runs as independent; reps of one case are not, so the case-bootstrap "
        "interval is the honest one. 3-rep cells swing by up to 2 runs on identical input: read "
        "MIXED as unstable, not as a rate.",
    ]

    reasons = []
    if abort:
        reasons.append(f"ABORTED: {abort}")
    if n_total < planned:
        reasons.append(f"{planned - n_total} planned runs missing")
    if n_total and err_share > args.max_error_share:
        reasons.append(f"error share {err_share:.0%} > {args.max_error_share:.0%}")
    if all_errored_cells:
        reasons.append(f"every rep errored in: {', '.join(all_errored_cells)}")
    flags = sanity_flags(records)
    for msg, severe in flags:
        if severe:
            reasons.append(msg)
    lines += ["", "## Harness sanity"]
    lines += [f"- {'SEVERE' if s else 'warn'}: {m}" for m, s in flags] or ["- no uniform-outcome or identical-reply flags"]

    if abort:
        verdict, code = "ABORTED", EXIT_ABORTED
    elif reasons:
        verdict, code = "INCONCLUSIVE", EXIT_INCONCLUSIVE
    else:
        verdict, code = "COMPLETE", EXIT_OK
    lines += ["", f"## RUN VERDICT: {verdict}"]
    lines += [f"- {r}" for r in reasons] or ["- all planned runs recorded, error share within bounds, no harness flags"]
    if verdict != "COMPLETE":
        lines.append("- DO NOT CITE the pooled rates above: they are partial or suspect.")
    return "\n".join(lines) + "\n", verdict, code


# ---------------------------------------------------------------------------
# Preflight controls
# ---------------------------------------------------------------------------


def preflight_fingerprint(ctx: Ctx, control: str) -> str:
    return _sha(json.dumps([ctx.agent_hash, ctx.config_hash, ctx.args.model, control]))


def check_preflight(recs: list[dict]) -> list[str]:
    """Problems with the control runs; empty = controls behaved."""
    problems = []
    by = {r["variant"]: r for r in recs}
    d, c = by.get("defect"), by.get("clean")
    if d is None or c is None:
        return ["control runs missing"]
    for r in (d, c):
        if r["outcome"] == ERROR:
            problems.append(f"{r['variant']} control ERROR ({r.get('error_kind')}): {(r.get('error') or '')[:300]}")
    if d["outcome"] != ERROR and d["outcome"] != CATCH:
        problems.append(f"positive control not caught: defect twin answered {d.get('verdict')}")
    elif d["outcome"] == CATCH and not d["located"]:
        problems.append("positive control REJECTed but the findings do not name the planted defect")
    if c["outcome"] != ERROR and c["outcome"] != APPROVE_OK:
        problems.append(f"negative control not approved: clean twin answered {c.get('verdict')}")
    if d.get("response_sha") and d.get("response_sha") == c.get("response_sha"):
        problems.append("defect and clean controls returned identical replies")
    return problems


def run_preflight(case: dict, ctx: Ctx) -> tuple[bool, list[dict], str | None]:
    ctx.log(f"PREFLIGHT: positive + negative control on {case['id']} (1 defect run, 1 clean run)")
    jobs = make_jobs([case], ("defect", "clean"), 1, ctx, prefix="preflight__")
    recs, abort = execute(jobs, ctx)
    problems = [f"aborted: {abort}"] if abort else check_preflight(recs)
    passed = not problems
    atomic_write(
        ctx.out_dir / "preflight.json",
        json.dumps(
            {
                "passed": passed,
                "problems": problems,
                "control_case": case["id"],
                "fingerprint": preflight_fingerprint(ctx, case["id"]),
                "agent_sha256": ctx.agent_hash,
                "runs": [
                    {k: r.get(k) for k in ("key", "variant", "outcome", "verdict", "located", "error_kind", "cost_usd", "seconds", "models_used")}
                    for r in recs
                ],
                "finished_utc": dt.datetime.now(dt.UTC).isoformat(timespec="seconds"),
            },
            indent=1,
        ),
    )
    ctx.log("PREFLIGHT " + ("PASS" if passed else "FAIL"))
    for p in problems:
        ctx.log(f"  - {p}")
    return passed, recs, abort


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--dry-run", action="store_true", help="validate case files offline and exit")
    ap.add_argument("--preflight", action="store_true", help="run ONLY the 2 control runs and exit")
    ap.add_argument("--skip-preflight", action="store_true", help="full run without controls (stamped)")
    ap.add_argument("--control-case", default=DEFAULT_CONTROL_CASE, help="case id used as the control pair")
    ap.add_argument("--cases-dir", default=str(CASES_DIR))
    ap.add_argument("--case", action="append", help="case id to run (repeatable); default all")
    ap.add_argument("--variant", choices=("defect", "clean", "both"), default="both")
    ap.add_argument("--n", type=int, default=3, help="repeated runs per case variant (default 3)")
    ap.add_argument("--agent", default="rhiza-forecasting:reviewer")
    ap.add_argument("--plugin-dir", type=Path, default=REPO, help="plugin root (default: repo)")
    ap.add_argument("--model", default=None, help="override the agent's pinned model")
    ap.add_argument("--timeout", type=int, default=600, help="seconds per run")
    ap.add_argument("--retries", type=int, default=2, help="retries for transient API errors (max 2)")
    ap.add_argument("--backoff", type=float, default=20.0, help="first retry wait, seconds (x3 per retry)")
    ap.add_argument("--max-cost", type=float, default=35.0, help="USD ceiling for this invocation")
    ap.add_argument("--max-run-cost", type=float, default=3.0, help="USD cap per run (claude --max-budget-usd)")
    ap.add_argument("--est-cost-per-run", type=float, default=0.45, help="USD estimate before measurements exist")
    ap.add_argument("--est-seconds-per-run", type=float, default=150.0)
    ap.add_argument("--max-error-share", type=float, default=0.10)
    ap.add_argument("--max-consecutive-errors", type=int, default=4)
    ap.add_argument("--min-reps-for-call", type=int, default=3)
    ap.add_argument("--workers", type=int, default=1, help="parallel CLI runs (results written atomically)")
    ap.add_argument("--allow-dirty", action="store_true", help="run with uncommitted changes under evals/agents/skills")
    ap.add_argument(
        "--no-inline-skills",
        action="store_true",
        help="do not paste the named SKILL.md texts; the reviewer must load them itself",
    )
    ap.add_argument("--out", default=None, help="results directory; reuse it to RESUME (default results/<timestamp>)")
    return ap


def main(argv=None, *, invoke=None, sleep=None, stamp_fn=None, dirty_fn=None, stage_fn=None, log=print) -> int:
    args = build_parser().parse_args(argv)
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="replace")

    cases_all = load_cases(Path(args.cases_dir))
    if args.dry_run:
        return dry_run(cases_all, REPO)
    if dry_run(cases_all, REPO, quiet=True) != 0:
        log("refusing to run: fix the case files first (see --dry-run)")
        return EXIT_REFUSED
    if args.n < 1 or args.retries < 0 or args.retries > 2 or args.workers < 1:
        log("--n >= 1, 0 <= --retries <= 2, --workers >= 1")
        return EXIT_USAGE
    if args.preflight and args.skip_preflight:
        log("--preflight and --skip-preflight are contradictory")
        return EXIT_USAGE
    by_id = {c["id"]: c for _, c in cases_all}
    if args.control_case not in by_id:
        log(f"unknown --control-case {args.control_case!r}")
        return EXIT_USAGE
    cases = [c for _, c in cases_all]
    if args.case:
        wanted = set(args.case)
        missing = wanted - set(by_id)
        if missing:
            log(f"unknown case id(s): {sorted(missing)}")
            return EXIT_USAGE
        cases = [c for c in cases if c["id"] in wanted]

    args.plugin_dir = args.plugin_dir.resolve()
    dirty = (dirty_fn or git_dirty)(args.plugin_dir)
    if dirty is None and not args.allow_dirty:
        log("refusing to run: could not read git status (pass --allow-dirty to override; stamped)")
        return EXIT_REFUSED
    if dirty and not args.allow_dirty:
        log("refusing to run: uncommitted changes under " + "/".join(DIRTY_GUARD_PATHS) + ":")
        for ln in dirty:
            log(f"  {ln}")
        log("commit them (results must be tied to a git sha) or pass --allow-dirty")
        return EXIT_REFUSED

    afile = agent_file(args.plugin_dir, args.agent)
    if not afile.is_file():
        log(f"agent file not found: {afile}")
        return EXIT_REFUSED
    agent_text = afile.read_text(encoding="utf-8")
    agent_hash = _sha(afile.read_bytes())
    skills_hash = _sha(
        "".join(
            _sha(p.read_bytes())
            for p in sorted((args.plugin_dir / "skills").glob("*/SKILL.md"))
        )
    )
    config_hash = _sha(json.dumps([skills_hash, args.no_inline_skills, args.agent]))
    out_dir = Path(args.out) if args.out else HERE / "results" / dt.datetime.now().strftime("%Y%m%dT%H%M%S")
    out_dir.mkdir(parents=True, exist_ok=True)
    stamp = (stamp_fn or collect_stamp)(args, agent_hash, config_hash)
    stamp["preflight"] = "skipped (--skip-preflight)" if args.skip_preflight else "required"

    with tempfile.TemporaryDirectory(prefix="reviewer-plugin-") as stage_root:
        staged = (stage_fn or stage_plugin)(args.plugin_dir, Path(stage_root))
        ctx = Ctx(
            args=args,
            out_dir=out_dir,
            cmd=build_cmd(args, staged) if invoke is None else ["claude", "-p", "--plugin-dir", str(staged)],
            plugin_dir=args.plugin_dir,
            agent_hash=agent_hash,
            config_hash=config_hash,
            model_pin=agent_model_pin(agent_text),
            invoke=invoke or invoke_cli,
            sleep=sleep or time.sleep,
            est_cost=args.est_cost_per_run,
            log=log,
        )
        prior = load_records(out_dir / "runs")
        measured = [r["cost_usd"] for r in prior.values() if r.get("cost_known") and r.get("outcome") != ERROR and r.get("cost_usd")]
        if measured:
            ctx.est_cost = max(args.est_cost_per_run * 0.5, sum(measured) / len(measured))
        foreign = [k for k, r in prior.items() if r.get("agent_sha256") != agent_hash]
        if foreign:
            log(f"note: {len(foreign)} runs in {out_dir} are from a different agent file; they are kept on disk and EXCLUDED from this run's results")

        control = by_id[args.control_case]
        pf_path = out_dir / "preflight.json"
        need_preflight = not args.skip_preflight
        if not args.preflight and need_preflight and pf_path.is_file():
            try:
                pf = json.loads(pf_path.read_text(encoding="utf-8"))
            except json.JSONDecodeError:
                pf = {}
            if pf.get("passed") and pf.get("fingerprint") == preflight_fingerprint(ctx, control["id"]):
                log(f"preflight: reusing PASS from {pf_path}")
                need_preflight = False
                stamp["preflight"] = f"PASS (reused, {pf.get('finished_utc')})"

        variants = ("defect", "clean") if args.variant == "both" else (args.variant,)
        main_jobs = [] if args.preflight else make_jobs(cases, variants, args.n, ctx)
        n_pre = 2 if need_preflight else 0
        pending_main = [j for j in main_jobs if j["key"] not in {k for k, r in prior.items() if r.get("outcome") not in (None, ERROR)}]
        est_runs = n_pre + len(pending_main)
        est = est_runs * ctx.est_cost
        log(
            f"plan: {n_pre} preflight + {len(pending_main)} pending main runs "
            f"(of {len(main_jobs)} planned); est ${est:.2f} at ${ctx.est_cost:.2f}/run, "
            f"~{est_runs * args.est_seconds_per_run / args.workers / 60:.0f} min with {args.workers} worker(s); "
            f"ceiling --max-cost ${args.max_cost:.2f}; results -> {out_dir}"
        )
        if est > args.max_cost:
            log(f"refusing to start: estimate ${est:.2f} exceeds --max-cost ${args.max_cost:.2f} (raise it or narrow the run)")
            return EXIT_ABORTED
        atomic_write(out_dir / "manifest.json", json.dumps({"stamp": stamp, "argv": list(argv or sys.argv[1:])}, indent=1))

        if need_preflight:
            passed, pre_recs, abort = run_preflight(control, ctx)
            if args.preflight:
                return EXIT_OK if passed else (EXIT_ABORTED if abort else EXIT_PREFLIGHT)
            if not passed:
                log("full run NOT started: the controls did not behave (see preflight.json)")
                return EXIT_ABORTED if abort else EXIT_PREFLIGHT
            stamp["preflight"] = "PASS (this invocation)"
            pre_costs = [r["cost_usd"] for r in pre_recs if r.get("cost_known") and r.get("cost_usd")]
            if pre_costs:
                ctx.est_cost = max(ctx.est_cost, sum(pre_costs) / len(pre_costs))
                log(f"estimate updated from the controls: ${ctx.est_cost:.2f}/run")
        elif args.preflight:
            log("preflight: a passing preflight for this agent/config already exists; nothing to do")
            return EXIT_OK

        records, abort = execute(main_jobs, ctx)
        report, verdict, code = summarize(records, len(main_jobs), stamp, abort, args)
        atomic_write(out_dir / "summary.md", report)
        atomic_write(out_dir / "runs.jsonl", "".join(json.dumps(r) + "\n" for r in records))
        log("\n" + report)
        log(f"RUN VERDICT: {verdict} (exit {code}); per-run records: {out_dir / 'runs'}")
        return code


if __name__ == "__main__":
    sys.exit(main())
