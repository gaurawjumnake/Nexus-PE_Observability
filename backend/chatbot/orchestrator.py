"""
Agentic RAG + text-to-SQL orchestrator.

Architecture (v2 — deterministic routing):
    1. classify_intent()  — scores the question for SQL vs RAG signals using
       keyword analysis, data-availability checks, and document-type awareness.
    2. Explicit dispatch   — calls run_sql / run_rag directly (no CrewAI tool
       selection).
    3. Synthesis            — when both sources are used, a CrewAI agent merges
       the two result sets into one cohesive answer.

This eliminates the non-deterministic "let the LLM pick the tool" pattern that
was causing incorrect routing (e.g. choosing RAG for quantitative queries).
"""

import asyncio
import json
import re
import sqlite3
from pathlib import Path
from typing import Any, Literal, Optional

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from backend.chatbot.chat import ChatQueryRequest, ChatQueryResponse, run_rag_query
from backend.chatbot.chat_history import ChatHistoryStore, needs_rewrite, rewrite_with_history
from backend.chatbot.text_to_sql_chatbot import FinancialTextToSQLChatbot
from backend.utilites.app_logger import Logger
from backend.utilites.llm_models import get_crewai_llm
from backend.config import DEFAULT_ORCHESTRATOR_MODEL as DEFAULT_MODEL

log = Logger()

RouteName = Literal["rag", "sql", "both", "registry", "chitchat"]


# ---------------------------------------------------------------------------
# Request / response models
# ---------------------------------------------------------------------------


class AgenticAskRequest(BaseModel):
    message: str = Field(..., min_length=1)
    contexts: Optional[list[str]] = Field(default_factory=list)
    session_id: Optional[str] = None
    top_c: int = Field(default=5, ge=1, le=20)
    company_id: Optional[str] = None


class RoutePlan(BaseModel):
    route: RouteName
    confidence: float = Field(default=0.5, ge=0, le=1)
    reasoning: str = ""


class AgenticToolAskResponse(BaseModel):
    """Response from the deterministic agentic orchestrator (/ask/agentic)."""

    response: str
    tools_used: list[str] = Field(default_factory=list)
    route_plan: Optional[RoutePlan] = None
    sql_query_executed: Optional[str] = None
    iterations_hint: Optional[str] = None


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


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


# ---------------------------------------------------------------------------
# Data-availability helpers
# ---------------------------------------------------------------------------


def _resolve_nexus_db() -> Optional[Path]:
    """Return the path to the nexus.db / financial_data.db used by the SQL bot."""
    from backend.chatbot.text_to_sql_chatbot import resolve_db_path

    try:
        db_path = resolve_db_path(None)
        return db_path if db_path.exists() else None
    except Exception:
        return None


def check_sql_data_availability(company_id: str) -> dict[str, Any]:
    """Quick probe to see whether the SQL database has rows for *company_id*.

    Returns a dict with:
        available   – bool
        row_count   – int
        columns     – list[str]  (of the financial_data table)
        years       – list[int]  (distinct financial_year values)
    """
    db_path = _resolve_nexus_db()
    if not db_path:
        return {"available": False, "row_count": 0, "columns": [], "years": []}

    try:
        conn = sqlite3.connect(db_path)
        conn.row_factory = sqlite3.Row

        # Check if financial_data table exists
        tables = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='financial_data'"
        ).fetchall()
        if not tables:
            conn.close()
            return {"available": False, "row_count": 0, "columns": [], "years": []}

        # Get columns
        cols = conn.execute("PRAGMA table_info(financial_data)").fetchall()
        column_names = [c["name"] for c in cols]

        # Count rows for this company (fuzzy match on company_name)
        row_count = conn.execute(
            "SELECT COUNT(*) AS cnt FROM financial_data "
            "WHERE LOWER(company_name) LIKE ?",
            (f"%{company_id.lower()}%",),
        ).fetchone()["cnt"]

        # Get distinct years
        years_rows = conn.execute(
            "SELECT DISTINCT financial_year FROM financial_data "
            "WHERE LOWER(company_name) LIKE ? AND financial_year IS NOT NULL "
            "ORDER BY financial_year",
            (f"%{company_id.lower()}%",),
        ).fetchall()
        years = [r["financial_year"] for r in years_rows]

        conn.close()
        return {
            "available": row_count > 0,
            "row_count": row_count,
            "columns": column_names,
            "years": years,
        }
    except Exception as exc:
        log.log_warning(f"SQL availability check failed: {exc}")
        return {"available": False, "row_count": 0, "columns": [], "years": []}


def get_uploaded_document_types(company_id: str) -> dict[str, int]:
    """Check the documents table to see what document types exist for a company.

    Returns e.g. {"financial": 4, "narrative": 1}
    """
    try:
        import backend.db.db_client as db

        with db.get_cursor() as cur:
            from sqlalchemy import text

            rows = cur.execute(
                text(
                    "SELECT document_type, COUNT(*) AS cnt "
                    "FROM documents WHERE company_id = :cid "
                    "GROUP BY document_type"
                ),
                {"cid": company_id},
            ).fetchall()
            return {str(r.document_type or "unknown"): r.cnt for r in rows}
    except Exception as exc:
        log.log_warning(f"Document type lookup failed: {exc}")
        return {}


# ---------------------------------------------------------------------------
# Intent classifier — the core routing logic
# ---------------------------------------------------------------------------

# Keywords that signal a SQL / quantitative question
SQL_KEYWORDS = {
    # Aggregations
    "average", "avg", "mean", "sum", "total", "count",
    "minimum", "min", "maximum", "max",
    # Rankings & comparisons
    "highest", "lowest", "top", "bottom", "rank", "ranking",
    "compare", "comparison", "versus", "vs",
    # Trends & time
    "trend", "growth", "decline", "change", "increase", "decrease",
    "year", "years", "month", "quarter", "annual", "yearly", "monthly",
    "yoy", "y-o-y", "qoq", "q-o-q",
    "2020", "2021", "2022", "2023", "2024", "2025", "2026",
    # Financial metrics
    "roi", "revenue", "spend", "spending", "cost", "savings",
    "ebitda", "margin", "profit", "loss", "budget",
    "percentage", "percent", "%",
    "rate", "ratio", "score", "index",
    # Data operations
    "how much", "how many",
    "calculate", "computed",
    "filter", "group by", "breakdown",
    "users", "adoption", "headcount",
}

# Keywords that signal a RAG / qualitative question
RAG_KEYWORDS = {
    "why", "explain", "reason", "reasons", "cause", "causes",
    "driver", "drivers", "factor", "factors",
    "strategy", "strategic", "initiative", "initiatives",
    "risk", "risks", "challenge", "challenges", "opportunity",
    "summarize", "summarise", "summary", "overview", "describe",
    "context", "background", "detail", "details",
    "document", "report", "source", "evidence", "finding",
    "recommend", "recommendation", "suggestion",
    "narrative", "qualitative", "insight", "insights",
    "what happened", "what led to", "what caused",
}


# Phrases that signal the user wants to know *what a KPI/metric means*,
# not its value for a company — answered from registry.db, not SQL/RAG.
REGISTRY_TRIGGER_PATTERNS = (
    r"\btell me (more |)(on|about|regarding)\b",
    r"\btell me more\b",
    r"\bmore (detail|info|information)s?\b",
    r"\bwhat (is|are|does)\b.*\b(kpi|metric|fact)\b",
    r"\bwhat does\b.*\bmean\b",
    r"\bexplain\b.*\b(kpi|metric|formula)\b",
    r"\bdefine\b",
    r"\bdefinition of\b",
    r"\bhow (is|do you|to|are)\b.*\bcalculat",
    r"\bhow (is|do you|to|are)\b.*\bcomput",
    r"\bformula for\b",
)

# Greetings / chitchat — answered directly, no tool calls.
CHITCHAT_PATTERNS = (
    r"^\s*(hi|hello|hey|yo|sup)[\s!.,]*$",
    r"^\s*(thanks|thank you|thx)[\s!.,]*$",
    r"^\s*(bye|goodbye|see ya)[\s!.,]*$",
    r"^\s*(how are you|what'?s up)[\s?!.,]*$",
)


def _strip_context_prefix(message: str) -> str:
    """Strip a UI-injected '[Context: ...]' prefix before intent matching —
    it's metadata about what the user clicked, not part of their question."""
    return re.sub(r"^\s*\[context:[^\]]*\]\s*", "", message, flags=re.IGNORECASE)


def _extract_context_kpi(message: str) -> Optional[str]:
    """Return the KPI/entity name from a UI-injected '[Context: ...]' prefix, or None."""
    m = re.match(r"^\s*\[context:\s*([^\]]+)\]\s*", message, flags=re.IGNORECASE)
    return m.group(1).strip() if m else None


def classify_intent(request: AgenticAskRequest) -> RoutePlan:
    """Deterministic intent classifier with keyword scoring + data awareness."""
    q = _strip_context_prefix(request.message).lower().strip()
    context_kpi = _extract_context_kpi(request.message)

    for pattern in CHITCHAT_PATTERNS:
        if re.match(pattern, q):
            return RoutePlan(
                route="chitchat",
                confidence=0.9,
                reasoning="Greeting/chitchat — no data lookup needed.",
            )

    for pattern in REGISTRY_TRIGGER_PATTERNS:
        if re.search(pattern, q):
            return RoutePlan(
                route="registry",
                confidence=0.8,
                reasoning=f"Question asks about a KPI/metric definition (matched: '{pattern}').",
            )

    # Contextual follow-up: when UI provides a KPI/metric in context and the question
    # is qualitative (why/explain/low/high/threshold), look up the registry definition.
    if context_kpi and re.search(
        r"\b(why|low|high|bad|poor|good|explain|score|threshold|benchmark|rating)\b", q
    ):
        return RoutePlan(
            route="registry",
            confidence=0.75,
            reasoning=f"Contextual question about '{context_kpi}' — looking up definition and thresholds.",
        )

    words = set(re.findall(r"[a-z0-9%\-]+", q))
    # Also check multi-word phrases
    bigrams = set()
    word_list = re.findall(r"[a-z0-9%\-]+", q)
    for i in range(len(word_list) - 1):
        bigrams.add(f"{word_list[i]} {word_list[i+1]}")

    all_tokens = words | bigrams

    sql_score = 0.0
    rag_score = 0.0
    sql_matches = []
    rag_matches = []

    for kw in SQL_KEYWORDS:
        if kw in all_tokens or kw in q:
            weight = 1.5 if kw in {
                "average", "avg", "sum", "total", "roi", "percentage",
                "percent", "%", "trend", "compare", "how much", "how many",
            } else 1.0
            sql_score += weight
            sql_matches.append(kw)

    for kw in RAG_KEYWORDS:
        if kw in all_tokens or kw in q:
            weight = 1.5 if kw in {
                "why", "explain", "summarize", "summarise", "reason",
                "driver", "strategy", "what caused", "what led to",
            } else 1.0
            rag_score += weight
            rag_matches.append(kw)

    # Regex boosters for strong SQL signals
    # Year range patterns like "2023 and 2026", "2023-2026", "in 2023"
    if re.search(r"\b20\d{2}\b", q):
        sql_score += 1.0
    if re.search(r"\b20\d{2}\s*(and|to|-|–)\s*20\d{2}\b", q):
        sql_score += 2.0  # Strong signal: comparing across years
    # Percentage / numeric ask
    if re.search(r"\b\d+(\.\d+)?%", q) or "percentage" in q or "%" in q:
        sql_score += 1.5

    # Normalize scores
    total = sql_score + rag_score
    if total == 0:
        # No signal at all
        return RoutePlan(
            route="both",
            confidence=0.5,
            reasoning="No clear signal from question; defaulting to both.",
        )

    sql_ratio = sql_score / total
    rag_ratio = rag_score / total

    log.log_info(
        f"Intent scores: sql={sql_score:.1f} ({sql_matches}), "
        f"rag={rag_score:.1f} ({rag_matches}), "
        f"sql_ratio={sql_ratio:.2f}, rag_ratio={rag_ratio:.2f}"
    )

    # Decision thresholds
    BOTH_THRESHOLD = 0.15  # If the gap is within this, use both

    if abs(sql_ratio - rag_ratio) <= BOTH_THRESHOLD:
        return RoutePlan(
            route="both",
            confidence=0.6,
            reasoning=(
                f"Question has both quantitative ({sql_matches[:3]}) and "
                f"qualitative ({rag_matches[:3]}) signals. Using both tools."
            ),
        )

    if sql_ratio > rag_ratio:
        confidence = min(0.95, 0.5 + sql_ratio * 0.5)
        return RoutePlan(
            route="sql",
            confidence=confidence,
            reasoning=(
                f"Quantitative signals dominate: {sql_matches[:5]}."
            ),
        )
    else:
        confidence = min(0.95, 0.5 + rag_ratio * 0.5)
        return RoutePlan(
            route="rag",
            confidence=confidence,
            reasoning=(
                f"Qualitative signals dominate: {rag_matches[:5]}."
            ),
        )


# ---------------------------------------------------------------------------
# Tool execution
# ---------------------------------------------------------------------------


async def run_sql(request: AgenticAskRequest) -> dict[str, Any]:
    log.log_info("Text-to-SQL execution started")
    bot = FinancialTextToSQLChatbot(
        db_path=None,
        row_limit=100,
        verbose=False,
    )
    
    question = request.message
    if request.contexts:
        question += f" (Context: {', '.join(request.contexts)})"
        
    result = await bot.ask(question)
    result = dict(result)
    result.pop("sql", None)
    log.log_info(
        f"Text-to-SQL execution completed: row_count={result.get('row_count')}"
    )
    return result


async def run_rag(request: AgenticAskRequest) -> ChatQueryResponse:
    log.log_info("RAG execution started")
    
    question = request.message
    if request.contexts:
        question += f" (Context: {', '.join(request.contexts)})"
        
    return await run_rag_query(
        ChatQueryRequest(
            message=question,
            contexts=request.contexts,
            company_id=request.company_id,
        )
    )


async def run_registry(request: AgenticAskRequest, registry: Any) -> dict[str, Any]:
    """Look up KPI/fact definitions directly from registry.db (no LLM, no embedding)."""
    log.log_info("Registry lookup started")
    # Use the context KPI name when available — generic phrases like "tell me more on
    # this" or "why is the score low" produce meaningless FTS results on their own.
    context_kpi = _extract_context_kpi(request.message)
    search_query = context_kpi if context_kpi else request.message
    log.log_info(f"Registry search query: {search_query!r} (context_kpi={context_kpi!r})")
    hits = await asyncio.to_thread(registry.search_registry, search_query, 5)

    details: list[dict[str, Any]] = []
    for hit in hits:
        try:
            if hit.get("entity_type") == "kpi":
                ctx = await asyncio.to_thread(registry.get_kpi_context, hit["entity_id"])
            else:
                ctx = await asyncio.to_thread(registry.get_fact_context, hit["entity_id"])
            if ctx:
                details.append({"entity_type": hit.get("entity_type"), **ctx})
        except Exception as exc:
            log.log_warning(f"Registry context lookup failed for {hit}: {exc}")

    log.log_info(f"Registry lookup completed: hits={len(hits)}, details={len(details)}")
    return {"hits": hits, "details": details}


def format_registry_answer(registry_result: dict[str, Any]) -> str:
    """Render registry details as markdown — deterministic, no LLM needed."""
    details = registry_result.get("details", [])
    if not details:
        return "I couldn't find a matching KPI or fact definition in the registry for that question."

    sections = []
    for item in details[:3]:
        name = item.get("name") or item.get("kpi_id") or item.get("fact_id") or "Unknown"
        lines = [f"**{name}**"]
        if item.get("description"):
            lines.append(item["description"])
        if item.get("formula"):
            lines.append(f"- Formula: `{item['formula']}`")
        if item.get("required_facts"):
            lines.append(f"- Required facts: {', '.join(item['required_facts'])}")
        if item.get("data_type"):
            lines.append(f"- Data type: {item['data_type']}")
        if item.get("thresholds"):
            lines.append(f"- Thresholds: {item['thresholds']}")
        sections.append("\n".join(lines))

    return "\n\n".join(sections)


# ---------------------------------------------------------------------------
# Synthesis (CrewAI is only used here — not for tool selection)
# ---------------------------------------------------------------------------


async def synthesize_answer(
    question: str,
    route_plan: RoutePlan,
    rag_result: Optional[ChatQueryResponse],
    sql_result: Optional[dict[str, Any]],
    verbose: bool,
) -> str:
    """Produce a final answer.

    For single-tool results the answer is returned directly.
    For combined results a CrewAI synthesis agent merges both.
    """
    if route_plan.route == "rag" and rag_result:
        log.log_info("Returning direct RAG answer")
        return rag_result.answer
    if route_plan.route == "sql" and sql_result:
        log.log_info("Returning direct SQL answer")
        return str(sql_result.get("answer", "")).strip()

    # Both tools were used — synthesise
    from crewai import Agent, Crew, Process, Task

    synthesis_agent = Agent(
        role="Nexus Answer Synthesizer",
        goal="Combine document evidence and SQL analytics into one direct answer.",
        backstory=(
            "You write concise executive answers. You keep SQL facts and document "
            "evidence distinct, and you do not invent missing information."
        ),
        llm=get_crewai_llm(DEFAULT_MODEL),
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


async def run_tool_based_agentic_query(
    request: AgenticAskRequest,
    registry: Any,
) -> AgenticToolAskResponse:
    """Deterministic tool dispatch.

    1. Classify the question's intent (SQL / RAG / both).
    2. Execute the required tool(s) directly — no LLM tool selection.
    3. Synthesise a combined answer when both tools are used.
    """
    log.log_info(
        f"Deterministic agentic query started: message={request.message!r}"
    )

    # ── Step 0: Chat history — rewrite follow-up questions ────────────────
    if request.session_id:
        history = ChatHistoryStore.get(request.session_id, request.top_c)
        ChatHistoryStore.add(request.session_id, "user", request.message)
        if history and needs_rewrite(request.message):
            rewritten = rewrite_with_history(request.message, history, DEFAULT_MODEL)
            if rewritten != request.message:
                log.log_info(f"Query rewritten: {request.message!r} -> {rewritten!r}")
                request = request.model_copy(update={"message": rewritten})

    # ── Step 1: Classify intent ────────────────────────────────────────────
    route_plan = classify_intent(request)
    log.log_info(
        f"Intent classification: route={route_plan.route}, "
        f"confidence={route_plan.confidence:.2f}, "
        f"reasoning={route_plan.reasoning}"
    )

    # ── Step 2: Execute tools ──────────────────────────────────────────────
    tools_used: list[str] = []
    rag_result: Optional[ChatQueryResponse] = None
    sql_result: Optional[dict[str, Any]] = None

    if route_plan.route == "chitchat":
        return AgenticToolAskResponse(
            response=(
                "Hi! I'm your Nexus AI Assistant. Ask me about KPIs, portfolio "
                "companies, or financial metrics, and I'll pull the relevant data "
                "or definitions for you."
            ),
            tools_used=[],
            route_plan=route_plan,
            sql_query_executed=None,
            iterations_hint="Route: chitchat | no tools called",
        )

    if route_plan.route == "registry":
        try:
            registry_result = await run_registry(request, registry)
            answer = format_registry_answer(registry_result)
            hits = len(registry_result.get("hits", []))
        except Exception as exc:
            log.log_error(f"Registry lookup failed: {type(exc).__name__}: {exc}")
            answer = (
                "I couldn't reach the KPI/metric registry right now, so I can't "
                "look up that definition. Please try again in a moment."
            )
            hits = 0
        if request.session_id:
            ChatHistoryStore.add(request.session_id, "assistant", answer)
        return AgenticToolAskResponse(
            response=answer,
            tools_used=["registry"],
            route_plan=route_plan,
            sql_query_executed=None,
            iterations_hint=f"Route: registry | hits={hits}",
        )

    if route_plan.route in ("sql", "both"):
        try:
            sql_result = await run_sql(request)
            tools_used.append("sql")
        except Exception as exc:
            log.log_warning(f"SQL tool failed: {exc}")
            # If SQL fails and we were supposed to use both, try RAG alone
            if route_plan.route == "both":
                log.log_info("SQL failed; falling back to RAG-only")
            else:
                # SQL-only route failed — try RAG as fallback if possible
                log.log_info("SQL-only route failed; attempting RAG fallback")
                route_plan = RoutePlan(
                    route="rag",
                    confidence=0.4,
                    reasoning=f"SQL failed ({exc}); falling back to RAG.",
                )

    if route_plan.route in ("rag", "both"):
        try:
            rag_result = await run_rag(request)
            tools_used.append("rag")
        except Exception as exc:
            log.log_warning(f"RAG tool failed: {exc}")
            if route_plan.route == "both" and sql_result:
                log.log_info("RAG failed but SQL succeeded; using SQL-only answer")
            elif not sql_result:
                raise

    # # Step 3: Synthesise answer --------------------------------------------

    if not sql_result and not rag_result:
        raise ValueError("Both SQL and RAG tools failed to produce results.")

    question = request.message
    if request.contexts:
        question += f" (Context: {', '.join(request.contexts)})"

    answer = await synthesize_answer(
        question=question,
        route_plan=route_plan,
        rag_result=rag_result,
        sql_result=sql_result,
        verbose=False,
    )

    sql_query_str = None

    if request.session_id:
        ChatHistoryStore.add(request.session_id, "assistant", answer)

    log.log_info(
        f"Deterministic agentic query completed: route={route_plan.route}, "
        f"tools_used={tools_used}, answer_chars={len(answer)}"
    )

    return AgenticToolAskResponse(
        response=answer,
        tools_used=tools_used,
        route_plan=route_plan,
        sql_query_executed=sql_query_str,
        iterations_hint=f"Route: {route_plan.route} | Tools: {tools_used}",
    )