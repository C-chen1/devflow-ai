import pytest
from pydantic import ValidationError
from uuid import uuid4

from devflow_ai.enums import FailureCategory
from devflow_ai.policy import PolicyBudget
from devflow_ai.recovery_policy import RecoveryFacts, assess_recovery_policy, public_failure_signature, semantic_failure_signature
from devflow_ai.supervisor import VerificationFailureProposal, resolve_verification_failure_evidence
from devflow_ai.verifier import SemanticReviewDraft, TestComparison as Comparison, parse_pytest_junit_report


def facts(**updates):
    return RecoveryFacts.model_validate({"category": "IMPLEMENTATION_FAILURE", "retries_started": 0,
        "signature_occurrences": 1, "rediagnoses_started": 0, "has_compliant_plan": True,
        "has_new_information_or_strategy": True, "transient_retry_safe": False, **updates})


def test_recovery_counts_current_signature_and_only_extra_runs():
    budget = PolicyBudget(max_retries_per_task=3, max_same_signature_occurrences=3)
    assert assess_recovery_policy(budget, facts(retries_started=2, signature_occurrences=2)).decision == "allow_recovery"
    result = assess_recovery_policy(budget, facts(retries_started=3, signature_occurrences=3))
    assert result.decision == "escalate" and set(result.reasons) == {"retry_limit", "signature_limit"}
    result = assess_recovery_policy(PolicyBudget(max_retries_per_task=0, max_same_signature_occurrences=1), facts())
    assert set(result.reasons) == {"retry_limit", "signature_limit"}


def test_unknown_and_transient_need_their_distinct_conditions():
    budget = PolicyBudget(max_rediagnose_attempts=0)
    result = assess_recovery_policy(budget, facts(category=FailureCategory.UNKNOWN_FAILURE))
    assert "rediagnose_limit" in result.reasons
    result = assess_recovery_policy(budget, facts(category=FailureCategory.TRANSIENT_EXECUTION_FAILURE,
                                                   has_new_information_or_strategy=False, transient_retry_safe=True))
    assert result.decision == "allow_recovery"
    result = assess_recovery_policy(budget, facts(category=FailureCategory.TRANSIENT_EXECUTION_FAILURE,
                                                   has_new_information_or_strategy=False, transient_retry_safe=False))
    assert "transient_retry_not_safe" in result.reasons
    assert "no_information_gain" in assess_recovery_policy(budget, facts(has_new_information_or_strategy=False)).reasons
    assert "no_compliant_plan" in assess_recovery_policy(budget, facts(has_compliant_plan=False)).reasons


def test_unknown_rediagnosis_history_does_not_pretend_to_be_zero_or_over_limit():
    budget = PolicyBudget(max_rediagnose_attempts=2)
    unknown = facts(category=FailureCategory.UNKNOWN_FAILURE, rediagnoses_started=None)
    result = assess_recovery_policy(budget, unknown)
    assert result.decision == "undetermined" and result.reasons == ("rediagnose_count_unknown",)
    exhausted = assess_recovery_policy(budget, unknown.model_copy(update={"retries_started": 3}))
    assert exhausted.decision == "escalate" and "retry_limit" in exhausted.reasons


def test_zero_rediagnosis_budget_is_exhausted_even_when_count_is_unknown():
    unknown = facts(category=FailureCategory.UNKNOWN_FAILURE, rediagnoses_started=None)
    result = assess_recovery_policy(PolicyBudget(max_rediagnose_attempts=0), unknown)
    assert result.decision == "escalate" and result.reasons == ("rediagnose_limit",)
    result = assess_recovery_policy(PolicyBudget(max_rediagnose_attempts=1), unknown)
    assert result.decision == "undetermined" and result.reasons == ("rediagnose_count_unknown",)
    assert assess_recovery_policy(PolicyBudget(max_rediagnose_attempts=1),
                                  facts(category=FailureCategory.UNKNOWN_FAILURE, rediagnoses_started=0)).decision == "allow_recovery"
    result = assess_recovery_policy(PolicyBudget(max_rediagnose_attempts=1),
                                    facts(category=FailureCategory.UNKNOWN_FAILURE, rediagnoses_started=1))
    assert result.decision == "escalate" and result.reasons == ("rediagnose_limit",)


def test_recovery_rejects_invalid_counts():
    with pytest.raises(ValidationError):
        facts(signature_occurrences=0)
    with pytest.raises(ValidationError):
        facts(retries_started=-1)
    with pytest.raises(ValidationError):
        facts(has_compliant_plan="yes")


def test_failure_classification_cannot_reference_unprovided_or_duplicate_evidence():
    proposal = VerificationFailureProposal(category="IMPLEMENTATION_FAILURE", reason="Failed check",
                                           evidence_refs=("public_tests",))
    observations = {"public_tests": {"outcome": "failed"}}
    assert resolve_verification_failure_evidence(proposal, observations) == (observations["public_tests"],)
    for references in (("missing",), ("public_tests", "public_tests")):
        with pytest.raises(ValueError, match="distinct"):
            resolve_verification_failure_evidence(proposal.model_copy(update={"evidence_refs": references}), observations)


def test_public_failure_signature_uses_case_and_actual_message_without_guessing_missing_details():
    def report(message):
        xml = ('<testsuite tests="1" failures="1" errors="0" skipped="0">'
               f'<testcase classname="tests.test_app" name="test_value"><failure message="{message}"/>'
               '</testcase></testsuite>').encode()
        return parse_pytest_junit_report(xml, tool_call_id=uuid4(), command_name="pytest")
    comparison = Comparison(target_result="failed", regression_result="passed", target_issues=("tests.test_app::test_value",))
    first = public_failure_signature(report("assert 1 == 2"), comparison)
    assert first == public_failure_signature(report("assert 1  == 2"), comparison)
    assert first != public_failure_signature(report("assert 1 == 3"), comparison)
    assert public_failure_signature(report(""), comparison) is None


def test_semantic_failure_signature_distinguishes_evidence_and_reason_without_exposing_them():
    citation = {"source": "read_file", "tool_call_id": uuid4(), "path": "app.py",
                "snippet": "return 7", "reason": "Hard coded value"}
    review = SemanticReviewDraft(outcome="REVIEW", result="failed", reason="Unexpected hard coding", citations=(citation,))
    first = semantic_failure_signature(review)
    assert first.startswith("semantic-v1:") and "return 7" not in first
    assert first == semantic_failure_signature(review.model_copy(update={"citations": tuple(reversed(review.citations))}))
    assert first != semantic_failure_signature(review.model_copy(update={"reason": "Different defect"}))
