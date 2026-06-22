"""
Project Paths Config
======================
Single source of truth for ALL filesystem paths and environment-variable-backed
configuration used across the backend. Every module must import from here
instead of calling os.getenv() or recomputing paths with Path(__file__).

Layout assumed (this file lives at backend/, one level below the workspace root):

    <workspace>/                        <- WORKSPACE_ROOT
    ├── parsed_data/                    <- PARSED_FILE_PATH
    └── backend/                        <- PROJECT_ROOT (this file's folder)
        ├── config.py
        ├── api/
        ├── db/
        │   ├── nexus.db                fact/KPI value store (sqlite)
        │   ├── registry.db             registry index (sqlite, derived from YAML)
        │   ├── registry_schema.sql     schema for registry.db
        │   └── chroma/                 Chroma vector store
        ├── document_parser/
        ├── kpi_extractor/
        │   └── app/
        │       └── registry/
        │           ├── facts/*.yaml    source of truth for facts
        │           └── kpis/*.yaml     source of truth for KPIs
        └── utilites/

If you ever move any of these folders, this is the only file that needs to change.
"""

from pathlib import Path
import os

PROJECT_ROOT   = Path(__file__).resolve().parent
WORKSPACE_ROOT = PROJECT_ROOT.parent

# ---------------------------------------------------------------------------
# Database paths
# ---------------------------------------------------------------------------
DB_DIR              = PROJECT_ROOT / "db"
DEFAULT_DB_PATH     = DB_DIR / "nexus.db"               # fact/KPI value store
REGISTRY_DB_PATH    = DB_DIR / "registry.db"            # registry index (sqlite)
REGISTRY_SCHEMA_PATH = DB_DIR / "registry_schema.sql"  # schema for registry.db
CHROMA_DIR          = DB_DIR / "chroma"                 # Chroma vector store

DATABASE_URL = os.getenv("DATABASE_URL", f"sqlite:///{DEFAULT_DB_PATH}")

# ---------------------------------------------------------------------------
# KPI extractor / registry
# ---------------------------------------------------------------------------
KPI_EXTRACTOR_DIR = PROJECT_ROOT / "kpi_extractor"
REGISTRY_DIR      = KPI_EXTRACTOR_DIR / "app" / "registry"
FACTS_DIR         = REGISTRY_DIR / "facts"
KPIS_DIR          = REGISTRY_DIR / "kpis"

# ---------------------------------------------------------------------------
# Document parser
# ---------------------------------------------------------------------------
PARSED_FILE_PATH = Path(os.getenv("PARSED_FILE_PATH", str(WORKSPACE_ROOT / "parsed_data")))

SUPPORTED_DOC_EXTENSIONS: list[str] = os.getenv(
    "SUPPORTED_DOC_TYPE_EXTENSIONS",
    ".pdf,.docx,.pptx,.xlsx,.md,.txt,.csv,.html",
).split(",")

# ---------------------------------------------------------------------------
# Chroma / RAG
# ---------------------------------------------------------------------------
CHROMA_PATH       = Path(os.getenv("CHROMA_PATH", str(CHROMA_DIR)))
CHROMA_COLLECTION = os.getenv("CHROMA_COLLECTION", "nexus_document_chunks")

# ---------------------------------------------------------------------------
# Chatbot / LLM defaults  (model names, not secrets — API keys stay in .env)
# ---------------------------------------------------------------------------
DEFAULT_GEMINI_MODEL    = os.getenv("GEMINI_MODEL", "gemini/gemini-2.5-flash")
DEFAULT_RAG_MODEL       = os.getenv("NEXUS_RAG_MODEL", DEFAULT_GEMINI_MODEL)
DEFAULT_ORCHESTRATOR_MODEL = os.getenv("NEXUS_ORCHESTRATOR_MODEL", DEFAULT_GEMINI_MODEL)
DEFAULT_SQL_MODEL       = os.getenv("NEXUS_SQL_MODEL", DEFAULT_GEMINI_MODEL)
