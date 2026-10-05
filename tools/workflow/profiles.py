"""Registered workflow profiles; new scientific capabilities require reviewed code."""

PROFILES = {
    "onset_map": {"id": "cached-onset/v1", "action": "cached-onset", "final": ["onset", "map"]},
    "onset_data": {"id": "cached-onset-data/v1", "action": "cached-onset-data", "final": ["onset"]},
}


def stages(task):
    chain = [
        "CAPTURED",
        "SOURCE_CHECKED",
        "GOAL_CHECKED",
        "PLAN_VALIDATED",
        "REVIEW_APPROVED",
        "AUTHORIZED",
        "ONSET_CREATED",
        "ONSET_CHECKED",
    ]
    if task == "onset_map":
        chain.append("MAP_CREATED")
    return chain + ["DATA_VERIFIED", "FINAL_VERIFIED", "RELEASED"]
