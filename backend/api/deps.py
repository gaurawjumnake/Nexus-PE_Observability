import os
from functools import lru_cache

from sqlalchemy import text

from backend.kpi_extractor.app.registry.registry_service import RegistryService
from backend.db.db_client import engine
from backend.utilites.app_logger import Logger

log = Logger()

_REQUIRED_BY_PROVIDER: dict[str, list[str]] = {
    "gemini": ["GEMINI_API_KEY"],
    "azure_openai": ["AZURE_OPENAI_API_KEY", "AZURE_OPENAI_ENDPOINT", "AZURE_OPENAI_DEPLOYMENT"],
}


@lru_cache(maxsize=1)
def get_registry() -> RegistryService:
    """Return the singleton RegistryService, building the index on first call."""
    registry = RegistryService()
    if not registry.is_populated():
        log.log_info("Registry empty — rebuilding from YAML...")
        registry.rebuild_from_yaml()
        log.log_info("Registry rebuild complete")
    return registry


def check_db() -> dict:
    """Ping the database and return a status dict. Safe to call at any time."""
    try:
        import backend.db.db_client as db
        db.check_db_connection()
        return {"status": "ok"}
    except Exception as e:
        return {"status": "error", "detail": str(e)}


def _check_env_vars() -> None:
    provider = os.getenv("NEXUS_LLM_PROVIDER", "gemini").lower()
    required = _REQUIRED_BY_PROVIDER.get(provider, [])
    missing = [k for k in required if not os.getenv(k)]
    if missing:
        log.log_error(f"Pre-deployment check FAILED — missing env vars for provider '{provider}': {missing}")
        raise RuntimeError(f"Missing required environment variables: {missing}")
    log.log_info(f"Pre-deployment check OK: provider={provider}, required env vars present")


def init_shared_resources(app) -> None:
    """Validate environment and DB connectivity at startup."""
    log.log_info("Running pre-deployment checks...")
    _check_env_vars()
    result = check_db()
    if result["status"] != "ok":
        log.log_error(f"DB health check failed at startup: {result.get('detail')}")
        raise RuntimeError(f"Database unreachable: {result.get('detail')}")
    log.log_info("Pre-deployment checks passed (env vars OK, DB reachable).")

