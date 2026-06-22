import os
import json
import uuid
from contextlib import contextmanager
from typing import Optional
from dotenv import load_dotenv
load_dotenv()

from backend.utilites.app_logger import Logger
log = Logger()

from sqlalchemy import create_engine, text, inspect
from sqlalchemy.orm import sessionmaker

from backend.config import DB_DIR, DEFAULT_DB_PATH, CHROMA_DIR, DATABASE_URL

# Re-export as strings for callers that expect os.path-style string paths.
DB_DIR      = str(DB_DIR)
DEFAULT_DB_PATH = str(DEFAULT_DB_PATH)
CHROMA_DIR  = str(CHROMA_DIR)

engine = create_engine(DATABASE_URL, connect_args={"check_same_thread": False})
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

# Documents ---------------------------------------------------------

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
              source_document: str, source_chunk: str,
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