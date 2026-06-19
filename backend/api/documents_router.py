"""
Document Parser Router
=======================
Handles document upload + parsing using LlamaCloudDocumentParser directly
(extract_all_text -> clean -> markdown), then persists narrative documents
as chunks in Chroma and tabular financial documents in nexus.db.
"""
import re
import shutil
import tempfile
from pathlib import Path

from fastapi import APIRouter, UploadFile, File, Form, HTTPException

from backend.document_ingestion.ingestion_service import (
    ingest_financial_upload,
    is_rag_file,
    is_tabular_file,
)
from backend.document_parser.llama_parsing import LlamaCloudDocumentParser
from backend.kpi_extractor.app.core.chunking import chunk_markdown
import backend.kpi_extractor.app.db.db_client as db
from backend.chatbot.chat import index_document_chunks
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
    """Parse, persist, chunk, and index one narrative uploaded document."""
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

    # Persist document + chunks (kpi_extractor DB)
    log.log_info(
        f"Persisting parsed document: company_id={company_id}, period={period}, "
        f"file_name={filename}, markdown_chars={len(markdown_text)}"
    )
    document_id = db.save_document(company_id, filename)
    db.set_document_type(document_id, "narrative")
    chunks = chunk_markdown(markdown_text, document_id)
    db.save_chunks(chunks)
    log.log_info(f"Saved document_id={document_id} with {len(chunks)} chunk(s)")

    try:
        vector_index = index_document_chunks(
            document_id=document_id,
            company_id=company_id,
            period=period,
            file_name=filename,
        )
        vector_status = "indexed"
        log.log_info(
            f"Document auto-indexed in Chroma: document_id={document_id}, "
            f"chunks_indexed={vector_index.get('chunks_indexed')}"
        )
    except Exception as exc:
        log.log_error(
            f"Document parsed but Chroma indexing failed for document_id={document_id}: "
            f"{type(exc).__name__}: {exc}"
        )
        raise HTTPException(
            status_code=500,
            detail={
                "message": "Document was parsed and saved, but Chroma indexing failed.",
                "document_id": document_id,
                "company_id": company_id,
                "period": period,
                "file_name": filename,
                "error": str(exc),
            },
        )

    return {
        "document_id": document_id,
        "company_id": company_id,
        "period": period,
        "file_name": filename,
        "status": "parsed",
        "destination": "chroma",
        "document_type": "narrative",
        "chunks_saved": len(chunks),
        "vector_status": vector_status,
        "vector_index": vector_index,
    }


@router.post(
    "/upload",
    openapi_extra={
        "requestBody": {
            "required": True,
            "content": {
                "multipart/form-data": {
                    "schema": {
                        "type": "object",
                        "required": ["files", "company_id", "period"],
                        "properties": {
                            "files": {
                                "type": "array",
                                "items": {"type": "string", "format": "binary"},
                                "description": "One or more documents (PDF → Chroma RAG; Excel/CSV/XLS → financial_data)",
                            },
                            "company_id": {
                                "type": "string",
                                "description": "Company identifier",
                            },
                            "period": {
                                "type": "string",
                                "description": "Reporting period",
                            },
                        },
                    }
                }
            },
        }
    },
)
async def upload_document(
    files: list[UploadFile] = File(...),
    company_id: str = Form(...),
    period: str = Form(...),
):
    """
    Upload one or more documents. Tabular financial files (xlsx, xls, csv) are
    persisted to nexus.db financial_data; narrative files (pdf, docx, txt, md)
    are parsed, chunked, and indexed in Chroma for RAG.
    """
    if not files:
        raise HTTPException(
            status_code=422,
            detail="At least one document must be uploaded.",
        )

    documents = []
    for upload_file in files:
        if is_tabular_file(upload_file.filename):
            documents.append(ingest_financial_upload(upload_file, company_id, period))
        elif is_rag_file(upload_file.filename):
            documents.append(process_uploaded_document(upload_file, company_id, period))
        else:
            raise HTTPException(
                status_code=415,
                detail=(
                    f"Unsupported file type for {upload_file.filename}. "
                    "Supported: csv, xls, xlsx, pdf, doc, docx, txt, md."
                ),
            )

    return {
        "document_ids": [document["document_id"] for document in documents],
        "company_id": company_id,
        "period": period,
        "status": "processed",
        "documents_uploaded": len(documents),
        "documents": documents,
    }


@router.get("/{document_id}")
async def get_document_chunks(document_id: str):
    """Inspect persisted chunks for a document (debug / status check)."""
    chunks = db.get_chunks(document_id)
    if not chunks:
        raise HTTPException(status_code=404, detail="Document not found")
    return {"document_id": document_id, "chunks": chunks}