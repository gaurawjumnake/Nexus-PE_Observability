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

## KPI Registry

The platform computes **59 KPIs** across **9 categories**, grouped below by domain. Each KPI is extracted from uploaded documents (financial reports, governance decks, telemetry exports, HR data) and stored in `nexus.db` for trend analysis and chatbot queries.

> **Tier key:** `executive` = C-suite / LP-facing headline · `operational` = operating partner / team-level

---

### 💰 Value Creation

| KPI | Unit | Freq | Tier | Business Value |
|---|---|---|---|---|
| **AI ROI** | ratio | monthly | executive | Measures total financial return per dollar of AI investment — primary capital allocation signal for the PE fund. |
| **Cost Savings (OpEx)** | USD | monthly | executive | Validates AI-driven margin improvement by comparing current OpEx to pre-AI baseline. |
| **EBITDA Uplift** | pp | quarterly | executive | Directly links AI initiatives to PE value creation through EBITDA margin expansion. |
| **AI Payback Period** | months | quarterly | executive | LP-ready measure of investment recovery timeline, benchmarked against PE industry panel. |

---

### 📈 Revenue Growth

| KPI | Unit | Freq | Tier | Business Value |
|---|---|---|---|---|
| **AI Revenue** | USD | monthly | executive | Quantifies AI's total top-line contribution (direct + assisted + pipeline) and justifies continued investment. |
| **Direct AI Revenue** | USD | monthly | operational | Isolates pure AI product revenue for product-level P&L accountability. |
| **AI-Assisted Revenue** | USD | monthly | operational | Captures AI's indirect revenue contribution through sales acceleration workflows. |
| **Pipeline Influenced Revenue** | USD | monthly | operational | Measures AI's top-of-funnel impact — revenue from outreach and marketing influenced by AI. |
| **Cross-Sell Uplift** | USD | monthly | operational | Quantifies incremental revenue from cross-sell opportunities enabled by AI-driven customer intelligence. |

---

### 👥 User Adoption

| KPI | Unit | Freq | Tier | Business Value |
|---|---|---|---|---|
| **Active AI Users** | count | weekly | operational | Measures real workforce engagement with AI tools vs mere license allocation — true adoption signal. |
| **DAU / MAU Intensity** | count | daily | operational | Measures depth of daily AI engagement beyond weekly adoption to reveal habitual usage patterns. |
| **Copilot Adoption** | count | weekly | operational | Tracks adoption of AI coding and writing assistants — leading indicator of productivity gains. |
| **Custom Agent Adoption** | count | weekly | operational | Measures adoption of bespoke AI automation beyond off-the-shelf tools, indicating maturity. |
| **Embedded Analytics Adoption** | count | weekly | operational | Tracks data-driven decision making enabled by AI-powered embedded analytics across business apps. |
| **Power User Ratio** | % | weekly | operational | Identifies depth of AI integration in daily workflows — distinguishes surface adoption from embedded use. |

---

### 📊 Adoption

| KPI | Unit | Freq | Tier | Business Value |
|---|---|---|---|---|
| **Portfolio AI Adoption Score** | score (0–100) | monthly | executive | Single composite health score enabling portfolio-wide AI progress tracking, weighted by revenue. |
| **Adoption YoY Growth** | % | annual | operational | Tracks year-over-year momentum of AI adoption maturity across the entire portfolio. |
| **Human Productivity Gains** | % | quarterly | operational | Demonstrates workforce efficiency gains from AI copilots and automation at a per-FTE level. |

---

### 🏗️ Project Portfolio

| KPI | Unit | Freq | Tier | Business Value |
|---|---|---|---|---|
| **Total AI Projects** | count | monthly | operational | Provides complete pipeline visibility for investment and resource planning across all stages. |
| **Projects in Production** | count | monthly | operational | Measures conversion from pilots to value-generating live deployments — the pipeline delivery rate. |
| **Projects in PoC / Pilot** | count | monthly | operational | Tracks the early-stage pipeline feeding future production deployments. |
| **% AI in Production** | % | monthly | operational | Tracks conversion rate from AI experimentation to value-generating production deployments. |
| **Production Ratio** | ratio | monthly | operational | Identifies portfolio velocity in converting experiments to production value (production:PoC ratio). |
| **Stalled / Deprioritized Projects** | count | monthly | operational | Surfaces resource waste and pipeline blockages requiring operating partner intervention. |

---

### 💸 FinOps

| KPI | Unit | Freq | Tier | Business Value |
|---|---|---|---|---|
| **Total AI Spend** | USD | monthly | operational | Provides full cost visibility across cloud, API licensing, storage, and talent — FinOps governance baseline. |
| **Cloud Compute Spend** | USD | monthly | operational | Tracks the largest AI cost driver (GPU/infra) to enable dedicated visibility and optimization. |
| **API Licensing Spend** | USD | monthly | operational | Tracks third-party AI dependency costs and provides vendor negotiation leverage. |
| **Budget Adherence** | % | monthly | operational | Ensures AI spending discipline and prevents cost overruns against approved annual budget. |
| **Budget Variance** | USD | monthly | operational | Provides early warning for budget overruns or windfalls requiring governance intervention. |
| **Forecasted AI Spend** | USD | monthly | operational | Enables proactive budget management by projecting end-of-year spend from run-rate velocity. |
| **Cost per AI Outcome** | USD | monthly | operational | Enables unit economics benchmarking and identifies inefficient AI deployments at the workload level. |
| **Spend by Model Family** | % | monthly | operational | Enables model vendor concentration risk management and cost optimization across model families. |

---

### 🛡️ Governance

| KPI | Unit | Freq | Tier | Business Value |
|---|---|---|---|---|
| **AI Governance Score** | score (0–100) | monthly | executive | Composite risk metric providing PE-level governance assurance and regulatory risk visibility. |
| **AI Incident Rate (Critical)** | count | monthly | operational | Early warning signal for AI reliability failures with reputational or financial consequences. |
| **Critical Incident Count** | count | daily | operational | Tracks open unresolved critical incidents (data leakage, drift, hallucinations) requiring immediate escalation. |
| **Hallucination Detection Rate** | % | daily | operational | Critical quality signal for AI reliability and trust, especially in financial and legal decision contexts. |
| **Human Review Coverage** | % | monthly | operational | Ensures human oversight is maintained for AI decisions with significant business or legal impact. |
| **Data Privacy Compliance** | score (0–100) | monthly | operational | Reduces regulatory risk and protects the portfolio from GDPR/CCPA privacy violation fines. |
| **Policy Compliance Rate** | % | monthly | operational | Ensures organizational AI usage aligns with legal, ethical, and regulatory standards. |
| **Regulatory Readiness** | score (0–100) | quarterly | operational | Proactively de-risks the portfolio from emerging AI regulations (EU AI Act, SEC AI disclosure). |
| **Vendor Compliance** | % | quarterly | operational | Manages third-party AI risk in the supply chain by validating vendor security and compliance. |

---

### 🧠 Maturity

| KPI | Unit | Freq | Tier | Business Value |
|---|---|---|---|---|
| **AI Maturity Score** | score (0–5) | quarterly | executive | Provides PE-level composite view of long-term AI capability across strategy, tech, talent, and governance. |
| **Strategic Alignment Score** | score (0–5) | quarterly | operational | Ensures AI investments support business objectives rather than being pursued for technology's sake alone. |
| **Technical Maturity Score** | score (0–5) | quarterly | operational | Identifies MLOps and data infrastructure gaps that limit AI scalability and reliability. |
| **Talent Readiness Score** | score (0–5) | quarterly | operational | Identifies human capital gaps constraining AI adoption velocity across the portfolio. |
| **Governance Maturity Score** | score (0–5) | quarterly | operational | Measures readiness to govern AI at scale against PE-required compliance standards. |

---

### 📡 Reliability

| KPI | Unit | Freq | Tier | Business Value |
|---|---|---|---|---|
| **System Availability (Uptime)** | % | monthly | operational | Ensures AI systems meet SLA commitments and do not disrupt business operations. |
| **API Success Rate** | % | daily | operational | Monitors AI endpoint reliability essential for user experience and business continuity. |
| **Error Rate** | % | daily | operational | Primary reliability health signal for AI infrastructure teams to detect degradation early. |
| **P95 Latency** | ms | daily | operational | Ensures AI system responsiveness meets user experience and SLA thresholds at the 95th percentile. |
| **Fallback Rate** | % | daily | operational | Indicates primary model reliability and efficiency of fallback architecture under load. |
| **MTTR (Incident Response)** | minutes | monthly | operational | Measures operational maturity and resilience of AI infrastructure teams in recovering from incidents. |

---

### 🏆 Benchmarking

| KPI | Unit | Freq | Tier | Business Value |
|---|---|---|---|---|
| **Portfolio Benchmark Score** | percentile | quarterly | executive | LP-facing evidence of portfolio AI leadership relative to industry-matched PE peer firms. |
| **AI Maturity Score** *(portfolio avg)* | score (0–5) | quarterly | executive | Cross-portfolio composite view of AI engineering culture, data readiness, and governance capability. |
| **Industry Benchmark Ratio** | ratio | quarterly | executive | Enables LP-ready narrative on AI alpha generation versus sector median ROI. |
| **Adoption Velocity Rank** | percentile | quarterly | executive | Provides competitive intelligence on adoption pace vs peer PE firms in the benchmark panel. |
| **Cost Efficiency Rank** | percentile | quarterly | executive | Validates FinOps discipline relative to peers and informs cost optimization targets. |
| **Governance Rank** | percentile | quarterly | executive | Demonstrates governance leadership to LPs and regulators relative to the benchmark panel. |
| **Company Maturity Rank** | rank | quarterly | executive | Identifies portfolio leaders and laggards by maturity score for targeted operating partner support. |
| **Top Quartile Position** | % | quarterly | executive | LP-facing headline metric showing what percentage of PortCos rank in the top industry quartile. |

---

## KPI Data Sourcing Reference

The table below details the **data inputs, automation feasibility, and sourcing method** for every KPI in the registry.

> **Source key:** `API` = can be pulled from a system/vendor API · `DB` = exists in an internal data warehouse / operational DB · `Manual` = requires human input or survey · `Hybrid` = combination of automated + manual

| KPI Name | Documents / Data Required | Can It Be Automated? | Data Source |
|---|---|---|---|
| **AI ROI** | Financial P&L (pre/post AI), AI cost ledger, revenue attribution model | ✅ Yes | DB / ERP system |
| **Cost Savings (OpEx)** | OpEx reports (baseline vs current), AP/GL data | ✅ Yes | DB / ERP / Finance system |
| **EBITDA Uplift** | Income statement, EBITDA bridge report, pre-AI EBITDA baseline | ✅ Yes | DB / ERP system |
| **AI Payback Period** | Total AI investment ledger, cumulative savings tracker | ✅ Yes | DB / Finance system |
| **AI Revenue** | Revenue attribution model, CRM deal data, product billing records | ✅ Yes | DB / CRM API (Salesforce, HubSpot) |
| **Direct AI Revenue** | Product P&L, billing system, AI product SKU revenue | ✅ Yes | DB / Billing API |
| **AI-Assisted Revenue** | CRM closed-won deals with AI-assist flags, sales workflow logs | ✅ Yes | CRM API (Salesforce, HubSpot) |
| **Pipeline Influenced Revenue** | Marketing automation logs, AI outreach campaign data, CRM pipeline | ✅ Yes | CRM / Marketing API (Marketo, HubSpot) |
| **Cross-Sell Uplift** | CRM cross-sell opportunity data, AI recommendation engine logs | ✅ Yes | CRM API / DB |
| **Active AI Users** | SSO/IdP login logs, AI tool usage telemetry, license assignment data | ✅ Yes | API (Azure AD, Okta, tool vendor APIs) |
| **DAU / MAU Intensity** | Daily/Monthly active user telemetry from AI platforms | ✅ Yes | API (tool vendor analytics APIs) |
| **Copilot Adoption** | GitHub Copilot / Microsoft 365 Copilot usage reports | ✅ Yes | API (GitHub, Microsoft Graph API) |
| **Custom Agent Adoption** | Internal agent platform usage logs, workflow execution counts | ✅ Yes | DB / Internal platform API |
| **Embedded Analytics Adoption** | BI platform usage logs (Power BI, Tableau), embedded app telemetry | ✅ Yes | API (Power BI REST API, Tableau API) |
| **Power User Ratio** | User session frequency data, AI tool engagement metrics | ✅ Yes | API / DB (usage telemetry) |
| **Portfolio AI Adoption Score** | Aggregated adoption sub-metrics across all portfolio companies | ✅ Yes (with component data) | DB / Aggregated from other KPIs |
| **Adoption YoY Growth** | Historical adoption metric snapshots (YoY comparison) | ✅ Yes | DB (historical KPI records) |
| **Human Productivity Gains** | HR time-tracking data, FTE output metrics, pre/post AI productivity benchmarks | ⚠️ Partial | DB / HR system + Manual survey |
| **Total AI Projects** | AI project portfolio tracker / PMO system | ✅ Yes | DB / Project management API (Jira, Azure DevOps) |
| **Projects in Production** | Project status fields in PMO/portfolio tracker | ✅ Yes | DB / Project management API |
| **Projects in PoC / Pilot** | Project status fields in PMO/portfolio tracker | ✅ Yes | DB / Project management API |
| **% AI in Production** | Project status counts (total vs production) | ✅ Yes | DB / Derived from other KPIs |
| **Production Ratio** | Production project count, PoC project count | ✅ Yes | DB / Derived from other KPIs |
| **Stalled / Deprioritized Projects** | Project status change logs, PMO flags | ✅ Yes | DB / Project management API |
| **Total AI Spend** | Cloud cost reports, SaaS invoices, API billing statements, HR cost allocation | ✅ Yes | API (AWS Cost Explorer, Azure Cost Mgmt, vendor invoices) |
| **Cloud Compute Spend** | Cloud billing reports (AWS, Azure, GCP) | ✅ Yes | API (AWS Cost Explorer, Azure Cost Management API) |
| **API Licensing Spend** | Vendor invoices, API usage billing (OpenAI, Azure OpenAI, etc.) | ✅ Yes | API (vendor billing APIs) / Finance DB |
| **Budget Adherence** | Approved AI budget document, actual spend ledger | ✅ Yes | DB / Finance system |
| **Budget Variance** | Approved AI budget, actual spend-to-date | ✅ Yes | DB / Finance system |
| **Forecasted AI Spend** | Monthly spend run-rate data, budget planning model | ✅ Yes | DB / Finance system |
| **Cost per AI Outcome** | Total AI spend, AI outcome count (transactions, inferences, tasks) | ✅ Yes | DB / API (spend + outcome telemetry) |
| **Spend by Model Family** | API billing broken down by model (GPT-4, Gemini, Claude, etc.) | ✅ Yes | API (vendor billing APIs) |
| **AI Governance Score** | Governance assessment questionnaire, policy compliance reports, audit logs | ⚠️ Partial | Manual survey + DB / Compliance tool API |
| **AI Incident Rate (Critical)** | Incident management system logs (PagerDuty, ServiceNow, Jira) | ✅ Yes | API (PagerDuty, ServiceNow, Jira API) |
| **Critical Incident Count** | Open incident tickets flagged as critical severity | ✅ Yes | API (incident management system) |
| **Hallucination Detection Rate** | AI output evaluation logs, human review override records, LLM eval pipelines | ⚠️ Partial | DB / LLM eval pipeline + Manual review |
| **Human Review Coverage** | Workflow logs showing AI decisions reviewed by humans vs total decisions | ✅ Yes | DB / Workflow orchestration system |
| **Data Privacy Compliance** | Privacy audit reports, DLP logs, consent management records | ⚠️ Partial | API (DLP tools, CASB) + Manual audit |
| **Policy Compliance Rate** | AI usage policy violation logs, HR compliance records | ⚠️ Partial | DB / Compliance tool API + Manual |
| **Regulatory Readiness** | Regulatory self-assessment docs, compliance gap analysis, legal review | ❌ Manual | Manual (governance survey / audit) |
| **Vendor Compliance** | Vendor security assessments (SOC 2, ISO 27001), contract review records | ❌ Manual | Manual (vendor assessment / procurement) |
| **AI Maturity Score** | Maturity assessment questionnaire across strategy, tech, talent, governance axes | ⚠️ Partial | Manual survey + DB (computed score) |
| **Strategic Alignment Score** | Strategic planning docs, OKR/goal alignment review, business case records | ❌ Manual | Manual (leadership survey / review) |
| **Technical Maturity Score** | MLOps maturity checklist, infrastructure audit, CI/CD pipeline review | ⚠️ Partial | DB / DevOps API + Manual checklist |
| **Talent Readiness Score** | HR skills data, AI training completion records, hiring pipeline data | ⚠️ Partial | DB / HR system API + Manual survey |
| **Governance Maturity Score** | Governance framework docs, policy review records, board-level AI charter | ❌ Manual | Manual (governance review) |
| **System Availability (Uptime)** | Infrastructure monitoring data, SLA reports | ✅ Yes | API (Datadog, New Relic, CloudWatch) |
| **API Success Rate** | API gateway access logs, error/success response telemetry | ✅ Yes | API (API Gateway logs, APM tools) |
| **Error Rate** | Application error logs, exception telemetry | ✅ Yes | API (APM tools: Datadog, New Relic, Sentry) |
| **P95 Latency** | Request latency telemetry, percentile trace data | ✅ Yes | API (APM / observability tools) |
| **Fallback Rate** | AI model routing logs, fallback trigger events | ✅ Yes | DB / Internal AI routing platform logs |
| **MTTR (Incident Response)** | Incident ticket open/close timestamps from ITSM | ✅ Yes | API (PagerDuty, ServiceNow, Jira) |
| **Portfolio Benchmark Score** | External PE benchmark panel data, portfolio KPI exports | ⚠️ Partial | Manual (benchmark report) + DB |
| **AI Maturity Score** *(portfolio avg)* | Individual company maturity scores, portfolio-level aggregation | ✅ Yes (once components exist) | DB / Derived from company KPIs |
| **Industry Benchmark Ratio** | Internal AI ROI vs external industry benchmark report | ⚠️ Partial | Manual (industry report) + DB |
| **Adoption Velocity Rank** | Adoption growth rates vs peer PE benchmark panel | ⚠️ Partial | Manual (benchmark report) + DB |
| **Cost Efficiency Rank** | Cost-per-outcome vs peer PE benchmark data | ⚠️ Partial | Manual (benchmark report) + DB |
| **Governance Rank** | Governance score vs peer benchmark panel | ⚠️ Partial | Manual (benchmark report) + DB |
| **Company Maturity Rank** | AI Maturity Score for each portfolio company | ✅ Yes | DB / Derived from maturity KPIs |
| **Top Quartile Position** | Portfolio company maturity/benchmark scores, quartile thresholds | ✅ Yes | DB / Derived from benchmark KPIs |

> **Legend:** ✅ Fully automatable · ⚠️ Partially automatable (some manual inputs required) · ❌ Primarily manual (human judgment / survey-driven)

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
