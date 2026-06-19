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

DATABASE_URL = os.getenv("DATABASE_URL", "sqlite:///backend/kpi_extractor/app/db/nexus.db")
engine = create_engine(DATABASE_URL, connect_args={"check_same_thread": False})
SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)


def _init_db():
    """Create tables if they don't already exist."""
    existing = inspect(engine).get_table_names()
    required = {"documents", "document_chunks", "facts", "kpis"}

    if required.issubset(existing):
        log.log_info("✅ Database exists with all required tables")
        return

    missing = required - set(existing)
    log.log_info(f"⚙️  Creating missing tables: {missing}")

    with open("backend/kpi_extractor/app/db/nexus_schema.sql", "r") as f:
        SCHEMA = f.read()

    with engine.connect() as conn:
        for statement in SCHEMA.strip().split(";"):
            stmt = statement.strip()
            if stmt:
                conn.execute(text(stmt))
        conn.commit()
    log.log_info("✅ Schema created successfully")


try:
    with engine.connect() as conn:
        log.log_info("✅ Database connected successfully")
    _init_db()
except Exception as e:
    log.log_error(f"❌ Database initialization failed: {e}")
    raise


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


# ------------------------------------------------------------------
# Documents
# ------------------------------------------------------------------
def save_document(company_id: str, file_name: str) -> str:
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


# ------------------------------------------------------------------
# Chunks
# ------------------------------------------------------------------
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


# ------------------------------------------------------------------
# Facts
# ------------------------------------------------------------------
def save_fact(fact_id: str, company_id: str, value, confidence: float,
              source_document: str, source_chunk: str,
              source_type: str, period: str):
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
    with get_cursor() as cur:
        result = cur.execute(
            text("SELECT * FROM facts WHERE fact_id=:fact_id AND company_id=:company_id AND period=:period"),
            {"fact_id": fact_id, "company_id": company_id, "period": period},
        )
        row = result.fetchone()
        return dict(row._mapping) if row else None


def get_facts_for_company(company_id: str, period: str) -> dict[str, any]: #type:ignore
    """Returns {fact_id: value} for all available facts."""
    with get_cursor() as cur:
        result = cur.execute(
            text("SELECT fact_id, value FROM facts WHERE company_id=:company_id AND period=:period"),
            {"company_id": company_id, "period": period},
        )
        return {row.fact_id: json.loads(row.value) for row in result.fetchall()}


# ------------------------------------------------------------------
# KPIs
# ------------------------------------------------------------------
def save_kpi(kpi_id: str, company_id: str, value: Optional[float],
             coverage: float, status: str, period: str):
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
    with get_cursor() as cur:
        result = cur.execute(
            text("SELECT * FROM kpis WHERE kpi_id=:kpi_id AND company_id=:company_id AND period=:period"),
            {"kpi_id": kpi_id, "company_id": company_id, "period": period},
        )
        row = result.fetchone()
        return dict(row._mapping) if row else None


def get_kpis_for_company(company_id: str, period: str) -> list[dict]:
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
    with get_cursor() as cur:
        result = cur.execute(
            text("SELECT period, value, status FROM kpis "
                 "WHERE kpi_id=:kpi_id AND company_id=:company_id ORDER BY period DESC LIMIT :limit"),
            {"kpi_id": kpi_id, "company_id": company_id, "limit": limit},
        )
        return [dict(row._mapping) for row in result.fetchall()]