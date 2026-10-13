"""Exercise actual guard/exception cleanup on short harness-owned actors only."""

import json
import os
import subprocess
import sys
import time
from contextlib import ExitStack
from ctypes import byref, wintypes

import pytest

from adapter_support.process_observation import ControllerProcessObserver
from adapter_support.signal_capture import ObservationError, OwnedTreeCapture, _handle, observe
from tools.workflow import processes
from tools.workflow.contracts import Refused

pytestmark = pytest.mark.skipif(os.name != "nt", reason="Windows owned-process canary")
ACTOR_PYTHON = getattr(sys, "_base_executable", sys.executable)


def test_default_path_retains_legacy_receipt(tmp_path, monkeypatch):
    monkeypatch.setattr(processes, "free_gib", lambda: 10.0)
    controller = processes.Processes(tmp_path, ram_floor=0)
    result = controller.run(
        [ACTOR_PYTHON, "-c", "print('owned control')"], tmp_path, os.environ.copy()
    )
    assert result["exit_code"] == 0
    assert "process_observations" not in result
    assert controller.observation_events == []


def test_direct_stop_observer_errors_cannot_prevent_owned_cleanup():
    def broken_observer(*args, **kwargs):
        raise RuntimeError("Injected direct observer failure")

    with (
        subprocess.Popen(
            [ACTOR_PYTHON, "-c", "import time; time.sleep(3)"],
            creationflags=subprocess.CREATE_NO_WINDOW,
        ) as root,
        subprocess.Popen(
            [ACTOR_PYTHON, "-c", "import time; time.sleep(4)"],
            creationflags=subprocess.CREATE_NO_WINDOW,
        ) as decoy,
    ):
        identity = OwnedTreeCapture(root).root
        decoy_identity = observe(decoy.pid)
        receipt = processes.stop(root, observe=broken_observer, reason="owned direct canary")
        assert receipt["backend_exit_code"] == 0
        assert root.returncode is not None
        assert [e["event"] for e in receipt["observer_errors"]] == [
            "stop_request",
            "stop_backend_return",
            "stop_wait_return",
        ]
        assert receipt["exhaustive_signal_capture"] is False
        assert decoy.poll() is None
        assert observe(decoy.pid) == decoy_identity
        assert decoy.wait(timeout=5) == 0
        print(
            json.dumps(
                {
                    "mode": "direct_observer_error",
                    "root_pid": identity.pid,
                    "root_creation_100ns": identity.created_100ns,
                    "root_exit": root.returncode,
                    "receipt": receipt,
                    "decoy_pid": decoy.pid,
                    "decoy_exit": 0,
                }
            )
        )


def test_direct_observer_cannot_mask_original_backend_failure(monkeypatch):
    backend_failure = OSError("Injected backend failure; no subprocess launched")

    def failed_backend(*args, **kwargs):
        raise backend_failure

    def broken_observer(*args, **kwargs):
        raise RuntimeError("Injected observer failure")

    class NoProcess:
        pid = 0  # Never reaches any OS API: subprocess.run is replaced below.

    monkeypatch.setattr(processes.subprocess, "run", failed_backend)
    with pytest.raises(OSError) as raised:
        processes.stop(NoProcess(), observe=broken_observer)
    assert raised.value is backend_failure
    assert "stop_request" in raised.value.__notes__[0]
    assert "stop_backend_error" in raised.value.__notes__[0]


@pytest.mark.parametrize("mode", ["guard", "observer_error", "exception_unregistered"])
def test_actual_stop_with_owned_child_and_decoy(tmp_path, monkeypatch, mode):
    child_file = tmp_path / "child.json"
    child_code = "import time; time.sleep(4)"
    parent_code = (
        "import subprocess,sys,pathlib; "
        f"p=subprocess.Popen([sys.executable,'-c',{child_code!r}]); "
        f"pathlib.Path({str(child_file)!r}).write_text(str(p.pid)); p.wait()"
    )
    monkeypatch.setattr(processes, "free_gib", lambda: 10.0)
    observer = ControllerProcessObserver()
    real_sleep = time.sleep
    with ExitStack() as stack:
        decoy = stack.enter_context(
            subprocess.Popen(
                [ACTOR_PYTHON, "-c", "import time; time.sleep(6)"],
                creationflags=subprocess.CREATE_NO_WINDOW,
            )
        )
        decoy_identity = observe(decoy.pid)
        child_receipt = {}

        def callback(event, proc, details):
            result = observer(event, proc, details)
            if event == "launch":
                deadline = time.monotonic() + 2
                while not child_file.exists():
                    if time.monotonic() >= deadline:
                        raise RuntimeError("Owned child announcement missing")
                    real_sleep(0.01)
                child_pid = int(child_file.read_text())
                child_receipt["identity"] = observe(child_pid)
                kernel, _, handle = stack.enter_context(_handle(child_pid))
                child_receipt.update(kernel=kernel, handle=handle)
                if mode != "exception_unregistered":
                    observer.register_descendant(proc.pid, child_pid)
                with pytest.raises(ObservationError, match="outside verified"):
                    observer.register_descendant(proc.pid, decoy.pid)
            if mode == "observer_error" and event == "stop_request":
                raise RuntimeError("Injected observer failure")
            return result

        if mode == "exception_unregistered":

            def interrupted_sleep(_seconds):
                raise RuntimeError("Injected monitor interruption")

            monkeypatch.setattr(processes.time, "sleep", interrupted_sleep)

        controller = processes.Processes(
            tmp_path,
            timeout=10 if mode == "exception_unregistered" else 0,
            ram_floor=0,
            total_seconds=15,
            observer=callback,
        )
        expected = RuntimeError if mode == "exception_unregistered" else Refused
        with pytest.raises(expected):
            controller.run([ACTOR_PYTHON, "-c", parent_code], tmp_path, os.environ.copy())
        events = controller.observation_events
        requests = [e for e in events if e["event"] == "stop_request"]
        assert len(requests) == 1
        request = requests[0]
        assert request["details"]["actual_target_set"] is None
        assert all(e["exhaustive_signal_capture"] is False for e in events)
        assert [e["sequence"] for e in events] == list(range(1, len(events) + 1))
        if mode == "observer_error":
            assert request["observation_status"] == "ERROR"
            assert "observation" not in request
        else:
            observation = request["observation"]
            assert observation["descendant_set_complete"] is False
            assert observation["actual_signal_targets"] is None
            known = observation["known_descendants_observed_now"]
            assert len(known) == (0 if mode == "exception_unregistered" else 1)
        returned = next(e for e in events if e["event"] == "stop_backend_return")
        assert returned["details"]["backend_exit_code"] == 0, returned
        assert returned["details"]["actual_target_set"] is None
        assert any(e["event"] == "stop_wait_return" for e in events)
        code = wintypes.DWORD()
        assert child_receipt["kernel"].GetExitCodeProcess(child_receipt["handle"], byref(code))
        assert code.value != 259  # Independent retained handle confirms child exited.
        assert decoy.poll() is None
        assert observe(decoy.pid) == decoy_identity
        assert decoy.wait(timeout=7) == 0
        print(
            json.dumps(
                {
                    "mode": mode,
                    "events": events,
                    "child_pid": child_receipt["identity"].pid,
                    "child_exit": code.value,
                    "decoy_pid": decoy.pid,
                    "decoy_exit": 0,
                }
            )
        )
