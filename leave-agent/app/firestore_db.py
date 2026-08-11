# Copyright 2026 Google LLC
# Firestore & Vertex AI Memory Bank Helper for LeaveFlow AI Vacation Management

import asyncio
import datetime
import logging
import os
import uuid
from typing import Any, Dict, List, Optional
from google.cloud import firestore
from google.genai import types

logger = logging.getLogger("firestore_db")

PROJECT_ID = os.environ.get("PROJECT_ID", "ai-sandbox-sw")
LOCATION = os.environ.get("LOCATION", "europe-west1")
DATABASE_NAME = os.environ.get("FIRESTORE_DATABASE", "holiday-data")
AGENT_ENGINE_ID = os.environ.get("AGENT_ENGINE_ID", "6128897715548979200")

INITIAL_USERS = {
    "alice": {"employee": "Alice Smith", "user_id": "alice", "starting_balance": 25.0, "used_days": 0.0, "remaining_balance": 25.0, "history": []},
    "bob": {"employee": "Bob Jones", "user_id": "bob", "starting_balance": 25.0, "used_days": 0.0, "remaining_balance": 25.0, "history": []},
    "charlie": {"employee": "Charlie Brown", "user_id": "charlie", "starting_balance": 25.0, "used_days": 0.0, "remaining_balance": 25.0, "history": []},
}

IN_MEMORY_BALANCES: Dict[str, Dict[str, Any]] = {k: v.copy() for k, v in INITIAL_USERS.items()}
IN_MEMORY_MEMORIES: Dict[str, List[str]] = {"alice": [], "bob": [], "charlie": []}


def get_firestore_client() -> Optional[firestore.Client]:
    try:
        return firestore.Client(project=PROJECT_ID, database=DATABASE_NAME)
    except Exception as err:
        logger.warning(f"Firestore client init fallback (holiday-data): {err}")
        return None


def normalize_user_id(employee_str: str) -> str:
    if not employee_str:
        return "alice"
    clean = str(employee_str).strip().lower()
    if "bob" in clean:
        return "bob"
    if "charlie" in clean:
        return "charlie"
    return "alice"


def get_employee_balance(employee: str) -> Dict[str, Any]:
    uid = normalize_user_id(employee)
    client = get_firestore_client()
    if client:
        try:
            doc_ref = client.collection("vacation_balances").document(uid)
            snapshot = doc_ref.get()
            if snapshot.exists:
                data = snapshot.to_dict() or {}
                IN_MEMORY_BALANCES[uid] = data
                return data
            else:
                init_data = INITIAL_USERS.get(uid, INITIAL_USERS["alice"]).copy()
                doc_ref.set(init_data)
                IN_MEMORY_BALANCES[uid] = init_data
                return init_data
        except Exception as err:
            logger.warning(f"Firestore get_employee_balance error for {uid}: {err}")

    return IN_MEMORY_BALANCES.get(uid, INITIAL_USERS["alice"])


def get_all_balances() -> List[Dict[str, Any]]:
    client = get_firestore_client()
    results = []
    if client:
        try:
            docs = client.collection("vacation_balances").stream()
            for d in docs:
                data = d.to_dict()
                if data:
                    results.append(data)
            if results:
                return sorted(results, key=lambda x: x.get("user_id", ""))
        except Exception as err:
            logger.warning(f"Firestore get_all_balances error: {err}")

    for uid in ["alice", "bob", "charlie"]:
        get_employee_balance(uid)
    return [IN_MEMORY_BALANCES[uid] for uid in ["alice", "bob", "charlie"]]


def record_pending_vacation(employee: str, days: float, reason: str, start_date: str = "2026-06-01") -> Dict[str, Any]:
    uid = normalize_user_id(employee)
    curr_data = get_employee_balance(uid)

    entry_id = str(uuid.uuid4())[:8]
    pending_entry = {
        "id": entry_id,
        "days": days,
        "reason": reason,
        "start_date": start_date,
        "status": "PENDING",
        "timestamp": datetime.datetime.now(datetime.timezone.utc).isoformat()
    }

    updated_history = curr_data.get("history", []) + [pending_entry]
    curr_data["history"] = updated_history
    IN_MEMORY_BALANCES[uid] = curr_data

    client = get_firestore_client()
    if client:
        try:
            client.collection("vacation_balances").document(uid).set(curr_data)
            logger.info(f"Recorded PENDING vacation for {uid} ({entry_id})")
        except Exception as err:
            logger.warning(f"Firestore pending update error for {uid}: {err}")

    # Also save memory to Vertex AI Agent Engine Memory Bank
    save_user_memory(employee, f"{uid.capitalize()} requested {days} days vacation for {reason}")

    return curr_data


def record_approved_vacation(employee: str, days: float, reason: str, start_date: str = "2026-06-01") -> Dict[str, Any]:
    uid = normalize_user_id(employee)
    curr_data = get_employee_balance(uid)

    new_used = float(curr_data.get("used_days", 0.0)) + days
    new_remaining = max(0.0, float(curr_data.get("starting_balance", 25.0)) - new_used)

    history = curr_data.get("history", [])
    updated = False
    for h in reversed(history):
        if h.get("status") in ["PENDING", "PENDING_MANAGER_REVIEW"] and abs(h.get("days", 0) - days) < 0.1:
            h["status"] = "APPROVED"
            h["days"] = days
            h["reason"] = reason
            updated = True
            break

    if not updated:
        history.append({
            "id": str(uuid.uuid4())[:8],
            "days": days,
            "reason": reason,
            "start_date": start_date,
            "status": "APPROVED",
            "timestamp": datetime.datetime.now(datetime.timezone.utc).isoformat()
        })

    updated_data = {
        "employee": curr_data.get("employee", uid.capitalize()),
        "user_id": uid,
        "starting_balance": curr_data.get("starting_balance", 25.0),
        "used_days": new_used,
        "remaining_balance": new_remaining,
        "history": history,
    }

    IN_MEMORY_BALANCES[uid] = updated_data

    client = get_firestore_client()
    if client:
        try:
            client.collection("vacation_balances").document(uid).set(updated_data)
            logger.info(f"Recorded APPROVED vacation in Firestore for {uid}: -{days} days")
        except Exception as err:
            logger.warning(f"Firestore update error for {uid}: {err}")

    # Save memory to Vertex AI Memory Bank
    save_user_memory(employee, f"Vacation approved to {reason} for {days} days")

    return updated_data


def record_denied_vacation(employee: str, days: float, reason: str, start_date: str = "2026-06-01") -> Dict[str, Any]:
    uid = normalize_user_id(employee)
    curr_data = get_employee_balance(uid)

    history = curr_data.get("history", [])
    updated = False
    for h in reversed(history):
        if h.get("status") in ["PENDING", "PENDING_MANAGER_REVIEW"] and abs(h.get("days", 0) - days) < 0.1:
            h["status"] = "DENIED"
            updated = True
            break

    if not updated:
        history.append({
            "id": str(uuid.uuid4())[:8],
            "days": days,
            "reason": reason,
            "start_date": start_date,
            "status": "DENIED",
            "timestamp": datetime.datetime.now(datetime.timezone.utc).isoformat()
        })

    curr_data["history"] = history
    IN_MEMORY_BALANCES[uid] = curr_data

    client = get_firestore_client()
    if client:
        try:
            client.collection("vacation_balances").document(uid).set(curr_data)
        except Exception as err:
            logger.warning(f"Firestore deny error for {uid}: {err}")

    return curr_data


def _async_save_vertex_memory(uid: str, memory_text: str):
    """Background task to save memory to Vertex AI Agent Engine Memory Bank."""
    try:
        from google.adk.memory.vertex_ai_memory_bank_service import VertexAiMemoryBankService
        from google.adk.memory.memory_entry import MemoryEntry

        clean_engine_id = AGENT_ENGINE_ID.split("/")[-1]
        mb = VertexAiMemoryBankService(project=PROJECT_ID, location=LOCATION, agent_engine_id=clean_engine_id)

        content = types.Content(role="user", parts=[types.Part.from_text(text=memory_text)])
        entry = MemoryEntry(content=content)

        async def _run():
            await mb.add_memory(app_name=clean_engine_id, user_id=uid, memories=[entry])

        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        loop.run_until_complete(_run())
        loop.close()
        logger.info(f"Saved memory to Vertex AI Memory Bank for {uid}: {memory_text}")
    except Exception as err:
        logger.warning(f"Vertex AI Memory Bank async save notice for {uid}: {err}")


def save_user_memory(employee: str, memory_text: str) -> List[str]:
    uid = normalize_user_id(employee)
    mems = get_user_memories(uid)
    if memory_text not in mems:
        mems.append(memory_text)

    IN_MEMORY_MEMORIES[uid] = mems

    client = get_firestore_client()
    if client:
        try:
            client.collection("user_memories").document(uid).set({"memories": mems})
        except Exception as err:
            logger.warning(f"Firestore memory save error for {uid}: {err}")

    # Synchronously or in background save to Vertex AI Memory Bank
    try:
        asyncio.create_task(asyncio.to_thread(_async_save_vertex_memory, uid, memory_text))
    except Exception:
        _async_save_vertex_memory(uid, memory_text)

    return mems


def get_user_memories(employee: str) -> List[str]:
    uid = normalize_user_id(employee)
    client = get_firestore_client()
    if client:
        try:
            snapshot = client.collection("user_memories").document(uid).get()
            if snapshot.exists:
                mems = snapshot.to_dict().get("memories", [])
                IN_MEMORY_MEMORIES[uid] = mems
                return mems
        except Exception as err:
            logger.warning(f"Firestore get memories error for {uid}: {err}")

    return IN_MEMORY_MEMORIES.get(uid, [])
