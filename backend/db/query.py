from __future__ import annotations

from typing import Any, Optional

from sqlalchemy import text


def run_query(sql: str, params: Optional[dict] = None) -> list[dict]:
    """
    Execute a raw SQL string against the Postgres database.

    Args:
        sql:    Parameterised SQL using :name placeholders, e.g.
                  "SELECT * FROM nexus_facts WHERE company_id = :cid"
        params: Dict of values, e.g. {"cid": "acme"}

    Returns:
        List of row dicts.  Empty list when the query returns no rows.

    Raises:
        sqlalchemy.exc.SQLAlchemyError on DB errors (caller handles).
    """
    from backend.db.db_client import get_session
    with get_session() as db:
        result = db.execute(text(sql), params or {})
        try:
            rows = result.fetchall()
            return [dict(r._mapping) for r in rows]
        except Exception:
            # DDL / DML that returns no rows
            return []


def query_table(
    table: str,
    where_col: Optional[str] = None,
    where_val: Optional[Any] = None,
    limit: Optional[int] = None,
    exact: bool = True,
) -> list[dict]:
    """
    Simple parameterised table scan — no raw SQL injection risk.

    Args:
        table:     Table name (e.g. "nexus_documents").
        where_col: Column to filter on, or None for full scan.
        where_val: Value to match.  Uses = when exact=True, ILIKE %val% otherwise.
        limit:     Max rows to return.
        exact:     True → equality match; False → case-insensitive substring.

    Returns:
        List of row dicts.
    """
    parts: list[str] = [f'SELECT * FROM "{table}"']
    params: dict = {}

    if where_col is not None and where_val is not None:
        if exact:
            parts.append(f'WHERE "{where_col}" = :val')
            params["val"] = where_val
        else:
            parts.append(f'WHERE LOWER("{where_col}"::text) LIKE :val')
            params["val"] = f"%{str(where_val).lower()}%"

    if limit is not None:
        parts.append("LIMIT :limit")
        params["limit"] = limit

    return run_query(" ".join(parts), params)
