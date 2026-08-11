# Copyright 2026 Google LLC
# LeaveFlow AI - Manager Portal (Pending Approvals & Vacation History)

import asyncio
import json
import os
import re
from typing import Any, Dict, List, Optional
import requests

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse
from pydantic import BaseModel
import google.auth
from google.auth.transport.requests import Request as AuthRequest
from google.adk.sessions.vertex_ai_session_service import VertexAiSessionService

from firestore_db import (
    get_all_balances,
    get_employee_balance,
    record_approved_vacation,
    record_denied_vacation,
)

app = FastAPI(title="LeaveFlow AI - Vacation & PTO Manager Portal")

PROJECT_ID = os.environ.get("PROJECT_ID", "ai-sandbox-sw")
LOCATION = os.environ.get("LOCATION", "europe-west1")
AGENT_RUNTIME_ID = os.environ.get(
    "AGENT_RUNTIME_ID",
    f"projects/110071138645/locations/{LOCATION}/reasoningEngines/6128897715548979200"
)

session_service = VertexAiSessionService(project=PROJECT_ID, location=LOCATION)


class ActionRequest(BaseModel):
    approved: bool
    user_id: Optional[str] = "alice"
    interrupt_id: Optional[str] = "manager_review"
    entry_id: Optional[str] = None
    days: Optional[float] = 1.0
    reason: Optional[str] = "Vacation"


def get_auth_headers() -> Dict[str, str]:
    credentials, _ = google.auth.default()
    credentials.refresh(AuthRequest())
    return {
        "Authorization": f"Bearer {credentials.token}",
        "Content-Type": "application/json",
    }


CACHED_PENDING_APPROVALS: List[Dict[str, Any]] = []
CACHE_LOCK = asyncio.Lock()


async def refresh_pending_cache():
    """Fetches pending approvals from Agent Engine sessions AND Firestore history."""
    global CACHED_PENDING_APPROVALS
    pending_approvals = []
    seen_keys = set()

    # 1. Fetch from Firestore history for PENDING items
    try:
        all_bal = get_all_balances()
        for b in all_bal:
            uid = b.get("user_id", "alice")
            emp = b.get("employee", uid.capitalize())
            for h in b.get("history", []):
                if h.get("status") in ["PENDING", "PENDING_MANAGER_REVIEW"]:
                    key = f"{uid}-{h.get('days')}-{h.get('reason')}"
                    seen_keys.add(key)
                    pending_approvals.append({
                        "session_id": h.get("id", f"fs-{uid}"),
                        "user_id": uid,
                        "entry_id": h.get("id"),
                        "interrupt_id": "manager_review",
                        "message": f"Holiday review required for {emp} ({h.get('days')} days)",
                        "days": h.get("days", 6.0),
                        "reason": h.get("reason", "Vacation Request"),
                        "employee": emp,
                        "department": "Engineering" if uid == "alice" else ("Product" if uid == "bob" else "Marketing"),
                        "source": "firestore"
                    })
    except Exception:
        pass

    # 2. Fetch from Agent Engine sessions
    target_users = ["alice", "bob", "charlie", "denise", "edward", "flora", "vais-query-reasoning-engine", "default-user"]
    emp_map = {
        "alice": "Alice Smith",
        "bob": "Bob Jones",
        "charlie": "Charlie Brown",
        "denise": "Denise Davis",
        "edward": "Edward Evans",
        "flora": "Flora Foster",
    }
    for user_id in target_users:
        try:
            sessions_resp = await session_service.list_sessions(app_name=AGENT_RUNTIME_ID, user_id=user_id)
            sessions_list = sessions_resp.sessions if hasattr(sessions_resp, "sessions") else sessions_resp

            for s in (sessions_list or []):
                sid = getattr(s, "id", getattr(s, "session_id", None))
                if not sid:
                    continue

                try:
                    session_obj = await session_service.get_session(app_name=AGENT_RUNTIME_ID, user_id=user_id, session_id=sid)
                    if not hasattr(session_obj, "events"):
                        continue

                    has_request = False
                    has_response = False
                    interrupt_id = "manager_review"
                    message = None
                    days = None
                    reason = None
                    employee = emp_map.get(user_id, user_id.capitalize())

                    for ev in session_obj.events:
                        if hasattr(ev, "actions") and ev.actions and hasattr(ev.actions, "state_delta"):
                            sd = ev.actions.state_delta or {}
                            if "leave_request" in sd:
                                req_data = sd["leave_request"]
                                if isinstance(req_data, dict):
                                    days = req_data.get("days")
                                    reason = req_data.get("reason")
                                    employee = req_data.get("employee", employee)

                        content = getattr(ev, "content", None)
                        if content and getattr(content, "parts", None):
                            for part in content.parts:
                                fc = getattr(part, "function_call", None)
                                if fc and getattr(fc, "name", None) == "adk_request_input":
                                    has_request = True
                                    interrupt_id = getattr(fc, "id", None) or "manager_review"

                                fr = getattr(part, "function_response", None)
                                if fr and getattr(fr, "name", None) == "adk_request_input":
                                    has_response = True

                    if has_request and not has_response:
                        key = f"{user_id}-{days}-{reason}"
                        if key not in seen_keys:
                            seen_keys.add(key)
                            pending_approvals.append({
                                "session_id": sid,
                                "user_id": user_id,
                                "interrupt_id": interrupt_id,
                                "message": message or f"Holiday approval required for {employee}",
                                "days": days or 6.0,
                                "reason": reason or "Vacation Request",
                                "employee": employee,
                                "department": "Engineering",
                                "source": "agent_engine"
                            })
                except Exception:
                    pass
        except Exception:
            pass

    async with CACHE_LOCK:
        CACHED_PENDING_APPROVALS = pending_approvals


async def background_poll_loop():
    while True:
        try:
            await refresh_pending_cache()
        except Exception:
            pass
        await asyncio.sleep(2.0)


@app.on_event("startup")
async def startup_event():
    asyncio.create_task(refresh_pending_cache())
    asyncio.create_task(background_poll_loop())


@app.get("/api/pending")
async def list_pending_approvals():
    async with CACHE_LOCK:
        current_pending = list(CACHED_PENDING_APPROVALS)

    if not current_pending:
        asyncio.create_task(refresh_pending_cache())

    return {"status": "success", "count": len(current_pending), "pending": current_pending}


@app.get("/api/balances")
async def list_balances():
    balances = get_all_balances()
    return {"status": "success", "balances": balances}


@app.post("/api/action/{session_id}")
async def take_approval_action(session_id: str, req: ActionRequest):
    target_user_id = req.user_id or "alice"
    interrupt_id = req.interrupt_id or "manager_review"
    days = req.days or 6.0
    reason = req.reason or "Vacation"

    # 1. Update Firestore State
    if req.approved:
        record_approved_vacation(target_user_id, days, reason)
    else:
        record_denied_vacation(target_user_id, days, reason)

    # 2. Resume Agent Engine Session if applicable
    review_text = f"Decision recorded ({'APPROVED ✅' if req.approved else 'REJECTED ❌'}). Balance and history updated."
    if session_id and not session_id.startswith("fs-"):
        try:
            stream_url = f"https://{LOCATION}-aiplatform.googleapis.com/v1/{AGENT_RUNTIME_ID}:streamQuery"
            headers = get_auth_headers()
            resume_message = {
                "role": "user",
                "parts": [
                    {
                        "function_response": {
                            "id": interrupt_id,
                            "name": "adk_request_input",
                            "response": {"approved": req.approved},
                        }
                    }
                ],
            }
            payload = {
                "class_method": "async_stream_query",
                "input": {
                    "user_id": target_user_id,
                    "session_id": session_id,
                    "message": resume_message,
                },
            }
            resp = requests.post(stream_url, headers=headers, json=payload, timeout=30)
            if resp.ok:
                review_text = f"Agent Engine Session Finalized: {'APPROVED ✅' if req.approved else 'REJECTED ❌'}"
        except Exception:
            pass

    asyncio.create_task(refresh_pending_cache())

    return {
        "status": "success",
        "session_id": session_id,
        "approved": req.approved,
        "review": review_text,
    }


@app.get("/", response_class=HTMLResponse)
async def serve_dashboard(request: Request):
    html_content = """<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>LeaveFlow AI - Vacation & PTO Manager Portal</title>
    <link href="https://fonts.googleapis.com/css2?family=Outfit:wght@300;400;500;600;700&display=swap" rel="stylesheet">
    <style>
        :root {
            --bg-color: #f8fafc;
            --card-bg: rgba(255, 255, 255, 0.92);
            --primary-blue: #0284c7;
            --ocean-light: #e0f2fe;
            --accent-gold: #f59e0b;
            --gold-light: #fef3c7;
            --emerald-green: #10b981;
            --green-light: #d1fae5;
            --rose-red: #ef4444;
            --red-light: #fee2e2;
            --text-dark: #0f172a;
            --text-muted: #64748b;
            --border-light: #e2e8f0;
            --shadow-subtle: 0 10px 25px -5px rgba(0, 0, 0, 0.05);
            --shadow-hover: 0 20px 25px -5px rgba(2, 132, 199, 0.12);
        }

        * { box-sizing: border-box; margin: 0; padding: 0; font-family: 'Outfit', sans-serif; }

        body {
            background-color: var(--bg-color);
            background-image: 
                radial-gradient(at 0% 0%, rgba(14, 165, 233, 0.08) 0px, transparent 50%),
                radial-gradient(at 100% 0%, rgba(245, 158, 11, 0.08) 0px, transparent 50%);
            color: var(--text-dark);
            padding: 30px 20px;
            min-height: 100vh;
        }

        .container { max-width: 1200px; margin: 0 auto; }

        header {
            background: linear-gradient(135deg, #ffffff 0%, #f0f9ff 100%);
            border: 1px solid var(--border-light);
            border-radius: 24px;
            padding: 28px 36px;
            box-shadow: var(--shadow-subtle);
            display: flex;
            justify-content: space-between;
            align-items: center;
            margin-bottom: 32px;
        }

        .brand-title { font-size: 28px; font-weight: 700; }
        .brand-title span { color: var(--primary-blue); }

        .section-title {
            font-size: 20px;
            font-weight: 600;
            margin-bottom: 16px;
            display: flex;
            align-items: center;
            gap: 10px;
        }

        .balance-grid {
            display: grid;
            grid-template-columns: repeat(auto-fit, minmax(320px, 1fr));
            gap: 20px;
            margin-bottom: 32px;
        }

        .balance-card {
            background: var(--card-bg);
            border: 1px solid var(--border-light);
            border-radius: 20px;
            padding: 24px;
            box-shadow: var(--shadow-subtle);
        }

        .user-header { display: flex; justify-content: space-between; align-items: center; margin-bottom: 16px; }
        .user-avatar {
            width: 44px; height: 44px; border-radius: 50%;
            background: var(--ocean-light); color: var(--primary-blue);
            display: flex; align-items: center; justify-content: center;
            font-weight: 700; font-size: 18px;
        }

        .rem-days { font-size: 32px; font-weight: 700; color: var(--primary-blue); }
        .progress-bar-bg { height: 10px; background: var(--border-light); border-radius: 9999px; overflow: hidden; }
        .progress-bar-fill { height: 100%; background: linear-gradient(90deg, var(--primary-blue), #38bdf8); border-radius: 9999px; }

        /* Pending Approvals */
        .approval-card {
            background: #ffffff;
            border: 1px solid #e0f2fe;
            border-radius: 20px;
            padding: 24px;
            box-shadow: var(--shadow-subtle);
            margin-bottom: 16px;
            display: flex;
            justify-content: space-between;
            align-items: center;
            gap: 20px;
        }

        .badge-pending {
            background: var(--gold-light);
            color: #b45309;
            font-size: 12px;
            font-weight: 600;
            padding: 4px 12px;
            border-radius: 9999px;
            display: inline-block;
            margin-bottom: 8px;
        }

        .btn {
            padding: 12px 24px;
            border-radius: 12px;
            font-weight: 600;
            font-size: 14px;
            cursor: pointer;
            border: none;
        }

        .btn-approve { background: linear-gradient(135deg, var(--emerald-green), #059669); color: white; }
        .btn-reject { background: #ffffff; color: var(--rose-red); border: 1px solid var(--rose-red); }

        .empty-state {
            background: white; border: 1px dashed var(--border-light);
            border-radius: 20px; padding: 48px; text-align: center; color: var(--text-muted);
        }

        /* History Table */
        .history-table {
            width: 100%; border-collapse: separate; border-spacing: 0;
            background: white; border-radius: 20px; border: 1px solid var(--border-light);
            overflow: hidden; box-shadow: var(--shadow-subtle);
        }

        .history-table th { background: #f8fafc; padding: 16px 24px; text-align: left; font-size: 13px; font-weight: 600; color: var(--text-muted); border-bottom: 1px solid var(--border-light); }
        .history-table td { padding: 16px 24px; font-size: 14px; border-bottom: 1px solid #f1f5f9; }

        .status-APPROVED { background: var(--green-light); color: #065f46; padding: 4px 12px; border-radius: 9999px; font-size: 12px; font-weight: 700; }
        .status-DENIED { background: var(--red-light); color: #991b1b; padding: 4px 12px; border-radius: 9999px; font-size: 12px; font-weight: 700; }
    </style>
</head>
<body>
    <div class="container">
        <header>
            <div>
                <div class="brand-title">LeaveFlow AI <span>Vacation Hub</span></div>
                <div style="color: var(--text-muted); font-size: 14px;">Autonomous Holiday Approvals & Real-Time PTO Allowance System</div>
            </div>
            <div style="font-size: 32px;">🌴 ☀️ ✈️</div>
        </header>

        <!-- Employee Balances -->
        <div class="section-title">👥 Employee Vacation Balances (25.0 Days Allowance)</div>
        <div class="balance-grid" id="balances-container">
            <div class="balance-card">Loading employee PTO balances...</div>
        </div>

        <!-- Pending Approvals -->
        <div class="section-title">⏳ Pending Manager Approvals (> 5 Days Policy Threshold)</div>
        <div id="pending-container">
            <div class="empty-state">
                <div style="font-size: 48px;">🏖️</div>
                <h3>Checking for pending requests...</h3>
            </div>
        </div>

        <!-- Approved Vacation History -->
        <div style="margin-top: 32px;">
            <div class="section-title">📜 Approved & Denied Vacation History Log</div>
            <table class="history-table">
                <thead>
                    <tr>
                        <th>Employee</th>
                        <th>Vacation Reason</th>
                        <th>Days</th>
                        <th>Status</th>
                        <th>Time</th>
                    </tr>
                </thead>
                <tbody id="history-container">
                    <tr><td colspan="5" style="text-align: center; color: var(--text-muted); padding: 32px;">No finalized vacation history recorded yet.</td></tr>
                </tbody>
            </table>
        </div>
    </div>

    <script>
        async function fetchBalances() {
            try {
                const res = await fetch('/api/balances');
                const data = await res.json();
                if (data.status === 'success' && data.balances) {
                    renderBalances(data.balances);
                    renderHistory(data.balances);
                }
            } catch (err) {
                console.error('Error fetching balances:', err);
            }
        }

        function renderBalances(balances) {
            const container = document.getElementById('balances-container');
            container.innerHTML = balances.map(b => {
                const rem = b.remaining_balance !== undefined ? b.remaining_balance : 25.0;
                const total = b.starting_balance || 25.0;
                const used = b.used_days || (total - rem);
                const pct = Math.min(100, Math.max(0, (rem / total) * 100));
                const avatar = (b.employee || b.user_id || 'A').charAt(0).toUpperCase();

                return '<div class="balance-card">' +
                    '<div class="user-header">' +
                        '<div>' +
                            '<div style="font-size: 17px; font-weight: 600;">' + (b.employee || b.user_id) + '</div>' +
                            '<div style="font-size: 12px; color: var(--text-muted);">ID: ' + b.user_id + ' | Allowance: ' + total + ' Days</div>' +
                        '</div>' +
                        '<div class="user-avatar">' + avatar + '</div>' +
                    '</div>' +
                    '<div style="display:flex; justify-between; align-items:baseline; margin-bottom:12px;">' +
                        '<div class="rem-days">' + rem + ' <span style="font-size:16px; font-weight:400; color:var(--text-muted);">days left</span></div>' +
                        '<div style="font-size:13px; color:var(--text-muted);">' + used + ' days used</div>' +
                    '</div>' +
                    '<div class="progress-bar-bg"><div class="progress-bar-fill" style="width: ' + pct + '%;"></div></div>' +
                '</div>';
            }).join('');
        }

        function renderHistory(balances) {
            const container = document.getElementById('history-container');
            let finalizedHistory = [];
            balances.forEach(b => {
                if (b.history && Array.isArray(b.history)) {
                    b.history.forEach(h => {
                        const st = h.status || 'APPROVED';
                        if (st === 'APPROVED' || st === 'DENIED') {
                            finalizedHistory.push({
                                employee: b.employee || b.user_id,
                                reason: h.reason || 'Vacation',
                                days: h.days || 1,
                                status: st,
                                timestamp: h.timestamp ? new Date(h.timestamp).toLocaleTimeString() : 'Recently'
                            });
                        }
                    });
                }
            });

            if (finalizedHistory.length === 0) {
                container.innerHTML = '<tr><td colspan="5" style="text-align: center; color: var(--text-muted); padding: 32px;">No finalized vacation history recorded yet.</td></tr>';
                return;
            }

            container.innerHTML = finalizedHistory.map(h => '<tr>' +
                '<td><strong>' + h.employee + '</strong></td>' +
                '<td>' + h.reason + '</td>' +
                '<td><strong style="color: var(--primary-blue);">' + h.days + ' days</strong></td>' +
                '<td><span class="status-' + h.status + '">' + h.status + '</span></td>' +
                '<td>' + h.timestamp + '</td>' +
            '</tr>').join('');
        }

        async function fetchPending() {
            try {
                const res = await fetch('/api/pending');
                const data = await res.json();
                const container = document.getElementById('pending-container');

                if (data.status === 'success' && data.pending && data.pending.length > 0) {
                    container.innerHTML = data.pending.map(p => {
                        const safeEmp = (p.employee || 'Employee').replace(/"/g, '&quot;');
                        const safeReason = (p.reason || 'Vacation Request').replace(/"/g, '&quot;');
                        const safeDept = (p.department || 'Engineering').replace(/"/g, '&quot;');
                        const sid = (p.session_id || '').replace(/"/g, '&quot;');
                        const uid = (p.user_id || '').replace(/"/g, '&quot;');
                        const iid = (p.interrupt_id || 'manager_review').replace(/"/g, '&quot;');
                        const days = p.days || 6.0;

                        return '<div class="approval-card">' +
                            '<div style="flex:1;">' +
                                '<span class="badge-pending">⚠️ MANAGER REVIEW REQUIRED</span>' +
                                '<div style="font-size:18px; font-weight:600; margin-bottom:6px;">' + safeEmp + ' (' + safeDept + ') &mdash; ' + days + ' Days Requested</div>' +
                                '<div style="font-size:14px; color:var(--text-muted); margin-bottom:8px;"><strong>Reason:</strong> ' + safeReason + '</div>' +
                                '<div style="font-size:12px; color:#64748b;">🆔 Session / Entry: ' + sid + ' | 👤 User: ' + uid + '</div>' +
                            '</div>' +
                            '<div style="display:flex; gap:12px;">' +
                                '<button class="btn btn-approve" data-sid="' + sid + '" data-uid="' + uid + '" data-iid="' + iid + '" data-approved="true" data-days="' + days + '" data-reason="' + safeReason + '" onclick="handleActionClick(this)">✅ Approve Vacation</button>' +
                                '<button class="btn btn-reject" data-sid="' + sid + '" data-uid="' + uid + '" data-iid="' + iid + '" data-approved="false" data-days="' + days + '" data-reason="' + safeReason + '" onclick="handleActionClick(this)">❌ Reject</button>' +
                            '</div>' +
                        '</div>';
                    }).join('');
                } else {
                    container.innerHTML = '<div class="empty-state"><div style="font-size:48px;">🏖️</div><h3>No Pending Approvals</h3><p style="margin-top:4px;">All submitted holiday requests are processed or within policy thresholds.</p></div>';
                }
            } catch (err) {
                console.error('Error fetching pending approvals:', err);
            }
        }

        function handleActionClick(btn) {
            const sid = btn.getAttribute('data-sid');
            const uid = btn.getAttribute('data-uid');
            const iid = btn.getAttribute('data-iid');
            const approved = btn.getAttribute('data-approved') === 'true';
            const days = parseFloat(btn.getAttribute('data-days')) || 1.0;
            const reason = btn.getAttribute('data-reason');
            takeAction(sid, uid, iid, approved, days, reason);
        }

        async function takeAction(sessionId, userId, interruptId, approved, days, reason) {
            try {
                const res = await fetch('/api/action/' + sessionId, {
                    method: 'POST',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify({ approved, user_id: userId, interrupt_id: interruptId, days: days, reason: reason })
                });
                const data = await res.json();
                if (data.status === 'success') {
                    alert('Decision Recorded! ' + (approved ? 'Approved ✅' : 'Rejected ❌'));
                    fetchPending();
                    fetchBalances();
                } else {
                    alert('Error: ' + (data.detail || 'Action failed'));
                }
            } catch (err) {
                alert('Action failed: ' + err);
            }
        }

        fetchBalances();
        fetchPending();
        setInterval(fetchBalances, 4000);
        setInterval(fetchPending, 3000);
    </script>
</body>
</html>"""
    return HTMLResponse(content=html_content)
