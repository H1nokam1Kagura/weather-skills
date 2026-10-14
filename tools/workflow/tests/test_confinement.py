"""Controller-native confinement of HOME-derived caches and temp files under run/scratch.

Frozen v2.7 confines filesystem_snapshot.created_paths to HARNESS_RUN_DIR (all_under, every
case): neither the controller process nor its children may create ~/.config/matplotlib,
~/.cache, ~/.local/share/cartopy or /tmp entries.
"""

import os
import sys
import tempfile
import types
from pathlib import Path

from tools.workflow.catalog import child_environment, confined_process, scratch_environment

VARS = (
    "TMP",
    "TEMP",
    "TMPDIR",
    "MPLCONFIGDIR",
    "XDG_CACHE_HOME",
    "XDG_CONFIG_HOME",
    "XDG_DATA_HOME",
)


def test_child_environment_puts_every_cache_under_scratch(tmp_path):
    scratch = tmp_path / "run" / "scratch"
    env = child_environment(scratch)
    for key in VARS:
        assert Path(env[key]).is_relative_to(scratch.resolve()), key
    assert env["WEATHER_SKILLS_REPLAY_RUNNER"] == "python"
    assert env.get("HOME") == os.environ.get("HOME")  # HOME itself is never redirected
    assert set(scratch_environment(scratch)) == set(VARS)


def test_confined_process_applies_and_restores(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_CACHE_HOME", "/home/someone/.cache")
    monkeypatch.delenv("XDG_DATA_HOME", raising=False)
    fake = types.ModuleType("cartopy")
    fake.config = {"cache_dir": Path("/tmp/cartopy_cache_dir"), "data_dir": Path("/home/x")}
    monkeypatch.setitem(sys.modules, "cartopy", fake)
    before_tempdir = tempfile.tempdir
    scratch = tmp_path / "run" / "scratch"
    with confined_process(scratch) as values:
        resolved = scratch.resolve()
        assert scratch.is_dir()
        assert Path(tempfile.gettempdir()) == resolved
        with tempfile.NamedTemporaryFile() as handle:
            assert Path(handle.name).parent == resolved
        for key in VARS:
            assert os.environ[key] == values[key]
            assert Path(os.environ[key]).is_relative_to(resolved), key
        assert Path(fake.config["cache_dir"]).is_relative_to(resolved)
        assert Path(fake.config["data_dir"]).is_relative_to(resolved)
        # children started inside the run inherit the confinement even without a scratch arg
        assert Path(child_environment()["XDG_CONFIG_HOME"]).is_relative_to(resolved)
    assert tempfile.tempdir == before_tempdir
    assert os.environ["XDG_CACHE_HOME"] == "/home/someone/.cache"
    assert "XDG_DATA_HOME" not in os.environ
    assert fake.config == {"cache_dir": Path("/tmp/cartopy_cache_dir"), "data_dir": Path("/home/x")}


def test_confined_process_restores_on_error(tmp_path):
    before = os.environ.get("MPLCONFIGDIR")
    try:
        with confined_process(tmp_path / "scratch"):
            raise RuntimeError("controller failure")
    except RuntimeError:
        pass
    assert os.environ.get("MPLCONFIGDIR") == before
