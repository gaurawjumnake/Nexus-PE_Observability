import re
import os
import shutil
import tempfile
from pathlib import Path

from fastapi import APIRouter, UploadFile, File, Form, HTTPException

from backend.config import PARSED_FILE_PATH
from backend.document_parser.data_ingestion import (
    ingest_financial_upload,
    is_rag_file,
    is_tabular_file,
    pandas_parse_tabular,
)
from backend.document_parser.docling_document_parser import DoclingDocumentParser
from backend.kpi_extractor.app.core.chunking import chunk_markdown
from backend.kpi_extractor.app.engine import fact_aggregation
from backend.kpi_extractor.extractor_pipeline import (
    ingest_document_from_chunks,
    ingest_structured_tables,
    calculate_kpis,
    calculate_kpis_for_periods,
    is_kpi_extraction_sufficient,
)
import backend.db.db_client as db
from backend.utilites.app_logger import Logger
from backend.utilites.llm_models import get_llm_client
from backend.api.deps import get_registry

log = Logger()
router = APIRouter(prefix="/documents", tags=["documents"])


_MIN_NARRATIVE_CHARS = 60
_BOILERPLATE_RE = re.compile(r"^#+\s*(Document:.*|Page\s+\S+)\s*$", re.MULTILINE)


def clean_text(text: str) -> str:
    """Collapse repeated whitespace/newlines from raw parser output."""
    if not text:
        return ""
    text = re.sub(r'[ \t]+', ' ', text)
    text = re.sub(r'\n+', '\n', text)
    return text.strip()


def _has_real_narrative_content(narrative_markdown: str) -> bool:
    """True if there's more here than the parser's own boilerplate headers -
    i.e. there's actual prose worth running the LLM classifier/extractor on."""
    stripped = _BOILERPLATE_RE.sub("", narrative_markdown or "").strip()
    return len(stripped) >= _MIN_NARRATIVE_CHARS


def process_uploaded_document(
    file: UploadFile,
    company_id: str,
    period: str,
) -> dict:
    """
    Parses one uploaded document with Docling, which splits its output into:

      - narrative_markdown   : prose content.
      - time_series_tables   : tables with a detected date column (e.g.
        years of daily telemetry).
      - structured_tables    : native csv/xlsx/json tables with no date
        column (e.g. a flat one-row-per-company KPI dump).

    Extraction order is deterministic-first, LLM-as-fallback:

      1. If any tables are present, the deterministic tabular path runs
         first (tabular_extraction -> fact_validation_engine ->
         facts/fact_observations, no LLM call) - this is authoritative
         whenever a document hands us data in a shape we can column-match
         directly, and also self-classifies document_type from the
         column names.
      2. KPIs are calculated from whatever facts exist so far.
      3. The narrative/LLM path (DocumentClassifierAgent ->
         FactExtractionAgent -> fact_validation_engine) only runs when
         it's actually needed: unconditionally for a narrative-only
         upload (no tables at all - there's no other way to get facts),
         or as a fallback for a tabular upload where
         is_kpi_extraction_sufficient() found a gap among the KPIs this
         document_type is supposed to be able to inform. This avoids
         paying for classify+extract LLM calls on a clean spreadsheet
         that already yields full KPI coverage, and avoids the LLM path
         silently overwriting a precise deterministic value for the same
         fact (previously both paths ran unconditionally and whichever
         wrote last won).
      4. If the narrative pass ran, KPIs are recalculated so any
         newly-extracted facts are reflected.

    KPI calculation runs against periods DERIVED FROM THE DATA, not the
    typed `period` Form field, whenever a time_series_table with real
    dates is present. See fact_aggregation.derive_periods_from_tables -
    the typed `period` is only used as a fallback for facts/observations
    that genuinely have no date of their own (flat structured_tables,
    narrative point-in-time facts), and as the calculation period when
    no date-bearing table was found at all.
    """
    company_id = company_id.strip().lower()
    filename = file.filename or "uploaded_document"
    suffix = Path(filename).suffix
    with tempfile.NamedTemporaryFile(delete=False, suffix=suffix) as tmp:
        shutil.copyfileobj(file.file, tmp)
        tmp_path = Path(tmp.name)

    try:
        base_name = tmp_path.stem
        log.log_info(f"Running parser for: {tmp_path} with base_name: {base_name}")
        output_path = PARSED_FILE_PATH / company_id / filename
        output_path.parent.mkdir(parents=True, exist_ok=True)
        parser = DoclingDocumentParser()
        parsed = parser.extract_structured_output(modified_name=base_name, file_path=str(tmp_path))
        output_path.write_text(parsed.get("narrative_markdown", ""))
        log.log_info(f"Parsing done for-{base_name}")
    except Exception as e:
        raise HTTPException(
            status_code=500,
            detail=f"Parsing failed for {filename}: {e}",
        )
    finally:
        tmp_path.unlink(missing_ok=True)

    narrative_markdown = clean_text(parsed.get("narrative_markdown", ""))
    time_series_tables = parsed.get("time_series_tables", [])
    structured_tables = parsed.get("structured_tables", [])
    has_narrative = _has_real_narrative_content(narrative_markdown)

    if not has_narrative and not time_series_tables and not structured_tables:
        raise HTTPException(
            status_code=422,
            detail=f"Parsing failed for {filename} - no narrative content or tables found",
        )
    log.log_info(
        f"Saving parsed data: has_narrative={has_narrative}, "
        f"ts_tables={len(time_series_tables)}, structured_tables={len(structured_tables)}"
    )
    document_id = db.save_document(company_id, filename)
    log.log_info(f"Document saved: document_id={document_id}")

    result = {
        "document_id": document_id,
        "company_id": company_id,
        "period": period,
        "file_name": filename,
        "status": "parsed",
        "destination": "nexus_db.document_chunks / facts / fact_observations",
        "document_type": None,
        "chunks_saved": 0,
        "facts_extracted": 0,
        "observations_saved": 0,
        "structured_facts_saved": 0,
        "unmatched_columns": [],
        "kpis_calculated": 0,
        "kpi_periods": [],
    }

    llm = get_llm_client()
    registry = get_registry()
    classified_document_type = None
    result["extraction_path"] = []

    # --- Tabular path FIRST (deterministic, no LLM): authoritative
    #     whenever Docling found a dated table or a native structured
    #     table. Self-classifies document_type from column names. ---
    if time_series_tables or structured_tables:
        try:
            structured_result = ingest_structured_tables(
                parsed,
                company_id=company_id,
                period=period,
                registry=registry,
                document_type=None,
                file_name=filename,
                document_id=document_id,
            )
            result["observations_saved"] = structured_result.get("observations_saved", 0)
            result["structured_facts_saved"] = structured_result.get("facts_saved", 0)
            result["unmatched_columns"] = structured_result.get("unmatched_columns", [])
            if structured_result.get("status") == "skipped":
                result["structured_warning"] = structured_result.get("reason")
            else:
                classified_document_type = structured_result.get("document_type")
                result["document_type"] = classified_document_type
                result["extraction_path"].append("structured")
        except Exception as exc:
            result["structured_warning"] = f"Tabular fact-extraction pass failed: {exc}"
            log.log_warning(
                f"Tabular fact-extraction failed for document_id={document_id}: {exc}"
            )

    # --- KPI calculation, pass 1: from whatever facts the tabular path
    #     (if any) just produced. Periods are DERIVED FROM THE DATA
    #     whenever a time_series_table with real per-row dates was
    #     found - this is what makes recalculation work regardless of
    #     what period string was typed into the upload form. Falls back
    #     to the typed `period` only when there's no date-bearing table
    #     to derive from at all. ---
    derived_periods = fact_aggregation.derive_periods_from_tables(time_series_tables)
    periods_to_calculate = derived_periods or [period]
    kpi_results_by_period: dict[str, list[dict]] = {}
    try:
        log.log_info(f"Calculating KPIs for periods={periods_to_calculate}, company_id={company_id}")
        kpi_results_by_period = calculate_kpis_for_periods(
            company_id=company_id, periods=periods_to_calculate, registry=registry,
        )
        result["kpis_calculated"] = sum(len(v) for v in kpi_results_by_period.values())
        result["kpi_periods"] = periods_to_calculate
    except Exception as exc:
        result["warning"] = (
            f"KPI calculation failed: {exc}. Facts were saved; run /kpis/calculate manually."
        )
        log.log_warning(f"KPI calculation failed for document_id={document_id}: {exc}")

    # --- Sufficiency check: is the narrative/LLM pass actually needed?
    #     Not sufficient if there's no classified document_type yet (no
    #     tabular pass ran at all - narrative-only upload), or if any KPI
    #     relevant to this document_type still came back insufficient_data. ---
    sufficient = bool(classified_document_type) and is_kpi_extraction_sufficient(
        kpi_results_by_period, classified_document_type, registry
    )

    # --- Narrative path (LLM-based): runs unconditionally when there was
    #     no tabular data at all (only way to get facts), or as a
    #     fallback when the tabular pass left a real KPI gap. Skipped
    #     entirely if there's no real prose to mine, or if the tabular
    #     pass already covered everything this document_type is relevant
    #     to - saving the classify+extract LLM calls in that case. ---
    if has_narrative and not sufficient:
        db.set_document_type(document_id, "narrative")
        chunks = chunk_markdown(narrative_markdown, document_id)
        db.save_chunks(chunks)
        result["chunks_saved"] = len(chunks)

        try:
            extract_result = ingest_document_from_chunks(
                document_id=document_id,
                company_id=company_id,
                period=period,
                llm=llm,
                registry=registry,
            )
            classified_document_type = extract_result.get("document_type") or classified_document_type
            result["document_type"] = classified_document_type
            result["facts_extracted"] = extract_result.get("facts_validated", 0)
            result["extraction_path"].append("narrative")
        except Exception as exc:
            result["warning"] = (
                f"{result.get('warning', '')} Narrative fact-extraction pass failed: {exc}. "
                "Document chunks were saved; run /kpis/extract manually if needed."
            ).strip()
            log.log_warning(
                f"Narrative fact-extraction failed for document_id={document_id}: {exc}"
            )

        # KPI calculation, pass 2: pick up any facts the narrative pass
        # just added on top of whatever the tabular pass already found.
        try:
            kpi_results_by_period = calculate_kpis_for_periods(
                company_id=company_id, periods=periods_to_calculate, registry=registry,
            )
            result["kpis_calculated"] = sum(len(v) for v in kpi_results_by_period.values())
        except Exception as exc:
            result["warning"] = (
                f"{result.get('warning', '')} KPI recalculation failed: {exc}. "
                "Facts were saved; run /kpis/calculate manually."
            ).strip()
            log.log_warning(f"KPI recalculation failed for document_id={document_id}: {exc}")

    return result


@router.post("/upload")
def upload_document(
    file: UploadFile = File(...),
    company_id: str = Form(...),
    period: str = Form(...),
):
    """
    Upload one document.

    Tabular files (csv/xls/xlsx) are saved to financial_data via the raw
    column bridge AND parsed through Docling for fact/observation
    extraction, so registry-defined granular facts are extracted from
    spreadsheet content, not just literal column-name facts.

    Narrative files (pdf/doc/docx/txt/md) are parsed, chunked, and run
    through fact extraction + KPI calculation automatically. Any dated
    table found in either file type is extracted deterministically
    (no LLM) into fact_observations, and KPI calculation runs against
    the period(s) that table's own dates actually cover - the `period`
    Form field here is a fallback for date-less uploads, not an
    override for dated ones.
    """
    if is_tabular_file(file.filename):
        log.log_info(f"Ingesting as financial data- {file.filename}")
        financial_result = ingest_financial_upload(
            file, company_id, period, registry=get_registry()
        )

        # ingest_financial_upload already consumed file.file up to EOF via
        # shutil.copyfileobj; rewind before reading it again for the
        # Docling/structured-extraction pass.
        file.file.seek(0)

        try:
            narrative_result = process_uploaded_document(file, company_id, period)
        except HTTPException as e:
            # Docling can't parse many xlsx files (they're not valid zip archives).
            # Fall back to pandas-based tabular parsing which produces the same
            # {time_series_tables, structured_tables} structure — this is what
            # captures direct KPI/fact column names from the spreadsheet.
            log.log_warning(
                f"Docling failed for {file.filename} ({e.detail}); "
                "falling back to pandas tabular parser."
            )
            narrative_result = {"status": "skipped", "warning": e.detail}
            try:
                import tempfile, shutil
                suffix = Path(file.filename or "").suffix
                file.file.seek(0)
                with tempfile.NamedTemporaryFile(delete=False, suffix=suffix) as tmp2:
                    shutil.copyfileobj(file.file, tmp2)
                    tmp2_path = Path(tmp2.name)
                try:
                    pandas_parsed = pandas_parse_tabular(tmp2_path, suffix, file.filename or "")
                finally:
                    tmp2_path.unlink(missing_ok=True)

                ts = pandas_parsed.get("time_series_tables", [])
                st = pandas_parsed.get("structured_tables", [])
                if ts or st:
                    registry = get_registry()
                    doc_id = financial_result.get("document_id")
                    tabular_result = ingest_structured_tables(
                        pandas_parsed,
                        company_id=company_id,
                        period=period,
                        registry=registry,
                        file_name=file.filename,
                        document_id=doc_id,
                    )
                    derived_periods = (
                        __import__("backend.kpi_extractor.app.engine.fact_aggregation",
                                   fromlist=["derive_periods_from_tables"])
                        .derive_periods_from_tables(ts) or [period]
                    )
                    kpi_results_by_period = calculate_kpis_for_periods(
                        company_id=company_id,
                        periods=derived_periods,
                        registry=registry,
                    )
                    narrative_result = {
                        **tabular_result,
                        "status": "pandas_tabular",
                        "kpis_calculated": sum(len(v) for v in kpi_results_by_period.values()),
                        "kpi_periods": derived_periods,
                    }
            except Exception as pandas_exc:
                log.log_warning(f"Pandas tabular fallback also failed: {pandas_exc}")
                narrative_result["pandas_warning"] = str(pandas_exc)

        document = {
            **financial_result,
            "status": "saved",
            "destination": "nexus_db.financial_data + nexus_db.document_chunks/facts/fact_observations",
            "narrative_extraction": narrative_result,
        }
    elif is_rag_file(file.filename):
        document = process_uploaded_document(file, company_id, period)
    else:
        raise HTTPException(
            status_code=415,
            detail=(
                f"Unsupported file type for {file.filename}. "
                "Supported: csv, xls, xlsx, pdf, doc, docx, txt, md."
            ),
        )

    return document


@router.get("/{document_id}")
def get_document_chunks(document_id: str):
    """Inspect persisted chunks for a document (debug / status check)."""
    chunks = db.get_chunks(document_id)
    if not chunks:
        raise HTTPException(status_code=404, detail="Document not found")
    return {"document_id": document_id, "chunks": chunks}