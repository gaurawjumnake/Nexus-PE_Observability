import os
import json
import time
import warnings
from typing import List, Dict, Any, Optional
from pathlib import Path
from backend.utilites.app_logger import Logger
log = Logger()
from dataclasses import dataclass, asdict

import pandas as pd

from backend.config import PARSED_FILE_PATH, SUPPORTED_DOC_EXTENSIONS

from docling.document_converter import DocumentConverter
from docling.datamodel.base_models import InputFormat
from docling.datamodel.pipeline_options import PdfPipelineOptions
from docling.document_converter import PdfFormatOption


# File types that ARE the data (no narrative prose to preserve) - every
# table in these gets returned as structured JSON regardless of whether
# it has a date column, instead of being flattened into markdown text.
_NATIVE_TABULAR_TYPES = {".csv", ".xlsx", ".xls", ".json"}


def _detect_date_column(headers: List[str], rows: List[List[Any]],
                         min_parse_rate: float = 0.7) -> Optional[int]:
    """
    Returns the index of the column most likely to be a date column, or
    None if no column qualifies. A column qualifies if at least
    `min_parse_rate` of its non-empty values parse as dates (via
    pandas.to_datetime) - this is a value-based check, not a header-name
    guess, so it works whether the column is called "Date", "Day",
    "Period", "Reporting Date", or has no obviously date-like name at all.
    """
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
        parsed = _to_datetime_quiet(col_values)
        rate = parsed.notna().mean()
        if rate > best_rate:
            best_rate, best_idx = rate, col_idx

    return best_idx if best_idx is not None and best_rate >= min_parse_rate else None


def _to_datetime_quiet(values):
    """pd.to_datetime with per-element format inference, without the
    UserWarning pandas would otherwise log once per table for mixed/
    unspecified date formats - expected and harmless here since source
    date formats vary across documents."""
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", UserWarning)
        return pd.to_datetime(pd.Series(values), errors="coerce")


def _normalize_date(raw_value: Any) -> Optional[str]:
    """Parse a single cell value to an ISO 'YYYY-MM-DD' string, or None if
    it doesn't parse as a date - callers should skip the row rather than
    guess, never fabricate a date."""
    parsed = _to_datetime_quiet([raw_value]).iloc[0]
    if pd.isna(parsed):
        return None
    return parsed.date().isoformat()


def _classify_table(headers: List[str], data: List[List[Any]],
                     force_structured: bool) -> Dict[str, Any]:
    """
    Classifies one table as either time-series (has a date column) or
    plain structured/narrative (no date column). Returns a dict tagged
    with "kind": "time_series" | "structured" | "narrative_markdown".

    force_structured=True (native tabular file types: csv/xlsx/json) means
    even a date-less table is returned as structured JSON rather than
    flattened into markdown, since there's no prose to preserve anyway.
    """
    date_col_idx = _detect_date_column(headers, data)

    if date_col_idx is not None:
        date_header = headers[date_col_idx]
        rows_out = []
        for row in data:
            if date_col_idx >= len(row):
                continue
            iso_date = _normalize_date(row[date_col_idx])
            if iso_date is None:
                continue  # row's date didn't parse - skip it, don't guess
            row_dict = {"date": iso_date}
            for i, h in enumerate(headers):
                if i != date_col_idx:
                    row_dict[h] = row[i] if i < len(row) else None #type:ignore
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


def _build_structured_output(parsed_document: Dict[str, Any]) -> Dict[str, Any]:
    """
    Pure function: takes one parsed-document dict (the shape produced by
    asdict(ParsedDocument) / run_parser()[0]) and splits it into narrative
    markdown vs structured/time-series tables. Kept separate from
    DoclingDocumentParser so it can be unit-tested without docling itself
    installed - it only depends on the plain dict shape, not on Docling's
    internal types.
    """
    filename = parsed_document.get("filename", "Unknown Document")
    file_type = parsed_document.get("file_type", "")
    force_structured = file_type in _NATIVE_TABULAR_TYPES

    narrative_parts = [f"# Document: {filename}\n"]
    time_series_tables: List[Dict[str, Any]] = []
    structured_tables: List[Dict[str, Any]] = []

    for page in parsed_document.get("pages", []):
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
            else:  # narrative_markdown - flatten exactly as the old extract_all_text() did
                narrative_parts.append(f"\n### Table {table_idx}\n")
                if headers:
                    narrative_parts.append("| " + " | ".join(str(h) for h in headers) + " |\n")
                    narrative_parts.append("|" + "|".join(["---"] * len(headers)) + "|\n")
                for row in data:
                    narrative_parts.append("| " + " | ".join(str(cell) for cell in row) + " |\n")

    return {
        "filename": filename,
        "file_type": file_type,
        "narrative_markdown": "\n".join(narrative_parts),
        "time_series_tables": time_series_tables,
        "structured_tables": structured_tables,
    }


class DoclingDocumentParser:
    SUPPORTED_EXTENSIONS = SUPPORTED_DOC_EXTENSIONS

    def __init__(self, max_timeout: int = 300, do_ocr: bool = False, do_table_structure: bool = True):
        self.max_timeout = max_timeout

        # Configure the PDF pipeline (OCR + table structure recognition).
        # Other formats (docx/pptx/xlsx) use Docling's default backends,
        # which already extract native tables/text without needing OCR.
        pdf_opts = PdfPipelineOptions()
        pdf_opts.do_ocr = do_ocr
        pdf_opts.do_table_structure = do_table_structure

        self.converter = DocumentConverter(
            format_options={
                InputFormat.PDF: PdfFormatOption(pipeline_options=pdf_opts)
            }
        )

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

        try:
            log.log_info(f"Starting to parse: {file_path}")

            result = self.converter.convert(file_path)
            doc = result.document

            if doc is None:
                log.log_error(f"No content extracted from: {file_path}")
                return None

            parsed_pages = self._build_pages(doc, file_path)

            processing_time = time.time() - start_time

            parsed_doc = ParsedDocument(
                filename=os.path.basename(file_path),
                file_type=Path(file_path).suffix.lower(),
                total_pages=len(parsed_pages),
                pages=parsed_pages,
                document_metadata=self._extract_document_metadata(doc),
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

    def _build_pages(self, doc, file_path: str) -> List[ParsedPage]:
        """
        Build one ParsedPage per page using Docling's structured document.
        Falls back to a single "page" for formats without page boundaries
        (e.g. some docx/markdown exports).
        """
        file_type = Path(file_path).suffix.lower()

        # Docling exposes doc.pages (dict keyed by page number) for paginated
        # formats. For non-paginated formats we treat the whole doc as 1 page.
        page_numbers = sorted(getattr(doc, "pages", {}).keys()) or [1]

        # Pre-extract all tables once; each table item carries a .prov with
        # page number info we can use to bucket them per page.
        all_tables_by_page = self._collect_tables_by_page(doc)
        all_pictures_by_page = self._collect_pictures_by_page(doc)

        parsed_pages = []
        for page_no in page_numbers:
            try:
                page_text = doc.export_to_markdown(page_no=page_no) if hasattr(doc, "export_to_markdown") else ""
            except TypeError:
                # Older docling versions may not support page_no filtering
                page_text = doc.export_to_markdown() if page_no == page_numbers[0] else ""
            except Exception as e:
                log.log_warning(f"Could not export markdown for page {page_no}: {str(e)}")
                page_text = ""

            tables = all_tables_by_page.get(page_no, [])
            images = all_pictures_by_page.get(page_no, [])

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

    def _collect_tables_by_page(self, doc) -> Dict[int, List[Dict[str, Any]]]:
        """Use Docling's structured TableItem objects -- no regex needed."""
        tables_by_page: Dict[int, List[Dict[str, Any]]] = {}

        for table in getattr(doc, "tables", []):
            page_no = self._get_item_page(table)
            try:
                df = table.export_to_dataframe()
                headers = list(df.columns)
                # Convert to list of dicts, preserving native types and replacing NaN with None
                rows = [
                    [None if pd.isna(cell) else cell for cell in row]
                    for row in df.values.tolist()
                ]
            except Exception:
                headers, rows = [], []

            table_dict = {
                "headers": headers,
                "row_count": len(rows),
                "column_count": len(headers),
                "data": rows,
            }
            tables_by_page.setdefault(page_no, []).append(table_dict)

        return tables_by_page

    def _collect_pictures_by_page(self, doc) -> Dict[int, List[Dict[str, Any]]]:
        """Use Docling's structured PictureItem objects (with captions if present)."""
        pictures_by_page: Dict[int, List[Dict[str, Any]]] = {}

        for picture in getattr(doc, "pictures", []):
            page_no = self._get_item_page(picture)
            caption = ""
            try:
                caption = picture.caption_text(doc) or ""
            except Exception:
                pass

            pictures_by_page.setdefault(page_no, []).append({
                "type": "image",
                "description": caption or "Image (no caption detected)",
            })

        return pictures_by_page

    def _get_item_page(self, item) -> int:
        try:
            prov = item.prov
            if prov:
                return prov[0].page_no
        except Exception:
            pass
        return 1

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

    def _extract_document_metadata(self, doc) -> Dict[str, Any]:
        num_tables = len(getattr(doc, "tables", []))
        num_pictures = len(getattr(doc, "pictures", []))

        return {
            'total_documents': 1,
            'extraction_method': 'Docling',
            'has_images': num_pictures > 0,
            'has_tables': num_tables > 0,
            'estimated_pages': len(getattr(doc, "pages", {})) or 1,
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
            base_dir = PARSED_FILE_PATH
            dest = Path(output_file)
            # If caller passed a bare filename (no directory component), put it
            # under PARSED_FILE_PATH so all parser output stays in one place.
            if dest.parent == Path("."):
                dest = base_dir / dest
            dest.parent.mkdir(parents=True, exist_ok=True)
            results_dict = [asdict(result) for result in self.results]
            with open(dest, 'w', encoding='utf-8') as f:
                json.dump(results_dict, f, indent=2, ensure_ascii=False)
            log.log_info(f"Results saved to {dest}")
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
        log.log_info(f"Attempting to parse document: {file_path} with DoclingDocumentParser.")

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

    def extract_all_text(self, modified_name: str, file_path: str) -> str:
        parsed_data = self.run_parser(modified_name, file_path)

        all_text = []

        if not parsed_data:
            log.log_error("Parsing failed - no data returned")
            return ""

        filename = parsed_data[0].get('filename', 'Unknown Document')
        all_text.append(f"# Document: {filename}\n")

        pages = parsed_data[0].get('pages', [])

        for page in pages:
            page_num = page.get('page_number', 'Unknown')
            all_text.append(f"\n## Page {page_num}\n")

            text_content = page.get('text_content', '').strip()
            if text_content:
                all_text.append(text_content)

            tables = page.get('tables', [])
            for table_idx, table in enumerate(tables, 1):
                all_text.append(f"\n### Table {table_idx}\n")

                headers = table.get('headers', [])
                if headers:
                    all_text.append("| " + " | ".join(str(h) for h in headers) + " |\n")
                    all_text.append("|" + "|".join(["---"] * len(headers)) + "|\n")

                data = table.get('data', [])
                for row in data:
                    all_text.append("| " + " | ".join(str(cell) for cell in row) + " |\n")

        return "\n".join(all_text)

    def extract_structured_output(self, modified_name: str, file_path: str) -> Dict[str, Any]:
        """
        Like extract_all_text(), but instead of flattening every table
        into markdown text, splits output into separate channels:

          - narrative_markdown : page prose, plus (for narrative file
            types like pdf/docx) any table with no detectable date
            column - e.g. a small one-row summary table. Flows through
            the existing LLM-based fact extraction path, unchanged.

          - time_series_tables : tables WITH a detected date column,
            kept as structured {date_column, value_columns, rows} data,
            date column normalized to ISO 'YYYY-MM-DD'. Meant for
            deterministic ingestion into fact_observations - no LLM call
            per cell. Column header -> fact_id mapping happens
            downstream in the extraction pipeline, where the fact
            registry is available (this parser has no registry
            dependency by design).

          - structured_tables : for native tabular file types only
            (csv/xlsx/json) - tables with NO date column still come back
            as structured JSON rather than flattened markdown, since
            there's no prose to preserve in those formats anyway (e.g. a
            flat one-row-per-company KPI dump).

        Returns:
            {
              "filename": str,
              "file_type": str,
              "narrative_markdown": str,
              "time_series_tables": [
                  {"page_number", "table_index", "date_column",
                   "value_columns", "rows": [{"date": "YYYY-MM-DD", ...}]}
              ],
              "structured_tables": [
                  {"page_number", "table_index", "columns", "rows": [{...}]}
              ],
            }
        """
        parsed_data = self.run_parser(modified_name, file_path)
        if not parsed_data:
            log.log_error("Parsing failed - no data returned")
            return {
                "filename": modified_name, "file_type": "",
                "narrative_markdown": "", "time_series_tables": [], "structured_tables": [],
            }
        return _build_structured_output(parsed_data[0])


# if __name__ == "__main__":
#     parser = DoclingDocumentParser()
#     test_path = Path("sample_data/synth/Fluke_2024.xlsx")
#     if test_path.exists():
#         parsed = parser.run_parser(modified_name=test_path.stem, file_path=str(test_path))
#         print(json.dumps(parsed[0], indent=2)[:1000] if parsed else "No data returned")
#     else:
#         log.log_error(f"Test file not found at: {test_path}")
