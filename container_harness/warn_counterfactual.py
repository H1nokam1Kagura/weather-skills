"""Labelled counterfactual for cases_report.py: v2.7 re-scored "with AMD-V27-WARN-IS-PASS".

The headline result is ALWAYS the frozen v2.7 score (score.json, computed by the pinned frozen
scorer over the pinned frozen cases). This module never edits the frozen package or the frozen
scorer. It re-scores a transformed deep COPY of one case's assertions with the same frozen scorer
over the same in-container projection, and returns the result under its own label so it cannot be
confused with, or overwrite, the frozen result.

Owner decision: simkit/handoffs/completion-20261012/decisions/PROPOSED-AMENDMENT-WARN-IS-PASS.json
(status DEFERRED_TO_NEXT_CASESET_VERSION) and OWNER-RULINGS.json oracle_warn_is_pass_2026-10-14.

The transform: every assertion clause of the form ``<gate>.checks[*].status == PASS`` is evaluated
in the FAIL-exclusion form, where WARN and INFO pass and FAIL, BLOCK and UNVERIFIABLE fail. The
frozen grammar has no set-membership operator and a bare ``!= FAIL`` would let BLOCK and
UNVERIFIABLE through, so the clause becomes the conjunction

    <gate>.checks[*].status exists and <gate>.checks[*].status != FAIL
      and <gate>.checks[*].status != BLOCK and <gate>.checks[*].status != UNVERIFIABLE

(unquantified ``[*]`` is all() in the frozen scorer, as for the original clause). The leading
``exists`` keeps the original positive claim's missing-evidence behaviour: an observed absence of
check statuses still FAILs rather than reading as a vacuous negative; an absent ancestor stays
NOT_OBSERVED. Nothing is PASS without observed evidence under the counterfactual either.
"""

import copy
import json
import re
from pathlib import Path

HERE = Path(__file__).resolve().parent
AMENDMENT_ID = "AMD-V27-WARN-IS-PASS"
LABEL = f"with {AMENDMENT_ID}"
# JSON key for every counterfactual block; the label is also repeated inside the block.
JSON_KEY = "counterfactual_with_AMD-V27-WARN-IS-PASS"
COLUMN = f"{LABEL} (counterfactual, not applied)"
DECISION = (
    "PROPOSED-AMENDMENT-WARN-IS-PASS.json (DEFERRED_TO_NEXT_CASESET_VERSION); "
    "OWNER-RULINGS.json oracle_warn_is_pass_2026-10-14"
)
NOTE = (
    f"Counterfactual '{LABEL}': NOT applied. v2.7 is scored exactly as frozen and the frozen "
    "result is the headline. Here every clause '<gate>.checks[*].status == PASS' is evaluated as "
    "the FAIL-exclusion form (WARN and INFO pass; FAIL, BLOCK and UNVERIFIABLE fail), by re-scoring a "
    "transformed copy of the case's assertions with the same frozen scorer over the same "
    "projection."
)
FAILING_CHECK_STATUSES = ("FAIL", "BLOCK", "UNVERIFIABLE")
PROFILE_PATH = HERE / "pinned" / "profiles" / "cached-onset" / "profile.json"

_CLAUSE = re.compile(r"(?<!\S)(\S+\.checks\[\*\]\.status) == PASS(?= and |$)")


def _rewrite_clause(m):
    path = m.group(1)
    parts = [f"{path} exists"] + [f"{path} != {s}" for s in FAILING_CHECK_STATUSES]
    return " and ".join(parts)


def rewrite_text(text):
    """(new_text, n_clauses_rewritten) for one predicate string."""
    return _CLAUSE.subn(_rewrite_clause, text)


def _rewrite(node):
    """Rewrite every string inside a predicate tree; returns (new_node, n_rewritten)."""
    if isinstance(node, str):
        return rewrite_text(node)
    if isinstance(node, list):
        out, n = [], 0
        for v in node:
            v2, k = _rewrite(v)
            out.append(v2)
            n += k
        return out, n
    if isinstance(node, dict):
        out, n = {}, 0
        for key, v in node.items():
            v2, k = _rewrite(v)
            out[key] = v2
            n += k
        return out, n
    return node, 0


def transform_case(case):
    """A deep copy of ``case`` with the clause rewritten in every assertion predicate.

    Returns (copy, {assertion_id: n_clauses_rewritten}). The input case is never mutated."""
    out = copy.deepcopy(case)
    touched = {}
    for a in out.get("assertions") or []:
        a["predicate"], n = _rewrite(a["predicate"])
        if n:
            touched[a["id"]] = n
    return out, touched


def changed_assertions(frozen_scored, cf_scored):
    """{assertion_id: {as_frozen, counterfactual}} for every assertion whose verdict differs."""
    fa = (frozen_scored or {}).get("assertions") or {}
    ca = (cf_scored or {}).get("assertions") or {}
    return {
        k: {"as_frozen": fa.get(k), LABEL: ca.get(k)}
        for k in sorted(set(fa) | set(ca))
        if fa.get(k) != ca.get(k)
    }


def block(frozen_scored, cf_scored, touched, rescore_matches=None):
    """The labelled counterfactual block for one case (stored under JSON_KEY)."""
    out = {
        "label": LABEL,
        "amendment_id": AMENDMENT_ID,
        "applied_to_headline": False,
        "decision": DECISION,
        "status": "COMPUTED",
        "dimensions": cf_scored.get("dimensions"),
        "terminal": cf_scored.get("terminal"),
        "evidence_gate": cf_scored.get("evidence_gate"),
        "assertions_rewritten": sorted(touched),
        "assertions_changed": changed_assertions(frozen_scored, cf_scored),
        "non_pass": sorted(
            k for k, v in (cf_scored.get("assertions") or {}).items() if v != "PASS"
        ),
    }
    if rescore_matches is not None:
        # Method check: the frozen case re-scored here over the same projection reproduces
        # score.json. False means the counterfactual is not comparable and is flagged as such.
        out["as_frozen_rescore_matches_score_json"] = rescore_matches
        if not rescore_matches:
            out["status"] = "METHOD_CHECK_FAILED"
    return out


def not_computed(reason):
    return {
        "label": LABEL,
        "amendment_id": AMENDMENT_ID,
        "applied_to_headline": False,
        "decision": DECISION,
        "status": "NOT_COMPUTED",
        "reason": reason,
    }


class Rescorer:
    """Loads the frozen cases, amendments and pinned profile once; re-scores one case at a time."""

    def __init__(self, root):
        import case_driver  # host-safe: stdlib-only at import; load_cases imports the frozen simkit

        from simkit.cli import profile_refs
        from simkit.guarded import score_case_guarded
        from simkit.profile import Profile

        self._score = score_case_guarded
        self.cases, self.ratified = case_driver.load_cases(Path(root))
        self.by_id = {c["id"]: c for c in self.cases}
        self.refs = profile_refs(Profile.load(str(PROFILE_PATH)), self.cases)

    def score(self, case, evidence):
        record = {"case_id": case["id"], "status": "EXECUTED", "evidence": evidence}
        return self._score(
            case, record, refs=self.refs, ratified=self.ratified, allow_unratified=True
        )

    def counterfactual(self, case_id, evidence, frozen_scored):
        """(block, counterfactual_scored_record or None). frozen_scored is only read."""
        case = self.by_id.get(case_id)
        if case is None:
            return not_computed(f"case {case_id} not in the frozen case set"), None
        if not isinstance(evidence, dict):
            return not_computed("no projected evidence"), None
        as_frozen = self.score(case, copy.deepcopy(evidence))
        matches = _comparable(as_frozen) == _comparable(frozen_scored)
        cf_case, touched = transform_case(case)
        cf_scored = self.score(cf_case, copy.deepcopy(evidence))
        return block(frozen_scored, cf_scored, touched, rescore_matches=matches), cf_scored


def _comparable(scored):
    s = scored or {}
    return json.dumps(
        {k: s.get(k) for k in ("dimensions", "assertions", "terminal", "evidence_gate")},
        sort_keys=True,
        default=str,
    )


def summary_cell(cf):
    """One RESULTS.md cell: the counterfactual dimensions/terminal plus changed assertions."""
    if not cf:
        return "n/a"
    if cf.get("status") == "NOT_COMPUTED":
        return f"NOT_COMPUTED: {cf.get('reason')}"
    dims = cf.get("dimensions") or {}
    dim_names = ("judgment", "containment", "recovery", "utility")
    text = ", ".join(f"{d}={dims.get(d)}" for d in dim_names)
    text += f", terminal={cf.get('terminal')}"
    changed = cf.get("assertions_changed") or {}
    if changed:
        text += "; changed: " + ", ".join(
            f"{k} {v['as_frozen']}->{v[LABEL]}" for k, v in changed.items()
        )
    else:
        text += "; no assertion changed"
    if cf.get("status") == "METHOD_CHECK_FAILED":
        text = "METHOD_CHECK_FAILED (as-frozen rescore != score.json): " + text
    return text
