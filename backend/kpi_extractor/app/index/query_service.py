"""
Registry Query Service
========================
Pure read functions against the SQLite Registry Index (data/registry.db).

This module is the implementation behind the MCP tool layer. It knows
about SQLite; the MCP tools and agents do not - they only call these
functions (or the MCP tool wrappers) and receive plain dicts.

No agent / MCP tool should import sqlite3 directly - only this module does.
"""

import os
import json
import sqlite3
from typing import Optional
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent
DB_PATH = BASE_DIR.parent / "db" / "registry.db"

def _connect():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def _row_to_dict(row) -> dict:
    return {k: row[k] for k in row.keys()}


# ------------------------------------------------------------------
# get_fact_context(fact_name)
# ------------------------------------------------------------------
def get_fact_context(fact_id: str) -> Optional[dict]:
    """
    Returns the full retrieval-relevant context for one fact:
    definition, data type, validation/normalization rules, aliases,
    extraction patterns, document sources + source priority,
    related facts, and which KPIs use it.
    """
    conn = _connect()
    try:
        cur = conn.cursor()
        fact = cur.execute("SELECT * FROM facts WHERE fact_id=?", (fact_id,)).fetchone()
        if not fact:
            return None

        fact_dict = _row_to_dict(fact)
        for json_field in ("validation_rules", "normalization_rules", "confidence_rules", "example_values"):
            fact_dict[json_field] = json.loads(fact_dict[json_field] or "null")

        fact_dict["aliases"] = [r["alias"] for r in cur.execute(
            "SELECT alias FROM fact_aliases WHERE fact_id=?", (fact_id,)
        ).fetchall()]

        fact_dict["extraction_patterns"] = [r["pattern"] for r in cur.execute(
            "SELECT pattern FROM fact_extraction_patterns WHERE fact_id=?", (fact_id,)
        ).fetchall()]

        fact_dict["document_sources"] = [
            {"document_type": r["document_type"], "priority_rank": r["priority_rank"]}
            for r in cur.execute(
                "SELECT document_type, priority_rank FROM fact_document_sources WHERE fact_id=? ORDER BY priority_rank",
                (fact_id,)
            ).fetchall()
        ]
        fact_dict["source_priority"] = [d["document_type"] for d in fact_dict["document_sources"]]

        fact_dict["related_facts"] = [r["related_fact_id"] for r in cur.execute(
            "SELECT related_fact_id FROM fact_related WHERE fact_id=?", (fact_id,)
        ).fetchall()]

        fact_dict["used_by_kpis"] = [r["kpi_id"] for r in cur.execute(
            "SELECT kpi_id FROM kpi_required_facts WHERE fact_id=? "
            "UNION SELECT kpi_id FROM kpi_derived_facts WHERE fact_id=?",
            (fact_id, fact_id)
        ).fetchall()]

        return fact_dict
    finally:
        conn.close()


# ------------------------------------------------------------------
# get_kpi_context(kpi_name)
# ------------------------------------------------------------------
def get_kpi_context(kpi_id: str) -> Optional[dict]:
    """
    Returns KPI definition: formula, required/derived facts, required
    documents, thresholds, data quality, benchmarking config.
    """
    conn = _connect()
    try:
        cur = conn.cursor()
        kpi = cur.execute("SELECT * FROM kpis WHERE kpi_id=?", (kpi_id,)).fetchone()
        if not kpi:
            return None

        kpi_dict = _row_to_dict(kpi)
        for json_field in ("thresholds", "data_quality", "benchmarking"):
            kpi_dict[json_field] = json.loads(kpi_dict[json_field] or "null")

        kpi_dict["required_facts"] = [r["fact_id"] for r in cur.execute(
            "SELECT fact_id FROM kpi_required_facts WHERE kpi_id=?", (kpi_id,)
        ).fetchall()]

        kpi_dict["derived_facts"] = [r["fact_id"] for r in cur.execute(
            "SELECT fact_id FROM kpi_derived_facts WHERE kpi_id=?", (kpi_id,)
        ).fetchall()]

        kpi_dict["required_documents"] = [r["document_type"] for r in cur.execute(
            "SELECT document_type FROM kpi_required_documents WHERE kpi_id=?", (kpi_id,)
        ).fetchall()]

        return kpi_dict
    finally:
        conn.close()


# ------------------------------------------------------------------
# retrieve_formula_context(kpi_name)
# ------------------------------------------------------------------
def retrieve_formula_context(kpi_id: str) -> Optional[dict]:
    """
    Minimal context for the KPI Calculation Engine: just the formula
    and the fact_ids it references (required + derived), with each
    fact's data_type and unit for type-safe formula evaluation.
    """
    conn = _connect()
    try:
        cur = conn.cursor()
        kpi = cur.execute(
            "SELECT kpi_id, formula, unit, aggregation FROM kpis WHERE kpi_id=?", (kpi_id,)
        ).fetchone()
        if not kpi:
            return None

        fact_ids = [r["fact_id"] for r in cur.execute(
            "SELECT fact_id FROM kpi_required_facts WHERE kpi_id=? "
            "UNION SELECT fact_id FROM kpi_derived_facts WHERE kpi_id=?",
            (kpi_id, kpi_id)
        ).fetchall()]

        facts = {}
        for fid in fact_ids:
            f = cur.execute("SELECT fact_id, data_type, unit FROM facts WHERE fact_id=?", (fid,)).fetchone()
            if f:
                facts[fid] = {"data_type": f["data_type"], "unit": f["unit"]}

        return {
            "kpi_id": kpi["kpi_id"],
            "formula": kpi["formula"],
            "unit": kpi["unit"],
            "aggregation": kpi["aggregation"],
            "facts": facts,
        }
    finally:
        conn.close()


# ------------------------------------------------------------------
# discover_relevant_facts(document_type)
# ------------------------------------------------------------------
def discover_relevant_facts(document_type: str) -> list[dict]:
    """
    Returns lightweight fact context for every fact whose document_sources
    includes this document_type - used by the Fact Extraction Agent to
    scope its search WITHOUT loading the entire fact registry.

    Each entry includes only what's needed for candidate-chunk search:
    fact_id, name, data_type, aliases, extraction_patterns.
    """
    conn = _connect()
    try:
        cur = conn.cursor()
        fact_ids = [r["fact_id"] for r in cur.execute(
            "SELECT fact_id FROM fact_document_sources WHERE document_type=?", (document_type,)
        ).fetchall()]

        results = []
        for fid in fact_ids:
            f = cur.execute(
                "SELECT fact_id, name, data_type, unit, category FROM facts WHERE fact_id=?", (fid,)
            ).fetchone()
            aliases = [r["alias"] for r in cur.execute(
                "SELECT alias FROM fact_aliases WHERE fact_id=?", (fid,)
            ).fetchall()]
            patterns = [r["pattern"] for r in cur.execute(
                "SELECT pattern FROM fact_extraction_patterns WHERE fact_id=?", (fid,)
            ).fetchall()]
            results.append({
                "fact_id": f["fact_id"],
                "name": f["name"],
                "category": f["category"],
                "data_type": f["data_type"],
                "unit": f["unit"],
                "aliases": aliases,
                "extraction_patterns": patterns,
            })
        return results
    finally:
        conn.close()


# ------------------------------------------------------------------
# search_registry(query) / search_related_registry_content(query)
# ------------------------------------------------------------------
def search_registry(query: str, limit: int = 10) -> list[dict]:
    """
    Full-text search across fact + KPI names/descriptions/aliases/formulas.
    Returns lightweight hits: entity_type, entity_id, name.
    """
    conn = _connect()
    try:
        cur = conn.cursor()
        fts_query = _to_fts_query(query)
        rows = cur.execute(
            "SELECT entity_type, entity_id, name FROM registry_fts "
            "WHERE registry_fts MATCH ? LIMIT ?",
            (fts_query, limit)
        ).fetchall()
        return [_row_to_dict(r) for r in rows]
    finally:
        conn.close()


def search_related_registry_content(query: str, limit: int = 10) -> list[dict]:
    """
    Like search_registry, but additionally expands each hit with its
    direct relationships (related_facts for facts, required_facts for
    KPIs) - used when an agent needs surrounding context, not just the
    matched entity.
    """
    hits = search_registry(query, limit)
    conn = _connect()
    try:
        cur = conn.cursor()
        for hit in hits:
            if hit["entity_type"] == "fact":
                hit["related"] = [r["related_fact_id"] for r in cur.execute(
                    "SELECT related_fact_id FROM fact_related WHERE fact_id=?", (hit["entity_id"],)
                ).fetchall()]
            else:
                hit["related"] = [r["fact_id"] for r in cur.execute(
                    "SELECT fact_id FROM kpi_required_facts WHERE kpi_id=?", (hit["entity_id"],)
                ).fetchall()]
        return hits
    finally:
        conn.close()


def _to_fts_query(query: str) -> str:
    """Turn a free-text query into an FTS5 prefix-match expression."""
    terms = [t.strip() for t in query.split() if t.strip()]
    return " ".join(f"{t}*" for t in terms) if terms else query
