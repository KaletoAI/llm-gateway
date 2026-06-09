"""Call stats: SQLite-backed per-request log + minimal HTML dashboard.

Zero new dependencies — sqlite3 is stdlib, FastAPI/uvicorn are already
required by the gateway. The dashboard is plain HTML rendered from
f-strings; no template engine, no JS, no chart libs. Auto-refresh via
<meta http-equiv="refresh">.
"""

from __future__ import annotations

import asyncio
import logging
import sqlite3
import time
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from fastapi import FastAPI
from fastapi.responses import HTMLResponse, PlainTextResponse

logger = logging.getLogger(__name__)

_DB_PATH: Optional[Path] = None
_SCHEMA = """
CREATE TABLE IF NOT EXISTS calls (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    ts            INTEGER NOT NULL,
    duration_ms   INTEGER NOT NULL,
    backend       TEXT    NOT NULL,
    source        TEXT,
    alias         TEXT,
    model         TEXT,
    endpoint      TEXT,
    status        INTEGER,
    input_tokens  INTEGER DEFAULT 0,
    output_tokens INTEGER DEFAULT 0,
    cost_usd      REAL    DEFAULT 0,
    req_preview   TEXT
);
CREATE INDEX IF NOT EXISTS idx_calls_ts      ON calls(ts);
CREATE INDEX IF NOT EXISTS idx_calls_backend ON calls(backend);
"""


def init(db_path: str) -> None:
    """Open / create the stats DB, set WAL, ensure schema."""
    global _DB_PATH
    _DB_PATH = Path(db_path)
    with _conn() as c:
        c.execute("PRAGMA journal_mode=WAL")
        c.executescript(_SCHEMA)
        # Migrate older DBs that predate the req_preview column.
        cols = {r[1] for r in c.execute("PRAGMA table_info(calls)").fetchall()}
        if "req_preview" not in cols:
            c.execute("ALTER TABLE calls ADD COLUMN req_preview TEXT")
    logger.info(f"stats: SQLite at {_DB_PATH} (WAL mode)")


@contextmanager
def _conn():
    conn = sqlite3.connect(_DB_PATH, isolation_level=None, timeout=10.0)
    try:
        yield conn
    finally:
        conn.close()


def _record_sync(row: tuple) -> None:
    with _conn() as c:
        c.execute(
            "INSERT INTO calls (ts, duration_ms, backend, source, alias, model, "
            "endpoint, status, input_tokens, output_tokens, cost_usd, req_preview) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
            row,
        )


def _preview(text: Optional[str], head: int = 50, tail: int = 50) -> Optional[str]:
    """First `head` + last `tail` chars of the request, ellipsis in between."""
    if not text:
        return None
    text = " ".join(text.split())  # collapse whitespace/newlines for a compact preview
    if len(text) <= head + tail:
        return text
    return f"{text[:head]} … {text[-tail:]}"


async def record_call(
    *,
    duration_ms: int,
    backend: str,
    source: Optional[str],
    alias: Optional[str],
    model: Optional[str],
    endpoint: Optional[str],
    status: int,
    input_tokens: int = 0,
    output_tokens: int = 0,
    cost_usd: float = 0.0,
    request_text: Optional[str] = None,
) -> None:
    """Async-safe insert. Never raises into the request path."""
    if _DB_PATH is None:
        return
    row = (
        int(time.time()),
        duration_ms,
        backend,
        source,
        alias,
        model,
        endpoint,
        status,
        input_tokens,
        output_tokens,
        cost_usd,
        _preview(request_text),
    )
    try:
        await asyncio.to_thread(_record_sync, row)
    except Exception as e:
        logger.warning(f"stats: insert failed: {e}")


async def prune_loop(retention_days: int, interval_s: int = 3600) -> None:
    """Periodically delete rows older than retention_days. 0 = disabled."""
    if retention_days <= 0:
        return
    while True:
        try:
            cutoff = int(time.time()) - retention_days * 86400
            def _prune():
                with _conn() as c:
                    cur = c.execute("DELETE FROM calls WHERE ts < ?", (cutoff,))
                    return cur.rowcount
            n = await asyncio.to_thread(_prune)
            if n:
                logger.info(f"stats: pruned {n} rows older than {retention_days} days")
        except Exception as e:
            logger.warning(f"stats: prune failed: {e}")
        await asyncio.sleep(interval_s)


# ── Dashboard ────────────────────────────────────────────────────────────────

stats_app = FastAPI(title="LLM Gateway Stats", docs_url=None, redoc_url=None)


def _q(sql: str, *params) -> list[tuple]:
    with _conn() as c:
        return c.execute(sql, params).fetchall()


@stats_app.get("/healthz", response_class=PlainTextResponse)
def healthz() -> str:
    return "ok"


@stats_app.get("/", response_class=HTMLResponse)
def dashboard() -> str:
    if _DB_PATH is None:
        return "<h1>Stats not enabled</h1>"

    total_count, total_cost = _q(
        "SELECT COUNT(*), COALESCE(SUM(cost_usd),0) FROM calls"
    )[0]
    h24 = int(time.time()) - 86400
    h24_count, h24_cost = _q(
        "SELECT COUNT(*), COALESCE(SUM(cost_usd),0) FROM calls WHERE ts > ?", h24
    )[0]
    by_backend = _q(
        "SELECT backend, COUNT(*), COALESCE(SUM(input_tokens),0), "
        "COALESCE(SUM(output_tokens),0), COALESCE(SUM(cost_usd),0), "
        "COALESCE(AVG(duration_ms),0) "
        "FROM calls GROUP BY backend ORDER BY COUNT(*) DESC"
    )
    by_model = _q(
        "SELECT COALESCE(alias,''), COALESCE(model,''), COUNT(*), "
        "COALESCE(SUM(input_tokens),0), COALESCE(SUM(output_tokens),0), "
        "COALESCE(SUM(cost_usd),0) "
        "FROM calls GROUP BY alias, model ORDER BY COUNT(*) DESC LIMIT 30"
    )
    by_source = _q(
        "SELECT COALESCE(source,'unknown'), COUNT(*), COALESCE(SUM(cost_usd),0) "
        "FROM calls GROUP BY source ORDER BY COUNT(*) DESC LIMIT 20"
    )
    recent = _q(
        "SELECT ts, duration_ms, backend, source, alias, model, endpoint, "
        "status, input_tokens, output_tokens, cost_usd, req_preview "
        "FROM calls ORDER BY id DESC LIMIT 50"
    )
    return _render(
        (total_count, total_cost),
        (h24_count, h24_cost),
        by_backend, by_model, by_source, recent,
    )


def _fmt_ts(ts: int) -> str:
    return datetime.fromtimestamp(ts, tz=timezone.utc).astimezone().strftime("%Y-%m-%d %H:%M:%S")


def _fmt_time(ts: int) -> str:
    return datetime.fromtimestamp(ts, tz=timezone.utc).astimezone().strftime("%H:%M:%S")


def _esc(s) -> str:
    if s is None:
        return ""
    return (str(s)
            .replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
            .replace('"', "&quot;").replace("'", "&#39;"))


# Shared chrome: CSS + nav tabs + optional search JS, wrapped by _doc(). Kept as
# plain (non-f) strings so braces don't need escaping.
_CSS = """
* { box-sizing: border-box; }
body { font: 14px/1.4 system-ui, -apple-system, "Segoe UI", sans-serif;
       margin: 1.5rem; color: #1c1c1c; background: #f4f5f7; }
h1 { margin: 0 0 .5rem; }
h2 { margin: 0 0 .75rem; font-size: 1.05rem; color: #555; }
.muted { color: #888; font-size: .85rem; }
.cards { display: grid; grid-template-columns: repeat(auto-fit, minmax(180px, 1fr));
         gap: 1rem; margin: 1rem 0 2rem; }
.card { background: #fff; border-radius: 8px; padding: 1rem 1.25rem;
        box-shadow: 0 1px 3px rgba(0,0,0,.06); }
.metric { font-size: 1.6rem; font-weight: 600; margin-top: .25rem; }
.panel { background: #fff; border-radius: 8px; padding: 1rem 1.25rem;
         margin: 1rem 0; box-shadow: 0 1px 3px rgba(0,0,0,.06); }
table { width: 100%; border-collapse: collapse; }
th, td { padding: .4rem .5rem; text-align: left;
         border-bottom: 1px solid #eee; vertical-align: top; }
th { background: #fafafa; font-weight: 600; color: #555; }
tr:hover td { background: #fcfcfc; }
td.ok { color: #1a7f37; font-weight: 600; }
td.err { color: #cf222e; font-weight: 600; }
code { background: #f1f1f1; padding: 1px 4px; border-radius: 3px; }
.tabs { display: flex; gap: .25rem; margin: .25rem 0 0; border-bottom: 2px solid #e0e2e8; }
.tabs a { padding: .45rem 1rem; border-radius: 6px 6px 0 0; text-decoration: none;
          color: #555; background: #e7e9ee; font-weight: 600; }
.tabs a.active { background: #fff; color: #1c1c1c; box-shadow: 0 -1px 3px rgba(0,0,0,.06); }
.search { margin: 1rem 0 0; }
.search input { width: 100%; max-width: 460px; padding: .55rem .75rem; font-size: 1rem;
                border: 1px solid #ccc; border-radius: 6px; }
.count { color: #888; font-size: .8rem; margin-left: .5rem; }
.badge { display: inline-block; padding: 1px 7px; border-radius: 10px; font-size: .75rem;
         font-weight: 600; }
.badge.ok   { background: #e6f4ea; color: #1a7f37; }
.badge.warn { background: #fff4e5; color: #b35900; }
.badge.err  { background: #fde8e8; color: #cf222e; }
.badge.info { background: #e8eefc; color: #1a56c4; }
.badge.off  { background: #eceef1; color: #888; }
.badge.busy { background: #f3e8fd; color: #8250df; }
tr.off td { color: #aaa; }
.prio { display: inline-block; min-width: 1.5rem; text-align: center; font-weight: 600;
        background: #eef0f4; border-radius: 5px; padding: 0 .35rem; }
.dim { color: #aaa; }
.host { white-space: nowrap; }
.hidden { display: none; }
"""

# Vanilla filter for the routing tab. Matches each row's text plus its
# data-search attribute (carries the alias name onto continuation rows), keeps
# the query in the URL hash so it survives a reload.
_SEARCH_JS = """
<script>
(function () {
  var box = document.getElementById('q');
  if (!box) return;
  var rows = Array.prototype.slice.call(document.querySelectorAll('tr.f'));
  var counter = document.getElementById('count');
  function apply(q) {
    q = q.trim().toLowerCase();
    var shown = 0;
    rows.forEach(function (r) {
      var hay = (r.textContent + ' ' + (r.dataset.search || '')).toLowerCase();
      var match = !q || hay.indexOf(q) !== -1;
      r.classList.toggle('hidden', !match);
      if (match) shown++;
    });
    if (counter) counter.textContent = q ? (shown + ' match' + (shown === 1 ? '' : 'es')) : '';
    history.replaceState(null, '', q ? '#' + encodeURIComponent(q) : location.pathname);
  }
  box.addEventListener('input', function () { apply(box.value); });
  var initial = decodeURIComponent(location.hash.slice(1));
  if (initial) box.value = initial;
  apply(box.value);
  box.focus();
})();
</script>
"""


def _nav(active: str) -> str:
    def cls(name: str) -> str:
        return ' class="active"' if name == active else ""
    return (f'<div class="tabs"><a href="/"{cls("stats")}>Stats</a>'
            f'<a href="/routing"{cls("routing")}>Routing</a></div>')


def _doc(title: str, active: str, body: str, *, refresh: Optional[int] = None,
         search: bool = False) -> str:
    refresh_tag = f'<meta http-equiv="refresh" content="{refresh}">' if refresh else ""
    return (f'<!DOCTYPE html>\n<html lang="en">\n<head>\n<meta charset="utf-8">\n'
            f'{refresh_tag}\n<title>{title}</title>\n<style>{_CSS}</style>\n</head>\n'
            f'<body>\n<h1>llm-gateway</h1>\n{_nav(active)}\n{body}\n'
            f'{_SEARCH_JS if search else ""}\n</body>\n</html>')


def _render(total, h24, by_backend, by_model, by_source, recent) -> str:
    rows_backend = "".join(
        f"<tr><td>{_esc(b)}</td><td>{c:,}</td><td>{int(it):,}</td>"
        f"<td>{int(ot):,}</td><td>${cu:.4f}</td><td>{int(dm):,} ms</td></tr>"
        for (b, c, it, ot, cu, dm) in by_backend
    ) or '<tr><td colspan="6" class="muted">no data yet</td></tr>'

    rows_model = "".join(
        f"<tr><td>{_esc(a)}</td><td>{_esc(m)}</td><td>{c:,}</td>"
        f"<td>{int(it):,}</td><td>{int(ot):,}</td><td>${cu:.4f}</td></tr>"
        for (a, m, c, it, ot, cu) in by_model
    ) or '<tr><td colspan="6" class="muted">no data yet</td></tr>'

    rows_source = "".join(
        f"<tr><td>{_esc(s)}</td><td>{c:,}</td><td>${cu:.4f}</td></tr>"
        for (s, c, cu) in by_source
    ) or '<tr><td colspan="3" class="muted">no data yet</td></tr>'

    rows_recent = "".join(
        f"<tr><td>{_fmt_ts(ts - round((dm or 0) / 1000))}</td><td>{_fmt_time(ts)}</td>"
        f"<td><code>{_esc(rp)}</code></td><td>{_esc(b)}</td><td>{_esc(src)}</td>"
        f"<td>{_esc(a)}</td><td>{_esc(m)}</td><td>{_esc(ep)}</td>"
        f"<td class='{ 'ok' if 200 <= (st or 0) < 400 else 'err' }'>{st}</td>"
        f"<td>{int(dm):,} ms</td><td>{int(it or 0)}/{int(ot or 0)}</td>"
        f"<td>${(cu or 0):.5f}</td></tr>"
        for (ts, dm, b, src, a, m, ep, st, it, ot, cu, rp) in recent
    ) or '<tr><td colspan="12" class="muted">no calls recorded yet</td></tr>'

    body = f"""<p class="muted">Auto-refreshes every 30 s. Source override: send header
<code>X-Source: my-workflow</code> from clients to tag calls.</p>

<div class="cards">
  <div class="card"><div class="muted">Total calls</div><div class="metric">{total[0]:,}</div></div>
  <div class="card"><div class="muted">Total cost</div><div class="metric">${total[1]:.4f}</div></div>
  <div class="card"><div class="muted">Last 24h calls</div><div class="metric">{h24[0]:,}</div></div>
  <div class="card"><div class="muted">Last 24h cost</div><div class="metric">${h24[1]:.4f}</div></div>
</div>

<div class="panel">
<h2>By backend</h2>
<table><thead><tr><th>Backend</th><th>Calls</th><th>Input tokens</th>
<th>Output tokens</th><th>Cost</th><th>Avg duration</th></tr></thead>
<tbody>{rows_backend}</tbody></table>
</div>

<div class="panel">
<h2>By model (top 30)</h2>
<table><thead><tr><th>Alias</th><th>Real model</th><th>Calls</th>
<th>Input</th><th>Output</th><th>Cost</th></tr></thead>
<tbody>{rows_model}</tbody></table>
</div>

<div class="panel">
<h2>By source (top 20)</h2>
<table><thead><tr><th>Source</th><th>Calls</th><th>Cost</th></tr></thead>
<tbody>{rows_source}</tbody></table>
</div>

<div class="panel">
<h2>Recent calls (last 50)</h2>
<table><thead><tr><th>Start</th><th>End</th><th>Request</th><th>Backend</th><th>Source</th><th>Alias</th>
<th>Model</th><th>Endpoint</th><th>Status</th><th>Duration</th>
<th>In/Out</th><th>Cost</th></tr></thead>
<tbody>{rows_recent}</tbody></table>
</div>"""
    return _doc("llm-gateway stats", "stats", body, refresh=30)


@stats_app.get("/routing", response_class=HTMLResponse)
def routing() -> str:
    import main  # lazy import: main imports stats at load, so defer to call time
    return _render_routing(main.routing_snapshot())


def _render_routing(snap: dict) -> str:
    aliases = snap["aliases"]
    models = snap["models"]
    conflicts = snap["conflicts"]

    # Collisions panel — only shown when an alias name equals a real model id.
    conflict_html = ""
    if conflicts:
        crows = []
        for c in conflicts:
            shadow = ", ".join(_esc(b) for b in c["shadowed"])
            covered = ", ".join(_esc(b) for b in c["covered"]) or "—"
            if c["shadowed"]:
                kind = '<span class="badge err">shadows real model</span>'
                shadow_cell = f'<td class="err">{shadow}</td>'
            else:
                kind = '<span class="badge info">covered</span>'
                shadow_cell = '<td class="dim">—</td>'
            crows.append(
                f'<tr class="f"><td><code>{_esc(c["name"])}</code></td>'
                f"<td>{kind}</td><td>{covered}</td>{shadow_cell}</tr>"
            )
        has_actionable = any(c["shadowed"] for c in conflicts)
        note = ('<p class="muted">⚠ <b>Shadowed</b> hosts serve a real model whose id '
                "equals the alias but aren't in its mapping — that model is unreachable "
                "by its bare name (the alias overrides the pass-through). Add them to the "
                "alias mapping, or rename the alias.</p>") if has_actionable else (
                '<p class="muted">These aliases intentionally shadow a real model id and '
                "cover every host that serves it — no backend is left unreachable.</p>")
        conflict_html = (
            '<div class="panel"><h2>Alias / model-name collisions</h2>' + note +
            "<table><thead><tr><th>Alias</th><th>Kind</th><th>Covered hosts</th>"
            "<th>Shadowed hosts</th></tr></thead><tbody>" + "".join(crows) +
            "</tbody></table></div>"
        )

    # Aliases — one row per route, in effective-priority order.
    arows = []
    for a in aliases:
        name = a["alias"]
        routes = a["routes"]
        if not routes:
            arows.append(f'<tr class="f"><td><code>{_esc(name)}</code></td>'
                         '<td colspan="4" class="muted">no enabled backend maps this alias</td></tr>')
            continue
        for i, r in enumerate(routes):
            label = f'<code>{_esc(name)}</code>' if i == 0 else '<span class="dim">↳</span>'
            if not r.get("enabled", True):
                status = '<span class="badge off">disabled</span>'
            elif not r["healthy"]:
                status = '<span class="badge warn">backend down</span>'
            elif not r["present"]:
                status = '<span class="badge err">model missing</span>'
            elif r.get("busy"):
                status = '<span class="badge busy">busy</span>'
            else:
                status = '<span class="badge ok">routable</span>'
            ovr = ' <span class="badge info">override</span>' if r["overridden"] else ""
            row_cls = "f off" if not r.get("enabled", True) else "f"
            arows.append(
                f'<tr class="{row_cls}" data-search="{_esc(name)}"><td>{label}</td>'
                f'<td class="host">{_esc(r["backend"])}</td>'
                f'<td><code>{_esc(r["model"])}</code></td>'
                f'<td><span class="prio">{r["priority"]}</span>{ovr}</td>'
                f"<td>{status}</td></tr>"
            )
    alias_html = (
        '<div class="panel"><h2>Aliases <span class="muted">'
        "(rows in resolution order = effective priority; lower wins)</span></h2>"
        "<table><thead><tr><th>Alias</th><th>Host</th><th>Real model</th>"
        "<th>Priority</th><th>Status</th></tr></thead><tbody>" +
        ("".join(arows) or '<tr><td colspan="5" class="muted">no virtual_models configured</td></tr>') +
        "</tbody></table></div>"
    )

    # Discovered models — how a bare/direct model id routes by backend priority.
    mrows = []
    for m in models:
        parts = []
        for h in m["hosts"]:
            down = "" if h["healthy"] else ' <span class="badge warn">down</span>'
            busy = ' <span class="badge busy">busy</span>' if h.get("busy") else ""
            parts.append(f'<span class="host">{_esc(h["backend"])} '
                         f'<span class="prio">{h["priority"]}</span>{down}{busy}</span>')
        shadow = ' <span class="badge info">alias exists</span>' if m["shadowed_by_alias"] else ""
        mrows.append(f'<tr class="f"><td><code>{_esc(m["model"])}</code>{shadow}</td>'
                     f'<td>{" &nbsp; ".join(parts)}</td></tr>')
    model_html = (
        '<div class="panel"><h2>Discovered models <span class="muted">'
        "(how a bare / direct model id routes, by backend priority)</span></h2>"
        "<table><thead><tr><th>Model id</th><th>Hosts (priority)</th></tr></thead><tbody>" +
        ("".join(mrows) or '<tr><td colspan="2" class="muted">nothing discovered yet</td></tr>') +
        "</tbody></table></div>"
    )

    search = ('<div class="search"><input id="q" type="search" autocomplete="off" '
              'placeholder="filter aliases, models, hosts…">'
              '<span id="count" class="count"></span></div>'
              '<p class="muted">Reflects live health &amp; discovery — reload to refresh.</p>')

    body = search + conflict_html + alias_html + model_html
    return _doc("llm-gateway routing", "routing", body, search=True)
