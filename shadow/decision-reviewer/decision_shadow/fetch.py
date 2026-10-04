"""Download the pinned Laya checkpoint from the OFFICIAL Hugging Face repo only.

Uses plain HTTPS against huggingface.co/<repo>/resolve/<full-sha>/<file> (stdlib, no
huggingface_hub needed), writes into the gitignored cache, then:
  * cross-checks every file against the Hub's own record for that revision (LFS sha256 for
    model.safetensors, git blob sha1 for the small files), and
  * with --write-lock records size + sha256 of every file in laya.lock.json, or without it
    verifies every file against the existing lock and refuses on any mismatch.
"""
from __future__ import annotations

import hashlib
import json
import os
import shutil
import urllib.request
from pathlib import Path

from .lock import (LAYA_FILES, LOCK_PATH, OFFICIAL_REPO, HashMismatch, cache_dir, read_lock,
                   sha256_file, verify_cache)

HF = "https://huggingface.co"


def _get(url: str, dest: Path) -> None:
    dest.parent.mkdir(parents=True, exist_ok=True)
    tmp = dest.with_suffix(dest.suffix + ".part")
    with urllib.request.urlopen(url, timeout=60) as r, open(tmp, "wb") as f:  # noqa: S310
        shutil.copyfileobj(r, f, length=1 << 20)
    os.replace(tmp, dest)


def hub_tree(repo: str, revision: str) -> dict[str, dict]:
    url = f"{HF}/api/models/{repo}/tree/{revision}?recursive=1"
    with urllib.request.urlopen(url, timeout=60) as r:  # noqa: S310
        tree = json.loads(r.read().decode("utf-8"))
    return {e["path"]: e for e in tree if isinstance(e, dict) and e.get("type") == "file"}


def git_blob_sha1(path: Path) -> str:
    data = path.read_bytes()
    return hashlib.sha1(b"blob %d\0" % len(data) + data).hexdigest()  # noqa: S324 (git object id)


def check_against_hub(directory: Path, tree: dict[str, dict]) -> None:
    """Every file must match the Hub's record for the pinned revision: LFS sha256 for large
    files, git blob sha1 for small ones. Refuses on any difference."""
    for rel in LAYA_FILES:
        e = tree.get(rel)
        if e is None:
            raise HashMismatch(f"{rel} not in the Hub tree at the pinned revision")
        p = directory / rel
        if e.get("lfs"):
            got, want = sha256_file(p), e["lfs"]["oid"]
        else:
            got, want = git_blob_sha1(p), e["oid"]
        if got != want:
            raise HashMismatch(f"{rel}: local {got} != Hub {want}")


def fetch(write_lock: bool = False, force: bool = False) -> dict:
    lock = read_lock()                       # refuses any repo but convaiinnovations/laya
    repo, rev = lock["repo"], lock["revision"]
    assert repo == OFFICIAL_REPO
    directory = cache_dir(lock)
    for rel in LAYA_FILES:
        dest = directory / rel
        if dest.is_file() and not force:
            continue
        print(f"downloading {repo}@{rev[:12]}/{rel}", flush=True)
        _get(f"{HF}/{repo}/resolve/{rev}/{rel}", dest)
    tree = hub_tree(repo, rev)
    check_against_hub(directory, tree)
    if write_lock:
        files = {}
        for rel in LAYA_FILES:
            p = directory / rel
            files[rel] = {"size": p.stat().st_size, "sha256": sha256_file(p),
                          "hub_git_oid": tree[rel]["oid"]}
        lock["files"] = files
        with open(LOCK_PATH, "w", encoding="utf-8") as f:
            json.dump(lock, f, indent=2)
            f.write("\n")
        print(f"wrote {LOCK_PATH}")
    verify_cache(lock, directory)
    print(f"verified {len(LAYA_FILES)} files at {directory} against the lock")
    return lock
