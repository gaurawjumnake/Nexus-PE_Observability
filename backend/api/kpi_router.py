from typing import Any, Optional

from fastapi import APIRouter, Request, HTTPException
from pydantic import BaseModel

from backend.kpi_extractor.extractor_pipeline import (
    ingest_document_from_chunks,
    calculate_kpis,
    calculate_kpi_trends,
    get_insights,
)
from backend.kpi_extractor.app.core.registry_writer import add_fact, add_kpi
from backend.utilites.app_logger import Logger
import backend.db.db_client as db

log = Logger()
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
    log.log_info(f"Fact extraction started: document_id={body.document_id} company_id={body.company_id} period={body.period}")
    chunks = db.get_chunks(body.document_id)
    if not chunks:
        log.log_warning(f"Fact extraction: no chunks found for document_id={body.document_id}")
        raise HTTPException(status_code=404, detail="Document not found or has no chunks")

    llm = request.app.state.llm
    registry = request.app.state.registry

    try:
        result = ingest_document_from_chunks(
            document_id=body.document_id,
            company_id=body.company_id,
            period=body.period,
            llm=llm,
            registry=registry,
        )
    except Exception as exc:
        log.log_error(f"Fact extraction failed: document_id={body.document_id}: {type(exc).__name__}: {exc}")
        raise HTTPException(status_code=500, detail=f"Fact extraction failed: {exc}")
    log.log_info(f"Fact extraction complete: document_id={body.document_id} facts_validated={result.get('facts_validated', 0)}")
    return result


@router.post("/calculate")
async def calculate(request: Request, body: CalculateKPIRequest):
    """Stages 8-10: read facts, compute KPIs, persist results."""
    log.log_info(f"KPI calculation started: company_id={body.company_id} period={body.period} kpi_ids={body.kpi_ids}")
    registry = request.app.state.registry
    try:
        results = calculate_kpis(
            company_id=body.company_id,
            period=body.period,
            registry=registry,
            kpi_ids=body.kpi_ids,
        )
    except Exception as exc:
        log.log_error(f"KPI calculation failed: company_id={body.company_id} period={body.period}: {type(exc).__name__}: {exc}")
        raise HTTPException(status_code=500, detail=f"KPI calculation failed: {exc}")
    log.log_info(f"KPI calculation complete: company_id={body.company_id} period={body.period} count={len(results)}")
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
    log.log_info(f"Insights requested: company_id={body.company_id} period={body.period} kpi_ids={body.kpi_ids}")
    llm = request.app.state.llm
    registry = request.app.state.registry
    try:
        report = get_insights(
            question=body.question,
            company_id=body.company_id,
            period=body.period,
            kpi_ids=body.kpi_ids,
            llm=llm,
            registry=registry,
        )
    except Exception as exc:
        log.log_error(f"Insights generation failed: company_id={body.company_id}: {type(exc).__name__}: {exc}")
        raise HTTPException(status_code=500, detail=f"Insights generation failed: {exc}")
    log.log_info(f"Insights complete: company_id={body.company_id} period={body.period}")
    return {"company_id": body.company_id, "period": body.period, "report": report}


# ---------------------------------------------------------------------------
# Registry management – create new facts and KPIs
# ---------------------------------------------------------------------------

class ConfidenceRulesModel(BaseModel):
    minimum_confidence: float = 0.8
    auto_approve: float = 0.95
    manual_review_below: float = 0.8


class AddFactRequest(BaseModel):
    fact_id: str
    name: str
    category: str
    description: str
    data_type: str                              # string | number | currency | percentage | boolean | date
    business_definition: str = ""
    unit: str = ""
    fact_type: str = "raw"                      # raw | derived | composite
    source_priority: list[str] = []
    document_sources: list[str] = []
    possible_aliases: list[str] = []
    extraction_patterns: list[str] = []
    validation_rules: Optional[dict[str, Any]] = None
    normalization_rules: dict[str, Any] = {}
    aggregation_strategy: str = "latest_value"
    related_facts: list[str] = []
    used_by_kpis: list[str] = []
    example_values: list[str] = []
    confidence_rules: ConfidenceRulesModel = ConfidenceRulesModel()
    missing_value_strategy: str = "mark_unavailable"
    status: str = "active"
    overwrite: bool = False


class BenchmarkingModel(BaseModel):
    enabled: bool = False
    benchmark_type: Optional[str] = None
    benchmark_source: Optional[str] = None


class ThresholdsModel(BaseModel):
    excellent: Optional[float] = None
    good: Optional[float] = None
    warning: Optional[float] = None
    critical: Optional[float] = None


class DataQualityModel(BaseModel):
    minimum_coverage: float = 80.0
    confidence_threshold: float = 0.8


class AddKPIRequest(BaseModel):
    kpi_id: str
    name: str
    category: str
    description: str
    formula: str
    tier: str = "operational"                   # operational | strategic | portfolio
    business_value: str = ""
    unit: str = "count"
    aggregation: str = "sum"
    frequency: str = "monthly"                  # daily | weekly | monthly | quarterly | yearly
    required_facts: list[str] = []
    derived_facts: list[str] = []
    required_documents: list[str] = []
    dependencies: list[str] = []
    benchmarking: BenchmarkingModel = BenchmarkingModel()
    thresholds: Optional[ThresholdsModel] = None
    data_quality: DataQualityModel = DataQualityModel()
    missing_data_strategy: str = "use_last_known"
    owner_persona: list[str] = []
    dashboard_visibility: list[str] = []
    example_calculation: str = ""
    status: str = "active"
    overwrite: bool = False


@router.post("/registry/facts")
async def create_fact(request: Request, body: AddFactRequest):
    """
    Create a new fact: writes registry/facts/<fact_id>.yaml and syncs SQLite.
    Pass overwrite=true to replace an existing fact with the same fact_id.
    """
    registry = request.app.state.registry
    fact_data = body.model_dump(exclude={"overwrite"})
    fact_data["confidence_rules"] = body.confidence_rules.model_dump()
    try:
        result = add_fact(registry, fact_data, overwrite=body.overwrite)
    except FileExistsError as e:
        raise HTTPException(status_code=409, detail=str(e))
    return result


@router.post("/registry/kpis")
async def create_kpi(request: Request, body: AddKPIRequest):
    """
    Create a new KPI: writes registry/kpis/<kpi_id>.yaml and syncs SQLite.
    All facts listed in required_facts must already exist in the registry.
    Pass overwrite=true to replace an existing KPI with the same kpi_id.
    """
    registry = request.app.state.registry
    kpi_data = body.model_dump(exclude={"overwrite"})
    kpi_data["benchmarking"] = body.benchmarking.model_dump()
    kpi_data["data_quality"] = body.data_quality.model_dump()
    if body.thresholds:
        kpi_data["thresholds"] = body.thresholds.model_dump()
    try:
        result = add_kpi(registry, kpi_data, overwrite=body.overwrite)
    except FileExistsError as e:
        raise HTTPException(status_code=409, detail=str(e))
    except ValueError as e:
        raise HTTPException(status_code=422, detail=str(e))
    return result