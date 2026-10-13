"""Correctness tests for ecmwf-hres-fetch (mocked Open Data client)."""

import datetime as dt
import sys
import types

import pytest
from conftest import load_skill, run_skill


@pytest.fixture(scope="module")
def mod():
    return load_skill("ecmwf-hres-fetch", "fetch")


def test_probe_latest_prints_date_usable_as_date_flag(mod, monkeypatch, capsys):
    class _Client:
        def __init__(self, source):
            pass

        def latest(self, **kwargs):
            return dt.datetime(2026, 10, 13, 12)

    opendata = types.ModuleType("ecmwf.opendata")
    opendata.Client = _Client
    monkeypatch.setitem(sys.modules, "ecmwf", types.ModuleType("ecmwf"))
    monkeypatch.setitem(sys.modules, "ecmwf.opendata", opendata)

    run_skill(mod.fetch, "--probe-latest")
    captured = capsys.readouterr()
    assert captured.out.strip() == "2026-10-13"
    assert "--date 2026-10-13 --run 12" in captured.err
