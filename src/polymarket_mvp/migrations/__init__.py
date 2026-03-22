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
    for path in migration_files():
        if path.name in applied:
            continue
        conn.executescript(path.read_text(encoding="utf-8"))
        mark_migration_applied(conn, path.name)
        newly_applied.append(path.name)
    return newly_applied
