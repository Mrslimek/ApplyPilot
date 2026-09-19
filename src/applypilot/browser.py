"""Shared browser launcher.

Prefers the system-installed Google Chrome (channel="chrome") when
available — Playwright's bundled Chromium is not always downloaded
(small VPSes, CDN timeouts) while Chrome is installed as an ApplyPilot
dependency for the apply stage anyway.
"""

from __future__ import annotations

import shutil


def launch_browser(p, headless: bool = True, **kwargs):
    """Launch a chromium-family browser via a Playwright instance."""
    if shutil.which("google-chrome") or shutil.which("google-chrome-stable"):
        try:
            return p.chromium.launch(headless=headless, channel="chrome", **kwargs)
        except Exception:
            pass
    return p.chromium.launch(headless=headless, **kwargs)
