import re
import shutil
import tempfile
from pathlib import Path
from typing import Any

import pandas as pd
from fastapi import HTTPException, UploadFile
from sqlalchemy import inspect, text

import backend.db.db_client as db
from backend.utilites.app_logger import Logger

log = Logger()

TABULAR_EXTENSIONS = {".csv", ".xls", ".xlsx"}
RAG_EXTENSIONS = {".pdf", ".doc", ".docx", ".txt", ".md", ".markdown"}
FINANCIAL_TABLE = "financial_data"

COLUMN_ALIASES: dict[str, tuple[str, float]] = {
    "period": ("date", 1),
    "quarter": ("date", 1),
    "company": ("company_name", 1),
    "portco_name": ("company_name", 1),
    "financial_year": ("financial_year", 1),
    "fiscal_year": ("financial_year", 1),
    "fy": ("financial_year", 1),
    "ai_revenue_m": ("ai_revenue", 1_000_000),
    "ai_revenue_mm": ("ai_revenue", 1_000_000),
    "ai_spend_actual_m": ("total_ai_spend", 1_000_000),
    "ai_spend_actual_mm": ("total_ai_spend", 1_000_000),
    "total_ai_spend_m": ("total_ai_spend", 1_000_000),
    "total_ai_spend_mm": ("total_ai_spend", 1_000_000),
    "cost_savings_m": ("cost_savings", 1_000_000),
    "cost_savings_mm": ("cost_savings", 1_000_000),
    "ebitda_uplift_pp": ("ebitda_uplift", 1),
}


def get_file_extension(filename: str | None) -> str:
    return Path(filename or "").suffix.lower()


def is_tabular_file(filename: str | None) -> bool:
    return get_file_extension(filename) in TABULAR_EXTENSIONS


def is_rag_file(filename: str | None) -> bool:
    return get_file_extension(filename) in RAG_EXTENSIONS


def normalize_column_name(column: Any) -> str:
    normalized = str(column).strip().lower()
    normalized = re.sub(r"[^a-z0-9]+", "_", normalized)
    return normalized.strip("_")


def extract_year(value: Any) -> int | None:
    if value is None:
        return None
    match = re.search(r"(20\d{2}|19\d{2})", str(value))
    if not match:
        return None
    return int(match.group(1))


def copy_upload_to_temp(file: UploadFile, suffix: str) -> Path:
    with tempfile.NamedTemporaryFile(delete=False, suffix=suffix) as tmp:
        shutil.copyfileobj(file.file, tmp)
        return Path(tmp.name)


def get_financial_columns() -> set[str]:
    """
    Returns financial_data's current column names. Calls
    db.ensure_schema() first - financial_data is defined in
    nexus_schema.sql (CREATE TABLE IF NOT EXISTS), so this is a no-op
    when the table already exists, but self-heals it if it was ever
    manually dropped, without requiring a server restart.
    """
    db.ensure_schema()
    columns = inspect(db.engine).get_columns(FINANCIAL_TABLE)
    return {column["name"] for column in columns}


# Columns in financial_data that are metadata, not fact values.
NON_FACT_COLUMNS = {"company_name", "financial_year", "date"}


def sync_facts_from_financial_data(company_id: str, document_id: str | None = None) -> dict[str, Any]:
    """
    Bridge: financial_data (raw tabular uploads) -> facts (used by the KPI
    engine). Without this, tabular financial uploads never produce KPIs,
    since calculate_kpis() only reads from the facts table.

    Groups all financial_data rows for `company_id` by financial_year and
    writes one fact per numeric column per year (mean across rows in that
    year), so multiple uploaded sheets/dates for the same year consolidate
    into a single fact value.
    """
    frame = db.get_financial_data(company_id)
    if frame.empty or "financial_year" not in frame.columns:
        return {"years_synced": [], "facts_written": 0}

    fact_columns = [c for c in frame.columns if c not in NON_FACT_COLUMNS]
    years_synced: list[int] = []
    facts_written = 0

    for year, group in frame.dropna(subset=["financial_year"]).groupby("financial_year"):
        period = str(int(year)) #type:ignore
        means = group[fact_columns].mean(numeric_only=True, skipna=True)
        for fact_id, value in means.items():
            if pd.isna(value):
                continue
            db.save_fact(
                fact_id=fact_id, #type:ignore
                company_id=company_id,
                value=round(float(value), 4),
                confidence=1.0,
                source_document=document_id,
                source_chunk=None,
                source_type="financial_data",
                period=period,
            )
            facts_written += 1
        years_synced.append(int(year)) #type:ignore

    return {"years_synced": years_synced, "facts_written": facts_written}


def read_tabular_frames(path: Path, suffix: str) -> dict[str, pd.DataFrame]:
    if suffix == ".csv":
        return {"data": pd.read_csv(path)}
    return pd.read_excel(path, sheet_name=None)


def prepare_financial_frame(
    frame: pd.DataFrame,
    financial_columns: set[str],
    company_id: str,
    period: str,
) -> pd.DataFrame:
    output = pd.DataFrame(index=frame.index)

    for source_column in frame.columns:
        normalized = normalize_column_name(source_column)
        target_column, multiplier = COLUMN_ALIASES.get(normalized, (normalized, 1))
        if target_column not in financial_columns:
            continue

        series = frame[source_column]
        if multiplier != 1:
            series = pd.to_numeric(series, errors="coerce") * multiplier
        elif target_column == "company_name":
            series = series.astype(str).str.strip().str.lower()
        output[target_column] = series

    if "company_name" in financial_columns and "company_name" not in output.columns:
        output["company_name"] = company_id

    if "date" in financial_columns and "date" not in output.columns:
        output["date"] = period

    if "financial_year" in financial_columns and "financial_year" not in output.columns:
        year = extract_year(period)
        if year is None and "date" in output.columns and not output.empty:
            year = extract_year(output["date"].iloc[0])
        if year is not None:
            output["financial_year"] = year

    if output.empty:
        return output

    output = output[[column for column in output.columns if column in financial_columns]]
    return output.dropna(how="all")


def ingest_financial_upload(
    file: UploadFile,
    company_id: str,
    period: str,
    registry: Any = None,
) -> dict[str, Any]:
    company_id = company_id.strip().lower()
    filename = file.filename or "uploaded_financial_data"
    suffix = get_file_extension(filename)
    tmp_path = copy_upload_to_temp(file, suffix)
    document_id = db.save_document(company_id, filename)
    db.set_document_type(document_id, "financial")

    try:
        financial_columns = get_financial_columns()
        frames = read_tabular_frames(tmp_path, suffix)
        sheets: list[dict[str, Any]] = []
        total_rows = 0

        for sheet_name, frame in frames.items():
            prepared = prepare_financial_frame(
                frame=frame,
                financial_columns=financial_columns,
                company_id=company_id,
                period=period,
            )
            if prepared.empty:
                sheets.append({"sheet_name": sheet_name, "rows_inserted": 0})
                continue

            db.append_financial_data(prepared)
            inserted = len(prepared)
            total_rows += inserted
            sheets.append({"sheet_name": sheet_name, "rows_inserted": inserted})

        if total_rows == 0:
            raise HTTPException(
                status_code=422,
                detail=f"No matching financial_data columns found in {filename}.",
            )

        log.log_info(
            f"Saved financial upload to {FINANCIAL_TABLE}: document_id={document_id}, "
            f"file_name={filename}, rows_inserted={total_rows}"
        )

        facts_result = {"years_synced": [], "facts_written": 0}
        kpis_calculated = 0
        try:
            facts_result = sync_facts_from_financial_data(company_id, document_id=document_id)
            if registry is not None:
                from backend.kpi_extractor.extractor_pipeline import calculate_kpis
                for year in facts_result["years_synced"]:
                    kpi_results = calculate_kpis(
                        company_id=company_id, period=str(year), registry=registry
                    )
                    kpis_calculated += len(kpi_results)
        except Exception as exc:
            log.log_warning(
                f"Facts/KPI sync failed for company_id={company_id}: {exc}. "
                "Raw data was saved to financial_data; sync manually if needed."
            )

        return {
            "document_id": document_id,
            "company_id": company_id,
            "period": period,
            "file_name": filename,
            "status": "saved",
            "destination": "nexus_db.financial_data",
            "document_type": "financial",
            "rows_inserted": total_rows,
            "sheets": sheets,
            "facts_written": facts_result["facts_written"],
            "years_synced": facts_result["years_synced"],
            "kpis_calculated": kpis_calculated,
        }
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(
            status_code=500,
            detail=f"Financial data ingestion failed for {filename}: {exc}",
        )
    finally:
        tmp_path.unlink(missing_ok=True)