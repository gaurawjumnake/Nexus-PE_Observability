import argparse
import json
import os
import re
from functools import lru_cache
from typing import Any

import yaml

from backend.utilites.app_logger import Logger
from backend.utilites.llm_models import get_crewai_llm
from backend.config import DEFAULT_SQL_MODEL as DEFAULT_MODEL, SQL_AGENT_SCHEMA_CATALOG_PATH

try:
    from dotenv import load_dotenv
except ImportError:  # pragma: no cover
    load_dotenv = None

try:
    from crewai import Agent, Crew, Process, Task
except ImportError as exc:  # pragma: no cover
    raise SystemExit("CrewAI is not installed. Install it with: uv add crewai") from exc

READ_ONLY_PREFIXES = ("select", "with")
DANGEROUS_SQL = re.compile(
    r"\b(insert|update|delete|drop|alter|create|replace|truncate|attach|detach|vacuum|grant|revoke)\b",
    re.IGNORECASE,
)
log = Logger()


class SQLValidationError(ValueError):
    """Raised when generated SQL is unsafe or invalid for this chatbot."""


def load_environment() -> None:
    if load_dotenv:
        load_dotenv()

    gemini_key = os.getenv("GEMINI_API_KEY")
    if gemini_key and not os.getenv("GOOGLE_API_KEY"):
        os.environ["GOOGLE_API_KEY"] = gemini_key


@lru_cache(maxsize=1)
def load_table_catalog() -> dict[str, Any]:
    """Load the hand-curated table catalog (nexus_* + registry_* tables only).

    This is the single source of truth for which tables the SQL agent is
    allowed to see and query — the Postgres database also hosts unrelated
    tables belonging to a separate dashboard app, which must never appear
    here or be queryable by this agent.
    """
    with open(SQL_AGENT_SCHEMA_CATALOG_PATH) as f:
        catalog = yaml.safe_load(f)
    log.log_info(f"Table catalog loaded: {len(catalog['tables'])} tables")
    return catalog


def allowed_tables() -> set[str]:
    return set(load_table_catalog()["tables"].keys())


def _fetch_sample_rows(table: str, limit: int = 3) -> list[dict]:
    """Best-effort live sample rows for one table. Never fatal — the static
    catalog summary/columns are enough context on their own if this fails."""
    from backend.db.db_client import engine
    from sqlalchemy import text as sql_text

    try:
        with engine.connect() as conn:
            rows = conn.execute(sql_text(f'SELECT * FROM "{table}" LIMIT {limit}')).fetchall()
            return [dict(r._mapping) for r in rows]
    except Exception as exc:
        log.log_warning(f"Sample-row fetch failed for table={table}: {exc}")
        return []


def build_schema_context(include_samples: bool = True) -> str:
    """Render the YAML table catalog (+ optional live sample rows) into the
    prompt text the SQL-writing agent uses. Postgres-backed — no DB path."""
    catalog = load_table_catalog()
    log.log_info(f"Building Postgres schema context from catalog: tables={len(catalog['tables'])}")

    sections: list[str] = []
    for table, meta in catalog["tables"].items():
        column_lines = [
            f"- {col['name']} ({col['type']})" + (f" — {col['note']}" if col.get("note") else "")
            for col in meta["columns"]
        ]
        lines = [
            f"Table: {table}",
            f"Summary: {meta['summary'].strip()}",
            f"Primary key: {', '.join(meta.get('primary_key', []))}",
            "Columns:",
            *column_lines,
        ]
        if meta.get("foreign_keys"):
            lines.append("Foreign keys:")
            lines += [f"- {fk['column']} -> {fk['references']}" for fk in meta["foreign_keys"]]
        if meta.get("related_to"):
            lines.append("Logical joins (not enforced FKs):")
            lines += [
                f"- {rel['column']} -> {rel['references']} ({rel.get('note', '')})"
                for rel in meta["related_to"]
            ]
        if include_samples:
            sample = _fetch_sample_rows(table)
            if sample:
                lines.append("Sample rows:")
                lines.append(json.dumps(sample, indent=2, default=str))
        sections.append("\n".join(lines))

    business_context = {
        "notes": [
            "The database is PostgreSQL.",
            "Only the tables listed above exist for this agent — the database also "
            "hosts unrelated tables from a different application; never reference "
            "any table not listed above.",
            "nexus_facts/nexus_fact_observations.value are stored as TEXT — cast "
            "explicitly (e.g. ::numeric) for numeric comparisons or aggregates.",
            "nexus_financial_data.company_name holds the full list of portfolio "
            "companies — use it for 'list companies' style questions.",
            "registry_facts/registry_kpis hold definitions/metadata, not values — "
            "join to nexus_facts/nexus_kpis on fact_id/kpi_id for actual values.",
        ]
    }

    schema_context = "\n\n".join(
        [
            "DATABASE SCHEMA",
            *sections,
            "BUSINESS CONTEXT",
            json.dumps(business_context, indent=2, default=str),
        ]
    )
    log.log_info(
        f"Schema context built: tables={len(catalog['tables'])}, chars={len(schema_context)}"
    )
    return schema_context


def strip_code_fence(text: str) -> str:
    cleaned = text.strip()
    if cleaned.startswith("```"):
        cleaned = re.sub(r"^```(?:json|sql)?\s*", "", cleaned, flags=re.IGNORECASE)
        cleaned = re.sub(r"\s*```$", "", cleaned)
    return cleaned.strip()


def parse_json_object(text: Any) -> dict[str, Any]:
    raw_text = strip_code_fence(str(getattr(text, "raw", text)))
    try:
        return json.loads(raw_text)
    except json.JSONDecodeError:
        match = re.search(r"\{.*\}", raw_text, flags=re.DOTALL)
        if match:
            return json.loads(match.group(0))
        raise


_TABLE_REF_RE = re.compile(r"\b(?:from|join)\s+\"?([a-zA-Z_][a-zA-Z0-9_]*)\"?", re.IGNORECASE)


def _referenced_tables(sql: str) -> set[str]:
    return {m.lower() for m in _TABLE_REF_RE.findall(sql)}


def normalize_sql(sql: str) -> str:
    sql = re.sub(r"\s+", " ", sql.strip().rstrip(";")).strip()
    lowered = sql.lower()

    if not lowered.startswith(READ_ONLY_PREFIXES):
        raise SQLValidationError("Only SELECT or WITH queries are allowed.")
    if DANGEROUS_SQL.search(sql):
        raise SQLValidationError("Generated SQL contains a blocked keyword.")
    if ";" in sql:
        raise SQLValidationError("Only one SQL statement is allowed.")

    referenced = _referenced_tables(sql)
    allowed = allowed_tables()
    disallowed = referenced - allowed
    if disallowed:
        raise SQLValidationError(
            f"Query references table(s) outside the allowed catalog: {sorted(disallowed)}. "
            f"Allowed tables: {sorted(allowed)}."
        )

    log.log_info(f"Validated read-only SQL: {sql}")
    return sql


def execute_sql(sql: str) -> dict[str, Any]:
    """Execute a validated read-only SQL statement against Postgres."""
    from backend.db.db_client import engine
    from sqlalchemy import text as sql_text

    safe_sql = normalize_sql(sql)
    log.log_info("Executing query against Postgres")

    with engine.connect() as conn:
        result = conn.execute(sql_text(safe_sql))
        columns = list(result.keys())
        rows = [dict(r._mapping) for r in result.fetchall()]

    log.log_info(f"Postgres query completed: row_count={len(rows)}")
    return {
        "sql": safe_sql,
        "columns": columns,
        "rows": rows,
        "row_count": len(rows),
    }


@lru_cache(maxsize=1)
def get_text_to_sql_chatbot() -> "FinancialTextToSQLChatbot":
    """Process-wide singleton — schema context (incl. sample-row queries) is
    built once per warm container instead of on every chat message."""
    return FinancialTextToSQLChatbot()


class FinancialTextToSQLChatbot:
    def __init__(
        self,
        model: str | None = None,
        verbose: bool = False,
    ) -> None:
        self.verbose = verbose
        load_environment()
        self.llm = get_crewai_llm(model or DEFAULT_MODEL)
        self.schema_context = build_schema_context()
        log.log_info("FinancialTextToSQLChatbot initialized (Postgres, catalog-scoped)")

    async def ask(self, question: str) -> dict[str, Any]:
        log.log_info(f"Text-to-SQL ask started: question={question}")
        sql_payload = await self._generate_sql(question)
        last_error: str | None = None
        for attempt in range(3):
            try:
                import asyncio
                query_result = await asyncio.to_thread(execute_sql, sql_payload["sql"])
                answer = await self._answer_question(question, query_result)
                log.log_info(
                    f"Text-to-SQL ask completed: row_count={query_result['row_count']}"
                )
                return {
                    "question": question,
                    "sql": query_result["sql"],
                    "columns": query_result["columns"],
                    "rows": query_result["rows"],
                    "row_count": query_result["row_count"],
                    "answer": answer,
                }
            except Exception as exc:
                last_error = str(exc)
                log.log_warning(
                    f"Text-to-SQL attempt {attempt + 1} failed: {type(exc).__name__}: {exc}"
                )
                if attempt == 2:
                    break
                sql_payload = await self._repair_sql(question, sql_payload["sql"], last_error)

        raise RuntimeError(f"Could not produce a valid SQL query: {last_error}")

    async def _generate_sql(self, question: str) -> dict[str, Any]:
        log.log_info("Starting CrewAI SQL generation")
        sql_architect = Agent(
            role="Financial PostgreSQL Query Architect",
            goal="Convert business questions into precise, read-only PostgreSQL SQL.",
            backstory="You write compact PostgreSQL queries that can be executed safely.",
            llm=self.llm,
            verbose=self.verbose,
            allow_delegation=False,
        )
        draft_task = Task(
            description=(
                "Use the schema below to answer the user question with one PostgreSQL query.\n\n"
                "{schema_context}\n\n"
                "User question: {question}\n\n"
                "Rules:\n"
                "- Use ONLY the tables and columns listed in the schema above. Never "
                "invent or guess a table/column name, and never reference any table "
                "not explicitly listed — the database also hosts unrelated tables "
                "from a different application.\n"
                "- Query must be read-only SELECT or WITH.\n"
                "- Prefer clear aliases.\n"
                "- For comparison, ranking, or summary questions (highest, lowest, total, "
                "average, by company, by year, etc.) use GROUP BY with the appropriate "
                "aggregate function (SUM, AVG, MAX, MIN, COUNT).\n"
                "- Do NOT add LIMIT to any query — always return the full result set "
                "so statistical and deep-analysis answers are complete.\n"
                "- Before returning, re-check your own SQL: confirm it is executable "
                "PostgreSQL syntax and every table/column referenced exists in the "
                "schema above; rewrite it yourself if it has any issue.\n"
                "- Return JSON only with keys: sql, rationale."
            ),
            expected_output='JSON only: {"sql": "...", "rationale": "..."}',
            agent=sql_architect,
        )
        result = await Crew(
            agents=[sql_architect],
            tasks=[draft_task],
            process=Process.sequential,
            verbose=self.verbose,
        ).kickoff_async(
            inputs={
                "schema_context": self.schema_context,
                "question": question,
            }
        )
        payload = parse_json_object(result)
        if "sql" not in payload:
            raise ValueError(f"Crew did not return SQL: {payload}")
        log.log_info(f"CrewAI SQL generation completed: {payload.get('sql')}")
        return payload

    async def _repair_sql(self, question: str, bad_sql: str, error: str) -> dict[str, Any]:
        log.log_info(f"Starting CrewAI SQL repair for error={error}")
        repair_agent = Agent(
            role="PostgreSQL Query Repair Specialist",
            goal="Fix invalid PostgreSQL SQL while preserving the user intent.",
            backstory="You repair failed read-only PostgreSQL queries using schema context.",
            llm=self.llm,
            verbose=self.verbose,
            allow_delegation=False,
        )
        repair_task = Task(
            description=(
                "{schema_context}\n\n"
                "User question: {question}\n"
                "Failed SQL: {bad_sql}\n"
                "Error: {error}\n\n"
                "Return corrected JSON only with keys: sql, rationale. Use PostgreSQL "
                "syntax and only read-only SELECT or WITH."
            ),
            expected_output='JSON only: {"sql": "...", "rationale": "..."}',
            agent=repair_agent,
        )
        result = await Crew(
            agents=[repair_agent],
            tasks=[repair_task],
            process=Process.sequential,
            verbose=self.verbose,
        ).kickoff_async(
            inputs={
                "schema_context": self.schema_context,
                "question": question,
                "bad_sql": bad_sql,
                "error": error,
            }
        )
        payload = parse_json_object(result)
        if "sql" not in payload:
            raise ValueError(f"Repair crew did not return SQL: {payload}")
        log.log_info(f"CrewAI SQL repair completed: {payload.get('sql')}")
        return payload

    async def _answer_question(self, question: str, query_result: dict[str, Any]) -> str:
        log.log_info("Starting CrewAI SQL result answer")

        analyst = Agent(
            role="Financial Data Analyst",
            goal="Explain SQL result rows as a concise business answer.",
            backstory="You translate financial and AI metrics into direct business answers.",
            llm=self.llm,
            verbose=self.verbose,
            allow_delegation=False,
        )
        answer_task = Task(
            description=(
                "Answer the user question using only these SQL results.\n\n"
                "User question: {question}\n"
                "SQL result JSON: {query_result}\n\n"
                "Guidelines:\n"
                "- Be concise and specific.\n"
                "- Mention when the result is limited to the returned rows.\n"
                "- Do not invent values that are not in the SQL result."
            ),
            expected_output="Concise markdown answer.",
            agent=analyst,
        )
        result = await Crew(
            agents=[analyst],
            tasks=[answer_task],
            process=Process.sequential,
            verbose=self.verbose,
        ).kickoff_async(
            inputs={
                "question": question,
                "query_result": json.dumps(query_result, indent=2, default=str),
            }
        )
        answer = str(getattr(result, "raw", result)).strip()
        log.log_info(f"CrewAI SQL result answer completed, chars={len(answer)}")
        return answer


async def interactive_chat(args: argparse.Namespace) -> None:
    chatbot = FinancialTextToSQLChatbot(
        model=args.model,
        verbose=args.verbose,
    )
    print("Financial Text-to-SQL Chatbot")
    print("Ask a question about the catalog-scoped Postgres tables. Type 'exit' to quit.\n")

    while True:
        question = input("You: ").strip()
        if question.lower() in {"exit", "quit", "q"}:
            break
        if not question:
            continue
        try:
            result = await chatbot.ask(question)
            if args.show_sql:
                print(f"\nSQL:\n{result['sql']}")
            print(f"\nBot:\n{result['answer']}\n")
        except Exception as exc:
            print(f"\nBot: I could not answer that yet. Error: {exc}\n")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="CrewAI + Gemini agentic chatbot for Postgres text-to-SQL."
    )
    parser.add_argument(
        "question",
        nargs="*",
        help="Optional one-shot question. Omit to start interactive chat.",
    )
    parser.add_argument("--model", default=None, help=f"Gemini model, default {DEFAULT_MODEL}.")
    parser.add_argument("--show-sql", action="store_true", help="Print generated SQL.")
    parser.add_argument("--verbose", action="store_true", help="Enable CrewAI verbose logs.")
    return parser.parse_args()


async def async_main() -> None:
    args = parse_args()
    if args.question:
        chatbot = FinancialTextToSQLChatbot(
            model=args.model,
            verbose=args.verbose,
        )
        result = await chatbot.ask(" ".join(args.question))
        if args.show_sql:
            print(f"SQL:\n{result['sql']}\n")
        print(result["answer"])
        return

    await interactive_chat(args)


def main() -> None:
    import asyncio
    asyncio.run(async_main())


if __name__ == "__main__":
    main()
