---
name: label-group
description: "Add label coords to a gridded or station Zarr: the country, admin-1 / admin-2 unit (province, state, county), subregion, continent, or GeoJSON polygon each cell or station lies in, or a time label (month, year, day of year, or month groups such as MAM,OND). Data is unchanged. Use before summarize-dim --by to get regional or seasonal means and totals."
license: MIT
compatibility: Requires Python 3.12 and uv.
allowed-tools: Bash(uv run ${CLAUDE_SKILL_DIR}/scripts/label_group.py *)
metadata:
  version: "0.0.1"
  catalog-group: transforms
---

# label-group

Writes label coords along space or time, so a later step can group, select, or plot by them.

## Usage

```bash
uv run ${CLAUDE_SKILL_DIR}/scripts/label_group.py -i IN.zarr -o OUT.zarr \
  [--level country|admin_1|admin_2|subregion|continent | --geojson FILE --property NAME] \
  [--time month|year|dayofyear|GROUPS ...]
```

### Arguments
- `--input`, `-i` — input Zarr, gridded or station.
- `--output`, `-o` — output Zarr, replaced if it exists.
- `--level` — label each cell or station with the unit at this level that contains it. `country` and `admin_1` / `admin_2` use geoBoundaries outlines, as `clip-region` does; `subregion` (UN, e.g. Eastern Africa) and `continent` use Natural Earth 1:110m outlines.
- `--geojson` — label by the polygons of this GeoJSON FeatureCollection instead.
- `--property` — with `--geojson`: the feature property that holds each polygon's label.
- `--time` — repeatable. `month`, `year`, or `dayofyear` labels each time step with that calendar part. A comma-separated list of month groups, written as runs of month initials (`MAM,OND`, `DJF,MAM,JJA,SON`, `OND`), labels each time step with its group.

Pass at least one of `--level`, `--geojson`, `--time`. `--level` and `--geojson` are mutually exclusive.

### Output
The input with new coords; data variables are unchanged.
- `--level L`: coord `L` on the grid's lat/lon dims or the station dim, holding resolve-region keys (`kenya`, `kenya-nairobi`, `eastern_africa`), plus `L_name` with display names (`Nairobi`).
- `--geojson`: coord named after `--property`, holding that property as text.
- `--time month|year|dayofyear`: coord of that name along `time` (integers).
- `--time GROUPS`: coord `month_group` along `time`, holding the group (`MAM`).
- Cells, stations, or months outside every unit or group get the empty label `""`.
- With both `year` and a group that crosses December, the group's months take the year the group begins: Dec 2023, Jan 2024, and Feb 2024 are all `DJF`, `year` 2023.

### Notes
- A grid cell belongs to the unit containing its centre. A unit too small to contain any cell centre gets no cells; such units are listed on stderr.
- Each country's admin layer is drawn separately, so neighbouring layers overlap slightly along borders and in disputed areas (Ilemi Triangle: Turkana and Eastern Equatoria). A cell in two units keeps the first, in order of country ISO3 code; stderr counts these cells for each pair of units. Pass `--geojson` with your own boundaries when border cells matter.
- Admin outlines are downloaded only for countries that overlap the input (about 1 MB per country for admin_1). Labelling a whole continent at admin_2 downloads every country's layer.
- Labels are resolve-region keys, so one label can be passed to `clip-region --region` or `resolve-region`. An admin name repeated within a country gets `_2`, `_3`, ... appended; those keys do not resolve.
- Month groups may not share a month. Single initials such as `J` match several months and are refused.
- `--time` needs a `time` dimension; relabel a forecast's lead axis with `step-to-time` first.

### Example
```bash
uv run ${CLAUDE_SKILL_DIR}/scripts/label_group.py -i chirps_kenya_daily.zarr -o chirps_kenya_labeled.zarr --level admin_1 --time year --time MAM,OND
```
Labels each CHIRPS cell with its Kenyan county and each day with its year and long-rains (`MAM`) or short-rains (`OND`) season. `summarize-dim --by admin_1 --by year --by month_group --method mean` then gives each county's mean daily rainfall for every season of every year.
