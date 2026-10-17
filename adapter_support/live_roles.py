"""CAP-LIVE-ROLE combining transport: live and substituted roles in one controller run.

Live-role wave brief (handoffs/completion-20261012/LIVE-WAVE-BRIEF.md, "Mechanism"):

    Controller.ask
      -> CombinedRoles                (this module)
           -> SubstitutedRoles        (double, no model; adapter_support/role_substitution.py)
           -> RelayRoles-shaped live member (tools/workflow/relay_roles.py; any object with
              the ask(role, payload) / receipts / spent / budget / model / dispatch_* interface)

Four parts:

* ``CombinedRoles`` -- the ``Controller(roles=...)`` object. Its routing table comes from the
  frozen ``agent_roles`` of the case (live / substituted / not_invoked) and from the
  controller's own attempt occurrence (``dispatch_context["occurrence"]``, Controller.ask). A
  role that is both live and substituted (split occurrence: E06-P1, E07-P2, E10-P2 reviewer)
  goes to the double only at the occurrences an after_role ``replace_role_output`` /
  ``json_patch`` entry of the case selects, and to the live member otherwise. A
  ``not_invoked`` role, or a role the case does not list, is refused with no call. There is one
  shared ``receipts`` list: the member's receipt dict object itself is appended, so
  CaseScheduleHooks.rebind's in-place output_hash update reaches the same object the double
  holds, and the controller's ``len(receipts) == before + 1`` check holds. ``spent``,
  ``budget`` and ``model`` delegate to the live member (setters too: resume restore and the
  consumed_model_usd meter write them); so do ``transport_retry_recorder`` and ``processes``.
* ``live_roles_factory(case, goal, run_dir, relay_factory)`` -- the driver hook that builds
  it; the static substitution plan is checked first (AdapterRefusal before any side effect).
* ``confirm_binding_passthrough(run_dir)`` -- CAP-BINDING-PASSTHROUGH ("Confirm that the
  reviewed candidate includes the injected proposal (no hidden proposal outside the review
  binding)"). Read from the controller's own records only: every delivered after_role
  substitution is checked against the recorded input of the invocation that consumes it next.
  A result that does not pass through is recorded as such, never filled.
* ``project_live_receipt(receipt)`` -- the live identity fields of one receipt for the
  invocation_receipts projection; a substituted receipt carries ``mode: substituted``.
"""

import copy
import json
from pathlib import Path

from adapter_support import role_substitution as rs
from tools.workflow.contracts import Refused, digest, plan_version_hash

CAPABILITY = "CAP-LIVE-ROLE"
PASSTHROUGH_CAPABILITY = "CAP-BINDING-PASSTHROUGH"
LIVE, SUBSTITUTED, REFUSED = "live", rs.MODE, "refused"
# Copy of runner.ROLE_GATES (tools/workflow/relay_roles.py keeps the same copy): a refusal
# here takes the role's own frozen gate, as Controller.ask would tag it.
ROLE_GATES = {
    "compiler": "goal_check",
    "planner": "plan_validation",
    "repairer": "plan_validation",
    "reviewer": "review",
    "verifier": "verifier",
    "router": "transition",
}
# Live receipt identity fields carried into the projection (LIVE-WAVE-BRIEF "Receipts"):
# the KongRoles receipt plus relay_id. Absent fields stay absent (never defaulted).
LIVE_RECEIPT_FIELDS = (
    "mode",
    "transport",
    "model_requested",
    "model_observed",
    "settings",
    "usage",
    "request_id",
    "response_id",
    "relay_id",
    "gateway_mock",
    "cost_basis",
)
# B-LR-1: a live receipt is a model result only when the gateway says it was not a mock
# (KongRoles receipt gateway_mock, the kit's is_mock_url). Anything other than exactly False
# (True, missing, null, non-boolean) is refused before the answer reaches the controller.
# The ONLY opt-out is an instance attribute of this name, set to exactly True on the live
# member object itself when it is constructed (the zero-cost fake relays of the tests). No
# production member carries it: RelayRoles / KongRoles never set it, no env var or case field
# is read for it, and case_driver / run_case / invoke_live_role pass nothing for it.
GATEWAY_MOCK_OPT_OUT = "allow_gateway_mock"
# Reviewer output keys that are the controller's own decision inputs, not a proposal carried
# on to the next consumer (accept_review_output, runner.py).
REVIEW_DECISION_KEYS = frozenset({"verdict", "approved_binding"})


def _refuse(message):
    rs._refuse(message)


def native(name):
    return rs.NATIVE_ROLE.get(name, name)


# --------------------------------------------------------------------------- routing


def routing_table(case):
    """{live, substituted, not_invoked, split: {role: [occurrences]}} in native role names, or
    AdapterRefusal for a table no route can honour."""
    roles = case.get("agent_roles") if isinstance(case, dict) else None
    if not isinstance(roles, dict):
        _refuse("the case lists no agent_roles")
    lists = {k: list(roles.get(k) or []) for k in ("live", "substituted", "not_invoked")}
    for key, names in lists.items():
        if not all(isinstance(n, str) and n for n in names):
            _refuse(f"agent_roles.{key} must list role names")
    for key in ("live", "substituted"):
        compilers = set(lists[key]) & set(rs.COMPILER_SELECTORS)
        if compilers and compilers != set(rs.COMPILER_SELECTORS):
            _refuse(f"compilers are {key} all three or not at all")
    live = {native(n) for n in lists["live"]}
    substituted = {native(n) for n in lists["substituted"]}
    not_invoked = {native(n) for n in lists["not_invoked"]}
    if not_invoked & (live | substituted):
        _refuse(
            f"roles listed as not_invoked and invoked: {sorted(not_invoked & (live | substituted))}"
        )
    split = {}
    for spec in case.get("injection_schedule") or []:
        if not isinstance(spec, dict) or spec.get("hook") != "after_role":
            continue
        if spec.get("operation") not in rs.ROLE_OUTPUT_OPS:
            continue
        selector = spec.get("selector") if isinstance(spec.get("selector"), dict) else {}
        name = selector.get("role_or_tool_or_stage")
        role = native(name)
        if role in live and role in substituted:
            if name in rs.COMPILER_SELECTORS:
                _refuse("a compiler split between live and substituted is not routed")
            occurrence = selector.get("occurrence", 1)
            if type(occurrence) is not int or occurrence < 1:
                _refuse(f"split-occurrence entry for {name}: occurrence must be positive")
            split.setdefault(role, set()).add(occurrence)
    unselected = sorted((live & substituted) - set(split))
    if unselected:
        _refuse(
            f"{unselected} are both live and substituted but no after_role entry selects a "
            "substituted occurrence"
        )
    return {
        "live": sorted(live),
        "substituted": sorted(substituted),
        "not_invoked": sorted(not_invoked),
        "split": {role: sorted(occ) for role, occ in sorted(split.items())},
    }


class CombinedRoles:
    """Controller roles over a live member and the substituted double (agents.Roles contract)."""

    def __init__(self, case, table, live=None, double=None):
        self.case_id = case["id"]
        self.table = table
        self.live_member = live
        self.double = double
        if table["live"] and live is None:
            _refuse("the case lists live roles; a live member is required")
        if table["substituted"] and not isinstance(double, rs.SubstitutedRoles):
            _refuse("the case lists substituted roles; this case's substituted double is required")
        if isinstance(double, rs.SubstitutedRoles) and double.case_id != self.case_id:
            _refuse("the substituted double belongs to another case")
        # Read once, from the member's own instance dict (not a class attribute, not a
        # __getattr__ fallback): see GATEWAY_MOCK_OPT_OUT.
        self.allow_gateway_mock = getattr(live, "__dict__", {}).get(GATEWAY_MOCK_OPT_OUT) is True
        self.receipts = []
        self.routes = []
        self.dispatch_context = None
        self.dispatch_observation = None
        self.dispatch_observer = None

    # ---- delegated attributes (the live member owns the budget meter)
    def _meter(self):
        return self.live_member if self.live_member is not None else self.double

    @property
    def spent(self):
        return self._meter().spent

    @spent.setter
    def spent(self, value):
        self._meter().spent = value

    @property
    def budget(self):
        return self._meter().budget

    @budget.setter
    def budget(self, value):
        self._meter().budget = value

    @property
    def model(self):
        return self._meter().model

    @model.setter
    def model(self, value):
        self._meter().model = value

    @property
    def sessions(self):
        out = set()
        for member in (self.live_member, self.double):
            out |= set(getattr(member, "sessions", None) or ())
        return out

    @sessions.setter
    def sessions(self, value):
        # Resume restore: every member refuses a session identity the run already recorded.
        for member in (self.live_member, self.double):
            if member is not None:
                member.sessions = set(value)

    @property
    def transport_retry_recorder(self):
        return getattr(self.live_member, "transport_retry_recorder", None)

    @transport_retry_recorder.setter
    def transport_retry_recorder(self, value):
        if self.live_member is None:
            raise AttributeError("no live member: no transport retry")
        self.live_member.transport_retry_recorder = value

    @property
    def processes(self):
        if self.live_member is None:
            raise AttributeError("no live member: no role processes")
        return self.live_member.processes

    @processes.setter
    def processes(self, value):
        if self.live_member is None:
            raise AttributeError("no live member: no role processes")
        self.live_member.processes = value

    @property
    def unknown_outcomes(self):
        return getattr(self.live_member, "unknown_outcomes", [])

    # ---- routing
    def route(self, role):
        """(member name, reason) for this invocation; nothing is called here."""
        table = self.table
        context = self.dispatch_context
        occurrence = context.get("occurrence") if isinstance(context, dict) else None
        if role in table["not_invoked"]:
            return REFUSED, f"{role} is listed under agent_roles.not_invoked in this case"
        live, substituted = role in table["live"], role in table["substituted"]
        if live and substituted:
            if type(occurrence) is not int:
                return REFUSED, (
                    f"{role} is split by occurrence and the controller supplied no attempt "
                    "occurrence (dispatch evidence mode off)"
                )
            if occurrence in table["split"][role]:
                return SUBSTITUTED, f"occurrence {occurrence} is selected by a schedule entry"
            return LIVE, f"occurrence {occurrence} is not selected by any schedule entry"
        if live:
            return LIVE, "live role"
        if substituted:
            return SUBSTITUTED, "substituted role"
        return REFUSED, f"{role} is not listed in this case's agent_roles"

    def ask(self, role, payload):
        member_name, reason = self.route(role)
        context = self.dispatch_context
        record = {
            "role": role,
            "occurrence": context.get("occurrence") if isinstance(context, dict) else None,
            "attempt_id": context.get("attempt_id") if isinstance(context, dict) else None,
            "route": member_name,
            "reason": reason,
        }
        self.routes.append(record)
        if member_name == REFUSED:
            record["outcome"] = "REFUSED_NO_CALL"
            raise Refused(
                f"{role} has no route in case {self.case_id}: {reason}; no call was made",
                gate=ROLE_GATES.get(role, "transition"),
            )
        member = self.live_member if member_name == LIVE else self.double
        before = len(member.receipts)
        member.dispatch_context = None if context is None else dict(context)
        member.dispatch_observation = None
        member.dispatch_observer = self.dispatch_observer
        try:
            answer = member.ask(role, payload)
        except BaseException as exc:
            record["outcome"] = f"RAISED {type(exc).__name__}"
            raise
        else:
            if member_name == LIVE:
                problem = self.gateway_mock_problem(member.receipts[before:])
                if problem is not None:
                    record["outcome"] = "REFUSED_GATEWAY_MOCK"
                    _refuse(f"live {role} answer refused in case {self.case_id}: {problem}")
            record["outcome"] = "RETURNED"
            return answer
        finally:
            observed = getattr(member, "dispatch_observation", None)
            self.dispatch_observation = None if observed is None else dict(observed)
            member.dispatch_context = None
            member.dispatch_observer = None
            # The member's own receipt objects, appended as they are (never copied), so a
            # rebind on the shared list is the rebind of the member's receipt.
            added = member.receipts[before:]
            self.receipts.extend(added)
            record["receipts"] = [r.get("invocation_id") for r in added]

    def gateway_mock_problem(self, added):
        """Why the live member's answer is not a model result (B-LR-1), or None.

        Every receipt the live member added for this answer must carry gateway_mock exactly
        False; an answer with no receipt has nothing showing a real model answered. With the
        construction-time opt-out (GATEWAY_MOCK_OPT_OUT) a mock receipt is let through, and
        the projection still shows gateway_mock true."""
        if not added:
            return "the live member added no receipt, so nothing shows a real model answered"
        for receipt in added:
            ident = receipt.get("invocation_id") if isinstance(receipt, dict) else None
            if not isinstance(receipt, dict) or "gateway_mock" not in receipt:
                return f"receipt {ident} carries no gateway_mock (MISSING, never assumed False)"
            value = receipt["gateway_mock"]
            if type(value) is not bool:
                return f"receipt {ident} gateway_mock is {value!r}, not a boolean"
            if value and not self.allow_gateway_mock:
                return (
                    f"receipt {ident} has gateway_mock true: a mock gateway answered "
                    f"({receipt.get('cost_basis')!r}); no model was called"
                )
        return None

    def report(self):
        return {
            "capability": CAPABILITY,
            "case_id": self.case_id,
            "allow_gateway_mock": self.allow_gateway_mock,
            "table": copy.deepcopy(self.table),
            "routes": copy.deepcopy(self.routes),
        }


def live_roles_factory(case, goal, run_dir, relay_factory):
    """Driver hook: the Controller roles of a case with live roles (mixed or all live).

    ``relay_factory(run_dir)`` returns the live member (RelayRoles in a container run). The
    substitution plan is mapped (or refused) and the routing table built before the live
    member is created, so a refusal happens before any side effect."""
    plan = rs.substitution_plan(case, goal)
    table = routing_table(case)
    if not table["live"]:
        _refuse("the case lists no live roles; use the substituted roles factory")
    for entry in plan["entries"]:
        role = entry["native_role"]
        if role in table["live"] and role not in table["split"]:  # pragma: no cover - plan refuses
            _refuse(f"schedule entry {entry['index']} would replace a live role's output")
    double = rs.SubstitutedRoles(case, goal, allow_live=True) if table["substituted"] else None
    return CombinedRoles(case, table, live=relay_factory(run_dir), double=double)


# --------------------------------------------------------------------------- receipts


def project_live_receipt(receipt):
    """The live identity fields of one receipt (LIVE-WAVE-BRIEF "Receipts"); a substituted
    receipt carries mode substituted and its transport. Missing fields stay missing."""
    if not isinstance(receipt, dict):
        return {}
    mode = receipt.get("mode")
    if mode == SUBSTITUTED:
        return {k: receipt[k] for k in ("mode", "transport") if k in receipt}
    if mode == LIVE:
        return {k: copy.deepcopy(receipt[k]) for k in LIVE_RECEIPT_FIELDS if k in receipt}
    return {"mode": mode} if "mode" in receipt else {}


# --------------------------------------------------------------------------- passthrough


def _read(path):
    return json.loads(Path(path).read_text(encoding="utf-8"))


def find_value(haystack, needle, path=""):
    """The JSON-pointer path at which ``needle`` occurs verbatim (deep equality) inside the
    recorded input, or None. A string the controller embedded as JSON text (e.g. a repairer's
    ``failure``, the serialized rejected review) is searched too; its path segment is marked
    ``(json)``."""
    if haystack == needle:
        return path or "/"
    if isinstance(haystack, dict):
        items = haystack.items()
    elif isinstance(haystack, list):
        items = enumerate(haystack)
    elif isinstance(haystack, str) and haystack[:1] in ("{", "["):
        try:
            decoded = json.loads(haystack)
        except ValueError:
            return None
        return find_value(decoded, needle, path + "(json)")
    else:
        return None
    for key, value in items:
        found = find_value(value, needle, f"{path}/{key}")
        if found is not None:
            return found
    return None


def _occurrence(receipt, ordinal):
    dispatch = receipt.get("controller_dispatch")
    if isinstance(dispatch, dict) and type(dispatch.get("occurrence")) is int:
        return dispatch["occurrence"]
    return ordinal


def review_proposal(output):
    """The proposal a reviewer output carries on: every finding, the waiver and the reason
    (the verdict and approved_binding are the controller's own decision inputs)."""
    if not isinstance(output, dict):
        return []
    items = []
    for key in sorted(set(output) - REVIEW_DECISION_KEYS):
        value = output[key]
        if key == "findings" and isinstance(value, list):
            items += [{"key": key, "value": v} for v in value]
        elif value not in (None, "", [], {}):
            items.append({"key": key, "value": value})
    return items


def confirm_binding_passthrough(run_dir):
    """CAP-BINDING-PASSTHROUGH over one controller run directory.

    For every receipt rebound by an after_role substitution (CaseScheduleHooks.rebind), in
    order, one record {confirmed, carrier_role, carrier_occurrence, delivered_hash,
    reviewed_hash, ...}:

    * planner / repairer: the carrier is the next reviewer invocation; its controller review
      capture (review-capture-NNN.json) must present review_evidence.plan_hash equal to
      plan_version_hash of the delivered plan and plan_claims.
    * reviewer: the carrier is the next live invocation; every proposal item of the delivered
      review (each finding, the waiver, the reason) must appear verbatim in its recorded input.
    * any other role: no passthrough rule; confirmed null with the reason.

    confirmed is True or False when a check ran, null when nothing could be checked (the
    reason says why). reviewed_hash is null when the carrier does not present the delivered
    value; nothing is filled.
    """
    root = Path(run_dir)
    files = [_read(p) for p in sorted(root.glob("role-[0-9][0-9][0-9]-*.json"))]
    captures = {}
    for path in sorted(root.glob("review-capture-[0-9][0-9][0-9].json")):
        snapshot = _read(path)
        receipt = snapshot.get("receipt")
        if isinstance(receipt, dict) and receipt.get("invocation_id"):
            captures[receipt["invocation_id"]] = (path.name, snapshot)
    ordinals, positions = {}, []
    for record in files:
        role = record["receipt"]["role"]
        ordinals[role] = ordinals.get(role, 0) + 1
        positions.append(_occurrence(record["receipt"], ordinals[role]))
    out = []
    for index, record in enumerate(files):
        receipt = record["receipt"]
        subs = [s for s in receipt.get("substitutions") or [] if s.get("hook") == "after_role"]
        if not subs:
            continue
        role, output = receipt["role"], record["output"]
        result = {
            "capability": PASSTHROUGH_CAPABILITY,
            "substituted_role": role,
            "substituted_occurrence": positions[index],
            "invocation_id": receipt.get("invocation_id"),
            "schedule_indexes": [i for s in subs for i in s.get("schedule_indexes", [])],
            "confirmed": None,
            "carrier_role": None,
            "carrier_occurrence": None,
            "carrier_mode": None,
            "delivered_hash": None,
            "reviewed_hash": None,
        }
        later = list(enumerate(files))[index + 1 :]
        if role in {"planner", "repairer"}:
            if not isinstance(output, dict) or not isinstance(output.get("plan"), dict):
                result["reason"] = f"the delivered {role} output carries no plan"
                out.append(result)
                continue
            result["delivered_hash"] = plan_version_hash(output["plan"], output.get("plan_claims"))
            carrier = next(((j, r) for j, r in later if r["receipt"]["role"] == "reviewer"), None)
            if carrier is None:
                result["confirmed"] = False
                result["reason"] = "not passed through: no reviewer invocation followed"
                out.append(result)
                continue
            j, consumer = carrier
            result.update(
                carrier_role="reviewer",
                carrier_occurrence=positions[j],
                carrier_mode=consumer["receipt"].get("mode"),
            )
            capture = captures.get(consumer["receipt"].get("invocation_id"))
            evidence = capture[1].get("review_evidence") if capture else None
            if not isinstance(evidence, dict) or "plan_hash" not in evidence:
                # No check ran: MISSING evidence, never a negative result (D4 N-BP-1).
                result["confirmed"] = None
                result["reason"] = (
                    "MISSING: no check ran: the reviewer has no review capture presenting a "
                    "plan_hash"
                )
                out.append(result)
                continue
            result["carrier_record"] = capture[0]
            presented = evidence["plan_hash"]
            if presented == result["delivered_hash"]:
                result.update(confirmed=True, reviewed_hash=presented)
            else:
                result.update(
                    confirmed=False,
                    presented_plan_hash=presented,
                    reason="not passed through: the reviewed plan version differs from the "
                    "delivered one",
                )
        elif role == "reviewer":
            proposal = review_proposal(output)
            if not proposal:
                result["reason"] = (
                    "the delivered review carries no proposal beyond its verdict; nothing to "
                    "pass through"
                )
                out.append(result)
                continue
            result["delivered_hash"] = digest([p["value"] for p in proposal])
            carrier = next(((j, r) for j, r in later if r["receipt"].get("mode") == LIVE), None)
            if carrier is None:
                result["confirmed"] = False
                result["reason"] = "not passed through: no live invocation followed"
                out.append(result)
                continue
            j, consumer = carrier
            result.update(
                carrier_role=consumer["receipt"]["role"],
                carrier_occurrence=positions[j],
                carrier_mode=LIVE,
            )
            located = [find_value(consumer["input"], p["value"]) for p in proposal]
            result["carrier_paths"] = located
            missing = sorted({p["key"] for p, at in zip(proposal, located, strict=True) if not at})
            if missing:
                result.update(
                    confirmed=False,
                    not_passed=missing,
                    reason="not passed through: the next live input lacks " + ", ".join(missing),
                )
            else:
                # Every item was found verbatim, so the carried values are the delivered ones.
                result.update(confirmed=True, reviewed_hash=digest([p["value"] for p in proposal]))
        else:
            result["reason"] = f"no passthrough rule for a {role} substitution"
        out.append(result)
    return out
