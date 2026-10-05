"""Correctness tests for check-artifact (synthetic Zarrs, no network)."""

import json

import numpy as np
import pytest
from conftest import load_skill, make_forecast, make_gridded, run_skill, write_zarr

HISTORY = json.dumps([{"skill": "chirps-fetch", "version": "0.0.2", "args": {}, "input": None}])


@pytest.fixture(scope="module")
def check_artifact():
    return load_skill("check-artifact", "check_artifact").check_artifact


def _run(fn, *argv):
    """Run the skill; return its exit code (0 when it returns normally)."""
    try:
        run_skill(fn, *argv)
    except SystemExit as exc:
        return exc.code
    return 0


def _store(tmp_path, ds, name="in.zarr", history=True):
    if history:
        ds.attrs["weather_skills_history"] = HISTORY
    return str(write_zarr(ds, tmp_path / name))


def _line(out, check_id):
    return next(line for line in out.splitlines() if f"] {check_id} " in line)


def test_clean_artifact_passes(tmp_path, check_artifact, capsys):
    src = _store(tmp_path, make_gridded(n_time=5))
    code = _run(
        check_artifact,
        "-i",
        src,
        "--bbox",
        "3/10/1/13",
        "--start-time",
        "2026-01-01",
        "--end-time",
        "2026-01-05",
        "--expect-units",
        "mm/day",
    )
    out = capsys.readouterr().out
    assert code == 0
    assert "RESULT: PASS - 0 FAIL" in out
    for check_id in (
        "units-present",
        "units-allowed",
        "expect-units",
        "nan-fraction",
        "range-min",
        "range-max",
        "time-monotonic",
        "time-gaps",
        "time-range",
        "bbox",
        "provenance",
    ):
        assert _line(out, check_id).startswith("[PASS]"), check_id


def test_negative_precip_fails(tmp_path, check_artifact, capsys):
    src = _store(tmp_path, make_gridded(fill=-5.0))
    assert _run(check_artifact, "-i", src) == 1
    out = capsys.readouterr().out
    assert _line(out, "range-min").startswith("[FAIL]")
    assert "min -5 mm/day" in out


def test_tiny_negative_precip_only_warns(tmp_path, check_artifact, capsys):
    ds = make_gridded()
    ds["precip"].values[0, 0, 0] = -1e-5
    src = _store(tmp_path, ds)
    assert _run(check_artifact, "-i", src) == 0
    assert _line(capsys.readouterr().out, "range-min").startswith("[WARN]")


def test_missing_units_fails(tmp_path, check_artifact, capsys):
    ds = make_gridded()
    del ds["precip"].attrs["units"]
    src = _store(tmp_path, ds)
    assert _run(check_artifact, "-i", src) == 1
    assert _line(capsys.readouterr().out, "units-present").startswith("[FAIL]")


def test_nan_heavy_fails(tmp_path, check_artifact, capsys):
    ds = make_gridded(n_time=5)
    ds["precip"].values[:4] = np.nan  # 80% missing
    src = _store(tmp_path, ds)
    assert _run(check_artifact, "-i", src) == 1
    out = capsys.readouterr().out
    assert _line(out, "nan-fraction").startswith("[FAIL]")
    assert "0.800" in out
    # A looser, explicit threshold accepts it — and still reports the empty slices.
    assert _run(check_artifact, "-i", src, "--max-nan-frac", "0.9") == 0
    out = capsys.readouterr().out
    assert _line(out, "empty-slices").startswith("[WARN]")


def test_all_nan_fails_regardless_of_threshold(tmp_path, check_artifact, capsys):
    src = _store(tmp_path, make_gridded(fill=np.nan))
    assert _run(check_artifact, "-i", src, "--max-nan-frac", "1") == 1
    assert "all values missing" in capsys.readouterr().out


def test_out_of_bbox_fails(tmp_path, check_artifact, capsys):
    src = _store(tmp_path, make_gridded())  # lon 10..13, lat 1..3
    assert _run(check_artifact, "-i", src, "--bbox", "3/10/1/11") == 1
    assert _line(capsys.readouterr().out, "bbox").startswith("[FAIL]")


def test_swapped_west_east_clip_output_fails(tmp_path, check_artifact, capsys):
    # What clip-region returns for --bbox 5/42/-5/34 (W and E swapped): the
    # complement of the box, read as an antimeridian box.
    ds = make_gridded(lats=(-4.0, 0.0, 4.0), lons=(30.0, 31.0, 32.0, 44.0, 45.0, 46.0))
    src = _store(tmp_path, ds)
    assert _run(check_artifact, "-i", src, "--bbox", "5/34/-5/42") == 1
    assert _line(capsys.readouterr().out, "bbox").startswith("[FAIL]")


def test_partial_bbox_coverage_warns(tmp_path, check_artifact, capsys):
    src = _store(tmp_path, make_gridded())
    assert _run(check_artifact, "-i", src, "--bbox", "10/10/1/13") == 0
    assert _line(capsys.readouterr().out, "bbox-coverage").startswith("[WARN]")


def test_bbox_north_south_reversed_is_usage_error(tmp_path, check_artifact):
    src = _store(tmp_path, make_gridded())
    assert _run(check_artifact, "-i", src, "--bbox", "1/10/3/13") == 2


@pytest.mark.parametrize("kind", ["missing", "file", "not-zarr"])
def test_unreadable_exits_2(tmp_path, check_artifact, capsys, kind):
    target = tmp_path / "x.zarr"
    if kind == "file":
        target.write_text("not a zarr")
    elif kind == "not-zarr":
        target.mkdir()
        (target / "junk.txt").write_text("x")
    assert _run(check_artifact, "-i", str(target)) == 2
    assert "RESULT: PASS" not in capsys.readouterr().out


def test_no_checkable_variable_exits_2(tmp_path, check_artifact, capsys):
    ds = make_gridded()
    ds["dry_spell_length"] = (ds["time"] - ds["time"][0]).broadcast_like(ds["precip"])
    ds = ds.drop_vars("precip")
    src = _store(tmp_path, ds)
    assert _run(check_artifact, "-i", src) == 2
    assert "absence of a check is not a pass" in capsys.readouterr().err


def _onset_store(
    tmp_path,
    dates,
    history_window=("2025-09-01", "2025-12-31"),
    effective_window=("2025-09-01", "2025-12-31"),
):
    """An onset-date-shaped artifact: one datetime64 variable on lat/lon, no time axis."""
    import xarray as xr

    vals = np.array(dates, dtype="datetime64[ns]").reshape(2, -1)
    ds = xr.Dataset(
        {"onset_precip_date": (("latitude", "longitude"), vals)},
        coords={"latitude": [1.0, 2.0], "longitude": np.arange(vals.shape[1], dtype=float) + 10},
    )
    args = (
        {"start_time": history_window[0], "end_time": history_window[1]} if history_window else {}
    )
    if effective_window:
        ds["onset_precip_date"].attrs.update(
            onset_search_start=effective_window[0], onset_search_end=effective_window[1]
        )
    ds.attrs["weather_skills_history"] = json.dumps(
        [
            {"skill": "chirps-fetch", "version": "0.0.2", "args": args, "input": None},
            {"skill": "onset-date", "version": "0.1.0", "args": {}, "input": "raw.zarr"},
        ]
    )
    return str(write_zarr(ds, tmp_path / "onset.zarr"))


def test_onset_dates_are_checked_not_skipped(tmp_path, check_artifact, capsys):
    src = _onset_store(
        tmp_path,
        [
            "2025-10-12",
            "2025-10-20",
            "NaT",
            "2025-11-02",
            "2025-10-26",
            "2025-10-30",
            "2025-10-05",
            "2025-10-18",
            "2025-11-10",
            "NaT",
            "2025-10-22",
            "2025-10-25",
            "2025-10-01",
            "2025-10-15",
            "2025-10-16",
            "2025-10-17",
            "2025-10-19",
            "2025-10-21",
            "2025-10-23",
            "2025-10-24",
        ],
    )
    assert _run(check_artifact, "-i", src) == 0
    out = capsys.readouterr().out
    assert _line(out, "event-found").startswith("[PASS]")
    assert _line(out, "date-window").startswith("[PASS]")
    assert _line(out, "date-censored").startswith("[PASS]")  # none on the first day
    assert "onset_search_start" in out


def test_left_censored_onsets_fail(tmp_path, check_artifact, capsys):
    # 10 of 20 events on the window's first day: already raining when the window opened.
    src = _onset_store(tmp_path, ["2025-09-01"] * 10 + ["2025-10-15"] * 10)
    assert _run(check_artifact, "-i", src) == 1
    out = capsys.readouterr().out
    assert _line(out, "date-censored").startswith("[FAIL]")
    assert "50.0% of events on the window's first day (2025-09-01)" in out


def test_some_censoring_warns(tmp_path, check_artifact, capsys):
    src = _onset_store(tmp_path, ["2025-09-01"] * 2 + ["2025-10-15"] * 18)  # 10%
    assert _run(check_artifact, "-i", src) == 0
    assert _line(capsys.readouterr().out, "date-censored").startswith("[WARN]")


def test_flags_override_history_window_and_catch_dates_outside(tmp_path, check_artifact, capsys):
    src = _onset_store(tmp_path, ["2025-10-15"] * 19 + ["2026-02-01"])
    code = _run(check_artifact, "-i", src, "--start-time", "2025-09-01", "--end-time", "2025-12-31")
    out = capsys.readouterr().out
    assert code == 1
    assert _line(out, "date-window").startswith("[FAIL]")
    assert "(1 outside)" in out and "--start-time/--end-time" in out
    assert "time-range" not in out  # an event-date artifact has no time axis to fail on


def test_unknown_window_warns_not_passes(tmp_path, check_artifact, capsys):
    src = _onset_store(tmp_path, ["2025-10-15"] * 20, history_window=None, effective_window=None)
    assert _run(check_artifact, "-i", src) == 0
    out = capsys.readouterr().out
    assert _line(out, "date-window").startswith("[WARN]")
    assert _line(out, "date-censored").startswith("[WARN]")


def test_effective_search_start_overrides_earlier_fetch(tmp_path, check_artifact, capsys):
    src = _onset_store(
        tmp_path,
        ["2025-10-01"] * 20,
        effective_window=("2025-10-01", "2025-12-31"),
    )
    assert _run(check_artifact, "-i", src) == 1
    out = capsys.readouterr().out
    assert _line(out, "date-censored").startswith("[FAIL]")
    assert "100.0% of events on the window's first day (2025-10-01)" in out


def test_end_only_override_preserves_effective_start(tmp_path, check_artifact, capsys):
    src = _onset_store(tmp_path, ["2025-09-01"] * 20)
    assert _run(check_artifact, "-i", src, "--end-time", "2025-12-31") == 1
    assert _line(capsys.readouterr().out, "date-censored").startswith("[FAIL]")


def test_start_only_override_preserves_effective_end(tmp_path, check_artifact, capsys):
    src = _onset_store(tmp_path, ["2026-01-01"] * 20)
    assert _run(check_artifact, "-i", src, "--start-time", "2025-09-01") == 1
    assert _line(capsys.readouterr().out, "date-window").startswith("[FAIL]")


@pytest.mark.parametrize("flags", [(), ("--end-time", "2025-12-31")])
def test_legacy_fetch_bounds_do_not_certify_censoring(tmp_path, check_artifact, capsys, flags):
    src = _onset_store(tmp_path, ["2025-10-01"] * 20, effective_window=None)
    assert _run(check_artifact, "-i", src, *flags) == 0
    out = capsys.readouterr().out
    assert _line(out, "date-censored").startswith("[WARN]")
    assert "effective search start unknown" in out


def test_legacy_explicit_start_enables_censoring(tmp_path, check_artifact, capsys):
    src = _onset_store(tmp_path, ["2025-10-01"] * 20, effective_window=None)
    assert _run(check_artifact, "-i", src, "--start-time", "2025-10-01") == 1
    assert _line(capsys.readouterr().out, "date-censored").startswith("[FAIL]")


def test_no_event_anywhere_warns(tmp_path, check_artifact, capsys):
    src = _onset_store(tmp_path, ["NaT"] * 20)
    assert _run(check_artifact, "-i", src) == 0
    assert _line(capsys.readouterr().out, "event-found").startswith("[WARN]")


def test_unknown_variable_exits_2(tmp_path, check_artifact):
    src = _store(tmp_path, make_gridded())
    assert _run(check_artifact, "-i", src, "-v", "t2m") == 2


def test_kelvin_values_labelled_celsius_fail(tmp_path, check_artifact, capsys):
    ds = make_gridded(name="t2m", fill=300.0)
    ds["t2m"].attrs.update(units="degree_Celsius", standard_name="air_temperature")
    src = _store(tmp_path, ds)
    assert _run(check_artifact, "-i", src) == 1
    assert _line(capsys.readouterr().out, "range").startswith("[FAIL]")


def test_temperature_in_kelvin_passes(tmp_path, check_artifact, capsys):
    ds = make_gridded(name="t2m", fill=300.0)
    ds["t2m"].attrs.update(units="K", standard_name="air_temperature")
    src = _store(tmp_path, ds)
    assert _run(check_artifact, "-i", src) == 0
    assert "max 26.85 degC" in capsys.readouterr().out


def test_precip_with_temperature_units_fails(tmp_path, check_artifact, capsys):
    ds = make_gridded()
    ds["precip"].attrs["units"] = "K"
    src = _store(tmp_path, ds)
    assert _run(check_artifact, "-i", src) == 1
    assert _line(capsys.readouterr().out, "units-allowed").startswith("[FAIL]")


def test_mass_flux_units_are_converted_for_range(tmp_path, check_artifact, capsys):
    # 5 kg m-2 s-1 is 432000 mm/day: a mm/day value mislabelled as a flux.
    ds = make_gridded(fill=5.0)
    ds["precip"].attrs["units"] = "kg m-2 s-1"
    src = _store(tmp_path, ds)
    assert _run(check_artifact, "-i", src) == 1
    assert _line(capsys.readouterr().out, "range-max").startswith("[FAIL]")


def test_expect_units_mismatch_fails(tmp_path, check_artifact, capsys):
    src = _store(tmp_path, make_gridded())
    assert _run(check_artifact, "-i", src, "--expect-units", "mm") == 1
    assert _line(capsys.readouterr().out, "expect-units").startswith("[FAIL]")


def test_weekly_total_uses_aggregation_period(tmp_path, check_artifact, capsys):
    ds = make_gridded(n_time=1, fill=3000.0)  # 3000 mm in 7 days = 429 mm/day
    ds["precip"].attrs.update(units="mm", aggregation_period="7 day")
    src = _store(tmp_path, ds)
    assert _run(check_artifact, "-i", src) == 0
    out = capsys.readouterr().out
    assert _line(out, "range-max").startswith("[PASS]")
    assert "over 7 day" in out


def test_anomaly_is_not_range_checked(tmp_path, check_artifact, capsys):
    ds = make_gridded(fill=-5.0)
    ds.attrs["weather_skills_history"] = json.dumps(
        [{"skill": "difference", "version": "0.0.2", "args": {}, "input": []}]
    )
    src = str(write_zarr(ds, tmp_path / "anom.zarr"))
    assert _run(check_artifact, "-i", src) == 0
    assert _line(capsys.readouterr().out, "range").startswith("[WARN]")


def test_duplicate_times_fail(tmp_path, check_artifact, capsys):
    ds = make_gridded(n_time=3)
    ds = ds.assign_coords(time=np.array(["2026-01-01", "2026-01-02", "2026-01-02"], "M8[ns]"))
    src = _store(tmp_path, ds)
    assert _run(check_artifact, "-i", src) == 1
    assert _line(capsys.readouterr().out, "time-monotonic").startswith("[FAIL]")


def test_time_gap_warns(tmp_path, check_artifact, capsys):
    ds = make_gridded(n_time=4)
    times = np.array(["2026-01-01", "2026-01-02", "2026-01-03", "2026-01-09"], "M8[ns]")
    src = _store(tmp_path, ds.assign_coords(time=times))
    assert _run(check_artifact, "-i", src) == 0
    assert _line(capsys.readouterr().out, "time-gaps").startswith("[WARN]")


def test_time_range_mismatch_fails(tmp_path, check_artifact, capsys):
    src = _store(tmp_path, make_gridded(n_time=5))
    assert _run(check_artifact, "-i", src, "--end-time", "2026-01-31") == 1
    assert "ends before --end-time" in capsys.readouterr().out


def test_forecast_valid_time_is_init_plus_step(tmp_path, check_artifact, capsys):
    ds = make_forecast(n_step=3, name="tp", init="2026-01-01")
    ds["tp"].attrs["units"] = "mm day-1"
    src = _store(tmp_path, ds)
    code = _run(check_artifact, "-i", src, "--start-time", "2026-01-01", "--end-time", "2026-01-03")
    assert code == 0
    assert _line(capsys.readouterr().out, "time-range").startswith("[PASS]")


def test_lead_axis_without_init_cannot_meet_time_range(tmp_path, check_artifact, capsys):
    ds = make_forecast(n_step=3, name="tp").drop_vars("time")
    ds["tp"].attrs["units"] = "mm day-1"
    src = _store(tmp_path, ds)
    assert _run(check_artifact, "-i", src, "--start-time", "2026-01-01") == 1
    assert "run step-to-time first" in capsys.readouterr().out


def test_incomplete_aggregation_coverage_warns(tmp_path, check_artifact, capsys):
    ds = make_gridded(n_time=2)
    ds = ds.assign_coords(aggregation_coverage=("time", [1.0, 0.71]))
    src = _store(tmp_path, ds)
    assert _run(check_artifact, "-i", src) == 0
    assert _line(capsys.readouterr().out, "aggregation-coverage").startswith("[WARN]")


def test_missing_provenance_warns_not_passes(tmp_path, check_artifact, capsys):
    src = _store(tmp_path, make_gridded(), history=False)
    assert _run(check_artifact, "-i", src) == 0
    assert _line(capsys.readouterr().out, "provenance").startswith("[WARN]")


def test_dimensionless_index_is_not_treated_as_precip(tmp_path, check_artifact, capsys):
    ds = make_gridded(name="precipitation_quality_index", fill=-1.0)
    ds["precipitation_quality_index"].attrs = {"units": "1"}
    src = _store(tmp_path, ds)
    assert _run(check_artifact, "-i", src) == 0
    assert "range-min" not in capsys.readouterr().out
