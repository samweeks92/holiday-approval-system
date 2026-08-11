# Copyright 2026 Google LLC
# LeaveFlow AI - Bright Vacation-Themed Manager Portal

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

from firestore_db import get_all_balances, get_employee_balance, record_approved_vacation

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
    """Fetches sessions across user_ids and updates the in-memory pending approvals cache."""
    global CACHED_PENDING_APPROVALS
    target_users = ["alice", "bob", "charlie", "vais-query-reasoning-engine", "default-user", "cli-user"]
    pending_approvals = []
    seen_sessions = set()

    for user_id in target_users:
        try:
            sessions_resp = await session_service.list_sessions(app_name=AGENT_RUNTIME_ID, user_id=user_id)
            sessions_list = sessions_resp.sessions if hasattr(sessions_resp, "sessions") else sessions_resp

            session_tasks = []
            valid_sids = []
            for s in sessions_list:
                sid = getattr(s, "id", getattr(s, "session_id", None))
                if sid and sid not in seen_sessions:
                    seen_sessions.add(sid)
                    valid_sids.append(sid)
                    session_tasks.append(
                        session_service.get_session(app_name=AGENT_RUNTIME_ID, user_id=user_id, session_id=sid)
                    )

            if not session_tasks:
                continue

            session_objs = await asyncio.gather(*session_tasks, return_exceptions=True)

            for sid, session_obj in zip(valid_sids, session_objs):
                if isinstance(session_obj, Exception) or not hasattr(session_obj, "events"):
                    continue

                has_request = False
                has_response = False
                interrupt_id = "manager_review"
                message = None
                days = None
                reason = None
                employee = "Alice Smith"
                department = "Engineering"

                for ev in session_obj.events:
                    if hasattr(ev, "actions") and ev.actions and hasattr(ev.actions, "state_delta"):
                        sd = ev.actions.state_delta or {}
                        if "leave_request" in sd:
                            req_data = sd["leave_request"]
                            if isinstance(req_data, dict):
                                days = req_data.get("days")
                                reason = req_data.get("reason")
                                employee = req_data.get("employee", employee)
                                department = req_data.get("department", department)

                    content = getattr(ev, "content", None)
                    if content and getattr(content, "parts", None):
                        for part in content.parts:
                            fc = getattr(part, "function_call", None)
                            if fc and getattr(fc, "name", None) == "adk_request_input":
                                has_request = True
                                interrupt_id = getattr(fc, "id", None) or "manager_review"
                                args = getattr(fc, "args", {}) or {}
                                message = args.get("message") or "Holiday review required (> 5 days threshold)"

                            fr = getattr(part, "function_response", None)
                            if fr and getattr(fr, "name", None) == "adk_request_input":
                                has_response = True

                if has_request and not has_response:
                    if days is None and message:
                        days_match = re.search(r"(\d+(?:\.\d+)?)\s*day", message, re.IGNORECASE)
                        if days_match:
                            days = float(days_match.group(1))

                    pending_approvals.append({
                        "session_id": sid,
                        "user_id": user_id,
                        "interrupt_id": interrupt_id,
                        "message": message or "Holiday approval required",
                        "days": days or 6.0,
                        "reason": reason or message or "Leave request exceeds 5 days policy threshold",
                        "employee": employee,
                        "department": department,
                    })

        except Exception as err:
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
    stream_url = f"https://{LOCATION}-aiplatform.googleapis.com/v1/{AGENT_RUNTIME_ID}:streamQuery"
    headers = get_auth_headers()

    target_user_id = req.user_id or "alice"
    interrupt_id = req.interrupt_id or "manager_review"

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

    try:
        resp = requests.post(stream_url, headers=headers, json=payload, timeout=120)
        if not resp.ok:
            raise HTTPException(status_code=resp.status_code, detail=f"Agent Runtime error: {resp.text}")

        review_text = ""
        for line in resp.text.splitlines():
            line_str = line.strip()
            if not line_str:
                continue
            try:
                event_data = json.loads(line_str)
                content = event_data.get("content", {})
                if content and "parts" in content:
                    for part in content["parts"]:
                        if "text" in part:
                            review_text += part["text"] + "\n"
            except json.JSONDecodeError:
                continue

        if not review_text.strip():
            status_word = "APPROVED ✅" if req.approved else "REJECTED ❌"
            review_text = f"Holiday request decision recorded ({status_word}). Workflow finalized."

        asyncio.create_task(refresh_pending_cache())

        return {
            "status": "success",
            "session_id": session_id,
            "approved": req.approved,
            "review": review_text.strip(),
        }

    except Exception as err:
        raise HTTPException(status_code=500, detail=str(err))


@app.get("/", response_class=HTMLResponse)
async def serve_dashboard(request: Request):
    """Renders the Bright Vacation-Themed Light UI Dashboard."""
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
            --shadow-subtle: 0 10px 25px -5px rgba(0, 0, 0, 0.05), 0 8px 10px -6px rgba(0, 0, 0, 0.02);
            --shadow-hover: 0 20px 25px -5px rgba(2, 132, 199, 0.12), 0 8px 10px -6px rgba(0, 0, 0, 0.04);
        }

        * { box-sizing: border-box; margin: 0; padding: 0; font-family: 'Outfit', sans-serif; }

        body {
            background-color: var(--bg-color);
            background-image: 
                radial-gradient(at 0% 0%, rgba(14, 165, 233, 0.08) 0px, transparent 50%),
                radial-gradient(at 100% 0%, rgba(245, 158, 11, 0.08) 0px, transparent 50%),
                radial-gradient(at 50% 100%, rgba(16, 185, 129, 0.06) 0px, transparent 50%);
            background-attachment: fixed;
            color: var(--text-dark);
            padding: 30px 20px;
            min-height: 100vh;
        }

        .container {
            max-width: 1200px;
            margin: 0 auto;
        }

        /* Vacation Banner Header */
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
            position: relative;
            overflow: hidden;
        }

        header::after {
            content: "🌴 ☀️ ✈️";
            position: absolute;
            right: 30px;
            top: 50%;
            transform: translateY(-50%);
            font-size: 38px;
            opacity: 0.85;
            letter-spacing: 8px;
        }

        .brand-title {
            font-size: 28px;
            font-weight: 700;
            color: var(--text-dark);
            display: flex;
            align-items: center;
            gap: 12px;
        }

        .brand-title span {
            background: linear-gradient(135deg, var(--primary-blue), #0369a1);
            -webkit-background-clip: text;
            -webkit-text-fill-color: transparent;
        }

        .brand-subtitle {
            font-size: 14px;
            color: var(--text-muted);
            margin-top: 4px;
        }

        .status-badge {
            display: inline-flex;
            align-items: center;
            gap: 8px;
            background: var(--green-light);
            color: #065f46;
            padding: 8px 16px;
            border-radius: 9999px;
            font-size: 13px;
            font-weight: 600;
            border: 1px solid rgba(16, 185, 129, 0.2);
        }

        .status-dot {
            width: 8px;
            height: 8px;
            background-color: var(--emerald-green);
            border-radius: 50%;
            box-shadow: 0 0 10px var(--emerald-green);
            animation: pulse 2s infinite;
        }

        @keyframes pulse {
            0% { transform: scale(0.95); opacity: 0.8; }
            50% { transform: scale(1.2); opacity: 1; }
            100% { transform: scale(0.95); opacity: 0.8; }
        }

        .grid-layout {
            display: grid;
            grid-template-columns: 1fr;
            gap: 32px;
        }

        /* Section Headings */
        .section-title {
            font-size: 20px;
            font-weight: 600;
            color: var(--text-dark);
            margin-bottom: 16px;
            display: flex;
            align-items: center;
            gap: 10px;
        }

        /* Balances Cards Grid */
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
            transition: all 0.3s ease;
        }

        .balance-card:hover {
            transform: translateY(-3px);
            box-shadow: var(--shadow-hover);
            border-color: #bae6fd;
        }

        .user-header {
            display: flex;
            justify-content: space-between;
            align-items: center;
            margin-bottom: 16px;
        }

        .user-avatar {
            width: 44px;
            height: 44px;
            border-radius: 50%;
            background: linear-gradient(135deg, var(--ocean-light), #bae6fd);
            color: var(--primary-blue);
            display: flex;
            align-items: center;
            justify-content: center;
            font-weight: 700;
            font-size: 18px;
        }

        .user-name {
            font-size: 17px;
            font-weight: 600;
            color: var(--text-dark);
        }

        .user-role {
            font-size: 12px;
            color: var(--text-muted);
        }

        .stat-numbers {
            display: flex;
            justify-content: space-between;
            align-items: baseline;
            margin-bottom: 12px;
        }

        .rem-days {
            font-size: 32px;
            font-weight: 700;
            color: var(--primary-blue);
        }

        .total-days {
            font-size: 13px;
            color: var(--text-muted);
        }

        .progress-bar-bg {
            height: 10px;
            background: var(--border-light);
            border-radius: 9999px;
            overflow: hidden;
        }

        .progress-bar-fill {
            height: 100%;
            background: linear-gradient(90deg, var(--primary-blue), #38bdf8);
            border-radius: 9999px;
            transition: width 0.5s ease;
        }

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
            transition: all 0.3s ease;
        }

        .approval-card:hover {
            box-shadow: var(--shadow-hover);
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

        .req-info {
            flex: 1;
        }

        .req-title {
            font-size: 18px;
            font-weight: 600;
            color: var(--text-dark);
            margin-bottom: 6px;
        }

        .req-desc {
            font-size: 14px;
            color: var(--text-muted);
            margin-bottom: 12px;
            line-height: 1.5;
        }

        .req-meta {
            display: flex;
            gap: 16px;
            font-size: 13px;
            color: #475569;
        }

        .action-btns {
            display: flex;
            gap: 12px;
        }

        .btn {
            padding: 12px 24px;
            border-radius: 12px;
            font-weight: 600;
            font-size: 14px;
            cursor: pointer;
            border: none;
            transition: all 0.2s ease;
            display: inline-flex;
            align-items: center;
            gap: 6px;
        }

        .btn-approve {
            background: linear-gradient(135deg, var(--emerald-green), #059669);
            color: white;
            box-shadow: 0 4px 12px rgba(16, 185, 129, 0.25);
        }

        .btn-approve:hover {
            transform: translateY(-2px);
            box-shadow: 0 6px 16px rgba(16, 185, 129, 0.35);
        }

        .btn-reject {
            background: #ffffff;
            color: var(--rose-red);
            border: 1px solid var(--rose-red);
        }

        .btn-reject:hover {
            background: var(--red-light);
        }

        .empty-state {
            background: white;
            border: 1px dashed var(--border-light);
            border-radius: 20px;
            padding: 48px;
            text-align: center;
            color: var(--text-muted);
        }

        .empty-icon {
            font-size: 48px;
            margin-bottom: 12px;
        }

        /* Vacation History Log */
        .history-table {
            width: 100%;
            border-collapse: separate;
            border-spacing: 0;
            background: white;
            border-radius: 20px;
            border: 1px solid var(--border-light);
            overflow: hidden;
            box-shadow: var(--shadow-subtle);
        }

        .history-table th {
            background: #f8fafc;
            padding: 16px 24px;
            text-align: left;
            font-size: 13px;
            font-weight: 600;
            color: var(--text-muted);
            border-bottom: 1px solid var(--border-light);
        }

        .history-table td {
            padding: 16px 24px;
            font-size: 14px;
            color: var(--text-dark);
            border-bottom: 1px solid #f1f5f9;
        }

        .history-table tr:last-child td {
            border-bottom: none;
        }

        .status-pill-approved {
            background: var(--green-light);
            color: #065f46;
            padding: 4px 12px;
            border-radius: 9999px;
            font-size: 12px;
            font-weight: 600;
        }
    </style>
</head>
<body>
    <div class="container">
        <!-- Header -->
        <header>
            <div>
                <div class="brand-title">LeaveFlow AI <span>Vacation Hub</span></div>
                <div class="brand-subtitle">Autonomous Holiday Approvals & Real-Time PTO Allowance System</div>
            </div>
            <div class="status-badge">
                <div class="status-dot"></div>
                Agent Engine Live
            </div>
        </header>

        <!-- Employee Balances Grid -->
        <div class="section-title">👥 Employee Vacation Balances (25.0 Days Annual Allowance)</div>
        <div class="balance-grid" id="balances-container">
            <div class="balance-card">Loading employee PTO balances...</div>
        </div>

        <!-- Pending Approvals Section -->
        <div class="section-title">⏳ Pending Manager Approvals (> 5 Days Policy Threshold)</div>
        <div id="pending-container">
            <div class="empty-state">
                <div class="empty-icon">🏖️</div>
                <h3>Checking for pending requests...</h3>
            </div>
        </div>

        <!-- Vacation History Log -->
        <div style="margin-top: 32px;">
            <div class="section-title">📜 Approved Vacation History & Audit Log</div>
            <table class="history-table">
                <thead>
                    <tr>
                        <th>Employee</th>
                        <th>Vacation Reason</th>
                        <th>Days Deducted</th>
                        <th>Status</th>
                        <th>Timestamp</th>
                    </tr>
                </thead>
                <tbody id="history-container">
                    <tr><td colspan="5" style="text-align: center; color: var(--text-muted); padding: 32px;">No approved vacation history recorded yet.</td></tr>
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

                return `
                    <div class="balance-card">
                        <div class="user-header">
                            <div>
                                <div class="user-name">${b.employee || b.user_id}</div>
                                <div class="user-role">ID: ${b.user_id} | Annual Allowance: ${total} Days</div>
                            </div>
                            <div class="user-avatar">${avatar}</div>
                        </div>
                        <div class="stat-numbers">
                            <div class="rem-days">${rem} <span style="font-size:16px; font-weight:400; color:var(--text-muted);">days left</span></div>
                            <div class="total-days">${used} days used</div>
                        </div>
                        <div class="progress-bar-bg">
                            <div class="progress-bar-fill" style="width: ${pct}%;"></div>
                        </div>
                    </div>
                `;
            }).join('');
        }

        function renderHistory(balances) {
            const container = document.getElementById('history-container');
            let allHistory = [];
            balances.forEach(b => {
                if (b.history && Array.isArray(b.history)) {
                    b.history.forEach(h => {
                        allHistory.push({
                            employee: b.employee || b.user_id,
                            reason: h.reason || 'Vacation',
                            days: h.days || 1,
                            status: h.status || 'APPROVED',
                            timestamp: h.timestamp ? new Date(h.timestamp).toLocaleTimeString() : 'Recently'
                        });
                    });
                }
            });

            if (allHistory.length === 0) {
                container.innerHTML = '<tr><td colspan="5" style="text-align: center; color: var(--text-muted); padding: 32px;">No approved vacation history recorded yet.</td></tr>';
                return;
            }

            container.innerHTML = allHistory.map(h => `
                <tr>
                    <td><strong>${h.employee}</strong></td>
                    <td>${h.reason}</td>
                    <td><strong style="color: var(--primary-blue);">${h.days} days</strong></td>
                    <td><span class="status-pill-approved">${h.status}</span></td>
                    <td>${h.timestamp}</td>
                </tr>
            `).join('');
        }

        async function fetchPending() {
            try {
                const res = await fetch('/api/pending');
                const data = await res.json();
                const container = document.getElementById('pending-container');

                if (data.status === 'success' && data.pending && data.pending.length > 0) {
                    container.innerHTML = data.pending.map(p => `
                        <div class="approval-card">
                            <div class="req-info">
                                <span class="badge-pending">⚠️ MANAGER REVIEW REQUIRED</span>
                                <div class="req-title">${p.employee} (${p.department || 'Engineering'}) &mdash; ${p.days} Days Requested</div>
                                <div class="req-desc"><strong>Reason:</strong> ${p.reason}</div>
                                <div class="req-meta">
                                    <span>🆔 Session: ${p.session_id}</span>
                                    <span>👤 User ID: ${p.user_id}</span>
                                </div>
                            </div>
                            <div class="action-btns">
                                <button class="btn btn-approve" onclick="takeAction('${p.session_id}', '${p.user_id}', '${p.interrupt_id}', true)">
                                    ✅ Approve Vacation
                                </button>
                                <button class="btn btn-reject" onclick="takeAction('${p.session_id}', '${p.user_id}', '${p.interrupt_id}', false)">
                                    ❌ Reject
                                </button>
                            </div>
                        </div>
                    `).join('');
                } else {
                    container.innerHTML = `
                        <div class="empty-state">
                            <div class="empty-icon">🏖️</div>
                            <h3>No Pending Approvals</h3>
                            <p style="margin-top: 4px;">All submitted holiday requests are processed or within policy thresholds.</p>
                        </div>
                    `;
                }
            } catch (err) {
                console.error('Error fetching pending approvals:', err);
            }
        }

        async function takeAction(sessionId, userId, interruptId, approved) {
            try {
                const res = await fetch(\`/api/action/\${sessionId}\`, {
                    method: 'POST',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify({ approved, user_id: userId, interrupt_id: interruptId })
                });
                const data = await res.json();
                if (data.status === 'success') {
                    alert(\`Decision Recorded! \${approved ? 'Approved ✅' : 'Rejected ❌'}\`);
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
