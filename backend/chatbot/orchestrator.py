"""
Agentic RAG + text-to-SQL orchestrator.

The orchestrator uses a CrewAI router agent to decide whether a user question
needs document RAG, SQLite text-to-SQL, or both. Execution remains controlled:
RAG is grounded in Chroma results, and SQL execution is delegated to the
read-only FinancialTextToSQLChatbot helper.
"""

import asyncio
import json
import os
import re
from pathlib import Path
from typing import Any, Literal, Optional

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from backend.chatbot.chat import ChatQueryRequest, ChatQueryResponse, run_rag_query
from backend.chatbot.text_to_sql_chatbot import FinancialTextToSQLChatbot
from backend.utilites.app_logger import Logger

log = Logger()
router = APIRouter(prefix="/analytics", tags=["analytics"])

RouteName = Literal["rag", "sql", "both"]
DEFAULT_MODEL = os.getenv("NEXUS_ORCHESTRATOR_MODEL", os.getenv("GEMINI_MODEL", "gemini/gemini-2.5-flash"))


class AgenticAskRequest(BaseModel):
    question: str = Field(..., min_length=1)
    company_id: Optional[str] = None
    document_ids: Optional[list[str]] = None
    top_k: int = Field(default=8, ge=1, le=20)
    sql_db_path: Optional[str] = None
    sql_row_limit: int = Field(default=100, ge=1, le=1000)
    show_sql: bool = False
    verbose: bool = False


class RoutePlan(BaseModel):
    route: RouteName
    confidence: float = Field(default=0.5, ge=0, le=1)
    reasoning: str = ""


class AgenticAskResponse(BaseModel):
    answer: str
    route_plan: RoutePlan
    rag: Optional[dict[str, Any]] = None
    sql: Optional[dict[str, Any]] = None


def get_crewai_llm():
    from crewai import LLM

    api_key = os.getenv("GEMINI_API_KEY") or os.getenv("GOOGLE_API_KEY")
    if not api_key:
        raise ValueError("Missing GEMINI_API_KEY or GOOGLE_API_KEY for CrewAI Gemini LLM")

    return LLM(model=DEFAULT_MODEL, api_key=api_key, temperature=0)


def parse_json_object(text: Any) -> dict[str, Any]:
    raw = str(getattr(text, "raw", text)).strip()
    if raw.startswith("```"):
        raw = re.sub(r"^```(?:json)?\s*", "", raw, flags=re.IGNORECASE)
        raw = re.sub(r"\s*```$", "", raw)
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        match = re.search(r"\{.*\}", raw, flags=re.DOTALL)
        if not match:
            raise
        return json.loads(match.group(0))


def heuristic_route(question: str, has_documents: bool) -> RoutePlan:
    q = question.lower()
    rag_terms = {
        "why",
        "explain",
        "reason",
        "driver",
        "drivers",
        "document",
        "report",
        "source",
        "evidence",
        "summarize",
        "summarise",
        "context",
    }
    sql_terms = {
        "average",
        "sum",
        "total",
        "highest",
        "lowest",
        "top",
        "trend",
        "compare",
        "count",
        "roi",
        "revenue",
        "spend",
        "users",
        "rate",
        "score",
        "year",
        "month",
        "quarter",
    }

    wants_rag = has_documents and any(term in q for term in rag_terms)
    wants_sql = any(term in q for term in sql_terms)
    if wants_rag and wants_sql:
        return RoutePlan(route="both", confidence=0.65, reasoning="Question asks for metrics and explanation/evidence.")
    if wants_sql:
        return RoutePlan(route="sql", confidence=0.6, reasoning="Question appears to ask for structured numeric data.")
    if has_documents:
        return RoutePlan(route="rag", confidence=0.6, reasoning="Question can be answered from indexed documents.")
    return RoutePlan(route="sql", confidence=0.4, reasoning="No document scope was provided, so SQL is the available route.")


def choose_route(request: AgenticAskRequest) -> RoutePlan:
    from crewai import Agent, Crew, Process, Task

    fallback = heuristic_route(request.question, bool(request.document_ids and request.company_id))
    route_agent = Agent(
        role="RAG SQL Routing Analyst",
        goal="Select the best tool route for a user analytics question.",
        backstory=(
            "You decide whether a question needs document retrieval, structured "
            "database analytics, or both. You return strict JSON only."
        ),
        llm=get_crewai_llm(),
        verbose=request.verbose,
        allow_delegation=False,
    )
    route_task = Task(
        description=(
            "Decide the route for this query.\n\n"
            "Question: {question}\n"
            "Company id provided: {has_company}\n"
            "Document ids provided: {has_documents}\n"
            "SQL database available: true\n\n"
            "Route meanings:\n"
            "- rag: use document retrieval when the user asks why, explain, "
            "summarize, evidence, document details, or qualitative context.\n"
            "- sql: use text-to-SQL when the user asks for metrics, totals, "
            "averages, rankings, comparisons, trends, filters, dates, or table data.\n"
            "- both: use both when the user needs numeric results plus explanation "
            "or evidence from documents.\n\n"
            "Return JSON only: {\"route\":\"rag|sql|both\","
            "\"confidence\":0.0,\"reasoning\":\"short reason\"}"
        ),
        expected_output='JSON only with route, confidence, and reasoning.',
        agent=route_agent,
    )
    try:
        result = Crew(
            agents=[route_agent],
            tasks=[route_task],
            process=Process.sequential,
            verbose=request.verbose,
        ).kickoff(
            inputs={
                "question": request.question,
                "has_company": bool(request.company_id),
                "has_documents": bool(request.document_ids),
            }
        )
        payload = parse_json_object(result)
        route = str(payload.get("route", fallback.route)).lower()
        if route not in {"rag", "sql", "both"}:
            return fallback
        if route in {"rag", "both"} and not request.company_id:
            return RoutePlan(route="sql", confidence=0.5, reasoning="RAG needs company_id; falling back to SQL.")
        if route in {"rag", "both"} and not request.document_ids:
            return RoutePlan(route="sql", confidence=0.5, reasoning="RAG needs document_ids; falling back to SQL.")
        return RoutePlan(
            route=route,
            confidence=float(payload.get("confidence", fallback.confidence)),
            reasoning=str(payload.get("reasoning", fallback.reasoning)),
        )
    except Exception as exc:
        log.log_warning(f"Routing agent failed, using heuristic route: {exc}")
        return fallback


async def run_sql(request: AgenticAskRequest) -> dict[str, Any]:
    def _ask() -> dict[str, Any]:
        bot = FinancialTextToSQLChatbot(
            db_path=Path(request.sql_db_path) if request.sql_db_path else None,
            row_limit=request.sql_row_limit,
            verbose=request.verbose,
        )
        return bot.ask(request.question)

    result = await asyncio.to_thread(_ask)
    if not request.show_sql:
        result = dict(result)
        result.pop("sql", None)
    return result


async def run_rag(request: AgenticAskRequest) -> ChatQueryResponse:
    if not request.company_id:
        raise ValueError("company_id is required for RAG")
    return await run_rag_query(
        ChatQueryRequest(
            question=request.question,
            company_id=request.company_id,
            document_ids=request.document_ids,
            top_k=request.top_k,
        )
    )


def synthesize_answer(
    question: str,
    route_plan: RoutePlan,
    rag_result: Optional[ChatQueryResponse],
    sql_result: Optional[dict[str, Any]],
    verbose: bool,
) -> str:
    if route_plan.route == "rag" and rag_result:
        return rag_result.answer
    if route_plan.route == "sql" and sql_result:
        return str(sql_result.get("answer", "")).strip()

    from crewai import Agent, Crew, Process, Task

    synthesis_agent = Agent(
        role="Nexus Answer Synthesizer",
        goal="Combine document evidence and SQL analytics into one direct answer.",
        backstory=(
            "You write concise executive answers. You keep SQL facts and document "
            "evidence distinct, and you do not invent missing information."
        ),
        llm=get_crewai_llm(),
        verbose=verbose,
        allow_delegation=False,
    )
    synthesis_task = Task(
        description=(
            "Synthesize the final answer.\n\n"
            "Question: {question}\n"
            "Route plan: {route_plan}\n"
            "SQL result: {sql_result}\n"
            "RAG result: {rag_result}\n\n"
            "Rules:\n"
            "- Lead with the answer.\n"
            "- Use SQL for numbers and RAG for explanations/evidence.\n"
            "- Mention if one side lacked useful evidence.\n"
            "- Keep it concise."
        ),
        expected_output="Concise markdown answer.",
        agent=synthesis_agent,
    )
    result = Crew(
        agents=[synthesis_agent],
        tasks=[synthesis_task],
        process=Process.sequential,
        verbose=verbose,
    ).kickoff(
        inputs={
            "question": question,
            "route_plan": route_plan.model_dump(),
            "sql_result": sql_result or {},
            "rag_result": rag_result.model_dump() if rag_result else {},
        }
    )
    return str(getattr(result, "raw", result)).strip()


@router.post("/ask", response_model=AgenticAskResponse)
async def ask_agentic(request: AgenticAskRequest):
    try:
        route_plan = choose_route(request)
        rag_result: Optional[ChatQueryResponse] = None
        sql_result: Optional[dict[str, Any]] = None

        if route_plan.route in {"sql", "both"}:
            sql_result = await run_sql(request)
        if route_plan.route in {"rag", "both"}:
            rag_result = await run_rag(request)

        answer = synthesize_answer(
            question=request.question,
            route_plan=route_plan,
            rag_result=rag_result,
            sql_result=sql_result,
            verbose=request.verbose,
        )

        return AgenticAskResponse(
            answer=answer,
            route_plan=route_plan,
            rag=rag_result.model_dump() if rag_result else None,
            sql=sql_result,
        )
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc))
    except Exception as exc:
        log.log_error(f"Agentic orchestrator failed: {type(exc).__name__}: {exc}")
        raise HTTPException(status_code=500, detail=f"Agentic orchestrator failed: {exc}")
