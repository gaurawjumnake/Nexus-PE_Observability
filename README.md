# AI Observability Tower — Nexus KPI Pipeline

A FastAPI service that ingests financial documents, extracts facts using LLMs, computes KPIs, and exposes a RAG + SQL chatbot interface.

---

## Architecture

```
Documents (PDF/DOCX/CSV/XLSX)
        │
        ▼
  /documents/upload
        │
  ┌─────┴──────────────────────┐
  │ Docling parser             │
  │  ├── narrative → LLM path  │
  │  └── tables → tabular path │
  └─────┬──────────────────────┘
        │ facts → SQLite (nexus.db)
        ▼
  /kpis/calculate  →  KPI results
  /kpis/insights   →  LLM analysis
  /chat/ask        →  SQL + RAG chatbot
```

**Key components:**
- `backend/api/` — FastAPI routers (documents, kpis, chat)
- `backend/kpi_extractor/` — fact extraction, KPI calculation engine, registry
- `backend/chatbot/` — text-to-SQL + RAG orchestrator
- `backend/db/` — SQLite (nexus.db, registry.db) + Chroma vector store
- `backend/config.py` — single source of truth for all paths and env vars

---

## Environment Variables

Copy `.env.example` to `.env` and fill in the values.

| Variable | Required | Description |
|---|---|---|
| `NEXUS_LLM_PROVIDER` | Yes | `gemini` or `azure_openai` |
| `GEMINI_API_KEY` | If `gemini` | Google Gemini API key |
| `AZURE_OPENAI_API_KEY` | If `azure_openai` | Azure OpenAI API key |
| `AZURE_OPENAI_ENDPOINT` | If `azure_openai` | Azure OpenAI endpoint URL |
| `AZURE_OPENAI_DEPLOYMENT` | If `azure_openai` | Deployment name (e.g. `gpt-4o`) |

---

## Running Locally (Docker Desktop)

```bash
# 1. Copy and fill in env vars
cp .env.example .env

# 2. Build and start
docker compose up --build

# 3. Verify health
curl http://localhost:8005/health
# → {"status": "200", "Message": "Platform is running"}

# 4. Open API docs
# http://localhost:8005/docs
```

Logs are written to stderr (visible via `docker compose logs -f`).

---

## API Endpoints

### Documents
| Method | Path | Description |
|---|---|---|
| `POST` | `/documents/upload` | Upload a document (PDF/DOCX/CSV/XLSX). Triggers parse → extract → KPI calc. |
| `GET` | `/documents/{document_id}` | Inspect stored chunks for a document. |

**Upload form fields:** `file`, `company_id` (string), `period` (e.g. `2025-Q1`)

### KPIs
| Method | Path | Description |
|---|---|---|
| `POST` | `/kpis/extract` | Re-run fact extraction on already-uploaded chunks. |
| `POST` | `/kpis/calculate` | Compute KPIs from stored facts. |
| `GET` | `/kpis/{company_id}/{period}` | List KPI results for a company/period. |
| `GET` | `/kpis/` | List all KPI results. |
| `POST` | `/kpis/trend` | MoM / QoQ / YoY trend for one or more KPIs. |
| `POST` | `/kpis/insights` | LLM executive analysis over KPI values. |
| `POST` | `/kpis/registry/facts` | Add a new fact definition to the registry. |
| `POST` | `/kpis/registry/kpis` | Add a new KPI definition to the registry. |

### Chat
| Method | Path | Description |
|---|---|---|
| `POST` | `/chat/index/{document_id}` | Index document chunks into the vector store. |
| `POST` | `/chat/query` | Direct RAG-only query. |
| `POST` | `/chat/ask` | Orchestrated query: routes to SQL, RAG, registry, or combined. |
| `DELETE` | `/chat/history/{session_id}` | Clear chat history for a session. |

### Health
| Method | Path | Description |
|---|---|---|
| `GET` | `/health` | Liveness check (used by Docker and Lambda adapter). |

---

## AWS Deployment (S3 → Lambda → API Gateway)

The Dockerfile is pre-configured for AWS Lambda via the [Lambda Web Adapter](https://github.com/awslabs/aws-lambda-web-adapter). The adapter translates Lambda invoke events to HTTP and forwards them to uvicorn on port 8005 — no code changes required.

**Steps:**

1. Build and push the image to ECR:
   ```bash
   aws ecr get-login-password | docker login --username AWS --password-stdin <account>.dkr.ecr.<region>.amazonaws.com
   docker build -t aiobservabilitytower .
   docker tag aiobservabilitytower <ecr-uri>:latest
   docker push <ecr-uri>:latest
   ```

2. Create a Lambda function from the container image. Set memory ≥ 1024 MB and timeout ≥ 60 s (document parsing is slow).

3. Set environment variables on the Lambda function (same as `.env`).

4. Attach an API Gateway (HTTP API) trigger. The `/health` path is used as the Lambda adapter readiness check (`AWS_LWA_READINESS_CHECK_PATH`).

5. For persistent storage (SQLite DBs, Chroma, parsed files), mount an EFS file system at `/app/backend/db` and `/app/parsed_data`, or switch to a managed database and S3.

> **Note:** SQLite and Chroma are local-disk stores. They work fine for single-instance Lambda but will not share state across concurrent invocations. Use EFS or migrate to PostgreSQL + a managed vector DB for production scale.

---

## Development (without Docker)

```bash
# Install dependencies
uv sync

# Run dev server (auto-reload)
python main.py
# or
uvicorn main:app --reload --port 8005
```

Pre-deployment checks run at startup and will raise a clear error if required env vars are missing.
