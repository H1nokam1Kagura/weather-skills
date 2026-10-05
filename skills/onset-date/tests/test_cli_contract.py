"""Parse the repaired rehearsal commands without opening data or running analysis."""

import pytest
from conftest import load_skill


def test_onset_command_takes_window_from_input_and_refuses_fetch_flags():
    parser = load_skill("onset-date", "onset_date").onset_date.parser
    args = [
        "-i",
        "daily.zarr",
        "-o",
        "onset.zarr",
        "--variable",
        "precip",
        "--definition-ref",
        "agrhymet-sos-rolling",
    ]
    parsed = parser.parse_args(args)
    assert parsed.definition_ref == "agrhymet-sos-rolling"
    for flag in ("--start-time", "--end-time"):
        with pytest.raises(SystemExit) as exc:
            parser.parse_args([*args, flag, "2025-09-01"])
        assert exc.value.code == 2


def test_final_gate_command_preserves_all_external_input_directories():
    parser = load_skill("verify-run", "verify_run").verify_run.parser
    parsed = parser.parse_args(
        [
            "--input",
            "artifacts/onset.png",
            "--require-replay",
            "--search-dir",
            "cached rainfall",
            "--search-dir",
            "artifacts",
        ]
    )
    assert parsed.require_replay is True
    assert parsed.search_dir == ["cached rainfall", "artifacts"]


def test_check_artifact_accepts_explicit_observed_window():
    parser = load_skill("check-artifact", "check_artifact").check_artifact.parser
    parsed = parser.parse_args(
        [
            "--input",
            "onset.zarr",
            "--bbox",
            "5/36.5/-5/42",
            "--start-time",
            "2025-09-01",
            "--end-time",
            "2025-12-31",
        ]
    )
    assert parsed.start_time == "2025-09-01"
    assert parsed.end_time == "2025-12-31"
