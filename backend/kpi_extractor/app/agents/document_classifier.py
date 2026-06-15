"""
Document Classification Agent  (Stage 3)
==========================================
CrewAI Role  : Document Classifier
Responsibility: Classify an uploaded document into one supported type.

MCP tools used : NONE
  (Classification is LLM reasoning over a chunk sample — no registry
   context needed. The document type drives which registry facts are
   loaded in Stage 4, so this agent runs before the MCP layer.)

Input:
    chunks: list of chunk dicts (from chunking.py)

Output:
    {"document_type": str, "confidence": float}

Runs ONCE per document. Result is stored in Postgres documents table.
"""

from backend.kpi_extractor.app.agents.base_agent import BaseAgent

SUPPORTED_TYPES = [
    "financial", "governance", "project_portfolio", "telemetry",
    "operations", "inventory", "benchmark", "crm", "hr",
    "board_deck", "audit",
]

SYSTEM = """You are a document classifier for a Private Equity AI Observability platform.
Classify documents into exactly one category. Be concise and precise."""

PROMPT = """Classify this document into ONE of these types:
{types}

Rules:
- financial     : P&L, balance sheet, revenue reports, budget documents
- governance    : AI policy, risk assessments, compliance reports, audit findings
- project_portfolio : AI initiative trackers, project status, use-case pipelines
- telemetry     : API logs, error rates, latency metrics, uptime data
- operations    : process docs, SOP, operational metrics, cost-per-outcome data
- inventory     : asset registers, model inventories, vendor catalogues
- benchmark     : industry comparisons, maturity assessments, peer benchmarks
- crm           : sales data, customer records, pipeline, deal tracking
- hr            : headcount, skills assessments, org charts, training records
- board_deck    : board presentations, investor updates, executive summaries
- audit         : audit trails, model evaluations, hallucination test reports

Document excerpt (first {n} chunks):
---
{excerpt}
---

Respond ONLY with JSON: {{"document_type": "<type>", "confidence": <0.0-1.0>}}"""


class DocumentClassifierAgent(BaseAgent):
    """
    CrewAI Agent: Document Classifier

    Wiring:
        agent = DocumentClassifierAgent(llm, registry)
        result = agent.run(chunks)
    """

    def run(self, chunks: list[dict], sample_size: int = 3) -> dict:
        sample = chunks[:sample_size]
        excerpt = "\n\n---\n\n".join(c["chunk_text"] for c in sample)[:4000]

        prompt = PROMPT.format(
            types=", ".join(SUPPORTED_TYPES),
            n=len(sample),
            excerpt=excerpt,
        )

        raw = self.llm.complete(user=prompt, system=SYSTEM, max_tokens=128)

        try:
            result = self._parse_json(raw)
        except ValueError:
            result = {"document_type": "operations", "confidence": 0.0}

        if result.get("document_type") not in SUPPORTED_TYPES: #type:ignore
            result = {"document_type": "operations", "confidence": 0.0}

        return result #type:ignore
