"""Real-process tests for the write-confinement prototype (Windows only, non-elevated).

Each test launches synthetic Python children under the confined token and checks two things
against the filesystem: what they could write, and what the run-dir watcher recorded.
"""

import json
import os
import sys
import uuid
from pathlib import Path

import pytest

pytestmark = pytest.mark.skipif(os.name != "nt", reason="Windows-only confinement prototype")

if os.name == "nt":
    from tools.workflow import write_confinement as wc

REPO = Path(__file__).resolve().parents[3]

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


@pytest.fixture
def dirs(tmp_path):
    run = tmp_path / "run"
    run.mkdir()
    out = tmp_path / "out"
    out.mkdir()
    yield run, out
    wc.release(run)  # never leave a Low-labelled, logon-granted directory behind


def launch(run, out, code, *args, mode="confined", **kw):
    result = wc.run_confined(
        [sys.executable, "-c", code, *map(str, args)],
        run,
        stdout_path=out / "stdout.txt",
        stderr_path=out / "stderr.txt",
        mode=mode,
        **kw,
    )
    for name in ("stdout", "stderr"):
        f = out / f"{name}.txt"
        result[name] = f.read_text(errors="replace") if f.exists() else None
    return result


def last_json(text):
    return json.loads(text.strip().splitlines()[-1])


def test_inside_creates_deletes_and_renames_are_recorded_and_exhaustive_in_scope(dirs, tmp_path):
    run, out = dirs
    code = (
        "import os\n"
        "open('a.txt','w').write('x')\n"
        "open('gone.txt','w').write('x'); os.remove('gone.txt')\n"
        "open('state.json.tmp','w').write('{}'); os.replace('state.json.tmp','state.json')\n"
        "os.mkdir('sub'); open(os.path.join('sub','b.txt'),'w').write('x')\n"
    )
    audit = wc.audit_for_mode("confined", run, [tmp_path])
    r = launch(run, out, code, precondition=audit)
    assert r["exit_code"] == 0, r["stderr"]
    names = {str(Path(p).relative_to(run)) for p in r["created_paths"]}
    assert names == {"a.txt", "gone.txt", "state.json.tmp", "state.json", r"sub\b.txt"}
    assert [str(Path(p).relative_to(run)) for p in r["created_dirs"]] == ["sub"]
    deleted = {Path(p).name for p in r["created_then_deleted"]}
    assert deleted == {"gone.txt", "state.json.tmp"}
    assert all(os.path.isabs(p) and wc._is_under(p, run) for p in r["created_paths"])
    assert r["exhaustive"] is True, r["not_exhaustive_because"]
    assert r["confinement"]["token_matches_expected"] is True
    assert wc.filesystem_snapshot(r)["exhaustive"] is True


def test_writes_outside_run_dir_fail_and_nothing_appears(dirs):
    run, out = dirs
    profile = Path(os.environ["USERPROFILE"])
    targets = [os.environ["TEMP"], profile, profile / "AppData" / "LocalLow", REPO, out]
    r = launch(run, out, PROBE, *targets)
    assert r["exit_code"] == 0, r["stderr"]
    verdicts = last_json(r["stdout"])
    # Every target has its own verdict: a missing key is not a denial (W2 review :97).
    assert verdicts == {str(t): "PermissionError" for t in targets}, verdicts
    assert r["created_paths"] == []


def test_env_redirect_lands_profile_and_temp_writes_inside_run_dir(dirs):
    run, out = dirs
    code = (
        "import os, tempfile\n"
        "fd, p = tempfile.mkstemp(); os.write(fd, b'x'); os.close(fd)\n"
        "open(os.path.join(os.path.expanduser('~'), 'home.txt'), 'w').write('x')\n"
        "open(os.path.join(os.environ['LOCALAPPDATA'], 'cache.txt'), 'w').write('x')\n"
    )
    r = launch(run, out, code, env=wc.confined_environment(run))
    assert r["exit_code"] == 0, r["stderr"]
    rel = {str(Path(p).relative_to(run)).split(os.sep)[0] for p in r["created_paths"]}
    assert rel == {"tmp", "profile"}
    assert any(p.endswith("home.txt") for p in r["created_paths"])
    assert any(p.endswith("cache.txt") for p in r["created_paths"])


def test_grandchild_is_confined_job_owned_and_recorded(dirs):
    run, out = dirs
    grand = "open('grand.txt','w').write('g')\n" + PROBE
    code = (
        "import subprocess, sys\n"
        "r = subprocess.run([sys.executable, '-c', sys.argv[1], sys.argv[2]],\n"
        "                   capture_output=True, text=True)\n"
        "print(r.stdout.strip().splitlines()[-1])\n"
    )
    r = launch(run, out, code, grand, os.environ["TEMP"])
    assert r["exit_code"] == 0, r["stderr"]
    assert last_json(r["stdout"]) == {os.environ["TEMP"]: "PermissionError"}
    assert [Path(p).name for p in r["created_paths"]] == ["grand.txt"]
    assert len(r["tree_pids"]) >= 2 and r["tree_drained"]


def test_watcher_overflow_makes_record_non_exhaustive(dirs, tmp_path):
    run, out = dirs
    code = "\n".join(
        [
            "import os, time",
            "time.sleep(0.3)",
            "for i in range(4000):",
            "    open('f%05d-with-a-long-name-to-fill-the-buffer.txt' % i, 'w').close()",
        ]
    )
    audit = wc.audit_for_mode("confined", run, [tmp_path])
    r = launch(run, out, code, buffer_bytes=4096, read_delay=0.4, precondition=audit)
    assert r["exit_code"] == 0, r["stderr"]
    assert r["watcher"]["overflow"] is True
    assert r["exhaustive"] is False
    assert any("overflow" in g for g in r["not_exhaustive_because"])
    assert len(r["created_paths"]) < 4000


def test_observer_failure_is_missing_evidence_never_empty(dirs, monkeypatch):
    run, out = dirs
    monkeypatch.setattr(wc.RunDirWatcher, "_issue", lambda self, buf, ov: False)
    r = launch(run, out, "open('a.txt','w').write('x')")
    assert r["created_paths"] is None
    assert r["exhaustive"] is False
    assert r["exit_code"] is None  # the tree is never launched without an armed observer
    assert not (run / "a.txt").exists()


def test_unconfined_control_writes_outside_unrecorded(dirs):
    run, out = dirs
    target = Path(os.environ["TEMP"]) / f"wcc-control-{uuid.uuid4().hex}.txt"
    try:
        code = f"open(r'{target}','w').write('x')"
        r = launch(run, out, code, mode="none")
        assert r["exit_code"] == 0, r["stderr"]
        assert target.exists()
        assert r["created_paths"] == []  # the run-dir watcher cannot see it
        assert r["exhaustive"] is False
    finally:
        target.unlink(missing_ok=True)


def test_low_integrity_alone_leaves_locallow_writable(dirs):
    run, out = dirs
    locallow = Path(os.environ["USERPROFILE"]) / "AppData" / "LocalLow"
    r = launch(run, out, PROBE, locallow, os.environ["TEMP"], mode="low_integrity")
    assert r["exit_code"] == 0, r["stderr"]
    verdicts = last_json(r["stdout"])
    assert verdicts[str(locallow)] == "WROTE"  # an escape the confined mode closes
    assert verdicts[os.environ["TEMP"]] == "PermissionError"
    assert r["exhaustive"] is False


def test_junction_and_cross_process_escapes_are_blocked(dirs):
    run, out = dirs
    code = r"""
import _winapi, ctypes, json, os, sys
res = {}
try:
    _winapi.CreateJunction(sys.argv[1], 'j')
    res['junction_created'] = True
    try:
        open(os.path.join('j', 'through.txt'), 'w').write('x'); res['write_through'] = 'WROTE'
    except OSError as e:
        res['write_through'] = type(e).__name__
except OSError as e:
    res['junction_created'] = type(e).__name__
k = ctypes.WinDLL('kernel32', use_last_error=True)
k.OpenProcess.restype = ctypes.c_void_p
h = k.OpenProcess(0x0020 | 0x0040 | 0x0002, False, int(sys.argv[2]))
res['open_harness_for_write'] = bool(h)
print(json.dumps(res))
"""
    r = launch(run, out, code, out, os.getpid())
    assert r["exit_code"] == 0, r["stderr"]
    res = last_json(r["stdout"])
    # One explicit branch, no default (W2 review :210): either the junction exists and the
    # write through it was denied, or its creation failed with an observed error name and
    # no write was attempted.
    if res["junction_created"] is True:
        assert res["write_through"] == "PermissionError", res
    else:
        assert isinstance(res["junction_created"], str) and "write_through" not in res, res
    assert res["open_harness_for_write"] is False
    assert not (out / "through.txt").exists()


def test_harness_output_inside_run_dir_is_refused(dirs):
    run, out = dirs
    with pytest.raises(ValueError, match="outside the watched run dir"):
        wc.run_confined(
            [sys.executable, "-c", "0"], run, stdout_path=run / "o", stderr_path=out / "e"
        )


def test_release_withdraws_write_access_for_later_trees(dirs, tmp_path):
    run, out = dirs
    r = launch(run, out, "import os; os.mkdir('made')")
    assert r["exit_code"] == 0, r["stderr"]
    after = wc.release(run)
    assert after["clean"] and after["complete"], after
    r2 = launch(run, out, PROBE, run, run / "made")  # relaunch re-grants only the root
    verdicts = last_json(r2["stdout"])
    assert verdicts[str(run)] == "WROTE" and verdicts[str(run / "made")] == "WROTE"
    wc.release(run)
    token = wc.mode_token("confined")
    imp = wc._impersonation_copy(token)
    try:
        assert wc.can_create(run, imp) is False
        assert wc.can_create(run / "made", imp) is False
    finally:
        wc._k.CloseHandle(imp)
        wc._k.CloseHandle(token)


def test_audit_finds_a_leftover_creatable_dir_and_blocks_exhaustive(dirs, tmp_path):
    run, out = dirs
    stale = tmp_path / "stale-run"
    stale.mkdir()
    wc.set_mandatory_label(stale, wc.LOW_IL)
    wc.grant_sid(stale, wc.logon_sid())
    try:
        audit = wc.audit_for_mode("confined", run, [tmp_path])
        assert audit["creatable_outside_allowed"] == [str(stale)]
        r = launch(run, out, "0", precondition=audit)
        assert r["exhaustive"] is False
        assert any("other creatable" in g for g in r["not_exhaustive_because"])
    finally:
        wc.release(stale)


def test_shared_writable_root_is_watched_kept_and_marks_attribution_inexact(dirs, tmp_path):
    """Like C:\\ProgramData\\Microsoft\\DeviceSync on this host: Low-labelled, Everyone-writable."""
    run, out = dirs
    shared = tmp_path / "shared-writable"
    shared.mkdir()
    wc.set_mandatory_label(shared, wc.LOW_IL)
    wc.grant_sid(shared, wc.EVERYONE)
    try:
        audit = wc.audit_for_mode("confined", run, [tmp_path])
        assert audit["creatable_outside_allowed"] == [str(shared)]
        code = f"open('in.txt','w').write('x'); open(r'{shared / 'escaped.txt'}','w').write('x')"
        r = launch(run, out, code, precondition=audit, shared_watch_roots=[shared])
        assert r["exit_code"] == 0, r["stderr"]
        assert str(shared / "escaped.txt") in r["created_paths"]  # the write is not omitted
        assert str(run / "in.txt") in r["created_paths"]
        assert r["attribution_exact"] is False
        assert r["exhaustive"] is True, r["not_exhaustive_because"]
    finally:
        wc.set_mandatory_label(shared, wc.MEDIUM_IL)


def test_fresh_restricting_sid_grants_nothing(dirs):
    run, out = dirs
    r = wc.run_confined(
        [os.path.join(os.environ["SystemRoot"], "System32", "cmd.exe"), "/c", "echo x> a.txt"],
        run,
        stdout_path=out / "o",
        stderr_path=out / "e",
        mode="fresh_sid_write_restricted",
    )
    assert r["confinement"]["token_matches_expected"] is True
    # An observed launch, even if DLL init failed (W2 review :290): exit code and tree seen.
    assert r["exit_code"] is not None and r["tree_pids"], r
    assert r["created_paths"] == []  # either DLL init fails or the write is denied
    assert not (run / "a.txt").exists()
    assert r["exhaustive"] is False
