"""Cross-match: link LinkedIn jobs to their ATS counterparts.

LinkedIn hides apply URLs from anonymous visitors, but the same job is
usually published by the same company on its own ATS board (Workday,
Greenhouse, Lever, ...), which we harvest with direct apply URLs.

Two phases:
  1. Company enrichment — LinkedIn rows carry no company name in the DB.
     The guest page title is "Company hiring Title in Location", so we
     visit eligible jobs once (headless browser) and store the company.
  2. Matching — for each LinkedIn job (score >= min_score, no apply URL)
     find an ATS job with the same normalized company and a similar
     title, then copy its application_url. The job becomes auto-appliable.

Run via CLI: `applypilot crossmatch` (also wired into the nightly cycle
and the Telegram bot as /crossmatch).
"""

from __future__ import annotations

import logging
import re
import sqlite3
import time
from difflib import SequenceMatcher

from applypilot.config import DEFAULTS
from applypilot.database import get_connection, ensure_columns

log = logging.getLogger(__name__)

UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"

LEGAL_SUFFIXES = (
    " inc", " inc.", " llc", " ltd", " ltd.", " limited", " gmbh", " ag",
    " sa", " s.a.", " plc", " corp", " corp.", " corporation", " co",
    " ab", " as", " oy", " oyj", " a/s", " aps", " bv", " nv", " srl",
    " spa", " sas", " pty", " pte",
    " technologies", " technology", " labs", " systems", " solutions",
    " software", " group", " holdings", " ventures",
)


def normalize_company(name: str | None) -> str:
    if not name:
        return ""
    s = name.lower().strip().strip(".,")
    changed = True
    while changed:
        changed = False
        for suf in LEGAL_SUFFIXES:
            if s.endswith(suf):
                s = s[: -len(suf)].strip().strip(".,")
                changed = True
    return re.sub(r"[^a-z0-9]+", "", s)


def normalize_title(title: str | None) -> str:
    if not title:
        return ""
    t = title.lower()
    t = re.sub(r"[^a-z0-9+# ]+", " ", t)
    stop = {"the", "a", "an", "at", "in", "for", "with", "and", "of", "to"}
    return " ".join(w for w in t.split() if w not in stop)


def title_similar(a: str, b: str, threshold: float = 0.62) -> bool:
    na, nb = normalize_title(a), normalize_title(b)
    if not na or not nb:
        return False
    # quick containment check catches "Senior " prefixes etc.
    if na in nb or nb in na:
        return True
    return SequenceMatcher(None, na, nb).ratio() >= threshold


# ── phase 1: company enrichment ───────────────────────────────────────────

def _fetch_companies(conn: sqlite3.Connection, batch_limit: int) -> int:
    """Visit LinkedIn guest pages and fill the company column."""
    rows = conn.execute(
        """
        SELECT url, title FROM jobs
        WHERE site = 'linkedin' AND company IS NULL
          AND fit_score >= ? AND application_url IS NULL
          AND applied_at IS NULL
        ORDER BY fit_score DESC LIMIT ?
        """,
        (DEFAULTS["min_score"], batch_limit),
    ).fetchall()
    if not rows:
        return 0

    from playwright.sync_api import sync_playwright

    from applypilot.browser import launch_browser

    updated = 0
    with sync_playwright() as p:
        browser = launch_browser(p, headless=True)
        ctx = browser.new_context(user_agent=UA)
        page = ctx.new_page()
        for url, _ in rows:
            company = ""
            title = ""
            try:
                page.goto(url, timeout=45000)
                page.wait_for_load_state("domcontentloaded", timeout=15000)
                time.sleep(1.5)
                title = page.title()
                # guest title: "Company hiring Title in Location | LinkedIn Jobs"
                m = re.match(r"(.+?)\s+hiring\s+", title)
                if m:
                    company = m.group(1).strip()[:80]
            except Exception as e:
                log.warning("title fetch failed for %s: %s", url, str(e)[:120])
            if company:
                conn.execute("UPDATE jobs SET company = ? WHERE url = ?", (company, url))
                updated += 1
                log.info("company: %s <- %s", company, url)
            else:
                log.warning("no company in title %r for %s", title[:60], url)
            time.sleep(2.0)  # be polite to the guest endpoint
        browser.close()
    conn.commit()
    return updated


# ── phase 2: matching ─────────────────────────────────────────────────────

def run_crossmatch(min_score: int | None = None, company_batch: int = 40) -> dict:
    """Link LinkedIn jobs to ATS postings. Returns stats."""
    conn = get_connection()
    ensure_columns(conn)
    threshold = min_score if min_score is not None else DEFAULTS["min_score"]

    companies_filled = _fetch_companies(conn, company_batch)

    # ATS rows with a usable company name in `site`
    ats = conn.execute(
        """
        SELECT site, title, url FROM jobs
        WHERE application_url IS NOT NULL
          AND strategy IN ('greenhouse', 'lever', 'remotive', 'remoteok', 'workday_api')
          AND site IS NOT NULL AND site != ''
        """
    ).fetchall()

    by_company: dict[str, list[tuple[str, str]]] = {}
    for site, title, url in ats:
        key = normalize_company(site)
        if key:
            by_company.setdefault(key, []).append((title, url))

    linked = conn.execute(
        """
        SELECT url, company, title FROM jobs
        WHERE site = 'linkedin' AND application_url IS NULL
          AND company IS NOT NULL AND fit_score >= ? AND applied_at IS NULL
        """,
        (threshold,),
    ).fetchall()

    matched = 0
    for url, company, title in linked:
        candidates = by_company.get(normalize_company(company), [])
        for ats_title, ats_url in candidates:
            if title_similar(title, ats_title):
                conn.execute(
                    "UPDATE jobs SET application_url = ? WHERE url = ?", (ats_url, url)
                )
                matched += 1
                log.info("crossmatch: %s [%s] -> %s", title[:50], company, ats_url[:70])
                break

    conn.commit()
    stats = {
        "ats_pool": len(ats),
        "companies_filled": companies_filled,
        "linkedin_candidates": len(linked),
        "matched": matched,
    }
    log.info(
        "Crossmatch done: ATS pool %d, companies fetched %d, "
        "linkedin candidates %d, matched %d",
        stats["ats_pool"], stats["companies_filled"],
        stats["linkedin_candidates"], stats["matched"],
    )
    return stats
