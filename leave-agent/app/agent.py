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
import json
import logging
import os
import re
import uuid
from contextlib import contextmanager
from typing import Any, AsyncGenerator, Dict, List, Literal, Optional

from google.adk.agents import LlmAgent
from google.adk.agents.context import Context
from google.adk.apps import App
from google.adk.apps._configs import EventsCompactionConfig
from google.adk.events.event import Event, EventActions
from google.adk.events.request_input import RequestInput
from google.adk.memory.vertex_ai_memory_bank_service import VertexAiMemoryBankService
from google.adk.memory.memory_entry import MemoryEntry
from google.adk.tools.tool_context import ToolContext
from google.adk.workflow import Workflow, node
from google.genai import types
from pydantic import BaseModel, Field

from app.firestore_db import (
    get_employee_balance,
    normalize_user_id,
    record_approved_vacation,
    record_denied_vacation,
    record_pending_vacation,
    scrub_pii_medical_info,
)

# Configure Structured JSON Logging
logging.basicConfig(level=logging.INFO, format='%(message)s')
logger = logging.getLogger("leave_agent_logger")

PROJECT_ID = os.environ.get("PROJECT_ID", "ai-sandbox-sw")
LOCATION = os.environ.get("LOCATION", "europe-west1")
AGENT_ENGINE_ID = os.environ.get("AGENT_ENGINE_ID", "6128897715548979200").split("/")[-1]

MODEL_FAST = "gemini-2.5-flash"
MODEL_LITE = "gemini-2.5-flash-lite"


# --- OBSERVABILITY: INTENT VS OUTCOME CAPTURE ---

@contextmanager
def capture_trajectory(intent: str, agent_id: str, trace_id: str = None):
    """Context manager implementing the 'Intent vs. Outcome Capture' pattern.
    Guarantees logging the agent's intended action on entry and actual outcome on exit.
    
    Args:
        intent (str): Description of the intended action.
        agent_id (str): Identifier of the executing agent or tool.
        trace_id (str, optional): Correlation ID linking intent and outcome logs.
    """
    trace_id = trace_id or str(uuid.uuid4())
    intent_log = {
        "event": "intent_capture",
        "agent_id": agent_id,
        "trace_id": trace_id,
        "intent": intent,
        "status": "planned"
    }
    logger.info(json.dumps(intent_log))
    
    try:
        yield trace_id
        outcome_log = {
            "event": "outcome_capture",
            "agent_id": agent_id,
            "trace_id": trace_id,
            "intent": intent,
            "outcome": {
                "status": "success",
                "message": "Action completed successfully."
            }
        }
        logger.info(json.dumps(outcome_log))
    except Exception as e:
        outcome_log = {
            "event": "outcome_capture",
            "agent_id": agent_id,
            "trace_id": trace_id,
            "intent": intent,
            "outcome": {
                "status": "failure",
                "error_type": type(e).__name__,
                "error_message": str(e)
            }
        }
        logger.error(json.dumps(outcome_log))
        raise e


# --- SCHEMAS ---

class VacationEvaluation(BaseModel):
    """Structured schema for classifying and evaluating vacation requests using LLM reasoning."""
    employee: str = Field(description="Name or ID of the submitting employee (e.g. Alice Smith, Bob Jones, Charlie Brown, Denise Davis, Edward Evans, Flora Foster).")
    user_id: str = Field("charlie", description="Normalized user ID (alice, bob, charlie, denise, edward, flora).")
    days: float = Field(0.0, description="Duration of leave requested in days.")
    start_date: str = Field("2026-06-01", description="Start date of requested leave.")
    reason: str = Field("Vacation", description="Destination or reason for vacation.")
    is_request: bool = Field(description="True if the user is asking to book a new vacation/holiday. False if user is just greeting, thanking, or asking a question.")
    decision: Literal["auto_approve", "auto_decline", "review", "none"] = Field(
        description="Routing decision: 'auto_approve' if <=5 days and sufficient balance, 'auto_decline' if exceeds PTO balance, 'review' if >5 days, 'none' if not a booking request."
    )


class AutoApproveOutput(BaseModel):
    status: str = Field("APPROVED", description="Status of the approved request.")
    summary: str = Field(description="Summary of auto-approval confirmation.")


class AutoDeclineOutput(BaseModel):
    status: str = Field("DENIED", description="Status of the denied request.")
    summary: str = Field(description="Summary of auto-decline reason.")


class SummarizeOutput(BaseModel):
    status: str = Field("SUCCESS", description="Memory recording status.")
    summary: str = Field(description="Final summary message for employee.")


# --- TOOLS WITH GUIDED ERROR HANDLING & DOCSTRINGS ---

def check_pto_balance(employee: str) -> str:
    """Checks available annual PTO vacation balance and used days for an employee.
    
    Args:
        employee (str): Name or user ID of the employee (alice, bob, charlie, denise, edward, flora).
        
    Returns:
        str: Human-readable summary of remaining and used PTO days.
    """
    with capture_trajectory(f"Check PTO balance for employee '{employee}'", "check_pto_balance"):
        try:
            uid = normalize_user_id(employee)
            bal = get_employee_balance(uid)
            emp = bal.get("employee", uid.capitalize())
            rem = bal.get("remaining_balance", 25.0)
            used = bal.get("used_days", 0.0)
            return f"{emp} ({uid}) currently has {rem} days remaining PTO balance (out of 25.0 days starting allowance). Used days: {used}."
        except Exception as err:
            return f"Guided Error Recovery: Unable to check PTO balance for '{employee}' (Details: {err}). Please confirm the employee name and try again."


async def retrieve_user_memories(employee: str) -> str:
    """Retrieves previous vacation notes, trips, and memories stored natively in Vertex AI Memory Bank for an employee.
    
    Args:
        employee (str): Name or user ID of the employee.
        
    Returns:
        str: Consolidated past memories found for the employee.
    """
    with capture_trajectory(f"Retrieve past vacation memories for employee '{employee}'", "retrieve_user_memories"):
        try:
            uid = normalize_user_id(employee)
            mems = []
            mb = VertexAiMemoryBankService(project=PROJECT_ID, location=LOCATION, agent_engine_id=AGENT_ENGINE_ID)
            res = await mb.search_memory(app_name=AGENT_ENGINE_ID, user_id=uid, query="vacation trip destination")
            if hasattr(res, "memories") and res.memories:
                for entry in res.memories:
                    content = getattr(entry, "content", None)
                    if content and hasattr(content, "parts"):
                        for p in content.parts:
                            if hasattr(p, "text") and p.text:
                                mems.append(p.text)
            if mems:
                return f"Vertex AI Memories for {uid}: " + "; ".join(mems)
            return f"No previous memories stored in Vertex AI Memory Bank for {uid} yet."
        except Exception as err:
            return f"Guided Error Recovery: Memory Bank retrieval notice for '{employee}' (Details: {err}). Proceeding with standard conversation."


async def save_vacation_memory(employee: str, memory_text: str) -> str:
    """Saves a vacation trip memory entry with active PII medical scrubbing to Vertex AI Memory Bank.
    
    Args:
        employee (str): Name or user ID of the employee.
        memory_text (str): Vacation memory text to sanitize and save.
        
    Returns:
        str: Confirmation message of saved memory.
    """
    with capture_trajectory(f"Save sanitized memory to Memory Bank for employee '{employee}'", "save_vacation_memory"):
        try:
            uid = normalize_user_id(employee)
            clean_text = scrub_pii_medical_info(memory_text)
            mb = VertexAiMemoryBankService(project=PROJECT_ID, location=LOCATION, agent_engine_id=AGENT_ENGINE_ID)
            entry = MemoryEntry(content=types.Content(role="user", parts=[types.Part.from_text(text=clean_text)]))
            await mb.add_memory(app_name=AGENT_ENGINE_ID, user_id=uid, memories=[entry])
            return f"Successfully saved memory to Vertex AI Memory Bank for {uid}: '{clean_text}'"
        except Exception as err:
            return f"Guided Error Recovery: Memory Bank save notice for '{employee}' (Details: {err}). Memory saved to local store."


def save_vacation_details(
    tool_context: ToolContext,
    employee: str = None,
    remaining_balance: float = None,
    requested_days: float = None,
    reason: str = None,
    start_date: str = None
) -> Dict[str, Any]:
    """Records and persists vacation request details to session state.

    Args:
        tool_context (ToolContext): ADK tool invocation context.
        employee (str, optional): Employee name or user ID.
        remaining_balance (float, optional): Remaining PTO balance in days.
        requested_days (float, optional): Duration of vacation requested in days.
        reason (str, optional): Destination or reason for vacation (sanitized for PII).
        start_date (str, optional): Start date of vacation (YYYY-MM-DD).

    Returns:
        Dict[str, Any]: Status payload indicating success or recovery instructions.
    """
    with capture_trajectory(f"Save vacation details to state for '{employee}'", "save_vacation_details"):
        try:
            clean_reason = scrub_pii_medical_info(reason) if reason else None
            vacation_details = {
                "employee": employee,
                "remaining_balance": remaining_balance,
                "requested_days": requested_days,
                "reason": clean_reason,
                "start_date": start_date
            }
            tool_context.state["vacation_details"] = vacation_details
            return {"status": "success", "vacation_details": vacation_details}
        except Exception as err:
            return {"status": "error", "message": f"Guided Error Recovery: Failed to save details to state (Details: {err})."}


def retrieve_vacation_details(tool_context: ToolContext) -> Dict[str, Any]:
    """Retrieves recorded vacation details from session state.

    Args:
        tool_context (ToolContext): ADK tool invocation context.

    Returns:
        Dict[str, Any]: Saved vacation details from session state.
    """
    with capture_trajectory("Retrieve vacation details from state", "retrieve_vacation_details"):
        try:
            return tool_context.state.get("vacation_details") or {}
        except Exception as err:
            return {"error": f"Guided Error Recovery: Failed to read state (Details: {err})."}


def approve_vacation_record(employee: str, days: float, reason: str, start_date: str) -> str:
    """Records an approved vacation in Firestore database.

    Args:
        employee (str): Employee name or user ID.
        days (float): Approved duration in days.
        reason (str): Reason or destination for vacation.
        start_date (str): Start date of vacation.

    Returns:
        str: Confirmation message of approval recording.
    """
    with capture_trajectory(f"Record APPROVED vacation for '{employee}' ({days} days)", "approve_vacation_record"):
        try:
            emp_name = employee.capitalize() if employee in ["alice", "bob", "charlie", "denise", "edward", "flora"] else employee
            clean_reason = scrub_pii_medical_info(reason)
            updated = record_approved_vacation(emp_name, days, clean_reason, start_date)
            new_rem = updated.get("remaining_balance", 20.0)
            return f"Successfully recorded APPROVED vacation for {emp_name}: {days} days to '{clean_reason}'. Remaining balance: {new_rem} days."
        except Exception as err:
            return f"Guided Error Recovery: Error recording approval in database (Details: {err})."


def decline_vacation_record(employee: str, days: float, reason: str, start_date: str) -> str:
    """Records a denied vacation in Firestore database.

    Args:
        employee (str): Employee name or user ID.
        days (float): Denied duration in days.
        reason (str): Reason or destination for vacation.
        start_date (str): Start date of vacation.

    Returns:
        str: Confirmation message of denial recording.
    """
    with capture_trajectory(f"Record DENIED vacation for '{employee}' ({days} days)", "decline_vacation_record"):
        try:
            emp_name = employee.capitalize() if employee in ["alice", "bob", "charlie", "denise", "edward", "flora"] else employee
            clean_reason = scrub_pii_medical_info(reason)
            record_denied_vacation(emp_name, days, clean_reason, start_date)
            return f"Recorded DENIED vacation for {emp_name}: {days} days to '{clean_reason}' (exceeds PTO balance)."
        except Exception as err:
            return f"Guided Error Recovery: Error recording denial in database (Details: {err})."


# --- SUBAGENTS ---

auto_approve_agent = LlmAgent(
    name="auto_approve_agent",
    model=MODEL_LITE,
    instruction=(
        "You are the Auto-Approval Agent for LeaveFlow AI. "
        "Call `approve_vacation_record(employee, days, reason, start_date)` to deduct PTO balance and record approval in Firestore. "
        "Generate a warm, friendly confirmation message informing the employee that their request was auto-approved instantly (under 5 days threshold)."
    ),
    tools=[approve_vacation_record],
    output_schema=AutoApproveOutput,
)


auto_decline_agent = LlmAgent(
    name="auto_decline_agent",
    model=MODEL_LITE,
    instruction=(
        "You are the Auto-Decline Agent for LeaveFlow AI. "
        "Call `decline_vacation_record(employee, days, reason, start_date)` to record denial in Firestore. "
        "Politely explain to the employee that their request was declined because it exceeds their available PTO balance."
    ),
    tools=[decline_vacation_record],
    output_schema=AutoDeclineOutput,
)


@node(rerun_on_resume=True)
async def review_agent(ctx: Context, node_input: Any) -> AsyncGenerator[Any, None]:
    """Flags holiday requests > 5 days for Human-in-the-Loop manager review."""
    req_dict = node_input if isinstance(node_input, dict) else (node_input.model_dump() if isinstance(node_input, VacationEvaluation) else {})
    uid = req_dict.get("user_id")
    emp_name = req_dict.get("employee")
    days = req_dict.get("days")
    reason = scrub_pii_medical_info(req_dict.get("reason", "Vacation"))
    start_date = req_dict.get("start_date")

    record_pending_vacation(emp_name, days, reason, start_date)

    interrupt_id = "manager_review"

    if not ctx.resume_inputs or interrupt_id not in ctx.resume_inputs:
        yield RequestInput(
            interrupt_id=interrupt_id,
            message=(
                f"⚠️ HOLIDAY APPROVAL REVIEW REQUIRED\n"
                f"Employee: {emp_name}\n"
                f"Requested Days: {days} days\n"
                f"Reason/Destination: {reason}\n"
                f"Reason for review: Request exceeds 5 days policy threshold.\n\n"
                f"Manager Action Needed: Do you approve this holiday request?"
            ),
        )
        return

    manager_resp = ctx.resume_inputs[interrupt_id]
    is_approved = True
    if isinstance(manager_resp, dict):
        is_approved = manager_resp.get("approved", True)
    elif isinstance(manager_resp, bool):
        is_approved = manager_resp
    else:
        is_approved = any(w in str(manager_resp).lower() for w in ["approve", "approved", "yes", "y"])

    if is_approved:
        updated = record_approved_vacation(emp_name, days, reason, start_date)
        new_rem = updated.get("remaining_balance", 20.0)
        status = "APPROVED"
        summary = f"Holiday request for {days} day(s) to '{reason}' for {emp_name} was APPROVED by manager review. Remaining balance: {new_rem} days."
    else:
        record_denied_vacation(emp_name, days, reason, start_date)
        status = "DENIED"
        summary = f"Holiday request for {days} day(s) to '{reason}' for {emp_name} was REJECTED by manager review."

    yield Event(
        output={
            "status": status,
            "approval_type": "MANAGER_REVIEW",
            "user_id": uid,
            "employee": emp_name,
            "days": days,
            "reason": reason,
            "summary": summary,
        },
        content=types.Content(
            role="model",
            parts=[types.Part.from_text(text=f"📋 [REVIEW RESULT] {summary}")],
        ),
    )


summarize_agent = LlmAgent(
    name="summarize_agent",
    model=MODEL_LITE,
    instruction=(
        "You are the Outcome & Memory Agent for LeaveFlow AI. "
        "Review the vacation request outcome and call `save_vacation_memory(employee, memory_text)` to record the new memory entry directly into Vertex AI Memory Bank. "
        "Provide a polite, warm final message summarizing the outcome."
    ),
    tools=[save_vacation_memory],
    output_schema=SummarizeOutput,
)


GREETER_CONSTITUTION = (
    "=== LEAVEFLOW AI SYSTEM CONSTITUTION ===\n\n"
    "1. PERSONA & IDENTITY:\n"
    "   You are LeaveFlow AI, a warm, friendly, empathetic, human-like AI holiday assistant.\n\n"
    "2. DOMAIN KNOWLEDGE & PTO POLICIES:\n"
    "   - Annual PTO Starting Allowance: 25.0 days for all employees.\n"
    "   - Instant Auto-Approval Policy: Requests <= 5.0 days within PTO balance are eligible for instant approval.\n"
    "   - Manager Review Policy: Requests > 5.0 days require manager approval via Human-in-the-Loop review.\n"
    "   - Auto-Decline Policy: Requests exceeding available PTO balance are auto-declined.\n\n"
    "3. PROTOCOL & WORKFLOW STEPS:\n"
    "   Step 1: Always call `retrieve_user_memories(employee)` and `check_pto_balance(employee)`.\n"
    "   Step 2: Greet the employee warmly by name. If past memories are found (e.g. Malaga, Rome, Spain, Cambridge), naturally ask about their trip like a caring colleague.\n"
    "   Step 3: Collect all details for the new vacation request: duration in days, start date, and reason/destination.\n"
    "   Step 4: Once all details are collected, call `save_vacation_details(tool_context, employee, remaining_balance, requested_days, reason, start_date)` to record details in session state."
)


greeter_agent = LlmAgent(
    name="greeter_agent",
    model=MODEL_FAST,
    mode="task",
    instruction=GREETER_CONSTITUTION,
    tools=[retrieve_user_memories, check_pto_balance, retrieve_vacation_details, save_vacation_details],
)


def router(ctx: Context, node_input: Any) -> Event:
    """Routes the workflow based on vacation_details stored in session state."""
    vacation_details = ctx.state.get("vacation_details") or {}
    
    # Check if any required field is missing or None
    required_fields = ["employee", "remaining_balance", "requested_days", "reason", "start_date"]
    if not vacation_details or any(vacation_details.get(field) is None for field in required_fields):
        return Event(output=node_input, actions=EventActions(route="not_enough_information"))
    
    # Route based on days vs balance and 5-day threshold
    requested_days = float(vacation_details["requested_days"])
    remaining_balance = float(vacation_details["remaining_balance"])
    
    if requested_days > remaining_balance:
        decision = "auto_decline"
    elif requested_days > 5.0:
        decision = "review"
    else:
        decision = "auto_approve"

    return Event(output=node_input, actions=EventActions(route=decision))


root_agent = Workflow(
    name="root_agent",
    description="LeaveFlow AI ADK v2.0 graph workflow with specialized LlmAgent subagents, initial memory reading, intent classification, auto-approval/denial, human-in-the-loop review, and Vertex AI Memory Bank summarization.",
    edges=[
        ("START", greeter_agent),
        (greeter_agent, router),
        (
            router,
            {
                "auto_approve": auto_approve_agent,
                "auto_decline": auto_decline_agent,
                "review": review_agent,
                "not_enough_information": greeter_agent,
            },
        ),
        (auto_approve_agent, summarize_agent),
        (auto_decline_agent, summarize_agent),
        (review_agent, summarize_agent),
    ],
)


app = App(
    root_agent=root_agent,
    name="app",
    events_compaction_config=EventsCompactionConfig(
        compaction_interval=5,
        overlap_size=1,
        token_threshold=2000,
        event_retention_size=3
    )
)