"""
Job worker — runs a nexus_jobs entry to completion.

Called in two contexts:
  1. Lambda async invocation: handler() in main.py detects {"nexus_job_id": "..."} and
     calls process_job() directly (no ASGI, no app.state).
  2. Local dev background thread: POST /kpis/extract/async spawns threading.Thread
     targeting process_job().

Both paths resolve llm and registry via their lru_cached singletons so the
expensive initialisation only happens once per container/process.
"""

import backend.db.db_client as db
from backend.utilites.llm_models import get_llm_client
from backend.api.deps import get_registry
from backend.utilites.app_logger import Logger

log = Logger()

_HANDLERS: dict = {}


def _register(job_type: str):
    def decorator(fn):
        _HANDLERS[job_type] = fn
        return fn
    return decorator


@_register("extract")
def _run_extract(params: dict) -> dict:
    from backend.kpi_extractor.extractor_pipeline import ingest_document_from_chunks
    return ingest_document_from_chunks(
        document_id=params["document_id"],
        company_id=params["company_id"],
        period=params["period"],
        llm=get_llm_client(),
        registry=get_registry(),
    )


@_register("chat_ask")
def _run_chat_ask(params: dict) -> dict:
    import asyncio
    from backend.chatbot.orchestrator import AgenticAskRequest, run_tool_based_agentic_query
    request = AgenticAskRequest(**params)
    response = asyncio.run(run_tool_based_agentic_query(request, get_registry()))
    return response.model_dump()


def process_job(job_id: str) -> None:
    job = db.get_job(job_id)
    if job is None:
        log.log_error(f"Job worker: job not found: {job_id}")
        return

    db.update_job_status(job_id, "running")
    log.log_info(f"Job {job_id} ({job['job_type']}) started")

    handler = _HANDLERS.get(job["job_type"])
    if handler is None:
        err = f"Unknown job type: {job['job_type']}"
        log.log_error(err)
        db.update_job_status(job_id, "failed", error=err)
        return

    try:
        result = handler(job["params"])
        db.update_job_status(job_id, "done", result=result)
        log.log_info(f"Job {job_id} done")
    except Exception as exc:
        log.log_error(f"Job {job_id} failed: {type(exc).__name__}: {exc}")
        db.update_job_status(job_id, "failed", error=str(exc))
