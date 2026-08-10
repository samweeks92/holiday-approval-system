# ruff: noqa
# Copyright 2026 Google LLC
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     https://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

import datetime
import json
import logging
import re
from typing import Any, AsyncGenerator, Dict, List, Optional
from pydantic import BaseModel, Field

from google.adk.agents.context import Context
from google.adk.apps import App
from google.adk.events.event import Event
from google.adk.events.request_input import RequestInput
from google.adk.models import Gemini
from google.adk.workflow import START, Edge, Workflow, node
from google.genai import types

# Configure Structured JSON Logging with Intent vs Outcome Capture
logging.basicConfig(level=logging.INFO, format='%(message)s')
logger = logging.getLogger("leave_agent_logger")


def log_structured(event_type: str, intent: str, outcome: Optional[str] = None, metadata: Optional[Dict[str, Any]] = None):
    """Outputs structured JSON logs capturing INTENT before execution and OUTCOME after execution."""
    log_entry = {
        "timestamp": datetime.datetime.now(datetime.timezone.utc).isoformat(),
        "event_type": event_type,
        "intent": intent,
        "outcome": outcome or "IN_PROGRESS",
        "metadata": sanitize_pii_text(json.dumps(metadata)) if metadata else {}
    }
    logger.info(json.dumps(log_entry))


def sanitize_pii_text(text: str) -> str:
    """Active PII Redaction Scrubbing Pipeline: Redacts SSN, phone numbers, credit cards, and medical notes."""
    if not text:
        return text
    # Redact SSN
    text = re.sub(r"\b\d{3}-\d{2}-\d{4}\b", "[REDACTED_SSN]", text)
    # Redact Phone Numbers
    text = re.sub(r"\b(?:\+?\d{1,3}[-.\s]?)?\(?\d{3}\)?[-.\s]?\d{3}[-.\s]?\d{4}\b", "[REDACTED_PHONE]", text)
    # Redact Credit Cards
    text = re.sub(r"\b(?:\d[ -]*?){13,16}\b", "[REDACTED_CARD]", text)
    # Redact Sensitive Medical Keywords
    text = re.sub(r"(?i)\b(medical|surgery|diagnosis|doctor note|hospital|condition)\b", "[REDACTED_MEDICAL_INFO]", text)
    return text


# Model Routing: Flash for fast processing, Pro for complex evaluation
FLASH_MODEL = Gemini(model="gemini-2.5-flash")
PRO_MODEL = Gemini(model="gemini-2.5-pro")


class LeaveRequest(BaseModel):
    """Schema representing an employee holiday/leave request."""
    employee: str = Field("Employee", description="Name or ID of the requesting employee.")
    days: float = Field(..., description="Total number of business days requested.")
    start_date: str = Field("2026-05-01", description="Leave start date (YYYY-MM-DD).")
    end_date: str = Field("2026-05-05", description="Leave end date (YYYY-MM-DD).")
    reason: str = Field("Vacation", description="Reason for the leave request.")
    department: str = Field("Engineering", description="Employee department.")


def check_team_coverage(department: str, start_date: str, days: float) -> str:
    """Checks team coverage and overlapping leave requests for the specified department.

    Args:
        department: The employee's department name.
        start_date: Request start date.
        days: Leave duration in days.

    Returns:
        A human-readable report on remaining team coverage percentage.
    """
    if days > 10:
        return f"Warning: High duration ({days} days) in {department}. Team coverage drops to 60%."
    return f"Team coverage for {department} remains optimal at 85% during {start_date}."


def validate_pto_balance(employee: str, days: float) -> str:
    """Verifies available PTO vacation balance for an employee.

    Args:
        employee: Name or ID of the employee.
        days: Days requested.

    Returns:
        Status message with available balance or recovery guidance if balance is exceeded.
    """
    max_balance = 20.0
    if days > max_balance:
        return f"ERROR: Requested {days} days exceeds available balance of {max_balance} days. Recovery: Reduce requested days or submit an unpaid leave request."
    return f"PTO balance verified: {employee} has sufficient balance for {days} days."


def _process_leave_request_impl(ctx: Context, node_input: Any) -> Event:
    """Parses incoming leave submission, applies PII redaction, and routes based on policy (<= 5 days auto-approve vs > 5 days HITL review)."""
    days = 1.0
    reason = "Vacation"
    employee = "Employee"
    department = "Engineering"
    start_date = "2026-05-01"

    if isinstance(node_input, LeaveRequest):
        days = node_input.days
        reason = node_input.reason
        employee = node_input.employee
        department = node_input.department
        start_date = node_input.start_date
    elif isinstance(node_input, dict):
        days = float(node_input.get("days", node_input.get("amount", 1.0)))
        reason = str(node_input.get("reason", node_input.get("description", "Vacation")))
        employee = str(node_input.get("employee", node_input.get("submitter", "Employee")))
        department = str(node_input.get("department", "Engineering"))
        start_date = str(node_input.get("start_date", "2026-05-01"))
    else:
        text_input = ""
        if isinstance(node_input, types.Content):
            parts_text = [p.text for p in node_input.parts if p.text] if node_input.parts else []
            text_input = " ".join(parts_text)
        else:
            text_input = str(node_input)

        try:
            parsed = json.loads(text_input)
            if isinstance(parsed, dict):
                days = float(parsed.get("days", parsed.get("amount", 1.0)))
                reason = parsed.get("reason", parsed.get("description", "Vacation"))
                employee = parsed.get("employee", parsed.get("submitter", "Employee"))
                department = parsed.get("department", "Engineering")
        except Exception:
            match = re.search(r"\b(\d+(?:\.\d+)?)\s*days?\b", text_input, re.IGNORECASE)
            if match:
                days = float(match.group(1))
            reason = text_input.strip() or "Vacation Request"

    sanitized_reason = sanitize_pii_text(reason)

    log_structured(
        event_type="PROCESS_LEAVE_REQUEST",
        intent=f"Evaluate leave request for {employee}: {days} days ({sanitized_reason})",
        metadata={"days": days, "department": department, "employee": employee}
    )

    req = LeaveRequest(
        employee=employee,
        days=days,
        reason=sanitized_reason,
        department=department,
        start_date=start_date,
    )

    if req.days <= 5.0:
        log_structured(
            event_type="PROCESS_LEAVE_REQUEST",
            intent=f"Route {employee} request for {days} days",
            outcome="ROUTED_TO_AUTO_APPROVE"
        )
        return Event(
            output=req.model_dump(),
            route="auto_approve",
            state={"leave_request": req.model_dump()},
        )
    else:
        log_structured(
            event_type="PROCESS_LEAVE_REQUEST",
            intent=f"Route {employee} request for {days} days (> 5 days threshold)",
            outcome="ROUTED_TO_MANAGER_REVIEW"
        )
        return Event(
            output=req.model_dump(),
            route="review",
            state={"leave_request": req.model_dump()},
        )


def _auto_approve_impl(ctx: Context, node_input: Any) -> Event:
    """Automatically approves leave requests <= 5 days."""
    req_dict = node_input if isinstance(node_input, dict) else (ctx.state.get("leave_request", {}) if ctx and hasattr(ctx, "state") else {})
    days = req_dict.get("days", 1.0)
    reason = req_dict.get("reason", "Vacation")
    employee = req_dict.get("employee", "Employee")

    summary = f"Holiday request of {days} day(s) for '{reason}' by {employee} AUTO-APPROVED instantly (<= 5 days policy)."

    log_structured(
        event_type="AUTO_APPROVE",
        intent=f"Auto-approve leave request for {employee}",
        outcome=summary
    )

    return Event(
        output={
            "status": "APPROVED",
            "approval_type": "AUTOMATIC",
            "days": days,
            "reason": reason,
            "summary": summary,
        },
        content=types.Content(
            role="model",
            parts=[types.Part.from_text(text=f"⚡ [AUTO-APPROVED] {summary}")],
        ),
    )


process_leave_request_node = node(_process_leave_request_impl)
auto_approve_node = node(_auto_approve_impl)


@node(rerun_on_resume=True)
async def review_agent(ctx: Context, node_input: Any) -> AsyncGenerator[Any, None]:
    """Flag leave requests > 5 days and pause execution for human-in-the-loop manager approval."""
    req_dict = node_input if isinstance(node_input, dict) else (ctx.state.get("leave_request", {}) if ctx and hasattr(ctx, "state") else {})
    days = req_dict.get("days", 6.0)
    reason = req_dict.get("reason", "Vacation")
    employee = req_dict.get("employee", "Employee")
    department = req_dict.get("department", "Engineering")

    interrupt_id = "manager_review"

    if not ctx or not hasattr(ctx, "resume_inputs") or not ctx.resume_inputs or interrupt_id not in ctx.resume_inputs:
        log_structured(
            event_type="HITL_PAUSE",
            intent=f"Pause execution for manager approval ({days} days requested by {employee})",
            outcome="EMITTED_REQUEST_INPUT_PAUSE"
        )
        yield RequestInput(
            interrupt_id=interrupt_id,
            message=(
                f"⚠️ HOLIDAY APPROVAL REQUIRED\n"
                f"Employee: {employee}\n"
                f"Duration: {days} day(s)\n"
                f"Department: {department}\n"
                f"Reason: {reason}\n"
                f"Policy Trigger: Request exceeds 5 business days threshold.\n\n"
                f"Manager Action Needed: Do you approve this holiday request? (Type 'approve' or 'reject')"
            ),
        )
        return

    manager_response = str(ctx.resume_inputs[interrupt_id]).strip().lower()
    is_approved = any(w in manager_response for w in ["approve", "approved", "yes", "y"])

    if is_approved:
        status = "APPROVED"
        summary = f"Holiday request of {days} day(s) for '{reason}' by {employee} WAS APPROVED by manager review."
    else:
        status = "REJECTED"
        summary = f"Holiday request of {days} day(s) for '{reason}' by {employee} WAS REJECTED by manager review."

    log_structured(
        event_type="HITL_RESUME",
        intent=f"Process manager decision for {employee} ({days} days)",
        outcome=summary,
        metadata={"manager_response": manager_response, "status": status}
    )

    yield Event(
        output={
            "status": status,
            "approval_type": "MANUAL_REVIEW",
            "days": days,
            "reason": reason,
            "manager_response": manager_response,
            "summary": summary,
        },
        content=types.Content(
            role="model",
            parts=[types.Part.from_text(text=f"📋 [REVIEW RESULT] {summary}")],
        ),
    )


# Alias for backward compatibility in eval tests
process_leave_request = _process_leave_request_impl
auto_approve = _auto_approve_impl

root_agent = Workflow(
    name="holiday_approval_workflow",
    description="Autonomous Leave Approval Workflow with auto-approval (<= 5 days) and Human-in-the-Loop review (> 5 days).",
    edges=[
        (START, process_leave_request_node),
        Edge(from_node=process_leave_request_node, to_node=auto_approve_node, route="auto_approve"),
        Edge(from_node=process_leave_request_node, to_node=review_agent, route="review"),
    ],
)

app = App(
    root_agent=root_agent,
    name="app",
)
