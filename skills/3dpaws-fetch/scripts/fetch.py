# /// script
# requires-python = ">=3.12,<3.13"
# dependencies = [
#   "weather-skills-core>=0.1.1",
#   "pandas",
# ]
# ///
"""Fetch daily 3D-PAWS weather-station observations from a CHORDS portal as a station Zarr."""
# 3D-PAWS (3D-Printed Automatic Weather Stations, UCAR/NCAR) stream raw
# readings to CHORDS portals. Credentials: CHORDS_EMAIL and CHORDS_API_KEY,
# issued per portal on the portal's /users page.
#
# List stations in a region (no ``-o``)::
#
#     3dpaws-fetch --list-stations --bbox N/W/S/E
#
# Then fetch chosen station ids (or every station in a bbox).

import csv
import json
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed

from weather_skills_core import DataError, UsageError, weather_skill
from weather_skills_core.standard_utils import is_transient, require_env
from weather_skills_core.units import stamp_data_interval

# Auto-populated by the version-bump CI workflow. Do not edit manually.
_SKILL_VERSION = "0.0.1"

DEFAULT_PORTAL = "https://3d-fewsnet.icdp.ucar.edu"
DEFAULT_WORKERS = 8
# CHORDS refuses a request over its max_download_points (200,000 on the FEWS
# NET portal). A 1-minute station with ~23 variables sends ~33,000 points a
# day, so 3-day requests stay well under the cap.
CHUNK_DAYS = 3

# CHORDS short names. Gauge 1 is "rg1" on newer stations and "rg" on older ones.
GAUGE1 = ("rg1", "rg")
GAUGE2 = "rg2"
# Output variable -> (CHORDS short name, daily statistic).
SENSORS = {
    "temperature": ("mt1", "mean"),
    "tmax": ("mt1", "max"),
    "tmin": ("mt1", "min"),
    "humidity": ("sh1", "mean"),
    "pressure": ("bp1", "mean"),
    "wind_speed": ("ws", "mean"),
    "wind_gust": ("wg", "max"),
}
# Plausible range per short name. Outside it the reading is missing; this drops
# the -999.9 a station sends for a failed read.
VALID_RANGE = {
    "mt1": (-40.0, 60.0),
    "sh1": (0.0, 100.0),
    "bp1": (500.0, 1100.0),
    "ws": (0.0, 75.0),
    "wg": (0.0, 100.0),
    "rg": (0.0, 100.0),
    "rg1": (0.0, 100.0),
    "rg2": (0.0, 100.0),
}
# A station with no sensor fitted reports exactly 0 for it, so an exact 0 is
# missing for these (calm wind and no rain are real zeros and stay).
ZERO_IS_MISSING = {"mt1", "sh1", "bp1"}
# Two gauges agree when their daily totals differ by at most this much.
GAUGE_TOLERANCE_MM = 1.0
GAUGE_TOLERANCE_FRAC = 0.25
CF_META = {
    "precip": ("lwe_precipitation_rate", "mm day-1", "time: sum", "daily rainfall"),
    "temperature": (
        "air_temperature",
        "degree_Celsius",
        "time: mean",
        "daily mean air temperature",
    ),
    "tmax": ("air_temperature", "degree_Celsius", "time: maximum", "daily maximum air temperature"),
    "tmin": ("air_temperature", "degree_Celsius", "time: minimum", "daily minimum air temperature"),
    "humidity": ("relative_humidity", "%", "time: mean", "daily mean relative humidity"),
    "pressure": ("surface_air_pressure", "hPa", "time: mean", "daily mean station pressure"),
    "wind_speed": ("wind_speed", "m s-1", "time: mean", "daily mean wind speed"),
    "wind_gust": ("wind_speed_of_gust", "m s-1", "time: maximum", "daily maximum wind gust"),
}


def _get_json(url: str, params: dict | None = None):
    """GET ``url`` and decode JSON, retrying one transient error after a short backoff."""
    if params:
        url = url + "?" + urllib.parse.urlencode(params)
    request = urllib.request.Request(url, headers={"Accept": "application/json"})
    for attempt in range(2):
        try:
            with urllib.request.urlopen(request, timeout=300) as response:
                return json.load(response)
        except urllib.error.HTTPError as exc:
            if exc.code == 403:
                raise UsageError(
                    "CHORDS refused CHORDS_EMAIL / CHORDS_API_KEY (HTTP 403). Keys are "
                    "issued per portal: use the key from this portal's /users page, and "
                    "the account needs data-download access."
                ) from None
            if attempt == 0 and is_transient(exc):
                time.sleep(2.0)
                continue
            body = exc.read().decode(errors="replace")[:300]
            raise DataError(f"CHORDS HTTP {exc.code}: {body}") from None
        except (urllib.error.URLError, TimeoutError) as exc:
            if attempt == 0:
                time.sleep(2.0)
                continue
            raise DataError(f"could not reach CHORDS ({exc})") from None


def _load_stations(portal: str):
    """Return the portal's instruments joined to their site coordinates, as a DataFrame."""
    import pandas as pd

    instruments = pd.DataFrame(_get_json(f"{portal}/instruments.json"))
    sites = pd.DataFrame(_get_json(f"{portal}/sites.json"))
    sites = sites.rename(columns={"id": "site_id", "name": "site"})[
        ["site_id", "site", "lat", "lon"]
    ]
    stations = instruments.merge(sites, on="site_id", how="left")
    stations["station_id"] = stations["id"].astype(str)
    stations["name"] = stations["name"].str.strip()
    stations["site"] = stations["site"].str.strip()
    stations["latitude"] = pd.to_numeric(stations["lat"], errors="coerce")
    stations["longitude"] = pd.to_numeric(stations["lon"], errors="coerce")
    return stations[["station_id", "name", "site", "latitude", "longitude"]]


def _filter_bbox(stations, bbox):
    north, west, south, east = bbox
    lat = stations["latitude"]
    lon = stations["longitude"]
    lat_in = (lat >= south) & (lat <= north)
    if west <= east:
        lon_in = (lon >= west) & (lon <= east)
    else:
        lon_in = (lon >= west) | (lon <= east)
    return stations.loc[lat_in & lon_in].copy()


def _select_stations(stations, station_ids, bbox):
    """Return rows for explicit ``--station`` ids, else every located station in ``--bbox``."""
    located = stations.dropna(subset=["latitude", "longitude"])
    if station_ids:
        wanted = [s.strip() for s in station_ids]
        unknown = [s for s in wanted if s not in set(stations["station_id"])]
        if unknown:
            raise UsageError("unknown station id(s): " + ", ".join(unknown))
        no_coords = [s for s in wanted if s not in set(located["station_id"])]
        if no_coords:
            raise DataError(
                "station(s) have no site coordinates on the portal: " + ", ".join(no_coords)
            )
        return located.set_index("station_id", drop=False).loc[wanted].reset_index(drop=True)
    selected = _filter_bbox(located, bbox)
    if selected.empty:
        north, west, south, east = bbox
        raise DataError(f"no stations in --bbox {north:g}/{west:g}/{south:g}/{east:g}.")
    return selected


def _fetch_records(portal, station_id, first, last, auth):
    """Download raw readings in ``[first, last)`` as a time-indexed DataFrame, or None."""
    import pandas as pd

    rows = []
    chunk_start = first
    while chunk_start < last:
        chunk_end = min(chunk_start + pd.Timedelta(days=CHUNK_DAYS), last)
        params = {
            "start": chunk_start.strftime("%Y-%m-%dT%H:%M:%SZ"),
            "end": chunk_end.strftime("%Y-%m-%dT%H:%M:%SZ"),
            **auth,
        }
        payload = _get_json(f"{portal}/api/v1/data/{station_id}.json", params)
        for feature in payload.get("features", []):
            for point in feature["properties"]["data"]:
                # CHORDS sends the test flag as the string "true" / "false".
                if str(point.get("test")).lower() != "true":
                    rows.append({"time": point["time"], **point["measurements"]})
        chunk_start = chunk_end
    if not rows:
        return None
    raw = pd.DataFrame(rows)
    raw["time"] = pd.to_datetime(raw["time"], utc=True).dt.tz_convert(None)
    raw = raw.drop_duplicates("time").set_index("time").sort_index()
    raw = raw[(raw.index >= first) & (raw.index < last)]
    return raw.apply(pd.to_numeric, errors="coerce")


def _station_daily(raw, start, end, day_start_hour):
    """Reduce one station's raw readings to daily values (NaN for incomplete days)."""
    import pandas as pd

    # The station's reporting interval (1, 10 or 15 minutes on most portals).
    interval_min = max(1, round(raw.index.to_series().diff().dt.total_seconds().median() / 60))
    # A day is complete when every slot of the day holds at least one valid
    # reading. The slot is 3 reporting intervals (at least 5 minutes) rounded up
    # to a divisor of the day, so a few late or dropped readings are tolerated
    # but any longer outage blanks the day.
    need = max(5, 3 * interval_min)
    slot_min = next(m for m in range(need, 1441) if 1440 % m == 0)
    slots_per_day = 1440 // slot_min

    # Shift by the day-start hour so each day bins as one calendar day labelled
    # by the date it starts on.
    raw = raw.copy()
    raw.index = raw.index - pd.Timedelta(hours=day_start_hour)
    days_index = pd.date_range(start, end, freq="D", name="time")

    clean = {}
    for short in set(GAUGE1) | {GAUGE2} | {s for s, _ in SENSORS.values()}:
        if short not in raw.columns:
            continue
        values = raw[short]
        lo, hi = VALID_RANGE[short]
        values = values.where((values >= lo) & (values <= hi))
        if short in ZERO_IS_MISSING:
            values = values.where(values != 0)
        # A sensor that sent only missing codes is treated as not fitted.
        if values.notna().any():
            clean[short] = values

    def complete_days(values):
        filled_slots = values.notna().resample(f"{slot_min}min").sum() > 0
        return filled_slots.resample("D").sum() == slots_per_day

    daily = pd.DataFrame(index=days_index)
    for name, (short, stat) in SENSORS.items():
        if short not in clean:
            continue
        values = clean[short]
        stat_daily = values.resample("D").agg(stat)
        daily[name] = stat_daily.where(complete_days(values)).reindex(days_index)

    gauge1 = next((g for g in GAUGE1 if g in clean), None)
    if gauge1 is not None:
        values = clean[gauge1]
        total1 = values.resample("D").sum().where(complete_days(values))
        if GAUGE2 in clean:
            # Both gauges must report a complete day and agree; a disagreement
            # means one gauge is faulty and there is no way to tell which.
            values2 = clean[GAUGE2]
            total2 = values2.resample("D").sum().where(complete_days(values2))
            larger = pd.concat([total1, total2], axis=1).max(axis=1)
            tolerance = (GAUGE_TOLERANCE_FRAC * larger).clip(lower=GAUGE_TOLERANCE_MM)
            total1 = total1.where((total1 - total2).abs() <= tolerance)
        daily["precip"] = total1.reindex(days_index)
    return daily, interval_min


@weather_skill(
    name="3dpaws-fetch",
    version=_SKILL_VERSION,
)
@weather_skill.argument("--start-time", required=True)
@weather_skill.argument("--end-time", required=True)
@weather_skill.argument(
    "--bbox",
    help=(
        "Select every station with site coordinates in this box. With "
        "--list-stations, the region to list. Ignored for selection when --station is given."
    ),
)
@weather_skill.argument(
    "--station",
    action="append",
    help="CHORDS instrument id (repeatable), e.g. 4. Discover ids with --list-stations.",
)
@weather_skill.argument(
    "--portal",
    default=DEFAULT_PORTAL,
    help=(
        f"CHORDS portal base URL (default {DEFAULT_PORTAL}, the FEWS NET 3D-PAWS "
        "network). Another 3D-PAWS portal, e.g. https://3d-kenya.chordsrt.com, needs that portal's key."
    ),
)
@weather_skill.argument(
    "--workers",
    type=int,
    default=DEFAULT_WORKERS,
    help=f"Max concurrent station downloads (default {DEFAULT_WORKERS}).",
)
@weather_skill.argument(
    "--day-start-hour",
    type=int,
    default=0,
    help=(
        "UTC hour each daily value starts at (0-23, default 0). Day D covers "
        "D HH:00 to D+1 HH:00 UTC and is labelled D; e.g. 6 for KMD's 06 UTC rainfall day."
    ),
)
@weather_skill.argument(
    "--list-stations",
    action="store_true",
    default=False,
    probe=True,
    help=(
        "Print stations in --bbox (or all, without --bbox) as TSV on stdout "
        "(station_id, name, site, latitude, longitude) and exit. No -o."
    ),
)
@weather_skill.argument(
    "--probe-latest",
    nargs="?",
    const="",
    metavar="IDENT",
    probe=True,
    help=(
        "Print the latest day with a complete record in the last 14 days "
        "(YYYY-MM-DD) on stdout and exit. Any IDENT passed is ignored."
    ),
)
def fetch(
    start_time,
    end_time,
    bbox,
    station,
    portal,
    workers,
    day_start_hour,
    list_stations,
    probe_latest,
    **kwargs,
):
    """Fetch daily 3D-PAWS weather-station observations as a weather-skills station Zarr."""
    from datetime import UTC, datetime, timedelta

    import pandas as pd
    import xarray as xr

    if not 0 <= day_start_hour <= 23:
        raise UsageError(f"--day-start-hour must be 0-23, got {day_start_hour}.")
    if not list_stations and probe_latest is None and not station and bbox is None:
        raise UsageError("pass --station ID (repeatable) or --bbox N/W/S/E.")

    portal = portal.rstrip("/")
    stations = _load_stations(portal)

    if list_stations:
        listed = stations if bbox is None else _filter_bbox(stations, bbox)
        writer = csv.writer(sys.stdout, dialect="excel-tab", lineterminator="\n")
        writer.writerow(["station_id", "name", "site", "latitude", "longitude"])
        for _, row in listed.sort_values("station_id", key=lambda s: s.astype(int)).iterrows():
            lat = "" if pd.isna(row["latitude"]) else f"{row['latitude']:.5f}"
            lon = "" if pd.isna(row["longitude"]) else f"{row['longitude']:.5f}"
            writer.writerow([row["station_id"], row["name"], row["site"], lat, lon])
        print(f"{len(listed)} stations", file=sys.stderr)
        return

    email, api_key = require_env(
        "CHORDS_EMAIL",
        "CHORDS_API_KEY",
        message="CHORDS_EMAIL and CHORDS_API_KEY must be set (from the portal's /users page).",
    )
    auth = {"email": email, "api_key": api_key}

    if probe_latest is not None:
        end_day = datetime.now(UTC).date()
        start_day = end_day - timedelta(days=14)
        if station or bbox is not None:
            selected = _select_stations(stations, station, bbox)
        else:
            selected = stations.dropna(subset=["latitude", "longitude"]).head(3)
        offset = pd.Timedelta(hours=day_start_hour)
        latest = None
        for _, row in selected.iterrows():
            raw = _fetch_records(
                portal,
                row["station_id"],
                pd.Timestamp(start_day) + offset,
                pd.Timestamp(end_day) + pd.Timedelta(days=1) + offset,
                auth,
            )
            if raw is None:
                continue
            daily, _ = _station_daily(
                raw, start_day.isoformat(), end_day.isoformat(), day_start_hour
            )
            observed = daily.dropna(how="all")
            if not observed.empty:
                day = observed.index.max().date()
                latest = day if latest is None else max(latest, day)
        if latest is None:
            raise DataError("3D-PAWS probe found no recent complete days")
        print(latest.isoformat())
        return

    selected = _select_stations(stations, station, bbox)
    start = start_time.isoformat()
    end = end_time.isoformat()
    offset = pd.Timedelta(hours=day_start_hour)
    first = pd.Timestamp(start) + offset
    last = pd.Timestamp(end) + pd.Timedelta(days=1) + offset
    print(
        f"Fetching {len(selected)} 3D-PAWS station(s) from {portal} {start} → {end}, "
        f"days starting {day_start_hour:02d}:00 UTC",
        file=sys.stderr,
    )

    # Fail fast on bad credentials before starting the pool: an empty window
    # still goes through the portal's download check.
    now = datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
    _get_json(
        f"{portal}/api/v1/data/{selected.iloc[0]['station_id']}.json",
        {"start": now, "end": now, **auth},
    )

    def fetch_one(row):
        raw = _fetch_records(portal, row["station_id"], first, last, auth)
        if raw is None:
            return None
        return _station_daily(raw, start, end, day_start_hour)

    frames = []
    meta_rows = []
    with ThreadPoolExecutor(max_workers=workers) as pool:
        futures = {pool.submit(fetch_one, row): row for _, row in selected.iterrows()}
        for fut in as_completed(futures):
            row = futures[fut]
            sid = row["station_id"]
            try:
                result = fut.result()
            except UsageError:
                raise
            except Exception as exc:  # noqa: BLE001
                print(f"{sid}: DROPPED, fetch failed ({exc})", file=sys.stderr)
                continue
            if result is None:
                print(f"{sid}: no readings in range", file=sys.stderr)
                continue
            daily, interval_min = result
            if daily.empty or daily.isna().all().all():
                print(f"{sid}: no complete day for any variable", file=sys.stderr)
                continue
            daily["station_id"] = sid
            frames.append(daily)
            meta_rows.append(row)
            complete = int(daily.drop(columns="station_id").notna().any(axis=1).sum())
            print(
                f"{sid} ({row['name']}): reports every {interval_min} min, "
                f"{complete}/{len(daily)} days with a complete record",
                file=sys.stderr,
            )

    if not frames:
        raise DataError("no complete daily data for any station.")

    df = pd.concat(frames).reset_index().set_index(["time", "station_id"])
    meta = pd.DataFrame(meta_rows).set_index("station_id")
    ds = xr.Dataset.from_dataframe(df)
    ids = ds["station_id"].values
    ds = ds.assign_coords(
        latitude=("station_id", meta.loc[ids, "latitude"].astype(float).values),
        longitude=("station_id", meta.loc[ids, "longitude"].astype(float).values),
        name=("station_id", meta.loc[ids, "name"].astype(str).values),
        site=("station_id", meta.loc[ids, "site"].fillna("").astype(str).values),
    )
    ds["latitude"].attrs.update(standard_name="latitude", units="degrees_north", axis="Y")
    ds["longitude"].attrs.update(standard_name="longitude", units="degrees_east", axis="X")
    ds["time"].attrs.update(
        standard_name="time",
        axis="T",
        comment=f"day D covers D {day_start_hour:02d}:00 to D+1 {day_start_hour:02d}:00 UTC",
    )
    ds["station_id"].attrs.update(cf_role="timeseries_id", long_name="CHORDS instrument id")
    ds["name"].attrs.update(long_name="instrument name")
    ds["site"].attrs.update(long_name="site name")
    for var in ds.data_vars:
        std_name, units, cell_methods, long_name = CF_META[var]
        ds[var].attrs.update(
            standard_name=std_name,
            units=units,
            cell_methods=cell_methods,
            long_name=long_name,
            coordinates="latitude longitude",
        )
    ds.attrs.update(
        featureType="timeSeries",
        Conventions="CF-1.13",
        weather_skills_source="3dpaws",
        source_url=portal,
    )
    # A station is complete when it reported every requested day; multi-day
    # totals are only meaningful for those (missing days are never filled).
    n_days = (end_time - start_time).days + 1
    for name in sorted(ds.data_vars):
        complete = int((ds[name].notnull().sum("time") == n_days).sum())
        print(
            f"{name}: {complete} of {ds.sizes['station_id']} stations complete "
            f"(all {n_days} days {start} → {end})",
            file=sys.stderr,
        )
    return stamp_data_interval(ds, period="1 day")


if __name__ == "__main__":
    fetch()
