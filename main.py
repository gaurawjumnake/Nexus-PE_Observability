from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from backend.api.deps import init_shared_resources
# from backend.chatbot.orchestrator import router as analytics_router
# from backend.chatbot.chat import router as chat_router
from backend.api.documents_router import router as documents_router
from backend.api.kpi_router import router as kpi_router
from backend.api.chatbot_router import router as chat_router


@asynccontextmanager
async def lifespan(app: FastAPI):
    init_shared_resources(app)
    yield


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
    return {"status": "200",
            "Message":"Platform is running"}


if __name__ == "__main__":
    import uvicorn
    uvicorn.run("main:app", host="0.0.0.0", port=8005, reload=True)
