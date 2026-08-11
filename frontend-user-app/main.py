# Copyright 2026 Google LLC
# LeaveFlow AI - Employee Portal with Live Chat & Manual Booking

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
from google.cloud import pubsub_v1

from firestore_db import (
    get_all_balances,
    get_employee_balance,
    get_user_memories,
    normalize_user_id,
    record_pending_vacation,
)

app = FastAPI(title="LeaveFlow AI - Employee Portal")

PROJECT_ID = os.environ.get("PROJECT_ID", "ai-sandbox-sw")
LOCATION = os.environ.get("LOCATION", "europe-west1")
AGENT_ENGINE_ID = os.environ.get("AGENT_ENGINE_ID", "projects/110071138645/locations/europe-west1/reasoningEngines/6128897715548979200")
PUBSUB_TOPIC_NAME = os.environ.get("PUBSUB_TOPIC", "holiday-approval-system-holiday-requests")


class ChatRequest(BaseModel):
    user_id: str
    session_id: Optional[str] = None
    message: str


class BookHolidayRequest(BaseModel):
    user_id: str
    employee_name: str
    days: float
    start_date: str
    end_date: str
    reason: str


def get_auth_headers() -> Dict[str, str]:
    credentials, _ = google.auth.default()
    credentials.refresh(AuthRequest())
    return {
        "Authorization": f"Bearer {credentials.token}",
        "Content-Type": "application/json",
    }


def fetch_vertex_memories(user_id: str) -> List[str]:
    try:
        from google.adk.memory.vertex_ai_memory_bank_service import VertexAiMemoryBankService
        clean_id = AGENT_ENGINE_ID.split("/")[-1]
        mb = VertexAiMemoryBankService(project=PROJECT_ID, location=LOCATION, agent_engine_id=clean_id)

        async def _search():
            res = await mb.search_memory(app_name=clean_id, user_id=user_id, query="vacation trip destination")
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
        mems = loop.run_until_complete(_search())
        loop.close()
        if mems:
            return mems
    except Exception:
        pass

    return get_user_memories(user_id)


def publish_to_pubsub(user_id: str, employee_name: str, days: float, start_date: str, reason: str):
    publisher = pubsub_v1.PublisherClient()
    topic_path = publisher.topic_path(PROJECT_ID, PUBSUB_TOPIC_NAME)

    message_payload = {
        "class_method": "async_stream_query",
        "input": {
            "user_id": user_id,
            "message": {
                "role": "user",
                "parts": [
                    {
                        "text": json.dumps({
                            "employee": employee_name,
                            "days": days,
                            "department": "Engineering" if user_id == "alice" else ("Product" if user_id == "bob" else "Marketing"),
                            "reason": reason,
                            "start_date": start_date
                        })
                    }
                ]
            }
        }
    }

    data_bytes = json.dumps(message_payload).encode("utf-8")
    future = publisher.publish(topic_path, data=data_bytes)
    return future.result(timeout=10)


@app.get("/api/user/{user_id}")
async def get_user_data(user_id: str):
    uid = normalize_user_id(user_id)
    balance_data = get_employee_balance(uid)
    vertex_memories = fetch_vertex_memories(uid)
    return {
        "status": "success",
        "data": balance_data,
        "vertex_memories": vertex_memories
    }


@app.post("/api/chat")
async def chat_with_agent(req: ChatRequest):
    uid = normalize_user_id(req.user_id)
    stream_url = f"https://{LOCATION}-aiplatform.googleapis.com/v1/{AGENT_ENGINE_ID}:streamQuery"
    headers = get_auth_headers()

    sid = req.session_id or f"session-{uid}-{os.urandom(4).hex()}"

    payload = {
        "class_method": "async_stream_query",
        "input": {
            "user_id": uid,
            "session_id": sid,
            "message": {
                "role": "user",
                "parts": [{"text": req.message}]
            }
        }
    }

    try:
        resp = requests.post(stream_url, headers=headers, json=payload, timeout=60)
        agent_reply = ""
        if resp.ok:
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
                                agent_reply += part["text"] + "\n"
                except json.JSONDecodeError:
                    continue

        vertex_memories = fetch_vertex_memories(uid)

        if not agent_reply.strip():
            m_snippet = f" I remember your previous note: '{vertex_memories[0]}'." if vertex_memories else ""
            agent_reply = f"Hi {uid.capitalize()}!{m_snippet} How can I help you today?"

        return {
            "status": "success",
            "session_id": sid,
            "reply": agent_reply.strip(),
            "vertex_memories": vertex_memories
        }
    except Exception as err:
        raise HTTPException(status_code=500, detail=str(err))


@app.post("/api/book")
async def book_holiday(req: BookHolidayRequest):
    uid = normalize_user_id(req.user_id)
    emp_name = req.employee_name or uid.capitalize()

    updated_record = record_pending_vacation(
        employee=emp_name,
        days=req.days,
        reason=req.reason,
        start_date=req.start_date
    )

    pubsub_msg_id = None
    try:
        pubsub_msg_id = publish_to_pubsub(
            user_id=uid,
            employee_name=emp_name,
            days=req.days,
            start_date=req.start_date,
            reason=req.reason
        )
    except Exception as err:
        print(f"PubSub publish notice: {err}")

    vertex_memories = fetch_vertex_memories(uid)

    return {
        "status": "success",
        "message": "Holiday request submitted! Status set to PENDING.",
        "pubsub_message_id": pubsub_msg_id,
        "record": updated_record,
        "vertex_memories": vertex_memories
    }


@app.get("/", response_class=HTMLResponse)
async def serve_portal():
    html_content = """<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>LeaveFlow AI - Interactive Chat & Holiday Portal</title>
    <link href="https://fonts.googleapis.com/css2?family=Outfit:wght@300;400;500;600;700&display=swap" rel="stylesheet">
    <style>
        :root {
            --bg-color: #f8fafc;
            --card-bg: #ffffff;
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
            --shadow: 0 10px 25px -5px rgba(0,0,0,0.05);
        }

        * { box-sizing: border-box; margin: 0; padding: 0; font-family: 'Outfit', sans-serif; }

        body {
            background-color: var(--bg-color);
            background-image: 
                radial-gradient(at 0% 0%, rgba(14, 165, 233, 0.08) 0px, transparent 50%),
                radial-gradient(at 100% 0%, rgba(245, 158, 11, 0.08) 0px, transparent 50%);
            color: var(--text-dark);
            padding: 24px;
            min-height: 100vh;
        }

        .container { max-width: 1300px; margin: 0 auto; }

        header {
            background: linear-gradient(135deg, #ffffff 0%, #f0f9ff 100%);
            border: 1px solid var(--border-light);
            border-radius: 20px;
            padding: 20px 32px;
            box-shadow: var(--shadow);
            margin-bottom: 24px;
            display: flex;
            justify-content: space-between;
            align-items: center;
        }

        .brand-title { font-size: 26px; font-weight: 700; }
        .brand-title span { color: var(--primary-blue); }

        .split-layout {
            display: grid;
            grid-template-columns: 1.1fr 1fr;
            gap: 24px;
        }

        .card {
            background: var(--card-bg);
            border: 1px solid var(--border-light);
            border-radius: 20px;
            padding: 24px;
            box-shadow: var(--shadow);
            margin-bottom: 20px;
        }

        .chat-box {
            height: 380px;
            overflow-y: auto;
            border: 1px solid var(--border-light);
            border-radius: 16px;
            padding: 16px;
            background: #fafafa;
            display: flex;
            flex-direction: column;
            gap: 12px;
            margin-bottom: 16px;
        }

        .msg-bubble {
            max-width: 80%;
            padding: 12px 16px;
            border-radius: 16px;
            font-size: 14px;
            line-height: 1.5;
        }

        .msg-user {
            align-self: flex-end;
            background: linear-gradient(135deg, var(--primary-blue), #0369a1);
            color: white;
            border-bottom-right-radius: 4px;
        }

        .msg-agent {
            align-self: flex-start;
            background: #ffffff;
            color: var(--text-dark);
            border: 1px solid var(--border-light);
            border-bottom-left-radius: 4px;
            box-shadow: 0 2px 8px rgba(0,0,0,0.03);
        }

        .chat-input-row { display: flex; gap: 12px; }

        input[type="text"], input[type="date"], select {
            width: 100%;
            padding: 12px 16px;
            border-radius: 12px;
            border: 1px solid var(--border-light);
            font-size: 14px;
            background: #ffffff;
        }

        input:focus, select:focus { outline: none; border-color: var(--primary-blue); }

        .btn-send {
            background: linear-gradient(135deg, var(--primary-blue), #0369a1);
            color: white;
            padding: 12px 24px;
            border: none;
            border-radius: 12px;
            font-weight: 700;
            cursor: pointer;
            box-shadow: 0 4px 12px rgba(2, 132, 199, 0.25);
            white-space: nowrap;
        }

        .btn-new-chat {
            background: #f1f5f9;
            color: var(--text-dark);
            border: 1px solid var(--border-light);
            padding: 8px 14px;
            border-radius: 10px;
            font-size: 12px;
            font-weight: 600;
            cursor: pointer;
        }

        .btn-new-chat:hover { background: var(--ocean-light); color: var(--primary-blue); }

        .memory-section {
            background: #f0f9ff;
            border: 1px solid #bae6fd;
            border-radius: 14px;
            padding: 16px;
            margin-top: 16px;
        }

        .memory-tag {
            background: #ffffff;
            color: var(--primary-blue);
            border: 1px solid #7dd3fc;
            padding: 6px 12px;
            border-radius: 9999px;
            font-size: 12px;
            font-weight: 600;
            display: inline-block;
            margin: 4px;
        }

        .balance-pill {
            background: var(--ocean-light);
            color: var(--primary-blue);
            padding: 14px 18px;
            border-radius: 14px;
            display: flex;
            justify-content: space-between;
            align-items: center;
            margin-bottom: 20px;
            font-weight: 600;
        }

        .btn-book {
            width: 100%;
            background: linear-gradient(135deg, var(--primary-blue), #0369a1);
            color: white;
            font-size: 15px;
            font-weight: 700;
            padding: 14px;
            border: none;
            border-radius: 12px;
            cursor: pointer;
            box-shadow: 0 4px 12px rgba(2, 132, 199, 0.25);
        }

        .status-pill {
            padding: 4px 12px;
            border-radius: 9999px;
            font-size: 12px;
            font-weight: 700;
            display: inline-block;
        }
        .status-PENDING { background: var(--gold-light); color: #b45309; }
        .status-APPROVED { background: var(--green-light); color: #065f46; }
        .status-DENIED { background: var(--red-light); color: #991b1b; }

        table { width: 100%; border-collapse: collapse; margin-top: 12px; }
        th, td { padding: 10px 12px; text-align: left; border-bottom: 1px solid var(--border-light); font-size: 13px; }
        th { background: #f8fafc; color: var(--text-muted); font-weight: 600; }
    </style>
</head>
<body>
    <div class="container">
        <header>
            <div>
                <div class="brand-title">LeaveFlow AI <span>Employee Portal ✈️</span></div>
                <div style="color: var(--text-muted); font-size: 14px; margin-top: 2px;">Interactive Agent Engine Chat & Manual Booking System</div>
            </div>
            <div style="font-size: 32px;">🌴</div>
        </header>

        <div class="split-layout">
            <!-- Left Pane: Interactive Chat Window -->
            <div>
                <div class="card">
                    <div style="display: flex; justify-content: space-between; align-items: center; margin-bottom: 16px;">
                        <div style="display: flex; align-items: center; gap: 10px;">
                            <div style="font-size: 18px; font-weight: 700;">💬 Interactive AI Agent Chat</div>
                            <button class="btn-new-chat" onclick="startNewChat()">➕ New Chat Session</button>
                        </div>
                        <div style="width: 180px;">
                            <select id="user-select-chat" onchange="switchUser()">
                                <option value="alice">Alice Smith (alice)</option>
                                <option value="bob">Bob Jones (bob)</option>
                                <option value="charlie" selected>Charlie Brown (charlie)</option>
                            </select>
                        </div>
                    </div>

                    <div class="chat-box" id="chat-stream">
                        <div class="msg-bubble msg-agent" id="initial-agent-msg">
                            🤖 Hello! I am LeaveFlow AI.
                        </div>
                    </div>

                    <div class="chat-input-row">
                        <input type="text" id="chat-input" placeholder="e.g. Hi! Requesting 3 days leave for Malaga beach trip" onkeypress="if(event.key==='Enter') sendChat()">
                        <button class="btn-send" onclick="sendChat()">Send Message</button>
                    </div>

                    <!-- Vertex AI Memory Bank Section -->
                    <div class="memory-section">
                        <div style="font-size: 13px; font-weight: 700; color: var(--primary-blue); margin-bottom: 8px;">
                            🧠 Vertex AI Memory Bank (Managed Agent Engine State)
                        </div>
                        <div id="memory-tags-container">
                            <span style="font-size: 12px; color: var(--text-muted);">Retrieving Vertex AI memories for user...</span>
                        </div>
                    </div>
                </div>
            </div>

            <!-- Right Pane: Or Enter Manually -->
            <div>
                <div class="card">
                    <div style="font-size: 18px; font-weight: 700; margin-bottom: 16px;">📋 Or Enter Manually</div>

                    <div style="margin-bottom: 16px;">
                        <label style="font-size:13px; font-weight:600; display:block; margin-bottom:6px;">Select Employee:</label>
                        <select id="user-select-manual" onchange="syncManualUser()">
                            <option value="alice">Alice Smith (alice)</option>
                            <option value="bob">Bob Jones (bob)</option>
                            <option value="charlie" selected>Charlie Brown (charlie)</option>
                        </select>
                    </div>

                    <div class="balance-pill">
                        <span>Remaining Annual PTO:</span>
                        <span id="balance-display" style="font-size: 22px; font-weight: 700;">25.0 Days</span>
                    </div>

                    <div style="display: grid; grid-template-columns: 1fr 1fr; gap: 12px; margin-bottom: 12px;">
                        <div>
                            <label style="font-size:12px; font-weight:600;">Start Date:</label>
                            <input type="date" id="start-date" value="2026-06-01" onchange="calcDays()">
                        </div>
                        <div>
                            <label style="font-size:12px; font-weight:600;">End Date:</label>
                            <input type="date" id="end-date" value="2026-06-03" onchange="calcDays()">
                        </div>
                    </div>

                    <div style="margin-bottom: 12px;">
                        <label style="font-size:12px; font-weight:600;">Requested Days:</label>
                        <input type="text" id="days-requested" value="3.0" readonly style="font-weight:700; color:var(--primary-blue);">
                    </div>

                    <div style="margin-bottom: 16px;">
                        <label style="font-size:12px; font-weight:600;">Destination / Reason:</label>
                        <input type="text" id="vacation-reason" placeholder="e.g. Vacation to Rome">
                    </div>

                    <button class="btn-book" onclick="submitBooking()">✈️ Book Holiday (Publish via Pub/Sub)</button>
                </div>

                <!-- Booking History Table -->
                <div class="card">
                    <div style="font-size: 16px; font-weight: 700; margin-bottom: 12px;">📜 Booking History & Status</div>
                    <table>
                        <thead>
                            <tr>
                                <th>Start Date</th>
                                <th>Days</th>
                                <th>Reason</th>
                                <th>Status</th>
                            </tr>
                        </thead>
                        <tbody id="history-rows">
                            <tr><td colspan="4" style="text-align: center; color: var(--text-muted);">Loading history...</td></tr>
                        </tbody>
                    </table>
                </div>
            </div>
        </div>
    </div>

    <script>
        let currentUserId = 'charlie';
        let currentSessionId = 'session-' + currentUserId + '-' + Date.now();

        function calcDays() {
            const start = new Date(document.getElementById('start-date').value);
            const end = new Date(document.getElementById('end-date').value);
            if (start && end && end >= start) {
                const diffTime = Math.abs(end - start);
                const diffDays = Math.ceil(diffTime / (1000 * 60 * 60 * 24)) + 1;
                document.getElementById('days-requested').value = diffDays.toFixed(1);
            }
        }

        function startNewChat() {
            currentSessionId = 'session-' + currentUserId + '-' + Date.now();
            const chatStream = document.getElementById('chat-stream');
            chatStream.innerHTML = '<div class="msg-bubble msg-agent">🤖 New session started! Sending hello...</div>';
            sendInitialGreeting();
        }

        function switchUser() {
            currentUserId = document.getElementById('user-select-chat').value;
            document.getElementById('user-select-manual').value = currentUserId;
            startNewChat();
            loadUserData();
        }

        function syncManualUser() {
            currentUserId = document.getElementById('user-select-manual').value;
            document.getElementById('user-select-chat').value = currentUserId;
            startNewChat();
            loadUserData();
        }

        async function sendInitialGreeting() {
            try {
                const res = await fetch('/api/chat', {
                    method: 'POST',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify({ user_id: currentUserId, session_id: currentSessionId, message: 'hi' })
                });
                const data = await res.json();
                const chatStream = document.getElementById('chat-stream');
                if (data.status === 'success' && data.reply) {
                    chatStream.innerHTML = '<div class="msg-bubble msg-agent">🤖 ' + data.reply.replace(/\\n/g, '<br>') + '</div>';
                    renderMemories(data.vertex_memories || []);
                }
            } catch (err) {
                console.error('Greeting error:', err);
            }
        }

        async function loadUserData() {
            try {
                const res = await fetch('/api/user/' + currentUserId);
                const data = await res.json();
                if (data.status === 'success' && data.data) {
                    const rem = data.data.remaining_balance !== undefined ? data.data.remaining_balance : 25.0;
                    document.getElementById('balance-display').innerText = rem + ' Days';
                    renderHistory(data.data.history || []);
                    renderMemories(data.vertex_memories || []);
                }
            } catch (err) {
                console.error('Error loading user data:', err);
            }
        }

        function renderMemories(memories) {
            const container = document.getElementById('memory-tags-container');
            if (!memories || memories.length === 0) {
                container.innerHTML = '<span style="font-size: 12px; color: var(--text-muted);">No Vertex AI memories stored for ' + currentUserId + ' yet. Memories are saved automatically upon booking.</span>';
                return;
            }

            container.innerHTML = memories.map(m => '<span class="memory-tag">📌 ' + m + '</span>').join('');
        }

        function renderHistory(history) {
            const tbody = document.getElementById('history-rows');
            if (!history || history.length === 0) {
                tbody.innerHTML = '<tr><td colspan="4" style="text-align: center; color: var(--text-muted);">No holiday requests submitted yet.</td></tr>';
                return;
            }

            tbody.innerHTML = history.slice().reverse().map(h => {
                const st = h.status || 'PENDING';
                return '<tr>' +
                    '<td><strong>' + (h.start_date || '2026-06-01') + '</strong></td>' +
                    '<td>' + h.days + ' day(s)</td>' +
                    '<td>' + (h.reason || 'Vacation') + '</td>' +
                    '<td><span class="status-pill status-' + st + '">' + st + '</span></td>' +
                '</tr>';
            }).join('');
        }

        async function sendChat() {
            const inputEl = document.getElementById('chat-input');
            const msg = inputEl.value.trim();
            if (!msg) return;

            const chatStream = document.getElementById('chat-stream');
            chatStream.innerHTML += '<div class="msg-bubble msg-user">' + msg + '</div>';
            inputEl.value = '';
            chatStream.scrollTop = chatStream.scrollHeight;

            try {
                const res = await fetch('/api/chat', {
                    method: 'POST',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify({ user_id: currentUserId, session_id: currentSessionId, message: msg })
                });
                const data = await res.json();
                if (data.status === 'success' && data.reply) {
                    chatStream.innerHTML += '<div class="msg-bubble msg-agent">🤖 ' + data.reply.replace(/\\n/g, '<br>') + '</div>';
                    renderMemories(data.vertex_memories || []);
                } else {
                    chatStream.innerHTML += '<div class="msg-bubble msg-agent">🤖 Request received and processed by LeaveFlow AI.</div>';
                }
                chatStream.scrollTop = chatStream.scrollHeight;
                loadUserData();
            } catch (err) {
                chatStream.innerHTML += '<div class="msg-bubble msg-agent" style="color:red;">Error connecting to LeaveFlow AI agent engine: ' + err + '</div>';
            }
        }

        async function submitBooking() {
            const empName = document.getElementById('user-select-manual').options[document.getElementById('user-select-manual').selectedIndex].text.split('(')[0].trim();
            const days = parseFloat(document.getElementById('days-requested').value) || 1.0;
            const startDate = document.getElementById('start-date').value;
            const endDate = document.getElementById('end-date').value;
            const reason = document.getElementById('vacation-reason').value || 'Vacation Trip';

            try {
                const res = await fetch('/api/book', {
                    method: 'POST',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify({
                        user_id: currentUserId,
                        employee_name: empName,
                        days: days,
                        start_date: startDate,
                        end_date: endDate,
                        reason: reason
                    })
                });

                const data = await res.json();
                if (data.status === 'success') {
                    alert('✅ Holiday request submitted! Status set to PENDING and memory saved to Vertex AI Memory Bank.');
                    loadUserData();
                } else {
                    alert('Submission error: ' + (data.detail || 'Failed'));
                }
            } catch (err) {
                alert('Submission error: ' + err);
            }
        }

        loadUserData();
        sendInitialGreeting();
        setInterval(loadUserData, 4000);
    </script>
</body>
</html>"""
    return HTMLResponse(content=html_content)
