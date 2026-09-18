#!/usr/bin/env python3
"""Local-only web UI for ApplyPilot data.

Serves on 127.0.0.1 only — reachable exclusively via SSH port forwarding:

    ssh -N -L 8730:127.0.0.1:8730 vps1euro-applypilot
    open http://127.0.0.1:8730

  /  and /dashboard.html  -> regenerated dashboard (fresh DB state)
  anything else           -> static file from ~/.applypilot
                             (e.g. /tailored_resumes/..., /cover_letters/...)
"""

from __future__ import annotations

import json
import os
import sqlite3
import sys
from functools import partial
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))

from applypilot.config import APP_DIR, DB_PATH  # noqa: E402
from applypilot.view import generate_dashboard  # noqa: E402

PORT = 8730


def api_delete(url: str) -> dict:
    """Remove a job (and its material files) and tombstone the URL."""
    if not url or not isinstance(url, str):
        return {"ok": False, "error": "url required"}
    conn = sqlite3.connect(DB_PATH, timeout=15)
    try:
        row = conn.execute(
            "SELECT tailored_resume_path, cover_letter_path FROM jobs WHERE url = ?", (url,)
        ).fetchone()
        removed_files = []
        if row:
            for path in row:
                if path:
                    p = Path(path)
                    if APP_DIR in p.resolve().parents and p.exists():
                        p.unlink()
                        removed_files.append(p.name)
        from datetime import datetime, timezone
        conn.execute(
            "INSERT OR IGNORE INTO dismissed_urls (url, fit_score, dismissed_at) VALUES (?, NULL, ?)",
            (url, datetime.now(timezone.utc).isoformat()),
        )
        cur = conn.execute("DELETE FROM jobs WHERE url = ?", (url,))
        conn.commit()
        return {"ok": True, "deleted": cur.rowcount, "files_removed": len(removed_files)}
    finally:
        conn.close()


class Handler(SimpleHTTPRequestHandler):
    def do_GET(self) -> None:  # noqa: N802
        path = self.path.split("?")[0].rstrip("/")
        if path in ("", "/dashboard", "/dashboard.html"):
            try:
                generate_dashboard()  # regenerate fresh from the DB
            except Exception as e:
                self.send_error(500, f"dashboard generation failed: {e}")
                return
            self.path = "/dashboard.html"
        return super().do_GET()

    def do_POST(self) -> None:  # noqa: N802
        if self.path.split("?")[0] != "/api/delete":
            self.send_error(404)
            return
        try:
            length = int(self.headers.get("Content-Length", 0))
            payload = json.loads(self.rfile.read(length) or b"{}")
            result = api_delete(payload.get("url", ""))
            body = json.dumps(result).encode()
            self.send_response(200 if result.get("ok") else 400)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        except Exception as e:
            self.send_error(500, str(e))

    def log_message(self, fmt: str, *args) -> None:
        print(f"[web] {self.address_string()} {fmt % args}", flush=True)


def main() -> None:
    os.chdir(APP_DIR)
    server = ThreadingHTTPServer(("127.0.0.1", PORT),
                                 partial(Handler, directory=str(APP_DIR)))
    print(f"applypilot web on http://127.0.0.1:{PORT} (localhost only)", flush=True)
    server.serve_forever()


if __name__ == "__main__":
    main()
