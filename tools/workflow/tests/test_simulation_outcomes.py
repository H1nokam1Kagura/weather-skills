import pytest

from tools.workflow.simulation import expected_injected_stop


@pytest.mark.parametrize("scenario", ["tamper", "denied"])
def test_unrelated_failure_cannot_pass_negative_journey(scenario):
    assert not expected_injected_stop(scenario, "CAPTURED", "source gate failed", False)
    assert not expected_injected_stop(scenario, "AUTHORIZED", "unrelated error", True)


def test_expected_failure_requires_injection_and_exact_stage():
    error = "artifact changed since its producing step: onset"
    assert expected_injected_stop("tamper", "ONSET_CHECKED", error, True)
    assert not expected_injected_stop("tamper", "ONSET_CHECKED", error, False)
    assert not expected_injected_stop("tamper", "RELEASED", error, True)
    assert expected_injected_stop(
        "denied", "AUTHORIZED", "injected write denial; no fallback authorized", True
    )
