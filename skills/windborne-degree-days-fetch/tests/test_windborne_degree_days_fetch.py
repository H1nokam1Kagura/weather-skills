"""Correctness tests for windborne-degree-days-fetch (mocked WindBorne API)."""

import numpy as np
import pytest
from conftest import load_skill, run_skill


@pytest.fixture(scope="module")
def mod():
    return load_skill("windborne-degree-days-fetch", "fetch")


def test_states_by_day_drops_empty_days(mod, monkeypatch, tmp_path):
    import requests
    import xarray as xr

    meta = {"initialization_time": "2026-10-19T20:00:00Z"}
    payloads = {
        "initialization_times": {"available": ["2026-10-19T20:00:00Z"]},
        "hdds": {
            "dates": ["2026-10-19", "2026-10-20", "2026-10-21"],
            "hdd": {
                "2026-10-19": {"Iowa": 11.0, "Texas": 2.0},
                "2026-10-20": {"Iowa": 12.0, "Texas": 1.0},
            },
            "metadata": meta,
        },
        "cdds": {
            "dates": ["2026-10-19", "2026-10-20", "2026-10-21"],
            "cdd": {
                "2026-10-19": {"Iowa": 0.0, "Texas": 3.0},
                "2026-10-20": {"Iowa": 0.0, "Texas": 4.0},
            },
            "metadata": meta,
        },
    }

    class _Resp:
        status_code = 200
        text = ""

        def __init__(self, payload):
            self._payload = payload

        def json(self):
            return self._payload

    class _Session:
        auth = None

        def get(self, url, params=None, timeout=None):
            return _Resp(payloads[url.rsplit("/", 1)[-1]])

    monkeypatch.setattr(requests, "Session", _Session)
    monkeypatch.setenv("WB_CLIENT_ID", "id")
    monkeypatch.setenv("WB_API_KEY", "key")
    out = tmp_path / "dd.zarr"
    run_skill(mod.fetch, "-o", str(out))
    ds = xr.open_zarr(out)
    assert list(ds["region"].values) == ["Iowa", "Texas"]
    assert ds.sizes["time"] == 2
    np.testing.assert_allclose(ds["hdd"].sel(region="Iowa").values, [11.0, 12.0])
    np.testing.assert_allclose(ds["cdd"].sel(region="Texas").values, [3.0, 4.0])
