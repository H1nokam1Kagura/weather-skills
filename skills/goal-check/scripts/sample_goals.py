# /// script
# requires-python = ">=3.12,<3.13"
# dependencies = [
#   "weather-skills-core @ git+https://github.com/rhiza-research/weather-skills-core@dev",
# ]
# ///
"""Compile a request into N independent typed goals and check them, in ONE command.

Runs N headless `claude -p` compiles in parallel (same prompt file, no tools, no MCP, no
settings), then runs goal-check's evaluate() over the samples. One literal command, so an agent
running unattended is not blocked on per-call permission prompts. Before this script existed,
the human-boundary agent fell back to a single in-context compile and labelled the card
"single".

Sampling is reported, never assumed:
  sampling = "independent"  all N compiles returned output
  sampling = "degraded"     some failed (exit 5 unless --allow-degraded)
  sampling = "unavailable"  the claude CLI is missing or every compile failed (exit 4)
Otherwise the exit code is goal-check's: 0 resolved, 1 invalid, 3 needs a human answer.
"""

from __future__ import annotations

import hashlib
import importlib.util
import json
import shutil
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from weather_skills_core import UsageError, weather_skill

# Auto-populated by the version-bump CI workflow. Do not edit manually.
_SKILL_VERSION = "0.0.1"

HERE = Path(__file__).resolve().parent
PROMPT_FILE = HERE.parent / "references" / "compile_prompt_rhiza.txt"


def _goal_check():
    spec = importlib.util.spec_from_file_location("_gc_for_sampling", HERE / "goal_check.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _extract_json(text: str):
    """First JSON object in a compile's stdout, or None (an unparsable sample)."""
    dec = json.JSONDecoder()
    for i, ch in enumerate(text or ""):
        if ch == "{":
            try:
                obj, _ = dec.raw_decode(text[i:])
                return obj
            except json.JSONDecodeError:
                continue
    return None


def _compile(claude: str, model: str, prompt: str, request: str, timeout: int) -> dict:
    cmd = [
        claude,
        "-p",
        "--model",
        model,
        "--tools",
        "",
        "--strict-mcp-config",
        "--setting-sources",
        "",
        "--no-session-persistence",
        "--system-prompt",
        prompt,
        request,
    ]
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", timeout=timeout)
    except subprocess.TimeoutExpired:
        return {"ok": False, "error": f"timeout after {timeout}s", "goal": None}
    except OSError as exc:
        return {"ok": False, "error": f"cannot run claude: {exc}", "goal": None}
    if r.returncode != 0:
        return {
            "ok": False,
            "error": f"exit {r.returncode}: {r.stderr.strip()[-300:]}",
            "goal": None,
        }
    goal = _extract_json(r.stdout)
    # An unparsable reply is still a completed compile: goal-check scores it as an invalid sample
    # (redraw), which is visible, so it does not degrade the sampling.
    return {"ok": True, "error": None, "goal": goal}


@weather_skill(name="goal-check", version=_SKILL_VERSION, output=False)
@weather_skill.argument("--request", default=None, help="The user's request text.")
@weather_skill.argument(
    "--request-file", default=None, metavar="PATH", help="Read the request from a file."
)
@weather_skill.argument("--n", type=int, default=3, help="Independent compiles (2-3). Default 3.")
@weather_skill.argument("--model", default="sonnet", help="Compile model alias. Default sonnet.")
@weather_skill.argument(
    "--timeout", type=int, default=180, help="Seconds per compile. Default 180."
)
@weather_skill.argument(
    "--allow-degraded",
    action="store_true",
    help="Exit with goal-check's code even if some compiles failed (still reported).",
)
def sample_goals(request, request_file, n, model, timeout, allow_degraded, **kwargs):
    """Compile a request into N independent typed goals and check them, in one command."""
    if (request is None) == (request_file is None):
        raise UsageError("pass exactly one of --request or --request-file")
    if request_file is not None:
        try:
            request = Path(request_file).read_text(encoding="utf-8").strip()
        except OSError as exc:
            raise UsageError(f"cannot read --request-file: {exc}") from None
    if not 2 <= n <= 3:
        raise UsageError("--n must be 2 or 3 (goal-check compares at most 3 samples)")
    prompt = PROMPT_FILE.read_text(encoding="utf-8")
    prompt_sha = hashlib.sha256(prompt.encode("utf-8")).hexdigest()

    claude = shutil.which("claude")
    if claude is None:
        print(
            json.dumps(
                {
                    "sampling": "unavailable",
                    "samples_ok": 0,
                    "exit_code": 4,
                    "reason": "claude CLI not on PATH",
                },
                indent=1,
            )
        )
        sys.exit(4)

    with ThreadPoolExecutor(max_workers=n) as pool:
        runs = list(pool.map(lambda _: _compile(claude, model, prompt, request, timeout), range(n)))
    ok = [r for r in runs if r["ok"]]
    failures = [r["error"] for r in runs if not r["ok"]]
    if not ok:
        print(
            json.dumps(
                {
                    "sampling": "unavailable",
                    "samples_ok": 0,
                    "exit_code": 4,
                    "reason": "every compile failed",
                    "failures": failures,
                    "compile_prompt_sha256": prompt_sha,
                },
                indent=1,
            )
        )
        sys.exit(4)

    rep = _goal_check().evaluate([r["goal"] for r in ok], request)
    sampling = "independent" if len(ok) == n else "degraded"
    rep.update(
        {
            "sampling": sampling,
            "samples_requested": n,
            "samples_ok": len(ok),
            "compile_failures": failures,
            "compile_model": model,
            "compile_prompt_sha256": prompt_sha,
        }
    )
    code = rep["exit_code"]
    if sampling == "degraded" and not allow_degraded:
        rep["exit_code"] = code = 5
    print(json.dumps(rep, indent=1, ensure_ascii=False))
    sys.exit(code)


if __name__ == "__main__":
    sample_goals()
