import inspect
import json
from typing import Any, Dict, Type

from crewai.tools import BaseTool
from pydantic import BaseModel, Field

import backend.db.db_client as db
from backend.utilites.app_logger import Logger

log = Logger()

# Explicit read-only whitelist — write/admin functions are intentionally excluded
# to prevent LLM agents from mutating data.
_READ_ONLY_FUNCTIONS: Dict[str, Any] = {
    # Postgres reads
    "get_document_type_counts": db.get_document_type_counts,
    "get_chunks": db.get_chunks,
    "get_fact": db.get_fact,
    "get_facts_for_company": db.get_facts_for_company,
    "get_kpi": db.get_kpi,
    "get_kpis_for_company": db.get_kpis_for_company,
    "get_kpis": db.get_kpis,
    "get_kpi_history": db.get_kpi_history,
    "get_observations_in_range": db.get_observations_in_range,
    "get_latest_observation_on_or_before": db.get_latest_observation_on_or_before,
    "get_observation_date_range": db.get_observation_date_range,
    "get_distinct_observed_facts": db.get_distinct_observed_facts,
    "get_financial_data": db.get_financial_data,
    # SQLite reads (financial_data.db used by text-to-SQL)
    "get_sqlite_table_names": db.get_sqlite_table_names,
    "get_sqlite_table_info": db.get_sqlite_table_info,
    "get_sqlite_table_count": db.get_sqlite_table_count,
    "get_sqlite_table_sample": db.get_sqlite_table_sample,
    "get_sqlite_companies_summary": db.get_sqlite_companies_summary,
    "get_sqlite_date_range": db.get_sqlite_date_range,
    "check_sqlite_table_exists": db.check_sqlite_table_exists,
    "get_sqlite_company_row_count": db.get_sqlite_company_row_count,
    "get_sqlite_company_years": db.get_sqlite_company_years,
    "execute_sqlite_query": db.execute_sqlite_query,
}


def _generate_functions_doc() -> str:
    lines = []
    for name, func in _READ_ONLY_FUNCTIONS.items():
        sig = inspect.signature(func)
        docstring = inspect.getdoc(func) or "No description provided."
        brief = docstring.split("\n\n")[0].strip()
        lines.append(f"- `{name}{sig}`: {brief}")
    return "\n".join(lines)


_FUNCTIONS_DOC = _generate_functions_doc()


class DatabaseOperationsToolInput(BaseModel):
    function_name: str = Field(..., description="Exact name of the read-only function to call.")
    arguments: str = Field(default="{}", description="JSON dict of keyword arguments for the function.")


class DatabaseOperationsTool(BaseTool):
    name: str = "Database Operations Tool"
    description: str = (
        "Execute read-only database queries. Specify 'function_name' from the list below "
        "and 'arguments' as a JSON string of keyword arguments. "
        "Write operations (save_*, append_*, set_*) are not available.\n\n"
        f"Available functions:\n{_FUNCTIONS_DOC}"
    )
    args_schema: Type[BaseModel] = DatabaseOperationsToolInput

    def _run(self, function_name: str, arguments: str = "{}") -> str:
        log.log_info(f"DatabaseOperationsTool called: {function_name} with args {arguments}")

        if function_name not in _READ_ONLY_FUNCTIONS:
            return (
                f"Error: '{function_name}' is not available. "
                f"Available functions: {list(_READ_ONLY_FUNCTIONS.keys())}"
            )

        try:
            kwargs = json.loads(arguments)
        except json.JSONDecodeError as e:
            return f"Error: Failed to parse 'arguments' as JSON: {e}"

        if not isinstance(kwargs, dict):
            return "Error: 'arguments' JSON must be a dictionary."

        func = _READ_ONLY_FUNCTIONS[function_name]
        try:
            result = func(**kwargs)
            if hasattr(result, "to_json"):
                try:
                    return result.to_json(orient="records")
                except Exception:
                    pass
            return json.dumps(result, default=str, indent=2)
        except TypeError as e:
            sig = inspect.signature(func)
            return f"Error: Invalid arguments. Expected signature: {sig}. Details: {e}"
        except Exception as e:
            log.log_error(f"DatabaseOperationsTool error in {function_name}: {e}")
            return f"Error executing {function_name}: {e}"
