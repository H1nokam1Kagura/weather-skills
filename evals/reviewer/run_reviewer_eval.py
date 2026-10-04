"""Planted-defect benchmark for the weather-skills reviewer agent.

Each case file in ``cases/`` holds one defect and its clean twin (the same
pipeline without the defect), the defect class, the expected verdicts, and the
SKILL.md clause(s) that make the defect a defect.

    # offline: validate every case file (schema, twins, premise quotes)
    uv run python evals/reviewer/run_reviewer_eval.py --dry-run

    # live: feed each case to the reviewer agent through the Claude Code CLI
    uv run python evals/reviewer/run_reviewer_eval.py --n 3
    uv run python evals/reviewer/run_reviewer_eval.py --case omitted-clip --variant defect --n 1

The live backend is headless Claude Code with this checkout loaded as a plugin:

    claude -p --agent rhiza-forecasting:reviewer --plugin-dir <repo> --output-format json ...

The prompt goes on stdin. Each run starts in an empty temporary directory with
file tools limited to Read/Grep/Glob/Skill and every permission prompt
auto-denied, so the reviewer cannot read the answer key in this directory.

Scoring, per case and overall:
  catch rate        defect runs answered REJECT
  located catch     defect runs answered REJECT whose FINDINGS match the case's
                    ``match_any`` patterns (the finding is about the planted defect)
  false-alarm rate  clean-twin runs answered REJECT
  NEEDS-INFO and unparseable replies are counted and reported separately; they
  are neither a catch nor a false alarm.
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import math
import re
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

import yaml

HERE = Path(__file__).resolve().parent
REPO = HERE.parents[1]
CASES_DIR = HERE / "cases"
VERDICTS = ("APPROVE", "REJECT", "NEEDS-INFO")
REQUIRED = ("id", "defect_class", "kind", "skills", "task", "context", "premise", "defect", "clean")
# Fields that are answer key or author notes: never sent to the reviewer.
HIDDEN = ("premise", "defect_class", "expected_verdict", "expected_step", "match_any", "note")
VERDICT_RE = re.compile(r"VERDICT:\s*\**\s*(APPROVE|REJECT|NEEDS-INFO)", re.IGNORECASE)


# ---------------------------------------------------------------------------
# Case loading and offline validation
# ---------------------------------------------------------------------------


def _norm(text: str) -> str:
    return " ".join(text.split())


def load_cases(cases_dir: Path) -> list[tuple[Path, dict]]:
    out = []
    for path in sorted(cases_dir.glob("*.yaml")):
        try:
            with path.open(encoding="utf-8") as fh:
                out.append((path, yaml.safe_load(fh)))
        except yaml.YAMLError as exc:
            out.append((path, f"YAML parse error: {exc}"))
    return out


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
    premise = case["premise"]
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
        if _norm(str(defect.get("submission", ""))) == _norm(str(clean.get("submission", ""))):
            errs.append("defect and clean submissions are identical (not a twin)")
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


def dry_run(cases: list[tuple[Path, dict]], repo: Path) -> int:
    problems, ids = [], {}
    for path, case in cases:
        problems += validate_case(path, case, repo)
        cid = case.get("id") if isinstance(case, dict) else None
        if cid in ids:
            problems.append(f"{path.name}: duplicate id {cid!r} (also {ids[cid]})")
        ids[cid] = path.name
    print(f"{'case':<36} {'kind':<5} {'defect class':<42} premise")
    for _path, case in cases:
        if not isinstance(case, dict):
            continue
        quotes = [p for p in case.get("premise", []) if isinstance(p, dict) and "quote" in p]
        files = ", ".join(sorted({p.get("file", "?").split("/")[1] for p in quotes}))
        print(
            f"{case.get('id', '?'):<36} {case.get('kind', '?'):<5} "
            f"{case.get('defect_class', '?'):<42} {files}"
        )
    n_cases = len(cases)
    print(f"\n{n_cases} cases ({2 * n_cases} variants: {n_cases} defect + {n_cases} clean twins)")
    if n_cases < 10:
        problems.append(f"only {n_cases} cases; the benchmark needs at least 10")
    if problems:
        print("\nINVALID:")
        for p in problems:
            print(f"  - {p}")
        return 1
    print("all case files valid; every premise quote found verbatim in its SKILL.md")
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
    return "\n".join(parts)


# ---------------------------------------------------------------------------
# Live backend: headless Claude Code
# ---------------------------------------------------------------------------


def claude_exe() -> str:
    exe = shutil.which("claude")
    if not exe:
        sys.exit("claude CLI not found on PATH; the live eval needs Claude Code installed")
    return exe


def run_reviewer(prompt: str, args) -> dict:
    cmd = [
        claude_exe(),
        "-p",
        "--agent",
        args.agent,
        "--plugin-dir",
        str(args.plugin_dir),
        "--output-format",
        "json",
        "--no-session-persistence",
        "--permission-prompts",
        "none",
        "--allowedTools",
        "Read Grep Glob Skill",
    ]
    if args.model:
        cmd += ["--model", args.model]
    started = time.monotonic()
    with tempfile.TemporaryDirectory(prefix="reviewer-eval-") as workdir:
        try:
            proc = subprocess.run(
                cmd,
                input=prompt,
                capture_output=True,
                text=True,
                encoding="utf-8",
                cwd=workdir,
                timeout=args.timeout,
            )
        except subprocess.TimeoutExpired:
            return {"error": f"timeout after {args.timeout}s", "seconds": args.timeout}
    seconds = round(time.monotonic() - started, 1)
    if proc.returncode != 0:
        return {
            "error": f"exit {proc.returncode}: {(proc.stderr or proc.stdout)[-800:]}",
            "seconds": seconds,
        }
    try:
        payload = json.loads(proc.stdout)
    except json.JSONDecodeError:
        return {"error": "non-JSON CLI output", "raw": proc.stdout[-2000:], "seconds": seconds}
    return {
        "text": payload.get("result") or "",
        "cost_usd": payload.get("total_cost_usd"),
        "is_error": payload.get("is_error"),
        "seconds": seconds,
    }


def parse_verdict(text: str) -> str:
    match = VERDICT_RE.search(text or "")
    return match.group(1).upper() if match else "UNPARSED"


def findings_text(text: str) -> str:
    """The FINDINGS section of a card (whole text if the section is not found)."""
    m = re.search(r"FINDINGS:(.*?)(?:\n\s*NEEDED:|\n\s*OPEN QUESTIONS:|\Z)", text or "", re.S)
    return m.group(1) if m else (text or "")


def located(text: str, patterns: list[str]) -> bool:
    body = findings_text(text)
    return any(re.search(p, body, re.IGNORECASE) for p in patterns)


RULE_RE = re.compile(r"rule:\s*skills/([\w-]+)/SKILL\.md:?(.*)")
QUOTE_RE = re.compile(r"\"([^\"]{8,})\"")


def citation_check(text: str, repo: Path) -> tuple[int, int]:
    """(verbatim, total) for the quoted SKILL.md lines on the card's ``rule:`` lines."""
    ok = total = 0
    for name, rest in RULE_RE.findall(findings_text(text)):
        path = repo / "skills" / name / "SKILL.md"
        source = _norm(path.read_text(encoding="utf-8")) if path.is_file() else ""
        for quote in QUOTE_RE.findall(rest):
            total += 1
            ok += bool(source) and _norm(quote) in source
    return ok, total


def wilson(k: int, n: int, z: float = 1.96) -> str:
    if n == 0:
        return "-"
    p = k / n
    denom = 1 + z * z / n
    centre = (p + z * z / (2 * n)) / denom
    half = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / denom
    return f"{max(0.0, centre - half):.2f}-{min(1.0, centre + half):.2f}"


def live(cases: list[tuple[Path, dict]], args) -> int:
    variants = ("defect", "clean") if args.variant == "both" else (args.variant,)
    out_dir = (
        Path(args.out)
        if args.out
        else HERE / "results" / dt.datetime.now().strftime("%Y%m%dT%H%M%S")
    )
    out_dir.mkdir(parents=True, exist_ok=True)
    records = []
    for _, case in cases:
        for variant in variants:
            prompt = build_prompt(case, variant, args.plugin_dir, not args.no_inline_skills)
            for rep in range(1, args.n + 1):
                res = run_reviewer(prompt, args)
                verdict = parse_verdict(res.get("text", "")) if "error" not in res else "ERROR"
                rec = {
                    "case": case["id"],
                    "defect_class": case["defect_class"],
                    "variant": variant,
                    "rep": rep,
                    "verdict": verdict,
                    "expected": case[variant]["expected_verdict"],
                    "located": (
                        variant == "defect"
                        and verdict == "REJECT"
                        and located(res.get("text", ""), case["defect"]["match_any"])
                    ),
                    **res,
                }
                rec["quotes_verbatim"], rec["quotes_total"] = citation_check(
                    res.get("text", ""), args.plugin_dir
                )
                records.append(rec)
                cost = rec.get("cost_usd")
                print(
                    f"{case['id']:<36} {variant:<6} rep {rep}: {verdict:<10}"
                    f" (expected {rec['expected']}){'  located' if rec['located'] else ''}"
                    f"  {rec.get('seconds', '?')}s"
                    + (f"  ${cost:.4f}" if isinstance(cost, (int, float)) else "")
                    + (f"  [{rec['error'][:120]}]" if "error" in rec else ""),
                    flush=True,
                )
                with (out_dir / "runs.jsonl").open("a", encoding="utf-8") as fh:
                    fh.write(json.dumps(rec) + "\n")
    report = summarize(records)
    (out_dir / "summary.md").write_text(report, encoding="utf-8")
    print("\n" + report)
    print(f"raw replies: {out_dir / 'runs.jsonl'}")
    return 0


def summarize(records: list[dict]) -> str:
    lines = [
        "| case | defect class | defect: REJECT / located / n | clean: REJECT / n | NEEDS-INFO | other |",
        "|---|---|---|---|---|---|",
    ]
    cases = list(dict.fromkeys(r["case"] for r in records))
    tot = {"d_n": 0, "d_rej": 0, "d_loc": 0, "c_n": 0, "c_rej": 0, "ni": 0, "other": 0}
    for cid in cases:
        rs = [r for r in records if r["case"] == cid]
        d = [r for r in rs if r["variant"] == "defect"]
        c = [r for r in rs if r["variant"] == "clean"]
        d_rej = sum(r["verdict"] == "REJECT" for r in d)
        d_loc = sum(bool(r["located"]) for r in d)
        c_rej = sum(r["verdict"] == "REJECT" for r in c)
        ni = sum(r["verdict"] == "NEEDS-INFO" for r in rs)
        other = sum(r["verdict"] in ("UNPARSED", "ERROR") for r in rs)
        tot["d_n"] += len(d)
        tot["d_rej"] += d_rej
        tot["d_loc"] += d_loc
        tot["c_n"] += len(c)
        tot["c_rej"] += c_rej
        tot["ni"] += ni
        tot["other"] += other
        lines.append(
            f"| {cid} | {rs[0]['defect_class']} | "
            f"{f'{d_rej} / {d_loc} / {len(d)}' if d else '-'} | "
            f"{f'{c_rej} / {len(c)}' if c else '-'} | {ni} | {other} |"
        )
    lines += [
        "",
        f"- catch rate: {tot['d_rej']}/{tot['d_n']} (95% CI {wilson(tot['d_rej'], tot['d_n'])})",
        f"- located catch: {tot['d_loc']}/{tot['d_n']} (95% CI {wilson(tot['d_loc'], tot['d_n'])})",
        f"- false-alarm rate: {tot['c_rej']}/{tot['c_n']} "
        f"(95% CI {wilson(tot['c_rej'], tot['c_n'])})",
        f"- NEEDS-INFO: {tot['ni']}; unparsed or errored: {tot['other']}",
        f"- cited SKILL.md quotes found verbatim: "
        f"{sum(r.get('quotes_verbatim', 0) for r in records)}/"
        f"{sum(r.get('quotes_total', 0) for r in records)}",
        "",
        "Runs are not independent across reps of one case: read per-case rows, not only the "
        "pooled intervals.",
    ]
    return "\n".join(lines) + "\n"


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--dry-run", action="store_true", help="validate case files offline and exit")
    ap.add_argument("--cases-dir", default=str(CASES_DIR))
    ap.add_argument("--case", action="append", help="case id to run (repeatable); default all")
    ap.add_argument("--variant", choices=("defect", "clean", "both"), default="both")
    ap.add_argument("--n", type=int, default=3, help="repeated runs per case variant (default 3)")
    ap.add_argument("--agent", default="rhiza-forecasting:reviewer")
    ap.add_argument("--plugin-dir", type=Path, default=REPO, help="plugin root (default: repo)")
    ap.add_argument("--model", default=None, help="override the agent's pinned model")
    ap.add_argument("--timeout", type=int, default=600, help="seconds per run")
    ap.add_argument(
        "--no-inline-skills",
        action="store_true",
        help="do not paste the named SKILL.md texts; the reviewer must load them itself",
    )
    ap.add_argument("--out", default=None, help="results directory (default results/<timestamp>)")
    args = ap.parse_args(argv)
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="replace")

    cases = load_cases(Path(args.cases_dir))
    if args.dry_run:
        return dry_run(cases, REPO)
    if dry_run(cases, REPO) != 0:
        print("refusing to run: fix the case files first", file=sys.stderr)
        return 1
    if args.case:
        wanted = set(args.case)
        cases = [(p, c) for p, c in cases if c["id"] in wanted]
        missing = wanted - {c["id"] for _, c in cases}
        if missing:
            print(f"unknown case id(s): {sorted(missing)}", file=sys.stderr)
            return 2
    if args.n < 1:
        print("--n must be >= 1", file=sys.stderr)
        return 2
    args.plugin_dir = args.plugin_dir.resolve()
    print()
    return live(cases, args)


if __name__ == "__main__":
    sys.exit(main())
