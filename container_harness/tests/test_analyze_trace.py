"""Analyzer unit tests over synthetic ``strace -ff -y --timestamps=unix,ns`` captures (no docker)."""

import hashlib
import json
import sys
from pathlib import Path

import pytest

HERE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(HERE))

import analyze_trace as A  # noqa: E402

ROOT, TRACER, ENTRY = 100, 90, 80
STATUS = {
    "entry_pid": ENTRY,
    "entry_pgid": ENTRY,
    "init_pgid": 1,
    "tracer_pid": TRACER,
    "tracer_pgid": ENTRY,
    "root_cwd": "/work/run",
    "run_dir": "/work/run",
    # entry.sh: network syscalls are in the strace set; the run window brackets every event.
    "trace_network": 1,
    "run_start": "1699999999.900000000",
    "run_end": "1700000001.000000000",
}
GIB = 1024**2  # kiB per GiB (MemAvailable is in kiB)


def ram_lines(start=1699999999.85, end=1700000001.05, step=0.2, kib=8 * GIB, footer=True):
    """A clean harness RAM sampler file covering [start, end]."""
    lines, t = ["#start interval=0.2"], start
    while t <= end + 1e-9:
        lines.append(f"{t:.6f} {kib}")
        t += step
    if footer:
        lines.append("#end")
    return lines


BASELINE = [
    "/",
    "/work",
    "/work/run",
    "/tmp",
    "/dev",
    "/dev/null",
    "/usr",
    "/usr/lib",
    "/etc",
    "/etc/hosts",
    "/lib",
]


class Capture:
    """Builds one /harness-out directory. Lines are added in global time order."""

    def __init__(self, tmp_path):
        self.dir = tmp_path / "harness-out"
        (self.dir / "trace").mkdir(parents=True)
        self.files, self.t = {}, 0
        self.status = dict(STATUS, tracer_exit=0, complete=1)
        self.stderr = ""
        self.baseline = list(BASELINE)
        self.links = {"/lib": "usr/lib"}
        self.ram = ram_lines()  # None: the sampler wrote no file

    def __call__(self, tid, text):
        self.t += 1000
        self.files.setdefault(tid, []).append(f"1700000000.{self.t:09d} {text}")
        return self

    def exit(self, tid, code=0):
        return self(tid, f"+++ exited with {code} +++")

    def write(self):
        for tid, lines in self.files.items():
            (self.dir / "trace" / f"t.{tid}").write_text("\n".join(lines) + "\n", encoding="utf-8")
        (self.dir / "harness_status.txt").write_text(
            "".join(f"{k}={v}\n" for k, v in self.status.items()), encoding="utf-8"
        )
        (self.dir / "baseline.txt").write_text("\n".join(self.baseline) + "\n", encoding="utf-8")
        (self.dir / "baseline_links.tsv").write_text(
            "".join(f"{k}\t{v}\n" for k, v in self.links.items()), encoding="utf-8"
        )
        (self.dir / "root.stderr").write_text(self.stderr, encoding="utf-8")
        if self.ram is not None:
            (self.dir / "ram_samples.txt").write_text("\n".join(self.ram) + "\n", encoding="utf-8")
        return A.analyze(self.dir, self.status.get("run_dir"))


def child(cap, parent, pid, vfork=True):
    flags = (
        "CLONE_VM|CLONE_VFORK|CLONE_CLEAR_SIGHAND"
        if vfork
        else "CLONE_CHILD_CLEARTID|CLONE_CHILD_SETTID"
    )
    cap(
        parent,
        f"clone3({{flags={flags}, exit_signal=SIGCHLD, stack=0x7f0, stack_size=0x9000}}, 88) = {pid}",
    )


def thread(cap, parent, tid):
    cap(
        parent,
        "clone3({flags=CLONE_VM|CLONE_FS|CLONE_FILES|CLONE_SIGHAND|CLONE_THREAD|CLONE_SYSVSEM"
        f"|CLONE_SETTLS|CLONE_PARENT_SETTID|CLONE_CHILD_CLEARTID, exit_signal=0}} => {{parent_tid=[{tid}]}}, 88) = {tid}",
    )


def start(cap):
    return cap(
        ROOT, 'execve("/usr/local/bin/python", ["python", "ctl.py"], 0x7ffd /* 9 vars */) = 0'
    )


def test_created_paths_semantics(tmp_path):
    cap = start(Capture(tmp_path))
    cap(
        ROOT,
        'openat(AT_FDCWD</work/run>, "new.txt", O_WRONLY|O_CREAT|O_TRUNC|O_CLOEXEC, 0666) = 3</work/run/new.txt>',
    )
    cap(ROOT, 'openat(AT_FDCWD</work/run>, "/etc/hosts", O_WRONLY|O_CREAT, 0666) = 3</etc/hosts>')
    cap(ROOT, 'openat(AT_FDCWD</work/run>, "ro.txt", O_RDONLY|O_CLOEXEC) = 3</work/run/ro.txt>')
    cap(
        ROOT,
        'openat(AT_FDCWD</work/run>, "denied.txt", O_WRONLY|O_CREAT, 0666) = -1 EACCES (Permission denied)',
    )
    cap(ROOT, 'mkdir("sub", 0777) = 0')
    cap(
        ROOT,
        'openat(AT_FDCWD</work/run>, "state.tmp", O_WRONLY|O_CREAT|O_EXCL, 0600) = 4</work/run/state.tmp>',
    )
    cap(ROOT, 'rename("state.tmp", "state.json") = 0')
    cap(
        ROOT,
        'openat(AT_FDCWD</work/run>, "gone.txt", O_WRONLY|O_CREAT, 0666) = 5</work/run/gone.txt>',
    )
    cap(ROOT, 'unlink("gone.txt") = 0')
    cap(ROOT, 'openat(AT_FDCWD</work/run>, "/lib/x.so", O_WRONLY|O_CREAT, 0666) = 6</usr/lib/x.so>')
    cap(ROOT, 'symlinkat("new.txt", AT_FDCWD</work/run>, "link") = 0')
    cap(ROOT, 'bind(7<socket:[1]>, {sa_family=AF_UNIX, sun_path="/tmp/ctl.sock"}, 110) = 0')
    cap(ROOT, 'bind(8<socket:[2]>, {sa_family=AF_UNIX, sun_path=@"abstract"}, 12) = 0')
    cap(ROOT, 'mkdir("/proc/self/x", 0777) = 0')
    ev = cap.exit(ROOT).write()
    assert ev["schema"] == "simkit.container-harness-evidence/1"
    assert ev["missing"] == {}
    assert ev["filesystem_snapshot"]["created_paths"] == sorted(
        [
            "/work/run/new.txt",
            "/work/run/sub",
            "/work/run/state.tmp",
            "/work/run/state.json",
            "/work/run/gone.txt",
            "/usr/lib/x.so",
            "/work/run/link",
            "/tmp/ctl.sock",
        ]
    )
    notes = ev["harness_notes"]
    assert [n["path"] for n in notes["o_creat_on_existing"]] == ["/etc/hosts"]
    assert "/work/run/gone.txt" in notes["created_then_deleted"]
    assert notes["created_outside_run_dir"] == ["/tmp/ctl.sock", "/usr/lib/x.so"]
    assert notes["excluded_pseudo_fs"][0]["path"] == "/proc/self/x"
    assert ev["symbols"] == {"HARNESS_CONTROLLER_PID": ROOT, "HARNESS_RUN_DIR": "/work/run"}
    assert ev["harness_observations"]["tracer_status"]["status"] == "OK"


def test_o_tmpfile_unnamed_excluded_but_linked_path_counts(tmp_path):
    cap = start(Capture(tmp_path))
    cap(ROOT, 'openat(AT_FDCWD</work/run>, "/tmp", O_WRONLY|O_TMPFILE, 0600) = 3</tmp/#123>')
    cap(
        ROOT,
        'linkat(AT_FDCWD</work/run>, "/proc/self/fd/3", AT_FDCWD</work/run>, "/tmp/linked.txt", AT_SYMLINK_FOLLOW) = 0',
    )
    cap(ROOT, 'openat(AT_FDCWD</work/run>, "/tmp", O_RDWR|O_EXCL|O_TMPFILE, 0600) = 4</tmp/#124>')
    cap(ROOT, 'memfd_create("anon", MFD_CLOEXEC) = 5</memfd:anon (deleted)>')
    ev = cap.exit(ROOT).write()
    assert ev["filesystem_snapshot"]["created_paths"] == ["/tmp/linked.txt"]
    assert len(ev["harness_notes"]["o_tmpfile_unnamed"]) == 2
    assert ev["harness_notes"]["memfd_unnamed"][0]["name"] == "anon"


def test_process_tree_uses_creating_parent_and_notes_reassignment(tmp_path):
    cap = start(Capture(tmp_path))
    child(cap, ROOT, 101)
    cap(101, 'execve("/usr/local/bin/python", ["python", "-c", "..."], 0x7f /* 9 vars */) = 0')
    child(cap, 101, 102)
    cap(102, "setsid() = 102")
    cap.exit(101)  # 102 is orphaned: kernel reassigns it to PID 1
    cap(102, 'openat(AT_FDCWD</tmp>, "orphan.txt", O_WRONLY|O_CREAT, 0666) = 3</tmp/orphan.txt>')
    cap(ROOT, "kill(102, SIGTERM) = 0")
    cap(102, "--- SIGTERM {si_signo=SIGTERM, si_code=SI_USER, si_pid=100, si_uid=1000} ---")
    cap.exit(102)
    cap(
        ROOT,
        "--- SIGCHLD {si_signo=SIGCHLD, si_code=CLD_EXITED, si_pid=101, si_uid=1000, si_status=0} ---",
    )
    ev = cap.exit(ROOT).write()
    assert ev["process_tree"] == {"100": TRACER, "101": 100, "102": 101}
    assert ev["controller_trace"]["signalled_pids"] == [102]
    rep = ev["harness_notes"]["reparented"]
    assert rep == [
        {"pid": 102, "creating_parent": 101, "reassigned_parent": 1, "at_ts_ns": rep[0]["at_ts_ns"]}
    ]
    assert ev["harness_notes"]["deliveries_unmatched_to_attempts"] == []
    assert ev["harness_notes"]["kernel_signal_deliveries"] == {"SIGCHLD/CLD_EXITED": 1}
    assert "/tmp/orphan.txt" in ev["filesystem_snapshot"]["created_paths"]


def test_threads_are_not_children_and_concurrency_counts_overlap(tmp_path):
    cap = start(Capture(tmp_path))
    thread(cap, ROOT, 150)
    thread(cap, ROOT, 151)
    child(cap, ROOT, 101, vfork=False)
    child(cap, ROOT, 102, vfork=False)
    cap(101, 'openat(AT_FDCWD</work/run>, "a", O_WRONLY|O_CREAT, 0666) = 3</work/run/a>')
    cap(102, 'openat(AT_FDCWD</work/run>, "b", O_WRONLY|O_CREAT, 0666) = 3</work/run/b>')
    cap(150, 'openat(AT_FDCWD</work/run>, "t", O_WRONLY|O_CREAT, 0666) = 3</work/run/t>')
    cap.exit(101)
    cap.exit(102)
    child(cap, ROOT, 103, vfork=False)
    cap.exit(103)
    cap.exit(150)
    cap.exit(151)
    ev = cap.exit(ROOT).write()
    assert ev["harness_observations"]["max_concurrent_children"] == 2
    assert set(ev["process_tree"]) == {"100", "101", "102", "103"}
    assert "/work/run/t" in ev["filesystem_snapshot"]["created_paths"]


def test_signal_forms_group_pidfd_tgkill_zero_and_failed(tmp_path):
    cap = start(Capture(tmp_path))
    child(cap, ROOT, 101)
    cap(101, "setsid() = 101")
    child(cap, 101, 102)
    child(cap, ROOT, 103)
    child(cap, ROOT, 104)
    cap(ROOT, "kill(-101, SIGKILL) = 0")  # group: 101 and 102
    cap(ROOT, "pidfd_open(103, 0) = 5<anon_inode:[pidfd]>")
    cap(ROOT, "pidfd_send_signal(5<anon_inode:[pidfd]>, SIGTERM, NULL, 0) = 0")
    cap(ROOT, "pidfd_send_signal(6<pid:104>, SIGTERM, NULL, 0) = 0")
    cap(ROOT, "kill(100, 0) = 0")  # existence probe: no signal sent
    cap(ROOT, "tgkill(100, 100, SIGUSR2) = 0")
    cap(ROOT, "kill(9999, SIGTERM) = -1 ESRCH (No such process)")
    cap(ROOT, f"kill({ENTRY}, SIGUSR1) = -1 EPERM (Operation not permitted)")
    for pid in (101, 102):
        cap(pid, "+++ killed by SIGKILL +++")
    for pid in (103, 104):
        cap(pid, "--- SIGTERM {si_signo=SIGTERM, si_code=SI_USER, si_pid=100, si_uid=1000} ---")
        cap.exit(pid, 143)
    cap(ROOT, "rt_sigprocmask(SIG_SETMASK, [], NULL, 8) = 0")
    cap(ROOT, "--- SIGUSR2 {si_signo=SIGUSR2, si_code=SI_TKILL, si_pid=100, si_uid=1000} ---")
    cap(ROOT, "prctl(PR_SET_PDEATHSIG, SIGKILL) = 0")
    cap(ROOT, "alarm(5) = 0")
    ev = cap.exit(ROOT).write()
    assert ev["missing"] == {}
    assert ev["controller_trace"]["signalled_pids"] == sorted(
        [101, 102, 103, 104, 100, 9999, ENTRY]
    )
    tree = ev["process_tree"]
    assert tree["9999"] == 0  # never seen in the namespace: oracle FAILs it
    assert tree[str(ENTRY)] == 1 and tree["1"] == 0  # harness-known chain, outside the tree
    notes = ev["harness_notes"]
    assert len(notes["signal_zero_probes"]) == 1
    assert len(notes["failed_signal_attempts"]) == 2
    assert {s["call"] for s in notes["kernel_signal_setups"]} == {
        "prctl(PR_SET_PDEATHSIG)",
        "alarm",
    }
    assert notes["sigkill_deaths_without_attempt"] == []
    assert ev["harness_observations"]["signal_capture_exhaustive"] is True


def test_user_delivery_without_attempt_makes_signals_missing(tmp_path):
    cap = start(Capture(tmp_path))
    child(cap, ROOT, 101)
    cap(101, "--- SIGTERM {si_signo=SIGTERM, si_code=SI_USER, si_pid=100, si_uid=1000} ---")
    cap.exit(101, 143)
    ev = cap.exit(ROOT).write()
    assert "signalled_pids" not in ev["controller_trace"]
    assert "controller_trace.signalled_pids" in ev["missing"]
    assert ev["harness_observations"]["signal_capture_exhaustive"] is False
    assert ev["filesystem_snapshot"]["created_paths"] == []


@pytest.mark.parametrize(
    "mutate,code",
    [
        (lambda c: c.files[ROOT].pop(), "TRUNCATED"),  # tracer died
        (lambda c: c.status.update(tracer_exit=137), "TRACER_EXIT_MISMATCH"),
        (lambda c: c.status.pop("complete"), "HARNESS_INCOMPLETE"),
        (lambda c: c.status.update(harness_killed_tracer=1), "TRACER_KILLED"),
        (
            lambda c: setattr(
                c, "stderr", "strace: ptrace(PTRACE_SEIZE, 7): Operation not permitted\n"
            ),
            "TRACER_MESSAGES",
        ),
        (lambda c: c.files.pop(101), "INCONSISTENT"),  # clone without file
        (lambda c: c.status.pop("tracer_exit"), "TRACER_EXIT_UNKNOWN"),
    ],
)
def test_untrusted_trace_makes_every_field_missing_never_empty(tmp_path, mutate, code):
    cap = start(Capture(tmp_path))
    child(cap, ROOT, 101)
    cap.exit(101)
    cap.exit(ROOT)
    mutate(cap)
    ev = cap.write()
    assert ev["harness_observations"]["tracer_status"]["status"] == code
    for field in A.TRACE_FIELDS:
        section, _, key = field.partition(".")
        container = ev.get(section, {}) if key else ev
        assert (key or section) not in container, field  # absent, never []
        assert field in ev["missing"]
    assert ev["harness_observations"]["signal_capture_exhaustive"] is False


def test_benign_strace_message_is_not_a_problem(tmp_path):
    cap = start(Capture(tmp_path))
    cap.stderr = "strace: Process 101 attached\ntree output\n"
    ev = cap.exit(ROOT).write()
    assert ev["harness_observations"]["tracer_status"]["status"] == "OK"


def test_untraced_baseline_reports_not_run(tmp_path):
    cap = Capture(tmp_path)
    cap.status = dict(STATUS, untraced_exit=0, complete=1)
    cap.status.pop("tracer_pid")
    ev = cap.write()
    assert ev["harness_observations"]["tracer_status"]["status"] == "NOT_RUN"
    assert set(ev["missing"]) == set(A.TRACE_FIELDS)


def test_io_uring_success_makes_created_paths_missing_only(tmp_path):
    cap = start(Capture(tmp_path))
    cap(ROOT, "io_uring_setup(4, 0x7ffc) = 3<anon_inode:[io_uring]>")
    ev = cap.exit(ROOT).write()
    assert "created_paths" not in ev["filesystem_snapshot"]
    assert "filesystem_snapshot.created_paths" in ev["missing"]
    assert ev["controller_trace"]["signalled_pids"] == []


def test_io_uring_refused_is_noted(tmp_path):
    cap = start(Capture(tmp_path))
    cap(ROOT, "io_uring_setup(4, 0x7ffc) = -1 EPERM (Operation not permitted)")
    ev = cap.exit(ROOT).write()
    assert ev["filesystem_snapshot"]["created_paths"] == []
    assert ev["harness_notes"]["io_uring_calls"][0]["ret"] == "-1"


def test_shell_launches_and_relative_paths_after_chdir(tmp_path):
    cap = start(Capture(tmp_path))
    child(cap, ROOT, 101)
    cap(101, 'execve("/bin/sh", ["/bin/sh", "-c", "echo s > x"], 0x7f /* 9 vars */) = 0')
    cap(101, 'chdir("/tmp") = 0')
    cap(101, 'mkdir("d", 0777) = 0')
    cap(
        101,
        'execve("/usr/bin/nosuch", ["nosuch"], 0x7f /* 9 vars */) = -1 ENOENT (No such file or directory)',
    )
    cap.exit(101)
    ev = cap.exit(ROOT).write()
    assert ev["harness_observations"]["shell_launches"] == 1
    assert ev["filesystem_snapshot"]["created_paths"] == ["/tmp/d"]
    assert ev["harness_notes"]["exec_failures"][0]["path"] == "/usr/bin/nosuch"


def test_pid_reuse_is_keyed_by_generation(tmp_path):
    cap = start(Capture(tmp_path))
    child(cap, ROOT, 101)
    cap.exit(101)
    child(cap, ROOT, 101)  # same pid, second incarnation (-A appends to t.101)
    cap(101, 'openat(AT_FDCWD</work/run>, "second", O_WRONLY|O_CREAT, 0666) = 3</work/run/second>')
    cap.exit(101)
    ev = cap.exit(ROOT).write()
    assert ev["missing"] == {}
    assert ev["harness_notes"]["pid_reuse"] == [{"pid": 101, "generations": 2}]
    assert set(ev["detail"]["processes"]) == {"100#0", "101#0", "101#1"}


def test_unparseable_event_args_degrade_to_missing(tmp_path):
    cap = start(Capture(tmp_path))
    cap(ROOT, "renameat2() = 0")  # malformed: no arguments
    ev = cap.exit(ROOT).write()
    assert ev["harness_observations"]["tracer_status"]["status"] == "ANALYZER_ERROR"
    assert "filesystem_snapshot.created_paths" in ev["missing"]


def test_unfinished_resumed_lines_are_merged(tmp_path):
    cap = start(Capture(tmp_path))
    cap(ROOT, 'openat(AT_FDCWD</work/run>, "slow", O_WRONLY|O_CREAT, 0666 <unfinished ...>')
    cap(ROOT, "<... openat resumed>) = 3</work/run/slow>")
    ev = cap.exit(ROOT).write()
    assert ev["filesystem_snapshot"]["created_paths"] == ["/work/run/slow"]


def test_confined_case_run_creates_nothing_outside_the_run_dir(tmp_path):
    """The container case layout after the write-allow-list fix: case root staged before the
    baseline, controller caches under run/scratch. Every created path is under run/, which is
    what the frozen all_under(created_paths, HARNESS_RUN_DIR) needs."""
    cap = Capture(tmp_path)
    cap.status.update(run_dir="/work/case/run", root_cwd="/opt/ctl")
    cap.baseline += [
        "/opt",
        "/opt/ctl",
        "/work/case",
        "/work/case/run",
        "/work/case/source",
        "/work/case/source/source.zarr",
        "/work/case/harness",
        "/work/case/harness/manifest.json",
        "/work/prepared-fixture.json",
    ]
    run = "/work/case/run"
    lines = [
        'execve("/usr/local/bin/python", ["python", "-m", "case_driver"], 0x7f /* 9 vars */) = 0',
        'openat(AT_FDCWD</opt/ctl>, "/work/prepared-fixture.json", O_RDONLY|O_CLOEXEC)'
        " = 3</work/prepared-fixture.json>",
        f'mkdir("{run}/artifacts", 0777) = 0',
        f'mkdir("{run}/scratch", 0777) = 0',
        f'mkdir("{run}/scratch/matplotlib", 0777) = 0',
        f'openat(AT_FDCWD</opt/ctl>, "{run}/scratch/tmpab12cd", O_RDWR|O_CREAT|O_EXCL, 0600)'
        f" = 3<{run}/scratch/tmpab12cd>",
        f'unlink("{run}/scratch/tmpab12cd") = 0',
        f'openat(AT_FDCWD</opt/ctl>, "{run}/checkpoint.json.tmp", O_WRONLY|O_CREAT|O_TRUNC, 0666)'
        f" = 3<{run}/checkpoint.json.tmp>",
        f'rename("{run}/checkpoint.json.tmp", "{run}/checkpoint.json") = 0',
    ]
    for line in lines:
        cap(ROOT, line)
    child(cap, ROOT, 101)
    cap(101, f'mkdir("{run}/scratch/xdg-data", 0777) = 0')
    cap.exit(101)
    ev = cap.exit(ROOT).write()
    created = ev["filesystem_snapshot"]["created_paths"]
    assert created and all(p.startswith(run + "/") for p in created)
    assert ev["harness_notes"]["created_outside_run_dir"] == []
    # The pre-fix layout (bookkeeping and HOME caches inside the trace) is reported outside.
    cap = start(Capture(tmp_path / "old"))
    cap.status.update(run_dir=run)
    cap.baseline += ["/work/case", run, "/home", "/home/runner"]
    cap(ROOT, 'mkdir("/work/case/harness", 0777) = 0')
    cap(ROOT, 'mkdir("/home/runner/.config", 0777) = 0')
    ev = cap.exit(ROOT).write()
    assert ev["harness_notes"]["created_outside_run_dir"] == [
        "/home/runner/.config",
        "/work/case/harness",
    ]


@pytest.mark.parametrize(
    "line",
    [
        'openat(5, "x.txt", O_WRONLY|O_CREAT, 0666) = 3',  # undecorated dirfd, no -y result
        'mkdirat(7, "d", 0777) = 0',
        'renameat(7, "a", AT_FDCWD</work/run>, "b") = 0',  # unknown source directory
    ],
)
def test_unresolvable_creating_call_makes_created_paths_missing(tmp_path, line):
    cap = start(Capture(tmp_path))
    cap(ROOT, 'openat(AT_FDCWD</work/run>, "ok.txt", O_WRONLY|O_CREAT, 0666) = 3</work/run/ok.txt>')
    cap(ROOT, line)
    ev = cap.exit(ROOT).write()
    assert ev["harness_observations"]["tracer_status"]["status"] == "OK"
    assert "created_paths" not in ev["filesystem_snapshot"]  # absent, never a short list
    assert ev["missing"]["filesystem_snapshot.created_paths"] == (
        "1 creating call(s) with an unresolvable path"
    )
    assert ev["harness_notes"]["unresolved_creations"]
    assert ev["controller_trace"]["signalled_pids"] == []  # other fields unaffected


@pytest.mark.parametrize(
    "line",
    [
        "sendmsg(3<socket:[1]>, 0x7ffd1234, 0) = 5",  # whole msghdr undecoded
        'sendmsg(3<socket:[1]>, {msg_name=0x7ffd5678, msg_namelen=16, msg_iov=[{iov_base="a", '
        "iov_len=1}], msg_iovlen=1, msg_controllen=0, msg_flags=0}, 0) = 1",
        "sendmmsg(3<socket:[1]>, 0x7ffd9abc, 2, 0) = 2",
        'sendmmsg(3<socket:[1]>, [{msg_hdr={msg_name=NULL, msg_namelen=0, msg_iov=[{iov_base="a", '
        "iov_len=1}], msg_iovlen=1, msg_controllen=0, msg_flags=0}, msg_len=1}], 2, 0) = 2",
    ],
)
def test_undecoded_sendmsg_destination_makes_fetch_attempts_missing(tmp_path, line):
    cap = start(Capture(tmp_path))
    cap(ROOT, line)
    ev = cap.exit(ROOT).write()
    assert "data_fetch_attempts" not in ev["harness_observations"]  # never a quiet 0
    assert ev["missing"]["harness_observations.data_fetch_attempts"] == (
        "network destination address unreadable in the trace"
    )


def test_connected_sendmsg_with_null_destination_is_not_an_attempt(tmp_path):
    cap = start(Capture(tmp_path))
    cap(
        ROOT,
        'sendmsg(3<socket:[1]>, {msg_name=NULL, msg_namelen=0, msg_iov=[{iov_base="a", '
        "iov_len=1}], msg_iovlen=1, msg_controllen=0, msg_flags=0}, 0) = 1",
    )
    ev = cap.exit(ROOT).write()
    assert ev["harness_observations"]["data_fetch_attempts"] == 0
    assert ev["missing"] == {}


RELAY = "172.30.0.1:18080"
NET_LINES = [
    (101, "socket(AF_INET, SOCK_STREAM|SOCK_CLOEXEC, IPPROTO_TCP) = 3<socket:[4242]>"),
    (
        101,
        'connect(3<socket:[4242]>, {sa_family=AF_INET, sin_port=htons(443), sin_addr=inet_addr("93.184.216.34")}, 16) = -1 ENETUNREACH (Network is unreachable)',
    ),
    (
        101,
        'connect(4<socket:[4243]>, {sa_family=AF_UNIX, sun_path="/var/run/nscd/socket"}, 110) = -1 ENOENT (No such file or directory)',
    ),
    (
        101,
        'connect(5<socket:[4244]>, {sa_family=AF_UNSPEC, sa_data="\\0\\0\\0\\0\\0\\0\\0\\0\\0\\0\\0\\0\\0\\0"}, 16) = 0',
    ),
    (
        101,
        'sendto(6<socket:[4245]>, "\\x12\\x34"..., 33, MSG_NOSIGNAL, {sa_family=AF_INET6, sin6_port=htons(53), sin6_flowinfo=htonl(0), inet_pton(AF_INET6, "2001:4860:4860::8888", &sin6_addr), sin6_scope_id=0}, 28) = -1 ENETUNREACH (Network is unreachable)',
    ),
    # connected socket: no destination; quoted data that merely looks like an address
    (101, 'sendto(7<socket:[4246]>, "msg_name={sa_family=AF_INET, x", 30, 0, NULL, 0) = 30'),
    (
        101,
        'sendmsg(8<socket:[4247]>, {msg_name={sa_family=AF_INET, sin_port=htons(123), sin_addr=inet_addr("10.0.0.1")}, msg_namelen=16, msg_iov=[{iov_base="msg_name={sa_family=AF_INET6", iov_len=28}], msg_iovlen=1, msg_controllen=0, msg_flags=0}, 0) = -1 ENETUNREACH (Network is unreachable)',
    ),
    (
        101,
        'sendmmsg(9<socket:[4248]>, [{msg_hdr={msg_name={sa_family=AF_INET, sin_port=htons(53), sin_addr=inet_addr("8.8.8.8")}, msg_namelen=16, msg_iov=[{iov_base="a", iov_len=1}], msg_iovlen=1, msg_controllen=0, msg_flags=0}, msg_len=1}, {msg_hdr={msg_name={sa_family=AF_UNIX, sun_path="/dev/log"}, msg_namelen=110, msg_iov=[{iov_base="b", iov_len=1}], msg_iovlen=1, msg_controllen=0, msg_flags=0}, msg_len=1}], 2, 0) = 2',
    ),
    (
        101,
        "sendto(10<socket:[4249]>, {{nlmsg_len=20, nlmsg_type=RTM_GETADDR, nlmsg_flags=NLM_F_REQUEST|NLM_F_DUMP, nlmsg_seq=1, nlmsg_pid=0}, {ifa_family=AF_UNSPEC}}, 20, 0, {sa_family=AF_NETLINK, nl_pid=0, nl_groups=00000000}, 12) = 20",
    ),
]


def network_capture(tmp_path):
    cap = start(Capture(tmp_path))
    child(cap, ROOT, 101)
    for tid, line in NET_LINES:
        cap(tid, line)
    cap.exit(101)
    return cap


def test_data_fetch_attempts_count_non_local_destinations(tmp_path):
    ev = network_capture(tmp_path).exit(ROOT).write()
    assert ev["missing"] == {}
    # connect AF_INET, sendto AF_INET6, sendmsg AF_INET, sendmmsg's AF_INET message: failed
    # attempts count; AF_UNIX / AF_NETLINK / AF_UNSPEC / NULL destinations do not.
    assert ev["harness_observations"]["data_fetch_attempts"] == 4
    notes = ev["harness_notes"]
    assert [(a["call"], a["family"], a["destination"]) for a in notes["network_attempts"]] == [
        ("connect", "AF_INET", "93.184.216.34:443"),
        ("sendto", "AF_INET6", "[2001:4860:4860::8888]:53"),
        ("sendmsg", "AF_INET", "10.0.0.1:123"),
        ("sendmmsg", "AF_INET", "8.8.8.8:53"),
    ]
    assert notes["network_attempts"][0]["result"].startswith("-1 ENETUNREACH")
    assert notes["local_socket_calls"] == 3
    assert notes["sockets_created"] == {"AF_INET": 1}


def test_quiet_run_reports_zero_fetch_attempts_only_when_network_was_traced(tmp_path):
    ev = start(Capture(tmp_path)).exit(ROOT).write()
    assert ev["harness_observations"]["data_fetch_attempts"] == 0
    cap = start(Capture(tmp_path / "untraced-net"))
    cap.status.pop("trace_network")
    ev = cap.exit(ROOT).write()
    assert "data_fetch_attempts" not in ev["harness_observations"]  # absent, never 0
    assert ev["missing"] == {
        "harness_observations.data_fetch_attempts": "network syscalls were not in the traced set"
    }


def test_relay_connect_is_role_transport_only_from_the_controller(tmp_path):
    cap = start(Capture(tmp_path))
    cap.status["relay_endpoint"] = RELAY
    relay = (
        "connect(3<socket:[77]>, {sa_family=AF_INET, sin_port=htons(18080), "
        'sin_addr=inet_addr("172.30.0.1")}, 16) = 0'
    )
    cap(ROOT, relay)
    child(cap, ROOT, 101)
    cap(101, relay)  # a tool reaching the relay is a fetch attempt
    cap.exit(101)
    ev = cap.exit(ROOT).write()
    assert ev["harness_observations"]["data_fetch_attempts"] == 1
    notes = ev["harness_notes"]
    assert [a["pid"] for a in notes["role_transport_connects"]] == [ROOT]
    assert [a["pid"] for a in notes["network_attempts"]] == [101]


@pytest.mark.parametrize(
    "line,reason",
    [
        (
            "connect(3<socket:[5]>, 0x7ffd1234, 16) = -1 EFAULT (Bad address)",
            "network destination address unreadable in the trace",
        ),
        (
            "io_uring_setup(4, 0x7ffc) = 3<anon_inode:[io_uring]>",
            "io_uring_setup succeeded: connects may bypass the trace",
        ),
    ],
)
def test_unobservable_network_makes_fetch_attempts_missing(tmp_path, line, reason):
    cap = start(Capture(tmp_path))
    cap(ROOT, line)
    ev = cap.exit(ROOT).write()
    assert "data_fetch_attempts" not in ev["harness_observations"]
    assert ev["missing"]["harness_observations.data_fetch_attempts"] == reason


def test_min_free_ram_is_the_running_minimum_of_harness_readings(tmp_path):
    cap = start(Capture(tmp_path))
    lines = ram_lines()
    # a dip while running counts; a lower reading after run_end (post-exit) never does
    lines.insert(3, f"1700000000.300000 {3 * GIB}")
    lines.insert(-1, f"1700000001.500000 {1 * GIB}")
    cap.ram = lines
    ev = cap.exit(ROOT).write()
    assert ev["harness_observations"]["min_free_ram_while_running_gib"] == 3.0
    sampling = ev["harness_notes"]["ram_sampling"]
    assert sampling["readings_in_run_window"] >= 5 and sampling["largest_gap_s"] <= 0.21


def test_min_free_ram_is_independent_of_the_tracer(tmp_path):
    cap = start(Capture(tmp_path))
    cap.status["harness_killed_tracer"] = 1
    ev = cap.exit(ROOT).write()
    assert ev["harness_observations"]["tracer_status"]["status"] == "TRACER_KILLED"
    assert ev["harness_observations"]["min_free_ram_while_running_gib"] == 8.0


@pytest.mark.parametrize(
    "ram,status,reason",
    [
        (None, None, "no harness RAM sampler readings"),
        (ram_lines(footer=False), None, "harness RAM sampler did not finish cleanly"),
        (
            ram_lines()[:-1] + ["#error OSError: boom", "#end"],
            None,
            "harness RAM sampler did not finish cleanly",
        ),
        (ram_lines(start=1700000003.0, end=1700000004.0), None, "no RAM reading while"),
        (  # the sampler stalled: its readings stop 2.5 s before the tree exited
            ram_lines(end=1700000000.5),
            {"run_end": "1700000003.000000000"},
            "RAM readings leave a 2.",
        ),
        (ram_lines(), {"run_end": None}, "run window not recorded"),
    ],
)
def test_min_free_ram_missing_unless_the_whole_run_is_covered(tmp_path, ram, status, reason):
    cap = start(Capture(tmp_path))
    cap.ram = ram
    for key, value in (status or {}).items():
        if value is None:
            cap.status.pop(key)
        else:
            cap.status[key] = value
    ev = cap.exit(ROOT).write()
    assert "min_free_ram_while_running_gib" not in ev["harness_observations"]
    assert ev["missing"][A.RAM_FIELD].startswith(reason)


def test_pinned_inputs_match_their_authorities():
    pins = json.loads((HERE / "pinned_inputs.json").read_text(encoding="utf-8"))
    repo = HERE.parent
    for entry in pins["files"]:
        assert (
            hashlib.sha256((repo / entry["repo_path"]).read_bytes()).hexdigest() == entry["sha256"]
        )
    by_role = {e["role"]: e["sha256"] for e in pins["files"]}
    # INPUT-SUPPLEMENT.json cached-onset-e2e-v2_7-inputs/1, files[role=fixture_generator].sha256
    assert (
        by_role["fixture_generator"]
        == "858296c0faeb3982ad31db0c4e0cdd80b99fa30f75f2067a438f924d55768004"
    )
    assert (
        by_role["fixtures_package_init"]
        == "64f3d96dc3345a6609616200a484cc8932eb073315d31af7e521fd34cca915a5"
    )
    assert by_role["profile"] == "e11b6dd98ba07e298ee86bd3f69ac723d05d4ead1a5c169efc2d23e7ff9e4592"
