-- nexus_documents

CREATE TABLE IF NOT EXISTS nexus_documents (
    document_id     TEXT PRIMARY KEY,
    company_id      TEXT NOT NULL,
    file_name       TEXT NOT NULL,
    document_type   TEXT,
    created_at      TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX IF NOT EXISTS idx_nexus_documents_company
ON nexus_documents(company_id);


-- nexus_document_chunks

CREATE TABLE IF NOT EXISTS nexus_document_chunks (
    chunk_id        TEXT PRIMARY KEY,
    document_id     TEXT NOT NULL,
    chunk_text      TEXT NOT NULL,
    chunk_index     INTEGER NOT NULL,
    page_number     INTEGER,
    section_title   TEXT,
    created_at      TIMESTAMP DEFAULT CURRENT_TIMESTAMP,

    FOREIGN KEY (document_id)
        REFERENCES nexus_documents(document_id)
        ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS idx_nexus_chunks_document
ON nexus_document_chunks(document_id);


-- nexus_facts
-- Point-in-time fact values, one row per (fact_id, company_id, period).

CREATE TABLE IF NOT EXISTS nexus_facts (
    fact_id             TEXT NOT NULL,
    company_id          TEXT NOT NULL,
    value               TEXT NOT NULL,
    confidence          REAL NOT NULL,
    source_document     TEXT,
    source_chunk        TEXT,
    source_type         TEXT NOT NULL,
    period              TEXT,
    timestamp           TIMESTAMP DEFAULT CURRENT_TIMESTAMP,

    PRIMARY KEY (fact_id, company_id, period),

    FOREIGN KEY (source_document)
        REFERENCES nexus_documents(document_id),

    FOREIGN KEY (source_chunk)
        REFERENCES nexus_document_chunks(chunk_id)
);

CREATE INDEX IF NOT EXISTS idx_nexus_facts_company
ON nexus_facts(company_id);

CREATE INDEX IF NOT EXISTS idx_nexus_facts_fact_id
ON nexus_facts(fact_id);


-- nexus_fact_observations
-- Raw dated observations for time-series facts. Never pre-aggregated.

CREATE TABLE IF NOT EXISTS nexus_fact_observations (
    fact_id             TEXT NOT NULL,
    company_id          TEXT NOT NULL,
    observation_date    TEXT NOT NULL,
    value               TEXT NOT NULL,
    confidence          REAL,
    source_document     TEXT,
    source_type         TEXT,
    created_at          TIMESTAMP DEFAULT CURRENT_TIMESTAMP,

    PRIMARY KEY (fact_id, company_id, observation_date),

    FOREIGN KEY (source_document)
        REFERENCES nexus_documents(document_id)
);

CREATE INDEX IF NOT EXISTS idx_nexus_fact_obs_company
ON nexus_fact_observations(company_id);

CREATE INDEX IF NOT EXISTS idx_nexus_fact_obs_fact
ON nexus_fact_observations(fact_id, company_id);

CREATE INDEX IF NOT EXISTS idx_nexus_fact_obs_date
ON nexus_fact_observations(observation_date);


-- nexus_kpis

CREATE TABLE IF NOT EXISTS nexus_kpis (
    kpi_id          TEXT NOT NULL,
    company_id      TEXT NOT NULL,
    value           REAL,
    coverage        REAL NOT NULL,
    status          TEXT NOT NULL,
    period          TEXT,
    timestamp       TIMESTAMP DEFAULT CURRENT_TIMESTAMP,

    PRIMARY KEY (kpi_id, company_id, period)
);

CREATE INDEX IF NOT EXISTS idx_nexus_kpis_company
ON nexus_kpis(company_id);


-- nexus_jobs
-- Background job tracking for async operations (extract, calculate, insights).

CREATE TABLE IF NOT EXISTS nexus_jobs (
    job_id      TEXT PRIMARY KEY,
    job_type    TEXT NOT NULL,
    status      TEXT NOT NULL DEFAULT 'pending',
    params      TEXT NOT NULL,
    result      TEXT,
    error       TEXT,
    created_at  TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    updated_at  TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX IF NOT EXISTS idx_nexus_jobs_status
ON nexus_jobs(status, created_at DESC);


-- nexus_financial_data

CREATE TABLE IF NOT EXISTS nexus_financial_data (
    id               SERIAL PRIMARY KEY,
    company_name     TEXT,
    financial_year   INTEGER,
    date             TEXT,
    ai_revenue       REAL,
    total_ai_spend   REAL,
    cost_savings     REAL,
    ebitda_uplift    REAL
);

CREATE INDEX IF NOT EXISTS idx_nexus_financial_company
ON nexus_financial_data(company_name);

CREATE INDEX IF NOT EXISTS idx_nexus_financial_year
ON nexus_financial_data(financial_year);
