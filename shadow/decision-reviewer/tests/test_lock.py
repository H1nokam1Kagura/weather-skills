"""Supply-chain refusals: hash mismatch, wrong repo, floating revision -- all refuse to load."""
import json

import pytest

from decision_shadow import backends, lock
from decision_shadow.shadow import read_log, shadow_score

REV = "7b928d828b7b0e022f929d9bd2e44165aa270148"


def _fake_checkpoint(tmp_path, monkeypatch, tamper=None):
    """A fake cache + lock in tmp. `tamper` names a file to corrupt after locking."""
    root = tmp_path / "cache"
    lk = {"repo": lock.OFFICIAL_REPO, "revision": REV, "files": {}}
    d = lock.cache_dir(lk, root)
    for rel in lock.LAYA_FILES:
        p = d / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(f"content of {rel}".encode())
        lk["files"][rel] = {"size": p.stat().st_size, "sha256": lock.sha256_file(p)}
    if tamper:
        (d / tamper).write_bytes(b"tampered")
    lock_path = tmp_path / "laya.lock.json"
    lock_path.write_text(json.dumps(lk), encoding="utf-8")
    monkeypatch.setattr(lock, "LOCK_PATH", lock_path)
    monkeypatch.setattr(lock, "CACHE_ROOT", root)
    # backends imported these names directly; point them at the tmp lock/cache too
    monkeypatch.setattr(backends, "read_lock", lambda: lock.read_lock(lock_path))
    monkeypatch.setattr(backends, "cache_dir", lambda l: lock.cache_dir(l, root))
    backends._LAYA_AGENT.clear()
    return lk, d


def test_verify_passes_on_intact_cache(tmp_path, monkeypatch):
    lk, d = _fake_checkpoint(tmp_path, monkeypatch)
    lock.verify_cache(lk, d)
    assert backends.laya_verify_only() == REV


def test_hash_mismatch_refuses(tmp_path, monkeypatch):
    lk, d = _fake_checkpoint(tmp_path, monkeypatch, tamper="model.safetensors")
    with pytest.raises(lock.HashMismatch):
        lock.verify_cache(lk, d)


def test_hash_mismatch_same_size_refuses(tmp_path, monkeypatch):
    lk, d = _fake_checkpoint(tmp_path, monkeypatch)
    p = d / "model.safetensors"
    data = bytearray(p.read_bytes())
    data[0] ^= 0xFF                                             # same size, one bit-flipped byte
    p.write_bytes(bytes(data))
    with pytest.raises(lock.HashMismatch, match="refusing to load"):
        lock.verify_cache(lk, d)


def test_laya_backend_refuses_to_load_and_logs_unavailable(tmp_path, monkeypatch, shadow_log):
    _fake_checkpoint(tmp_path, monkeypatch, tamper="tokenizer/tokenizer.json")
    with pytest.raises(lock.HashMismatch):
        backends.score("escalate", "x", backend="laya")
    shadow_score("escalate", "x", backend="laya")
    (rec,) = read_log(shadow_log)
    assert rec["status"] == "unavailable" and "HashMismatch" in rec["error"]
    assert not backends._LAYA_AGENT                             # nothing was loaded


def test_missing_file_refuses(tmp_path, monkeypatch):
    lk, d = _fake_checkpoint(tmp_path, monkeypatch)
    (d / "rl_agent_config.json").unlink()
    with pytest.raises(lock.HashMismatch, match="missing"):
        lock.verify_cache(lk, d)


def test_unpinned_lock_refuses(tmp_path):
    p = tmp_path / "l.json"
    p.write_text(json.dumps({"repo": lock.OFFICIAL_REPO, "revision": REV, "files": {}}), encoding="utf-8")
    lk = lock.read_lock(p)
    with pytest.raises(lock.HashMismatch, match="no sha256"):
        lock.verify_cache(lk, tmp_path)


@pytest.mark.parametrize("repo,rev", [("Mattepiu/laya-onnx", REV), ("mys/laya-GGUF", REV),
                                      (lock.OFFICIAL_REPO, "main"), (lock.OFFICIAL_REPO, "7b928d828b")])
def test_mirror_or_floating_revision_refused(tmp_path, repo, rev):
    p = tmp_path / "l.json"
    p.write_text(json.dumps({"repo": repo, "revision": rev, "files": {}}), encoding="utf-8")
    with pytest.raises(lock.HashMismatch):
        lock.read_lock(p)


def test_committed_lock_is_official_and_full_sha():
    lk = lock.read_lock()
    assert lk["repo"] == "convaiinnovations/laya" and lk["revision"] == REV
