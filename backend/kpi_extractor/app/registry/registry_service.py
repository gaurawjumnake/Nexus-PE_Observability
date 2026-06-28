"""
Registry Service
==================
Single in-process module for all registry reads + writes (facts & KPIs).

Source of truth: registry/facts/*.yaml and registry/kpis/*.yaml.
PostgreSQL (Supabase) is a derived, queryable index of those YAML files.
add_fact/add_kpi/remove_fact/remove_kpi keep both in sync so a future
`python -m app.registry.service --rebuild` never silently undoes an
admin-panel change.
"""

from backend.db.db_client import RegistryService, DependencyError

if __name__ == "__main__":
    RegistryService().rebuild_from_yaml()
    print("Registry rebuilt from YAML.")
