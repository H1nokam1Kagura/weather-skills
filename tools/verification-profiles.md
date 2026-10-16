# Experimental synthetic verification profiles

`tools.verification_profiles` executes the local scientific subset of the proposed
`harvest-verification/v1` and `gefs-history/v1` contracts. It is not registered in
the production workflow controller or frozen SIMKIT adapter. No new capability is
signed, and no live readiness or forecast-skill claim follows from these runs.

Run a new demonstration from the repository root using its scientific environment:

```powershell
python -m tools.verification_profiles --synthetic --profile harvest-verification/v1 --run-dir C:/path/to/new-harvest-demo
python -m tools.verification_profiles --synthetic --profile gefs-history/v1 --run-dir C:/path/to/new-history-demo
python -m pytest tests/test_verification_profiles.py
```

Each demonstration creates explicitly synthetic daily rainfall inputs and an exact
request, then calls the real aggregate-temporal, convert-to-totals and verify skills.
It produces 2-, 5- and 10-day totals with baseline comparisons, paired sample counts,
and paired circular time-block bootstrap intervals. Equal weights apply to finite
window/grid cells; RMSE takes the square root after pooling squared errors. Rainfall
verification does not determine crop maturity or safe harvest conditions.

The harness requires daily bounds, exact dates, coordinates, units, initialization,
member counts and a shared finite mask. Tail days outside complete windows are
reported. Historical product identifiers are explicit: an unavailable initialization
remains in the ledger and cannot be replaced with a latest forecast. Its bounded
probe reads cached synthetic Zarr only; it does not measure remote GEFS throughput.

The checkpoint binds the request, source hashes, implementation and core dependency
versions. Three compiler calls, a planner, reviewer and final verifier are explicit
deterministic role substitutions. They test transitions, not independent judgment.
A second scientific execution replays paired scores from the registered sources.
Verified outputs and replay evidence are hash-checked on reuse. One typed worker
failure may retry into a fresh directory; a second blocks. An interrupted attempt
is never reused as verified output. Explicit pause and unclean interruption require
`Controller.run(resume=True)` from the trusted Python harness; CLI demonstrations
always create a new run. The time budget survives checkpoints and recovery.

This is a trusted, in-process synthetic harness, not an untrusted-agent security
boundary. Tools are synchronous. Pause and time checks are cooperative between
operations, not mid-tool OS termination. Input checksums and the request's operator
label are local bindings, not cryptographic authorization. Resource limits bound
inputs; they do not provide a hard total process memory or output-disk quota.

Remaining production work includes real bounded acquisition, live role integration,
OS containment, evidence projection, independent case review, capability re-signing
and admission through the existing SIMKIT gates. The frozen onset corpus and
ratification records are unchanged.
