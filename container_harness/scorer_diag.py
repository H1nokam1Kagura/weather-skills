"""scorer_diag: operand-level explanations and vacuity accounting for the frozen v2.7 scorer.

DIAGNOSTIC ONLY. This module never changes a verdict. For every assertion the verdict it
reports is the one the pinned frozen scorer computes (``simkit.guarded.score_case_guarded``
over the same case, record, refs and ratification state that ``case_driver score`` uses).
Next to that verdict it gives an *explanation*: the evaluation re-traced operand by operand
with the scorer's own internal functions (``_clause``, ``condition``, ``_resolve_full``,
``present_count`` ...), imported read-only from the pinned package. The re-traced verdict is
compared with the scorer's verdict; when they differ, or when the predicate form cannot be
decomposed (``rubric``: semantic grading needs a grader), the assertion is reported as
``not_explained`` rather than guessed. ``tests/test_scorer_diag.py`` asserts verdict identity
over real live evidence.

What it reports (background: research/FAULT-INJECTION-SURVEY-DISTSYS.md ideas 1-3,
FAULT-INJECTION-SURVEY-AGENTS.md ideas 5-6):

1. Operand explanations: per case and assertion, the scorer verdict and, per conjunct, the
   JSON path read, the value found (truncated) and the conjunct's verdict; for an ``implies``
   whether the antecedent held.
2. Vacuity accounting: for every implication-shaped assertion (``implies``, and
   ``typed_shape`` with a ``when`` guard), how often the antecedent held across a corpus of
   runs; assertions that passed only vacuously in every run; for injection cases, the
   assertions tied to a forbidden effect (``covers_forbidden`` non-empty) whose antecedent
   never fired.

The scored evidence is ``projection.json``'s ``projection.evidence`` (only when the projection
status is OK), with ``runner_record.json``'s status, exactly as ``case_driver.score`` selects it.

TODO (out of scope for this pass): mutation testing of the scorer, i.e. corrupting evidence
documents and requiring some predicate to reject each mutant (DISTSYS idea 2, AGENTS idea 5).

CLI:
    python container_harness/scorer_diag.py explain PROJECTION_OR_DIR... [--out FILE]
    python container_harness/scorer_diag.py corpus DIR... --out-json FILE --out-md FILE
"""

from __future__ import annotations

import argparse
import copy
import gzip
import hashlib
import json
import re
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
REPO = HERE.parent
PROFILE_PATH = HERE / "pinned" / "profiles" / "cached-onset" / "profile.json"
PINNED_SCORE = HERE / "pinned" / "simkit" / "score.py"
DATASET_REL = "casesets/cached-onset-e2e-v2_7/frozen/agentic-e2e-injections.DEV-ONLY.json"
SCHEMA = "simkit.scorer-diag/1"
VALUE_LIMIT = 160
MAX_RECORD_DETAILS = 4

PASS, FAIL, NOT_OBSERVED, NOT_APPLICABLE = "PASS", "FAIL", "NOT_OBSERVED", "NOT_APPLICABLE"
LEVEL_OPERANDS = "operands"  # every conjunct / operand re-traced
LEVEL_WHOLE = "whole"  # a single-operand form: its inputs shown, verdict from the scorer helper
LEVEL_NONE = "not_explained"


# ------------------------------------------------------------------ loading the frozen scorer
def _sha256(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


class Frozen:
    """The verified frozen package (tests/frozen_v27.py), loaded read-only, plus the effective
    cases, ratification state and profile refs exactly as case_driver.score uses them."""

    def __init__(self, root=None, frozen_package=None):
        for p in (REPO / "tests", HERE):
            if str(p) not in sys.path:
                sys.path.insert(0, str(p))
        import frozen_v27

        self.root = Path(root) if root else frozen_v27.frozen_root()
        if str(self.root) not in sys.path:
            sys.path.insert(0, str(self.root))
        import case_driver  # stdlib-only at import; load_cases imports the frozen simkit
        from simkit import score
        from simkit.cli import profile_refs
        from simkit.guarded import score_case_guarded
        from simkit.profile import Profile

        self.S = score
        self.scorer_sha256 = _sha256(self.root / "simkit" / "score.py")
        self.pinned_scorer_sha256 = _sha256(PINNED_SCORE)
        if self.scorer_sha256 != self.pinned_scorer_sha256:
            raise RuntimeError(
                "frozen package scorer differs from container_harness/pinned/simkit/score.py"
            )
        self.dataset_sha256 = _sha256(self.root / DATASET_REL)
        if frozen_package is not None and _sha256(frozen_package) != self.dataset_sha256:
            raise RuntimeError(f"{frozen_package} is not byte-identical to the pinned case package")
        self._score_case = score_case_guarded
        self.cases, self.ratified = case_driver.load_cases(self.root)
        self.by_id = {c["id"]: c for c in self.cases}
        self.refs = profile_refs(Profile.load(str(PROFILE_PATH)), self.cases)

    def score(self, case, record):
        """The frozen verdicts: score_case_guarded exactly as case_driver.score calls it."""
        return self._score_case(
            case, record, refs=self.refs, ratified=self.ratified, allow_unratified=True
        )


# ------------------------------------------------------------------ evidence selection
def _load_json(path):
    path = Path(path)
    if not path.is_file():
        return None
    raw = path.read_bytes()
    if path.suffix == ".gz":
        raw = gzip.decompress(raw)
    try:
        return json.loads(raw)
    except ValueError:
        return None


def _sibling(projection_path, name):
    p = Path(projection_path)
    for cand in (p.with_name(name), p.with_name(name + ".gz")):
        if cand.is_file():
            return cand
    return None


def scored_record(projection_path, case_id=None):
    """(case_id, record, score_json) mirroring case_driver.score's choice of scored evidence."""
    projection = _load_json(projection_path) or {}
    rr_path = _sibling(projection_path, "runner_record.json")
    sj_path = _sibling(projection_path, "score.json")
    runner = _load_json(rr_path) if rr_path else None
    score_json = _load_json(sj_path) if sj_path else None
    cid = (
        case_id
        or (score_json or {}).get("case_id")
        or (runner or {}).get("case_id")
        or Path(projection_path).resolve().parent.parent.name
    )
    if isinstance(runner, dict):
        status = runner.get("status")
        if status == "EXECUTED" and not runner.get("controller_ran"):
            status = "NOT_EXECUTED"
    else:
        status = "EXECUTED" if projection.get("status") == "OK" else "HARNESS_ERROR"
    evidence = None
    if status == "EXECUTED":
        if projection.get("status") == "OK":
            evidence = (projection.get("projection") or {}).get("evidence")
        else:
            status = "HARNESS_ERROR"
    return cid, {"case_id": cid, "status": status, "evidence": evidence}, score_json


# ------------------------------------------------------------------ value display
def _show(v, missing):
    if v is missing:
        return None
    try:
        s = json.dumps(v, ensure_ascii=False, default=str, sort_keys=True)
    except (TypeError, ValueError):
        s = repr(v)
    return s if len(s) <= VALUE_LIMIT else s[: VALUE_LIMIT - 3] + "..."


def _read_path(S, ev, path):
    """Resolve one evidence path with the scorer's own resolver: a display dict."""
    v, stream, multi = S._resolve_full(ev, path)
    out = {"path": path}
    if not stream:
        out["found"] = "stream_missing"
        return out
    if v is S._MISSING:
        why = S.why_missing(ev, path)
        out["found"] = "missing:" + (why if isinstance(why, str) else "key_missing")
        return out
    out["value"] = _show(v, S._MISSING)
    if multi and isinstance(v, list):
        out["n_selected"] = len(v)
    return out


# ------------------------------------------------------------------ clause / condition explanations
_BEFORE = re.compile(r"^(\S+) before (\S+)$")
_EXISTS = re.compile(r"^(\S+) (exists|missing)$")
_COUNT = re.compile(r"^count\((\S+)\) (==|>=|<=|>|<) (\d+|\$\S+)$")
_CMP = re.compile(r"^(\S+) (==|!=|>=|<=|>|<) (.+)$")
_CONT = re.compile(r"^(\S+) (contains|lacks) (.+)$")


def explain_clause(S, text, ev, role):
    """One clause: the scorer's _clause verdict plus the operands it read."""
    raw = text.strip()
    verdict = S._clause(raw, ev, role)
    d = {"clause": raw, "verdict": verdict}
    bound = S._bind(ev, raw)
    if bound is None:
        d["form"] = "unresolved_reference"
        return d
    if bound != raw:
        d["bound"] = bound
    t = bound
    m = _BEFORE.match(t)
    if m:
        seq = ev.get("event_seq") if isinstance(ev, dict) else None
        seq = seq if isinstance(seq, dict) else {}
        d["form"] = "before"
        d["operands"] = [
            {"path": f"event_seq[{k}]", "value": _show(seq.get(k), None)} for k in m.groups()
        ]
        return d
    m = _EXISTS.match(t)
    if m:
        d["form"] = m.group(2)
        d["operands"] = [_read_path(S, ev, m.group(1))]
        return d
    m = _COUNT.match(t)
    if m:
        d["form"] = "count"
        n = S.present_count(ev, m.group(1))
        d["operands"] = [{"path": f"count({m.group(1)})", "value": _show(n, S._MISSING)}]
        d["expected"] = f"{m.group(2)} {m.group(3)}"
        return d
    quant = None
    for q in ("any ", "every "):
        if t.startswith(q):
            quant, t = q.strip(), t[len(q) :]
    m = _CMP.match(t) or _CONT.match(t)
    if not m or " " in m.group(1):
        d["form"] = "unparseable"
        return d
    path, op, rhs = m.groups()
    d["form"] = "compare"
    if quant:
        d["quantifier"] = quant
    d["operands"] = [_read_path(S, ev, path)]
    d["expected"] = f"{op} {rhs.strip()}"
    if rhs.strip().startswith(("$", "HARNESS_")):
        d["expected_value"] = _show(S._literal(rhs, ev), S._MISSING)
    return d


def _kleene_and(results):
    if any(r is None for r in results):
        return None
    if FAIL in results:
        return FAIL
    return PASS if all(r == PASS for r in results) else NOT_OBSERVED


def explain_condition(S, text, ev, role):
    """A condition string: the scorer's condition() verdict, each clause explained, and whether
    the clause verdicts combine (Kleene AND, as the scorer does) to the scorer's verdict."""
    clauses = [explain_clause(S, p, ev, role) for p in S._CLAUSE_SPLIT.split(text)]
    verdict = S.condition(text, ev, role)
    combined = _kleene_and([c["verdict"] for c in clauses])
    return {
        "text": text,
        "role": role,
        "verdict": verdict,
        "consistent": combined == verdict,
        "clauses": clauses,
    }


# ------------------------------------------------------------------ predicate explanations
def _implies(S, pred, ev):
    obs, cons = pred["observation"], pred.get("operand")
    ex = {"form": "implies", "vacuous_mode": pred.get("vacuous", "pass")}
    consistent = True
    if S._is_condition(obs):
        ante = explain_condition(S, obs.replace("==", " == ").replace("  ", " "), ev, S.ANTECEDENT)
        consistent &= ante["consistent"]
        av = ante["verdict"]
    else:
        ante, av = {"text": obs, "verdict": None, "note": "not a condition"}, None
    ex["antecedent"] = ante
    ex["antecedent_held"] = av == PASS
    if av is None or av == NOT_OBSERVED:
        return ex, NOT_OBSERVED, consistent
    if av == FAIL:
        ex["vacuous"] = True
        return ex, (NOT_APPLICABLE if pred.get("vacuous") == "not_applicable" else PASS), consistent
    voc = pred.get("closed_vocabulary")
    if voc is not None:
        vex = {"vocabulary": voc}
        ex["closed_vocabulary"] = vex
        if not (
            isinstance(voc, dict)
            and isinstance(voc.get("observation"), str)
            and isinstance(voc.get("values"), list)
        ):
            vex["verdict"] = "malformed"
            return ex, NOT_OBSERVED, consistent
        vex["operand"] = _read_path(S, ev, voc["observation"])
        v, stream, multi = S._resolve_full(ev, voc["observation"])
        if stream and v is not S._MISSING:
            vals = v if (multi and isinstance(v, list)) else [v]
            if any(not any(S._teq(x, ok) for ok in voc["values"]) for x in vals):
                vex["verdict"] = "outside_vocabulary"
                return ex, NOT_OBSERVED, consistent
        vex["verdict"] = "ok"
    if isinstance(cons, str):
        c = explain_condition(S, cons, ev, S.CONSEQUENT)
        consistent &= c["consistent"]
        ex["consequent"] = c
        return ex, (NOT_OBSERVED if c["verdict"] is None else c["verdict"]), consistent
    ex["consequent"] = {"text": cons, "verdict": None, "note": "not a condition string"}
    return ex, NOT_OBSERVED, consistent


def _condition_list(S, pred, ev, role, form):
    conds = pred.get("operand")
    ex = {"form": form}
    if not isinstance(conds, list) or not conds:
        ex["note"] = "operand is not a non-empty list"
        return ex, NOT_OBSERVED, True
    items, res, consistent = [], [], True
    for c in conds:
        if isinstance(c, str):
            e = explain_condition(S, c, ev, role)
            consistent &= e["consistent"]
            items.append(e)
            res.append(e["verdict"])
        else:
            items.append({"text": c, "verdict": None, "note": "not a condition string"})
            res.append(None)
    ex["operands"] = items
    if any(r is None for r in res):
        return ex, NOT_OBSERVED, consistent
    if form == "matches_any":
        if PASS in res:
            return ex, PASS, consistent
        return ex, (FAIL if all(r == FAIL for r in res) else NOT_OBSERVED), consistent
    if FAIL in res:
        return ex, FAIL, consistent
    return ex, (PASS if all(r == PASS for r in res) else NOT_OBSERVED), consistent


def _for_each(S, pred, ev):
    conds, obs = pred.get("operand"), pred.get("observation")
    ex = {"form": "for_each", "observation": obs}
    if not isinstance(conds, list) or not conds or not isinstance(obs, str) or S._is_condition(obs):
        ex["note"] = "malformed for_each"
        return ex, NOT_OBSERVED, True
    for need in pred.get("requires_present", []):
        v, s, _ = S._resolve_full(ev, need) if isinstance(need, str) else (S._MISSING, False, False)
        if not s or v is S._MISSING or v is None:
            ex["requires_present_missing"] = need
            return ex, NOT_OBSERVED, True
    bound = S._bind(ev, obs)
    if bound is None:
        ex["note"] = "observation reference did not resolve"
        return ex, NOT_OBSERVED, True
    val, stream, fanned = S._resolve_full(ev, bound)
    ex["list"] = _read_path(S, ev, bound)
    if not stream:
        return ex, NOT_OBSERVED, True
    if val is S._MISSING:
        empty = S.why_missing(ev, bound) == "empty_selection"
        ex["empty_selection"] = empty
        return ex, (PASS if empty else NOT_OBSERVED), True
    recs = val if (fanned or isinstance(val, list)) else [val]
    results, details, consistent = [], [], True
    for i, rec in enumerate(recs):
        if not isinstance(rec, dict):
            results.append(NOT_OBSERVED)
            details.append({"record": i, "verdict": NOT_OBSERVED, "note": "not an object"})
            continue
        sub = dict(ev)
        sub[S._EACH_ROOT] = rec
        rec_res, rec_items = [], []
        for c in conds:
            t = S._each_text(c, rec) if isinstance(c, str) else None
            if t is None:
                rec_res.append(NOT_OBSERVED)
                rec_items.append({"text": c, "verdict": NOT_OBSERVED, "note": "substitution"})
                continue
            e = explain_condition(S, t, sub, S.CONSEQUENT)
            consistent &= e["consistent"]
            r = NOT_OBSERVED if e["verdict"] is None else e["verdict"]
            rec_res.append(r)
            rec_items.append(e)
        results.extend(rec_res)
        rv = (
            FAIL if FAIL in rec_res else (PASS if all(r == PASS for r in rec_res) else NOT_OBSERVED)
        )
        details.append({"record": i, "verdict": rv, "conditions": rec_items})
    ex["n_records"] = len(recs)
    ex["record_verdicts"] = {
        k: sum(1 for d in details if d["verdict"] == k) for k in (PASS, FAIL, NOT_OBSERVED)
    }
    shown = [d for d in details if d["verdict"] != PASS][:MAX_RECORD_DETAILS] or details[:1]
    ex["records"] = shown
    if FAIL in results:
        return ex, FAIL, consistent
    return ex, (PASS if all(r == PASS for r in results) else NOT_OBSERVED), consistent


def _typed_shape(S, pred, ev, refs):
    ex = {"form": "typed_shape", "observation": pred.get("observation")}
    verdict = S._op_typed_shape(pred, ev, refs)
    if "when" in pred:
        ex["guarded"] = True
        ex["antecedent"] = None
        list_path = pred.get("when_list")
        when = pred.get("when")
        ok = (
            isinstance(list_path, str)
            and list_path
            and not S._is_condition(list_path)
            and S._bare_paths_ok(list_path)
        )
        lp = S._bind(ev, list_path) if ok else None
        if lp is not None:
            receipts, observed, fanned = S._resolve_full(ev, lp)
            ex["when_list"] = _read_path(S, ev, lp)
            if (
                observed
                and not fanned
                and isinstance(receipts, list)
                and isinstance(when, str)
                and when
                and S._bare_paths_ok(when)
            ):
                ante = explain_condition(S, when, ev, S.ANTECEDENT)
                ex["antecedent"] = ante
                ex["antecedent_held"] = ante["verdict"] == PASS
                if ante["verdict"] == FAIL:
                    ex["vacuous"] = True
    obs = pred.get("observation")
    if isinstance(obs, str) and obs and not S._is_condition(obs):
        b = S._bind(ev, obs)
        if b is not None:
            ex["operand"] = _read_path(S, ev, b)
    return ex, verdict, True


def _before(S, pred, ev, refs):
    seq = ev.get("event_seq") if isinstance(ev, dict) else None
    seq = seq if isinstance(seq, dict) else {}
    ex = {"form": "before", "right_absent": pred.get("right_absent")}
    ops = []
    for side in (pred.get("observation"), pred.get("operand") or ""):
        b = S._bind(ev, side) if isinstance(side, str) else None
        key = b.split("==")[0].strip() if isinstance(b, str) else side
        ops.append({"path": f"event_seq[{key}]", "value": _show(seq.get(key), None)})
    ex["operands"] = ops
    return ex, S._op_before(pred, ev, refs), True


def _none_after(S, pred, ev, refs):
    seq = ev.get("event_seq") if isinstance(ev, dict) else None
    seq = seq if isinstance(seq, dict) else {}
    stop = S._bind(ev, pred.get("observation", ""))
    ex = {
        "form": "none_after",
        "operands": [{"path": f"event_seq[{stop}]", "value": _show(seq.get(stop), None)}],
        "banned": pred.get("operand"),
        "note": "verdict from the scorer helper; violating keys are not itemised",
    }
    return ex, S._op_none_after(pred, ev, refs), True


def _run_evidence(S, pred, ev, refs):
    found = S.run_evidence_findings(pred.get("operand"), ev)
    ex = {
        "form": "run_evidence",
        "n_findings": len(found),
        "findings": [f"{s}: {m}"[:VALUE_LIMIT] for s, m in found[:10]],
    }
    if any(s == FAIL for s, _ in found):
        return ex, FAIL, True
    return ex, (NOT_OBSERVED if found else PASS), True


def _leaf(S, pred, ev, refs):
    op = pred["operator"]
    ex = {"form": op}
    obs = pred.get("observation")
    if isinstance(obs, str) and not S._is_condition(obs):
        b = S._bind(ev, obs)
        if b is not None:
            ex["operands"] = [_read_path(S, ev, b)]
    operand = pred.get("operand")
    if operand is not None:
        ex["expected"] = _show(operand, None)
    return ex, S.evaluate(pred, ev, None, refs), True


def explain_predicate(S, pred, ev, refs):
    """(explanation, re-traced verdict, level). The re-traced verdict is only ever compared with
    the scorer's verdict; it is never reported as the verdict."""
    op = pred.get("operator")
    if op == "rubric":
        return {"form": "rubric", "note": "semantic grading needs a grader"}, None, LEVEL_NONE
    if op == "implies":
        ex, v, ok = _implies(S, pred, ev)
        return ex, v, (LEVEL_OPERANDS if ok else LEVEL_NONE)
    if op in ("all_of", "matches_any"):
        role = S.CONSEQUENT if op == "all_of" else S.ANTECEDENT
        ex, v, ok = _condition_list(S, pred, ev, role, op)
        return ex, v, (LEVEL_OPERANDS if ok else LEVEL_NONE)
    if op == "for_each":
        ex, v, ok = _for_each(S, pred, ev)
        return ex, v, (LEVEL_OPERANDS if ok else LEVEL_NONE)
    if op == "typed_shape":
        ex, v, _ = _typed_shape(S, pred, ev, refs)
        return ex, v, LEVEL_WHOLE
    if op == "before":
        ex, v, _ = _before(S, pred, ev, refs)
        return ex, v, LEVEL_OPERANDS
    if op == "none_after":
        ex, v, _ = _none_after(S, pred, ev, refs)
        return ex, v, LEVEL_WHOLE
    if op == "run_evidence":
        ex, v, _ = _run_evidence(S, pred, ev, refs)
        return ex, v, LEVEL_WHOLE
    ex, v, _ = _leaf(S, pred, ev, refs)
    return ex, v, LEVEL_WHOLE


def implication_of(pred, ex):
    """Vacuity facts for an implication-shaped assertion, None for any other form."""
    op = pred.get("operator")
    if op == "implies":
        ante = (ex.get("antecedent") or {}).get("verdict")
    elif op == "typed_shape" and "when" in pred:
        ante = (ex.get("antecedent") or {}).get("verdict") if ex.get("antecedent") else None
    else:
        return None
    outcome = {PASS: "fired", FAIL: "vacuous"}.get(ante, "undecided")
    return {"antecedent": ante, "antecedent_held": ante == PASS, "outcome": outcome}


# ------------------------------------------------------------------ one case run
def diagnose(fz, case, record, source=None, score_json=None):
    """Explain every assertion of one case run. Verdicts are the frozen scorer's, unchanged."""
    S = fz.S
    scored = fz.score(case, copy.deepcopy(record))
    out = {
        "source": str(source) if source else None,
        "case_id": case["id"],
        "kind": case.get("kind"),
        "family_id": case.get("family_id"),
        "run_status": scored.get("run_status"),
        "dimensions": scored.get("dimensions"),
        "terminal": scored.get("terminal"),
        "scorer_error": scored.get("scorer_error"),
        "assertions": [],
    }
    if score_json is not None:
        sj = (score_json.get("scored") or {}).get("assertions")
        out["score_json_assertions_match"] = sj == scored.get("assertions")
    verdicts = scored.get("assertions") or {}
    ev = copy.deepcopy(record.get("evidence") or {})
    for a in case["assertions"]:
        if a["id"] not in verdicts:
            continue
        pred = a["predicate"]
        row = {
            "id": a["id"],
            "dimension": a["dimension"],
            "operator": pred.get("operator"),
            "verdict": verdicts[a["id"]],
            "covers_forbidden": a.get("covers_forbidden") or [],
        }
        try:
            ex, retraced, level = explain_predicate(S, pred, ev, fz.refs)
        except Exception as e:  # noqa: BLE001 - a diagnostic failure is reported, never scored
            ex, retraced, level = {"error": f"{type(e).__name__}: {e}"}, None, LEVEL_NONE
        if level != LEVEL_NONE and retraced != row["verdict"]:
            ex["not_explained_reason"] = (
                f"re-traced verdict {retraced} differs from the scorer's {row['verdict']}"
            )
            level = LEVEL_NONE
        row["retraced_verdict"] = retraced
        row["explanation_level"] = level
        row["implication"] = implication_of(pred, ex)
        row["explanation"] = ex
        out["assertions"].append(row)
    return out


def run_label(path):
    parts = Path(path).resolve().parts
    for i, p in enumerate(parts):
        if p.startswith("run-"):
            return "/".join(parts[max(0, i - 1) : i + 1])
    return str(path)


def find_projections(paths):
    out = []
    for p in paths:
        p = Path(p)
        if p.is_file():
            out.append(p)
        else:
            found = sorted(p.rglob("harness-out/projection.json"))
            found += sorted(p.rglob("harness-out/projection.json.gz"))
            out.extend(found)
    return out


def diagnose_paths(fz, paths):
    runs = []
    for proj in find_projections(paths):
        cid, record, sj = scored_record(proj)
        case = fz.by_id.get(cid)
        if case is None:
            runs.append({"source": str(proj), "case_id": cid, "error": "case not in frozen set"})
            continue
        r = diagnose(fz, case, record, source=proj, score_json=sj)
        r["run"] = run_label(proj)
        runs.append(r)
    return runs


# ------------------------------------------------------------------ corpus report
def identity_summary(runs):
    n = same_json = json_checked = 0
    levels, not_explained = {}, {}
    for r in runs:
        if "score_json_assertions_match" in r:
            json_checked += 1
            same_json += bool(r["score_json_assertions_match"])
        for a in r.get("assertions", []):
            n += 1
            levels[a["explanation_level"]] = levels.get(a["explanation_level"], 0) + 1
            if a["explanation_level"] == LEVEL_NONE:
                k = a["operator"]
                not_explained[k] = not_explained.get(k, 0) + 1
    return {
        "case_runs": len(runs),
        "assertion_verdicts": n,
        "explanation_levels": levels,
        "not_explained_by_operator": not_explained,
        "score_json_checked": json_checked,
        "score_json_identical": same_json,
    }


def vacuity(runs, by_id):
    acc = {}
    for r in runs:
        for a in r.get("assertions", []):
            imp = a.get("implication")
            if imp is None:
                continue
            k = (r["case_id"], a["id"])
            e = acc.setdefault(
                k,
                {
                    "case_id": r["case_id"],
                    "kind": r.get("kind"),
                    "assertion": a["id"],
                    "dimension": a["dimension"],
                    "operator": a["operator"],
                    "covers_forbidden": a["covers_forbidden"],
                    "runs": 0,
                    "fired": 0,
                    "vacuous": 0,
                    "undecided": 0,
                    "verdicts": {},
                },
            )
            e["runs"] += 1
            e[imp["outcome"]] += 1
            e["verdicts"][a["verdict"]] = e["verdicts"].get(a["verdict"], 0) + 1
    rows = []
    for e in acc.values():
        decided_pass = all(v in (PASS, NOT_APPLICABLE) for v in e["verdicts"])
        e["vacuous_every_run"] = e["fired"] == 0 and e["vacuous"] == e["runs"] and decided_pass
        e["never_fired"] = e["fired"] == 0
        e["injection_target_never_fired"] = (
            e["kind"] == "injection" and bool(e["covers_forbidden"]) and e["fired"] == 0
        )
        case = by_id.get(e["case_id"]) or {}
        pred = next(
            (a["predicate"] for a in case.get("assertions", []) if a["id"] == e["assertion"]), {}
        )
        e["antecedent_text"] = (
            pred.get("observation") if e["operator"] == "implies" else pred.get("when")
        )
        rows.append(e)
    rows.sort(key=lambda e: (e["case_id"], e["assertion"]))
    by_assertion = {}
    for e in rows:
        b = by_assertion.setdefault(
            e["assertion"], {"cases": 0, "runs": 0, "fired": 0, "vacuous": 0, "undecided": 0}
        )
        b["cases"] += 1
        for k in ("runs", "fired", "vacuous", "undecided"):
            b[k] += e[k]
    return rows, by_assertion


def failing_operands(row):
    """Short strings naming the non-PASS conjuncts of an explained assertion."""
    ex = row.get("explanation") or {}
    out = []

    def from_condition(c, prefix=""):
        for cl in c.get("clauses", []):
            if cl.get("verdict") != PASS:
                vals = "; ".join(
                    f"{o.get('path')} -> {o.get('value', o.get('found'))}"
                    for o in cl.get("operands", [])
                )
                exp = f" (expected {cl['expected']})" if cl.get("expected") else ""
                out.append(f"{prefix}[{cl.get('verdict')}] {cl['clause'][:140]}{exp} :: {vals}")

    if ex.get("form") == "implies":
        if ex.get("antecedent_held") and ex.get("consequent"):
            from_condition(ex["consequent"], "then ")
        elif not ex.get("antecedent_held"):
            from_condition(ex.get("antecedent") or {}, "if ")
    elif ex.get("form") in ("all_of", "matches_any"):
        for c in ex.get("operands", []):
            if c.get("verdict") != PASS:
                from_condition(c)
    elif ex.get("form") == "for_each":
        for rec in ex.get("records", []):
            for c in rec.get("conditions", []):
                if c.get("verdict") != PASS:
                    from_condition(c, f"record {rec['record']}: ")
    else:
        for o in ex.get("operands", []):
            out.append(f"{o.get('path')} -> {o.get('value', o.get('found'))}")
        out.extend(ex.get("findings", [])[:3])
    if ex.get("not_explained_reason"):
        out.append(ex["not_explained_reason"])
    return out[:8]


def non_pass_digest(runs):
    groups = {}
    for r in runs:
        for a in r.get("assertions", []):
            if a["verdict"] in (PASS, NOT_APPLICABLE):
                continue
            ops = failing_operands(a)
            k = (r["case_id"], a["id"], a["verdict"], json.dumps(ops))
            g = groups.setdefault(
                k,
                {
                    "case_id": r["case_id"],
                    "assertion": a["id"],
                    "verdict": a["verdict"],
                    "operands": ops,
                    "runs": [],
                },
            )
            g["runs"].append(r.get("run"))
    out = sorted(groups.values(), key=lambda g: (g["case_id"], g["assertion"], g["verdict"]))
    return out


def corpus_report(fz, runs):
    rows, by_assertion = vacuity(runs, fz.by_id)
    return {
        "schema": SCHEMA,
        "diagnostic_only": True,
        "scorer_sha256": fz.scorer_sha256,
        "case_package_sha256": fz.dataset_sha256,
        "identity": identity_summary(runs),
        "vacuity": rows,
        "vacuity_by_assertion": by_assertion,
        "non_pass": non_pass_digest(runs),
        "runs": [
            {
                k: r.get(k)
                for k in (
                    "run",
                    "source",
                    "case_id",
                    "kind",
                    "run_status",
                    "dimensions",
                    "terminal",
                    "score_json_assertions_match",
                )
            }
            for r in runs
        ],
    }


def slim(runs):
    """Drop the operand explanation of PASS / NOT_APPLICABLE assertions (verdict, level and
    implication facts stay), to keep a corpus report readable."""
    out = []
    for r in runs:
        r2 = dict(r)
        r2["assertions"] = [
            {k: v for k, v in a.items() if k != "explanation"}
            if a["verdict"] in (PASS, NOT_APPLICABLE)
            else a
            for a in r.get("assertions", [])
        ]
        out.append(r2)
    return out


def _md_cell(s):
    return str(s).replace("|", "\\|").replace("\n", " ")


def corpus_markdown(rep):
    idn = rep["identity"]
    L = [
        "# Scorer diagnostics over the live corpus",
        "",
        "Diagnostic only: every verdict below is the pinned frozen scorer's own verdict, unchanged.",
        "Explanations re-trace the scorer's evaluation operand by operand with its own functions;",
        "where the re-traced verdict differs, or the form cannot be decomposed, the assertion is",
        "counted as not explained rather than guessed.",
        "",
        f"- Scorer sha256: `{rep['scorer_sha256']}`",
        f"- Case package sha256: `{rep['case_package_sha256']}`",
        f"- Case runs: {idn['case_runs']}; assertion verdicts: {idn['assertion_verdicts']}",
        "",
        "## Verdict identity",
        "",
        f"- In-container `score.json` assertion verdicts identical to a fresh frozen-scorer run: "
        f"{idn['score_json_identical']} of {idn['score_json_checked']} case runs.",
        f"- Explanation levels: {json.dumps(idn['explanation_levels'], sort_keys=True)}",
        f"- Not explained, by operator: {json.dumps(idn['not_explained_by_operator'], sort_keys=True)}",
        "",
        "Levels: `operands` = every conjunct re-traced and the re-traced verdict equals the scorer's;",
        "`whole` = single-operand form, inputs shown, verdict from the scorer's helper;",
        "`not_explained` = rubric (needs a grader) or a re-trace that disagreed.",
        "",
    ]
    vac = rep["vacuity"]
    inj = [e for e in vac if e["injection_target_never_fired"]]
    allvac = [e for e in vac if e["vacuous_every_run"]]
    L += [
        "## Vacuity",
        "",
        f"Implication-shaped (case, assertion) pairs: {len(vac)}; never fired: "
        f"{sum(e['never_fired'] for e in vac)}; passed only vacuously in every run: {len(allvac)}; "
        f"injection-target implications that never fired: {len(inj)}.",
        "",
        "### Injection-target implications that never fired",
        "",
        "An injection case's assertion is treated as an injection target when it names a forbidden",
        "effect (`covers_forbidden` non-empty).",
        "",
        "| case | assertion | dim | runs | fired | vacuous | undecided | verdicts | antecedent |",
        "|---|---|---|---|---|---|---|---|---|",
    ]
    for e in inj:
        L.append(
            f"| {e['case_id']} | {e['assertion']} | {e['dimension']} | {e['runs']} | {e['fired']} | "
            f"{e['vacuous']} | {e['undecided']} | {_md_cell(json.dumps(e['verdicts'], sort_keys=True))} | "
            f"`{_md_cell(str(e['antecedent_text'])[:110])}` |"
        )
    L += [
        "",
        "### Passed only vacuously in every run",
        "",
        "| case | assertion | dim | runs | verdicts | antecedent |",
        "|---|---|---|---|---|---|",
    ]
    for e in allvac:
        L.append(
            f"| {e['case_id']} | {e['assertion']} | {e['dimension']} | {e['runs']} | "
            f"{_md_cell(json.dumps(e['verdicts'], sort_keys=True))} | `{_md_cell(str(e['antecedent_text'])[:110])}` |"
        )
    L += [
        "",
        "### By assertion id, across cases",
        "",
        "| assertion | cases | runs | fired | vacuous | undecided |",
        "|---|---|---|---|---|---|",
    ]
    for k in sorted(rep["vacuity_by_assertion"]):
        b = rep["vacuity_by_assertion"][k]
        L.append(
            f"| {k} | {b['cases']} | {b['runs']} | {b['fired']} | {b['vacuous']} | {b['undecided']} |"
        )
    L += [
        "",
        "## Non-PASS assertions with their failing operands",
        "",
        "Grouped where the failing operands are identical across runs.",
        "",
    ]
    for g in rep["non_pass"]:
        L.append(
            f"- **{g['case_id']} {g['assertion']}: {g['verdict']}** in {len(g['runs'])} run(s) ({', '.join(map(str, g['runs']))})"
        )
        for o in g["operands"]:
            L.append(f"  - `{_md_cell(o)}`")
    L += [
        "",
        "## Runs",
        "",
        "| run | case | status | dimensions | terminal | score.json identical |",
        "|---|---|---|---|---|---|",
    ]
    for r in rep["runs"]:
        L.append(
            f"| {r['run']} | {r['case_id']} | {r['run_status']} | "
            f"{_md_cell(json.dumps(r['dimensions'], sort_keys=True))} | {r['terminal']} | {r.get('score_json_assertions_match')} |"
        )
    L += [
        "",
        "## Limitations",
        "",
        "- `whole`-level forms (typed_shape, none_after, run_evidence and the simple operators) show",
        "  their inputs but not a decomposition; none_after does not itemise violating keys.",
        "- rubric assertions are not explained (semantic grading needs a grader).",
        "- An injection target is identified by `covers_forbidden`; the frozen package has no",
        "  explicit per-assertion modality (always / sometimes), so this is a heuristic.",
        "- Vacuity is counted for `implies` and guarded `typed_shape` only. A `for_each` over an",
        "  observed empty selection also passes with nothing checked; that is shown in its",
        "  explanation (`empty_selection`) but not counted here. Rubric `applies_when` guards are",
        "  not counted.",
        "- Values are truncated to keep the report readable; the JSON carries the same truncation.",
        "- TODO: mutation testing of the scorer (corrupted evidence documents) is out of scope here.",
        "",
    ]
    return "\n".join(L)


def main(argv=None):
    ap = argparse.ArgumentParser(prog="scorer_diag", description=__doc__.split("\n")[0])
    sub = ap.add_subparsers(dest="cmd", required=True)
    pe = sub.add_parser("explain", help="explain every assertion of one or more case runs")
    pe.add_argument("paths", nargs="+")
    pe.add_argument("--out")
    pc = sub.add_parser("corpus", help="vacuity accounting and explanations over a corpus")
    pc.add_argument("paths", nargs="+")
    pc.add_argument("--out-json", required=True)
    pc.add_argument("--out-md", required=True)
    pc.add_argument("--full", action="store_true", help="keep explanations of passing assertions")
    for p in (pe, pc):
        p.add_argument("--frozen-root")
        p.add_argument("--frozen-package", help="must be byte-identical to the pinned package")
    a = ap.parse_args(argv)
    fz = Frozen(a.frozen_root, a.frozen_package)
    runs = diagnose_paths(fz, a.paths)
    if a.cmd == "explain":
        text = json.dumps({"schema": SCHEMA, "diagnostic_only": True, "runs": runs}, indent=1)
        if a.out:
            Path(a.out).write_text(text + "\n", encoding="utf-8")
        else:
            print(text)
        return 0
    rep = corpus_report(fz, runs)
    rep["runs_detail"] = runs if a.full else slim(runs)
    Path(a.out_json).write_text(json.dumps(rep, indent=1) + "\n", encoding="utf-8")
    Path(a.out_md).write_text(corpus_markdown(rep), encoding="utf-8")
    print(json.dumps(rep["identity"], sort_keys=True))
    return 0


if __name__ == "__main__":
    sys.exit(main())
