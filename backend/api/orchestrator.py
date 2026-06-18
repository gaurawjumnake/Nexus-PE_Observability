"""
Analytics Orchestrator Router
=============================
Hybrid text-to-SQL + RAG orchestration for Nexus analytics questions.

The SQL side is intentionally constrained: the request is mapped to a
structured analytics intent, then this module executes parameterized SELECTs
against known tables/columns only. The RAG side reuses the existing Chroma
chat router for document-backed reasoning.
"""

import json
import re
from pathlib import Path
from typing import Any, Optional

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy import text

import backend.kpi_extractor.app.db.db_client as db
from backend.api.chat import ChatQueryRequest, query_chat
from backend.utilites.app_logger import Logger

log = Logger()
router = APIRouter(prefix="/analytics", tags=["analytics"])

KNOWN_TABLES = {
    "kpis": {"id_column": "kpi_id", "value_column": "value"},
    "facts": {"id_column": "fact_id", "value_column": "value"},
}

METRIC_HINTS = {
    "adoption": "adoption_yoy_growth",
    "year on year adoption": "adoption_yoy_growth",
    "yoy adoption": "adoption_yoy_growth",
    "ai revenue": "ai_revenue",
    "revenue": "ai_revenue",
    "roi": "ai_roi",
    "maturity": "ai_maturity_score",
    "governance": "ai_governance_score",
    "spend": "total_ai_spend",
    "budget": "budget_variance",
    "users": "active_ai_users",
}


class AnalyticsAskRequest(BaseModel):
    question: str = Field(..., min_length=1)
    company_id: str
    period: Optional[str] = None
    document_ids: Optional[list[str]] = None
    include_chart: bool = False
    top_k: int = Field(default=6, ge=1, le=20)


class AnalyticsAskResponse(BaseModel):
    answer: str
    route_plan: dict[str, Any]
    analytics: Optional[dict[str, Any]] = None
    reasoning: Optional[dict[str, Any]] = None
    chart: Optional[dict[str, Any]] = None


def _normalize(text_value: str) -> str:
    return re.sub(r"[^a-z0-9]+", " ", text_value.lower()).strip()


def _comparison_from_question(question: str) -> str:
    q = _normalize(question)
    log.log_info(f"Detecting comparison intent from normalized question='{q}'")
    if "year on year" in q or "year over year" in q or "yoy" in q:
        log.log_info("Comparison intent detected: yoy")
        return "yoy"
    if "quarter on quarter" in q or "quarter over quarter" in q or "qoq" in q:
        log.log_info("Comparison intent detected: qoq")
        return "qoq"
    if "month on month" in q or "month over month" in q or "mom" in q:
        log.log_info("Comparison intent detected: mom")
        return "mom"
    if "trend" in q or "over time" in q or "history" in q:
        log.log_info("Comparison intent detected: trend")
        return "trend"
    log.log_info("Comparison intent detected: current")
    return "current"


def _wants_reasoning(question: str, document_ids: Optional[list[str]]) -> bool:
    q = _normalize(question)
    reasoning_terms = {"why", "reason", "reasons", "explain", "driver", "drivers", "behind", "analysis"}
    wants_reasoning = bool(document_ids) and any(term in q.split() for term in reasoning_terms)
    log.log_info(
        f"Reasoning route decision: wants_reasoning={wants_reasoning}, "
        f"document_count={len(document_ids or [])}"
    )
    return wants_reasoning


def _wants_chart(question: str, include_chart: bool) -> bool:
    q = _normalize(question)
    wants_chart = include_chart or any(term in q.split() for term in ["chart", "graph", "plot", "visualize", "visualise"])
    log.log_info(f"Chart route decision: wants_chart={wants_chart}, include_chart={include_chart}")
    return wants_chart


def _load_registry_ids(kind: str) -> list[str]:
    registry_dir = Path("backend/kpi_extractor/registry") / kind
    if not registry_dir.exists():
        log.log_warning(f"Registry directory not found for kind={kind}: {registry_dir}")
        return []
    ids = sorted(path.stem for path in registry_dir.glob("*.yaml"))
    log.log_info(f"Loaded {len(ids)} {kind} id(s) from registry")
    return ids


def _db_ids(table_name: str) -> list[str]:
    info = KNOWN_TABLES[table_name]
    query = text(f"SELECT DISTINCT {info['id_column']} AS metric_id FROM {table_name}")
    log.log_info(f"Loading distinct metric ids from table={table_name}")
    with db.get_cursor() as cur:
        rows = cur.execute(query).fetchall()
        ids = [row.metric_id for row in rows]
        log.log_info(f"Loaded {len(ids)} metric id(s) from table={table_name}")
        return ids


def _available_metric_ids() -> dict[str, list[str]]:
    available = {
        "kpis": sorted(set(_db_ids("kpis") + _load_registry_ids("kpis"))),
        "facts": sorted(set(_db_ids("facts") + _load_registry_ids("facts"))),
    }
    log.log_info(
        f"Available metric universe prepared: "
        f"kpis={len(available['kpis'])}, facts={len(available['facts'])}"
    )
    return available


def _resolve_metric(question: str) -> tuple[str, str]:
    normalized_question = _normalize(question)
    log.log_info(f"Resolving metric from normalized question='{normalized_question}'")
    available = _available_metric_ids()

    candidates = []
    for table_name, metric_ids in available.items():
        for metric_id in metric_ids:
            metric_phrase = metric_id.replace("_", " ")
            if metric_id in question.lower() or metric_phrase in normalized_question:
                candidates.append((table_name, metric_id, len(metric_phrase)))

    log.log_info(f"Direct metric candidates found: {candidates[:10]}")
    if candidates:
        table_name, metric_id, _ = max(candidates, key=lambda item: item[2])
        log.log_info(f"Metric resolved by direct match: table={table_name}, metric_id={metric_id}")
        return table_name, metric_id

    for hint, metric_id in METRIC_HINTS.items():
        if hint in normalized_question:
            log.log_info(f"Metric hint matched: hint='{hint}', candidate_metric_id={metric_id}")
            if metric_id in available["kpis"]:
                log.log_info(f"Metric resolved by hint in kpis: metric_id={metric_id}")
                return "kpis", metric_id
            if metric_id in available["facts"]:
                log.log_info(f"Metric resolved by hint in facts: metric_id={metric_id}")
                return "facts", metric_id

    log.log_warning("Metric resolution failed")
    raise ValueError("Could not identify a KPI or fact from the question")


def _parse_value(value: Any) -> Optional[float | str]:
    if value is None:
        return None
    if isinstance(value, (int, float)):
        return float(value)
    try:
        parsed = json.loads(value)
        if isinstance(parsed, dict):
            for key in ("value", "amount", "score"):
                if key in parsed:
                    return _parse_value(parsed[key])
            return next((_parse_value(v) for v in parsed.values() if _parse_value(v) is not None), None)
        if isinstance(parsed, (int, float, str)):
            return _parse_value(parsed)
    except (TypeError, json.JSONDecodeError):
        pass
    try:
        return float(value)
    except (TypeError, ValueError):
        return str(value)


def _load_export_fallback(company_id: str, metric_id: str, period: Optional[str]) -> list[dict[str, Any]]:
    path = Path("novamind_kpis_export.json")
    if not path.exists():
        log.log_info("KPI export fallback file not found")
        return []
    try:
        rows = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        log.log_warning("KPI export fallback could not be read or parsed")
        return []
    matches = [
        row for row in rows
        if row.get("company_id") == company_id and row.get("kpi_id") == metric_id
    ]
    if period:
        matches = [row for row in matches if row.get("period") == period]
    log.log_info(
        f"KPI export fallback returned {len(matches)} row(s) for "
        f"company_id={company_id}, metric_id={metric_id}, period={period}"
    )
    return matches


def _metric_rows(
    table_name: str,
    metric_id: str,
    company_id: str,
    period: Optional[str],
    limit: int = 12,
) -> tuple[list[dict[str, Any]], str, dict[str, Any]]:
    info = KNOWN_TABLES[table_name]
    id_column = info["id_column"]
    value_column = info["value_column"]

    if period:
        sql = (
            f"SELECT {id_column} AS metric_id, company_id, {value_column} AS value, "
            f"period, timestamp FROM {table_name} "
            f"WHERE company_id=:company_id AND {id_column}=:metric_id AND period=:period "
            f"ORDER BY timestamp DESC LIMIT :limit"
        )
        params = {"company_id": company_id, "metric_id": metric_id, "period": period, "limit": limit}
    else:
        sql = (
            f"SELECT {id_column} AS metric_id, company_id, {value_column} AS value, "
            f"period, timestamp FROM {table_name} "
            f"WHERE company_id=:company_id AND {id_column}=:metric_id "
            f"ORDER BY period DESC, timestamp DESC LIMIT :limit"
        )
        params = {"company_id": company_id, "metric_id": metric_id, "limit": limit}

    log.log_info(
        f"Executing safe analytics SQL: table={table_name}, metric_id={metric_id}, "
        f"company_id={company_id}, period={period}, limit={limit}"
    )
    log.log_debug(f"SQL statement: {sql}")
    log.log_debug(f"SQL params: {params}")
    with db.get_cursor() as cur:
        rows = [dict(row._mapping) for row in cur.execute(text(sql), params).fetchall()]
    log.log_info(f"SQL returned {len(rows)} row(s)")

    if not rows and table_name == "kpis":
        log.log_info("No KPI rows found in DB; trying novamind_kpis_export.json fallback")
        rows = _load_export_fallback(company_id, metric_id, period)

    for row in rows:
        row["value"] = _parse_value(row.get("value"))

    log.log_info(f"Prepared {len(rows)} parsed metric row(s)")
    return rows, sql, params


def _growth(current: Any, previous: Any) -> Optional[float]:
    log.log_info(f"Calculating growth with current={current}, previous={previous}")
    if not isinstance(current, (int, float)) or not isinstance(previous, (int, float)):
        log.log_warning("Growth calculation skipped because one or both values are non-numeric")
        return None
    if previous == 0:
        log.log_warning("Growth calculation skipped because previous value is zero")
        return None
    growth = ((current - previous) / previous) * 100
    log.log_info(f"Growth calculated: {growth}")
    return growth


def _run_sql_agent(question: str, company_id: str, period: Optional[str]) -> dict[str, Any]:
    log.log_info("SQL analytics agent started")
    comparison = _comparison_from_question(question)
    table_name, metric_id = _resolve_metric(question)
    log.log_info(
        f"SQL analytics intent: table={table_name}, metric_id={metric_id}, "
        f"comparison={comparison}, company_id={company_id}, period={period}"
    )
    rows, sql, params = _metric_rows(table_name, metric_id, company_id, period)

    if not rows:
        log.log_warning(
            f"SQL analytics found no data for table={table_name}, metric_id={metric_id}, "
            f"company_id={company_id}, period={period}"
        )
        return {
            "status": "no_data",
            "table": table_name,
            "metric_id": metric_id,
            "comparison": comparison,
            "executed_sql": sql,
            "sql_params": params,
            "message": "No matching rows found in nexus.db",
        }

    series = [
        {"period": row.get("period"), "value": row.get("value")}
        for row in sorted(rows, key=lambda item: str(item.get("period") or ""))
    ]
    log.log_info(f"Analytics series prepared with {len(series)} point(s): {series}")
    current = rows[0]
    result = {
        "status": "ok",
        "table": table_name,
        "metric_id": metric_id,
        "comparison": comparison,
        "current": {"period": current.get("period"), "value": current.get("value")},
        "series": series,
        "executed_sql": sql,
        "sql_params": params,
    }

    if comparison in {"yoy", "qoq", "mom"}:
        if metric_id.endswith(f"_{comparison}_growth") or metric_id.endswith(f"{comparison}_growth"):
            result["growth_percent"] = current.get("value")
            result["growth_source"] = "stored_kpi"
            log.log_info(
                f"Using stored growth KPI for {metric_id}: "
                f"growth_percent={result['growth_percent']}"
            )
        elif len(series) >= 2:
            previous = series[-2]
            result["previous"] = previous
            result["growth_percent"] = _growth(series[-1]["value"], previous["value"])
            result["growth_source"] = "calculated_from_history"
            log.log_info(
                f"Calculated growth from history: previous={previous}, "
                f"current={series[-1]}, growth_percent={result['growth_percent']}"
            )
        else:
            result["status"] = "insufficient_history"
            result["message"] = f"{comparison.upper()} needs at least two comparable periods"
            log.log_warning(result["message"])

    log.log_info(f"SQL analytics agent completed with result status={result.get('status')}")
    return result


def _build_chart(analytics: Optional[dict[str, Any]]) -> Optional[dict[str, Any]]:
    if not analytics or not analytics.get("series"):
        log.log_warning("Chart build skipped because analytics series is missing")
        return None
    series = analytics["series"]
    chart = {
        "type": "line" if len(series) > 2 else "bar",
        "title": f"{analytics['metric_id']} by period",
        "x_axis": "period",
        "y_axis": analytics["metric_id"],
        "data": series,
    }
    log.log_info(f"Chart payload built: type={chart['type']}, points={len(series)}")
    return chart


def _compose_answer(analytics: Optional[dict[str, Any]], reasoning: Optional[dict[str, Any]]) -> str:
    log.log_info(
        f"Composing final answer: has_analytics={bool(analytics)}, "
        f"has_reasoning={bool(reasoning)}"
    )
    parts = []

    if analytics:
        metric = analytics.get("metric_id")
        comparison = analytics.get("comparison")
        current = analytics.get("current") or {}
        status = analytics.get("status")

        if status == "ok" and analytics.get("growth_percent") is not None:
            parts.append(
                f"{metric} {comparison.upper()} growth is {analytics['growth_percent']:.2f}% "
                f"for period {current.get('period')}."
            )
        elif status == "ok":
            parts.append(f"{metric} is {current.get('value')} for period {current.get('period')}.")
        else:
            parts.append(analytics.get("message") or f"Could not calculate {metric}.")

    if reasoning and reasoning.get("answer"):
        parts.append(f"Reasoning from documents: {reasoning['answer']}")

    answer = "\n\n".join(parts) if parts else "I could not produce an analytics answer from the available data."
    log.log_info(f"Final answer composed, chars={len(answer)}")
    return answer


@router.post("/ask", response_model=AnalyticsAskResponse)
async def ask_analytics(request: AnalyticsAskRequest):
    log.log_info(
        f"Analytics orchestrator started for company_id={request.company_id}, "
        f"period={request.period}, question={request.question}"
    )
    try:
        needs_reasoning = _wants_reasoning(request.question, request.document_ids)
        needs_chart = _wants_chart(request.question, request.include_chart)
        route_plan = {
            "sql_agent": True,
            "rag_agent": needs_reasoning,
            "chart_agent": needs_chart,
        }
        log.log_info(f"Route plan selected: {route_plan}")

        analytics = _run_sql_agent(request.question, request.company_id, request.period)
        log.log_info(
            f"SQL analytics completed with metric_id={analytics.get('metric_id')}, "
            f"status={analytics.get('status')}"
        )

        reasoning = None
        if needs_reasoning:
            log.log_info("Invoking RAG reasoning agent")
            rag_response = await query_chat(ChatQueryRequest(
                question=f"Explain the business reasoning behind this analytics result: {request.question}",
                company_id=request.company_id,
                document_ids=request.document_ids,
                top_k=request.top_k,
            ))
            reasoning = {
                "answer": rag_response.answer,
                "citations": [citation.model_dump() for citation in rag_response.citations],
            }
            log.log_info(f"RAG reasoning completed with {len(reasoning['citations'])} citation(s)")
        else:
            log.log_info("RAG reasoning agent skipped")

        chart = _build_chart(analytics) if needs_chart else None
        if not needs_chart:
            log.log_info("Chart agent skipped")
        answer = _compose_answer(analytics, reasoning)
        log.log_info("Analytics orchestrator completed successfully")

        return AnalyticsAskResponse(
            answer=answer,
            route_plan=route_plan,
            analytics=analytics,
            reasoning=reasoning,
            chart=chart,
        )
    except ValueError as e:
        log.log_warning(f"Analytics orchestrator could not resolve request: {e}")
        raise HTTPException(status_code=422, detail=str(e))
    except Exception as e:
        log.log_error(f"Analytics orchestrator failed: {type(e).__name__}: {e}")
        raise HTTPException(status_code=500, detail=f"Analytics orchestrator failed: {e}")
