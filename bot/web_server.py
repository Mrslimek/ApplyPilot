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

import os
import sys
import threading
from functools import partial
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))

from applypilot.config import APP_DIR  # noqa: E402
from applypilot.view import generate_dashboard  # noqa: E402

PORT = 8730


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
