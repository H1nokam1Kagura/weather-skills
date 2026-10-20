---
name: 3dpaws-fetch
description: "Fetch daily 3D-PAWS (3D-Printed Automatic Weather Station) observations (rainfall, mean, max and min temperature, humidity, pressure, wind speed and gust) from a CHORDS portal, by default the FEWS NET network of Kenya Met Department and KALRO stations, for chosen station ids or every station in a lat/lon box, as a weather-skills station Zarr. Can also list the stations. Needs a CHORDS email and API key. Use when a task needs 3D-PAWS station data, for example to compare against gridded satellite or forecast data."
license: MIT
compatibility: Requires Python 3.12 and uv. Requires CHORDS_EMAIL and CHORDS_API_KEY in the environment for downloads (not for --list-stations).
allowed-tools: Bash(uv run ${CLAUDE_SKILL_DIR}/scripts/fetch.py *)
metadata:
  version: "0.0.1"
  catalog-group: fetchers
  variables:
    - precip
    - temperature
    - tmax
    - tmin
    - humidity
    - pressure
    - wind_speed
    - wind_gust
  openclaw:
    requires:
      env:
        - CHORDS_EMAIL
        - CHORDS_API_KEY
    primaryEnv: CHORDS_API_KEY
---

# 3dpaws-fetch

Lists the 3D-PAWS stations on a CHORDS portal, or downloads their raw readings and writes daily values as one station Zarr.

## Usage

```bash
# List stations (TSV on stdout, no -o); --bbox optional
uv run ${CLAUDE_SKILL_DIR}/scripts/fetch.py --list-stations [--bbox N/W/S/E] [--portal URL]
# Fetch chosen stations, or every station in a box
uv run ${CLAUDE_SKILL_DIR}/scripts/fetch.py (--station ID [--station ID ...] | --bbox N/W/S/E) --start-time YYYY-MM-DD --end-time YYYY-MM-DD [--day-start-hour H] [--portal URL] [--workers N] -o OUT.zarr
# Print the latest day with a complete record, and exit
uv run ${CLAUDE_SKILL_DIR}/scripts/fetch.py --probe-latest [--station ID | --bbox N/W/S/E]
```

### Arguments
- `--list-stations` — print the portal's stations as TSV (`station_id`, `name`, `site`, `latitude`, `longitude`) and exit. With `--bbox`, only those inside it. Stations without site coordinates list with blank latitude and longitude. Needs no credentials.
- `--station` — CHORDS instrument id such as `4`; repeat for several. When given, `--bbox` is not used to select stations. An unknown id exits 2; a station with no site coordinates exits 1.
- `--bbox` — `N/W/S/E` decimal degrees. Without `--station`, selects every station whose site lies inside the box. W > E crosses the antimeridian.
- `--start-time`, `--end-time` — inclusive range of days, `YYYY-MM-DD`.
- `--day-start-hour` — UTC hour each day starts at, 0-23. Default: 0 (UTC calendar days). Day D runs from D HH:00 to D+1 HH:00 UTC and is labelled D. Most Kenyan stations reset their own rain total at 06 UTC (09 EAT), the KMD rainfall day; pass 6 to match it.
- `--portal` — CHORDS portal base URL. Default: `https://3d-fewsnet.icdp.ucar.edu` (3D-PAWS FEWS NET). Other 3D-PAWS portals work too, e.g. `https://3d-kenya.chordsrt.com` (GLOBE Kenya schools), each with its own key.
- `--workers` — concurrent station downloads. Default: 8.
- `--probe-latest` — print the latest day that has ended (on the `--day-start-hour` day) before the newest reading from the `--station` / `--bbox` stations (default: every located station) and exit. One latest-reading request per station, so it takes seconds; it does not check that the day is complete, which only a fetch does. Any value passed is ignored.
- `--output`, `-o` — output Zarr, overwritten.

### Output
- Dims `(time, station_id)` with coords `latitude`, `longitude`, `name` (instrument name), `site` on `station_id`. `featureType="timeSeries"`, stamped `data_interval = "1 day"`.
- `precip` (`mm day-1`, daily sum of the rain gauge), `temperature` / `tmax` / `tmin` (`degree_Celsius`, daily mean / maximum / minimum of the MCP9808 sensor `mt1`), `humidity` (`%`, daily mean of `sh1`), `pressure` (`hPa`, station pressure, daily mean of `bp1`), `wind_speed` (`m s-1`, daily mean of `ws`), `wind_gust` (`m s-1`, daily maximum of `wg`) — whichever the stations report.
- `time` holds every requested day, labelled by the date each day starts on. Stations with no complete day are left out.

### Quality control
- Readings outside a plausible range are missing, which drops the `-999.9` a station sends for a failed read. An exact `0` for temperature, humidity or pressure is missing too: stations without that sensor report 0. Readings flagged as test data are dropped.
- Stations carry two rain gauges. `precip` is gauge 1 (`rg1`, or `rg` on older stations) only where gauge 2 (`rg2`) also has a complete day and the two daily totals agree within 1 mm or 25 %, whichever is larger. Otherwise the day is NaN, since one gauge is faulty and there is no telling which. A station with only one gauge uses it as is.

### Daily values: complete records only
- Stations report every 1, 10 or 15 minutes; the interval is taken from each station's readings. The day is split into slots of three reporting intervals (at least 5 minutes, rounded up to divide the day evenly). A daily value is NaN unless every slot holds a valid reading, so an outage longer than about three intervals blanks the day. This holds for every variable: a partial day is not 0 mm or a scaled-up sum, and a partial-day mean, maximum or minimum may miss the afternoon peak or the night low.
- Each variable is judged on its own readings; `tmax` and `tmin` share `temperature`'s, so the three are NaN on the same days.

### Notes
- Set `CHORDS_EMAIL` and `CHORDS_API_KEY` for downloads and `--probe-latest`; without them it exits 2. Keys are issued per portal on its `/users` page, and the account needs download access; a rejected key exits 2.
- Stations often miss days. Never fill or extrapolate them. For multi-day totals, a station missing any day of the period must come out NaN; report how many stations were complete. stderr prints each station's reporting interval and complete days and, per variable, how many stations are complete for the whole range (`precip: 2 of 4 stations complete`).
- A station whose download fails is dropped with a stderr line. No data for any station exits 1.
- Site coordinates come from the portal and are not checked; a few test or draft instruments sit at the NCAR site in Boulder, which a bbox excludes.

### Example
```bash
uv run ${CLAUDE_SKILL_DIR}/scripts/fetch.py --list-stations --bbox 5.5/33.5/-5/42.5
uv run ${CLAUDE_SKILL_DIR}/scripts/fetch.py --station 1 --station 4 --start-time 2026-10-01 --end-time 2026-10-17 --day-start-hour 6 -o paws_kenya.zarr
```
Lists the Kenyan stations, then writes daily values for 1-17 October 2026 at Nairobi KMD HQ and Kitui, on the KMD 06 UTC day.
