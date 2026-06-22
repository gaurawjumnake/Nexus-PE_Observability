"""
Purpose of this file is db testing and knowing data in db. verificationa and validation
"""

import sys
from sqlalchemy import create_engine, inspect, text, MetaData, Table
from sqlalchemy.orm import sessionmaker


class DBOperations:
    def __init__(self, db_url:str):
        self.db_url = db_url
        try:
            self.engine = create_engine(db_url, echo=False)
            Session = sessionmaker(bind=self.engine)
            self.session =  Session()
        except Exception as e:
            print(f"  ✗ Could not connect: {e}")
            sys.exit(1)

    # # Introspection ---------------------------------------------------------

    def get_tables(self, ) -> list[str]:
        """Return all table names in the database."""
        return inspect(self.engine).get_table_names()


    def get_table_summary(self, table_name: str) -> dict:
        """Return column info and row count for a table."""
        inspector = inspect(self.engine)
        columns = inspector.get_columns(table_name)
        with self.engine.connect() as conn:
            count = conn.execute(text(f'SELECT COUNT(*) FROM "{table_name}"')).scalar()
        return {
            "columns": [{"name": c["name"], "type": str(c["type"])} for c in columns],
            "row_count": count,
        }


    # # Read / Print ---------------------------------------------------------

    def get_records(self, engine, table_name: str, limit: int | None = 20) -> list[dict]:
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


    def print_table(self, rows: list[dict], title: str = ""):
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


    # # Search ---------------------------------------------------------

    def search_records(self, table_name: str, column: str, value: str, exact: bool = False) -> list[dict]:
        """Search rows where column matches value (exact or LIKE)."""
        meta = MetaData()
        table = Table(table_name, meta, autoload_with=self.engine)

        if column not in [c.name for c in table.columns]:
            print(f"  ✗ Column '{column}' not found in '{table_name}'")
            return []

        col = table.c[column]
        condition = (col == value) if exact else col.like(f"%{value}%")

        with self.engine.connect() as conn:
            rows = conn.execute(table.select().where(condition)).fetchall()
            cols = [c.name for c in table.columns]

        return [dict(zip(cols, row)) for row in rows]


    # # Delete ---------------------------------------------------------

    def delete_records(self, table_name: str, column: str, value: str, exact: bool = True) -> int:
        """
        Delete rows where column matches value (exact or LIKE).
        Returns the number of rows deleted. Use exact=True by default
        to avoid accidentally deleting more rows than intended via LIKE matches.
        """
        meta = MetaData()
        table = Table(table_name, meta, autoload_with=self.engine)

        if column not in [c.name for c in table.columns]:
            print(f"  ✗ Column '{column}' not found in '{table_name}'")
            return 0

        col = table.c[column]
        condition = (col == value) if exact else col.like(f"%{value}%")

        with self.engine.begin() as conn:
            result = conn.execute(table.delete().where(condition))
            deleted = result.rowcount

        print(f"  ✓ Deleted {deleted} row(s) from '{table_name}'")
        return deleted


    def delete_all_records(self, table_name: str) -> int:
        """Delete ALL rows from a table (keeps the table/schema intact)."""
        meta = MetaData()
        table = Table(table_name, meta, autoload_with=self.engine)

        with self.engine.begin() as conn:
            result = conn.execute(table.delete())
            deleted = result.rowcount

        print(f"  ✓ Deleted all {deleted} row(s) from '{table_name}'")
        return deleted


    def drop_table(self, table_name: str) -> bool:
        """Drop an entire table from the database."""
        meta = MetaData()
        try:
            table = Table(table_name, meta, autoload_with=self.engine)
            table.drop(self.engine)
            print(f"  ✓ Dropped table '{table_name}'")
            return True
        except Exception as e:
            print(f"  ✗ Could not drop table '{table_name}': {e}")
            return False


    def drop_column(self, table_name: str, column_name: str) -> bool:
        """
        Drop a column from a table.
        Note: SQLite has limited native ALTER TABLE support. Modern SQLite
        (3.35.0+) supports DROP COLUMN directly; this method uses that path
        and falls back to a rebuild-the-table approach for older engines
        or other backends where DROP COLUMN isn't supported.
        """
        inspector = inspect(self.engine)
        columns = [c["name"] for c in inspector.get_columns(table_name)]

        if column_name not in columns:
            print(f"  ✗ Column '{column_name}' not found in '{table_name}'")
            return False

        try:
            with self.engine.begin() as conn:
                conn.execute(text(f'ALTER TABLE "{table_name}" DROP COLUMN "{column_name}"'))
            print(f"  ✓ Dropped column '{column_name}' from '{table_name}'")
            return True
        except Exception as e:
            print(f"  ! Direct DROP COLUMN failed ({e}); attempting table rebuild fallback...")
            return self._drop_column_rebuild(table_name, column_name)


    def _drop_column_rebuild(self, table_name: str, column_name: str) -> bool:
        """
        Fallback for dropping a column by rebuilding the table without it.
        Steps: create new table without the column -> copy data -> drop old
        table -> rename new table to original name.
        """
        meta = MetaData()
        try:
            old_table = Table(table_name, meta, autoload_with=self.engine)
        except Exception as e:
            print(f"  ✗ Could not load table '{table_name}': {e}")
            return False

        remaining_cols = [c for c in old_table.columns if c.name != column_name]
        if len(remaining_cols) == len(old_table.columns):
            print(f"  ✗ Column '{column_name}' not found in '{table_name}'")
            return False

        col_names = [c.name for c in remaining_cols]
        tmp_table_name = f"{table_name}__tmp_rebuild"

        try:
            with self.engine.begin() as conn:
                # Build column defs for the new table
                col_defs = ", ".join(f'"{c.name}" {c.type}' for c in remaining_cols)
                conn.execute(text(f'CREATE TABLE "{tmp_table_name}" ({col_defs})'))

                cols_csv = ", ".join(f'"{c}"' for c in col_names)
                conn.execute(text(
                    f'INSERT INTO "{tmp_table_name}" ({cols_csv}) '
                    f'SELECT {cols_csv} FROM "{table_name}"'
                ))

                conn.execute(text(f'DROP TABLE "{table_name}"'))
                conn.execute(text(f'ALTER TABLE "{tmp_table_name}" RENAME TO "{table_name}"'))

            print(f"  ✓ Dropped column '{column_name}' from '{table_name}' (via rebuild)")
            return True
        except Exception as e:
            print(f"  ✗ Rebuild fallback failed: {e}")
            return False


    # -- Interactive Menu --------------------------------------------

    def cmd_list_tables(self, ):
        tables = self.get_tables()
        if not tables:
            print("  No tables found.\n")
            return

        print(f"\n{'='*60}")
        print(f"  {'TABLE':<30} {'ROWS':>8}  COLUMNS")
        print(f"{'='*60}")
        for t in tables:
            info = self.get_table_summary(t)
            col_names = ", ".join(c["name"] for c in info["columns"])
            print(f"  {t:<30} {info['row_count']:>8}  {col_names}")
        print(f"{'='*60}\n")


    def cmd_print_records(self, ):
        tables = self.get_tables()
        print(f"\n  Available tables: {', '.join(tables)}")
        table_name = input("  Table name: ").strip()
        if table_name not in tables:
            print(f"  ✗ Table '{table_name}' not found.\n")
            return

        raw = input("  Limit (leave blank for 20): ").strip()
        limit = int(raw) if raw.isdigit() else 20

        rows = self.get_records(self.engine, table_name, limit=limit)
        self.print_table(rows, title=f"  [{table_name}] — first {limit} rows")


    def cmd_search(self, ):
        tables = self.get_tables()
        print(f"\n  Available tables: {', '.join(tables)}")
        table_name = input("  Table name: ").strip()
        if table_name not in tables:
            print(f"  ✗ Table '{table_name}' not found.\n")
            return

        info = self.get_table_summary(table_name)
        cols = [c["name"] for c in info["columns"]]
        print(f"  Columns: {', '.join(cols)}")

        column = input("  Column to search: ").strip()
        value  = input("  Value to search for: ").strip()
        mode   = input("  Exact match? (y/N): ").strip().lower()
        exact  = mode == "y"

        rows = self.search_records(table_name, column, value, exact=exact)
        label = f"  [{table_name}] where {column} {'=' if exact else 'LIKE'} '{value}'"
        self.print_table(rows, title=label)


    def cmd_delete_records(self, ):
        tables = self.get_tables()
        print(f"\n  Available tables: {', '.join(tables)}")
        table_name = input("  Table name: ").strip()
        if table_name not in tables:
            print(f"  ✗ Table '{table_name}' not found.\n")
            return

        info = self.get_table_summary(table_name)
        cols = [c["name"] for c in info["columns"]]
        print(f"  Columns: {', '.join(cols)}")

        column = input("  Column to filter on: ").strip()
        value  = input("  Value to match: ").strip()
        mode   = input("  Exact match? (Y/n): ").strip().lower()
        exact  = mode != "n"

        preview = self.search_records(table_name, column, value, exact=exact)
        if not preview:
            print("  (no matching records — nothing to delete)\n")
            return

        self.print_table(preview, title=f"  About to delete {len(preview)} row(s) from [{table_name}]")
        confirm = input(f"  Type 'DELETE' to confirm deletion of {len(preview)} row(s): ").strip()
        if confirm == "DELETE":
            self.delete_records(table_name, column, value, exact=exact)
        else:
            print("  Cancelled.\n")


    def cmd_delete_all_records(self, ):
        tables = self.get_tables()
        print(f"\n  Available tables: {', '.join(tables)}")
        table_name = input("  Table name: ").strip()
        if table_name not in tables:
            print(f"  ✗ Table '{table_name}' not found.\n")
            return

        info = self.get_table_summary(table_name)
        confirm = input(
            f"  Type 'DELETE ALL' to confirm wiping all {info['row_count']} row(s) "
            f"from '{table_name}': "
        ).strip()
        if confirm == "DELETE ALL":
            self.delete_all_records(table_name)
        else:
            print("  Cancelled.\n")


    def cmd_drop_table(self, ):
        tables = self.get_tables()
        print(f"\n  Available tables: {', '.join(tables)}")
        table_name = input("  Table name to DROP: ").strip()
        if table_name not in tables:
            print(f"  ✗ Table '{table_name}' not found.\n")
            return

        confirm = input(f"  Type 'DROP {table_name}' to confirm dropping this table entirely: ").strip()
        if confirm == f"DROP {table_name}":
            self.drop_table(table_name)
        else:
            print("  Cancelled.\n")


    def cmd_drop_column(self, ):
        tables = self.get_tables()
        print(f"\n  Available tables: {', '.join(tables)}")
        table_name = input("  Table name: ").strip()
        if table_name not in tables:
            print(f"  ✗ Table '{table_name}' not found.\n")
            return

        info = self.get_table_summary(table_name)
        cols = [c["name"] for c in info["columns"]]
        print(f"  Columns: {', '.join(cols)}")
        column_name = input("  Column name to DROP: ").strip()
        if column_name not in cols:
            print(f"  ✗ Column '{column_name}' not found.\n")
            return

        confirm = input(f"  Type 'DROP {column_name}' to confirm dropping this column: ").strip()
        if confirm == f"DROP {column_name}":
            self.drop_column(table_name, column_name)
        else:
            print("  Cancelled.\n")


from backend.config import DEFAULT_DB_PATH, DATABASE_URL

def main():
    dbops = DBOperations(DATABASE_URL)

    try:
        dbops.get_tables()
        print("  ✓ Connected\n")
    except Exception as e:
        print(f"  ✗ Could not connect: {e}")
        sys.exit(1)

    menu = {
        "1": ("List all tables + summary", dbops.cmd_list_tables),
        "2": ("Print records from a table", dbops.cmd_print_records),
        "3": ("Search records by column value", dbops.cmd_search),
        "4": ("Delete records by column value", dbops.cmd_delete_records),
        "5": ("Delete ALL records in a table", dbops.cmd_delete_all_records),
        "6": ("Drop a table entirely", dbops.cmd_drop_table),
        "7": ("Drop a column from a table", dbops.cmd_drop_column),
        "8": ("Exit", None),
    }

    while True:
        print("─" * 40)
        for key, (label, _) in menu.items():
            print(f"  {key}. {label}")
        print("─" * 40)
        choice = input("  Choose: ").strip()

        if choice == "8":
            print("  Bye!\n")
            break
        elif choice in menu:
            _, fn = menu[choice]
            fn()
        else:
            print("  Invalid choice.\n")


if __name__ == "__main__":
    main()