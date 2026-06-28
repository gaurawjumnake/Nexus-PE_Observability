import os
import json
import uuid
from functools import lru_cache
from contextlib import contextmanager
from typing import Optional
from dotenv import load_dotenv
load_dotenv()

from backend.utilites.app_logger import Logger
log = Logger()

from sqlalchemy import create_engine, text, inspect
from sqlalchemy.orm import sessionmaker
import psycopg2
from psycopg2.extras import RealDictCursor
import threading
import time
import re
from backend.config import DB_DIR, CHROMA_DIR, REGISTRY_SCHEMA_PATH, FACTS_DIR, KPIS_DIR

# Re-export as strings for callers that expect os.path-style string paths.
DB_DIR     = str(DB_DIR)
CHROMA_DIR = str(CHROMA_DIR)


def pg_connect_kwargs() -> dict:
    """Postgres connection params from env — single source for all psycopg2 callers."""
    return {
        "host": os.getenv("POSTGRES_HOST"),
        "port": int(os.getenv("POSTGRES_PORT", 5432)),
        "dbname": os.getenv("POSTGRES_DB", "postgres"),
        "user": os.getenv("POSTGRES_USER", "postgres"),
        "password": os.getenv("POSTGRES_PWD", ""),
        "connect_timeout": 10,
        "sslmode": "require",
    }


def _build_database_url() -> str:
    if url := os.getenv("DATABASE_URL"):
        return url
    pg_host = os.getenv("POSTGRES_HOST")
    if pg_host:
        kw = pg_connect_kwargs()
        return f"postgresql+psycopg2://{kw['user']}:{kw['password']}@{kw['host']}:{kw['port']}/{kw['dbname']}?sslmode=require"
    raise RuntimeError("No database configured. Set DATABASE_URL or POSTGRES_HOST.")


DATABASE_URL = _build_database_url()

engine = create_engine(
    DATABASE_URL,
    pool_size=5,
    max_overflow=2,
    pool_pre_ping=True,   # drops stale connections silently before use
    pool_recycle=240,     # recycle before Supabase's ~5-min idle timeout
    connect_args={
        "connect_timeout": 10,       # fail fast on unreachable host
        "keepalives": 1,             # enable TCP keepalives
        "keepalives_idle": 30,       # send first keepalive after 30s idle
        "keepalives_interval": 5,    # retry every 5s
        "keepalives_count": 5,       # drop after 5 missed keepalives (~55s)
        "options": "-c statement_timeout=30000",  # cancel any query > 30s
    },
)
SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)


def _init_db():
    """
    Apply schema (idempotent - every statement is CREATE ... IF NOT EXISTS).
    Always runs, even against an existing database: this is what lets a
    newly-added table like fact_observations get created on app startup
    without a manual migration step, while leaving existing tables/data
    untouched.
    """
    with open(os.path.join(DB_DIR, "nexus_schema.sql"), "r") as f:
        SCHEMA = f.read()

    with engine.connect() as conn:
        for statement in SCHEMA.strip().split(";"):
            stmt = statement.strip()
            if stmt:
                conn.execute(text(stmt))
        conn.commit()

    existing = inspect(engine).get_table_names()
    log.log_info(f"Schema up to date. Tables present: {sorted(existing)}")


try:
    with engine.connect() as conn:
        log.log_info("Database connected successfully")
    _init_db()
except Exception as e:
    log.log_error(f"Database initialization failed: {e}")
    raise


def ensure_schema():
    """
    Public re-entry point for _init_db(). Call this defensively from any
    code path that depends on a table existing but doesn't control
    startup timing (e.g. get_financial_columns() in data_ingestion.py) -
    safe to call any number of times since every statement in
    nexus_schema.sql is CREATE ... IF NOT EXISTS. This is what lets a
    manually-dropped table self-heal on the next request instead of
    requiring a full server restart.
    """
    _init_db()


@contextmanager
def get_cursor():
    db = SessionLocal()
    try:
        yield db
        db.commit()
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()

# API Health Check --------------------------------------------------

def check_db_connection() -> None:
    with engine.connect() as conn:
        conn.execute(text("SELECT 1"))


# Documents ---------------------------------------------------------

def get_document_type_counts(company_id: str) -> dict[str, int]:
    with get_cursor() as cur:
        rows = cur.execute(
            text(
                "SELECT document_type, COUNT(*) AS cnt "
                "FROM documents WHERE company_id = :cid "
                "GROUP BY document_type"
            ),
            {"cid": company_id},
        ).fetchall()
        return {str(r.document_type or "unknown"): r.cnt for r in rows}


def save_document(company_id: str, file_name: str) -> str:
    company_id = company_id.strip().lower()
    doc_id = str(uuid.uuid4())
    with get_cursor() as cur:
        cur.execute(
            text("INSERT INTO documents (document_id, company_id, file_name) VALUES (:doc_id, :company_id, :file_name)"),
            {"doc_id": doc_id, "company_id": company_id, "file_name": file_name},
        )
    return doc_id


def set_document_type(document_id: str, document_type: str):
    with get_cursor() as cur:
        cur.execute(
            text("UPDATE documents SET document_type=:document_type WHERE document_id=:document_id"),
            {"document_type": document_type, "document_id": document_id},
        )

# Chunks ------------------------------------------------------------------

def save_chunks(chunks: list[dict]):
    with get_cursor() as cur:
        for c in chunks:
            cur.execute(text("""
                INSERT INTO document_chunks
                    (chunk_id, document_id, chunk_text, chunk_index, page_number, section_title)
                VALUES (:chunk_id, :document_id, :chunk_text, :chunk_index, :page_number, :section_title)
            """), {
                "chunk_id": c["chunk_id"],
                "document_id": c["document_id"],
                "chunk_text": c["chunk_text"],
                "chunk_index": c["metadata"].get("chunk_index", 0),
                "page_number": c["metadata"].get("page_number"),
                "section_title": c["metadata"].get("section"),
            })


def get_chunks(document_id: str) -> list[dict]:
    with get_cursor() as cur:
        result = cur.execute(
            text("SELECT * FROM document_chunks WHERE document_id=:document_id ORDER BY chunk_index"),
            {"document_id": document_id},
        )
        return [row._mapping for row in result.fetchall()] #type:ignore


# Facts ------------------------------------------------------------------

def save_fact(fact_id: str, company_id: str, value, confidence: float,
              source_document: str, source_chunk: str | None,
              source_type: str, period: str):
    company_id = company_id.strip().lower()
    value_json = json.dumps(value)
    with get_cursor() as cur:
        cur.execute(text("""
            INSERT INTO facts (fact_id, company_id, value, confidence,
                               source_document, source_chunk, source_type, period)
            VALUES (:fact_id, :company_id, :value, :confidence,
                    :source_document, :source_chunk, :source_type, :period)
            ON CONFLICT (fact_id, company_id, period) DO UPDATE SET
                value           = excluded.value,
                confidence      = excluded.confidence,
                source_document = excluded.source_document,
                source_chunk    = excluded.source_chunk,
                source_type     = excluded.source_type,
                timestamp       = CURRENT_TIMESTAMP
        """), {
            "fact_id": fact_id, "company_id": company_id, "value": value_json,
            "confidence": confidence, "source_document": source_document,
            "source_chunk": source_chunk, "source_type": source_type, "period": period,
        })


def get_fact(fact_id: str, company_id: str, period: str) -> Optional[dict]:
    company_id = company_id.strip().lower()
    with get_cursor() as cur:
        result = cur.execute(
            text("SELECT * FROM facts WHERE fact_id=:fact_id AND company_id=:company_id AND period=:period"),
            {"fact_id": fact_id, "company_id": company_id, "period": period},
        )
        row = result.fetchone()
        return dict(row._mapping) if row else None


def get_facts_for_company(company_id: str, period: str) -> dict[str, any]: #type:ignore
    """Returns {fact_id: value} for all available facts."""
    company_id = company_id.strip().lower()
    with get_cursor() as cur:
        result = cur.execute(
            text("SELECT fact_id, value FROM facts WHERE company_id=:company_id AND period=:period"),
            {"company_id": company_id, "period": period},
        )
        return {row.fact_id: json.loads(row.value) for row in result.fetchall()}


# KPIs ------------------------------------------------------------------

def save_kpi(kpi_id: str, company_id: str, value: Optional[float],
             coverage: float, status: str, period: str):
    company_id = company_id.strip().lower()
    with get_cursor() as cur:
        cur.execute(text("""
            INSERT INTO kpis (kpi_id, company_id, value, coverage, status, period)
            VALUES (:kpi_id, :company_id, :value, :coverage, :status, :period)
            ON CONFLICT (kpi_id, company_id, period) DO UPDATE SET
                value     = excluded.value,
                coverage  = excluded.coverage,
                status    = excluded.status,
                timestamp = CURRENT_TIMESTAMP
        """), {
            "kpi_id": kpi_id, "company_id": company_id, "value": value,
            "coverage": coverage, "status": status, "period": period,
        })


def get_kpi(kpi_id: str, company_id: str, period: str) -> Optional[dict]:
    company_id = company_id.strip().lower()
    with get_cursor() as cur:
        result = cur.execute(
            text("SELECT * FROM kpis WHERE kpi_id=:kpi_id AND company_id=:company_id AND period=:period"),
            {"kpi_id": kpi_id, "company_id": company_id, "period": period},
        )
        row = result.fetchone()
        return dict(row._mapping) if row else None


def get_kpis_for_company(company_id: str, period: str) -> list[dict]:
    company_id = company_id.strip().lower()
    with get_cursor() as cur:
        result = cur.execute(
            text("SELECT * FROM kpis WHERE company_id=:company_id AND period=:period ORDER BY kpi_id"),
            {"company_id": company_id, "period": period},
        )
        return [dict(row._mapping) for row in result.fetchall()]
    
def get_kpis() -> list[dict]:
    with get_cursor() as cur:
        result = cur.execute(
            text("SELECT * FROM kpis ORDER BY kpi_id"),
        )
        return [dict(row._mapping) for row in result.fetchall()]


def get_kpi_history(kpi_id: str, company_id: str, limit: int = 6) -> list[dict]:
    company_id = company_id.strip().lower()
    with get_cursor() as cur:
        result = cur.execute(
            text("SELECT period, value, status FROM kpis "
                 "WHERE kpi_id=:kpi_id AND company_id=:company_id ORDER BY period DESC LIMIT :limit"),
            {"kpi_id": kpi_id, "company_id": company_id, "limit": limit},
        )
        return [dict(row._mapping) for row in result.fetchall()]


# Fact Observations (dated/time-series facts) ----------------------------
#
# Use these instead of save_fact()/get_fact() when a fact comes from a
# dated source - daily, weekly, or monthly rows spanning a date range -
# rather than a single value stated for one reporting period. Raw rows
# are stored exactly as given; month/quarter/year aggregation happens
# separately (in the KPI calculation layer), using each fact's
# aggregation_strategy from the registry, not here.

def save_observation(fact_id: str, company_id: str, observation_date: str,
                      value, confidence: Optional[float] = None,
                      source_document: Optional[str] = None,
                      source_type: Optional[str] = None):
    """
    Upsert one dated observation. observation_date must be an ISO
    'YYYY-MM-DD' string. One value per (fact_id, company_id, date) -
    re-ingesting the same fact+date overwrites the previous value rather
    than keeping both.
    """
    company_id = company_id.strip().lower()
    with get_cursor() as cur:
        cur.execute(text("""
            INSERT INTO fact_observations
                (fact_id, company_id, observation_date, value, confidence, source_document, source_type)
            VALUES (:fact_id, :company_id, :observation_date, :value, :confidence, :source_document, :source_type)
            ON CONFLICT (fact_id, company_id, observation_date) DO UPDATE SET
                value           = excluded.value,
                confidence      = excluded.confidence,
                source_document = excluded.source_document,
                source_type     = excluded.source_type,
                created_at      = CURRENT_TIMESTAMP
        """), {
            "fact_id": fact_id, "company_id": company_id, "observation_date": observation_date,
            "value": json.dumps(value), "confidence": confidence,
            "source_document": source_document, "source_type": source_type,
        })


def save_observations_bulk(observations: list[dict]) -> int:
    """
    Upsert many observations in a single transaction. Use this for
    tabular/time-series ingestion (e.g. years of daily rows) instead of
    calling save_observation() in a loop, which would open and commit a
    separate transaction per row.

    Each dict: {fact_id, company_id, observation_date, value,
                confidence?, source_document?, source_type?}

    Returns the number of rows written.
    """
    if not observations:
        return 0
    with get_cursor() as cur:
        for obs in observations:
            cur.execute(text("""
                INSERT INTO fact_observations
                    (fact_id, company_id, observation_date, value, confidence, source_document, source_type)
                VALUES (:fact_id, :company_id, :observation_date, :value, :confidence, :source_document, :source_type)
                ON CONFLICT (fact_id, company_id, observation_date) DO UPDATE SET
                    value           = excluded.value,
                    confidence      = excluded.confidence,
                    source_document = excluded.source_document,
                    source_type     = excluded.source_type,
                    created_at      = CURRENT_TIMESTAMP
            """), {
                "fact_id": obs["fact_id"],
                "company_id": obs["company_id"].strip().lower(),
                "observation_date": obs["observation_date"],
                "value": json.dumps(obs["value"]),
                "confidence": obs.get("confidence"),
                "source_document": obs.get("source_document"),
                "source_type": obs.get("source_type"),
            })
    return len(observations)


def get_observations_in_range(fact_id: str, company_id: str,
                               start_date: str, end_date: str) -> list[dict]:
    """
    Raw observations for one fact, ordered by date, within
    [start_date, end_date] inclusive (ISO 'YYYY-MM-DD' strings).

    Returns raw rows only - this is the building block for period
    aggregation (sum/latest/average), not an aggregator itself.
    """
    company_id = company_id.strip().lower()
    with get_cursor() as cur:
        result = cur.execute(text("""
            SELECT observation_date, value, confidence, source_document, source_type
            FROM fact_observations
            WHERE fact_id=:fact_id AND company_id=:company_id
              AND observation_date BETWEEN :start_date AND :end_date
            ORDER BY observation_date
        """), {
            "fact_id": fact_id, "company_id": company_id,
            "start_date": start_date, "end_date": end_date,
        })
        return [
            {**dict(row._mapping), "value": json.loads(row.value)}
            for row in result.fetchall()
        ]


def get_latest_observation_on_or_before(fact_id: str, company_id: str,
                                         as_of_date: str) -> Optional[dict]:
    """
    Most recent observation for a fact at or before as_of_date. Used for
    carry-forward when a period has no observation of its own (e.g. a
    maturity score measured quarterly, requested for a month with no
    new reading) - callers are responsible for flagging the result as
    carried-forward, this just returns the raw row.
    """
    company_id = company_id.strip().lower()
    with get_cursor() as cur:
        result = cur.execute(text("""
            SELECT observation_date, value, confidence, source_document, source_type
            FROM fact_observations
            WHERE fact_id=:fact_id AND company_id=:company_id
              AND observation_date <= :as_of_date
            ORDER BY observation_date DESC
            LIMIT 1
        """), {"fact_id": fact_id, "company_id": company_id, "as_of_date": as_of_date})
        row = result.fetchone()
        if row is None:
            return None
        return {**dict(row._mapping), "value": json.loads(row.value)}


def get_observation_date_range(fact_id: str, company_id: str) -> Optional[dict]:
    """
    {"min_date", "max_date"} across all observations for this fact, or
    None if there are no observations at all. Used to know which periods
    are even possible to compute before attempting to - never fabricate
    a period that has no underlying data anywhere in range.
    """
    company_id = company_id.strip().lower()
    with get_cursor() as cur:
        result = cur.execute(text("""
            SELECT MIN(observation_date) AS min_date, MAX(observation_date) AS max_date
            FROM fact_observations
            WHERE fact_id=:fact_id AND company_id=:company_id
        """), {"fact_id": fact_id, "company_id": company_id})
        row = result.fetchone()
        if row is None or row.min_date is None:
            return None
        return {"min_date": row.min_date, "max_date": row.max_date}


def get_distinct_observed_facts(company_id: str) -> list[str]:
    """
    All fact_ids that have at least one dated observation for this
    company - used by the aggregation layer to know which facts need
    period aggregation (from fact_observations) versus which facts only
    ever arrive as point-in-time values (the `facts` table).
    """
    company_id = company_id.strip().lower()
    with get_cursor() as cur:
        result = cur.execute(
            text("SELECT DISTINCT fact_id FROM fact_observations WHERE company_id=:company_id"),
            {"company_id": company_id},
        )
        return [row.fact_id for row in result.fetchall()]


# Financial Data ----------------------------------------------------

def get_financial_data(company_id: str) -> "pd.DataFrame":
    import pandas as pd
    with engine.connect() as conn:
        return pd.read_sql(
            text("SELECT * FROM financial_data WHERE LOWER(company_name) = :cid").bindparams(
                cid=company_id.lower()
            ),
            conn,
        )

def append_financial_data(frame: "pd.DataFrame"):
    frame.to_sql("financial_data", engine, if_exists="append", index=False)


# Registry DB Operations --------------------------------------------

class DependencyError(Exception):
    """Raised when removing a fact/KPI would orphan something that depends on it."""
    def __init__(self, message: str, dependents: list[str]):
        super().__init__(message)
        self.dependents = dependents

def _load_yaml(path: str) -> dict:
    import yaml
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
                    schema = REGISTRY_SCHEMA_PATH.read_text()
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

    @lru_cache(maxsize=1024)
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

# Administrative DB Operations --------------------------------------

def get_tables() -> list[str]:
    return inspect(engine).get_table_names()

def get_table_columns(table_name: str) -> list[dict]:
    return [{"name": c["name"], "type": str(c["type"])} for c in inspect(engine).get_columns(table_name)]

def get_pg_table_count(table_name: str) -> int:
    with engine.connect() as conn:
        return conn.execute(text(f'SELECT COUNT(*) FROM "{table_name}"')).scalar() #type:ignore

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
    table = Table(table_name, meta, autoload_with=engine)
    with engine.connect() as conn:
        query = table.select()
        if limit:
            query = query.limit(limit)
        rows = conn.execute(query).fetchall()
        cols = [c.name for c in table.columns]
    return [dict(zip(cols, row)) for row in rows]

def search_records_in_table(table_name: str, column: str, value: str, exact: bool = False) -> list[dict]:
    from sqlalchemy import MetaData, Table
    meta = MetaData()
    table = Table(table_name, meta, autoload_with=engine)
    if column not in [c.name for c in table.columns]:
        return []
    col = table.c[column]
    condition = (col == value) if exact else col.like(f"%{value}%")
    with engine.connect() as conn:
        rows = conn.execute(table.select().where(condition)).fetchall()
        cols = [c.name for c in table.columns]
    return [dict(zip(cols, row)) for row in rows]

def delete_records_from_table(table_name: str, column: str, value: str, exact: bool = True) -> int:
    from sqlalchemy import MetaData, Table
    meta = MetaData()
    table = Table(table_name, meta, autoload_with=engine)
    if column not in [c.name for c in table.columns]:
        return 0
    col = table.c[column]
    condition = (col == value) if exact else col.like(f"%{value}%")
    with engine.begin() as conn:
        result = conn.execute(table.delete().where(condition))
        return result.rowcount

def delete_all_records_from_table(table_name: str) -> int:
    from sqlalchemy import MetaData, Table
    meta = MetaData()
    table = Table(table_name, meta, autoload_with=engine)
    with engine.begin() as conn:
        result = conn.execute(table.delete())
        return result.rowcount

def drop_pg_table(table_name: str):
    from sqlalchemy import MetaData, Table
    meta = MetaData()
    table = Table(table_name, meta, autoload_with=engine)
    table.drop(engine)

# Text-to-SQL Chatbot Helpers (SQLite) ------------------------------
import sqlite3
from pathlib import Path

def get_sqlite_table_names(db_path: Path) -> list[dict]:
    with sqlite3.connect(db_path) as conn:
        conn.row_factory = sqlite3.Row
        return conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' "
            "AND name IN ('financial_data', 'kpis', 'fact_observations', 'facts') ORDER BY name"
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
            "FROM financial_data GROUP BY company_name ORDER BY company_name"
        ).fetchall()

def get_sqlite_date_range(db_path: Path) -> dict:
    with sqlite3.connect(db_path) as conn:
        conn.row_factory = sqlite3.Row
        return dict(conn.execute("SELECT MIN(date) AS min_date, MAX(date) AS max_date FROM financial_data").fetchone())

def check_sqlite_table_exists(db_path: Path, table: str) -> bool:
    with sqlite3.connect(db_path) as conn:
        conn.row_factory = sqlite3.Row
        tables = conn.execute("SELECT name FROM sqlite_master WHERE type='table' AND name=?", (table,)).fetchall()
        return len(tables) > 0

def get_sqlite_company_row_count(db_path: Path, company_id: str) -> int:
    with sqlite3.connect(db_path) as conn:
        conn.row_factory = sqlite3.Row
        return conn.execute(
            "SELECT COUNT(*) AS cnt FROM financial_data WHERE LOWER(company_name) LIKE ?",
            (f"%{company_id.lower()}%",),
        ).fetchone()["cnt"]

def get_sqlite_company_years(db_path: Path, company_id: str) -> list[int]:
    with sqlite3.connect(db_path) as conn:
        conn.row_factory = sqlite3.Row
        years_rows = conn.execute(
            "SELECT DISTINCT financial_year FROM financial_data "
            "WHERE LOWER(company_name) LIKE ? AND financial_year IS NOT NULL ORDER BY financial_year",
            (f"%{company_id.lower()}%",),
        ).fetchall()
        return [r["financial_year"] for r in years_rows]

def explain_sqlite_query(db_path: Path, sql: str) -> list[dict]:
    with sqlite3.connect(db_path) as conn:
        conn.row_factory = sqlite3.Row
        return [dict(r) for r in conn.execute(f"EXPLAIN QUERY PLAN {sql}").fetchall()]

def execute_sqlite_query(db_path: Path, sql: str, limit: int) -> list[dict]:
    with sqlite3.connect(db_path) as conn:
        conn.row_factory = sqlite3.Row
        return [dict(r) for r in conn.execute(sql).fetchmany(limit)]
