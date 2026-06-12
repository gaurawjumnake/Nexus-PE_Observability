import pandas as pd
import pymupdf4llm
import os
from crewai import Agent, Task, Crew, Process
from pathlib import Path
from backend.utitlites.llm_models import llm
from backend.doc_insighter.tools.file_reader_tool import CustomFileReaderTool
from pydantic import BaseModel
from typing import Optional
from dotenv import load_dotenv
load_dotenv()

from backend.doc_insighter.tools.app_logger import Logger
log = Logger()

class ResponseModel(BaseModel):
    message :  str

class PDFToMarkdown_1:
    def __init__(self, ) -> None:
        pass

    def convert(self,pdf_path):
        log.log_info(f"Converting pdf file:{pdf_path}")
        return pymupdf4llm.to_markdown(pdf_path)

class DataExtractor_1:
    def __init__(self, verbose: bool = True) -> None:
        self.verbose = verbose
        self.converter = PDFToMarkdown_1()
        self.file_reader_tool = CustomFileReaderTool()

    def summarizer_assistant(self, requirements: str, input_text: str, output_schema: Optional[ResponseModel] = None):
        summary_agent = Agent(
            role="Document Analyst",
            goal="Analyze input text and generate structured JSON response as per user requirements.",
            backstory="""You are an expert document analyzer, reader, and parser.
            You are an experienced project manager who has a very good understanding of documents used for IT related projects.
            You know how to precisely analyze Markdown text to generate responses which satisfy user requirements.""",
            llm=llm,
            tools=[self.file_reader_tool],
            verbose=self.verbose
        )
            
        summary_task = Task(
            description="""
            Task is to analyze input document or text and extract useful information as per user requirements.

            USER REQUIREMENTS:{user_requirement}
            INPUT TEXT :{input_text}

            Follow below given guidelines:
            1. Read and understand the user requirements.
            2. Analyze the input text.
            3. Generate the specific JSON response requested.
            """,
            expected_output="""Response as per user requirements in JSON format.""",
            output_json= output_schema, #type:ignore
            agent=summary_agent,
        )

        crew = Crew(
            agents=[summary_agent],
            tasks = [summary_task],
            process= Process.sequential,
            # memory=True,
            verbose=self.verbose,
        )

        result = crew.kickoff(inputs={'user_requirement':requirements,
                                      'input_text':input_text})

        return result.raw
    
    def invoke(self,user_requirement, input_content):
        if input_content:
            log.log_info("File Converted, Starting Summarizer")
        response = self.summarizer_assistant(user_requirement, input_content)
        if response:
            log.log_info(f"Process Completed, Response generated")
            return response
        else:
            log.log_debug(f"Failed to Generate response")

    def extract_data(self, input_file , user_requirement, file_type:str = 'pdf'):
        if file_type == "pdf":
            input_content = self.converter.convert(input_file)
        else:
            input_content = input_file

        response = self.invoke(user_requirement, input_content)
        return response



import pymupdf4llm
from crewai import Agent, Task, Crew, Process
from backend.utitlites.llm_models import llm
from backend.doc_insighter.tools.file_reader_tool import CustomFileReaderTool
from backend.doc_insighter.tools.app_logger import Logger
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


    
        
