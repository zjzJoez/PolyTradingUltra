from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from polymarket_mvp.db import connect_db, init_db


class DbConnectionManagementTests(unittest.TestCase):
    def test_connect_db_context_manager_closes_connection(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = Path(tmpdir) / "test.sqlite3"
            init_db(db_path)
            with connect_db(db_path) as conn:
                conn.execute("SELECT 1").fetchone()
            with self.assertRaises(Exception):
                conn.execute("SELECT 1")
