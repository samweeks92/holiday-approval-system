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

app = FastAPI(title="LeaveFlow AI - Holiday Manager Portal")

# Environment & Runtime Configurations
PROJECT_ID = os.environ.get("PROJECT_ID", "ai-sandbox-sw")
LOCATION = os.environ.get("LOCATION", "europe-west1")
AGENT_RUNTIME_ID = os.environ.get(
    "AGENT_RUNTIME_ID",
    f"projects/110071138645/locations/{LOCATION}/reasoningEngines/6128897715548979200"
)

session_service = VertexAiSessionService(project=PROJECT_ID, location=LOCATION)


class ActionRequest(BaseModel):
    approved: bool
    user_id: Optional[str] = "cli-user"
    interrupt_id: Optional[str] = "manager_review"


def get_auth_headers() -> Dict[str, str]:
    """Retrieves Google Auth bearer token for Agent Runtime HTTP calls."""
    credentials, _ = google.auth.default()
    credentials.refresh(AuthRequest())
    return {
        "Authorization": f"Bearer {credentials.token}",
        "Content-Type": "application/json",
    }


# Global In-Memory Cache for Sub-Millisecond Dashboard Responses
CACHED_PENDING_APPROVALS: List[Dict[str, Any]] = []
CACHE_LOCK = asyncio.Lock()


async def refresh_pending_cache():
    """Fetches sessions in parallel across user_ids and updates the in-memory pending approvals cache."""
    global CACHED_PENDING_APPROVALS
    target_users = ["default-user", "cli-user"]
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
                employee = "Employee"
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
            print(f"Error querying user_id {user_id}: {err}")

    async with CACHE_LOCK:
        CACHED_PENDING_APPROVALS = pending_approvals


async def background_poll_loop():
    """Continuous background loop updating pending approvals cache every 2 seconds."""
    while True:
        try:
            await refresh_pending_cache()
        except Exception as err:
            print(f"Background polling error: {err}")
        await asyncio.sleep(2.0)


@app.on_event("startup")
async def startup_event():
    """Triggers immediate cache population and launches background polling task."""
    asyncio.create_task(refresh_pending_cache())
    asyncio.create_task(background_poll_loop())


@app.get("/api/pending")
async def list_pending_approvals():
    """
    Serves pending holiday approvals instantly (<1ms) from in-memory cache,
    updated continuously in the background by parallel session calls.
    """
    async with CACHE_LOCK:
        current_pending = list(CACHED_PENDING_APPROVALS)

    if not current_pending:
        asyncio.create_task(refresh_pending_cache())

    return {"status": "success", "count": len(current_pending), "pending": current_pending}


@app.post("/api/action/{session_id}")
async def take_approval_action(session_id: str, req: ActionRequest):
    """Resumes a paused holiday approval session on Agent Runtime with manager's decision."""
    stream_url = f"https://{LOCATION}-aiplatform.googleapis.com/v1/{AGENT_RUNTIME_ID}:streamQuery"
    headers = get_auth_headers()

    target_user_id = req.user_id or "default-user"
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

        return {
            "status": "success",
            "session_id": session_id,
            "approved": req.approved,
            "review": review_text.strip(),
        }

    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc))


@app.get("/", response_class=HTMLResponse)
async def serve_dashboard():
    """Serves the premium glassmorphic Holiday Approval Portal UI."""
    html_content = """<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <title>LeaveFlow AI - Manager Approval Portal</title>
  <link rel="preconnect" href="https://fonts.googleapis.com">
  <link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
  <link href="https://fonts.googleapis.com/css2?family=Inter:wght@300;400;500;600;700&family=Outfit:wght@400;500;600;700;800&display=swap" rel="stylesheet">
  <style>
    :root {
      --bg-dark: #0B0F19;
      --bg-card: rgba(255, 255, 255, 0.03);
      --bg-card-hover: rgba(255, 255, 255, 0.05);
      --border-glow: rgba(255, 255, 255, 0.08);
      --accent-cyan: #38BDF8;
      --accent-purple: #818CF8;
      --accent-emerald: #34D399;
      --accent-rose: #FB7185;
      --text-main: #F8FAFC;
      --text-muted: #94A3B8;
    }

    * { box-sizing: border-box; margin: 0; padding: 0; }

    body {
      font-family: 'Inter', sans-serif;
      background-color: var(--bg-dark);
      color: var(--text-main);
      min-height: 100vh;
      overflow-x: hidden;
      position: relative;
    }

    .bg-glow-1 {
      position: absolute; top: -150px; left: -100px; width: 600px; height: 600px;
      background: radial-gradient(circle, rgba(129, 140, 248, 0.15) 0%, rgba(11, 15, 25, 0) 70%);
      pointer-events: none; z-index: 0;
    }

    .bg-glow-2 {
      position: absolute; top: 400px; right: -150px; width: 700px; height: 700px;
      background: radial-gradient(circle, rgba(56, 189, 248, 0.12) 0%, rgba(11, 15, 25, 0) 70%);
      pointer-events: none; z-index: 0;
    }

    .app-container {
      max-width: 1200px; margin: 0 auto; padding: 40px 24px; position: relative; z-index: 1;
    }

    header {
      display: flex; justify-content: space-between; align-items: center;
      margin-bottom: 40px; padding-bottom: 24px; border-bottom: 1px solid var(--border-glow);
    }

    .brand-group { display: flex; align-items: center; gap: 16px; }

    .brand-icon {
      width: 48px; height: 48px;
      background: linear-gradient(135deg, var(--accent-purple), var(--accent-cyan));
      border-radius: 14px; display: flex; align-items: center; justify-content: center;
      box-shadow: 0 0 20px rgba(129, 140, 248, 0.3); font-size: 24px;
    }

    .brand-title h1 {
      font-family: 'Outfit', sans-serif; font-weight: 700; font-size: 26px;
      background: linear-gradient(135deg, #FFFFFF 0%, #CBD5E1 100%);
      -webkit-background-clip: text; -webkit-text-fill-color: transparent;
    }

    .brand-title p { font-size: 13px; color: var(--text-muted); margin-top: 2px; }

    .status-badge {
      display: flex; align-items: center; gap: 8px;
      background: rgba(52, 211, 153, 0.1); border: 1px solid rgba(52, 211, 153, 0.2);
      padding: 8px 16px; border-radius: 30px; font-size: 13px; color: var(--accent-emerald); font-weight: 500;
    }

    .status-dot {
      width: 8px; height: 8px; background-color: var(--accent-emerald); border-radius: 50%;
      box-shadow: 0 0 8px var(--accent-emerald); animation: pulse 2s infinite;
    }

    @keyframes pulse {
      0% { opacity: 1; transform: scale(1); }
      50% { opacity: 0.4; transform: scale(1.2); }
      100% { opacity: 1; transform: scale(1); }
    }

    .stats-grid {
      display: grid; grid-template-columns: repeat(auto-fit, minmax(280px, 1fr)); gap: 20px; margin-bottom: 40px;
    }

    .stat-card {
      background: var(--bg-card); backdrop-filter: blur(16px); -webkit-backdrop-filter: blur(16px);
      border: 1px solid var(--border-glow); border-radius: 16px; padding: 24px; transition: all 0.3s ease;
    }

    .stat-card:hover {
      background: var(--bg-card-hover); border-color: rgba(255, 255, 255, 0.15); transform: translateY(-2px);
    }

    .stat-label { font-size: 13px; color: var(--text-muted); text-transform: uppercase; letter-spacing: 0.05em; margin-bottom: 8px; }

    .stat-value { font-family: 'Outfit', sans-serif; font-size: 32px; font-weight: 700; color: #FFFFFF; }

    .section-header { display: flex; justify-content: space-between; align-items: center; margin-bottom: 24px; }

    .section-title { font-family: 'Outfit', sans-serif; font-size: 20px; font-weight: 600; display: flex; align-items: center; gap: 10px; }

    .badge-count { background: rgba(129, 140, 248, 0.2); color: var(--accent-purple); padding: 2px 10px; border-radius: 12px; font-size: 13px; font-weight: 600; }

    .btn-refresh {
      background: rgba(255, 255, 255, 0.05); border: 1px solid var(--border-glow);
      color: var(--text-main); padding: 10px 18px; border-radius: 10px; cursor: pointer;
      font-size: 13px; font-weight: 500; display: flex; align-items: center; gap: 8px; transition: all 0.2s ease;
    }

    .btn-refresh:hover { background: rgba(255, 255, 255, 0.1); border-color: rgba(255, 255, 255, 0.2); }

    .cards-grid { display: grid; grid-template-columns: repeat(auto-fill, minmax(360px, 1fr)); gap: 24px; }

    .card {
      background: var(--bg-card); backdrop-filter: blur(16px); -webkit-backdrop-filter: blur(16px);
      border: 1px solid var(--border-glow); border-radius: 20px; padding: 28px;
      display: flex; flex-direction: column; justify-content: space-between;
      position: relative; overflow: hidden; transition: all 0.3s cubic-bezier(0.4, 0, 0.2, 1);
    }

    .card::before {
      content: ''; position: absolute; top: 0; left: 0; width: 100%; height: 4px;
      background: linear-gradient(90deg, var(--accent-purple), var(--accent-cyan));
    }

    .card:hover {
      background: var(--bg-card-hover); border-color: rgba(129, 140, 248, 0.3);
      box-shadow: 0 12px 40px rgba(0, 0, 0, 0.4); transform: translateY(-4px);
    }

    .card-header { display: flex; justify-content: space-between; align-items: flex-start; margin-bottom: 20px; }

    .employee-info h3 { font-size: 18px; font-weight: 600; color: #FFFFFF; margin-bottom: 4px; }

    .employee-info p { font-size: 13px; color: var(--text-muted); }

    .days-badge {
      font-family: 'Outfit', sans-serif; font-size: 20px; font-weight: 700; color: var(--accent-cyan);
      background: rgba(56, 189, 248, 0.1); border: 1px solid rgba(56, 189, 248, 0.2);
      padding: 6px 14px; border-radius: 12px;
    }

    .card-body { margin-bottom: 24px; }

    .description-box {
      background: rgba(0, 0, 0, 0.2); border-radius: 12px; padding: 14px;
      font-size: 14px; color: #CBD5E1; line-height: 1.5; margin-bottom: 16px;
      border: 1px solid rgba(255, 255, 255, 0.04);
    }

    .meta-row { display: flex; justify-content: space-between; font-size: 12px; color: var(--text-muted); }

    .card-actions { display: grid; grid-template-columns: 1fr 1fr; gap: 12px; }

    .btn-action {
      padding: 12px; border-radius: 12px; font-size: 14px; font-weight: 600;
      cursor: pointer; border: none; display: flex; align-items: center; justify-content: center;
      gap: 8px; transition: all 0.2s ease;
    }

    .btn-approve {
      background: linear-gradient(135deg, #10B981 0%, #059669 100%); color: #FFFFFF;
      box-shadow: 0 4px 14px rgba(16, 185, 129, 0.25);
    }

    .btn-approve:hover {
      background: linear-gradient(135deg, #34D399 0%, #10B981 100%);
      box-shadow: 0 6px 20px rgba(16, 185, 129, 0.4); transform: translateY(-1px);
    }

    .btn-reject {
      background: rgba(251, 113, 133, 0.1); border: 1px solid rgba(251, 113, 133, 0.3); color: var(--accent-rose);
    }

    .btn-reject:hover { background: rgba(251, 113, 133, 0.2); border-color: rgba(251, 113, 133, 0.5); transform: translateY(-1px); }

    .empty-state {
      background: var(--bg-card); border: 1px dashed var(--border-glow); border-radius: 20px;
      padding: 60px; text-align: center; grid-column: 1 / -1;
    }

    .empty-state-icon { font-size: 48px; margin-bottom: 16px; opacity: 0.6; }
    .empty-state h3 { font-size: 18px; font-weight: 600; color: #FFFFFF; margin-bottom: 8px; }
    .empty-state p { font-size: 14px; color: var(--text-muted); }

    .modal-overlay {
      position: fixed; top: 0; left: 0; width: 100vw; height: 100vh;
      background: rgba(11, 15, 25, 0.7); backdrop-filter: blur(8px); z-index: 100;
      opacity: 0; pointer-events: none; transition: opacity 0.3s ease;
    }

    .modal-overlay.active { opacity: 1; pointer-events: auto; }

    .drawer {
      position: fixed; top: 0; right: -500px; width: 100%; max-width: 480px; height: 100vh;
      background: #111827; border-left: 1px solid var(--border-glow); z-index: 101;
      padding: 32px; display: flex; flex-direction: column; justify-content: space-between;
      transition: right 0.4s cubic-bezier(0.16, 1, 0.3, 1); box-shadow: -20px 0 50px rgba(0, 0, 0, 0.5);
    }

    .modal-overlay.active .drawer { right: 0; }

    .drawer-header { display: flex; justify-content: space-between; align-items: center; margin-bottom: 24px; }
    .drawer-header h2 { font-family: 'Outfit', sans-serif; font-size: 22px; font-weight: 700; }

    .btn-close {
      background: rgba(255, 255, 255, 0.05); border: 1px solid var(--border-glow);
      color: var(--text-muted); width: 36px; height: 36px; border-radius: 10px; cursor: pointer; font-size: 18px;
    }

    .drawer-body { flex: 1; overflow-y: auto; }

    .review-card {
      background: rgba(0, 0, 0, 0.3); border: 1px solid var(--border-glow); border-radius: 14px;
      padding: 20px; font-size: 14px; line-height: 1.6; color: #E2E8F0; white-space: pre-wrap;
    }

    .drawer-footer { margin-top: 24px; }
    .btn-done { width: 100%; padding: 14px; background: var(--accent-purple); color: #FFFFFF; border: none; border-radius: 12px; font-weight: 600; cursor: pointer; }
  </style>
</head>
<body>
  <div class="bg-glow-1"></div>
  <div class="bg-glow-2"></div>

  <div class="app-container">
    <header>
      <div class="brand-group">
        <div class="brand-icon">🌴</div>
        <div class="brand-title">
          <h1>LeaveFlow AI Manager Portal</h1>
          <p>ADK 2.0 Agent Runtime • Leave Governance Stream</p>
        </div>
      </div>
      <div class="status-badge">
        <div class="status-dot"></div>
        Agent Engine Connected
      </div>
    </header>

    <div class="stats-grid">
      <div class="stat-card">
        <div class="stat-label">Pending Review (>5 Days)</div>
        <div class="stat-value" id="stat-pending">0</div>
      </div>
      <div class="stat-card">
        <div class="stat-label">Auto-Approved (<= 5 Days)</div>
        <div class="stat-value" style="color: var(--accent-emerald);">Auto-Active</div>
      </div>
      <div class="stat-card">
        <div class="stat-label">Agent Engine Runtime</div>
        <div class="stat-value" style="color: var(--accent-purple); font-size: 24px;">Vertex AI Engine</div>
      </div>
    </div>

    <div class="section-header">
      <div class="section-title">
        Pending Holiday Approvals
        <span class="badge-count" id="count-badge">0</span>
      </div>
      <button class="btn-refresh" onclick="fetchPendingApprovals()">
        <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M21.5 2v6h-6M21.34 15.57a10 10 0 1 1-.57-8.38l5.67-5.67"/></svg>
        Refresh Stream
      </button>
    </div>

    <div class="cards-grid" id="cards-container">
      <div class="empty-state">
        <div class="empty-state-icon">✨</div>
        <h3>No Pending Holiday Requests</h3>
        <p>All employee leave requests have been processed or auto-approved by the policy engine.</p>
      </div>
    </div>
  </div>

  <div class="modal-overlay" id="modal-overlay">
    <div class="drawer">
      <div>
        <div class="drawer-header">
          <h2>Agent Compliance Result</h2>
          <button class="btn-close" onclick="closeModal()">&times;</button>
        </div>
        <div class="drawer-body">
          <div class="review-card" id="modal-review-content">Processing decision...</div>
        </div>
      </div>
      <div class="drawer-footer">
        <button class="btn-done" onclick="closeModal()">Close Review</button>
      </div>
    </div>
  </div>

  <script>
    async function fetchPendingApprovals() {
      try {
        const res = await fetch('/api/pending');
        const data = await res.json();
        const pending = data.pending || [];
        
        document.getElementById('stat-pending').innerText = pending.length;
        document.getElementById('count-badge').innerText = pending.length;

        const container = document.getElementById('cards-container');
        if (pending.length === 0) {
          container.innerHTML = `
            <div class="empty-state">
              <div class="empty-state-icon">✨</div>
              <h3>No Pending Holiday Requests</h3>
              <p>All employee leave requests have been processed or auto-approved by the policy engine.</p>
            </div>`;
          return;
        }

        container.innerHTML = '';
        pending.forEach(function(item) {
          const card = document.createElement('div');
          card.className = 'card';
          card.id = 'card-' + item.session_id;

          const emp = item.employee || 'Employee Claim';
          const dept = item.department || 'Engineering';
          const days = item.days ? item.days + ' Day(s)' : '6 Day(s)';
          const reason = item.reason || item.message || 'Holiday request exceeds 5 days threshold';
          const shortSid = item.session_id ? item.session_id.substring(0, 12) : '';

          card.innerHTML = `
            <div>
              <div class="card-header">
                <div class="employee-info">
                  <h3 class="emp-title"></h3>
                  <p class="dept-title"></p>
                </div>
                <div class="days-badge"></div>
              </div>
              <div class="card-body">
                <div class="description-box"></div>
                <div class="meta-row">
                  <span>Session: ` + shortSid + `...</span>
                  <span>Requires Manager Decision</span>
                </div>
              </div>
            </div>
            <div class="card-actions">
              <button class="btn-action btn-approve" data-sid="` + item.session_id + `" data-user="` + item.user_id + `" data-interrupt="` + item.interrupt_id + `">
                Approve Leave
              </button>
              <button class="btn-action btn-reject" data-sid="` + item.session_id + `" data-user="` + item.user_id + `" data-interrupt="` + item.interrupt_id + `">
                Reject
              </button>
            </div>`;

          card.querySelector('.emp-title').textContent = emp;
          card.querySelector('.dept-title').textContent = 'Department: ' + dept;
          card.querySelector('.days-badge').textContent = days;
          card.querySelector('.description-box').textContent = reason;

          card.querySelector('.btn-approve').addEventListener('click', function() {
            takeAction(item.session_id, true, item.user_id, item.interrupt_id, card);
          });
          card.querySelector('.btn-reject').addEventListener('click', function() {
            takeAction(item.session_id, false, item.user_id, item.interrupt_id, card);
          });

          container.appendChild(card);
        });

      } catch (err) {
        console.error('Error fetching pending approvals:', err);
      }
    }

    async function takeAction(sessionId, approved, userId, interruptId, card) {
      const btnEl = card.querySelector(approved ? '.btn-approve' : '.btn-reject');
      btnEl.disabled = true;

      try {
        const res = await fetch('/api/action/' + sessionId, {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ approved, user_id: userId, interrupt_id: interruptId })
        });

        const data = await res.json();
        
        document.getElementById('modal-review-content').innerText = data.review || 'Action recorded successfully.';
        document.getElementById('modal-overlay').classList.add('active');

        if (card) {
          card.style.opacity = '0';
          card.style.transform = 'scale(0.9)';
          setTimeout(() => fetchPendingApprovals(), 400);
        }

      } catch (err) {
        alert('Action failed: ' + err.message);
      } finally {
        btnEl.disabled = false;
      }
    }

    function closeModal() {
      document.getElementById('modal-overlay').classList.remove('active');
    }

    document.addEventListener('DOMContentLoaded', fetchPendingApprovals);
    window.addEventListener('load', fetchPendingApprovals);
    fetchPendingApprovals();
    setInterval(fetchPendingApprovals, 3000);
  </script>
</body>
</html>
"""
    return HTMLResponse(content=html_content)
