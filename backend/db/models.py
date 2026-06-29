from sqlalchemy import Column, Text, Integer, Float, TIMESTAMP, ForeignKey, func
from sqlalchemy.orm import DeclarativeBase, relationship


class Base(DeclarativeBase):
    pass


class Document(Base):
    __tablename__ = "nexus_documents"

    document_id   = Column(Text, primary_key=True)
    company_id    = Column(Text, nullable=False, index=True)
    file_name     = Column(Text, nullable=False)
    document_type = Column(Text)
    created_at    = Column(TIMESTAMP, server_default=func.now())

    chunks = relationship("DocumentChunk", back_populates="document", cascade="all, delete-orphan")


class DocumentChunk(Base):
    __tablename__ = "nexus_document_chunks"

    chunk_id      = Column(Text, primary_key=True)
    document_id   = Column(Text, ForeignKey("nexus_documents.document_id", ondelete="CASCADE"), nullable=False, index=True)
    chunk_text    = Column(Text, nullable=False)
    chunk_index   = Column(Integer, nullable=False)
    page_number   = Column(Integer)
    section_title = Column(Text)
    created_at    = Column(TIMESTAMP, server_default=func.now())

    document = relationship("Document", back_populates="chunks")


class Fact(Base):
    __tablename__ = "nexus_facts"

    fact_id         = Column(Text, primary_key=True)
    company_id      = Column(Text, primary_key=True, index=True)
    period          = Column(Text, primary_key=True)
    value           = Column(Text, nullable=False)
    confidence      = Column(Float, nullable=False)
    source_document = Column(Text, ForeignKey("nexus_documents.document_id"))
    source_chunk    = Column(Text, ForeignKey("nexus_document_chunks.chunk_id"))
    source_type     = Column(Text, nullable=False)
    timestamp       = Column(TIMESTAMP, server_default=func.now())


class FactObservation(Base):
    __tablename__ = "nexus_fact_observations"

    fact_id          = Column(Text, primary_key=True)
    company_id       = Column(Text, primary_key=True, index=True)
    observation_date = Column(Text, primary_key=True, index=True)
    value            = Column(Text, nullable=False)
    confidence       = Column(Float)
    source_document  = Column(Text, ForeignKey("nexus_documents.document_id"))
    source_type      = Column(Text)
    created_at       = Column(TIMESTAMP, server_default=func.now())


class KPI(Base):
    __tablename__ = "nexus_kpis"

    kpi_id     = Column(Text, primary_key=True)
    company_id = Column(Text, primary_key=True, index=True)
    period     = Column(Text, primary_key=True)
    value      = Column(Float)
    coverage   = Column(Float, nullable=False)
    status     = Column(Text, nullable=False)
    timestamp  = Column(TIMESTAMP, server_default=func.now())


class FinancialData(Base):
    __tablename__ = "nexus_financial_data"

    id             = Column(Integer, primary_key=True, autoincrement=True)
    company_name   = Column(Text, index=True)
    financial_year = Column(Integer, index=True)
    date           = Column(Text)
    ai_revenue     = Column(Float)
    total_ai_spend = Column(Float)
    cost_savings   = Column(Float)
    ebitda_uplift  = Column(Float)
