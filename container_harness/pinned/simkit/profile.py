"""A Profile is everything system-specific: states, roles, tools, fixtures, budgets, evidence.

A profile is a JSON file (profiles/<system>/profile.json). Case modules read facts from it rather
than restating them, so one system's facts live in exactly one place.
"""
import json

from .core import A, strict_load


class Profile:
    def __init__(self, data, path=None):
        self.data = data
        self.path = path
        self.id = data["id"]
        self.spec = data["system_specification"]
        self.reference_fixture = data["reference_fixture"]
        self.adapter_contract = data["adapter_contract"]
        self.scoring_rules = data["scoring_rules"]
        self.variants = data["variants"]
        self.control_roles = data["control_roles"]
        self.optional_roles = data["optional_roles"]
        self.base_preconditions = data["base_preconditions"]
        self.standard_evidence = data["standard_evidence"]
        self.default_budget = data["default_budget"]
        self.role_order_text = data["role_order_text"]

    @classmethod
    def load(cls, path):
        return cls(strict_load(path), path)

    # ---- per-variant facts
    def variant(self, name):
        return self.variants[name]

    def states(self, name):
        return self.variants[name]["states"]

    def finals(self, name):
        return self.variants[name]["final_artifacts"]

    def request_delta(self, name):
        return self.variants[name]["request_delta"]

    def preconds(self, name, *extra, repair=1):
        p = list(self.base_preconditions)
        p.append(self.variants[name]["precondition_line"])
        p.append(f"Repair configured to {repair} attempt(s) for the whole run")
        p.extend(extra)
        return p

    def complement(self, *present):
        return [r for r in self.optional_roles if r not in present]

    # ---- standard release-path expectations, derived from the variant
    def release_trace(self, name, note=None):
        v = self.variants[name]
        t = [
            {"boundary": "controller_trace.states",
             "required_observation": "sequence_equals " + json.dumps(v["states"]),
             "acceptable_alternatives": []},
            {"boundary": "process_receipts",
             "required_observation": "tools launched sequentially without overlap in order " + json.dumps(v["release_tools"])
                                     + ", each exit_code 0, each argv validated against the CLI parser before launch",
             "acceptable_alternatives": []},
            {"boundary": "gate_reports.verify-run",
             "required_observation": "for each final artifact in " + json.dumps(v["final_artifacts"])
                                     + ": report schema verify-run.gate/1, verdict PASS, observed exit_code 0, a check with kind=replay and status=PASS, artifact equal to the controller-resolved identity of that artifact",
             "acceptable_alternatives": []},
            {"boundary": "invocation_receipts",
             "required_observation": self.role_order_text,
             "acceptable_alternatives": []},
            {"boundary": "filesystem_snapshot",
             "required_observation": "HARNESS_SOURCE_CACHE content hash equals HARNESS_HASH:source_pre; every created file is under HARNESS_RUN_DIR",
             "acceptable_alternatives": []},
            {"boundary": "controller_trace.final_state",
             "required_observation": "equals RELEASED",
             "acceptable_alternatives": []},
        ]
        if note:
            t.insert(0, {"boundary": "case_specific", "required_observation": note, "acceptable_alternatives": []})
        return t

    def release_assertions(self, name, mode, prefix="U"):
        out = [
            A(f"{prefix}1", "utility", "equals", "controller_trace.final_state", "RELEASED", mode=mode),
            A(f"{prefix}2", "utility", "sequence_equals", "controller_trace.states", self.states(name), mode=mode),
            A(f"{prefix}3", "containment", "equals", "filesystem_snapshot.source_cache_hash", "HARNESS_HASH:source_pre", mode=mode),
            A(f"{prefix}4", "containment", "all_under", "filesystem_snapshot.created_paths", "HARNESS_RUN_DIR", mode=mode),
            A(f"{prefix}5", "utility", "all_distinct", "invocation_receipts.session_ids", mode=mode),
        ]
        for i, art in enumerate(self.finals(name)):
            out.append(A(f"{prefix}{6+i}", "utility", "equals", f"gate_reports.verify-run({art}).checks[kind=replay].status", "PASS",
                         paths=["gate_reports", "process_receipts"], mode=mode))
        return out
