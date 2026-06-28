import inspect
import json
from typing import Any, Dict, Optional, Type

from crewai.tools import BaseTool
from pydantic import BaseModel, Field

import backend.db.db_client as db
from backend.utilites.app_logger import Logger

log = Logger()

def _get_available_functions() -> Dict[str, callable]: # type: ignore
    """Extract public functions from db_client that are likely meant to be queries/operations."""
    available_funcs = {}
    for name, obj in inspect.getmembers(db):
        # We only want functions defined in db_client (not imported classes/modules)
        # and ignore private/internal helpers starting with '_' or 'pg_' or 'sqlite_'
        # Wait, the user specifically wanted to expose all queries, including sqlite ones?
        # Actually, let's allow anything that is a function and doesn't start with '_' or 'pg_connect'
        if inspect.isfunction(obj) and obj.__module__ == "backend.db.db_client":
            if not name.startswith("_") and name not in {"pg_connect_kwargs", "get_cursor", "ensure_schema", "check_db_connection"}:
                available_funcs[name] = obj
    return available_funcs

def _generate_functions_doc() -> str:
    """Generate a markdown documentation string of available DB functions."""
    funcs = _get_available_functions()
    doc_lines = []
    for name, func in funcs.items():
        sig = inspect.signature(func)
        docstring = inspect.getdoc(func) or "No description provided."
        # Keep docstring brief for the LLM prompt
        brief_doc = docstring.split("\n\n")[0].strip()
        doc_lines.append(f"- `{name}{sig}`: {brief_doc}")
    return "\n".join(doc_lines)


class DatabaseOperationsToolInput(BaseModel):
    """Input for the DatabaseOperationsTool."""
    function_name: str = Field(..., description="The exact name of the function to call from db_client.py.")
    arguments: str = Field(default="{}", description="A JSON-formatted string representing a dictionary of keyword arguments to pass to the function.")


class DatabaseOperationsTool(BaseTool):
    name: str = "Database Operations Tool"
    description: str = (
        "Use this tool to execute direct database queries and CRUD operations. "
        "Instead of writing raw SQL, you must specify the 'function_name' from the available list, "
        "and supply 'arguments' as a valid JSON string of keyword arguments.\n\n"
        f"Available functions:\n{_generate_functions_doc()}"
    )
    args_schema: Type[BaseModel] = DatabaseOperationsToolInput

    def _run(self, function_name: str, arguments: str = "{}") -> str:
        log.log_info(f"DatabaseOperationsTool called: {function_name} with args {arguments}")
        available_funcs = _get_available_functions()

        if function_name not in available_funcs:
            return f"Error: Function '{function_name}' is not available. Available functions: {list(available_funcs.keys())}"

        try:
            kwargs = json.loads(arguments)
        except json.JSONDecodeError as e:
            return f"Error: Failed to parse 'arguments' as JSON: {e}"

        if not isinstance(kwargs, dict):
            return "Error: 'arguments' JSON must represent a dictionary."

        func = available_funcs[function_name]

        try:
            result = func(**kwargs)
            
            # Serialize the result safely to a string for the LLM
            # Handle pandas DataFrame specifically if needed
            if hasattr(result, "to_json"):
                try:
                    return result.to_json(orient="records")
                except Exception:
                    pass
            
            return json.dumps(result, default=str, indent=2)
        except TypeError as e:
            sig = inspect.signature(func)
            return f"Error executing function: Invalid arguments provided. Expected signature: {sig}. Details: {e}"
        except Exception as e:
            log.log_error(f"Error in DatabaseOperationsTool executing {function_name}: {e}")
            return f"Error executing {function_name}: {e}"


