# Nexus Platform — KPI Extractor

Document → Facts → KPIs → Insight pipeline. Three CrewAI-style agents
plus two deterministic engines, backed by a YAML-defined fact/KPI
registry that's indexed into SQLite for fast lookup.

## Folder Structure

```
backend/
├── config.py                      Single source of truth for all paths
├── db/
│   ├── nexus.db                   fact + KPI value store (sqlite)
│   ├── registry.db                registry index, derived from YAML (sqlite)
│   └── registry_schema.sql        schema for registry.db
└── kpi_extractor/
    ├── extractor_pipeline.py      Orchestration: ingest_document / calculate_kpis / get_insights
    └── app/
        ├── core/
        │   └── chunking.py        markdown -> chunk dicts
        ├── registry/
        │   ├── registry_service.py   reads + writes for facts & KPIs (no MCP, no subprocess)
        │   ├── facts/*.yaml           source of truth - 102 facts
        │   └── kpis/*.yaml            source of truth - 59 KPIs
        ├── agents/
        │   ├── base_agent.py              shared LLM + registry wiring
        │   ├── document_classifier.py     Stage 3  (LLM only)
        │   ├── fact_extraction_agent.py    Stage 4+5 (registry.discover_relevant_facts)
        │   └── insight_agent.py            Stage 11 (registry.get_kpi_context / get_fact_context)
        └── engine/
            ├── fact_validation_engine.py    Stage 6 - deterministic
            └── kpi_calculation_engine.py    Stage 8-10 - deterministic
```

## Why there's no MCP layer

Earlier versions of this app routed registry lookups (`get_kpi_context`,
`discover_relevant_facts`, etc.) through an MCP server running as a
subprocess, talked to over JSON-RPC. That made sense if an *external*
LLM client needed to discover and call these as tools across a process
boundary. It doesn't make sense here: agents and engines are plain
Python in the same process calling `RegistryService` methods directly -
there's no LLM tool-calling boundary to bridge. The subprocess + RPC
indirection only added latency and a process to babysit, for zero
benefit. `registry_service.py` is the direct replacement: same method
names (`get_kpi_context`, `discover_relevant_facts`, `retrieve_formula_context`,
`search_registry`, ...), called as ordinary method calls.

## Agent Responsibilities

| Agent | Role | Registry calls | LLM Calls |
|---|---|---|---|
| **DocumentClassifierAgent** | Document Classifier | none | 1 per document |
| **FactExtractionAgent** | Fact Extractor | `discover_relevant_facts(document_type)` | 1 per candidate fact |
| **InsightAgent** | AI Investment Analyst | `get_kpi_context`, `get_fact_context` | 1 per question |

KPI Calculation and Fact Validation are **not agents** — deterministic
Python services in `app/engine/`.

## Pipeline Flow

```
ingest_document() / ingest_document_from_chunks()
    chunk_markdown()
    DocumentClassifierAgent.run()              1 LLM call
    FactExtractionAgent.run()
        -> registry.discover_relevant_facts(doc_type)   rule-based scoping, no LLM
        -> _find_candidates()                            rule-based, no LLM
        -> _extract_fact() per candidate                 1 LLM call each
    validate_extractions()                      deterministic
    resolve_source_conflicts()                  deterministic
    db.save_fact() per validated fact

calculate_kpis()
    KPICalculationEngine.calculate_all()
        -> registry.retrieve_formula_context(kpi_id)
        -> coverage check
        -> _eval_formula()                       pure Python AST, deterministic
    db.save_kpi() per result

get_insights()
    InsightAgent.run()                           1 LLM call
        -> registry.get_kpi_context() per relevant KPI
        -> registry.get_fact_context() per missing fact
```

## Fact selection at scale (why no embeddings/vector search)

102 facts and 59 KPIs is small enough that a two-stage funnel of plain
filtering does the job, with zero LLM/embedding cost:

1. `discover_relevant_facts(document_type)` — SQL filter on
   `document_sources`, narrows ~161 entities down to ~30-40 for a given
   doc type.
2. `_find_candidates()` in `fact_extraction_agent.py` — checks whether
   each fact's `aliases`/`extraction_patterns` literally appear in the
   document's chunks, narrowing further to typically 3-8 facts.

Only after that does an LLM get called, once per surviving candidate,
to extract the value. If recall ever becomes a real problem (document
phrasing not covered by aliases), add a fuzzy-match fallback
(`rapidfuzz`) before reaching for embeddings — a vector store would be
solving a scale problem this registry doesn't have.

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
  fact_ids with their data types. Returns `None` if the KPI doesn't exist
  (callers check for `None`, not an exception).
- Coverage = `len(available_facts) / len(required_facts)`. If < 1.0,
  status = `insufficient_data` (no calculation attempted).
- Arithmetic formulas (`(a + b) / c`) are evaluated via a restricted AST
  walker — only `+ - * / ** unary-` on named fact variables and numeric
  constants are permitted.
- Aggregation-style formulas (`count()`, `sum() WHERE`, `percentile_rank()`)
  are detected and treated as already pre-resolved into a derived fact
  upstream — the engine reads that derived fact's value directly.

## Running for Real

```bash
pip install crewai anthropic pyyaml
export ANTHROPIC_API_KEY=...
```

The registry SQLite index is built automatically on first app startup
(see `api/deps.py`) — no manual build step needed. To force a full
resync after hand-editing YAML files directly:

```python
from backend.kpi_extractor.app.registry.registry_service import RegistryService
RegistryService().rebuild_from_yaml()
```

```python
from backend.utilites.llm_models import get_llm_client
from backend.kpi_extractor.app.registry.registry_service import RegistryService
from backend.kpi_extractor import extractor_pipeline as pipeline

llm = get_llm_client()
registry = RegistryService()

result = pipeline.ingest_document(markdown_text, "finance_q2.pdf",
                                   "portco_001", "2026-Q2", llm, registry)
kpis = pipeline.calculate_kpis("portco_001", "2026-Q2", registry)
insight = pipeline.get_insights(
    "Why did ROI improve this quarter?",
    "portco_001", "2026-Q2", ["ai_roi", "ebitda_uplift"], llm, registry,
)
```

## Adding / removing a KPI or fact

Use `RegistryService.add_fact` / `add_kpi` / `remove_fact` / `remove_kpi`
(see `registry_service.py`) — each keeps the YAML file and the SQLite
index in sync in one call, so you never need a manual rebuild step.
`remove_fact` blocks if a KPI still depends on it unless you pass
`force=True`. An admin HTTP API for these (for a dashboard) can sit on
top of `RegistryService` directly.
