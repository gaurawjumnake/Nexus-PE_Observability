"""
Fact Extraction Agent  (Stage 4 + 5)
======================================
CrewAI Role  : Fact Extractor
Responsibility:
    1. Call MCP → discover_relevant_facts(document_type)
       to get ONLY the facts relevant to this document's category.
    2. Scan chunks for alias/pattern matches (rule-based candidate finder).
    3. For each candidate fact, call LLM with just those chunks to extract
       the value and confidence.
    4. Return raw extractions for the Fact Validation Engine.

MCP tools used:
    discover_relevant_facts(document_type)
    get_fact_context(fact_id)           ← only for high-confidence candidates

Agents NEVER see the full registry. discover_relevant_facts returns a
scoped subset (e.g. 36 facts for financial docs vs 102 total).
"""

from typing import Optional
from backend.kpi_extractor.app.agents.base_agent import BaseAgent

SYSTEM = """You are a structured data extraction specialist for a PE AI Observability platform.
Extract exactly the requested fact value from the document excerpt.
Respond only with JSON. Never guess — return null if the value is not clearly stated."""

EXTRACTION_PROMPT = """Extract the value for this fact from the document excerpt below.

Fact ID    : {fact_id}
Fact Name  : {name}
Data Type  : {data_type}
Unit       : {unit}
Definition : {description}
Look for   : {aliases}

Document excerpt:
---
{excerpt}
---

Respond ONLY with JSON:
{{"value": <number, string, bool, or null>, "confidence": <0.0-1.0>, "verbatim": "<exact phrase found>"}}

CRITICAL RULES:
- If Data Type is numeric (number/float/integer), the "value" MUST be a pure number (e.g., 2.8, 3.4) without any descriptive text, units, or commas.
- Set value=null and confidence=0.0 if the fact is not clearly present.
- confidence=1.0 means the value is explicitly stated with no ambiguity.
- verbatim is the exact text in the document that supports the value."""


class FactExtractionAgent(BaseAgent):
    """
    CrewAI Agent: Fact Extractor

    Wiring:
        agent = FactExtractionAgent(llm, registry)
        extractions = agent.run(document_type, chunks)
    """

    # Minimum alias/pattern overlap to consider a chunk a candidate
    MIN_TERM_HITS = 1

    def run(self, document_type: str, chunks: list[dict]) -> list[dict]:
        """
        Returns list of raw extractions:
            [{fact_id, value, confidence, verbatim, chunk_id, document_id}, ...]

        Only facts with value != null are returned.
        """
        # Step 1: MCP → scoped fact list for this document type
        relevant_facts = self.registry.discover_relevant_facts(document_type)

        # Step 2: rule-based candidate finder (no LLM)
        candidates = self._find_candidates(chunks, relevant_facts)

        # Step 3: LLM extraction per (fact, candidate chunks)
        extractions = []
        chunks_by_id = {c["chunk_id"]: c for c in chunks}

        for fact_id, chunk_ids in candidates.items():
            fact_meta = next(f for f in relevant_facts if f["fact_id"] == fact_id)
            candidate_chunks = [chunks_by_id[cid] for cid in chunk_ids]
            result = self._extract_fact(fact_meta, candidate_chunks)
            if result is not None:
                extractions.append(result)

        return extractions

    # ------------------------------------------------------------------
    def _find_candidates(
        self, chunks: list[dict], relevant_facts: list[dict]
    ) -> dict[str, list[str]]:
        """
        Pure rule-based: for each fact, find chunks containing any alias
        or extraction pattern. Returns {fact_id: [chunk_id, ...]}.
        """
        candidates: dict[str, list[str]] = {}

        for fact in relevant_facts:
            terms = [t.lower() for t in fact["aliases"] + fact["extraction_patterns"]]
            matched = []
            for chunk in chunks:
                text = chunk["chunk_text"].lower()
                if any(term in text for term in terms):
                    matched.append(chunk["chunk_id"])
            if matched:
                candidates[fact["fact_id"]] = matched

        return candidates

    def _extract_fact(
        self, fact_meta: dict, candidate_chunks: list[dict]
    ) -> Optional[dict]:
        """
        Single LLM call: extract one fact from its candidate chunks.
        Returns None if the LLM says value=null.
        """
        excerpt = "\n\n".join(c["chunk_text"] for c in candidate_chunks)[:3000]
        aliases_str = "; ".join(fact_meta["aliases"][:6])

        prompt = EXTRACTION_PROMPT.format(
            fact_id=fact_meta["fact_id"],
            name=fact_meta["name"],
            data_type=fact_meta["data_type"],
            unit=fact_meta.get("unit", ""),
            description=fact_meta.get("description", ""),
            aliases=aliases_str,
            excerpt=excerpt,
        )

        raw = self.llm.complete(user=prompt, system=SYSTEM, max_tokens=256)

        try:
            parsed = self._parse_json(raw)
        except ValueError:
            return None

        if parsed.get("value") is None: #type:ignore
            return None

        source_chunk = candidate_chunks[0]
        return {
            "fact_id":     fact_meta["fact_id"],
            "value":       parsed["value"], #type:ignore
            "confidence":  float(parsed.get("confidence", 0.0)), #type:ignore
            "verbatim":    parsed.get("verbatim", ""), #type:ignore
            "chunk_id":    source_chunk["chunk_id"],
            "document_id": source_chunk["document_id"],
        }
