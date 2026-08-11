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

from app.firestore_db import (
    get_employee_balance,
    get_user_memories,
    normalize_user_id,
    record_approved_vacation,
    remove_user_memory,
    save_user_memory,
)

# Configure Structured JSON Logging
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
    text = re.sub(r"\b\d{3}-\d{2}-\d{4}\b", "[REDACTED_SSN]", text)
    text = re.sub(r"\b(?:\+?\d{1,3}[-.\s]?)?\(?\d{3}\)?[-.\s]?\d{3}[-.\s]?\d{4}\b", "[REDACTED_PHONE]", text)
    text = re.sub(r"\b(?:\d[ -]*?){13,16}\b", "[REDACTED_CARD]", text)
    text = re.sub(r"(?i)\b(medical|surgery|diagnosis|doctor note|hospital|condition)\b", "[REDACTED_MEDICAL_INFO]", text)
    return text


FLASH_MODEL = Gemini(model="gemini-2.5-flash")
PRO_MODEL = Gemini(model="gemini-2.5-pro")


class LeaveRequest(BaseModel):
    """Schema representing an employee holiday/leave request."""
    employee: str = Field("Alice Smith", description="Name or ID of requesting employee (Alice, Bob, Charlie).")
    days: float = Field(..., description="Total number of business days requested.")
    start_date: str = Field("2026-06-01", description="Leave start date (YYYY-MM-DD).")
    end_date: str = Field("2026-06-05", description="Leave end date (YYYY-MM-DD).")
    reason: str = Field("Vacation", description="Reason for the leave request (e.g. Malaga beach trip).")
    department: str = Field("Engineering", description="Employee department.")


def check_team_coverage(department: str, start_date: str, days: float) -> str:
    """Checks team coverage and overlapping leave requests for department."""
    if days > 10:
        return f"Warning: High duration ({days} days) in {department}. Team coverage drops to 60%."
    return f"Team coverage for {department} remains optimal at 85% during {start_date}."


def _process_leave_request_impl(ctx: Context, node_input: Any) -> Event:
    """
    Parses incoming leave submission, checks Firestore PTO balance, applies PII scrubbing,
    and routes:
    - requested_days > remaining_balance -> AUTO_DENY
    - requested_days <= 5 -> AUTO_APPROVE
    - requested_days > 5 -> MANAGER_REVIEW (HITL Pause)
    """
    days = 1.0
    reason = "Vacation"
    employee = "Alice Smith"
    department = "Engineering"
    start_date = "2026-06-01"

    text_input = ""

    if isinstance(node_input, LeaveRequest):
        days = node_input.days
        reason = node_input.reason
        employee = node_input.employee
        department = node_input.department
        start_date = node_input.start_date
    elif isinstance(node_input, dict):
        days = float(node_input.get("days", node_input.get("amount", 1.0)))
        reason = str(node_input.get("reason", node_input.get("description", "Vacation")))
        employee = str(node_input.get("employee", node_input.get("submitter", "Alice Smith")))
        department = str(node_input.get("department", "Engineering"))
        start_date = str(node_input.get("start_date", "2026-06-01"))
    else:
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
                employee = parsed.get("employee", parsed.get("submitter", "Alice Smith"))
                department = parsed.get("department", "Engineering")
        except Exception:
            # Check if this is a friendly conversational greeting leveraging ADK memory
            clean_lower = text_input.strip().lower()
            if any(g in clean_lower for g in ["hello", "hi", "hey", "greetings"]):
                uid = normalize_user_id(text_input)
                mems = get_user_memories(uid)
                destination_mention = ""
                for m in mems:
                    match_dest = re.search(r"vacation to ([a-zA-Z\s]+)", m, re.IGNORECASE)
                    if match_dest:
                        destination_mention = match_dest.group(1).strip()
                        break

                greeting_msg = f"Hello {uid.capitalize()}! Welcome back to LeaveFlow AI."
                if destination_mention:
                    greeting_msg = f"Hello {uid.capitalize()}! Welcome back. How was your vacation to {destination_mention}?"

                return Event(
                    output={"greeting": greeting_msg, "memories": mems},
                    route="greeting",
                    content=types.Content(role="model", parts=[types.Part.from_text(text=greeting_msg)]),
                )

            match = re.search(r"\b(\d+(?:\.\d+)?)\s*days?\b", text_input, re.IGNORECASE)
            if match:
                days = float(match.group(1))
            reason = text_input.strip() or "Vacation Request"

    sanitized_reason = sanitize_pii_text(reason)
    uid = normalize_user_id(employee)

    # 1. Firestore PTO Balance Validation
    balance_record = get_employee_balance(uid)
    remaining_balance = float(balance_record.get("remaining_balance", 25.0))

    log_structured(
        event_type="PROCESS_LEAVE_REQUEST",
        intent=f"Evaluate leave request for {employee} ({uid}): {days} days ({sanitized_reason}). Remaining balance: {remaining_balance}",
        metadata={"days": days, "department": department, "employee": employee, "remaining_balance": remaining_balance}
    )

    curr_employee_name = balance_record.get("employee", employee)
    req = LeaveRequest(
        employee=curr_employee_name,
        days=days,
        reason=sanitized_reason,
        department=department,
        start_date=start_date,
    )

    req_data = req.model_dump()
    req_data["user_id"] = uid
    req_data["remaining_balance"] = remaining_balance

    # Rule 1: Insufficient Balance -> AUTO DENY
    if days > remaining_balance:
        log_structured(
            event_type="PROCESS_LEAVE_REQUEST",
            intent=f"Check PTO balance for {employee} ({days} requested vs {remaining_balance} remaining)",
            outcome="ROUTED_TO_AUTO_DENY_INSUFFICIENT_BALANCE"
        )
        return Event(
            output=req_data,
            route="auto_deny",
            state={"leave_request": req_data},
        )

    # Rule 2: <= 5 days -> AUTO APPROVE
    if days <= 5.0:
        log_structured(
            event_type="PROCESS_LEAVE_REQUEST",
            intent=f"Route {employee} request for {days} days (<= 5 days policy)",
            outcome="ROUTED_TO_AUTO_APPROVE"
        )
        return Event(
            output=req_data,
            route="auto_approve",
            state={"leave_request": req_data},
        )

    # Rule 3: > 5 days -> HITL MANAGER REVIEW
    log_structured(
        event_type="PROCESS_LEAVE_REQUEST",
        intent=f"Route {employee} request for {days} days (> 5 days threshold)",
        outcome="ROUTED_TO_MANAGER_REVIEW"
    )
    return Event(
        output=req_data,
        route="review",
        state={"leave_request": req_data},
    )


def _auto_approve_impl(ctx: Context, node_input: Any) -> Event:
    """Automatically approves leave requests <= 5 days and deducts balance in Firestore."""
    req_dict = node_input if isinstance(node_input, dict) else (ctx.state.get("leave_request", {}) if ctx and hasattr(ctx, "state") else {})
    days = req_dict.get("days", 1.0)
    reason = req_dict.get("reason", "Vacation")
    employee = req_dict.get("employee", "Alice Smith")
    start_date = req_dict.get("start_date", "2026-06-01")

    # Update Firestore Balance & History
    updated_rec = record_approved_vacation(employee, days, reason, start_date)
    new_rem = updated_rec.get("remaining_balance", 24.0)

    # Save Memory
    dest_match = re.search(r"to\s+([a-zA-Z\s]+)", reason, re.IGNORECASE)
    destination = dest_match.group(1).strip() if dest_match else reason
    save_user_memory(employee, f"Going on vacation to {destination}")

    summary = (
        f"Holiday request of {days} day(s) for '{reason}' by {employee} AUTO-APPROVED (<= 5 days policy). "
        f"Remaining balance updated to {new_rem} day(s)."
    )

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
            "remaining_balance": new_rem,
            "summary": summary,
        },
        content=types.Content(
            role="model",
            parts=[types.Part.from_text(text=f"⚡ [AUTO-APPROVED] {summary}")],
        ),
    )


def _auto_deny_impl(ctx: Context, node_input: Any) -> Event:
    """Automatically denies leave requests where requested days > remaining PTO balance."""
    req_dict = node_input if isinstance(node_input, dict) else (ctx.state.get("leave_request", {}) if ctx and hasattr(ctx, "state") else {})
    days = req_dict.get("days", 10.0)
    reason = req_dict.get("reason", "Vacation")
    employee = req_dict.get("employee", "Alice Smith")
    remaining_balance = req_dict.get("remaining_balance", 0.0)

    summary = (
        f"Holiday request of {days} day(s) for '{reason}' by {employee} AUTO-DENIED. "
        f"Insufficient PTO balance (Requested: {days} days, Available: {remaining_balance} days)."
    )

    log_structured(
        event_type="AUTO_DENY",
        intent=f"Auto-deny request for {employee} due to insufficient balance",
        outcome=summary
    )

    return Event(
        output={
            "status": "DENIED",
            "approval_type": "AUTOMATIC_DENIAL",
            "days": days,
            "reason": reason,
            "remaining_balance": remaining_balance,
            "summary": summary,
        },
        content=types.Content(
            role="model",
            parts=[types.Part.from_text(text=f"❌ [AUTO-DENIED] {summary}")],
        ),
    )


def _greeting_impl(ctx: Context, node_input: Any) -> Event:
    """Renders contextual greeting response."""
    greeting_text = node_input.get("greeting", "Hello! Welcome back to LeaveFlow AI.") if isinstance(node_input, dict) else "Hello! Welcome back."
    return Event(
        output={"status": "GREETING", "greeting": greeting_text},
        content=types.Content(role="model", parts=[types.Part.from_text(text=greeting_text)]),
    )


process_leave_request_node = node(_process_leave_request_impl)
auto_approve_node = node(_auto_approve_impl)
auto_deny_node = node(_auto_deny_impl)
greeting_node = node(_greeting_impl)


@node(rerun_on_resume=True)
async def review_agent(ctx: Context, node_input: Any) -> AsyncGenerator[Any, None]:
    """Flag leave requests > 5 days and pause execution for human-in-the-loop manager approval."""
    req_dict = node_input if isinstance(node_input, dict) else (ctx.state.get("leave_request", {}) if ctx and hasattr(ctx, "state") else {})
    days = req_dict.get("days", 6.0)
    reason = req_dict.get("reason", "Vacation")
    employee = req_dict.get("employee", "Alice Smith")
    department = req_dict.get("department", "Engineering")
    start_date = req_dict.get("start_date", "2026-06-01")

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
        updated_rec = record_approved_vacation(employee, days, reason, start_date)
        new_rem = updated_rec.get("remaining_balance", 18.0)
        dest_match = re.search(r"to\s+([a-zA-Z\s]+)", reason, re.IGNORECASE)
        destination = dest_match.group(1).strip() if dest_match else reason
        save_user_memory(employee, f"Going on vacation to {destination}")
        summary = f"Holiday request of {days} day(s) for '{reason}' by {employee} WAS APPROVED by manager review. Remaining balance: {new_rem} days."
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


process_leave_request = _process_leave_request_impl
auto_approve = _auto_approve_impl
auto_deny = _auto_deny_impl

root_agent = Workflow(
    name="holiday_approval_workflow",
    description="Autonomous Leave Approval Workflow with PTO balance validation, auto-approval (<= 5 days), auto-denial, and Human-in-the-Loop review (> 5 days).",
    edges=[
        (START, process_leave_request_node),
        Edge(from_node=process_leave_request_node, to_node=auto_approve_node, route="auto_approve"),
        Edge(from_node=process_leave_request_node, to_node=auto_deny_node, route="auto_deny"),
        Edge(from_node=process_leave_request_node, to_node=greeting_node, route="greeting"),
        Edge(from_node=process_leave_request_node, to_node=review_agent, route="review"),
    ],
)

app = App(
    root_agent=root_agent,
    name="app",
)
