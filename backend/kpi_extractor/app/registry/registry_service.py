"""
Registry Service
==================
Single in-process module for all registry reads + writes (facts & KPIs).

Source of truth: registry/facts/*.yaml and registry/kpis/*.yaml.
PostgreSQL (Supabase) is a derived, queryable index of those YAML files.
add_fact/add_kpi/remove_fact/remove_kpi keep both in sync so a future
`python -m app.registry.service --rebuild` never silently undoes an
admin-panel change.
"""

import json
import os
import re
import threading
import time
from pathlib import Path
from typing import Optional

import psycopg2
from psycopg2.extras import RealDictCursor
import yaml

from backend.config import FACTS_DIR, KPIS_DIR, REGISTRY_SCHEMA_PATH
from backend.db.db_client import pg_connect_kwargs

SCHEMA_PATH = REGISTRY_SCHEMA_PATH


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
    Thread-safe wrapper around the registry PostgreSQL tables. One short-lived
    connection per call; psycopg2 handles connection-level thread safety.
    """

    def __init__(self):
        self._init_lock = threading.Lock()
        self._ensure_db()

    def _connect(self):
        """Dict-cursor connection for registry queries."""
        conn = psycopg2.connect(**pg_connect_kwargs())
        conn.cursor_factory = RealDictCursor
        return conn

    def _connect_plain(self):
        """Tuple-cursor connection for scalar queries."""
        return psycopg2.connect(**pg_connect_kwargs())

    def _ensure_db(self):
        with self._init_lock:
            conn = self._connect()
            try:
                cur = conn.cursor()
                cur.execute(
                    "SELECT table_name FROM information_schema.tables "
                    "WHERE table_schema='public' AND table_name='registry_facts'"
                )
                if cur.fetchone() is None:
                    schema = SCHEMA_PATH.read_text()
                    for stmt in schema.strip().split(";"):
                        body = re.sub(r"--[^\n]*", "", stmt).strip()
                        if body:
                            cur.execute(stmt.strip())
                    conn.commit()
            finally:
                conn.close()

    def is_populated(self) -> bool:
        """
        True if the registry index already has facts loaded.
        """
        conn = self._connect_plain()
        try:
            cur = conn.cursor()
            cur.execute("SELECT COUNT(*) FROM registry_facts")
            return cur.fetchone()[0] > 0 #type:ignore
        finally:
            conn.close()

    @staticmethod
    def _row(row) -> dict:
        return dict(row) if row else {}

    # ------------------------------------------------------------------
    # READS
    # ------------------------------------------------------------------
    def get_fact_context(self, fact_id: str) -> Optional[dict]:
        conn = self._connect()
        try:
            cur = conn.cursor()
            cur.execute("SELECT * FROM registry_facts WHERE fact_id=%s", (fact_id,))
            fact = cur.fetchone()
            if not fact:
                return None
            d = self._row(fact)
            for f in ("validation_rules", "normalization_rules", "confidence_rules", "example_values"):
                d[f] = json.loads(d[f] or "null")

            cur.execute("SELECT alias FROM fact_aliases WHERE fact_id=%s", (fact_id,))
            d["aliases"] = [r["alias"] for r in cur.fetchall()] #type:ignore

            cur.execute("SELECT pattern FROM fact_extraction_patterns WHERE fact_id=%s", (fact_id,))
            d["extraction_patterns"] = [r["pattern"] for r in cur.fetchall()] #type:ignore

            cur.execute(
                "SELECT document_type, priority_rank FROM fact_document_sources "
                "WHERE fact_id=%s ORDER BY priority_rank", (fact_id,))
            d["document_sources"] = [
                {"document_type": r["document_type"], "priority_rank": r["priority_rank"]} #type:ignore
                for r in cur.fetchall()
            ]
            d["source_priority"] = [s["document_type"] for s in d["document_sources"]]

            cur.execute("SELECT related_fact_id FROM fact_related WHERE fact_id=%s", (fact_id,)) #type:ignore
            d["related_facts"] = [r["related_fact_id"] for r in cur.fetchall()] #type:ignore

            cur.execute(
                "SELECT kpi_id FROM kpi_required_facts WHERE fact_id=%s "
                "UNION SELECT kpi_id FROM kpi_derived_facts WHERE fact_id=%s",
                (fact_id, fact_id))
            d["used_by_kpis"] = [r["kpi_id"] for r in cur.fetchall()] #type:ignore
            return d
        finally:
            conn.close()

    def get_kpi_context(self, kpi_id: str) -> Optional[dict]:
        conn = self._connect()
        try:
            cur = conn.cursor()
            cur.execute("SELECT * FROM registry_kpis WHERE kpi_id=%s", (kpi_id,))
            kpi = cur.fetchone()
            if not kpi:
                return None
            d = self._row(kpi)
            for f in ("thresholds", "data_quality", "benchmarking"):
                d[f] = json.loads(d[f] or "null")

            cur.execute("SELECT fact_id FROM kpi_required_facts WHERE kpi_id=%s", (kpi_id,))
            d["required_facts"] = [r["fact_id"] for r in cur.fetchall()] #type:ignore

            cur.execute("SELECT fact_id FROM kpi_derived_facts WHERE kpi_id=%s", (kpi_id,))
            d["derived_facts"] = [r["fact_id"] for r in cur.fetchall()] #type:ignore

            cur.execute("SELECT document_type FROM kpi_required_documents WHERE kpi_id=%s", (kpi_id,))
            d["required_documents"] = [r["document_type"] for r in cur.fetchall()] #type:ignore
            return d
        finally:
            conn.close()

    def retrieve_formula_context(self, kpi_id: str) -> Optional[dict]:
        conn = self._connect()
        try:
            cur = conn.cursor()
            cur.execute(
                "SELECT kpi_id, formula, unit, aggregation FROM registry_kpis WHERE kpi_id=%s", (kpi_id,))
            kpi = cur.fetchone()
            if not kpi:
                return None

            cur.execute(
                "SELECT fact_id FROM kpi_required_facts WHERE kpi_id=%s "
                "UNION SELECT fact_id FROM kpi_derived_facts WHERE kpi_id=%s",
                (kpi_id, kpi_id))
            fact_ids = [r["fact_id"] for r in cur.fetchall()] #type:ignore

            facts = {}
            for fid in fact_ids:
                cur.execute("SELECT fact_id, data_type, unit FROM registry_facts WHERE fact_id=%s", (fid,))
                f = cur.fetchone()
                if f:
                    facts[fid] = {"data_type": f["data_type"], "unit": f["unit"]} #type:ignore
            return {
                "kpi_id": kpi["kpi_id"], "formula": kpi["formula"], #type:ignore
                "unit": kpi["unit"], "aggregation": kpi["aggregation"], "facts": facts, #type:ignore
            }
        finally:
            conn.close()

    def discover_relevant_facts(self, document_type: str) -> list[dict]:
        conn = self._connect()
        try:
            cur = conn.cursor()
            cur.execute(
                "SELECT fact_id FROM fact_document_sources WHERE document_type=%s",
                (document_type,))
            fact_ids = [r["fact_id"] for r in cur.fetchall()]  #type:ignore

            out = []
            for fid in fact_ids:
                cur.execute(
                    "SELECT fact_id, name, data_type, unit, category FROM registry_facts WHERE fact_id=%s",
                    (fid,))
                f = cur.fetchone()
                cur.execute("SELECT alias FROM fact_aliases WHERE fact_id=%s", (fid,))
                aliases = [r["alias"] for r in cur.fetchall()] #type:ignore
                cur.execute("SELECT pattern FROM fact_extraction_patterns WHERE fact_id=%s", (fid,))
                patterns = [r["pattern"] for r in cur.fetchall()] #type:ignore
                out.append({
                    "fact_id": f["fact_id"], "name": f["name"], "category": f["category"], #type:ignore
                    "data_type": f["data_type"], "unit": f["unit"], #type:ignore
                    "aliases": aliases, "extraction_patterns": patterns,
                })
            return out
        finally:
            conn.close()

    def search_registry(self, query: str, limit: int = 10) -> list[dict]:
        conn = self._connect()
        try:
            cur = conn.cursor()
            fts_query = self._to_fts_query(query)
            cur.execute(
                "SELECT entity_type, entity_id, name FROM registry_fts "
                "WHERE to_tsvector('english', COALESCE(name,'') || ' ' || COALESCE(text,'')) "
                "@@ websearch_to_tsquery('english', %s) LIMIT %s",
                (fts_query, limit),
            )
            return [self._row(r) for r in cur.fetchall()]
        finally:
            conn.close()

    def search_related_registry_content(self, query: str, limit: int = 10) -> list[dict]:
        hits = self.search_registry(query, limit)
        conn = self._connect()
        try:
            cur = conn.cursor()
            for hit in hits:
                if hit["entity_type"] == "fact":
                    cur.execute(
                        "SELECT related_fact_id FROM fact_related WHERE fact_id=%s",
                        (hit["entity_id"],))
                    hit["related"] = [r["related_fact_id"] for r in cur.fetchall()] #type:ignore
                else:
                    cur.execute(
                        "SELECT fact_id FROM kpi_required_facts WHERE kpi_id=%s",
                        (hit["entity_id"],))
                    hit["related"] = [r["fact_id"] for r in cur.fetchall()] #type:ignore
            return hits
        finally:
            conn.close()

    @staticmethod
    def _to_fts_query(query: str) -> str:
        # websearch_to_tsquery accepts plain natural language — just strip special chars
        return re.sub(r"[^\w\s]", " ", query).strip() or query

    # ------------------------------------------------------------------
    # WRITES  -  add / remove, syncing PostgreSQL + YAML together
    # ------------------------------------------------------------------
    def add_fact(self, fact: dict, overwrite: bool = False) -> dict:
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
                cur.execute(f"DELETE FROM {table} WHERE fact_id=%s", (fact_id,))
            cur.execute(
                "DELETE FROM fact_related WHERE fact_id=%s OR related_fact_id=%s",
                (fact_id, fact_id))
            cur.execute("DELETE FROM fact_kpi_related WHERE fact_id=%s", (fact_id,))
            cur.execute("DELETE FROM kpi_required_facts WHERE fact_id=%s", (fact_id,))
            cur.execute("DELETE FROM kpi_derived_facts WHERE fact_id=%s", (fact_id,))
            cur.execute(
                "DELETE FROM registry_fts WHERE entity_type='fact' AND entity_id=%s", (fact_id,))
            cur.execute("DELETE FROM registry_facts WHERE fact_id=%s", (fact_id,))
            conn.commit()
        finally:
            conn.close()

        path = FACTS_DIR / f"{fact_id}.yaml"
        if path.exists():
            path.unlink()

        return {"fact_id": fact_id, "status": "removed", "cascaded_kpi_links": dependents if force else []}

    def remove_kpi(self, kpi_id: str, force: bool = False) -> dict:
        if self.get_kpi_context(kpi_id) is None:
            raise KeyError(f"KPI '{kpi_id}' not found")

        conn = self._connect()
        try:
            cur = conn.cursor()
            for table in ("kpi_required_facts", "kpi_derived_facts", "kpi_required_documents"):
                cur.execute(f"DELETE FROM {table} WHERE kpi_id=%s", (kpi_id,))
            cur.execute("DELETE FROM fact_kpi_related WHERE kpi_id=%s", (kpi_id,))
            cur.execute(
                "DELETE FROM registry_fts WHERE entity_type='kpi' AND entity_id=%s", (kpi_id,))
            cur.execute("DELETE FROM registry_kpis WHERE kpi_id=%s", (kpi_id,))
            conn.commit()
        finally:
            conn.close()

        path = KPIS_DIR / f"{kpi_id}.yaml"
        if path.exists():
            path.unlink()

        return {"kpi_id": kpi_id, "status": "removed"}

    # ------------------------------------------------------------------
    # internal: single-row upserts
    # ------------------------------------------------------------------
    def _upsert_fact_row(self, conn, d: dict):
        cur = conn.cursor()
        cur.execute("DELETE FROM registry_facts WHERE fact_id=%s", (d["fact_id"],))
        cur.execute("""
            INSERT INTO registry_facts (fact_id, name, category, description, business_definition,
                data_type, unit, fact_type, aggregation_strategy, missing_value_strategy,
                status, validation_rules, normalization_rules, confidence_rules, example_values)
            VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
        """, (
            d["fact_id"], d["name"], d.get("category"), d.get("description"),
            d.get("business_definition"), d["data_type"], d.get("unit"), d["fact_type"],
            d.get("aggregation_strategy"), d.get("missing_value_strategy"), d.get("status", "active"),
            json.dumps(d.get("validation_rules", {})), json.dumps(d.get("normalization_rules", {})),
            json.dumps(d.get("confidence_rules", {})), json.dumps(d.get("example_values", [])),
        ))

        for table in ("fact_aliases", "fact_extraction_patterns", "fact_document_sources", "fact_related"):
            cur.execute(f"DELETE FROM {table} WHERE fact_id=%s", (d["fact_id"],))

        for alias in d.get("possible_aliases", []):
            cur.execute(
                "INSERT INTO fact_aliases (fact_id, alias) VALUES (%s,%s) ON CONFLICT DO NOTHING",
                (d["fact_id"], alias.lower()))

        for pattern in d.get("extraction_patterns", []):
            cur.execute(
                "INSERT INTO fact_extraction_patterns (fact_id, pattern) VALUES (%s,%s) ON CONFLICT DO NOTHING",
                (d["fact_id"], pattern.lower()))

        priority = d.get("source_priority", [])
        for rank, doc_type in enumerate(priority):
            cur.execute(
                "INSERT INTO fact_document_sources (fact_id, document_type, priority_rank) "
                "VALUES (%s,%s,%s) ON CONFLICT DO NOTHING",
                (d["fact_id"], doc_type, rank))
        next_rank = len(priority)
        for doc_type in d.get("document_sources", []):
            if doc_type not in priority:
                cur.execute(
                    "INSERT INTO fact_document_sources (fact_id, document_type, priority_rank) "
                    "VALUES (%s,%s,%s) ON CONFLICT DO NOTHING",
                    (d["fact_id"], doc_type, next_rank))
                next_rank += 1

        cur.execute("SELECT fact_id FROM registry_facts")
        known_facts = {r["fact_id"] for r in cur.fetchall()}
        cur.execute("SELECT kpi_id FROM registry_kpis")
        known_kpis = {r["kpi_id"] for r in cur.fetchall()}

        for related in d.get("related_facts", []):
            if related in known_facts:
                cur.execute(
                    "INSERT INTO fact_related (fact_id, related_fact_id) VALUES (%s,%s) ON CONFLICT DO NOTHING",
                    (d["fact_id"], related))
            elif related in known_kpis:
                cur.execute(
                    "INSERT INTO fact_kpi_related (fact_id, kpi_id) VALUES (%s,%s) ON CONFLICT DO NOTHING",
                    (d["fact_id"], related))

        text = " ".join(filter(None, [
            d.get("description"), d.get("business_definition"),
            " ".join(d.get("possible_aliases", [])),
        ]))
        cur.execute(
            "DELETE FROM registry_fts WHERE entity_type='fact' AND entity_id=%s", (d["fact_id"],))
        cur.execute(
            "INSERT INTO registry_fts (entity_type, entity_id, name, text) VALUES (%s,%s,%s,%s)",
            ("fact", d["fact_id"], d["name"], text))

    def _upsert_kpi_row(self, conn, d: dict):
        cur = conn.cursor()
        cur.execute("DELETE FROM registry_kpis WHERE kpi_id=%s", (d["kpi_id"],))
        cur.execute("""
            INSERT INTO registry_kpis (kpi_id, name, category, tier, description, business_value,
                formula, unit, aggregation, frequency, missing_data_strategy, status,
                thresholds, data_quality, benchmarking, example_calculation)
            VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
        """, (
            d["kpi_id"], d["name"], d.get("category"), d.get("tier"), d.get("description"),
            d.get("business_value"), d.get("formula"), d.get("unit"), d.get("aggregation"),
            d.get("frequency"), d.get("missing_data_strategy"), d.get("status", "active"),
            json.dumps(d.get("thresholds", {})), json.dumps(d.get("data_quality", {})),
            json.dumps(d.get("benchmarking", {})), d.get("example_calculation"),
        ))

        for table in ("kpi_required_facts", "kpi_derived_facts", "kpi_required_documents"):
            cur.execute(f"DELETE FROM {table} WHERE kpi_id=%s", (d["kpi_id"],))

        for fact_id in d.get("required_facts", []):
            cur.execute(
                "INSERT INTO kpi_required_facts (kpi_id, fact_id) VALUES (%s,%s) ON CONFLICT DO NOTHING",
                (d["kpi_id"], fact_id))
        for fact_id in d.get("derived_facts") or []:
            cur.execute(
                "INSERT INTO kpi_derived_facts (kpi_id, fact_id) VALUES (%s,%s) ON CONFLICT DO NOTHING",
                (d["kpi_id"], fact_id))
        for doc_type in d.get("required_documents", []):
            cur.execute(
                "INSERT INTO kpi_required_documents (kpi_id, document_type) VALUES (%s,%s) ON CONFLICT DO NOTHING",
                (d["kpi_id"], doc_type))

        text = " ".join(filter(None, [
            d.get("description"), d.get("business_value"), d.get("formula"),
        ]))
        cur.execute(
            "DELETE FROM registry_fts WHERE entity_type='kpi' AND entity_id=%s", (d["kpi_id"],))
        cur.execute(
            "INSERT INTO registry_fts (entity_type, entity_id, name, text) VALUES (%s,%s,%s,%s)",
            ("kpi", d["kpi_id"], d["name"], text))

    # ------------------------------------------------------------------
    def rebuild_from_yaml(self):
        """Full rebuild from registry/facts + registry/kpis YAML files."""
        conn = self._connect()
        try:
            cur = conn.cursor()
            # Delete in FK-safe order (children before parents)
            for table in (
                "registry_fts", "fact_kpi_related", "fact_related",
                "fact_document_sources", "fact_extraction_patterns", "fact_aliases",
                "kpi_required_facts", "kpi_derived_facts", "kpi_required_documents",
                "registry_kpis", "registry_facts",
            ):
                cur.execute(f"DELETE FROM {table}")

            for fname in sorted(os.listdir(FACTS_DIR)):
                if fname.endswith(".yaml"):
                    self._upsert_fact_row(conn, _load_yaml(FACTS_DIR / fname))
            for fname in sorted(os.listdir(KPIS_DIR)):
                if fname.endswith(".yaml"):
                    self._upsert_kpi_row(conn, _load_yaml(KPIS_DIR / fname))

            cur.execute(
                "INSERT INTO registry_meta VALUES ('last_built_at', %s) "
                "ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value",
                (str(int(time.time())),))
            conn.commit()
        finally:
            conn.close()


if __name__ == "__main__":
    RegistryService().rebuild_from_yaml()
    print("Registry rebuilt from YAML.")
