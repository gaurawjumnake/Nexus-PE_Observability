from crewai.tools import BaseTool
from typing import Any, List, Union, Dict
import os
import json
import csv
from pathlib import Path
from langchain_text_splitters import RecursiveJsonSplitter, RecursiveCharacterTextSplitter
from pydantic import BaseModel, Field
from backend.doc_insighter.tools.app_logger import Logger
log = Logger()

class FileReaderInput(BaseModel):
    file_path: str = Field(..., description="Path to the file to read or direct text content")
    encoding: str = Field(default="utf-8", description="Text encoding")
    chunk_size: int = Field(default=0, description="Size of chunks (0 means no chunking)")
    overlap: int = Field(default=0, description="Overlap between chunks")


class CustomFileReaderTool(BaseTool):
    name: str = "file_reader_tool"
    description: str = "Read any type of file with automatic encoding detection and format handling, with optional chunking support"
    args_schema: Any = FileReaderInput

    def _run(self, file_path: str, encoding: str = "utf-8", chunk_size: int = 0, overlap: int = 0) -> Union[str, List[str]]:
        """
        Read any file type with automatic handling and optional chunking
        
        Args:
            file_path: Path to the file or direct text content
            encoding: Text encoding (default: utf-8)
            chunk_size: If provided, return content in chunks of this size (characters)
            overlap: Number of characters to overlap between chunks (default: 0)
        
        Returns:
            str: Full content if chunk_size is None
            List[str]: List of chunks if chunk_size is provided
        """
        try:
            if os.path.exists(file_path):
                content = self._read_file_content(file_path, encoding)
            else:
                content = f"Direct Text Input:\n{file_path}"

            if chunk_size is not None:
                return self.chunk_text(content, chunk_size, overlap)
            
            return content
                
        except Exception as e:
            return f"Error processing input: {str(e)}"
    
    def _read_file_content(self, file_path: str, encoding: str) -> str:
        """Read file content based on file type"""
        if not os.path.exists(file_path):
            return f"Error: File not found - {file_path}"

        file_ext = Path(file_path).suffix.lower()
        
        if file_ext in ['.txt', '.md', '.markdown', '.py', '.js', '.html', '.css', '.json', '.xml', '.yaml', '.yml', '.log']:
            return self._read_text_file(file_path, encoding)
        elif file_ext == '.csv':
            log.log_info("Reading file with csv")
            return self._read_csv_file(file_path, encoding)
        elif file_ext == '.json':
            log.log_info("Reading file with json")
            return self._read_json_file(file_path, encoding)
        else:
            log.log_info("Reading file with fallback")
            return self._read_with_fallback(file_path, encoding)
    
    def chunk_text(self, content: str, chunk_size: int, overlap: int = 0) -> List[str]:
        """
        Split content into chunks with optional overlap
        Args:
            content: The content to chunk
            chunk_size: Size of each chunk in characters
            overlap: Number of characters to overlap between chunks
        """
        if chunk_size <= 0:
            return [content]
        
        if overlap >= chunk_size:
            overlap = chunk_size // 2  
        
        chunks = []
        start = 0
        
        while start < len(content):
            end = start + chunk_size
            chunk = content[start:end]
            
            if chunk.strip():  
                chunks.append(chunk)
            
            start = end - overlap
            
            if end >= len(content):
                break
        
        return chunks
    
    def chunk_json(self, text: Dict, chunk_size: int) -> List[str]:
        """
        Public method to chunk json content
        
        Args:
            text: The json to chunk
            chunk_size: Size of each chunk in characters
            overlap: Number of characters to overlap between chunks
        
        Returns:
            List of json chunks
        """
        splitter  = RecursiveJsonSplitter(max_chunk_size=chunk_size)
        json_chunks = splitter.split_text(text) 
        return json_chunks
    
    def _read_text_file(self, file_path: str, encoding: str) -> str:
        """Read text files with encoding fallback"""
        encodings_to_try = [encoding, 'utf-8', 'utf-16', 'cp1252', 'latin1', 'latin']
        
        for enc in encodings_to_try:
            try:
                with open(file_path, 'r', encoding=enc, errors='replace') as f:
                    content = f.read()
                return f"File: {file_path}\nEncoding: {enc}\nContent:\n{content}"
            except UnicodeDecodeError:
                continue
            except Exception as e:
                continue
        
        return f"Error: Could not decode file {file_path} with any encoding"
    
    def _read_csv_file(self, file_path: str, encoding: str) -> str:
        """Read CSV files and return structured info"""
        try:
            with open(file_path, 'r', encoding=encoding, errors='replace') as f:
                reader = csv.reader(f)
                rows = list(reader)
            
            if not rows:
                return f"File: {file_path}\nType: CSV\nContent: Empty CSV file"
            
            headers = rows[0] if rows else []
            row_count = len(rows)

            # For CSV, include more rows in preview when chunking might be used
            preview_rows = rows[:10]  # Increased from 5 to 10
            preview = "\n".join([",".join(row) for row in preview_rows])
            
            # Also include the full CSV content for potential chunking
            full_content = "\n".join([",".join(row) for row in rows])
            
            return f"""File: {file_path}
Type: CSV
Rows: {row_count}
Columns: {len(headers)}
Headers: {headers}
Preview (first 10 rows):
{preview}

Full Content:
{full_content}"""
            
        except Exception as e:
            return self._read_text_file(file_path, encoding)
    
    def _read_json_file(self, file_path: str, encoding: str) -> str:
        """Read JSON files with pretty formatting"""
        try:
            with open(file_path, 'r', encoding=encoding, errors='replace') as f:
                data = json.load(f)
            
            pretty_json = json.dumps(data, indent=2, ensure_ascii=False)
            
            return f"""File: {file_path}
Type: JSON
Content:
{pretty_json}"""
                                
        except json.JSONDecodeError:
            return self._read_text_file(file_path, encoding)
        except Exception as e:
            return f"Error reading JSON file: {str(e)}"
    
    def _read_with_fallback(self, file_path: str, encoding: str) -> str:
        """Try reading unknown files as text with multiple encodings"""
        try:
            # First try as text
            result = self._read_text_file(file_path, encoding)
            if "Error:" not in result:
                return result
            
            # If text fails, provide file info
            size = os.path.getsize(file_path)
            ext = Path(file_path).suffix or "no extension"
            
            return f"""File: {file_path}
            Type: Unknown ({ext})
            Size: {size} bytes
            Note: Could not read as text file. May be binary or use unsupported encoding.
            """
            
        except Exception as e:
            return f"Error processing file {file_path}: {str(e)}"
