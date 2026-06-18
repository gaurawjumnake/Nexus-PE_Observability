"""
Document Parser Router
=======================
Handles document upload + parsing using LlamaCloudDocumentParser directly
(extract_all_text -> clean -> markdown), then persists the document
record + chunks via the kpi_extractor DB layer.

Scope ends here: parse -> store. KPI extraction/calculation is handled
entirely by the KPI router.
"""
import re
import shutil
import tempfile
from pathlib import Path

from fastapi import APIRouter, UploadFile, File, Form, HTTPException

from backend.document_parser.llama_parsing import LlamaCloudDocumentParser
from backend.kpi_extractor.app.core.chunking import chunk_markdown
import backend.kpi_extractor.app.db.db_client as db
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


@router.post("/upload")
async def upload_document(
    file: UploadFile = File(...),
    company_id: str = Form(...),
    period: str = Form(...),
):
    """
    Upload + parse a document via LlamaCloudDocumentParser, clean the
    output, and persist it as a document record + chunks.

    Returns document_id. Extraction/KPI calculation is a separate step
    triggered via the KPI router.
    """
    suffix = Path(file.filename).suffix #type:ignore
    with tempfile.NamedTemporaryFile(delete=False, suffix=suffix) as tmp:
        shutil.copyfileobj(file.file, tmp)
        tmp_path = Path(tmp.name)

    try:
        base_name = tmp_path.stem
        log.log_info(f"Running parser for: {tmp_path} with base_name: {base_name}")
        parser = LlamaCloudDocumentParser()
        raw_text = parser.extract_all_text(modified_name=base_name, file_path=str(tmp_path))
    except Exception as e:
        tmp_path.unlink(missing_ok=True)
        raise HTTPException(status_code=500, detail=f"Parsing failed: {e}")
    finally:
        tmp_path.unlink(missing_ok=True)

    markdown_text = clean_text(raw_text)

    if not markdown_text:
        raise HTTPException(status_code=422, detail="Parsing failed - empty content")

    # Persist document + chunks (kpi_extractor DB)
    document_id = db.save_document(company_id, file.filename) #type:ignore
    chunks = chunk_markdown(markdown_text, document_id)
    db.save_chunks(chunks)

    return {
        "document_id": document_id,
        "company_id": company_id,
        "period": period,
        "file_name": file.filename,
        "status": "parsed",
        "chunks_saved": len(chunks),
    }


@router.get("/{document_id}")
async def get_document_chunks(document_id: str):
    """Inspect persisted chunks for a document (debug / status check)."""
    chunks = db.get_chunks(document_id)
    if not chunks:
        raise HTTPException(status_code=404, detail="Document not found")
    return {"document_id": document_id, "chunks": chunks}