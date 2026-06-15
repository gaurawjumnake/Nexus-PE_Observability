"""
Insight Agent: The ONLY reasoning agent in the platform.

Inputs (already-computed, structured data - never raw documents/chunks):
  - KPI values + historical trend (from KPI Store)
  - Underlying fact values + lineage (from Fact Store)
  - KPI coverage / missing fact data (from Dependency Resolver)

Outputs: Executive Summary, Root Cause Analysis, Recommendations,
         Missing Data Analysis, KPI Coverage Analysis.

Token-efficient: only the KPIs/facts relevant to the question are passed in,
never the full registry or full fact store.
"""

from typing import Dict, List

INSIGHT_PROMPT = """You are an AI Investment Analyst for a Private Equity Observability platform.

Question: {question}

KPI Data (current + trend):
{kpi_data}

Underlying Facts:
{fact_data}

Coverage / Missing Data:
{coverage_data}

Provide:
1. Executive Summary
2. Root Cause Analysis (if relevant to the question)
3. Recommendations
4. Missing Data Analysis (if coverage is incomplete)
"""


def generate_insight(
    question: str,
    kpi_records: List[Dict],
    fact_records: List[Dict],
    coverage_records: List[Dict],
    llm_client,
) -> str:
    """
    kpi_records: list of {"kpi_id","company_id","value","coverage","calculated_on","status",
                           "history": [...]}
    fact_records: list of {"fact_id","company_id","value","source_document","timestamp"}
    coverage_records: output of dependency_resolver.resolve_kpi_coverage()

    Returns: free-text insight (executive summary + analysis).
    """
    prompt = INSIGHT_PROMPT.format(
        question=question,
        kpi_data=_format_kpis(kpi_records),
        fact_data=_format_facts(fact_records),
        coverage_data=_format_coverage(coverage_records),
    )
    return llm_client.complete(prompt)


def _format_kpis(records: List[Dict]) -> str:
    lines = []
    for r in records:
        history = r.get("history", [])
        trend = " -> ".join(str(h) for h in history) if history else "n/a"
        lines.append(f"- {r['kpi_id']} ({r['company_id']}): {r['value']} "
                      f"(coverage={r.get('coverage')}, trend={trend})")
    return "\n".join(lines) or "none"


def _format_facts(records: List[Dict]) -> str:
    lines = []
    for r in records:
        lines.append(f"- {r['fact_id']} ({r['company_id']}) = {r['value']} "
                      f"[source: {r.get('source_document')}]")
    return "\n".join(lines) or "none"


def _format_coverage(records: List[Dict]) -> str:
    lines = []
    for r in records:
        lines.append(f"- {r['kpi_id']}: coverage={r['coverage']:.0%}, "
                      f"missing={r['missing_facts']}")
    return "\n".join(lines) or "none"
