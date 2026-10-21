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
# Print the latest ended day (not a completeness check), and exit
uv run ${CLAUDE_SKILL_DIR}/scripts/fetch.py --probe-latest [--station ID | --bbox N/W/S/E]
```

### Arguments
- `--list-stations` — print the portal's stations as TSV (`station_id`, `name`, `site`, `latitude`, `longitude`) and exit. With `--bbox`, only those inside it. Stations without site coordinates list with blank latitude and longitude. Needs no credentials.
- `--station` — CHORDS instrument id such as `4`; repeat for several. When given, `--bbox` is not used to select stations. An unknown id exits 2; a station with no site coordinates exits 1.
- `--bbox` — `N/W/S/E` decimal degrees. Without `--station`, selects every station whose site lies inside the box. W > E crosses the antimeridian.
- `--start-time`, `--end-time` — inclusive range of days, `YYYY-MM-DD`.
- `--day-start-hour` — UTC hour each day starts at, 0-23. Default: 0. Day D runs from D HH:00 to D+1 HH:00 UTC and is labelled D HH:00, the actual interval start. Select this boundary from the comparison contract, not an assumed gauge reset time.
- `--reporting-interval-minutes` — independently known nominal cadence, a positive integer divisor of 1440. Required for defensible reference qualification; if omitted, median observed cadence is inferred and output is marked `cadence_source="inferred_unverified"`. Inference cannot detect systematic loss of every second reading or prove actual completeness.
- `--portal` — CHORDS portal base URL. Default: `https://3d-fewsnet.icdp.ucar.edu` (3D-PAWS FEWS NET). Other 3D-PAWS portals work too, e.g. `https://3d-kenya.chordsrt.com` (GLOBE Kenya schools), each with its own key.
- `--workers` — concurrent station downloads. Default: 8.
- `--probe-latest` — print the latest day that has ended (on the `--day-start-hour` day) before the newest reading from the `--station` / `--bbox` stations (default: every located station) and exit. One latest-reading request per station, so it takes seconds; it does not check that the day is complete, which only a fetch does. Any value passed is ignored.
- `--output`, `-o` — output Zarr, overwritten.

### Output
- Dims `(time, station_id)` with coords `latitude`, `longitude`, `name` (instrument name), `site` on `station_id`. `featureType="timeSeries"`, stamped `data_interval = "1 day"`.
- `precip` (`mm day-1`, daily sum of the rain gauge), `temperature` / `tmax` / `tmin` (`degree_Celsius`, daily mean / maximum / minimum of the MCP9808 sensor `mt1`), `humidity` (`%`, daily mean of `sh1`), `pressure` (`hPa`, station pressure, daily mean of `bp1`), `wind_speed` (`m s-1`, daily mean of `ws`), `wind_gust` (`m s-1`, daily maximum of `wg`) — whichever the stations report.
- `time` holds every requested day's actual UTC start. Stations with no day passing any variable's coverage check are left out. `cadence_source`, `completeness_policy`, `rainfall_measurement_assumption`, and `source_url` describe the applied assumptions.

### Quality control
- Readings outside a plausible range are missing, which drops the `-999.9` a station sends for a failed read. An exact `0` for temperature, humidity or pressure is missing too: stations without that sensor report 0. Readings flagged as test data are dropped.
- When both rain channels appear, `precip` is gauge 1 (`rg1`, or `rg` on older stations) only where gauge 2 (`rg2`) also passes coverage and the two totals agree within 1 mm or 25 %, whichever is larger. A present but entirely invalid second channel still rejects rainfall. When no second channel appears, gauge 1 alone is used; channel absence does not establish hardware configuration.

### Daily values: complete records only
- Every expected timestamp on the day-start-anchored cadence grid must have a valid reading for that variable. Missing increments yield NaN, never zero or a scaled total. Any off-grid timestamp rejects that day: clock jitter deliberately fails closed. No broad slots tolerate lost increments. Empty/invalid records or unresolvable inferred cadence produce no usable daily record. This policy applies to means/extrema too; it may reject otherwise useful partial observations.
- Each variable is judged on its own readings; `tmax` and `tmin` share `temperature`'s, so the three are NaN on the same days.

### Notes
- These are station reference candidates, not automatically ground truth. Confirm instrument calibration, channel units, nominal cadence, and timestamp semantics before comparisons. The adapter assumes rain channels are **millimetre increments whose timestamps mark interval starts**; it does not identify cumulative counters, convert resets, or shift end-labelled increments. These assumptions are unverified by the API mocks. Even explicit cadence only checks coverage against the supplied contract.
- Compare with the exact IMERG/CHIRPS product and matching UTC support; a 06-06 station day cannot be compared directly with a 00-00 daily grid. Choose station-to-grid matching explicitly. Portal/instrument IDs and locations require independent qualification.
- Set `CHORDS_EMAIL` and `CHORDS_API_KEY` for downloads and `--probe-latest`; without them it exits 2. Keys are issued per portal on its `/users` page, and the account needs download access; a rejected key exits 2.
- Stations often miss days. Never fill or extrapolate them. For multi-day totals, a station missing any day of the period must come out NaN; report how many stations were complete. stderr prints each station's reporting interval and complete days and, per variable, how many stations are complete for the whole range (`precip: 2 of 4 stations complete`).
- A station whose download fails is dropped with a stderr line. No data for any station exits 1.
- Site coordinates come from the portal and are not checked; a few test or draft instruments sit at the NCAR site in Boulder, which a bbox excludes.

### Example
```bash
uv run ${CLAUDE_SKILL_DIR}/scripts/fetch.py --list-stations --bbox 5.5/33.5/-5/42.5
uv run ${CLAUDE_SKILL_DIR}/scripts/fetch.py --station CHOSEN_ID --start-time YYYY-MM-DD --end-time YYYY-MM-DD --reporting-interval-minutes KNOWN_MINUTES --day-start-hour 6 -o paws_kenya.zarr
```
Discover stations first, then replace placeholders with the selected instrument, dates and independently known cadence. The example's 06 UTC boundary is a choice to verify against the comparison product, not a claim about any instrument's calibration or reset behavior.
