import os
import json
import time
from typing import List, Dict, Any, Optional
from pathlib import Path
from backend.utilites.app_logger import Logger
log = Logger()
from dataclasses import dataclass, asdict
from rich import print
from dotenv import load_dotenv
from llama_parse import LlamaParse
from llama_parse.base import ResultType
load_dotenv(override=True)

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

class LlamaCloudDocumentParser:
    SUPPORTED_EXTENSIONS = os.getenv("SUPPORTED_DOC_TYPE_EXTENSIONS", ".pdf,.docx,.pptx,.xlsx,.md,.txt,.csv").split(",")

    def __init__(self, max_timeout: int = 300):
        api_key = os.getenv("LLAMA_CLOUD_API_KEY")
        if not api_key:
            log.log_error("LLAMA_CLOUD_API_KEY not found in environment variables.")
            return []
        
        self.max_timeout = max_timeout
        
        self.parser = LlamaParse(
            api_key=api_key, 
            result_type=ResultType.MD,
            verbose=True,
            language="en",
            use_vendor_multimodal_model=True,
            vendor_multimodal_model_name="openai-gpt4o",
            system_prompt="""
                You are an expert document parser using vision capabilities. Analyze each page image and extract:

                1.  **Text Content**: All visible text with proper hierarchy and formatting
                2.  **Visual Elements**: Detailed descriptions of images, charts, diagrams, graphs, and visual layouts
                3.  **Tables**: Complete table data with headers, structure, and formatting
                4.  **Contextual Information**: Relationships between text and visuals

                For presentations (PPTX slides):
                - Identify slide titles, subtitles, and content hierarchy
                - Describe slide layouts and visual themes
                - Extract all bullet points and text blocks
                - Describe charts, graphs, images, and their context
                - Note slide transitions and visual flow

                For documents with charts/graphs:
                - Describe chart types (bar, line, pie, etc.)
                - Extract data values and labels where visible
                - Explain trends and patterns shown

                For spreadsheets:
                - Extract visible cell data and formatting
                - Describe chart elements and data visualizations
                - Note conditional formatting and visual cues

                Format output in clear Markdown with appropriate headers and structure.

                **IMPORTANT FINAL RULE:**
                Your output must ONLY be the clean Markdown representation of the page's content.
                While performing the analysis requested above, **DO NOT** create your own headers like "## Visual Elements", "## Design Elements", or "## Contextual Information" in the final text.
                Instead, integrate your findings naturally. For example, represent visuals using Markdown image syntax: `![A concise description of the chart or image.]`. Your output should be a direct, clean transcription of the document.
                """
        ) # type:ignore
        self.results = []
    
    def is_supported_file(self, file_path: str) -> bool:
        return Path(file_path).suffix.lower() in self.SUPPORTED_EXTENSIONS # type:ignore
    
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
            
            try:
                documents = self.parser.load_data(file_path)
            except Exception as e:
                log.log_warning(f"Sync load_data failed ({e}), attempting async execution fallback...")
                import concurrent.futures
                import asyncio
                def run_in_thread():
                    new_loop = asyncio.new_event_loop()
                    asyncio.set_event_loop(new_loop)
                    try:
                        return new_loop.run_until_complete(self.parser.aload_data(file_path))
                    finally:
                        new_loop.close()
                with concurrent.futures.ThreadPoolExecutor() as executor:
                    documents = list(executor.submit(run_in_thread).result())
            
            if not documents:
                log.log_error(f"No content extracted from: {file_path}")
                return None
            
            parsed_pages = self._process_pages_with_lvm(documents, file_path)
            
            processing_time = time.time() - start_time
            
            parsed_doc = ParsedDocument(
                filename=os.path.basename(file_path),
                file_type=Path(file_path).suffix.lower(),
                total_pages=len(parsed_pages),
                pages=parsed_pages,
                document_metadata=self._extract_document_metadata(documents),
                processing_time=processing_time
            )
            
            self.results.append(parsed_doc)
            
            log.log_info(f"Successfully parsed {file_path} - {len(parsed_pages)} pages in {processing_time:.2f}s")
            return parsed_doc
            
        except Exception as e:
            log.log_error(f"Error parsing {file_path}: {str(e)}")
            return None
    
    def _process_pages_with_lvm(self, documents: List, file_path: str) -> List[ParsedPage]:
        parsed_pages = []
        file_type = Path(file_path).suffix.lower()
        
        for idx, doc in enumerate(documents):
            try:
                page_content = doc.text if hasattr(doc, 'text') else str(doc)
                
                page_analysis = self._analyze_page_with_lvm(page_content, idx + 1, file_type)
                
                parsed_page = ParsedPage(
                    page_number=idx + 1,
                    text_content=page_content,
                    images=page_analysis.get('images', []),
                    tables=page_analysis.get('tables', []),
                    metadata=page_analysis.get('metadata', {})
                )
                
                parsed_pages.append(parsed_page)
                
            except Exception as e:
                log.log_warning(f"Error processing page {idx + 1}: {str(e)}")
                continue
        
        return parsed_pages
    
    def _analyze_page_with_lvm(self, content: str, page_num: int, file_type: str) -> Dict[str, Any]:
        analysis = {
            'images': [],
            'tables': [],
            'charts': [],
            'visual_elements': [],
            'metadata': {
                'page_number': page_num,
                'content_length': len(content),
                'file_type': file_type,
                'has_visual_content': False,
                'content_type': 'unknown'
            }
        }
        
        content_lower = content.lower()
        
        visual_keywords = [
            'image', 'figure', 'chart', 'graph', 'diagram', 'illustration',
            'photo', 'picture', 'visual', 'screenshot', 'logo', 'icon',
            'bar chart', 'pie chart', 'line chart', 'scatter plot',
            'flowchart', 'timeline', 'infographic'
        ]
        
        for keyword in visual_keywords:
            if keyword in content_lower:
                analysis['metadata']['has_visual_content'] = True
                break
        
        lines = content.split('\n')
        current_section = None
        
        # Track processed table indices to avoid duplicates
        processed_table_indices = set()

        for i, line in enumerate(lines):
            line_lower = line.lower().strip()
            
            if any(chart_type in line_lower for chart_type in ['chart', 'graph', 'plot']):
                chart_info = {
                    'type': self._identify_chart_type(line),
                    'description': line.strip(),
                    'position': i,
                    'context': self._get_context_lines(lines, i, 2)
                }
                analysis['charts'].append(chart_info)
            
            elif any(img_type in line_lower for img_type in ['image', 'figure', 'photo', 'illustration']):
                image_info = {
                    'type': self._identify_image_type(line),
                    'description': line.strip(),
                    'position': i,
                    'context': self._get_context_lines(lines, i, 1)
                }
                analysis['images'].append(image_info)
            
            elif '|' in line and len(line.split('|')) > 2 and i not in processed_table_indices:
                # if not any(table['position'] == i for table in analysis['tables']):
                #     # table_data = self._extract_enhanced_table_data(lines, i)
                #     if table_data:
                #         analysis['tables'].append(table_data)
                all_tables = self._extract_all_tables(lines)  # Returns List[Dict]
                if all_tables:
                    # Add all extracted tables
                    for table_data in all_tables:
                        if table_data and table_data not in analysis['tables']:
                            analysis['tables'].append(table_data)
                            
                            # Mark all indices as processed
                            start_idx = table_data.get('position', i)
                            end_idx = table_data.get('end_index', i)
                            for idx in range(start_idx, end_idx + 1):
                                processed_table_indices.add(idx)
                
                # Skip to end of table to avoid reprocessing
                break
        
        if file_type == '.pptx':
            slide_analysis = self._analyze_slide_with_lvm(content)
            analysis['metadata'].update(slide_analysis)
        
        analysis['metadata']['content_type'] = self._determine_content_type(content, file_type)
        return analysis
    
    def _identify_chart_type(self, line: str) -> str:
        line_lower = line.lower()
        
        chart_types = {
            'bar': ['bar chart', 'bar graph', 'column chart'],
            'line': ['line chart', 'line graph', 'trend line'],
            'pie': ['pie chart', 'donut chart', 'circular chart'],
            'scatter': ['scatter plot', 'scatter chart', 'point plot'],
            'area': ['area chart', 'filled area'],
            'histogram': ['histogram', 'frequency chart'],
            'flowchart': ['flowchart', 'flow diagram', 'process diagram'],
            'timeline': ['timeline', 'gantt chart', 'schedule']
        }
        
        for chart_type, keywords in chart_types.items():
            if any(keyword in line_lower for keyword in keywords):
                return chart_type
        return 'chart'
    
    def _identify_image_type(self, line: str) -> str:
        line_lower = line.lower()
        
        if 'logo' in line_lower:
            return 'logo'
        elif 'screenshot' in line_lower:
            return 'screenshot'
        elif 'diagram' in line_lower:
            return 'diagram'
        elif 'photo' in line_lower or 'photograph' in line_lower:
            return 'photograph'
        elif 'illustration' in line_lower:
            return 'illustration'
        elif 'icon' in line_lower:
            return 'icon'
        else:
            return 'image'
    
    def _get_context_lines(self, lines: List[str], index: int, context_size: int) -> List[str]:
        start = max(0, index - context_size)
        end = min(len(lines), index + context_size + 1)
        return [line.strip() for line in lines[start:end] if line.strip()]
    
    def _extract_enhanced_table_data(self, lines: List[str], start_index: int) -> Optional[Dict[str, Any]]:
        table_lines = []
        i = start_index
        
        while i < len(lines) and ('|' in lines[i] or lines[i].strip() == ''):
            if '|' in lines[i]:
                table_lines.append(lines[i].strip())
            i += 1
        
        if len(table_lines) < 2:
            return None
        
        headers = [cell.strip() for cell in table_lines[0].split('|') if cell.strip()]
        rows = []
        
        for line in table_lines[1:]:
            if '---' not in line:
                row_data = [cell.strip() for cell in line.split('|') if cell.strip()]
                if row_data:
                    rows.append(row_data)
        
        return {
            'position': start_index,
            'headers': headers,
            'rows': len(rows),
            'columns': len(headers),
            'data': rows[:5],
            'full_table': table_lines
        }
    
    def _analyze_slide_with_lvm(self, content: str) -> Dict[str, Any]:
        slide_info = {
            'slide_title': 'Untitled Slide',
            'slide_type': 'content',
            'has_title_slide': False,
            'bullet_points_count': 0,
            'has_speaker_notes': False,
            'visual_complexity': 'medium',
            'color_scheme': 'unknown',
            'layout_type': 'unknown'
        }
        
        lines = [line.strip() for line in content.split('\n') if line.strip()]
        
        if lines:
            slide_info['slide_title'] = lines[0]
            
            if any(keyword in lines[0].lower() for keyword in ['welcome', 'introduction', 'agenda', 'overview']):
                slide_info['has_title_slide'] = True
        
        bullet_indicators = ['•', '-', '*', '◦', '▪', '▫']
        slide_info['bullet_points_count'] = sum(
            1 for line in lines 
            if any(line.startswith(bullet) for bullet in bullet_indicators)
        )
        
        content_lower = content.lower()
        if 'agenda' in content_lower or 'outline' in content_lower:
            slide_info['slide_type'] = 'agenda'
        elif 'thank you' in content_lower or 'questions' in content_lower:
            slide_info['slide_type'] = 'closing'
        elif any(keyword in content_lower for keyword in ['chart', 'graph', 'data']):
            slide_info['slide_type'] = 'data_visualization'
        elif slide_info['bullet_points_count'] > 3:
            slide_info['slide_type'] = 'bullet_points'
        elif slide_info['has_title_slide']:
            slide_info['slide_type'] = 'title'
        
        visual_elements = content_lower.count('image') + content_lower.count('chart') + content_lower.count('diagram')
        if visual_elements > 3:
            slide_info['visual_complexity'] = 'high'
        elif visual_elements > 1:
            slide_info['visual_complexity'] = 'medium'
        else:
            slide_info['visual_complexity'] = 'low'
        
        return slide_info
    
    def _determine_content_type(self, content: str, file_type: str) -> str:
        content_lower = content.lower()
        
        if file_type == '.pptx':
            if 'chart' in content_lower or 'graph' in content_lower:
                return 'presentation_with_charts'
            elif 'image' in content_lower or 'photo' in content_lower:
                return 'presentation_with_images'
            else:
                return 'text_presentation'
        
        elif file_type in ['.xlsx', '.xls']:
            if 'chart' in content_lower:
                return 'spreadsheet_with_charts'
            else:
                return 'data_spreadsheet'
        
        elif file_type == '.pdf':
            if any(keyword in content_lower for keyword in ['chart', 'graph', 'figure']):
                return 'visual_document'
            else:
                return 'text_document'
        
        return 'mixed_content'
    
    def _extract_image_descriptions(self, content: str) -> List[Dict[str, Any]]:
        images = []
        lines = content.split('\n')
        
        for i, line in enumerate(lines):
            if any(keyword in line.lower() for keyword in ['image', 'figure', 'chart', 'graph']):
                image_info = {
                    'description': line.strip(),
                    'position': i,
                    'type': 'unknown'
                }
                
                if 'chart' in line.lower():
                    image_info['type'] = 'chart'
                elif 'graph' in line.lower():
                    image_info['type'] = 'graph'
                elif 'figure' in line.lower():
                    image_info['type'] = 'figure'
                
                images.append(image_info)
        
        return images
    
    def _extract_table_data(self, content: str) -> List[Dict[str, Any]]:
        tables = []
        lines = content.split('\n')
        
        current_table = []
        in_table = False
        
        for line in lines:
            if '|' in line and len(line.split('|')) > 2:
                if not in_table:
                    in_table = True
                    current_table = []
                current_table.append(line.strip())
            else:
                if in_table and current_table:
                    table_info = {
                        'rows': len(current_table),
                        'raw_data': current_table,
                        'headers': current_table[0].split('|') if current_table else []
                    }
                    tables.append(table_info)
                    current_table = []
                in_table = False
        
        return tables
    
    def _extract_slide_title(self, content: str) -> str:
        lines = content.split('\n')
        for line in lines:
            if line.strip() and not line.startswith(' '):
                return line.strip()
        return "Untitled Slide"
    
    def _determine_slide_type(self, content: str) -> str:
        content_lower = content.lower()
        
        if 'agenda' in content_lower or 'outline' in content_lower:
            return 'agenda'
        elif 'thank you' in content_lower or 'questions' in content_lower:
            return 'closing'
        elif content.count('•') > 3 or content.count('-') > 3:
            return 'bullet_points'
        elif any(keyword in content_lower for keyword in ['chart', 'graph', 'data']):
            return 'data_visualization'
        else:
            return 'content'
    
    def _extract_document_metadata(self, documents: List) -> Dict[str, Any]:
        metadata = {
            'total_documents': len(documents),
            'extraction_method': 'LlamaCloud',
            'has_images': False,
            'has_tables': False,
            'estimated_pages': len(documents)
        }
        
        all_content = ' '.join([doc.text if hasattr(doc, 'text') else str(doc) for doc in documents])
        
        if any(keyword in all_content.lower() for keyword in ['image', 'figure', 'chart']):
            metadata['has_images'] = True
        
        if '|' in all_content or 'table' in all_content.lower():
            metadata['has_tables'] = True
        
        return metadata
    
    def parse_multiple_documents(self, file_paths: List[str]) -> List[ParsedDocument]:
        results = []
        
        for file_path in file_paths:
            log.log_info(f"Processing file {len(results) + 1}/{len(file_paths)}: {file_path}")
            
            parsed_doc = self.parse_single_document(file_path)
            if parsed_doc:
                results.append(parsed_doc)
            time.sleep(1)
        
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
            results_dict = [asdict(result) for result in self.results]
            # output_file = os.path.join(self.save_parsed_file_path + output_file) #type:ignore

            with open(output_file, 'w', encoding='utf-8') as f:
                json.dump(results_dict, f, indent=2, ensure_ascii=False)
            
            log.log_info(f"Results saved to {output_file}")
            
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
            if doc.file_type in stats['file_types']:
                stats['file_types'][doc.file_type] += 1
            else:
                stats['file_types'][doc.file_type] = 1
            
            if any(len(page.images) > 0 for page in doc.pages):
                stats['documents_with_images'] += 1
            
            if any(len(page.tables) > 0 for page in doc.pages):
                stats['documents_with_tables'] += 1
        
        return stats

    def _extract_all_tables(self, lines: List[str]) -> List[Dict[str, Any]]:
        """Extract all tables from lines, avoiding duplicates."""
        tables = []
        i = 0
        processed_indices = set()
        
        while i < len(lines):
            line = lines[i].strip()
            
            # Skip if already processed
            if i in processed_indices:
                i += 1
                continue
            
            # Check if line is a table row (contains pipes and multiple cells)
            if '|' in line and len(line.split('|')) > 2:
                # Found start of a table
                table_data = self._extract_single_table(lines, i, processed_indices)
                if table_data:
                    tables.append(table_data)  # Append Dict, not List
                    # Move to end of table
                    i = table_data.get('end_index', i) + 1
                else:
                    i += 1
            else:
                i += 1
        
        return tables  # Returns List[Dict[str, Any]]
    
    def _extract_single_table(self, lines: List[str], start_index: int, processed_indices: set) -> Optional[Dict[str, Any]]:
        """Extract a single complete table and mark lines as processed."""
        table_lines = []
        i = start_index
        
        # Collect all consecutive table rows
        while i < len(lines):
            line = lines[i].strip()
            
            # Check if it's a table row (contains pipes)
            if '|' in line and len(line.split('|')) > 2:
                table_lines.append(line)
                processed_indices.add(i)
                i += 1
            elif not line:
                # Empty line, might continue table
                i += 1
            else:
                # Non-table content, end of table
                break
        
        if len(table_lines) < 1:  # Changed from < 2 to < 1
            return None
        
        # Parse table
        headers = [cell.strip() for cell in table_lines[0].split('|') if cell.strip()]
        rows = []
        
        # Skip separator line (second row if it contains dashes)
        start_data_row = 1
        if len(table_lines) > 1 and all('-' in cell or '=' in cell or not cell.strip() for cell in table_lines[1].split('|')):
            start_data_row = 2
        
        # Extract data rows
        for line_idx in range(start_data_row, len(table_lines)):
            line = table_lines[line_idx]
            
            # Skip separator rows (rows that are all dashes/equals)
            cells = [cell.strip() for cell in line.split('|')]
            if all(not cell or all(c in '-=' for c in cell) for cell in cells):
                continue
            
            row_data = [cell.strip() for cell in cells if cell.strip()]
            if row_data:
                rows.append(row_data)
        
        if not rows and len(headers) == 0:
            return None
        
        # Return single table entry as Dict
        return {
            'position': start_index,
            'end_index': i - 1,
            'headers': headers,
            'row_count': len(rows),
            'column_count': len(headers),
            'data': rows,
            'full_table': table_lines
        }

    def run_parser(self, modified_name: str, file_path: str) -> List[Dict[str, Any]]:
        log.log_info(f"Attempting to parse document: {file_path} with LlamaCloudDocumentParser.")

        single_doc_object = self.parse_single_document(file_path)

        if single_doc_object:
            log.log_info(f"Successfully parsed: {single_doc_object.filename}, Pages: {single_doc_object.total_pages}")
            self.save_results(f"{modified_name}_parsed_results.json")
            return [asdict(single_doc_object)]
        else:
            log.log_error(f"Failed to parse document: {file_path}")
            return []

    def extract_all_text(self, modified_name: str, file_path: str) -> str:
        """
        Extract all text content from parsed document in reading order.
        
        Args:
            parsed_document: Single document dict from parsed_results.json
            
        Returns:
            Combined text from all pages and tables
        """
        parsed_data = self.run_parser(modified_name, file_path)        

        all_text = []
        
        if not parsed_data:
            log.log_error("Parsing failed - no data returned")
            return ""

        # Extract filename as context
        filename = parsed_data[0].get('filename', 'Unknown Document')
        all_text.append(f"# Document: {filename}\n")
        
        # Process each page
        pages = parsed_data[0].get('pages', [])
        
        for page in pages:
            page_num = page.get('page_number', 'Unknown')
            all_text.append(f"\n## Page {page_num}\n")
            
            # Extract text content
            text_content = page.get('text_content', '').strip()
            if text_content:
                all_text.append(text_content)
            
            # Extract table data
            tables = page.get('tables', [])
            for table_idx, table in enumerate(tables, 1):
                all_text.append(f"\n### Table {table_idx}\n")
                
                # Add headers
                headers = table.get('headers', [])
                if headers:
                    all_text.append("| " + " | ".join(headers) + " |\n")
                    all_text.append("|" + "|".join(["---"] * len(headers)) + "|\n")
                
                # Add data rows
                data = table.get('data', [])
                for row in data:
                    all_text.append("| " + " | ".join(str(cell) for cell in row) + " |\n")
        
        return "\n".join(all_text)

# if __name__ == "__main__":
#     parser = LlamaCloudDocumentParser()

#     current_script_dir = Path(__file__).parent.resolve()
#     test_eoc_path = Path("test_data/sample sow.pdf")

#     if not test_eoc_path.exists():
#         log.log_error(f"Test SOB file not found at: {test_eoc_path}")
#         log.log_error("Please ensure the test file 'SOB.pdf' exists in the same directory as the script or update the path.")
#     else:
#         base_name = test_eoc_path.stem
#         log.log_info(f"Running parser_main for test file: {test_eoc_path} with base_name: {base_name}")
        
#         parsed_data_list = parser.run_parser(modified_name=base_name, file_path=str(test_eoc_path))
        
#         if parsed_data_list:
#             log.log_info(f"parser_main returned {len(parsed_data_list)} document(s).")
#             if parsed_data_list[0]:
#                 log.log_info("Sample of parsed data (first document):")
#                 doc_sample = parsed_data_list[0]
#                 summary_info = {
#                     "filename": doc_sample.get("filename"),
#                     "file_type": doc_sample.get("file_type"),
#                     "total_pages": doc_sample.get("total_pages"),
#                     "num_pages_data": len(doc_sample.get("pages", [])),
#                     "first_page_text_preview": doc_sample.get("pages", [{}])[0].get("text_content", "")[:200] + "..." if doc_sample.get("pages") else "N/A"
#                 }
#                 print(json.dumps(summary_info, indent=2))
#         else:
#             log.log_info("parser_main returned no data.")
