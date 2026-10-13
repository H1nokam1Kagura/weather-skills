"""Partial observations at actual controller launch/stop boundaries on Windows.

No enumeration or signaling occurs here. Known descendants are observed only
after explicit registration; that subset is never an exhaustive target list.
"""

from dataclasses import asdict

from .signal_capture import ObservationError, OwnedTreeCapture, observe


class ControllerProcessObserver:
    exhaustive_signal_capture = False

    def __init__(self):
        self.scopes = {}

    def register_descendant(self, root_pid, descendant_pid):
        return self.scopes[root_pid].register_descendant(descendant_pid)

    def __call__(self, event, proc, details):
        if event == "launch":
            self.scopes[proc.pid] = OwnedTreeCapture(proc)
        scope = self.scopes.get(proc.pid)
        if scope is None:
            raise ObservationError("No observed root identity for this process")
        result = {
            "root_identity": asdict(scope.root),
            "root_identity_observed_now": False,
            "known_descendants_observed_now": [],
            "unavailable_known_descendants": [],
            "descendant_set_complete": False,
            "actual_signal_targets": None,
            "exhaustive_signal_capture": False,
        }
        if event in {"launch", "stop_request"}:
            if observe(proc.pid) != scope.root:
                raise ObservationError("Observed root identity changed")
            result["root_identity_observed_now"] = True
            for identity in scope.known.values():
                if identity == scope.root:
                    continue
                try:
                    ancestry = scope._ancestry(identity)
                except ObservationError:
                    result["unavailable_known_descendants"].append(asdict(identity))
                else:
                    result["known_descendants_observed_now"].append({
                        "identity": asdict(identity),
                        "ancestry": [asdict(item) for item in ancestry],
                    })
        # Backend return codes prove only command outcomes. They are never
        # promoted to a process target list or a fresh parent observation.
        return result
