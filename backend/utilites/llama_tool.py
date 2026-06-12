from crewai.tools import BaseTool
from pydantic import BaseModel, Field
from typing import Type
from pathlib import Path
import os
from backend.doc_insighter.core.llama_parsing import LlamaCloudDocumentParser

# 1. Define Input Schema
class LlamaParseInput(BaseModel):
    file_path: str = Field(..., description="The full system path to the file (PDF, DOCX, PPTX) to be parsed.")

# 2. Define the Tool
class LlamaParseTool(BaseTool):
    name: str = "LlamaParse Document Reader"
    description: str = (
        "An advanced AI-powered file reader. "
        "Use this tool to read SOWs and WSRs. "
        "It uses Computer Vision to perfectly extract tables, charts, and complex layouts "
        "into Markdown format."
    )
    args_schema: Type[BaseModel] = LlamaParseInput

    def _run(self, file_path: str) -> str:
        try:
            if not os.path.exists(file_path):
                return f"Error: File not found at {file_path}"

            # Instantiate your existing parser
            parser = LlamaCloudDocumentParser()

            # Your parser expects a 'modified_name' and 'file_path'
            base_name = Path(file_path).stem  

            # Call the specific method that returns the string text
            print(f"DEBUG: Agent calling LlamaParse on {file_path}")
            extracted_text = parser.extract_all_text(modified_name=base_name, file_path=file_path)

            if not extracted_text:
                return "Error: The parser returned empty content. The file might be corrupted or empty."

            return extracted_text

        except Exception as e:
            return f"Error using LlamaParse: {str(e)}"