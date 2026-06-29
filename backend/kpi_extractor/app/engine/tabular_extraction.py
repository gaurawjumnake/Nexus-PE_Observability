"""
Tabular Fact Extraction  (Stage 4+5, tabular counterpart)
=============================================================
100% deterministic. No LLM. No agents.

Counterpart to FactExtractionAgent for the structured output produced by
DoclingDocumentParser.extract_structured_output() - i.e. tables that
were NOT flattened into markdown because they either have a detected
date column (time_series_tables) or come from a native tabular file
type with no prose to preserve (structured_tables).

Column headers are matched to fact_ids using the SAME alias /
extraction_pattern data already used for narrative candidate-finding in
fact_extraction_agent._find_candidates - just matched against a short
header string instead of scanned across prose. No LLM call: a table
header is already exactly the kind of short, explicit label this
alias data is built to match, and guessing a value from a known cell is
strictly easier (and cheaper, and more correct) than guessing it from
paragraphs of text.

Confidence is fixed at 1.0 for every tabular extraction - there is no
LLM guess involved, the value is read verbatim from a structured cell.
An unmatched column is skipped, never guessed.
"""

import re
from collections import Counter
from typing import Optional

# Minimum alias/term length to use for substring matching - prevents
# very short aliases (e.g. "id", "ai") from false-matching unrelated
# headers via naive substring search.
_MIN_TERM_LEN_FOR_SUBSTRING = 4


def _normalize(text: str) -> str:
    return re.sub(r"[_\-]+", " ", text.strip().lower())


def _collapse(text: str) -> str:
    """Remove all non-alphanumeric chars — lets 'activeaiusers' match 'active_ai_users'."""
    return re.sub(r"[^a-z0-9]", "", text.strip().lower())


def map_column_to_fact(column_header: str, relevant_facts: list[dict]) -> Optional[dict]:
    """
    Matches one table column header against a set of facts' aliases /
    extraction_patterns / fact_id (word-boundary match, case-insensitive,
    underscores and hyphens treated as spaces). Returns the best-matching
    fact dict, or None if nothing matches confidently - callers should
    skip the column, not guess.

    `relevant_facts` should already be scoped to the document's type
    (registry.discover_relevant_facts(document_type)) so an unrelated
    fact from a different domain can't accidentally win a fuzzy match.
    """
    header_norm = _normalize(column_header)
    header_collapsed = _collapse(column_header)
    if not header_norm:
        return None

    best_match, best_score = None, 0
    for fact in relevant_facts:
        terms = [fact["fact_id"]] + fact.get("aliases", []) + fact.get("extraction_patterns", [])
        for term in terms:
            term_norm = _normalize(term)
            if not term_norm:
                continue
            if term_norm == header_norm:
                return fact  # exact match - can't do better than this
            # Collapsed match: handles column headers where word separators
            # were stripped (e.g. Docling exports 'active_ai_users' → 'activeaiusers').
            term_collapsed = _collapse(term)
            if term_collapsed and term_collapsed == header_collapsed and len(term_collapsed) >= _MIN_TERM_LEN_FOR_SUBSTRING:
                return fact
            # Word-boundary substring match, but only when BOTH sides are
            # long enough to be meaningful - "ai" matching as a standalone
            # word inside "monthly ai return" is a false positive, not a
            # real match, even though "monthly ai return" itself is long.
            if len(term_norm) < _MIN_TERM_LEN_FOR_SUBSTRING or len(header_norm) < _MIN_TERM_LEN_FOR_SUBSTRING:
                continue
            if re.search(rf"\b{re.escape(term_norm)}\b", header_norm) or \
               re.search(rf"\b{re.escape(header_norm)}\b", term_norm):
                score = len(term_norm)
                if score > best_score:
                    best_score, best_match = score, fact

    return best_match


def classify_document_type_from_columns(columns: list[str], registry) -> Optional[str]:
    """
    Fallback document_type classifier for native tabular files (csv/xlsx)
    that have no narrative text for the LLM-based DocumentClassifierAgent
    to read. Looks up each column header against the full registry
    (search_registry, not pre-scoped to a document_type - there isn't
    one yet) and picks whichever document_type most of the matched
    facts agree on. Returns None if nothing matches confidently enough -
    callers should require an explicit document_type in that case rather
    than guess.
    """
    votes = Counter()
    for col in columns:
        for hit in registry.search_registry(col, limit=5):
            if hit["entity_type"] != "fact":
                continue
            fact_ctx = registry.get_fact_context(hit["entity_id"])
            if not fact_ctx:
                continue
            for doc_type in fact_ctx.get("source_priority", []):
                votes[doc_type] += 1

    return votes.most_common(1)[0][0] if votes else None


_GENERIC_JUNK_COLUMNS = {"id", "index", "row", "no", "num", "sr", "sno", "s no", "#"}


def _fact_from_direct_lookup(col: str, registry) -> Optional[dict]:
    """
    Fallback for tabular columns that didn't match via alias/pattern search.
    Tries the normalized column name as a literal fact_id, then as a kpi_id.
    This handles Excel files where column headers ARE the fact/kpi IDs directly.
    KPI-named columns are stored as fact_observations so the KPI engine's
    calculated_direct path can use them when sub-component facts are missing.
    """
    # Skip generic row-numbering columns that will never be fact IDs.
    if _normalize(col).replace(" ", "_") in _GENERIC_JUNK_COLUMNS or col.strip() in _GENERIC_JUNK_COLUMNS:
        return None
    candidates = [col, _normalize(col).replace(" ", "_")]
    for candidate in candidates:
        ctx = registry.get_fact_context(candidate)
        if ctx:
            return {
                "fact_id": ctx["fact_id"],
                "name": ctx.get("name", candidate),
                "category": ctx.get("category"),
                "data_type": ctx.get("data_type"),
                "unit": ctx.get("unit"),
                "aliases": ctx.get("aliases", []),
                "extraction_patterns": ctx.get("extraction_patterns", []),
            }
        kpi_ctx = registry.get_kpi_context(candidate)
        if kpi_ctx:
            return {
                "fact_id": kpi_ctx["kpi_id"],
                "name": kpi_ctx.get("name", candidate),
                "category": kpi_ctx.get("category"),
                "data_type": "numeric",
                "unit": kpi_ctx.get("unit"),
                "aliases": [],
                "extraction_patterns": [],
            }

    # Collapsed-form lookup: handles column headers where separators were
    # stripped by the parser (e.g. 'activeaiusers' → fact 'active_ai_users').
    real_id = registry.get_id_by_collapsed(col)
    if real_id:
        ctx = registry.get_fact_context(real_id)
        if ctx:
            return {
                "fact_id": ctx["fact_id"],
                "name": ctx.get("name", real_id),
                "category": ctx.get("category"),
                "data_type": ctx.get("data_type"),
                "unit": ctx.get("unit"),
                "aliases": ctx.get("aliases", []),
                "extraction_patterns": ctx.get("extraction_patterns", []),
            }
        kpi_ctx = registry.get_kpi_context(real_id)
        if kpi_ctx:
            return {
                "fact_id": kpi_ctx["kpi_id"],
                "name": kpi_ctx.get("name", real_id),
                "category": kpi_ctx.get("category"),
                "data_type": "numeric",
                "unit": kpi_ctx.get("unit"),
                "aliases": [],
                "extraction_patterns": [],
            }

    return None


def extract_observations_from_time_series_table(
    table: dict, document_type: str, registry, document_id: Optional[str] = None,
) -> tuple[list[dict], list[str]]:
    """
    table: one entry from DoclingDocumentParser's time_series_tables -
        {"date_column", "value_columns", "rows": [{"date": "...", "<header>": "<raw>", ...}]}

    Returns (extractions, unmatched_columns):
        extractions       : [{"fact_id", "value", "confidence", "observation_date",
                               "document_id", "chunk_id"}, ...] - ready for
                             fact_validation_engine.validate_extractions()
        unmatched_columns : column headers that didn't map to any fact,
                             for visibility into what got skipped and why.
    """
    relevant_facts = registry.discover_relevant_facts(document_type)
    column_fact_map = {col: map_column_to_fact(col, relevant_facts) for col in table["value_columns"]}
    # Fallback: for columns not matched by scoped alias search, try direct fact_id lookup.
    # This handles spreadsheets where column headers are literally the fact_ids.
    for col in table["value_columns"]:
        if column_fact_map[col] is None:
            column_fact_map[col] = _fact_from_direct_lookup(col, registry)
    unmatched = [col for col, fact in column_fact_map.items() if fact is None]
    matched = {col: fact for col, fact in column_fact_map.items() if fact is not None}

    extractions = []
    for row in table["rows"]:
        observation_date = row.get("date")
        if not observation_date:
            continue
        for col, fact in matched.items():
            raw_value = row.get(col)
            if raw_value is None or str(raw_value).strip() == "" or str(raw_value).lower() == "nan":
                continue
            extractions.append({
                "fact_id": fact["fact_id"],
                "value": raw_value,
                "confidence": 1.0,
                "observation_date": observation_date,
                "document_id": document_id,
                "chunk_id": None,
            })

    return extractions, unmatched


def extract_facts_from_structured_table(
    table: dict, document_type: str, registry, document_id: Optional[str] = None,
) -> tuple[list[dict], list[str]]:
    """
    table: one entry from structured_tables (no date column, e.g. a flat
    one-row-per-company KPI dump) - {"columns", "rows": [{...}]}

    Same column matching as the time-series path, but for flat rows with
    no date - these are point-in-time facts (one value per fact per
    row), not observations. Returns extraction dicts WITHOUT an
    observation_date; the caller attaches whatever `period` the upload
    specifies, the same way narrative extraction already does.

    Returns (extractions, unmatched_columns).
    """
    relevant_facts = registry.discover_relevant_facts(document_type)
    column_fact_map = {col: map_column_to_fact(col, relevant_facts) for col in table["columns"]}
    for col in table["columns"]:
        if column_fact_map[col] is None:
            column_fact_map[col] = _fact_from_direct_lookup(col, registry)
    unmatched = [col for col, fact in column_fact_map.items() if fact is None]
    matched = {col: fact for col, fact in column_fact_map.items() if fact is not None}

    extractions = []
    for row in table["rows"]:
        for col, fact in matched.items():
            raw_value = row.get(col)
            if raw_value is None or str(raw_value).strip() == "" or str(raw_value).lower() == "nan":
                continue
            extractions.append({
                "fact_id": fact["fact_id"],
                "value": raw_value,
                "confidence": 1.0,
                "document_id": document_id,
                "chunk_id": None,
            })

    return extractions, unmatched
