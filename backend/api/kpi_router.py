from typing import Optional

from fastapi import APIRouter, Request, HTTPException
from pydantic import BaseModel

from backend.kpi_extractor.extractor_pipeline import (
    ingest_document_from_chunks,
    calculate_kpis,
    calculate_kpi_trends,
    get_insights,
)
import backend.db.db_client as db

router = APIRouter(prefix="/kpis", tags=["kpis"])


class ExtractRequest(BaseModel):
    document_id: str
    company_id: str
    period: str


class CalculateKPIRequest(BaseModel):
    company_id: str
    period: str
    kpi_ids: Optional[list[str]] = None


class InsightRequest(BaseModel):
    question: str
    company_id: str
    period: str
    kpi_ids: list[str]


class TrendRequest(BaseModel):
    company_id: str
    kpi_ids: list[str]
    period_type: str          # 'month' | 'quarter' | 'year'
    start_period: str         # e.g. '2025-Q1' for period_type='quarter'
    end_period: str           # e.g. '2025-Q4'
    save_results: bool = False  # persist each period's result via /calculate's storage path


@router.post("/extract")
async def extract_facts(request: Request, body: ExtractRequest):
    """
    Stages 3-7: classify document, extract + validate facts from
    chunks already persisted by the document router, save to fact store.
    """
    chunks = db.get_chunks(body.document_id)
    if not chunks:
        raise HTTPException(status_code=404, detail="Document not found or has no chunks")

    llm = request.app.state.llm
    registry = request.app.state.registry

    result = ingest_document_from_chunks(
        document_id=body.document_id,
        company_id=body.company_id,
        period=body.period,
        llm=llm,
        registry=registry,
    )
    return result


@router.post("/calculate")
async def calculate(request: Request, body: CalculateKPIRequest):
    """Stages 8-10: read facts, compute KPIs, persist results."""
    registry = request.app.state.registry
    results = calculate_kpis(
        company_id=body.company_id,
        period=body.period,
        registry=registry,
        kpi_ids=body.kpi_ids,
    )
    return {"company_id": body.company_id, "period": body.period, "results": results}


@router.get("/{company_id}/{period}")
async def list_kpis_company_wise(company_id: str, period: str):
    kpis = db.get_kpis_for_company(company_id, period)
    return {"company_id": company_id, "period": period, "kpis": kpis}


@router.get("/")
async def list_all_kpis():
    kpis = db.get_kpis()
    return {"kpis": kpis}


@router.post("/trend")
async def trend(request: Request, body: TrendRequest):
    """
    MoM / QoQ / YoY for one or more KPIs: the same formula evaluated once
    per period between start_period and end_period, each period pulling
    its own facts independently (a period with no underlying data comes
    back insufficient_data rather than reusing a neighboring period's
    value or fabricating one).

    period_type='month'   -> start_period/end_period like '2025-01'
    period_type='quarter' -> start_period/end_period like '2025-Q1'
    period_type='year'    -> start_period/end_period like '2025'

    By default this does NOT persist results (read-only, for charting) -
    pass save_results=true to also write each period's value via the
    same storage path /calculate uses.
    """
    registry = request.app.state.registry
    try:
        results = calculate_kpi_trends(
            kpi_ids=body.kpi_ids,
            company_id=body.company_id,
            period_type=body.period_type,
            start_period=body.start_period,
            end_period=body.end_period,
            registry=registry,
            save_results=body.save_results,
        )
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))

    return {
        "company_id": body.company_id,
        "period_type": body.period_type,
        "start_period": body.start_period,
        "end_period": body.end_period,
        "results": results,
    }


@router.post("/insights")
async def insights(request: Request, body: InsightRequest):
    """Stage 11: KPIs + facts -> executive analysis."""
    llm = request.app.state.llm
    registry = request.app.state.registry
    report = get_insights(
        question=body.question,
        company_id=body.company_id,
        period=body.period,
        kpi_ids=body.kpi_ids,
        llm=llm,
        registry=registry,
    )
    return {"company_id": body.company_id, "period": body.period, "report": report}