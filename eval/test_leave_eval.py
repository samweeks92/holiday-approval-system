import json
import pathlib
import pytest
import sys

# Ensure leave-agent is on path
AGENT_DIR = pathlib.Path(__file__).parent.parent / "leave-agent"
if str(AGENT_DIR) not in sys.path:
    sys.path.insert(0, str(AGENT_DIR))

from app.agent import process_leave_request, sanitize_pii_text

EVAL_DIR = pathlib.Path(__file__).parent
GOLDEN_DATASET_PATH = EVAL_DIR / "golden_dataset.json"


@pytest.fixture
def golden_cases():
    with open(GOLDEN_DATASET_PATH, "r") as f:
        return json.load(f)


def test_golden_dataset_routing(golden_cases):
    """Verifies that all golden test cases route according to the <= 5 days policy."""
    for case in golden_cases:
        req_data = case["input"]
        event = process_leave_request(ctx=None, node_input=req_data)
        route = event.actions.route if hasattr(event, "actions") and event.actions else getattr(event, "route", None)
        assert route == case["expected_route"], f"Case {case['id']} failed: expected {case['expected_route']}, got {route}"


def test_pii_sanitization():
    """Verifies that PII redaction pipeline strips SSN, phones, and medical keywords."""
    sensitive_text = "Medical surgery at hospital SSN 123-45-6789 Phone 555-123-4567"
    sanitized = sanitize_pii_text(sensitive_text)
    assert "123-45-6789" not in sanitized
    assert "555-123-4567" not in sanitized
    assert "[REDACTED_SSN]" in sanitized
    assert "[REDACTED_PHONE]" in sanitized
    assert "[REDACTED_MEDICAL_INFO]" in sanitized
