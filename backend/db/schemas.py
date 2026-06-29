from __future__ import annotations
from typing import Any, Optional
from datetime import datetime
from pydantic import BaseModel, field_validator


class _Base(BaseModel):
    model_config = {"from_attributes": True}


# ---------------------------------------------------------------------------
# Documents
# ---------------------------------------------------------------------------

class DocumentCreate(_Base):
    document_id: str
    company_id: str
    file_name: str
    document_type: Optional[str] = None

    @field_validator("company_id")
    @classmethod
    def normalise_company(cls, v: str) -> str:
        return v.strip().lower()


class DocumentRead(DocumentCreate):
    created_at: Optional[datetime] = None


# ---------------------------------------------------------------------------
# Document Chunks
# ---------------------------------------------------------------------------

class ChunkCreate(_Base):
    chunk_id: str
    document_id: str
    chunk_text: str
    chunk_index: int
    page_number: Optional[int] = None
    section_title: Optional[str] = None


class ChunkRead(ChunkCreate):
    created_at: Optional[datetime] = None


# ---------------------------------------------------------------------------
# Facts
# ---------------------------------------------------------------------------

class FactCreate(_Base):
    fact_id: str
    company_id: str
    period: str
    value: Any                      # stored as JSON text
    confidence: float
    source_document: Optional[str] = None
    source_chunk: Optional[str] = None
    source_type: str

    @field_validator("company_id")
    @classmethod
    def normalise_company(cls, v: str) -> str:
        return v.strip().lower()


class FactRead(FactCreate):
    timestamp: Optional[datetime] = None


# ---------------------------------------------------------------------------
# Fact Observations
# ---------------------------------------------------------------------------

class ObservationCreate(_Base):
    fact_id: str
    company_id: str
    observation_date: str           # ISO YYYY-MM-DD
    value: Any
    confidence: Optional[float] = None
    source_document: Optional[str] = None
    source_type: Optional[str] = None

    @field_validator("company_id")
    @classmethod
    def normalise_company(cls, v: str) -> str:
        return v.strip().lower()


class ObservationRead(ObservationCreate):
    created_at: Optional[datetime] = None


# ---------------------------------------------------------------------------
# KPIs
# ---------------------------------------------------------------------------

class KPICreate(_Base):
    kpi_id: str
    company_id: str
    period: str
    value: Optional[float] = None
    coverage: float
    status: str

    @field_validator("company_id")
    @classmethod
    def normalise_company(cls, v: str) -> str:
        return v.strip().lower()


class KPIRead(KPICreate):
    timestamp: Optional[datetime] = None
