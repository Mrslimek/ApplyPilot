#!/usr/bin/env python3
"""Manage screening questions collected by the apply agent.

Usage:
  questions.py list              — pending questions with ids
  questions.py answer <id> <text...>  — record the user's answer
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

    conn = sqlite3.connect(f"file:{DB}?mode=ro", uri=True) if args[0] == "list" else sqlite3.connect(DB)

    if args[0] == "list":
        rows = conn.execute(
            "SELECT id, question, COALESCE(options, ''), COALESCE(answer, ''), status "
            "FROM screening_answers ORDER BY status, id"
        ).fetchall()
        if not rows:
            print("no collected questions yet")
            return
        for qid, q, opts, ans, status in rows:
            mark = "✅" if status == "answered" else "❓"
            print(f"{mark} #{qid} {q}")
            if opts:
                print(f"     options: {opts}")
            if ans:
                print(f"     answer: {ans}")
    elif args[0] == "answer" and len(args) >= 3:
        qid = args[1]
        text = " ".join(args[2:])
        from datetime import datetime, timezone
        cur = conn.execute(
            "UPDATE screening_answers SET answer = ?, status = 'answered', answered_at = ? WHERE id = ?",
            (text, datetime.now(timezone.utc).isoformat(), qid),
        )
        conn.commit()
        print(f"saved #{qid}: {text}" if cur.rowcount else f"#{qid} not found")
    else:
        print("usage: questions.py list | answer <id> <text>")


if __name__ == "__main__":
    main()
