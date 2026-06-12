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