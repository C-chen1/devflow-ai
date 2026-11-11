import pytest
from pydantic import ValidationError

from devflow_ai.admission import assess_recorded_timeout_limit
from devflow_ai.policy import PolicyBudget, build_default_policy


def test_timeout_limit_counts_initial_attempt_and_stops_at_three():
    budget = build_default_policy().budget
    assert assess_recorded_timeout_limit({"retry_hint": "transient", "consecutive_timeouts": 1}, budget) == "BELOW_LIMIT"
    assert assess_recorded_timeout_limit({"retry_hint": "transient", "consecutive_timeouts": 2}, budget) == "BELOW_LIMIT"
    assert assess_recorded_timeout_limit({"retry_hint": "transient", "consecutive_timeouts": 3}, budget) == "LIMIT_REACHED"


def test_legacy_or_uncertain_count_cannot_authorize_timeout_retry():
    assert assess_recorded_timeout_limit({"retry_hint": "transient", "consecutive_timeouts": 1}, PolicyBudget()) == "UNKNOWN"
    budget = build_default_policy().budget
    assert assess_recorded_timeout_limit({"retry_hint": "transient"}, budget) == "UNKNOWN"
    assert assess_recorded_timeout_limit({"retry_hint": "needs_review", "consecutive_timeouts": 1}, budget) == "NOT_TRANSIENT"
    with pytest.raises(ValidationError):
        PolicyBudget(max_admission_consecutive_timeouts=0)
