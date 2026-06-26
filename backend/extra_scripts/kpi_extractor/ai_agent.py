import pandas as pd
import pymupdf4llm
import os
from crewai import Agent, Task, Crew, Process
from pathlib import Path
from backend.utilites.llm_models import get_crewai_llm
from backend.utilites.file_reader_tool import CustomFileReaderTool
from backend.utilites.app_logger import Logger
from pydantic import BaseModel
from typing import Optional, Any, Dict, List
from dotenv import load_dotenv

load_dotenv()
log = Logger()

class PDFToMarkdown:
    def convert(self, pdf_path: str) -> str:
        log.log_info(f"Converting PDF file: {pdf_path}")
        return pymupdf4llm.to_markdown(pdf_path)

class DataExtractor:
    def __init__(self, verbose: bool = True) -> None:
        self.verbose = verbose
        self.converter = PDFToMarkdown()
        self.file_reader_tool = CustomFileReaderTool()

    def _create_agent(self) -> Agent:
        """Create the document analyzer agent"""
        return Agent(
            role="Document Analyst",
            goal="Extract key insights from project documents based on user requirements",
            backstory="""You are an expert document analyzer specializing in IT project documentation 
            (SOW, WSR, technical reviews, Jira reports, test reports, SQL query results).
            You excel at parsing Markdown text content and extracting relevant information in strict JSON format.""",
            llm=llm,
            tools=[self.file_reader_tool],
            verbose=self.verbose
        )

    def _create_task(self, requirements: str, input_text: str, output_schema: Optional[List[Dict[str,str]]] = None) -> Task:
        """Create the extraction task"""
        return Task(
            description=f"""
            Analyze the input text and extract key insights as per user requirements.
            
            USER REQUIREMENTS: {requirements}
            INPUT CONTENT: {input_text}
            
            Guidelines:
            1. Read and process the input content systematically
            2. Focus on information relevant to user requirements
            3. Extract key insights in strictly structured JSON format
            4. Do not include random thoughts, only output the JSON.
            5. Handle project documents: SOW, WSR, technical reviews, Jira reports, test reports, SQL results
            """,
            expected_output="Key insights in JSON format matching user requirements",
            output_json=output_schema, #type:ignore
            agent=self._create_agent()
        )

    def extract_data(
        self, 
        input_file: str, 
        user_requirement: str, 
        file_type: str = 'pdf',
        output_schema: Optional[List[Dict[str,str]]] = None
    ) -> Any:
        """
        Extract data from input file or text based on user requirements           
        Returns: Extracted data in JSON format
        """
        if file_type == "pdf":
            log.log_info(f"Processing PDF file: {input_file}")
            input_content = self.converter.convert(input_file)
        else:
            log.log_info("Processing text input")
            input_content = input_file
        log.log_info("Starting data extraction")
        crew = Crew(
            agents=[self._create_agent()],
            tasks=[self._create_task(user_requirement, input_content, output_schema)],
            process=Process.sequential,
            verbose=self.verbose
        )
        result = crew.kickoff()
        if result:
            log.log_info("Extraction completed successfully")
            return result.raw
        else:
            log.log_debug("Failed to generate response")
            return None
        

# if __name__ == '__main__':
#     # user_requirement = """summarize provided text in 2-3 line and generate 5 questions on text with expected answers in json format. design question such that will requrie descriptive answers"""
#     user_requirement = """Summarize provided text up to 2-3 lines."""
#     input_dir = "sample_data/New folder"
#     file_path = "test_data/Health Companion-Health Insurance Plan_GEN617.pdf"
#     pdf_path = "sample_data/Urban Mixed-Use Tower Development.pdf"
#     extractor = DataExtractor(verbose=False)
#     ans = extractor.extract_data(file_path, user_requirement)
#     print(ans)


    
        
