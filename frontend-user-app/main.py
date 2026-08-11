# Copyright 2026 Google LLC
# LeaveFlow AI - User Holiday Booking Portal

import datetime
import json
import os
import re
from typing import Any, Dict, List, Optional

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse
from pydantic import BaseModel
from google.cloud import pubsub_v1

from firestore_db import get_all_balances, get_employee_balance, record_pending_vacation, normalize_user_id

app = FastAPI(title="LeaveFlow AI - Employee Holiday Booking Portal")

PROJECT_ID = os.environ.get("PROJECT_ID", "ai-sandbox-sw")
PUBSUB_TOPIC_NAME = os.environ.get("PUBSUB_TOPIC", "holiday-approval-system-holiday-requests")


class BookHolidayRequest(BaseModel):
    user_id: str
    employee_name: str
    days: float
    start_date: str
    end_date: str
    reason: str


def publish_to_pubsub(user_id: str, employee_name: str, days: float, start_date: str, reason: str):
    """Publishes holiday request payload to Pub/Sub topic for Agent Engine processing."""
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
    message_id = future.result(timeout=10)
    return message_id


@app.get("/api/user/{user_id}")
async def get_user_data(user_id: str):
    uid = normalize_user_id(user_id)
    data = get_employee_balance(uid)
    return {"status": "success", "data": data}


@app.post("/api/book")
async def book_holiday(req: BookHolidayRequest):
    uid = normalize_user_id(req.user_id)
    emp_name = req.employee_name or uid.capitalize()

    # 1. Record PENDING entry in Firestore
    updated_record = record_pending_vacation(
        employee=emp_name,
        days=req.days,
        reason=req.reason,
        start_date=req.start_date
    )

    # 2. Publish message to Pub/Sub
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
        print(f"PubSub publish notice (async fallback): {err}")

    return {
        "status": "success",
        "message": f"Holiday request submitted! Status set to PENDING.",
        "pubsub_message_id": pubsub_msg_id,
        "record": updated_record
    }


@app.get("/", response_class=HTMLResponse)
async def serve_user_portal():
    html_content = """<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>LeaveFlow AI - Book Your Holiday</title>
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
            padding: 30px 20px;
            min-height: 100vh;
        }

        .container { max-width: 900px; margin: 0 auto; }

        header {
            background: linear-gradient(135deg, #ffffff 0%, #f0f9ff 100%);
            border: 1px solid var(--border-light);
            border-radius: 24px;
            padding: 28px 36px;
            box-shadow: var(--shadow);
            margin-bottom: 28px;
            display: flex;
            justify-content: space-between;
            align-items: center;
        }

        .brand-title { font-size: 26px; font-weight: 700; }
        .brand-title span { color: var(--primary-blue); }

        .card {
            background: var(--card-bg);
            border: 1px solid var(--border-light);
            border-radius: 20px;
            padding: 28px;
            box-shadow: var(--shadow);
            margin-bottom: 28px;
        }

        .form-group { margin-bottom: 20px; }
        label { display: block; font-weight: 600; font-size: 14px; margin-bottom: 8px; color: var(--text-dark); }

        select, input[type="text"], input[type="date"] {
            width: 100%;
            padding: 12px 16px;
            border-radius: 12px;
            border: 1px solid var(--border-light);
            font-size: 15px;
            background: #fafafa;
            transition: border-color 0.2s;
        }

        select:focus, input:focus { outline: none; border-color: var(--primary-blue); background: #fff; }

        .balance-pill {
            background: var(--ocean-light);
            color: var(--primary-blue);
            padding: 16px 20px;
            border-radius: 14px;
            display: flex;
            justify-content: space-between;
            align-items: center;
            margin-bottom: 24px;
            font-weight: 600;
        }

        .btn-book {
            width: 100%;
            background: linear-gradient(135deg, var(--primary-blue), #0369a1);
            color: white;
            font-size: 16px;
            font-weight: 700;
            padding: 16px;
            border: none;
            border-radius: 14px;
            cursor: pointer;
            box-shadow: 0 4px 14px rgba(2, 132, 199, 0.3);
            transition: all 0.2s;
        }

        .btn-book:hover { transform: translateY(-2px); box-shadow: 0 6px 20px rgba(2, 132, 199, 0.4); }

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

        table { width: 100%; border-collapse: collapse; margin-top: 16px; }
        th, td { padding: 12px 16px; text-align: left; border-bottom: 1px solid var(--border-light); font-size: 14px; }
        th { background: #f8fafc; color: var(--text-muted); font-weight: 600; }
    </style>
</head>
<body>
    <div class="container">
        <header>
            <div>
                <div class="brand-title">LeaveFlow AI <span>User Portal ✈️</span></div>
                <div style="color: var(--text-muted); font-size: 14px; margin-top: 4px;">Book Holidays & Track Vacation Approvals in Real Time</div>
            </div>
            <div style="font-size: 32px;">🌴</div>
        </header>

        <!-- User Selection & Balance -->
        <div class="card">
            <div class="form-group">
                <label for="user-select">Select Employee Account:</label>
                <select id="user-select" onchange="loadUserData()">
                    <option value="alice">Alice Smith (alice)</option>
                    <option value="bob">Bob Jones (bob)</option>
                    <option value="charlie">Charlie Brown (charlie)</option>
                </select>
            </div>

            <div class="balance-pill">
                <span>Remaining Annual PTO Balance:</span>
                <span id="balance-display" style="font-size: 24px; font-weight: 700;">25.0 Days</span>
            </div>

            <!-- Booking Form -->
            <div style="font-size: 18px; font-weight: 700; margin-bottom: 16px;">🏖️ Book New Vacation</div>
            <div style="display: grid; grid-template-columns: 1fr 1fr; gap: 16px;">
                <div class="form-group">
                    <label for="start-date">Start Date:</label>
                    <input type="date" id="start-date" value="2026-06-01" onchange="calcDays()">
                </div>
                <div class="form-group">
                    <label for="end-date">End Date:</label>
                    <input type="date" id="end-date" value="2026-06-03" onchange="calcDays()">
                </div>
            </div>

            <div class="form-group">
                <label for="days-requested">Requested Days:</label>
                <input type="text" id="days-requested" value="3.0" readonly style="font-weight:700; color:var(--primary-blue);">
            </div>

            <div class="form-group">
                <label for="vacation-reason">Destination / Vacation Reason:</label>
                <input type="text" id="vacation-reason" placeholder="e.g. Malaga beach vacation with family">
            </div>

            <button class="btn-book" onclick="submitBooking()">✈️ Book Holiday (Publish via Pub/Sub)</button>
        </div>

        <!-- Booked Holidays History & Tracking -->
        <div class="card">
            <div style="font-size: 18px; font-weight: 700; margin-bottom: 12px;">📜 Your Holiday Booking History & Status</div>
            <table>
                <thead>
                    <tr>
                        <th>Start Date</th>
                        <th>Days</th>
                        <th>Reason / Destination</th>
                        <th>Status</th>
                    </tr>
                </thead>
                <tbody id="history-rows">
                    <tr><td colspan="4" style="text-align: center; color: var(--text-muted);">Loading holiday history...</td></tr>
                </tbody>
            </table>
        </div>
    </div>

    <script>
        function calcDays() {
            const start = new Date(document.getElementById('start-date').value);
            const end = new Date(document.getElementById('end-date').value);
            if (start && end && end >= start) {
                const diffTime = Math.abs(end - start);
                const diffDays = Math.ceil(diffTime / (1000 * 60 * 60 * 24)) + 1;
                document.getElementById('days-requested').value = diffDays.toFixed(1);
            }
        }

        async function loadUserData() {
            const userId = document.getElementById('user-select').value;
            try {
                const res = await fetch(`/api/user/${userId}`);
                const data = await res.json();
                if (data.status === 'success' && data.data) {
                    const rem = data.data.remaining_balance !== undefined ? data.data.remaining_balance : 25.0;
                    document.getElementById('balance-display').innerText = `${rem} Days`;
                    renderHistory(data.data.history || []);
                }
            } catch (err) {
                console.error('Error loading user data:', err);
            }
        }

        function renderHistory(history) {
            const tbody = document.getElementById('history-rows');
            if (!history || history.length === 0) {
                tbody.innerHTML = '<tr><td colspan="4" style="text-align: center; color: var(--text-muted);">No holiday requests submitted yet.</td></tr>';
                return;
            }

            tbody.innerHTML = history.slice().reverse().map(h => {
                const st = h.status || 'PENDING';
                return `
                    <tr>
                        <td><strong>${h.start_date || '2026-06-01'}</strong></td>
                        <td>${h.days} day(s)</td>
                        <td>${h.reason || 'Vacation'}</td>
                        <td><span class="status-pill status-${st}">${st}</span></td>
                    </tr>
                `;
            }).join('');
        }

        async function submitBooking() {
            const userId = document.getElementById('user-select').value;
            const empName = document.getElementById('user-select').options[document.getElementById('user-select').selectedIndex].text.split('(')[0].trim();
            const days = parseFloat(document.getElementById('days-requested').value) || 1.0;
            const startDate = document.getElementById('start-date').value;
            const endDate = document.getElementById('end-date').value;
            const reason = document.getElementById('vacation-reason').value || 'Vacation Trip';

            try {
                const res = await fetch('/api/book', {
                    method: 'POST',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify({
                        user_id: userId,
                        employee_name: empName,
                        days: days,
                        start_date: startDate,
                        end_date: endDate,
                        reason: reason
                    })
                });

                const data = await res.json();
                if (data.status === 'success') {
                    alert(`✅ Holiday request submitted successfully! Status set to PENDING.`);
                    loadUserData();
                } else {
                    alert('Submission error: ' + (data.detail || 'Failed'));
                }
            } catch (err) {
                alert('Submission error: ' + err);
            }
        }

        loadUserData();
        setInterval(loadUserData, 3000);
    </script>
</body>
</html>"""
    return HTMLResponse(content=html_content)
