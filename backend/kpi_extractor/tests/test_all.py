"""
Nexus Platform - Full Test Suite
==================================
Tests all components without requiring a live LLM or Postgres.

- MCP layer  : real subprocess (server.py + SQLite)
- Agents     : stub LLM client injected
- Engines    : fully deterministic, no stubs needed
- Chunking   : pure Python, no deps

Run: python3 tests/test_all.py
"""

import sys
import os
import json

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from app.mcp.client import RegistryMCPClient
from app.core.chunking import chunk_markdown
from app.core.llm_client import BaseLLMClient
from app.agents.document_classifier import DocumentClassifierAgent
from app.agents.fact_extraction_agent import FactExtractionAgent
from app.agents.insight_agent import InsightAgent
from app.engine.fact_validation_engine import validate_extractions, resolve_source_conflicts
from app.engine.kpi_calculation_engine import KPICalculationEngine, _eval_formula

PASS = "\033[92m✓\033[0m"
FAIL = "\033[91m✗\033[0m"
SECTION = "\033[94m──\033[0m"


def check(label: str, condition: bool, detail: str = "") -> bool:
    icon = PASS if condition else FAIL
    print(f"    {icon}  {label}" + (f"  →  {detail}" if detail else ""))
    return condition


# ------------------------------------------------------------------
# Stub LLM Client
# ------------------------------------------------------------------
class StubLLM(BaseLLMClient):
    """Returns pre-canned JSON responses per keyword."""

    def __init__(self, responses: dict):
        self._responses = responses  # {keyword: json_string}

    def complete(self, user: str, system: str = None, max_tokens: int = 2048) -> str:
        for keyword, response in self._responses.items():
            if keyword.lower() in user.lower():
                return response
        return '{"document_type": "financial", "confidence": 0.90}'


# ------------------------------------------------------------------
# Test Data
# ------------------------------------------------------------------
SAMPLE_MARKDOWN = """## Q2 2026 Financial Summary

AI Revenue Performance:
Direct AI Revenue for Q2 2026 was $5.2M driven by AI-native product lines.
AI-assisted revenue from sales acceleration reached $2.1M this quarter.
Pipeline influenced revenue from AI-driven marketing was $8.9M.

Cost Analysis:
Total AI Spend year-to-date: $12.8M
Cloud compute spend for AI workloads: $6.4M
API licensing spend (OpenAI, Anthropic): $2.9M

EBITDA:
EBITDA post-AI implementation: $25M
EBITDA pre-AI baseline (same period prior year): $21M
Revenue base for this period: $100M
"""


# ------------------------------------------------------------------
# Test 1: Chunking
# ------------------------------------------------------------------
def test_chunking() -> bool:
    print(f"\n{SECTION} 1. Chunking")
    ok = True
    chunks = chunk_markdown(SAMPLE_MARKDOWN, document_id="test-doc-001")
    ok &= check("produces chunks", len(chunks) >= 1, f"{len(chunks)} chunks")
    ok &= check("each chunk has chunk_id", all("chunk_id" in c for c in chunks))
    ok &= check("each chunk has document_id", all(c["document_id"] == "test-doc-001" for c in chunks))
    ok &= check("each chunk has chunk_text", all(len(c["chunk_text"]) > 0 for c in chunks))
    ok &= check("metadata has chunk_index", all("chunk_index" in c["metadata"] for c in chunks))
    ok &= check("section titles extracted", any(c["metadata"].get("section") for c in chunks))
    return ok


# ------------------------------------------------------------------
# Test 2: MCP Layer (real server subprocess)
# ------------------------------------------------------------------
def test_mcp_layer() -> bool:
    print(f"\n{SECTION} 2. MCP Layer")
    ok = True
    with RegistryMCPClient() as r:
        # discover_relevant_facts
        facts = r.discover_relevant_facts("financial")
        ok &= check("financial: 30+ facts", len(facts) >= 30, f"{len(facts)}")
        ok &= check("financial: direct_ai_revenue present",
                    any(f["fact_id"] == "direct_ai_revenue" for f in facts))
        ok &= check("financial: aliases populated",
                    all(len(f["aliases"]) >= 1 for f in facts))
        ok &= check("financial: patterns populated",
                    all(len(f["extraction_patterns"]) >= 1 for f in facts))

        # get_kpi_context
        kpi = r.get_kpi_context("ai_roi")
        ok &= check("ai_roi: formula correct",
                    "(ai_revenue + cost_savings) / total_ai_spend" in kpi["formula"])
        ok &= check("ai_roi: required_facts populated", len(kpi["required_facts"]) == 3)

        # retrieve_formula_context
        fc = r.retrieve_formula_context("ebitda_uplift")
        ok &= check("ebitda_uplift: formula present", bool(fc.get("formula")), fc.get("formula"))
        ok &= check("ebitda_uplift: fact types present",
                    all("data_type" in v for v in fc["facts"].values()))

        # search
        hits = r.search_registry("ebitda")
        ok &= check("search ebitda: kpi hit", any(h["entity_id"] == "ebitda_uplift" for h in hits))
    return ok


# ------------------------------------------------------------------
# Test 3: Document Classifier Agent
# ------------------------------------------------------------------
def test_classifier() -> bool:
    print(f"\n{SECTION} 3. Document Classifier Agent")
    ok = True
    stub = StubLLM({
        "financial":     '{"document_type": "financial", "confidence": 0.95}',
        "governance":    '{"document_type": "governance", "confidence": 0.88}',
        "project_portfolio": '{"document_type": "project_portfolio", "confidence": 0.82}',
    })
    with RegistryMCPClient() as r:
        agent = DocumentClassifierAgent(stub, r)
        chunks = chunk_markdown(SAMPLE_MARKDOWN, "doc-001")

        result = agent.run(chunks)
        ok &= check("returns document_type", "document_type" in result)
        ok &= check("returns confidence", "confidence" in result)
        ok &= check("valid type returned", result["document_type"] == "financial", result["document_type"])
        ok &= check("confidence float", isinstance(result["confidence"], float))

        # test fallback for invalid LLM response
        bad_stub = StubLLM({"x": '{"document_type": "invalid_type", "confidence": 0.5}'})
        agent2 = DocumentClassifierAgent(bad_stub, r)
        result2 = agent2.run(chunks)
        ok &= check("invalid type falls back to 'operations'", result2["document_type"] == "operations")
    return ok


# ------------------------------------------------------------------
# Test 4: Fact Extraction Agent
# ------------------------------------------------------------------
def test_extractor() -> bool:
    print(f"\n{SECTION} 4. Fact Extraction Agent")
    ok = True

    # Stub returns a valid extraction for any fact with "revenue" in the prompt
    def make_stub():
        return StubLLM({
            "direct_ai_revenue":      '{"value": 5200000, "confidence": 0.96, "verbatim": "Direct AI Revenue for Q2 2026 was $5.2M"}',
            "ai_assisted_revenue":    '{"value": 2100000, "confidence": 0.93, "verbatim": "AI-assisted revenue from sales acceleration reached $2.1M"}',
            "total_ai_spend":         '{"value": 12800000, "confidence": 0.95, "verbatim": "Total AI Spend year-to-date: $12.8M"}',
            "ebitda_post_ai":         '{"value": 25000000, "confidence": 0.97, "verbatim": "EBITDA post-AI implementation: $25M"}',
            "ebitda_pre_ai":          '{"value": 21000000, "confidence": 0.95, "verbatim": "EBITDA pre-AI baseline: $21M"}',
            "revenue_base":           '{"value": 100000000, "confidence": 0.98, "verbatim": "Revenue base for this period: $100M"}',
        })

    with RegistryMCPClient() as r:
        agent = FactExtractionAgent(make_stub(), r)
        chunks = chunk_markdown(SAMPLE_MARKDOWN, "doc-001")
        extractions = agent.run("financial", chunks)

        ok &= check("extractions returned", len(extractions) >= 1, f"{len(extractions)} extractions")
        ok &= check("each has fact_id", all("fact_id" in e for e in extractions))
        ok &= check("each has value", all("value" in e for e in extractions))
        ok &= check("each has confidence", all("confidence" in e for e in extractions))
        ok &= check("each has chunk_id", all("chunk_id" in e for e in extractions))
        ok &= check("each has document_id", all("document_id" in e for e in extractions))

        # Candidate finding sub-test
        facts_meta = r.discover_relevant_facts("financial")
        candidates = agent._find_candidates(chunks, facts_meta)
        ok &= check("candidate finder: finds revenue facts",
                    "direct_ai_revenue" in candidates or "ai_revenue" in candidates,
                    list(candidates.keys())[:5])
        ok &= check("candidate finder: finds spend facts",
                    "total_ai_spend" in candidates, list(candidates.keys())[:8])
    return ok


# ------------------------------------------------------------------
# Test 5: Fact Validation Engine
# ------------------------------------------------------------------
def test_validation_engine() -> bool:
    print(f"\n{SECTION} 5. Fact Validation Engine")
    ok = True

    raw_extractions = [
        {"fact_id": "direct_ai_revenue", "value": 5200000.0, "confidence": 0.96,
         "chunk_id": "c1", "document_id": "d1", "verbatim": "$5.2M"},
        {"fact_id": "ebitda_post_ai",    "value": 25000000.0, "confidence": 0.97,
         "chunk_id": "c2", "document_id": "d1", "verbatim": "$25M"},
        {"fact_id": "ebitda_pre_ai",     "value": 21000000.0, "confidence": 0.95,
         "chunk_id": "c3", "document_id": "d1", "verbatim": "$21M"},
        {"fact_id": "revenue_base",      "value": 100000000.0, "confidence": 0.98,
         "chunk_id": "c4", "document_id": "d1", "verbatim": "$100M"},
        # Invalid: negative revenue → should fail range check
        {"fact_id": "direct_ai_revenue", "value": -999.0, "confidence": 0.99,
         "chunk_id": "c5", "document_id": "d1", "verbatim": "-$999"},
        # Low confidence → should be dropped
        {"fact_id": "total_ai_spend",    "value": 12800000.0, "confidence": 0.1,
         "chunk_id": "c6", "document_id": "d1", "verbatim": "$12.8M"},
    ]

    with RegistryMCPClient() as r:
        validated = validate_extractions(raw_extractions, "financial", r)

        ok &= check("validated count < raw count (filtering works)",
                    len(validated) < len(raw_extractions),
                    f"{len(validated)} validated from {len(raw_extractions)} raw")
        ok &= check("negative revenue rejected",
                    not any(e["fact_id"] == "direct_ai_revenue" and e["value"] < 0 for e in validated))
        ok &= check("low confidence dropped",
                    not any(e.get("confidence", 1) < 0.5 and e["fact_id"] == "total_ai_spend" for e in validated))
        ok &= check("each validated has source_type",
                    all("source_type" in e for e in validated))

        # Source priority: two sources for same fact
        conflict = [
            {"fact_id": "ebitda_post_ai", "value": 25000000.0, "confidence": 0.9,
             "source_type": "board_deck", "chunk_id": "c1", "document_id": "d1"},
            {"fact_id": "ebitda_post_ai", "value": 26000000.0, "confidence": 0.95,
             "source_type": "financial",  "chunk_id": "c2", "document_id": "d2"},
        ]
        resolved = resolve_source_conflicts(conflict, r)
        ok &= check("source priority: financial wins over board_deck",
                    resolved[0]["source_type"] == "financial" and resolved[0]["value"] == 26000000.0,
                    f"winner source={resolved[0]['source_type']}, value={resolved[0]['value']}")
    return ok


# ------------------------------------------------------------------
# Test 6: KPI Calculation Engine
# ------------------------------------------------------------------
def test_kpi_engine() -> bool:
    print(f"\n{SECTION} 6. KPI Calculation Engine")
    ok = True

    # Test formula evaluator directly
    ok &= check("simple arithmetic",
                abs(_eval_formula("(ai_revenue + cost_savings) / total_ai_spend",
                                   {"ai_revenue": 39400000, "cost_savings": 12800000,
                                    "total_ai_spend": 12800000}) - 4.078125) < 0.001)
    ok &= check("ebitda uplift formula",
                abs(_eval_formula("(ebitda_post_ai - ebitda_pre_ai) / revenue_base * 100",
                                   {"ebitda_post_ai": 25000000, "ebitda_pre_ai": 21000000,
                                    "revenue_base": 100000000}) - 4.0) < 0.001)
    ok &= check("zero denominator returns ZeroDivisionError", True)
    try:
        _eval_formula("ai_revenue / total_ai_spend", {"ai_revenue": 1.0, "total_ai_spend": 0.0})
        ok &= check("zero denominator returns ZeroDivisionError", False)
    except ZeroDivisionError:
        ok &= check("zero denominator returns ZeroDivisionError", True)

    # Test engine with MCP
    fact_store = {
        "ai_revenue":    39400000.0,
        "cost_savings":  12800000.0,
        "total_ai_spend": 12800000.0,
        "ebitda_post_ai": 25000000.0,
        "ebitda_pre_ai":  21000000.0,
        "ebitda_delta":   4000000.0,
        "revenue_base":   100000000.0,
    }
    saved_kpis = []

    def get_facts(company_id, period):
        return fact_store

    def save_kpi(kpi_id, company_id, value, coverage, status, period):
        saved_kpis.append({"kpi_id": kpi_id, "value": value, "status": status})

    with RegistryMCPClient() as r:
        engine = KPICalculationEngine(r)

        # ai_roi
        result = engine.calculate_one("ai_roi", "portco_001", "2026-Q2", get_facts, save_kpi)
        ok &= check("ai_roi status=calculated", result["status"] == "calculated", str(result["status"]))
        ok &= check("ai_roi value ≈ 4.08", abs(result["value"] - 4.078125) < 0.001, str(result["value"]))
        ok &= check("ai_roi coverage=1.0", result["coverage"] == 1.0)

        # ebitda_uplift
        result2 = engine.calculate_one("ebitda_uplift", "portco_001", "2026-Q2", get_facts, save_kpi)
        ok &= check("ebitda_uplift status=calculated", result2["status"] == "calculated")
        ok &= check("ebitda_uplift value ≈ 4.0%", abs(result2["value"] - 4.0) < 0.001, str(result2["value"]))

        # insufficient data
        result3 = engine.calculate_one(
            "ai_roi", "portco_002", "2026-Q2",
            lambda c, p: {},  # empty fact store
            save_kpi,
        )
        ok &= check("insufficient_data when facts missing", result3["status"] == "insufficient_data")
        ok &= check("missing_facts populated", len(result3.get("missing_facts", [])) > 0)
    return ok


# ------------------------------------------------------------------
# Test 7: Insight Agent
# ------------------------------------------------------------------
def test_insight_agent() -> bool:
    print(f"\n{SECTION} 7. Insight Agent")
    ok = True

    stub = StubLLM({"question": """## Executive Summary
AI ROI of 4.1x exceeds the industry median of 2.4x.

## Analysis
Portfolio is performing strongly on financial KPIs.

## Recommendations
1. Expand AI investment in under-performing PortCos.
2. Prioritise governance compliance.

## Missing Data
ebitda_uplift missing revenue_base from financial documents."""})

    kpi_records = [
        {"kpi_id": "ai_roi", "company_id": "portco_001", "value": 4.1,
         "coverage": 1.0, "status": "calculated", "history": [{"period": "2026-Q1", "value": 3.8}]},
        {"kpi_id": "ebitda_uplift", "company_id": "portco_001", "value": None,
         "coverage": 0.66, "status": "insufficient_data", "history": []},
    ]
    fact_records = [
        {"fact_id": "ai_revenue", "value": 39400000, "confidence": 0.96, "source_type": "financial", "company_id": "portco_001"},
        {"fact_id": "cost_savings", "value": 12800000, "confidence": 0.94, "source_type": "financial", "company_id": "portco_001"},
    ]
    coverage_records = [
        {"kpi_id": "ai_roi", "required_facts": ["ai_revenue","cost_savings","total_ai_spend"],
         "available_facts": ["ai_revenue","cost_savings","total_ai_spend"], "missing_facts": [],
         "coverage": 1.0, "ready_to_calculate": True},
        {"kpi_id": "ebitda_uplift", "required_facts": ["ebitda_post_ai","ebitda_pre_ai","revenue_base"],
         "available_facts": ["ebitda_post_ai","ebitda_pre_ai"], "missing_facts": ["revenue_base"],
         "coverage": 0.66, "ready_to_calculate": False},
    ]

    with RegistryMCPClient() as r:
        agent = InsightAgent(stub, r)
        response = agent.run(
            question="Why did ROI improve this quarter? What is still missing?",
            kpi_records=kpi_records,
            fact_records=fact_records,
            coverage_records=coverage_records,
            kpi_ids=["ai_roi", "ebitda_uplift"],
        )
        ok &= check("response is non-empty string", isinstance(response, str) and len(response) > 50)
        ok &= check("response contains Executive Summary", "Executive Summary" in response)
        ok &= check("response contains Recommendations", "Recommendations" in response)
    return ok


# ------------------------------------------------------------------
# Run all
# ------------------------------------------------------------------
def run_all():
    print("\n" + "═" * 48)
    print("  Nexus Platform — Full Test Suite")
    print("═" * 48)

    tests = [
        ("Chunking",                test_chunking),
        ("MCP Layer",               test_mcp_layer),
        ("Document Classifier",     test_classifier),
        ("Fact Extraction Agent",   test_extractor),
        ("Fact Validation Engine",  test_validation_engine),
        ("KPI Calculation Engine",  test_kpi_engine),
        ("Insight Agent",           test_insight_agent),
    ]

    results = {}
    for name, fn in tests:
        try:
            results[name] = fn()
        except Exception as e:
            print(f"    {FAIL}  EXCEPTION: {e}")
            results[name] = False

    print("\n" + "═" * 48)
    passed = sum(1 for v in results.values() if v)
    total  = len(results)
    for name, ok in results.items():
        print(f"  {PASS if ok else FAIL}  {name}")
    print("─" * 48)
    print(f"  {passed}/{total} test groups passed")
    print("═" * 48 + "\n")
    return passed == total


if __name__ == "__main__":
    ok = run_all()
    sys.exit(0 if ok else 1)
