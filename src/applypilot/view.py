"""ApplyPilot HTML Dashboard Generator.

Generates a self-contained HTML dashboard with:
  - Summary stats (total, enriched, scored, high-fit)
  - Score distribution bar chart
  - Jobs-by-source breakdown
  - Filterable job cards grouped by score
  - Client-side search and score filtering
"""

from __future__ import annotations

import os
import webbrowser
from html import escape
from pathlib import Path

from rich.console import Console

from applypilot.config import APP_DIR, DB_PATH
from applypilot.database import get_connection

console = Console()


def generate_dashboard(output_path: str | None = None) -> str:
    """Generate an HTML dashboard of all jobs with fit scores.

    Args:
        output_path: Where to write the HTML file. Defaults to ~/.applypilot/dashboard.html.

    Returns:
        Absolute path to the generated HTML file.
    """
    out = Path(output_path) if output_path else APP_DIR / "dashboard.html"

    conn = get_connection()

    # Stats
    total = conn.execute("SELECT COUNT(*) FROM jobs").fetchone()[0]
    ready = conn.execute(
        "SELECT COUNT(*) FROM jobs "
        "WHERE full_description IS NOT NULL AND application_url IS NOT NULL"
    ).fetchone()[0]
    scored = conn.execute(
        "SELECT COUNT(*) FROM jobs WHERE fit_score IS NOT NULL"
    ).fetchone()[0]
    high_fit = conn.execute(
        "SELECT COUNT(*) FROM jobs WHERE fit_score >= 7"
    ).fetchone()[0]

    # Score distribution
    score_dist: dict[int, int] = {}
    if scored:
        rows = conn.execute(
            "SELECT fit_score, COUNT(*) FROM jobs "
            "WHERE fit_score IS NOT NULL "
            "GROUP BY fit_score ORDER BY fit_score DESC"
        ).fetchall()
        for r in rows:
            score_dist[r[0]] = r[1]

    # Site stats
    site_stats = conn.execute("""
        SELECT site,
               COUNT(*) as total,
               SUM(CASE WHEN fit_score >= 7 THEN 1 ELSE 0 END) as high_fit,
               SUM(CASE WHEN fit_score BETWEEN 5 AND 6 THEN 1 ELSE 0 END) as mid_fit,
               SUM(CASE WHEN fit_score < 5 AND fit_score IS NOT NULL THEN 1 ELSE 0 END) as low_fit,
               SUM(CASE WHEN fit_score IS NULL THEN 1 ELSE 0 END) as unscored,
               ROUND(AVG(fit_score), 1) as avg_score
        FROM jobs GROUP BY site ORDER BY high_fit DESC, total DESC
    """).fetchall()

    # All scored jobs (5+), ordered by score desc
    jobs = conn.execute("""
        SELECT url, title, salary, description, location, site, strategy,
               full_description, application_url, detail_error,
               fit_score, score_reasoning
        FROM jobs
        WHERE fit_score >= 5
        ORDER BY fit_score DESC, site, title
    """).fetchall()

    # Color map per site
    colors = {
        "RemoteOK": "#10b981", "WelcomeToTheJungle": "#f59e0b",
        "Job Bank Canada": "#3b82f6", "CareerJet Canada": "#8b5cf6",
        "Hacker News Jobs": "#ff6600", "BuiltIn Remote": "#ec4899",
        "TD Bank": "#00a651", "CIBC": "#c41f3e", "RBC": "#003168",
        "indeed": "#2164f3", "linkedin": "#0a66c2",
        "Dice": "#eb1c26", "Glassdoor": "#0caa41",
    }

    # Score distribution bar chart
    score_bars = ""
    max_count = max(score_dist.values()) if score_dist else 1
    for s in range(10, 0, -1):
        count = score_dist.get(s, 0)
        pct = (count / max_count * 100) if max_count else 0
        score_color = "#10b981" if s >= 7 else ("#f59e0b" if s >= 5 else "#ef4444")
        score_bars += f"""
        <div class="score-row">
          <span class="score-label">{s}</span>
          <div class="score-bar-track">
            <div class="score-bar-fill" style="width:{pct}%;background:{score_color}"></div>
          </div>
          <span class="score-count">{count}</span>
        </div>"""

    # Site stats rows
    site_rows = ""
    for s in site_stats:
        site = s["site"] or "?"
        color = colors.get(site, "#6b7280")
        avg = s["avg_score"] or 0
        site_rows += f"""
        <div class="site-row">
          <div class="site-name" style="color:{color}">{escape(site)}</div>
          <div class="site-nums">{s['total']} jobs &middot; {s['high_fit']} strong fit &middot; avg score {avg}</div>
          <div class="bar-track">
            <div class="bar-fill" style="width:{s['high_fit']/max(s['total'],1)*100}%;background:{color}"></div>
            <div class="bar-fill" style="width:{s['mid_fit']/max(s['total'],1)*100}%;background:{color}66"></div>
          </div>
        </div>"""

    # Job cards grouped by score
    job_sections = ""
    current_score = None
    for j in jobs:
        score = j["fit_score"] or 0
        if score != current_score:
            if current_score is not None:
                job_sections += "</div>"
            score_color = "#10b981" if score >= 7 else "#f59e0b"
            score_label = {
                10: "Perfect Match", 9: "Excellent Fit", 8: "Strong Fit",
                7: "Good Fit", 6: "Moderate+", 5: "Moderate",
            }.get(score, f"Score {score}")
            count_at_score = score_dist.get(score, 0)
            job_sections += f"""
            <h2 class="score-header" style="border-color:{score_color}">
              <span class="score-badge" style="background:{score_color}">{score}</span>
              {score_label} ({count_at_score} jobs)
            </h2>
            <div class="job-grid">"""
            current_score = score

        title = escape(j["title"] or "Untitled")
        url = escape(j["url"] or "")
        salary = escape(j["salary"] or "")
        location = escape(j["location"] or "")
        site = escape(j["site"] or "")
        site_color = colors.get(j["site"] or "", "#6b7280")
        apply_url = escape(j["application_url"] or "")

        # Parse keywords and reasoning from score_reasoning
        reasoning_raw = j["score_reasoning"] or ""
        reasoning_lines = reasoning_raw.split("\n")
        keywords = reasoning_lines[0][:120] if reasoning_lines else ""
        reasoning = reasoning_lines[1][:200] if len(reasoning_lines) > 1 else ""

        desc_preview = escape(j["full_description"] or "")[:300]
        full_desc_html = escape(j["full_description"] or "").replace("\n", "<br>")
        desc_len = len(j["full_description"] or "")

        meta_parts = []
        meta_parts.append(
            f'<span class="meta-tag site-tag" style="background:{site_color}33;color:{site_color}">{site}</span>'
        )
        if salary:
            meta_parts.append(f'<span class="meta-tag salary">{salary}</span>')
        if location:
            meta_parts.append(f'<span class="meta-tag location">{location[:40]}</span>')
        meta_html = " ".join(meta_parts)

        apply_html = ""
        if apply_url:
            apply_html = f'<a href="{apply_url}" class="apply-link" target="_blank">Apply</a>'

        job_sections += f"""
        <div class="job-card" data-score="{score}" data-site="{escape(j['site'] or '')}" data-location="{location.lower()}" data-url="{escape(url)}">
          <div class="card-header">
            <span class="score-pill" style="background:{'#10b981' if score >= 7 else '#f59e0b'}">{score}</span>
            <a href="{url}" class="job-title" target="_blank">{title}</a>
            <button class="del-btn" title="Remove from ApplyPilot">&#10005;</button>
          </div>
          <div class="meta-row">{meta_html}</div>
          {f'<div class="keywords-row">{escape(keywords)}</div>' if keywords else ''}
          {f'<div class="reasoning-row">{escape(reasoning)}</div>' if reasoning else ''}
          <p class="desc-preview">{desc_preview}...</p>
          {"<details class='full-desc-details'><summary class='expand-btn'>Full Description (" + f'{desc_len:,}' + " chars)</summary><div class='full-desc'>" + full_desc_html + "</div></details>" if j["full_description"] else ""}
          <div class="card-footer">{apply_html}</div>
        </div>"""

    if current_score is not None:
        job_sections += "</div>"

    html = f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>ApplyPilot Dashboard</title>
<style>
  :root {{
    --bg: #0f172a; --card: #1e293b; --text: #e2e8f0; --muted: #94a3b8; --faint: #64748b;
    --border: #334155; --border2: #475569; --chip-bg: #334155; --chip-fg: #94a3b8;
    --accent: #60a5fa; --accent-fg: #0f172a; --accent-soft: #60a5fa33; --accent-hover: #60a5fa22;
    --kw: #10b981; --pill-fg: #0f172a; --desc-bg: #0f172a; --desc-text: #cbd5e1;
    --shadow-card: #00000044; --modal-shadow: rgba(0, 0, 0, 0.5); --backdrop: rgba(15, 23, 42, 0.72);
    --del-hover-bg: #7f1d1d; --del-hover-border: #ef4444; --del-hover-fg: #fecaca;
    --icon-bg: #7f1d1d; --icon-fg: #fca5a5;
  }}
  :root[data-theme="light"] {{
    --bg: #f1f5f9; --card: #ffffff; --text: #0f172a; --muted: #64748b; --faint: #94a3b8;
    --border: #e2e8f0; --border2: #cbd5e1; --chip-bg: #e2e8f0; --chip-fg: #475569;
    --accent: #2563eb; --accent-fg: #ffffff; --accent-soft: #2563eb33; --accent-hover: #2563eb1a;
    --kw: #059669; --pill-fg: #ffffff; --desc-bg: #f8fafc; --desc-text: #334155;
    --shadow-card: rgba(15, 23, 42, 0.08); --modal-shadow: rgba(15, 23, 42, 0.2); --backdrop: rgba(100, 116, 139, 0.4);
    --del-hover-bg: #fee2e2; --del-hover-border: #ef4444; --del-hover-fg: #b91c1c;
    --icon-bg: #fee2e2; --icon-fg: #b91c1c;
  }}

  * {{ margin: 0; padding: 0; box-sizing: border-box; }}
  body {{ font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', system-ui, sans-serif; background: var(--bg); color: var(--text); padding: 2rem; transition: background 0.2s, color 0.2s; }}

  h1 {{ font-size: 1.8rem; font-weight: 700; margin-bottom: 0.5rem; }}
  .subtitle {{ color: var(--muted); margin-bottom: 2rem; }}

  .theme-btn {{ position: fixed; top: 1.1rem; right: 1.1rem; background: var(--card); border: 1px solid var(--border2);
                color: var(--muted); border-radius: 8px; width: 34px; height: 34px; cursor: pointer; font-size: 15px;
                display: flex; align-items: center; justify-content: center; z-index: 100; transition: all 0.15s; }}
  .theme-btn:hover {{ color: var(--text); border-color: var(--accent); }}

  /* Summary cards */
  .summary {{ display: grid; grid-template-columns: repeat(4, 1fr); gap: 1rem; margin-bottom: 2.5rem; }}
  .stat-card {{ background: var(--card); border-radius: 12px; padding: 1.25rem; }}
  .stat-num {{ font-size: 2rem; font-weight: 700; }}
  .stat-label {{ color: var(--muted); font-size: 0.85rem; margin-top: 0.25rem; }}
  .stat-ok .stat-num {{ color: #10b981; }}
  .stat-scored .stat-num {{ color: #3b82f6; }}
  .stat-high .stat-num {{ color: #f59e0b; }}
  .stat-total .stat-num {{ color: var(--text); }}

  /* Filters */
  .filters {{ background: var(--card); border-radius: 12px; padding: 1.25rem; margin-bottom: 2rem; display: flex; gap: 1rem; flex-wrap: wrap; align-items: center; }}
  .filter-label {{ color: var(--muted); font-size: 0.85rem; font-weight: 600; }}
  .filter-btn {{ background: var(--chip-bg); border: none; color: var(--chip-fg); padding: 0.4rem 0.8rem; border-radius: 6px; cursor: pointer; font-size: 0.8rem; transition: all 0.15s; }}
  .filter-btn:hover {{ background: var(--border2); color: var(--text); }}
  .filter-btn.active {{ background: var(--accent); color: var(--accent-fg); font-weight: 600; }}
  .search-input {{ background: var(--chip-bg); border: 1px solid var(--border2); color: var(--text); padding: 0.4rem 0.8rem; border-radius: 6px; font-size: 0.8rem; width: 200px; }}
  .search-input::placeholder {{ color: var(--faint); }}

  /* Score distribution */
  .score-section {{ display: grid; grid-template-columns: 1fr 1fr; gap: 1.5rem; margin-bottom: 2.5rem; }}
  .score-dist {{ background: var(--card); border-radius: 12px; padding: 1.5rem; }}
  .score-dist h3 {{ font-size: 1rem; margin-bottom: 1rem; color: var(--muted); }}
  .score-row {{ display: flex; align-items: center; gap: 0.5rem; margin-bottom: 0.4rem; }}
  .score-label {{ width: 1.5rem; text-align: right; font-size: 0.85rem; font-weight: 600; }}
  .score-bar-track {{ flex: 1; height: 14px; background: var(--border); border-radius: 4px; overflow: hidden; }}
  .score-bar-fill {{ height: 100%; border-radius: 4px; transition: width 0.3s; }}
  .score-count {{ width: 2.5rem; font-size: 0.8rem; color: var(--muted); }}

  /* Site bars */
  .sites-section {{ background: var(--card); border-radius: 12px; padding: 1.5rem; }}
  .sites-section h3 {{ font-size: 1rem; margin-bottom: 1rem; color: var(--muted); }}
  .site-row {{ margin-bottom: 0.8rem; }}
  .site-name {{ font-weight: 600; font-size: 0.9rem; }}
  .site-nums {{ color: var(--muted); font-size: 0.75rem; margin: 0.15rem 0; }}
  .bar-track {{ height: 8px; background: var(--border); border-radius: 4px; display: flex; overflow: hidden; }}
  .bar-fill {{ height: 100%; transition: width 0.3s; }}

  /* Score group headers */
  .score-header {{ font-size: 1.2rem; font-weight: 600; margin: 2.5rem 0 1rem; padding-bottom: 0.5rem; border-bottom: 3px solid; display: flex; align-items: center; gap: 0.75rem; }}
  .score-badge {{ display: inline-flex; align-items: center; justify-content: center; width: 2rem; height: 2rem; border-radius: 8px; color: var(--pill-fg); font-weight: 700; font-size: 1rem; }}

  /* Job grid */
  .job-grid {{ display: grid; grid-template-columns: repeat(auto-fill, minmax(380px, 1fr)); gap: 1rem; }}

  .job-card {{ background: var(--card); border-radius: 10px; padding: 1rem; border-left: 3px solid var(--border); transition: all 0.15s; }}
  .job-card:hover {{ transform: translateY(-2px); box-shadow: 0 4px 12px var(--shadow-card); }}
  .job-card[data-score="9"], .job-card[data-score="10"] {{ border-left-color: #10b981; }}
  .job-card[data-score="8"] {{ border-left-color: #34d399; }}
  .job-card[data-score="7"] {{ border-left-color: #60a5fa; }}
  .job-card[data-score="6"] {{ border-left-color: #f59e0b; }}
  .job-card[data-score="5"] {{ border-left-color: #f59e0b88; }}

  .card-header {{ display: flex; align-items: center; gap: 0.5rem; margin-bottom: 0.5rem; }}
  .del-btn {{ margin-left: auto; background: transparent; border: 1px solid var(--border2); color: var(--muted);
              border-radius: 6px; width: 22px; height: 22px; line-height: 1; cursor: pointer;
              font-size: 12px; flex-shrink: 0; }}
  .del-btn:hover {{ background: var(--del-hover-bg); border-color: var(--del-hover-border); color: var(--del-hover-fg); }}

  /* Confirm modal (dashboard-styled replacement for window.confirm) */
  .modal-backdrop {{ position: fixed; inset: 0; background: var(--backdrop);
                    backdrop-filter: blur(4px); -webkit-backdrop-filter: blur(4px);
                    display: none; align-items: center; justify-content: center; z-index: 1000; }}
  .modal-backdrop.visible {{ display: flex; }}
  .modal {{ background: var(--card); border: 1px solid var(--border); border-radius: 14px;
            max-width: 400px; width: calc(100% - 2rem); padding: 1.4rem;
            box-shadow: 0 20px 50px var(--modal-shadow);
            animation: modalIn 0.16s ease-out; }}
  @keyframes modalIn {{ from {{ opacity: 0; transform: translateY(8px) scale(0.97); }}
                        to {{ opacity: 1; transform: none; }} }}
  .modal-title {{ font-size: 1rem; font-weight: 700; color: var(--text); margin-bottom: 0.6rem;
                  display: flex; align-items: center; gap: 0.6rem; }}
  .modal-icon {{ width: 30px; height: 30px; border-radius: 8px; background: var(--icon-bg); color: var(--icon-fg);
                 display: flex; align-items: center; justify-content: center; flex-shrink: 0; font-size: 13px; }}
  .modal-text {{ color: var(--muted); font-size: 0.88rem; line-height: 1.5; margin-bottom: 1.2rem; }}
  .modal-btns {{ display: flex; gap: 0.6rem; justify-content: flex-end; }}
  .btn {{ border-radius: 8px; padding: 0.5rem 1.1rem; font-size: 0.85rem; font-weight: 600;
          cursor: pointer; border: 1px solid transparent; font-family: inherit; }}
  .btn-danger {{ background: #dc2626; color: #fff; }}
  .btn-danger:hover {{ background: #ef4444; }}
  .btn-ghost {{ background: transparent; border-color: var(--border2); color: var(--muted); }}
  .btn-ghost:hover {{ background: var(--border); color: var(--text); }}
  .score-pill {{ display: inline-flex; align-items: center; justify-content: center; min-width: 1.6rem; height: 1.6rem; border-radius: 6px; color: var(--pill-fg); font-weight: 700; font-size: 0.8rem; flex-shrink: 0; }}

  .job-title {{ color: var(--text); text-decoration: none; font-weight: 600; font-size: 0.95rem; }}
  .job-title:hover {{ color: var(--accent); }}

  .meta-row {{ display: flex; flex-wrap: wrap; gap: 0.4rem; margin-bottom: 0.4rem; }}
  .meta-tag {{ font-size: 0.72rem; padding: 0.15rem 0.5rem; border-radius: 4px; background: var(--chip-bg); color: var(--chip-fg); }}
  .meta-tag.salary {{ background: #064e3b; color: #6ee7b7; }}
  .meta-tag.location {{ background: #1e3a5f; color: #93c5fd; }}

  .keywords-row {{ font-size: 0.75rem; color: var(--kw); margin-bottom: 0.3rem; line-height: 1.4; }}
  .reasoning-row {{ font-size: 0.75rem; color: var(--muted); margin-bottom: 0.5rem; font-style: italic; line-height: 1.4; }}

  .desc-preview {{ font-size: 0.8rem; color: var(--faint); line-height: 1.5; margin-bottom: 0.75rem; max-height: 3.6em; overflow: hidden; }}

  .card-footer {{ display: flex; justify-content: flex-end; }}
  .apply-link {{ font-size: 0.8rem; color: var(--accent); text-decoration: none; padding: 0.3rem 0.8rem; border: 1px solid var(--accent-soft); border-radius: 6px; font-weight: 500; }}
  .apply-link:hover {{ background: var(--accent-hover); }}

  /* Expandable full description */
  .full-desc-details {{ margin-bottom: 0.75rem; }}
  .expand-btn {{ font-size: 0.8rem; color: var(--accent); cursor: pointer; list-style: none; padding: 0.3rem 0; }}
  .expand-btn::-webkit-details-marker {{ display: none; }}
  .expand-btn:hover {{ color: var(--accent); opacity: 0.8; }}
  .full-desc {{ font-size: 0.8rem; color: var(--desc-text); line-height: 1.6; margin-top: 0.5rem; padding: 0.75rem; background: var(--desc-bg); border-radius: 8px; max-height: 400px; overflow-y: auto; white-space: pre-wrap; word-break: break-word; }}

  .hidden {{ display: none !important; }}
  .job-count {{ color: var(--muted); font-size: 0.85rem; margin-bottom: 1rem; }}

  @media (max-width: 768px) {{
    .summary {{ grid-template-columns: repeat(2, 1fr); }}
    .score-section {{ grid-template-columns: 1fr; }}
    .job-grid {{ grid-template-columns: 1fr; }}
    body {{ padding: 1rem; }}
  }}
</style>
<script>
(function() {{
  var saved = localStorage.getItem('dash-theme');
  var theme = saved || (window.matchMedia && matchMedia('(prefers-color-scheme: light)').matches ? 'light' : 'dark');
  document.documentElement.dataset.theme = theme;
}})();
</script>
</head>
<body>

<button class="theme-btn" id="theme-toggle" type="button" title="Toggle light / dark theme">&#9789;</button>

<h1>ApplyPilot Dashboard</h1>
<p class="subtitle">{total} jobs &middot; {scored} scored &middot; {high_fit} strong matches (7+)</p>

<div class="summary">
  <div class="stat-card stat-total"><div class="stat-num">{total}</div><div class="stat-label">Total Jobs</div></div>
  <div class="stat-card stat-ok"><div class="stat-num">{ready}</div><div class="stat-label">Ready (desc + URL)</div></div>
  <div class="stat-card stat-scored"><div class="stat-num">{scored}</div><div class="stat-label">Scored by LLM</div></div>
  <div class="stat-card stat-high"><div class="stat-num">{high_fit}</div><div class="stat-label">Strong Fit (7+)</div></div>
</div>

<div class="filters">
  <span class="filter-label">Score:</span>
  <button class="filter-btn active" onclick="filterScore(0)">All 5+</button>
  <button class="filter-btn" onclick="filterScore(7)">7+ Strong</button>
  <button class="filter-btn" onclick="filterScore(8)">8+ Excellent</button>
  <button class="filter-btn" onclick="filterScore(9)">9+ Perfect</button>
  <span class="filter-label" style="margin-left:1rem">Search:</span>
  <input type="text" class="search-input" placeholder="Filter by title, site..." oninput="filterText(this.value)">
</div>

<div class="score-section">
  <div class="score-dist">
    <h3>Score Distribution</h3>
    {score_bars}
  </div>
  <div class="sites-section">
    <h3>By Source</h3>
    {site_rows}
  </div>
</div>

<div id="job-count" class="job-count"></div>

{job_sections}

<script>
let minScore = 0;
let searchText = '';

function filterScore(min) {{
  minScore = min;
  document.querySelectorAll('.filter-btn').forEach(b => b.classList.remove('active'));
  event.target.classList.add('active');
  applyFilters();
}}

function filterText(text) {{
  searchText = text.toLowerCase();
  applyFilters();
}}

function applyFilters() {{
  let shown = 0;
  let total = 0;
  document.querySelectorAll('.job-card').forEach(card => {{
    total++;
    const score = parseInt(card.dataset.score) || 0;
    const text = card.textContent.toLowerCase();
    const scoreMatch = score >= (minScore || 5);
    const textMatch = !searchText || text.includes(searchText);
    if (scoreMatch && textMatch) {{
      card.classList.remove('hidden');
      shown++;
    }} else {{
      card.classList.add('hidden');
    }}
  }});
  document.getElementById('job-count').textContent = `Showing ${{shown}} of ${{total}} jobs`;

  // Hide empty score groups
  document.querySelectorAll('.score-header').forEach(header => {{
    const grid = header.nextElementSibling;
    if (grid && grid.classList.contains('job-grid')) {{
      const visible = grid.querySelectorAll('.job-card:not(.hidden)').length;
      header.style.display = visible ? '' : 'none';
      grid.style.display = visible ? '' : 'none';
    }}
  }});
}}

applyFilters();

// ── Delete support ────────────────────────────────────────────────────
function removeCard(url) {{
  document.querySelectorAll('.job-card').forEach(c => {{
    if (c.dataset.url === url) {{
      const grid = c.parentElement;
      c.remove();
      if (grid && grid.querySelectorAll('.job-card:not(.hidden)').length === 0) {{
        const header = grid.previousElementSibling;
        if (header && header.classList.contains('score-header')) {{ header.style.display = 'none'; grid.style.display = 'none'; }}
      }}
      const total = document.querySelectorAll('.job-card').length;
      const shown = document.querySelectorAll('.job-card:not(.hidden)').length;
      const counter = document.getElementById('job-count');
      if (counter) counter.textContent = `Showing ${{shown}} of ${{total}} jobs`;
    }}
  }});
}}
function deleteJob(url) {{
  fetch('/api/delete', {{
    method: 'POST',
    headers: {{'Content-Type': 'application/json'}},
    body: JSON.stringify({{url: url}})
  }})
  .then(r => {{
    if (r.ok) {{ removeCard(url); }}
    else {{ showModal({{title: 'Delete failed', text: 'Server returned HTTP ' + r.status + '.', dangerOnly: true}}); }}
  }})
  .catch(() => showModal({{title: 'Delete failed', text: 'Could not reach the server. Is the SSH tunnel up?', dangerOnly: true}}));
}}
// Styled confirm dialog (matches the dashboard theme)
function showModal(opts) {{
  return new Promise(resolve => {{
    const bd = document.getElementById('confirm-modal');
    const panel = bd.querySelector('.modal');
    bd.querySelector('.modal-title span').textContent = opts.title || 'Are you sure?';
    bd.querySelector('.modal-text').textContent = opts.text || '';
    const okBtn = bd.querySelector('.btn-danger');
    const cancelBtn = bd.querySelector('.btn-ghost');
    okBtn.textContent = opts.confirmLabel || 'Delete';
    okBtn.style.display = opts.dangerOnly ? 'none' : '';
    function close(v) {{
      bd.classList.remove('visible');
      okBtn.onclick = cancelBtn.onclick = bd.onclick = null;
      document.removeEventListener('keydown', onKey);
      resolve(v);
    }}
    function onKey(e) {{
      if (e.key === 'Escape') close(false);
      if (e.key === 'Enter') close(!!opts.dangerOnly ? true : false);
    }}
    okBtn.onclick = () => close(true);
    cancelBtn.onclick = () => close(false);
    bd.onclick = e => {{ if (e.target === bd) close(false); }};
    document.addEventListener('keydown', onKey);
    bd.classList.add('visible');
    cancelBtn.focus();
    panel.setAttribute('aria-label', opts.title || 'Confirm');
  }});
}}
async function askDelete(card, title, text) {{
  const jobTitle = (card.querySelector('.job-title') || {{}}).textContent || 'this job';
  const t = (jobTitle || 'this job').trim();
  if (await showModal({{title: title, text: text.replace('{{JOB}}', t.slice(0, 80))}})) {{
    deleteJob(card.dataset.url);
  }}
}}
document.addEventListener('click', e => {{
  const delBtn = e.target.closest('.del-btn');
  if (delBtn) {{
    const card = delBtn.closest('.job-card');
    if (card) askDelete(card, 'Delete this job?',
      'Remove "{{JOB}}" from ApplyPilot? Its tailored resume and cover letter will be deleted too. This job will not be tracked again.');
    return;
  }}
  const link = e.target.closest('a.job-title, a.apply-link');
  if (link) {{
    const card = link.closest('.job-card');
    if (card) {{
      // The job opens in a new tab; afterwards ask whether to drop it here.
      setTimeout(() => {{
        askDelete(card, 'Already applied?',
          'Remove "{{JOB}}" from the list? The job stays open in the other tab — this only cleans up your ApplyPilot board.');
      }}, 1500);
    }}
  }}
}});

// ── Theme toggle ─────────────────────────────────────────────────────
const themeBtn = document.getElementById('theme-toggle');
function paintThemeBtn() {{
  themeBtn.innerHTML = document.documentElement.dataset.theme === 'light' ? '&#9790;' : '&#9788;';
}}
themeBtn.addEventListener('click', () => {{
  const next = document.documentElement.dataset.theme === 'light' ? 'dark' : 'light';
  document.documentElement.dataset.theme = next;
  localStorage.setItem('dash-theme', next);
  paintThemeBtn();
}});
// follow system theme changes unless the user picked manually
const mq = window.matchMedia('(prefers-color-scheme: light)');
mq.addEventListener('change', e => {{
  if (!localStorage.getItem('dash-theme')) {{
    document.documentElement.dataset.theme = e.matches ? 'light' : 'dark';
    paintThemeBtn();
  }}
}});
paintThemeBtn();
</script>

<div class="modal-backdrop" id="confirm-modal">
  <div class="modal" role="dialog" aria-modal="true" aria-label="Confirm">
    <div class="modal-title">
      <div class="modal-icon">&#10005;</div>
      <span>Are you sure?</span>
    </div>
    <div class="modal-text"></div>
    <div class="modal-btns">
      <button class="btn btn-ghost" type="button">Cancel</button>
      <button class="btn btn-danger" type="button">Delete</button>
    </div>
  </div>
</div>

</body>
</html>"""

    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(html, encoding="utf-8")

    abs_path = str(out.resolve())
    console.print(f"[green]Dashboard written to {abs_path}[/green]")
    return abs_path


def open_dashboard(output_path: str | None = None) -> None:
    """Generate the dashboard and open it in the default browser.

    Args:
        output_path: Where to write the HTML file. Defaults to ~/.applypilot/dashboard.html.
    """
    path = generate_dashboard(output_path)
    console.print("[dim]Opening in browser...[/dim]")
    webbrowser.open(f"file:///{path}")
