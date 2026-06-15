"""
Document Chunking
==================
LlamaParse is already implemented and produces Markdown output.
This module adds ONE method: chunk the markdown and wrap each chunk
in a dict with metadata (file_name, document_type placeholder, etc.)
ready for PostgreSQL storage.
"""

import uuid
import re
from typing import List, Dict, Optional


def chunk_markdown(
    markdown_text: str,
    file_name: str,
    company_id: str,
    document_id: Optional[str] = None,
    max_chunk_chars: int = 1500,
) -> List[Dict]:
    """
    Split LlamaParse markdown output into chunks and attach metadata.

    Splits on markdown headers first (## / ###) to preserve semantic
    boundaries, then further splits any oversized section by paragraph.

    Returns a list of dicts matching the PostgreSQL chunk schema:
        {
          "document_id": ...,
          "chunk_id": ...,
          "company_id": ...,
          "file_name": ...,
          "document_type": None,   # filled later by Classification Agent
          "chunk_text": ...,
          "metadata": {"section": ..., "chunk_index": ...}
        }
    """
    document_id = document_id or str(uuid.uuid4())

    # Split on markdown headers, keeping the header with its section
    sections = re.split(r"(?=^#{1,3}\s)", markdown_text, flags=re.MULTILINE)
    sections = [s.strip() for s in sections if s.strip()]

    chunks: List[Dict] = []
    chunk_index = 0

    for section in sections:
        header_match = re.match(r"^(#{1,3})\s+(.*)", section)
        section_title = header_match.group(2).strip() if header_match else None

        if len(section) <= max_chunk_chars:
            pieces = [section]
        else:
            pieces = _split_by_paragraph(section, max_chunk_chars)

        for piece in pieces:
            chunks.append({
                "document_id": document_id,
                "chunk_id": str(uuid.uuid4()),
                "company_id": company_id,
                "file_name": file_name,
                "document_type": None,  # set by Document Classification Agent (Stage 3)
                "chunk_text": piece,
                "metadata": {
                    "section": section_title,
                    "chunk_index": chunk_index,
                },
            })
            chunk_index += 1

    return chunks


def _split_by_paragraph(text: str, max_chars: int) -> List[str]:
    paragraphs = text.split("\n\n")
    pieces, current = [], ""

    for para in paragraphs:
        if len(current) + len(para) + 2 <= max_chars:
            current = f"{current}\n\n{para}" if current else para
        else:
            if current:
                pieces.append(current)
            current = para

    if current:
        pieces.append(current)

    return pieces
