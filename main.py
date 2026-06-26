from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware

from backend.api.deps import init_shared_resources, get_registry, check_db
from backend.api.documents_router import router as documents_router
from backend.api.kpi_router import router as kpi_router
from backend.api.chatbot_router import router as chat_router
from backend.utilites.app_logger import Logger
from backend.utilites.llm_models import get_llm_client

log = Logger()

_resources_ready = False


@asynccontextmanager
async def lifespan(app: FastAPI):
    # Fast path: only validate env vars — no heavy I/O at cold start.
    log.log_info("Application starting up...")
    init_shared_resources(app)
    log.log_info("Server ready (resources will lazy-load on first request)")
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


@app.middleware("http")
async def lazy_resource_init(request: Request, call_next):
    # Runs once per Lambda instance. lru_cache on get_llm_client / get_registry
    # means the work is only done the first time; subsequent requests are free.
    global _resources_ready
    if not _resources_ready:
        log.log_info("First request — initialising shared resources...")
        request.app.state.llm = get_llm_client()
        request.app.state.registry = get_registry()
        _resources_ready = True
        log.log_info("Shared resources ready")
    return await call_next(request)

app.include_router(documents_router)
app.include_router(kpi_router)
app.include_router(chat_router)

@app.get("/health")
async def health():
    return {"status": "200", "message": "Platform is running"}


@app.get("/health/db")
async def health_db():
    result = check_db()
    if result["status"] != "ok":
        from fastapi import HTTPException
        raise HTTPException(status_code=503, detail=result.get("detail", "DB unreachable"))
    return result

from mangum import Mangum
handler = Mangum(app, lifespan="auto")


if __name__ == "__main__":
    import uvicorn
    uvicorn.run("main:app", host="0.0.0.0", port=8005, reload=True)
