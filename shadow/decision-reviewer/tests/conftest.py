import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))


@pytest.fixture
def shadow_log(tmp_path, monkeypatch):
    """Isolated log file; shadow env cleared so nothing leaks between tests."""
    log = tmp_path / "decisions.jsonl"
    monkeypatch.setenv("WS_DECISION_SHADOW_LOG", str(log))
    for var in (
        "WS_DECISION_SHADOW",
        "WS_DECISION_SHADOW_LOG_TEXT",
        "KEV_URL",
        "KEV_API_KEY",
        "CLM_URL",
        "CLM_API_KEY",
    ):
        monkeypatch.delenv(var, raising=False)
    return log
