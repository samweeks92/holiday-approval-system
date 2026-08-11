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

import asyncio
import datetime
import json
import logging
import os
import re
from typing import Any, AsyncGenerator, Dict, List, Optional
from pydantic import BaseModel, Field

from google.adk.agents.context import Context
from google.adk.apps import App
from google.adk.events.event import Event
from google.adk.events.request_input import RequestInput
from google.adk.memory.vertex_ai_memory_bank_service import VertexAiMemoryBankService
from google.adk.memory.memory_entry import MemoryEntry
from google.adk.models import Gemini
from google.adk.workflow import START, Edge, Workflow, node
from google.genai import types

from app.firestore_db import (
    get_employee_balance,
    normalize_user_id,
    record_approved_vacation,
    record_denied_vacation,
    record_pending_vacation,
)

# Configure Structured JSON Logging
logging.basicConfig(level=logging.INFO, format='%(message)s')
logger = logging.getLogger("leave_agent_logger")

PROJECT_ID = os.environ.get("PROJECT_ID", "ai-sandbox-sw")
LOCATION = os.environ.get("LOCATION", "europe-west1")
AGENT_ENGINE_ID = os.environ.get("AGENT_ENGINE_ID", "6128897715548979200").split("/")[-1]


def log_structured(event_type: str, intent: str, outcome: Optional[str] = None, metadata: Optional[Dict[str, Any]] = None):
    log_entry = {
        "timestamp": datetime.datetime.now(datetime.timezone.utc).isoformat(),
        "event_type": event_type,
        "intent": intent,
        "outcome": outcome or "IN_PROGRESS",
        "metadata": sanitize_pii_text(json.dumps(metadata)) if metadata else {}
    }
    logger.info(json.dumps(log_entry))


def sanitize_pii_text(text: str) -> str:
    if not text:
        return text
    text = re.sub(r"\b\d{3}-\d{2}-\d{4}\b", "[REDACTED_SSN]", text)
    text = re.sub(r"\b(?:\+?\d{1,3}[-.\s]?)?\(?\d{3}\)?[-.\s]?\d{3}[-.\s]?\d{4}\b", "[REDACTED_PHONE]", text)
    text = re.sub(r"\b(?:\d[ -]*?){13,16}\b", "[REDACTED_CARD]", text)
    text = re.sub(r"(?i)\b(medical|surgery|diagnosis|doctor note|hospital|condition)\b", "[REDACTED_MEDICAL_INFO]", text)
    return text


def save_vertex_memory_sync(uid: str, text: str):
    """Saves a memory entry directly to Vertex AI Agent Engine Memory Bank."""
    try:
        mb = VertexAiMemoryBankService(project=PROJECT_ID, location=LOCATION, agent_engine_id=AGENT_ENGINE_ID)
        entry = MemoryEntry(content=types.Content(role="user", parts=[types.Part.from_text(text=text)]))
        
        async def _save():
            await mb.add_memory(app_name=AGENT_ENGINE_ID, user_id=uid, memories=[entry])

        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        loop.run_until_complete(_save())
        loop.close()
        logger.info(f"Saved memory to Vertex AI Memory Bank for {uid}: {text}")
    except Exception as e:
        logger.warning(f"Vertex AI Memory Bank save notice for {uid}: {e}")


def get_vertex_memories_sync(uid: str, query: str = "vacation trip destination") -> List[str]:
    """Retrieves user memories directly from Vertex AI Agent Engine Memory Bank."""
    try:
        mb = VertexAiMemoryBankService(project=PROJECT_ID, location=LOCATION, agent_engine_id=AGENT_ENGINE_ID)
        
        async def _search():
            res = await mb.search_memory(app_name=AGENT_ENGINE_ID, user_id=uid, query=query)
            mems = []
            if hasattr(res, "memories") and res.memories:
                for entry in res.memories:
                    content = getattr(entry, "content", None)
                    if content and hasattr(content, "parts"):
                        for p in content.parts:
                            if hasattr(p, "text") and p.text:
                                mems.append(p.text)
            return mems

        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        results = loop.run_until_complete(_search())
        loop.close()
        return results
    except Exception as e:
        logger.warning(f"Vertex AI Memory Bank search notice for {uid}: {e}")
        return []


FLASH_MODEL = Gemini(model="gemini-2.5-flash")
PRO_MODEL = Gemini(model="gemini-2.5-pro")


class LeaveRequest(BaseModel):
    employee: str = Field("Alice Smith", description="Name or ID of requesting employee (Alice, Bob, Charlie).")
    days: float = Field(..., description="Total number of business days requested.")
    start_date: str = Field("2026-06-01", description="Leave start date (YYYY-MM-DD).")
    end_date: str = Field("2026-06-05", description="Leave end date (YYYY-MM-DD).")
    reason: str = Field("Vacation", description="Reason for the leave request.")
    department: str = Field("Engineering", description="Employee department.")


def _process_leave_request_impl(ctx: Context, node_input: Any) -> Event:
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
            clean_lower = text_input.strip().lower()
            uid = normalize_user_id(text_input)
            
            mems = get_vertex_memories_sync(uid)
            
            # Smart Greeting with Memory
            if any(g in clean_lower for g in ["hi", "hello", "hey", "greetings"]):
                memory_snippet = ""
                if mems:
                    latest_mem = mems[0]
                    memory_snippet = f" I remember your previous note: '{latest_mem}'."

                greeting_msg = f"Hi {uid.capitalize()}!{memory_snippet} How can I help you today?"

                return Event(
                    output={"greeting": greeting_msg, "vertex_memories": mems},
                    route="greeting",
                    content=types.Content(role="model", parts=[types.Part.from_text(text=greeting_msg)]),
                )

            match = re.search(r"\b(\d+(?:\.\d+)?)\s*days?\b", text_input, re.IGNORECASE)
            if match:
                days = float(match.group(1))
            reason = text_input.strip() or "Vacation Request"

    sanitized_reason = sanitize_pii_text(reason)
    uid = normalize_user_id(employee)

    vertex_memories = get_vertex_memories_sync(uid)

    balance_record = get_employee_balance(uid)
    remaining_balance = float(balance_record.get("remaining_balance", 25.0))

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
    req_data["vertex_memories"] = vertex_memories

    if days > remaining_balance:
        save_vertex_memory_sync(uid, f"{curr_employee_name} requested {days} days vacation for {sanitized_reason} (Denied - Insufficient Balance)")
        record_denied_vacation(curr_employee_name, days, sanitized_reason, start_date)
        return Event(
            output=req_data,
            route="auto_deny",
            state={"leave_request": req_data},
        )

    if days <= 5.0:
        return Event(
            output=req_data,
            route="auto_approve",
            state={"leave_request": req_data},
        )

    # > 5 days -> Record pending in Firestore and route to manager review
    record_pending_vacation(curr_employee_name, days, sanitized_reason, start_date)
    return Event(
        output=req_data,
        route="review",
        state={"leave_request": req_data},
    )


def _auto_approve_impl(ctx: Context, node_input: Any) -> Event:
    req_dict = node_input if isinstance(node_input, dict) else (ctx.state.get("leave_request", {}) if ctx and hasattr(ctx, "state") else {})
    days = req_dict.get("days", 1.0)
    reason = req_dict.get("reason", "Vacation")
    employee = req_dict.get("employee", "Alice Smith")
    start_date = req_dict.get("start_date", "2026-06-01")
    uid = normalize_user_id(employee)

    updated_rec = record_approved_vacation(employee, days, reason, start_date)
    new_rem = updated_rec.get("remaining_balance", 24.0)

    save_vertex_memory_sync(uid, f"{employee} booked a {days}-day vacation to {reason}")

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
    req_dict = node_input if isinstance(node_input, dict) else (ctx.state.get("leave_request", {}) if ctx and hasattr(ctx, "state") else {})
    days = req_dict.get("days", 10.0)
    reason = req_dict.get("reason", "Vacation")
    employee = req_dict.get("employee", "Alice Smith")
    remaining_balance = req_dict.get("remaining_balance", 0.0)

    summary = (
        f"Holiday request of {days} day(s) for '{reason}' by {employee} AUTO-DENIED. "
        f"Insufficient PTO balance (Requested: {days} days, Available: {remaining_balance} days)."
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
    req_dict = node_input if isinstance(node_input, dict) else (ctx.state.get("leave_request", {}) if ctx and hasattr(ctx, "state") else {})
    days = req_dict.get("days", 6.0)
    reason = req_dict.get("reason", "Vacation")
    employee = req_dict.get("employee", "Alice Smith")
    department = req_dict.get("department", "Engineering")
    start_date = req_dict.get("start_date", "2026-06-01")
    uid = normalize_user_id(employee)

    interrupt_id = "manager_review"

    if not ctx or not hasattr(ctx, "resume_inputs") or not ctx.resume_inputs or interrupt_id not in ctx.resume_inputs:
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
        save_vertex_memory_sync(uid, f"{employee} booked a {days}-day vacation to {reason}")
        summary = f"Holiday request of {days} day(s) for '{reason}' by {employee} WAS APPROVED by manager review. Remaining balance: {new_rem} days."
    else:
        status = "REJECTED"
        record_denied_vacation(employee, days, reason, start_date)
        save_vertex_memory_sync(uid, f"{employee} requested {days} days vacation to {reason} (Denied by Manager)")
        summary = f"Holiday request of {days} day(s) for '{reason}' by {employee} WAS REJECTED by manager review."

    yield Event(
        output={
            "status": status,
            "approval_type": "MANUAL_REVIEW",
            "days": days,
            "reason": reason,
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
    description="Autonomous Leave Approval Workflow using Vertex AI Memory Bank for state management.",
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
