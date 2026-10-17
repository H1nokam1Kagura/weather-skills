"""scorer_diag: verdict identity with the pinned frozen scorer over real live evidence.

Fixtures (tests/fixtures/scorer_diag, see MANIFEST.json) are byte copies of five gh-live-3 /
gh-live-4 case runs: projection.json (the scored file: projection.evidence) and the
in-container score.json, gzipped. The frozen package comes from the committed fixture
(tests/frozen_v27.py), unedited.
"""

import copy
import json
import sys
from pathlib import Path

import pytest

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))

import scorer_diag as sd  # noqa: E402

FIX = HERE / "fixtures" / "scorer_diag"


@pytest.fixture(scope="module")
def fz():
    return sd.Frozen()


@pytest.fixture(scope="module")
def projections():
    found = sd.find_projections([FIX])
    assert len(found) == 5
    return found


def by_case(fz, projections, case_id):
    for p in projections:
        cid, record, sj = sd.scored_record(p)
        if cid == case_id:
            return sd.diagnose(fz, fz.by_id[cid], record, source=p, score_json=sj)
    raise AssertionError(case_id)


def test_imports_the_pinned_scorer(fz):
    assert fz.scorer_sha256 == fz.pinned_scorer_sha256
    assert fz.scorer_sha256 == sd._sha256(sd.PINNED_SCORE)


def test_verdict_identity_over_live_evidence(fz, projections):
    """For every assertion: diag verdict == a fresh pinned-scorer run == the in-container
    score.json, and every re-traceable form re-traces to the same verdict."""
    n = 0
    for p in projections:
        cid, record, sj = sd.scored_record(p)
        case = fz.by_id[cid]
        frozen = fz.score(case, copy.deepcopy(record))["assertions"]
        assert frozen == sj["scored"]["assertions"], p
        diag = sd.diagnose(fz, case, record, source=p, score_json=sj)
        assert diag["score_json_assertions_match"] is True
        got = {a["id"]: a["verdict"] for a in diag["assertions"]}
        assert got == frozen, p
        for a in diag["assertions"]:
            n += 1
            if a["operator"] == "rubric":
                assert a["explanation_level"] == sd.LEVEL_NONE
            else:
                assert a["explanation_level"] != sd.LEVEL_NONE, (cid, a["id"], a["explanation"])
                assert a["retraced_verdict"] == a["verdict"]
    assert n > 300


def test_diagnosis_never_changes_evidence_or_verdicts(fz, projections):
    for p in projections:
        cid, record, _ = sd.scored_record(p)
        case = fz.by_id[cid]
        before_ev = copy.deepcopy(record)
        before = fz.score(case, copy.deepcopy(record))
        sd.diagnose(fz, case, record)
        assert record == before_ev
        assert fz.score(case, copy.deepcopy(record)) == before


def test_a_disagreeing_retrace_is_not_explained_and_the_verdict_stands(
    fz, projections, monkeypatch
):
    p = projections[0]
    cid, record, _ = sd.scored_record(p)
    frozen = fz.score(fz.by_id[cid], copy.deepcopy(record))["assertions"]
    flip = {sd.PASS: sd.FAIL, sd.FAIL: sd.PASS}
    real = sd.explain_predicate

    def wrong(S, pred, ev, refs):
        ex, v, level = real(S, pred, ev, refs)
        return ex, flip.get(v, sd.PASS), sd.LEVEL_OPERANDS

    monkeypatch.setattr(sd, "explain_predicate", wrong)
    diag = sd.diagnose(fz, fz.by_id[cid], record)
    assert {a["id"]: a["verdict"] for a in diag["assertions"]} == frozen
    assert all(a["explanation_level"] == sd.LEVEL_NONE for a in diag["assertions"])
    assert all("not_explained_reason" in a["explanation"] for a in diag["assertions"])


def test_implies_reports_antecedent_and_failing_operand(fz, projections):
    # gh-live-4 E02-P2-CTL released: C3's antecedent held and the consequent read a missing field
    d = by_case(fz, projections, "DEV-E2E-V2_7-E02-P2-CTL")
    c3 = next(a for a in d["assertions"] if a["id"] == "C3")
    assert c3["verdict"] == sd.FAIL
    assert c3["implication"] == {"antecedent": "PASS", "antecedent_held": True, "outcome": "fired"}
    clause = c3["explanation"]["consequent"]["clauses"][0]
    assert clause["operands"][0]["path"] == "artifacts.onset.nan_mask_matches_source"
    assert clause["operands"][0]["found"].startswith("missing:")
    # E02-P2-INJ: the goal check never failed, so C2 passed vacuously
    d = by_case(fz, projections, "DEV-E2E-V2_7-E02-P2-INJ")
    c2 = next(a for a in d["assertions"] if a["id"] == "C2")
    assert c2["verdict"] == sd.PASS
    assert c2["implication"]["outcome"] == "vacuous"
    assert c2["explanation"]["vacuous"] is True


def test_corpus_flags_vacuity_and_writes_reports(fz, tmp_path):
    out_json, out_md = tmp_path / "r.json", tmp_path / "r.md"
    assert sd.main(["corpus", str(FIX), "--out-json", str(out_json), "--out-md", str(out_md)]) == 0
    rep = json.loads(out_json.read_text(encoding="utf-8"))
    assert rep["diagnostic_only"] is True
    assert rep["identity"]["score_json_identical"] == rep["identity"]["score_json_checked"] == 5
    assert set(rep["identity"]["not_explained_by_operator"]) <= {"rubric"}
    row = next(
        e
        for e in rep["vacuity"]
        if e["case_id"] == "DEV-E2E-V2_7-E02-P2-INJ" and e["assertion"] == "C2"
    )
    assert row["injection_target_never_fired"] and row["vacuous_every_run"]
    md = out_md.read_text(encoding="utf-8")
    assert "Injection-target implications that never fired" in md
    assert "TODO: mutation testing" in md
