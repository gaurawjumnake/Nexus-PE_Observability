"""
Wires all stages into three entry-point functions:

    ingest_document(...)    Stages 1-6  → facts in Postgres
    calculate_kpis(...)     Stages 8-10 → KPIs in Postgres
    get_insights(...)       Stage 11    → executive analysis

CrewAI three agents run as a sequential CrewAI crew when used in agentic mode. 
For batch/API mode, call the entry-point functions directly.
"""
from typing import Optional, Any

from backend.utilites.llm_models import BaseLLMClient, get_llm_client
from backend.kpi_extractor.app.core.chunking import chunk_markdown
from backend.kpi_extractor.app.mcp.client import RegistryMCPClient
from backend.kpi_extractor.app.agents.document_classifier import DocumentClassifierAgent
from backend.kpi_extractor.app.agents.fact_extraction_agent import FactExtractionAgent
from backend.kpi_extractor.app.agents.insight_agent import InsightAgent
from backend.kpi_extractor.app.engine.fact_validation_engine import validate_extractions, resolve_source_conflicts
from backend.kpi_extractor.app.engine.kpi_calculation_engine import KPICalculationEngine
import backend.kpi_extractor.app.db.db_client as db
from backend.utilites.llm_models import llm
from crewai import Agent, Task, Crew, Process

# ------------------------------------------------------------------
# Stage 1-6: Document → Validated Facts
# ------------------------------------------------------------------
def ingest_document(
    markdown_text: str,
    file_name: str,
    company_id: str,
    period: str,
    llm: BaseLLMClient,
    registry: RegistryMCPClient,
) -> dict:
    """
    Full ingestion pipeline for one document.

    Args:
        markdown_text : LlamaParse output
        file_name     : original filename (for lineage)
        company_id    : portfolio company identifier
        period        : reporting period, e.g. '2026-Q2'
        llm           : LLM client instance
        registry      : active RegistryMCPClient

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
    registry: RegistryMCPClient,
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
# Stage 8-10: Facts → KPIs
# ------------------------------------------------------------------
def calculate_kpis(
    company_id: str,
    period: str,
    registry: RegistryMCPClient,
    kpi_ids: Optional[list[str]] = None,
) -> list[dict]:
    """
    Runs KPI Calculation Engine for a company/period.
    Reads facts from Postgres, writes KPI results to Postgres.
    """
    engine = KPICalculationEngine(registry)
    return engine.calculate_all(
        company_id=company_id,
        period=period,
        get_facts_fn=db.get_facts_for_company,
        save_kpi_fn=db.save_kpi,
        kpi_ids=kpi_ids,
    )


# ------------------------------------------------------------------
# Stage 11: KPIs + Facts → Insight
# ------------------------------------------------------------------
def get_insights(
    question: str,
    company_id: str,
    period: str,
    kpi_ids: list[str],
    llm: BaseLLMClient,
    registry: RegistryMCPClient,
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

    # Load underlying fact values
    all_facts = db.get_facts_for_company(company_id, period)
    fact_records = [
        {"fact_id": fid, "value": val,
         "confidence": None, "source_type": None, "company_id": company_id}
        for fid, val in all_facts.items()
    ]

    # Build coverage records from KPI engine output
    coverage_records = []
    for kid in kpi_ids:
        try:
            ctx = registry.retrieve_formula_context(kid)
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
        except RuntimeError:
            pass

    agent = InsightAgent(llm, registry)
    return agent.run(question, kpi_records, fact_records, coverage_records, kpi_ids)



def build_crew(llm: Optional[BaseLLMClient] = None, registry: Optional[RegistryMCPClient] = None):
    """
    Returns a configured CrewAI Crew.

    Install: pip install crewai
    Usage:
        with RegistryMCPClient() as registry:
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

# if __name__ == "__main__":
#     llm = get_llm_client()
#     with RegistryMCPClient() as registry:
#         result = ingest_document(
#             markdown_text='## Revenue\nTotal AI revenue: /$5.2M',
#             file_name='test.pdf',
#             company_id='portco_001',
#             period='2026-Q2',
#             llm=llm,
#             registry=registry,
#         )
#     print(result)

