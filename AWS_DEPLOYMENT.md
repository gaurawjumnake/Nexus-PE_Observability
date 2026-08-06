# AWS Deployment Guide — Nexus Observability Tool

**Stack**: Docker image → ECR → Lambda (container) → API Gateway v2 (HTTP API)
**Region**: `ap-southeast-2`
**Account**: `231733667519`
**ECR repo**: `nexus-observability-tool`
**Lambda function**: `nexus_observability_tool`
**API Gateway**: `nexus_observability_tool-API` (ID: `ch3lr5e5g7`)
**Live base URL**: `https://ch3lr5e5g7.execute-api.ap-southeast-2.amazonaws.com`

---

## Key Concepts for ECR → Lambda → API Gateway Deployments

### 1. Mangum — ASGI Adapter for Lambda

FastAPI is an ASGI framework; AWS Lambda speaks a proprietary event/response contract. **Mangum** bridges the two by translating API Gateway HTTP events into ASGI-compatible requests and back.

**Where to use it**: At the bottom of `main.py`, after the FastAPI `app` is fully configured (all routers included, middleware added, lifespan set):

```python
# main.py — handler wrapper (current production setup)
from mangum import Mangum

_mangum = Mangum(app, lifespan="off")

def handler(event, context):
    # EventBridge warmup ping — return immediately, skip ASGI stack
    if event.get("source") == "aws.events":
        return {"statusCode": 200, "body": "warm"}

    # Async job worker invocation (self-invoked with InvocationType=Event)
    if "nexus_job_id" in event:
        from backend.api.job_worker import process_job
        process_job(event["nexus_job_id"])
        return {"statusCode": 200, "body": "done"}

    # Normal API Gateway request
    return _mangum(event, context)
```

The name `handler` must match the `CMD` entry point in the Dockerfile (`main.handler`). Lambda calls this function for every invocation.

**`lifespan` parameter:**

| Value | Behaviour | When to use |
|-------|-----------|-------------|
| `"auto"` | Runs ASGI lifespan (startup/shutdown) on every invocation | Default; fine for local dev, adds cold-start overhead on Lambda |
| `"off"` | Skips lifespan entirely | **Current setting** — startup I/O runs at module level so it only happens once per container, not per request |

---

### 2. Dockerfile

Lambda container images must use an AWS-provided base image and expose the handler via `CMD`. The project Dockerfile:

```dockerfile
# syntax=docker/dockerfile:1

FROM public.ecr.aws/lambda/python:3.13

# Pin uv to a specific version so the layer hash is stable across builds
COPY --from=ghcr.io/astral-sh/uv:0.7.12 /uv /uvx /bin/

# Copy dependency files
COPY pyproject.toml uv.lock ${LAMBDA_TASK_ROOT}/

# Install dependencies — cache mount keeps downloaded wheels across builds
# even when pyproject.toml/uv.lock change, avoiding re-downloads from PyPI
RUN --mount=type=cache,target=/root/.cache/uv \
    cd ${LAMBDA_TASK_ROOT} && uv pip install --system .

# Copy application code
COPY . ${LAMBDA_TASK_ROOT}

# Lambda invokes main.handler via Mangum
CMD ["main.handler"]
```

Key points:
- Base image `public.ecr.aws/lambda/python:3.13` is Lambda-compatible (x86_64 / `linux/amd64` runtime).
- `${LAMBDA_TASK_ROOT}` is `/var/task` — the directory Lambda loads the function from.
- `CMD ["main.handler"]` tells Lambda to call the `handler` function in `main.py`.
- The `uv` cache mount speeds up local rebuilds without affecting the final image size.

---

### 3. Docker Build Command

**Always build with the `linux/amd64` platform flag** when targeting Lambda, regardless of the host machine architecture (especially on Apple Silicon / ARM Macs). Omitting it produces an ARM image that Lambda will reject or run incorrectly.

```bash
docker build --platform linux/amd64 --provenance=false -t nexus-observability-tool .
```

| Flag | Why it's needed |
|------|----------------|
| `--platform linux/amd64` | Produces an x86_64 image compatible with the Lambda execution environment |
| `--provenance=false` | Disables BuildKit provenance attestation; ECR rejects multi-manifest images that include it, causing a push failure |
| `-t nexus-observability-tool` | Tags the image for subsequent `docker tag` / `docker push` steps |

> Skipping either flag is a common source of silent failures: the build succeeds locally but Lambda refuses the image or behaves incorrectly at runtime.

---

## Issues Found & Solutions Applied

### Issue 1 — API Gateway had no catch-all route (2026-06-24)

**Symptom**: Every request returned `{"message":"Not Found"}` (HTTP 404) from API Gateway. Lambda was never invoked.

**Root cause**: The only route configured was `ANY /nexus_observability_tool` — a fixed path. FastAPI routes (`/health`, `/kpis`, `/chat`, `/documents`) never matched it.

**Diagnosed by**:
- `aws apigatewayv2 get-routes` — confirmed only one fixed-path route existed
- Response header `apigw-requestid` present on 404s — confirmed API Gateway, not Lambda, was returning them
- Direct `aws lambda invoke` with a synthesised event — confirmed Lambda + FastAPI worked fine in isolation

**Fix**: Added a `$default` catch-all route pointing to the existing Lambda integration.
```bash
aws apigatewayv2 create-route \
  --api-id ch3lr5e5g7 \
  --route-key '$default' \
  --target 'integrations/622l7uo' \
  --region ap-southeast-2
```

---

### Issue 2 — Named stage `default` prepended `/default/` to every path (2026-06-24)

**Symptom**: After Issue 1 was fixed, requests still returned `{"detail":"Not Found"}` (FastAPI 404, not API Gateway 404). Lambda was invoked but routed to the wrong path.

**Root cause**: The stage was named `default` (not the special `$default` stage). HTTP API Gateway includes the stage name as a path prefix when forwarding to Lambda. Lambda received `rawPath: /default/health` but FastAPI only has a route at `/health`.

**Diagnosed by**:
- `{"detail":"Not Found"}` body format = FastAPI (vs `{"message":"Not Found"}` = API Gateway)
- Direct `aws lambda invoke` with `rawPath: /health` returned 200 — Lambda itself was fine
- Confirmed: `/default/health` → FastAPI 404, direct invocation with `/health` → 200

**Fix**: Created a `$default` stage (the special stage name that adds no URL prefix).
```bash
aws apigatewayv2 create-stage \
  --api-id ch3lr5e5g7 \
  --stage-name '$default' \
  --auto-deploy \
  --region ap-southeast-2
```
Requests now go to `https://ch3lr5e5g7.execute-api.ap-southeast-2.amazonaws.com/health` — no stage prefix.

---

### Issue 3 — Cold starts timing out API Gateway's 29-second window (2026-07-10)

**Symptom**: First request after the Lambda container was idle (typically after ~15–45 min of inactivity) caused a `Sandbox.Timeout` error. API Gateway has a hard 29-second integration timeout; the cold start + request processing exceeded it.

**Root cause (two parts)**:

1. `Mangum(app, lifespan="auto")` was set. This runs the full FastAPI lifespan (DB connect → schema check → registry rebuild) on *every* invocation's init phase, which counted against the 29-second window.
2. After idle periods, AWS recycles the Lambda container entirely. The next request hits a true cold start with no warm container to reuse.

**Fix — two changes applied together**:

#### Part A: Switch to `lifespan="off"` and wrap the handler

`lifespan="off"` tells Mangum not to run ASGI startup/shutdown. Startup I/O (DB, schema, registry, LLM client) now runs at **module level** — during Lambda's INIT phase, which does not count against the API Gateway timeout.

The bare `handler = Mangum(app, lifespan="off")` was replaced with a `def handler()` wrapper that intercepts non-HTTP events before they reach Mangum:

```python
# main.py
from mangum import Mangum

_mangum = Mangum(app, lifespan="off")

def handler(event, context):
    if event.get("source") == "aws.events":          # EventBridge warmup ping
        log.log_info("Warmup ping received from EventBridge")
        return {"statusCode": 200, "body": "warm"}

    if "nexus_job_id" in event:                       # async job worker invocation
        from backend.api.job_worker import process_job
        process_job(event["nexus_job_id"])
        return {"statusCode": 200, "body": "done"}

    return _mangum(event, context)                    # normal API Gateway request
```

A lightweight `GET /warmup` route was also added to FastAPI for API-Gateway-based health checks:

```python
@app.get("/warmup")
async def warmup():
    return {"status": "warm"}
```

#### Part B: EventBridge scheduled rule to keep the container alive

Lambda recycles idle containers after roughly 15–45 minutes (undocumented, varies). An EventBridge rule fires every 5 minutes directly at the Lambda function, keeping the container perpetually warm.

**The ping goes directly to Lambda** (not through API Gateway), so the `handler()` wrapper short-circuits it immediately — the full ASGI stack never runs for warmup events.

**AWS resources created (one-time setup, already applied)**:

```bash
# 1. Create the schedule rule
aws events put-rule \
  --name nexus-warmup \
  --schedule-expression "rate(5 minutes)" \
  --state ENABLED \
  --description "Keeps nexus_observability_tool Lambda warm by pinging every 5 minutes" \
  --region ap-southeast-2

# 2. Grant EventBridge permission to invoke the Lambda
RULE_ARN=$(aws events describe-rule --name nexus-warmup \
  --region ap-southeast-2 --query 'Arn' --output text)

aws lambda add-permission \
  --function-name nexus_observability_tool \
  --statement-id nexus-warmup-trigger \
  --action lambda:InvokeFunction \
  --principal events.amazonaws.com \
  --source-arn "$RULE_ARN" \
  --region ap-southeast-2

# 3. Point the rule at Lambda with the warmup payload
LAMBDA_ARN=$(aws lambda get-function-configuration \
  --function-name nexus_observability_tool \
  --region ap-southeast-2 --query 'FunctionArn' --output text)

aws events put-targets \
  --rule nexus-warmup \
  --region ap-southeast-2 \
  --targets "[{
    \"Id\": \"nexus-lambda-warmup\",
    \"Arn\": \"$LAMBDA_ARN\",
    \"Input\": \"{\\\"source\\\": \\\"aws.events\\\", \\\"detail-type\\\": \\\"Scheduled Event\\\"}\"
  }]"
```

**Verify the rule is active**:
```bash
aws events describe-rule --name nexus-warmup --region ap-southeast-2 \
  --query '{Name: Name, State: State, Schedule: ScheduleExpression}'
# Expected: { "Name": "nexus-warmup", "State": "ENABLED", "Schedule": "rate(5 minutes)" }

aws events list-targets-by-rule --rule nexus-warmup --region ap-southeast-2
# Expected: one target pointing at nexus_observability_tool Lambda
```

**Test the warmup manually** (after deploying updated image):
```bash
AWS_CLI_READ_TIMEOUT=0 aws lambda invoke \
  --function-name nexus_observability_tool \
  --region ap-southeast-2 \
  --payload '{"source": "aws.events", "detail-type": "Scheduled Event"}' \
  --cli-binary-format raw-in-base64-out \
  /tmp/warmup_test.json

cat /tmp/warmup_test.json
# Expected: {"statusCode": 200, "body": "warm"}
```

> **Note**: `AWS_CLI_READ_TIMEOUT=0` is required for the first invocation after a cold start — the default CLI timeout is shorter than the Lambda init time. Subsequent warm invocations complete in ~200ms.

**Limitation**: This keeps **one** container warm. A concurrency spike (multiple simultaneous users after idle) may still cold-start additional containers. For this use case (small team, low burst), one warm container is sufficient.

---

### Issue 4 — Long-running operations exceeded API Gateway 29-second limit (2026-07-10)

**Symptom**: `POST /kpis/extract` timed out at 60 seconds (Lambda limit) with API Gateway already having returned a 504 at the 29-second mark. The operation — classifying a document, calling the LLM for fact extraction, and validating results — inherently takes longer than 29 seconds.

**Root cause**: API Gateway's integration timeout is hard-capped at 29 seconds. There is no configuration to raise it. Synchronous LLM-based extraction cannot fit within this window.

**Fix**: Async job pattern — submit returns immediately (202), client polls for result.

#### New endpoints

| Method | Path | Description |
|--------|------|-------------|
| `POST` | `/kpis/extract/async` | Submit extraction job, returns `job_id` immediately |
| `GET` | `/jobs/{job_id}` | Poll job status and retrieve result when done |
| `GET` | `/jobs/` | List recent jobs |

#### How it works

```
Client                    API Gateway              Lambda
  │                           │                      │
  │  POST /kpis/extract/async │                      │
  │──────────────────────────►│                      │
  │                           │──────────────────────►
  │                           │   creates DB job     │
  │                           │   fires async invoke ─┤──► Lambda (job worker)
  │◄──────────────────────────│                      │        │
  │  202 { job_id, poll_url } │                      │   updates DB
  │                           │                      │   status: running → done
  │  GET /jobs/{job_id}       │                      │
  │──────────────────────────►│──────────────────────►
  │◄──────────────────────────│◄──────────────────────
  │  { status: "done", result }                      │
```

**On Lambda**: `POST /kpis/extract/async` calls `boto3 lambda.invoke(InvocationType='Event')` — Lambda returns 202 immediately and processes the job asynchronously. The `handler()` wrapper detects the `nexus_job_id` key and routes to `process_job()`, bypassing Mangum entirely.

**Locally**: falls back to `threading.Thread` so development works without AWS credentials.

#### New files

| File | Purpose |
|------|---------|
| `backend/api/job_worker.py` | Runs the job outside ASGI context — resolves LLM and registry via `lru_cache` singletons |
| `backend/api/jobs_router.py` | `GET /jobs/` and `GET /jobs/{job_id}` |

#### New DB table (`nexus_jobs`)

Added to `backend/db/schema_template/nexus_schema.sql` (idempotent — applied automatically on next startup):

```sql
CREATE TABLE IF NOT EXISTS nexus_jobs (
    job_id      TEXT PRIMARY KEY,
    job_type    TEXT NOT NULL,
    status      TEXT NOT NULL DEFAULT 'pending',  -- pending | running | done | failed
    params      TEXT NOT NULL,                    -- JSON
    result      TEXT,                             -- JSON, populated on done
    error       TEXT,                             -- populated on failed
    created_at  TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    updated_at  TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);
```

#### Usage example

```bash
BASE=https://ch3lr5e5g7.execute-api.ap-southeast-2.amazonaws.com

# Submit
curl -X POST $BASE/kpis/extract/async \
  -H "Content-Type: application/json" \
  -d '{"document_id":"<id>","company_id":"acme","period":"2025-Q1"}'
# → {"job_id":"abc-123","status":"pending","poll_url":"/jobs/abc-123"}

# Poll
curl $BASE/jobs/abc-123
# → {"job_id":"abc-123","status":"running",...}
# → {"job_id":"abc-123","status":"done","result":{...},"params":{...}}
```

---

### Issue 5 — Chat requests hung for 5 minutes, then 503'd (2026-07-12)

**Symptom**: `POST /chat/ask` intermittently (~16% of invocations sampled over 3 days) returned a 503 to the browser, and CloudWatch showed the underlying Lambda invocation running all the way to `Status: timeout` at the full 300s function timeout, with **zero log output** between `START` and the timeout `REPORT` line.

**Root cause**: `AzureOpenAIClient` and `GeminiClient` (`backend/utilites/llm_models.py`) constructed their SDK clients (`AzureOpenAI(...)`, `genai.Client(...)`) with no explicit `timeout=`. When the LLM provider stalled (throttling, network blip), the call blocked on the SDK's own default timeout (openai SDK defaults to 600s) instead of failing fast — so it ran until Lambda's hard 300s kill. Meanwhile API Gateway's fixed **29-second integration timeout** (hard cap, not configurable) had already dropped the client connection and returned 503 at the 29s mark, regardless of what the Lambda was still doing.

**Diagnosed by**:
- `aws logs filter-log-events` on `/aws/lambda/nexus_observability_tool`, filtering for `REPORT` lines and sorting by `Duration` — found `Duration: 300000.00 ms ... Status: timeout` entries.
- Traced the call path via the code graph: `ask_agentic` → `run_tool_based_agentic_query` → `classify_intent`/`synthesize_answer` → `llm.complete` → the un-timed SDK client constructors.

**Fix — bound every LLM/embedding call to 20s** (under API Gateway's 29s ceiling), applied at the client-construction level so every call site is covered without touching each call site individually:

```python
# backend/utilites/llm_models.py
LLM_CALL_TIMEOUT_SECONDS = 20

# AzureOpenAIClient.__init__
self.client = AzureOpenAI(
    api_key=..., api_version=..., azure_endpoint=...,
    timeout=LLM_CALL_TIMEOUT_SECONDS,
    max_retries=1,
)

# GeminiClient.__init__
from google.genai import types
self.client = genai.Client(
    api_key=...,
    http_options=types.HttpOptions(timeout=LLM_CALL_TIMEOUT_SECONDS * 1000),  # ms
)

# get_crewai_llm()
return LLM(model=..., api_key=..., temperature=..., timeout=LLM_CALL_TIMEOUT_SECONDS)
```

Same `timeout=`/`http_options` pattern was also applied to the two embedding-function clients in `backend/chatbot/chat.py` (`AzureOpenAIEmbeddingFunction`, `GeminiEmbeddingFunction`) since they construct their own separate SDK clients.

**Effect**: a stuck provider call now raises within 20s instead of hanging to Lambda's 300s kill — the existing `except Exception` fallback in `ask_agentic` returns a graceful error instead of nothing. **This alone does not eliminate 503s** for questions that legitimately take longer than 29s to compute (e.g. SQL routing, see Issue 4's async-job pattern, extended to chat in Issue 6) — it only stops indefinite hangs.

---

### Issue 6 — Chat's async-job self-invoke denied by IAM (2026-07-12)

**Symptom**: After extending the Issue 4 async-job pattern to chat (new `POST /chat/ask/async` — same shape as `/kpis/extract/async`), the endpoint returned 500 `Internal Server Error`.

**Root cause**: The Lambda execution role had only `AWSLambdaBasicExecutionRole` attached (log-write permissions) — nothing granting `lambda:InvokeFunction`. The async endpoint's `boto3.client("lambda").invoke(..., InvocationType="Event")` self-invoke call was rejected:
```
botocore.exceptions.ClientError: An error occurred (AccessDeniedException) when calling
the Invoke operation: User: ...role/nexus_observability_tool-role-xxxx is not authorized
to perform: lambda:InvokeFunction on resource: ...function:nexus_observability_tool
```
This affected `/kpis/extract/async` too — it had apparently never been exercised in production before this.

**Diagnosed by**: `aws logs filter-log-events` with `--filter-pattern "?ERROR ?Traceback ?Exception"` — the traceback was in the logs immediately.

**Fix**: scoped inline policy on the Lambda's own execution role, granting `lambda:InvokeFunction` on just its own ARN (least privilege, not a wildcard):

```bash
ROLE_ARN=$(aws lambda get-function-configuration --region ap-southeast-2 \
  --function-name nexus_observability_tool --query "Role" --output text)
ROLE_NAME=$(basename "$ROLE_ARN")
FN_ARN=$(aws lambda get-function-configuration --region ap-southeast-2 \
  --function-name nexus_observability_tool --query "FunctionArn" --output text)

cat > policy.json <<EOF
{
  "Version": "2012-10-17",
  "Statement": [{
    "Effect": "Allow",
    "Action": "lambda:InvokeFunction",
    "Resource": "${FN_ARN}"
  }]
}
EOF

aws iam put-role-policy --role-name "$ROLE_NAME" \
  --policy-name "SelfInvokeForAsyncJobs" \
  --policy-document file://policy.json
```

**Important — IAM changes don't apply to already-warm containers immediately.** Warm Lambda execution environments cache STS credentials issued before the policy change; retrying the same request against a warm container can still fail with the old `AccessDeniedException` for up to several minutes. To force fresh credentials immediately instead of waiting, touch the function config (any no-op update triggers new execution environments):
```bash
aws lambda update-function-configuration --region ap-southeast-2 \
  --function-name nexus_observability_tool --timeout 300
aws lambda wait function-updated --region ap-southeast-2 \
  --function-name nexus_observability_tool
```
Verify the policy took effect with a dry-run simulation before retrying live traffic:
```bash
aws iam simulate-principal-policy --policy-source-arn "$ROLE_ARN" \
  --action-names "lambda:InvokeFunction" --resource-arns "$FN_ARN" \
  --query "EvaluationResults[].{Action:EvalActionName,Decision:EvalDecision}"
# Expected: Decision = allowed
```

---

### Issue 7 — One chat request took down the entire backend (2026-07-12)

**Symptom**: Shortly after Issue 6's fix, the whole app started showing "Backend Offline" — not just chat. `GET /health` itself started returning 500.

**Root cause**: `backend/api/job_worker.py`'s `chat_ask` handler called `asyncio.run(run_tool_based_agentic_query(...))` **directly inside the Lambda handler thread** (`main.py`'s `handler()`, in the `nexus_job_id` branch). `asyncio.run()` explicitly nulls out the calling thread's event loop when it finishes (`asyncio.set_event_loop(None)`). Lambda reuses warm containers **sequentially** across different invocations — so when a chat-job invocation and a later plain HTTP request (routed through Mangum, which needs a working event loop) landed in the same warm container's MainThread, every request after the chat job in that container broke with:
```
RuntimeError: There is no current event loop in thread 'MainThread'.
```
This is exactly the `/health` 500 — one async chat job silently poisoned its container for every subsequent request until that container was recycled.

**Diagnosed by**: `aws logs filter-log-events` on the recent window — found the `RuntimeError` on a *different, interleaved* `RequestId` immediately after a `chat_ask` job had run in the same log stream. Reproduced locally in isolation to confirm: `asyncio.run()` called directly in a thread breaks that thread's `asyncio.get_event_loop()` afterward; called inside a child `threading.Thread` instead, the parent thread's loop state is untouched (event-loop state is thread-local).

**Fix**: isolate any job handler that might call `asyncio.run()` into its own thread, so it can never touch the container's MainThread event-loop state:

```python
# main.py — handler(), nexus_job_id branch
if "nexus_job_id" in event:
    job_id = event["nexus_job_id"]
    from backend.api.job_worker import process_job
    import threading
    # Job handlers may call asyncio.run(), which nulls out the calling
    # thread's event loop when it finishes. MainThread is reused by Mangum
    # for unrelated HTTP requests on the next warm invocation — running
    # asyncio.run() there directly would break every subsequent request
    # in this container. asyncio's loop state is thread-local, so
    # isolating to a child thread keeps MainThread's loop state untouched.
    t = threading.Thread(target=process_job, args=(job_id,))
    t.start()
    t.join()
    return {"statusCode": 200, "body": "done"}
```

**This requires a new image build/deploy** (it's a `main.py` code change, not a config/IAM change) — see Step-by-Step Deployment Procedure below. After deploying, verify by running an async chat job to completion and immediately hitting `/health` twice in the same window — both should return 200.

---

### Issue 8 — Chatbot felt slow even when working correctly (2026-07-12)

**Symptom**: Once Issues 5-7 were fixed, chat worked end-to-end but felt sluggish: a browser network waterfall showed `GET /jobs/{job_id}` being polled roughly every ~1s, with **each individual poll costing ~850ms** on its own (not the job — just the lightweight status read), and SQL-routed questions taking 40-70s+ overall.

**Root cause (two independent causes)**:

1. **Per-poll latency**: the Postgres DB (`aws-0-ap-south-1.pooler.supabase.com`, Mumbai) is cross-region from the Lambda (`ap-southeast-2`, Sydney). The shared SQLAlchemy `engine` has `pool_pre_ping=True`, which issues a hidden liveness round-trip on every connection checkout, on top of the actual query — roughly doubling round-trip cost on a trivial one-row `nexus_jobs` read/write, polled every ~1s.
2. **LLM call count**: a SQL-routed chat question required **4 sequential LLM calls** — `classify_intent` (orchestrator) → SQL draft agent → SQL review agent → answer-synthesis agent (`backend/chatbot/text_to_sql_chatbot.py`) — plus the answer agent had a `DatabaseOperationsTool` attached that could trigger further nondeterministic nested tool-reasoning calls, even though its own task instructions said to answer using only the SQL results already in hand.

**Fix 1 — dedicated low-latency engine for the jobs table** (`backend/db/db_client.py`). Lambda execution environments *freeze* between invocations, which can let a pooled connection go stale in ways `pool_recycle=240` (age-based) and TCP keepalives won't catch until thaw — so `pool_pre_ping` was **not** removed globally (that would risk silent failures on write paths like KPI/fact writes that aren't retried today). Instead, a second engine, scoped only to the frequently-polled `nexus_jobs` table, skips the pre-ping — a rare stale-connection error there just self-heals on the client's next poll ~1-3s later:

```python
# backend/db/db_client.py — added after the main `engine` definition
jobs_engine = create_engine(
    DATABASE_URL,
    pool_size=2,
    max_overflow=1,
    pool_timeout=30,
    pool_pre_ping=False,   # main `engine` keeps pool_pre_ping=True — untouched
    pool_recycle=240,
    connect_args={
        "connect_timeout": 10,
        "keepalives": 1, "keepalives_idle": 30,
        "keepalives_interval": 5, "keepalives_count": 5,
    },
)
```
`create_job`, `get_job`, `update_job_status`, and `list_jobs` were repointed from `engine` to `jobs_engine`; every other DB function (writes, KPI/fact reads, schema init) is untouched and keeps its stale-connection protection.

**Fix 2 — fewer sequential LLM calls** (`backend/chatbot/text_to_sql_chatbot.py`):
- Collapsed the 2-agent SQL draft+review CrewAI flow (`_generate_sql`) into a single agent that self-checks its own SQL in the same call (folded the review instructions into the draft task's prompt as a final "re-check your own SQL" rule).
- Removed the unused `DatabaseOperationsTool` from the answer-synthesis agent (`_answer_question`) — its task already restricts it to "answer using only these SQL results," so the tool was logically unreachable and only added latency risk.
- The existing 3-attempt `_repair_sql` retry loop, all SQL-safety validation (`normalize_sql`, `DANGEROUS_SQL`, table allowlisting), and `execute_sql` were **not** touched.
- Net effect: SQL-routed chat drops from 4 sequential LLM calls to 3 (`classify_intent`, draft+self-review, answer).

**Result** (measured against the exact same question, same session): the baseline "which portco has highest ai roi" job took **~72s** end-to-end before these fixes; after, the same question (even hitting an unrelated SQL-repair retry mid-run) completed in **~42s**.

**Note**: these are pure application-code changes (no schema, no API contract, no IAM/infra change) — folded into the same image rebuild as Issue 7's fix.

**Also recommended (frontend-side, not implemented in this repo)**: the polling interval itself has no backoff — switch from a fixed ~1s to ~1.5s for the first 10s, ~3s until 30s, ~5s after, with a ~5 minute hard timeout, and show a "thinking…" state immediately on job creation instead of a blank bubble.

---

## Step-by-Step Deployment Procedure

### Prerequisites
- AWS CLI configured (`aws configure`) with access to account `231733667519`
- Docker Desktop running
- Authenticated to ECR:
```bash
aws ecr get-login-password --region ap-southeast-2 \
  | docker login --username AWS --password-stdin \
    231733667519.dkr.ecr.ap-southeast-2.amazonaws.com
```

---

### Step 1 — Build the Docker image

```bash
docker build --platform linux/amd64 --provenance=false -t nexus-observability-tool .
```

> `--platform linux/amd64` is required for Lambda compatibility on any host architecture.
> `--provenance=false` prevents ECR from rejecting the push due to a multi-manifest image.
> See the **Key Concepts** section above for a full explanation of both flags.

---

### Step 2 — Tag and push to ECR

```bash
ECR_URI=231733667519.dkr.ecr.ap-southeast-2.amazonaws.com/nexus-observability-tool

docker tag nexus-observability-tool:latest $ECR_URI:latest
docker push $ECR_URI:latest
```

Confirm the push:
```bash
aws ecr describe-images \
  --repository-name nexus-observability-tool \
  --region ap-southeast-2 \
  --query 'imageDetails[?contains(imageTags,`latest`)].[imagePushedAt,imageSizeInBytes]' \
  --output table
```

---

### Step 3 — Update Lambda to use the new image

```bash
aws lambda update-function-code \
  --function-name nexus_observability_tool \
  --image-uri 231733667519.dkr.ecr.ap-southeast-2.amazonaws.com/nexus-observability-tool:latest \
  --region ap-southeast-2

aws lambda wait function-updated \
  --function-name nexus_observability_tool \
  --region ap-southeast-2
```

Confirm state is `Active` and last update status is `Successful`:
```bash
aws lambda get-function-configuration \
  --function-name nexus_observability_tool \
  --region ap-southeast-2 \
  --query '[State, LastUpdateStatus, CodeSha256]'
```

---

### Step 4 — Verify API Gateway is configured correctly

These only need to be checked once; they persist. Run to confirm they're in place:

```bash
# Confirm $default catch-all route exists
aws apigatewayv2 get-routes \
  --api-id ch3lr5e5g7 \
  --region ap-southeast-2 \
  --query 'Items[?RouteKey==`$default`]'

# Confirm $default stage exists
aws apigatewayv2 get-stages \
  --api-id ch3lr5e5g7 \
  --region ap-southeast-2 \
  --query 'Items[?StageName==`$default`].[StageName,AutoDeploy,LastDeploymentStatusMessage]'
```

Expected output for routes: one item with `"RouteKey": "$default"`.
Expected output for stages: `$default` stage with `AutoDeploy: true`.

**If either is missing**, recreate them (see Issue 1 and Issue 2 fixes above).

---

### Step 5 — Verify EventBridge warmup rule is active

This was created once (2026-07-10) and persists. Confirm it is still in place after every deploy:

```bash
aws events describe-rule --name nexus-warmup --region ap-southeast-2 \
  --query '{Name: Name, State: State, Schedule: ScheduleExpression}'
# Expected: { "State": "ENABLED", "Schedule": "rate(5 minutes)" }
```

If it ever gets deleted, recreate it using the commands in **Issue 3** above.

Test the warmup handler manually after each new image deploy:
```bash
AWS_CLI_READ_TIMEOUT=0 aws lambda invoke \
  --function-name nexus_observability_tool \
  --region ap-southeast-2 \
  --payload '{"source": "aws.events", "detail-type": "Scheduled Event"}' \
  --cli-binary-format raw-in-base64-out \
  /tmp/warmup_test.json && cat /tmp/warmup_test.json
# Expected: {"statusCode": 200, "body": "warm"}
```

---

### Step 6 — Smoke test the live endpoints

```bash
BASE=https://ch3lr5e5g7.execute-api.ap-southeast-2.amazonaws.com

curl $BASE/health
# Expected: {"status":"200","message":"Platform is running"}

curl $BASE/warmup
# Expected: {"status":"warm"}

curl $BASE/jobs/
# Expected: {"jobs":[...]}

curl $BASE/kpis/
# Expected: {"kpis":[...]}

curl $BASE/docs
# Expected: Swagger UI HTML
```

---

### Step 7 — Check Lambda logs if anything looks wrong

```bash
aws logs filter-log-events \
  --log-group-name /aws/lambda/nexus_observability_tool \
  --region ap-southeast-2 \
  --start-time $(date -d '5 minutes ago' +%s000) \
  --output text \
  --query 'events[*].message'
```

Look for:
- `ERROR` lines — application exceptions
- `Task timed out` — Lambda exceeded 60s timeout (use `/kpis/extract/async` for long operations)
- `Warmup ping received from EventBridge` — confirms warmup rule is firing correctly
- `Init Duration` in REPORT lines — cold start time (~1s init + startup at module level)
- No `START RequestId` after hitting the URL — means API Gateway is not reaching Lambda (recheck Step 4)

---

## Key Resource IDs

| Resource | ID / ARN |
|----------|----------|
| ECR repository | `nexus-observability-tool` |
| Lambda function | `nexus_observability_tool` |
| API Gateway ID | `ch3lr5e5g7` |
| API Gateway integration ID | `622l7uo` |
| Lambda role | `[REDACTED_AWS_ARN_1]-...` |
| Lambda role inline policy | `SelfInvokeForAsyncJobs` — grants `lambda:InvokeFunction` on its own ARN, required for the async job self-invoke pattern (see Issue 6) |
| Lambda log group | `/aws/lambda/nexus_observability_tool` |
| EventBridge rule | `nexus-warmup` (ap-southeast-2) |
| Live URL | `https://ch3lr5e5g7.execute-api.ap-southeast-2.amazonaws.com` |

---

## API Routes

| Method | Path | Description |
|--------|------|-------------|
| GET | `/health` | Health check |
| GET | `/warmup` | Lightweight keepalive (also used by EventBridge) |
| GET/POST | `/kpis/*` | KPI endpoints |
| POST | `/kpis/extract/async` | Submit long extraction as background job |
| GET/POST | `/chat/*` | Chatbot endpoints |
| POST | `/chat/ask/async` | Submit chat question as background job (avoids 29s timeout on slow SQL/RAG routes — see Issue 8) |
| GET/POST | `/documents/*` | Document endpoints |
| GET | `/jobs/` | List background jobs |
| GET | `/jobs/{job_id}` | Poll background job status and result |
| GET | `/docs` | Swagger UI |
| GET | `/redoc` | ReDoc UI |
