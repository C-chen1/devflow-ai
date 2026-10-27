from fastapi.testclient import TestClient
from devflow_ai.api import app
import logging,json

def test_http_requests_have_distinct_ids(caplog):
    client = TestClient(app)
    caplog.set_level(logging.INFO,logger = "devflow_ai.api")
    first_response = client.get("/health/live")
    second_response = client.get("/health/live")
    assert first_response.status_code == 200
    assert second_response.status_code == 200
    request_records = []
    for record in caplog.records:
        if record.name == "devflow_ai.api":
            log_data = json.loads(record.getMessage())
            if log_data["event"] =="http_request_completed":
                request_records.append(record)
    first_log_data = json.loads(request_records[0].getMessage())
    second_log_data = json.loads(request_records[1].getMessage())
    assert first_log_data["request_id"] != second_log_data["request_id"]

