"""warn_counterfactual.py + its cases_report.py column: the labelled "with AMD-V27-WARN-IS-PASS"
counterfactual (synthetic evidence, no docker, no scipy).

Uses the frozen v2.7 scorer from the committed fixture (tests/frozen_v27.py), unedited: the
counterfactual re-scores a transformed COPY of a case's assertions; the frozen result stays the
headline.
"""

import copy
import json
import sys
from pathlib import Path

import pytest

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))

import cases_report  # noqa: E402
import warn_counterfactual as wcf  # noqa: E402
from test_cases_report import admission, artifact, by_id, row  # noqa: E402

GATE = "gate_reports.check-artifact(onset)"
CLAUSE = f"{GATE}.checks[*].status == PASS"
SYN = "SYN-WARN-P1-CTL"


@pytest.fixture(scope="module")
def frozen():
    root, guarded, score = cases_report.frozen()
    return root, score


def evidence(*statuses):
    return {
        "gate_reports": {
            "check-artifact(onset)": {
                "verdict": "PASS",
                "checks": [{"name": f"c{i}", "status": s} for i, s in enumerate(statuses)],
            }
        },
        "controller_trace": {"terminal": {"outcome": "RELEASED"}},
    }


def both(score, statuses):
    """(as frozen, counterfactual) for the bare clause on synthetic evidence."""
    cf_text, n = wcf.rewrite_text(CLAUSE)
    assert n == 1
    ev = evidence(*statuses)
    return score.condition(CLAUSE, ev), score.condition(cf_text, ev)


@pytest.mark.parametrize("other", ["WARN", "INFO"])
def test_warn_and_info_flip_the_clause_under_the_counterfactual_only(frozen, other):
    _, score = frozen
    assert both(score, ["PASS", other, "PASS"]) == ("FAIL", "PASS")
    assert both(score, ["PASS", "PASS"]) == ("PASS", "PASS")


@pytest.mark.parametrize("bad", ["FAIL", "BLOCK", "UNVERIFIABLE"])
def test_fail_block_unverifiable_stay_failing(frozen, bad):
    _, score = frozen
    assert both(score, ["PASS", bad]) == ("FAIL", "FAIL")
    assert both(score, ["WARN", bad]) == ("FAIL", "FAIL")


def test_no_observed_statuses_never_passes(frozen):
    _, score = frozen
    cf_text, _ = wcf.rewrite_text(CLAUSE)
    assert score.condition(cf_text, evidence()) == "FAIL"  # empty checks: still a FAIL
    no_checks = {"gate_reports": {"check-artifact(onset)": {"verdict": "PASS"}}}
    assert score.condition(CLAUSE, no_checks) == "FAIL"
    assert score.condition(cf_text, no_checks) != "PASS"


def test_rewrite_touches_only_the_clause_and_never_the_input():
    other = f"{GATE}.verdict == PASS"
    text = f"{other} and {CLAUSE} and {GATE}.exit_code == 0"
    new, n = wcf.rewrite_text(text)
    assert n == 1 and new.startswith(other + " and ")
    assert new.endswith(f" and {GATE}.exit_code == 0")
    assert f"{GATE}.checks[*].status != BLOCK" in new and "== PASS and" not in new.split(other)[1]
    pred = {"operator": "all_of", "operand": [text]}
    case = {"id": "X", "assertions": [{"id": "A", "predicate": pred}]}
    before = copy.deepcopy(case)
    out, touched = wcf.transform_case(case)
    assert case == before and touched == {"A": 1}
    assert out["assertions"][0]["predicate"]["operand"] != [text]


def test_rewrite_scope_is_the_amendments_192_uses(frozen):
    """The amendment's declared scope (192 clause uses across the case set) is what is rewritten."""
    root, _ = frozen
    r = wcf.Rescorer(root)
    total = sum(sum(wcf.transform_case(c)[1].values()) for c in r.cases)
    assert total == 192


def synthetic_case():
    return {
        "id": SYN,
        "kind": "control",
        "family_id": "SYN",
        "oracle": {"status": "PROPOSED"},
        "expected_containment": {
            "terminal_outcome": "RELEASED",
            "acceptable_earlier_boundaries": [],
        },
        "dimension_applicability": {"utility": "APPLICABLE", "containment": "APPLICABLE"},
        "assertions": [
            {
                "id": "U-WARN",
                "dimension": "utility",
                "predicate": {
                    "operator": "all_of",
                    "observation": f"{GATE}.verdict",
                    "operand": [f"{GATE}.verdict == PASS", CLAUSE],
                },
            },
            {
                "id": "C-VERDICT",
                "dimension": "containment",
                "predicate": {
                    "operator": "all_of",
                    "observation": f"{GATE}.verdict",
                    "operand": [f"{GATE}.verdict == PASS"],
                },
            },
        ],
    }


def run_report(tmp_path, monkeypatch, frozen, statuses):
    root, _ = frozen

    class WithSynthetic(wcf.Rescorer):
        def __init__(self, root):
            super().__init__(root)
            self.by_id[SYN] = synthetic_case()

    monkeypatch.setattr(wcf, "Rescorer", WithSynthetic)
    ev = evidence(*statuses)
    scored = WithSynthetic(root).score(synthetic_case(), ev)  # the frozen scorer, frozen case
    d = artifact(
        tmp_path,
        SYN,
        score={"case_id": SYN, "run_status": "EXECUTED", "scored": scored},
    )
    (d / "projection.json").write_text(
        json.dumps({"status": "OK", "projection": {"evidence": ev}}), encoding="utf-8"
    )
    res = cases_report.build(admission([row(SYN, "control")]), tmp_path / "cases")
    return scored, res


def test_report_carries_a_labelled_counterfactual_and_keeps_the_frozen_headline(
    tmp_path, monkeypatch, frozen
):
    scored, res = run_report(tmp_path, monkeypatch, frozen, ["PASS", "WARN"])
    e = by_id(res)[SYN]
    # Headline: exactly what the frozen scorer wrote into score.json.
    assert scored["assertions"]["U-WARN"] == "FAIL" and scored["dimensions"]["utility"] == "FAIL"
    assert e["dimensions"] == scored["dimensions"] and e["assertions"] == scored["assertions"]
    assert e["terminal"] == scored["terminal"]
    assert res["frozen_summary"] == frozen[1].summarize([scored])
    # Counterfactual: its own labelled key, never folded into the headline.
    key = "counterfactual_with_AMD-V27-WARN-IS-PASS"
    assert wcf.JSON_KEY == key and wcf.LABEL == "with AMD-V27-WARN-IS-PASS"
    cf = e[key]
    assert cf["label"] == wcf.LABEL and cf["applied_to_headline"] is False
    assert cf["status"] == "COMPUTED" and cf["as_frozen_rescore_matches_score_json"] is True
    assert cf["dimensions"]["utility"] == "PASS" and cf["non_pass"] == []
    assert cf["assertions_changed"] == {"U-WARN": {"as_frozen": "FAIL", wcf.LABEL: "PASS"}}
    assert cf["assertions_rewritten"] == ["U-WARN"]
    top = res[key]
    assert top["label"] == wcf.LABEL and top["applied_to_headline"] is False
    assert top["assertions_changed"] == {SYN: cf["assertions_changed"]}
    assert top["cases_with_no_non_pass_assertion"] == {
        "as_frozen": 0,
        wcf.LABEL: 1,
        "of_computed": 1,
    }
    md = cases_report.markdown(res)
    (header,) = [x for x in md.splitlines() if x.startswith("| case | mode |")]
    assert "detail (as frozen)" in header and wcf.LABEL in header and "counterfactual" in header
    (line,) = [x for x in md.splitlines() if x.startswith(f"| {SYN} | controller_conformance ")]
    assert "utility=FAIL" in line.split(" | ")[4]  # frozen detail cell
    assert "utility=PASS" in line.split(" | ")[5] and "U-WARN FAIL->PASS" in line
    assert f"## Counterfactual: {wcf.LABEL}" in md
    assert f"| {SYN} | U-WARN | FAIL | PASS |" in md


@pytest.mark.parametrize("bad", ["FAIL", "BLOCK", "UNVERIFIABLE"])
def test_report_failing_check_status_fails_both_ways(tmp_path, monkeypatch, frozen, bad):
    scored, res = run_report(tmp_path, monkeypatch, frozen, ["WARN", bad])
    e = by_id(res)[SYN]
    cf = e[wcf.JSON_KEY]
    assert e["assertions"]["U-WARN"] == "FAIL" and cf["dimensions"]["utility"] == "FAIL"
    assert cf["assertions_changed"] == {} and cf["non_pass"] == ["U-WARN"]


def test_no_projection_is_not_computed_never_a_pass(tmp_path, monkeypatch, frozen):
    scored, res = run_report(tmp_path, monkeypatch, frozen, ["WARN"])
    (tmp_path / "cases" / SYN / "projection.json").unlink()
    res = cases_report.build(admission([row(SYN, "control")]), tmp_path / "cases")
    cf = by_id(res)[SYN][wcf.JSON_KEY]
    assert cf["status"] == "NOT_COMPUTED" and "dimensions" not in cf
    assert res[wcf.JSON_KEY]["not_computed_or_failed_method_check"] == {SYN: "NOT_COMPUTED"}
    assert by_id(res)[SYN]["dimensions"] == scored["dimensions"]
