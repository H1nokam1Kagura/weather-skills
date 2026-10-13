"""System-agnostic case-building primitives.

Nothing here knows about any particular agentic system: states, roles, tools and fixtures come
from a Profile (simkit.profile). These helpers only fix the SHAPE of a case so every system's
case set is validated and scored the same way.
"""
import datetime
import hashlib
import json
import os
import re

HOOKS = ("before_run", "before_role", "after_role", "before_tool", "after_tool", "after_transition")
OPERATIONS = ("replace_role_output", "json_patch", "mutate_fixture", "interrupt", "deny_write",
              "request_transition", "inject_metric")
TERMINAL_OUTCOMES = ("RELEASED", "BLOCKED", "NEEDS_INFO", "INCOMPLETE")
EXECUTION_MODES = ("controller_conformance", "live_agent_containment", "scientific_e2e")
DIMENSIONS = ("judgment", "containment", "recovery", "utility")
ORACLE_STATUSES = ("PROPOSED", "UNRESOLVED")

NO_RECOVERY = {"permitted": False, "actions": [], "max_attempts": 0, "success_predicate": "not applicable"}


def inj(hook, target, occurrence, operation, payload, resume_behavior="once only; not re-applied on resume"):
    """One scheduled injection at a named lifecycle boundary."""
    return {"hook": hook, "selector": {"role_or_tool_or_stage": target, "occurrence": occurrence},
            "operation": operation, "payload": payload, "resume_behavior": resume_behavior}


def A(aid, dim, op, left, right=None, paths=None, mode="controller_conformance"):
    """One machine-adaptable assertion: operator + observation (+ operand)."""
    pred = {"operator": op, "observation": left}
    if right is not None:
        pred["operand"] = right
    return {"id": aid, "dimension": dim, "predicate": pred,
            "evidence_paths": paths or [left.split(".")[0]],
            "required_execution_mode": mode}


def oracle(basis, uncertainties=(), status="PROPOSED"):
    return {"status": status, "spec_basis": basis, "uncertainties": list(uncertainties)}


def containment(primary, terminal, reason, earlier=()):
    return {"primary_boundary": primary, "acceptable_earlier_boundaries": list(earlier),
            "terminal_outcome": terminal, "reason_predicate": reason}


def roles(live=(), substituted=(), not_invoked=()):
    return {"live": list(live), "substituted": list(substituted), "not_invoked": list(not_invoked)}


def dims(judgment, containment_, recovery, utility):
    """Which paired dimensions a case can score. NOT_APPLICABLE is never reported as PASS."""
    return {"judgment": judgment, "containment": containment_, "recovery": recovery, "utility": utility}


class CaseSet:
    """Accumulates cases for one profile; fills the profile's defaults into each case."""

    def __init__(self, profile, base_caps):
        self.profile = profile
        self.base_caps = set(base_caps)
        self.cases = []

    def case(self, **kw):
        p = self.profile
        c = {
            "id": kw["id"], "family_id": kw["family"], "pair_id": kw["pair"], "kind": kw["kind"],
            "extends_case_ids": kw["extends"], "profile": kw["profile"], "execution_mode": kw["mode"],
            "purpose": kw["purpose"], "preconditions": kw["preconditions"],
            "request_delta": p.request_delta(kw["profile"]),
            "agent_roles": kw["roles"], "injection_schedule": kw.get("schedule", []),
            "expected_trace": kw["trace"], "expected_containment": kw["containment"],
            "recovery": kw.get("recovery", NO_RECOVERY), "forbidden_effects": kw["forbidden"],
            "required_evidence": kw.get("evidence", p.standard_evidence), "assertions": kw["assertions"],
            "oracle": kw["oracle"], "adapter_requirements": sorted(set(kw["caps"]) | self.base_caps),
            "budget": dict(p.default_budget, **kw.get("budget", {})),
            "dimension_applicability": kw["dims"],
        }
        self.cases.append(c)
        return c

    def ids_where(self, pred):
        return sorted(c["id"] for c in self.cases if pred(c))


def sha256_file(path):
    with open(path, "rb") as f:
        return hashlib.sha256(f.read()).hexdigest()


def no_overwrite_path(path):
    """Return path, or path with a timestamp suffix if it already exists. Never overwrites."""
    if not os.path.exists(path):
        return path
    stem, ext = os.path.splitext(path)
    return f"{stem}.{datetime.datetime.now().strftime('%Y%m%dT%H%M%S')}{ext}"


_TS = re.compile(r"\.(\d{8}T\d{6})(\.[^.\\/]+)$")


def newest_record(files):
    """The most recent of the records no_overwrite_path produced, decided by NAME, never by mtime: a fresh clone gives
    every file the checkout time, so mtime picks arbitrarily. The un-suffixed file is the first; later re-runs carry
    .YYYYMMDDTHHMMSS before the extension. Returns None for an empty list."""
    files = list(files)
    if not files:
        return None

    def key(p):
        m = _TS.search(os.path.basename(p))
        return (m.group(1) if m else "", os.path.basename(p))
    return max(files, key=key)


def emit_json(path, obj):
    """Write strict UTF-8 JSON (no NaN/Infinity) to a never-overwriting path; return (path, sha256)."""
    out = no_overwrite_path(path)
    with open(out, "w", encoding="utf-8", newline="\n") as f:
        json.dump(obj, f, allow_nan=False, ensure_ascii=False, indent=2)
        f.write("\n")
    return out, sha256_file(out)


def strict_load(path):
    def bad(x):
        raise ValueError(f"non-finite constant {x} in {path}")
    with open(path, encoding="utf-8") as f:
        return json.load(f, parse_constant=bad)
