"""The frozen v2.7 resolver fails loudly on any drift; it never degrades to a skip."""

import hashlib
import io
import json
import shutil
import tarfile

import frozen_v27
import pytest
from frozen_v27 import FINAL_PINS_SHA256, FrozenPackageError, extract_fixture, frozen_root, verify


@pytest.fixture
def copy_root(tmp_path):
    dest = tmp_path / "frozen"
    shutil.copytree(frozen_root(), dest)
    return dest


def pins_of(root):
    return json.loads((root / "FINAL-PINS.json").read_bytes())["files"]


def test_resolved_root_matches_every_pin():
    root = frozen_root()
    raw = (root / "FINAL-PINS.json").read_bytes()
    assert hashlib.sha256(raw).hexdigest() == FINAL_PINS_SHA256
    pins = pins_of(root)
    assert len(pins) == 24
    for rel, expected in pins.items():
        assert hashlib.sha256((root / rel).read_bytes()).hexdigest() == expected
    assert (root / "simkit/__init__.py").is_file()


def test_committed_fixture_extracts_to_a_verified_root(tmp_path):
    root = extract_fixture(frozen_v27.FIXTURE, tmp_path / "x")
    assert sorted(p.relative_to(root).as_posix() for p in root.rglob("*") if p.is_file()) == sorted(
        [*pins_of(root), "FINAL-PINS.json"]
    )


def test_tampered_pinned_file_fails(copy_root):
    target = copy_root / "simkit/score.py"
    target.write_bytes(target.read_bytes() + b"\n# drift\n")
    with pytest.raises(FrozenPackageError, match="simkit/score.py"):
        verify(copy_root)


def test_missing_pinned_file_fails(copy_root):
    (copy_root / "casesets/cached-onset-e2e-v2_7/schedule_fixes.py").unlink()
    with pytest.raises(FrozenPackageError, match="missing casesets/.*schedule_fixes.py"):
        verify(copy_root)


def test_tampered_final_pins_fails(copy_root):
    pins_path = copy_root / "FINAL-PINS.json"
    pins_path.write_bytes(pins_path.read_bytes() + b" ")
    with pytest.raises(FrozenPackageError, match="FINAL-PINS.json sha256"):
        verify(copy_root)


def test_absent_package_fails(tmp_path):
    with pytest.raises(FrozenPackageError, match="no FINAL-PINS.json"):
        verify(tmp_path)


def test_env_override_is_verified_not_fallen_through(tmp_path, monkeypatch):
    monkeypatch.setenv(frozen_v27.ENV_VAR, str(tmp_path / "nowhere"))
    frozen_root.cache_clear()
    try:
        with pytest.raises(FrozenPackageError):
            frozen_root()
    finally:
        monkeypatch.delenv(frozen_v27.ENV_VAR)
        frozen_root.cache_clear()


def test_fixture_with_unexpected_member_is_refused(tmp_path):
    with tarfile.open(frozen_v27.FIXTURE, mode="r:xz") as src:
        members = [(m, src.extractfile(m).read()) for m in src.getmembers()]
    bad = tmp_path / "bad.tar.xz"
    with tarfile.open(bad, mode="w:xz") as out:
        for info, data in members:
            out.addfile(info, io.BytesIO(data))
        extra = tarfile.TarInfo("simkit/extra.py")
        extra.size = 1
        out.addfile(extra, io.BytesIO(b"x"))
    with pytest.raises(FrozenPackageError, match="extra=\\['simkit/extra.py'\\]"):
        extract_fixture(bad, tmp_path / "out")
    assert not (tmp_path / "out").exists() or not any((tmp_path / "out").iterdir())
