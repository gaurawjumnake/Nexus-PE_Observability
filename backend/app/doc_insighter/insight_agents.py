import os
import re
import json
from crewai import Agent, Task, Crew, Process, LLM
from backend.utilites.llama_tool import LlamaParseTool
from backend.utilites.llm_models import llm
from dotenv import load_dotenv

load_dotenv()

class ProjectScopeCrew:
    """
    CrewAI implementation for SOW/WSR Analysis.
    Generates detailed Circle analysis with Status, Work Summaries, Reasons, and Suggestions.
    """

    def __init__(self):
        self.file_reader = LlamaParseTool()
        
        self.circles = [
            "AIML Circle", 
            "Data Eng Circle", 
            "DevOps Circle", 
            "Quality Eng Circle", 
            "Mobility Circle"
        ]

        # STRICT Taxonomy for generating precise Suggestions/Gaps
        self.taxonomy = json.dumps({
          "IT_Project_Classification": {
            "Application_Development": {
              "description": "Projects focused on building or modernizing software applications",
              "subcategories": ["Web_Application_Development", "Mobile_Application_Development", "Desktop_Application_Development", "API_and_Microservices", "SaaS_Product_Development", "Legacy_Modernization", "Low_Code_No_Code"]
            },
            "Data_and_Analytics": {
              "description": "Projects related to data processing, reporting, and analytics",
              "subcategories": ["Data_Engineering", "Data_Warehousing", "Business_Intelligence", "Advanced_Analytics", "Big_Data_Platforms", "Data_Governance", "Master_Data_Management"]
            },
            "Artificial_Intelligence_and_Machine_Learning": {
              "description": "Projects involving AI models and intelligent systems",
              "subcategories": ["Machine_Learning", "Deep_Learning", "Generative_AI", "Natural_Language_Processing", "Computer_Vision", "Recommendation_Systems", "Predictive_Analytics", "AI_Agents_and_Agentic_Systems", "Model_Monitoring_and_Evaluation"]
            },
            "Cloud_and_Infrastructure": {
              "description": "Infrastructure, hosting, and cloud transformation initiatives",
              "subcategories": ["Cloud_Migration", "Cloud_Native_Development", "Infrastructure_as_Code", "Virtualization", "Server_Management", "Storage_Solutions", "Network_Architecture", "Hybrid_and_Multi_Cloud"]
            },
            "DevOps_and_Platform_Engineering": {
              "description": "Automation, CI/CD, and developer platform projects",
              "subcategories": ["CI_CD_Pipelines", "Release_Automation", "Containerization", "Kubernetes_Orchestration", "Monitoring_and_Observability", "Platform_Engineering", "FinOps_Optimization"]
            },
            "Cybersecurity_and_Compliance": {
              "description": "Security, governance, and regulatory initiatives",
              "subcategories": ["Identity_and_Access_Management", "Application_Security", "Network_Security", "Cloud_Security", "Security_Audits", "Vulnerability_and_PenTesting", "Compliance_and_Regulation", "Data_Privacy"]
            },
            "Testing_and_Quality_Assurance": {
              "description": "Quality validation and test automation initiatives",
              "subcategories": ["Manual_Testing", "Automation_Testing", "Regression_Testing", "Performance_Testing", "Security_Testing", "UAT_Support", "Test_Framework_Development"]
            },
            "Digital_Transformation": {
              "description": "Business modernization and automation initiatives",
              "subcategories": ["Process_Automation_RPA", "Workflow_Automation", "Business_Process_Reengineering", "Legacy_Transformation", "Digital_Enablement"]
            },
            "Emerging_Technologies": {
              "description": "Innovation and experimental technology projects",
              "subcategories": ["Blockchain", "Internet_of_Things", "Edge_Computing", "AR_VR", "Robotics_and_Automation", "Quantum_Computing"]
            }
          }
        }, indent=2)

    def run(self, sow_path: str, wsr_path: str):
                
        # --- Agent 1: Analysis Agent (Scope Extractor) ---
        analysis_agent = Agent(
            role='Senior Project Analyst',
            goal='Extract "Work Descriptions" for every technical domain from the SOW.',
            backstory=(
                "You are an expert in reading SOWs. Your job is to extract 1-2 sentences that describe the specific work happening "
                "in specific domains (Data, AI, DevOps, QA, Mobile). "
                "If a domain is not mentioned, you explicitly state 'No mention found in SOW'."
            ),
            tools=[self.file_reader],
            llm=llm, 
            verbose=True,
            allow_delegation=False
        )

        # --- Agent 2: Strategy Agent (Suggestion Generator) ---
        strategy_agent = Agent(
            role='Strategic Growth Consultant',
            goal='Generate specific penetration suggestions for ALL circles based on the Strict Taxonomy.',
            backstory=(
                "You analyze the current project state. "
                "For EACH of the 5 Circles, you provide a 'Penetration Suggestion'.\n"
                "1. If the Circle is Active: Suggest an adjacent Taxonomy Subcategory to expand (e.g., 'They have CI/CD, sell them FinOps').\n"
                "2. If the Circle is Inactive: Suggest a low-hanging fruit entry point based on the other active tech."
            ),
            llm=llm,
            verbose=True,
            allow_delegation=False
        )

        # --- Agent 3: Consolidation Agent (Final JSON Formatter) ---
        consolidation_agent = Agent(
            role='Project Delivery Manager',
            goal='Generate the final detailed JSON report.',
            backstory=(
                "You aggregate insights into a strict JSON structure. "
                "You ensure every Circle has a Status, a Work Summary (from SOW), a Reason, and a Suggestion."
            ),
            llm=llm,  
            verbose=True,
            allow_delegation=False
        )

        # --- Task 1: Scope & Work Extraction ---
        analysis_task = Task(
            description=(
                f"1. Read SOW: {sow_path}\n"
                f"2. Read WSR: {wsr_path}\n"
                "3. Extract a short text summary (Work Description) of what is happening in the SOW for these areas:\n"
                "   - AI/ML/GenAI\n"
                "   - Data Engineering/ETL/SQL\n"
                "   - DevOps/Cloud/Infrastructure\n"
                "   - Quality Engineering/Testing\n"
                "   - Mobility/App Development\n"
                "4. If no work is found for an area, note that it is missing."
            ),
            expected_output="A text report describing the specific work found for each domain.",
            agent=analysis_agent
        )

        # --- Task 2: Strategic Suggestions ---
        strategy_task = Task(
            description=(
                f"Reference this Taxonomy STRICTLY for terminology:\n{self.taxonomy}\n\n"
                "Review the Work Descriptions from Task 1.\n"
                "For EACH of the 5 Circles (AIML, Data, DevOps, Quality, Mobility), generate:\n"
                "1. **Status**: YES (if work exists) or NO.\n"
                "2. **Reason**: Why is it NO? (e.g., 'Client focusing on backend only, no mobile requested').\n"
                "3. **Suggestion**: \n"
                "   - If YES: What is the next logical Subcategory from the Taxonomy to upsell?\n"
                "   - If NO: What is a standard Taxonomy Subcategory we should pitch to enter this circle?"
            ),
            expected_output="A structured analysis of Status, Reasons, and Strategic Suggestions for all 5 circles.",
            agent=strategy_agent,
            context=[analysis_task]
        )

        # --- Task 3: Final JSON Consolidation ---
        consolidation_task = Task(
            description=(
                f"Generate the final JSON Object based on the previous tasks.\n\n"
                "OUTPUT FORMAT (STRICT JSON ONLY, NO MARKDOWN):\n"
                "{\n"
                "  \"circles_analysis\": {\n"
                "      \"AIML Circle\": {\n"
                "          \"status\": \"YES/NO\",\n"
                "          \"work_happening_sow\": \"(Insert text extracted from SOW in Task 1. If NO, say 'None')\",\n"
                "          \"reason_for_status\": \"(e.g., 'Evidence of Python/GenAI found' OR 'No AI requirements in scope')\",\n"
                "          \"penetration_suggestion\": \"(Strategic suggestion from Task 2)\"\n"
                "      },\n"
                "      \"Data Eng Circle\": {\n"
                "          \"status\": \"YES/NO\",\n"
                "          \"work_happening_sow\": \"...\",\n"
                "          \"reason_for_status\": \"...\",\n"
                "          \"penetration_suggestion\": \"...\"\n"
                "      },\n"
                "      \"DevOps Circle\": {\n"
                "          \"status\": \"YES/NO\",\n"
                "          \"work_happening_sow\": \"...\",\n"
                "          \"reason_for_status\": \"...\",\n"
                "          \"penetration_suggestion\": \"...\"\n"
                "      },\n"
                "      \"Quality Eng Circle\": {\n"
                "          \"status\": \"YES/NO\",\n"
                "          \"work_happening_sow\": \"...\",\n"
                "          \"reason_for_status\": \"...\",\n"
                "          \"penetration_suggestion\": \"...\"\n"
                "      },\n"
                "      \"Mobility Circle\": {\n"
                "          \"status\": \"YES/NO\",\n"
                "          \"work_happening_sow\": \"...\",\n"
                "          \"reason_for_status\": \"...\",\n"
                "          \"penetration_suggestion\": \"...\"\n"
                "      }\n"
                "  }\n"
                "}"
            ),
            expected_output="A valid JSON object matching the structure above.",
            agent=consolidation_agent,
            context=[analysis_task, strategy_task]
        )

        # --- Execute Crew ---
        crew = Crew(
            agents=[analysis_agent, strategy_agent, consolidation_agent],
            tasks=[analysis_task, strategy_task, consolidation_task],
            process=Process.sequential,
            verbose=True
        )

        result = crew.kickoff()
        
        # Output Handling
        if hasattr(result, 'raw'):
            raw_output = result.raw #type:ignore
        else:
            raw_output = str(result)

        return self._clean_and_parse_json(raw_output)

    def _clean_and_parse_json(self, text: str) -> dict:
        try:
            text = re.sub(r'```json\s*', '', text)
            text = re.sub(r'```\s*', '', text)
            text = text.strip()
            start_index = text.find('{')
            end_index = text.rfind('}')
            if start_index != -1 and end_index != -1:
                json_str = text[start_index : end_index + 1]
                return json.loads(json_str)
            else:
                return {"error": "No JSON structure found", "raw_output": text}
        except json.JSONDecodeError as e:
            return {"error": f"Invalid JSON format: {str(e)}", "raw_output": text}