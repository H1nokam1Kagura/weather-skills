"""Pure post-processing checks for write_capture_canary: no process is launched.

W2 review: a missing capture (created_paths None) must stay missing, never become 0, False
or [] in the canary's derived figures.
"""

import importlib.util
import os
from pathlib import Path

import pytest

if os.name != "nt":
    pytest.skip("the canary module reads Windows profile variables", allow_module_level=True)

spec = importlib.util.spec_from_file_location(
    "write_capture_canary", Path(__file__).with_name("write_capture_canary.py")
)
wcc = importlib.util.module_from_spec(spec)
spec.loader.exec_module(wcc)


def result(**scenarios):
    base = {
        "confined_scenario": {"created_paths": [], "exhaustive": True},
        "confined_outside_writes": {"verdicts": {}},
        "confined_overflow": {"watcher": {"overflow": True}, "exhaustive": False},
        "observer_failure": {"created_paths": None},
        "unconfined_control": {"outside_file_written": True, "outside_file_recorded": False},
        "low_integrity_alone": {"verdicts": None},
        "confined_shared_roots": {
            "verdicts": {},
            "created_paths": [],
            "run_dir": "C:/run",
            "attribution_exact": True,
            "exhaustive": True,
            "not_exhaustive_because": None,
        },
    }
    for name, overrides in scenarios.items():
        base[name] = {**base[name], **overrides}
    return {
        "host": {"token": {"elevated": False}, "procmon": {"on_path": {}, "known_dirs": {}}},
        "scenarios": base,
        "system_audit": {"creatable_outside_allowed": []},
        "released": [],
    }


def test_missing_created_paths_elide_to_none_not_zero():  # lines 254 and 388
    r = wcc.elide_created_paths({"created_paths": None})
    assert r["created_count"] is None and r["created_paths"] is None
    r = wcc.elide_created_paths({"created_paths": ["a", "b"]})
    assert r["created_count"] == 2 and r["created_paths"] == "<2 paths elided>"
    assert wcc.created_count(None) is None and wcc.created_count([]) == 0


def test_missing_capture_is_not_an_unrecorded_outside_write():  # line 281 / summarize
    assert wcc.outside_recorded("C:/x", None) is None
    assert wcc.outside_recorded("C:/x", ["C:/x"]) is True
    summary = wcc.summarize(result(unconfined_control={"outside_file_recorded": None}))
    assert summary["unconfined_outside_write_unrecorded"] is None
    assert wcc.summarize(result())["unconfined_outside_write_unrecorded"] is True


def test_unknown_exhaustive_never_reads_as_non_exhaustive():  # line 418
    summary = wcc.summarize(result(confined_overflow={"exhaustive": None}))
    assert summary["overflow_forces_non_exhaustive"] is None
    assert wcc.summarize(result())["overflow_forces_non_exhaustive"] is True


def test_missing_shared_root_capture_is_not_an_empty_record():  # line 444
    summary = wcc.summarize(result(confined_shared_roots={"created_paths": None}))
    assert summary["shared_root_escape"]["recorded"] is None
    assert wcc.summarize(result())["shared_root_escape"]["recorded"] == []
