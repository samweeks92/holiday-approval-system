# Agent Engine Frontend

A simple, attractive UI for interacting with the Vertex AI Agent Engine.

## Structure
- `api/`: Python FastAPI backend (proxies Agent Engine).
- `ui/`: React + Vite frontend.
- `Dockerfile`: Multi-stage build for production.

## Local Development

### Prerequisites
- Python 3.11+
- Node.js 20+
- Google Cloud Credentials (ADC) configured.

### 1. Start Backend
```bash
cd api
pyenv version
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
# Run server
export PROJECT_ID=ai-sandbox-sw
export LOCATION=us-central1
export DEMO_NAME=base
export AGENT_NAME=$DEMO_NAME
export STAGING_BUCKET_NAME=$PROJECT_ID-$DEMO_NAME-as-staging-bucket

uvicorn main:app --reload --port 8000
```

### 2. Start Frontend
```bash
cd ui
npm install
npm run dev
```
Open `http://localhost:5173`. 
Note: The frontend is configured to proxy API requests to `http://localhost:8000` via `api.ts` hardcoded base URL for now. Update `API_BASE` in `ui/src/api.ts` if needed, or configure Vite proxy.

## Deployment (Docker)

The `Dockerfile` builds the React app and serves it via FastAPI.

```bash
cd frontend
docker build -t agent-frontend .
docker run -p 8080:8080 -e PROJECT_ID=... agent-frontend
```

## Cloud Build

The `cloudbuild.yaml` is configured to build and deploy this directory to Cloud Run.
