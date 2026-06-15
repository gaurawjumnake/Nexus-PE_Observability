"""
Dependency Resolver: 
Reads KPI Registry dependencies (from RegistryCache) and checks which
required facts are available in the Fact Store for a given company.

Determines KPI "coverage" = % of required facts available.
"""

from typing import Dict, List
from backend.utilites.registry_loader import RegistryCache


def resolve_kpi_coverage(
    kpi_id: str,
    company_id: str,
    registry: RegistryCache,
    fact_store_lookup,
) -> Dict:
    """
    fact_store_lookup: callable(fact_id, company_id) -> bool
                        (True if a validated fact value exists)

    Returns:
        {
          "kpi_id": ...,
          "company_id": ...,
          "required_facts": [...],
          "available_facts": [...],
          "missing_facts": [...],
          "coverage": 0.0-1.0,
          "ready_to_calculate": bool
        }
    """
    required = registry.dependencies_for_kpi(kpi_id)
    available, missing = [], []

    for fact_id in required:
        if fact_store_lookup(fact_id, company_id):
            available.append(fact_id)
        else:
            missing.append(fact_id)

    coverage = len(available) / len(required) if required else 1.0
    min_coverage_pct = registry.kpis[kpi_id].data_quality.get("minimum_coverage", 100) / 100.0

    return {
        "kpi_id": kpi_id,
        "company_id": company_id,
        "required_facts": required,
        "available_facts": available,
        "missing_facts": missing,
        "coverage": coverage,
        "ready_to_calculate": coverage >= min_coverage_pct,
    }


def resolve_all_kpis(
    company_id: str,
    registry: RegistryCache,
    fact_store_lookup,
) -> List[Dict]:
    return [
        resolve_kpi_coverage(kpi_id, company_id, registry, fact_store_lookup)
        for kpi_id in registry.kpis
    ]
