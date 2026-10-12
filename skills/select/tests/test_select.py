"""Correctness tests for select."""

from pathlib import Path

import pytest
import xarray as xr
from conftest import load_skill, make_forecast, make_gridded, run_skill, write_zarr
from weather_skills_core.provenance import load_history


@pytest.fixture(scope="module")
def select():
    return load_skill("select", "select_dim").select


def test_select_by_index_collapses_dim(tmp_path, select):
    src = write_zarr(make_gridded(n_time=3), tmp_path / "in.zarr")
    out = tmp_path / "out.zarr"

    run_skill(select, "-i", str(src), "-o", str(out), "--dim", "time", "--index", "1")

    assert Path(out).exists()
    ds = xr.open_zarr(out, consolidated=True)
    assert "time" not in ds.dims
    assert load_history(out)[-1]["skill"] == "select"


def test_select_requires_index_or_value(tmp_path, select):
    src = write_zarr(make_gridded(), tmp_path / "in.zarr")
    out = tmp_path / "out.zarr"

    with pytest.raises(SystemExit) as exc:
        run_skill(select, "-i", str(src), "-o", str(out), "--dim", "time")
    assert exc.value.code == 2


def test_select_value_station_id_stringdtype(tmp_path, select):
    """Zarr v3 stores pandas/object station ids as NumPy StringDType (kind T)."""
    import pandas as pd

    times = pd.to_datetime(["2026-08-19", "2026-08-20"])
    frames = []
    for sid in ("TA00072", "TA00131"):
        frames.append(
            pd.DataFrame(
                {"precip": [1.0, 2.0], "station_id": sid},
                index=times,
            )
        )
    long = pd.concat(frames)
    long.index.name = "time"
    long = long.reset_index().set_index(["time", "station_id"])
    ds = xr.Dataset.from_dataframe(long)
    ds["precip"].attrs.update(units="mm day-1", standard_name="lwe_precipitation_rate")
    src = write_zarr(ds, tmp_path / "in.zarr")
    assert xr.open_zarr(src, consolidated=True)["station_id"].dtype.kind == "T"

    out = tmp_path / "out.zarr"
    run_skill(
        select,
        "-i",
        str(src),
        "-o",
        str(out),
        "--dim",
        "station_id",
        "--value",
        "TA00072",
        "--value",
        "TA00131",
    )
    result = xr.open_zarr(out, consolidated=True)
    assert list(result["station_id"].values) == ["TA00072", "TA00131"]
    assert load_history(out)[-1]["skill"] == "select"


def test_select_one_station_keeps_identity_coords(tmp_path, select):
    import pandas as pd

    times = pd.to_datetime(["2026-08-19", "2026-08-20"])
    long = pd.DataFrame(
        {
            "precip": [1.0, 2.0, 3.0, 4.0],
            "station_id": ["TA00072", "TA00072", "TA00131", "TA00131"],
            "time": list(times) * 2,
        }
    ).set_index(["time", "station_id"])
    ds = xr.Dataset.from_dataframe(long)
    ds = ds.assign_coords(
        name=("station_id", ["Likoni", "Shimo la Tewa"]),
        latitude=("station_id", [-4.08, -3.99]),
        longitude=("station_id", [39.66, 39.74]),
    )
    ds["precip"].attrs.update(units="mm day-1", standard_name="lwe_precipitation_rate")
    src = write_zarr(ds, tmp_path / "in.zarr")
    out = tmp_path / "one.zarr"
    run_skill(
        select,
        "-i",
        str(src),
        "-o",
        str(out),
        "--dim",
        "station_id",
        "--value",
        "TA00072",
    )
    result = xr.open_zarr(out, consolidated=True)
    assert "station_id" not in result.dims
    assert str(result["station_id"].values) == "TA00072"
    assert str(result["name"].values) == "Likoni"


def _with_coordless_dim(tmp_path):
    ds = make_gridded(n_time=3)
    ds["member_data"] = ds["precip"].expand_dims(member=3, axis=-1)
    src = write_zarr(ds, tmp_path / "in.zarr")
    assert "member" not in xr.open_zarr(src, consolidated=True).coords
    return src


def test_select_value_rejects_dim_without_coord(tmp_path, select):
    src = _with_coordless_dim(tmp_path)
    out = tmp_path / "out.zarr"
    with pytest.raises(SystemExit) as exc:
        run_skill(select, "-i", str(src), "-o", str(out), "--dim", "member", "--value", "1")
    assert exc.value.code == 2
    assert not out.exists()


def test_select_index_accepts_dim_without_coord(tmp_path, select):
    src = _with_coordless_dim(tmp_path)
    out = tmp_path / "out.zarr"
    run_skill(select, "-i", str(src), "-o", str(out), "--dim", "member", "--index", "1")
    assert "member" not in xr.open_zarr(out, consolidated=True).dims


@pytest.mark.parametrize(
    "args",
    [
        ("--dim", "nope", "--index", "0"),
        ("--dim", "time", "--index", "3"),
        ("--dim", "time", "--index", "-4"),
        ("--dim", "time", "--value", "garbage"),
        ("--dim", "time", "--value", "2026-13-45"),
    ],
)
def test_select_bad_selector_exits_2(tmp_path, select, args):
    src = write_zarr(make_gridded(n_time=3), tmp_path / "in.zarr")
    out = tmp_path / "out.zarr"
    with pytest.raises(SystemExit) as exc:
        run_skill(select, "-i", str(src), "-o", str(out), *args)
    assert exc.value.code == 2
    assert not out.exists()


def test_select_bad_timedelta_value_exits_2(tmp_path, select):
    src = write_zarr(make_forecast(n_step=3), tmp_path / "in.zarr")
    with pytest.raises(SystemExit) as exc:
        run_skill(
            select, "-i", str(src), "-o", str(tmp_path / "o.zarr"), "--dim", "step", "--value", "x"
        )
    assert exc.value.code == 2


def test_select_negative_index_and_values_accepted(tmp_path, select):
    src = write_zarr(make_forecast(n_step=3), tmp_path / "in.zarr")
    out_i = tmp_path / "i.zarr"
    out_v = tmp_path / "v.zarr"
    run_skill(select, "-i", str(src), "-o", str(out_i), "--dim", "step", "--index", "-3")
    run_skill(select, "-i", str(src), "-o", str(out_v), "--dim", "step", "--value", "0D")
    a = xr.open_zarr(out_i, consolidated=True)
    b = xr.open_zarr(out_v, consolidated=True)
    assert "step" not in a.dims and "step" not in b.dims
    xr.testing.assert_identical(a["tp"].load(), b["tp"].load())


def test_select_records_index_as_canonical_integers(tmp_path, select):
    """SKILL.md: index positions are recorded as canonical integers (`0` == `00`)."""
    src = write_zarr(make_gridded(n_time=4), tmp_path / "in.zarr")
    out = tmp_path / "out.zarr"
    run_skill(
        select, "-i", str(src), "-o", str(out), "--dim", "time", "--index", "-1", "--index", "00"
    )
    assert load_history(out)[-1]["args"]["index"] == [-1, 0]
    ds = xr.open_zarr(out, consolidated=True)
    assert ds.sizes["time"] == 2


@pytest.mark.parametrize("raw", ["1.0", "+1", "1_0", "x"])
def test_select_index_rejects_non_integer_literal(tmp_path, select, raw):
    src = write_zarr(make_gridded(n_time=3), tmp_path / "in.zarr")
    with pytest.raises(SystemExit) as exc:
        run_skill(
            select, "-i", str(src), "-o", str(tmp_path / "o.zarr"), "--dim", "time", "--index", raw
        )
    assert exc.value.code == 2


def test_select_index_zero_aliases_are_duplicates(tmp_path, select):
    src = write_zarr(make_gridded(n_time=3), tmp_path / "in.zarr")
    with pytest.raises(SystemExit) as exc:
        run_skill(
            select,
            "-i",
            str(src),
            "-o",
            str(tmp_path / "o.zarr"),
            "--dim",
            "time",
            "--index",
            "0",
            "--index",
            "00",
        )
    assert exc.value.code == 2
