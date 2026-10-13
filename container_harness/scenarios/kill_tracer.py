"""Regression scenario: the traced tree tries to kill its tracer and PID 1, then keeps writing.

Hardened mode (strace root, tree uid 1000): both kills must fail with EPERM, the tracer must
survive, and every later write must still be in the trace. Ground truth goes to stdout.
"""

import os
import signal


def gt(path):
    print("GT_CREATE", os.path.abspath(path), flush=True)


def write(path):
    with open(path, "w") as stream:
        stream.write("x")
    gt(path)


write("/work/run/before_kill.txt")
for target, sig, label in ((os.getppid(), signal.SIGKILL, "tracer"), (1, signal.SIGTERM, "init")):
    print("GT_SIG", target, int(sig), "kill_" + label, flush=True)
    try:
        os.kill(target, sig)
        print("KILL_RESULT", label, "delivered", flush=True)
    except PermissionError:
        print("KILL_RESULT", label, "EPERM", flush=True)
write("/work/run/after_kill.txt")
print("TREE_DONE", flush=True)
