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
from typing import Any, AsyncGenerator, Dict, List, Literal, Optional

from google.adk.agents import LlmAgent
from google.adk.agents.context import Context
from google.adk.apps import App
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
)

# Configure Structured JSON Logging
logging.basicConfig(level=logging.INFO, format='%(message)s')
logger = logging.getLogger("leave_agent_logger")

PROJECT_ID = os.environ.get("PROJECT_ID", "ai-sandbox-sw")
LOCATION = os.environ.get("LOCATION", "europe-west1")
AGENT_ENGINE_ID = os.environ.get("AGENT_ENGINE_ID", "6128897715548979200").split("/")[-1]

MODEL = "gemini-2.5-flash"


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


# --- TOOLS ---
def check_pto_balance(employee: str) -> str:
    """Checks available annual PTO vacation balance and used days for an employee (alice, bob, charlie, denise, edward, flora)."""
    uid = normalize_user_id(employee)
    bal = get_employee_balance(uid)
    emp = bal.get("employee", uid.capitalize())
    rem = bal.get("remaining_balance", 25.0)
    used = bal.get("used_days", 0.0)
    return f"{emp} ({uid}) currently has {rem} days remaining PTO balance (out of 25.0 days starting allowance). Used days: {used}."


async def retrieve_user_memories(employee: str) -> str:
    """Retrieves previous vacation notes, trips, and memories stored natively in Vertex AI Memory Bank for an employee."""
    uid = normalize_user_id(employee)
    mems = []

    try:
        mb = VertexAiMemoryBankService(project=PROJECT_ID, location=LOCATION, agent_engine_id=AGENT_ENGINE_ID)
        res = await mb.search_memory(app_name=AGENT_ENGINE_ID, user_id=uid, query="vacation trip destination")
        if hasattr(res, "memories") and res.memories:
            for entry in res.memories:
                content = getattr(entry, "content", None)
                if content and hasattr(content, "parts"):
                    for p in content.parts:
                        if hasattr(p, "text") and p.text:
                            mems.append(p.text)
    except Exception as e:
        logger.warning(f"Memory Bank search notice for {uid}: {e}")

    if mems:
        return f"Vertex AI Memories for {uid}: " + "; ".join(mems)

    return f"No previous memories stored in Vertex AI Memory Bank for {uid} yet."


async def save_vacation_memory(employee: str, memory_text: str) -> str:
    """Saves a vacation trip memory entry directly to Vertex AI Memory Bank."""
    uid = normalize_user_id(employee)
    try:
        mb = VertexAiMemoryBankService(project=PROJECT_ID, location=LOCATION, agent_engine_id=AGENT_ENGINE_ID)
        entry = MemoryEntry(content=types.Content(role="user", parts=[types.Part.from_text(text=memory_text)]))
        await mb.add_memory(app_name=AGENT_ENGINE_ID, user_id=uid, memories=[entry])
        return f"Successfully saved memory to Vertex AI Memory Bank for {uid}: '{memory_text}'"
    except Exception as e:
        return f"Vertex AI Memory save notice for {uid}: {e}"
    
def save_vacation_details(
    tool_context: ToolContext,
    employee: str = None,
    remaining_balance: float = None,
    requested_days: float = None,
    reason: str = None,
    start_date: str = None
) -> Dict[str, Any]:
    """
    Tool to record and save vacation details to state.

    Args:
        employee: The employee name to store in session state
        requested_days: The number of days to store in session state
        remaining_balance: The number of days remaining in the employee's PTO balance
        reason: The reason for the vacation to store in session state
        start_date: The start date of the vacation to store in session state
    """

    vacation_details = {
        "employee":employee,
        "remaining_balance":remaining_balance,
        "requested_days":requested_days,
        "reason":reason,
        "start_date":start_date
        
    }
    tool_context.state["vacation_details"] = vacation_details

    return {"status": "success"}


def retrieve_vacation_details(tool_context: ToolContext) -> Dict[str, Any]:
    """
    Tool to retrieve vacation details from session state.
    """
    # Read from session state
    vacation_details = tool_context.state.get("vacation_details")

    return vacation_details


def approve_vacation_record(employee: str, days: float, reason: str, start_date: str) -> str:
    """Records an approved vacation in Firestore database."""
    emp_name = employee.capitalize() if employee in ["alice", "bob", "charlie"] else employee
    updated = record_approved_vacation(emp_name, days, reason, start_date)
    new_rem = updated.get("remaining_balance", 20.0)
    return f"Successfully recorded APPROVED vacation for {emp_name}: {days} days to '{reason}'. Remaining balance: {new_rem} days."


def decline_vacation_record(employee: str, days: float, reason: str, start_date: str) -> str:
    """Records a denied vacation in Firestore database."""
    emp_name = employee.capitalize() if employee in ["alice", "bob", "charlie"] else employee
    record_denied_vacation(emp_name, days, reason, start_date)
    return f"Recorded DENIED vacation for {emp_name}: {days} days to '{reason}' (exceeds PTO balance)."


# --- SUBAGENTS ---
# process_vacation_agent = LlmAgent(
#     name="process_vacation_agent",
#     model=MODEL,
#     instruction=(
#         "You are an expert holiday request evaluation agent. "
#         "Analyze the user's message and determine if they are asking to book a NEW vacation or holiday request. "
#         "Call `check_pto_balance(employee)` to check the employee's available PTO balance. "
#         "Extract employee, user_id (alice, bob, charlie), requested days (float), start_date, and reason/destination. "
#         "Set `is_request=True` if the user is asking to book a new holiday. Set `is_request=False` if user is just greeting, thanking, or asking a question. "
#         "Set `decision` to:\n"
#         "- 'auto_approve' if requested days <= 5.0 and days <= remaining PTO balance.\n"
#         "- 'auto_decline' if requested days > remaining PTO balance.\n"
#         "- 'review' if requested days > 5.0 and days <= remaining PTO balance.\n"
#     ),
#     tools=[check_pto_balance],
#     output_schema=VacationEvaluation,
# )

auto_approve_agent = LlmAgent(
    name="auto_approve_agent",
    model=MODEL,
    instruction=(
        "You are the Auto-Approval Agent for LeaveFlow AI. "
        "Call `approve_vacation_record(employee, days, reason, start_date)` to deduct PTO balance and record approval in Firestore. "
        "Generate a warm, friendly confirmation message informing the employee that their request was auto-approved instantly (under 5 days threshold)."
    ),
    tools=[approve_vacation_record],
)


auto_decline_agent = LlmAgent(
    name="auto_decline_agent",
    model=MODEL,
    instruction=(
        "You are the Auto-Decline Agent for LeaveFlow AI. "
        "Call `decline_vacation_record(employee, days, reason, start_date)` to record denial in Firestore. "
        "Politely explain to the employee that their request was declined because it exceeds their available PTO balance."
    ),
    tools=[decline_vacation_record],
)


@node(rerun_on_resume=True)
async def review_agent(ctx: Context, node_input: Any) -> AsyncGenerator[Any, None]:
    """Flags holiday requests > 5 days for Human-in-the-Loop manager review."""
    req_dict = node_input if isinstance(node_input, dict) else (node_input.model_dump() if isinstance(node_input, VacationEvaluation) else {})
    uid = req_dict.get("user_id", "charlie")
    emp_name = req_dict.get("employee", "Charlie Brown")
    days = req_dict.get("days", 6.0)
    reason = req_dict.get("reason", "Vacation")
    start_date = req_dict.get("start_date", "2026-06-01")

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
    model=MODEL,
    instruction=(
        "You are the Outcome & Memory Agent for LeaveFlow AI. "
        "Review the vacation request outcome and call `save_vacation_memory(employee, memory_text)` to record the new memory entry directly into Vertex AI Memory Bank. "
        "Provide a polite, warm final message summarizing the outcome."
    ),
    tools=[save_vacation_memory],
)


greeter_agent = LlmAgent(
    name="greeter_agent",
    model=MODEL,
    mode="task",
    instruction=(
        "You are LeaveFlow AI, a warm, friendly, human-like AI holiday assistant. "
        "Call `retrieve_user_memories(employee)` and `check_pto_balance(employee)` to look up past notes and PTO balance.\n\n"
        "1. Greet the employee warmly by name. If past memories are found (e.g. Malaga, Rome, Spain), naturally ask them about their trip like a caring colleague (e.g. 'Hey Alice, so how was Malaga? Enough time to switch off?').\n"
        "2. Ensure to collect all details of the new vacation request (duration/days, start date, reason/destination).\n"
        "3. After collecting all details of the new vacation request (duration/days, start date, reason/destination), use tools retrieve_vacation_details and save_vacation_details to save these details to session state"
    ),
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
)