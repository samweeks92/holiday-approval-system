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
from typing import Any, Dict, List, Optional

from google.adk.agents import Agent
from google.adk.apps import App
from google.adk.memory.vertex_ai_memory_bank_service import VertexAiMemoryBankService
from google.adk.memory.memory_entry import MemoryEntry
from google.adk.models import Gemini
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

FLASH_MODEL = Gemini(model="gemini-2.5-flash")


def check_pto_balance(employee: str) -> str:
    """Checks available annual PTO vacation balance and used days for an employee (alice, bob, charlie)."""
    uid = normalize_user_id(employee)
    bal = get_employee_balance(uid)
    emp = bal.get("employee", uid.capitalize())
    rem = bal.get("remaining_balance", 25.0)
    used = bal.get("used_days", 0.0)
    return f"{emp} ({uid}) currently has {rem} days remaining PTO balance (out of 25.0 days starting allowance). Used days: {used}."


def retrieve_user_memories(employee: str) -> str:
    """Retrieves previous vacation notes, trips, and memories stored natively in Vertex AI Memory Bank for an employee."""
    uid = normalize_user_id(employee)
    mems = []

    try:
        mb = VertexAiMemoryBankService(project=PROJECT_ID, location=LOCATION, agent_engine_id=AGENT_ENGINE_ID)
        async def _search():
            res = await mb.search_memory(app_name=AGENT_ENGINE_ID, user_id=uid, query="vacation trip destination")
            if hasattr(res, "memories") and res.memories:
                for entry in res.memories:
                    content = getattr(entry, "content", None)
                    if content and hasattr(content, "parts"):
                        for p in content.parts:
                            if hasattr(p, "text") and p.text:
                                mems.append(p.text)

        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        loop.run_until_complete(_search())
        loop.close()
    except Exception as e:
        logger.warning(f"Memory Bank search notice for {uid}: {e}")

    if mems:
        return f"Vertex AI Memories for {uid}: " + "; ".join(mems)

    return f"No previous memories stored in Vertex AI Memory Bank for {uid} yet."


def save_vacation_memory(employee: str, memory_text: str) -> str:
    """Saves a vacation trip memory entry directly to Vertex AI Memory Bank."""
    uid = normalize_user_id(employee)
    try:
        mb = VertexAiMemoryBankService(project=PROJECT_ID, location=LOCATION, agent_engine_id=AGENT_ENGINE_ID)
        entry = MemoryEntry(content=types.Content(role="user", parts=[types.Part.from_text(text=memory_text)]))
        async def _save():
            await mb.add_memory(app_name=AGENT_ENGINE_ID, user_id=uid, memories=[entry])

        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        loop.run_until_complete(_save())
        loop.close()
        return f"Successfully saved memory to Vertex AI Memory Bank for {uid}: '{memory_text}'"
    except Exception as e:
        return f"Vertex AI Memory save notice for {uid}: {e}"


def submit_holiday_request(employee: str, days: float, start_date: str, reason: str) -> str:
    """Submits a holiday vacation request, validates PTO balance, auto-approves (<= 5 days) or records pending (> 5 days)."""
    uid = normalize_user_id(employee)
    bal = get_employee_balance(uid)
    emp = bal.get("employee", uid.capitalize())
    remaining = float(bal.get("remaining_balance", 25.0))

    if days > remaining:
        record_denied_vacation(emp, days, reason, start_date)
        save_vacation_memory(uid, f"{emp} requested {days} days to {reason} (Denied - Insufficient PTO balance)")
        return f"AUTO_DENIED: Insufficient PTO balance. {emp} requested {days} days, but only has {remaining} days available."

    if days <= 5.0:
        updated = record_approved_vacation(emp, days, reason, start_date)
        new_rem = updated.get("remaining_balance", 20.0)
        save_vacation_memory(uid, f"{emp} booked a {days}-day vacation to {reason}")
        return f"AUTO_APPROVED: Holiday request for {days} day(s) to '{reason}' for {emp} was AUTO-APPROVED! Remaining balance updated to {new_rem} days."

    record_pending_vacation(emp, days, reason, start_date)
    save_vacation_memory(uid, f"{emp} requested {days} days vacation to {reason} (Pending Manager Approval)")
    return f"PENDING_MANAGER_APPROVAL: Holiday request for {days} day(s) to '{reason}' exceeds 5 days threshold. Recorded as PENDING and submitted to manager for review."


SYSTEM_INSTRUCTION = """
You are LeaveFlow AI, an AI holiday assistant for company employees (Alice Smith, Bob Jones, Charlie Brown).
You speak naturally, warmly, and helpfully like a human HR colleague.

Key Instructions:
1. At the very beginning of a session or when an employee first greets you / tells you who they are (e.g. "hi", "hello", "alice", "charlie"):
   - ALWAYS call `retrieve_user_memories(employee)` as the FIRST thing you do to check their past vacation memories in Vertex AI Memory Bank.
   - Greet them warmly by name and reference their past trip or memory naturally if found (e.g., "Hey Alice! Welcome back! How was your vacation to Malaga?").
2. When an employee asks to book a vacation or mentions a new trip:
   - If travel dates, duration in days, or destination/reason are missing, ask for them naturally (e.g., "Sure, I can help with that! What dates are you planning to travel and how many days?").
   - Call `check_pto_balance(employee)` to verify available balance.
   - Once dates, duration (days), and destination/reason are provided, call `submit_holiday_request(employee, days, start_date, reason)`.
   - Respond naturally with the result (e.g., "That's all booked and approved! Enjoy your time in Spain!", or "Since that's over 5 days, I've submitted this to your manager for approval!").
3. Keep all responses friendly, human-like, concise, and helpful.
"""

root_agent = Agent(
    name="leave_approval_agent",
    description="Autonomous human-like Leave Approval Agent with PTO balance validation and native Vertex AI Memory Bank.",
    model=FLASH_MODEL,
    instruction=SYSTEM_INSTRUCTION,
    tools=[check_pto_balance, retrieve_user_memories, save_vacation_memory, submit_holiday_request]
)

app = App(
    root_agent=root_agent,
    name="app"
)
