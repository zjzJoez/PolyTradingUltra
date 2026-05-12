from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Iterable

from ..common import utc_now_iso


def migrations_dir() -> Path:
    return Path(__file__).resolve().parent


def migration_files() -> Iterable[Path]:
    return sorted(path for path in migrations_dir().glob("*.sql") if path.is_file())


def ensure_schema_migrations(conn: sqlite3.Connection) -> None:
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS schema_migrations (
          name TEXT PRIMARY KEY,
          applied_at TEXT NOT NULL
        )
        """
    )


def applied_migration_names(conn: sqlite3.Connection) -> set[str]:
    ensure_schema_migrations(conn)
    rows = conn.execute("SELECT name FROM schema_migrations").fetchall()
    return {str(row[0]) for row in rows}


def mark_migration_applied(conn: sqlite3.Connection, name: str) -> None:
    conn.execute(
        "INSERT OR IGNORE INTO schema_migrations (name, applied_at) VALUES (?, ?)",
        (name, utc_now_iso()),
    )


def mark_all_migrations_applied(conn: sqlite3.Connection) -> None:
    ensure_schema_migrations(conn)
    for path in migration_files():
        mark_migration_applied(conn, path.name)


def apply_pending_migrations(conn: sqlite3.Connection) -> list[str]:
    ensure_schema_migrations(conn)
    applied = applied_migration_names(conn)
    newly_applied: list[str] = []

    # CRITICAL: switch to autocommit for the migration run.
    #
    # `PRAGMA foreign_keys = OFF` inside a script is a no-op when SQLite is
    # already in an active transaction — and Python's default sqlite3
    # connection auto-opens a transaction before DML statements, so the
    # PRAGMA at the top of a migration file is silently ignored. That gap
    # made v10's table rebuild proceed with FK enforcement ON, which caused
    # SQLite to rewrite FK references in 3 dependent tables to point to the
    # `_old` rename target. After the rename target was dropped at the end
    # of the migration, those FKs were left dangling. v11 had to repair
    # them. Setting isolation_level=None for the duration of migrations
    # makes the PRAGMA at the top of each script actually take effect.
    saved_isolation = conn.isolation_level
    try:
        conn.commit()  # flush any pending implicit transaction
        conn.isolation_level = None
        for path in migration_files():
            if path.name in applied:
                continue
            try:
                conn.executescript(path.read_text(encoding="utf-8"))
            except sqlite3.OperationalError as exc:
                # ALTER TABLE may fail if table doesn't exist yet (will be created by schema.sql)
                # or if column already exists. Both are safe to skip.
                err_msg = str(exc).lower()
                if "no such table" in err_msg or "duplicate column" in err_msg:
                    import sys
                    print(f"[migrations] {path.name}: skipped ({exc})", file=sys.stderr)
                else:
                    raise
            mark_migration_applied(conn, path.name)
            newly_applied.append(path.name)
    finally:
        conn.isolation_level = saved_isolation
    return newly_applied
