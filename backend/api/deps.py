import os
from functools import lru_cache
from typing import Generator

from fastapi import Depends
from sqlalchemy.orm import Session

from backend.kpi_extractor.app.registry.registry_service import RegistryService
from backend.db.db_client import check_db_connection, ensure_schema, get_session, SessionLocal
from backend.utilites.app_logger import Logger

log = Logger()

_REQUIRED_BY_PROVIDER: dict[str, list[str]] = {
    "gemini": ["GEMINI_API_KEY"],
    "azure_openai": ["AZURE_OPENAI_API_KEY", "AZURE_OPENAI_ENDPOINT", "AZURE_OPENAI_DEPLOYMENT"],
}


# ---------------------------------------------------------------------------
# DB session dependency (for route handlers that need a session directly)
# ---------------------------------------------------------------------------

def get_db() -> Generator[Session, None, None]:
    """FastAPI dependency that yields a SQLAlchemy session per request."""
    with get_session() as db:
        yield db


# ---------------------------------------------------------------------------
# Registry singleton
# ---------------------------------------------------------------------------

@lru_cache(maxsize=1)
def get_registry() -> RegistryService:
    registry = RegistryService()
    if not registry.is_populated():
        log.log_info("Registry empty — rebuilding from YAML...")
        registry.rebuild_from_yaml()
        log.log_info("Registry rebuild complete")
    return registry


# ---------------------------------------------------------------------------
# Health helpers (also used by /health/db route)
# ---------------------------------------------------------------------------

def check_db() -> dict:
    """Ping the database. Returns {"status": "ok"} or {"status": "error", "detail": ...}."""
    try:
        check_db_connection()
        return {"status": "ok"}
    except Exception as e:
        return {"status": "error", "detail": str(e)}


# ---------------------------------------------------------------------------
# Startup verification
# ---------------------------------------------------------------------------

def _check_env_vars() -> None:
    provider = os.getenv("NEXUS_LLM_PROVIDER", "gemini").lower()
    required = _REQUIRED_BY_PROVIDER.get(provider, [])
    missing = [k for k in required if not os.getenv(k)]
    if missing:
        log.log_error(f"Missing env vars for provider '{provider}': {missing}")
        raise RuntimeError(f"Missing required environment variables: {missing}")
    log.log_info(f"Env vars OK: provider={provider}")


def init_shared_resources(app) -> None:
    """
    Run at startup (lifespan). Hard-fails if anything is wrong so the
    process never comes up in a broken state.

    Order:
      1. env vars   — fail if LLM keys are missing
      2. DB ping    — fail if Postgres is unreachable
      3. schema     — ensure nexus_* tables exist (idempotent, fast-path if present)
    """
    log.log_info("Running startup checks...")
    _check_env_vars()

    result = check_db()
    if result["status"] != "ok":
        log.log_error(f"DB unreachable at startup: {result.get('detail')}")
        raise RuntimeError(f"Database unreachable: {result.get('detail')}")
    log.log_info("DB connection OK")

    ensure_schema()
    log.log_info("Schema verified")

    log.log_info("Startup checks passed")

