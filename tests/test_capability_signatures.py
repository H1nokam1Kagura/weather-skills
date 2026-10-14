"""D4 re-review G-1: a signature covers only the code its reviewed_commit held.

The committed-file test runs the signature check against this repository's history, so CI
fails whenever a signed record's producer files change without a re-review (or the
rereview_required flag, which makes the gate treat the record as undeclared).
"""

import copy
import json
import subprocess
import sys

import pytest
from frozen_v27 import frozen_root

# Explicit, test-only import location; no package bridge or Simkit modification.
sys.path.insert(0, str(frozen_root()))  # verified against FINAL-PINS by tests/frozen_v27.py

from adapter_support.weather_adapter import (  # noqa: E402 - explicit pinned test bootstrap
    WeatherAdapter,
    load_producers,
)
from tools import check_capability_signatures as sig  # noqa: E402


def git(repo, *args):
    out = subprocess.run(
        ["git", "-C", str(repo), *args], capture_output=True, text=True, check=True
    )
    return out.stdout.strip()


@pytest.fixture
def repo(tmp_path):
    root = tmp_path / "repo"
    root.mkdir()
    git(root, "init", "-q")
    git(root, "config", "user.email", "t@example.invalid")
    git(root, "config", "user.name", "t")
    git(root, "config", "commit.gpgsign", "false")
    (root / "a.py").write_text("a = 1\n", encoding="utf-8")
    (root / "b.py").write_text("b = 1\n", encoding="utf-8")
    git(root, "add", ".")
    git(root, "commit", "-q", "-m", "base")
    return root


def record(commit, files=("a.py",), **extra):
    return {
        "d4_gated": True,
        "status": "PRODUCER_MAPPED",
        "producers": [{"file": f, "line": 1, "symbol": "s", "emits": "e"} for f in files],
        "reviewer": "lead",
        "reviewed_at": "2026-10-14T00:00:00Z",
        "reviewed_commit": commit,
        **extra,
    }


def commit_change(repo, name):
    (repo / name).write_text("changed = 2\n", encoding="utf-8")
    git(repo, "commit", "-q", "-am", f"change {name}")


def test_unchanged_producer_files_pass(repo):
    base = git(repo, "rev-parse", "HEAD")
    commit_change(repo, "b.py")  # a file no producer names
    failures, flagged = sig.check({"capabilities": {"CAP-X": record(base)}}, repo)
    assert failures == [] and flagged == []


def test_changed_producer_file_without_the_flag_fails_and_names_it(repo):
    base = git(repo, "rev-parse", "HEAD")
    commit_change(repo, "a.py")
    producers = {"capabilities": {"CAP-X": record(base, files=("a.py", "b.py"))}}
    failures, flagged = sig.check(producers, repo)
    assert flagged == []
    assert [(f["capability"], f["files"]) for f in failures] == [("CAP-X", ["a.py"])]
    assert sig.report(failures, flagged).splitlines()[0].startswith("FAIL CAP-X")


def test_flagged_record_is_listed_not_failed_and_is_not_declared(repo):
    base = git(repo, "rev-parse", "HEAD")
    commit_change(repo, "a.py")
    flagged_record = record(base, rereview_required="producer changed")
    failures, flagged = sig.check({"capabilities": {"CAP-X": flagged_record}}, repo)
    assert failures == []
    assert flagged == [{"capability": "CAP-X", "rereview_required": "producer changed"}]

    producers = load_producers()
    for flag in ("producer changed", ["x"], True):
        flagged_producers = copy.deepcopy(producers)
        flagged_producers["capabilities"]["CAP-RECEIPTS"]["rereview_required"] = flag
        adapter = WeatherAdapter(producers=flagged_producers)
        assert adapter.d4_declared("CAP-RECEIPTS") is False
        assert "CAP-RECEIPTS" not in adapter.implemented_capabilities()
    for clear in (None, "", "  "):
        cleared = copy.deepcopy(producers)
        cleared["capabilities"]["CAP-RECEIPTS"]["rereview_required"] = clear
        assert WeatherAdapter(producers=cleared).d4_declared("CAP-RECEIPTS") is True


def test_unavailable_reviewed_commit_fails_closed(repo):
    producers = {"capabilities": {"CAP-X": record("0" * 40)}}
    (failure,) = sig.check(producers, repo)[0]
    assert failure["capability"] == "CAP-X" and "not available" in failure["reason"]


def test_shallow_clone_names_the_fetch_depth(repo, tmp_path):
    base = git(repo, "rev-parse", "HEAD")
    commit_change(repo, "b.py")
    shallow = tmp_path / "shallow"
    subprocess.run(
        ["git", "clone", "-q", "--no-local", "--depth", "1", str(repo), str(shallow)],
        check=True,
        capture_output=True,
    )
    (failure,) = sig.check({"capabilities": {"CAP-X": record(base)}}, shallow)[0]
    assert "fetch-depth: 0" in failure["reason"]


def test_producer_file_missing_at_head_or_unnamed_fails(repo):
    base = git(repo, "rev-parse", "HEAD")
    gone = record(base, files=("a.py", "gone.py"))
    unnamed = record(base)
    unnamed["producers"].append({"file": None, "line": None, "symbol": "s", "emits": "e"})
    failures, _ = sig.check({"capabilities": {"CAP-G": gone, "CAP-U": unnamed}}, repo)
    assert {f["capability"]: f["files"] for f in failures} == {"CAP-G": ["gone.py"], "CAP-U": []}


def test_not_a_git_work_tree_fails_closed(tmp_path):
    failures, _ = sig.check({"capabilities": {"CAP-X": record("abc")}}, tmp_path)
    assert failures and "not a git work tree" in failures[0]["reason"]


def test_committed_signatures_cover_their_producer_files_at_head():
    """Every signed d4_gated record still covers its producer code at HEAD, or is flagged."""
    producers = json.loads(sig.PRODUCERS_FILE.read_text(encoding="utf-8"))
    failures, flagged = sig.check(producers)
    assert not failures, "\n" + sig.report(failures, flagged)
