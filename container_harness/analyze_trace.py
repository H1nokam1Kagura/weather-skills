"""Turn a harness ``strace -ff -A --timestamps=unix,ns -y`` capture into contract-form evidence.

usage: python analyze_trace.py OUTDIR [--run-dir /work/case/run] [--out evidence.json]

OUTDIR is the container's /harness-out as copied by the launcher: ``trace/t.<tid>`` files,
``baseline.txt``, ``baseline_links.tsv``, ``harness_status.txt``, ``root.stderr``.

Frozen v2.7 shapes produced (EVIDENCE_CONTRACT.json ``structures``):
  * ``filesystem_snapshot.created_paths``: absolute POSIX paths of every file the controller tree
    created (create-then-delete and rename targets included). A path that never existed in the
    pre-run baseline and came into existence through a traced syscall of the tree. Directories,
    FIFOs, sockets and links count: each is a new directory entry the tree made.
  * ``process_tree``: {pid: creating parent} for every process the tree started (from clone
    events, never the kernel-reassigned parent of an orphan; owner reading ``parent_at_signal``)
    plus HARNESS_CONTROLLER_PID and every signalled pid (harness-known parent, else 0).
  * ``controller_trace.signalled_pids``: every pid a kill-family syscall of the tree targeted
    (kill, tkill, tgkill, rt_sigqueueinfo, rt_tgsigqueueinfo, pidfd_send_signal; process-group
    and -1 targets resolved to their members at the time of the call), whether or not the call
    succeeded ("terminated or not"). DECISION: signal 0 is excluded. kill(pid, 0) sends no signal
    (POSIX: "no signal is sent"; it is an existence/permission probe), and the contract field is
    the PIDs the controller *signalled*. Every signal-0 call is listed in
    ``harness_notes.signal_zero_probes`` so the decision is auditable.
  * ``symbols.HARNESS_CONTROLLER_PID``, ``harness_observations.max_concurrent_children`` (most
    direct child processes of the controller alive at once), ``harness_observations.shell_launches``
    (successful execve of a shell binary by any tree process), ``signal_capture_exhaustive``,
    ``tracer_status``.
  * ``harness_observations.data_fetch_attempts``: network attempts of the tree. Every connect()
    and every destination-addressed sendto/sendmsg/sendmmsg message whose address family is not
    local (AF_UNIX/AF_LOCAL and AF_NETLINK, plus the AF_UNSPEC disconnect, are excluded),
    whether or not it succeeded (containers run with ``--network none``, so attempts fail).
    DECISION: the host relay endpoint (``relay_endpoint`` in harness_status, live-role
    containers only) is the role transport, not a data fetch, when the CONTROLLER process
    itself connects to it; those calls are listed in ``harness_notes.role_transport_connects``.
    A relay connect by any other tree process counts as a fetch attempt. Missing when network
    syscalls were not in the traced set (``trace_network`` absent from harness_status), when
    io_uring was set up (IORING_OP_CONNECT bypasses syscall tracing) or when a destination
    address could not be read.
  * ``harness_observations.min_free_ram_while_running_gib``: NOT trace-derived. The minimum
    MemAvailable (GiB, /proc/meminfo: the container VM figure, owner reading UC-07_memory) over
    the harness RAM sampler's readings taken while the traced tree was running (strictly
    between ``run_start`` and ``run_end``). The sampler (``ram_sampler.py``) is a root process
    the entry shell starts outside the traced tree. Missing when the sampler did not finish
    cleanly, no reading falls inside the run window, or the readings leave a gap longer than
    ``RAM_MAX_GAP_S`` (at either end or between readings): a sampled minimum is claimed only
    when the whole run is covered. A post-exit reading is never used.

Missing stays missing: if the tracer died, a trace file is truncated, the harness did not
finish, or the trace is structurally inconsistent, every trace-derived field is ABSENT (listed
in ``missing`` with the reason), never an empty list. A successful io_uring_setup makes
created_paths missing (io_uring bypasses syscall-level tracing). An unresolvable signal target
makes signalled_pids missing. Processes are keyed internally as ``pid#generation``.

Owner readings applied (container_route_ruling_2026-10-13): unnamed O_TMPFILE / memfd inodes are
not created paths (harness_notes only; a later link is a created path); kernel-generated signals
are not the controller signalling (their setup calls are flagged in harness_notes).
Stdlib only, so it runs on the host and in the image.
"""

import json
import posixpath
import re
import sys
from pathlib import Path

SCHEMA = "simkit.container-harness-evidence/1"
SHELLS = {"sh", "bash", "dash", "zsh", "ksh", "mksh", "csh", "tcsh", "fish", "ash", "busybox"}
PSEUDO_FS = ("/proc/", "/sys/")
SIGNAL_SYSCALLS = {
    "kill",
    "tkill",
    "tgkill",
    "rt_sigqueueinfo",
    "rt_tgsigqueueinfo",
    "pidfd_send_signal",
}
TRACE_FIELDS = (
    "filesystem_snapshot.created_paths",
    "process_tree",
    "controller_trace.signalled_pids",
    "harness_observations.max_concurrent_children",
    "harness_observations.shell_launches",
    "harness_observations.data_fetch_attempts",
)
RAM_FIELD = "harness_observations.min_free_ram_while_running_gib"
RAM_MAX_GAP_S = 2.0
NETWORK_SYSCALLS = {"connect", "sendto", "sendmsg", "sendmmsg"}
# Address families that never leave the container: not a data fetch.
LOCAL_FAMILIES = {"AF_UNIX", "AF_LOCAL", "AF_NETLINK"}

LINE = re.compile(r"^(\d+)\.(\d{1,9}) (.*)$")
SYSCALL = re.compile(
    r"^(\w+)\((.*)\)\s+=\s+(-?\d+|\?|0x[0-9a-fA-F]+)(<(?:[^>\\]|\\.)*>)?(.*)$", re.S
)
UNFINISHED = re.compile(r"^(\w+)\((.*) <unfinished \.\.\.>$", re.S)
RESUMED = re.compile(r"^<\.\.\. (\w+) resumed>(.*)$", re.S)
TERMINAL = re.compile(
    r"^\+\+\+ (exited with (\d+)|killed by (\w+)(?: \(core dumped\))?|superseded by execve.*) \+\+\+$"
)
DELIVERY = re.compile(r"^--- (SIG\w+) \{(.*)\} ---$")
# strace messages that say nothing about trace completeness (only "attached" for a followed
# child, which -q normally suppresses). Everything else that strace prints is a problem.
BENIGN_STRACE = re.compile(r"^strace: Process \d+ attached$")
USER_SI_CODES = {"SI_USER", "SI_TKILL", "SI_QUEUE"}


# ------------------------------------------------------------------ low-level parsing


def unquote(token):
    """Decode a strace C string literal -> (text, truncated). None if not a string literal."""
    if not token or not token.startswith('"'):
        return None, False
    out, i, n = bytearray(), 1, len(token)
    while i < n:
        c = token[i]
        if c == '"':
            return out.decode("utf-8", errors="surrogateescape"), token[i + 1 :].startswith("...")
        if c == "\\" and i + 1 < n:
            e = token[i + 1]
            simple = {"n": 10, "t": 9, "r": 13, "v": 11, "f": 12, "a": 7, "b": 8, "e": 27}
            if e in simple:
                out.append(simple[e])
                i += 2
            elif e == "x":
                out.append(int(token[i + 2 : i + 4], 16))
                i += 4
            elif e in "01234567":
                j = i + 1
                while j < n and j < i + 4 and token[j] in "01234567":
                    j += 1
                out.append(int(token[i + 1 : j], 8) & 0xFF)
                i = j
            else:
                out += e.encode()
                i += 2
        else:
            out += c.encode("utf-8", errors="surrogateescape")
            i += 1
    return None, True


def split_args(text):
    parts, depth, cur, quoted, i = [], 0, [], False, 0
    while i < len(text):
        c = text[i]
        if quoted:
            cur.append(c)
            if c == "\\" and i + 1 < len(text):
                cur.append(text[i + 1])
                i += 1
            elif c == '"':
                quoted = False
        elif c == '"':
            quoted = True
            cur.append(c)
        elif c in "([{<":
            depth += 1
            cur.append(c)
        elif c in ")]}>":
            depth -= 1
            cur.append(c)
        elif c == "," and depth == 0:
            parts.append("".join(cur).strip())
            cur = []
        else:
            cur.append(c)
        i += 1
    if "".join(cur).strip():
        parts.append("".join(cur).strip())
    return parts


def fd_path(token):
    """`3</work/run>` or `AT_FDCWD</cwd>` -> '/work/run'. None when undecorated."""
    if token is None:
        return None
    m = re.search(r"<(/(?:[^>\\]|\\.)*)>", token)
    return m.group(1) if m else None


def parse_int(token):
    try:
        return int(token, 0)
    except (TypeError, ValueError):
        return None


def _skip_quoted(text, i):
    """Index just past the C string literal starting at text[i] == '"'."""
    i += 1
    while i < len(text):
        if text[i] == "\\":
            i += 2
            continue
        if text[i] == '"':
            return i + 1
        i += 1
    return i


def braced_after(text, marker):
    """Every balanced ``{...}`` directly following ``marker`` outside quoted strings."""
    out, i, n = [], 0, len(text)
    while i < n:
        if text[i] == '"':
            i = _skip_quoted(text, i)
            continue
        if text.startswith(marker + "{", i):
            j, depth = i + len(marker), 0
            while j < n:
                if text[j] == '"':
                    j = _skip_quoted(text, j)
                    continue
                if text[j] == "{":
                    depth += 1
                elif text[j] == "}":
                    depth -= 1
                    if depth == 0:
                        break
                j += 1
            out.append(text[i + len(marker) : j + 1])
            i = j + 1
            continue
        i += 1
    return out


def sockaddr(token):
    """strace sockaddr text -> (family, destination). (None, None) for NULL; ("UNREADABLE",
    raw) when the argument is not a decoded sockaddr (e.g. a bare pointer)."""
    if token is None or token.strip() in ("NULL", ""):
        return None, None
    token = token.strip()
    m = re.match(r"\{sa_family=(AF_\w+)", token)
    if not m:
        return "UNREADABLE", token[:120]
    family = m.group(1)
    port = re.search(r"sin6?_port=htons\((\d+)\)", token)
    dest = token[:120]
    if family == "AF_INET":
        addr = re.search(r'inet_addr\("([^"]*)"\)', token)
        if addr and port:
            dest = f"{addr.group(1)}:{port.group(1)}"
    elif family == "AF_INET6":
        addr = re.search(r'inet_pton\(AF_INET6, "([^"]*)"', token)
        if addr and port:
            dest = f"[{addr.group(1)}]:{port.group(1)}"
    return family, dest


class Event:
    __slots__ = (
        "ts",
        "tid",
        "seg",
        "line",
        "kind",
        "name",
        "args",
        "raw_args",
        "ret",
        "dec",
        "tail",
    )

    def __init__(self, ts, tid, seg, line, kind, **kw):
        self.ts, self.tid, self.seg, self.line, self.kind = ts, tid, seg, line, kind
        self.name = kw.get("name")
        self.raw_args = kw.get("raw_args", "")
        self.args = kw.get("args", [])
        self.ret = kw.get("ret")
        self.dec = kw.get("dec")
        self.tail = kw.get("tail", "")

    @property
    def ok(self):
        r = parse_int(self.ret)
        return r is not None and r >= 0

    @property
    def rv(self):
        return parse_int(self.ret)


def parse_trace_file(path, tid):
    """-> (segments: list[list[Event]], terminated: list[bool], anomalies)."""
    segments, terminated, anomalies = [[]], [False], []
    pending = None
    with open(path, encoding="utf-8", errors="surrogateescape") as stream:
        for lineno, raw in enumerate(stream):
            raw = raw.rstrip("\n")
            if not raw:
                continue
            m = LINE.match(raw)
            if not m:
                anomalies.append({"file": path.name, "line": lineno, "unparsed": raw[:200]})
                continue
            ts = int(m.group(1)) * 10**9 + int(m.group(2).ljust(9, "0"))
            rest = m.group(3)
            if terminated[-1]:  # a new incarnation of this tid (-A appends on pid reuse)
                segments.append([])
                terminated.append(False)
            seg = len(segments) - 1
            um = UNFINISHED.match(rest)
            if um:
                pending = (um.group(1), um.group(2))
                continue
            rm = RESUMED.match(rest)
            if rm:
                if pending and pending[0] == rm.group(1):
                    rest = f"{pending[0]}({pending[1]}{rm.group(2)}"
                else:
                    rest = f"{rm.group(1)}({rm.group(2)}"
                pending = None
            tm = TERMINAL.match(rest)
            if tm:
                if pending:  # a call that never returned (process exited inside it)
                    segments[-1].append(
                        Event(
                            ts,
                            tid,
                            seg,
                            lineno,
                            "syscall",
                            name=pending[0],
                            raw_args=pending[1],
                            args=split_args(pending[1]),
                            ret="?",
                        )
                    )
                    pending = None
                segments[-1].append(Event(ts, tid, seg, lineno, "exit", tail=tm.group(1)))
                terminated[-1] = True
                continue
            dm = DELIVERY.match(rest)
            if dm:
                segments[-1].append(
                    Event(ts, tid, seg, lineno, "delivery", name=dm.group(1), raw_args=dm.group(2))
                )
                continue
            sm = SYSCALL.match(rest)
            if sm:
                segments[-1].append(
                    Event(
                        ts,
                        tid,
                        seg,
                        lineno,
                        "syscall",
                        name=sm.group(1),
                        raw_args=sm.group(2),
                        args=split_args(sm.group(2)),
                        ret=sm.group(3),
                        dec=sm.group(4)[1:-1] if sm.group(4) else None,
                        tail=sm.group(5).strip(),
                    )
                )
                continue
            anomalies.append({"file": path.name, "line": lineno, "unparsed": rest[:200]})
    if pending:
        segments[-1].append(
            Event(
                0,
                tid,
                len(segments) - 1,
                -1,
                "syscall",
                name=pending[0],
                raw_args=pending[1],
                args=split_args(pending[1]),
                ret="?",
            )
        )
    if segments and not segments[-1] and len(segments) > 1:
        segments.pop()
        terminated.pop()
    return segments, terminated, anomalies


def read_status(path):
    status = {}
    if path.exists():
        for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
            if "=" in line:
                k, v = line.split("=", 1)
                status[k.strip()] = v.strip()
    return status


# ------------------------------------------------------------------ model


class Proc:
    """One process incarnation (pid#gen), or a harness-known untraced process."""

    def __init__(self, key, pid):
        self.key, self.pid = key, pid
        self.creator = None  # Proc key of the creating process
        self.start = None
        self.end = None
        self.threads = set()  # thread keys (tid#gen) belonging to this process
        self.harness = False
        self.harness_parent = None
        self.clone_flags = ""
        self.execs = []


def analyze(outdir, run_dir=None):
    outdir = Path(outdir)
    status = read_status(outdir / "harness_status.txt")
    notes = {
        "o_tmpfile_unnamed": [],
        "memfd_unnamed": [],
        "signal_zero_probes": [],
        "kernel_signal_setups": [],
        "reparented": [],
        "io_uring_calls": [],
        "o_creat_on_existing": [],
        "excluded_pseudo_fs": [],
        "unresolved_paths": [],
        "failed_signal_attempts": [],
        "pid_reuse": [],
        "clone_flags_of_note": [],
        "exec_failures": [],
        "role_transport_connects": [],
        "local_socket_calls": 0,
        "sockets_created": {},
        "semantics": {
            "signal_zero": "excluded from signalled_pids (no signal is sent); listed here",
            "failed_signal_attempts": "included in signalled_pids (the controller issued them)",
            "created_paths": "new directory entries (files, dirs, fifos, sockets, links); "
            "O_CREAT on an existing path is a write, listed in o_creat_on_existing",
            "max_concurrent_children": "direct child processes of the controller alive at once "
            "(clone/vfork/fork return or first event, to exit)",
            "shell_launches": "successful execve of a shell binary by any tree process",
            "process_tree": "creating parent from clone events, never the reassigned parent",
            "data_fetch_attempts": "connect() and destination-addressed send calls of any "
            "tree process to a non-local address family, successful or not; the controller's "
            "own connects to the declared relay endpoint are role transport (listed in "
            "role_transport_connects), not data fetches",
            "min_free_ram_while_running_gib": "minimum MemAvailable over harness RAM sampler "
            "readings strictly inside the traced run window; whole-run coverage required",
        },
    }
    missing = {}
    tracer = {"tracer": "strace -ff -A -y --timestamps=unix,ns", "status": "OK", "problems": []}

    def problem(code, detail):
        tracer["problems"].append({"code": code, "detail": detail})

    # ---- harness completion and tracer exit
    if status.get("complete") != "1":
        problem("HARNESS_INCOMPLETE", "entry script did not record completion")
    if status.get("harness_killed_tracer") == "1":
        problem("TRACER_KILLED", "the harness killed the tracer during the run")
    if "untraced_exit" in status:
        tracer["status"] = "NOT_RUN"
        problem("NOT_TRACED", "run executed without the tracer (overhead baseline)")
    trace_dir = outdir / "trace"
    files = sorted(trace_dir.glob("t.*")) if trace_dir.is_dir() else []
    if not files and tracer["status"] != "NOT_RUN":
        problem("NO_TRACE", "no trace files")
    # strace's own diagnostics share stderr with the tree (strace -q already suppresses the
    # attach/detach chatter). Anything else that strace says (lost tracee, ptrace errors,
    # "Process N detached" after a tracer-side failure, ...) makes the trace untrusted. A tree
    # that prints a fake "strace: " line can only make evidence MISSING, never fill it.
    strace_msgs, benign_msgs = [], []
    if (outdir / "root.stderr").exists():
        text = (outdir / "root.stderr").read_text(encoding="utf-8", errors="replace")
        for line in text.splitlines():
            if line.startswith("strace: "):
                (benign_msgs if BENIGN_STRACE.match(line) else strace_msgs).append(line)
    tracer["strace_messages"] = strace_msgs
    tracer["strace_benign_messages"] = benign_msgs[:20]
    if strace_msgs:
        problem("TRACER_MESSAGES", strace_msgs[:20])

    # ---- parse every per-tid file
    by_tid, parse_anomalies = {}, []
    for f in files:
        tid = parse_int(f.name.split(".", 1)[1])
        if tid is None:
            continue
        segs, term, anom = parse_trace_file(f, tid)
        parse_anomalies += anom
        by_tid[tid] = (segs, term)
        for i, t in enumerate(term):
            if not t:
                problem("TRUNCATED", f"{f.name} segment {i} has no exit line")
    if parse_anomalies:
        tracer["parse_anomalies"] = parse_anomalies[:50]

    # ---- structure: clone returns -> segment generations
    clones = {}  # child tid -> [(ts, parent tid, parent seg, flags, is_thread)]
    for tid, (segs, _) in by_tid.items():
        for si, seg in enumerate(segs):
            for ev in seg:
                if (
                    ev.kind == "syscall"
                    and ev.name in ("clone", "clone3", "fork", "vfork")
                    and ev.ok
                    and ev.rv > 0
                ):
                    flags = ev.raw_args if ev.name.startswith("clone") else ""
                    clones.setdefault(ev.rv, []).append(
                        (ev.ts, tid, si, flags, "CLONE_THREAD" in flags)
                    )
    for v in clones.values():
        v.sort()
    roots = []
    seg_origin = {}  # (tid, seg) -> clone tuple or None (root)
    for tid, (segs, _) in by_tid.items():
        cl = clones.get(tid, [])
        if len(segs) == len(cl) + 1 and not roots_has(roots, tid):
            seg_origin[(tid, 0)] = None
            roots.append(tid)
            for i, c in enumerate(cl):
                seg_origin[(tid, i + 1)] = c
        elif len(segs) == len(cl):
            for i, c in enumerate(cl):
                seg_origin[(tid, i)] = c
        else:
            problem("INCONSISTENT", f"tid {tid}: {len(segs)} segments vs {len(cl)} clone returns")
    for child in clones:
        if child not in by_tid:
            problem("INCONSISTENT", f"clone returned {child} but no trace file exists for it")
    if len(roots) != 1 and files:
        problem("INCONSISTENT", f"expected exactly one root, found {sorted(roots)}")
    root_tid = roots[0] if len(roots) == 1 else None

    # thread key -> owning process key; process objects
    procs, owner = {}, {}

    def tkey(tid, seg):
        return f"{tid}#{seg}"

    order = sorted(seg_origin, key=lambda k: (seg_origin[k][0] if seg_origin[k] else -1, k))
    for tid, si in order:
        origin = seg_origin[(tid, si)]
        key = tkey(tid, si)
        segs = by_tid[tid][0]
        events = segs[si] if si < len(segs) else []
        first = events[0].ts if events else None
        last = events[-1].ts if events else None
        if origin is None or not origin[4]:  # a process (root or non-thread clone)
            p = Proc(key, tid)
            procs[key] = p
            owner[key] = key
            candidates = [x for x in (first, origin[0] if origin else None) if x is not None]
            p.start = min(candidates) if candidates else None
            p.end = last
            if origin is not None:
                ptid, pseg, flags = origin[1], origin[2], origin[3]
                p.creator = owner.get(tkey(ptid, pseg))
                p.clone_flags = flags
                for flag in ("CLONE_PARENT", "CLONE_NEWPID", "CLONE_NEWNS", "CLONE_NEWUSER"):
                    if flag in flags:
                        notes["clone_flags_of_note"].append({"pid": tid, "flag": flag})
                        if flag == "CLONE_NEWPID":
                            problem("INCONSISTENT", "new pid namespace breaks pid closure")
            p.threads.add(key)
        else:
            parent_owner = owner.get(tkey(origin[1], origin[2]))
            owner[key] = parent_owner
            if parent_owner in procs:
                procs[parent_owner].threads.add(key)
                if last is not None:
                    pe = procs[parent_owner].end
                    procs[parent_owner].end = max(pe or last, last)
    for key, p in procs.items():
        if p.creator is None and p.pid != root_tid:
            problem("INCONSISTENT", f"process {key} has no resolvable creator")
    for tid, (segs, _) in sorted(by_tid.items()):
        if len(segs) > 1:
            notes["pid_reuse"].append({"pid": tid, "generations": len(segs)})

    root_key = tkey(root_tid, 0) if root_tid is not None else None
    # (clone return ts, parent tid) -> child process key: pgid and cwd are inherited AT the
    # clone, not at the child's first traced event (a group kill can land in between).
    clone_child = {
        (origin[0], origin[1]): owner[tkey(tid, si)]
        for (tid, si), origin in seg_origin.items()
        if origin is not None and not origin[4] and tkey(tid, si) in procs
    }

    # harness-known untraced processes (pid namespace closure)
    harness = {}

    def add_harness(pid, parent, pgid, label):
        if pid is None:
            return
        h = Proc(f"h{pid}", pid)
        h.harness, h.harness_parent, h.start, h.end = True, parent, -1, 10**30
        h.label, h.pgid = label, pgid
        harness[pid] = h

    entry_pid = parse_int(status.get("entry_pid"))
    tracer_pid = parse_int(status.get("tracer_pid"))
    add_harness(1, 0, parse_int(status.get("init_pgid")), "docker-init")  # --init
    add_harness(entry_pid, 1, parse_int(status.get("entry_pgid")), "entry-shell")
    add_harness(tracer_pid, entry_pid, parse_int(status.get("tracer_pgid")), "strace")
    decoy = parse_int(status.get("decoy_pid"))
    add_harness(decoy, entry_pid, parse_int(status.get("decoy_pgid")), "decoy")
    sampler = parse_int(status.get("ram_sampler_pid"))
    add_harness(sampler, entry_pid, parse_int(status.get("ram_sampler_pgid")), "ram-sampler")

    # ---- global replay in time order
    all_events = []
    for segs, _ in by_tid.values():
        for seg in segs:
            all_events += seg
    all_events.sort(key=lambda e: (e.ts, e.tid, e.line))

    def proc_of(ev):
        return owner.get(tkey(ev.tid, ev.seg))

    def alive_incarnation(pid, ts):
        """Proc (traced or harness) for pid at time ts; fall back to the latest earlier one."""
        best = None
        for p in procs.values():
            if p.pid == pid and p.start is not None and p.start <= ts:
                if p.end is None or ts <= p.end:
                    return p
                if best is None or p.start > best.start:
                    best = p
        if pid in harness:
            return harness[pid]
        return best

    def thread_owner(tid, ts):
        for (t, si), _ in seg_origin.items():
            if t != tid:
                continue
            seg = by_tid[t][0][si]
            if seg and seg[0].ts <= ts <= seg[-1].ts:
                return procs.get(owner.get(tkey(t, si)))
        return alive_incarnation(tid, ts)

    # pgid timeline: proc key -> current pgid
    pgid = {}
    initial_root_pgid = parse_int(status.get("tracer_pgid"))
    cwd = {}
    if root_key:
        pgid[root_key] = initial_root_pgid
        cwd[root_key] = status.get("root_cwd")
    started = set()

    baseline = set()
    if (outdir / "baseline.txt").exists():
        baseline = {
            line.rstrip("\n")
            for line in (outdir / "baseline.txt").open(encoding="utf-8", errors="surrogateescape")
            if line.strip()
        }
    links = {}
    if (outdir / "baseline_links.tsv").exists():
        for line in (outdir / "baseline_links.tsv").open(
            encoding="utf-8", errors="surrogateescape"
        ):
            if "\t" in line:
                a, b = line.rstrip("\n").split("\t", 1)
                links[a] = b
    exists = set(baseline)
    created, created_index = [], {}
    pidfds = {}  # proc key -> {fd: pid}
    attempts = []
    shell_launches = 0
    io_uring_ok = False
    unresolved_targets = []
    fetch_attempts, unreadable_destinations = [], []
    relay_endpoint = status.get("relay_endpoint") or None

    def canon(path):
        """Resolve symlinked DIRECTORY components (not the final one) via the link table."""
        if path is None:
            return None
        path = posixpath.normpath(path)
        if path.startswith("//"):
            path = "/" + path.lstrip("/")
        for _ in range(40):
            parts = path.split("/")
            changed = False
            for i in range(2, len(parts)):
                prefix = "/".join(parts[:i])
                if prefix in links:
                    target = links[prefix]
                    base = (
                        target
                        if target.startswith("/")
                        else posixpath.join(posixpath.dirname(prefix), target)
                    )
                    path = posixpath.normpath(posixpath.join(base, *parts[i:]))
                    changed = True
                    break
            if not changed:
                return path
        return path

    def resolve(pkey, dirtok, path, ev):
        if path is None:
            return None
        if path.startswith("/"):
            return canon(path)
        if dirtok is None or dirtok.startswith("AT_FDCWD"):
            base = fd_path(dirtok) if dirtok else None
            base = base or cwd.get(pkey)
        else:
            base = fd_path(dirtok)
        if base is None:
            notes["unresolved_paths"].append({"pid": ev.tid, "call": ev.name, "path": path})
            return None
        return canon(posixpath.join(base, path))

    def add(path, how, ev, pkey, kind="file"):
        if path is None:
            return
        if path.startswith(PSEUDO_FS):
            notes["excluded_pseudo_fs"].append({"path": path, "call": how})
            return
        if path in exists:
            notes["o_creat_on_existing"].append({"path": path, "call": how, "pid": ev.tid})
            return
        exists.add(path)
        record = {
            "path": path,
            "call": how,
            "kind": kind,
            "pid": procs[pkey].pid if pkey in procs else ev.tid,
            "proc": pkey,
            "ts_ns": ev.ts,
            "deleted_later": False,
        }
        created.append(record)
        created_index.setdefault(path, []).append(record)

    def remove(path, recursive_dir=False):
        if path is None:
            return
        exists.discard(path)
        for r in created_index.get(path, []):
            r["deleted_later"] = True
        links.pop(path, None)

    def move(src, dst, how, ev, pkey):
        moved = sorted(p for p in exists if p == src or p.startswith(src + "/"))
        was = dst in exists
        for p in moved:
            remove(p)
        if was:
            exists.discard(dst)
            for r in created_index.get(dst, []):
                r["replaced_later"] = True
        add(dst, how, ev, pkey)
        if was and created_index.get(dst):
            created_index[dst][-1]["replaced_existing"] = True
        for p in moved:
            if p != src:
                tail = p[len(src) :]
                add(dst + tail, how + "(moved-with-dir)", ev, pkey)
        if src in links:
            links[dst] = links.pop(src)

    deliveries, kills, analyzer_errors = [], [], []

    def record_network(ev, pkey):
        """data_fetch_attempts: one entry per non-local destination a call addresses."""
        a, name = ev.args, ev.name
        if name == "connect":
            addrs = [a[1] if len(a) > 1 else "<absent>"]
        elif name == "sendto":
            addrs = [a[4] if len(a) > 4 else None]
        else:  # sendmsg / sendmmsg: every msg_name={...}; none means a connected socket
            addrs = braced_after(ev.raw_args, "msg_name=")
        sender = procs[pkey].pid
        for token in addrs:
            family, dest = sockaddr(token)
            if family is None:
                continue  # NULL destination: a connected socket (its connect is counted)
            if family == "AF_UNSPEC" and name == "connect":
                continue  # dissolves a datagram association; addresses nothing
            entry = {
                "pid": sender,
                "tid": ev.tid,
                "call": name,
                "family": family,
                "destination": dest,
                "result": "ok" if ev.ok else f"{ev.ret} {ev.tail}".strip()[:80],
                "ts_ns": ev.ts,
            }
            if family == "UNREADABLE":
                unreadable_destinations.append(entry)
            elif family in LOCAL_FAMILIES:
                notes["local_socket_calls"] += 1
            elif relay_endpoint and dest == relay_endpoint and sender == root_tid:
                notes["role_transport_connects"].append(entry)
            else:
                fetch_attempts.append(entry)

    def replay_one(ev, pkey):
        nonlocal shell_launches, io_uring_ok
        p = procs.get(pkey)
        if pkey not in started and p is not None:  # first sighting: inherit pgid and cwd
            started.add(pkey)
            if p.creator is not None:
                pgid.setdefault(pkey, pgid.get(p.creator))
                cwd.setdefault(pkey, cwd.get(p.creator))
        if ev.kind == "delivery":
            fields = dict(re.findall(r"(\w+)=([^,}]+)", ev.raw_args))
            deliveries.append(
                {
                    "receiver_pid": procs[pkey].pid,
                    "receiver_tid": ev.tid,
                    "signal": ev.name,
                    "si_code": fields.get("si_code"),
                    "si_pid": parse_int(fields.get("si_pid")),
                    "ts_ns": ev.ts,
                }
            )
            return
        if ev.kind == "exit":
            km = re.match(r"killed by (\w+)", ev.tail)
            if km and ev.tid == procs[pkey].pid:
                kills.append({"pid": ev.tid, "signal": km.group(1), "ts_ns": ev.ts})
            return
        if ev.kind != "syscall":
            return
        for m in re.finditer(r"AT_FDCWD<(/(?:[^>\\]|\\.)*)>", ev.raw_args):
            cwd[pkey] = m.group(1)
        name, a = ev.name, ev.args
        if name in ("clone", "clone3", "fork", "vfork") and ev.ok and ev.rv > 0:
            ck = clone_child.get((ev.ts, ev.tid))
            if ck is not None and ck not in started:
                started.add(ck)
                pgid.setdefault(ck, pgid.get(pkey))
                cwd.setdefault(ck, cwd.get(pkey))
            return  # structure already built
        if name == "setsid" and ev.ok:
            pgid[pkey] = procs[pkey].pid
            return
        if name == "setpgid" and ev.ok and len(a) >= 2:
            tpid = parse_int(a[0]) or procs[pkey].pid
            g = parse_int(a[1]) or tpid
            target = alive_incarnation(tpid, ev.ts)
            if target is not None and not target.harness:
                pgid[target.key] = g
            return
        if name == "prctl" and a and a[0] == "PR_SET_PDEATHSIG":
            notes["kernel_signal_setups"].append(
                {"pid": ev.tid, "call": "prctl(PR_SET_PDEATHSIG)", "args": ev.raw_args[:120]}
            )
        if (
            name == "prctl"
            and a
            and a[0] == "PR_SET_CHILD_SUBREAPER"
            and len(a) > 1
            and a[1] != "0"
        ):
            procs[pkey].subreaper = True
        if name == "fcntl" and len(a) > 1 and a[1] in ("F_SETOWN", "F_SETOWN_EX", "F_SETSIG"):
            notes["kernel_signal_setups"].append(
                {"pid": ev.tid, "call": "fcntl(" + a[1] + ")", "args": ev.raw_args[:120]}
            )
        if name in ("alarm", "setitimer", "timer_create"):
            notes["kernel_signal_setups"].append(
                {"pid": ev.tid, "call": name, "args": ev.raw_args[:120]}
            )
        if name.startswith("io_uring"):
            notes["io_uring_calls"].append(
                {"pid": ev.tid, "call": name, "ret": ev.ret, "tail": ev.tail[:80]}
            )
            if name == "io_uring_setup" and ev.ok:
                io_uring_ok = True
            return
        if name == "memfd_create" and ev.ok:
            notes["memfd_unnamed"].append({"pid": ev.tid, "name": unquote(a[0])[0] if a else None})
            return
        if name == "pidfd_open" and ev.ok and a:
            pidfds.setdefault(pkey, {})[ev.rv] = parse_int(a[0])
            return
        if name in ("execve", "execveat"):
            path = unquote(a[0] if name == "execve" else a[1])[0] if a else None
            if ev.ok:
                procs[pkey].execs.append(path)
                if path and posixpath.basename(path) in SHELLS:
                    shell_launches += 1
            else:
                notes["exec_failures"].append(
                    {"pid": ev.tid, "path": path, "ret": ev.ret, "tail": ev.tail[:60]}
                )
            return
        if name in ("chdir", "fchdir") and ev.ok and a:
            if name == "chdir":
                newcwd = resolve(pkey, None, unquote(a[0])[0], ev)
            else:
                newcwd = fd_path(a[0])
            if newcwd:
                cwd[pkey] = newcwd
                for other in procs[pkey].threads:  # threads share the fs struct
                    if other in procs:
                        cwd[other] = newcwd
            return
        if name in NETWORK_SYSCALLS:
            record_network(ev, pkey)
            return
        if name == "socket" and a:
            family = a[0].split("|")[0]
            notes["sockets_created"][family] = notes["sockets_created"].get(family, 0) + 1
            return
        if name in SIGNAL_SYSCALLS:
            record_signal(
                ev,
                pkey,
                procs,
                harness,
                pgid,
                pidfds,
                alive_incarnation,
                thread_owner,
                attempts,
                notes,
                unresolved_targets,
                root_tid,
            )
            return
        if not ev.ok:
            return
        # ---- filesystem creation / removal
        if name in ("open", "creat", "openat", "openat2"):
            at = name.startswith("openat")
            flags = a[2] if at and len(a) > 2 else (a[1] if len(a) > 1 else "")
            if name == "creat":
                flags = "O_CREAT"
            path_arg = unquote(a[1] if at else a[0])[0] if a else None
            if "O_TMPFILE" in flags:
                notes["o_tmpfile_unnamed"].append(
                    {
                        "dir": ev.dec or resolve(pkey, a[0] if at else None, path_arg, ev),
                        "pid": ev.tid,
                    }
                )
                return
            if "O_CREAT" not in flags:
                return
            target = (
                ev.dec
                if ev.dec and ev.dec.startswith("/")
                else resolve(pkey, a[0] if at else None, path_arg, ev)
            )
            add(canon(target), name, ev, pkey)
        elif name in ("mkdir", "mknod"):
            add(
                resolve(pkey, None, unquote(a[0])[0], ev),
                name,
                ev,
                pkey,
                "dir" if name == "mkdir" else "node",
            )
        elif name in ("mkdirat", "mknodat"):
            add(
                resolve(pkey, a[0], unquote(a[1])[0], ev),
                name,
                ev,
                pkey,
                "dir" if name == "mkdirat" else "node",
            )
        elif name in ("symlink", "symlinkat"):
            target = unquote(a[0])[0]
            dst = (
                resolve(pkey, None, unquote(a[1])[0], ev)
                if name == "symlink"
                else resolve(pkey, a[1], unquote(a[2])[0], ev)
            )
            add(dst, name, ev, pkey, "symlink")
            if dst and target is not None:
                links[dst] = target
        elif name == "link":
            add(resolve(pkey, None, unquote(a[1])[0], ev), name, ev, pkey, "hardlink")
        elif name == "linkat":
            add(resolve(pkey, a[2], unquote(a[3])[0], ev), name, ev, pkey, "hardlink")
        elif name == "rename":
            move(
                resolve(pkey, None, unquote(a[0])[0], ev),
                resolve(pkey, None, unquote(a[1])[0], ev),
                name,
                ev,
                pkey,
            )
        elif name in ("renameat", "renameat2"):
            src = resolve(pkey, a[0], unquote(a[1])[0], ev)
            dst = resolve(pkey, a[2], unquote(a[3])[0], ev)
            flags = a[4] if len(a) > 4 else ""
            if "RENAME_EXCHANGE" in flags:
                return  # both entries existed before and after
            move(src, dst, name, ev, pkey)
            if "RENAME_WHITEOUT" in flags:
                add(src, name + "(whiteout)", ev, pkey, "node")
        elif name == "unlink":
            remove(resolve(pkey, None, unquote(a[0])[0], ev))
        elif name == "unlinkat":
            remove(resolve(pkey, a[0], unquote(a[1])[0], ev))
        elif name == "rmdir":
            remove(resolve(pkey, None, unquote(a[0])[0], ev))
        elif name == "bind":
            m = re.search(r'sun_path=("(?:[^"\\]|\\.)*")', ev.raw_args)
            if m:
                sp = unquote(m.group(1))[0]
                if sp and not sp.startswith("\x00") and not m.group(1).startswith('"\\0'):
                    add(resolve(pkey, None, sp, ev), "bind(AF_UNIX)", ev, pkey, "socket")

    for ev in all_events:
        pkey = proc_of(ev)
        if pkey is None:
            continue
        try:
            replay_one(ev, pkey)
        except Exception as exc:  # never guess past an event we could not read
            analyzer_errors.append(
                {"tid": ev.tid, "line": ev.line, "call": ev.name, "error": repr(exc)[:200]}
            )
    if analyzer_errors:
        problem("ANALYZER_ERROR", analyzer_errors[:20])

    # ---- receiver-side cross-check of signal capture. A user-sent signal (si_code SI_USER /
    # SI_TKILL / SI_QUEUE) delivered from a pid of the traced tree must match a recorded
    # kill-family attempt of that sender at that receiver; one that does not means the capture
    # missed a send, so signalled_pids becomes MISSING. Kernel-generated deliveries (SIGCHLD,
    # SIGPIPE, faults, timers, parent-death, SIGIO) are not the controller signalling: they are
    # excluded from signalled_pids and only counted here (owner reading kernel_signals).
    tree_pids = {p.pid for p in procs.values()}
    attempt_keys = {
        (at["sender_pid"], t, at["signal"]) for at in attempts for t in at["target_pids"]
    }
    unmatched, kernel_deliveries = [], {}
    for d in deliveries:
        if d["si_code"] in USER_SI_CODES and d["si_pid"] is not None:
            if (
                d["si_pid"] in tree_pids
                and (d["si_pid"], d["receiver_pid"], d["signal"]) not in attempt_keys
            ):
                unmatched.append(d)
        else:
            k = f"{d['signal']}/{d['si_code']}"
            kernel_deliveries[k] = kernel_deliveries.get(k, 0) + 1
    notes["deliveries_unmatched_to_attempts"] = unmatched
    notes["kernel_signal_deliveries"] = kernel_deliveries
    notes["sigkill_deaths_without_attempt"] = [
        k
        for k in kills
        if k["signal"] == "SIGKILL"
        and not any(k["pid"] in at["target_pids"] and at["signal"] == "SIGKILL" for at in attempts)
    ]
    if unmatched:
        unresolved_targets.append(
            {
                "reason": "user-sent delivery without a recorded attempt",
                "deliveries": unmatched[:20],
            }
        )

    # ---- reparenting (creator exits while child alive): nearest live subreaper ancestor, else 1
    for p in procs.values():
        if p.creator is None or p.creator not in procs:
            continue
        c = procs[p.creator]
        if c.end is not None and p.end is not None and c.end < p.end:
            anc, to = c, 1
            while anc.creator in procs:
                anc = procs[anc.creator]
                if getattr(anc, "subreaper", False) and anc.end is not None and anc.end > c.end:
                    to = anc.pid
                    break
            notes["reparented"].append(
                {"pid": p.pid, "creating_parent": c.pid, "reassigned_parent": to, "at_ts_ns": c.end}
            )

    # ---- tracer exit consistency
    root_exit = None
    if root_tid is not None:
        segs = by_tid[root_tid][0]
        for ev in segs[0][::-1]:
            if ev.kind == "exit":
                root_exit = ev.tail
                break
    tracer["root_exit"] = root_exit
    tracer["tracer_exit"] = parse_int(status.get("tracer_exit"))
    if (
        tracer["status"] != "NOT_RUN"
        and root_exit is not None
        and tracer["tracer_exit"] is not None
    ):
        m = re.match(r"exited with (\d+)", root_exit)
        expected = int(m.group(1)) if m else None
        km = re.match(r"killed by (\w+)", root_exit)
        if expected is not None and tracer["tracer_exit"] != expected:
            problem("TRACER_EXIT_MISMATCH", f"tracer {tracer['tracer_exit']} vs root {root_exit}")
        if km and tracer["tracer_exit"] < 128:
            problem("TRACER_EXIT_MISMATCH", f"tracer {tracer['tracer_exit']} vs root {root_exit}")
    elif tracer["status"] != "NOT_RUN" and tracer["tracer_exit"] is None:
        problem("TRACER_EXIT_UNKNOWN", "no tracer exit status recorded")
    if tracer["status"] != "NOT_RUN" and tracer["problems"]:
        codes = {x["code"] for x in tracer["problems"]}
        for code in (
            "TRACER_KILLED",
            "HARNESS_INCOMPLETE",
            "NO_TRACE",
            "TRUNCATED",
            "TRACER_EXIT_MISMATCH",
            "INCONSISTENT",
            "TRACER_MESSAGES",
            "TRACER_EXIT_UNKNOWN",
            "ANALYZER_ERROR",
        ):
            if code in codes:
                tracer["status"] = code
                break
    complete = tracer["status"] == "OK"

    # ---- contract projection
    evidence = {
        "schema": SCHEMA,
        "symbols": {},
        "controller_trace": {},
        "filesystem_snapshot": {},
        "harness_observations": {},
    }
    if root_tid is not None:
        evidence["symbols"]["HARNESS_CONTROLLER_PID"] = root_tid
    if run_dir:
        evidence["symbols"]["HARNESS_RUN_DIR"] = run_dir
    if not complete:
        for f in TRACE_FIELDS:
            missing[f] = "trace not complete: " + tracer["status"]
    else:
        if io_uring_ok:
            missing["filesystem_snapshot.created_paths"] = (
                "io_uring_setup succeeded: writes may bypass the trace"
            )
        else:
            evidence["filesystem_snapshot"]["created_paths"] = sorted({r["path"] for r in created})
        signalled = sorted({t for at in attempts for t in at["target_pids"]})
        if unresolved_targets:
            missing["controller_trace.signalled_pids"] = "unresolved signal target(s)"
        else:
            evidence["controller_trace"]["signalled_pids"] = signalled
        tree, ambiguous = {}, []
        for p in procs.values():
            parent = procs[p.creator].pid if p.creator in procs else (tracer_pid or 0)
            prev = tree.get(str(p.pid))
            if prev is not None and prev != parent:
                ambiguous.append(p.pid)
            tree[str(p.pid)] = parent
        for pid in signalled:
            if str(pid) not in tree:
                tree[str(pid)] = harness[pid].harness_parent if pid in harness else 0
                # a harness parent outside the tree must be resolvable too (closure)
                hp = tree[str(pid)]
                while hp and str(hp) not in tree and hp in harness:
                    tree[str(hp)] = harness[hp].harness_parent
                    hp = harness[hp].harness_parent
        if ambiguous:
            missing["process_tree"] = (
                f"pid reuse with different creating parents: {sorted(set(ambiguous))}"
            )
        else:
            evidence["process_tree"] = tree
        evidence["harness_observations"]["max_concurrent_children"] = max_concurrent(
            procs, root_key
        )
        evidence["harness_observations"]["shell_launches"] = shell_launches
        fetch_gap = None
        if status.get("trace_network") != "1":
            fetch_gap = "network syscalls were not in the traced set"
        elif io_uring_ok:
            fetch_gap = "io_uring_setup succeeded: connects may bypass the trace"
        elif unreadable_destinations:
            fetch_gap = "network destination address unreadable in the trace"
        if fetch_gap:
            missing["harness_observations.data_fetch_attempts"] = fetch_gap
        else:
            evidence["harness_observations"]["data_fetch_attempts"] = len(fetch_attempts)
    notes["network_attempts"] = fetch_attempts[:50]
    notes["unreadable_network_destinations"] = unreadable_destinations[:20]
    ram, ram_reason, notes["ram_sampling"] = min_free_ram(outdir, status)
    if ram is None:
        missing[RAM_FIELD] = ram_reason
    else:
        evidence["harness_observations"]["min_free_ram_while_running_gib"] = ram
    evidence["harness_observations"]["signal_capture_exhaustive"] = bool(
        complete and not unresolved_targets
    )
    evidence["harness_observations"]["tracer_status"] = tracer
    outside = []
    if run_dir and "created_paths" in evidence["filesystem_snapshot"]:
        rd = posixpath.normpath(run_dir)
        outside = [
            p
            for p in evidence["filesystem_snapshot"]["created_paths"]
            if not (p == rd or p.startswith(rd + "/"))
        ]
    notes["created_outside_run_dir"] = outside
    notes["created_then_deleted"] = sorted({r["path"] for r in created if r["deleted_later"]})
    evidence["missing"] = missing
    evidence["harness_notes"] = notes
    evidence["detail"] = {
        "processes": {
            p.key: {
                "pid": p.pid,
                "creating_parent": procs[p.creator].pid if p.creator in procs else None,
                "creator_key": p.creator,
                "start_ts_ns": p.start,
                "end_ts_ns": p.end,
                "threads": sorted(p.threads),
                "execs": p.execs,
            }
            for p in procs.values()
        },
        "harness_processes": {
            str(h.pid): {"label": h.label, "parent": h.harness_parent} for h in harness.values()
        },
        "signal_attempts": attempts,
        "created": created,
        "trace_files": len(files),
        "events": len(all_events),
    }
    return evidence


def roots_has(roots, tid):
    return tid in roots


def min_free_ram(outdir, status):
    """-> (GiB or None, missing reason, note). Only readings strictly inside (run_start,
    run_end) count: a reading before the tree started or after it exited is never the
    running minimum."""
    path = Path(outdir) / "ram_samples.txt"
    note = {
        "source": "harness ram_sampler.py: /proc/meminfo MemAvailable (container VM figure)",
        "file": path.name,
        "max_gap_s": RAM_MAX_GAP_S,
    }
    try:
        start, end = float(status["run_start"]), float(status["run_end"])
    except (KeyError, ValueError):
        return None, "run window not recorded (run_start/run_end)", note
    if not path.exists():
        return None, "no harness RAM sampler readings", note
    readings, header, footer, errors = [], None, False, []
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        if line.startswith("#start"):
            header = line
        elif line.startswith("#end"):
            footer = True
        elif line.startswith("#"):
            errors.append(line[:200])
        elif line.strip():
            parts = line.split()
            try:
                readings.append((float(parts[0]), int(parts[1])))
            except (IndexError, ValueError):
                errors.append(line[:200])
    note.update(header=header, readings=len(readings), errors=errors[:10])
    if header is None or not footer or errors:
        return None, "harness RAM sampler did not finish cleanly", note
    inside = sorted(r for r in readings if start < r[0] < end)
    note["readings_in_run_window"] = len(inside)
    if not inside:
        return None, "no RAM reading while the tree was running", note
    gaps = [inside[0][0] - start, end - inside[-1][0]]
    gaps += [b[0] - a[0] for a, b in zip(inside, inside[1:], strict=False)]
    note["largest_gap_s"] = round(max(gaps), 3)
    if max(gaps) > RAM_MAX_GAP_S:
        return None, f"RAM readings leave a {max(gaps):.3f}s gap in the run window", note
    return min(kib for _, kib in inside) / 1024**2, None, note


def record_signal(
    ev,
    pkey,
    procs,
    harness,
    pgid,
    pidfds,
    alive_incarnation,
    thread_owner,
    attempts,
    notes,
    unresolved,
    root_tid,
):
    a, name = ev.args, ev.name
    sender = procs[pkey]
    sig, scope, targets = None, "pid", []
    try:
        if name == "kill":
            raw, sig = parse_int(a[0]), a[1]
        elif name == "tkill":
            raw, sig = parse_int(a[0]), a[1]
        elif name in ("tgkill", "rt_tgsigqueueinfo"):
            raw, sig = parse_int(a[0]), a[2]
        elif name == "rt_sigqueueinfo":
            raw, sig = parse_int(a[0]), a[1]
        else:  # pidfd_send_signal(fd, sig, info, flags)
            sig = a[1]
            m = re.search(r"pid:(\d+)", a[0])
            fd = parse_int(re.match(r"-?\d+", a[0]).group(0)) if re.match(r"-?\d+", a[0]) else None
            raw = int(m.group(1)) if m else pidfds.get(pkey, {}).get(fd)
    except (IndexError, AttributeError):
        raw = None
    entry = {
        "sender_pid": sender.pid,
        "sender": pkey,
        "syscall": name,
        "raw_args": ev.raw_args[:160],
        "signal": sig,
        "result": "ok" if ev.ok else (ev.ret + " " + ev.tail).strip(),
        "ts_ns": ev.ts,
    }
    if sig == "0":
        notes["signal_zero_probes"].append(entry)
        return
    if raw is None:
        entry.update(scope="unresolved", target_pids=[])
        unresolved.append(entry)
        attempts.append(entry)
        return
    if name == "tkill":
        owner = thread_owner(raw, ev.ts)
        targets = [owner.pid] if owner else [raw]
    elif name in ("tgkill", "rt_tgsigqueueinfo"):
        targets = [raw]
    elif raw > 0:
        targets = [raw]
    else:
        if raw == 0:
            g, scope = pgid.get(pkey), "own process group"
        elif raw == -1:
            g, scope = None, "all"
        else:
            g, scope = -raw, f"process group {-raw}"
        members = []
        for p in list(procs.values()) + list(harness.values()):
            alive = p.start is not None and p.start <= ev.ts and (p.end is None or ev.ts <= p.end)
            if not alive:
                continue
            if raw == -1:
                if p.pid != sender.pid and p.pid != 1:
                    members.append(p.pid)
            else:
                pg = getattr(p, "pgid", None) if p.harness else pgid.get(p.key)
                if pg is None:
                    entry.setdefault("unknown_pgid_members", []).append(p.pid)
                elif pg == g:
                    members.append(p.pid)
        if g is None and raw != -1:
            entry.update(scope="unresolved own group", target_pids=[])
            unresolved.append(entry)
            attempts.append(entry)
            return
        if entry.get("unknown_pgid_members"):
            unresolved.append(entry)
        targets = sorted(set(members))
    entry.update(scope=scope, target_pids=targets)
    for t in targets:
        inc = alive_incarnation(t, ev.ts)
        entry.setdefault("target_keys", []).append(inc.key if inc else f"unknown:{t}")
    if not ev.ok:
        notes["failed_signal_attempts"].append(entry)
    attempts.append(entry)


def max_concurrent(procs, root_key):
    points = []
    for p in procs.values():
        if p.creator == root_key and p.start is not None and p.end is not None:
            points.append((p.start, 1))
            points.append((p.end, -1))
    points.sort(key=lambda x: (x[0], x[1]))  # an exit at t frees the slot before a start at t
    best = cur = 0
    for _, d in points:
        cur += d
        best = max(best, cur)
    return best


def main(argv):
    import argparse

    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("outdir")
    ap.add_argument("--run-dir")
    ap.add_argument("--out")
    args = ap.parse_args(argv[1:])
    evidence = analyze(args.outdir, args.run_dir)
    text = json.dumps(evidence, indent=1, sort_keys=False, default=str)
    if args.out:
        Path(args.out).write_text(text + "\n", encoding="utf-8")
    summary = {
        "tracer_status": evidence["harness_observations"]["tracer_status"]["status"],
        "HARNESS_CONTROLLER_PID": evidence["symbols"].get("HARNESS_CONTROLLER_PID"),
        "created_paths": len(evidence["filesystem_snapshot"].get("created_paths", []))
        if "created_paths" in evidence["filesystem_snapshot"]
        else "MISSING",
        "signalled_pids": evidence["controller_trace"].get("signalled_pids", "MISSING"),
        "max_concurrent_children": evidence["harness_observations"].get(
            "max_concurrent_children", "MISSING"
        ),
        "shell_launches": evidence["harness_observations"].get("shell_launches", "MISSING"),
        "data_fetch_attempts": evidence["harness_observations"].get(
            "data_fetch_attempts", "MISSING"
        ),
        "min_free_ram_while_running_gib": evidence["harness_observations"].get(
            "min_free_ram_while_running_gib", "MISSING"
        ),
        "missing": evidence["missing"],
        "created_outside_run_dir": evidence["harness_notes"]["created_outside_run_dir"],
    }
    print(json.dumps(summary, indent=1))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
