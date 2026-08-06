import json
import os
import threading

from backend.chatbot.chat import (
    IndexDocumentRequest,
    ChatQueryResponse,
    ChatQueryRequest,
    index_document_chunks,
    run_rag_query,
)
from backend.chatbot.chat_history import ChatHistoryStore
from backend.chatbot.orchestrator import (
    AgenticAskRequest,
    AgenticToolAskResponse,
    run_tool_based_agentic_query,
)

from backend.utilites.app_logger import Logger
from backend.api.deps import get_registry
import backend.db.db_client as db
from fastapi import APIRouter, HTTPException

log = Logger()

router = APIRouter(prefix="/chat", tags=["chat"])

## Chat -----------------------------------------------------------------


@router.post("/index/{document_id}")
async def index_document(document_id: str, request: IndexDocumentRequest):
    try:
        return index_document_chunks(
            document_id=document_id,
            company_id=request.company_id,
            period=request.period,
            file_name=request.file_name,
        )
    except HTTPException:
        raise
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc))
    except Exception as exc:
        log.log_error(f"Chat indexing failed: {type(exc).__name__}: {exc}")
        raise HTTPException(status_code=500, detail=f"Chat indexing failed: {exc}")


@router.post("/query", response_model=ChatQueryResponse)
async def query_chat(request: ChatQueryRequest):
    """Direct RAG-only query (bypasses the orchestrator's SQL/RAG routing)."""
    try:
        return await run_rag_query(request)
    except Exception as exc:
        log.log_error(f"Chat query failed: {type(exc).__name__}: {exc}")
        raise HTTPException(status_code=500, detail=f"Chat query failed: {exc}")


# Orchestrator ------------------------------------------------------------


@router.post("/ask", response_model=AgenticToolAskResponse)
async def ask_agentic(body: AgenticAskRequest):
    """Orchestrated ask.

    1. classify_intent() scores the question for SQL vs RAG vs registry-lookup signals.
    2. Routes deterministically to text-to-SQL, RAG, registry, or both sql+rag.
    3. Synthesises one answer when both sql+rag were used.
    """
    try:
        log.log_info(f"Agentic ask started: {body.model_dump()}")
        return await run_tool_based_agentic_query(body, get_registry())
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc))
    except Exception as exc:
        log.log_error(f"Agentic orchestrator failed: {type(exc).__name__}: {exc}")
        return AgenticToolAskResponse(
            response=(
                "Something went wrong while processing that request. "
                "Please try again — if it keeps happening, let the team know."
            ),
            tools_used=[],
            route_plan=None,
            sql_query_executed=None,
            iterations_hint=f"error: {type(exc).__name__}",
        )


@router.post("/ask/async")
async def ask_agentic_async(body: AgenticAskRequest):
    """
    Submit an agentic ask as a background job and return immediately (202).
    Poll GET /jobs/{job_id} for status and result.

    Use this instead of POST /ask for anything that might run long — API
    Gateway's HTTP API integration has a fixed 29s timeout it will 503 the
    caller at regardless of what the Lambda is still doing, and tool chains
    involving SQL + RAG + synthesis routinely exceed that.

    On Lambda: invokes the same function asynchronously (InvocationType=Event).
    Locally: runs in a background thread.
    """
    job_id = db.create_job("chat_ask", body.model_dump())
    log.log_info(f"Async chat ask job created: job_id={job_id}")

    fn_name = os.getenv("AWS_LAMBDA_FUNCTION_NAME")
    if fn_name:
        import boto3
        boto3.client("lambda", region_name=os.getenv("AWS_REGION", "ap-southeast-2")).invoke(
            FunctionName=fn_name,
            InvocationType="Event",
            Payload=json.dumps({"nexus_job_id": job_id}),
        )
        log.log_info(f"Job {job_id} dispatched via Lambda async invocation")
    else:
        from backend.api.job_worker import process_job
        threading.Thread(target=process_job, args=(job_id,), daemon=True).start()
        log.log_info(f"Job {job_id} dispatched via background thread (local)")

    return {"job_id": job_id, "status": "pending", "poll_url": f"/jobs/{job_id}"}


@router.delete("/history/{session_id}")
async def clear_chat_history(session_id: str):
    """Clear all stored chat history for a session (UI 'New Chat' / clear action)."""
    found = ChatHistoryStore.clear(session_id)
    if not found:
        raise HTTPException(status_code=404, detail=f"No history found for session '{session_id}'")
    log.log_info(f"Chat history cleared for session_id={session_id}")
    return {"session_id": session_id, "cleared": True}