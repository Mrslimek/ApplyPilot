"""SQLite / PostgreSQL compatibility layer.

ApplyPilot's data layer speaks a small SQLite dialect. To run against a
single shared PostgreSQL instance (VPS canonical DB, Mac connected via SSH
tunnel), set APPLYPILOT_DB_URL, e.g.

    APPLYPILOT_DB_URL=postgresql://applypilot:pass@127.0.0.1:5432/applypilot

Unset -> local SQLite file (default, unchanged behaviour).

The PgConnection wrapper translates the SQL subset we use:
  ?                     -> %s                (outside single-quoted strings)
  INSERT OR IGNORE INTO -> INSERT ... ON CONFLICT DO NOTHING
  BEGIN [IMMEDIATE]     -> no-op (autocommit; atomicity via MVCC row locks)
  PRAGMA ...            -> no-op
Rows are returned as sqlite-style Row objects (int and string indexing,
.keys()).
"""

from __future__ import annotations

import os
import re
import sqlite3


def backend() -> tuple[str, str]:
    """Return (kind, target): ('postgres', dsn) or ('sqlite', path)."""
    url = os.environ.get("APPLYPILOT_DB_URL", "")
    if url.startswith(("postgres://", "postgresql://")):
        return "postgres", url
    from applypilot.config import DB_PATH
    return "sqlite", str(DB_PATH)


class Row:
    """sqlite3.Row-alike: tuple indexing, key indexing, .keys()."""

    def __init__(self, keys, values):
        self._keys = list(keys)
        self._values = tuple(values)
        self._map = {k: v for k, v in zip(self._keys, self._values)}

    def keys(self):
        return self._keys

    def __iter__(self):
        return iter(self._values)

    def __len__(self):
        return len(self._values)

    def __getitem__(self, key):
        if isinstance(key, int):
            return self._values[key]
        return self._map[key]

    def __eq__(self, other):
        if isinstance(other, Row):
            return self._values == other._values
        return self._values == other


class PgCursor:
    def __init__(self, cur):
        self._cur = cur
        self._keys = [d[0] for d in cur.description] if cur.description else []

    def _row(self, values):
        return Row(self._keys, values) if self._keys else values

    def fetchone(self):
        r = self._cur.fetchone()
        return self._row(r) if r is not None else None

    def fetchall(self):
        return [self._row(r) for r in self._cur.fetchall()]

    def __iter__(self):
        while True:
            r = self.fetchone()
            if r is None:
                return
            yield r

    @property
    def rowcount(self):
        return self._cur.rowcount

    @property
    def lastrowid(self):
        return self._cur.fetchone()[0] if self._cur.description else None


_INSERT_IGNORE = re.compile(r"^(\s*)INSERT\s+OR\s+IGNORE\s+INTO\s+(.*)$", re.IGNORECASE | re.DOTALL)


class PgConnection:
    """Minimal sqlite3.Connection-alike over psycopg3."""

    def __init__(self, dsn: str):
        import psycopg
        self._conn = psycopg.connect(dsn, autocommit=True)
        self._conn.execute("SET timezone TO 'UTC'")

    # -- SQL translation ---------------------------------------------------
    @staticmethod
    def _rewrite(sql: str) -> str:
        s = sql.strip()
        if not s:
            return s
        upper = s.upper()
        if upper.startswith("PRAGMA") or upper.startswith(("BEGIN", "COMMIT", "ROLLBACK")):
            return ""  # handled separately / autocommit
        m = _INSERT_IGNORE.match(s)
        if m:
            s = f"{m.group(1)}INSERT INTO {m.group(2)} ON CONFLICT DO NOTHING"
        # ? -> %s, but only outside single-quoted literals
        out, in_str = [], False
        for ch in s:
            if ch == "'":
                in_str = not in_str
                out.append(ch)
            elif ch == "?" and not in_str:
                out.append("%s")
            else:
                out.append(ch)
        return "".join(out)

    # -- sqlite-like API ----------------------------------------------------
    def execute(self, sql, params=()):
        rewritten = self._rewrite(sql)
        if not rewritten:
            return PgCursor(_FakeCur())
        cur = self._conn.execute(rewritten, params if params else None)
        return PgCursor(cur)

    def executemany(self, sql, seq):
        rewritten = self._rewrite(sql)
        if rewritten and seq:
            self._conn.cursor().executemany(rewritten, seq)

    def commit(self):
        pass  # autocommit

    def rollback(self):
        pass

    def close(self):
        try:
            self._conn.close()
        except Exception:
            pass

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()


class _FakeCur:
    """Cursor stand-in for no-op statements (PRAGMA/BEGIN)."""

    description = None

    def fetchone(self):
        return None

    def fetchall(self):
        return []

    @property
    def rowcount(self):
        return 0


def connect():
    """Open a connection to the configured backend."""
    kind, target = backend()
    if kind == "postgres":
        return PgConnection(target)
    return sqlite3.connect(target)
