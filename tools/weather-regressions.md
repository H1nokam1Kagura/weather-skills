# Weather tool regression and optimization checks

From the repository root, using its installed development Python environment:

```powershell
python tools/run_weather_regressions.py --output-dir C:/weather-checks/run-001
```

Use a new output directory for each run. The runner executes fetch, aggregation,
rate-to-total conversion, verification and controller recovery tests in one
process. It retains JUnit results, the full log, package versions, source hashes
and the ten slowest tests in `summary.json`. Its exit code is pytest's exit code;
the recovery suite is included when present in the checkout, otherwise it is
explicitly listed in `unavailable_suites` and no recovery pass is claimed.
skips are reported explicitly. Install the optional `xskillscore` package in the
test environment to enable its independent CRPS cross-check. The separate
pairwise-formula CRPS tests run without that optional package.

The synthetic window pipeline runs the actual aggregate → totals → verify chain
for 2-, 5- and 10-day periods, checks analytic totals and bias, and checks that
partial trailing intervals are excluded. Verification also refuses mismatched
or one-sided aggregation metadata. The direct-read fixture exercises a local
Zarr catalog through the real fetcher with exact date and value checks.

For optimization across tools:

1. Reuse a validated bounded fetch across aggregation periods, rather than
   downloading each period separately. Reuse only complete, provenance-matching
   outputs after interruption.
2. Keep date/bbox/variable selection ahead of loading. Catalog reads now use
   direct backend indexing consistently with CHIRPS; historical downloads still
   need a representative bounded timing probe.
3. Use the recorded slowest-test durations to identify local hotspots. Compare
   runs on the same host, inputs, dependencies and cache conditions. These test
   durations are not evidence of remote GEFS throughput.
4. Preserve scientific checks while optimizing: exact units, interval support,
   missing-value behavior, coordinates and values must remain equivalent.

These are software regressions using synthetic data and a mocked catalog.
Recovery tests use fake roles/processes. They do not replay a hosted agent,
reproduce historical forecast skill, diagnose hosted worker crashes, or establish
the reported direct-read speedup. The controlled production profiles remain
cached onset only. Keep real session exports outside the repository.
