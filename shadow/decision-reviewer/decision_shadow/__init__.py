"""Throwaway shadow-only decision reviewer. Logs what a fast decision model WOULD have said;
never blocks, changes or delays a verdict. Delete the whole shadow/decision-reviewer folder to remove."""
from .backends import BACKENDS, BackendUnavailable, score
from .shadow import record_outcome, shadow_score, shadow_score_background

__all__ = ["BACKENDS", "BackendUnavailable", "score", "shadow_score", "shadow_score_background",
           "record_outcome"]
