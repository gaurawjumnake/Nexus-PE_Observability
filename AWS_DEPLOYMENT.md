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
# main.py (last lines — after all routers and middleware are registered)
from mangum import Mangum
handler = Mangum(app, lifespan="auto")
```

The name `handler` must match the `CMD` entry point in the Dockerfile (`main.handler`). Lambda calls this function for every invocation.

**`lifespan` parameter:**

| Value | Behaviour | When to use |
|-------|-----------|-------------|
| `"auto"` | Runs ASGI lifespan (startup/shutdown) on every invocation | Default; fine for local dev, adds cold-start overhead on Lambda |
| `"off"` | Skips lifespan entirely | Recommended for Lambda — move startup I/O to module-level globals or lazy-init middleware instead |

> **Current state**: `lifespan="auto"` is set in `main.py`. See Known Issue 3 below for the impact and fix.

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
- `CMD ["main.handler"]` tells Lambda to call the `handler` object in `main.py` (the Mangum instance).
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

## Issues Found & Solutions Applied (2026-06-24)

### Issue 1 — API Gateway had no catch-all route

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

### Issue 2 — Named stage `default` prepended `/default/` to every path

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

### Known Issue 3 — `lifespan="auto"` causes 9-second cold starts (not fixed yet)

**Symptom**: First request after a cold start takes ~15 seconds (9s init + 6s request).

**Root cause**: `Mangum(app, lifespan="auto")` in `main.py` runs the full ASGI lifespan (startup → request → shutdown) on every Lambda invocation. The lazy-init middleware rebuilds the registry (~5s) on the first request per container.

**Fix (requires image rebuild)**:
Change `main.py`:
```python
# Before
handler = Mangum(app, lifespan="auto")

# After
handler = Mangum(app, lifespan="off")
```
Move any startup I/O out of the lifespan context and into module-level globals so they survive across warm invocations without re-running.

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
```

Wait for the update to complete:
```bash
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

### Step 5 — Smoke test the live endpoints

```bash
BASE=https://ch3lr5e5g7.execute-api.ap-southeast-2.amazonaws.com

curl $BASE/health
# Expected: {"status":"200","Message":"Platform is running"}

curl $BASE/kpis
# Expected: {"kpis":[...]}

curl $BASE/docs
# Expected: Swagger UI HTML
```

---

### Step 6 — Check Lambda logs if anything looks wrong

```bash
# Tail recent logs
aws logs filter-log-events \
  --log-group-name /aws/lambda/nexus_observability_tool \
  --region ap-southeast-2 \
  --start-time $(date -d '5 minutes ago' +%s000) \
  --output text \
  --query 'events[*].message'
```

Look for:
- `ERROR` lines — application exceptions
- `Task timed out` — Lambda exceeded 60s timeout
- `Init Duration` in REPORT lines — cold start time (expected ~9s currently)
- No `START RequestId` after hitting the URL — means API Gateway is not reaching Lambda (recheck Step 4)

---

## Key Resource IDs

| Resource | ID / ARN |
|----------|----------|
| ECR repository | `nexus-observability-tool` |
| Lambda function | `nexus_observability_tool` |
| API Gateway ID | `ch3lr5e5g7` |
| API Gateway integration ID | `622l7uo` |
| Lambda role | `arn:aws:iam::231733667519:role/nexus_observability_tool-role-...` |
| Lambda log group | `/aws/lambda/nexus_observability_tool` |
| Live URL | `https://ch3lr5e5g7.execute-api.ap-southeast-2.amazonaws.com` |

## API Routes

| Method | Path | Description |
|--------|------|-------------|
| GET | `/health` | Health check |
| GET/POST | `/kpis/*` | KPI endpoints |
| GET/POST | `/chat/*` | Chatbot endpoints |
| GET/POST | `/documents/*` | Document endpoints |
| GET | `/docs` | Swagger UI |
| GET | `/redoc` | ReDoc UI |
