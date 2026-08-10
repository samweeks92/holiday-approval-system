# Holiday & PTO Approval System ("LeaveFlow AI")

An event-driven, human-in-the-loop autonomous AI agent system for managing employee leave requests, team coverage validation, and manager approvals.

## Directory Structure
- `leave-agent/`: ADK 2.0 Agent Core (Graph workflow, PII redaction, tools, history compaction, and model routing).
- `frontend/`: Cloud Run Manager Portal (FastAPI dashboard with real-time session polling and approval drawers).
- `infrastructure/`: Infrastructure as Code (Provisioning Pub/Sub topics, Cloud Run, Cloud Build trigger, and IAM roles).
- `eval/`: Automated Evaluation Suite (Golden dataset testing harness for leave approval policy benchmarking).
- `cloudbuild.yaml`: Google Cloud Build CI/CD pipeline definition.
