"""
Shared singletons (LLM client + Registry MCP client).

Created once at app startup via FastAPI lifespan, reused across all
requests/background tasks. RegistryMCPClient spawns a single MCP server
subprocess and is internally thread-safe, so it must NOT be re-created
per request or per document.
"""
from backend.utilites.llm_models import get_llm_client
from backend.kpi_extractor.app.mcp.client import RegistryMCPClient


def init_shared_resources(app):
    """Call once during FastAPI lifespan startup."""
    app.state.llm = get_llm_client()
    app.state.registry = RegistryMCPClient().start()


def shutdown_shared_resources(app):
    """Call once during FastAPI lifespan shutdown."""
    registry: RegistryMCPClient = getattr(app.state, "registry", None)
    if registry:
        registry.stop()
