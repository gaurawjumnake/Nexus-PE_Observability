"""
Registry Writer
================
Core methods for creating new KPI and fact entries.

Each function builds a complete, YAML-compatible dict from the caller's input
(applying sensible defaults for optional fields) and then delegates to
RegistryService, which writes the YAML file and syncs SQLite in one call.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Optional

if TYPE_CHECKING:
    from backend.kpi_extractor.app.registry.registry_service import RegistryService


# ---------------------------------------------------------------------------
# Template builders – produce the full dict shape expected by registry YAML
# ---------------------------------------------------------------------------

def build_fact_template(
    fact_id: str,
    name: str,
    category: str,
    description: str,
    data_type: str,
    *,
    business_definition: str = "",
    unit: str = "",
    fact_type: str = "raw",
    source_priority: Optional[list[str]] = None,
    document_sources: Optional[list[str]] = None,
    possible_aliases: Optional[list[str]] = None,
    extraction_patterns: Optional[list[str]] = None,
    validation_rules: Optional[dict] = None,
    normalization_rules: Optional[dict] = None,
    aggregation_strategy: str = "latest_value",
    related_facts: Optional[list[str]] = None,
    used_by_kpis: Optional[list[str]] = None,
    example_values: Optional[list[str]] = None,
    confidence_rules: Optional[dict] = None,
    missing_value_strategy: str = "mark_unavailable",
    status: str = "active",
) -> dict:
    """Return a fully-formed fact dict matching the registry/facts/*.yaml schema."""
    return {
        "fact_id": fact_id,
        "name": name,
        "category": category,
        "description": description,
        "business_definition": business_definition,
        "data_type": data_type,
        "unit": unit,
        "fact_type": fact_type,
        "source_priority": source_priority or [],
        "document_sources": document_sources or [],
        "possible_aliases": possible_aliases or [],
        "extraction_patterns": extraction_patterns or [],
        "validation_rules": validation_rules or {"required_type": data_type},
        "normalization_rules": normalization_rules or {},
        "aggregation_strategy": aggregation_strategy,
        "related_facts": related_facts or [],
        "used_by_kpis": used_by_kpis or [],
        "example_values": example_values or [],
        "confidence_rules": confidence_rules or {
            "minimum_confidence": 0.8,
            "auto_approve": 0.95,
            "manual_review_below": 0.8,
        },
        "missing_value_strategy": missing_value_strategy,
        "status": status,
    }


def build_kpi_template(
    kpi_id: str,
    name: str,
    category: str,
    description: str,
    formula: str,
    *,
    tier: str = "operational",
    business_value: str = "",
    unit: str = "count",
    aggregation: str = "sum",
    frequency: str = "monthly",
    required_facts: Optional[list[str]] = None,
    derived_facts: Optional[list[str]] = None,
    required_documents: Optional[list[str]] = None,
    dependencies: Optional[list[str]] = None,
    benchmarking: Optional[dict] = None,
    thresholds: Optional[dict] = None,
    data_quality: Optional[dict] = None,
    missing_data_strategy: str = "use_last_known",
    owner_persona: Optional[list[str]] = None,
    dashboard_visibility: Optional[list[str]] = None,
    example_calculation: str = "",
    status: str = "active",
) -> dict:
    """Return a fully-formed KPI dict matching the registry/kpis/*.yaml schema."""
    return {
        "name": name,
        "category": category,
        "tier": tier,
        "description": description,
        "business_value": business_value,
        "formula": formula,
        "unit": unit,
        "aggregation": aggregation,
        "frequency": frequency,
        "required_facts": required_facts or [],
        "derived_facts": derived_facts or [],
        "required_documents": required_documents or [],
        "dependencies": dependencies or [],
        "benchmarking": benchmarking or {
            "enabled": False,
            "benchmark_type": None,
            "benchmark_source": None,
        },
        "thresholds": thresholds or {
            "excellent": None,
            "good": None,
            "warning": None,
            "critical": None,
        },
        "data_quality": data_quality or {
            "minimum_coverage": 80.0,
            "confidence_threshold": 0.8,
        },
        "missing_data_strategy": missing_data_strategy,
        "owner_persona": owner_persona or [],
        "dashboard_visibility": dashboard_visibility or [],
        "example_calculation": example_calculation,
        "status": status,
        "kpi_id": kpi_id,
    }


# ---------------------------------------------------------------------------
# Write helpers – build + persist in one call
# ---------------------------------------------------------------------------

def add_fact(
    registry: "RegistryService",
    fact_data: dict,
    overwrite: bool = False,
) -> dict:
    """
    Build a complete fact dict from *fact_data* (filling defaults where
    fields are absent) and persist it via RegistryService.

    *fact_data* must contain at minimum: fact_id, name, category, description,
    data_type.  All other fields default to safe empty/template values.

    Returns the dict produced by RegistryService.add_fact().
    """
    fact = build_fact_template(
        fact_id=fact_data["fact_id"],
        name=fact_data["name"],
        category=fact_data["category"],
        description=fact_data["description"],
        data_type=fact_data["data_type"],
        business_definition=fact_data.get("business_definition", ""),
        unit=fact_data.get("unit", ""),
        fact_type=fact_data.get("fact_type", "raw"),
        source_priority=fact_data.get("source_priority"),
        document_sources=fact_data.get("document_sources"),
        possible_aliases=fact_data.get("possible_aliases"),
        extraction_patterns=fact_data.get("extraction_patterns"),
        validation_rules=fact_data.get("validation_rules"),
        normalization_rules=fact_data.get("normalization_rules"),
        aggregation_strategy=fact_data.get("aggregation_strategy", "latest_value"),
        related_facts=fact_data.get("related_facts"),
        used_by_kpis=fact_data.get("used_by_kpis"),
        example_values=fact_data.get("example_values"),
        confidence_rules=fact_data.get("confidence_rules"),
        missing_value_strategy=fact_data.get("missing_value_strategy", "mark_unavailable"),
        status=fact_data.get("status", "active"),
    )
    return registry.add_fact(fact, overwrite=overwrite)


def add_kpi(
    registry: "RegistryService",
    kpi_data: dict,
    overwrite: bool = False,
) -> dict:
    """
    Build a complete KPI dict from *kpi_data* (filling defaults where
    fields are absent) and persist it via RegistryService.

    *kpi_data* must contain at minimum: kpi_id, name, category, description,
    formula.  All other fields default to safe empty/template values.

    Returns the dict produced by RegistryService.add_kpi().
    """
    kpi = build_kpi_template(
        kpi_id=kpi_data["kpi_id"],
        name=kpi_data["name"],
        category=kpi_data["category"],
        description=kpi_data["description"],
        formula=kpi_data["formula"],
        tier=kpi_data.get("tier", "operational"),
        business_value=kpi_data.get("business_value", ""),
        unit=kpi_data.get("unit", "count"),
        aggregation=kpi_data.get("aggregation", "sum"),
        frequency=kpi_data.get("frequency", "monthly"),
        required_facts=kpi_data.get("required_facts"),
        derived_facts=kpi_data.get("derived_facts"),
        required_documents=kpi_data.get("required_documents"),
        dependencies=kpi_data.get("dependencies"),
        benchmarking=kpi_data.get("benchmarking"),
        thresholds=kpi_data.get("thresholds"),
        data_quality=kpi_data.get("data_quality"),
        missing_data_strategy=kpi_data.get("missing_data_strategy", "use_last_known"),
        owner_persona=kpi_data.get("owner_persona"),
        dashboard_visibility=kpi_data.get("dashboard_visibility"),
        example_calculation=kpi_data.get("example_calculation", ""),
        status=kpi_data.get("status", "active"),
    )
    return registry.add_kpi(kpi, overwrite=overwrite)
