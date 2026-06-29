import os
import json
import uuid
from contextlib import contextmanager
from functools import lru_cache
from typing import Any, Optional
from dotenv import load_dotenv
load_dotenv()

from backend.utilites.app_logger import Logger
log = Logger()

from sqlalchemy import create_engine, text, inspect
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.orm import sessionmaker
import psycopg2
from psycopg2.extras import RealDictCursor
import threading
import time
import re

from backend.config import DB_DIR, CHROMA_DIR, REGISTRY_SCHEMA_PATH, FACTS_DIR, KPIS_DIR
from backend.db.models import Document, DocumentChunk, Fact, FactObservation, KPI

DB_DIR     = str(DB_DIR)
CHROMA_DIR = str(CHROMA_DIR)


# ---------------------------------------------------------------------------
# Connection
# ---------------------------------------------------------------------------

def pg_connect_kwargs() -> dict:
    return {
        "host":            os.getenv("POSTGRES_HOST"),
        "port":            int(os.getenv("POSTGRES_PORT", 5432)),
        "dbname":          os.getenv("POSTGRES_DB", "postgres"),
        "user":            os.getenv("POSTGRES_USER", "postgres"),
        "password":        os.getenv("POSTGRES_PWD", ""),
        "connect_timeout": 10,
        "sslmode":         "require",
    }


def _build_database_url() -> str:
    if url := os.getenv("DATABASE_URL"):
        return url
    if pg_host := os.getenv("POSTGRES_HOST"):
        kw = pg_connect_kwargs()
        return (
            f"postgresql+psycopg2://{kw['user']}:{kw['password']}"
            f"@{kw['host']}:{kw['port']}/{kw['dbname']}?sslmode=require"
        )
    raise RuntimeError("No database configured. Set DATABASE_URL or POSTGRES_HOST.")


DATABASE_URL = _build_database_url()

engine = create_engine(
    DATABASE_URL,
    pool_size=3,
    max_overflow=2,
    pool_timeout=30,
    pool_pre_ping=True,
    pool_recycle=240,
    connect_args={
        "connect_timeout":    10,
        "keepalives":         1,
        "keepalives_idle":    30,
        "keepalives_interval": 5,
        "keepalives_count":   5,
        # NOTE: do NOT set "options" here — Supabase's transaction-mode
        # PgBouncer does not support session startup parameters and will
        # crash its DbHandler (EDBHANDLEREXITED) if any are sent.
    },
)
SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)


# ---------------------------------------------------------------------------
# Session
# ---------------------------------------------------------------------------

@contextmanager
def get_session():
    """Single canonical session context manager. Commits on success, rolls back on error."""
    db = SessionLocal()
    try:
        yield db
        db.commit()
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()


get_cursor = get_session  # backward-compat alias


# ---------------------------------------------------------------------------
# Schema bootstrap
# ---------------------------------------------------------------------------

def _init_db():
    """
    Apply nexus_schema.sql on every startup (idempotent — every statement uses
    CREATE TABLE/INDEX IF NOT EXISTS). This is what lets a newly-added table like
    nexus_fact_observations get created automatically on restart without a manual
    migration step, while leaving existing tables and data completely untouched.
    """
    with open(os.path.join(DB_DIR, "nexus_schema.sql")) as f:
        schema = f.read()

    with engine.connect() as conn:
        for stmt in schema.strip().split(";"):
            s = stmt.strip()
            if s:
                conn.execute(text(s))
        conn.commit()

    log.log_info(f"Schema applied. Tables: {sorted(inspect(engine).get_table_names())}")


def ensure_schema():
    _init_db()


def check_db_connection() -> None:
    with engine.connect() as conn:
        conn.execute(text("SELECT 1"))


# ---------------------------------------------------------------------------
# Startup
# ---------------------------------------------------------------------------

try:
    with engine.connect():
        log.log_info("Database connected successfully")
    _init_db()
except Exception as e:
    log.log_error(f"Database initialization failed: {e}")
    raise


# ---------------------------------------------------------------------------
# Documents
# ---------------------------------------------------------------------------

def save_document(company_id: str, file_name: str) -> str:
    doc_id = str(uuid.uuid4())
    with get_session() as db:
        db.add(Document(
            document_id=doc_id,
            company_id=company_id.strip().lower(),
            file_name=file_name,
        ))
    return doc_id


def set_document_type(document_id: str, document_type: str) -> None:
    with get_session() as db:
        doc = db.get(Document, document_id)
        if doc:
            doc.document_type = document_type  # type: ignore[assignment]


def get_document_type_counts(company_id: str) -> dict[str, int]:
    with get_session() as db:
        rows = db.execute(
            text(
                "SELECT document_type, COUNT(*) AS cnt "
                "FROM nexus_documents WHERE company_id = :cid "
                "GROUP BY document_type"
            ),
            {"cid": company_id.strip().lower()},
        ).fetchall()
    return {str(r.document_type or "unknown"): r.cnt for r in rows}


# ---------------------------------------------------------------------------
# Chunks
# ---------------------------------------------------------------------------

def save_chunks(chunks: list[dict]) -> None:
    with get_session() as db:
        for c in chunks:
            db.add(DocumentChunk(
                chunk_id=c["chunk_id"],
                document_id=c["document_id"],
                chunk_text=c["chunk_text"],
                chunk_index=c["metadata"].get("chunk_index", 0),
                page_number=c["metadata"].get("page_number"),
                section_title=c["metadata"].get("section"),
            ))


def get_chunks(document_id: str) -> list[dict]:
    with get_session() as db:
        rows = (
            db.query(DocumentChunk)
            .filter(DocumentChunk.document_id == document_id)
            .order_by(DocumentChunk.chunk_index)
            .all()
        )
        return [_orm_to_dict(r) for r in rows]


# ---------------------------------------------------------------------------
# Facts
# ---------------------------------------------------------------------------

def save_fact(
    fact_id: str, company_id: str, value: Any, confidence: float,
    source_document: str, source_chunk: Optional[str],
    source_type: str, period: str,
) -> None:
    stmt = pg_insert(Fact).values(
        fact_id=fact_id,
        company_id=company_id.strip().lower(),
        value=json.dumps(value),
        confidence=confidence,
        source_document=source_document,
        source_chunk=source_chunk,
        source_type=source_type,
        period=period,
    ).on_conflict_do_update(
        index_elements=["fact_id", "company_id", "period"],
        set_={
            "value":           pg_insert(Fact).excluded.value,
            "confidence":      pg_insert(Fact).excluded.confidence,
            "source_document": pg_insert(Fact).excluded.source_document,
            "source_chunk":    pg_insert(Fact).excluded.source_chunk,
            "source_type":     pg_insert(Fact).excluded.source_type,
        },
    )
    with get_session() as db:
        db.execute(stmt)


def get_fact(fact_id: str, company_id: str, period: str) -> Optional[dict]:
    with get_session() as db:
        row = db.get(Fact, (fact_id, company_id.strip().lower(), period))
        if row is None:
            return None
        d = _orm_to_dict(row)
        d["value"] = json.loads(d["value"])
        return d


def get_facts_for_company(company_id: str, period: str) -> dict[str, Any]:
    with get_session() as db:
        rows = (
            db.query(Fact.fact_id, Fact.value)
            .filter(Fact.company_id == company_id.strip().lower(), Fact.period == period)
            .all()
        )
    return {r.fact_id: json.loads(r.value) for r in rows}  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# KPIs
# ---------------------------------------------------------------------------

def save_kpi(
    kpi_id: str, company_id: str, value: Optional[float],
    coverage: float, status: str, period: str,
) -> None:
    stmt = pg_insert(KPI).values(
        kpi_id=kpi_id,
        company_id=company_id.strip().lower(),
        value=value,
        coverage=coverage,
        status=status,
        period=period,
    ).on_conflict_do_update(
        index_elements=["kpi_id", "company_id", "period"],
        set_={
            "value":    pg_insert(KPI).excluded.value,
            "coverage": pg_insert(KPI).excluded.coverage,
            "status":   pg_insert(KPI).excluded.status,
        },
    )
    with get_session() as db:
        db.execute(stmt)


def get_kpi(kpi_id: str, company_id: str, period: str) -> Optional[dict]:
    with get_session() as db:
        row = db.get(KPI, (kpi_id, company_id.strip().lower(), period))
        return _orm_to_dict(row) if row else None


def get_kpis_for_company(company_id: str, period: str) -> list[dict]:
    with get_session() as db:
        rows = (
            db.query(KPI)
            .filter(KPI.company_id == company_id.strip().lower(), KPI.period == period)
            .order_by(KPI.kpi_id)
            .all()
        )
        return [_orm_to_dict(r) for r in rows]


def get_kpis() -> list[dict]:
    with get_session() as db:
        rows = db.query(KPI).order_by(KPI.kpi_id).all()
        return [_orm_to_dict(r) for r in rows]


def get_kpi_history(kpi_id: str, company_id: str, limit: int | None = None) -> list[dict]:
    with get_session() as db:
        q = (
            db.query(KPI.period, KPI.value, KPI.status)
            .filter(KPI.kpi_id == kpi_id, KPI.company_id == company_id.strip().lower())
            .order_by(KPI.period.desc())
        )
        if limit is not None:
            q = q.limit(limit)
        rows = q.all()
        return [{"period": r.period, "value": r.value, "status": r.status} for r in rows]  # type: ignore[misc]


# ---------------------------------------------------------------------------
# Fact Observations
# ---------------------------------------------------------------------------

def save_observation(
    fact_id: str, company_id: str, observation_date: str, value: Any,
    confidence: Optional[float] = None,
    source_document: Optional[str] = None,
    source_type: Optional[str] = None,
) -> None:
    stmt = pg_insert(FactObservation).values(
        fact_id=fact_id,
        company_id=company_id.strip().lower(),
        observation_date=observation_date,
        value=json.dumps(value),
        confidence=confidence,
        source_document=source_document,
        source_type=source_type,
    ).on_conflict_do_update(
        index_elements=["fact_id", "company_id", "observation_date"],
        set_={
            "value":           pg_insert(FactObservation).excluded.value,
            "confidence":      pg_insert(FactObservation).excluded.confidence,
            "source_document": pg_insert(FactObservation).excluded.source_document,
            "source_type":     pg_insert(FactObservation).excluded.source_type,
        },
    )
    with get_session() as db:
        db.execute(stmt)


def save_observations_bulk(observations: list[dict]) -> int:
    if not observations:
        return 0
    # Deduplicate within the batch: Postgres ON CONFLICT DO UPDATE cannot
    # affect the same row twice in one statement. Last-write-wins per key.
    seen: dict[tuple, dict] = {}
    for obs in observations:
        key = (obs["fact_id"], obs["company_id"].strip().lower(), str(obs["observation_date"]))
        seen[key] = obs
    rows = [
        {
            "fact_id":          obs["fact_id"],
            "company_id":       obs["company_id"].strip().lower(),
            "observation_date": obs["observation_date"],
            "value":            json.dumps(obs["value"]),
            "confidence":       obs.get("confidence"),
            "source_document":  obs.get("source_document"),
            "source_type":      obs.get("source_type"),
        }
        for obs in seen.values()
    ]
    stmt = pg_insert(FactObservation).values(rows).on_conflict_do_update(
        index_elements=["fact_id", "company_id", "observation_date"],
        set_={
            "value":           pg_insert(FactObservation).excluded.value,
            "confidence":      pg_insert(FactObservation).excluded.confidence,
            "source_document": pg_insert(FactObservation).excluded.source_document,
            "source_type":     pg_insert(FactObservation).excluded.source_type,
        },
    )
    with get_session() as db:
        db.execute(stmt)
    return len(rows)


def get_observations_in_range(
    fact_id: str, company_id: str, start_date: str, end_date: str,
) -> list[dict]:
    with get_session() as db:
        rows = (
            db.query(FactObservation)
            .filter(
                FactObservation.fact_id == fact_id,
                FactObservation.company_id == company_id.strip().lower(),
                FactObservation.observation_date.between(start_date, end_date),
            )
            .order_by(FactObservation.observation_date)
            .all()
        )
        return [
            {**_orm_to_dict(r), "value": json.loads(r.value)} #type:ignore
            for r in rows
        ]


def get_latest_observation_on_or_before(
    fact_id: str, company_id: str, as_of_date: str,
) -> Optional[dict]:
    with get_session() as db:
        row = (
            db.query(FactObservation)
            .filter(
                FactObservation.fact_id == fact_id,
                FactObservation.company_id == company_id.strip().lower(),
                FactObservation.observation_date <= as_of_date,
            )
            .order_by(FactObservation.observation_date.desc())
            .first()
        )
        if row is None:
            return None
        return {**_orm_to_dict(row), "value": json.loads(row.value)} #type:ignore


def get_observation_date_range(fact_id: str, company_id: str) -> Optional[dict]:
    with get_session() as db:
        row = db.execute(
            text(
                "SELECT MIN(observation_date) AS min_date, MAX(observation_date) AS max_date "
                "FROM nexus_fact_observations "
                "WHERE fact_id=:fid AND company_id=:cid"
            ),
            {"fid": fact_id, "cid": company_id.strip().lower()},
        ).fetchone()
    if row is None or row.min_date is None:
        return None
    return {"min_date": row.min_date, "max_date": row.max_date}


def get_distinct_observed_facts(company_id: str) -> list[str]:
    with get_session() as db:
        rows = (
            db.query(FactObservation.fact_id)
            .filter(FactObservation.company_id == company_id.strip().lower())
            .distinct()
            .all()
        )
    return [r.fact_id for r in rows]


# ---------------------------------------------------------------------------
# Financial Data
# ---------------------------------------------------------------------------

def get_financial_data(company_id: str) -> Any:
    import pandas as pd
    with engine.connect() as conn:
        return pd.read_sql(
            text("SELECT * FROM nexus_financial_data WHERE LOWER(company_name) = :cid").bindparams(
                cid=company_id.lower()
            ),
            conn,
        )


def append_financial_data(frame: Any) -> None:
    frame.to_sql("nexus_financial_data", engine, if_exists="append", index=False)


# ---------------------------------------------------------------------------
# Internal helper
# ---------------------------------------------------------------------------

def _orm_to_dict(row) -> dict:
    return {c.key: getattr(row, c.key) for c in row.__mapper__.columns}


# ---------------------------------------------------------------------------
# Registry DB Operations
# ---------------------------------------------------------------------------

class DependencyError(Exception):
    def __init__(self, message: str, dependents: list[str]):
        super().__init__(message)
        self.dependents = dependents


def _load_yaml(path) -> dict:
    import yaml
    with open(path, "rb") as f:
        return yaml.safe_load(f.read().decode("utf-8", errors="replace"))


class RegistryService:
    """
    Thread-safe wrapper around the registry PostgreSQL tables. One short-lived
    connection per call; psycopg2 handles connection-level thread safety.
    """

    def __init__(self):
        self._init_lock = threading.Lock()
        self._ensure_db()

    def _connect(self):
        conn = psycopg2.connect(**pg_connect_kwargs())
        conn.cursor_factory = RealDictCursor
        return conn

    def _connect_plain(self):
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
                    schema = REGISTRY_SCHEMA_PATH.read_text()
                    for stmt in schema.strip().split(";"):
                        body = re.sub(r"--[^\n]*", "", stmt).strip()  # type: ignore[arg-type]
                        if body:
                            cur.execute(stmt.strip())
                    conn.commit()
            finally:
                conn.close()

    def is_populated(self) -> bool:
        conn = self._connect_plain()
        try:
            cur = conn.cursor()
            cur.execute("SELECT COUNT(*) FROM registry_facts")
            return cur.fetchone()[0] > 0  # type: ignore
        finally:
            conn.close()

    @staticmethod
    def _row(row) -> dict:
        return dict(row) if row else {}

    # ------------------------------------------------------------------
    # READS
    # ------------------------------------------------------------------
    @lru_cache(maxsize=1024)
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
            d["aliases"] = [r["alias"] for r in cur.fetchall()]  # type: ignore

            cur.execute("SELECT pattern FROM fact_extraction_patterns WHERE fact_id=%s", (fact_id,))
            d["extraction_patterns"] = [r["pattern"] for r in cur.fetchall()]  # type: ignore

            cur.execute(
                "SELECT document_type, priority_rank FROM fact_document_sources "
                "WHERE fact_id=%s ORDER BY priority_rank", (fact_id,))
            d["document_sources"] = [
                {"document_type": r["document_type"], "priority_rank": r["priority_rank"]}  # type: ignore
                for r in cur.fetchall()
            ]
            d["source_priority"] = [s["document_type"] for s in d["document_sources"]]

            cur.execute("SELECT related_fact_id FROM fact_related WHERE fact_id=%s", (fact_id,))
            d["related_facts"] = [r["related_fact_id"] for r in cur.fetchall()]  # type: ignore

            cur.execute(
                "SELECT kpi_id FROM kpi_required_facts WHERE fact_id=%s "
                "UNION SELECT kpi_id FROM kpi_derived_facts WHERE fact_id=%s",
                (fact_id, fact_id))
            d["used_by_kpis"] = [r["kpi_id"] for r in cur.fetchall()]  # type: ignore
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
            d["required_facts"] = [r["fact_id"] for r in cur.fetchall()]  # type: ignore

            cur.execute("SELECT fact_id FROM kpi_derived_facts WHERE kpi_id=%s", (kpi_id,))
            d["derived_facts"] = [r["fact_id"] for r in cur.fetchall()]  # type: ignore

            cur.execute("SELECT document_type FROM kpi_required_documents WHERE kpi_id=%s", (kpi_id,))
            d["required_documents"] = [r["document_type"] for r in cur.fetchall()]  # type: ignore
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
            fact_ids = [r["fact_id"] for r in cur.fetchall()]  # type: ignore

            facts = {}
            for fid in fact_ids:
                cur.execute("SELECT fact_id, data_type, unit FROM registry_facts WHERE fact_id=%s", (fid,))
                f = cur.fetchone()
                if f:
                    facts[fid] = {"data_type": f["data_type"], "unit": f["unit"]}  # type: ignore
            return {
                "kpi_id": kpi["kpi_id"], "formula": kpi["formula"],  # type: ignore
                "unit": kpi["unit"], "aggregation": kpi["aggregation"], "facts": facts,  # type: ignore
            }
        finally:
            conn.close()

    @lru_cache(maxsize=1024)
    def discover_relevant_facts(self, document_type: str) -> list[dict]:
        conn = self._connect()
        try:
            cur = conn.cursor()
            cur.execute(
                "SELECT fact_id FROM fact_document_sources WHERE document_type=%s",
                (document_type,))
            fact_ids = [r["fact_id"] for r in cur.fetchall()]  # type: ignore

            out = []
            for fid in fact_ids:
                cur.execute(
                    "SELECT fact_id, name, data_type, unit, category FROM registry_facts WHERE fact_id=%s",
                    (fid,))
                f = cur.fetchone()
                cur.execute("SELECT alias FROM fact_aliases WHERE fact_id=%s", (fid,))
                aliases = [r["alias"] for r in cur.fetchall()]  # type: ignore
                cur.execute("SELECT pattern FROM fact_extraction_patterns WHERE fact_id=%s", (fid,))
                patterns = [r["pattern"] for r in cur.fetchall()]  # type: ignore
                out.append({
                    "fact_id": f["fact_id"], "name": f["name"], "category": f["category"],  # type: ignore
                    "data_type": f["data_type"], "unit": f["unit"],  # type: ignore
                    "aliases": aliases, "extraction_patterns": patterns,
                })
            return out
        finally:
            conn.close()

    @lru_cache(maxsize=1024)
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
                    hit["related"] = [r["related_fact_id"] for r in cur.fetchall()]  # type: ignore
                else:
                    cur.execute(
                        "SELECT fact_id FROM kpi_required_facts WHERE kpi_id=%s",
                        (hit["entity_id"],))
                    hit["related"] = [r["fact_id"] for r in cur.fetchall()]  # type: ignore
            return hits
        finally:
            conn.close()

    def list_all_kpi_ids(self) -> list[str]:
        """Return every kpi_id in the registry, regardless of name content."""
        conn = self._connect()
        try:
            cur = conn.cursor()
            cur.execute("SELECT kpi_id FROM registry_kpis")
            return [r["kpi_id"] for r in cur.fetchall()]
        finally:
            conn.close()

    def list_all_fact_ids(self) -> list[str]:
        """Return every fact_id in the registry."""
        conn = self._connect()
        try:
            cur = conn.cursor()
            cur.execute("SELECT fact_id FROM registry_facts")
            return [r["fact_id"] for r in cur.fetchall()]
        finally:
            conn.close()

    @lru_cache(maxsize=1)
    def _collapsed_id_map(self) -> dict[str, str]:
        """
        Maps collapsed (all-non-alnum stripped) form → real fact_id/kpi_id.
        Cached once per RegistryService instance. Used by tabular_extraction
        to match column headers where separators were stripped by the parser
        (e.g. Docling exports 'active_ai_users' → 'activeaiusers').
        """
        import re as _re
        def collapse(s): return _re.sub(r"[^a-z0-9]", "", s.lower())
        mapping = {}
        for fid in self.list_all_fact_ids():
            mapping[collapse(fid)] = fid
        for kid in self.list_all_kpi_ids():
            mapping.setdefault(collapse(kid), kid)
        return mapping

    def get_id_by_collapsed(self, collapsed_header: str) -> str | None:
        """
        Return the real fact_id or kpi_id whose collapsed form equals
        `collapsed_header`, or None if no match.
        """
        import re as _re
        c = _re.sub(r"[^a-z0-9]", "", collapsed_header.lower())
        return self._collapsed_id_map().get(c)

    @staticmethod
    def _to_fts_query(query: str) -> str:
        # Remove parenthetical qualifiers and bracket tags
        q = re.sub(r"\([^)]*\)", " ", query)
        q = re.sub(r"\[[^\]]*\]", " ", q)
        # Hyphens and underscores → spaces so "AI-Attributed" / "ai_roi" tokenize correctly
        q = re.sub(r"[-_]", " ", q)
        # Strip remaining non-alphanumeric chars
        q = re.sub(r"[^\w\s]", " ", q)
        q = re.sub(r"\s+", " ", q).strip()
        return q or query  # type: ignore[return-value]

    # ------------------------------------------------------------------
    # WRITES
    # ------------------------------------------------------------------
    def add_fact(self, fact: dict, overwrite: bool = False) -> dict:
        import yaml
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
        import yaml
        kpi_id = kpi["kpi_id"]
        path = KPIS_DIR / f"{kpi_id}.yaml"
        if path.exists() and not overwrite:
            raise FileExistsError(f"KPI '{kpi_id}' already exists. Pass overwrite=True to replace it.")
        missing = [f for f in kpi.get("required_facts", []) if self.get_fact_context(f) is None]
        if missing:
            raise ValueError(f"KPI '{kpi_id}' references unknown facts: {missing}.")
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
                f"Pass force=True to remove anyway.",
                dependents,
            )
        conn = self._connect()
        try:
            cur = conn.cursor()
            for table in ("fact_aliases", "fact_extraction_patterns", "fact_document_sources"):
                cur.execute(f"DELETE FROM {table} WHERE fact_id=%s", (fact_id,))
            cur.execute("DELETE FROM fact_related WHERE fact_id=%s OR related_fact_id=%s", (fact_id, fact_id))
            cur.execute("DELETE FROM fact_kpi_related WHERE fact_id=%s", (fact_id,))
            cur.execute("DELETE FROM kpi_required_facts WHERE fact_id=%s", (fact_id,))
            cur.execute("DELETE FROM kpi_derived_facts WHERE fact_id=%s", (fact_id,))
            cur.execute("DELETE FROM registry_fts WHERE entity_type='fact' AND entity_id=%s", (fact_id,))
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
            cur.execute("DELETE FROM registry_fts WHERE entity_type='kpi' AND entity_id=%s", (kpi_id,))
            cur.execute("DELETE FROM registry_kpis WHERE kpi_id=%s", (kpi_id,))
            conn.commit()
        finally:
            conn.close()
        path = KPIS_DIR / f"{kpi_id}.yaml"
        if path.exists():
            path.unlink()
        return {"kpi_id": kpi_id, "status": "removed"}

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

        fts_text = " ".join(filter(None, [
            d.get("description"), d.get("business_definition"),
            " ".join(d.get("possible_aliases", [])),
        ]))
        cur.execute("DELETE FROM registry_fts WHERE entity_type='fact' AND entity_id=%s", (d["fact_id"],))
        cur.execute(
            "INSERT INTO registry_fts (entity_type, entity_id, name, text) VALUES (%s,%s,%s,%s)",
            ("fact", d["fact_id"], d["name"], fts_text))

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

        fts_text = " ".join(filter(None, [
            d.get("description"), d.get("business_value"), d.get("formula"),
        ]))
        cur.execute("DELETE FROM registry_fts WHERE entity_type='kpi' AND entity_id=%s", (d["kpi_id"],))
        cur.execute(
            "INSERT INTO registry_fts (entity_type, entity_id, name, text) VALUES (%s,%s,%s,%s)",
            ("kpi", d["kpi_id"], d["name"], fts_text))

    def rebuild_from_yaml(self):
        conn = self._connect()
        try:
            cur = conn.cursor()
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


# ---------------------------------------------------------------------------
# Administrative DB Operations
# ---------------------------------------------------------------------------

def get_tables() -> list[str]:
    return inspect(engine).get_table_names()


def get_table_columns(table_name: str) -> list[dict]:
    return [{"name": c["name"], "type": str(c["type"])} for c in inspect(engine).get_columns(table_name)]


def get_pg_table_count(table_name: str) -> int:
    with engine.connect() as conn:
        return conn.execute(text(f'SELECT COUNT(*) FROM "{table_name}"')).scalar()  # type: ignore


def drop_pg_column(table_name: str, column_name: str):
    with engine.begin() as conn:
        conn.execute(text(f'ALTER TABLE "{table_name}" DROP COLUMN "{column_name}"'))


def rebuild_pg_table_without_column(table_name: str, tmp_table_name: str, col_defs: str, cols_csv: str):
    with engine.begin() as conn:
        conn.execute(text(f'CREATE TABLE "{tmp_table_name}" ({col_defs})'))
        conn.execute(text(f'INSERT INTO "{tmp_table_name}" ({cols_csv}) SELECT {cols_csv} FROM "{table_name}"'))
        conn.execute(text(f'DROP TABLE "{table_name}"'))
        conn.execute(text(f'ALTER TABLE "{tmp_table_name}" RENAME TO "{table_name}"'))


def get_records_from_table(table_name: str, limit: int | None = 20) -> list[dict]:
    from sqlalchemy import MetaData, Table
    meta = MetaData()
    tbl = Table(table_name, meta, autoload_with=engine)
    with engine.connect() as conn:
        q = tbl.select()
        if limit:
            q = q.limit(limit)
        rows = conn.execute(q).fetchall()
        cols = [c.name for c in tbl.columns]
    return [dict(zip(cols, row)) for row in rows]


def search_records_in_table(table_name: str, column: str, value: str, exact: bool = False) -> list[dict]:
    from sqlalchemy import MetaData, Table
    meta = MetaData()
    tbl = Table(table_name, meta, autoload_with=engine)
    if column not in [c.name for c in tbl.columns]:
        return []
    col = tbl.c[column]
    condition = (col == value) if exact else col.like(f"%{value}%")
    with engine.connect() as conn:
        rows = conn.execute(tbl.select().where(condition)).fetchall()
        cols = [c.name for c in tbl.columns]
    return [dict(zip(cols, row)) for row in rows]


def delete_records_from_table(table_name: str, column: str, value: str, exact: bool = True) -> int:
    from sqlalchemy import MetaData, Table
    meta = MetaData()
    tbl = Table(table_name, meta, autoload_with=engine)
    if column not in [c.name for c in tbl.columns]:
        return 0
    col = tbl.c[column]
    condition = (col == value) if exact else col.like(f"%{value}%")
    with engine.begin() as conn:
        return conn.execute(tbl.delete().where(condition)).rowcount


def delete_all_records_from_table(table_name: str) -> int:
    from sqlalchemy import MetaData, Table
    meta = MetaData()
    tbl = Table(table_name, meta, autoload_with=engine)
    with engine.begin() as conn:
        return conn.execute(tbl.delete()).rowcount


def drop_pg_table(table_name: str):
    from sqlalchemy import MetaData, Table
    meta = MetaData()
    tbl = Table(table_name, meta, autoload_with=engine)
    tbl.drop(engine)


# ---------------------------------------------------------------------------
# Text-to-SQL Chatbot Helpers (SQLite) — second pass
# ---------------------------------------------------------------------------
import sqlite3
from pathlib import Path


def get_sqlite_table_names(db_path: Path) -> list[dict]:
    with sqlite3.connect(db_path) as conn:
        conn.row_factory = sqlite3.Row
        return conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' "
            "AND name IN ('nexus_financial_data', 'nexus_kpis', 'nexus_fact_observations', 'nexus_facts') ORDER BY name"
        ).fetchall()


def get_sqlite_table_info(db_path: Path, table: str) -> list[dict]:
    with sqlite3.connect(db_path) as conn:
        conn.row_factory = sqlite3.Row
        return conn.execute(f'PRAGMA table_info("{table}")').fetchall()


def get_sqlite_table_count(db_path: Path, table: str) -> int:
    with sqlite3.connect(db_path) as conn:
        conn.row_factory = sqlite3.Row
        return conn.execute(f'SELECT COUNT(*) AS count FROM "{table}"').fetchone()["count"]


def get_sqlite_table_sample(db_path: Path, table: str, limit: int = 3) -> list[dict]:
    with sqlite3.connect(db_path) as conn:
        conn.row_factory = sqlite3.Row
        return conn.execute(f'SELECT * FROM "{table}" LIMIT {limit}').fetchall()


def get_sqlite_companies_summary(db_path: Path) -> list[dict]:
    with sqlite3.connect(db_path) as conn:
        conn.row_factory = sqlite3.Row
        return conn.execute(
            "SELECT company_name, MIN(financial_year) AS min_year, "
            "MAX(financial_year) AS max_year, COUNT(*) AS rows "
            "FROM nexus_financial_data GROUP BY company_name ORDER BY company_name"
        ).fetchall()


def get_sqlite_date_range(db_path: Path) -> dict:
    with sqlite3.connect(db_path) as conn:
        conn.row_factory = sqlite3.Row
        return dict(conn.execute(
            "SELECT MIN(date) AS min_date, MAX(date) AS max_date FROM nexus_financial_data"
        ).fetchone())


def check_sqlite_table_exists(db_path: Path, table: str) -> bool:
    with sqlite3.connect(db_path) as conn:
        conn.row_factory = sqlite3.Row
        return len(conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name=?", (table,)
        ).fetchall()) > 0


def get_sqlite_company_row_count(db_path: Path, company_id: str) -> int:
    with sqlite3.connect(db_path) as conn:
        conn.row_factory = sqlite3.Row
        return conn.execute(
            "SELECT COUNT(*) AS cnt FROM nexus_financial_data WHERE LOWER(company_name) LIKE ?",
            (f"%{company_id.lower()}%",),
        ).fetchone()["cnt"]


def get_sqlite_company_years(db_path: Path, company_id: str) -> list[int]:
    with sqlite3.connect(db_path) as conn:
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            "SELECT DISTINCT financial_year FROM nexus_financial_data "
            "WHERE LOWER(company_name) LIKE ? AND financial_year IS NOT NULL ORDER BY financial_year",
            (f"%{company_id.lower()}%",),
        ).fetchall()
        return [r["financial_year"] for r in rows]


def explain_sqlite_query(db_path: Path, sql: str) -> list[dict]:
    with sqlite3.connect(db_path) as conn:
        conn.row_factory = sqlite3.Row
        return [dict(r) for r in conn.execute(f"EXPLAIN QUERY PLAN {sql}").fetchall()]


def execute_sqlite_query(db_path: Path, sql: str) -> list[dict]:
    with sqlite3.connect(db_path) as conn:
        conn.row_factory = sqlite3.Row
        return [dict(r) for r in conn.execute(sql).fetchall()]
