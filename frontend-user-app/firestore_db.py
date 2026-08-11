# Copyright 2026 Google LLC
# Firestore Database Helper for LeaveFlow AI Vacation Management (User App)

import datetime
import logging
import os
import uuid
from typing import Any, Dict, List, Optional
from google.cloud import firestore

logger = logging.getLogger("firestore_db_user_app")

PROJECT_ID = os.environ.get("PROJECT_ID", "ai-sandbox-sw")
DATABASE_NAME = os.environ.get("FIRESTORE_DATABASE", "holiday-data")

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

    return curr_data
