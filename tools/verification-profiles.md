# Experimental synthetic verification profiles

`tools.verification_profiles` executes the local scientific subset of the proposed
`harvest-verification/v1` and `gefs-history/v1` contracts. The main workflow CLI now
dispatches their lifecycle through `verification`, and a separate SIMKIT adapter
projects their observed evidence. The new capability is unsigned and normal
admission refuses execution. No live readiness or forecast-skill claim follows.

Run a new demonstration from the repository root using its scientific environment:

```powershell
python -m tools.verification_profiles --synthetic --profile harvest-verification/v1 --run-dir C:/path/to/new-harvest-demo
python -m tools.verification_profiles --synthetic --profile gefs-history/v1 --run-dir C:/path/to/new-history-demo
python -m pytest tests/test_verification_profiles.py
python -m tools.workflow verification profiles
python -m tools.workflow verification eval --run-dir C:/path/to/new-admission-check
python -m tools.workflow verification eval --diagnostic --run-dir C:/path/to/new-diagnostic-cases
python -m tools.workflow verification status --run-dir C:/path/to/case/run
python -m tools.workflow verification pause --run-dir C:/path/to/case/run
python -m tools.workflow verification resume --diagnostic --run-dir C:/path/to/case/run
python -m tools.workflow verification report --run-dir C:/path/to/case/run
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
an explicit resume through the lifecycle CLI or `Controller.run(resume=True)`.
Pause requests use separate immutable intent records; only the active owner updates
the checkpoint under its lock. A final boundary check honors pause and deadlines
after the verifier returns. The time budget survives checkpoints and recovery.

The lifecycle `run` command accepts `--request`, `--sources` (a product/role-to-path
JSON map), and a new `--run-dir`. It requires capability admission or an explicit
`--diagnostic` flag. All inputs still require honest synthetic registration. A
captured request/source binding enables later status, pause, resume and projection.
The older demonstration CLI creates fixtures but does not create that lifecycle
capture; use lifecycle or adapter runs for the commands above.

The executable 19-case set is `evals/controller/verification-v1/cases.json`. The
verified committed SIMKIT runner dispatches it without editing frozen onset cases.
Unsigned normal cases yield `NOT_IMPLEMENTED` and CLI exit 2. Explicit diagnostic
cases use distinct `DIAG-` IDs and a separate capability namespace; their proposed
analytic oracles remain unratified. Failures exit 1. `results.json` retains every
execution or admission record and its bound projection. This is not a frozen
onset headline score, live role run or capability signature.

`adapter_support/VERIFICATION-PRODUCERS.json` is the separate review gate. It must
retain its schema, mandatory D4 flag and complete producer mapping. A signature
must cover the current producer files; local modifications also prevent admission.
Prior onset approvals cannot authorize these profiles. The projection validates
request/source/runtime bindings, role evidence hashes, scientific artifacts, replay
hashes and terminal verifier binding. Unobserved OS/live evidence is explicit.

This is a trusted, in-process synthetic harness, not an untrusted-agent security
boundary. Tools are synchronous. Pause and time checks are cooperative between
operations, not mid-tool OS termination. Input checksums and the request's operator
label are local bindings, not cryptographic authorization. Resource limits bound
inputs; they do not provide a hard total process memory or output-disk quota.

Remaining production work includes real bounded acquisition, live role integration,
OS containment, formal independent case review/ratification, capability signing
and admission through the existing SIMKIT gates. The frozen onset corpus and
ratification records are unchanged.
