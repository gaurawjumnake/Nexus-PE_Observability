-- documents

CREATE TABLE IF NOT EXISTS documents (
    document_id     TEXT PRIMARY KEY,
    company_id      TEXT NOT NULL,
    file_name       TEXT NOT NULL,
    document_type   TEXT,
    created_at      DATETIME DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX IF NOT EXISTS idx_documents_company
ON documents(company_id);


-- document_chunks

CREATE TABLE IF NOT EXISTS document_chunks (
    chunk_id        TEXT PRIMARY KEY,
    document_id     TEXT NOT NULL,
    chunk_text      TEXT NOT NULL,
    chunk_index     INTEGER NOT NULL,
    page_number     INTEGER,
    section_title   TEXT,
    created_at      DATETIME DEFAULT CURRENT_TIMESTAMP,

    FOREIGN KEY (document_id)
        REFERENCES documents(document_id)
        ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS idx_chunks_document
ON document_chunks(document_id);


-- facts
-- Point-in-time fact values, one row per (fact_id, company_id, period).
-- Used for documents that state a value directly for a single reporting
-- period (e.g. a narrative doc saying "Q2 2026 AI ROI: 7.0"). For facts
-- that arrive as a dated time series (e.g. 4 years of daily values),
-- see fact_observations below instead - this table is NOT used for those.

CREATE TABLE IF NOT EXISTS facts (
    fact_id             TEXT NOT NULL,
    company_id          TEXT NOT NULL,

    value               TEXT NOT NULL,   -- JSON stored as text

    confidence          REAL NOT NULL,

    source_document     TEXT,
    source_chunk        TEXT,

    source_type         TEXT NOT NULL,
    period              TEXT,

    timestamp           DATETIME DEFAULT CURRENT_TIMESTAMP,

    PRIMARY KEY (fact_id, company_id, period),

    FOREIGN KEY (source_document)
        REFERENCES documents(document_id),

    FOREIGN KEY (source_chunk)
        REFERENCES document_chunks(chunk_id)
);

CREATE INDEX IF NOT EXISTS idx_facts_company
ON facts(company_id);

CREATE INDEX IF NOT EXISTS idx_facts_fact_id
ON facts(fact_id);


-- fact_observations
-- Raw, dated observations for facts extracted from time-series / tabular
-- sources (e.g. daily KPI values spanning multiple years). Stored at
-- whatever granularity the source actually provides (daily, weekly,
-- monthly, ...) - NEVER pre-aggregated at ingestion time. Month/quarter/
-- year rollups (MoM, QoQ, YoY) are computed on demand from these raw rows
-- using each fact's aggregation_strategy (sum / latest_value / average,
-- from the registry), so no granularity is ever lost or guessed at.
--
-- One value per (fact_id, company_id, observation_date): re-ingesting the
-- same fact for the same date overwrites the previous value rather than
-- keeping both (no multi-source conflict tracking for observations).

CREATE TABLE IF NOT EXISTS fact_observations (
    fact_id             TEXT NOT NULL,
    company_id          TEXT NOT NULL,

    observation_date    TEXT NOT NULL,   -- ISO 'YYYY-MM-DD'

    value               TEXT NOT NULL,   -- JSON stored as text (matches facts.value)
    confidence          REAL,

    source_document     TEXT,
    source_type         TEXT,            -- classified document_type, e.g. 'financial'

    created_at          DATETIME DEFAULT CURRENT_TIMESTAMP,

    PRIMARY KEY (fact_id, company_id, observation_date),

    FOREIGN KEY (source_document)
        REFERENCES documents(document_id)
);

CREATE INDEX IF NOT EXISTS idx_fact_observations_company
ON fact_observations(company_id);

CREATE INDEX IF NOT EXISTS idx_fact_observations_fact
ON fact_observations(fact_id, company_id);

CREATE INDEX IF NOT EXISTS idx_fact_observations_date
ON fact_observations(observation_date);


-- kpis

CREATE TABLE IF NOT EXISTS kpis (
    kpi_id          TEXT NOT NULL,
    company_id      TEXT NOT NULL,

    value           REAL,

    coverage        REAL NOT NULL,
    status          TEXT NOT NULL,

    period          TEXT,

    timestamp       DATETIME DEFAULT CURRENT_TIMESTAMP,

    PRIMARY KEY (kpi_id, company_id, period)
);

CREATE INDEX IF NOT EXISTS idx_kpis_company
ON kpis(company_id);



CREATE TABLE IF NOT EXISTS financial_data (
    company_name    TEXT,
    financial_year   INTEGER,
    date             TEXT,

    ai_revenue       REAL,
    total_ai_spend   REAL,
    cost_savings     REAL,
    ebitda_uplift    REAL
);

CREATE INDEX IF NOT EXISTS idx_financial_data_company
ON financial_data(company_name);

CREATE INDEX IF NOT EXISTS idx_financial_data_year
ON financial_data(financial_year);