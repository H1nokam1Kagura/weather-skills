# /// script
# requires-python = ">=3.12,<3.13"
# dependencies = [
#   "weather-skills-core @ git+https://github.com/rhiza-research/weather-skills-core@dev",
# ]
# ///
"""Validate a typed goal JSON, fill rule-decidable defaults, and flag what only a human can settle.

Deterministic: no model call, no network, no file written. Exit 0 resolved, 1 invalid,
2 usage error, 3 needs a human answer.
"""

from __future__ import annotations

import json
import re
from collections import Counter
from pathlib import Path

from weather_skills_core import SkillError, UsageError, weather_skill

# Auto-populated by the version-bump CI workflow. Do not edit manually.
_SKILL_VERSION = "0.0.1"

SCHEMA = "rhiza-goal/1"

# --- Vocabulary: the clm-weather-skills compile schema v2/v3 (references/SOURCE.json) ----------
TASKS = {
    "map": "a map of a variable",
    "spread_map": "a map of ensemble spread (member disagreement)",
    "fcst_vs_obs": "a forecast compared side by side with observations",
    "timeseries": "forecast and observations as overlaid time series",
    "bias_map": "a map of forecast minus observations (bias)",
    "station_vs_sat": "gridded satellite data validated against station observations",
    "change_map": "projected climate change: future minus historical baseline",
}
VARIABLES = {
    "precip": "rainfall",
    "t2m": "air temperature 2 m above the ground",
    "sst": "sea surface temperature",
    "soil_moisture": "soil moisture",
}
REGIONS = ["Kenya", "Senegal", "western Kenya", "Ethiopia", "Malawi"]
COUNTRY_ISO3 = {"Kenya": "KEN", "Senegal": "SEN", "Ethiopia": "ETH", "Malawi": "MWI"}
PERIODS = ("weekly", "monthly")
WINDOWS = ("relative", "fixed")
OBS_TASKS = ("fcst_vs_obs", "timeseries")  # the only tasks where obs_source is a free choice (D1)

CORE = ["task", "variable", "region", "time_window", "period", "legacy_cumulative", "obs_source"]
EXTENSION = [
    "window_phrase",
    "window_start",
    "window_end",
    "baseline",
    "season",
    "decision",
    "admin_level",
    "output",
]
# Slots compared across independent samples. CORE is clm's consumed set (D45 asks on any of them);
# baseline and season are compared too because a human preference hides in them.
COMPARED = CORE + ["baseline", "season"]
QUESTION_ORDER = ["task", "variable", "period", "region", "time_window"]
QUESTION_ORDER += ["obs_source", "legacy_cumulative", "baseline", "season"]
MAX_QUESTIONS = 2

# --- Deterministic rules v3rr: regexes copied verbatim from clm probes/o1_compile.py -------------
_STATION = re.compile(r"station|gauge|in[- ]?situ", re.I)
_RELATIVE = re.compile(
    r"\b(next|coming|upcoming|ahead|past|last|recent|recently|latest|rolling|sliding|current|"
    r"so far|to date|until now|up to now|today|tonight|this (week|month|season|year|fortnight)|"
    r"fortnight|lately|ongoing|now|previous|prior|preceding|relative)\b",
    re.I,
)

# Sources. `source` is the technical citation (for the audit trail); `plain` is what a reader of
# the read-back sees, so the read-back never names a tool or a file.
SRC = {
    "schema": (
        "compile schema v2/v3 (clm-weather-skills run_h7.py COMPILE_SYS_V2, "
        "probes/o1_compile.py COMPILE_SYS_V3)",
        "the request format this assistant uses",
    ),
    "v3rr_station": (
        "rule v3rr.obs_station (clm-weather-skills probes/o1_compile.py rule_obs_station, D44)",
        "your request does not mention weather stations or rain gauges",
    ),
    "v3rr_relative": (
        "rule v3rr.relative_cue (clm-weather-skills probes/o1_compile.py rule_relative_cue, D44)",
        "your request has no words like 'last', 'next' or 'recent'",
    ),
    "contract": (
        "goal contract (clm-weather-skills clm_ws/tasks.py generate_goals / FIGURE_FOR)",
        "it is the standard figure for this kind of analysis",
    ),
    "wmo": (
        "WMO standard climatological normal 1991-2020 (WMO Guidelines on the Calculation of "
        "Climate Normals, WMO-No. 1203, 2017)",
        "the World Meteorological Organization's current standard 30-year reference period",
    ),
    "latest": (
        "forecaster agent: latest published day via the fetcher's latest-data probe",
        "nothing in your request fixes the dates, so the newest data available is used",
    ),
    "no_resolution": (
        "compile schema v2: 'Never infer a resolution'",
        "you did not ask for weekly or monthly values, so none is invented",
    ),
    "no_place": (
        "compile schema v2: region null if no place is named",
        "you did not name a place",
    ),
}

READ_OUTPUT = {
    "map": "one map image",
    "spread_map": "one map image of how much the forecast's ensemble members disagree",
    "bias_map": "one map image of forecast minus observed values",
    "change_map": "one map image of future minus the historical reference period",
    "fcst_vs_obs": "side-by-side panels: the forecast next to the observations",
    "station_vs_sat": "side-by-side panels: the satellite product next to the station records",
    "timeseries": "a chart with the forecast and the observations as lines over time",
}

# Which steps each slot value changes: the forecaster plans from this; packets quote it.
PLAN_HINTS = {
    "task": {
        "map": "fetch one dataset, then plot a map",
        "spread_map": "fetch an ensemble forecast, take the spread across members, plot a map",
        "fcst_vs_obs": "fetch a forecast and observations, align their time axes, plot them "
        "side by side",
        "timeseries": "fetch a forecast and observations, align their time axes, plot lines "
        "over time",
        "bias_map": "fetch a forecast and gridded observations, put them on one grid, subtract, "
        "plot a map",
        "station_vs_sat": "fetch a satellite product and station records, sample the grid at "
        "the stations, plot side by side",
        "change_map": "fetch a climate projection and historical observations, average each "
        "over its period, subtract, plot a map",
    },
    "region": "look up the place's boundary, then cut every dataset to it",
    "time_window.relative": "turn the relative phrase into calendar dates against today (UTC)",
    "period": "add up (rainfall) or average (other variables) into {v} values; rainfall is then "
    "expressed as {v} totals in mm",
    "legacy_cumulative": "convert the archive from accumulated-since-start to per-step amounts "
    "first",
    "obs_source.station": "fetch station / rain-gauge records instead of gridded observations",
    "baseline": "the historical reference uses the years {v}",
}


class InvalidGoal(SkillError):
    """The goal JSON is malformed or contradicts a hard rule. Exit 1: fix it; do not ask."""

    exit_code = 1


class NeedsHuman(SkillError):
    """A required slot is missing or independent samples disagree. Exit 3: ask the human."""

    exit_code = 3


# ------------------------------------------------------------------------------------------------
def _canon_region(v):
    if v is None or (isinstance(v, str) and not v.strip()):
        return None
    if not isinstance(v, str):
        return v
    for r in REGIONS:
        if v.strip().casefold() == r.casefold():
            return r
    return v.strip()


def _empty(v) -> bool:
    return v is None or (isinstance(v, str) and not v.strip())


def normalise(raw: dict) -> dict:
    """Map a compiled object (rhiza-goal/1, or clm's relative_time form) onto the flat schema."""
    g = {k: raw.get(k) for k in CORE + EXTENSION}
    if "time_window" not in raw and "relative_time" in raw:  # clm truth / v1 form
        g["time_window"] = "relative" if raw.get("relative_time") else None
    for k in ("task", "variable", "time_window", "period", "obs_source"):
        if isinstance(g[k], str):
            g[k] = g[k].strip() or None
    g["region"] = _canon_region(g["region"])
    for k in ("window_phrase", "window_start", "window_end", "baseline", "season", "decision"):
        if _empty(g[k]):
            g[k] = None
    g["legacy_cumulative"] = bool(g["legacy_cumulative"]) if g["legacy_cumulative"] else False
    if g["obs_source"] is None:
        g["obs_source"] = "grid"
    return g


def rule_settles(slot: str, request: str | None, goal: dict | None = None):
    """Value a deterministic rule assigns to `slot` for this request, or None if no rule decides.

    Used here and by decision-packet's premature-ask guard, so both read one rule set.
    """
    if request is None:
        return None
    if slot == "obs_source" and not _STATION.search(request):
        return "grid"
    if slot in ("time_window", "relative_time") and not _RELATIVE.search(request):
        return "not_relative"
    if slot == "output" and goal and goal.get("task") in READ_OUTPUT:
        return goal["task"]
    return None


def apply_rules(g: dict, request: str | None, log: list) -> dict:
    """v3rr: obs_source 'station' needs a station cue; a relative window needs a relative cue."""
    if request is None:
        return g
    if g["obs_source"] == "station" and not _STATION.search(request):
        log.append(_rule("obs_source", "station", "grid", "v3rr_station"))
        g["obs_source"] = "grid"
    if g["time_window"] == "relative" and not _RELATIVE.search(request):
        log.append(_rule("time_window", "relative", None, "v3rr_relative"))
        g["time_window"] = None
    return g


def _rule(slot, frm, to, key):
    return {"slot": slot, "from": frm, "to": to, "source": SRC[key][0], "plain": SRC[key][1]}


def _default(slot, value, key, plain_value):
    return {
        "slot": slot,
        "value": value,
        "source": SRC[key][0],
        "why": SRC[key][1],
        "plain_value": plain_value,
    }


def check_goal(raw, request: str | None) -> dict:
    """One goal: hard errors, missing required slots, rule rewrites and sourced defaults."""
    errors, missing, rules, defaults, notes = [], [], [], [], []
    if not isinstance(raw, dict):
        return {"goal": None, "errors": ["goal is not a JSON object"], "missing": [], "rules": []}
    g = normalise(raw)
    if g["task"] is None:
        missing.append({"slot": "task", "reason": "the request does not say what to produce"})
    elif g["task"] not in TASKS:
        errors.append(f"task {g['task']!r} is not one of {sorted(TASKS)}")
    if g["variable"] is None:
        missing.append({"slot": "variable", "reason": "the request does not name a variable"})
    elif g["variable"] not in VARIABLES:
        errors.append(f"variable {g['variable']!r} is not one of {sorted(VARIABLES)}")
    if g["period"] is not None and g["period"] not in PERIODS:
        errors.append(f"period {g['period']!r} is not one of {list(PERIODS)} or null")
    if g["time_window"] is not None and g["time_window"] not in WINDOWS:
        errors.append(f"time_window {g['time_window']!r} is not one of {list(WINDOWS)} or null")
    if g["obs_source"] not in ("grid", "station"):
        errors.append(f"obs_source {g['obs_source']!r} is not 'grid' or 'station'")
    if not isinstance(raw.get("legacy_cumulative", False), (bool, type(None))):
        errors.append("legacy_cumulative must be true, false or null")
    for k in ("window_start", "window_end"):
        if g[k] is not None and not re.fullmatch(r"\d{4}-\d{2}-\d{2}", str(g[k])):
            errors.append(f"{k} {g[k]!r} is not YYYY-MM-DD")
    if g["baseline"] is not None:
        m = re.fullmatch(r"(\d{4})\s*[-/to ]+\s*(\d{4})", str(g["baseline"]))
        if not m or int(m.group(1)) >= int(m.group(2)):
            errors.append(f"baseline {g['baseline']!r} is not a 'YYYY-YYYY' span")
        else:
            g["baseline"] = f"{m.group(1)}-{m.group(2)}"
    if errors:
        return {"goal": g, "errors": errors, "missing": missing, "rules": []}

    g = apply_rules(g, request, rules)
    t, v = g["task"], g["variable"]

    # Hard contract rules (clm_ws/tasks.py generate_goals): contradictions are INVALID, not asks.
    if t == "spread_map" and g["period"] is not None:
        errors.append(
            "a spread map has no time resolution (contract: spread_map never has a period)"
        )
    if t == "change_map" and g["time_window"] == "relative":
        errors.append(
            "a climate-change map uses fixed scenario years, never a relative window "
            "(contract: change_map is never relative)"
        )
    if g["legacy_cumulative"] and v is not None and v != "precip":
        errors.append("an accumulated-since-start archive is rainfall only (contract)")
    if g["window_start"] and g["window_end"] and g["window_start"] > g["window_end"]:
        errors.append("window_start is after window_end")
    if g["time_window"] == "relative" and (g["window_start"] or g["window_end"]):
        rules.append(
            {
                "slot": "window_start/window_end",
                "from": [g["window_start"], g["window_end"]],
                "to": None,
                "source": "rule: relative windows are dated at run time against today (UTC), "
                "never by the compiler",
                "plain": "dates for 'last/next ...' are worked out when the work starts",
            }
        )
        g["window_start"] = g["window_end"] = None
    if errors:
        return {"goal": g, "errors": errors, "missing": missing, "rules": rules}

    # Required-but-missing: rainfall figures must be period totals (clm soft rule, tasks.py).
    if v == "precip" and t is not None and t != "spread_map" and g["period"] is None:
        missing.append(
            {
                "slot": "period",
                "reason": "rainfall is shown as totals over a period, and the request does not "
                "say weekly or monthly",
            }
        )

    # Normalisations that are not choices.
    if t is not None and t not in OBS_TASKS and g["obs_source"] == "station":
        notes.append("obs_source ignored: only a forecast-vs-observation comparison chooses it")
    if t is not None and t not in OBS_TASKS:
        # Not a free choice: station_vs_sat implies stations, the others use no observations of
        # that kind. clm labels them all "grid" (D1), so they compare equal across samples.
        g["obs_source"] = "grid"
    if t != "change_map" and g["baseline"] is not None:
        notes.append("baseline ignored: only a climate-change map uses a reference period")
        g["baseline"] = None

    # Sourced defaults for unstated, rule-decidable slots.
    if t is not None:
        g["output"] = t
        defaults.append(_default("output", t, "contract", READ_OUTPUT.get(t, t)))
    if g["region"] is None:
        g["admin_level"] = None
        defaults.append(
            _default("region", None, "no_place", "the whole area each data source covers")
        )
    elif g["region"] in COUNTRY_ISO3:
        g["admin_level"] = "country"
    else:
        g["admin_level"] = "place_lookup"
        notes.append(
            f"'{g['region']}' is not a country: its boundary comes from a place-name lookup; "
            "say so in the read-back so the human can correct the area"
        )
    if g["time_window"] is None and t != "change_map":
        defaults.append(_default("time_window", None, "latest", "the most recent data available"))
    if g["period"] is None and v is not None and t is not None:
        plain = "no time averaging" if t == "spread_map" else "the data's own time steps"
        defaults.append(_default("period", None, "no_resolution", plain))
    if t in OBS_TASKS and g["obs_source"] == "grid":
        defaults.append(
            _default(
                "obs_source", "grid", "v3rr_station", "gridded (satellite or model) observations"
            )
        )
    if t == "change_map" and g["baseline"] is None:
        g["baseline"] = "1991-2020"
        defaults.append(_default("baseline", "1991-2020", "wmo", "1991-2020"))
    return {
        "goal": g,
        "errors": [],
        "missing": missing,
        "rules": rules,
        "defaults": defaults,
        "notes": notes,
    }


def _sig(g: dict | None):
    if g is None:
        return None
    return tuple((k, g.get(k)) for k in COMPARED)


def compare(results: list[dict]) -> list[dict]:
    """Slot-level disagreement across the VALID independently compiled goals (after the rules).

    Invalid samples are reported separately (evaluate: `invalid_samples`): an unparsable reading
    carries no evidence about any slot, so it is redrawn, not turned into a fake option.
    """
    goals = [r["goal"] for r in results if not r["errors"]]
    out = []
    for k in COMPARED:
        c = Counter(json.dumps(g.get(k)) for g in goals)
        if len(c) > 1:
            out.append(
                {
                    "slot": k,
                    "values": {kk: n for kk, n in c.most_common()},
                    "n_samples": len(results),
                }
            )
    return out


# --- Plain-language read-back (no skill, tool or file names) ------------------------------------
def readback(g: dict, defaults: list[dict], notes: list[str]) -> dict:
    t, v = g["task"], g["variable"]
    var = VARIABLES.get(v, v)
    what = {
        "map": f"A map of {var}.",
        "spread_map": f"A map of how much the forecast's ensemble members disagree about {var}.",
        "fcst_vs_obs": f"The {var} forecast shown side by side with what was observed.",
        "timeseries": f"The {var} forecast and the observations as lines over time.",
        "bias_map": f"A map of where the {var} forecast was too high or too low "
        "(forecast minus observed).",
        "station_vs_sat": f"A check of gridded satellite {var} against weather-station records.",
        "change_map": f"A map of projected change in {var}: future minus the historical "
        "reference period.",
    }.get(t, f"(not yet decided) for {var}")
    where = g["region"] or "No place named: the whole area each data source covers."
    if g["admin_level"] == "country":
        where = f"{g['region']} (the whole country, national boundary)."
    elif g["admin_level"] == "place_lookup":
        where = f"{g['region']} (boundary from a place-name lookup; correct it if you mean a set "
        where += "of counties or districts)."
    if g["time_window"] == "relative":
        phrase = g["window_phrase"] or "a period relative to today"
        when = f'"{phrase}", worked out as calendar dates from today (UTC) when the work starts.'
    elif g["time_window"] == "fixed":
        span = (
            f"{g['window_start']} to {g['window_end']}"
            if g["window_start"] and g["window_end"]
            else (g["window_phrase"] or "the dates you gave")
        )
        when = f"{span} (fixed dates)."
    elif t == "change_map":
        when = "The projection's future period compared with the historical reference period."
    else:
        when = "The most recent data available (you did not give dates)."
    if g["season"]:
        when += f" Season: {g['season']}."
    if g["period"]:
        res = f"{g['period']} totals" if v == "precip" else f"{g['period']} averages"
    elif t == "spread_map":
        res = "No time averaging."
    else:
        res = "The data's own time steps (no weekly or monthly averaging)."
    rb = {
        "what": what,
        "variable": var,
        "where": where,
        "when": when,
        "time_resolution": res,
        "output": READ_OUTPUT.get(t, "to be decided"),
    }
    if t in OBS_TASKS:
        rb["observations"] = (
            "Weather-station and rain-gauge records."
            if g["obs_source"] == "station"
            else "Gridded observations (satellite or model analysis), not station records."
        )
    if t == "station_vs_sat":
        rb["observations"] = "Weather-station records, compared with the gridded satellite product."
    if t == "change_map":
        rb["baseline"] = f"Historical reference period {g['baseline']}."
    if g["legacy_cumulative"]:
        rb["input"] = (
            "Your own forecast archive, where rainfall is accumulated since each forecast "
            "started; it is converted to per-period amounts first."
        )
    if g["decision"]:
        rb["decision_it_feeds"] = g["decision"]
    rb["defaults_used"] = [
        f"{d['slot']}: {d['plain_value']} (because {d['why']})" for d in defaults
    ]
    rb["caveats"] = [n for n in notes if "place-name lookup" in n]
    return rb


def plan_hints(g: dict) -> list[str]:
    h = [PLAN_HINTS["task"][g["task"]]] if g.get("task") in TASKS else []
    if g.get("region"):
        h.append(PLAN_HINTS["region"])
    if g.get("time_window") == "relative":
        h.append(PLAN_HINTS["time_window.relative"])
    if g.get("period"):
        h.append(PLAN_HINTS["period"].format(v=g["period"]))
    if g.get("legacy_cumulative"):
        h.append(PLAN_HINTS["legacy_cumulative"])
    if g.get("task") in OBS_TASKS and g.get("obs_source") == "station":
        h.append(PLAN_HINTS["obs_source.station"])
    if g.get("task") == "change_map" and g.get("baseline"):
        h.append(PLAN_HINTS["baseline"].format(v=g["baseline"]))
    return h


# --- Question packets (handed to decision-packet for the completeness check) -------------------
QUESTION = {
    "task": "Which of these do you want to end up with?",
    "variable": "Which quantity should this be about?",
    "period": "Should the values be grouped week by week or month by month?",
    "region": "Which area should this cover?",
    "time_window": "Is the period you mean counted from today, or fixed calendar dates?",
    "obs_source": "Should the comparison use weather-station records or gridded observations?",
    "legacy_cumulative": "Is the forecast data your own archive, with rainfall accumulated "
    "since each forecast started?",
    "baseline": "Which historical years should the change be measured against?",
    "season": "Which season or months matter for your decision?",
}


def _label(slot, val):
    if slot == "task":
        return READ_OUTPUT.get(val, str(val))
    if slot == "variable":
        return VARIABLES.get(val, str(val))
    if slot == "period":
        return {"weekly": "Week by week", "monthly": "Month by month"}.get(val, str(val))
    if slot == "time_window":
        return {
            "relative": "Counted from today",
            "fixed": "Fixed calendar dates",
            None: "Not stated",
        }[val]
    if slot == "obs_source":
        return {"station": "Weather-station records", "grid": "Gridded observations"}[val]
    if slot == "legacy_cumulative":
        return {True: "Yes, my own accumulated archive", False: "No, a standard forecast"}[val]
    return "Not stated" if val is None else str(val)


def _definition(slot, val):
    if slot == "task":
        return f"You get {READ_OUTPUT.get(val, val)}: {TASKS.get(val, val)}."
    if slot == "variable":
        return f"The analysis is about {VARIABLES.get(val, val)}."
    if slot == "period":
        return (
            f"Values are combined into {val} totals (rainfall) or {val} averages (other "
            "quantities) before they are shown."
        )
    if slot == "time_window":
        return {
            "relative": "Phrases like 'the last two weeks' are turned into dates from today.",
            "fixed": "The calendar dates or years you give are used as they are.",
            None: "No window: the most recent data available is used.",
        }[val]
    if slot == "obs_source":
        return {
            "station": "Point measurements from weather stations and rain gauges.",
            "grid": "Gridded satellite or model-analysis observations covering every location.",
        }[val]
    if slot == "legacy_cumulative":
        return {
            True: "You supply the archive; it is converted from accumulated to per-period amounts.",
            False: "A standard published forecast is fetched; no conversion is needed.",
        }[val]
    if slot == "region":
        return "No place: the whole area each source covers." if val is None else f"Cut to {val}."
    if slot == "baseline":
        return f"Change is measured against the average of {val}." if val else "Not stated."
    return f"{slot} = {val}"


def _downstream(slot, val):
    if slot == "task":
        return "The whole pipeline differs: " + PLAN_HINTS["task"].get(val, str(val)) + "."
    if slot == "period":
        return f"Data are combined into {val} values before plotting; one panel or point per {val[:-2]}."
    if slot == "region":
        return (
            "Every dataset is cut to this area." if val else "No cutting: the full domain is shown."
        )
    if slot == "time_window":
        return {
            "relative": "Dates are computed from today when the work starts.",
            "fixed": "Your dates are used directly.",
            None: "The newest available data is fetched.",
        }[val]
    if slot == "obs_source":
        return {
            "station": "Station records are fetched, and the forecast is sampled at the stations.",
            "grid": "A gridded observation product is fetched and compared cell by cell.",
        }[val]
    if slot == "legacy_cumulative":
        return {
            True: "An extra conversion step runs on your archive before anything else.",
            False: "No conversion step; a forecast is downloaded.",
        }[val]
    return f"The analysis uses {slot} = {val}."


def packets(disagree, missing, request, goal, n_samples) -> tuple[list[dict], list[str]]:
    """Packet skeletons for at most MAX_QUESTIONS slots, in QUESTION_ORDER; the rest are queued."""
    todo = {}
    for d in disagree:
        todo[d["slot"]] = ("disagree", [json.loads(k) for k in d["values"]], d["values"])
    for m in missing:
        if m["slot"] in todo:
            continue
        vocab = {
            "task": list(TASKS),
            "variable": list(VARIABLES),
            "period": list(PERIODS),
        }.get(m["slot"], [])
        todo[m["slot"]] = ("missing", vocab, m["reason"])
    order = [s for s in QUESTION_ORDER if s in todo] + [s for s in todo if s not in QUESTION_ORDER]
    out = []
    for slot in order[:MAX_QUESTIONS]:
        kind, vals, info = todo[slot]
        opts = []
        for val in vals[:8]:
            if kind == "disagree":
                n = info.get(json.dumps(val), 0)
                ev = [
                    {
                        "text": f"{n} of {n_samples} independent readings of your request "
                        "chose this.",
                        "source": "independent readings of the request",
                    }
                ]
            else:
                ev = [
                    {
                        "text": f"Supported; your request does not choose it ({info}).",
                        "source": "the request format this assistant uses",
                    }
                ]
            if request:
                ev.append({"text": f'Your words: "{request}"', "source": "your request"})
            opts.append(
                {
                    "code": json.dumps(val) if not isinstance(val, str) else val,
                    "label": _label(slot, val),
                    "definition": _definition(slot, val),
                    "evidence": ev,
                    "downstream": _downstream(slot, val),
                }
            )
        checks = [
            {
                "check": "deterministic request rules and defaults",
                "settled": False,
                "result": f"no rule or default decides {slot} for this request",
            }
        ]
        if kind == "disagree":
            checks.append(
                {
                    "check": f"{n_samples} independent readings of the request",
                    "settled": False,
                    "result": "the readings disagree: "
                    + ", ".join(f"{k} x{v}" for k, v in info.items()),
                }
            )
        out.append(
            {
                "schema": "rhiza-decision-packet/1",
                "id": f"goal-{slot}",
                "kind": "goal_slot",
                "slot": slot,
                "request": request,
                # The disputed slot is unset in the packet's goal, so the reply's echo reads
                # "unset -> X" rather than pretending a majority reading was the standing answer.
                "goal": dict(goal) | {slot: None},
                "question": QUESTION.get(slot, f"What should {slot} be?"),
                "options": opts,
                "default": {
                    "option": None,
                    "if_no_reply": "Nothing runs until you answer; the request stays on hold.",
                    "source": "ask policy: a disputed or missing required detail is never guessed",
                },
                "checks_run": checks,
            }
        )
    return out, order[MAX_QUESTIONS:]


def evaluate(goals: list, request: str | None) -> dict:
    results = [check_goal(g, request) for g in goals]
    disagree = compare(results) if len(results) > 1 else []
    valid = [r for r in results if not r["errors"]]
    if not valid:
        return {
            "schema": SCHEMA,
            "status": "invalid",
            "exit_code": 1,
            "errors": sorted({e for r in results for e in r["errors"]}),
            "samples": len(results),
        }
    invalid = [i for i, r in enumerate(results) if r["errors"]]
    head = valid[0]
    g = head["goal"]
    missing = [m for r in valid for m in r["missing"]]
    missing = list({m["slot"]: m for m in missing}.values())
    # clm F1 counts an unparsable sample as an ask; here it asks for a REDRAW first (exit 3 with
    # resample=true and no question), because a failed reading says nothing about the request.
    needs = bool(disagree or missing or invalid)
    qs, queued = packets(disagree, missing, request, g, len(results)) if needs else ([], [])
    rep = {
        "schema": SCHEMA,
        "status": "needs_human" if needs else "resolved",
        "exit_code": 3 if needs else 0,
        "samples": len(results),
        "goal": g,
        "rules_applied": head["rules"],
        "defaults": head.get("defaults", []),
        "notes": head.get("notes", []),
        "unresolved": missing,
        "disagreements": disagree,
        "invalid_samples": [{"index": i, "errors": results[i]["errors"]} for i in invalid],
        "resample": bool(invalid),
        "questions": qs,
        "queued_questions": queued,
        "readback": readback(g, head.get("defaults", []), head.get("notes", [])),
        "plan_hints": plan_hints(g),
    }
    if request is None:
        rep["notes"] = rep["notes"] + ["no --request given: rules v3rr were NOT applied"]
    return rep


def _human(rep: dict) -> str:
    L = [f"status: {rep['status']} (exit {rep['exit_code']})"]
    for e in rep.get("errors", []):
        L.append(f"  error: {e}")
    if "goal" in rep:
        L.append("goal: " + json.dumps({k: rep["goal"][k] for k in CORE}, sort_keys=False))
        for k, v in rep["readback"].items():
            if isinstance(v, list):
                for x in v:
                    L.append(f"  {k}: {x}")
            else:
                L.append(f"  {k}: {v}")
        for d in rep["disagreements"]:
            L.append(f"  DISAGREE {d['slot']}: {d['values']}")
        for m in rep["unresolved"]:
            L.append(f"  MISSING {m['slot']}: {m['reason']}")
        for q in rep["questions"]:
            L.append(f"  QUESTION {q['question']} [{' / '.join(o['label'] for o in q['options'])}]")
    return "\n".join(L)


@weather_skill(name="goal-check", version=_SKILL_VERSION, output=False)
@weather_skill.argument(
    "--goal",
    action="append",
    required=True,
    metavar="PATH",
    help="Goal JSON file. Repeat 2-3 times with independently compiled samples to check "
    "slot-level disagreement.",
)
@weather_skill.argument("--request", default=None, help="The user's request text (enables v3rr).")
@weather_skill.argument(
    "--request-file", default=None, metavar="PATH", help="Read the request text from a file."
)
@weather_skill.argument(
    "--format", choices=["human", "json"], default="json", help="Report format on stdout."
)
def goal_check(goal, request, request_file, format, **kwargs):
    """Validate a typed goal JSON, fill rule-decidable defaults, and flag what only a human can settle."""
    if len(goal) > 3:
        raise UsageError("pass at most 3 --goal samples")
    if request is not None and request_file is not None:
        raise UsageError("pass --request or --request-file, not both")
    if request_file is not None:
        try:
            request = Path(request_file).read_text(encoding="utf-8").strip()
        except OSError as exc:
            raise UsageError(f"cannot read --request-file: {exc}") from None
    parsed = []
    for p in goal:
        try:
            text = Path(p).read_text(encoding="utf-8")
        except OSError as exc:
            raise UsageError(f"cannot read --goal {p}: {exc}") from None
        try:
            parsed.append(json.loads(text))
        except json.JSONDecodeError:
            # A sample that is not JSON is an unparsable compile: one invalid sample (clm F1
            # counts it as asked), never a usage error that would hide the other samples.
            m = re.search(r"\{.*\}", text, re.S)
            try:
                parsed.append(json.loads(m.group(0)) if m else None)
            except json.JSONDecodeError:
                parsed.append(None)
    rep = evaluate(parsed, request)
    print(json.dumps(rep, indent=1, ensure_ascii=False) if format == "json" else _human(rep))
    if rep["exit_code"] == 1:
        raise InvalidGoal("goal is invalid: " + "; ".join(rep["errors"]), prefix=False)
    if rep["exit_code"] == 3:
        slots = [d["slot"] for d in rep["disagreements"]] + [m["slot"] for m in rep["unresolved"]]
        raise NeedsHuman(
            "needs a human answer on: " + ", ".join(dict.fromkeys(slots)), prefix=False
        )


if __name__ == "__main__":
    goal_check()
