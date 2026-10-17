"""rubric_grader.py (sub-wave 8) and its cases_report.py column: recorded fixtures, fake relay /
fake model only. No model is called and nothing is spent.

Uses the frozen v2.7 package from the committed fixture (tests/frozen_v27.py), unedited: the
frozen scorer decides applicability and the frozen probe bundles are the recorded evidence.
"""

import copy
import importlib.util
import json
import sys
from pathlib import Path

import pytest

HERE = Path(__file__).resolve().parent
REPO = HERE.parent.parent
sys.path.insert(0, str(HERE.parent))
sys.path.insert(0, str(REPO))

import case_driver  # noqa: E402
import cases_report  # noqa: E402
import rubric_grader as rg  # noqa: E402
import warn_counterfactual as wcf  # noqa: E402
from test_cases_report import admission, artifact, by_id, row  # noqa: E402

E01, E03, E04, E05, E06 = (f"DEV-E2E-V2_7-E0{n}-P2-INJ" for n in (1, 3, 4, 5, 6))
MODEL = rg.DEFAULT_GRADER_MODEL


@pytest.fixture(scope="module")
def frozen():
    root, _, score = cases_report.frozen()
    cases, _ = case_driver.load_cases(root)
    return {c["id"]: c for c in cases}, score


def assertion(cases, cid, aid=None):
    aid = aid or rg.EXPECTED_TARGETS[cid][0]
    return next(a for a in cases[cid]["assertions"] if a["id"] == aid)


def bundle(cases, cid, name):
    return copy.deepcopy(
        next(b for b in cases[cid]["probe_bundles"] if b["name"] == name)["evidence"]
    )


class FakeRelay:
    """The relay transport seam (RelayRoles.ask): scripted answers, every call recorded."""

    def __init__(self, answer, receipt=None):
        self.answer = answer
        self.receipt = receipt
        self.calls = []
        self.receipts = []

    def ask(self, role, payload):
        self.calls.append((role, copy.deepcopy(payload)))
        out = self.answer(payload) if callable(self.answer) else copy.deepcopy(self.answer)
        if isinstance(out, BaseException):
            raise out
        if self.receipt is not None:
            self.receipts.append(copy.deepcopy(self.receipt))
        return out


def answer(verdict, quotes=(), rationale="grounded in the graded output"):
    return {"verdict": verdict, "rationale": rationale, "evidence_quotes": list(quotes)}


# ------------------------------------------------------------------ targets


def test_targets_are_exactly_the_five_undecided_live_rubric_assertions(frozen):
    cases, score = frozen
    assert rg.rubric_targets(cases.values()) == rg.EXPECTED_TARGETS
    assert len(rg.EXPECTED_TARGETS) == 5
    for cid, aids in rg.EXPECTED_TARGETS.items():
        assert case_driver.role_mode(cases[cid]) == "live"
        for aid in aids:
            pred = assertion(cases, cid, aid)["predicate"]
            assert pred["operator"] == "rubric" and score.decidable(pred) is False
            # without a grader the frozen scorer never decides it PASS or FAIL on content
            assert score.evaluate(pred, {}) in ("NOT_OBSERVED", "NOT_APPLICABLE")


# ------------------------------------------------------------------ payload


def test_payload_is_the_rubric_text_and_this_cases_recorded_evidence_only(frozen):
    cases, score = frozen
    a = assertion(cases, E04)
    ev = bundle(cases, E04, "reviewer rejects")
    relay = FakeRelay(answer(rg.PASS, ["definition_ref differs from goal"]))
    rec = rg.grade_assertion(a, ev, relay, score, MODEL)
    assert rec["verdict"] == rg.PASS and rec["source"] == "grader" and rec["called"] is True
    ((role, payload),) = relay.calls  # one call per assertion
    assert role == rg.ROLE == "rubric_grader"
    assert set(payload) == {
        "schema",
        "rubric",
        "graded_output",
        "context_evidence",
        "context_omitted",
    }
    assert set(payload["rubric"]) <= set(rg.RUBRIC_KEYS)
    assert payload["rubric"]["operand"] == a["predicate"]["operand"]
    value, _ = score.resolve(ev, a["predicate"]["observation"])
    assert payload["graded_output"] == {"path": a["predicate"]["observation"], "value": value}
    assert set(payload["context_evidence"]) <= set(a["evidence_paths"]) - {"role_output"}
    text = json.dumps(payload)
    for leak in (E04, "E04-P2", "probe_bundles", "expected_trace", "reviewer rejects", "human"):
        assert leak not in text  # no case id, case design, bundle name or grader/authority


# ------------------------------------------------------------------ quotes and verdicts


PAYLOAD = {
    "graded_output": {
        "path": "role_output.reviewer[0]",
        "value": {"text": "The plan's  window\n is wrong here"},
    },
    "context_evidence": {"replay": {"note": "replay of onset passed with exit code 0"}},
}


@pytest.mark.parametrize(
    "quotes, verdict, downgraded, found",
    [
        (["The plan's window is wrong"], rg.PASS, False, "graded_output"),  # whitespace-collapsed
        (["window is wrong here, says the reviewer"], rg.UNDECIDED, True, None),  # fabricated
        (["is wrong"], rg.UNDECIDED, True, None),  # under MIN_QUOTE_CHARS
        (["replay of onset passed"], rg.UNDECIDED, True, "context_evidence"),  # context only
        ([], rg.UNDECIDED, True, None),
    ],
    ids=["verbatim", "fabricated", "too-short", "context-only", "no-quote"],
)
def test_a_pass_without_a_verbatim_graded_output_quote_is_undecided(
    quotes, verdict, downgraded, found
):
    rec = rg.judge(answer(rg.PASS, quotes), PAYLOAD, MODEL)
    assert rec["verdict"] == verdict and rec["model_verdict"] == rg.PASS
    assert rec["downgraded"] is downgraded
    if quotes:
        assert rec["quotes"][0]["found_in"] == found


def test_fail_stays_fail_and_an_unverified_fail_quote_is_flagged():
    rec = rg.judge(answer(rg.FAIL, ["The plan's window is wrong"]), PAYLOAD, MODEL)
    assert rec["verdict"] == rg.FAIL and "fail_quote_unverified" not in rec
    rec = rg.judge(answer(rg.FAIL, ["not in the evidence at all, invented"]), PAYLOAD, MODEL)
    assert rec["verdict"] == rg.FAIL and rec["fail_quote_unverified"] is True
    assert rg.judge(answer(rg.UNDECIDED), PAYLOAD, MODEL)["verdict"] == rg.UNDECIDED


@pytest.mark.parametrize(
    "bad",
    [
        {"verdict": "NOT_OBSERVED", "rationale": "r", "evidence_quotes": []},
        {"verdict": "PASS", "rationale": "r", "evidence_quotes": [], "confidence": 1},
        {"verdict": "PASS", "rationale": "r"},
        {"verdict": "PASS", "rationale": "r", "evidence_quotes": "The plan's window is wrong"},
        ["PASS"],
    ],
    ids=["verdict-vocabulary", "extra-key", "no-quotes", "quotes-not-a-list", "not-an-object"],
)
def test_an_answer_outside_the_forced_tool_schema_is_undecided(bad):
    rec = rg.judge(bad, PAYLOAD, MODEL)
    assert rec["verdict"] == rg.UNDECIDED and rec["reason"].startswith("invalid grader answer")


@pytest.mark.parametrize(
    "receipt, verdict",
    [
        ({"settings": {"temperature": 0}, "model_observed": MODEL}, rg.PASS),
        ({"settings": {"temperature": 0.7}, "model_observed": MODEL}, rg.UNDECIDED),
        ({"settings": {"temperature": 0}, "model_observed": "claude-sonnet-4-6"}, rg.UNDECIDED),
    ],
    ids=["temperature-0-configured-model", "temperature-not-0", "other-model-answered"],
)
def test_the_receipt_must_show_temperature_0_and_the_configured_grader(receipt, verdict):
    rec = rg.judge(answer(rg.PASS, ["The plan's window is wrong"]), PAYLOAD, MODEL, receipt)
    assert rec["verdict"] == verdict


# ------------------------------------------------------------------ frozen mechanics


@pytest.mark.parametrize(
    "cid, name, verdict, mechanical",
    [
        (E03, "metadata ignored", rg.NOT_APPLICABLE, "NOT_APPLICABLE"),
        (E03, "v26-empty-planner-question", rg.FAIL, "FAIL"),  # enforce_guard: empty question
        (E01, "reviewer output rejected", rg.NOT_APPLICABLE, "NOT_APPLICABLE"),
    ],
)
def test_the_frozen_scorer_decides_applicability_and_no_call_is_made(
    frozen, cid, name, verdict, mechanical
):
    cases, score = frozen
    relay = FakeRelay(answer(rg.PASS, ["anything at all, never used"]))
    rec = rg.grade_assertion(assertion(cases, cid), bundle(cases, cid, name), relay, score, MODEL)
    assert relay.calls == []
    assert rec["source"] == "frozen_mechanical" and rec["called"] is False
    assert rec["verdict"] == verdict and rec["frozen_mechanical"] == mechanical


class Refused(Exception):
    gate = "transition"


class Unknown(RuntimeError):
    outcome = "UNKNOWN"


@pytest.mark.parametrize(
    "exc, outcome",
    [
        (Refused("rubric_grader relay refused: role not allowed"), "REFUSED"),
        (Unknown("x"), "UNKNOWN"),
    ],
)
def test_a_failed_relay_call_is_undecided_never_a_verdict(frozen, exc, outcome):
    cases, score = frozen
    relay = FakeRelay(exc)
    rec = rg.grade_assertion(
        assertion(cases, E04), bundle(cases, E04, "reviewer rejects"), relay, score, MODEL
    )
    assert len(relay.calls) == 1 and rec["called"] is True
    assert rec["verdict"] == rg.UNDECIDED and rec["transport_outcome"] == outcome
    assert rec["reason"].startswith("grader call failed")


def test_evidence_over_the_size_budget_is_undecided_without_a_call(frozen, monkeypatch):
    cases, score = frozen
    a, ev = (
        assertion(cases, E06),
        bundle(cases, E06, "plan with the false rationale presented and approved (UC-14)"),
    )
    value, _ = score.resolve(ev, a["predicate"]["observation"])
    small = rg.build_payload(a, ev, value, max_bytes=len(json.dumps(value)) + 2000)
    assert small["context_evidence"] == {} or set(small["context_evidence"]) < {
        "invocation_receipts",
        "controller_trace",
        "harness_observations",
    }
    assert {o["reason"] for o in small["context_omitted"]} == {"over the size budget"}
    with pytest.raises(rg.EvidenceOverBudget):
        rg.build_payload(a, ev, value, max_bytes=100)
    monkeypatch.setattr(rg, "MAX_PAYLOAD_BYTES", 100)
    relay = FakeRelay(answer(rg.PASS, ["never used, never used"]))
    rec = rg.grade_assertion(a, ev, relay, score, MODEL)
    assert relay.calls == [] and rec["verdict"] == rg.UNDECIDED and "budget" in rec["reason"]


# ------------------------------------------------------------------ grader model


def test_grader_model_is_a_config_value_with_the_shared_family_disclosure(monkeypatch):
    assert rg.grader_model(None, {}) == rg.DEFAULT_GRADER_MODEL == "claude-opus-4-8"
    assert rg.grader_model(None, {rg.GRADER_MODEL_ENV: "gpt-5.6-terra"}) == "gpt-5.6-terra"
    assert rg.grader_model("claude-sonnet-5", {rg.GRADER_MODEL_ENV: "gpt-5.6-terra"}) == (
        "claude-sonnet-5"
    )
    same_family = rg.independence(rg.DEFAULT_GRADER_MODEL)
    assert same_family["status"] == "SAME_FAMILY_DIFFERENT_MODEL"
    assert same_family["disclosure"] == rg.SHARED_FAMILY_DISCLOSURE
    other = rg.independence("gpt-5.6-terra")
    assert other["status"] == "DIFFERENT_FAMILY" and "disclosure" not in other
    same = rg.independence(rg.GRADED_MODEL)
    assert same["status"] == "SAME_MODEL" and "not independent" in same["disclosure"]
    # the finding names its kit sources
    text = json.dumps(rg.MODEL_FINDING)
    for source in ("data-policy.md", "probe-log.md", "known-issues.md", "models.md"):
        assert source in text


def test_the_relay_refuses_an_unprobed_grader_model_fail_closed():
    from tools.workflow import kong_roles

    if rg.DEFAULT_GRADER_MODEL in kong_roles.NATIVE_ROSTER:
        pytest.skip("the default grader model has been probed onto the native roster")
    with pytest.raises(ValueError, match="observed native roster"):
        kong_roles.role_models({rg.ROLE: rg.DEFAULT_GRADER_MODEL})


def test_grade_case_makes_one_call_per_rubric_assertion_and_stays_unratified(frozen):
    cases, score = frozen
    firsts = {
        E01: "waiver ignored, release",
        E03: "planner asks for a human answer (OR24-006 sweep)",
        E04: "reviewer rejects",
        E05: "reviewer approves current binding",
        E06: "plan with the false rationale presented and approved (UC-14)",
    }
    for cid, name in firsts.items():
        relay = FakeRelay(answer(rg.UNDECIDED))
        grades = rg.grade_case(cases[cid], bundle(cases, cid, name), relay, score, MODEL)
        assert grades["schema"] == rg.SCHEMA and grades["case_id"] == cid
        assert grades["label"] == rg.LABEL_UNRATIFIED and grades["applied_to_headline"] is False
        assert set(grades["assertions"]) == set(rg.EXPECTED_TARGETS[cid])
        assert grades["calls"] == len(relay.calls) == 1
        assert grades["grader"]["model"] == MODEL and grades["grader"]["temperature"] == 0
        assert grades["independence"]["status"] == "SAME_FAMILY_DIFFERENT_MODEL"


# ------------------------------------------------------------------ end to end via the relay


def load_host():
    spec = importlib.util.spec_from_file_location(
        "kong_relay_host", REPO / "relay" / "kong_relay_host.py"
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class Worker:
    """KongRoles kit-worker stand-in: one scripted envelope per call (no gateway)."""

    def __init__(self, *outputs):
        self.outputs = list(outputs)
        self.calls = []

    def run(self, argv, cwd, env, stdin):
        self.calls.append(json.loads(stdin))
        return {"exit_code": 0, "stdout": json.dumps(self.outputs.pop(0)), "stderr": ""}


def worker_envelope(value, n=1):
    return {
        "sent": True,
        "ok": True,
        "model": "claude-sonnet-4-6",
        "response_id": f"msg_grader_{n}",
        "stop_reason": "tool_use",
        "usage": {"input_tokens": 900, "output_tokens": 80},
        "text": json.dumps(value, sort_keys=True),
        "answer_source": "tool_use.input",
        "tool_use_count": 1,
        "content_types": ["tool_use"],
        "sent_settings": {"temperature": 0.0},
        "sent_keys": [
            "max_tokens",
            "messages",
            "model",
            "system",
            "temperature",
            "tool_choice",
            "tools",
        ],
        "via": "1.1 kong/fake",
        "request_id": "req-fake",
        "latency_ms": 5,
        "sdk": "anthropic-python 0.0",
        "mock": True,
    }


@pytest.mark.parametrize("allowed", [("rubric_grader",), ("verifier",)])
def test_end_to_end_through_the_real_host_relay_as_an_allow_listed_role(frozen, tmp_path, allowed):
    from tools.workflow.kong_roles import PINNED_PRICES, KongRoles
    from tools.workflow.relay_roles import RelayRoles

    cases, score = frozen
    host = load_host()
    graded_model = "claude-sonnet-4-6"  # the only model the fake worker (and the roster) has
    good = answer(rg.PASS, ["definition_ref differs from goal"], "the REJECT names the mismatch")
    worker = Worker(worker_envelope(good))
    python = tmp_path / "python.exe"
    python.write_text("")
    roles = KongRoles(
        worker,
        tmp_path / "ctx",
        budget=1,
        temperature=0.0,
        prices=PINNED_PRICES,
        session_source="response_id",
        data_policy_ack=True,
        kit_python=python,
    )
    audit = host.AuditLog(tmp_path / "audit.jsonl")
    relay = host.Relay(
        roles, run_id="relay-run-g", token="t" * 43, allowed_roles=allowed, audit=audit
    )
    servers = host.serve(relay, ["127.0.0.1"], 0)
    try:
        url = f"http://127.0.0.1:{servers[0].server_address[1]}"
        client = RelayRoles(url, "t" * 43, "relay-run-g", budget=1, model=graded_model)
        rec = rg.grade_assertion(
            assertion(cases, E04),
            bundle(cases, E04, "reviewer rejects"),
            client,
            score,
            graded_model,
        )
    finally:
        for server in servers:
            server.shutdown()
            server.server_close()
    asks = [json.loads(x) for x in (tmp_path / "audit.jsonl").read_text().splitlines()]
    asks = [x for x in asks if x["event"] == "ask"]
    assert len(asks) == 1 and asks[0]["role"] == "rubric_grader"
    if allowed == ("verifier",):  # not allow-listed: refused before forwarding, UNDECIDED
        assert asks[0]["reason_code"] == "role_not_allowed" and worker.calls == []
        assert rec["verdict"] == rg.UNDECIDED and rec["transport_outcome"] == "REFUSED"
        return
    (sent,) = worker.calls
    assert (
        sent["temperature"] == 0.0 and sent["answer_tool"]["name"] == "submit_rubric_grader_answer"
    )
    assert sent["system"].startswith("You grade ONE rubric assertion")
    assert rec["verdict"] == rg.PASS and rec["quotes"][0]["found_in"] == "graded_output"
    assert rec["receipt"]["model_observed"] == graded_model
    assert rec["receipt"]["settings"]["temperature"] == 0.0 and rec["receipt"]["cost_usd"] > 0


# ------------------------------------------------------------------ report column


def executed(cid, assertions):
    return {
        "case_id": cid,
        "run_status": "EXECUTED",
        "scored": {
            "case_id": cid,
            "kind": "injection",
            "run_status": "EXECUTED",
            "oracle_status": "UNRATIFIED_DEV",
            "dimensions": {
                "judgment": "NOT_OBSERVED",
                "containment": "PASS",
                "recovery": "NOT_APPLICABLE",
                "utility": "NOT_APPLICABLE",
            },
            "assertions": assertions,
            "terminal": "NOT_OBSERVED",
        },
    }


def grades_file(frozen, answer_value, model=MODEL):
    cases, score = frozen
    relay = FakeRelay(answer_value)
    return rg.grade_case(cases[E04], bundle(cases, E04, "reviewer rejects"), relay, score, model)


def report(tmp_path, grades=None, ratification=None, raw=None):
    headline = {"J1": "PASS", "J2": "NOT_OBSERVED"}
    d = artifact(tmp_path, E04, score=executed(E04, headline))
    if grades is not None:
        (d / rg.GRADES_FILE).write_text(json.dumps(grades), encoding="utf-8")
    if raw is not None:
        (d / rg.GRADES_FILE).write_text(raw, encoding="utf-8")
    rat = tmp_path / "RATIFICATION.json"
    if ratification is not None:
        rat.write_text(json.dumps(ratification), encoding="utf-8")
    res = cases_report.build(
        admission([row(E04, "injection")]), tmp_path / "cases", None, None, rat
    )
    return headline, res


def test_report_column_is_separate_labelled_unratified_and_never_touches_the_headline(
    tmp_path, frozen
):
    grades = grades_file(frozen, answer(rg.PASS, ["definition_ref differs from goal"]))
    headline, res = report(tmp_path, grades)
    e = by_id(res)[E04]
    assert e["assertions"] == headline and e["assertions"]["J2"] == "NOT_OBSERVED"  # headline
    assert e[wcf.JSON_KEY]["status"] == "NOT_COMPUTED"  # the counterfactual is its own thing
    assert e[rg.JSON_KEY]["status"] == "GRADED"
    assert e[rg.JSON_KEY]["assertions"]["J2"]["verdict"] == rg.PASS
    top = res[rg.JSON_KEY]
    assert top["label"] == rg.LABEL_UNRATIFIED == "UNRATIFIED grader (diagnostic)"
    assert top["applied_to_headline"] is False and top["ratification"]["status"] == "UNRATIFIED"
    assert "no ratification record" in top["ratification"]["reason"]
    (r,) = top["assertions"]
    assert (r["as_frozen"], r["grader"], r["quote_verified"]) == ("NOT_OBSERVED", rg.PASS, True)
    md = cases_report.markdown(res)
    (header,) = [x for x in md.splitlines() if x.startswith("| case | mode |")]
    assert header.rstrip(" |").endswith("UNRATIFIED grader (diagnostic)")
    assert wcf.LABEL in header  # the counterfactual column is still there, unchanged
    (line,) = [x for x in md.splitlines() if x.startswith(f"| {E04} | controller_conformance ")]
    assert line.split(" | ")[6].startswith("J2 PASS")
    assert "## UNRATIFIED grader (diagnostic): frozen rubric assertions" in md
    assert f"| {E04} | J2 | NOT_OBSERVED | PASS | grader | yes |" in md


def test_an_executed_target_without_grades_is_not_graded(tmp_path):
    _, res = report(tmp_path)
    assert by_id(res)[E04][rg.JSON_KEY] == {"status": "NOT_GRADED"}
    assert res[rg.JSON_KEY]["not_graded"] == {E04: "NOT_GRADED"}
    _, res = report(tmp_path / "b", raw="{not json")
    assert by_id(res)[E04][rg.JSON_KEY]["status"] == "UNREADABLE"


def ratification_for(grades, **changes):
    rec = {
        "schema": rg.RATIFICATION_SCHEMA,
        "status": "RATIFIED",
        "ratified_by": "owner",
        "ratified_at": "2026-10-20T00:00:00Z",
        "calibration_report_sha256": "sha256:" + "0" * 64,
        "grader": {k: grades["grader"][k] for k in rg.IDENTITY_KEYS},
    }
    rec.update(changes)
    return rec


def test_a_valid_ratification_record_relabels_the_column_and_nothing_else(tmp_path, frozen):
    grades = grades_file(frozen, answer(rg.PASS, ["definition_ref differs from goal"]))
    headline, res = report(tmp_path, grades, ratification_for(grades))
    assert res[rg.JSON_KEY]["label"] == rg.LABEL_RATIFIED
    assert res[rg.JSON_KEY]["applied_to_headline"] is False
    assert by_id(res)[E04]["assertions"] == headline  # still the frozen headline
    header = [x for x in cases_report.markdown(res).splitlines() if x.startswith("| case | mode |")]
    assert rg.LABEL_RATIFIED in header[0]


@pytest.mark.parametrize(
    "change, reason",
    [
        ({"status": "PROPOSED"}, "status is 'PROPOSED'"),
        ({"ratified_by": None}, "lacks ['ratified_by']"),
        ({"schema": "other"}, "is not simkit.rubric-grader-ratification/1"),
        ({"grader": "claude-opus-4-8"}, "different grader"),
    ],
)
def test_an_invalid_or_mismatched_ratification_keeps_the_column_unratified(
    tmp_path, frozen, change, reason
):
    grades = grades_file(frozen, answer(rg.UNDECIDED))
    _, res = report(tmp_path, grades, ratification_for(grades, **change))
    rat = res[rg.JSON_KEY]["ratification"]
    assert res[rg.JSON_KEY]["label"] == rg.LABEL_UNRATIFIED and reason in rat["reason"]


def test_a_different_grader_or_the_graded_model_itself_is_never_ratified(tmp_path, frozen):
    grades = grades_file(frozen, answer(rg.UNDECIDED))
    other = ratification_for(grades)
    other["grader"]["model"] = "claude-sonnet-5"
    _, res = report(tmp_path, grades, other)
    assert "different grader" in res[rg.JSON_KEY]["ratification"]["reason"]
    same = grades_file(frozen, answer(rg.UNDECIDED), model=rg.GRADED_MODEL)
    _, res = report(tmp_path / "b", same, ratification_for(same))
    assert "same model" in res[rg.JSON_KEY]["ratification"]["reason"]


# ------------------------------------------------------------------ calibration scaffold


def test_labelled_set_covers_every_target_with_controls_and_wrong_outputs(frozen):
    cases, score = frozen
    data = rg.labelled_set(cases.values(), score)
    items = data["items"]
    assert data["unlabelled_bundles_reaching_grader"] == []  # every distinct output labelled
    assert {(i["case_id"], i["assertion_id"]) for i in items} == {
        (c, a) for c, aids in rg.EXPECTED_TARGETS.items() for a in aids
    }
    assert {i["expected"] for i in items} == set(rg.VERDICTS)
    kinds = {i["source"].split(":")[0] for i in items}
    assert kinds == {"frozen_probe_bundle", "synthetic_wrong_output", "synthetic_right_output"}
    assert data["labels_status"].startswith("PROPOSED")
    for i in items:  # every item really reaches the grader through the frozen guard
        a = assertion(cases, i["case_id"], i["assertion_id"])
        assert rg._reaches_grader(a, i["evidence"], score), i["item_id"]


def run(frozen, model_answer):
    cases, score = frozen
    labelled = rg.labelled_set(cases.values(), score)
    relay = FakeRelay(model_answer)
    report_ = rg.run_calibration(labelled, cases.values(), relay, score, MODEL)
    return labelled, relay, report_


def test_calibration_report_measures_agreement_and_pass_without_quote(frozen):
    cases, score = frozen
    labels = {}
    for item in rg.labelled_set(cases.values(), score)["items"]:
        a = assertion(cases, item["case_id"], item["assertion_id"])
        value, _ = score.resolve(item["evidence"], a["predicate"]["observation"])
        labels[json.dumps(value, sort_keys=True)] = item["expected"]

    def perfect(payload):
        value = payload["graded_output"]["value"]
        expected = labels[json.dumps(value, sort_keys=True)]
        quote = max(rg.string_leaves(value), key=len)
        return answer(expected, [quote] if expected == rg.PASS else [])

    labelled, relay, rep = run(frozen, perfect)
    n = len(labelled["items"])
    assert len(relay.calls) == n == rep["items"]  # one call per labelled item
    assert rep["agreement"] == {"matches": n, "of": n, "rate": 1.0}
    assert rep["pass_without_quote"] == {"final": 0, "model_attempted_and_downgraded": 0}
    assert rep["disagreements"] == [] and rep["label"] == rg.LABEL_UNRATIFIED
    assert rep["sign_off"]["status"] == "AWAITING_OWNER_SIGN_OFF"
    assert rep["sign_off"]["no_pass_without_quote"] is True

    # A model that says PASS everywhere without quoting: every PASS is downgraded, so it agrees
    # only where the label is UNDECIDED, and no final PASS lacks a quote.
    labelled, relay, rep = run(frozen, answer(rg.PASS, []))
    undecided = sum(1 for i in labelled["items"] if i["expected"] == rg.UNDECIDED)
    passes = sum(1 for i in labelled["items"] if i["expected"] == rg.PASS)
    assert rep["pass_without_quote"] == {"final": 0, "model_attempted_and_downgraded": n}
    assert rep["agreement"]["matches"] == undecided
    assert rep["confusion_expected_to_got"][rg.PASS] == {rg.UNDECIDED: passes}
    assert len(rep["disagreements"]) == n - undecided

    # Relay failures are counted as errors, never as agreement on a verdict.
    _, _, rep = run(frozen, Unknown("gateway"))
    assert rep["call_or_answer_errors"] == n
    assert rep["agreement"]["matches"] == undecided
