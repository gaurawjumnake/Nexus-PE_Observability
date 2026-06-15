from crewai import LLM
import os
from dotenv import load_dotenv
load_dotenv()

llm_2 = LLM(model="azure/gpt-4o",
          api_key=os.getenv("AZURE_API_KEY"),
          api_base=os.getenv("AZURE_API_BASE"),
          api_version=os.getenv("AZURE_API_VERSION"),
          temperature=0.5,
          )
llm = LLM(
    model="gemini/gemini-2.5-flash",
    api_key=os.getenv("GEMINI_API_KEY"))


# llm = LLM(model="ollama/codellama:7b",
#           base_url="http://localhost:11434")

# Sample code to test llm ############################################

from crewai import Agent, Task, Crew

data_analyst = Agent(
    role='Graph Database Analyst',
    goal='Analyze and query graph data stored in Neo4j database',
    backstory="""You are an expert graph database analyst with deep knowledge of 
    Cypher query language and Neo4j operations. You can efficiently retrieve, 
    analyze, and interpret complex graph data patterns.""",
    # tools=[neo4j_tool],
    llm=llm,
    verbose=True
)

# Example tasks
def create_sample_data_task():
    return Task(
        description="""Create sample data in the Neo4j database. Create nodes for:
        - 3 Person nodes with properties: name, age, city
        - 2 Company nodes with properties: name, industry
        - Create relationships between people and companies (WORKS_FOR)
        - Create relationships between people (KNOWS)
        
        Use appropriate Cypher CREATE statements.""",
        agent=data_analyst,
        expected_output="Confirmation that sample data has been created successfully"
    )

def run_crew():
    crew = Crew(
        agents=[data_analyst],
        tasks=[
            create_sample_data_task(),
            # query_data_task(),
            # analyze_patterns_task()
        ],
        verbose=True
    )
    
    result = crew.kickoff()
    return result

# if __name__ == "__main__":    
#     print("Starting CrewAI with Neo4j integration...")
#     result = run_crew()
#     print("\nFinal Result:")
#     print(result)



# from openai import OpenAI
 

# client = OpenAI(
#     base_url=endpoint,
#     api_key=api_key
# )
 
# completion = client.chat.completions.create(
#     model=deployment_name,
#     messages=[
#         {
#             "role": "user",
#             "content": "What is the capital of France?",
#         }
#     ],
# )
 
# print(completion.choices[0].message)


# from langchain_openai import AzureChatOpenAI 
# azure_llm = AzureChatOpenAI(
#     azure_deployment="gpt-4o",
#     api_key=os.getenv("AZURE_OPENAI_API_KEY"), # type: ignore
#     azure_endpoint=os.getenv("AZURE_OPENAI_ENDPOINT"),
#     api_version="2024-02-15-preview",
# )

# # -- Abstract Completion models -------------------------------------------
# # uv add google-genai

from abc import ABC, abstractmethod
from typing import Optional

class BaseLLMClient(ABC):

    @abstractmethod
    def complete(
        self,
        user: str,
        system: Optional[str] = None,
        max_tokens: int = 2048,
        temperature: float = 0.0,
    ) -> str:
        pass

# class AnthropicClient(BaseLLMClient):

#     def __init__(self):
#         import anthropic

#         self.client = anthropic.Anthropic(
#             api_key=os.environ["ANTHROPIC_API_KEY"]
#         )

#         self.model = os.getenv(
#             "NEXUS_ANTHROPIC_MODEL",
#             "claude-sonnet-4-5"
#         )

#     def complete(
#         self,
#         user,
#         system=None,
#         max_tokens=2048,
#         temperature=0,
#     ):
#         response = self.client.messages.create(
#             model=self.model,
#             max_tokens=max_tokens,
#             temperature=temperature,
#             system=system,
#             messages=[
#                 {
#                     "role": "user",
#                     "content": user
#                 }
#             ]
#         )

#         return response.content[0].text
    

class AzureOpenAIClient(BaseLLMClient):

    def __init__(self):

        from openai import AzureOpenAI

        self.client = AzureOpenAI(
            api_key=os.environ["AZURE_OPENAI_API_KEY"],
            api_version=os.getenv(
                "AZURE_OPENAI_API_VERSION",
                "2025-01-01-preview"
            ),
            azure_endpoint=os.environ[
                "AZURE_OPENAI_ENDPOINT"
            ]
        )

        self.deployment = os.environ[
            "AZURE_OPENAI_DEPLOYMENT"
        ]

    def complete( #type:ignore
        self,
        user,
        system=None,
        max_tokens=2048,
        temperature=0
    ):

        messages = []

        if system:
            messages.append({
                "role": "system",
                "content": system
            })

        messages.append({
            "role": "user",
            "content": user
        })

        response = self.client.chat.completions.create(
            model=self.deployment,
            messages=messages,
            max_tokens=max_tokens,
            temperature=temperature,
        )

        return response.choices[0].message.content
    

class GeminiClient(BaseLLMClient):

    def __init__(self):

        from google import genai

        self.client = genai.Client(
            api_key=os.environ["GEMINI_API_KEY"]
        )

        self.model = os.getenv(
            "NEXUS_GEMINI_MODEL",
            "gemini-2.5-pro"
        )

    def complete( #type:ignore
        self,
        user,
        system=None,
        max_tokens=2048,
        temperature=0,
    ):

        prompt = user

        if system:
            prompt = f"{system}\n\n{user}"

        response = self.client.models.generate_content(
            model=self.model,
            contents=prompt
        )

        return response.text
    
# # Calling Entry point ----------------------------------------

from functools import lru_cache

@lru_cache(maxsize=1)
def get_llm_client() -> BaseLLMClient:

    provider = os.getenv(
        "NEXUS_LLM_PROVIDER",
        "anthropic"
    ).lower()

    providers = {
        # "anthropic": AnthropicClient,
        "azure_openai": AzureOpenAIClient,
        "gemini": GeminiClient,
    }

    if provider not in providers:
        raise ValueError(
            f"Unsupported provider: {provider}"
        )

    return providers[provider]()
