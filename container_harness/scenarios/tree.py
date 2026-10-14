"""Synthetic controller stand-in. Prints ground truth (GT_*) to stdout (a pipe, not a file)."""

# ruff: noqa: E402  (probe scenario container-probe/probe/tree.py, ruff-formatted only)

import ctypes
import errno
import os
import signal
import socket
import subprocess
import sys
import threading
import time

RUN = "/work/run"
HOME = os.path.expanduser("~")
OUT = "/srv/outside"


def gt(p):
    print("GT_CREATE", os.path.abspath(p), flush=True)


def gsig(pid, sig, how):
    print("GT_SIG", pid, int(sig), how, flush=True)


def w(p, data="x"):
    with open(p, "w") as f:
        f.write(data)
    gt(p)


PY = sys.executable
CHILD = r"""
import os, subprocess, sys, time
def gt(p): print("GT_CREATE", os.path.abspath(p), flush=True)
open("child.txt","w").write("c"); gt("child.txt")
os.chdir("/tmp"); open("child_rel_after_chdir.txt","w").write("c"); gt("child_rel_after_chdir.txt")
g = subprocess.run([sys.executable, "-c", "import os\nopen('/work/run/grand.txt','w').write('g')\nprint('GT_CREATE /work/run/grand.txt', flush=True)"])
"""
ORPHAN_PARENT = r'''
import subprocess, sys
p = subprocess.Popen([sys.executable, "-c", """
import os, time, signal
signal.signal(signal.SIGTERM, lambda *a: (print('ORPHAN got SIGTERM ppid_now', os.getppid(), flush=True), os._exit(0)))
time.sleep(0.6)
open('/tmp/orphan.txt','w').write('o'); print('GT_CREATE /tmp/orphan.txt', flush=True)
open('/work/orphan.pid','w').write(str(os.getpid())); print('GT_CREATE /work/orphan.pid', flush=True)
time.sleep(20)
"""], start_new_session=True)
print("ORPHAN_PID", p.pid, flush=True)
'''  # parent exits immediately -> grandchild orphaned, reparented to PID 1 (docker-init)
res = {}
os.chdir(RUN)
# --- root-level writes
w("inside.txt")
w("gone.txt")
os.remove("gone.txt")
w("state.json.tmp", "{}")
os.replace("state.json.tmp", "state.json")
gt("state.json")
os.mkdir("sub")
gt("sub")
w("sub/b.txt")
w("/tmp/root_tmp.txt")
w(os.path.join(HOME, "home.txt"))
w(OUT + "/abs_outside.txt")
os.symlink("inside.txt", "link_sym")
gt("link_sym")
os.link("inside.txt", "link_hard")
gt("link_hard")
s = socket.socket(socket.AF_UNIX)
s.bind(RUN + "/ctl.sock")
gt(RUN + "/ctl.sock")
s.close()
os.mkfifo("fifo1")
gt("fifo1")
# O_TMPFILE: unnamed then linked into place
for d in ("/tmp", "/dev/shm"):
    try:
        fd = os.open(d, os.O_TMPFILE | os.O_WRONLY, 0o600)
        os.write(fd, b"t")
    except OSError as e:
        print("OTMPFILE_UNSUPPORTED", d, e.errno, flush=True)
        continue
    _lc = ctypes.CDLL(None, use_errno=True)
    if (
        _lc.linkat(
            -100, f"/proc/self/fd/{fd}".encode(), -100, (d + "/tmpfile_linked.txt").encode(), 0x400
        )
        != 0
    ):
        print("LINKAT_FAILED", ctypes.get_errno(), flush=True)
    else:
        gt(d + "/tmpfile_linked.txt")
    os.close(fd)
    fd = os.open(d, os.O_TMPFILE | os.O_WRONLY, 0o600)
    os.write(fd, b"u")
    os.close(fd)
    print("GT_UNNAMED O_TMPFILE never linked in", d, flush=True)
import tempfile

tf = tempfile.TemporaryFile()
tf.write(b"anon")
tf.close()  # O_TMPFILE, never named
print("GT_UNNAMED tempfile.TemporaryFile in", tempfile.gettempdir(), flush=True)
# --- thread write
t = threading.Thread(target=lambda: w(RUN + "/thread.txt"))
t.start()
t.join()
# --- raw fork
pid = os.fork()
if pid == 0:
    open(RUN + "/forked.txt", "w").write("f")
    print("GT_CREATE", RUN + "/forked.txt", flush=True)
    os._exit(0)
os.waitpid(pid, 0)
# --- child + grandchild (subprocess uses vfork/clone3 CLONE_VFORK)
subprocess.run([PY, "-c", CHILD], cwd=RUN)
# --- shell launch writing via redirection
subprocess.run(["/bin/sh", "-c", "echo s > /work/run/shell.txt; touch /var/tmp/shell_vartmp.txt"])
gt("/work/run/shell.txt")
gt("/var/tmp/shell_vartmp.txt")
# --- rapid churn: 30 short-lived processes, create-then-delete
for i in range(30):
    subprocess.run(
        [
            PY,
            "-S",
            "-c",
            f"import os\np='/tmp/churn{i}.txt'\nopen(p,'w').write('x')\nos.remove(p)\nprint('GT_CREATE', p, flush=True)",
        ]
    )
# --- io_uring availability (would bypass syscall-level %file tracing)
libc = ctypes.CDLL(None, use_errno=True)
params = (ctypes.c_ubyte * 120)()
r = libc.syscall(425, 4, params)  # io_uring_setup
print("IOURING_SETUP ret", r, "errno", errno.errorcode.get(ctypes.get_errno()), flush=True)
# --- orphan
op = subprocess.run([PY, "-c", ORPHAN_PARENT])  # parent exits at once; grandchild orphaned
time.sleep(1.5)
orphan = int(open("/work/orphan.pid").read())


# --- signal phase
def ppid_of(p):
    try:
        return int(open(f"/proc/{p}/stat").read().rsplit(")", 1)[1].split()[1])
    except Exception as e:
        return repr(e)


print("PPID_AT_SIGNAL", orphan, ppid_of(orphan), flush=True)
gsig(orphan, signal.SIGTERM, "kill")
os.kill(orphan, signal.SIGTERM)
# group of two sleepers in its own session; kill via killpg (what processes.py uses on POSIX)
g1 = subprocess.Popen(["/bin/sh", "-c", "sleep 30 & sleep 30; wait"], start_new_session=True)
time.sleep(0.3)
print("PGRP_LEADER", g1.pid, flush=True)
gsig(-g1.pid, signal.SIGKILL, "killpg")
os.killpg(g1.pid, signal.SIGKILL)
g1.wait()
# pidfd_send_signal
p2 = subprocess.Popen(["sleep", "30"])
time.sleep(0.1)
pfd = os.pidfd_open(p2.pid)
gsig(p2.pid, signal.SIGTERM, "pidfd_send_signal")
signal.pidfd_send_signal(pfd, signal.SIGTERM)
p2.wait()
os.close(pfd)
# kill of an already-exited (reaped) pid -> ESRCH, still an attempt
p3 = subprocess.Popen(["true"])
p3.wait()
gsig(p3.pid, signal.SIGTERM, "kill_exited")
try:
    os.kill(p3.pid, signal.SIGTERM)
except ProcessLookupError:
    pass
# signal 0 probe, and own-thread tgkill
gsig(os.getpid(), 0, "kill_sig0")
os.kill(os.getpid(), 0)
signal.signal(signal.SIGUSR2, lambda *a: None)
gsig(os.getpid(), signal.SIGUSR2, "pthread_kill")
signal.pthread_kill(threading.get_ident(), signal.SIGUSR2)
# decoy outside the tree (a violation the oracle must see)
decoy = int(open("/work/decoy.pid").read())
gsig(decoy, signal.SIGUSR1, "kill_decoy")
os.kill(decoy, signal.SIGUSR1)
print("TREE_DONE", flush=True)
