#!/usr/bin/env python3
"""Recent applications with outcomes and stored reports.

Usage:
  applied.py list          — last 10 attempts with status
  applied.py show <n>      — full report (agent narrative + perf) for entry n
"""

import os
import sqlite3
import sys
from pathlib import Path

APP_DIR = Path(os.environ.get("APPLYPILOT_DIR", Path.home() / ".applypilot"))
DB = APP_DIR / "applypilot.db"


def main() -> None:
    args = sys.argv[1:]
    if not DB.exists() or not args:
        print("no db / no args")
        return
    conn = sqlite3.connect(f"file:{DB}?mode=ro", uri=True)

    if args[0] == "list":
        rows = conn.execute(
            "SELECT fit_score, title, site, "
            "COALESCE(apply_status, '—'), COALESCE(applied_at, last_attempted_at, ''), "
            "COALESCE(apply_duration_ms, 0) "
            "FROM jobs WHERE applied_at IS NOT NULL OR apply_status IS NOT NULL "
            "ORDER BY COALESCE(applied_at, last_attempted_at) DESC LIMIT 10"
        ).fetchall()
        if not rows:
            print("no applications yet")
            return
        for i, (score, title, site, status, ts, ms) in enumerate(rows, 1):
            mins = f"{ms//60000}m{ms//1000%60:02d}s" if ms else "—"
            print(f"{i}. [{status}] {score} | {title[:44]} | {site[:14]} | {ts[:16]} | {mins}")
        print("\nfull story: /report <n>")

    elif args[0] == "show" and len(args) > 1:
        try:
            n = int(args[1])
        except ValueError:
            print("bad index")
            return
        row = conn.execute(
            "SELECT title, url, COALESCE(apply_report, 'no report stored') "
            "FROM jobs WHERE applied_at IS NOT NULL OR apply_status IS NOT NULL "
            "ORDER BY COALESCE(applied_at, last_attempted_at) DESC LIMIT 1 OFFSET ?",
            (n - 1,),
        ).fetchone()
        if not row:
            print("no such entry")
            return
        title, url, report = row
        print(f"{title}\n{url}\n\n{report[-3000:]}")
    else:
        print("usage: applied.py list | show <n>")


if __name__ == "__main__":
    main()
