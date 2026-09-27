"""Load and validate the onset-definition registry; compile entries to indicator rules.

A definition is data. Consumers (indicator aliases, onset-date defaults, an agent choosing a
definition as a scientific parameter) read it from here instead of restating it. Each entry
has a content hash, so a routing log or output provenance can record exactly which version
was used.

compile_to_indicator() translates an entry into the indicator skill's --rule grammar and
reports, honestly, what the grammar cannot express (per-cell thresholds, search start dates,
calendar dekads, inclusive wet-day tests). A compiled rule is only `exact` when nothing was
dropped.
"""

from __future__ import annotations

import hashlib
import json
import tomllib
from dataclasses import dataclass, field
from pathlib import Path

REGISTRY_PATH = Path(__file__).resolve().parent / "onset_definitions.toml"
STATUSES = {"canonical", "variant", "candidate"}
OPS = {">", ">=", "<", "<="}
VETO_MODES = {"none", "consecutive_dry", "window_sum"}
TIME_BASES = {"rolling_daily", "calendar_dekad"}
OPTIMIZATION_FIELDS = {"objective", "data", "method", "validation", "date"}


class RegistryError(ValueError):
    pass


def load(path: Path = REGISTRY_PATH) -> dict[str, dict]:
    with path.open("rb") as f:
        doc = tomllib.load(f)
    defs = doc.get("definitions", {})
    for name, d in defs.items():
        validate(name, d, defs)
    return defs


def validate(name: str, d: dict, all_defs: dict) -> None:
    def need(cond, msg):
        if not cond:
            raise RegistryError(f"{name}: {msg}")

    need(d.get("status") in STATUSES, f"status must be one of {sorted(STATUSES)}")
    need(
        d.get("source", {}).get("citation") and d["source"].get("url"), "needs source citation+url"
    )
    need(d.get("time_basis") in TIME_BASES, f"time_basis must be one of {sorted(TIME_BASES)}")
    t = d.get("trigger", {})
    need(isinstance(t.get("window_days"), int) and t["window_days"] >= 1, "trigger.window_days")
    need(t.get("total_op") in OPS, "trigger.total_op")
    per_cell = t.get("threshold_kind") == "per_cell_climatology"
    need(per_cell or isinstance(t.get("total_mm"), float), "trigger.total_mm or per-cell threshold")
    if t.get("all_days_wet"):
        need(t.get("wet_day_op") in (">", ">="), "trigger.wet_day_op for all_days_wet")
    v = d.get("veto", {})
    need(v.get("mode") in VETO_MODES, f"veto.mode must be one of {sorted(VETO_MODES)}")
    if v["mode"] != "none":
        need(isinstance(v.get("follow_days"), int) and v["follow_days"] >= 1, "veto.follow_days")
    if d["status"] in ("variant", "candidate"):
        parent = d.get("derived_from")
        need(parent in all_defs, f"derived_from {parent!r} must name a registered definition")
        need(d.get("why"), "variant/candidate needs a 'why'")
    if d["status"] == "candidate":
        opt = d.get("optimization", {})
        need(
            OPTIMIZATION_FIELDS <= set(opt),
            f"candidate needs optimization.{sorted(OPTIMIZATION_FIELDS)}",
        )
    for key in d.get("unspecified_in_source", []):
        need(has_field(d, key), f"unspecified_in_source names missing field {key!r}")


_MISSING = object()


def get_field(d: dict, dotted: str, default=_MISSING):
    """Return d["a"]["b"] for dotted name "a.b"; raise KeyError unless a default is given."""
    node = d
    for part in dotted.split("."):
        if not isinstance(node, dict) or part not in node:
            if default is _MISSING:
                raise KeyError(dotted)
            return default
        node = node[part]
    return node


def has_field(d: dict, dotted: str) -> bool:
    absent = object()
    return get_field(d, dotted, absent) is not absent


def content_hash(d: dict) -> str:
    """Hash of the scientific content only (not prose), so wording edits keep the identity."""
    keep = {k: d[k] for k in ("time_basis", "trigger", "confirm", "veto", "search") if k in d}
    return hashlib.sha256(json.dumps(keep, sort_keys=True).encode()).hexdigest()[:12]


@dataclass
class Compiled:
    name: str
    rule: str
    exact: bool
    dropped: list[str] = field(default_factory=list)


def _num(x: float) -> str:
    return str(int(x)) if float(x).is_integer() else str(x)


def compile_to_indicator(
    name: str, d: dict, variable: str = "precip", scalar_threshold: float | None = None
) -> Compiled:
    """Translate one definition into the indicator --rule grammar."""
    dropped: list[str] = []
    t, v = d["trigger"], d["veto"]
    w = t["window_days"]
    clauses = []
    if t.get("all_days_wet"):
        # indicator counts a wet day as value > daily threshold (strict)
        clauses.append(f"{variable} count-above {_num(t['wet_day_mm'])} {w}d >= {w}")
        if t["wet_day_op"] == ">=":
            dropped.append(
                f"wet day is >= {_num(t['wet_day_mm'])} mm in the source; the grammar only has '>'"
            )
    if t.get("threshold_kind") == "per_cell_climatology":
        if scalar_threshold is None:
            raise RegistryError(
                f"{name}: per-cell climatological threshold is not expressible in the grammar; "
                "pass scalar_threshold to compile a single-threshold approximation"
            )
        clauses.append(f"{variable} sum {w}d {t['total_op']} {_num(scalar_threshold)}")
        dropped.append("per-cell climatological threshold replaced by one scalar")
    else:
        clauses.append(f"{variable} sum {w}d {t['total_op']} {_num(t['total_mm'])}")
    c = d.get("confirm")
    if c:
        clauses.append(
            f"{variable} sum {c['window_days']}d {c['total_op']} {_num(c['total_mm'])} "
            f"after {c['after_days']}d"
        )
    if v["mode"] == "consecutive_dry":
        clauses.append(
            f"not {variable} consecutive-below {_num(v['dry_day_mm'])} {v['dry_days']}d "
            f"within {v['follow_days']}d"
        )
    elif v["mode"] == "window_sum":
        clauses.append(
            f"not {variable} sum {v['window_days']}d < {_num(v['window_total_mm'])} "
            f"within {v['follow_days']}d"
        )
    if v["mode"] != "none" and v.get("follow_anchor", "").endswith("after_trigger_end"):
        dropped.append(
            "veto window counted from the trigger END; the grammar counts from its start"
        )
    if d.get("search", {}).get("start"):
        dropped.append(
            f"search start {d['search']['start']} (no start-date support in the grammar)"
        )
    if d.get("search", {}).get("window_days"):
        dropped.append(f"search window {d['search']['window_days']} days")
    if d["time_basis"] == "calendar_dekad":
        dropped.append("calendar dekads approximated by rolling daily windows")
    return Compiled(name=name, rule=" and ".join(clauses), exact=not dropped, dropped=dropped)
