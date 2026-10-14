"""Exercise actual guard/exception cleanup on short harness-owned actors only."""

import json
import os
import socket
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

    # Neither actor may exit on its own: each blocks on stdin, so the root is alive when
    # taskkill reaches it and the decoy outlives the stop until it is released below.
    with (
        subprocess.Popen(
            [ACTOR_PYTHON, "-c", "import sys; sys.stdin.read()"],
            stdin=subprocess.PIPE,
            creationflags=subprocess.CREATE_NO_WINDOW,
        ) as root,
        subprocess.Popen(
            [ACTOR_PYTHON, "-c", "import sys; sys.stdin.read()"],
            stdin=subprocess.PIPE,
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
        decoy.stdin.close()  # Release the decoy only now: it must outlive the stop.
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
    # Owned actors live until the controller stops them: neither may exit on its own
    # (the root must not exit when taskkill /T kills its child first). The child PID is
    # announced over loopback, so no file can be read half-written or while locked.
    announce = socket.create_server(("127.0.0.1", 0))
    port = announce.getsockname()[1]
    child_code = "import time; time.sleep(60)"
    parent_code = (
        "import socket,subprocess,sys,time; "
        f"p=subprocess.Popen([sys.executable,'-c',{child_code!r}]); "
        f"s=socket.create_connection(('127.0.0.1',{port})); s.sendall(str(p.pid).encode()); "
        "s.close(); time.sleep(60)"
    )
    monkeypatch.setattr(processes, "free_gib", lambda: 10.0)
    observer = ControllerProcessObserver()
    with ExitStack() as stack:
        stack.enter_context(announce)
        decoy = stack.enter_context(
            subprocess.Popen(
                [ACTOR_PYTHON, "-c", "import sys; sys.stdin.read()"],
                stdin=subprocess.PIPE,
                creationflags=subprocess.CREATE_NO_WINDOW,
            )
        )
        decoy_identity = observe(decoy.pid)
        child_receipt = {}

        def callback(event, proc, details):
            result = observer(event, proc, details)
            if event == "launch":
                announce.settimeout(0.05)
                # Hang ceiling only (~10x the worst loaded gap observed); never a timing assumption.
                ceiling = time.monotonic() + 120
                while True:
                    try:
                        conn, _ = announce.accept()
                        break
                    except TimeoutError:
                        if proc.poll() is not None:
                            raise RuntimeError(
                                "Owned root exited before announcing its child"
                            ) from None
                        if time.monotonic() > ceiling:
                            raise RuntimeError("Owned root never announced its child") from None
                with conn:
                    conn.settimeout(None)  # Root sends then closes: EOF ends the read.
                    data = b""
                    while chunk := conn.recv(64):
                        data += chunk
                child_pid = int(data)
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

        # Guard modes trip on the first monitor pass (timeout=0). The interruption mode must
        # reach the injected sleep instead, so no time budget may preempt it however long
        # actor start-up takes: its budgets are unbounded, not merely generous.
        unbounded = mode == "exception_unregistered"
        controller = processes.Processes(
            tmp_path,
            timeout=float("inf") if unbounded else 0,
            ram_floor=0,
            total_seconds=float("inf") if unbounded else 15,
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
        decoy.stdin.close()  # Release the decoy only now: it must outlive every stop.
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
