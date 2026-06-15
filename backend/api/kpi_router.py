"""
KPI Router
==========
Stages 3-11: reads persisted chunks/facts from the DB, runs fact
extraction, calculates KPIs, and generates insights. Uses the shared
llm/registry singletons from app.state - never instantiates a new
RegistryMCPClient per request.
"""
from typing import Optional

from fastapi import APIRouter, Request, HTTPException
from pydantic import BaseModel

from backend.kpi_extractor.extractor_pipeline import (
    ingest_document_from_chunks,
    calculate_kpis,
    get_insights,
)
import backend.kpi_extractor.app.db.db_client as db

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
async def list_kpis(company_id: str, period: str):
    kpis = db.get_kpis_for_company(company_id, period)
    if not kpis:
        raise HTTPException(status_code=404, detail="No KPIs found for company/period")
    return {"company_id": company_id, "period": period, "kpis": kpis}


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