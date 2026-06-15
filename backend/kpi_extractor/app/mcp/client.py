"""
Registry MCP Client
=====================
Manages the MCP server subprocess and exposes each registry tool as a
typed Python function. This is the ONLY way agents interact with registry
data - they import and call these functions, never touching YAML or SQLite.

Usage:
    from app.mcp.client import RegistryMCPClient

    with RegistryMCPClient() as registry:
        facts = registry.discover_relevant_facts("financial")
        kpi   = registry.get_kpi_context("ai_roi")
        hits  = registry.search_registry("hallucination detection")
"""

import json
import subprocess
import sys
import threading
import uuid
import os
from typing import Optional

SERVER_MODULE = "app.mcp.server"
BASE_DIR = os.path.join(os.path.dirname(__file__), "..", "..")


class RegistryMCPClient:
    """
    Spawns the MCP server as a subprocess and communicates via
    JSON-RPC 2.0 over stdin/stdout. Thread-safe for concurrent tool calls.
    """

    def __init__(self):
        self._proc: Optional[subprocess.Popen] = None
        self._lock = threading.Lock()

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------
    def start(self):
        self._proc = subprocess.Popen(
            [sys.executable, "-m", SERVER_MODULE],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            cwd=BASE_DIR,
            text=True,
            bufsize=1,
        )
        self._initialize()
        return self

    def stop(self):
        if self._proc:
            self._proc.terminate()
            self._proc.wait()
            self._proc = None

    def __enter__(self):
        return self.start()

    def __exit__(self, *_):
        self.stop()

    # ------------------------------------------------------------------
    # Tool wrappers (public API for agents)
    # ------------------------------------------------------------------
    def search_registry(self, query: str, limit: int = 10) -> list[dict]:
        """
        Full-text search across facts + KPIs.
        Returns: [{"entity_type", "entity_id", "name"}, ...]
        """
        return self._call("search_registry", {"query": query, "limit": limit})

    def get_kpi_context(self, kpi_id: str) -> dict:
        """
        Complete KPI definition: formula, required_facts, required_documents,
        thresholds, data_quality config.
        """
        return self._call("get_kpi_context", {"kpi_id": kpi_id})

    def get_fact_context(self, fact_id: str) -> dict:
        """
        Complete fact definition: data_type, validation_rules, aliases,
        extraction_patterns, source_priority, related_facts, used_by_kpis.
        """
        return self._call("get_fact_context", {"fact_id": fact_id})

    def search_related_registry_content(self, query: str, limit: int = 10) -> list[dict]:
        """
        FTS search + relationship expansion for each hit.
        Returns: [{"entity_type", "entity_id", "name", "related": [...]}, ...]
        """
        return self._call("search_related_registry_content", {"query": query, "limit": limit})

    def discover_relevant_facts(self, document_type: str) -> list[dict]:
        """
        Returns facts scoped to a document type with aliases + extraction
        patterns - the Fact Extraction Agent's entry point.
        """
        return self._call("discover_relevant_facts", {"document_type": document_type})

    def retrieve_formula_context(self, kpi_id: str) -> dict:
        """
        Minimal KPI formula context for the KPI Calculation Engine:
        {"kpi_id", "formula", "unit", "facts": {fact_id: {data_type, unit}}}
        """
        return self._call("retrieve_formula_context", {"kpi_id": kpi_id})

    # ------------------------------------------------------------------
    # JSON-RPC plumbing
    # ------------------------------------------------------------------
    def _initialize(self):
        """Send MCP initialize handshake."""
        self._rpc("initialize", {
            "protocolVersion": "2024-11-05",
            "clientInfo": {"name": "nexus-registry-client", "version": "1.0.0"},
            "capabilities": {},
        })
        # send initialized notification (no response expected)
        notif = json.dumps({
            "jsonrpc": "2.0",
            "method": "notifications/initialized",
            "params": {},
        }) + "\n"
        with self._lock:
            self._proc.stdin.write(notif)
            self._proc.stdin.flush()

    def _call(self, tool_name: str, arguments: dict):
        """Call a tool and return the parsed result."""
        result = self._rpc("tools/call", {"name": tool_name, "arguments": arguments})
        # MCP returns content as [{type:text, text:...}]
        text = result["content"][0]["text"]
        return json.loads(text)

    def _rpc(self, method: str, params: dict):
        """Send a JSON-RPC request and return the result."""
        req_id = str(uuid.uuid4())
        request = json.dumps({
            "jsonrpc": "2.0",
            "id": req_id,
            "method": method,
            "params": params,
        }) + "\n"

        with self._lock:
            self._proc.stdin.write(request)
            self._proc.stdin.flush()
            response_line = self._proc.stdout.readline()

        response = json.loads(response_line)

        if "error" in response:
            raise RuntimeError(f"MCP error [{response['error']['code']}]: {response['error']['message']}")

        return response["result"]
