# PROGRESS — feat/simkit-w3-ci-frozen-snowyowl

Goal: tests stop hard-coding local Windows paths to the frozen v2.7 package (gitignored,
local-only), so GitHub CI (ubuntu) can run them. No skip/xfail on absence.

- [x] Verified local frozen-baseline FINAL-PINS.json sha256 = 153f207e…0402; all 24 pinned files match.
- [x] Old `simkit-v2_7-closure-20261011` root: all 24 pinned files byte-identical to frozen-baseline.
- [ ] Fixture `tests/fixtures/frozen-v27.tar.xz` (24 pinned files + FINAL-PINS.json, xz preset 9e).
- [ ] Resolver `tests/frozen_v27.py` + root `conftest.py` (binds pytest basetemp for extraction).
- [ ] Point 5 test files at the resolver (keep their own hash checks).
- [ ] Run touched tests on Windows (fixture path, env path, repo-local path, tamper negatives).
- [ ] Linux run in python:3.12-slim container (if docker works).
