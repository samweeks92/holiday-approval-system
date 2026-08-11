#!/usr/bin/env bash
# Copyright 2026 Google LLC
# LeaveFlow AI Eval-Gated CI/CD Deployment Script

set -e

PROJECT_ID="ai-sandbox-sw"
REGION="europe-west1"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

echo "=================================================="
echo "🚀 LeaveFlow AI - CI/CD Deployment Pipeline"
echo "=================================================="

# 1. Execute Automated Evaluation Suite
echo ""
echo "Step 1: Running Static Evaluation & PII Redaction Harness..."
echo "--------------------------------------------------"
"${SCRIPT_DIR}/leave-agent/.venv/bin/python" "${SCRIPT_DIR}/eval/test_runner.py"

if [ $? -ne 0 ]; then
  echo ""
  echo "❌ DEPLOYMENT ABORTED: Evaluation suite failed with regressions!"
  exit 1
fi

echo ""
echo "Step 2: Deploying Reasoning Engine to Vertex AI Agent Engine..."
echo "--------------------------------------------------"
cd "${SCRIPT_DIR}/leave-agent"

agents-cli deploy \
  --region "${REGION}" \
  --project "${PROJECT_ID}" \
  --no-confirm-project

echo ""
echo "=================================================="
echo "🎉 SUCCESS: LeaveFlow AI Agent Engine Deployed!"
echo "=================================================="
