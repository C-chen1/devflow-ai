"""Policy checks for a proposed Supervisor recovery; no state changes occur here."""
from typing import Literal
from hashlib import sha256
import json

from pydantic import BaseModel, ConfigDict, Field

from devflow_ai.enums import FailureCategory
from devflow_ai.policy import PolicyBudget
from devflow_ai.verifier import CheckResult, SemanticReviewDraft, TestComparison, TestSuiteReport, TestCaseStatus


class RecoveryFacts(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    category: FailureCategory
    retries_started: int = Field(ge=0, strict=True)
    signature_occurrences: int = Field(ge=1, strict=True)
    rediagnoses_started: int | None = Field(default=None, ge=0, strict=True)
    has_compliant_plan: bool = Field(strict=True)
    has_new_information_or_strategy: bool = Field(strict=True)
    transient_retry_safe: bool = Field(strict=True)


class RecoveryAssessment(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    decision: Literal["allow_recovery", "escalate", "undetermined"]
    reasons: tuple[str, ...]


class RecoveryHistory(BaseModel):
    """Observed history; missing records and untracked diagnostics remain explicit."""
    model_config = ConfigDict(frozen=True, extra="forbid")

    retries_started: int = Field(ge=0)
    previous_signature_occurrences: int = Field(ge=0)
    failed_runs_without_record: int = Field(ge=0)
    # Counted from durable actual-start events, not Run creation, proposals or FailureRecords.
    rediagnoses_started: int | None = Field(default=None, ge=0)


def public_failure_signature(report: TestSuiteReport, comparison: TestComparison) -> str | None:
    """Fingerprint concrete public test failures; missing detail is not guessed."""
    names = set(comparison.target_issues) | set(comparison.regression_issues)
    if not names or CheckResult.FAILED not in (comparison.target_result, comparison.regression_result):
        return None
    tests = {case.name: case for case in report.tests}
    entries = []
    for name in sorted(names):
        case = tests.get(name)
        if (case is None or case.status != TestCaseStatus.FAILED or not case.failure_kind
                or not case.failure_message or not case.failure_message.strip()):
            return None
        entries.append({"name": name, "kind": case.failure_kind,
                        "message": " ".join(case.failure_message.split())})
    payload = json.dumps(entries, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return "public-test-v1:" + sha256(payload).hexdigest()


def semantic_failure_signature(review: SemanticReviewDraft) -> str | None:
    """Fingerprint the cited issue and reason without exposing code or merging different reasons."""
    if review.result != CheckResult.FAILED or not review.citations:
        return None
    entries = sorted((citation.path, citation.source, citation.diff_side or "",
                      " ".join(citation.snippet.split()), " ".join(citation.reason.split()))
                     for citation in review.citations)
    payload = json.dumps({"reason": " ".join(review.reason.split()), "citations": entries},
                         ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return "semantic-v1:" + sha256(payload).hexdigest()


def assess_recovery_policy(budget: PolicyBudget, facts: RecoveryFacts) -> RecoveryAssessment:
    """Evaluate recorded counts including the current failure; caller verifies the facts."""
    reasons = []
    if facts.retries_started >= budget.max_retries_per_task:
        reasons.append("retry_limit")
    if facts.signature_occurrences >= budget.max_same_signature_occurrences:
        reasons.append("signature_limit")
    rediagnoses_unknown = facts.category == FailureCategory.UNKNOWN_FAILURE and facts.rediagnoses_started is None
    if facts.category == FailureCategory.UNKNOWN_FAILURE:
        if facts.rediagnoses_started is None:
            if budget.max_rediagnose_attempts == 0:
                # A zero quota is exhausted without knowing how many attempts started.
                reasons.append("rediagnose_limit")
        elif facts.rediagnoses_started >= budget.max_rediagnose_attempts:
            reasons.append("rediagnose_limit")
    if not facts.has_compliant_plan:
        reasons.append("no_compliant_plan")
    if facts.category == FailureCategory.TRANSIENT_EXECUTION_FAILURE:
        if not facts.transient_retry_safe:
            reasons.append("transient_retry_not_safe")
    elif not facts.has_new_information_or_strategy:
        reasons.append("no_information_gain")
    if reasons:
        return RecoveryAssessment(decision="escalate", reasons=tuple(reasons))
    if rediagnoses_unknown:
        return RecoveryAssessment(decision="undetermined", reasons=("rediagnose_count_unknown",))
    return RecoveryAssessment(decision="allow_recovery", reasons=())
