---
name: meteosat-fetch
description: "Fetch a few Meteosat geostationary satellite scans (FCI or SEVIRI) over a lat/lon box from the EUMETSAT Data Store, as a weather-skills Zarr of visible reflectance (%) and infrared brightness temperature (K) on a regular lat/lon grid. Covers Africa, Europe and the Indian Ocean. Needs EUMETSAT credentials. Use for satellite imagery snapshots of cloud tops, convection, fog or dust at chosen times, not long time series."
license: MIT
compatibility: Requires Python 3.12 and uv. Requires EUMETSAT_CONSUMER_KEY and EUMETSAT_CONSUMER_SECRET in the environment, or an eumdac credentials file at ~/.eumdac/credentials (free account, key from https://api.eumetsat.int/api-key/).
allowed-tools: Bash(uv run ${CLAUDE_SKILL_DIR}/scripts/fetch.py *)
metadata:
  version: "0.0.1"
  catalog-group: fetchers
  variables:
    - ir
    - vis
    - ir_105
    - vis_06
    - wv_63
    - IR_108
    - VIS006
    - WV_062
  openclaw:
    requires:
      env:
        - EUMETSAT_CONSUMER_KEY
        - EUMETSAT_CONSUMER_SECRET
    primaryEnv: EUMETSAT_CONSUMER_KEY
---

# meteosat-fetch

Downloads Meteosat Level 1 scans, calibrates them with satpy, keeps only the `--bbox`, and writes one variable per band on a regular lat/lon grid.

## Usage

```bash
uv run ${CLAUDE_SKILL_DIR}/scripts/fetch.py --date YYYY-MM-DD --time HH:MM [--time HH:MM ...] --bbox N/W/S/E -v {vis,ir,BAND}[,...] [--service 0deg|iodc] [--instrument fci|seviri] [--max-scans N] [--workers N] -o OUT.zarr
uv run ${CLAUDE_SKILL_DIR}/scripts/fetch.py --probe-latest [--service 0deg|iodc] [--instrument fci|seviri]
```

### Arguments
- `--date` — UTC day of the scans, `YYYY-MM-DD`.
- `--time` — UTC `HH:MM`; repeat or comma-separate for several. Each is floored to its repeat-cycle start (10 min FCI, 15 min SEVIRI); times in the same cycle are fetched once.
- `--bbox` — `N/W/S/E` decimal degrees. Required; whole disks are never stored.
- `--variable`, `-v` — bands; repeat or comma-separate. `vis` is every solar channel, `ir` every thermal and water-vapour channel, or name bands (case-insensitive). FCI: `vis_04 vis_05 vis_06 vis_08 vis_09 nir_13 nir_16 nir_22`, `ir_38 wv_63 wv_73 ir_87 ir_97 ir_105 ir_123 ir_133`. SEVIRI: `VIS006 VIS008 IR_016`, `IR_039 WV_062 WV_073 IR_087 IR_097 IR_108 IR_120 IR_134`.
- `--service` — `0deg` (prime satellite at 0°) or `iodc` (Indian Ocean, 45.5°E, SEVIRI only; a better view of East Africa and the Horn). Default: `0deg`.
- `--instrument` — `fci` or `seviri`. Default: `fci` for `0deg` from 2024-09-24, else `seviri`.
- `--max-scans` — refuse requests above this many scans. Default: 12.
- `--workers` — concurrent file downloads per scan. Default: 4.
- `--probe-latest` — print the day of the latest scan (`YYYY-MM-DD`) for `--service`/`--instrument` and exit; stderr gives its `HH:MM` UTC. Any value passed is ignored.
- `--output`, `-o` — output Zarr, overwritten.

### Output
- Dims `(time, latitude, longitude)`, one variable per band named as above. `vis` bands are reflectance in `%` (`toa_bidirectional_reflectance`), `ir` bands brightness temperature in `K` (`toa_brightness_temperature`).
- Regular grid at the finest requested band's native sampling: 0.01° FCI visible, 0.02° FCI infrared only, 0.03° SEVIRI. Nearest-neighbour resampled; pixels off the disk are NaN.
- `time` is the repeat-cycle start; coord `scan_start` is the sensing start. The scan sweeps the disk over 10–12 minutes, so a region is imaged a few minutes after either.
- Attrs `platform`, `instrument`, `eumetsat_service`, `sub_satellite_longitude`; `weather_skills_source=eumetsat-datastore:<collection id>`.

### Notes
- Set `EUMETSAT_CONSUMER_KEY` and `EUMETSAT_CONSUMER_SECRET` in the environment for every call, including `--probe-latest`; without them (or `~/.eumdac/credentials`) it exits 2. Never put the values on the command line.
- Each scan is a large download: ~180 MB for SEVIRI, ~200–300 MB for FCI over a Kenya-sized box (only the strips covering the box). Stderr warns above 4 scans.
- Visible bands are dark at night. A scan missing from the archive exits 1.

### Example
```bash
uv run ${CLAUDE_SKILL_DIR}/scripts/fetch.py --date 2026-10-06 --time 12:00 --time 13:00 \
  --bbox 5.5/33.9/-4.7/41.9 -v vis_06,ir_105 -o fci_kenya.zarr
```
Writes two FCI scans over Kenya, 12:00 and 13:00 UTC, with 0.6 µm reflectance and 10.5 µm brightness temperature on a 0.01° grid.

## Details

| `--service` | `--instrument` | Collection | Satellite | Repeat |
|---|---|---|---|---|
| `0deg` | `fci` | `EO:EUM:DAT:0662` | MTG-I1 (Meteosat-12) | 10 min |
| `0deg` | `seviri` | `EO:EUM:DAT:MSG:HRSEVIRI` | MSG (Meteosat-11/10) | 15 min |
| `iodc` | `seviri` | `EO:EUM:DAT:MSG:HRSEVIRI-IODC` | MSG (Meteosat-9) | 15 min |

There is no FCI Indian Ocean service, and SEVIRI HRV is not offered. FCI L1c starts 2024-09-24. Raw files go to a temporary directory and are deleted after each scan.
