#!/usr/bin/env python3
"""Print jobs ready for auto-apply (one per line). Called by the Go bot."""

import os
import sqlite3
import sys
from pathlib import Path

APP_DIR = Path(os.environ.get("APPLYPILOT_DIR", Path.home() / ".applypilot"))
DB = APP_DIR / "applypilot.db"
SITES_YAML = Path(__file__).resolve().parent.parent / "src" / "applypilot" / "config" / "sites.yaml"


def blocked_sites() -> set[str]:
    try:
        import yaml
        cfg = yaml.safe_load(SITES_YAML.read_text(encoding="utf-8")) or {}
        return {s.lower() for s in (cfg.get("blocked", {}) or {}).get("sites", [])}
    except Exception:
        return set()


def main() -> None:
    if not DB.exists():
        return
    conn = sqlite3.connect(f"file:{DB}?mode=ro", uri=True)
    rows = conn.execute(
        """
        SELECT fit_score, title, site FROM jobs
        WHERE tailored_resume_path IS NOT NULL AND application_url IS NOT NULL
          AND applied_at IS NULL AND (apply_status IS NULL OR apply_status='failed')
          AND (apply_attempts IS NULL OR apply_attempts < 3) AND fit_score >= 5
        ORDER BY fit_score DESC LIMIT 15
        """
    ).fetchall()
    conn.close()
    blocked = blocked_sites()
    shown = 0
    hidden = 0
    for score, title, site in rows:
        if site.lower() in blocked:
            hidden += 1
            continue
        print(f"{score} | {title[:45]} | {site}")
        shown += 1
    if hidden:
        print(f"({hidden} blocked-site jobs hidden)", file=sys.stderr)
    if not shown:
        print("queue is empty — run /run to refresh the pool")


if __name__ == "__main__":
    main()
