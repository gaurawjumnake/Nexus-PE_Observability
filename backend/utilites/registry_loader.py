"""
Registry Loader
================
Loads Fact Registry + KPI Registry YAML files ONCE at application startup
and compiles them into in-memory lookup structures (RegistryCache).

No agent ever re-reads YAML files at runtime - they only query RegistryCache.
"""

import os
import yaml
from dataclasses import dataclass, field
from typing import Dict, List, Set


FACTS_DIR = os.path.join(os.path.dirname(__file__), "..", "..", "registry", "facts")
KPIS_DIR = os.path.join(os.path.dirname(__file__), "..", "..", "registry", "kpis")


@dataclass
class FactDef:
    fact_id: str
    name: str
    category: str
    data_type: str
    unit: str
    fact_type: str
    source_priority: List[str]
    document_sources: List[str]
    possible_aliases: List[str]
    extraction_patterns: List[str]
    validation_rules: dict
    normalization_rules: dict
    aggregation_strategy: str
    related_facts: List[str]
    used_by_kpis: List[str]
    confidence_rules: dict
    missing_value_strategy: str
    status: str


@dataclass
class KPIDef:
    kpi_id: str
    name: str
    category: str
    formula: str
    required_facts: List[str]
    derived_facts: List[str]
    required_documents: List[str]
    aggregation: str
    thresholds: dict
    data_quality: dict
    missing_data_strategy: str
    status: str


class RegistryCache:
    """
    Compiled, in-memory registry. Built once at startup via load_registries().
    """

    def __init__(self):
        self.facts: Dict[str, FactDef] = {}
        self.kpis: Dict[str, KPIDef] = {}

        # facts grouped by category, e.g. {"revenue": ["direct_ai_revenue", ...]}
        self.facts_by_category: Dict[str, List[str]] = {}

        # facts grouped by document_source, e.g. {"financial": ["direct_ai_revenue", ...]}
        self.facts_by_document_type: Dict[str, List[str]] = {}

        # kpi_id -> list of fact_ids required (required_facts + derived_facts)
        self.kpi_dependencies: Dict[str, List[str]] = {}

        # fact_id -> list of kpi_ids that use it (reverse index)
        self.fact_to_kpis: Dict[str, List[str]] = {}

    # ------------------------------------------------------------------
    def load(self):
        self._load_facts()
        self._load_kpis()
        self._build_indexes()
        return self

    def _load_facts(self):
        for fname in os.listdir(FACTS_DIR):
            if not fname.endswith(".yaml"):
                continue
            with open(os.path.join(FACTS_DIR, fname), "r", encoding="utf-8") as f:
                raw = yaml.safe_load(f)
            fact = FactDef(
                fact_id=raw["fact_id"],
                name=raw["name"],
                category=raw["category"],
                data_type=raw["data_type"],
                unit=raw["unit"],
                fact_type=raw["fact_type"],
                source_priority=raw.get("source_priority", []),
                document_sources=raw.get("document_sources", []),
                possible_aliases=raw.get("possible_aliases", []),
                extraction_patterns=raw.get("extraction_patterns", []),
                validation_rules=raw.get("validation_rules", {}),
                normalization_rules=raw.get("normalization_rules", {}),
                aggregation_strategy=raw.get("aggregation_strategy", "latest_value"),
                related_facts=raw.get("related_facts", []),
                used_by_kpis=raw.get("used_by_kpis", []),
                confidence_rules=raw.get("confidence_rules", {}),
                missing_value_strategy=raw.get("missing_value_strategy", "ask_user"),
                status=raw.get("status", "active"),
            )
            self.facts[fact.fact_id] = fact

    def _load_kpis(self):
        for fname in os.listdir(KPIS_DIR):
            if not fname.endswith(".yaml"):
                continue
            with open(os.path.join(KPIS_DIR, fname), "rb") as f:
                raw = yaml.safe_load(f.read().decode("utf-8", errors="replace"))
            kpi = KPIDef(
                kpi_id=raw["kpi_id"],
                name=raw["name"],
                category=raw.get("category", ""),
                formula=raw.get("formula", ""),
                required_facts=raw.get("required_facts", []),
                derived_facts=raw.get("derived_facts", []) or [],
                required_documents=raw.get("required_documents", []),
                aggregation=raw.get("aggregation", ""),
                thresholds=raw.get("thresholds", {}),
                data_quality=raw.get("data_quality", {}),
                missing_data_strategy=raw.get("missing_data_strategy", "ask_user"),
                status=raw.get("status", "active"),
            )
            self.kpis[kpi.kpi_id] = kpi

    def _build_indexes(self):
        # facts by category
        for fact in self.facts.values():
            self.facts_by_category.setdefault(fact.category, []).append(fact.fact_id)

        # facts by document source
        for fact in self.facts.values():
            for doc_type in fact.document_sources:
                self.facts_by_document_type.setdefault(doc_type, []).append(fact.fact_id)

        # kpi -> dependencies (union of required + derived facts)
        for kpi in self.kpis.values():
            deps = sorted(set(kpi.required_facts) | set(kpi.derived_facts))
            self.kpi_dependencies[kpi.kpi_id] = deps

        # reverse index: fact -> kpis
        for kpi_id, deps in self.kpi_dependencies.items():
            for fact_id in deps:
                self.fact_to_kpis.setdefault(fact_id, []).append(kpi_id)

    # ------------------------------------------------------------------
    def facts_for_document_type(self, document_type: str) -> List[FactDef]:
        """Used by Fact Candidate Finder: only facts relevant to this doc type."""
        fact_ids = self.facts_by_document_type.get(document_type, [])
        return [self.facts[fid] for fid in fact_ids]

    def dependencies_for_kpi(self, kpi_id: str) -> List[str]:
        return self.kpi_dependencies.get(kpi_id, [])

    def kpis_using_fact(self, fact_id: str) -> List[str]:
        return self.fact_to_kpis.get(fact_id, [])


# Singleton, populated once via load_registries()
_registry_cache: RegistryCache | None = None


def load_registries() -> RegistryCache:
    global _registry_cache
    if _registry_cache is None:
        _registry_cache = RegistryCache().load()
    return _registry_cache


def get_registry() -> RegistryCache:
    if _registry_cache is None:
        raise RuntimeError("Registries not loaded. Call load_registries() at startup.")
    return _registry_cache
