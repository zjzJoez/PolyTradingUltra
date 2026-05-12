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

    # CRITICAL: stop ALTER TABLE RENAME from auto-rewriting FK references
    # in dependent tables.
    #
    # Migration v10 used the standard `RENAME → CREATE → INSERT SELECT →
    # DROP` rebuild pattern with `PRAGMA foreign_keys = OFF` at the top of
    # the script. That PRAGMA was silently dropped by executescript (and
    # even when re-asserted via explicit execute() the FK auto-rewrite
    # still happened — see the test cases in tests/test_migration_runner_fk.py).
    #
    # The actual control is `PRAGMA legacy_alter_table`. SQLite docs:
    #   "ALTER TABLE will update the foreign_key_list of any tables
    #    containing FOREIGN KEY constraints that reference the renamed
    #    table. This update of the foreign_key_list occurs regardless
    #    of the foreign_keys PRAGMA setting."
    # legacy_alter_table = ON disables that rewrite — which is what a
    # table-rebuild migration actually wants (the rename target is just a
    # scratch table; dependent FKs should keep pointing at the canonical
    # name that gets recreated). Without this, v10 corrupted 3 tables and
    # v11 corrupted position_events.
    #
    # foreign_keys = OFF is set too so the brief window between DROP-old
    # and CREATE-new doesn't trip runtime FK checks on cascading deletes.
    #
    # isolation_level = None puts the connection in autocommit so the
    # script's own BEGIN/COMMIT pair runs as an explicit transaction
    # instead of being nested inside Python's implicit one.
    saved_isolation = conn.isolation_level
    try:
        conn.commit()
        conn.isolation_level = None
        for path in migration_files():
            if path.name in applied:
                continue
            conn.execute("PRAGMA foreign_keys = OFF")
            conn.execute("PRAGMA legacy_alter_table = ON")
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
        conn.execute("PRAGMA legacy_alter_table = OFF")
        conn.execute("PRAGMA foreign_keys = ON")
    finally:
        conn.isolation_level = saved_isolation
    return newly_applied
