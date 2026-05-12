"""Regression test for the v10/v11 FK-corruption bug.

When a migration uses the standard SQLite `ALTER TABLE x RENAME TO x_old →
CREATE TABLE x → INSERT...SELECT → DROP TABLE x_old` table-rebuild pattern,
the runner must prevent SQLite from auto-rewriting foreign-key references
in *other* tables to point at the rename target. That auto-rewrite is
controlled by `PRAGMA legacy_alter_table`, not by `PRAGMA foreign_keys`
(SQLite docs: "This update of the foreign_key_list occurs regardless of
the foreign_keys PRAGMA setting"). v10 and v11 both believed they were
disabling the rewrite via `foreign_keys = OFF` and silently corrupted
production schemas.

If this test fails, the runner has regressed and the next DROP/CREATE
table migration will repeat the v10/v11 incident.
"""
from __future__ import annotations

import os
import sqlite3
import tempfile
import unittest
from pathlib import Path

from polymarket_mvp.migrations import apply_pending_migrations


class MigrationRunnerFKPreservationTests(unittest.TestCase):
    def setUp(self):
        self.tmp_dir = tempfile.TemporaryDirectory()
        self.db_path = Path(self.tmp_dir.name) / "test.sqlite3"
        self.mig_dir = Path(self.tmp_dir.name) / "migrations"
        self.mig_dir.mkdir()

    def tearDown(self):
        self.tmp_dir.cleanup()

    def _seed_parent_child(self, conn: sqlite3.Connection) -> None:
        conn.execute("CREATE TABLE parent (id INTEGER PRIMARY KEY, data TEXT)")
        conn.execute(
            "CREATE TABLE child (id INTEGER PRIMARY KEY, parent_id INTEGER, "
            "FOREIGN KEY (parent_id) REFERENCES parent(id) ON DELETE CASCADE)"
        )
        conn.execute("INSERT INTO parent (id, data) VALUES (1, 'foo')")
        conn.execute("INSERT INTO child (id, parent_id) VALUES (1, 1)")
        conn.commit()

    def test_table_rebuild_does_not_corrupt_dependent_fk_refs(self):
        """Apply a migration that rebuilds `parent` via rename → CREATE →
        copy → drop. The `child` table's FK should still reference `parent`
        afterwards (not `parent_old`, which v10/v11 incorrectly produced)."""
        # Build seed DB
        conn = sqlite3.connect(self.db_path)
        self._seed_parent_child(conn)
        conn.close()

        # Write a v10-style rebuild migration into a private migration dir
        migration_text = """
PRAGMA foreign_keys = OFF;
BEGIN TRANSACTION;
ALTER TABLE parent RENAME TO parent_old;
CREATE TABLE parent (id INTEGER PRIMARY KEY, data TEXT, extra TEXT);
INSERT INTO parent (id, data) SELECT id, data FROM parent_old;
DROP TABLE parent_old;
COMMIT;
PRAGMA foreign_keys = ON;
"""
        # Monkeypatch the migrations_dir helper to point at our private dir
        from polymarket_mvp import migrations as mig_mod
        (self.mig_dir / "20990101_rebuild_parent.sql").write_text(migration_text)
        original_dir = mig_mod.migrations_dir
        mig_mod.migrations_dir = lambda: self.mig_dir
        try:
            conn = sqlite3.connect(self.db_path)
            apply_pending_migrations(conn)
            # Child's FK must still point at "parent", not "parent_old"
            fk_refs = [row[2] for row in conn.execute("PRAGMA foreign_key_list(child)")]
            self.assertEqual(fk_refs, ["parent"],
                             f"child FK was corrupted to {fk_refs} — see comment block in "
                             f"apply_pending_migrations for the legacy_alter_table fix")
            # And the data round-tripped correctly
            row = conn.execute("SELECT id, data FROM parent WHERE id = 1").fetchone()
            self.assertEqual(row, (1, "foo"))
            conn.close()
        finally:
            mig_mod.migrations_dir = original_dir


if __name__ == "__main__":
    unittest.main()
