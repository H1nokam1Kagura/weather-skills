"""Registry schema, content hash, and compilation against the real indicator grammar."""

from __future__ import annotations

import copy
import importlib.util
import sys
from pathlib import Path

import pytest

from registry import onset

REPO = Path(__file__).resolve().parents[2]
INDICATOR_SPEC = REPO / "skills" / "indicator" / "spec.py"


@pytest.fixture(scope="module")
def defs() -> dict[str, dict]:
    return onset.load()


@pytest.fixture(scope="module")
def indicator_spec():
    """The indicator skill's rule parser, loaded by path (skills are not packages)."""
    name = "_registry_test_indicator_spec"
    spec = importlib.util.spec_from_file_location(name, INDICATOR_SPEC)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module  # dataclasses resolve their module through sys.modules
    try:
        spec.loader.exec_module(module)
        yield module
    finally:
        sys.modules.pop(name, None)


def _validate(name: str, d: dict, defs: dict) -> None:
    all_defs = {**defs, name: d}
    onset.validate(name, d, all_defs)


# ---------------------------------------------------------------- schema: positive


def test_registry_loads_and_every_entry_validates(defs):
    assert {"icpac-onset", "agrhymet-sos", "moron-robertson-2014"} <= set(defs)
    for name, d in defs.items():
        onset.validate(name, d, defs)


def test_variants_name_a_registered_parent(defs):
    for d in defs.values():
        if d["status"] != "canonical":
            assert d["derived_from"] in defs


# ---------------------------------------------------------------- schema: negative


def test_bad_status_rejected(defs):
    d = copy.deepcopy(defs["icpac-onset"])
    d["status"] = "official"
    with pytest.raises(onset.RegistryError, match="status"):
        _validate("x", d, defs)


def test_missing_source_rejected(defs):
    d = copy.deepcopy(defs["icpac-onset"])
    del d["source"]
    with pytest.raises(onset.RegistryError, match="source"):
        _validate("x", d, defs)


def test_unspecified_in_source_naming_missing_field_rejected(defs):
    d = copy.deepcopy(defs["icpac-onset"])
    d["unspecified_in_source"] = ["trigger.no_such_field"]
    with pytest.raises(onset.RegistryError, match="unspecified_in_source"):
        _validate("x", d, defs)


def test_variant_without_registered_parent_rejected(defs):
    d = copy.deepcopy(defs["icpac-onset-north"])
    d["derived_from"] = "nope"
    with pytest.raises(onset.RegistryError, match="derived_from"):
        _validate("x", d, defs)


# ---------------------------------------------------------------- content hash


def test_content_hash_is_stable_under_prose_edits(defs):
    d = copy.deepcopy(defs["icpac-onset"])
    before = onset.content_hash(d)
    d["name"] = "renamed"
    d["notes"] = "reworded"
    d["source"]["checked"] = "2099-01-01"
    d["unspecified_in_source"] = []
    assert onset.content_hash(d) == before


def test_content_hash_is_sensitive_to_parameter_edits(defs):
    base = defs["icpac-onset"]
    before = onset.content_hash(base)
    for dotted, value in [
        ("trigger.total_mm", 25.0),
        ("trigger.total_op", ">"),
        ("veto.follow_days", 30),
        ("search.window_days", 45),
        ("time_basis", "calendar_dekad"),
    ]:
        d = copy.deepcopy(base)
        *path, leaf = dotted.split(".")
        node = d
        for part in path:
            node = node[part]
        node[leaf] = value
        assert onset.content_hash(d) != before, dotted


def test_content_hash_recipe_is_the_documented_one(defs):
    # The contract skills reproduce without importing registry/. Keep this literal.
    import hashlib
    import json

    d = defs["agrhymet-sos"]
    keep = {k: d[k] for k in ("time_basis", "trigger", "confirm", "veto", "search") if k in d}
    expected = hashlib.sha256(json.dumps(keep, sort_keys=True).encode()).hexdigest()[:12]
    assert onset.content_hash(d) == expected
    assert len(expected) == 12


def test_content_hashes_are_distinct(defs):
    hashes = [onset.content_hash(d) for d in defs.values()]
    assert len(set(hashes)) == len(hashes)


# ---------------------------------------------------------------- compilation


def _compile(name, d):
    per_cell = d["trigger"].get("threshold_kind") == "per_cell_climatology"
    return onset.compile_to_indicator(name, d, scalar_threshold=10.0 if per_cell else None)


def test_every_definition_compiles_and_parses_with_indicator_grammar(defs, indicator_spec):
    for name, d in defs.items():
        compiled = _compile(name, d)
        parsed = indicator_spec.parse_rule(compiled.rule)
        assert parsed.clauses, name
        assert compiled.exact == (not compiled.dropped)


def test_per_cell_definition_requires_scalar_threshold(defs):
    with pytest.raises(onset.RegistryError, match="per-cell"):
        onset.compile_to_indicator("moron-robertson-2014", defs["moron-robertson-2014"])


def test_calendar_dekad_is_reported_as_dropped(defs):
    compiled = _compile("agrhymet-sos", defs["agrhymet-sos"])
    assert not compiled.exact
    assert any("dekad" in reason for reason in compiled.dropped)


# ---------------------------------------------------------------- drift vs indicator ALIASES


def test_icpac_alias_matches_registry(defs, indicator_spec):
    compiled = onset.compile_to_indicator("icpac-onset", defs["icpac-onset"])
    assert compiled.rule == indicator_spec.ALIASES["icpac-onset"]


@pytest.mark.xfail(
    strict=True,
    reason=(
        "Known drift: the source says 'at least' 25 mm / 20 mm (>=); indicator's chc-onset "
        "alias uses strict '>'. Remove this xfail when the alias is regenerated from the registry."
    ),
)
def test_chc_alias_matches_agrhymet_rolling(defs, indicator_spec):
    compiled = onset.compile_to_indicator("agrhymet-sos-rolling", defs["agrhymet-sos-rolling"])
    assert compiled.rule == indicator_spec.ALIASES["chc-onset"]
