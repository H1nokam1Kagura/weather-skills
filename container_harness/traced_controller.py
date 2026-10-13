"""Traced root of a container run: the real ``python -m tools.workflow`` CLI, roles substituted.

usage (inside the container, cwd /opt/ctl, under the harness strace):
    python -m container_harness.traced_controller run --request R --source S --run-dir D [...]

This process IS the controller (HARNESS_CONTROLLER_PID). Every argument goes to the unmodified
``tools.workflow.__main__.main``; the only substitution is the role factory, so no model or CLI
is ever invoked. The fixture roles are the controller repo's own deterministic doubles
(``tools.workflow.simulation.SyntheticRoles``: compiler -> the request goal, planner -> the
registered plan, reviewer APPROVE, verifier PASS). Processes, gates, replay, checkpoints and the
scientific tools are the real ones. The harness itself writes nothing here.

Dispatch evidence mode binds every role receipt to the controller's attempt by the hashes of
the payload the role received and the answer it returned (runner.py: "dispatch receipt binding
mismatch"). The real model transport records those (agents.Roles.ask); the fixture doubles do
not, so ``BoundFixtureRoles`` adds exactly those two fields, computed from what the double was
actually given and actually returned, with the controller's own ``digest``. Its receipts keep
``mode: fixture``: nothing claims a model was called.
"""

import json
import sys
from pathlib import Path

from tools.workflow import __main__ as cli
from tools.workflow.contracts import digest, request_goal
from tools.workflow.simulation import SyntheticRoles


class BoundFixtureRoles(SyntheticRoles):
    def ask(self, role, payload):
        before = len(self.receipts)
        input_hash = digest(payload)
        answer = super().ask(role, payload)
        if len(self.receipts) == before + 1:
            self.receipts[-1].setdefault("input_hash", input_hash)
            self.receipts[-1].setdefault("output_hash", digest(answer))
        return answer


def _request_goal(argv):
    path = Path(argv[argv.index("--request") + 1])
    return request_goal(json.loads(path.read_text(encoding="utf-8")))


def main():
    goal = _request_goal(sys.argv)

    def fixture_roles(processes, context_dir, budget):  # signature of agents.Roles
        return BoundFixtureRoles(goal)

    cli.Roles = fixture_roles
    cli.main()


if __name__ == "__main__":
    main()
