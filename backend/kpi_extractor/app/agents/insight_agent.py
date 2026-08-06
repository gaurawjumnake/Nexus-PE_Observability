"""
Insight Agent  (Stage 11)
===========================
CrewAI Role  : AI Investment Analyst
Responsibility:
    The ONLY reasoning-heavy agent. Receives already-computed structured
    data (KPI values, facts, coverage) and produces PE-grade analysis.

MCP tools used:
    get_kpi_context(kpi_id)           ← to understand KPI definitions
    search_related_registry_content(query) ← to trace KPI dependencies
    get_fact_context(fact_id)         ← to understand missing fact impact

Agents NEVER receive raw document chunks or the full registry.
"""

from backend.kpi_extractor.app.agents.base_agent import BaseAgent

SYSTEM = """You are a Senior AI Investment Analyst at a Private Equity firm.
You analyse AI initiative performance data across a portfolio of companies.
Your analysis is precise, evidence-based, and actionable for PE partners and portfolio CEOs.
Never fabricate data. If data is missing, say so explicitly."""

INSIGHT_PROMPT = """Analyse the AI performance data below and answer the question.

QUESTION: {question}

KPI VALUES:
{kpi_section}

FACT VALUES (underlying data):
{fact_section}

KPI COVERAGE (missing data):
{coverage_section}

REGISTRY CONTEXT (KPI definitions):
{registry_section}

Produce a structured response:

## Executive Summary
2-3 sentences on the overall AI performance picture.

## Analysis
Answer the question directly using the data provided.

## Root Cause (if relevant)
If a KPI is underperforming or missing, explain the likely cause based on missing facts.

## Recommendations
3-5 specific, actionable recommendations for the portfolio company or PE team.

## Missing Data
List any KPIs that could not be calculated and what documents are needed to resolve this.
"""


class InsightAgent(BaseAgent):
    """
    CrewAI Agent: AI Investment Analyst

    Wiring:
        agent = InsightAgent(llm, registry)
        response = agent.run(question, kpi_records, fact_records,
                             coverage_records, kpi_ids)
    """

    def run(
        self,
        question: str,
        kpi_records: list[dict],
        fact_records: list[dict],
        coverage_records: list[dict],
        kpi_ids: list[str],
    ) -> str:
        # Fetch targeted registry context via MCP — only the KPIs relevant
        # to the question, not the full registry
        registry_context = self._fetch_registry_context(kpi_ids, coverage_records)

        prompt = INSIGHT_PROMPT.format(
            question=question,
            kpi_section=self._format_kpis(kpi_records),
            fact_section=self._format_facts(fact_records),
            coverage_section=self._format_coverage(coverage_records),
            registry_section=registry_context,
        )

        return self.llm.complete(user=prompt, system=SYSTEM, max_tokens=2048)

    # ------------------------------------------------------------------
    def _fetch_registry_context(
        self, kpi_ids: list[str], coverage_records: list[dict]
    ) -> str:
        lines = []

        for kpi_id in kpi_ids:
            try:
                ctx = self.registry.get_kpi_context(kpi_id)
                lines.append(
                    f"KPI: {ctx['name']} ({kpi_id})\n"  #type:ignore
                    f"  Formula: {ctx.get('formula', 'N/A')}\n"  #type:ignore
                    f"  Required facts: {', '.join(ctx['required_facts'])}\n" #type:ignore
                    f"  Required docs:  {', '.join(ctx['required_documents'])}" #type:ignore
                )
            except RuntimeError:
                lines.append(f"KPI: {kpi_id} — definition not found in registry")

        # For KPIs with missing facts, explain what those facts represent
        missing_fact_ids = set()
        for cov in coverage_records:
            missing_fact_ids.update(cov.get("missing_facts", []))

        for fact_id in list(missing_fact_ids)[:5]:  # cap at 5 to keep prompt tight
            try:
                fact_ctx = self.registry.get_fact_context(fact_id)
                lines.append(
                    f"Missing fact: {fact_ctx['name']} ({fact_id})\n" #type:ignore
                    f"  Description: {fact_ctx['description']}\n" #type:ignore
                    f"  Find in: {', '.join(fact_ctx['source_priority'][:3])}" #type:ignore
                )
            except RuntimeError:
                pass

        return "\n\n".join(lines) if lines else "No registry context retrieved."

    # ------------------------------------------------------------------
    @staticmethod
    def _format_kpis(records: list[dict]) -> str:
        if not records:
            return "No KPI data available."
        lines = []
        for r in records:
            history = r.get("history", [])
            trend = " → ".join(
                f"{h['period']}:{h['value']}" for h in reversed(history)
            ) if history else "no history"
            val = f"{r['value']:.4g}" if r.get("value") is not None else "N/A"
            lines.append(
                f"- {r['kpi_id']} | {r['company_id']} | value={val} "
                f"| coverage={r.get('coverage', 0):.0%} "
                f"| status={r.get('status')} | trend: {trend}"
            )
        return "\n".join(lines)

    @staticmethod
    def _format_facts(records: list[dict]) -> str:
        if not records:
            return "No fact data available."
        lines = []
        for r in records:
            confidence = r.get("confidence")
            conf_str = f"{confidence:.2f}" if confidence is not None else "?"
            lines.append(
                f"- {r['fact_id']} = {r['value']} "
                f"(confidence={conf_str}, "
                f"source={r.get('source_type') or '?'})"
            )
        return "\n".join(lines)

    @staticmethod
    def _format_coverage(records: list[dict]) -> str:
        if not records:
            return "No coverage data."
        lines = []
        for r in records:
            status = "✓ ready" if r["ready_to_calculate"] else "✗ incomplete"
            missing = ", ".join(r["missing_facts"]) if r["missing_facts"] else "none"
            lines.append(
                f"- {r['kpi_id']}: {status} | coverage={r['coverage']:.0%} "
                f"| missing facts: {missing}"
            )
        return "\n".join(lines)
