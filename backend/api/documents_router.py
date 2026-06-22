import re
import os
import shutil
import tempfile
from pathlib import Path

from fastapi import APIRouter, UploadFile, File, Form, HTTPException, Request

from backend.config import PARSED_FILE_PATH
from backend.db.document_parser.data_ingestion import (
    ingest_financial_upload,
    is_rag_file,
    is_tabular_file,
)
from backend.document_parser.docling_document_parser import DoclingDocumentParser
from backend.kpi_extractor.app.core.chunking import chunk_markdown
from backend.kpi_extractor.app.engine import fact_aggregation
from backend.kpi_extractor.extractor_pipeline import (
    ingest_document_from_chunks,
    ingest_structured_tables,
    calculate_kpis,
    calculate_kpis_for_periods,
)
import backend.db.db_client as db
from backend.utilites.app_logger import Logger

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
    request: Request,
) -> dict:
    """
    Parses one uploaded document with Docling, which splits its output into:

      - narrative_markdown   : prose + any table with no detectable date
        column -> existing LLM-based path (DocumentClassifierAgent ->
        FactExtractionAgent -> fact_validation_engine), unchanged.
      - time_series_tables   : tables with a detected date column (e.g.
        years of daily telemetry) -> deterministic tabular path
        (tabular_extraction -> fact_validation_engine -> fact_observations),
        no LLM call per cell.
      - structured_tables    : native csv/xlsx/json tables with no date
        column (e.g. a flat one-row-per-company KPI dump) -> same
        deterministic path, written to `facts` as point-in-time values.

    A single document can go through either path, both, or neither
    (if parsing produced nothing usable) - whichever of
    time_series_tables/structured_tables/real narrative content is
    actually present is what runs.

    KPI calculation at the end runs against periods DERIVED FROM THE
    DATA, not the typed `period` Form field, whenever a time_series_table
    with real dates is present. See fact_aggregation.derive_periods_from_tables -
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
    log.log_info(f"Saving parsed data...")
    document_id = db.save_document(company_id, filename)

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

    llm = request.app.state.llm
    registry = request.app.state.registry
    classified_document_type = None

    # --- Narrative path (LLM-based): unchanged behavior, just sourced
    #     from Docling's narrative_markdown instead of LlamaParse's
    #     flattened text. Skipped entirely if there's no real prose. ---
    if has_narrative:
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
            classified_document_type = extract_result.get("document_type")
            result["document_type"] = classified_document_type
            result["facts_extracted"] = extract_result.get("facts_validated", 0)
        except Exception as exc:
            result["warning"] = (
                f"Narrative fact-extraction pass failed: {exc}. Document chunks were "
                "saved; run /kpis/extract manually if needed."
            )
            log.log_warning(
                f"Narrative fact-extraction failed for document_id={document_id}: {exc}"
            )

    # --- Tabular path (deterministic, no LLM): only runs if Docling
    #     actually found a dated table or a native structured table.
    #     Reuses the SAME document_id as the narrative path above (if
    #     any ran) instead of creating a second document record, and
    #     reuses its classified document_type when available so a table
    #     embedded in an already-classified narrative doc doesn't get a
    #     second, possibly-disagreeing classification. ---
    if time_series_tables or structured_tables:
        try:
            structured_result = ingest_structured_tables(
                parsed,
                company_id=company_id,
                period=period,
                registry=registry,
                document_type=classified_document_type,
                file_name=filename,
                document_id=document_id,
            )
            result["observations_saved"] = structured_result.get("observations_saved", 0)
            result["structured_facts_saved"] = structured_result.get("facts_saved", 0)
            result["unmatched_columns"] = structured_result.get("unmatched_columns", [])
            if structured_result.get("status") == "skipped":
                result["structured_warning"] = structured_result.get("reason")
            elif result["document_type"] is None:
                result["document_type"] = structured_result.get("document_type")
        except Exception as exc:
            result["structured_warning"] = f"Tabular fact-extraction pass failed: {exc}"
            log.log_warning(
                f"Tabular fact-extraction failed for document_id={document_id}: {exc}"
            )

    # --- KPI calculation: best-effort, runs once regardless of which
    #     path(s) above produced new facts/observations. A failure here
    #     shouldn't fail the upload itself - the document and any
    #     extracted facts are already safely persisted.
    #
    #     Periods to calculate are DERIVED FROM THE DATA whenever a
    #     time_series_table with real per-row dates was found - this is
    #     what makes recalculation work regardless of what period string
    #     was typed into the upload form, and regardless of whether the
    #     sheet's own "year" label (if any) agrees with its date column.
    #     Falls back to the typed `period` only when there's no
    #     date-bearing table to derive from at all (e.g. a flat
    #     structured-only upload, or narrative-only with no tables). ---
    try:
        derived_periods = fact_aggregation.derive_periods_from_tables(time_series_tables)
        periods_to_calculate = derived_periods or [period]

        kpi_results_by_period = calculate_kpis_for_periods(
            company_id=company_id, periods=periods_to_calculate, registry=registry,
        )
        result["kpis_calculated"] = sum(len(v) for v in kpi_results_by_period.values())
        result["kpi_periods"] = periods_to_calculate
    except Exception as exc:
        result["warning"] = (
            f"{result.get('warning', '')} KPI calculation failed: {exc}. "
            "Facts were saved; run /kpis/calculate manually."
        ).strip()
        log.log_warning(f"KPI calculation failed for document_id={document_id}: {exc}")

    return result


@router.post("/upload")
async def upload_document(
    request: Request,
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
            file, company_id, period, registry=request.app.state.registry
        )

        # ingest_financial_upload already consumed file.file up to EOF via
        # shutil.copyfileobj; rewind before reading it again for the
        # Docling/structured-extraction pass.
        file.file.seek(0)

        try:
            narrative_result = process_uploaded_document(file, company_id, period, request)
        except HTTPException as e:
            narrative_result = {
                "status": "skipped",
                "warning": f"Docling/fact-extraction pass over the spreadsheet failed: {e.detail}",
            }

        document = {
            **financial_result,
            "status": "saved",
            "destination": "nexus_db.financial_data + nexus_db.document_chunks/facts/fact_observations",
            "narrative_extraction": narrative_result,
        }
    elif is_rag_file(file.filename):
        document = process_uploaded_document(file, company_id, period, request)
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
async def get_document_chunks(document_id: str):
    """Inspect persisted chunks for a document (debug / status check)."""
    chunks = db.get_chunks(document_id)
    if not chunks:
        raise HTTPException(status_code=404, detail="Document not found")
    return {"document_id": document_id, "chunks": chunks}