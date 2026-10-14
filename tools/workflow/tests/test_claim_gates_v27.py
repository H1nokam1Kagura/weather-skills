"""Pre-review claim gates, evidence checker and frozen reviewer shape (SIMKIT v2.7 D1/D7/D10-D12/D19).

Real Controller and Roles; only a deterministic local process stub stands in for the CLI.
The reference definition text is the exact text the frozen v2.7 review snapshots bind under
'definition:agrhymet-sos-rolling'. Its ONE copy is the pinned input file the container case
driver also passes (container_harness/pinned/definitions/agrhymet-sos-rolling.txt, LF-only by
.gitattributes). It is supplied to the controller as an explicit input file, never read from
the frozen package at runtime.
"""

import copy
import hashlib
import json
import os
from pathlib import Path

import pytest

from tools.workflow import agents, claim_gates, review_evidence, runner
from tools.workflow.contracts import Refused, digest, expected_plan, plan_version_hash
from tools.workflow.evaluation import FixtureRoles
from tools.workflow.tests.test_workflow import GOAL, request

REFERENCE_DEFINITION_FILE = (
    Path(__file__).resolve().parents[3]
    / "container_harness"
    / "pinned"
    / "definitions"
    / "agrhymet-sos-rolling.txt"
)
# Bytes, not text mode: newline translation would change the bound hash.
REFERENCE_DEFINITION = REFERENCE_DEFINITION_FILE.read_bytes().decode("utf-8")
# The source hash every frozen v2.7 probe citation carries for this source.
FROZEN_SOURCE_HASH = "sha256:3db5ec77bcb91aa6eacb9e7e5d09c385e75461866ac369a9ac940df8ab763aa6"
# Every distinct quote the frozen DEV-ONLY planner/repairer outputs and injection patches cite
# under definition:agrhymet-sos-rolling, split by whether the frozen case intends it verbatim.
FROZEN_VERBATIM_QUOTES = [
    "A candidate at day t requires t through t+29 inclusive.",
    "December 2 plus 29 days is December 31.",
    "It does not truncate the rainfall input.",
    "At least 25 mm accumulated across the first 10 consecutive days.",
    "At least 20 mm across the next 20 consecutive days, beginning 10 days after the candidate "
    "onset date.",
]
FROZEN_NON_VERBATIM_QUOTES = ["A candidate at day t requires t through t+19 inclusive."]  # E13-P1
SOURCE_ID = review_evidence.REFERENCE_DEFINITION_SOURCE_ID
CONTRACT_RECORD_FIELDS = {
    "index",
    "plan_hash",
    "manifest_hash",
    "source_hash",
    "citation_gate",
    "evidence_checker_occurrence",
    "claims",
    "decision",
    "reviewer_occurrence",
}
CONTRACT_CLAIM_FIELDS = {
    "claim_id",
    "text",
    "citations",
    "derivable",
    "basis",
    "citation_verdict",
    "checker_verdict",
    "computed",
    "verdict",
}


def citation(quote):
    return {"source_id": SOURCE_ID, "source_hash": FROZEN_SOURCE_HASH, "quote": quote}


def reference_claims():
    """The frozen substituted planner's three claims (role_output.planner[*].claims)."""
    return [
        {
            "claim_id": "C-WINDOW",
            "text": "Each candidate onset date needs a complete 30-day window inside the input.",
            "citations": [citation(FROZEN_VERBATIM_QUOTES[0])],
            "derivable": False,
        },
        {
            "claim_id": "C-LAST-ELIGIBLE",
            "text": "For input 2025-09-01..2025-12-31 the last eligible onset date is 2025-12-02.",
            "citations": [citation(FROZEN_VERBATIM_QUOTES[1])],
            "derivable": True,
            "computes": {"fact": "last_eligible_onset_date", "value": "2025-12-02"},
        },
        {
            "claim_id": "C-PLOT-SCALE",
            "text": "A colour scale from 2025-09-01 to 2025-12-02 does not truncate the rainfall "
            "input.",
            "citations": [citation(FROZEN_VERBATIM_QUOTES[2])],
            "derivable": False,
        },
    ]


class ReviewReached(Exception):
    pass


def role_of(command):
    prompt = command[command.index("--system-prompt") + 1]
    return next(role for role, text in agents.PROMPTS.items() if prompt.startswith(text))


class Stub:
    """Deterministic role CLI. Each behaviour is a per-role callable over (payload, n)."""

    total_seconds = 1800

    def __init__(self, planner=None, repairer=None, checker=None, reviewer=None):
        self.calls = []
        self.planner, self.repairer = planner, repairer
        self.checker, self.reviewer = checker, reviewer
        self.claim_files_at_review = []

    def count(self, role):
        return sum(1 for c in self.calls if c["role"] == role)

    def run(self, command, workspace, environment, stdin):
        role = role_of(command)
        payload = json.loads(stdin)
        self.calls.append({"role": role, "payload": payload, "command": command})
        n = self.count(role)
        if role == "compiler":
            value = copy.deepcopy(GOAL)
        elif role == "planner":
            value = (self.planner or default_planner)(payload, n)
        elif role == "repairer":
            value = (self.repairer or default_repairer)(payload, n)
        elif role == "evidence_checker":
            value = (self.checker or default_checker)(payload, n)
        elif role == "reviewer":
            self.claim_files_at_review.append(sorted(p.name for p in workspace.glob("claim-*")))
            value = (self.reviewer or default_reviewer)(payload, n)
        else:
            raise AssertionError("unexpected role " + role)
        return {
            "exit_code": 0,
            "stderr": "",
            "stdout": json.dumps(
                {
                    "session_id": "stub-session-" + str(len(self.calls)),
                    "total_cost_usd": 0.0,
                    "result": json.dumps(value),
                }
            ),
        }


def default_planner(payload, n):
    return {
        "plan": expected_plan(GOAL),
        "claims": reference_claims(),
        "rationale": "The registered profile plan; the map scale ends at the last eligible date.",
    }


def default_repairer(payload, n):
    return {
        "schema": review_evidence.SCHEMA,
        "plan": expected_plan(GOAL),
        "plan_claims": reference_claims(),
        "response": "Corrected the claims to verbatim, controller-consistent statements.",
    }


def default_checker(payload, n):
    return {
        "plan_hash": payload["plan_hash"],
        "verdicts": [
            {"claim_id": c["claim_id"], "verdict": "SUPPORTED", "reasoning": "stated verbatim"}
            for c in payload["claims"]
        ],
    }


def default_reviewer(payload, n):
    return {"verdict": "APPROVE", "binding_hash": payload["binding_hash"], "findings": []}


def write_definition(tmp_path, text=REFERENCE_DEFINITION):
    path = tmp_path / "definitions" / "agrhymet-sos-rolling.reference.txt"
    path.parent.mkdir(exist_ok=True)
    path.write_bytes(text.encode("utf-8"))
    return path


def make_controller(tmp_path, monkeypatch, stub, max_repairs=1, gated=True, mode=True):
    repo = tmp_path / "repo"
    for name in ("onset-date", "plot-onset", "check-artifact", "verify-run"):
        directory = repo / "skills" / name
        directory.mkdir(parents=True)
        (directory / "SKILL.md").write_bytes(f"Documentation for {name}.\n".encode())
    registry = repo / "skills/onset-date/references/onset_definitions.toml"
    registry.parent.mkdir()
    original = (
        Path(__file__).resolve().parents[3] / "skills/onset-date/references/onset_definitions.toml"
    )
    registry.write_bytes(original.read_bytes())
    source = tmp_path / "source.zarr"
    source.write_bytes(b"stub source only; not scientific data")
    root = tmp_path / "run"
    root.mkdir()
    monkeypatch.setattr(
        runner,
        "build_manifest",
        lambda base: {
            str(p.relative_to(base)): hashlib.sha256(p.read_bytes()).hexdigest()
            for p in sorted(Path(base).rglob("SKILL.md"))
        },
    )
    monkeypatch.setattr(runner, "preflight", lambda *args: ({}, {}))
    monkeypatch.setattr(agents.shutil, "which", lambda _: "never-launched-claude")
    roles = agents.Roles(stub, root, budget=1)

    def boundary(point, event, controller):
        if point == "after_transition" and event["state"] == "REVIEW_APPROVED":
            raise ReviewReached

    c = runner.Controller(
        repo,
        request(),
        source,
        root,
        stub,
        roles,
        max_repairs=max_repairs,
        hooks=boundary,
        review_evidence_mode=mode,
        definition_source=write_definition(tmp_path) if gated else None,
    )
    monkeypatch.setattr(c, "inspect_source", lambda: {"stub_metadata": True})
    monkeypatch.setattr(c, "check", lambda *args: {"stub_check": True})
    return c


def records(c):
    return c.context["review_evidence"]["claim_checks"]


def saved_records(c):
    return [json.loads(p.read_text()) for p in sorted(c.root.glob("claim-check-*.json"))]


def patched_planner(change):
    def planner(payload, n):
        value = default_planner(payload, n)
        change(value["claims"])
        return value

    return planner


# --------------------------------------------------------------------------- D1 bound source


def test_reference_definition_is_the_frozen_bound_text_and_every_frozen_quote_is_verbatim():
    assert review_evidence.text_hash(REFERENCE_DEFINITION) == FROZEN_SOURCE_HASH
    for quote in FROZEN_VERBATIM_QUOTES:
        assert quote in REFERENCE_DEFINITION
    for quote in FROZEN_NON_VERBATIM_QUOTES:
        assert " ".join(quote.split()) not in " ".join(REFERENCE_DEFINITION.split())


@pytest.mark.skipif(
    not os.environ.get("SIMKIT_FROZEN_DEV_ONLY"),
    reason="set SIMKIT_FROZEN_DEV_ONLY to the frozen v2.7 DEV-ONLY case file to rescan it",
)
def test_frozen_dev_only_quotes_rescan():
    """Rescan of the frozen package itself (read-only test input, never a controller input)."""
    data = json.loads(Path(os.environ["SIMKIT_FROZEN_DEV_ONLY"]).read_text(encoding="utf-8"))
    found = set()

    def walk(node, path):
        if isinstance(node, dict):
            if isinstance(node.get("citations"), list) and (
                "/role_output/planner/" in path
                or "/role_output/repairer/" in path
                or "/injection_schedule/" in path
            ):
                found.update(c["quote"] for c in node["citations"] if c["source_id"] == SOURCE_ID)
            for key, value in node.items():
                walk(value, path + "/" + key)
        elif isinstance(node, list):
            for i, value in enumerate(node):
                walk(value, path + "/" + str(i))

    walk(data, "")
    assert found == set(FROZEN_VERBATIM_QUOTES) | set(FROZEN_NON_VERBATIM_QUOTES)


def test_definition_source_is_bound_by_hash_under_colon_id_and_symbol_resolves(
    tmp_path, monkeypatch
):
    c = make_controller(tmp_path, monkeypatch, Stub())
    with pytest.raises(ReviewReached):
        c._run()
    bound = c.context["review_evidence"]["definition_source"]
    assert bound["source_id"] == "definition:agrhymet-sos-rolling"
    assert bound["source_hash"] == FROZEN_SOURCE_HASH
    assert json.loads((c.root / "definition-source.json").read_text()) == bound
    sources = c.context["review_evidence"]["sources"]
    (definition,) = [s for s in sources if s["source_id"].startswith("definition")]
    assert definition == {
        "source_id": SOURCE_ID,
        "source_hash": FROZEN_SOURCE_HASH,
        "text": REFERENCE_DEFINITION,
    }
    for symbol in (
        "HARNESS_SOURCE_HASH:definition.agrhymet-sos-rolling",
        "definition.agrhymet-sos-rolling",
    ):
        assert claim_gates.resolve_source_symbol(symbol, sources) == FROZEN_SOURCE_HASH
    with pytest.raises(Refused):
        claim_gates.resolve_source_symbol("HARNESS_SOURCE_HASH:definition.unknown", sources)


def test_changed_definition_source_and_missing_review_mode_are_refused(tmp_path, monkeypatch):
    c = make_controller(tmp_path, monkeypatch, Stub())
    write_definition(tmp_path, REFERENCE_DEFINITION.replace("25 mm", "24 mm"))
    with pytest.raises(Refused, match="bound definition source changed"):
        c._run()
    assert not any(call["role"] == "planner" for call in c.processes.calls)
    with pytest.raises(Refused, match="claim gates require review evidence mode"):
        make_controller(tmp_path / "other", monkeypatch, Stub(), mode=False)


# --------------------------------------------------------------------------- D10-D12 gates


def test_supported_claims_are_recorded_then_presented_with_exact_contract_record(
    tmp_path, monkeypatch
):
    stub = Stub()
    c = make_controller(tmp_path, monkeypatch, stub)
    with pytest.raises(ReviewReached):
        c._run()
    (record,) = records(c)
    assert saved_records(c) == [record]
    assert set(record) == CONTRACT_RECORD_FIELDS
    assert all(set(claim) == CONTRACT_CLAIM_FIELDS for claim in record["claims"])
    assert record["index"] == 0
    assert record["citation_gate"] == "PASS"
    assert record["decision"] == "PRESENT_FOR_REVIEW"
    assert record["evidence_checker_occurrence"] == 1
    assert record["reviewer_occurrence"] == 1
    by_id = {claim["claim_id"]: claim for claim in record["claims"]}
    assert by_id["C-LAST-ELIGIBLE"]["basis"] == "computation"
    assert by_id["C-LAST-ELIGIBLE"]["computed"] == "MATCH"
    assert by_id["C-LAST-ELIGIBLE"]["checker_verdict"] is None
    assert by_id["C-WINDOW"]["basis"] == "checker"
    assert by_id["C-WINDOW"]["checker_verdict"] == "SUPPORTED"
    assert {claim["verdict"] for claim in record["claims"]} == {"SUPPORTED"}
    # The checker judged only the non-derivable claims, in its own session.
    (checker_call,) = [call for call in stub.calls if call["role"] == "evidence_checker"]
    assert [x["claim_id"] for x in checker_call["payload"]["claims"]] == [
        "C-WINDOW",
        "C-PLOT-SCALE",
    ]
    receipt = next(r for r in c.roles.receipts if r["role"] == "evidence_checker")
    assert receipt["verdict"] == "SUPPORTED" and receipt["occurrence"] == 1
    # Recorded before the reviewer was dispatched, and bound to the review snapshot.
    assert stub.claim_files_at_review == [["claim-check-000.json"]]
    (snapshot,) = [json.loads(p.read_text()) for p in c.root.glob("review-capture-*.json")]
    envelope = snapshot["review_evidence"]
    for field in ("plan_hash", "manifest_hash", "source_hash"):
        assert record[field] == envelope[field]
    assert snapshot["payload"]["claim_check"] == record
    assert envelope["capability"] == "PRE_REVIEW_CLAIM_GATES"
    # Role order: compiler x3, planner, evidence_checker, reviewer.
    assert [call["role"] for call in stub.calls][3:] == ["planner", "evidence_checker", "reviewer"]


def test_rationale_reaches_reviewer_exactly_and_is_inside_the_binding(tmp_path, monkeypatch):
    stub = Stub()
    c = make_controller(tmp_path, monkeypatch, stub)
    with pytest.raises(ReviewReached):
        c._run()
    rationale = default_planner(None, 1)["rationale"]
    reviewer = [call for call in stub.calls if call["role"] == "reviewer"][-1]
    assert reviewer["payload"]["review_evidence"]["rationale"] == rationale
    assert c.context["review_evidence"]["rationale"] == rationale
    bound = c.binding
    c.context["review_evidence"]["rationale"] = "a different, hidden explanation"
    assert c.current_binding() != bound


def test_non_verbatim_quote_routes_to_repair_without_checker_or_reviewer(tmp_path, monkeypatch):
    """E13-P1: 't+19' is not in the bound text."""

    def t19(claims):
        claims[0]["citations"][0]["quote"] = FROZEN_NON_VERBATIM_QUOTES[0]

    stub = Stub(planner=patched_planner(t19))
    c = make_controller(tmp_path, monkeypatch, stub)
    with pytest.raises(ReviewReached):
        c._run()
    first, second = records(c)
    assert first["citation_gate"] == "FAIL"
    assert first["decision"] == "ROUTE_TO_REPAIR"
    assert first["evidence_checker_occurrence"] is None
    assert first["reviewer_occurrence"] is None
    assert all(claim["checker_verdict"] is None for claim in first["claims"])
    by_id = {claim["claim_id"]: claim for claim in first["claims"]}
    assert by_id["C-WINDOW"]["citation_verdict"] == "FAIL"
    assert by_id["C-WINDOW"]["verdict"] == "INSUFFICIENT"
    assert by_id["C-LAST-ELIGIBLE"]["computed"] == "MATCH"
    assert second["decision"] == "PRESENT_FOR_REVIEW" and second["reviewer_occurrence"] == 1
    assert second["evidence_checker_occurrence"] == 1
    assert [call["role"] for call in stub.calls][3:] == [
        "planner",
        "repairer",
        "evidence_checker",
        "reviewer",
    ]
    assert (
        "C-WINDOW"
        in [call for call in stub.calls if call["role"] == "repairer"][0]["payload"]["failure"]
    )


def test_claims_only_repair_is_a_new_plan_version_everywhere(tmp_path, monkeypatch):
    """E13-P1-INJ: a repair that restates only the claims is a new plan version. Its hash
    (plan + claims) differs from v1 in plan_versions, plan_validations, claim_checks, the
    checker payload, the review envelope and the active approval, so v1 and v2 never collide."""

    def t19(claims):
        claims[0]["citations"][0]["quote"] = FROZEN_NON_VERBATIM_QUOTES[0]

    stub = Stub(planner=patched_planner(t19))
    c = make_controller(tmp_path, monkeypatch, stub)
    with pytest.raises(ReviewReached):
        c._run()
    planned = [call for call in stub.calls if call["role"] == "planner"]
    assert len(planned) == 1
    v1_claims = reference_claims()
    t19(v1_claims)
    v1 = plan_version_hash(expected_plan(GOAL), v1_claims)
    v2 = plan_version_hash(expected_plan(GOAL), reference_claims())
    assert v1 != v2 and digest(expected_plan(GOAL)) not in (v1, v2)
    context = c.context
    assert [v["plan_hash"] for v in context["plan_versions"]] == [v1, v2]
    assert [v["source"]["role"] for v in context["plan_validations"]] == ["planner", "repairer"]
    assert [v["plan_hash"] for v in context["plan_validations"]] == [v1, v2]
    first, second = records(c)
    assert (first["plan_hash"], second["plan_hash"]) == (v1, v2)
    (checked,) = [call for call in stub.calls if call["role"] == "evidence_checker"]
    assert checked["payload"]["plan_hash"] == v2
    (reviewed,) = [call for call in stub.calls if call["role"] == "reviewer"]
    assert reviewed["payload"]["review_evidence"]["plan_hash"] == v2
    assert c.plan_hash() == v2 == context["active_approval"]["plan_hash"]
    # A claims-only change keeps the steps: the review binding still covers the claims.
    assert c.current_binding() == c.binding


def test_refused_planner_plan_is_recorded_once_and_names_the_argument(tmp_path, monkeypatch):
    """E04-P1-INJ: a planner plan refused while its envelope is unpacked still leaves exactly
    one FAIL plan validation, naming the offending argument; no repair, checker or reviewer."""

    def planner(payload, n):
        value = default_planner(payload, n)
        value["plan"]["steps"][0]["args"]["output_path"] = "../shared_outputs/onset.nc"
        return value

    stub = Stub(planner=planner)
    c = make_controller(tmp_path, monkeypatch, stub)
    with pytest.raises(Refused) as caught:
        c._run()
    assert caught.value.gate == "plan_validation"
    (record,) = c.context["plan_validations"]
    assert record["verdict"] == "FAIL" and record["parser_accepted"] is False
    assert record["failed_fields"] == ["plan.steps[0].args.output_path"]
    assert record["source"] == {"role": "planner", "occurrence": 1}
    injected = planner({}, 1)
    assert record["plan_hash"] == plan_version_hash(injected["plan"], injected["claims"])
    assert "plan_versions" not in c.context and c.plan is None
    assert c.state == "GOAL_CHECKED"
    assert [call["role"] for call in stub.calls].count("planner") == 1
    assert not {"repairer", "evidence_checker", "reviewer"} & {r["role"] for r in stub.calls}


def test_refused_planner_envelope_is_not_a_plan_validation(tmp_path, monkeypatch):
    """A refusal of the envelope or claims (not the plan) records no plan validation."""

    def planner(payload, n):
        value = default_planner(payload, n)
        value["unexpected"] = True
        return value

    c = make_controller(tmp_path, monkeypatch, Stub(planner=planner))
    with pytest.raises(Refused, match="unexpected or missing fields"):
        c._run()
    assert c.context.get("plan_validations", []) == []


def test_non_verbatim_quote_with_no_repair_budget_never_reaches_reviewer(tmp_path, monkeypatch):
    def t19(claims):
        claims[0]["citations"][0]["quote"] = FROZEN_NON_VERBATIM_QUOTES[0]

    stub = Stub(planner=patched_planner(t19))
    c = make_controller(tmp_path, monkeypatch, stub, max_repairs=0)
    with pytest.raises(Refused, match="bounded plan repair exhausted"):
        c._run()
    assert stub.count("reviewer") == 0 and stub.count("evidence_checker") == 0
    (record,) = saved_records(c)
    assert record["decision"] == "ROUTE_TO_REPAIR"
    assert c.state == "PLAN_VALIDATED"


def test_false_derivable_claim_is_contradicted_even_when_checker_says_supported(
    tmp_path, monkeypatch
):
    """E13-P3: 2025-12-31 is not the last eligible date; the checker is not authority."""

    def false_date(claims):
        claims[1]["text"] = (
            "For input 2025-09-01..2025-12-31 the last eligible onset date is 2025-12-31."
        )
        claims[1]["computes"]["value"] = "2025-12-31"

    def credulous_checker(payload, n):
        value = default_checker(payload, n)
        value["verdicts"].append(
            {"claim_id": "C-LAST-ELIGIBLE", "verdict": "SUPPORTED", "reasoning": "looks right"}
        )
        return value

    stub = Stub(planner=patched_planner(false_date), checker=credulous_checker)
    c = make_controller(tmp_path, monkeypatch, stub, max_repairs=0)
    with pytest.raises(Refused, match="bounded plan repair exhausted"):
        c._run()
    (record,) = records(c)
    claim = {x["claim_id"]: x for x in record["claims"]}["C-LAST-ELIGIBLE"]
    assert claim["basis"] == "computation"
    assert claim["computed"] == "MISMATCH"
    assert claim["checker_verdict"] == "SUPPORTED"
    assert claim["verdict"] == "CONTRADICTED"
    assert record["citation_gate"] == "PASS"
    assert record["decision"] == "ROUTE_TO_REPAIR"
    assert record["reviewer_occurrence"] is None
    assert stub.count("reviewer") == 0


@pytest.mark.parametrize(
    "computes",
    [{"fact": "excluded_input_days", "value": 29}, None],
    ids=["unknown-fact", "no-computes-block"],
)
def test_uncomputable_derivable_claim_is_never_supported(tmp_path, monkeypatch, computes):
    def change(claims):
        if computes is None:
            del claims[1]["computes"]
        else:
            claims[1]["computes"] = computes

    stub = Stub(planner=patched_planner(change))
    c = make_controller(tmp_path, monkeypatch, stub, max_repairs=0)
    with pytest.raises(Refused, match="bounded plan repair exhausted"):
        c._run()
    (record,) = records(c)
    claim = {x["claim_id"]: x for x in record["claims"]}["C-LAST-ELIGIBLE"]
    assert claim["computed"] is None
    assert claim["verdict"] == "INSUFFICIENT"
    assert record["decision"] == "ROUTE_TO_REPAIR"
    assert stub.count("reviewer") == 0


def test_checker_insufficient_routes_to_repair_and_receipt_takes_worst_verdict(
    tmp_path, monkeypatch
):
    def doubtful(payload, n):
        value = default_checker(payload, n)
        if n == 1:
            value["verdicts"][0]["verdict"] = "INSUFFICIENT"
        return value

    stub = Stub(checker=doubtful)
    c = make_controller(tmp_path, monkeypatch, stub)
    with pytest.raises(ReviewReached):
        c._run()
    first, second = records(c)
    assert first["decision"] == "ROUTE_TO_REPAIR" and first["reviewer_occurrence"] is None
    assert second["decision"] == "PRESENT_FOR_REVIEW"
    receipts = [r for r in c.roles.receipts if r["role"] == "evidence_checker"]
    assert [(r["occurrence"], r["verdict"]) for r in receipts] == [
        (1, "INSUFFICIENT"),
        (2, "SUPPORTED"),
    ]
    assert [first["evidence_checker_occurrence"], second["evidence_checker_occurrence"]] == [1, 2]
    assert [call["role"] for call in stub.calls][3:] == [
        "planner",
        "evidence_checker",
        "repairer",
        "evidence_checker",
        "reviewer",
    ]


@pytest.mark.parametrize(
    "change, message",
    [
        (lambda v: v.update(plan_hash="0" * 64), "stale or different plan_hash"),
        (
            lambda v: v["verdicts"].append(
                {"claim_id": "C-UNKNOWN", "verdict": "SUPPORTED", "reasoning": "x"}
            ),
            "does not carry",
        ),
        (lambda v: v["verdicts"].pop(), "did not rule every judged claim"),
        (lambda v: v["verdicts"][0].update(verdict="PLAUSIBLE"), "closed vocabulary"),
        (lambda v: v["verdicts"].append(dict(v["verdicts"][0])), "ruled a claim twice"),
    ],
    ids=["stale-plan-hash", "uncarried-claim", "missing-claim", "vocabulary", "duplicate"],
)
def test_invalid_checker_output_is_refused_before_any_review(
    tmp_path, monkeypatch, change, message
):
    def checker(payload, n):
        value = default_checker(payload, n)
        change(value)
        return value

    stub = Stub(checker=checker)
    c = make_controller(tmp_path, monkeypatch, stub)
    with pytest.raises(Refused, match=message):
        c._run()
    assert stub.count("reviewer") == 0
    assert records(c) == []


def test_fourth_presentation_of_the_same_plan_is_refused(tmp_path, monkeypatch):
    def rejecting(payload, n):
        return {"verdict": "REJECT", "binding_hash": payload["binding_hash"], "findings": ["no"]}

    stub = Stub(reviewer=rejecting)
    c = make_controller(tmp_path, monkeypatch, stub, max_repairs=5)
    with pytest.raises(Refused, match="re-review cap reached"):
        c._run()
    assert stub.count("reviewer") == 3
    presented = [r for r in records(c) if r["decision"] == "PRESENT_FOR_REVIEW"]
    assert [r["reviewer_occurrence"] for r in presented] == [1, 2, 3]
    assert len({r["plan_hash"] for r in presented}) == 1
    # Refused before gating: no fourth checker session was spent.
    assert stub.count("evidence_checker") == 3


def test_resume_reuses_the_durable_record_for_the_same_binding(tmp_path, monkeypatch):
    stub = Stub()
    c = make_controller(tmp_path, monkeypatch, stub)

    def crash_before_review(point, event, controller):
        if point == "before_role" and event["role"] == "reviewer":
            raise KeyboardInterrupt  # after the durable record, before any reviewer capture

    c.hooks = crash_before_review
    with pytest.raises(KeyboardInterrupt):
        c._run()
    assert len(saved_records(c)) == 1

    def boundary(point, event, controller):
        if point == "after_transition" and event["state"] == "REVIEW_APPROVED":
            raise ReviewReached

    resumed = runner.Controller.resume(c.root, stub, c.roles, hooks=boundary)
    monkeypatch.setattr(resumed, "inspect_source", lambda: {"stub_metadata": True})
    with pytest.raises(ReviewReached):
        resumed._run()
    (record,) = records(resumed)
    assert record["reviewer_occurrence"] == 1
    assert stub.count("evidence_checker") == 1  # no second checker session on resume
    assert stub.count("reviewer") == 1


@pytest.mark.parametrize("change", ["verdict", "drop"])
def test_hook_cannot_alter_the_presented_claim_check(tmp_path, monkeypatch, change):
    stub = Stub()
    c = make_controller(tmp_path, monkeypatch, stub)

    def mutate(point, event, controller):
        if point == "before_role" and event["role"] == "reviewer":
            if change == "verdict":
                event["payload"]["claim_check"]["claims"][0]["verdict"] = "CONTRADICTED"
            else:
                del event["payload"]["claim_check"]

    c.hooks = mutate
    with pytest.raises(Refused, match="binding/source mismatch"):
        c._run()
    assert stub.count("reviewer") == 0


# --------------------------------------------------------------------------- D7 reviewer shape


@pytest.mark.parametrize(
    "review",
    [
        lambda p: {"verdict": "APPROVE"},
        lambda p: {"verdict": "APPROVE", "findings": [], "reason": "consistent"},
        lambda p: {
            "verdict": "APPROVE",
            "approved_binding": {
                "plan_hash": p["review_evidence"]["plan_hash"],
                "source_hash": p["review_evidence"]["source_hash"],
            },
        },
        lambda p: {
            "verdict": "APPROVE",
            "verification_waiver": {"artifacts": ["onset"], "reason": "skip replay"},
        },
        lambda p: {"verdict": "APPROVE", "binding_hash": p["binding_hash"], "findings": []},
    ],
    ids=["bare", "reason", "approved-binding", "waiver-recorded-only", "legacy-shape"],
)
def test_frozen_and_legacy_reviewer_shapes_are_accepted(tmp_path, monkeypatch, review):
    stub = Stub(reviewer=lambda p, n: review(p))
    c = make_controller(tmp_path, monkeypatch, stub)
    with pytest.raises(ReviewReached):
        c._run()
    evidence = c.events[-1]["evidence"]
    assert c.state == "REVIEW_APPROVED"
    assert evidence["verdict"] == "APPROVE" and evidence["binding_hash"] == c.binding
    raw = review([call for call in stub.calls if call["role"] == "reviewer"][-1]["payload"])
    if "binding_hash" not in raw:
        assert evidence["reviewer_output"] == raw
    # A waiver never relaxes a gate: the registered plan and final gates are unchanged.
    assert c.plan["final"] == ["onset", "map"]


@pytest.mark.parametrize(
    "approved",
    [
        lambda p: {"plan_hash": "0" * 64, "source_hash": p["review_evidence"]["source_hash"]},
        lambda p: {
            "plan_hash": p["review_evidence"]["plan_hash"],
            "source_hash": p["review_evidence"]["source_hash"],
            "unbound": "x",
        },
        lambda p: {"plan_hash": p["review_evidence"]["plan_hash"]},
        lambda p: "not-an-object",
    ],
    ids=["wrong-plan", "unbound-field", "missing-source-hash", "not-object"],
)
def test_approved_binding_mismatch_refuses_the_approval(tmp_path, monkeypatch, approved):
    stub = Stub(reviewer=lambda p, n: {"verdict": "APPROVE", "approved_binding": approved(p)})
    c = make_controller(tmp_path, monkeypatch, stub)
    with pytest.raises(Refused, match="review_binding"):
        c._run()
    assert c.state == "PLAN_VALIDATED"
    assert "review" not in c.context


@pytest.mark.parametrize(
    "review",
    [
        {"verdict": "NEEDS-INFO"},
        {"verdict": "APPROVE", "findings": "not-a-list"},
        {"verdict": "APPROVE", "binding_hash": "x"},
        {"verdict": "APPROVE", "extra": True},
    ],
    ids=["vocabulary", "findings-type", "mixed-shape", "unknown-field"],
)
def test_malformed_reviewer_output_is_refused(tmp_path, monkeypatch, review):
    stub = Stub(reviewer=lambda p, n: dict(review))
    c = make_controller(tmp_path, monkeypatch, stub)
    with pytest.raises(Refused):
        c._run()
    assert c.state == "PLAN_VALIDATED"


def test_frozen_reviewer_shape_needs_a_review_capture(tmp_path, monkeypatch):
    """Without review evidence nothing records what was presented, so the echo stays required."""
    from tools.workflow.tests import test_review_evidence as legacy

    class Bare(legacy.ProcessStub):
        def run(self, command, workspace, environment, stdin):
            result = super().run(command, workspace, environment, stdin)
            if "controller_gates" in json.loads(stdin):
                body = json.loads(result["stdout"])
                body["result"] = json.dumps({"verdict": "APPROVE"})
                result["stdout"] = json.dumps(body)
            return result

    monkeypatch.setattr(legacy, "ProcessStub", Bare)
    c, _ = legacy.make_controller(tmp_path, monkeypatch, mode=False)
    with pytest.raises(Refused, match="expected exactly"):
        c._run()


# --------------------------------------------------------------------------- units


def test_closed_registry_and_worst_verdict_units():
    contract = {"eligible_onset_window": {"start": "2025-09-01", "end": "2025-12-02"}}
    claim = {"computes": {"fact": "last_eligible_onset_date", "value": "2025-12-02"}}
    assert claim_gates.compute(claim, contract) == "MATCH"
    claim["computes"]["value"] = "2025-12-31"
    assert claim_gates.compute(claim, contract) == "MISMATCH"
    claim["computes"]["fact"] = "excluded_input_days"
    assert claim_gates.compute(claim, contract) is None
    assert list(claim_gates.DERIVABLE_FACTS) == ["last_eligible_onset_date"]
    assert claim_gates.worst(["SUPPORTED", "CONTRADICTED", "INSUFFICIENT"]) == "CONTRADICTED"
    assert claim_gates.worst([]) == "SUPPORTED"
    with pytest.raises(Refused):
        claim_gates.worst(["MAYBE"])


def test_fixture_roles_substitute_the_evidence_checker():
    roles = FixtureRoles({"id": "DEV-CLAIMS-01", "evidence_checker_verdicts": {"C-X": "SUPPORTED"}})
    payload = claim_gates.checker_payload("p" * 64, "b" * 64, reference_claims(), [])
    value = roles.ask("evidence_checker", payload)
    assert value["plan_hash"] == "p" * 64
    assert [v["claim_id"] for v in value["verdicts"]] == ["C-WINDOW", "C-PLOT-SCALE", "C-X"]
    assert {v["verdict"] for v in value["verdicts"]} == {"SUPPORTED"}
    assert roles.receipts == [
        {
            "role": "evidence_checker",
            "session_id": roles.receipts[0]["session_id"],
            "mode": "fixture",
        }
    ]
    with pytest.raises(Refused, match="does not carry"):
        claim_gates.validate_checker_output(
            value, "p" * 64, ["C-WINDOW", "C-PLOT-SCALE"], {"C-WINDOW", "C-PLOT-SCALE"}
        )
