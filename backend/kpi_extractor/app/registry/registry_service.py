"""
Registry Service
==================
Single in-process module for all registry reads + writes (facts & KPIs).

Replaces the old three-file split (app/mcp/server.py, app/mcp/client.py,
app/index/query_service.py). There is no LLM tool-calling boundary here -
agents and engines are plain Python calling plain Python - so the old
subprocess + JSON-RPC indirection bought nothing but latency and a process
to babysit. This module is a drop-in: same method names, same return
shapes, called directly instead of through RegistryMCPClient.

    from backend.kpi_extractor.app.registry.registry_service import RegistryService
    registry = RegistryService()          # no .start()/.stop(), no subprocess
    facts = registry.discover_relevant_facts("financial")

Source of truth: registry/facts/*.yaml and registry/kpis/*.yaml.
SQLite (db/registry.db) is a derived, queryable index of those YAML files.
add_fact/add_kpi/remove_fact/remove_kpi keep both in sync so a future
`python -m app.registry.service --rebuild` never silently undoes an
admin-panel change.
"""

import json
import os
import sqlite3
import threading
import time
from pathlib import Path
from typing import Optional

import yaml

from backend.config import FACTS_DIR, KPIS_DIR, REGISTRY_SCHEMA_PATH, REGISTRY_DB_PATH

SCHEMA_PATH = REGISTRY_SCHEMA_PATH
DB_PATH = REGISTRY_DB_PATH


class DependencyError(Exception):
    """Raised when removing a fact/KPI would orphan something that depends on it."""
    def __init__(self, message: str, dependents: list[str]):
        super().__init__(message)
        self.dependents = dependents


def _load_yaml(path: Path) -> dict:
    """
    yaml.safe_load over a file, tolerant of stray non-UTF-8 bytes (a few
    registry YAML files were saved with a Windows-1252 dash/quote
    character mixed into otherwise UTF-8 text - errors='replace' swaps
    that single byte for U+FFFD instead of crashing the whole load).
    """
    with open(path, "rb") as f:
        text = f.read().decode("utf-8", errors="replace")
    return yaml.safe_load(text)


class RegistryService:
    """
    Thread-safe wrapper around the registry SQLite index. One short-lived
    connection per call (SQLite handles this cheaply); WAL mode lets reads
    and writes coexist without blocking each other.
    """

    def __init__(self, db_path: Path = DB_PATH):
        self.db_path = db_path
        self._init_lock = threading.Lock()
        self._ensure_db()

    def _ensure_db(self):
        with self._init_lock:
            os.makedirs(self.db_path.parent, exist_ok=True)
            conn = sqlite3.connect(self.db_path)
            try:
                conn.execute("PRAGMA journal_mode=WAL")
                already_initialized = conn.execute(
                    "SELECT name FROM sqlite_master WHERE type='table' AND name='facts'"
                ).fetchone() is not None
                if not already_initialized:
                    conn.executescript(SCHEMA_PATH.read_text())
                    conn.commit()
            finally:
                conn.close()

    def _connect(self):
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys=ON")
        return conn

    def is_populated(self) -> bool:
        """
        True if the registry index already has facts loaded. The DB *file*
        always exists after __init__ (schema is created on first connect),
        so checking file existence alone can't tell you whether YAML data
        has actually been loaded yet - use this instead.
        """
        conn = self._connect()
        try:
            return conn.execute("SELECT COUNT(*) FROM facts").fetchone()[0] > 0
        finally:
            conn.close()

    @staticmethod
    def _row(row) -> dict:
        return {k: row[k] for k in row.keys()}

    # ------------------------------------------------------------------
    # READS  (unchanged behavior from the old query_service.py)
    # ------------------------------------------------------------------
    def get_fact_context(self, fact_id: str) -> Optional[dict]:
        conn = self._connect()
        try:
            cur = conn.cursor()
            fact = cur.execute("SELECT * FROM facts WHERE fact_id=?", (fact_id,)).fetchone()
            if not fact:
                return None
            d = self._row(fact)
            for f in ("validation_rules", "normalization_rules", "confidence_rules", "example_values"):
                d[f] = json.loads(d[f] or "null")
            d["aliases"] = [r["alias"] for r in cur.execute(
                "SELECT alias FROM fact_aliases WHERE fact_id=?", (fact_id,))]
            d["extraction_patterns"] = [r["pattern"] for r in cur.execute(
                "SELECT pattern FROM fact_extraction_patterns WHERE fact_id=?", (fact_id,))]
            d["document_sources"] = [
                {"document_type": r["document_type"], "priority_rank": r["priority_rank"]}
                for r in cur.execute(
                    "SELECT document_type, priority_rank FROM fact_document_sources "
                    "WHERE fact_id=? ORDER BY priority_rank", (fact_id,))
            ]
            d["source_priority"] = [s["document_type"] for s in d["document_sources"]]
            d["related_facts"] = [r["related_fact_id"] for r in cur.execute(
                "SELECT related_fact_id FROM fact_related WHERE fact_id=?", (fact_id,))]
            d["used_by_kpis"] = [r["kpi_id"] for r in cur.execute(
                "SELECT kpi_id FROM kpi_required_facts WHERE fact_id=? "
                "UNION SELECT kpi_id FROM kpi_derived_facts WHERE fact_id=?", (fact_id, fact_id))]
            return d
        finally:
            conn.close()

    def get_kpi_context(self, kpi_id: str) -> Optional[dict]:
        conn = self._connect()
        try:
            cur = conn.cursor()
            kpi = cur.execute("SELECT * FROM kpis WHERE kpi_id=?", (kpi_id,)).fetchone()
            if not kpi:
                return None
            d = self._row(kpi)
            for f in ("thresholds", "data_quality", "benchmarking"):
                d[f] = json.loads(d[f] or "null")
            d["required_facts"] = [r["fact_id"] for r in cur.execute(
                "SELECT fact_id FROM kpi_required_facts WHERE kpi_id=?", (kpi_id,))]
            d["derived_facts"] = [r["fact_id"] for r in cur.execute(
                "SELECT fact_id FROM kpi_derived_facts WHERE kpi_id=?", (kpi_id,))]
            d["required_documents"] = [r["document_type"] for r in cur.execute(
                "SELECT document_type FROM kpi_required_documents WHERE kpi_id=?", (kpi_id,))]
            return d
        finally:
            conn.close()

    def retrieve_formula_context(self, kpi_id: str) -> Optional[dict]:
        conn = self._connect()
        try:
            cur = conn.cursor()
            kpi = cur.execute(
                "SELECT kpi_id, formula, unit, aggregation FROM kpis WHERE kpi_id=?", (kpi_id,)
            ).fetchone()
            if not kpi:
                return None
            fact_ids = [r["fact_id"] for r in cur.execute(
                "SELECT fact_id FROM kpi_required_facts WHERE kpi_id=? "
                "UNION SELECT fact_id FROM kpi_derived_facts WHERE kpi_id=?", (kpi_id, kpi_id))]
            facts = {}
            for fid in fact_ids:
                f = cur.execute("SELECT fact_id, data_type, unit FROM facts WHERE fact_id=?", (fid,)).fetchone()
                if f:
                    facts[fid] = {"data_type": f["data_type"], "unit": f["unit"]}
            return {"kpi_id": kpi["kpi_id"], "formula": kpi["formula"], "unit": kpi["unit"],
                    "aggregation": kpi["aggregation"], "facts": facts}
        finally:
            conn.close()

    def discover_relevant_facts(self, document_type: str) -> list[dict]:
        conn = self._connect()
        try:
            cur = conn.cursor()
            fact_ids = [r["fact_id"] for r in cur.execute(
                "SELECT fact_id FROM fact_document_sources WHERE document_type=?", (document_type,))]
            out = []
            for fid in fact_ids:
                f = cur.execute(
                    "SELECT fact_id, name, data_type, unit, category FROM facts WHERE fact_id=?", (fid,)
                ).fetchone()
                aliases = [r["alias"] for r in cur.execute(
                    "SELECT alias FROM fact_aliases WHERE fact_id=?", (fid,))]
                patterns = [r["pattern"] for r in cur.execute(
                    "SELECT pattern FROM fact_extraction_patterns WHERE fact_id=?", (fid,))]
                out.append({"fact_id": f["fact_id"], "name": f["name"], "category": f["category"],
                            "data_type": f["data_type"], "unit": f["unit"],
                            "aliases": aliases, "extraction_patterns": patterns})
            return out
        finally:
            conn.close()

    def search_registry(self, query: str, limit: int = 10) -> list[dict]:
        conn = self._connect()
        try:
            cur = conn.cursor()
            fts_query = self._to_fts_query(query)
            rows = cur.execute(
                "SELECT entity_type, entity_id, name FROM registry_fts "
                "WHERE registry_fts MATCH ? LIMIT ?", (fts_query, limit)
            ).fetchall()
            return [self._row(r) for r in rows]
        finally:
            conn.close()

    def search_related_registry_content(self, query: str, limit: int = 10) -> list[dict]:
        hits = self.search_registry(query, limit)
        conn = self._connect()
        try:
            cur = conn.cursor()
            for hit in hits:
                if hit["entity_type"] == "fact":
                    hit["related"] = [r["related_fact_id"] for r in cur.execute(
                        "SELECT related_fact_id FROM fact_related WHERE fact_id=?", (hit["entity_id"],))]
                else:
                    hit["related"] = [r["fact_id"] for r in cur.execute(
                        "SELECT fact_id FROM kpi_required_facts WHERE kpi_id=?", (hit["entity_id"],))]
            return hits
        finally:
            conn.close()

    @staticmethod
    def _to_fts_query(query: str) -> str:
        terms = [t.strip() for t in query.split() if t.strip()]
        return " ".join(f"{t}*" for t in terms) if terms else query

    # ------------------------------------------------------------------
    # WRITES  -  add / remove, syncing SQLite + YAML together
    # ------------------------------------------------------------------
    def add_fact(self, fact: dict, overwrite: bool = False) -> dict:
        """
        fact: same shape as a registry/facts/*.yaml file, e.g.:
            {
              "fact_id": "new_metric", "name": "...", "category": "...",
              "data_type": "currency", "fact_type": "raw",
              "possible_aliases": [...], "extraction_patterns": [...],
              "document_sources": ["financial"], "source_priority": ["financial"],
              "validation_rules": {...}, "related_facts": [...], ...
            }
        Writes registry/facts/<fact_id>.yaml, then re-syncs that one
        fact's rows into SQLite (no full rebuild needed).
        """
        fact_id = fact["fact_id"]
        path = FACTS_DIR / f"{fact_id}.yaml"
        if path.exists() and not overwrite:
            raise FileExistsError(f"Fact '{fact_id}' already exists. Pass overwrite=True to replace it.")

        FACTS_DIR.mkdir(parents=True, exist_ok=True)
        path.write_text(yaml.safe_dump(fact, sort_keys=False))

        conn = self._connect()
        try:
            self._upsert_fact_row(conn, fact)
            conn.commit()
        finally:
            conn.close()
        return {"fact_id": fact_id, "status": "added", "yaml_path": str(path)}

    def add_kpi(self, kpi: dict, overwrite: bool = False) -> dict:
        """Same idea as add_fact, for registry/kpis/<kpi_id>.yaml."""
        kpi_id = kpi["kpi_id"]
        path = KPIS_DIR / f"{kpi_id}.yaml"
        if path.exists() and not overwrite:
            raise FileExistsError(f"KPI '{kpi_id}' already exists. Pass overwrite=True to replace it.")

        missing = [f for f in kpi.get("required_facts", []) if self.get_fact_context(f) is None]
        if missing:
            raise ValueError(f"KPI '{kpi_id}' references unknown facts: {missing}. Add those facts first.")

        KPIS_DIR.mkdir(parents=True, exist_ok=True)
        path.write_text(yaml.safe_dump(kpi, sort_keys=False))

        conn = self._connect()
        try:
            self._upsert_kpi_row(conn, kpi)
            conn.commit()
        finally:
            conn.close()
        return {"kpi_id": kpi_id, "status": "added", "yaml_path": str(path)}

    def remove_fact(self, fact_id: str, force: bool = False) -> dict:
        """
        Deletes registry/facts/<fact_id>.yaml + all SQLite rows for it.
        By default blocks if any KPI still depends on this fact - pass
        force=True to cascade-delete those dependency links too (the
        KPI rows themselves are NOT deleted, just their reference to
        this fact, which will make that KPI's coverage check fail at
        calculation time instead of failing here).
        """
        ctx = self.get_fact_context(fact_id)
        if ctx is None:
            raise KeyError(f"Fact '{fact_id}' not found")

        dependents = ctx["used_by_kpis"]
        if dependents and not force:
            raise DependencyError(
                f"Fact '{fact_id}' is required by {len(dependents)} KPI(s): {dependents}. "
                f"Pass force=True to remove anyway, or remove those KPIs first.",
                dependents,
            )

        conn = self._connect()
        try:
            cur = conn.cursor()
            for table in ("fact_aliases", "fact_extraction_patterns", "fact_document_sources"):
                cur.execute(f"DELETE FROM {table} WHERE fact_id=?", (fact_id,))
            cur.execute("DELETE FROM fact_related WHERE fact_id=? OR related_fact_id=?", (fact_id, fact_id))
            cur.execute("DELETE FROM fact_kpi_related WHERE fact_id=?", (fact_id,))
            cur.execute("DELETE FROM kpi_required_facts WHERE fact_id=?", (fact_id,))
            cur.execute("DELETE FROM kpi_derived_facts WHERE fact_id=?", (fact_id,))
            cur.execute("DELETE FROM registry_fts WHERE entity_type='fact' AND entity_id=?", (fact_id,))
            cur.execute("DELETE FROM facts WHERE fact_id=?", (fact_id,))
            conn.commit()
        finally:
            conn.close()

        path = FACTS_DIR / f"{fact_id}.yaml"
        if path.exists():
            path.unlink()

        return {"fact_id": fact_id, "status": "removed", "cascaded_kpi_links": dependents if force else []}

    def remove_kpi(self, kpi_id: str, force: bool = False) -> dict:
        """Deletes registry/kpis/<kpi_id>.yaml + all SQLite rows for it. No dependency
        check needed in the other direction - nothing depends on a KPI."""
        if self.get_kpi_context(kpi_id) is None:
            raise KeyError(f"KPI '{kpi_id}' not found")

        conn = self._connect()
        try:
            cur = conn.cursor()
            for table in ("kpi_required_facts", "kpi_derived_facts", "kpi_required_documents"):
                cur.execute(f"DELETE FROM {table} WHERE kpi_id=?", (kpi_id,))
            cur.execute("DELETE FROM fact_kpi_related WHERE kpi_id=?", (kpi_id,))
            cur.execute("DELETE FROM registry_fts WHERE entity_type='kpi' AND entity_id=?", (kpi_id,))
            cur.execute("DELETE FROM kpis WHERE kpi_id=?", (kpi_id,))
            conn.commit()
        finally:
            conn.close()

        path = KPIS_DIR / f"{kpi_id}.yaml"
        if path.exists():
            path.unlink()

        return {"kpi_id": kpi_id, "status": "removed"}

    # ------------------------------------------------------------------
    # internal: single-row upserts (used by add_fact/add_kpi, and reused
    # by a full rebuild if you ever need one - see __main__ below)
    # ------------------------------------------------------------------
    def _upsert_fact_row(self, conn, d: dict):
        cur = conn.cursor()
        cur.execute("DELETE FROM facts WHERE fact_id=?", (d["fact_id"],))
        cur.execute("""
            INSERT INTO facts (fact_id, name, category, description, business_definition,
                data_type, unit, fact_type, aggregation_strategy, missing_value_strategy,
                status, validation_rules, normalization_rules, confidence_rules, example_values)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
        """, (
            d["fact_id"], d["name"], d.get("category"), d.get("description"),
            d.get("business_definition"), d["data_type"], d.get("unit"), d["fact_type"],
            d.get("aggregation_strategy"), d.get("missing_value_strategy"), d.get("status", "active"),
            json.dumps(d.get("validation_rules", {})), json.dumps(d.get("normalization_rules", {})),
            json.dumps(d.get("confidence_rules", {})), json.dumps(d.get("example_values", [])),
        ))
        for table in ("fact_aliases", "fact_extraction_patterns", "fact_document_sources",
                      "fact_related"):
            cur.execute(f"DELETE FROM {table} WHERE fact_id=?", (d["fact_id"],))

        for alias in d.get("possible_aliases", []):
            cur.execute("INSERT OR IGNORE INTO fact_aliases (fact_id, alias) VALUES (?,?)",
                        (d["fact_id"], alias.lower()))
        for pattern in d.get("extraction_patterns", []):
            cur.execute("INSERT OR IGNORE INTO fact_extraction_patterns (fact_id, pattern) VALUES (?,?)",
                        (d["fact_id"], pattern.lower()))

        priority = d.get("source_priority", [])
        for rank, doc_type in enumerate(priority):
            cur.execute("INSERT OR IGNORE INTO fact_document_sources (fact_id, document_type, priority_rank) "
                        "VALUES (?,?,?)", (d["fact_id"], doc_type, rank))
        next_rank = len(priority)
        for doc_type in d.get("document_sources", []):
            if doc_type not in priority:
                cur.execute("INSERT OR IGNORE INTO fact_document_sources (fact_id, document_type, priority_rank) "
                            "VALUES (?,?,?)", (d["fact_id"], doc_type, next_rank))
                next_rank += 1

        known_facts = {r[0] for r in cur.execute("SELECT fact_id FROM facts")}
        known_kpis = {r[0] for r in cur.execute("SELECT kpi_id FROM kpis")}
        for related in d.get("related_facts", []):
            if related in known_facts:
                cur.execute("INSERT OR IGNORE INTO fact_related (fact_id, related_fact_id) VALUES (?,?)",
                            (d["fact_id"], related))
            elif related in known_kpis:
                cur.execute("INSERT OR IGNORE INTO fact_kpi_related (fact_id, kpi_id) VALUES (?,?)",
                            (d["fact_id"], related))

        text = " ".join(filter(None, [d.get("description"), d.get("business_definition"),
                                       " ".join(d.get("possible_aliases", []))]))
        cur.execute("DELETE FROM registry_fts WHERE entity_type='fact' AND entity_id=?", (d["fact_id"],))
        cur.execute("INSERT INTO registry_fts (entity_type, entity_id, name, text) VALUES (?,?,?,?)",
                    ("fact", d["fact_id"], d["name"], text))

    def _upsert_kpi_row(self, conn, d: dict):
        cur = conn.cursor()
        cur.execute("DELETE FROM kpis WHERE kpi_id=?", (d["kpi_id"],))
        cur.execute("""
            INSERT INTO kpis (kpi_id, name, category, tier, description, business_value,
                formula, unit, aggregation, frequency, missing_data_strategy, status,
                thresholds, data_quality, benchmarking, example_calculation)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
        """, (
            d["kpi_id"], d["name"], d.get("category"), d.get("tier"), d.get("description"),
            d.get("business_value"), d.get("formula"), d.get("unit"), d.get("aggregation"),
            d.get("frequency"), d.get("missing_data_strategy"), d.get("status", "active"),
            json.dumps(d.get("thresholds", {})), json.dumps(d.get("data_quality", {})),
            json.dumps(d.get("benchmarking", {})), d.get("example_calculation"),
        ))
        for table in ("kpi_required_facts", "kpi_derived_facts", "kpi_required_documents"):
            cur.execute(f"DELETE FROM {table} WHERE kpi_id=?", (d["kpi_id"],))

        for fact_id in d.get("required_facts", []):
            cur.execute("INSERT OR IGNORE INTO kpi_required_facts (kpi_id, fact_id) VALUES (?,?)",
                        (d["kpi_id"], fact_id))
        for fact_id in d.get("derived_facts") or []:
            cur.execute("INSERT OR IGNORE INTO kpi_derived_facts (kpi_id, fact_id) VALUES (?,?)",
                        (d["kpi_id"], fact_id))
        for doc_type in d.get("required_documents", []):
            cur.execute("INSERT OR IGNORE INTO kpi_required_documents (kpi_id, document_type) VALUES (?,?)",
                        (d["kpi_id"], doc_type))

        text = " ".join(filter(None, [d.get("description"), d.get("business_value"), d.get("formula")]))
        cur.execute("DELETE FROM registry_fts WHERE entity_type='kpi' AND entity_id=?", (d["kpi_id"],))
        cur.execute("INSERT INTO registry_fts (entity_type, entity_id, name, text) VALUES (?,?,?,?)",
                    ("kpi", d["kpi_id"], d["name"], text))

    # ------------------------------------------------------------------
    def rebuild_from_yaml(self):
        """Full rebuild from registry/facts + registry/kpis - same effect as the
        old index_builder.py, kept here for 'I edited YAML by hand, resync everything'."""
        conn = self._connect()
        try:
            conn.executescript("""
                DELETE FROM facts; DELETE FROM kpis; DELETE FROM fact_aliases;
                DELETE FROM fact_extraction_patterns; DELETE FROM fact_document_sources;
                DELETE FROM fact_related; DELETE FROM fact_kpi_related;
                DELETE FROM kpi_required_facts; DELETE FROM kpi_derived_facts;
                DELETE FROM kpi_required_documents; DELETE FROM registry_fts;
            """)
            for fname in sorted(os.listdir(FACTS_DIR)):
                if fname.endswith(".yaml"):
                    self._upsert_fact_row(conn, _load_yaml(FACTS_DIR / fname))
            for fname in sorted(os.listdir(KPIS_DIR)):
                if fname.endswith(".yaml"):
                    self._upsert_kpi_row(conn, _load_yaml(KPIS_DIR / fname))
            conn.execute("INSERT OR REPLACE INTO registry_meta VALUES ('last_built_at', ?)",
                         (str(int(time.time())),))
            conn.commit()
        finally:
            conn.close()


if __name__ == "__main__":
    RegistryService().rebuild_from_yaml()
    print("Registry rebuilt from YAML.")
