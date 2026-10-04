"""Observing wrapper for the NESTED `claude` calls the human-boundary agent makes.

The agent compiles each request three times with `claude -p --system-prompt <compile prompt>`
from inside its own Bash tool. Those calls are invisible to the harness: if they fail, the agent
falls back to one reading in its own context ("sampling": "single") and the card still looks
fine. The harness therefore puts a `claude` launcher first on the agent session's PATH that runs
this file, which:

  * runs the REAL claude (absolute path in INTAKE_REAL_CLAUDE) with the same arguments;
  * for a print-mode call that did not choose an output format, asks for `--output-format json`
    and writes ONLY the `result` text to stdout, so the caller sees what text mode would print
    while the harness gets the model id and cost of every nested call;
  * writes one JSON record per call into INTAKE_NESTED_DIR (one file per call: three calls run
    in parallel, so no shared file).

The record never stores the system prompt itself, only its sha256 and length; the harness
compares that hash with the plugin's compile prompt so a call made with a different prompt is
visible. Exit code and stderr are passed through unchanged.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import subprocess
import sys
import time
import uuid
from pathlib import Path


def norm_prompt(text: str) -> str:
    """`P="$(cat file)"` strips trailing newlines and Windows files may carry CRLF."""
    return text.replace("\r\n", "\n").rstrip("\n")


def prompt_sha(text: str) -> str:
    return hashlib.sha256(norm_prompt(text).encode("utf-8")).hexdigest()


def parse_goal(text: str):
    """Same tolerance as goal_check.py: whole text as JSON, else the outermost {...}."""
    try:
        d = json.loads(text)
    except (json.JSONDecodeError, TypeError):
        m = re.search(r"\{.*\}", text or "", re.S)
        try:
            d = json.loads(m.group(0)) if m else None
        except json.JSONDecodeError:
            d = None
    return d if isinstance(d, dict) else None


def _system_prompt(args: list[str]) -> str | None:
    for i, a in enumerate(args):
        if a == "--system-prompt" and i + 1 < len(args):
            return args[i + 1]
        if a.startswith("--system-prompt="):
            return a.split("=", 1)[1]
        if a == "--system-prompt-file" and i + 1 < len(args):
            try:
                return Path(args[i + 1]).read_text(encoding="utf-8")
            except OSError:
                return None
    return None


def _redacted_args(args: list[str]) -> list[str]:
    out, skip = [], False
    for i, a in enumerate(args):
        if skip:
            skip = False
            continue
        if a == "--system-prompt" and i + 1 < len(args):
            out += [a, f"<sha256:{prompt_sha(args[i + 1])[:16]} len={len(args[i + 1])}>"]
            skip = True
        else:
            out.append(a if len(a) < 400 else a[:400] + "...")
    return out


def _real_claude() -> str:
    real = os.environ.get("INTAKE_REAL_CLAUDE")
    if not real:
        sys.stderr.write("claude_shim: INTAKE_REAL_CLAUDE is not set; refusing to guess\n")
        sys.exit(127)
    return real


def main(argv: list[str]) -> int:
    real = _real_claude()
    log_dir = os.environ.get("INTAKE_NESTED_DIR")
    is_print = "-p" in argv or "--print" in argv
    chose_format = any(a == "--output-format" or a.startswith("--output-format=") for a in argv)
    inject = is_print and not chose_format and os.environ.get("INTAKE_SHIM_PASSTHROUGH") != "1"
    cmd = [real, *argv] + (["--output-format", "json"] if inject else [])
    t0 = time.time()
    p = subprocess.run(cmd, capture_output=True)
    secs = round(time.time() - t0, 2)
    out_b, err_b, rc = p.stdout, p.stderr, p.returncode
    rec = {
        "argv": _redacted_args(argv),
        "rc": rc,
        "secs": secs,
        "injected_json": inject,
        "stderr_head": err_b.decode("utf-8", "replace")[:600],
    }
    sp = _system_prompt(argv)
    rec["system_prompt_sha256"] = prompt_sha(sp) if sp is not None else None
    m = [argv[i + 1] for i, a in enumerate(argv[:-1]) if a == "--model"]
    rec["model_arg"] = m[-1] if m else None
    text_out = out_b
    if inject:
        try:
            env = json.loads(out_b.decode("utf-8"))
        except (json.JSONDecodeError, UnicodeDecodeError):
            env = None
        if isinstance(env, dict) and "result" in env:
            result = env.get("result") or ""
            rec.update(
                envelope_ok=True,
                is_error=bool(env.get("is_error")),
                subtype=env.get("subtype"),
                cost_usd=env.get("total_cost_usd"),
                models=sorted((env.get("modelUsage") or {}).keys()),
            )
            text_out = (result + "\n").encode("utf-8")
            if env.get("is_error") and rc == 0:
                rc = 1
                rec["rc"] = rc
        else:
            rec.update(envelope_ok=False, is_error=True, cost_usd=None, models=[])
    text = text_out.decode("utf-8", "replace")
    rec["stdout_len"] = len(text)
    rec["stdout_head"] = text[:300]
    goal = parse_goal(text)
    rec["goal"] = goal
    rec["parsed_goal"] = goal is not None
    if log_dir:
        try:
            Path(log_dir).mkdir(parents=True, exist_ok=True)
            name = f"{time.time_ns()}-{uuid.uuid4().hex[:8]}.json"
            (Path(log_dir) / name).write_text(json.dumps(rec, ensure_ascii=False), "utf-8")
        except OSError as exc:  # never break the caller because logging failed; say so
            sys.stderr.write(f"claude_shim: could not write nested log: {exc}\n")
    sys.stdout.buffer.write(text_out)
    sys.stdout.buffer.flush()
    sys.stderr.buffer.write(err_b)
    sys.stderr.buffer.flush()
    return rc


def install(shim_dir: Path, python: str | None = None) -> Path:
    """Write `claude` (bash) and `claude.cmd` launchers into shim_dir; return the dir."""
    shim_dir.mkdir(parents=True, exist_ok=True)
    py = Path(python or sys.executable).as_posix()
    me = Path(__file__).resolve().as_posix()
    sh = shim_dir / "claude"
    sh.write_bytes(f'#!/usr/bin/env bash\nexec "{py}" "{me}" "$@"\n'.encode())
    try:
        sh.chmod(0o755)
    except OSError:
        pass
    (shim_dir / "claude.cmd").write_text(
        f'@"{Path(py)}" "{Path(me)}" %*\r\n', encoding="utf-8", newline=""
    )
    return shim_dir


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
