# Nexus Platform — Stage 2: Agents & Deterministic Engines

Builds on Stage 1 (Registry MCP). Adds the three CrewAI agents and two
deterministic engines that complete the pipeline.

## Folder Structure

```
kpi_extractor/
├── registry/                          # Stage 1 (unchanged)
│   ├── facts/        102 YAML files
│   └── kpis/         59 YAML files
├── app/
│   ├── core/
│   │   ├── llm_client.py     LLM provider abstraction (Anthropic/OpenAI)
│   │   └── chunking.py        Stage 1 - markdown -> chunk dicts
│   ├── index/                 Stage 1 (unchanged) - SQLite registry index
│   ├── mcp/                   Stage 1 (unchanged) - MCP server + client
│   ├── agents/
│   │   ├── base_agent.py             shared LLM + MCP wiring
│   │   ├── document_classifier.py    Stage 3  (LLM, no MCP)
│   │   ├── fact_extraction_agent.py  Stage 4+5 (MCP: discover_relevant_facts)
│   │   └── insight_agent.py          Stage 11 (MCP: get_kpi_context, get_fact_context)
│   ├── engine/
│   │   ├── fact_validation_engine.py    Stage 6 - deterministic
│   │   └── kpi_calculation_engine.py    Stage 8-10 - deterministic
│   ├── db/
│   │   ├── pg_schema.sql      Postgres schema
│   │   └── pg_client.py       Postgres wrapper
│   └── pipeline.py             Orchestration + CrewAI Crew definition
├── data/registry.db            Built SQLite index
└── tests/test_all.py           Full test suite (7/7 passing)
```

## Agent Responsibilities (CrewAI)

| Agent | Role | MCP Tools Used | LLM Calls |
|---|---|---|---|
| **DocumentClassifierAgent** | Document Classifier | none | 1 per document |
| **FactExtractionAgent** | Fact Extractor | `discover_relevant_facts(document_type)` | 1 per candidate fact |
| **InsightAgent** | AI Investment Analyst | `get_kpi_context`, `get_fact_context` | 1 per question |

KPI Calculation and Fact Validation are **not agents** — they are
deterministic Python services (`app/engine/`).

## Pipeline Flow

```
ingest_document()
    chunk_markdown()                          [Stage 1]
    DocumentClassifierAgent.run()             [Stage 3, 1 LLM call]
    FactExtractionAgent.run()                  [Stage 4+5]
        -> discover_relevant_facts(doc_type)      MCP, rule-based scoping
        -> _find_candidates()                     rule-based, no LLM
        -> _extract_fact() per candidate          1 LLM call each
    validate_extractions()                     [Stage 6, deterministic]
    resolve_source_conflicts()                 [Stage 6, deterministic]
    db.save_fact() per validated fact          [Stage 7]

calculate_kpis()
    KPICalculationEngine.calculate_all()       [Stage 8-10, deterministic]
        -> retrieve_formula_context(kpi_id)       MCP
        -> coverage check
        -> _eval_formula()                        pure Python AST
    db.save_kpi() per result

get_insights()
    InsightAgent.run()                          [Stage 11, 1 LLM call]
        -> get_kpi_context() per relevant KPI     MCP
        -> get_fact_context() per missing fact    MCP
```

## Fact Extraction Agent — candidate scoping example

For a `financial` document, `discover_relevant_facts("financial")` returns
36 of the 102 facts (those with `financial` in `document_sources`), each
with aliases + extraction patterns. The agent's rule-based
`_find_candidates()` then narrows to only the facts whose alias/pattern
text actually appears in the document's chunks - typically 3-8 facts per
document - before any LLM extraction call.

## Fact Validation Engine — what it enforces

1. **Confidence gate** — drops extractions below `confidence_rules.minimum_confidence`
2. **Type coercion** — currency/float/percentage -> float, boolean -> bool
3. **Range validation** — `validation_rules.min` / `.max`
4. **Enum validation** — `validation_rules.allowed_values`
5. **Normalization** — decimal-to-percentage conversion per `normalization_rules`
6. **Source priority** — when the same fact is extracted from multiple
   document types, the one earliest in `source_priority` wins

## KPI Calculation Engine — formula evaluation

- `retrieve_formula_context(kpi_id)` returns the formula string + required
  fact_ids with their data types.
- Coverage = `len(available_facts) / len(required_facts)`. If < 1.0,
  status = `insufficient_data` (no calculation attempted).
- Arithmetic formulas (`(a + b) / c`) are evaluated via a restricted AST
  walker — only `+ - * / ** unary-` on named fact variables and numeric
  constants are permitted.
- Aggregation-style formulas (`count()`, `sum() WHERE`, `percentile_rank()`)
  are detected and treated as already pre-resolved into a derived fact
  upstream — the engine reads that derived fact's value directly.

## Running Tests

```bash
cd kpi_extractor
python3 tests/test_all.py
```

7/7 test groups pass without a live LLM or Postgres:
- MCP layer runs as a real subprocess against SQLite
- Agents use a `StubLLM` returning pre-canned JSON
- Engines are fully deterministic

## Running for Real

```bash
pip install crewai anthropic psycopg2-binary pyyaml

export ANTHROPIC_API_KEY=...
export NEXUS_PG_HOST=... NEXUS_PG_DB=nexus NEXUS_PG_USER=... NEXUS_PG_PASSWORD=...

# 1. Create Postgres schema
psql -f app/db/pg_schema.sql

# 2. Build/refresh registry index (after any YAML change)
python -m app.index.index_builder
```

```python
from app.core.llm_client import get_llm_client
from app.mcp.client import RegistryMCPClient
from app import pipeline

llm = get_llm_client()
with RegistryMCPClient() as registry:
    result = pipeline.ingest_document(markdown_text, "finance_q2.pdf",
                                       "portco_001", "2026-Q2", llm, registry)
    kpis = pipeline.calculate_kpis("portco_001", "2026-Q2", registry)
    insight = pipeline.get_insights(
        "Why did ROI improve this quarter?",
        "portco_001", "2026-Q2", ["ai_roi", "ebitda_uplift"], llm, registry,
    )
```

## New KPI / Fact = YAML only (unchanged)

Add YAML to `registry/facts/` or `registry/kpis/`, run
`python -m app.index.index_builder`. No agent or engine code changes -
`discover_relevant_facts`, `get_kpi_context`, `retrieve_formula_context`
all pick up new entries automatically.
