"""
Fact Extraction Agent:
Input: fact definition + its candidate chunks (from Stage 4 only).
Never processes all facts or all chunks - strictly category/candidate-driven.

One LLM call per (fact, document) pair, using only candidate chunk text.
"""

import json
from typing import Dict, List, Optional
from backend.utilites.registry_loader import FactDef

EXTRACTION_PROMPT = """You are a fact extraction agent for a PE AI Observability platform.

Extract the value for this fact from the document excerpt below.

Fact: {name}
Definition: {business_definition}
Data type: {data_type}
Unit: {unit}
Aliases this fact may appear as: {aliases}

Document excerpt:
---
{excerpt}
---

Respond as strict JSON only:
{{"value": <extracted value or null if not found>, "confidence": <0.0-1.0>}}
"""


def extract_fact(
    fact: FactDef,
    candidate_chunks: List[Dict],
    llm_client,
) -> Optional[Dict]:
    """
    fact: FactDef from RegistryCache
    candidate_chunks: chunk dicts whose chunk_id was returned for this fact
                       by find_fact_candidates()
    llm_client: object with .complete(prompt) -> str

    Returns: {"fact_id", "value", "confidence", "source_chunk_id",
              "document_id", "company_id"} or None if nothing extracted.
    """
    if not candidate_chunks:
        return None

    excerpt = "\n\n".join(c["chunk_text"] for c in candidate_chunks)
    excerpt = excerpt[:4000]

    prompt = EXTRACTION_PROMPT.format(
        name=fact.name,
        business_definition=fact.description, #type:ignore
        data_type=fact.data_type,
        unit=fact.unit,
        aliases=", ".join(fact.possible_aliases),
        excerpt=excerpt,
    )

    response = llm_client.complete(prompt)
    parsed = _parse_json_response(response)

    if parsed.get("value") is None:
        return None

    source_chunk = candidate_chunks[0]

    return {
        "fact_id": fact.fact_id,
        "value": parsed["value"],
        "confidence": parsed.get("confidence", 0.0),
        "source_chunk_id": source_chunk["chunk_id"],
        "document_id": source_chunk["document_id"],
        "document_type": source_chunk["document_type"],
        "company_id": source_chunk["company_id"],
    }


def _parse_json_response(response: str) -> Dict:
    try:
        cleaned = response.strip().strip("`").lstrip("json").strip()
        return json.loads(cleaned)
    except Exception:
        return {"value": None, "confidence": 0.0}
