from contextlib import asynccontextmanager

from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware

from backend.api.deps import init_shared_resources, get_registry, check_db
from backend.api.documents_router import router as documents_router
from backend.api.kpi_router import router as kpi_router
from backend.api.chatbot_router import router as chat_router
from backend.api.jobs_router import router as jobs_router
from backend.utilites.app_logger import Logger
from backend.utilites.llm_models import get_llm_client

log = Logger()


@asynccontextmanager
async def lifespan(app: FastAPI):
    log.log_info("Application starting up...")

    # 1. Env vars + DB connection + schema — hard-fail if broken
    init_shared_resources(app)

    # 2. LLM client — fail fast so a missing API key surfaces at startup
    app.state.llm = get_llm_client()
    log.log_info("LLM client ready")

    # 3. Registry — loads from YAML if empty; lru_cached after first call
    app.state.registry = get_registry()
    log.log_info("Registry ready")

    log.log_info("Application ready")
    yield
    log.log_info("Application shutting down")


app = FastAPI(title="Nexus KPI Pipeline", lifespan=lifespan)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(documents_router)
app.include_router(kpi_router)
app.include_router(chat_router)
app.include_router(jobs_router)


@app.get("/health")
async def health():
    return {"status": "200", "message": "Platform is running"}


@app.get("/health/db")
async def health_db():
    result = check_db()
    if result["status"] != "ok":
        raise HTTPException(status_code=503, detail=result.get("detail", "DB unreachable"))
    return result


@app.get("/warmup")
async def warmup():
    """
    Lightweight endpoint for EventBridge keepalive pings via API Gateway.
    Returns immediately — no DB or LLM calls.
    """
    return {"status": "warm"}


# ---------------------------------------------------------------------------
# Lambda entrypoint
# ---------------------------------------------------------------------------
# _mangum handles normal API Gateway HTTP events.
# handler() wraps it to intercept two additional event shapes before they
# reach Mangum (which would fail to parse them):
#
#   1. EventBridge scheduled ping  {"source": "aws.events", ...}
#      → returns immediately so the container stays warm without a real request.
#
#   2. Async job invocation        {"nexus_job_id": "<uuid>"}
#      → runs the job worker inline (Lambda was invoked with InvocationType=Event
#        by POST /kpis/extract/async, so the original caller already got 202).
# ---------------------------------------------------------------------------
from mangum import Mangum

_mangum = Mangum(app, lifespan="off")


def handler(event, context):
    # EventBridge warmup ping
    if event.get("source") == "aws.events":
        log.log_info("Warmup ping received from EventBridge")
        return {"statusCode": 200, "body": "warm"}

    # Async job worker invocation (self-invoked with InvocationType=Event)
    if "nexus_job_id" in event:
        job_id = event["nexus_job_id"]
        log.log_info(f"Processing background job: {job_id}")
        from backend.api.job_worker import process_job
        import threading
        # Run in a dedicated thread: job handlers may call asyncio.run(),
        # which nulls out the calling thread's event loop when it finishes.
        # This container's MainThread is reused by Mangum for unrelated HTTP
        # requests on the next warm invocation — if asyncio.run() ran there
        # directly, it would leave every subsequent request in this container
        # broken with "no current event loop in thread 'MainThread'".
        # asyncio's loop state is thread-local, so isolating to a child
        # thread keeps MainThread's loop state untouched.
        t = threading.Thread(target=process_job, args=(job_id,))
        t.start()
        t.join()
        return {"statusCode": 200, "body": "done"}

    # Normal API Gateway request
    return _mangum(event, context)


if __name__ == "__main__":
    import uvicorn
    uvicorn.run("main:app", host="0.0.0.0", port=8005, reload=True)
