"""
Fact Validation Engine  (Stage 6)
====================================
100% deterministic. No LLM. No agents.

Responsibilities:
    1. Type validation   — value matches declared data_type
    2. Range validation  — value within min/max from validation_rules
    3. Enum validation   — value in allowed_values list
    4. Normalization     — currency scale, pct decimal→100 conversion
    5. Confidence gate   — drop extractions below minimum_confidence
    6. Source priority   — when multiple sources have the same fact,
                           pick winner by source_priority order

Input:
    extractions: raw output from FactExtractionAgent.run()
    document_type: classified type of the source document
    registry: RegistryMCPClient (to fetch fact context)

Output:
    validated: list of dicts ready for Postgres fact_store save
"""

from typing import Optional
from backend.kpi_extractor.app.mcp.client import RegistryMCPClient


def validate_extractions(
    extractions: list[dict],
    document_type: str,
    registry: RegistryMCPClient,
) -> list[dict]:
    """
    Main entry point. Returns only validated, normalized extractions.
    """
    validated = []
    for ext in extractions:
        fact_ctx = registry.get_fact_context(ext["fact_id"])
        result = _validate_one(ext, fact_ctx, document_type)
        if result is not None:
            validated.append(result)
    return validated


def resolve_source_conflicts(
    validated: list[dict],
    registry: RegistryMCPClient,
) -> list[dict]:
    """
    When the same fact_id appears more than once (extracted from
    multiple documents), keep only the winner per source_priority.
    """
    by_fact: dict[str, list[dict]] = {}
    for v in validated:
        by_fact.setdefault(v["fact_id"], []).append(v)

    winners = []
    for fact_id, candidates in by_fact.items():
        if len(candidates) == 1:
            winners.append(candidates[0])
            continue
        fact_ctx = registry.get_fact_context(fact_id)
        winner = _pick_by_priority(candidates, fact_ctx["source_priority"])
        winners.append(winner)
    return winners


# ------------------------------------------------------------------
# Internal
# ------------------------------------------------------------------
def _validate_one(ext: dict, fact_ctx: dict, document_type: str) -> Optional[dict]:
    value = ext["value"]
    confidence = ext["confidence"]
    rules = fact_ctx.get("validation_rules") or {}
    conf_rules = fact_ctx.get("confidence_rules") or {}
    data_type = fact_ctx["data_type"]

    # 1. Confidence gate
    min_conf = conf_rules.get("minimum_confidence", 0.0)
    if confidence < min_conf:
        return None

    # 2. Type coercion + validation
    value = _coerce_type(value, data_type)
    if value is None:
        return None

    # 3. Range check
    if data_type in ("integer", "float", "currency", "percentage"):
        try:
            num = float(value)
        except (TypeError, ValueError):
            return None
        if "min" in rules and num < rules["min"]:
            return None
        if "max" in rules and num > rules["max"]:
            return None

    # 4. Enum check
    if data_type == "enum":
        allowed = rules.get("allowed_values", [])
        if allowed and value not in allowed:
            return None

    # 5. Normalization
    value = _normalize(value, fact_ctx)

    return {
        **ext,
        "value":       value,
        "source_type": document_type,
    }


def _coerce_type(value, data_type: str):
    try:
        if data_type == "integer":
            return int(float(value))
        if data_type in ("float", "currency"):
            return float(value)
        if data_type == "percentage":
            return float(value)
        if data_type == "boolean":
            if isinstance(value, bool):
                return value
            return str(value).lower() in ("true", "1", "yes")
        return value  # string, enum, date → keep as-is
    except (TypeError, ValueError):
        return None


def _normalize(value, fact_ctx: dict):
    rules = fact_ctx.get("normalization_rules") or {}
    data_type = fact_ctx["data_type"]

    if not rules:
        return value

    # Currency: ensure float
    if data_type == "currency" and isinstance(value, (int, float)):
        return float(value)

    # Percentage: if stored as decimal (0–1), convert to 0–100
    if "scale_to" in rules and rules["scale_to"] == "0-100":
        if isinstance(value, (int, float)) and 0.0 <= value <= 1.0:
            mult = rules.get("decimal_to_percent_multiplier", 100)
            return value * mult

    return value


def _pick_by_priority(candidates: list[dict], priority: list[str]) -> dict:
    for doc_type in priority:
        for c in candidates:
            if c.get("source_type") == doc_type:
                return c
    # fallback: highest confidence
    return max(candidates, key=lambda c: c.get("confidence", 0.0))
