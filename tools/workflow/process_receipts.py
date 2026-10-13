"""Dispatch-evidence completeness for one controller-launched process (W2 review runner.py:89).

``supports_process_receipts`` only proves the process manager has the receipt INTERFACE.
This module proves, at process completion, that the manager actually delivered the
receipts for the process it returned: exactly one ``launch_check``, ``process_launch`` and
``process_exit`` in that order on the controller sequence, launch and exit naming the same
serial and PID (and the returned record's, where it carries them), plus exactly one
``signal_stop`` iff the returned record says a per-PID stop ran. A process record without
its receipts is missing evidence, refused at ``gate_evidence``; nothing is filled in.
"""

from .contracts import Refused

REASON = "process_receipts_missing"
REQUIRED = ("launch_check", "process_launch", "process_exit")


def _refuse(detail):
    return Refused(f"process receipts missing: {detail}", gate="gate_evidence", reason=REASON)


def verify(window, result):
    """``window``: the (kind, details) process events recorded during one ``run()``."""
    kinds = [kind for kind, _ in window]
    for kind in REQUIRED:
        if kinds.count(kind) != 1:
            raise _refuse(f"{kinds.count(kind)} {kind} event(s) for one launched process")
    stopped = isinstance(result, dict) and result.get("signal_stop") is not None
    if kinds.count("signal_stop") != int(stopped):
        raise _refuse(
            f"{kinds.count('signal_stop')} signal_stop event(s), returned record "
            f"{'has' if stopped else 'has no'} signal_stop"
        )
    order = [kinds.index(kind) for kind in REQUIRED]
    if stopped:
        order.append(kinds.index("signal_stop"))
    if order != sorted(order):
        raise _refuse(f"out-of-order process events {kinds}")
    events = dict(window)  # each required kind occurs exactly once
    launch, exit_ = events["process_launch"], events["process_exit"]
    for key in ("serial", "pid"):
        if key not in launch or launch.get(key) != exit_.get(key):
            raise _refuse(f"process_launch and process_exit disagree on {key}")
        if isinstance(result, dict) and key in result and result[key] != launch[key]:
            raise _refuse(f"returned process record disagrees with its receipts on {key}")
    if stopped and events["signal_stop"].get("serial") != launch["serial"]:
        raise _refuse("signal_stop names another serial")
