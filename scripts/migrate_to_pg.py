#!/usr/bin/env python3
"""One-off migration: SQLite -> PostgreSQL via the dbcompat layer.

Run with APPLYPILOT_DB_URL set (it is read from ~/.applypilot/.env by
load_env). Copies jobs / dismissed_urls / screening_answers verbatim.
"""

import os
import sqlite3
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "src"))

from applypilot.config import load_env  # noqa: E402

load_env()

from applypilot.database import init_db  # noqa: E402
from applypilot.dbcompat import backend  # noqa: E402

kind, _ = backend()
if kind != "postgres":
    print("APPLYPILOT_DB_URL is not set to postgres — aborting")
    sys.exit(1)

SQLITE_PATH = os.path.expanduser("~/.applypilot/applypilot.db")
lite = sqlite3.connect(SQLITE_PATH)
lite.row_factory = sqlite3.Row

pg = init_db()  # creates the schema on the new backend

for table in ("jobs", "dismissed_urls", "screening_answers"):
    cols = [r[1] for r in lite.execute(f"PRAGMA table_info({table})")]
    rows = [dict(r) for r in lite.execute(f"SELECT * FROM {table}")]
    if not rows:
        print(f"{table}: 0 rows")
        continue
    ph = ", ".join("?" * len(cols))
    collist = ", ".join(cols)
    for r in rows:
        pg.execute(
            f"INSERT OR IGNORE INTO {table} ({collist}) VALUES ({ph})",
            tuple(r[c] for c in cols),
        )
    print(f"{table}: {len(rows)} rows migrated")

counts = {t: pg.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0] for t in
          ("jobs", "dismissed_urls", "screening_answers")}
print("PG counts:", counts)
