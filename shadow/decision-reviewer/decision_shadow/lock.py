"""Supply-chain pin for the Laya weights: one official repo, one full revision sha, sha256 per file.

The lock file (``laya.lock.json`` next to this package) is the authority. ``verify_cache``
recomputes every file's sha256 and REFUSES (raises ``HashMismatch``) on any difference, a
missing file, or an extra repo/revision. Only ``convaiinnovations/laya`` is accepted; third-party
mirrors (ONNX/GGUF re-packs) and the ``ollaya`` runner are not, by design.
"""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path

HERE = Path(__file__).resolve().parent.parent          # shadow/decision-reviewer/
LOCK_PATH = HERE / "laya.lock.json"
CACHE_ROOT = HERE / "cache"                            # gitignored

OFFICIAL_REPO = "convaiinnovations/laya"
# English root checkpoint: the files laya.Agent reads from a local directory.
LAYA_FILES = (
    "rl_agent_config.json",
    "config.json",
    "model.safetensors",
    "encoder/config.json",
    "tokenizer/tokenizer.json",
    "tokenizer/tokenizer_config.json",
)


class HashMismatch(RuntimeError):
    """A cached artifact does not match the lock. The backend must refuse to load."""


def sha256_file(path: Path, chunk: int = 1 << 20) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        while True:
            b = f.read(chunk)
            if not b:
                break
            h.update(b)
    return h.hexdigest()


def read_lock(path: Path = LOCK_PATH) -> dict:
    with open(path, encoding="utf-8") as f:
        lock = json.load(f)
    if lock.get("repo") != OFFICIAL_REPO:
        raise HashMismatch(f"lock names repo {lock.get('repo')!r}; only {OFFICIAL_REPO!r} is allowed")
    rev = str(lock.get("revision", ""))
    if len(rev) != 40 or any(c not in "0123456789abcdef" for c in rev):
        raise HashMismatch(f"lock revision must be a full 40-hex commit sha, got {rev!r}")
    return lock


def cache_dir(lock: dict, root: Path = CACHE_ROOT) -> Path:
    return root / f"laya-{lock['revision'][:12]}"


def verify_cache(lock: dict, directory: Path) -> None:
    """Raise HashMismatch unless every locked file is present with the locked sha256."""
    files = lock.get("files") or {}
    missing_pins = [f for f in LAYA_FILES if not files.get(f, {}).get("sha256")]
    if missing_pins:
        raise HashMismatch(f"lock has no sha256 for {missing_pins}; run fetch-laya --write-lock first")
    for rel, meta in files.items():
        p = directory / rel
        if not p.is_file():
            raise HashMismatch(f"{rel} missing from {directory}; run fetch-laya")
        size = meta.get("size")
        if size is not None and os.path.getsize(p) != int(size):
            raise HashMismatch(f"{rel}: size {os.path.getsize(p)} != locked {size}")
        got = sha256_file(p)
        if got != meta["sha256"]:
            raise HashMismatch(f"{rel}: sha256 {got} != locked {meta['sha256']}; refusing to load")
