-- documents

CREATE TABLE IF NOT EXISTS documents (
    document_id     TEXT PRIMARY KEY,
    company_id      TEXT NOT NULL,
    file_name       TEXT NOT NULL,
    document_type   TEXT,
    created_at      DATETIME DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX idx_documents_company
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

CREATE INDEX idx_chunks_document
ON document_chunks(document_id);


-- facts

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

CREATE INDEX idx_facts_company
ON facts(company_id);

CREATE INDEX idx_facts_fact_id
ON facts(fact_id);


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

CREATE INDEX idx_kpis_company
ON kpis(company_id);