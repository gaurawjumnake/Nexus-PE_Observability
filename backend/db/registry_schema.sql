PRAGMA foreign_keys = ON;

-- ============================================================
-- FACTS
-- ============================================================

CREATE TABLE facts (
    fact_id                 TEXT PRIMARY KEY,
    name                    TEXT NOT NULL,
    category                TEXT NOT NULL,
    description             TEXT,
    business_definition     TEXT,
    data_type               TEXT NOT NULL,
    unit                    TEXT,
    fact_type               TEXT NOT NULL,
    aggregation_strategy    TEXT,
    missing_value_strategy  TEXT,
    status                  TEXT DEFAULT 'active',
    validation_rules        TEXT,
    normalization_rules     TEXT,
    confidence_rules        TEXT,
    example_values          TEXT
);

CREATE INDEX idx_facts_category
ON facts(category);

CREATE INDEX idx_facts_name
ON facts(name);


-- ============================================================
-- FACT ALIASES
-- ============================================================

CREATE TABLE fact_aliases (
    fact_id     TEXT NOT NULL,
    alias       TEXT NOT NULL,

    PRIMARY KEY (fact_id, alias),

    FOREIGN KEY (fact_id)
        REFERENCES facts(fact_id)
        ON DELETE CASCADE
);

CREATE INDEX idx_fact_aliases_alias
ON fact_aliases(alias);


-- ============================================================
-- FACT EXTRACTION PATTERNS
-- ============================================================

CREATE TABLE fact_extraction_patterns (
    fact_id     TEXT NOT NULL,
    pattern     TEXT NOT NULL,

    PRIMARY KEY (fact_id, pattern),

    FOREIGN KEY (fact_id)
        REFERENCES facts(fact_id)
        ON DELETE CASCADE
);

CREATE INDEX idx_fact_patterns_pattern
ON fact_extraction_patterns(pattern);


-- ============================================================
-- FACT DOCUMENT SOURCES
-- ============================================================

CREATE TABLE fact_document_sources (
    fact_id         TEXT NOT NULL,
    document_type   TEXT NOT NULL,
    priority_rank   INTEGER NOT NULL,

    PRIMARY KEY (fact_id, document_type),

    FOREIGN KEY (fact_id)
        REFERENCES facts(fact_id)
        ON DELETE CASCADE
);

CREATE INDEX idx_fact_doc_sources_doctype
ON fact_document_sources(document_type);


-- ============================================================
-- FACT RELATIONSHIPS
-- ============================================================

CREATE TABLE fact_related (
    fact_id             TEXT NOT NULL,
    related_fact_id     TEXT NOT NULL,

    PRIMARY KEY (fact_id, related_fact_id),

    FOREIGN KEY (fact_id)
        REFERENCES facts(fact_id)
        ON DELETE CASCADE,

    FOREIGN KEY (related_fact_id)
        REFERENCES facts(fact_id)
        ON DELETE CASCADE
);

CREATE INDEX idx_fact_related_target
ON fact_related(related_fact_id);


-- ============================================================
-- KPIS
-- ============================================================

CREATE TABLE kpis (
    kpi_id                  TEXT PRIMARY KEY,
    name                    TEXT NOT NULL,
    category                TEXT,
    tier                    TEXT,
    description             TEXT,
    business_value          TEXT,
    formula                 TEXT,
    unit                    TEXT,
    aggregation             TEXT,
    frequency               TEXT,
    missing_data_strategy   TEXT,
    status                  TEXT DEFAULT 'active',
    thresholds              TEXT,
    data_quality            TEXT,
    benchmarking            TEXT,
    example_calculation     TEXT
);

CREATE INDEX idx_kpis_category
ON kpis(category);

CREATE INDEX idx_kpis_name
ON kpis(name);


-- ============================================================
-- KPI REQUIRED FACTS
-- ============================================================

CREATE TABLE kpi_required_facts (
    kpi_id      TEXT NOT NULL,
    fact_id     TEXT NOT NULL,

    PRIMARY KEY (kpi_id, fact_id),

    FOREIGN KEY (kpi_id)
        REFERENCES kpis(kpi_id)
        ON DELETE CASCADE,

    FOREIGN KEY (fact_id)
        REFERENCES facts(fact_id)
        ON DELETE CASCADE
);

CREATE INDEX idx_kpi_required_facts_fact
ON kpi_required_facts(fact_id);


-- ============================================================
-- KPI DERIVED FACTS
-- ============================================================

CREATE TABLE kpi_derived_facts (
    kpi_id      TEXT NOT NULL,
    fact_id     TEXT NOT NULL,

    PRIMARY KEY (kpi_id, fact_id),

    FOREIGN KEY (kpi_id)
        REFERENCES kpis(kpi_id)
        ON DELETE CASCADE,

    FOREIGN KEY (fact_id)
        REFERENCES facts(fact_id)
        ON DELETE CASCADE
);

CREATE INDEX idx_kpi_derived_facts_fact
ON kpi_derived_facts(fact_id);


-- ============================================================
-- KPI REQUIRED DOCUMENTS
-- ============================================================

CREATE TABLE kpi_required_documents (
    kpi_id          TEXT NOT NULL,
    document_type   TEXT NOT NULL,

    PRIMARY KEY (kpi_id, document_type),

    FOREIGN KEY (kpi_id)
        REFERENCES kpis(kpi_id)
        ON DELETE CASCADE
);

CREATE INDEX idx_kpi_required_docs_doctype
ON kpi_required_documents(document_type);


-- ============================================================
-- FULL TEXT SEARCH
-- ============================================================

CREATE VIRTUAL TABLE registry_fts USING fts5(
    entity_type,
    entity_id,
    name,
    text,
    tokenize='porter'
);


-- ============================================================
-- REGISTRY METADATA
-- ============================================================

CREATE TABLE registry_meta (
    key     TEXT PRIMARY KEY,
    value   TEXT
);

-- ============================================================
-- FACT → KPI RELATIONSHIPS  (related_facts that are KPI ids)
-- ============================================================
CREATE TABLE IF NOT EXISTS fact_kpi_related (
    fact_id     TEXT NOT NULL,
    kpi_id      TEXT NOT NULL,

    PRIMARY KEY (fact_id, kpi_id),

    FOREIGN KEY (fact_id) REFERENCES facts(fact_id) ON DELETE CASCADE,
    FOREIGN KEY (kpi_id)  REFERENCES kpis(kpi_id)   ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS idx_fact_kpi_related_kpi ON fact_kpi_related(kpi_id);