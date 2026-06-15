"""
Fact Candidate Finder: rule-based engine.

Given a document's classified type, load ONLY the facts relevant to that
document type (via RegistryCache.facts_for_document_type), then scan chunks
for alias / extraction_pattern keyword matches.

Output: {fact_id: [chunk_id, chunk_id, ...]}
"""

from typing import Dict, List
from backend.utilites.registry_loader import RegistryCache, FactDef


def find_fact_candidates(chunks: List[Dict], registry: RegistryCache) -> Dict[str, List[str]]:
    """
    chunks: list of chunk dicts, ALL belonging to one document, already
            stamped with document_type by the Classification Agent.
    registry: compiled RegistryCache (loaded once at startup)

    Returns: {fact_id: [chunk_id, ...]} candidate mapping.
    """
    if not chunks:
        return {}

    document_type = chunks[0]["document_type"]
    relevant_facts = registry.facts_for_document_type(document_type)

    candidates: Dict[str, List[str]] = {}

    for fact in relevant_facts:
        search_terms = _build_search_terms(fact)
        matching_chunk_ids = []

        for chunk in chunks:
            text_lower = chunk["chunk_text"].lower()
            if any(term in text_lower for term in search_terms):
                matching_chunk_ids.append(chunk["chunk_id"])

        if matching_chunk_ids:
            candidates[fact.fact_id] = matching_chunk_ids

    return candidates


def _build_search_terms(fact: FactDef) -> List[str]:
    """Combine aliases + extraction patterns into a flat lowercase term list."""
    terms = []
    for alias in fact.possible_aliases:
        terms.append(alias.lower())
    for pattern in fact.extraction_patterns:
        terms.append(pattern.lower())
    return terms
