import pytest
from pydantic import ValidationError

from devflow_ai.services import AdmissionRecord


def test_admission_record_requires_reason_and_evidence_field():
    with pytest.raises(ValidationError):
        AdmissionRecord(reason="  ", evidence=[])
    with pytest.raises(ValidationError):
        AdmissionRecord(reason="insufficient information")
    record = AdmissionRecord(reason="insufficient information", evidence=[])
    assert record.evidence == []
