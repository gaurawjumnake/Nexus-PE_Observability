"""
Registry Index Builder
========================
Reads /registry/facts/*.yaml and /registry/kpis/*.yaml and builds (or
rebuilds) the SQLite Registry Index at data/registry.db.

This is the ONLY component that reads the raw YAML files. Everything
downstream (MCP tools, agents) queries the SQLite index instead.

Run:
    python -m app.index.index_builder
"""

import os
import json
import sqlite3
import time
from pathlib import Path
import yaml
from backend.utilites.app_logger import Logger
log = Logger()

from backend.config import FACTS_DIR, KPIS_DIR, REGISTRY_SCHEMA_PATH, REGISTRY_DB_PATH

FACTS_DIR   = str(FACTS_DIR)
KPIS_DIR    = str(KPIS_DIR)
SCHEMA_PATH = str(REGISTRY_SCHEMA_PATH)
DB_PATH     = str(REGISTRY_DB_PATH)


def build_index(db_path: str = DB_PATH):
    # os.makedirs(os.path.dirname(db_path), exist_ok=True)
    # if os.path.exists(db_path):
    #     os.remove(db_path)
    
    conn = sqlite3.connect(db_path)
    conn.executescript(open(SCHEMA_PATH).read())

    related_pairs = _load_facts(conn)     # pass 1: facts (returns deferred pairs)
    _load_kpis(conn)                      # pass 2: kpis
    _load_relationships(conn, related_pairs)  # pass 3: resolve fact↔fact and fact↔kpi
    _build_fts(conn)
    _set_meta(conn)

    conn.commit()
    conn.close()
    log.log_info(f"Registry index built at {db_path}")


# ------------------------------------------------------------------
def _load_facts(conn) -> list[tuple[str, str]]:
    """
    Inserts all facts, aliases, extraction patterns, and document sources.
    Defers related_facts inserts — returns them as pairs for _load_relationships.
    """
    cur = conn.cursor()
    related_pairs = []

    for fname in sorted(os.listdir(FACTS_DIR)):
        if not fname.endswith(".yaml"):
            continue
        with open(os.path.join(FACTS_DIR, fname), "r", encoding="utf-8") as f:
            d = yaml.safe_load(f)

        cur.execute("""
            INSERT INTO facts (
                fact_id, name, category, description, business_definition,
                data_type, unit, fact_type, aggregation_strategy,
                missing_value_strategy, status, validation_rules,
                normalization_rules, confidence_rules, example_values
            ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
        """, (
            d["fact_id"], d["name"], d["category"], d.get("description"),
            d.get("business_definition"), d["data_type"], d.get("unit"),
            d["fact_type"], d.get("aggregation_strategy"),
            d.get("missing_value_strategy"), d.get("status", "active"),
            json.dumps(d.get("validation_rules", {})),
            json.dumps(d.get("normalization_rules", {})),
            json.dumps(d.get("confidence_rules", {})),
            json.dumps(d.get("example_values", [])),
        ))

        for alias in d.get("possible_aliases", []):
            cur.execute(
                "INSERT OR IGNORE INTO fact_aliases (fact_id, alias) VALUES (?,?)",
                (d["fact_id"], alias.lower())
            )

        for pattern in d.get("extraction_patterns", []):
            cur.execute(
                "INSERT OR IGNORE INTO fact_extraction_patterns (fact_id, pattern) VALUES (?,?)",
                (d["fact_id"], pattern.lower())
            )

        existing = set(d.get("source_priority", []))
        for rank, doc_type in enumerate(d.get("source_priority", [])):
            cur.execute("""
                INSERT OR IGNORE INTO fact_document_sources (fact_id, document_type, priority_rank)
                VALUES (?,?,?)
            """, (d["fact_id"], doc_type, rank))

        next_rank = len(existing)
        for doc_type in d.get("document_sources", []):
            if doc_type not in existing:
                cur.execute("""
                    INSERT OR IGNORE INTO fact_document_sources (fact_id, document_type, priority_rank)
                    VALUES (?,?,?)
                """, (d["fact_id"], doc_type, next_rank))
                next_rank += 1

        # defer — related target may not be loaded yet, or may be a KPI
        for related in d.get("related_facts", []):
            related_pairs.append((d["fact_id"], related))

    return related_pairs


# ------------------------------------------------------------------
def _load_kpis(conn):
    cur = conn.cursor()
    for fname in sorted(os.listdir(KPIS_DIR)):
        if not fname.endswith(".yaml"):
            continue
        with open(os.path.join(KPIS_DIR, fname), "rb") as f:
            d = yaml.safe_load(f.read().decode("utf-8", errors="replace"))

        cur.execute("""
            INSERT INTO kpis (
                kpi_id, name, category, tier, description, business_value,
                formula, unit, aggregation, frequency, missing_data_strategy,
                status, thresholds, data_quality, benchmarking, example_calculation
            ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
        """, (
            d["kpi_id"], d["name"], d.get("category"), d.get("tier"),
            d.get("description"), d.get("business_value"), d.get("formula"),
            d.get("unit"), d.get("aggregation"), d.get("frequency"),
            d.get("missing_data_strategy"), d.get("status", "active"),
            json.dumps(d.get("thresholds", {})),
            json.dumps(d.get("data_quality", {})),
            json.dumps(d.get("benchmarking", {})),
            d.get("example_calculation"),
        ))

        for fact_id in d.get("required_facts", []):
            cur.execute(
                "INSERT OR IGNORE INTO kpi_required_facts (kpi_id, fact_id) VALUES (?,?)",
                (d["kpi_id"], fact_id)
            )

        for fact_id in (d.get("derived_facts") or []):
            cur.execute(
                "INSERT OR IGNORE INTO kpi_derived_facts (kpi_id, fact_id) VALUES (?,?)",
                (d["kpi_id"], fact_id)
            )

        for doc_type in d.get("required_documents", []):
            cur.execute(
                "INSERT OR IGNORE INTO kpi_required_documents (kpi_id, document_type) VALUES (?,?)",
                (d["kpi_id"], doc_type)
            )


# ------------------------------------------------------------------
def _load_relationships(conn, related_pairs: list[tuple[str, str]]):
    """
    Resolves deferred related_facts pairs collected from fact YAMLs.
    Routes each pair to the correct table:
      - related_id is a fact  → fact_related
      - related_id is a KPI   → fact_kpi_related
      - neither               → warning (genuinely missing from registry)
    """
    cur = conn.cursor()
    known_facts = {r[0] for r in cur.execute("SELECT fact_id FROM facts").fetchall()}
    known_kpis  = {r[0] for r in cur.execute("SELECT kpi_id FROM kpis").fetchall()}

    skipped = []
    for fact_id, related_id in related_pairs:
        if related_id in known_facts:
            cur.execute(
                "INSERT OR IGNORE INTO fact_related (fact_id, related_fact_id) VALUES (?,?)",
                (fact_id, related_id)
            )
        elif related_id in known_kpis:
            cur.execute(
                "INSERT OR IGNORE INTO fact_kpi_related (fact_id, kpi_id) VALUES (?,?)",
                (fact_id, related_id)
            )
        else:
            skipped.append((fact_id, related_id))

    for fact_id, related_id in skipped:
        log.log_warning(f"⚠️  Unknown related id '{related_id}' for fact '{fact_id}' — not in facts or kpis")


# ------------------------------------------------------------------
def _build_fts(conn):
    outer = conn.cursor()
    inner = conn.cursor()

    for row in outer.execute(
        "SELECT fact_id, name, description, business_definition FROM facts"
    ).fetchall():
        fact_id, name, desc, biz_def = row
        aliases = [r[0] for r in inner.execute(
            "SELECT alias FROM fact_aliases WHERE fact_id=?", (fact_id,)
        ).fetchall()]
        text = " ".join(filter(None, [desc, biz_def, " ".join(aliases)]))
        inner.execute(
            "INSERT INTO registry_fts (entity_type, entity_id, name, text) VALUES (?,?,?,?)",
            ("fact", fact_id, name, text)
        )

    for row in outer.execute(
        "SELECT kpi_id, name, description, business_value, formula FROM kpis"
    ).fetchall():
        kpi_id, name, desc, biz_val, formula = row
        text = " ".join(filter(None, [desc, biz_val, formula]))
        inner.execute(
            "INSERT INTO registry_fts (entity_type, entity_id, name, text) VALUES (?,?,?,?)",
            ("kpi", kpi_id, name, text)
        )


# ------------------------------------------------------------------
def _set_meta(conn):
    cur = conn.cursor()
    cur.execute("INSERT OR REPLACE INTO registry_meta (key, value) VALUES (?,?)",
                ("last_built_at", str(int(time.time()))))
    fact_count = cur.execute("SELECT COUNT(*) FROM facts").fetchone()[0]
    kpi_count  = cur.execute("SELECT COUNT(*) FROM kpis").fetchone()[0]
    cur.execute("INSERT OR REPLACE INTO registry_meta (key, value) VALUES (?,?)",
                ("fact_count", str(fact_count)))
    cur.execute("INSERT OR REPLACE INTO registry_meta (key, value) VALUES (?,?)",
                ("kpi_count", str(kpi_count)))
    log.log_info(f"facts: {fact_count}, kpis: {kpi_count}")


# if __name__ == "__main__":
#     build_index()