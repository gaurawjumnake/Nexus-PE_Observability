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
        import backend.db.db_client as db
        if not db.check_sqlite_table_exists(db_path, "nexus_financial_data"):
            return {"available": False, "row_count": 0, "columns": [], "years": []}

        cols = db.get_sqlite_table_info(db_path, "nexus_financial_data")
        column_names = [c["name"] for c in cols]

        row_count = db.get_sqlite_company_row_count(db_path, company_id)
        years = db.get_sqlite_company_years(db_path, company_id)

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
        return db.get_document_type_counts(company_id)
    except Exception as exc:
        log.log_warning(f"Document type lookup failed: {exc}")
        return {}


# ---------------------------------------------------------------------------
# Intent classifier — LLM-based routing
# ---------------------------------------------------------------------------

# Chitchat fast-path: no LLM cost for trivial inputs.
CHITCHAT_PATTERNS = (
    r"^\s*(hi|hello|hey|yo|sup)[\s!.,]*$",
    r"^\s*(thanks|thank you|thx)[\s!.,]*$",
    r"^\s*(bye|goodbye|see ya)[\s!.,]*$",
    r"^\s*(how are you|what'?s up)[\s?!.,]*$",
)

_CLASSIFIER_SYSTEM = (
    "You are a routing classifier for a financial portfolio analytics chatbot. "
    "Your only job is to return valid JSON — no explanation, no markdown fences."
)

_CLASSIFIER_USER_TMPL = """\
Classify the user question into exactly one route.

Routes:
- "registry"  : user wants to understand WHAT a KPI/metric IS — its definition, formula, thresholds, data type, or how it is calculated
- "sql"        : user wants actual data values — comparisons, rankings, trends, totals across companies or years
- "rag"        : user wants qualitative reasoning from documents — strategy, narrative, root causes, or explanations from reports
- "both"       : user wants data AND qualitative context together (e.g. "which company is lowest and why", "compare revenue and explain the gap")
- "chitchat"   : off-topic, greeting, or non-business question

Context KPI (the card the user clicked on, if any): {context_kpi}
User question: {question}

Decision rules:
- "which company … and why" or any request for data + an explanation → "both"
- Context KPI set + question is about definition / formula / calculation → "registry"
- Context KPI set + question is about comparing companies or fetching values → "sql" or "both"
- When unsure between sql and rag → "both"

Respond with ONLY this JSON (no markdown, no extra text):
{{"route": "<route>", "confidence": <0.0-1.0>, "reasoning": "<one sentence>"}}"""


def _strip_context_prefix(message: str) -> str:
    return re.sub(r"^\s*\[context:[^\]]*\]\s*", "", message, flags=re.IGNORECASE)


def _extract_context_kpi(message: str) -> Optional[str]:
    m = re.match(r"^\s*\[context:\s*([^\]]+)\]\s*", message, flags=re.IGNORECASE)
    return m.group(1).strip() if m else None


async def classify_intent(request: AgenticAskRequest) -> RoutePlan:
    """LLM-based intent classifier. Chitchat is short-circuited via regex;
    everything else is routed by a fast Gemini call with structured JSON output."""
    from backend.utilites.llm_models import get_llm_client

    q = _strip_context_prefix(request.message).lower().strip()
    context_kpi = _extract_context_kpi(request.message)

    for pattern in CHITCHAT_PATTERNS:
        if re.match(pattern, q):
            return RoutePlan(
                route="chitchat",
                confidence=0.9,
                reasoning="Greeting/chitchat — no data lookup needed.",
            )

    user_prompt = _CLASSIFIER_USER_TMPL.format(
        context_kpi=context_kpi or "none",
        question=request.message,
    )

    try:
        llm = get_llm_client()
        raw = await asyncio.to_thread(
            llm.complete, user_prompt, _CLASSIFIER_SYSTEM, 256, 0.0
        )
        data = parse_json_object(raw)
        route = data.get("route", "both")
        if route not in ("registry", "sql", "rag", "both", "chitchat"):
            log.log_warning(f"LLM classifier returned unknown route {route!r}; defaulting to 'both'")
            route = "both"
        plan = RoutePlan(
            route=route,
            confidence=float(data.get("confidence", 0.7)),
            reasoning=str(data.get("reasoning", "")),
        )
        log.log_info(
            f"LLM classifier: route={plan.route}, confidence={plan.confidence:.2f}, "
            f"reasoning={plan.reasoning!r}"
        )
        return plan
    except Exception as exc:
        log.log_warning(f"LLM classifier failed ({exc}); defaulting to 'both'")
        return RoutePlan(route="both", confidence=0.5, reasoning=f"Classifier error: {exc}")


# ---------------------------------------------------------------------------
# Tool execution
# ---------------------------------------------------------------------------


async def run_sql(request: AgenticAskRequest) -> dict[str, Any]:
    """Replaced raw text-to-SQL with an agent using DatabaseOperationsTool."""
    log.log_info("DB Tool Agent execution started")
    from crewai import Agent, Crew, Process, Task
    from backend.chatbot.tools.db_tool import DatabaseOperationsTool

    question = request.message
    if request.contexts:
        question += f" (Context: {', '.join(request.contexts)})"

    db_agent = Agent(
        role="Database Operations Analyst",
        goal="Answer the user's question by utilizing the DatabaseOperationsTool to retrieve data.",
        backstory=(
            "You are a backend database analyst. You do not write raw SQL. "
            "Instead, you use the DatabaseOperationsTool to invoke python functions from db_client.py "
            "to fetch the required data. If the user asks for KPIs, fetch KPIs. If they ask for financial data, "
            "fetch financial data. Be flexible with column and fact names (e.g. 'ai_revenue' might answer a question about 'AI Revenue'). "
            "For statistical or deep-analysis questions, always fetch the full dataset — never apply artificial row limits."
        ),
        llm=get_crewai_llm(DEFAULT_MODEL),
        verbose=False,
        allow_delegation=False,
        tools=[DatabaseOperationsTool()],
    )

    db_task = Task(
        description=(
            "Answer the following question using the DatabaseOperationsTool if data is needed.\n\n"
            f"Question: {question}\n\n"
            "Rules:\n"
            "- Use the tool to gather any necessary data.\n"
            "- If the tool returns JSON with field names close to what the user asked (like 'ai_revenue' for AI Revenue), use that data.\n"
            "- For statistical or analytical questions, fetch the complete dataset — do not pass limit parameters that would truncate results.\n"
            "- Provide a clear, concise answer based ONLY on the data returned by the tool.\n"
            "- If the tool returns an error or absolutely no matching data, state that clearly."
        ),
        expected_output="A direct, informative answer to the question based on database results.",
        agent=db_agent,
    )

    result = await Crew(
        agents=[db_agent],
        tasks=[db_task],
        process=Process.sequential,
        verbose=False,
    ).kickoff_async()

    answer = str(getattr(result, "raw", result)).strip()
    log.log_info(f"DB Tool Agent execution completed, answer_chars={len(answer)}")
    return {"answer": answer, "row_count": 1}


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


def _clean_search_query(text: str) -> str:
    """Normalize any text before passing to FTS.

    Handles the common ways KPI/fact names arrive dirty:
    - Parenthetical qualifiers  "AI ROI (Portfolio-wide)"  → "AI ROI"
    - Hyphens used as separators "AI-Attributed Revenue"   → "AI Attributed Revenue"
    - Underscores as word sep    "ai_attributed_revenue"   → "ai attributed revenue"
    - Brackets/special chars     "[Context: X] why..."     → "why..."
    - Trailing punctuation / extra whitespace
    """
    # Drop parenthetical qualifiers like "(Portfolio-wide)", "(Total)", "(Q2 2026)"
    text = re.sub(r"\([^)]*\)", " ", text)
    # Drop square-bracket context tags injected by the UI
    text = re.sub(r"\[[^\]]*\]", " ", text)
    # Replace hyphens and underscores with spaces so "AI-Attributed" → "AI Attributed"
    text = re.sub(r"[-_]", " ", text)
    # Remove everything that isn't a letter, digit, or space
    text = re.sub(r"[^\w\s]", " ", text)
    # Collapse runs of whitespace
    text = re.sub(r"\s+", " ", text).strip()
    return text


async def run_registry(request: AgenticAskRequest, registry: Any) -> dict[str, Any]:
    """Look up KPI/fact definitions directly from registry.db (no LLM, no embedding)."""
    log.log_info("Registry lookup started")
    context_kpi = _extract_context_kpi(request.message)
    raw_query = context_kpi if context_kpi else request.message
    search_query = _clean_search_query(raw_query)
    log.log_info(f"Registry search query: {search_query!r} (raw={raw_query!r})")
    hits = await asyncio.to_thread(registry.search_registry, search_query, 5)

    # Fallback 1: if context_kpi alone found nothing, try the cleaned stripped question
    if not hits and context_kpi:
        fallback = _clean_search_query(_strip_context_prefix(request.message))
        log.log_info(f"Registry FTS fallback (stripped message): {fallback!r}")
        hits = await asyncio.to_thread(registry.search_registry, fallback, 5)

    # Fallback 2: try each meaningful word from the cleaned context_kpi individually
    if not hits and context_kpi:
        for term in _clean_search_query(context_kpi).split():
            if len(term) > 3:
                hits = await asyncio.to_thread(registry.search_registry, term, 5)
                if hits:
                    log.log_info(f"Registry FTS matched on term {term!r}")
                    break

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


def _format_registry_details(registry_result: dict[str, Any]) -> str:
    """Render registry details as structured markdown (used as LLM input context)."""
    details = registry_result.get("details", [])
    sections = []
    for item in details[:3]:
        name = item.get("name") or item.get("kpi_id") or item.get("fact_id") or "Unknown"
        lines = [f"**{name}**"]
        if item.get("description"):
            lines.append(item["description"])
        if item.get("business_value"):
            lines.append(f"- Business value: {item['business_value']}")
        if item.get("formula"):
            lines.append(f"- Formula: `{item['formula']}`")
        if item.get("required_facts"):
            lines.append(f"- Required facts: {', '.join(item['required_facts'])}")
        if item.get("derived_facts"):
            lines.append(f"- Derived facts: {', '.join(item['derived_facts'])}")
        if item.get("data_type"):
            lines.append(f"- Data type: {item['data_type']}")
        if item.get("unit"):
            lines.append(f"- Unit: {item['unit']}")
        if item.get("thresholds"):
            lines.append(f"- Thresholds: {item['thresholds']}")
        if item.get("example_calculation"):
            lines.append(f"- Example: {item['example_calculation']}")
        sections.append("\n".join(lines))
    return "\n\n".join(sections)


async def synthesize_registry_answer(question: str, registry_result: dict[str, Any]) -> str:
    """LLM-synthesized answer from registry details. Falls back to raw formatting if LLM fails."""
    from backend.utilites.llm_models import get_llm_client

    details = registry_result.get("details", [])
    if not details:
        return "I couldn't find a matching KPI or fact definition in the registry for that question."

    raw_context = _format_registry_details(registry_result)
    prompt = (
        f"Answer the following question directly using only the KPI/metric registry definitions below.\n\n"
        f"Question: {question}\n\n"
        f"Registry definitions:\n{raw_context}\n\n"
        "Rules:\n"
        "- Answer the question directly — do not say 'the registry shows' or 'according to the registry'.\n"
        "- If the question asks about calculation basis or formula, explain it clearly in plain language.\n"
        "- If multiple related metrics are shown, identify which is most relevant and explain the relationship.\n"
        "- If facts/inputs are listed, explain what they mean in context.\n"
        "- Be concise and specific. Use markdown where helpful."
    )
    try:
        llm = get_llm_client()
        answer = await asyncio.to_thread(llm.complete, prompt, None, 600, 0.0)
        log.log_info(f"Registry synthesis completed, chars={len(answer)}")
        return answer.strip()
    except Exception as exc:
        log.log_warning(f"Registry synthesis LLM failed ({exc}); falling back to template")
        return raw_context


# ---------------------------------------------------------------------------
# Synthesis (CrewAI is only used here — not for tool selection)
# ---------------------------------------------------------------------------


async def synthesize_answer(
    question: str,
    route_plan: RoutePlan,
    rag_result: Optional[ChatQueryResponse],
    sql_result: Optional[dict[str, Any]],
    verbose: bool,
    registry_context: Optional[str] = None,
) -> str:
    """Produce a final answer.

    For single-tool results the answer is returned directly.
    For combined results a CrewAI synthesis agent merges all available context.
    registry_context is included when a context KPI is known — it replaces RAG
    for the 'how/why is this calculated' part of mixed questions.
    """
    if route_plan.route == "rag" and rag_result:
        log.log_info("Returning direct RAG answer")
        return rag_result.answer
    if route_plan.route == "sql" and sql_result:
        log.log_info("Returning direct SQL answer")
        return str(sql_result.get("answer", "")).strip()

    # Both tools were used — synthesise
    from crewai import Agent, Crew, Process, Task
    from backend.chatbot.tools.db_tool import DatabaseOperationsTool

    synthesis_agent = Agent(
        role="Nexus Answer Synthesizer",
        goal=(
            "Combine data results, KPI/metric definitions, and document evidence "
            "into one direct, well-reasoned answer."
        ),
        backstory=(
            "You write concise executive answers grounded only in the provided context. "
            "When KPI/metric definitions are available, use them to explain 'how' or 'why' "
            "instead of relying on document excerpts. "
            "If any specific data point is still missing, use the DatabaseOperationsTool to retrieve it."
        ),
        llm=get_crewai_llm(DEFAULT_MODEL),
        verbose=verbose,
        allow_delegation=False,
        tools=[DatabaseOperationsTool()],
    )
    synthesis_task = Task(
        description=(
            "Synthesize the final answer from all available context.\n\n"
            "Question: {question}\n"
            "SQL / database result: {sql_result}\n"
            "KPI/metric registry definitions: {registry_context}\n"
            "Document RAG result: {rag_result}\n\n"
            "Rules:\n"
            "- Lead with the direct answer.\n"
            "- Use SQL data for numbers, rankings, and company comparisons.\n"
            "- Use registry definitions to explain formulas, calculation basis, or what a metric means — "
            "prefer this over document excerpts when registry context is available.\n"
            "- Only fall back to document RAG for narrative or strategic context not covered by the above.\n"
            "- Do NOT say 'indexed documents do not provide' if registry definitions are present — use those.\n"
            "- Keep it concise and specific."
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
            "sql_result": sql_result or {},
            "registry_context": registry_context or "Not available.",
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
    # IMPORTANT: rewrite BEFORE storing so history always contains the self-contained
    # question that matches the assistant's answer.  Storing the original first and
    # then answering the rewritten version creates a history mismatch that causes
    # future rewrites to reference wrong context.
    # Also skip rewriting when [Context: KPI] is already present — the context is
    # explicit and there is nothing ambiguous to resolve.
    if request.session_id:
        history = ChatHistoryStore.get(request.session_id, request.top_c)
        context_kpi_explicit = bool(_extract_context_kpi(request.message))
        if history and not context_kpi_explicit and needs_rewrite(request.message):
            rewritten = rewrite_with_history(request.message, history, DEFAULT_MODEL)
            if rewritten != request.message:
                log.log_info(f"Query rewritten: {request.message!r} -> {rewritten!r}")
                request = request.model_copy(update={"message": rewritten})
        # Store after rewrite so the recorded question matches the answer we will return
        ChatHistoryStore.add(request.session_id, "user", request.message)

    # ── Step 1: Classify intent ────────────────────────────────────────────
    route_plan = await classify_intent(request)
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
            answer = await synthesize_registry_answer(request.message, registry_result)
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

    # ── Step 3: Fetch registry context when a KPI card is in context ──────
    # Enriches synthesis so "how/why" parts are answered from KPI definitions
    # rather than document excerpts.
    registry_context: Optional[str] = None
    context_kpi = _extract_context_kpi(request.message)
    if context_kpi:
        try:
            reg_result = await run_registry(request, registry)
            if reg_result.get("details"):
                registry_context = _format_registry_details(reg_result)
                log.log_info(f"Registry context fetched for synthesis: kpi={context_kpi!r}")
        except Exception as exc:
            log.log_warning(f"Registry context fetch for synthesis failed: {exc}")

    # ── Step 4: Synthesise answer ──────────────────────────────────────────

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
        registry_context=registry_context,
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