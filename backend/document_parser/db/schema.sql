-- =====================================================================
-- PE AI Observability & Control Tower - Database Schema
-- =====================================================================

-- Stage 1: Parsed document chunks (no embeddings)
CREATE TABLE document_chunks (
    chunk_id        UUID PRIMARY KEY,
    document_id     UUID NOT NULL,
    company_id      TEXT NOT NULL,
    file_name       TEXT NOT NULL,
    document_type   TEXT,                -- set by Document Classification Agent
    chunk_text      TEXT NOT NULL,
    metadata        JSONB DEFAULT '{}',  -- {"section":..., "chunk_index":...}
    created_at      TIMESTAMPTZ DEFAULT now()
);

CREATE INDEX idx_chunks_document ON document_chunks(document_id);
CREATE INDEX idx_chunks_company_doctype ON document_chunks(company_id, document_type);

-- Document-level classification result (Stage 3, runs once per document)
CREATE TABLE document_classifications (
    document_id     UUID PRIMARY KEY,
    company_id      TEXT NOT NULL,
    document_type   TEXT NOT NULL,
    confidence      FLOAT NOT NULL,
    classified_at   TIMESTAMPTZ DEFAULT now()
);

-- Stage 7: Fact Store
CREATE TABLE facts (
    id              BIGSERIAL PRIMARY KEY,
    fact_id         TEXT NOT NULL,
    company_id      TEXT NOT NULL,
    value           JSONB NOT NULL,      -- numeric, string, bool depending on data_type
    confidence      FLOAT NOT NULL,
    document_id     UUID NOT NULL,
    chunk_id        UUID NOT NULL,
    source_type     TEXT NOT NULL,       -- document_type used for source_priority resolution
    period          TEXT,                -- e.g. '2026-06' for time-scoped facts
    "timestamp"     TIMESTAMPTZ DEFAULT now(),
    UNIQUE (fact_id, company_id, period)
);

CREATE INDEX idx_facts_company_fact ON facts(company_id, fact_id);

-- Stage 10: KPI Store
CREATE TABLE kpis (
    id              BIGSERIAL PRIMARY KEY,
    kpi_id          TEXT NOT NULL,
    company_id      TEXT NOT NULL,
    value           FLOAT,
    coverage        FLOAT NOT NULL,
    calculated_on   TIMESTAMPTZ DEFAULT now(),
    status          TEXT NOT NULL,       -- calculated | insufficient_data | unsupported_formula
    period          TEXT,
    UNIQUE (kpi_id, company_id, period)
);

CREATE INDEX idx_kpis_company_kpi ON kpis(company_id, kpi_id);

-- Optional: insight agent outputs, for audit/history
CREATE TABLE insights (
    id              BIGSERIAL PRIMARY KEY,
    company_id      TEXT,
    question        TEXT NOT NULL,
    response        TEXT NOT NULL,
    created_at      TIMESTAMPTZ DEFAULT now()
);
