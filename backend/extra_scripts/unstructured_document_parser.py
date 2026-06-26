import os
import json
import re
import time
import warnings
from typing import List, Dict, Any, Optional
from pathlib import Path
from backend.utilites.app_logger import Logger
log = Logger()
from dataclasses import dataclass, asdict

import pandas as pd
from unstructured.partition.auto import partition
from unstructured.documents.elements import Table, Image, Title

# File types that ARE the data - every table returned as structured JSON
# regardless of whether it has a date column (no prose to preserve).
_NATIVE_TABULAR_TYPES = {".csv", ".xlsx", ".xls", ".json"}


def _to_datetime_quiet(values):
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", UserWarning)
        return pd.to_datetime(pd.Series(values), errors="coerce")


def _detect_date_column(headers: List[str], rows: List[List[Any]],
                         min_parse_rate: float = 0.7) -> Optional[int]:
    if not headers or not rows:
        return None
    best_idx, best_rate = None, 0.0
    for col_idx in range(len(headers)):
        col_values = [
            row[col_idx] for row in rows
            if col_idx < len(row) and str(row[col_idx]).strip()
        ]
        if len(col_values) < 2:
            continue
        rate = _to_datetime_quiet(col_values).notna().mean()
        if rate > best_rate:
            best_rate, best_idx = rate, col_idx
    return best_idx if best_idx is not None and best_rate >= min_parse_rate else None


def _normalize_date(raw_value: Any) -> Optional[str]:
    parsed = _to_datetime_quiet([raw_value]).iloc[0]
    if pd.isna(parsed):
        return None
    return parsed.date().isoformat()


def _classify_table(headers: List[str], data: List[List[Any]],
                     force_structured: bool) -> Dict[str, Any]:
    date_col_idx = _detect_date_column(headers, data)

    if date_col_idx is not None:
        date_header = headers[date_col_idx]
        rows_out = []
        for row in data:
            if date_col_idx >= len(row):
                continue
            iso_date = _normalize_date(row[date_col_idx])
            if iso_date is None:
                continue
            row_dict = {"date": iso_date}
            for i, h in enumerate(headers):
                if i != date_col_idx:
                    row_dict[h] = row[i] if i < len(row) else None  #type:ignore
            rows_out.append(row_dict)
        return {
            "kind": "time_series",
            "date_column": date_header,
            "value_columns": [h for i, h in enumerate(headers) if i != date_col_idx],
            "rows": rows_out,
        }

    if force_structured:
        rows_out = [
            {h: (row[i] if i < len(row) else None) for i, h in enumerate(headers)}
            for row in data
        ]
        return {"kind": "structured", "columns": headers, "rows": rows_out}

    return {"kind": "narrative_markdown", "headers": headers, "data": data}
from backend.config import PARSED_FILE_PATH


@dataclass
class ParsedPage:
    page_number: int
    text_content: str
    images: List[Dict[str, Any]]
    tables: List[Dict[str, Any]]
    metadata: Dict[str, Any]


@dataclass
class ParsedDocument:
    filename: str
    file_type: str
    total_pages: int
    pages: List[ParsedPage]
    document_metadata: Dict[str, Any]
    processing_time: float


class UnstructuredDocumentParser:
    """
    Drop-in replacement for LlamaCloudDocumentParser / DoclingDocumentParser,
    backed by the `unstructured` library.

    Design choice for production weight:
      - PDFs are parsed with strategy="fast" by default (pdfminer text-layer
        extraction, no torch / no layout models, no OCR). This is appropriate
        for text-based PDFs (contracts, SOWs, financial reports) and keeps the
        Lambda deployment lightweight.
      - If the fast pass returns suspiciously little text (e.g. a scanned
        PDF with no real text layer), it automatically falls back to
        strategy="hi_res" (OCR + layout model) ONLY for that document, so you
        don't pay the heavy-model cost on every request -- only on the rare
        scanned/garbled file. This requires the optional heavy extras to be
        installed (see notes below); if they aren't installed, the fallback
        is skipped and a warning is logged instead of crashing.
      - docx/pptx/xlsx/csv/md/txt/html use unstructured's native partitioners,
        which never need OCR or layout models.

    Keeps the same dataclasses (ParsedPage / ParsedDocument) and the same
    public method surface as the previous parsers, so it can be swapped in
    with minimal changes elsewhere in the pipeline.
    """

    SUPPORTED_EXTENSIONS = os.getenv(
        "SUPPORTED_DOC_TYPE_EXTENSIONS",
        ".pdf,.docx,.pptx,.xlsx,.md,.txt,.csv,.html"
    ).split(",")

    # If fast-strategy extraction yields fewer than this many characters,
    # treat the PDF as likely scanned/image-based and consider a hi_res retry.
    MIN_TEXT_LENGTH_THRESHOLD = 50

    def __init__(self, max_timeout: int = 300, enable_hires_fallback: bool = True):
        self.max_timeout = max_timeout
        self.enable_hires_fallback = enable_hires_fallback
        self.results: List[ParsedDocument] = []

    def is_supported_file(self, file_path: str) -> bool:
        return Path(file_path).suffix.lower() in self.SUPPORTED_EXTENSIONS  # type: ignore

    def parse_single_document(self, file_path: str) -> Optional[ParsedDocument]:
        start_time = time.time()

        if not os.path.exists(file_path):
            log.log_error(f"File not found: {file_path}")
            return None

        if not self.is_supported_file(file_path):
            log.log_error(f"Unsupported file type: {file_path}")
            return None

        file_type = Path(file_path).suffix.lower()

        try:
            log.log_info(f"Starting to parse: {file_path}")

            elements = self._partition_with_fallback(file_path, file_type)

            if not elements:
                log.log_error(f"No content extracted from: {file_path}")
                return None

            parsed_pages = self._build_pages(elements, file_type)

            processing_time = time.time() - start_time

            parsed_doc = ParsedDocument(
                filename=os.path.basename(file_path),
                file_type=file_type,
                total_pages=len(parsed_pages),
                pages=parsed_pages,
                document_metadata=self._extract_document_metadata(elements),
                processing_time=processing_time
            )

            self.results.append(parsed_doc)

            log.log_info(
                f"Successfully parsed {file_path} - {len(parsed_pages)} pages in {processing_time:.2f}s"
            )
            return parsed_doc

        except Exception as e:
            log.log_error(f"Error parsing {file_path}: {str(e)}")
            return None

    def _partition_with_fallback(self, file_path: str, file_type: str) -> List[Any]:
        """
        Run partition() with the lightest viable strategy. For PDFs, try
        'fast' first; only escalate to 'hi_res' (OCR + layout model) if the
        fast pass looks like it failed to find real text (scanned PDF).
        """
        if file_type != '.pdf':
            # docx/pptx/xlsx/csv/md/txt/html: native partitioners, no
            # OCR/layout models involved regardless of strategy.
            return partition(filename=file_path)

        try:
            elements = partition(filename=file_path, strategy="fast")
        except Exception as e:
            log.log_warning(f"Fast-strategy PDF parse failed for {file_path}: {e}")
            elements = []

        total_text_len = sum(len(str(el)) for el in elements) if elements else 0

        if total_text_len >= self.MIN_TEXT_LENGTH_THRESHOLD or not self.enable_hires_fallback:
            return elements

        log.log_warning(
            f"Fast-strategy extracted only {total_text_len} chars from {file_path} "
            f"-- likely a scanned PDF. Retrying with hi_res (OCR) strategy."
        )
        try:
            hires_elements = partition(filename=file_path, strategy="hi_res")
            if hires_elements:
                return hires_elements
        except Exception as e:
            log.log_warning(
                f"hi_res fallback failed for {file_path} ({e}). "
                f"Falling back to fast-strategy result, which may be incomplete. "
                f"Ensure 'unstructured[pdf]' with detectron2/onnx extras and OCR "
                f"deps are installed if scanned PDFs are expected."
            )

        return elements

    def _build_pages(self, elements: List[Any], file_type: str) -> List[ParsedPage]:
        """
        Group unstructured Elements by page number (element.metadata.page_number).
        Formats without page boundaries (docx/csv/md/etc.) fall back to a
        single page.
        """
        pages_map: Dict[int, List[Any]] = {}

        for el in elements:
            page_no = getattr(el.metadata, "page_number", None) or 1
            pages_map.setdefault(page_no, []).append(el)

        parsed_pages = []
        for page_no in sorted(pages_map.keys()):
            page_elements = pages_map[page_no]

            text_parts = []
            tables = []
            images = []

            for el in page_elements:
                if isinstance(el, Table):
                    tables.append(self._table_element_to_dict(el))
                    # Tables still contribute their text representation to
                    # the page text for downstream KPI-extraction context.
                    text_parts.append(str(el))
                elif isinstance(el, Image):
                    images.append({
                        "type": "image",
                        "description": getattr(el.metadata, "text_as_html", None) or str(el) or "Image (no description)",
                    })
                else:
                    text_parts.append(str(el))

            page_text = "\n".join(text_parts).strip()

            metadata = {
                "page_number": page_no,
                "content_length": len(page_text),
                "file_type": file_type,
                "has_visual_content": bool(images or tables),
                "content_type": self._determine_content_type(page_text, file_type),
            }

            parsed_pages.append(
                ParsedPage(
                    page_number=page_no,
                    text_content=page_text,
                    images=images,
                    tables=tables,
                    metadata=metadata,
                )
            )

        return parsed_pages

    def _table_element_to_dict(self, table_el: Table) -> Dict[str, Any]:
        """
        Convert an unstructured Table element into the same headers/data
        shape used by the previous parsers. Unstructured exposes
        metadata.text_as_html for tables when available, which we parse
        for clean headers/rows; otherwise we fall back to splitting the
        plain-text representation by lines.
        """
        html = getattr(table_el.metadata, "text_as_html", None)

        if html:
            try:
                dfs = pd.read_html(html)
                if dfs:
                    df = dfs[0]
                    headers = [str(c) for c in df.columns]
                    rows = df.astype(str).values.tolist()
                    return {
                        "headers": headers,
                        "row_count": len(rows),
                        "column_count": len(headers),
                        "data": rows,
                    }
            except Exception as e:
                log.log_warning(f"Could not parse table HTML, falling back to plain text: {e}")

        # Fallback: no structured HTML available, return raw text as a
        # single-column table so no information is silently dropped.
        raw_text = str(table_el)
        return {
            "headers": [],
            "row_count": 1,
            "column_count": 1,
            "data": [[raw_text]],
        }

    def _determine_content_type(self, content: str, file_type: str) -> str:
        content_lower = content.lower()

        if file_type == '.pptx':
            if 'chart' in content_lower or 'graph' in content_lower:
                return 'presentation_with_charts'
            elif 'image' in content_lower or 'photo' in content_lower:
                return 'presentation_with_images'
            return 'text_presentation'

        elif file_type in ['.xlsx', '.xls']:
            return 'spreadsheet_with_charts' if 'chart' in content_lower else 'data_spreadsheet'

        elif file_type == '.pdf':
            if any(k in content_lower for k in ['chart', 'graph', 'figure']):
                return 'visual_document'
            return 'text_document'

        return 'mixed_content'

    def _extract_document_metadata(self, elements: List[Any]) -> Dict[str, Any]:
        num_tables = sum(1 for el in elements if isinstance(el, Table))
        num_images = sum(1 for el in elements if isinstance(el, Image))
        page_numbers = {getattr(el.metadata, "page_number", None) or 1 for el in elements}

        return {
            'total_documents': 1,
            'extraction_method': 'unstructured',
            'has_images': num_images > 0,
            'has_tables': num_tables > 0,
            'estimated_pages': len(page_numbers) or 1,
        }

    def parse_multiple_documents(self, file_paths: List[str]) -> List[ParsedDocument]:
        results = []
        for file_path in file_paths:
            log.log_info(f"Processing file {len(results) + 1}/{len(file_paths)}: {file_path}")
            parsed_doc = self.parse_single_document(file_path)
            if parsed_doc:
                results.append(parsed_doc)
        return results

    def parse_directory(self, directory_path: str, recursive: bool = False) -> List[ParsedDocument]:
        file_paths = []

        if recursive:
            for root, dirs, files in os.walk(directory_path):
                for file in files:
                    file_path = os.path.join(root, file)
                    if self.is_supported_file(file_path):
                        file_paths.append(file_path)
        else:
            for file in os.listdir(directory_path):
                file_path = os.path.join(directory_path, file)
                if os.path.isfile(file_path) and self.is_supported_file(file_path):
                    file_paths.append(file_path)

        log.log_info(f"Found {len(file_paths)} supported documents to parse")
        return self.parse_multiple_documents(file_paths)

    

    def save_results(self, output_file: str = "parsed_results.json"):
        try:
            PARSED_FILE_PATH.mkdir(parents=True, exist_ok=True)
            output_path = PARSED_FILE_PATH / Path(output_file).name
            results_dict = [asdict(result) for result in self.results]
            with open(output_path, 'w', encoding='utf-8') as f:
                json.dump(results_dict, f, indent=2, ensure_ascii=False)
            log.log_info(f"Results saved to {output_path}")
        except Exception as e:
            log.log_error(f"Error saving results: {str(e)}")

    def get_summary_statistics(self) -> Dict[str, Any]:
        if not self.results:
            return {}

        stats = {
            'total_documents': len(self.results),
            'total_pages': sum(doc.total_pages for doc in self.results),
            'average_pages_per_doc': sum(doc.total_pages for doc in self.results) / len(self.results),
            'file_types': {},
            'total_processing_time': sum(doc.processing_time for doc in self.results),
            'documents_with_images': 0,
            'documents_with_tables': 0
        }

        for doc in self.results:
            stats['file_types'][doc.file_type] = stats['file_types'].get(doc.file_type, 0) + 1
            if any(len(page.images) > 0 for page in doc.pages):
                stats['documents_with_images'] += 1
            if any(len(page.tables) > 0 for page in doc.pages):
                stats['documents_with_tables'] += 1

        return stats

    def run_parser(self, modified_name: str, file_path: str) -> List[Dict[str, Any]]:
        log.log_info(f"Attempting to parse document: {file_path} with UnstructuredDocumentParser.")

        single_doc_object = self.parse_single_document(file_path)

        if single_doc_object:
            log.log_info(
                f"Successfully parsed: {single_doc_object.filename}, Pages: {single_doc_object.total_pages}"
            )
            self.save_results(f"{modified_name}_parsed_results.json")
            return [asdict(single_doc_object)]
        else:
            log.log_error(f"Failed to parse document: {file_path}")
            return []

    def extract_all_text(self, modified_name: str, file_path: str) -> Dict[str, Any]:
        """Returns a dict with narrative_markdown, time_series_tables, structured_tables."""
        parsed_data = self.run_parser(modified_name, file_path)

        if not parsed_data:
            log.log_error("Parsing failed - no data returned")
            return {"narrative_markdown": "", "time_series_tables": [], "structured_tables": []}

        doc = parsed_data[0]
        file_type = doc.get("file_type", "")
        force_structured = file_type in _NATIVE_TABULAR_TYPES
        filename = doc.get("filename", "Unknown Document")

        narrative_parts = [f"# Document: {filename}\n"]
        time_series_tables: List[Dict[str, Any]] = []
        structured_tables: List[Dict[str, Any]] = []

        for page in doc.get("pages", []):
            page_num = page.get("page_number", "Unknown")
            text_content = (page.get("text_content") or "").strip()

            if text_content and not force_structured:
                narrative_parts.append(f"\n## Page {page_num}\n")
                narrative_parts.append(text_content)

            for table_idx, table in enumerate(page.get("tables", []), 1):
                headers = table.get("headers", [])
                data = table.get("data", [])
                classified = _classify_table(headers, data, force_structured)

                if classified["kind"] == "time_series":
                    time_series_tables.append({
                        "page_number": page_num,
                        "table_index": table_idx,
                        "date_column": classified["date_column"],
                        "value_columns": classified["value_columns"],
                        "rows": classified["rows"],
                    })
                elif classified["kind"] == "structured":
                    structured_tables.append({
                        "page_number": page_num,
                        "table_index": table_idx,
                        "columns": classified["columns"],
                        "rows": classified["rows"],
                    })
                else:  # narrative_markdown - flatten into prose
                    narrative_parts.append(f"\n### Table {table_idx}\n")
                    if headers:
                        narrative_parts.append("| " + " | ".join(str(h) for h in headers) + " |\n")
                        narrative_parts.append("|" + "|".join(["---"] * len(headers)) + "|\n")
                    for row in data:
                        narrative_parts.append("| " + " | ".join(str(cell) for cell in row) + " |\n")

        return {
            "narrative_markdown": "\n".join(narrative_parts),
            "time_series_tables": time_series_tables,
            "structured_tables": structured_tables,
        }


# if __name__ == "__main__":
#     parser = UnstructuredDocumentParser()
#     test_path = Path("sample_data/synth/novamind/novamind_board_deck_q2_2026.docx")
#     if test_path.exists():
#         parsed = parser.run_parser(modified_name=test_path.stem, file_path=str(test_path))
#         print(json.dumps(parsed[0], indent=2)[:1000] if parsed else "No data returned")
#     else:
#         log.log_error(f"Test file not found at: {test_path}")
