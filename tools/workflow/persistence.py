"""Atomic checkpoints and a kernel-held single-writer lock; no stale PID guessing."""

import json
import os
import time
from contextlib import contextmanager
from pathlib import Path

from .contracts import Refused, digest


def atomic_json(path, value):
    path = Path(path)
    temporary = path.with_suffix(path.suffix + ".tmp")
    with temporary.open("w", encoding="utf-8") as stream:
        json.dump(value, stream, indent=2, allow_nan=False)
        stream.flush()
        os.fsync(stream.fileno())
    for attempt in range(4):
        try:
            os.replace(temporary, path)
            break
        except PermissionError:
            if os.name != "nt" or attempt == 3:
                raise
            # Windows scanners may briefly hold the target. Retry the identical
            # atomic replacement only; never change destination or remove the old state.
            time.sleep(0.05 * (attempt + 1))


def write_checkpoint(root, payload):
    atomic_json(
        Path(root) / "checkpoint.json",
        {
            "schema": "workflow.checkpoint/2",
            "digest": digest(payload),
            "payload": payload,
            "state": payload["state"],
            "events": payload["events"],
        },
    )


def read_checkpoint(root):
    value = json.loads((Path(root) / "checkpoint.json").read_text(encoding="utf-8"))
    if value.get("schema") != "workflow.checkpoint/2" or digest(value["payload"]) != value.get(
        "digest"
    ):
        raise Refused("unsupported or damaged checkpoint")
    return value["payload"]


@contextmanager
def run_lock(root):
    path = Path(root) / ".run.lock"
    with path.open("a+b") as stream:
        stream.seek(0)
        if os.fstat(stream.fileno()).st_size == 0:
            stream.write(b"0")
            stream.flush()
        stream.seek(0)
        try:
            if os.name == "nt":
                import msvcrt

                msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl

                fcntl.flock(stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            raise Refused("run is already owned by another process") from exc
        try:
            yield
        finally:
            stream.seek(0)
            if os.name == "nt":
                msvcrt.locking(stream.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                fcntl.flock(stream.fileno(), fcntl.LOCK_UN)
