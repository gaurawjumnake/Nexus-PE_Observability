"""
Document Parser Router
=======================
Routes uploaded files by type:
- CSV/XLS/XLSX financial/statistical files are saved to nexus.db financial_data.
- PDF/DOC/DOCX/TXT/MD narrative files are parsed into chunks for RAG.
"""
import re
import shutil
import tempfile
from pathlib import Path

from fastapi import APIRouter, UploadFile, File, Form, HTTPException

from backend.db.document_parser.data_ingestion import (
    ingest_financial_upload,
    is_rag_file,
    is_tabular_file,
)
from backend.document_parser.llama_parsing import LlamaCloudDocumentParser
from backend.kpi_extractor.app.core.chunking import chunk_markdown
import backend.db.db_client as db
from backend.utilites.app_logger import Logger

log = Logger()
router = APIRouter(prefix="/documents", tags=["documents"])


def clean_text(text: str) -> str:
    """Collapse repeated whitespace/newlines from raw parser output."""
    if not text:
        return ""
    text = re.sub(r'[ \t]+', ' ', text)
    text = re.sub(r'\n+', '\n', text)
    return text.strip()


def process_uploaded_document(
    file: UploadFile,
    company_id: str,
    period: str,
) -> dict:
    """Parse, persist, and chunk one narrative uploaded document."""
    filename = file.filename or "uploaded_document"
    suffix = Path(filename).suffix
    with tempfile.NamedTemporaryFile(delete=False, suffix=suffix) as tmp:
        shutil.copyfileobj(file.file, tmp)
        tmp_path = Path(tmp.name)

    try:
        base_name = tmp_path.stem
        log.log_info(f"Running parser for: {tmp_path} with base_name: {base_name}")
        parser = LlamaCloudDocumentParser()
        raw_text = parser.extract_all_text(modified_name=base_name, file_path=str(tmp_path))
    except Exception as e:
        raise HTTPException(
            status_code=500,
            detail=f"Parsing failed for {filename}: {e}",
        )
    finally:
        tmp_path.unlink(missing_ok=True)

    markdown_text = clean_text(raw_text)

    if not markdown_text:
        raise HTTPException(
            status_code=422,
            detail=f"Parsing failed for {filename} - empty content",
        )

    document_id = db.save_document(company_id, filename)
    db.set_document_type(document_id, "narrative")
    chunks = chunk_markdown(markdown_text, document_id)
    db.save_chunks(chunks)

    return {
        "document_id": document_id,
        "company_id": company_id,
        "period": period,
        "file_name": filename,
        "status": "parsed",
        "destination": "nexus_db.document_chunks",
        "document_type": "narrative",
        "chunks_saved": len(chunks),
    }


@router.post("/upload")
async def upload_document(
    file: UploadFile = File(...),
    company_id: str = Form(...),
    period: str = Form(...),
):
    """
    Upload one document. Tabular files are saved to financial_data; narrative
    files are parsed and chunked for RAG.
    """
    if is_tabular_file(file.filename):
        document = ingest_financial_upload(file, company_id, period)
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
async def get_document_chunks(document_id: str):
    """Inspect persisted chunks for a document (debug / status check)."""
    chunks = db.get_chunks(document_id)
    if not chunks:
        raise HTTPException(status_code=404, detail="Document not found")
    return {"document_id": document_id, "chunks": chunks}