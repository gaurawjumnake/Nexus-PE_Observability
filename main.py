"""
Nexus API entrypoint.

Run with: uvicorn backend.main:app --reload
"""
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from backend.api.deps import init_shared_resources, shutdown_shared_resources
from backend.api.documents_router import router as documents_router
from backend.api.kpi_router import router as kpi_router


@asynccontextmanager
async def lifespan(app: FastAPI):
    # Startup: one shared LLM client + one shared RegistryMCPClient
    # (single MCP server subprocess) for the whole app lifetime.
    init_shared_resources(app)
    yield
    # Shutdown: terminate the MCP server subprocess.
    shutdown_shared_resources(app)


app = FastAPI(title="Nexus KPI Pipeline", lifespan=lifespan)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(documents_router)
app.include_router(kpi_router)


@app.get("/health")
async def health():
    return {"status": "200",
            "Message":"Platform is running"}


if __name__ == "__main__":
    import uvicorn
    uvicorn.run("main:app", host="0.0.0.0", port=8000, reload=True)