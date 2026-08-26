#!/usr/bin/env python3
"""One-shot legacy SQLite -> PostgreSQL data migration.

Usage:
  python scripts/migrate_sqlite_to_postgres.py \
      --sqlite ./data/pcg.db \
      --database-url 'postgresql://user:pass@host/db?sslmode=require'

The target schema is created first. Data is copied in schema order inside one
transaction with deferrable PostgreSQL foreign keys. Existing target rows are
left intact on primary/unique conflicts (notably seeded platform rules).
"""
from __future__ import annotations

import argparse
import os
import re
import sqlite3
import sys
from pathlib import Path


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser()
    p.add_argument("--sqlite", required=True, help="Path to the legacy SQLite .db file")
    p.add_argument("--database-url", required=True, help="Target PostgreSQL DATABASE_URL")
    return p.parse_args()


def main() -> int:
    args = parse_args()
    source_path = Path(args.sqlite).expanduser().resolve()
    if not source_path.is_file():
        raise SystemExit(f"SQLite database not found: {source_path}")
    if not args.database_url.startswith(("postgresql://", "postgres://")):
        raise SystemExit("--database-url must be a PostgreSQL URL")

    os.environ["DATABASE_URL"] = args.database_url
    os.environ.pop("DATABASE_PATH", None)

    # Import only after DATABASE_URL is fixed.
    from app.core.db import _POSTGRES_SCHEMA, connect, init_db

    init_db()

    source = sqlite3.connect(str(source_path))
    source.row_factory = sqlite3.Row
    available = {
        r["name"]
        for r in source.execute("SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'")
    }
    schema_text = _POSTGRES_SCHEMA.read_text(encoding="utf-8")
    ordered_tables = re.findall(r"CREATE TABLE IF NOT EXISTS\s+(\w+)\s*\(", schema_text, re.IGNORECASE)

    copied: dict[str, int] = {}
    skipped_columns: dict[str, list[str]] = {}
    with connect() as target:
        target.execute("SET CONSTRAINTS ALL DEFERRED")
        for table in ordered_tables:
            if table not in available:
                continue
            src_cols = [r[1] for r in source.execute(f'PRAGMA table_info("{table}")')]
            target_cols_rows = target.execute(
                """SELECT column_name FROM information_schema.columns
                   WHERE table_schema='public' AND table_name=? ORDER BY ordinal_position""",
                (table,),
            ).fetchall()
            target_cols = {r["column_name"] for r in target_cols_rows}
            cols = [c for c in src_cols if c in target_cols]
            extra = [c for c in src_cols if c not in target_cols]
            if extra:
                skipped_columns[table] = extra
            if not cols:
                continue

            quoted_cols = ",".join(f'"{c}"' for c in cols)
            placeholders = ",".join("?" for _ in cols)
            insert_sql = (
                f'INSERT INTO "{table}" ({quoted_cols}) VALUES ({placeholders}) '
                "ON CONFLICT DO NOTHING"
            )
            n = 0
            for row in source.execute(f'SELECT {quoted_cols} FROM "{table}"'):
                target.execute(insert_sql, tuple(row[c] for c in cols))
                n += 1
            copied[table] = n
        target.commit()

    source.close()
    total = sum(copied.values())
    print(f"Migration complete: {total} SQLite rows scanned/copied into PostgreSQL.")
    for table, n in copied.items():
        print(f"  {table}: {n}")
    if skipped_columns:
        print("Columns skipped because they do not exist in the target schema:")
        for table, cols in skipped_columns.items():
            print(f"  {table}: {', '.join(cols)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
