"""Single resolver for the frozen Simkit v2.7 package used as read-only test authority.

The frozen package is gitignored and local-only, so tests must not hard-code a host path to
it. ``frozen_root()`` returns a directory holding the pinned package, chosen in this order:

1. ``$SIMKIT_FROZEN_V27`` if set (an explicit choice: never silently falls through);
2. ``<repo>/completion-evidence/frozen-baseline`` if it holds ``FINAL-PINS.json``;
3. the committed fixture ``tests/fixtures/frozen-v27.tar.xz``, extracted once per session
   under the pytest basetemp (or a fresh temp dir outside pytest).

Whatever the source, ``FINAL-PINS.json`` is checked against ``FINAL_PINS_SHA256`` and every
file it lists against its pin before the root is returned. Any mismatch, missing file or
unexpected archive member raises ``FrozenPackageError``: an absent or drifted package is a
failure, never a skip.

Rebuild the fixture (deterministic) from a verified package:
    python tests/frozen_v27.py --build <frozen-root>
"""

from __future__ import annotations

import functools
import hashlib
import io
import json
import lzma
import os
import sys
import tarfile
import tempfile
from pathlib import Path, PurePosixPath

ENV_VAR = "SIMKIT_FROZEN_V27"
# sha256 of FINAL-PINS.json of the frozen v2.7 closure package.
FINAL_PINS_SHA256 = "153f207e89c6ac7786d8203413eeef1e1fce219aef7252d63aced1b5b33d0402"
PINS_NAME = "FINAL-PINS.json"

REPO_ROOT = Path(__file__).resolve().parents[1]
REPO_LOCAL = REPO_ROOT / "completion-evidence" / "frozen-baseline"
FIXTURE = Path(__file__).resolve().parent / "fixtures" / "frozen-v27.tar.xz"

# Set by the root conftest.py at pytest_configure so extraction lands in the session basetemp.
_pytest_config = None


class FrozenPackageError(RuntimeError):
    """The frozen v2.7 package is missing, incomplete or does not match its pins."""


def bind_pytest_config(config) -> None:
    global _pytest_config
    _pytest_config = config


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _load_pins(raw: bytes, source: str) -> dict[str, str]:
    actual = _sha256(raw)
    if actual != FINAL_PINS_SHA256:
        raise FrozenPackageError(
            f"{source}: {PINS_NAME} sha256 {actual} != pinned {FINAL_PINS_SHA256}"
        )
    files = json.loads(raw)["files"]
    for rel in files:
        p = PurePosixPath(rel)
        if p.is_absolute() or ".." in p.parts or rel == PINS_NAME:
            raise FrozenPackageError(f"{source}: unsafe pinned path {rel!r}")
    return files


def verify(root: Path) -> Path:
    """Verify ``root`` against the pinned FINAL-PINS.json and every file it lists."""
    root = Path(root)
    pins_path = root / PINS_NAME
    if not pins_path.is_file():
        raise FrozenPackageError(f"{root}: no {PINS_NAME}")
    pins = _load_pins(pins_path.read_bytes(), str(root))
    problems = []
    for rel, expected in sorted(pins.items()):
        path = root / rel
        if not path.is_file():
            problems.append(f"missing {rel}")
        elif (actual := _sha256(path.read_bytes())) != expected:
            problems.append(f"{rel}: sha256 {actual} != pinned {expected}")
    if problems:
        raise FrozenPackageError(f"{root}: frozen package drifted: " + "; ".join(problems))
    return root


def _extraction_dir() -> Path:
    factory = getattr(_pytest_config, "_tmp_path_factory", None)
    if factory is not None:
        return factory.mktemp("frozen-v27", numbered=True)
    return Path(tempfile.mkdtemp(prefix="frozen-v27-"))


def extract_fixture(fixture: Path = FIXTURE, dest: Path | None = None) -> Path:
    """Extract exactly FINAL-PINS.json and the files it lists; anything else is refused."""
    if not fixture.is_file():
        raise FrozenPackageError(f"committed fixture missing: {fixture}")
    dest = Path(dest) if dest is not None else _extraction_dir()
    with tarfile.open(fixture, mode="r:xz") as tar:
        members = {m.name: m for m in tar.getmembers()}
        if PINS_NAME not in members:
            raise FrozenPackageError(f"{fixture}: no {PINS_NAME}")
        pins = _load_pins(tar.extractfile(members[PINS_NAME]).read(), str(fixture))
        expected = set(pins) | {PINS_NAME}
        if set(members) != expected or not all(m.isfile() for m in members.values()):
            extra = sorted(set(members) - expected)
            missing = sorted(expected - set(members))
            raise FrozenPackageError(
                f"{fixture}: members differ from pins (extra={extra}, missing={missing})"
            )
        tar.extractall(dest, filter="data")
    return verify(dest)


@functools.cache
def frozen_root() -> Path:
    """Return a verified frozen v2.7 root (cached per process)."""
    env = os.environ.get(ENV_VAR)
    if env:
        return verify(Path(env))
    if (REPO_LOCAL / PINS_NAME).exists():
        return verify(REPO_LOCAL)
    return extract_fixture()


def build_fixture(source: Path, fixture: Path = FIXTURE) -> int:
    """Write a deterministic xz tar of a verified package; returns its size in bytes."""
    source = verify(Path(source))
    pins = json.loads((source / PINS_NAME).read_bytes())["files"]
    buf = io.BytesIO()
    with lzma.LZMAFile(buf, "wb", preset=9 | lzma.PRESET_EXTREME) as xz:
        with tarfile.open(fileobj=xz, mode="w", format=tarfile.PAX_FORMAT) as tar:
            for rel in sorted([*pins, PINS_NAME]):
                data = (source / rel).read_bytes()
                info = tarfile.TarInfo(rel)
                info.size, info.mode, info.mtime = len(data), 0o644, 0
                tar.addfile(info, io.BytesIO(data))
    fixture.parent.mkdir(parents=True, exist_ok=True)
    fixture.write_bytes(buf.getvalue())
    extract_fixture(fixture, Path(tempfile.mkdtemp(prefix="frozen-v27-check-")))
    return fixture.stat().st_size


if __name__ == "__main__":
    if len(sys.argv) != 3 or sys.argv[1] != "--build":
        sys.exit("usage: python tests/frozen_v27.py --build <frozen-root>")
    print(f"{FIXTURE}: {build_fixture(Path(sys.argv[2]))} bytes")
