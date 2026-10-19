"""Correctness tests for 3dpaws-fetch (missing creds + mocked CHORDS API)."""

import os
from unittest.mock import patch

import numpy as np
import pandas as pd
import pytest
import xarray as xr
from conftest import load_skill, run_skill

PORTAL = "https://3d-fewsnet.icdp.ucar.edu"
CREDS = {"CHORDS_EMAIL": "user@example.org", "CHORDS_API_KEY": "key"}


@pytest.fixture(scope="module")
def mod():
    return load_skill("3dpaws-fetch", "fetch")


@pytest.fixture(scope="module")
def fetch(mod):
    return mod.fetch


INSTRUMENTS = [
    {"id": 1, "name": "Nairobi ", "site_id": 10},
    {"id": 2, "name": "Far south", "site_id": 11},
    {"id": 3, "name": "No coords", "site_id": 12},
]
SITES = [
    {"id": 10, "name": " KMD HQ", "lat": "-1.3", "lon": "36.76"},
    {"id": 11, "name": "South", "lat": "-10.0", "lon": "36.0"},
    {"id": 12, "name": "KSA", "lat": None, "lon": None},
]


def _readings(start, days, interval_min, make):
    """CHORDS data points every ``interval_min`` from ``start`` for ``days`` days."""
    times = pd.date_range(start, periods=days * 1440 // interval_min, freq=f"{interval_min}min")
    return [
        {"time": t.strftime("%Y-%m-%dT%H:%M:%SZ"), "test": "false", "measurements": make(t)}
        for t in times
    ]


def _fake_get_json(points_by_station):
    """Serve instruments/sites listings and the per-station data in the requested window."""

    def fake(url, params=None):
        if url.endswith("/instruments.json"):
            return INSTRUMENTS
        if url.endswith("/sites.json"):
            return SITES
        sid = url.rsplit("/", 1)[-1].removesuffix(".json")
        lo = pd.Timestamp(params["start"]).tz_convert(None)
        hi = pd.Timestamp(params["end"]).tz_convert(None)
        data = [
            p
            for p in points_by_station.get(sid, [])
            if lo <= pd.Timestamp(p["time"]).tz_convert(None) < hi
        ]
        return {"type": "FeatureCollection", "features": [{"properties": {"data": data}}]}

    return fake


def _run(tmp_path, mod, fetch, points_by_station, *extra):
    out = tmp_path / "out.zarr"
    with (
        patch.dict(os.environ, CREDS),
        patch.object(mod, "_get_json", side_effect=_fake_get_json(points_by_station)),
    ):
        run_skill(
            fetch,
            "--station",
            "1",
            "--start-time",
            "2026-10-01",
            "--end-time",
            "2026-10-02",
            *extra,
            "-o",
            str(out),
        )
    return xr.open_zarr(out)


def test_missing_credentials_exits_2(tmp_path, mod, fetch):
    env = {k: v for k, v in os.environ.items() if k not in CREDS}
    with (
        patch.dict(os.environ, env, clear=True),
        patch.object(mod, "_get_json", side_effect=_fake_get_json({})),
        pytest.raises(SystemExit) as exc,
    ):
        run_skill(
            fetch,
            "--station",
            "1",
            "--start-time",
            "2026-10-01",
            "--end-time",
            "2026-10-02",
            "-o",
            str(tmp_path / "out.zarr"),
        )
    assert exc.value.code == 2


def test_requires_station_or_bbox(tmp_path, fetch):
    with patch.dict(os.environ, CREDS), pytest.raises(SystemExit) as exc:
        run_skill(
            fetch,
            "--start-time",
            "2026-10-01",
            "--end-time",
            "2026-10-02",
            "-o",
            str(tmp_path / "o.zarr"),
        )
    assert exc.value.code == 2


def test_unknown_station_exits_2(tmp_path, mod, fetch):
    with (
        patch.dict(os.environ, CREDS),
        patch.object(mod, "_get_json", side_effect=_fake_get_json({})),
        pytest.raises(SystemExit) as exc,
    ):
        run_skill(
            fetch,
            "--station",
            "99",
            "--start-time",
            "2026-10-01",
            "--end-time",
            "2026-10-02",
            "-o",
            str(tmp_path / "o.zarr"),
        )
    assert exc.value.code == 2


def test_list_stations_tsv_filters_bbox(capsys, mod, fetch):
    with patch.object(mod, "_get_json", side_effect=_fake_get_json({})):
        run_skill(fetch, "--list-stations", "--bbox", "0/35/-2/37")
    lines = capsys.readouterr().out.splitlines()
    assert lines[0] == "station_id\tname\tsite\tlatitude\tlongitude"
    assert lines[1] == "1\tNairobi\tKMD HQ\t-1.30000\t36.76000"
    assert len(lines) == 2


def test_daily_values_and_qc(tmp_path, mod, fetch):
    def make(t):
        return {
            # 0.1 mm every 15 min on day 1 only -> 9.6 mm; gauge 2 agrees.
            "rg1": 0.1 if t.day == 1 else 0.0,
            "rg2": 0.1 if t.day == 1 else 0.0,
            # -999.9 at one reading must be ignored, not drag the min down.
            "mt1": -999.9 if t.hour == 3 and t.minute == 0 else 20.0 + t.hour / 2,
            "sh1": 80.0,
            "bp1": 0,  # no pressure sensor fitted
            "ws": 2.0,
            "wg": 5.0 if t.hour == 12 and t.minute == 0 else 3.0,
        }

    ds = _run(tmp_path, mod, fetch, {"1": _readings("2026-10-01", 2, 15, make)})
    assert list(ds.station_id.values) == ["1"]
    precip = ds["precip"].sel(station_id="1").values
    np.testing.assert_allclose(precip, [9.6, 0.0])
    assert ds["tmin"].sel(station_id="1").values[0] == pytest.approx(20.0)
    assert ds["tmax"].sel(station_id="1").values[0] == pytest.approx(31.5)
    assert ds["wind_gust"].sel(station_id="1").values[0] == pytest.approx(5.0)
    assert "pressure" not in ds  # all readings were the 0 sentinel
    assert ds["precip"].attrs["units"] == "mm day-1"
    assert ds["humidity"].attrs["units"] == "%"
    assert ds["latitude"].values[0] == pytest.approx(-1.3)
    assert str(ds["site"].values[0]) == "KMD HQ"
    assert ds.attrs["featureType"] == "timeSeries"


def test_outage_blanks_the_day(tmp_path, mod, fetch):
    def make(t):
        return {"rg1": 0.0, "rg2": 0.0, "mt1": 20.0}

    points = _readings("2026-10-01", 2, 15, make)
    # Drop three hours of day 2: longer than three reporting intervals.
    points = [
        p
        for p in points
        if not p["time"].startswith(("2026-10-02T10", "2026-10-02T11", "2026-10-02T12"))
    ]
    ds = _run(tmp_path, mod, fetch, {"1": points})
    assert not np.isnan(ds["precip"].values[0, 0])
    assert np.isnan(ds["precip"].values[1, 0])
    assert np.isnan(ds["temperature"].values[1, 0])


def test_gauges_disagree_gives_nan(tmp_path, mod, fetch):
    def make(t):
        # Gauge 1 is stuck adding 5 mm every reading on day 2; gauge 2 reads dry.
        return {"rg1": 5.0 if t.day == 2 else 0.0, "rg2": 0.0, "mt1": 20.0}

    ds = _run(tmp_path, mod, fetch, {"1": _readings("2026-10-01", 2, 15, make)})
    np.testing.assert_array_equal(np.isnan(ds["precip"].values[:, 0]), [False, True])


def test_day_start_hour_shifts_the_rain_day(tmp_path, mod, fetch):
    def make(t):
        # 1 mm at 03 UTC on 2 Oct: day 2 Oct in UTC, day 1 Oct on a 06 UTC day.
        wet = t == pd.Timestamp("2026-10-02T03:00")
        return {"rg1": 1.0 if wet else 0.0, "rg2": 1.0 if wet else 0.0, "mt1": 20.0}

    points = _readings("2026-10-01T06:00", 2, 15, make)
    ds = _run(tmp_path, mod, fetch, {"1": points}, "--day-start-hour", "6")
    np.testing.assert_allclose(ds["precip"].values[:, 0], [1.0, 0.0])
    assert "06:00" in ds["time"].attrs["comment"]


def test_test_readings_are_dropped(tmp_path, mod, fetch):
    def make(t):
        return {"rg1": 0.0, "rg2": 0.0, "mt1": 20.0}

    points = _readings("2026-10-01", 2, 15, make)
    points.append({"time": "2026-10-01T12:07:00Z", "test": "true", "measurements": {"mt1": 45.0}})
    ds = _run(tmp_path, mod, fetch, {"1": points})
    assert ds["tmax"].values[0, 0] == pytest.approx(20.0)


def test_probe_latest_prints_last_complete_day(capsys, mod, fetch):
    def make(t):
        return {"rg1": 0.0, "rg2": 0.0, "mt1": 20.0}

    today = pd.Timestamp.now(tz="UTC").tz_convert(None).normalize()
    # Complete days up to three days ago; the station is silent after that.
    points = _readings(today - pd.Timedelta(days=10), 8, 15, make)
    with (
        patch.dict(os.environ, CREDS),
        patch.object(mod, "_get_json", side_effect=_fake_get_json({"1": points})),
    ):
        run_skill(fetch, "--probe-latest", "--station", "1")
    out = capsys.readouterr().out.strip()
    assert out == (today - pd.Timedelta(days=3)).strftime("%Y-%m-%d")
