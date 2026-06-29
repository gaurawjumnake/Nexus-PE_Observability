from contextlib import asynccontextmanager

from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware

from backend.api.deps import init_shared_resources, get_registry, check_db
from backend.api.documents_router import router as documents_router
from backend.api.kpi_router import router as kpi_router
from backend.api.chatbot_router import router as chat_router
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


@app.get("/health")
async def health():
    return {"status": "200", "message": "Platform is running"}


@app.get("/health/db")
async def health_db():
    result = check_db()
    if result["status"] != "ok":
        raise HTTPException(status_code=503, detail=result.get("detail", "DB unreachable"))
    return result

from mangum import Mangum
handler = Mangum(app, lifespan="auto")


if __name__ == "__main__":
    import uvicorn
    uvicorn.run("main:app", host="0.0.0.0", port=8005, reload=True)
