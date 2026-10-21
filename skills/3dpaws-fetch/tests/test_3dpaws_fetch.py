"""Correctness tests for 3dpaws-fetch (missing creds + mocked CHORDS API)."""

import io
import os
import urllib.error
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
        if "last" in params:
            data = points_by_station.get(sid, [])[-1:]
            return {"type": "FeatureCollection", "features": [{"properties": {"data": data}}]}
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


@pytest.mark.parametrize("http", [False, True])
def test_download_errors_do_not_echo_credentials(mod, http):
    secret = "example-secret-not-real"
    error = (
        urllib.error.HTTPError(
            "https://example.org/?api_key=" + secret, 400, secret, {}, io.BytesIO(secret.encode())
        )
        if http
        else urllib.error.URLError(secret)
    )
    with (
        patch.object(mod.urllib.request, "urlopen", side_effect=error),
        patch.object(mod.time, "sleep"),
        pytest.raises(mod.DataError) as exc,
    ):
        mod._get_json("https://example.org", {"api_key": secret})
    assert secret not in str(exc.value)


def test_probe_empty_selection_is_controlled_error(mod, fetch):
    with (
        patch.dict(os.environ, CREDS),
        patch.object(mod, "_get_json", side_effect=_fake_get_json({})),
        pytest.raises(SystemExit) as exc,
    ):
        run_skill(fetch, "--probe-latest", "--bbox", "80/0/70/10")
    assert exc.value.code == 1


def test_probe_ignores_test_invalid_future_and_accepts_naive_time(capsys, mod, fetch):
    today = pd.Timestamp.now(tz="UTC").tz_convert(None).normalize()
    good = today - pd.Timedelta(days=3)
    payload = {
        "features": [
            {
                "properties": {
                    "data": [
                        {"time": str(today), "test": "true"},
                        {"time": "invalid"},
                        {"time": "2099-01-01T00:00Z"},
                        {"time": str(good)},
                    ]
                }
            }
        ]
    }
    fake = _fake_get_json({})

    def get(url, params=None):
        return payload if params and "last" in params else fake(url, params)

    with patch.dict(os.environ, CREDS), patch.object(mod, "_get_json", side_effect=get):
        run_skill(fetch, "--probe-latest", "--station", "1")
    assert capsys.readouterr().out.strip() == (good - pd.Timedelta(days=1)).strftime("%Y-%m-%d")


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
    assert np.isnan(ds["tmin"].sel(station_id="1").values[0])
    assert np.isnan(ds["tmax"].sel(station_id="1").values[0])
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
    assert ds["precip"].attrs.get("data_interval") == "1 day"
    np.testing.assert_array_equal(
        ds.time.values, pd.date_range("2026-10-01T06:00", periods=2).values
    )


@pytest.mark.parametrize("explicit", [False, True])
def test_single_missing_increment_rejects_day(tmp_path, mod, fetch, explicit):
    points = _readings("2026-10-01", 2, 15, lambda t: {"rg1": 0.1, "mt1": 20})
    points = [p for p in points if p["time"] != "2026-10-02T00:15:00Z"]
    args = ["--reporting-interval-minutes", "15"] if explicit else []
    ds = _run(tmp_path, mod, fetch, {"1": points}, *args)
    assert ds.precip.values[0, 0] == pytest.approx(9.6)
    assert np.isnan(ds.precip.values[1, 0])
    assert ds.attrs["cadence_source"] == ("explicit" if explicit else "inferred_unverified")


def test_all_invalid_second_gauge_rejects_rainfall(tmp_path, mod, fetch):
    points = _readings("2026-10-01", 2, 15, lambda t: {"rg1": 0.1, "rg2": -999.9, "mt1": 20})
    ds = _run(tmp_path, mod, fetch, {"1": points})
    assert ds.precip.isnull().all()
    assert ds.temperature.notnull().all()


@pytest.mark.parametrize(
    "times",
    [
        [],
        ["invalid"],
        ["2026-09-01T00:00Z"],
        ["2026-10-01T00:00Z"],
        ["2026-10-01T00:00Z", "2026-10-04T00:00Z"],
    ],
)
def test_sparse_or_filtered_records_safe(mod, times):
    payload = {
        "features": [
            {"properties": {"data": [{"time": t, "measurements": {"rg1": 0.1}} for t in times]}}
        ]
    }
    with patch.object(mod, "_get_json", return_value=payload):
        daily, interval = mod._station_daily(
            PORTAL, "1", pd.Timestamp("2026-10-01"), pd.Timestamp("2026-10-04"), 0, {}
        )
    assert daily is None
    assert interval is None


def test_explicit_cadence_rejects_systematic_missing_increments(tmp_path, mod, fetch):
    points = _readings("2026-10-01", 2, 15, lambda t: {"rg1": 0.1, "mt1": 20})
    points = [
        p
        for p in points
        if not (p["time"].startswith("2026-10-02") and pd.Timestamp(p["time"]).minute in (15, 45))
    ]
    ds = _run(tmp_path, mod, fetch, {"1": points}, "--reporting-interval-minutes", "15")
    assert np.isnan(ds.precip.values[1, 0])
    assert ds.reporting_interval_minutes.values.tolist() == [15]


def test_explicit_sparse_record_has_missing_daily_total(mod):
    points = [{"time": "2026-10-01T00:00Z", "measurements": {"rg1": 0.1}}]
    with patch.object(
        mod, "_get_json", return_value={"features": [{"properties": {"data": points}}]}
    ):
        daily, interval = mod._station_daily(
            PORTAL, "1", pd.Timestamp("2026-10-01"), pd.Timestamp("2026-10-01"), 0, {}, 15
        )
    assert interval == 15
    assert daily.precip.isna().all()


def test_mixed_iso_timestamp_precision_preserves_valid_increments(tmp_path, mod, fetch):
    points = _readings("2026-10-01", 2, 15, lambda t: {"rg1": 0.1})
    points[1]["time"] = "2026-10-01T00:15:00.000Z"
    ds = _run(tmp_path, mod, fetch, {"1": points}, "--reporting-interval-minutes", "15")
    np.testing.assert_allclose(ds.precip.values[:, 0], [9.6, 9.6])


@pytest.mark.parametrize("empty", ["instruments", "sites"])
def test_empty_metadata_is_controlled_error(mod, fetch, empty):
    fake = _fake_get_json({})

    def get(url, params=None):
        return [] if url.endswith(f"/{empty}.json") else fake(url, params)

    with patch.object(mod, "_get_json", side_effect=get), pytest.raises(SystemExit) as exc:
        run_skill(fetch, "--list-stations")
    assert exc.value.code == 1


def test_off_grid_reading_rejects_day(tmp_path, mod, fetch):
    points = _readings("2026-10-01", 2, 15, lambda t: {"rg1": 0.1, "mt1": 20})
    points.append({"time": "2026-10-02T00:01:00Z", "measurements": {"rg1": 0.2}})
    ds = _run(tmp_path, mod, fetch, {"1": points}, "--reporting-interval-minutes", "15")
    assert np.isnan(ds.precip.values[1, 0])


def test_test_readings_are_dropped(tmp_path, mod, fetch):
    def make(t):
        return {"rg1": 0.0, "rg2": 0.0, "mt1": 20.0}

    points = _readings("2026-10-01", 2, 15, make)
    points.append({"time": "2026-10-01T12:07:00Z", "test": "true", "measurements": {"mt1": 45.0}})
    ds = _run(tmp_path, mod, fetch, {"1": points})
    assert ds["tmax"].values[0, 0] == pytest.approx(20.0)


def test_probe_latest_prints_last_ended_day(capsys, mod, fetch):
    def make(t):
        return {"rg1": 0.0, "rg2": 0.0, "mt1": 20.0}

    today = pd.Timestamp.now(tz="UTC").tz_convert(None).normalize()
    # Readings until 23:45 three days ago: that day has not ended at the newest
    # reading, so the latest ended day is four days ago.
    points = _readings(today - pd.Timedelta(days=10), 8, 15, make)
    # A station whose clock runs ahead is ignored.
    ahead = [{"time": "2099-01-01T00:00:00Z", "test": "false", "measurements": {"mt1": 20.0}}]
    with (
        patch.dict(os.environ, CREDS),
        patch.object(mod, "_get_json", side_effect=_fake_get_json({"1": points, "2": ahead})),
    ):
        run_skill(fetch, "--probe-latest", "--station", "1", "--station", "2")
    out = capsys.readouterr().out.strip()
    assert out == (today - pd.Timedelta(days=4)).strftime("%Y-%m-%d")
