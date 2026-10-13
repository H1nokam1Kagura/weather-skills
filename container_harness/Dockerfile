# simkit-harness-ctl:<controller git short sha>
#
# Built ONLY by container_harness/run_case_in_container.py, which stages the context:
#   ctl/                 git archive of the controller repo at the named commit (LF, no CRLF)
#   simkit/              pinned SIMKIT inputs (supplement cached-onset-e2e-v2_7-inputs/1 generator,
#                        fixtures/__init__.py, profile.json) plus frozen simkit core/profile/__init__
#   inputs.json          expected sha256 of every staged input, checked at build AND at run
#   requirements-ctl.txt pinned controller dev dependencies
# Run conditions (owner ruling container_route_ruling_2026-10-13) are applied by the launcher:
# default seccomp, never --privileged, --cap-add SYS_PTRACE only, no bind mounts, core dumps off.
FROM python:3.12-slim

RUN apt-get update \
 && apt-get install -y --no-install-recommends strace git \
 && rm -rf /var/lib/apt/lists/*

COPY requirements-ctl.txt /opt/requirements-ctl.txt
RUN pip install --no-cache-dir -r /opt/requirements-ctl.txt \
 && apt-get purge -y git && apt-get autoremove -y && rm -rf /var/lib/apt/lists/*

RUN useradd -m -u 1000 runner \
 && mkdir -p /work /harness-out \
 && chown runner:runner /work \
 && chmod 0700 /harness-out

# Read-only layers: root-owned, no write bit for anyone. Bytecode is compiled at build so the
# traced tree never needs (or attempts) to write __pycache__ under the controller copy.
COPY ctl /opt/ctl
COPY simkit /opt/simkit
COPY inputs.json /opt/inputs.json
RUN python -m compileall -q /opt/ctl/tools /opt/ctl/adapter_support /opt/ctl/container_harness /opt/ctl/skills /opt/simkit/simkit >/dev/null \
 && chown -R root:root /opt/ctl /opt/simkit /opt/inputs.json \
 && chmod -R a-w /opt/ctl /opt/simkit /opt/inputs.json \
 && python -I /opt/ctl/container_harness/verify_inputs.py /opt/inputs.json

ARG CONTROLLER_COMMIT=unknown
LABEL org.simkit.harness="simkit-harness-ctl" org.simkit.controller_commit="${CONTROLLER_COMMIT}"

WORKDIR /
ENTRYPOINT ["/bin/sh", "/opt/ctl/container_harness/entry.sh"]
