---
name: weathernext-fetch
description: "Fetch a Google WeatherNext AI forecast for one init over an optional lat/lon box, as a weather-skills Zarr: WeatherNext 2 (64-member ensemble, 6-hourly leads to 15 days, 0.25° global, from 2025) or WeatherNext 3 (64-member ensemble or its precomputed mean and percentiles, hourly leads to 15 days, 0.1° global, from 2026). Precipitation, 2 m temperature and dewpoint, winds, sea-level pressure, SST, cloud, radiation and pressure-level fields. Reads the gs://weathernext buckets with a Google Cloud login."
license: MIT
compatibility: Requires Python 3.12 and uv. Reads consolidated Zarr from the WeatherNext 2 and 3 realtime archives via gcsfs. Requires Google Cloud Application Default Credentials (GOOGLE_APPLICATION_CREDENTIALS or `gcloud auth application-default login`) — any authenticated Google account works, the buckets have no special allowlist. WeatherNext 3 ensemble reads are requester pays and need a billing project (--billing-project or GOOGLE_CLOUD_PROJECT).
allowed-tools: Bash(uv run ${CLAUDE_SKILL_DIR}/scripts/fetch.py *)
metadata:
  version: "0.0.2"
  catalog-group: fetchers
  variables:
    - tp
    - 2m_temperature
    - 2m_dewpoint_temperature
    - 10m_u_component_of_wind
    - 10m_v_component_of_wind
    - 100m_u_component_of_wind
    - 100m_v_component_of_wind
    - mean_sea_level_pressure
    - sea_surface_temperature
    - total_cloud_cover
    - surface_solar_radiation_downwards_1hr
    - specific_humidity
    - temperature
    - u_component_of_wind
    - v_component_of_wind
    - vertical_velocity
    - geopotential
  openclaw:
    requires:
      env:
        - GOOGLE_APPLICATION_CREDENTIALS
    primaryEnv: GOOGLE_APPLICATION_CREDENTIALS
    envVars:
      - name: GOOGLE_APPLICATION_CREDENTIALS
        description: Path to a GCP service-account JSON (any Google Cloud identity can read the WeatherNext buckets)
      - name: GOOGLE_CLOUD_PROJECT
        description: Billing project for requester-pays WeatherNext 3 ensemble reads (alternative to --billing-project)
---

# weathernext-fetch

Fetches one WeatherNext forecast init as a weather-skills standard dataset Zarr.

| `--version` / `--product` | Store | Members | Leads | Grid | Cost |
|---|---|---|---|---|---|
| `2` (default) | `gs://weathernext/weathernext_2_0_0/zarr/2025_to_present/` | 64 | 60 × 6 h | 0.25° | free |
| `3` | `gs://weathernext3_spatial/weathernext_3_0_0/zarr/2026_to_present/` | 64 | 360 × 1 h | 0.1° surface, 0.05° station-head, 0.25° upper air | requester pays |
| `3 --product statistics` | `gs://weathernext3_statistics_spatial/weathernext_3_0_0_statistics/zarr/2026_to_present/` | mean, p10/p25/p50/p75/p90 | 360 × 1 h | 0.1° / 0.05° surface | free |

## Usage

```bash
uv run ${CLAUDE_SKILL_DIR}/scripts/fetch.py [--version 2|3] [--product ensemble|statistics] [--date YYYY-MM-DD] [--cycle HH] [--bbox N/W/S/E] -v VAR [-v VAR ...] [--member N ...] [--statistic STAT ...] [--max-lead HOURS] [--billing-project PROJECT] -o OUT.zarr
uv run ${CLAUDE_SKILL_DIR}/scripts/fetch.py [--version 3 ...] --probe-latest [--cycle HH]
```

Needs Google Cloud Application Default Credentials: set `GOOGLE_APPLICATION_CREDENTIALS` to a service-account JSON, or run `gcloud auth application-default login`. Any Google identity can read the buckets; anonymous access fails. The WeatherNext 3 ensemble bucket bills every listing and read to `--billing-project` (default `$GOOGLE_CLOUD_PROJECT`, then `$CLOUDSDK_CORE_PROJECT`); without one the skill exits 2 before touching GCS.

### Arguments
- `--version` — `2` (default) or `3`.
- `--product` — WeatherNext 3 only: `ensemble` (default) or `statistics` (precomputed ensemble mean and percentiles, no members).
- `--date` — init date. Default: the latest published 00/06/12/18 init. WeatherNext 2 inits run daily from 2025-01-01, WeatherNext 3 from 2026.
- `--cycle` — init hour UTC. WeatherNext 2: `00`, `06`, `12`, `18`. WeatherNext 3 also has 48 h interim inits at every other hour (`01`–`05`, `07`–`11`, …). Default: `00` with `--date`, otherwise the newest 00/06/12/18 cycle.
- `--bbox` — `N/W/S/E` decimal degrees; negative longitudes work. Default: the full global grid.
- `--variable`, `-v` — one field per flag; repeat for several. Aliases: `tp`/`precip`/`pr`, `t2m`, `d2m`, `u10`, `v10`, `u100`, `v100`, `msl`, `sst`; pressure levels `t`, `u`, `v`, `w`, `q`, `z`/`geopotential`. WeatherNext 3 adds `tcc`, `lcc`, `mcc`, `hcc`, `ssrd`, `si10`/`si100` (statistics only), `z300`, `z500`, `t300`, `t500`, `u1000`, `v1000`, `imerg_tp`, `experimental_tp`, and the 0.05° `station_head_temperature_2m` / `station_head_dewpoint_temperature_2m`. Native names also work. Default: WeatherNext 2 every field; WeatherNext 3 every 0.1° surface field. An unknown name exits and lists `Available:`.
- `--member` — 0-based member index; repeat for several (negative counts from the end). Default: all 64. Not valid with `--product statistics`.
- `--statistic` — with `--product statistics`: `mean`, `p10`, `p25`, `p50`, `p75`, `p90`; repeat for several. Default: all six.
- `--max-lead` — keep only leads up to this many hours after init.
- `--billing-project` — GCP project billed for WeatherNext 3 ensemble reads.
- `--probe-latest [IDENT]` — print the latest init date (or `none`) and exit; honours `--version`, `--product` and `--cycle`. `IDENT` is ignored.
- `--output`, `-o` — output Zarr, overwritten.

### Output
- Dims `(number, step, [level,] lat, lon)` with scalar `time` = init and `valid_time` on `step`. `number` is 0..63 (absent for statistics); `level` is 13 pressure levels, 50–1000 hPa. WeatherNext 3's `lead_time` × `lead_subtime` becomes one hourly `step`, and its `lat_0p1`/`lon_0p1` (etc.) become `lat`/`lon`.
- `tp` (and WeatherNext 3's `imerg_tp`, `experimental_tp`) is a rate in `mm day-1`, clipped at zero, labelled at the start of its period (`step=0` is `[init, init+6h)` in WeatherNext 2, `[init, init+1h)` in WeatherNext 3). Other fields are instants at `init + step`. Fetched together, the step axis starts at 0: precipitation is NaN at the last lead and the instant fields are NaN at 0 h. Do not run `deaccumulate` on the output.
- WeatherNext 3 fields shared with WeatherNext 2 take the same ERA5-style names (`2m_temperature`, `10m_u_component_of_wind`, …). Statistics variables are `<name>_<stat>` (`tp_mean`, `2m_temperature_p90`).
- Air temperatures are `degree_Celsius`; SST stays `K`; others keep ERA5-style units (`m s-1`, `Pa`, `Pa s-1`, `kg kg-1`, `m2 s-2`, `J m-2`, cloud fraction `1`). Stamped `weather_skills_source=weathernext:<init stamp>`, `weathernext3:<init stamp>` or `weathernext3-statistics:<init stamp>`.

### Notes
- Each store chunk holds the full globe for one member and lead (or 6 h lead block in WeatherNext 3), so `--bbox` shrinks the output but not the download. Cut the read with `-v`, `--member` or `--statistic`, and `--max-lead`. One WeatherNext 2 surface field, all members, global, is ~16 GB; one WeatherNext 3 0.1° field is ~9 GB per member over 15 days, and billed egress.
- WeatherNext 3 stores fields on four grids and lead axes: 0.1° hourly surface, 0.05° hourly station-head, 0.25° hourly single-level upper air (`z500`, `t500`, …) and 0.25° 6-hourly pressure-level cubes (`t`, `u`, `v`, `w`, `q`, `z`). One run takes fields from one group; mixing groups exits 2 and lists the groups.
- Only the per-init realtime stores are read; the older multi-init WeatherNext 2 stores and WeatherNext 3 backfill stores are not.

### Example
```bash
uv run ${CLAUDE_SKILL_DIR}/scripts/fetch.py --date 2026-10-14 --cycle 00 --bbox 5.5/33.5/-5/42.5 \
  -v tp -v t2m --member 0 --member 1 --member 2 --member 3 -o weathernext_kenya.zarr
```
Writes 4 members of WeatherNext 2 6-hourly precip rates and 2 m temperature over Kenya for 15 days.

```bash
uv run ${CLAUDE_SKILL_DIR}/scripts/fetch.py --version 3 --product statistics --bbox 5.5/33.5/-5/42.5 \
  -v tp --statistic mean --statistic p90 --max-lead 72 -o wn3_kenya_tp_stats.zarr
```
Writes the WeatherNext 3 hourly ensemble-mean and 90th-percentile precip rate over Kenya for the first 3 days, from the free statistics bucket.
