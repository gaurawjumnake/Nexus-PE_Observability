from backend.chatbot.chat import (
    run_rag_query, 
    IndexDocumentRequest, 
    CHROMA_COLLECTION, 
    ChatQueryResponse, 
    ChatQueryRequest, 
    index_document_chunks
)
from backend.chatbot.orchestrator import *

from backend.utilites.app_logger import Logger
from fastapi import APIRouter, HTTPException
import backend.db.db_client as db
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
    try:
        return await run_rag_query(request)
    except Exception as exc:
        log.log_error(f"Chat query failed: {type(exc).__name__}: {exc}")
        raise HTTPException(status_code=500, detail=f"Chat query failed: {exc}")
    
# # Orchastrator ----------------------------------------


@router.post("", response_model=AgenticToolAskResponse)
async def ask_agentic_tools(request: AgenticAskRequest):
    """Deterministic agentic endpoint.

    Uses a keyword + data-availability intent classifier to decide the tool
    route, then executes tools directly and synthesises the answer.
    No LLM-based tool selection — only LLM-based answer synthesis.
    """
    try:
        log.log_info(f"Agentic ask (deterministic) started: {request.model_dump()}")
        return await run_tool_based_agentic_query(request)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc))
    except Exception as exc:
        log.log_error(
            f"Deterministic agentic orchestrator failed: {type(exc).__name__}: {exc}"
        )
        raise HTTPException(
            status_code=500,
            detail=f"Agentic orchestrator failed: {exc}",
        )


# ---------------------------------------------------------------------------
# Original router-based endpoint (kept for backwards compatibility)
# ---------------------------------------------------------------------------


async def choose_route(request: AgenticAskRequest) -> RoutePlan:
    """Compatibility wrapper — uses the same deterministic classifier."""
    return classify_intent(request)


@router.post("/ask", response_model=AgenticAskResponse)
async def ask_agentic(request: AgenticAskRequest):
    try:
        log.log_info(f"Agentic ask started: {request.model_dump()}")
        route_plan = await choose_route(request)
        rag_result: Optional[ChatQueryResponse] = None
        sql_result: Optional[dict[str, Any]] = None

        if route_plan.route in {"sql", "both"}:
            log.log_info("Route includes SQL; invoking text-to-SQL")
            sql_result = await run_sql(request)
        if route_plan.route in {"rag", "both"}:
            log.log_info("Route includes RAG; invoking document retrieval")
            rag_result = await run_rag(request)

        question = request.message
        if request.contexts:
            question += f" (Context: {', '.join(request.contexts)})"

        answer = await synthesize_answer(
            question=question,
            route_plan=route_plan,
            rag_result=rag_result,
            sql_result=sql_result,
            verbose=False,
        )

        response = AgenticAskResponse(
            response=answer,
            route_plan=route_plan,
            rag=rag_result.model_dump() if rag_result else None,
            sql=sql_result,
        )
        log.log_info(
            f"Agentic ask completed: route={route_plan.route}, "
            f"has_rag={rag_result is not None}, has_sql={sql_result is not None}"
        )
        return response
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc))
    except Exception as exc:
        log.log_error(f"Agentic orchestrator failed: {type(exc).__name__}: {exc}")
        raise HTTPException(
            status_code=500, detail=f"Agentic orchestrator failed: {exc}"
        )
    

