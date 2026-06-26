import argparse
import json
import os
import re
import sqlite3
from pathlib import Path
from typing import Any

from backend.utilites.app_logger import Logger
from backend.utilites.llm_models import get_crewai_llm
from backend.config import DEFAULT_SQL_MODEL as DEFAULT_MODEL

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
    r"\b(insert|update|delete|drop|alter|create|replace|truncate|attach|detach|vacuum|pragma)\b",
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


def resolve_db_path(db_path: Path | str | None = None) -> Path:
    if db_path:
        resolved = Path(db_path)
        log.log_info(f"Text-to-SQL DB path supplied by request: {resolved}")
        return resolved

    env_path = os.getenv("FINANCIAL_DATA_DB_PATH") or os.getenv("NEXUS_TEXT_TO_SQL_DB_PATH")
    if env_path:
        resolved = Path(env_path)
        log.log_info(f"Text-to-SQL DB path supplied by env: {resolved}")
        return resolved

    raise FileNotFoundError(
        "No database path configured. Set FINANCIAL_DATA_DB_PATH or NEXUS_TEXT_TO_SQL_DB_PATH."
    )


def connect(db_path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    return conn


def build_schema_context(db_path: Path) -> str:
    log.log_info(f"Building SQLite schema context for db_path={db_path}")
    with connect(db_path) as conn:
        table_rows = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' "
            "AND name IN ('financial_data', 'kpis', 'fact_observations', 'facts') ORDER BY name"
        ).fetchall()

        sections: list[str] = []
        for table_row in table_rows:
            table = table_row["name"]
            columns = conn.execute(f'PRAGMA table_info("{table}")').fetchall()
            count = conn.execute(f'SELECT COUNT(*) AS count FROM "{table}"').fetchone()["count"]
            sample = conn.execute(f'SELECT * FROM "{table}" LIMIT 3').fetchall()
            column_lines = [f"- {col['name']} ({col['type'] or 'UNKNOWN'})" for col in columns]
            sections.append(
                "\n".join(
                    [
                        f"Table: {table}",
                        f"Rows: {count}",
                        "Columns:",
                        *column_lines,
                        "Sample rows:",
                        json.dumps([dict(row) for row in sample], indent=2, default=str),
                    ]
                )
            )

        business_context: dict[str, Any] = {"notes": ["The database is SQLite."]}
        table_names = {row["name"] for row in table_rows}
        if "financial_data" in table_names:
            companies = conn.execute(
                """
                SELECT company_name, MIN(financial_year) AS min_year,
                       MAX(financial_year) AS max_year, COUNT(*) AS rows
                FROM financial_data
                GROUP BY company_name
                ORDER BY company_name
                """
            ).fetchall()
            date_range = conn.execute(
                "SELECT MIN(date) AS min_date, MAX(date) AS max_date FROM financial_data"
            ).fetchone()
            business_context.update(
                {
                    "companies": [dict(row) for row in companies],
                    "date_range": dict(date_range),
                    "notes": [
                        "The main table is financial_data.",
                        "Use company_name for company filters and financial_year for yearly filters.",
                        "date is a timestamp string; use SQLite date functions when needed.",
                    ],
                }
            )

    schema_context = "\n\n".join(
        [
            "DATABASE SCHEMA",
            *sections,
            "BUSINESS CONTEXT",
            json.dumps(business_context, indent=2, default=str),
        ]
    )
    log.log_info(
        f"Schema context built: tables={len(table_rows)}, chars={len(schema_context)}"
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


def normalize_sql(sql: str, row_limit: int) -> str:
    sql = re.sub(r"\s+", " ", sql.strip().rstrip(";")).strip()
    lowered = sql.lower()

    if not lowered.startswith(READ_ONLY_PREFIXES):
        raise SQLValidationError("Only SELECT or WITH queries are allowed.")
    if DANGEROUS_SQL.search(sql):
        raise SQLValidationError("Generated SQL contains a blocked keyword.")
    if ";" in sql:
        raise SQLValidationError("Only one SQL statement is allowed.")
    # Aggregation queries (GROUP BY) already consolidate rows — do not add an
    # arbitrary LIMIT that would silently drop groups and skew results.
    is_aggregation = bool(re.search(r"\bgroup\s+by\b", lowered))
    if not is_aggregation and not re.search(r"\blimit\s+\d+\b", lowered):
        sql = f"{sql} LIMIT {row_limit}"
    log.log_info(f"Validated read-only SQL: {sql}")
    return sql


def execute_sql(db_path: Path, sql: str, row_limit: int) -> dict[str, Any]:
    safe_sql = normalize_sql(sql, row_limit)
    log.log_info(f"Executing SQLite query against {db_path}")
    with connect(db_path) as conn:
        conn.execute(f"EXPLAIN QUERY PLAN {safe_sql}").fetchall()
        rows = conn.execute(safe_sql).fetchmany(row_limit)

    log.log_info(f"SQLite query completed: row_count={len(rows)}")
    return {
        "sql": safe_sql,
        "columns": list(rows[0].keys()) if rows else [],
        "rows": [dict(row) for row in rows],
        "row_count": len(rows),
    }


class FinancialTextToSQLChatbot:
    def __init__(
        self,
        db_path: Path | str | None = None,
        model: str | None = None,
        row_limit: int = 100,
        verbose: bool = False,
    ) -> None:
        resolved_db_path = resolve_db_path(db_path)
        if not resolved_db_path.exists():
            raise FileNotFoundError(
                f"Database not found: {resolved_db_path}. Set NEXUS_TEXT_TO_SQL_DB_PATH "
                "or point FINANCIAL_DATA_DB_PATH to a valid SQLite database."
            )
        self.db_path = resolved_db_path
        self.row_limit = row_limit
        self.verbose = verbose
        load_environment()
        self.llm = get_crewai_llm(model or DEFAULT_MODEL)
        self.schema_context = build_schema_context(resolved_db_path)
        log.log_info(
            f"FinancialTextToSQLChatbot initialized: db_path={self.db_path}, "
            f"row_limit={self.row_limit}"
        )

    async def ask(self, question: str) -> dict[str, Any]:
        log.log_info(f"Text-to-SQL ask started: question={question}")
        sql_payload = await self._generate_sql(question)
        last_error: str | None = None
        for attempt in range(3):
            try:
                import asyncio
                query_result = await asyncio.to_thread(execute_sql, self.db_path, sql_payload["sql"], self.row_limit)
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
            role="Financial SQLite Query Architect",
            goal="Convert business questions into precise, read-only SQLite SQL.",
            backstory="You write compact SQLite queries that can be executed safely.",
            llm=self.llm,
            verbose=self.verbose,
            allow_delegation=False,
        )
        sql_reviewer = Agent(
            role="SQL Safety Reviewer",
            goal="Review generated SQL for correctness, SQLite compatibility, and safety.",
            backstory="You use only the provided schema and never allow write operations.",
            llm=self.llm,
            verbose=self.verbose,
            allow_delegation=False,
        )
        draft_task = Task(
            description=(
                "Use the schema below to answer the user question with one SQLite query.\n\n"
                "{schema_context}\n\n"
                "User question: {question}\n\n"
                "Rules:\n"
                "- Use only the tables and columns in the schema.\n"
                "- Query must be read-only SELECT or WITH.\n"
                "- Prefer clear aliases.\n"
                "- For comparison, ranking, or summary questions (highest, lowest, total, "
                "average, by company, by year, etc.) use GROUP BY with the appropriate "
                "aggregate function (SUM, AVG, MAX, MIN, COUNT). Never add LIMIT to "
                "aggregation queries — return all groups so results are complete.\n"
                "- For row-level detail queries that are not aggregated, add LIMIT {row_limit}.\n"
                "- Return JSON only with keys: sql, rationale."
            ),
            expected_output='JSON only: {"sql": "...", "rationale": "..."}',
            agent=sql_architect,
        )
        review_task = Task(
            description=(
                "Review the drafted SQL. If needed, rewrite it. Return JSON only "
                "with keys: sql, rationale. The SQL must be executable in SQLite."
            ),
            expected_output='JSON only: {"sql": "...", "rationale": "..."}',
            agent=sql_reviewer,
            context=[draft_task],
        )
        result = await Crew(
            agents=[sql_architect, sql_reviewer],
            tasks=[draft_task, review_task],
            process=Process.sequential,
            verbose=self.verbose,
        ).kickoff_async(
            inputs={
                "schema_context": self.schema_context,
                "question": question,
                "row_limit": self.row_limit,
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
            role="SQLite Query Repair Specialist",
            goal="Fix invalid SQLite SQL while preserving the user intent.",
            backstory="You repair failed read-only SQLite queries using schema context.",
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
                "Return corrected JSON only with keys: sql, rationale. Use SQLite "
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
        db_path=args.db,
        model=args.model,
        row_limit=args.limit,
        verbose=args.verbose,
    )
    print("Financial Text-to-SQL Chatbot")
    print("Ask a question about the configured SQLite DB. Type 'exit' to quit.\n")

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
        description="CrewAI + Gemini agentic chatbot for SQLite text-to-SQL."
    )
    parser.add_argument(
        "question",
        nargs="*",
        help="Optional one-shot question. Omit to start interactive chat.",
    )
    parser.add_argument("--db", default=None, help="SQLite DB path.")
    parser.add_argument("--model", default=None, help=f"Gemini model, default {DEFAULT_MODEL}.")
    parser.add_argument("--limit", type=int, default=100, help="Maximum rows to return.")
    parser.add_argument("--show-sql", action="store_true", help="Print generated SQL.")
    parser.add_argument("--verbose", action="store_true", help="Enable CrewAI verbose logs.")
    return parser.parse_args()


async def async_main() -> None:
    args = parse_args()
    if args.question:
        chatbot = FinancialTextToSQLChatbot(
            db_path=args.db,
            model=args.model,
            row_limit=args.limit,
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
