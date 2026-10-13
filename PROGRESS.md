# PROGRESS — feat/simkit-w3-ci-frozen-snowyowl

Goal: tests stop hard-coding local Windows paths to the frozen v2.7 package (gitignored,
local-only), so GitHub CI (ubuntu) can run them. No skip/xfail on absence.

- [x] Verified local frozen-baseline FINAL-PINS.json sha256 = 153f207e…0402; all 24 pinned files match.
- [x] Old `simkit-v2_7-closure-20261011` root: all 24 pinned files byte-identical to frozen-baseline.
- [x] Fixture `tests/fixtures/frozen-v27.tar.xz`: 24 pinned files + FINAL-PINS.json, xz preset 9e,
      deterministic (mtime 0, sorted, PAX). 258,284 bytes; sha256 0a8ebf05…9925 (rebuild reproduces it).
- [x] Resolver `tests/frozen_v27.py` + repo-root `conftest.py` (binds pytest basetemp for extraction).
- [x] 5 test files point at the resolver; their own hash checks kept. New `tests/test_frozen_v27.py`
      (8 tests: pins, extraction, tamper/missing/absent/env-override/extra-member negatives).
- [x] Windows: 6 touched files, fixture path: 195 passed. Env path + repo-local path: 10 passed each.
      Tampered repo-local copy and missing env path: collection error FrozenPackageError (loud).
- [ ] Linux container run: NOT DONE — Rancher Desktop not running (`docker version`: no
      docker_engine pipe; WSL rancher distros stopped). Unverified on Linux.
