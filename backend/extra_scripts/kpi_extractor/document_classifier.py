"""
Document Classification Agent:
Runs ONCE per document (not per chunk).
Uses a small sample of chunks (first N) to classify the document
into one of the supported document_type categories.

This is the only place an LLM sees raw document content for classification.
"""

from typing import Dict, List

SUPPORTED_DOCUMENT_TYPES = [
    "financial", "governance", "project_portfolio", "telemetry",
    "operations", "inventory", "benchmark", "crm", "hr",
    "board_deck", "audit",
]

CLASSIFICATION_PROMPT = """You are a document classifier for a PE AI Observability platform.

Classify the document into exactly ONE of these types:
{types}

Use only the excerpt below. Respond as strict JSON:
{{"document_type": "<type>", "confidence": <0.0-1.0>}}

Document excerpt:
---
{excerpt}
---
"""


def classify_document(chunks: List[Dict], llm_client, sample_size: int = 3) -> Dict:
    """
    chunks: list of chunk dicts from chunk_markdown() for ONE document
    llm_client: object with a .complete(prompt) -> str method (any LLM provider)

    Returns: {"document_type": str, "confidence": float}
    """
    sample = chunks[:sample_size]
    excerpt = "\n\n".join(c["chunk_text"] for c in sample)
    excerpt = excerpt[:4000]  # cap excerpt size sent to LLM

    prompt = CLASSIFICATION_PROMPT.format(
        types=", ".join(SUPPORTED_DOCUMENT_TYPES),
        excerpt=excerpt,
    )

    response = llm_client.complete(prompt)
    result = _parse_json_response(response)

    if result["document_type"] not in SUPPORTED_DOCUMENT_TYPES:
        result = {"document_type": "operations", "confidence": 0.0}  # safe fallback

    return result


def _parse_json_response(response: str) -> Dict:
    import json
    try:
        cleaned = response.strip().strip("`").lstrip("json").strip()
        return json.loads(cleaned)
    except Exception:
        return {"document_type": "operations", "confidence": 0.0}


def apply_classification(chunks: List[Dict], document_type: str) -> List[Dict]:
    """Stamp document_type onto every chunk of a classified document."""
    for c in chunks:
        c["document_type"] = document_type
    return chunks
