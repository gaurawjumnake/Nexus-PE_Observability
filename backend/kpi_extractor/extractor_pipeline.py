from typing import Optional, Any

from backend.utilites.llm_models import BaseLLMClient
from backend.kpi_extractor.app.core.chunking import chunk_markdown
from backend.kpi_extractor.app.registry.registry_service import RegistryService
from backend.kpi_extractor.app.agents.document_classifier import DocumentClassifierAgent
from backend.kpi_extractor.app.agents.fact_extraction_agent import FactExtractionAgent
from backend.kpi_extractor.app.agents.insight_agent import InsightAgent
from backend.kpi_extractor.app.engine.fact_validation_engine import validate_extractions, resolve_source_conflicts
from backend.kpi_extractor.app.engine.kpi_calculation_engine import KPICalculationEngine
from backend.kpi_extractor.app.engine import tabular_extraction
from backend.kpi_extractor.app.engine import fact_aggregation
import backend.db.db_client as db
from crewai import Agent, Task, Crew, Process
from backend.utilites.app_logger import Logger

log = Logger()

# ------------------------------------------------------------------
# Stage 1-6: Document → Validated Facts
# ------------------------------------------------------------------
def ingest_document(
    markdown_text: str,
    file_name: str,
    company_id: str,
    period: str,
    llm: BaseLLMClient,
    registry: RegistryService,
) -> dict:
    """
    Full ingestion pipeline for one document.

    Args:
        markdown_text : LlamaParse output
        file_name     : original filename (for lineage)
        company_id    : portfolio company identifier
        period        : reporting period, e.g. '2026-Q2'
        llm           : LLM client instance
        registry      : active RegistryService

    Returns:
        {"document_id", "document_type", "facts_extracted", "facts_validated"}
    """
    # Stage 1: save document record, chunk markdown
    document_id = db.save_document(company_id, file_name)
    chunks = chunk_markdown(markdown_text, document_id)
    db.save_chunks(chunks)

    # Stage 3: Document Classification Agent
    classifier = DocumentClassifierAgent(llm, registry)
    classification = classifier.run(chunks)
    document_type = classification["document_type"]
    db.set_document_type(document_id, document_type)

    # Stage 4+5: Fact Extraction Agent
    extractor = FactExtractionAgent(llm, registry)
    raw_extractions = extractor.run(document_type, chunks)

    # Stage 6: Deterministic Fact Validation Engine
    validated = validate_extractions(raw_extractions, document_type, registry)
    resolved = resolve_source_conflicts(validated, registry)

    # Stage 7: Persist to Fact Store
    for fact in resolved:
        db.save_fact(
            fact_id=fact["fact_id"],
            company_id=company_id,
            value=fact["value"],
            confidence=fact["confidence"],
            source_document=fact["document_id"],
            source_chunk=fact["chunk_id"],
            source_type=fact["source_type"],
            period=period,
        )

    return {
        "document_id":      document_id,
        "document_type":    document_type,
        "facts_extracted":  len(raw_extractions),
        "facts_validated":  len(resolved),
    }

 # # Additional method -----------------------------------------------------
def ingest_document_from_chunks(
    document_id: str,
    company_id: str,
    period: str,
    llm: BaseLLMClient,
    registry: RegistryService,
) -> dict:
    """
    Stages 3-7 only - for use when chunks were already persisted by the
    document-parser router (Stage 1 done at upload time).
 
    Loads chunks for `document_id` from the DB and runs classification,
    extraction, validation, and fact persistence.
    """
    rows = db.get_chunks(document_id)
    chunks = [
        {
            "chunk_id": r["chunk_id"],
            "document_id": r["document_id"],
            "chunk_text": r["chunk_text"],
            "metadata": {
                "chunk_index": r["chunk_index"],
                "section": r["section_title"],
                "page_number": r["page_number"],
            },
        }
        for r in rows
    ]
 
    # Stage 3: Document Classification Agent
    classifier = DocumentClassifierAgent(llm, registry)
    classification = classifier.run(chunks)
    document_type = classification["document_type"]
    db.set_document_type(document_id, document_type)
 
    # Stage 4+5: Fact Extraction Agent
    extractor = FactExtractionAgent(llm, registry)
    raw_extractions = extractor.run(document_type, chunks)
 
    # Stage 6: Deterministic Fact Validation Engine
    validated = validate_extractions(raw_extractions, document_type, registry)
    resolved = resolve_source_conflicts(validated, registry)
 
    # Stage 7: Persist to Fact Store
    for fact in resolved:
        db.save_fact(
            fact_id=fact["fact_id"],
            company_id=company_id,
            value=fact["value"],
            confidence=fact["confidence"],
            source_document=fact["document_id"],
            source_chunk=fact["chunk_id"],
            source_type=fact["source_type"],
            period=period,
        )
 
    return {
        "document_id":      document_id,
        "document_type":    document_type,
        "facts_extracted":  len(raw_extractions),
        "facts_validated":  len(resolved),
    }

# ------------------------------------------------------------------
# Stage 4+5+6+7, tabular counterpart: Structured Tables -> Facts/Observations
# ------------------------------------------------------------------
def ingest_structured_tables(
    parsed_output: dict,
    company_id: str,
    period: str,
    registry: RegistryService,
    document_type: Optional[str] = None,
    file_name: Optional[str] = None,
    document_id: Optional[str] = None,
) -> dict:
    """
    Deterministic counterpart to ingest_document_from_chunks() for the
    structured output of DoclingDocumentParser.extract_structured_output()
    - no LLM call anywhere in this path.

        parsed_output["time_series_tables"] -> fact_observations
            (dated rows, e.g. years of daily telemetry - aggregated into
            KPI periods later via app.engine.fact_aggregation)
        parsed_output["structured_tables"] -> facts
            (flat rows with no date, e.g. a one-row-per-company KPI dump
            - point-in-time, same storage as narrative-extracted facts)

    document_id: pass this when a document record already exists for
    this upload (e.g. the caller already ran the narrative path on the
    same file and got a document_id back) - avoids creating a second,
    orphaned document row for what is the same uploaded file. If
    omitted, a new document record is created (e.g. for a standalone
    csv/xlsx upload with no narrative pass at all).

    document_type: pass this when the table came from an already-
    classified document (e.g. an appendix table inside a narrative PDF
    that DocumentClassifierAgent already classified). If omitted - the
    case for a standalone csv/xlsx/json upload with no narrative for the
    LLM classifier to read - falls back to
    tabular_extraction.classify_document_type_from_columns() using the
    first table's columns. If that can't determine a type either, the
    whole call is skipped (status="skipped") rather than guessing.

    Returns a summary dict including any column headers that didn't map
    to a fact, so you can see at a glance what an upload's columns
    didn't capture.
    """
    if document_id is None:
        document_id = db.save_document(company_id, file_name or parsed_output.get("filename", "uploaded_table"))

    all_tables = parsed_output.get("time_series_tables", []) + parsed_output.get("structured_tables", [])

    if document_type is None:
        sample_columns = []
        if all_tables:
            sample_columns = all_tables[0].get("value_columns") or all_tables[0].get("columns") or []
        document_type = tabular_extraction.classify_document_type_from_columns(sample_columns, registry)
        if document_type is None:
            return {
                "document_id": document_id,
                "document_type": None,
                "status": "skipped",
                "reason": "Could not determine document_type from table columns and none was provided.",
                "observations_saved": 0,
                "facts_saved": 0,
                "unmatched_columns": [],
            }

    db.set_document_type(document_id, document_type)

    observations_saved = 0
    facts_saved = 0
    unmatched_all: list[str] = []

    ts_tables = parsed_output.get("time_series_tables", [])
    st_tables = parsed_output.get("structured_tables", [])
    log.log_info(
        f"ingest_structured_tables: document_id={document_id}, "
        f"type={document_type}, ts_tables={len(ts_tables)}, structured_tables={len(st_tables)}"
    )

    for i, table in enumerate(ts_tables):
        log.log_info(f"Processing time_series_table {i+1}/{len(ts_tables)}")
        extractions, unmatched = tabular_extraction.extract_observations_from_time_series_table(
            table, document_type, registry, document_id=document_id,
        )
        unmatched_all.extend(unmatched)

        validated = validate_extractions(extractions, document_type, registry)
        rows = [{
            "fact_id": v["fact_id"],
            "company_id": company_id,
            "observation_date": v["observation_date"],
            "value": v["value"],
            "confidence": v["confidence"],
            "source_document": document_id,
            "source_type": document_type,
        } for v in validated]
        log.log_info(f"Saving {len(rows)} observations for time_series_table {i+1}")
        observations_saved += db.save_observations_bulk(rows)

    for i, table in enumerate(st_tables):
        log.log_info(f"Processing structured_table {i+1}/{len(st_tables)}")
        extractions, unmatched = tabular_extraction.extract_facts_from_structured_table(
            table, document_type, registry, document_id=document_id,
        )
        unmatched_all.extend(unmatched)

        validated = validate_extractions(extractions, document_type, registry)
        resolved = resolve_source_conflicts(validated, registry)
        for fact in resolved:
            db.save_fact(
                fact_id=fact["fact_id"],
                company_id=company_id,
                value=fact["value"],
                confidence=fact["confidence"],
                source_document=document_id,
                source_chunk=None,
                source_type=document_type,
                period=period,
            )
            facts_saved += 1

    log.log_info(
        f"ingest_structured_tables done: observations={observations_saved}, facts={facts_saved}"
    )
    return {
        "document_id": document_id,
        "document_type": document_type,
        "status": "ingested",
        "observations_saved": observations_saved,
        "facts_saved": facts_saved,
        "unmatched_columns": sorted(set(unmatched_all)),
    }


# ------------------------------------------------------------------
# Stage 8-10: Facts → KPIs
# ------------------------------------------------------------------
def calculate_kpis(
    company_id: str,
    period: str,
    registry: RegistryService,
    kpi_ids: Optional[list[str]] = None,
) -> list[dict]:
    """
    Runs KPI Calculation Engine for a company/period.

    Facts are read via fact_aggregation.get_facts_for_company_period,
    NOT db.get_facts_for_company directly - this is what makes a fact
    that arrived as a dated time series (fact_observations) actually
    count toward KPI coverage, aggregated into this period using its own
    aggregation_strategy. Point-in-time facts (the `facts` table) are
    still included unchanged.
    """
    engine = KPICalculationEngine(registry)
    return engine.calculate_all(
        company_id=company_id,
        period=period,
        get_facts_fn=lambda cid, p: fact_aggregation.get_facts_for_company_period(registry, cid, p),
        save_kpi_fn=db.save_kpi,
        kpi_ids=kpi_ids,
    )


def calculate_kpis_for_periods(
    company_id: str,
    periods: list[str],
    registry: RegistryService,
    kpi_ids: Optional[list[str]] = None,
) -> dict[str, list[dict]]:
    """
    Runs calculate_kpis once per period in `periods` (e.g. every distinct
    year/quarter actually covered by an upload's time-series tables, from
    fact_aggregation.derive_periods_from_tables) instead of trusting one
    caller-supplied period that may not match the data at all.

    Returns {period: [kpi_result, ...]}. An empty `periods` list returns
    {} - callers should fall back to a single explicit period (e.g. the
    one typed into the upload form) when there's no date-bearing table
    to derive periods from.
    """
    return {p: calculate_kpis(company_id=company_id, period=p, registry=registry, kpi_ids=kpi_ids) for p in periods}


def calculate_kpi_trend(
    kpi_id: str,
    company_id: str,
    period_type: str,
    start_period: str,
    end_period: str,
    registry: RegistryService,
    save_results: bool = False,
) -> list[dict]:
    """
    MoM / QoQ / YoY for one KPI: same formula, evaluated once per period
    bucket between start_period and end_period (inclusive), each pulling
    its own facts independently via fact_aggregation - a period with no
    underlying data comes back insufficient_data rather than reusing a
    neighboring period's value.

    period_type: 'month' | 'quarter' | 'year'
    start_period/end_period: in that granularity's format, e.g.
        period_type='quarter' -> start_period='2025-Q1', end_period='2025-Q4'

    save_results: if True, persists each period's result via db.save_kpi
    (same as calculate_kpis does) - off by default since a trend is
    often requested read-only for a dashboard chart.
    """
    periods = fact_aggregation.enumerate_periods(period_type, start_period, end_period)
    engine = KPICalculationEngine(registry)
    return engine.calculate_trend(
        kpi_id=kpi_id,
        company_id=company_id,
        periods=periods,
        get_facts_fn=lambda cid, p: fact_aggregation.get_facts_for_company_period(registry, cid, p),
        save_kpi_fn=db.save_kpi if save_results else None,
    )


def calculate_kpi_trends(
    kpi_ids: list[str],
    company_id: str,
    period_type: str,
    start_period: str,
    end_period: str,
    registry: RegistryService,
    save_results: bool = False,
) -> dict[str, list[dict]]:
    """
    Like calculate_kpi_trend, but for several KPIs at once - e.g. a
    dashboard chart with multiple lines. Computes each period's facts
    ONCE via fact_aggregation and reuses that across every KPI, instead
    of redundantly re-aggregating the same underlying facts once per
    KPI per period.

    Returns {kpi_id: [period_result, ...]}, one entry per kpi_id, each a
    list in the same order as the resolved periods.
    """
    periods = fact_aggregation.enumerate_periods(period_type, start_period, end_period)
    engine = KPICalculationEngine(registry)

    facts_by_period = {
        period: fact_aggregation.get_facts_for_company_period(registry, company_id, period)
        for period in periods
    }

    def get_facts_fn(cid, period):
        return facts_by_period[period]

    return {
        kpi_id: engine.calculate_trend(
            kpi_id=kpi_id,
            company_id=company_id,
            periods=periods,
            get_facts_fn=get_facts_fn,
            save_kpi_fn=db.save_kpi if save_results else None,
        )
        for kpi_id in kpi_ids
    }


# ------------------------------------------------------------------
# Stage 11: KPIs + Facts → Insight
# ------------------------------------------------------------------
def get_insights(
    question: str,
    company_id: str,
    period: str,
    kpi_ids: list[str],
    llm: BaseLLMClient,
    registry: RegistryService,
) -> str:
    """
    Runs the Insight Agent. Loads KPI + fact data ,
    fetches targeted registry context via MCP, returns analysis.
    """
    # Load KPI records (with history)
    kpi_records = []
    for kid in kpi_ids:
        record = db.get_kpi(kid, company_id, period) or {}
        record["kpi_id"] = kid
        record["company_id"] = company_id
        record["history"] = db.get_kpi_history(kid, company_id)
        kpi_records.append(record)

    # Load underlying fact values (aggregation-aware: includes time-series
    # facts aggregated into this period, not just point-in-time facts)
    all_facts = fact_aggregation.get_facts_for_company_period(registry, company_id, period)
    fact_records = [
        {"fact_id": fid, "value": val,
         "confidence": None, "source_type": None, "company_id": company_id}
        for fid, val in all_facts.items()
    ]

    # Build coverage records from KPI engine output
    coverage_records = []
    for kid in kpi_ids:
        ctx = registry.retrieve_formula_context(kid)
        if ctx is None:
            continue
        required = list(ctx["facts"].keys())
        available = [f for f in required if f in all_facts]
        missing = [f for f in required if f not in all_facts]
        coverage_records.append({
            "kpi_id": kid,
            "required_facts": required,
            "available_facts": available,
            "missing_facts": missing,
            "coverage": len(available) / len(required) if required else 1.0,
            "ready_to_calculate": len(missing) == 0,
        })

    agent = InsightAgent(llm, registry)
    return agent.run(question, kpi_records, fact_records, coverage_records, kpi_ids)


def build_crew(llm: Optional[BaseLLMClient] = None, registry: Optional[RegistryService] = None):
    """
    Returns a configured CrewAI Crew.

    Install: pip install crewai
    Usage:
        registry = RegistryService()
        crew = build_crew(get_llm_client(), registry)
        crew.kickoff(inputs={"markdown": "...", "company_id": "...", ...})
    """
    llm = llm

    # -- Agent definitions --
    classifier_agent = Agent(
        role="Document Classifier",
        goal="Determine the type of each uploaded document",
        backstory=(
            "You are an expert at categorising financial and operational "
            "documents for Private Equity firms."
        ),
        verbose=False,
        allow_delegation=False,
    )

    extractor_agent = Agent(
        role="Fact Extractor",
        goal="Extract structured fact values from classified documents",
        backstory=(
            "You are a precise data extraction specialist trained to find "
            "specific financial and operational metrics in business documents."
        ),
        verbose=False,
        allow_delegation=False,
    )

    analyst_agent = Agent(
        role="AI Investment Analyst",
        goal="Produce actionable insights from portfolio AI performance data",
        backstory=(
            "You are a Senior AI Investment Analyst who translates raw KPI data "
            "into clear strategic recommendations for PE partners."
        ),
        verbose=True,
        allow_delegation=False,
    )

    # -- Task definitions --
    classify_task = Task(
        description=(
            "Classify the uploaded document into one supported document type. "
            "Use the document excerpt provided in the context."
        ),
        expected_output='JSON: {"document_type": str, "confidence": float}',
        agent=classifier_agent,
    )

    extract_task = Task(
        description=(
            "Using the classified document type, discover relevant facts via "
            "the registry and extract all fact values from the document chunks."
        ),
        expected_output="List of extracted fact dicts with value and confidence",
        agent=extractor_agent,
        context=[classify_task],
    )

    insight_task = Task(
        description=(
            "Given the extracted and validated facts, computed KPIs, and the "
            "analyst question, produce a structured PE-grade insight report."
        ),
        expected_output="Structured markdown insight report",
        agent=analyst_agent,
        context=[extract_task],
    )

    return Crew(
        agents=[classifier_agent, extractor_agent, analyst_agent],
        tasks=[classify_task, extract_task, insight_task],
        process=Process.sequential,
        verbose=False,
    )


def test_extractor(
    file_path: str,
    company_id: str,
    period: str,
    kpi_ids: Optional[list[str]] = None,
) -> dict:

    import json
    from pathlib import Path
    from backend.document_parser.docling_document_parser import DoclingDocumentParser

    registry = RegistryService()
    parser = DoclingDocumentParser()
    filename = Path(file_path).name

    log.log_info(f"[diagnostic] parsing {file_path}")
    parsed = parser.extract_structured_output(modified_name=Path(file_path).stem, file_path=file_path)

    ts_tables = parsed.get("time_series_tables", [])
    st_tables = parsed.get("structured_tables", [])
    narrative = parsed.get("narrative_markdown", "")

    # Pandas fallback: Docling can't parse xlsx files that aren't valid zip archives.
    if not ts_tables and not st_tables and not narrative.strip():
        suffix = Path(file_path).suffix.lower()
        if suffix in (".xlsx", ".xls", ".csv"):
            from backend.document_parser.data_ingestion import pandas_parse_tabular
            log.log_info(f"[diagnostic] Docling produced no output; using pandas fallback")
            parsed = pandas_parse_tabular(Path(file_path), suffix, filename)
            ts_tables = parsed.get("time_series_tables", [])
            st_tables = parsed.get("structured_tables", [])
            narrative = ""

    log.log_info(
        f"[diagnostic] parsed: ts_tables={len(ts_tables)}, "
        f"structured_tables={len(st_tables)}, narrative_chars={len(narrative)}"
    )

    ingest_result: dict = {}

    if ts_tables or st_tables:
        ingest_result = ingest_structured_tables(
            parsed,
            company_id=company_id,
            period=period,
            registry=registry,
            file_name=filename,
        )
        log.log_info(f"[diagnostic] tabular ingest: {ingest_result}")
    elif narrative.strip():
        from backend.utilites.llm_models import get_llm_client
        llm = get_llm_client()
        from backend.kpi_extractor.app.core.chunking import chunk_markdown
        doc_id = db.save_document(company_id, filename)
        chunks = chunk_markdown(narrative, doc_id)
        db.save_chunks(chunks)
        ingest_result = ingest_document_from_chunks(
            document_id=doc_id,
            company_id=company_id,
            period=period,
            llm=llm,
            registry=registry,
        )
        log.log_info(f"[diagnostic] narrative ingest: {ingest_result}")
    else:
        log.log_warning("[diagnostic] no extractable content found in file")

    derived_periods = fact_aggregation.derive_periods_from_tables(ts_tables) or [period]
    log.log_info(f"[diagnostic] calculating KPIs for periods={derived_periods}")

    kpi_results: dict[str, list[dict]] = {}
    for p in derived_periods:
        kpi_results[p] = calculate_kpis(
            company_id=company_id,
            period=p,
            registry=registry,
            kpi_ids=kpi_ids or None,
        )

    output = {
        "file": file_path,
        "company_id": company_id,
        "period": period,
        "ingest": ingest_result,
        "kpi_periods": derived_periods,
        "kpis": kpi_results,
    }
    print(json.dumps(output, indent=2, default=str))
    return output


# if __name__ == "__main__":
#     file_path = "sample_data/synth/Fluke_2023.xlsx"
#     company_id = "Fluke"
#     period = 2023
#     ans =  test_extractor(file_path, company_id, period)
#     print(ans)
