import argparse
import json
import os
import re
import sqlite3
from pathlib import Path
from typing import Any

try:
    from dotenv import load_dotenv
except ImportError:  # pragma: no cover
    load_dotenv = None

try:
    from crewai import Agent, Crew, LLM, Process, Task
except ImportError as exc:  # pragma: no cover
    raise SystemExit("CrewAI is not installed. Install it with: uv add crewai") from exc


BASE_DIR = Path(__file__).resolve().parent
WORKSPACE_ROOT = BASE_DIR.parents[2]
DEFAULT_DB_PATH = BASE_DIR / "financial_data.db"
FALLBACK_DB_PATH = WORKSPACE_ROOT / "tests_chatbot" / "financial_data.db"
DEFAULT_MODEL = "gemini/gemini-2.5-flash"
READ_ONLY_PREFIXES = ("select", "with")
DANGEROUS_SQL = re.compile(
    r"\b(insert|update|delete|drop|alter|create|replace|truncate|attach|detach|vacuum|pragma)\b",
    re.IGNORECASE,
)


class SQLValidationError(ValueError):
    """Raised when generated SQL is unsafe or invalid for this chatbot."""


def load_environment() -> None:
    if load_dotenv:
        load_dotenv(BASE_DIR / ".env")
        load_dotenv()

    gemini_key = os.getenv("GEMINI_API_KEY")
    if gemini_key and not os.getenv("GOOGLE_API_KEY"):
        os.environ["GOOGLE_API_KEY"] = gemini_key


def get_llm(model: str | None = None) -> LLM:
    load_environment()
    api_key = os.getenv("GEMINI_API_KEY") or os.getenv("GOOGLE_API_KEY")
    if not api_key:
        raise SystemExit(
            "Missing Gemini API key. Set GEMINI_API_KEY in your environment "
            "or in backend/chatbot/.env."
        )

    return LLM(
        model=model or os.getenv("GEMINI_MODEL", DEFAULT_MODEL),
        api_key=api_key,
        temperature=0,
    )


def resolve_db_path(db_path: Path | str | None = None) -> Path:
    if db_path:
        return Path(db_path)

    env_path = os.getenv("FINANCIAL_DATA_DB_PATH") or os.getenv("NEXUS_TEXT_TO_SQL_DB_PATH")
    if env_path:
        return Path(env_path)

    if DEFAULT_DB_PATH.exists():
        return DEFAULT_DB_PATH
    return FALLBACK_DB_PATH


def connect(db_path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    return conn


def build_schema_context(db_path: Path) -> str:
    with connect(db_path) as conn:
        table_rows = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name"
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

    return "\n\n".join(
        [
            "DATABASE SCHEMA",
            *sections,
            "BUSINESS CONTEXT",
            json.dumps(business_context, indent=2, default=str),
        ]
    )


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
    if not re.search(r"\blimit\s+\d+\b", lowered):
        sql = f"{sql} LIMIT {row_limit}"
    return sql


def execute_sql(db_path: Path, sql: str, row_limit: int) -> dict[str, Any]:
    safe_sql = normalize_sql(sql, row_limit)
    with connect(db_path) as conn:
        conn.execute(f"EXPLAIN QUERY PLAN {safe_sql}").fetchall()
        rows = conn.execute(safe_sql).fetchmany(row_limit)

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
                f"Database not found: {resolved_db_path}. Set FINANCIAL_DATA_DB_PATH "
                "or copy financial_data.db into backend/chatbot/."
            )
        self.db_path = resolved_db_path
        self.row_limit = row_limit
        self.verbose = verbose
        self.llm = get_llm(model)
        self.schema_context = build_schema_context(resolved_db_path)

    def ask(self, question: str) -> dict[str, Any]:
        sql_payload = self._generate_sql(question)
        last_error: str | None = None
        for attempt in range(3):
            try:
                query_result = execute_sql(self.db_path, sql_payload["sql"], self.row_limit)
                answer = self._answer_question(question, query_result)
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
                if attempt == 2:
                    break
                sql_payload = self._repair_sql(question, sql_payload["sql"], last_error)

        raise RuntimeError(f"Could not produce a valid SQL query: {last_error}")

    def _generate_sql(self, question: str) -> dict[str, Any]:
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
                "- Add LIMIT {row_limit} for detail queries.\n"
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
        result = Crew(
            agents=[sql_architect, sql_reviewer],
            tasks=[draft_task, review_task],
            process=Process.sequential,
            verbose=self.verbose,
        ).kickoff(
            inputs={
                "schema_context": self.schema_context,
                "question": question,
                "row_limit": self.row_limit,
            }
        )
        payload = parse_json_object(result)
        if "sql" not in payload:
            raise ValueError(f"Crew did not return SQL: {payload}")
        return payload

    def _repair_sql(self, question: str, bad_sql: str, error: str) -> dict[str, Any]:
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
        result = Crew(
            agents=[repair_agent],
            tasks=[repair_task],
            process=Process.sequential,
            verbose=self.verbose,
        ).kickoff(
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
        return payload

    def _answer_question(self, question: str, query_result: dict[str, Any]) -> str:
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
        result = Crew(
            agents=[analyst],
            tasks=[answer_task],
            process=Process.sequential,
            verbose=self.verbose,
        ).kickoff(
            inputs={
                "question": question,
                "query_result": json.dumps(query_result, indent=2, default=str),
            }
        )
        return str(getattr(result, "raw", result)).strip()


def interactive_chat(args: argparse.Namespace) -> None:
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
            result = chatbot.ask(question)
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


def main() -> None:
    args = parse_args()
    if args.question:
        chatbot = FinancialTextToSQLChatbot(
            db_path=args.db,
            model=args.model,
            row_limit=args.limit,
            verbose=args.verbose,
        )
        result = chatbot.ask(" ".join(args.question))
        if args.show_sql:
            print(f"SQL:\n{result['sql']}\n")
        print(result["answer"])
        return

    interactive_chat(args)


if __name__ == "__main__":
    main()
