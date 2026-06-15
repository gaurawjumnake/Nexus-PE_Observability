"""
Fact Validation Engine: Rule-based again

- Validates data type & range against fact.validation_rules
- Normalizes values (currency scale, percentage scale, units) via
  fact.normalization_rules
- When multiple extractions exist for the same fact/company/period,
  resolves conflicts using fact.source_priority
- Produces lineage record ready for the Fact Store
"""

from typing import Dict, List, Optional
from backend.utilites.registry_loader import FactDef


def validate_and_normalize(extraction: Dict, fact: FactDef) -> Optional[Dict]:
    """
    extraction: output of extract_fact() - {"fact_id","value","confidence",
                "source_chunk_id","document_id","document_type","company_id"}
    fact: FactDef

    Returns the extraction dict augmented with a normalized value, or None
    if the value fails validation (caller should then apply
    fact.missing_value_strategy).
    """
    value = extraction["value"]
    rules = fact.validation_rules

    # ---- type / range validation ----
    if not _validate_type(value, fact.data_type, rules):
        return None

    if fact.data_type in ("integer", "float", "currency", "percentage"):
        numeric = float(value)
        if "min" in rules and numeric < rules["min"]:
            return None
        if "max" in rules and numeric > rules["max"]:
            return None

    if fact.data_type == "enum":
        allowed = rules.get("allowed_values", [])
        if allowed and value not in allowed:
            return None

    # ---- normalization ----
    normalized_value = _normalize(value, fact)

    extraction["value"] = normalized_value
    return extraction


def _validate_type(value, data_type: str, rules: Dict) -> bool:
    if value is None:
        return False
    if data_type in ("integer",):
        return isinstance(value, (int, float)) and float(value).is_integer()
    if data_type in ("float", "currency", "percentage"):
        return isinstance(value, (int, float))
    if data_type == "boolean":
        return isinstance(value, bool)
    if data_type == "date":
        return isinstance(value, str)
    if data_type in ("string", "enum"):
        return isinstance(value, str)
    return True


def _normalize(value, fact: FactDef):
    rules = fact.normalization_rules
    if not rules:
        return value

    if fact.data_type == "currency" and isinstance(value, (int, float)):
        return float(value)

    if "scale_to" in rules and rules["scale_to"] == "0-100" and isinstance(value, (int, float)):
        if 0 <= value <= 1:
            return value * rules.get("decimal_to_percent_multiplier", 100)
        return value

    return value


def resolve_by_source_priority(extractions: List[Dict], fact: FactDef) -> Optional[Dict]:
    """
    Given multiple validated extractions for the SAME fact_id + company_id
    (from different document types), pick the winner using fact.source_priority.

    Example: financial=12M, board_deck=10M, source_priority=[financial, board_deck]
             -> financial (12M) wins.
    """
    if not extractions:
        return None
    if len(extractions) == 1:
        return extractions[0]

    priority = fact.source_priority
    for doc_type in priority:
        for ext in extractions:
            if ext["document_type"] == doc_type:
                return ext

    # fallback: highest confidence
    return max(extractions, key=lambda e: e.get("confidence", 0.0))
