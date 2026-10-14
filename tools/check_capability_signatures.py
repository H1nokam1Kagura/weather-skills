# /// script
# requires-python = ">=3.10"
# dependencies = []
# ///
"""Static check: a D4 signature must still cover the code its record names.

Owner ruling D4 (CR-09) declares a d4_gated capability only on a signed record: reviewer,
reviewed_at and the reviewed_commit the independent review read. A signature says nothing
about code written after that commit, so this check (D4 re-review G-1) fails when any producer
file a signed record names changed between its reviewed_commit and HEAD:

    git diff --name-only <reviewed_commit> HEAD -- <producer files>

A record carrying a non-empty rereview_required is exempt here because the gate already treats
it as undeclared (WeatherAdapter.d4_declared); it is listed, never silently passed. The check
fails closed: a reviewed_commit the clone does not hold (a shallow CI checkout, or a commit
that was never pushed), a producer file missing at HEAD, a producer entry with no file, or a
directory that is not a git work tree is a failure with the reason, never a pass.

Usage:
    python tools/check_capability_signatures.py [adapter_support/CAPABILITY-PRODUCERS.json]
"""

import json
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
PRODUCERS_FILE = REPO_ROOT / "adapter_support" / "CAPABILITY-PRODUCERS.json"


def rereview_flagged(record):
    """A record whose rereview_required carries anything (a non-blank reason, or any other
    truthy value) awaits re-review; only an absent, null or blank flag is clear."""
    flag = record.get("rereview_required")
    return bool(flag.strip()) if isinstance(flag, str) else bool(flag)


def producer_files(record):
    """The repo-relative code files a record's producers name ("file" of every entry), sorted;
    an entry without a usable file is returned separately so the caller can fail on it."""
    files, unusable = set(), []
    for index, producer in enumerate(record.get("producers") or []):
        path = producer.get("file") if isinstance(producer, dict) else None
        if isinstance(path, str) and path.strip():
            files.add(path.strip().replace("\\", "/"))
        else:
            unusable.append(index)
    return sorted(files), unusable


def _git(repo, *args):
    return subprocess.run(
        ["git", "-C", str(repo), *args], capture_output=True, text=True, check=False
    )


def check(producers, repo=REPO_ROOT, head="HEAD"):
    """Return (failures, flagged): failures as [{capability, reviewed_commit, files, reason}]
    and flagged as [{capability, rereview_required}] for records the gate treats as undeclared."""
    failures, flagged = [], []
    inside = _git(repo, "rev-parse", "--is-inside-work-tree")
    if inside.returncode != 0 or inside.stdout.strip() != "true":
        return [
            {
                "capability": "*",
                "reviewed_commit": None,
                "files": [],
                "reason": f"not a git work tree: {repo} ({inside.stderr.strip()})",
            }
        ], flagged
    shallow = _git(repo, "rev-parse", "--is-shallow-repository").stdout.strip() == "true"
    for capability, record in sorted(producers.get("capabilities", {}).items()):
        if not isinstance(record, dict) or record.get("d4_gated") is not True:
            continue
        commit = record.get("reviewed_commit")
        if not (isinstance(commit, str) and commit.strip()):
            continue  # unsigned: the gate already refuses it
        commit = commit.strip()
        if rereview_flagged(record):
            flagged.append(
                {"capability": capability, "rereview_required": record["rereview_required"]}
            )
            continue
        files, unusable = producer_files(record)

        def fail(reason, paths=files, commit=commit, capability=capability):
            failures.append(
                {
                    "capability": capability,
                    "reviewed_commit": commit,
                    "files": list(paths),
                    "reason": reason,
                }
            )

        if unusable or not files:
            fail(f"producer entries without a file: {unusable or 'no producers'}", [])
            continue
        if _git(repo, "cat-file", "-e", f"{commit}^{{commit}}").returncode != 0:
            hint = (
                " (this clone is shallow: check out with fetch-depth: 0)"
                if shallow
                else " (commit absent from this clone)"
            )
            fail(f"reviewed_commit {commit} is not available{hint}; cannot verify the signature")
            continue
        missing = [f for f in files if _git(repo, "cat-file", "-e", f"{head}:{f}").returncode != 0]
        if missing:
            fail("producer files absent at HEAD (renamed or deleted since review)", missing)
            continue
        diff = _git(repo, "diff", "--name-only", commit, head, "--", *files)
        if diff.returncode != 0:
            fail(f"git diff failed: {diff.stderr.strip()}")
            continue
        changed = sorted(line.strip() for line in diff.stdout.splitlines() if line.strip())
        if changed:
            fail("producer files changed since reviewed_commit, record not flagged", changed)
    return failures, flagged


def report(failures, flagged):
    lines = []
    for item in failures:
        commit = (item["reviewed_commit"] or "")[:12]
        files = ", ".join(item["files"]) or "-"
        lines.append(
            f"FAIL {item['capability']} (reviewed_commit {commit}): {item['reason']}: {files}"
        )
    for item in flagged:
        lines.append(
            f"FLAGGED {item['capability']} (undeclared until re-signed): {item['rereview_required']}"
        )
    if failures:
        lines.append(
            f"{len(failures)} signed record(s) no longer cover their producer code: re-review and"
            " re-sign them, or set rereview_required (which makes the gate treat them as undeclared)."
        )
    return "\n".join(lines)


def main(argv):
    path = Path(argv[1]) if len(argv) > 1 else PRODUCERS_FILE
    producers = json.loads(path.read_text(encoding="utf-8"))
    failures, flagged = check(producers)
    text = report(failures, flagged)
    if text:
        print(text)
    if not failures:
        print("OK: every signed D4 record covers its producer files at HEAD")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
