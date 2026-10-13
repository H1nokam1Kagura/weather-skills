"""Opt-in host feasibility canary for CAP-EXHAUSTIVE-WRITES (Windows, non-elevated).

It probes the write-observation mechanisms this host allows and runs the confinement prototype
(tools/workflow/write_confinement.py) against synthetic children. It records what breaks under
confinement and writes RESULT.json. It never elevates, installs anything, or changes system
settings. The ETW probe is opt-in and only attempts a short real-time session (expected: access
denied). It makes no network or paid calls: claude is only asked for --version and --help.

Not SIMKIT evidence. It does not advertise CAP-EXHAUSTIVE-WRITES.
"""

import argparse
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import uuid
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))

from tools.workflow import write_confinement as wc  # noqa: E402

NO_WINDOW = 0x08000000
PROFILE = Path(os.environ["USERPROFILE"])
SYSROOT = Path(os.environ.get("SystemRoot", r"C:\Windows"))
OUTSIDE_TARGETS = [
    os.environ["TEMP"],
    str(PROFILE),
    str(PROFILE / "AppData" / "LocalLow"),
    str(PROFILE / "AppData" / "Roaming"),
    r"C:\Users\Public",
    r"C:\ProgramData",
    str(SYSROOT / "Temp"),
    str(SYSROOT / "Tasks"),
    str(SYSROOT / "tracing"),
    str(SYSROOT / "System32" / "spool" / "drivers" / "color"),
    r"C:\ProgramData\Microsoft\Crypto\RSA\MachineKeys",
    str(REPO),
    str(REPO / "tools" / "workflow"),
]
SYSTEM_AUDIT_ROOTS = [
    r"C:\Users\Public",
    r"C:\ProgramData",
    str(SYSROOT / "Temp"),
    str(SYSROOT / "Tasks"),
    str(SYSROOT / "tracing"),
    str(SYSROOT / "System32" / "spool"),
    str(SYSROOT / "System32" / "Tasks"),
]
PROBE = r"""
import json, os, sys
out = {}
for t in sys.argv[1:]:
    p = os.path.join(t, "wcc-probe-%s.txt" % os.getpid())
    try:
        with open(p, "w") as f:
            f.write("x")
        out[t] = "WROTE"
        os.remove(p)
    except OSError as e:
        out[t] = type(e).__name__
print(json.dumps(out))
"""
SCENARIO = r"""
import _winapi, ctypes, json, os, subprocess, sys
res = {}
open("inside.txt", "w").write("x")
open("gone.txt", "w").write("x"); os.remove("gone.txt")
open("state.json.tmp", "w").write("{}"); os.replace("state.json.tmp", "state.json")
os.mkdir("sub"); open(os.path.join("sub", "b.txt"), "w").write("x")
probe, targets, harness_pid, outside_dir = sys.argv[1], json.loads(sys.argv[2]), int(sys.argv[3]), sys.argv[4]
r = subprocess.run([sys.executable, "-c", "open('grand.txt','w').write('g')\n" + probe, *targets],
                   capture_output=True, text=True)
res["grandchild_rc"] = r.returncode
res["grandchild_outside"] = json.loads(r.stdout.strip().splitlines()[-1]) if r.stdout.strip() else r.stderr[-300:]
try:
    _winapi.CreateJunction(outside_dir, "j")
    try:
        open(os.path.join("j", "through.txt"), "w").write("x"); res["junction_write_through"] = "WROTE"
    except OSError as e:
        res["junction_write_through"] = type(e).__name__
except OSError as e:
    res["junction_write_through"] = "junction_not_created:" + type(e).__name__
k = ctypes.WinDLL("kernel32", use_last_error=True)
k.OpenProcess.restype = ctypes.c_void_p
res["open_harness_vm_write_dup_handle"] = bool(k.OpenProcess(0x0020 | 0x0040 | 0x0002, False, harness_pid))
print(json.dumps(res))
"""


def sh(argv, timeout=60):
    try:
        p = subprocess.run(
            argv, capture_output=True, text=True, timeout=timeout, creationflags=NO_WINDOW
        )
        return {
            "argv": argv,
            "rc": p.returncode,
            "stdout": p.stdout[-1500:],
            "stderr": p.stderr[-1500:],
        }
    except (OSError, subprocess.TimeoutExpired) as exc:
        return {"argv": argv, "error": f"{type(exc).__name__}: {exc}"}


def host_probes(work, etw_probe):
    facts = wc.token_facts()
    out = {"token": facts, "logon_sid": wc.logon_sid()}
    q = sh(["logman", "query", "-ets"])
    q["session_rows"] = sum(
        1
        for line in q.get("stdout", "").splitlines()[3:]
        if line.strip() and not line.startswith("The command")
    )
    out["etw_session_enumeration"] = q
    if etw_probe:
        name = "simkit-wcc-kfile-" + uuid.uuid4().hex[:8]
        out["etw_kernel_file_start"] = sh(
            ["logman", "start", name, "-p", "Microsoft-Windows-Kernel-File", "0x10", "-rt", "-ets"]
        )
        out["etw_kernel_file_stop"] = sh(["logman", "stop", name, "-ets"])
    out["procmon"] = {
        "on_path": {n: shutil.which(n) for n in ("procmon", "procmon64", "Procmon64a")},
        "known_dirs": {
            d: Path(d).exists()
            for d in (
                r"C:\Program Files\Sysinternals",
                r"C:\SysinternalsSuite",
                r"C:\Tools\Sysinternals",
            )
        },
        "driver_service": sh(["sc", "query", "PROCMON24"]),
        "note": "Process Monitor loads a kernel minifilter driver; loading it requires administrator.",
    }
    out["fsutil_8dot3_query"] = sh(["fsutil", "8dot3name", "query", "C:"])
    out["short_names_enabled_on_work_volume"] = wc.short_names_enabled(work)
    out["locallow_security"] = wc.security_sddl(PROFILE / "AppData" / "LocalLow")
    base_py = Path(sys.base_prefix) / "python.exe"
    out["appcontainer_assessment"] = {
        "prototyped": False,
        "base_python": str(base_py),
        "base_python_dir_security": wc.security_sddl(base_py.parent, wc.DACL_SI),
        "reason": (
            "AppContainer/LPAC tokens can read only objects whose DACL grants ALL APPLICATION "
            "PACKAGES or the package SID. The interpreter, venv, repository and claude CLI all live "
            "under the user profile and carry no such ACE (see base_python_dir_security). Running "
            "the controller there would mean persistent ACL grants on user directories plus a "
            "registered profile. The confined mode (Low IL + write-restricted) gets comparable write "
            "confinement with no persistent state outside the run dir."
        ),
    }
    return out


def launch(mode, run, logs, argv, **kw):
    r = wc.run_confined(
        argv, run, stdout_path=logs / "o.txt", stderr_path=logs / "e.txt", mode=mode, **kw
    )
    r["stdout"] = (
        (logs / "o.txt").read_text(errors="replace")[-3000:] if (logs / "o.txt").exists() else None
    )
    r["stderr"] = (
        (logs / "e.txt").read_text(errors="replace")[-1500:] if (logs / "e.txt").exists() else None
    )
    return r


def new_case(base, name):
    d = base / name
    (d / "run").mkdir(parents=True)
    (d / "logs").mkdir()
    return d / "run", d / "logs"


def release_all(released, run):
    try:
        released.append({"run_dir": str(run), **wc.release(run)})
    except OSError as exc:
        released.append({"run_dir": str(run), "error": str(exc)})


def scenario_suite(base, released, shared_roots):
    py = sys.executable
    out = {}
    outside_dir = base / "outside-target"
    outside_dir.mkdir()

    # 1. full scenario under the prototype, with a scoped precondition audit
    run, logs = new_case(base, "confined-scenario")
    audit = wc.audit_for_mode("confined", run, [base])
    r = launch(
        "confined",
        run,
        logs,
        [
            py,
            "-c",
            SCENARIO,
            PROBE,
            json.dumps(OUTSIDE_TARGETS),
            str(os.getpid()),
            str(outside_dir),
        ],
        precondition=audit,
    )
    r["outside_targets_verdicts"] = (
        json.loads(r["stdout"].strip().splitlines()[-1]) if r["stdout"] else None
    )
    r["outside_dir_untouched"] = not any(outside_dir.iterdir())
    r["scoped_audit"] = audit
    out["confined_scenario"] = r
    release_all(released, run)

    # 2. direct outside-write probe (child, not grandchild)
    run, logs = new_case(base, "confined-outside")
    r = launch("confined", run, logs, [py, "-c", PROBE, *OUTSIDE_TARGETS])
    r["verdicts"] = json.loads(r["stdout"].strip().splitlines()[-1]) if r["stdout"] else None
    out["confined_outside_writes"] = r
    release_all(released, run)

    # 3. env redirect: env-honouring tools land inside the run dir and are recorded
    run, logs = new_case(base, "confined-env-redirect")
    code = (
        "import os, tempfile\n"
        "fd, p = tempfile.mkstemp(); os.write(fd, b'x'); os.close(fd)\n"
        "open(os.path.join(os.path.expanduser('~'), 'home.txt'), 'w').write('x')\n"
        "open(os.path.join(os.environ['APPDATA'], 'roaming.txt'), 'w').write('x')\n"
    )
    out["confined_env_redirect"] = launch(
        "confined", run, logs, [py, "-c", code], env=wc.confined_environment(run)
    )
    release_all(released, run)

    # 4. overflow: tiny buffer + slow reader + burst => exhaustive must be False
    run, logs = new_case(base, "confined-overflow")
    code = (
        "import time\ntime.sleep(0.3)\nfor i in range(4000):\n"
        "    open('f%05d-with-a-long-name-to-fill-the-buffer.txt' % i, 'w').close()\n"
    )
    r = launch(
        "confined",
        run,
        logs,
        [py, "-c", code],
        buffer_bytes=4096,
        read_delay=0.4,
        precondition=wc.audit_for_mode("confined", run, [base / "confined-overflow"]),
    )
    r["created_count"] = len(r["created_paths"] or [])
    r.pop("events", None)
    r["created_paths"] = f"<{r['created_count']} paths elided>"
    out["confined_overflow"] = r
    release_all(released, run)

    # 5. observer failure: the watcher cannot arm => missing evidence (None), tree never launched
    run, logs = new_case(base, "observer-failure")
    original = wc.RunDirWatcher._issue
    wc.RunDirWatcher._issue = lambda self, buf, ov: False
    try:
        r = launch("confined", run, logs, [py, "-c", "open('a.txt','w').write('x')"])
    finally:
        wc.RunDirWatcher._issue = original
    out["observer_failure"] = {
        k: r.get(k)
        for k in ("created_paths", "exhaustive", "exit_code", "not_exhaustive_because", "error")
    }
    out["observer_failure"]["child_file_exists"] = (run / "a.txt").exists()
    release_all(released, run)

    # 6. comparisons: unconfined control, low integrity alone, fresh-SID, logon-only, untrusted
    run, logs = new_case(base, "none-control")
    target = Path(os.environ["TEMP"]) / f"wcc-control-{uuid.uuid4().hex}.txt"
    try:
        r = launch("none", run, logs, [py, "-c", f"open(r'{target}','w').write('x')"])
        r["outside_file_written"] = target.exists()
        r["outside_file_recorded"] = str(target) in (r["created_paths"] or [])
    finally:
        target.unlink(missing_ok=True)
    out["unconfined_control"] = r

    run, logs = new_case(base, "low-integrity")
    r = launch("low_integrity", run, logs, [py, "-c", PROBE, *OUTSIDE_TARGETS])
    r["verdicts"] = json.loads(r["stdout"].strip().splitlines()[-1]) if r["stdout"] else None
    out["low_integrity_alone"] = r
    release_all(released, run)

    cmd = str(SYSROOT / "System32" / "cmd.exe")
    for mode in ("fresh_sid_write_restricted", "confined_logon_only", "untrusted_integrity"):
        res = {}
        for label, argv in (
            ("cmd", [cmd, "/c", "echo x> a.txt"]),
            ("python", [py, "-c", "open('a.txt','w').write('x'); print('ok')"]),
        ):
            run, logs = new_case(base, f"{mode}-{label}")
            r = launch(mode, run, logs, argv)
            res[label] = {
                k: r[k] for k in ("exit_code", "created_paths", "stdout", "stderr", "error")
            }
            res[label]["exit_code_hex"] = (
                hex(r["exit_code"]) if r["exit_code"] is not None else None
            )
            res[label]["token"] = (r["confinement"] or {}).get("token")
            release_all(released, run)
        out[mode] = res

    # 7. pre-existing creatable dirs found by the system audit: a real escape from the token
    #    confinement, so they are watched as shared roots (probe files are removed at once)
    run, logs = new_case(base, "confined-shared-roots")
    audit = wc.audit_for_mode("confined", run, [base, *SYSTEM_AUDIT_ROOTS])
    r = launch(
        "confined",
        run,
        logs,
        [py, "-c", PROBE, *shared_roots],
        precondition=audit,
        shared_watch_roots=shared_roots,
    )
    r["verdicts"] = json.loads(r["stdout"].strip().splitlines()[-1]) if r["stdout"] else None
    r["scoped_audit_creatable"] = audit["creatable_outside_allowed"]
    r["scoped_audit_unreadable_count"] = len(audit["unreadable"])
    out["confined_shared_roots"] = r
    release_all(released, run)
    return out


def breakage(base, released):
    """What still works under the confined token (with and without env redirect)."""
    py = sys.executable
    git = shutil.which("git")
    claude = shutil.which("claude")
    node = shutil.which("node")
    pycache = REPO / "tools" / "workflow" / "__pycache__"

    def pyc_state():
        return (
            sorted((p.name, p.stat().st_mtime_ns) for p in pycache.glob("*.pyc"))
            if pycache.exists()
            else []
        )

    probes = {
        "python_import_controller": [
            py,
            "-c",
            f"import sys; sys.path.insert(0, r'{REPO}'); "
            "import tools.workflow.runner, tools.workflow.agents; print('ok')",
        ],
        "claude_version": [claude, "--version"] if claude else None,
        "claude_help": [claude, "--help"] if claude else None,
        "git_version": [git, "--version"] if git else None,
        "git_status": [git, "-C", str(REPO), "status", "--porcelain"] if git else None,
        "node": [node, "-e", "console.log('ok')"] if node else None,
        "powershell": [
            str(SYSROOT / "System32" / "WindowsPowerShell" / "v1.0" / "powershell.exe"),
            "-NoProfile",
            "-Command",
            "'ok'",
        ],
        "cmd": [str(SYSROOT / "System32" / "cmd.exe"), "/c", "echo ok"],
    }
    out = {}
    for redirect in (False, True):
        for name, argv in probes.items():
            key = f"{name}{'+env_redirect' if redirect else ''}"
            if argv is None:
                out[key] = {"skipped": "executable not found"}
                continue
            run, logs = new_case(base, "break-" + key.replace("+", "-"))
            env = wc.confined_environment(run) if redirect else None
            if not redirect and name == "python_import_controller":
                env = dict(os.environ)
                env.pop("PYTHONDONTWRITEBYTECODE", None)
            before = pyc_state()
            r = launch("confined", run, logs, argv, env=env, timeout=90)
            out[key] = {
                "exit_code": r["exit_code"],
                "exit_code_hex": hex(r["exit_code"]) if r["exit_code"] is not None else None,
                "stdout": (r["stdout"] or "")[:400],
                "stderr": (r["stderr"] or "")[-600:],
                "created_paths": r["created_paths"][:20]
                if r["created_paths"]
                else r["created_paths"],
                "created_count": len(r["created_paths"] or []),
                "repo_pycache_changed": pyc_state() != before,
                "error": r["error"],
            }
            release_all(released, run)
    return out


def summarize(result):
    host, sc = result["host"], result["scenarios"]
    conf = sc["confined_scenario"]
    verdicts = sc["confined_outside_writes"]["verdicts"] or {}
    return {
        "elevated": host["token"]["elevated"],
        "etw_kernel_file_non_admin": (
            "denied"
            if "denied" in json.dumps(host.get("etw_kernel_file_start", {})).lower()
            else (
                "not attempted"
                if "etw_kernel_file_start" not in host
                else "see host.etw_kernel_file_start"
            )
        ),
        "procmon_present": any(host["procmon"]["on_path"].values())
        or any(host["procmon"]["known_dirs"].values()),
        "confined_outside_targets_all_denied": bool(verdicts)
        and set(verdicts.values()) == {"PermissionError"},
        "confined_scenario_created_paths": conf["created_paths"],
        "confined_scenario_exhaustive_in_scoped_audit": conf["exhaustive"],
        "overflow_forces_non_exhaustive": sc["confined_overflow"]["watcher"]["overflow"]
        and not sc["confined_overflow"]["exhaustive"],
        "observer_failure_is_missing": sc["observer_failure"]["created_paths"] is None,
        "unconfined_outside_write_unrecorded": sc["unconfined_control"]["outside_file_written"]
        and not sc["unconfined_control"]["outside_file_recorded"],
        "low_integrity_locallow_writable": (sc["low_integrity_alone"]["verdicts"] or {}).get(
            str(PROFILE / "AppData" / "LocalLow")
        )
        == "WROTE",
        "profile_audit": {
            k: result.get("profile_audit", {}).get(k)
            for k in ("dirs_scanned", "seconds", "complete", "clean")
        }
        if result.get("profile_audit")
        else None,
        "system_audit": {
            k: result["system_audit"].get(k)
            for k in ("dirs_scanned", "seconds", "complete", "clean", "unreadable_count")
        },
        "system_audit_creatable": result["system_audit"]["creatable_outside_allowed"],
        "profile_audit_creatable": (result.get("profile_audit") or {}).get(
            "creatable_outside_allowed"
        ),
        "shared_root_escape": {
            "verdicts": sc["confined_shared_roots"]["verdicts"],
            "recorded": [
                p
                for p in sc["confined_shared_roots"]["created_paths"] or []
                if not wc._is_under(p, sc["confined_shared_roots"]["run_dir"])
            ],
            "attribution_exact": sc["confined_shared_roots"]["attribution_exact"],
            "exhaustive": sc["confined_shared_roots"]["exhaustive"],
            "not_exhaustive_because": sc["confined_shared_roots"]["not_exhaustive_because"],
        },
        "releases_clean": all(x.get("clean") for x in result["released"]),
    }


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--out", type=Path, default=Path(__file__).with_name("write-capture-canary"))
    ap.add_argument(
        "--work", type=Path, default=Path(tempfile.gettempdir()) / "claude" / "wcc-canary"
    )
    ap.add_argument(
        "--etw-probe", action="store_true", help="attempt a real-time Kernel-File session"
    )
    ap.add_argument(
        "--profile-audit", action="store_true", help="AccessCheck the whole user profile (slow)"
    )
    args = ap.parse_args(argv)
    if os.name != "nt":
        raise SystemExit("Windows-only canary")
    args.work.mkdir(parents=True, exist_ok=True)
    base = Path(tempfile.mkdtemp(prefix="run-", dir=args.work))
    started = time.time()
    released = []
    result = {
        "schema": "simkit.write-capture-canary/1",
        "started_unix": started,
        "python": sys.executable,
        "repo": str(REPO),
        "work": str(base),
        "host": host_probes(base, args.etw_probe),
    }
    token = wc.mode_token("confined")
    try:
        result["system_audit"] = wc.audit_create_access(token, SYSTEM_AUDIT_ROOTS)
    finally:
        wc._k.CloseHandle(token)
    shared = result["system_audit"]["creatable_outside_allowed"]
    result["scenarios"] = scenario_suite(base, released, shared)
    result["breakage"] = breakage(base, released)
    token = wc.mode_token("confined")
    try:
        if args.profile_audit:
            result["profile_audit"] = wc.audit_create_access(token, [PROFILE])
    finally:
        wc._k.CloseHandle(token)
    for key in ("system_audit", "profile_audit"):
        a = result.get(key)
        if a:
            a["unreadable_count"] = len(a["unreadable"])
            a["unreadable"] = a["unreadable"][:40]
    result["released"] = released
    result["seconds"] = round(time.time() - started, 1)
    result["summary"] = summarize(result)
    args.out.mkdir(parents=True, exist_ok=True)
    (args.out / "RESULT.json").write_text(
        json.dumps(result, indent=2, default=str) + "\n", encoding="utf-8"
    )
    print(json.dumps(result["summary"], indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
