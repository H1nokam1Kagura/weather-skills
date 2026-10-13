# simkit-harness-ctl:<controller git short sha>
#
# Built ONLY by `container_harness/launch.py build`, which stages the context:
#   ctl/                 git archive of the controller repo at HEAD
#   simkit/              container_harness/pinned/ raw bytes: supplement cached-onset-e2e-v2_7-inputs/1
#                        generator, fixtures/__init__.py, profile.json; frozen v2.7 simkit
#                        __init__/core/profile/score (FINAL-PINS hashes)
#   inputs.json          expected sha256 of every pinned input, checked on the host, at build AND
#                        at run (from container_harness/pinned_inputs.json)
#   requirements-ctl.txt pinned controller dev dependencies
# Run conditions (owner ruling container_route_ruling_2026-10-13) are applied by launch.py run:
# default seccomp, never --privileged, --cap-add SYS_PTRACE only, no bind mounts, core dumps off,
# no network, docker-init as PID 1.
FROM python:3.12-slim

RUN apt-get update \
 && apt-get install -y --no-install-recommends strace git \
 && rm -rf /var/lib/apt/lists/*

COPY requirements-ctl.txt /opt/requirements-ctl.txt
RUN pip install --no-cache-dir -r /opt/requirements-ctl.txt \
 && apt-get purge -y git && apt-get autoremove -y && rm -rf /var/lib/apt/lists/*

# Natural Earth shapes plot-onset draws (coastline 10m; BORDERS = admin_0 boundary lines, which
# cartopy 0.26 auto-scales: 10m was requested on the canary extent, so all three scales are seeded),
# fetched at BUILD time into a read-only pre-existing data dir: run containers have no network,
# and a host with a warm cartopy cache would otherwise behave differently from this image.
ENV CARTOPY_DATA_DIR=/opt/cartopy-data
RUN mkdir -p /opt/cartopy-data \
 && python -c "import cartopy, cartopy.io.shapereader as s; cartopy.config['data_dir'] = '/opt/cartopy-data'; s.natural_earth('10m', 'physical', 'coastline'); [s.natural_earth(r, 'cultural', 'admin_0_boundary_lines_land') for r in ('10m', '50m', '110m')]" \
 && chmod -R a-w /opt/cartopy-data

RUN useradd -m -u 1000 runner \
 && mkdir -p /work /harness-out \
 && chown runner:runner /work \
 && chmod 0700 /harness-out

# Read-only layers: root-owned, no write bit for anyone. Bytecode is compiled at build so the
# traced tree never needs (or attempts) to write __pycache__ under the controller copy.
COPY ctl /opt/ctl
COPY simkit /opt/simkit
COPY inputs.json /opt/inputs.json
RUN (python -m compileall -q /opt/ctl/tools /opt/ctl/adapter_support /opt/ctl/container_harness /opt/ctl/skills /opt/simkit/simkit >/dev/null \
     || echo "compileall reported errors (those modules run uncompiled)") \
 && chown -R root:root /opt/ctl /opt/simkit /opt/inputs.json \
 && chmod -R a-w /opt/ctl /opt/simkit /opt/inputs.json \
 && python -I /opt/ctl/container_harness/verify_inputs.py /opt/inputs.json \
 && python -I -B -c "import sys; sys.path.insert(0, '/opt/ctl/tests'); import frozen_v27 as f; from pathlib import Path; Path('/opt/frozen-v27').mkdir(); print(f.extract_fixture(dest=Path('/opt/frozen-v27')))" \
 && python -m compileall -q /opt/frozen-v27/simkit >/dev/null \
 && chown -R root:root /opt/frozen-v27 && chmod -R a-w /opt/frozen-v27

# Frozen v2.7 package (24 files + FINAL-PINS.json), extracted from the committed fixture
# tests/fixtures/frozen-v27.tar.xz and verified against FINAL_PINS_SHA256 and every file pin at
# build; tests/frozen_v27.py re-verifies it on every use (SIMKIT_FROZEN_V27 never falls through).
ENV SIMKIT_FROZEN_V27=/opt/frozen-v27

ARG CONTROLLER_COMMIT=unknown
LABEL org.simkit.harness="simkit-harness-ctl" org.simkit.controller_commit="${CONTROLLER_COMMIT}"

WORKDIR /
ENTRYPOINT ["/bin/sh", "/opt/ctl/container_harness/entry.sh"]
