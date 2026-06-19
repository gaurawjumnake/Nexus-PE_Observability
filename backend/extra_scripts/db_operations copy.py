"""
Purpose of this file is db testing and knowing data in db. verificationa and validation
"""

import sys
from sqlalchemy import create_engine, inspect, text, MetaData, Table
from sqlalchemy.orm import sessionmaker


# ── Engine / Session ─────────────────────────────────────────────────────────

def init_db(db_url: str):
    engine = create_engine(db_url, echo=False)
    Session = sessionmaker(bind=engine)
    return engine, Session()


# ── Introspection ─────────────────────────────────────────────────────────────

def get_tables(engine) -> list[str]:
    """Return all table names in the database."""
    return inspect(engine).get_table_names()


def get_table_summary(engine, table_name: str) -> dict:
    """Return column info and row count for a table."""
    inspector = inspect(engine)
    columns = inspector.get_columns(table_name)
    with engine.connect() as conn:
        count = conn.execute(text(f'SELECT COUNT(*) FROM "{table_name}"')).scalar()
    return {
        "columns": [{"name": c["name"], "type": str(c["type"])} for c in columns],
        "row_count": count,
    }


# ── Read / Print ──────────────────────────────────────────────────────────────

def get_records(engine, table_name: str, limit: int | None = 20) -> list[dict]:
    """Fetch rows from a table as list of dicts."""
    meta = MetaData()
    table = Table(table_name, meta, autoload_with=engine)
    with engine.connect() as conn:
        query = table.select()
        if limit:
            query = query.limit(limit)
        rows = conn.execute(query).fetchall()
        cols = [c.name for c in table.columns]
    return [dict(zip(cols, row)) for row in rows]


def print_table(rows: list[dict], title: str = ""):
    """Pretty-print a list of dicts as an aligned table."""
    if not rows:
        print("  (no records)\n")
        return

    if title:
        print(f"\n{title}")

    keys = list(rows[0].keys())
    widths = {k: max(len(k), max(len(str(r.get(k, ""))) for r in rows)) for k in keys}

    sep = "+-" + "-+-".join("-" * widths[k] for k in keys) + "-+"
    header = "| " + " | ".join(k.ljust(widths[k]) for k in keys) + " |"

    print(sep)
    print(header)
    print(sep)
    for row in rows:
        line = "| " + " | ".join(str(row.get(k, "")).ljust(widths[k]) for k in keys) + " |"
        print(line)
    print(sep)
    print(f"  {len(rows)} row(s)\n")


# ── Search ────────────────────────────────────────────────────────────────────

def search_records(engine, table_name: str, column: str, value: str, exact: bool = False) -> list[dict]:
    """Search rows where column matches value (exact or LIKE)."""
    meta = MetaData()
    table = Table(table_name, meta, autoload_with=engine)

    if column not in [c.name for c in table.columns]:
        print(f"  ✗ Column '{column}' not found in '{table_name}'")
        return []

    col = table.c[column]
    condition = (col == value) if exact else col.like(f"%{value}%")

    with engine.connect() as conn:
        rows = conn.execute(table.select().where(condition)).fetchall()
        cols = [c.name for c in table.columns]

    return [dict(zip(cols, row)) for row in rows]


# ── Interactive Menu ──────────────────────────────────────────────────────────

def cmd_list_tables(engine):
    tables = get_tables(engine)
    if not tables:
        print("  No tables found.\n")
        return

    print(f"\n{'='*60}")
    print(f"  {'TABLE':<30} {'ROWS':>8}  COLUMNS")
    print(f"{'='*60}")
    for t in tables:
        info = get_table_summary(engine, t)
        col_names = ", ".join(c["name"] for c in info["columns"])
        print(f"  {t:<30} {info['row_count']:>8}  {col_names}")
    print(f"{'='*60}\n")


def cmd_print_records(engine):
    tables = get_tables(engine)
    print(f"\n  Available tables: {', '.join(tables)}")
    table_name = input("  Table name: ").strip()
    if table_name not in tables:
        print(f"  ✗ Table '{table_name}' not found.\n")
        return

    raw = input("  Limit (leave blank for 20): ").strip()
    limit = int(raw) if raw.isdigit() else 20

    rows = get_records(engine, table_name, limit=limit)
    print_table(rows, title=f"  [{table_name}] — first {limit} rows")


def cmd_search(engine):
    tables = get_tables(engine)
    print(f"\n  Available tables: {', '.join(tables)}")
    table_name = input("  Table name: ").strip()
    if table_name not in tables:
        print(f"  ✗ Table '{table_name}' not found.\n")
        return

    info = get_table_summary(engine, table_name)
    cols = [c["name"] for c in info["columns"]]
    print(f"  Columns: {', '.join(cols)}")

    column = input("  Column to search: ").strip()
    value  = input("  Value to search for: ").strip()
    mode   = input("  Exact match? (y/N): ").strip().lower()
    exact  = mode == "y"

    rows = search_records(engine, table_name, column, value, exact=exact)
    label = f"  [{table_name}] where {column} {'=' if exact else 'LIKE'} '{value}'"
    print_table(rows, title=label)


def main():
    db_url = "sqlite:///backend/kpi_extractor/app/db/nexus.db"
    print(f"\n  Connecting to: {db_url}")
    try:
        engine, _ = init_db(db_url)
        # Quick connectivity check
        get_tables(engine)
        print("  ✓ Connected\n")
    except Exception as e:
        print(f"  ✗ Could not connect: {e}")
        sys.exit(1)

    menu = {
        "1": ("List all tables + summary", cmd_list_tables),
        "2": ("Print records from a table", cmd_print_records),
        "3": ("Search records by column value", cmd_search),
        "4": ("Exit", None),
    }

    while True:
        print("─" * 40)
        for key, (label, _) in menu.items():
            print(f"  {key}. {label}")
        print("─" * 40)
        choice = input("  Choose: ").strip()

        if choice == "4":
            print("  Bye!\n")
            break
        elif choice in menu:
            _, fn = menu[choice]
            fn(engine)
        else:
            print("  Invalid choice.\n")


if __name__ == "__main__":
    main()