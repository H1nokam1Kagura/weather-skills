"""Real bounded Windows plumbing canary; no candidate or scientific execution."""

import json
import os
import subprocess
import sys
from dataclasses import asdict, replace

import pytest

from adapter_support.signal_capture import ObservationError, OwnedTreeCapture, observe

pytestmark = pytest.mark.skipif(os.name != "nt", reason="Windows-only targeted capture")

# Windows venv python.exe can be a redirector which starts a separate runtime.
# These stdlib-only actors use the concrete interpreter so Popen.pid identifies
# the executing parent, rather than an unobserved redirector ancestor.
ACTOR_PYTHON = getattr(sys, "_base_executable", sys.executable)


def test_owned_child_and_unrelated_decoy():
    child_code = "import time; time.sleep(4)"
    parent_code = (
        "import subprocess,sys; "
        f"p=subprocess.Popen([sys.executable,'-c',{child_code!r}]); "
        "print(p.pid,flush=True); print(p.wait(),flush=True)"
    )
    # Every launched process naturally exits within a few seconds even on failure.
    with subprocess.Popen(
        [ACTOR_PYTHON, "-c", parent_code], stdout=subprocess.PIPE, text=True,
        creationflags=subprocess.CREATE_NO_WINDOW,
    ) as parent, subprocess.Popen(
        [ACTOR_PYTHON, "-c", child_code], creationflags=subprocess.CREATE_NO_WINDOW,
    ) as decoy:
        child_pid = int(parent.stdout.readline())
        capture = OwnedTreeCapture(parent)
        child = capture.register_descendant(child_pid)
        assert child.parent_pid == parent.pid
        assert capture.events == []  # Preserving observation alone sends nothing.
        assert capture.exhaustive_controller_capture is False
        with pytest.raises(ObservationError, match="outside verified"):
            capture.register_descendant(decoy.pid)
        with pytest.raises(ObservationError, match="not an authorized"):
            capture.terminate(observe(decoy.pid))
        with pytest.raises(ObservationError, match="not an authorized"):
            capture.terminate(replace(child, created_100ns=child.created_100ns + 1))
        assert decoy.poll() is None
        decoy_identity = observe(decoy.pid)
        assert capture.events == []
        record = capture.terminate(child)
        assert record["sequence"] == 1
        assert record["target"]["pid"] == child_pid
        assert [x["pid"] for x in record["ancestry"]] == [child_pid, parent.pid]
        assert record["observed_ns"] <= record["requested_ns"] <= record["returned_ns"]
        assert record["status"] == "REQUEST_ACCEPTED"
        assert int(parent.stdout.readline()) == 97  # Independent parent wait receipt.
        assert parent.wait(timeout=5) == 0
        assert decoy.poll() is None
        assert decoy.wait(timeout=6) == 0  # Natural exit, never targeted.
        print(json.dumps({"canary": "owned_child_and_unrelated_decoy", "event": record,
                          "decoy": asdict(decoy_identity), "decoy_exit": 0,
                          "child_exit": 97, "exhaustive_controller_capture": False}))


def test_missing_observation_never_asserts_capture(monkeypatch):
    def unavailable(_pid):
        raise ObservationError("Observation unavailable")

    monkeypatch.setattr("adapter_support.signal_capture.observe", unavailable)
    with subprocess.Popen([ACTOR_PYTHON, "-c", "import time; time.sleep(0.2)"],
                          creationflags=subprocess.CREATE_NO_WINDOW) as root:
        with pytest.raises(ObservationError, match="unavailable"):
            OwnedTreeCapture(root)
