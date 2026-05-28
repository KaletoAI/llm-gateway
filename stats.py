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
    cost_usd      REAL    DEFAULT 0
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
            "endpoint, status, input_tokens, output_tokens, cost_usd) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            row,
        )


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
        "status, input_tokens, output_tokens, cost_usd "
        "FROM calls ORDER BY id DESC LIMIT 50"
    )
    return _render(
        (total_count, total_cost),
        (h24_count, h24_cost),
        by_backend, by_model, by_source, recent,
    )


def _fmt_ts(ts: int) -> str:
    return datetime.fromtimestamp(ts, tz=timezone.utc).astimezone().strftime("%Y-%m-%d %H:%M:%S")


def _esc(s) -> str:
    if s is None:
        return ""
    return (str(s)
            .replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
            .replace('"', "&quot;").replace("'", "&#39;"))


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
        f"<tr><td>{_fmt_ts(ts)}</td><td>{_esc(b)}</td><td>{_esc(src)}</td>"
        f"<td>{_esc(a)}</td><td>{_esc(m)}</td><td>{_esc(ep)}</td>"
        f"<td class='{ 'ok' if 200 <= (st or 0) < 400 else 'err' }'>{st}</td>"
        f"<td>{int(dm):,} ms</td><td>{int(it or 0)}/{int(ot or 0)}</td>"
        f"<td>${(cu or 0):.5f}</td></tr>"
        for (ts, dm, b, src, a, m, ep, st, it, ot, cu) in recent
    ) or '<tr><td colspan="10" class="muted">no calls recorded yet</td></tr>'

    return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta http-equiv="refresh" content="30">
<title>llm-gateway stats</title>
<style>
* {{ box-sizing: border-box; }}
body {{ font: 14px/1.4 system-ui, -apple-system, "Segoe UI", sans-serif;
        margin: 1.5rem; color: #1c1c1c; background: #f4f5f7; }}
h1 {{ margin: 0 0 .25rem; }}
h2 {{ margin: 0 0 .75rem; font-size: 1.05rem; color: #555; }}
.muted {{ color: #888; font-size: .85rem; }}
.cards {{ display: grid; grid-template-columns: repeat(auto-fit, minmax(180px, 1fr));
          gap: 1rem; margin: 1rem 0 2rem; }}
.card {{ background: #fff; border-radius: 8px; padding: 1rem 1.25rem;
         box-shadow: 0 1px 3px rgba(0,0,0,.06); }}
.metric {{ font-size: 1.6rem; font-weight: 600; margin-top: .25rem; }}
.panel {{ background: #fff; border-radius: 8px; padding: 1rem 1.25rem;
          margin: 1rem 0; box-shadow: 0 1px 3px rgba(0,0,0,.06); }}
table {{ width: 100%; border-collapse: collapse; }}
th, td {{ padding: .4rem .5rem; text-align: left;
          border-bottom: 1px solid #eee; vertical-align: top; }}
th {{ background: #fafafa; font-weight: 600; color: #555; }}
tr:hover td {{ background: #fcfcfc; }}
td.ok {{ color: #1a7f37; font-weight: 600; }}
td.err {{ color: #cf222e; font-weight: 600; }}
code {{ background: #f1f1f1; padding: 1px 4px; border-radius: 3px; }}
</style>
</head>
<body>
<h1>llm-gateway stats</h1>
<p class="muted">Auto-refreshes every 30 s. Source override: send header
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
<table><thead><tr><th>Time</th><th>Backend</th><th>Source</th><th>Alias</th>
<th>Model</th><th>Endpoint</th><th>Status</th><th>Duration</th>
<th>In/Out</th><th>Cost</th></tr></thead>
<tbody>{rows_recent}</tbody></table>
</div>

</body>
</html>"""
