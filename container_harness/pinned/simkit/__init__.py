"""simkit: reusable fault-injection evaluation machinery for agentic pipelines.

core      -- case-shape primitives and strict JSON emit (system-agnostic)
profile   -- loads a system profile (states, roles, tools, fixtures)
validate  -- generic dataset/manifest validator driven by a requirements block
adapter   -- the capability interface a harness must implement per system
runner    -- dispatches cases to an adapter; unimplemented capabilities => NOT_IMPLEMENTED
score     -- per-dimension scoring that never turns missing evidence into PASS
"""
