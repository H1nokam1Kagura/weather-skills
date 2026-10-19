---
name: windborne-fetch
description: "Fetch a WindBorne WeatherMesh-6 AI forecast (128-member ensemble, 3-hourly leads to 15 days, 0.25°) for a recent init over the region the API key may read, as a weather-skills Zarr: ensemble mean, spread or all members of precipitation, 2 m temperature, winds, pressure and pressure-level fields. Needs WindBorne API credentials. Serves only about the last two days of runs."
license: MIT
compatibility: Requires Python 3.12 and uv. Reads the WindBorne Forecasts API (api.windbornesystems.com). Requires WB_CLIENT_ID and WB_API_KEY in the environment.
allowed-tools: Bash(uv run ${CLAUDE_SKILL_DIR}/scripts/fetch.py *)
metadata:
  version: "0.0.1"
  catalog-group: fetchers
  variables:
    - tp
    - t2m
    - d2m
    - msl
    - u10
    - v10
    - u100
    - v100
    - ws10
    - ws100
    - tmax
    - tmin
    - sp
    - tcwv
    - cape
    - tcc
    - ssrd
    - skt
    - t
    - z
    - q
    - u
    - v
  openclaw:
    requires:
      env:
        - WB_CLIENT_ID
        - WB_API_KEY
    primaryEnv: WB_API_KEY
---

# windborne-fetch

Fetches one WindBorne WeatherMesh-6 (`wm-6`) run from the WindBorne API as a weather-skills standard dataset Zarr.

## Usage

```bash
uv run ${CLAUDE_SKILL_DIR}/scripts/fetch.py [--date YYYY-MM-DD] [--bbox N/W/S/E] [-v VAR ...] [--level HPA ...] [--ensemble mean|std|members] [--max-lead HOURS] -o OUT.zarr
```

### Arguments
- `--date` — init date; the latest run published that day is fetched. Default: the latest run. Only runs on the live list (about the last two days) are served; older dates exit 1.
- `--bbox` — `N/W/S/E`. Default: the whole region the key may read. The API returns that region; the box is cut from it, so a box reaching past it is trimmed to the overlap.
- `--variable`, `-v` — one field per flag; repeat for several. Default: `tp`. Short names: `tp`, `t2m`, `d2m`, `msl`, `u10`, `v10`, `u100`, `v100`, `ws10`, `ws100`, `tmax`, `tmin`, `sp`, `tcwv`, `cape`, `tcc`, `ssrd`, `skt`. Pressure-level: `t`, `z`, `q`, `u`, `v`. Any other name exits 2.
- `--level` — hPa, repeatable; required for pressure-level fields. 25 levels, 10–1000 hPa; the API rejects any other level at download.
- `--ensemble` — `mean` (default), `std` (ensemble spread), or `members` (all 128). Members exist only for `tp`, `t2m`, `d2m`, `msl`, `u10`, `v10`, `u100`, `v100`, `tcc` and `z` at 500 hPa; other fields exit 1.
- `--max-lead` — last lead hour, a multiple of 3. Default: 360 (15 days).
- `--output`, `-o` — output Zarr, overwritten.

### Output
- Dims `(step, latitude, longitude)`, plus `vertical` (hPa) for pressure-level fields and `number` (0–127) for `--ensemble members`. 0.25° grid.
- Scalar `time` is the run's forecast zero, which leads count from. For off-synoptic runs it is earlier than the init hour (the 20Z run has forecast zero 18Z); the init is stamped as `windborne_initialization_time`.
- `tp` is a rate in `mm day-1` over `[step, step + 3 h)`. Lead 0 holds the 3 h before forecast zero, so it is dropped — for every field when `tp` is in the request.
- `t2m`, `d2m`, `tmax`, `tmin`, `skt`, `t` are `degree_Celsius`; `msl`, `sp` are `hPa`; `z` is geopotential in `m2 s-2`. Under `--ensemble std` values stay in kelvin and pascals.
- Stamps `data_interval = 3 hour`, `weather_skills_source=windborne-wm6` and `windborne_ensemble`.

### Notes
- Set `WB_CLIENT_ID` and `WB_API_KEY` in the environment for every call; without them it exits 2. Never write their values into files or commands.
- Each field, level and lead is a separate request, so a full run of one field is 121 requests (a few seconds). `--ensemble members` is ~7 MB per lead over the key region: about 1 GB for a full run of one field.
- History is not reliably served: archive runs mostly return 404. Save outputs if you need them later.
- `tmax`, `tmin` and the other `_3h` fields cover the 3 h ending at the step. `ssrd` stays an amount per 3 h in J m-2.

### Example
```bash
uv run ${CLAUDE_SKILL_DIR}/scripts/fetch.py --bbox 5.5/33.5/-5/42.5 -v tp -v t2m -o wm6_kenya.zarr
```
Writes the latest run's 15-day ensemble-mean precipitation rate and 2 m temperature over Kenya.
