import pytest
from pydantic import ValidationError

from devflow_ai.policy import PolicyBudget


def test_admission_budget_must_be_explicit_and_nonnegative():
    assert PolicyBudget().max_admission_llm_tokens_per_task is None
    assert PolicyBudget(max_admission_llm_tokens_per_task=0).max_admission_llm_tokens_per_task == 0
    assert PolicyBudget(max_admission_llm_tokens_per_task=100).max_admission_llm_tokens_per_task == 100
    for invalid in (-1, True, 1.5):
        with pytest.raises(ValidationError):
            PolicyBudget(max_admission_llm_tokens_per_task=invalid)
