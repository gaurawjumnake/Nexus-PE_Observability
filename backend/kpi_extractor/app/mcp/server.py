"""
Registry MCP Server
=====================
Implements the Model Context Protocol (MCP) over stdio (JSON-RPC 2.0).
This is a spec-compliant MCP server - no SDK required, runs as a subprocess
that any MCP-capable client (Claude Desktop, Claude Code, CrewAI MCP adapter)
can connect to via:

    command: python -m app.mcp.server

Exposed tools:
    search_registry(query, limit?)
    get_kpi_context(kpi_id)
    get_fact_context(fact_id)
    search_related_registry_content(query, limit?)
    discover_relevant_facts(document_type)
    retrieve_formula_context(kpi_id)
"""


import sys
import os
import json
import traceback

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))
from app.index.query_service import (
    search_registry,
    get_kpi_context,
    get_fact_context,
    search_related_registry_content,
    discover_relevant_facts,
    retrieve_formula_context,
)

# ------------------------------------------------------------------
# Tool Schemas (MCP tools/list)
# ------------------------------------------------------------------
TOOLS = [
    {
        "name": "search_registry",
        "description": (
            "Full-text search across the entire registry (facts + KPIs). "
            "Use when you know a business term but not the exact entity ID."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "Free-text search query"},
                "limit": {"type": "integer", "description": "Max results (default 10)", "default": 10},
            },
            "required": ["query"],
        },
    },
    {
        "name": "get_kpi_context",
        "description": (
            "Returns complete context for a named KPI: formula, required facts, "
            "derived facts, required document types, thresholds, and data quality config."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "kpi_id": {"type": "string", "description": "KPI identifier, e.g. 'ai_roi'"},
            },
            "required": ["kpi_id"],
        },
    },
    {
        "name": "get_fact_context",
        "description": (
            "Returns complete context for a named fact: definition, data type, "
            "validation rules, normalization rules, aliases, extraction patterns, "
            "source priority, related facts, and which KPIs use it."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "fact_id": {"type": "string", "description": "Fact identifier, e.g. 'direct_ai_revenue'"},
            },
            "required": ["fact_id"],
        },
    },
    {
        "name": "search_related_registry_content",
        "description": (
            "Like search_registry but expands each hit with its direct relationships "
            "(related facts for fact hits; required facts for KPI hits). "
            "Use when you need context around a match, not just the match itself."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "Free-text search query"},
                "limit": {"type": "integer", "description": "Max results (default 10)", "default": 10},
            },
            "required": ["query"],
        },
    },
    {
        "name": "discover_relevant_facts",
        "description": (
            "Returns all facts that can appear in a given document type, "
            "with their aliases and extraction patterns. "
            "Use at the start of fact extraction to scope which facts to look for "
            "in a classified document - never loads the entire fact registry."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "document_type": {
                    "type": "string",
                    "description": "Classified document type",
                    "enum": [
                        "financial", "governance", "project_portfolio", "telemetry",
                        "operations", "inventory", "benchmark", "crm", "hr",
                        "board_deck", "audit",
                    ],
                },
            },
            "required": ["document_type"],
        },
    },
    {
        "name": "retrieve_formula_context",
        "description": (
            "Returns formula + required fact metadata for a KPI, scoped to what "
            "the KPI Calculation Engine needs: formula string, fact IDs, data types. "
            "Does NOT include full fact definitions - use get_fact_context for those."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "kpi_id": {"type": "string", "description": "KPI identifier, e.g. 'ai_roi'"},
            },
            "required": ["kpi_id"],
        },
    },
]


# ------------------------------------------------------------------
# Tool Dispatch
# ------------------------------------------------------------------
def dispatch(name: str, args: dict):
    if name == "search_registry":
        return search_registry(args["query"], args.get("limit", 10))

    if name == "get_kpi_context":
        result = get_kpi_context(args["kpi_id"])
        if result is None:
            raise ValueError(f"KPI not found: {args['kpi_id']}")
        return result

    if name == "get_fact_context":
        result = get_fact_context(args["fact_id"])
        if result is None:
            raise ValueError(f"Fact not found: {args['fact_id']}")
        return result

    if name == "search_related_registry_content":
        return search_related_registry_content(args["query"], args.get("limit", 10))

    if name == "discover_relevant_facts":
        return discover_relevant_facts(args["document_type"])

    if name == "retrieve_formula_context":
        result = retrieve_formula_context(args["kpi_id"])
        if result is None:
            raise ValueError(f"KPI not found: {args['kpi_id']}")
        return result

    raise ValueError(f"Unknown tool: {name}")


# # -- JSON-RPC 2.0 Handlers -------------------------------------------------------

def handle_request(req: dict):
    req_id = req.get("id")
    method = req.get("method", "")
    params = req.get("params", {})

    try:
        if method == "initialize":
            return _ok(req_id, {
                "protocolVersion": "2024-11-05",
                "serverInfo": {"name": "nexus-registry-mcp", "version": "1.0.0"},
                "capabilities": {"tools": {}},
            })

        if method == "tools/list":
            return _ok(req_id, {"tools": TOOLS})

        if method == "tools/call":
            tool_name = params.get("name")
            tool_args = params.get("arguments", {})
            result = dispatch(tool_name, tool_args)
            return _ok(req_id, {
                "content": [{"type": "text", "text": json.dumps(result, indent=2)}],
                "isError": False,
            })

        if method == "notifications/initialized":
            return None  

        return _error(req_id, -32601, f"Method not found: {method}")

    except ValueError as e:
        return _error(req_id, -32602, str(e))
    except Exception as e:
        return _error(req_id, -32603, f"Internal error: {traceback.format_exc()}")


def _ok(req_id, result):
    return {"jsonrpc": "2.0", "id": req_id, "result": result}


def _error(req_id, code, message):
    return {"jsonrpc": "2.0", "id": req_id, "error": {"code": code, "message": message}}


# ------------------------------------------------------------------
# stdio Event Loop
# ------------------------------------------------------------------
def run():
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            req = json.loads(line)
        except json.JSONDecodeError:
            continue

        response = handle_request(req)
        if response is not None:
            sys.stdout.write(json.dumps(response) + "\n")
            sys.stdout.flush()


if __name__ == "__main__":
    run()
