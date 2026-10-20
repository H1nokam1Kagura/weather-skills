---
name: csv-io
description: "Export a weather-skills Zarr to a CSV table (one row per time, station or grid cell, units in the headers), or read a CSV of station records, a gridded table, or a single rain gauge into a weather-skills Zarr. Use when someone wants data in a spreadsheet, or has their own CSV, such as station rainfall, to compare, plot or verify."
license: MIT
compatibility: Requires Python 3.12 and uv.
allowed-tools: Bash(uv run ${CLAUDE_SKILL_DIR}/scripts/csv_io.py *)
metadata:
  version: "0.0.1"
  catalog-group: transforms
---

# csv-io

Moves data between a weather-skills Zarr and a tidy CSV table, in either direction: pass exactly one of `-i` (export) or `--csv` (read).

## Usage

```bash
uv run ${CLAUDE_SKILL_DIR}/scripts/csv_io.py -i IN.zarr -o OUT.csv [-v VAR ...] [--dropna] [--max-rows N]
uv run ${CLAUDE_SKILL_DIR}/scripts/csv_io.py --csv IN.csv -o OUT.zarr [--date-format FMT] [--na-value CODE ...] [--units COLUMN=UNITS ...] [--point LAT,LON[,ID]] [--index COLUMN ...] [-v VAR ...]
```

### Arguments
- `--input`, `-i` — Zarr to export.
- `--csv` — CSV to read.
- `--output`, `-o` — the `.csv` to write when exporting, the `.zarr` to write when reading.
- `--variable`, `-v` — keep only these data variables; repeat for several. Default: all.
- `--dropna` — export: skip rows where every variable is missing, such as ocean cells of a masked grid.
- `--max-rows` — export: refuse to write more rows than this. Default: 2000000.
- `--date-format` — read: strftime format of the time column. Without it only `YYYY-MM-DD` is accepted; `01/10/2026` is ambiguous, so day-first dates need `--date-format %d/%m/%Y`.
- `--na-value` — read: extra missing-value code, such as `-99`, `-999`, `T`; repeat for several. Empty cells are always missing.
- `--units` — read: `COLUMN=UNITS` for a column whose header has no units; repeat for several.
- `--point` — read: `LAT,LON[,ID]` for a single-site file with no station or lat/lon columns, such as one gauge's `date,rain`. ID defaults to `P0`. A negative latitude needs `=`: `--point=-1.29,36.82,dagoretti`.
- `--index` — read: column to use as a dim instead of detecting the layout; repeat for several.

### Output
- Export: columns are the dims (`time`, `station_id`, `latitude`, `longitude`, `step`, …), then other coords (station `name`, a forecast's init `time`), then variables with units in the header (`precip [mm]`, `t2m [degC]`). Dates are `YYYY-MM-DD`, with a time of day only when there is one. Leads are numbers, `step [days]` or `step [hours]`. Missing values are empty cells.
- Read, station data: dims `(station_id, time)` with `latitude`/`longitude` on `station_id`, CF `featureType=timeSeries`.
- Read, gridded data: `latitude`/`longitude` dims, sorted, longitudes in -180..180.
- Read: `weather_skills_source=csv:<file name>` and the CSV's sha256 in `source_csv_sha256`.

### Notes
- Units come from the header, `rain [mm]` or `Rainfall (mm)`; columns with none are listed on stderr. Precipitation and temperature need units before later skills can use them.
- Column names are cleaned for CF: spaces and punctuation become `_` (`Station Name` → `Station_Name`). `--units` and `--index` take the cleaned names.
- Missing values stay missing: a station/time absent from the CSV is NaN, nothing is filled. Duplicate rows for one station and time exit 2.
- A CSV keeps names, units and values only; other attrs (`aggregation_period`, `cell_methods`) and provenance are not written, so export at the end of a pipeline.

### Example
```bash
uv run ${CLAUDE_SKILL_DIR}/scripts/csv_io.py --csv kmd_stations.csv -o kmd_stations.zarr \
  --date-format %d/%m/%Y --na-value -99
```
Reads a met-service sheet with columns `Station,Station Name,Lat,Lon,Date,Rainfall (mm),Remarks` into a daily station Zarr of `Rainfall` in mm, with `Station_Name` on each station and `Remarks` dropped.

## Details

The read layout comes from the column names (case-insensitive):

| Column | Becomes |
|---|---|
| `station_id`, `station`, `point_id`, `site` | station dim `station_id` (read as text, so leading zeros stay) |
| `latitude`, `lat`, `longitude`, `lon`, `long` | per-station coords, or grid dims when there is no station column |
| `time`, `date`, `datetime`, `valid_time` | `time` dim |
| `step [days]`, `step [hours]`, `lead_time` | forecast `step` dim; a constant `time` beside it is the init |
| `number`, `member`, `realization` | ensemble `number` dim |
| `level`, `pressure` | `level` dim |

Numeric columns become data variables. Text columns constant per station (a station name) become station coords; other text columns (remarks, quality flags) are dropped with a note on stderr. Numeric per-station `altitude`/`elevation` columns are station coords.
