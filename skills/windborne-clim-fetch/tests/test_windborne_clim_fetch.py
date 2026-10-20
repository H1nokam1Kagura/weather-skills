"""Correctness tests for windborne-clim-fetch (mocked WindBorne API)."""

import numpy as np
import pytest
from conftest import load_skill, run_skill


@pytest.fixture(scope="module")
def mod():
    return load_skill("windborne-clim-fetch", "fetch")


def test_hourly_normals_with_rate_precip(mod, monkeypatch, tmp_path):
    import requests
    import xarray as xr

    seen = {}

    class _Resp:
        status_code = 200
        text = ""

        def json(self):
            return {
                "normal": "1991-2020",
                "climatologies": [
                    [
                        {
                            "time": f"2026-07-15T{h:02d}:00:00Z",
                            "temperature_2m": 15.0 + h,
                            "precipitation": 0.5,
                            "pressure_msl": None,
                        }
                        for h in range(3)
                    ]
                ],
            }

    class _Session:
        auth = None

        def get(self, url, params=None, timeout=None):
            seen.update(params)
            return _Resp()

    monkeypatch.setattr(requests, "Session", _Session)
    monkeypatch.setenv("WB_CLIENT_ID", "id")
    monkeypatch.setenv("WB_API_KEY", "key")
    out = tmp_path / "clim.zarr"
    run_skill(
        mod.fetch,
        "--point",
        "-1.29,36.82",
        "--start-time",
        "2026-07-15",
        "--end-time",
        "2026-07-15",
        "--rolling-days",
        "7",
        "-o",
        str(out),
    )
    ds = xr.open_zarr(out)
    assert seen["start_time"] == "2026-07-15T00:00:00Z"
    assert seen["end_time"] == "2026-07-15T23:00:00Z"
    assert seen["rolling_average_days"] == 7
    assert "msl" not in ds.data_vars
    np.testing.assert_allclose(ds["tp"].values[0], [12.0, 12.0, 12.0])
    # Precip over the preceding hour is labelled at the hour's start.
    assert ds["time"].values[0] == np.datetime64("2026-07-14T23:00")
    assert ds["t2m"].attrs["units"] == "degree_Celsius"
