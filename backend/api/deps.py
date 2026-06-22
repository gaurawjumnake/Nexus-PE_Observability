from backend.utilites.llm_models import get_llm_client
from backend.kpi_extractor.app.registry.registry_service import RegistryService


def init_shared_resources(app):
    """Call once during FastAPI lifespan startup."""
    app.state.llm = get_llm_client()
    app.state.registry = RegistryService()              # creates db/registry.db + schema if missing
    if not app.state.registry.is_populated():
        app.state.registry.rebuild_from_yaml()           # first-run population from registry/*.yaml

