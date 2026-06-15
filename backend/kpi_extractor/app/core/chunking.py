"""
Chunking  (Stage 1 - Post LlamaParse)
=======================================
LlamaParse is already implemented and produces Markdown.
This module takes that Markdown and returns a list of chunk dicts
ready for Postgres storage.
"""

import re
import uuid
from typing import Optional


def chunk_markdown(
    markdown_text: str,
    document_id: str,
    max_chunk_chars: int = 1500,
) -> list[dict]:
    """
    Split LlamaParse Markdown into chunks preserving section boundaries.

    Returns list of:
        {
          "chunk_id":    str (uuid),
          "document_id": str,
          "chunk_text":  str,
          "metadata": {
              "chunk_index":  int,
              "section":      str | None,
              "page_number":  int | None,
          }
        }
    """
    sections = re.split(r"(?=^#{1,3}\s)", markdown_text, flags=re.MULTILINE)
    sections = [s.strip() for s in sections if s.strip()]

    chunks, idx = [], 0
    for section in sections:
        m = re.match(r"^(#{1,3})\s+(.*)", section)
        title = m.group(2).strip() if m else None
        page = _extract_page(section)
        pieces = [section] if len(section) <= max_chunk_chars else _split_paragraphs(section, max_chunk_chars)
        for piece in pieces:
            chunks.append({
                "chunk_id":    str(uuid.uuid4()),
                "document_id": document_id,
                "chunk_text":  piece,
                "metadata":    {"chunk_index": idx, "section": title, "page_number": page},
            })
            idx += 1
    return chunks


def _extract_page(text: str) -> Optional[int]:
    m = re.search(r"(?:page|pg)[.\s#]*(\d+)", text, re.IGNORECASE)
    return int(m.group(1)) if m else None


def _split_paragraphs(text: str, max_chars: int) -> list[str]:
    pieces, buf = [], ""
    for para in text.split("\n\n"):
        if len(buf) + len(para) + 2 <= max_chars:
            buf = f"{buf}\n\n{para}" if buf else para
        else:
            if buf:
                pieces.append(buf)
            buf = para
    if buf:
        pieces.append(buf)
    return pieces
