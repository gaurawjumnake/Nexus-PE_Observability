"""
Agentic RAG + text-to-SQL orchestrator.

The orchestrator uses a CrewAI router agent to decide whether a user question
needs document RAG, SQLite text-to-SQL, or both. Execution remains controlled:
RAG is grounded in Chroma results, and SQL execution is delegated to the
read-only FinancialTextToSQLChatbot helper.
"""

import asyncio
import concurrent.futures
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
    period: Optional[str] = None
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


class AgenticToolAskResponse(BaseModel):
    """Response from the tool-based agentic orchestrator (/ask/agentic)."""

    answer: str
    tools_used: list[str] = Field(default_factory=list)
    iterations_hint: Optional[str] = None


def get_crewai_llm():
    from crewai import LLM

    api_key = os.getenv("GEMINI_API_KEY") or os.getenv("GOOGLE_API_KEY")
    if not api_key:
        raise ValueError("Missing GEMINI_API_KEY or GOOGLE_API_KEY for CrewAI Gemini LLM")

    log.log_info(f"Creating CrewAI orchestrator LLM with model={DEFAULT_MODEL}")
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


def _run_async_in_thread(coro) -> Any:
    """Run an async coroutine in a dedicated worker thread with its own event loop.

    CrewAI tool callbacks are synchronous, but our underlying helpers
    (run_rag_query, FinancialTextToSQLChatbot.ask) are async. Spawning a
    fresh thread avoids conflicts with FastAPI's running event loop.
    """
    with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
        return pool.submit(asyncio.run, coro).result()


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


async def choose_route(request: AgenticAskRequest) -> RoutePlan:
    from crewai import Agent, Crew, Process, Task

    can_use_rag = bool(request.company_id)
    fallback = heuristic_route(request.question, can_use_rag)
    log.log_info(
        f"Choosing route: question={request.question}, company_id={request.company_id}, "
        f"period={request.period}, document_ids={request.document_ids}, fallback={fallback.model_dump()}"
    )
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
            "Document ids provided: {has_document_ids}\n"
            "Company-level RAG available: {can_use_rag}\n"
            "SQL database available: true\n\n"
            "Route meanings:\n"
            "- rag: use document retrieval when the user asks why, explain, "
            "summarize, evidence, document details, or qualitative context. "
            "Document ids are optional; company_id can retrieve all indexed company documents.\n"
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
        result = await Crew(
            agents=[route_agent],
            tasks=[route_task],
            process=Process.sequential,
            verbose=request.verbose,
        ).kickoff_async(
            inputs={
                "question": request.question,
                "has_company": bool(request.company_id),
                "has_document_ids": bool(request.document_ids),
                "can_use_rag": can_use_rag,
            }
        )
        payload = parse_json_object(result)
        log.log_info(f"Routing agent raw payload: {payload}")
        route = str(payload.get("route", fallback.route)).lower()
        if route not in {"rag", "sql", "both"}:
            log.log_warning(f"Routing agent returned invalid route={route}; using fallback")
            return fallback
        if route in {"rag", "both"} and not request.company_id:
            return RoutePlan(route="sql", confidence=0.5, reasoning="RAG needs company_id; falling back to SQL.")
        selected = RoutePlan(
            route=route,
            confidence=float(payload.get("confidence", fallback.confidence)),
            reasoning=str(payload.get("reasoning", fallback.reasoning)),
        )
        log.log_info(f"Route selected by agent: {selected.model_dump()}")
        return selected
    except Exception as exc:
        log.log_warning(f"Routing agent failed, using heuristic route: {exc}")
        return fallback


async def run_sql(request: AgenticAskRequest) -> dict[str, Any]:
    log.log_info(
        f"Text-to-SQL execution started: db_path={request.sql_db_path}, "
        f"row_limit={request.sql_row_limit}, show_sql={request.show_sql}"
    )
    bot = FinancialTextToSQLChatbot(
        db_path=Path(request.sql_db_path) if request.sql_db_path else None,
        row_limit=request.sql_row_limit,
        verbose=request.verbose,
    )
    result = await bot.ask(request.question)
    
    if not request.show_sql:
        result = dict(result)
        result.pop("sql", None)
    log.log_info(f"Text-to-SQL execution completed: row_count={result.get('row_count')}")
    return result


async def run_rag(request: AgenticAskRequest) -> ChatQueryResponse:
    if not request.company_id:
        raise ValueError("company_id is required for RAG")
    log.log_info(
        f"RAG execution started: company_id={request.company_id}, period={request.period}, "
        f"document_ids={request.document_ids}, top_k={request.top_k}"
    )
    return await run_rag_query(
        ChatQueryRequest(
            question=request.question,
            company_id=request.company_id,
            period=request.period,
            document_ids=request.document_ids,
            top_k=request.top_k,
        )
    )


async def synthesize_answer(
    question: str,
    route_plan: RoutePlan,
    rag_result: Optional[ChatQueryResponse],
    sql_result: Optional[dict[str, Any]],
    verbose: bool,
) -> str:
    if route_plan.route == "rag" and rag_result:
        log.log_info("Returning direct RAG answer")
        return rag_result.answer
    if route_plan.route == "sql" and sql_result:
        log.log_info("Returning direct SQL answer")
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
    result = await Crew(
        agents=[synthesis_agent],
        tasks=[synthesis_task],
        process=Process.sequential,
        verbose=verbose,
    ).kickoff_async(
        inputs={
            "question": question,
            "route_plan": route_plan.model_dump(),
            "sql_result": sql_result or {},
            "rag_result": rag_result.model_dump() if rag_result else {},
        }
    )
    answer = str(getattr(result, "raw", result)).strip()
    log.log_info(f"Synthesis completed, chars={len(answer)}")
    return answer


# ---------------------------------------------------------------------------
# Tool-based agentic orchestration  (/ask/agentic)
# ---------------------------------------------------------------------------


def make_rag_sql_tools(request: AgenticAskRequest):
    """Build per-request CrewAI tool closures.

    All request context (company_id, period, db_path …) is captured in the
    closure so the agent only needs to pass a *question* string to each tool.
    The SQL bot is initialised once here to avoid re-building the schema
    context on every tool call.

    Returns:
        tools       – list of crewai Tool objects ready for an Agent
        tools_used  – a shared mutable list that tools append their name to
    """
    from crewai.tools import tool  # local import keeps top-level clean

    tools_used: list[str] = []

    # Pre-initialise SQL chatbot (builds schema context once per request).
    try:
        sql_bot = FinancialTextToSQLChatbot(
            db_path=Path(request.sql_db_path) if request.sql_db_path else None,
            row_limit=request.sql_row_limit,
            verbose=request.verbose,
        )
        sql_available = True
    except FileNotFoundError as exc:
        sql_bot = None
        sql_available = False
        log.log_warning(f"SQL bot init failed (tool will report unavailable): {exc}")

    # ── RAG tool ────────────────────────────────────────────────────────────
    if request.company_id:
        @tool("rag_search")
        def rag_search(question: str) -> str:
            """Search financial reports and uploaded documents stored in ChromaDB.

            Use this tool for ANY question whose answer may live inside uploaded
            documents or reports, including:
            - Structure, schema, or layout of a dataset or document
            - Descriptions of data fields, columns, tables, or metrics
            - Qualitative context: why something happened, strategy, risk factors
            - Summaries, narrative explanations, and evidence from reports
            - Any factual question when specific document_ids have been provided

            When document_ids are supplied by the user, ALWAYS call this tool
            first — the user has explicitly scoped the question to those documents.

            Args:
                question: The specific question to answer from the documents.
            """
            tools_used.append("rag")
            log.log_info(f"[rag_search tool] question={question!r}")
            try:
                result = _run_async_in_thread(
                    run_rag_query(
                        ChatQueryRequest(
                            question=question,
                            company_id=request.company_id,
                            period=request.period,
                            document_ids=request.document_ids,
                            top_k=request.top_k,
                        )
                    )
                )
                citations_text = ""
                if result.citations:
                    citations_text = "\n\nSources:\n" + "\n".join(
                        f"- [{c.document_id}] chunk {c.chunk_id}: {c.excerpt[:200]}…"
                        for c in result.citations[:4]
                    )
                return f"{result.answer}{citations_text}"
            except Exception as exc:
                log.log_warning(f"[rag_search tool] failed: {exc}")
                return f"RAG search failed: {exc}"

        rag_tool: Optional[Any] = rag_search
    else:
        rag_tool = None

    # ── SQL tool ─────────────────────────────────────────────────────────────
    @tool("sql_query")
    def sql_query(question: str) -> str:
        """Query structured financial and operational metrics from the SQLite database.

        Use this tool for quantitative questions: totals, averages, rankings,
        trends, counts, time-series comparisons, and any question that needs
        exact numbers from the structured data store.

        Args:
            question: The specific question to answer from the database.
        """
        tools_used.append("sql")
        log.log_info(f"[sql_query tool] question={question!r}")
        if not sql_available:
            return "SQL database is unavailable for this request."
        try:
            result = _run_async_in_thread(sql_bot.ask(question))  # type: ignore[union-attr]
            answer = result.get("answer", "")
            row_count = result.get("row_count", 0)
            output = f"{answer}\n\n[{row_count} row(s) returned from database]"
            if request.show_sql:
                output += f"\n\nSQL executed:\n```sql\n{result.get('sql', '')}\n```"
            return output
        except Exception as exc:
            log.log_warning(f"[sql_query tool] failed: {exc}")
            return f"SQL query failed: {exc}"

    active_tools = [t for t in [rag_tool, sql_query] if t is not None]
    return active_tools, tools_used


async def run_tool_based_agentic_query(request: AgenticAskRequest) -> AgenticToolAskResponse:
    """Run the tool-based agentic orchestrator.

    A single master agent is given both the RAG and SQL tools and is allowed
    up to ``max_iter=5`` reasoning iterations so it can:
    - call one tool and refine with another,
    - issue follow-up tool calls with narrower questions,
    - synthesise a combined answer from multiple results.
    """
    from crewai import Agent, Crew, Process, Task

    log.log_info("Building tool-based agentic crew")
    available_tools, tools_used = make_rag_sql_tools(request)

    tool_names = [t.name for t in available_tools]
    log.log_info(f"Tools registered for agent: {tool_names}")

    # Build a readable context string that the agent can use when calling tools.
    context_lines: list[str] = []
    if request.company_id:
        context_lines.append(f"Company ID: {request.company_id}")
    if request.period:
        context_lines.append(f"Period filter: {request.period}")
    if request.document_ids:
        context_lines.append(f"Document IDs (user-pinned): {', '.join(request.document_ids)}")
    context_info = "\n".join(context_lines) if context_lines else "No specific company/period scope provided."

    # Signal whether the user has explicitly scoped to specific documents.
    has_pinned_docs = bool(request.document_ids)
    doc_priority_note = (
        "IMPORTANT: The user has pinned specific document_ids. "
        "Call rag_search first for this request regardless of question type."
        if has_pinned_docs
        else ""
    )

    rag_note = (
        "- rag_search  → ChromaDB: any document content including structure, fields, "
        "descriptions, qualitative context, narrative, evidence, summaries."
        if request.company_id
        else "- rag_search  → NOT available (no company_id supplied)."
    )
    tool_guide = f"{rag_note}\n- sql_query   → SQLite: structured metrics, numbers, trends, rankings."

    orchestrator_agent = Agent(
        role="Nexus Analytics Orchestrator",
        goal=(
            "Answer the user's analytics question completely and accurately by "
            "intelligently combining structured database metrics and document evidence."
        ),
        backstory=(
            "You are the senior analytics orchestrator for the Nexus PE Observability "
            "platform. You have two data sources at your disposal: a ChromaDB vector "
            "store containing uploaded financial reports (qualitative, narrative content) "
            "and a SQLite database containing structured financial metrics. "
            "You decide which source(s) to query, can issue multiple tool calls with "
            "progressively refined questions, and always ground your final answer "
            "entirely in tool results — never inventing data."
        ),
        llm=get_crewai_llm(),
        tools=available_tools,
        verbose=request.verbose,
        allow_delegation=False,
        max_iter=5,
    )

    orchestrator_task = Task(
        description=(
            "Answer the user question below using the available tools.\n\n"
            "User question: {question}\n\n"
            "Request context:\n{context_info}\n\n"
            "{doc_priority_note}\n\n"
            "Tool guide:\n{tool_guide}\n\n"
            "Strategy (apply in order):\n"
            "1. If document_ids are pinned by the user → call rag_search FIRST with the user's question.\n"
            "2. For questions about document structure, data fields, dataset layout, or column descriptions\n"
            "   → use rag_search (this information lives in the uploaded reports).\n"
            "3. For qualitative questions (why, explain, summarise, context, strategy, risk) → use rag_search.\n"
            "4. For quantitative questions (totals, averages, trends, counts, rankings) → use sql_query.\n"
            "5. For mixed questions → call BOTH tools, possibly with refined follow-up questions.\n"
            "6. If a tool result is incomplete or ambiguous, issue a follow-up tool call with a more\n"
            "   specific question before writing your final answer.\n"
            "7. FALLBACK: If you are unsure which tool to use and rag_search is available, call it —\n"
            "   never return an answer without calling at least one tool.\n"
            "8. Synthesise all tool results into one clear, cohesive markdown answer.\n"
            "9. Clearly attribute numeric facts to the SQL tool and narrative insights to the RAG tool.\n"
            "10. If a data source returned nothing useful, say so explicitly."
        ),
        expected_output=(
            "A comprehensive, well-structured markdown answer that directly addresses "
            "the user's question, grounded entirely in the tool results obtained."
        ),
        agent=orchestrator_agent,
    )

    crew = Crew(
        agents=[orchestrator_agent],
        tasks=[orchestrator_task],
        process=Process.sequential,
        verbose=request.verbose,
    )

    result = await crew.kickoff_async(
        inputs={
            "question": request.question,
            "context_info": context_info,
            "tool_guide": tool_guide,
            "doc_priority_note": doc_priority_note,
        }
    )

    answer = str(getattr(result, "raw", result)).strip()
    # Deduplicate tool names while preserving call order.
    unique_tools = list(dict.fromkeys(tools_used))
    log.log_info(
        f"Tool-based agentic query completed: tools_used={unique_tools}, "
        f"total_tool_calls={len(tools_used)}, answer_chars={len(answer)}"
    )

    return AgenticToolAskResponse(
        answer=answer,
        tools_used=unique_tools,
        iterations_hint=f"{len(tools_used)} tool call(s): {tools_used}",
    )


@router.post("/ask/agentic", response_model=AgenticToolAskResponse)
async def ask_agentic_tools(request: AgenticAskRequest):
    """Tool-based agentic endpoint.

    Unlike the router-based ``/ask`` endpoint, here a single master agent
    iterates over the RAG and SQL tools until it builds a complete answer.
    This handles queries that need both data sources or require follow-up
    reasoning without you having to predict the route upfront.
    """
    try:
        log.log_info(f"Tool-based agentic ask started: {request.model_dump()}")
        return await run_tool_based_agentic_query(request)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc))
    except Exception as exc:
        log.log_error(
            f"Tool-based agentic orchestrator failed: {type(exc).__name__}: {exc}"
        )
        raise HTTPException(
            status_code=500,
            detail=f"Tool-based agentic orchestrator failed: {exc}",
        )


# ---------------------------------------------------------------------------
# Original router-based endpoint (kept for backwards compatibility)
# ---------------------------------------------------------------------------


@router.post("/ask", response_model=AgenticAskResponse)
async def ask_agentic(request: AgenticAskRequest):
    try:
        log.log_info(f"Agentic ask started: {request.model_dump()}")
        route_plan = await choose_route(request)
        rag_result: Optional[ChatQueryResponse] = None
        sql_result: Optional[dict[str, Any]] = None

        if route_plan.route in {"sql", "both"}:
            log.log_info("Route includes SQL; invoking text-to-SQL")
            sql_result = await run_sql(request)
        if route_plan.route in {"rag", "both"}:
            log.log_info("Route includes RAG; invoking document retrieval")
            rag_result = await run_rag(request)

        answer = await synthesize_answer(
            question=request.question,
            route_plan=route_plan,
            rag_result=rag_result,
            sql_result=sql_result,
            verbose=request.verbose,
        )

        response = AgenticAskResponse(
            answer=answer,
            route_plan=route_plan,
            rag=rag_result.model_dump() if rag_result else None,
            sql=sql_result,
        )
        log.log_info(
            f"Agentic ask completed: route={route_plan.route}, "
            f"has_rag={rag_result is not None}, has_sql={sql_result is not None}"
        )
        return response
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc))
    except Exception as exc:
        log.log_error(f"Agentic orchestrator failed: {type(exc).__name__}: {exc}")
        raise HTTPException(status_code=500, detail=f"Agentic orchestrator failed: {exc}")
