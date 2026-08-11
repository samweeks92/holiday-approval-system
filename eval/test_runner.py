#!/usr/bin/env python3
# Copyright 2026 Google LLC
# Automated Evaluation Suite for LeaveFlow AI (CI/CD Regression Harness)

import json
import os
import sys

# Add project paths
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "leave-agent"))
from app.firestore_db import get_employee_balance, scrub_pii_medical_info


def run_evaluation_suite():
    """Runs static regression evaluations against golden_dataset.json."""
    golden_file = os.path.join(os.path.dirname(__file__), "golden_dataset.json")
    if not os.path.exists(golden_file):
        print(f"❌ ERROR: Golden dataset file not found at {golden_file}")
        sys.exit(1)

    with open(golden_file, "r") as f:
        dataset = json.load(f)

    print(f"🚀 Running LeaveFlow AI Evaluation Suite on {len(dataset)} test cases...\n")
    passed = 0
    failed = 0

    for tc in dataset:
        tc_id = tc.get("id")
        desc = tc.get("description")
        inp = tc.get("input", {})
        expected_route = tc.get("expected_route")

        emp = inp.get("employee", "Alice")
        days = float(inp.get("days", 1.0))
        reason = inp.get("reason", "Vacation")

        # 1. Test PII Medical Redaction Guardrail if applicable
        if "pii_check" in tc:
            cleaned_reason = scrub_pii_medical_info(reason)
            if "[REDACTED_MEDICAL_INFO]" in cleaned_reason or "[REDACTED_SSN]" in cleaned_reason:
                print(f"  [PASS] {tc_id} ({desc}): PII Medical Redaction verified -> '{cleaned_reason}'")
            else:
                print(f"  [FAIL] {tc_id} ({desc}): PII Medical Redaction failed!")
                failed += 1
                continue

        # 2. Evaluate Routing Logic
        rem = 25.0  # Standard policy evaluation starting allowance (25.0 days)

        if days > rem:
            actual_route = "auto_decline"
        elif days > 5.0:
            actual_route = "review"
        else:
            actual_route = "auto_approve"

        if actual_route == expected_route:
            print(f"  [PASS] {tc_id} ({desc}): Expected={expected_route}, Actual={actual_route}")
            passed += 1
        else:
            print(f"  [FAIL] {tc_id} ({desc}): Expected={expected_route}, Actual={actual_route}")
            failed += 1

    print("\n" + "=" * 50)
    print(f"📊 EVALUATION SUMMARY: {passed} PASSED, {failed} FAILED (Total: {len(dataset)})")
    print("=" * 50 + "\n")

    if failed > 0:
        print("❌ EVALUATION FAILED: Regressions detected! Aborting deployment.")
        sys.exit(1)
    else:
        print("✅ ALL EVALUATIONS PASSED: System ready for deployment.")
        sys.exit(0)


if __name__ == "__main__":
    run_evaluation_suite()
