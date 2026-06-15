"""
Base Agent
===========
Provides the shared plumbing that all three CrewAI agents inherit:
  - LLM client (provider-agnostic)
  - RegistryMCPClient (registry access via MCP only)
  - JSON parsing helper

CrewAI integration note:
    These agents are designed to be wrapped as CrewAI Agent + Task pairs.
    The `run()` method on each agent is the Task callable — CrewAI passes
    context in, the agent uses MCP tools + LLM, returns structured output.

    Example CrewAI wiring (in pipeline.py):
        from crewai import Agent, Task, Crew
        from app.agents.document_classifier import DocumentClassifierAgent

        nexus_agent = DocumentClassifierAgent(llm, registry)
        crewai_agent = Agent(role="Document Classifier", ...)
        task = Task(description="...", agent=crewai_agent,
                    execute=lambda ctx: nexus_agent.run(ctx))
"""

import json
import re
from typing import Optional
from backend.utilites.llm_models import BaseLLMClient
from backend.kpi_extractor.app.mcp.client import RegistryMCPClient


class BaseAgent:
    def __init__(self, llm: BaseLLMClient, registry: RegistryMCPClient):
        self.llm = llm
        self.registry = registry

    def _parse_json(self, text: str) -> dict | list:
        """Extract and parse JSON from LLM response, stripping markdown fences."""
        cleaned = re.sub(r"```(?:json)?\s*", "", text).replace("```", "").strip()
        try:
            return json.loads(cleaned)
        except json.JSONDecodeError:
            # try finding first {...} or [...]
            m = re.search(r"(\{[\s\S]*\}|\[[\s\S]*\])", cleaned)
            if m:
                return json.loads(m.group(1))
            raise ValueError(f"No valid JSON in LLM response: {text[:200]}")
