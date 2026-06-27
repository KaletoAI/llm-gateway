"""Generation / management UI — a tabbed console mounted on the main app at /ui.

Plain server-rendered (no JS / no build / no extra deps). The nav shell (`TABS` +
`_page`) is the extension point. A small component layer (`_field` / `_btn` /
`_select`) keeps every form consistent — one label width, one input height, one
button size. POST bodies are parsed by hand (`parse_qs` / minimal multipart) to
avoid a `python-multipart` dependency.

Deferred (Phase 3 hardening): admin auth / multi-user, HTMX live validation.
"""
from __future__ import annotations

import asyncio
import html
import ipaddress
import json
import logging
import os
import re
import socket
import time
from typing import Callable, Optional
from urllib.parse import parse_qs, quote, urlencode

import httpx
from fastapi import HTTPException, Request
from fastapi.responses import FileResponse, HTMLResponse, RedirectResponse

import adapters
import jobs
import stats
import store

logger = logging.getLogger(__name__)

_MODEL_EXTS = (".safetensors", ".gguf", ".ckpt", ".pt", ".pth", ".bin", ".sft", ".onnx")
_LOADER_HINTS = ("loader", "checkpoint", "unet", "clip", "vae", "lora", "gguf", "controlnet")

TABS = [
    ("dashboard", "Dashboard"), ("server", "Server"), ("backends", "Backends"),
    ("input", "Input"), ("routing", "Routing Overview"), ("mapping", "Mapping"),
    ("chatplay", "Chat Playground"), ("playground", "Image Playground"),
    ("jobs", "Image Jobs"),
    ("statistic", "Statistic"), ("users", "Users"),
]
DEFAULT_TAB = "dashboard"

# Request fields (the params that vary per generation) are NOT a fixed list — each
# alias defines its own by promoting Available fields to request fields in Mapping.
# The model stays a per-alias fixed choice (Pinned values), never a request field.


def _num(s: str):
    """Coerce a submitted string to int/float when it parses cleanly, else leave it
    a string (so free-text params like a sampler name survive untouched)."""
    s = s.strip()
    try:
        return int(s)
    except ValueError:
        try:
            return float(s)
        except ValueError:
            return s


_comfy_backends: Callable[[], list] = lambda: []
_gateway_info: Callable[[], dict] = lambda: {}
_run_generation = None
_apply_backends: Callable[[], None] = lambda: None
_llm_backends: Callable[[], list] = lambda: []
_config_chat_aliases: Callable[[], dict] = lambda: {}
_apply_chat_aliases: Callable[[], None] = lambda: None
_run_chat = None
_routing_snapshot: Callable[[], dict] = lambda: {"aliases": [], "models": [], "conflicts": []}
_server_info: Callable[[], dict] = lambda: {"effective": {}, "runtime": {}}
_apply_server_settings: Callable[[], None] = lambda: None
_apply_users: Callable[[], None] = lambda: None
_resolve_admin: Callable = lambda key: None
_ui_locked: Callable[[], bool] = lambda: False
_dashboard_snapshot: Callable[[], dict] = lambda: {}


def bind(comfy_backends: Callable[[], list], run_generation, gateway_info: Callable[[], dict],
         apply_backends: Callable[[], None], *, llm_backends: Callable[[], list] = lambda: [],
         config_chat_aliases: Callable[[], dict] = lambda: {},
         apply_chat_aliases: Callable[[], None] = lambda: None, run_chat=None,
         routing_snapshot: Callable[[], dict] = lambda: {"aliases": [], "models": [], "conflicts": []},
         server_info: Callable[[], dict] = lambda: {"effective": {}, "runtime": {}},
         apply_server_settings: Callable[[], None] = lambda: None,
         apply_users: Callable[[], None] = lambda: None,
         resolve_admin: Callable = lambda key: None,
         ui_locked: Callable[[], bool] = lambda: False,
         dashboard_snapshot: Callable[[], dict] = lambda: {}) -> None:
    global _comfy_backends, _run_generation, _gateway_info, _apply_backends
    global _llm_backends, _config_chat_aliases, _apply_chat_aliases, _run_chat, _routing_snapshot
    global _server_info, _apply_server_settings, _apply_users, _resolve_admin, _ui_locked
    global _dashboard_snapshot
    _dashboard_snapshot = dashboard_snapshot
    _comfy_backends, _run_generation = comfy_backends, run_generation
    _gateway_info, _apply_backends = gateway_info, apply_backends
    _llm_backends, _config_chat_aliases = llm_backends, config_chat_aliases
    _apply_chat_aliases, _run_chat = apply_chat_aliases, run_chat
    _routing_snapshot = routing_snapshot
    _server_info, _apply_server_settings = server_info, apply_server_settings
    _apply_users = apply_users
    _resolve_admin, _ui_locked = resolve_admin, ui_locked


# ── Shell + components ──────────────────────────────────────────────────────────

def _esc(s) -> str:
    return html.escape(str(s if s is not None else ""))


def _nav(active: str) -> str:
    links = "".join(f'<a class="{"on" if k == active else ""}" href="/ui/{k}">{_esc(label)}</a>'
                    for k, label in TABS)
    logout = ('<a href="/ui/logout" style="margin-left:auto;color:#8b97a4">Logout</a>'
              if _ui_locked() else "")
    return f'<header><span class="brand">LLM Gateway</span><nav>{links}{logout}</nav></header>'


_CSS = """
*{box-sizing:border-box}
html,body{height:100%}
body{font:14px/1.6 system-ui,-apple-system,sans-serif;margin:0;background:#0f1115;color:#d7dbe0;overflow:hidden;display:flex;flex-direction:column}
a{color:#6cb0ef}
*{scrollbar-width:thin;scrollbar-color:#39414e transparent}
::-webkit-scrollbar{width:11px;height:11px}
::-webkit-scrollbar-track{background:transparent}
::-webkit-scrollbar-thumb{background:#2d3440;border-radius:7px;border:2px solid #0f1115}
::-webkit-scrollbar-thumb:hover{background:#3d4654}
::-webkit-scrollbar-corner{background:transparent}
header{display:flex;align-items:center;background:#171a21;border-bottom:1px solid #272b33;padding:0 20px;flex:none}
.brand{font-weight:700;padding:14px 16px 14px 0;color:#e7ebf0}
nav{display:flex;flex-wrap:wrap}
nav a{color:#9aa7b4;padding:14px 14px;text-decoration:none;border-bottom:2px solid transparent;font-size:13px}
nav a:hover{color:#dce4ec;background:#1b1f27}
nav a.on{color:#fff;border-bottom-color:#3b82f6}
main{flex:1;min-height:0;overflow-y:auto;padding:18px 26px}
h2{font-size:12px;font-weight:700;text-transform:uppercase;letter-spacing:.06em;color:#7e8b99;margin:28px 0 12px;padding-bottom:7px;border-bottom:1px solid #242a33}
h2:first-child{margin-top:4px}
p.hint{color:#9aa7b4;margin:0 0 14px;max-width:62ch;line-height:1.5}
.field{display:flex;align-items:center;gap:14px;margin:10px 0}
.field>label{flex:0 0 140px;text-align:left;color:#9aa7b4;font-size:13px}
.field>.control{flex:1;min-width:0;max-width:480px;display:flex;gap:8px;align-items:center}
.control.short input,.control.short select{max-width:150px}
input,select,textarea{width:100%;background:#0c0e12;color:#dce4ec;border:1px solid #313a46;border-radius:7px;padding:0 10px;height:36px;font:inherit;outline:none}
input:focus,select:focus,textarea:focus{border-color:#3b82f6}
textarea{height:auto;min-height:150px;padding:8px 10px;font-family:ui-monospace,monospace;font-size:12px}
input[type=file]{padding:7px 10px;height:auto}
input[type=checkbox]{width:auto;height:auto;margin:0 6px 0 0;vertical-align:middle;accent-color:#2563eb}
.ckbox{display:inline-flex;align-items:center;margin-right:18px;color:#cdd5de;font-size:13px;cursor:pointer;white-space:nowrap}
.ckbox code{margin:0}
.btn{height:36px;padding:0 18px;background:#2563eb;color:#fff;border:0;border-radius:7px;font:inherit;font-weight:500;cursor:pointer;display:inline-flex;align-items:center;justify-content:center;text-decoration:none;white-space:nowrap}
.btn:hover{background:#1d4ed8}
.btn.secondary{background:#2a313c}.btn.secondary:hover{background:#343c49}
.btn.danger{background:#b4433f}.btn.danger:hover{background:#9e3a36}
.btn.sm{height:28px;padding:0 11px;font-size:12px;border-radius:6px}
.btn.icon{padding:0;width:32px;min-width:32px;font-size:15px}
.btn.sm.icon{width:28px;min-width:28px;font-size:14px}
.actions{display:flex;gap:10px;margin-top:18px;padding-left:0}
table{border-collapse:collapse;width:100%;margin:6px 0;font-size:13px}
th,td{border-bottom:1px solid #242a33;padding:8px 10px;text-align:left;vertical-align:middle;overflow-wrap:anywhere}
th{color:#7e8b99;font-weight:600;font-size:11px;text-transform:uppercase;letter-spacing:.04em;border-bottom-color:#313a46}
td .btn{margin-right:4px}
td input,td select{height:30px}
.muted{color:#6b7682}.ok{color:#5cb87f}.bad{color:#e06c6c}
code{background:#1b1f27;padding:2px 6px;border-radius:4px;font-size:12px}
.stub{color:#7e8b99;border:1px dashed #313a46;border-radius:8px;padding:22px;margin-top:8px;line-height:1.8}
img.result{max-width:512px;border:1px solid #313a46;border-radius:8px;margin:8px 0}
pre.err{white-space:pre-wrap;word-break:break-word;background:#1a1113;border:1px solid #5a2a2a;color:#f0b6b6;border-radius:8px;padding:12px 14px;margin:8px 0;max-height:340px;overflow:auto;font:12px/1.5 ui-monospace,monospace;user-select:text}
.cols{display:flex;gap:24px;align-items:flex-start}
.col{flex:1;min-width:0}
.cols{gap:0;align-items:stretch;height:100%}
.cols>.col{flex:1 1 0;min-width:0;height:100%;overflow-y:auto;padding:0 16px 18px 0}
.cols>.col+.col{border-left:1px solid #2a313c;margin-left:22px;padding-left:22px}
.bar{display:flex;align-items:center;justify-content:space-between;gap:14px;margin:0 0 8px;padding:8px 0 8px;position:sticky;top:0;z-index:10;background:#0f1115}
.bar h2{margin:0;border:0;padding:0}
td.acts{white-space:nowrap;text-align:right;width:1%}
td.acts .btn{margin:0 0 0 6px}
tr.sel td{background:#19222e}
.item{display:flex;align-items:center;justify-content:space-between;gap:10px;padding:9px 4px;border-bottom:1px solid #242a33}
.item.sel{background:#19222e}
.item-main{min-width:0}
.item-title{font-weight:600;font-size:13px}
.item-sub{color:#6b7682;font-size:12px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.item-acts{white-space:nowrap;flex:none}
.item-acts .btn{margin:0 0 0 6px}
.badge{font-size:11px;font-weight:500;padding:1px 7px;border-radius:10px;margin-left:7px;white-space:nowrap}
.badge.ok{background:#16361f;color:#5cb87f}.badge.bad{background:#3a1b1b;color:#e06c6c}.badge.muted{background:#23262d;color:#7e8b99}
.badge.warn{background:#3a2f12;color:#d8b35a}
tr.grp td{background:#13161c;font-weight:600}
.grouphdr{font-size:11px;text-transform:uppercase;letter-spacing:.6px;color:#8b97a4;font-weight:600;margin:16px 0 6px;padding-bottom:4px;border-bottom:1px solid #242a33}
.grouphdr:first-child{margin-top:4px}
.formbar{display:flex;gap:8px;align-items:center;margin:0 0 14px;padding:8px 0 12px;border-bottom:1px solid #242a33;position:sticky;top:0;z-index:10;background:#0f1115}
.formbar h2{margin:0;border:0;padding:0;flex:1}
.formwrap{max-width:560px}
.avail tr td:last-child{text-align:right;white-space:nowrap}
.avail .node{color:#9aa7b4}.avail .node code{background:#13202f}
table.pins td:nth-child(2){width:62%}
table.pins td:last-child{width:1%;white-space:nowrap;text-align:right}
table.reqf tr[draggable]{cursor:grab}
table.reqf tr.dragging{opacity:.45}
table.reqf tr[draggable]:hover{background:#13202f}
.grip{color:#5a6675;cursor:grab;user-select:none;margin-right:4px}
.tag{font-size:10px;background:#1d3a52;color:#9fd0ff;border-radius:3px;padding:1px 5px;margin-left:4px;vertical-align:middle}
textarea{height:auto;min-height:60px;padding:8px 10px;line-height:1.5}
.chatout{white-space:pre-wrap;word-break:break-word;background:#0c0e12;border:1px solid #242a33;border-radius:8px;padding:12px 14px;user-select:text;font-size:13px}
.ok-banner{background:#16361f;color:#5cb87f;border:1px solid #1f5232;border-radius:8px;padding:8px 12px;margin:8px 0}
.acctbl{max-height:360px;overflow-y:auto;border:1px solid #242a33;border-radius:8px}
.acctbl table{margin:0}
.acctbl td:first-child,.acctbl th:first-child{width:1%;text-align:center}
.acctbl thead th{position:sticky;top:0;background:#13161c;z-index:1}
.cards{display:flex;gap:14px;flex-wrap:wrap;margin:10px 0 6px}
.card{background:#13161c;border:1px solid #242a33;border-radius:10px;padding:12px 18px;min-width:130px}
.card .cnum{font-size:22px;font-weight:600;color:#e8edf2}
.card .clbl{font-size:12px;color:#8b97a4;margin-top:2px}
table.recent{font-size:12px}
"""


# Preserve each scrolling pane's position across the 303-redirect reloads that every
# inline edit action triggers — otherwise the column jumps back to the top on each
# change. Keyed per URL (same key after an action redirects back), stored in
# sessionStorage so it survives the reload but not a fresh visit.
_SCROLL_JS = ("<script>(function(){"
              "var k='scr:'+location.pathname+location.search;"
              "function t(){return [document.querySelector('main')].concat("
              "[].slice.call(document.querySelectorAll('.col'))).filter(Boolean);}"
              "try{var s=JSON.parse(sessionStorage.getItem(k)||'[]');"
              "t().forEach(function(e,i){if(s[i]!=null)e.scrollTop=s[i];});}catch(e){}"
              "var p=false;function save(){if(p)return;p=true;requestAnimationFrame(function(){p=false;"
              "try{sessionStorage.setItem(k,JSON.stringify(t().map(function(e){return e.scrollTop;})));}catch(e){}});}"
              "t().forEach(function(e){e.addEventListener('scroll',save);});"
              "window.addEventListener('beforeunload',save);"
              "})();</script>")


def _page(title: str, body: str, active: str = "", refresh: Optional[int] = None,
          nologin: bool = False) -> str:
    meta = f'<meta http-equiv="refresh" content="{int(refresh)}">' if refresh else ""
    head = "" if nologin else _nav(active)        # login page renders without the nav
    return (f'<!doctype html><html><head><meta charset="utf-8">{meta}<title>{_esc(title)} · Gateway</title>'
            f"<style>{_CSS}</style></head><body>{head}<main>{body}</main>{_SCROLL_JS}</body></html>")


def _field(label: str, control: str, short: bool = False) -> str:
    cls = "control short" if short else "control"
    return f'<div class="field"><label>{_esc(label)}</label><div class="{cls}">{control}</div></div>'


def _btn(label: str, href: str = "", kind: str = "", sm: bool = False, submit: bool = False,
         confirm: str = "", title: str = "", icon: bool = False) -> str:
    cls = "btn" + (f" {kind}" if kind else "") + (" sm" if sm else "") + (" icon" if icon else "")
    t = f' title="{_esc(title)}"' if title else ""
    onclick = f' onclick="return confirm(\'{_esc(confirm)}\')"' if confirm else ""
    if submit:
        return f'<button type="submit" class="{cls}"{t}>{_esc(label)}</button>'
    return f'<a class="{cls}" href="{_esc(href)}"{onclick}{t}>{_esc(label)}</a>'


def _icon_acts(*specs) -> str:
    """Compact icon action buttons. Each spec: (glyph, href, kind, title[, confirm])."""
    out = ""
    for s in specs:
        glyph, href, kind, ttl = s[0], s[1], s[2], s[3]
        confirm = s[4] if len(s) > 4 else ""
        out += _btn(glyph, href, kind, sm=True, icon=True, title=ttl, confirm=confirm)
    return out


def _select(name: str, options: list, selected=None) -> str:
    opts = ""
    for o in options:
        val, lbl = (o if isinstance(o, tuple) else (o, o))
        opts += f'<option value="{_esc(val)}"{" selected" if str(val) == str(selected) else ""}>{_esc(lbl)}</option>'
    return f'<select name="{_esc(name)}">{opts}</select>'


def _inp(name: str, value="", placeholder: str = "", typ: str = "text") -> str:
    return (f'<input type="{typ}" name="{_esc(name)}" value="{_esc(value)}" '
            f'placeholder="{_esc(placeholder)}">')


def _textarea(name: str, value="", rows: int = 3, placeholder: str = "") -> str:
    return (f'<textarea name="{_esc(name)}" rows="{rows}" placeholder="{_esc(placeholder)}">'
            f'{_esc(value)}</textarea>')


def _checkbox(name: str, checked: bool, label: str, title: str = "") -> str:
    ck = " checked" if checked else ""
    t = f' title="{_esc(title)}"' if title else ""
    return (f'<label class="ckbox"{t}><input type="checkbox" name="{_esc(name)}" value="1"{ck}> '
            f'{_esc(label)}</label>')


def _item(title: str, sub: str, acts: str, sel: bool = False) -> str:
    """A compact two-line list item: bold title (may contain a badge) + a muted,
    single-line truncated sub line, with right-aligned actions."""
    return (f'<div class="item{" sel" if sel else ""}"><div class="item-main">'
            f'<div class="item-title">{title}</div>'
            f'<div class="item-sub" title="{_esc(sub)}">{_esc(sub)}</div></div>'
            f'<div class="item-acts">{acts}</div></div>')


def _badge(text: str, kind: str = "muted", title: str = "") -> str:
    t = f' title="{_esc(title)}"' if title else ""
    return f'<span class="badge {kind}"{t}>{_esc(text)}</span>'


def _qp(request: Request, key: str, default: str = "") -> str:
    return request.query_params.get(key, default)


async def root_redirect():
    return RedirectResponse(f"/ui/{DEFAULT_TAB}", status_code=307)


async def ui_root():
    return RedirectResponse(f"/ui/{DEFAULT_TAB}", status_code=307)


def _inactive():
    return HTMLResponse(_page("UI", '<p class="hint">The generation store is not enabled. '
        'Set <code>image_models</code> or <code>jobs.enabled: true</code> in config.</p>'))


# ── Admin login / session guard (Phase 3 hardening) ──────────────────────────────
# /ui requires an admin login once an admin credential exists (master api_key or an
# admin user). Bootstrap-open until then. Session = a store-signed (encrypt-then-MAC)
# cookie carrying the user name + expiry; tamper-proof, no server-side session store.

_SESSION_COOKIE = "gw_session"
_SESSION_TTL = 12 * 3600


def _make_session(name: str) -> str:
    return store.encrypt_secret(json.dumps({"u": name, "exp": int(time.time()) + _SESSION_TTL}))


def _session_user(request: Request) -> Optional[str]:
    tok = request.cookies.get(_SESSION_COOKIE)
    if not tok:
        return None
    try:
        d = json.loads(store.decrypt_secret(tok) or "{}")
        return d["u"] if d.get("exp", 0) > int(time.time()) else None
    except Exception:
        return None


async def _ui_guard(request: Request, call_next):
    """Block /ui (except the login routes) without a valid admin session — but only
    once the gateway is locked (an admin credential exists)."""
    p = request.url.path
    if p.startswith("/ui") and not p.startswith("/ui/login") and _ui_locked():
        if not _session_user(request):
            return RedirectResponse(f"/ui/login?next={quote(p)}", status_code=303)
    return await call_next(request)


def _login_page(error: str = "", nxt: str = "/ui") -> str:
    err = f"<p class='bad'>{_esc(error)}</p>" if error else ""
    body = (f"<div style='max-width:360px;margin:8vh auto;text-align:left'>"
            f"<h2>Gateway login</h2><p class='hint'>Enter an <b>admin API key</b> to access the console.</p>"
            f"{err}<form action='/ui/login' method='post'>"
            f"<input type='hidden' name='next' value='{_esc(nxt)}'>"
            f"{_field('admin key', _inp('key', '', placeholder='Bearer token', typ='password'))}"
            f"<div class='field'><label></label><div class='control'>{_btn('Sign in', submit=True)}</div></div>"
            f"</form></div>")
    return body


async def login_page(request: Request):
    if not _ui_locked():
        return RedirectResponse("/ui", status_code=303)
    nxt = request.query_params.get("next", "/ui")
    return HTMLResponse(_page("Login", _login_page(nxt=nxt), active="", nologin=True))


async def login_post(request: Request):
    f = await _form(request)
    key = (f.get("key", "") or "").strip()
    nxt = f.get("next", "/ui") or "/ui"
    admin = _resolve_admin(key)
    if not admin:
        return HTMLResponse(_page("Login", _login_page("Invalid admin key.", nxt), active="", nologin=True),
                            status_code=401)
    resp = RedirectResponse(nxt if nxt.startswith("/ui") else "/ui", status_code=303)
    resp.set_cookie(_SESSION_COOKIE, _make_session(admin["name"]),
                    max_age=_SESSION_TTL, httponly=True, samesite="lax", path="/")
    logger.info(f"ui: admin '{admin['name']}' logged in")
    return resp


async def logout(request: Request):
    resp = RedirectResponse("/ui/login", status_code=303)
    resp.delete_cookie(_SESSION_COOKIE, path="/")
    return resp


# ── POST body parsing (no python-multipart) ─────────────────────────────────────

async def _form(request: Request) -> dict:
    raw = (await request.body()).decode("utf-8", "replace")
    return {k: v[-1] for k, v in parse_qs(raw, keep_blank_values=True).items()}


async def _multipart(request: Request) -> dict:
    ctype = request.headers.get("content-type", "")
    m = re.search(r"boundary=([^;]+)", ctype)
    if not m:
        return await _form(request)
    body = await request.body()
    delim = b"--" + m.group(1).strip().strip('"').encode()
    out: dict = {}
    for part in body.split(delim):
        part = part.strip(b"\r\n")
        if not part or part == b"--" or b"\r\n\r\n" not in part:
            continue
        head, _, content = part.partition(b"\r\n\r\n")
        head_s = head.decode("utf-8", "replace")
        nm = re.search(r'name="([^"]*)"', head_s)
        if nm:
            out[nm.group(1)] = content if 'filename="' in head_s else content.decode("utf-8", "replace")
    return out


# ── Discovery helpers (object_info → model dropdowns) ────────────────────────────

def _backend_url(name: str):
    for b in _comfy_backends():
        if b["name"] == name:
            return b["url"].rstrip("/")
    return None


def _is_model_field(options: list, current) -> bool:
    sample = [str(o) for o in (options[:8] if options else [])] + [str(current or "")]
    return any(s.lower().endswith(_MODEL_EXTS) for s in sample)


_OI_CACHE: dict = {}              # (backend, class) → (monotonic_ts, fields|None)
_OI_TTL = 300.0                   # node defs change rarely; 5 min keeps edits snappy


async def _fetch_oi_class(client, url: str, cls: str):
    """Fetch + parse one class's input spec from /object_info/{cls}."""
    try:
        r = await client.get(f"{url}/object_info/{cls}")
        if r.status_code != 200:
            return cls, None
        spec = r.json()[cls]["input"]
        fields = {}
        for section in ("required", "optional"):
            for fn, fspec in (spec.get(section) or {}).items():
                if isinstance(fspec, list) and fspec and isinstance(fspec[0], list):
                    fields[fn] = fspec[0]
        return cls, fields
    except Exception:
        return cls, None


async def _object_info(backend_name: str, wf: dict) -> dict:
    """Per-class /object_info for the workflow's loader nodes. Fetches the uncached
    classes **in parallel** (was sequential → slow editor open) and caches them with
    a short TTL, so re-opening an alias is instant."""
    url = _backend_url(backend_name)
    if not url:
        return {}
    classes = {n.get("class_type", "") for n in wf.values()
               if any(h in (n.get("class_type", "") or "").lower() for h in _LOADER_HINTS)}
    now = time.monotonic()
    out: dict = {}
    missing = []
    for cls in classes:
        hit = _OI_CACHE.get((backend_name, cls))
        if hit and now - hit[0] < _OI_TTL:
            if hit[1] is not None:
                out[cls] = hit[1]
        else:
            missing.append(cls)
    if missing:
        async with httpx.AsyncClient(timeout=8.0) as c:
            results = await asyncio.gather(*[_fetch_oi_class(c, url, cls) for cls in missing])
        for cls, fields in results:
            _OI_CACHE[(backend_name, cls)] = (now, fields)
            if fields is not None:
                out[cls] = fields
    return out


def _detect_model_bindings(wf: dict, oi: dict) -> list:
    out = []
    for nid, n in wf.items():
        opts_by_field = oi.get(n.get("class_type", ""), {})
        for fn, val in (n.get("inputs") or {}).items():
            opts = opts_by_field.get(fn)
            if opts is not None and _is_model_field(opts, val):
                out.append({"node": nid, "field": fn, "value": val})
    return out


def _value_control(name: str, node: str, field: str, value, wf: dict, oi: dict) -> str:
    """Render the right input widget for a pinned node field: model dropdown,
    true/false, image (placeholder/upload), or a plain text box."""
    cls = wf.get(node, {}).get("class_type", "")
    file_val = (wf.get(node, {}).get("inputs") or {}).get(field)
    cur = value if value is not None else file_val
    opts = oi.get(cls, {}).get(field)
    if opts and _is_model_field(opts, cur):
        o = list(opts)
        stale = cur not in o and o
        if cur not in o:
            o = [cur] + o
        flag = ' <span class="bad">(stale)</span>' if stale else ""
        return _select(name, o, cur) + flag
    if cls in adapters._IMG_LOADER_CLASSES and field == "image":
        o = [(adapters.UPLOAD_SENTINEL, "playground upload (8×8 if empty)"),
             (adapters.PLACEHOLDER_SENTINEL, "8×8 placeholder (always)")]
        if cur and cur not in (adapters.PLACEHOLDER_SENTINEL, adapters.UPLOAD_SENTINEL):
            o.append((cur, cur))
        return _select(name, o, cur)
    if isinstance(file_val, bool):
        return _select(name, ["true", "false"], str(cur).lower())
    return _inp(name, "" if cur is None else cur)


def _boolean_fields(wf: dict) -> list:
    return [{"node": nid, "field": fn, "value": v, "class": n.get("class_type", "")}
            for nid, n in wf.items()
            for fn, v in (n.get("inputs") or {}).items() if isinstance(v, bool)]


def _image_fields(wf: dict) -> list:
    return [{"node": nid, "field": "image", "value": (n.get("inputs") or {}).get("image", ""),
             "class": n.get("class_type", ""), "title": n.get("_meta", {}).get("title", "")}
            for nid, n in wf.items() if n.get("class_type") in adapters._IMG_LOADER_CLASSES]


# ── Tab: Backends ───────────────────────────────────────────────────────────────

def _backend_form(b: Optional[dict]) -> str:
    g = lambda k, d="": str((b or {}).get(k) if (b or {}).get(k) is not None else d)
    gb = lambda k: bool((b or {}).get(k))
    title = "Edit Backend" if b else "Add Backend"
    orig = f'<input type="hidden" name="orig" value="{_esc(_bid(b))}">' if b else ""
    return (f'<form action="/ui/backends/save" method="post">{orig}'
            f'<div class="formbar"><h2>{title}</h2>'
            f'{_btn("Save", submit=True)}{_btn("Cancel", "/ui/backends", "secondary")}</div>'
            + _field("name", _inp("name", g("name"), placeholder="evo-comfy"))
            + _field("type", _type_select(g("type", "openai")))
            + _field("url", _inp("url", g("url"), placeholder="http://192.168.8.40:8188"))
            + _field("priority", _inp("priority", g("priority", "10"), typ="number"))
            + _field("max_concurrent", _inp("max_concurrent", g("max_concurrent"), placeholder="optional, e.g. 1", typ="number"))
            + _field("api key", _inp("api_key", g("api_key"), placeholder="optional — cloud backends"))
            # LLM-only options — hidden for comfyui (none of these apply to ComfyUI)
            + f'<div id="llmopts" style="{"" if g("type", "openai") == "openai" else "display:none"}">'
            + _field("discovery filters",
                     _checkbox("chat_only", gb("chat_only"), "chat_only",
                               "keep only models with type==chat (skip image/video/embedding)")
                     + _checkbox("serverless_only", gb("serverless_only"), "serverless_only",
                                 "keep only priced models (skip dedicated-only; OpenRouter :free)"))
            + "<p class='hint'>Filters for cloud LLM catalogs (Together / OpenRouter); "
              "backends whose models carry no type/pricing are unaffected.</p>"
            + _field("list models under bare id",
                     _checkbox("local", gb("local"), "local"))
            + "<p class='hint'><b>local</b>: also list each of this backend's models under its plain "
              "id (without the <code>backend/</code> prefix). Several local backends sharing a model id "
              "then collapse into one entry that routes by priority and fails over — an implicit "
              "cross-backend alias.</p>"
            + "</div></form>")


def _type_select(current: str) -> str:
    """Backend type select that shows/hides the LLM-only options on change."""
    opts = "".join(f'<option value="{t}"{" selected" if t == current else ""}>{t}</option>'
                   for t in ("comfyui", "openai"))
    return ('<select name="type" onchange="var e=document.getElementById(\'llmopts\');'
            "if(e)e.style.display=this.value==='openai'?'':'none'\">" + opts + "</select>")


def _bid(b: dict) -> str:
    """Stable backend id = type:name (so LLM + ComfyUI may share a name)."""
    return f'{b.get("type", "openai")}:{b["name"]}'


def _parse_bid(s: str) -> tuple:
    """'type:name' → (name, type). Bare value (legacy) → (value, 'openai')."""
    t, sep, n = s.partition(":")
    return (n, t) if sep else (s, "openai")


async def backends_page(request: Request):
    qp = request.query_params
    edit_id = qp.get("edit", "")
    binfo = _gateway_info().get("backends", [])
    # editable from either source: store (full dict incl. api_key) or the live summary (config)
    editing = None
    if edit_id:
        en, et = _parse_bid(edit_id)
        editing = (store.get_backend(en, et)
                   or next((b for b in binfo if _bid(b) == edit_id), None))
    def render(b):
        bid = _bid(b)
        badge = (_badge("healthy", "ok") if b["healthy"]
                 else (_badge("disabled") if not b["enabled"] else _badge("down", "bad")))
        edit_a = ("✎", f"/ui/backends?edit={quote(bid)}", "secondary", "Edit")
        if b.get("source", "config") == "ui":
            acts = _icon_acts(edit_a, ("✕", f"/ui/backends/delete?id={quote(bid)}", "danger",
                                       "Delete", f"Remove backend {b['name']} ({b['type']})?"))
        else:
            acts = _icon_acts(edit_a)
        src = "" if b.get("source") == "ui" else " · config"
        flags = "".join(f" · {fl}" for fl in ("chat_only", "serverless_only", "local") if b.get(fl))
        sub = f"{b['type']} · {b['url']} · prio {b['priority']} · {b['models']} models{flags}{src}"
        return _item(f"{_esc(b['name'])}{badge}", sub, acts, sel=(bid == edit_id))

    # group by kind: LLM (openai-compatible) vs Image (comfyui), alphabetical within each
    binfo = sorted(binfo, key=lambda b: b["name"].lower())
    llm = [b for b in binfo if b.get("type", "openai") != "comfyui"]
    img = [b for b in binfo if b.get("type") == "comfyui"]
    items = ""
    for label, group in (("LLM", llm), ("Image", img)):
        if group:
            items += f'<div class="grouphdr">{label}</div>' + "".join(render(b) for b in group)
    items = items or "<p class='muted'>No backends.</p>"
    list_html = (f'<div class="bar"><h2>Backends</h2>{_btn("+ New", "/ui/backends?new=1")}</div>'
                 f"<p class='hint'>Edit a backend to manage it here (editing a config one creates an "
                 f"editable copy that overrides it).</p>{items}")
    if editing or qp.get("new"):
        detail = _backend_form(editing)
    else:
        detail = ("<h2>Details</h2><p class='hint'>Select a backend's <b>Edit</b>, "
                  "or <b>+ New</b> to add one.</p>")
    body = (f'<div class="cols"><div class="col">{list_html}</div>'
            f'<div class="col">{detail}</div></div>')
    return HTMLResponse(_page("Backends", body, "backends"))


async def backend_save(request: Request):
    f = await _form(request)
    name = (f.get("name", "") or "").strip()
    url = (f.get("url", "") or "").strip().rstrip("/")
    if not name or not url:
        return HTMLResponse(_page("Backends", '<p class="bad">name and url are required</p>'
            f'<div class="actions">{_btn("← Back", "/ui/backends", "secondary")}</div>', "backends"))
    new_type = (f.get("type", "openai") or "openai").strip()
    orig = (f.get("orig", "") or "").strip()
    oname, otype = _parse_bid(orig) if orig else (name, new_type)
    # start from the existing store backend (by old identity) so fields we don't render
    # (e.g. enabled) survive an edit; merge the form values over it.
    b = dict(store.get_backend(oname, otype) or store.get_backend(name, new_type) or {})
    b.update({"name": name, "type": new_type, "url": url,
              "priority": int(f.get("priority") or 10)})
    mc = (f.get("max_concurrent", "") or "").strip()
    if mc.isdigit():
        b["max_concurrent"] = int(mc)
    else:
        b.pop("max_concurrent", None)
    if (f.get("api_key", "") or "").strip():
        b["api_key"] = f["api_key"].strip()
    # boolean flags: checkbox present → True, absent → drop the key (= False)
    for flag in ("chat_only", "serverless_only", "local"):
        if f.get(flag):
            b[flag] = True
        else:
            b.pop(flag, None)
    renamed = 0
    if orig and (oname != name or otype != new_type):
        store.delete_backend(oname, otype)      # identity changed (rename / type change)
        if oname != name:
            renamed = store.rename_backend_references(oname, name)   # re-point aliases
    store.upsert_backend(b)
    _apply_backends()
    if renamed:
        _apply_chat_aliases()              # chat aliases changed → rebind the router
    logger.info(f"ui: saved backend '{name}' ({b['type']} {url})"
                + (f" — re-pointed {renamed} alias(es) from '{oname}'" if renamed else ""))
    return RedirectResponse("/ui/backends", status_code=303)


async def backend_del(request: Request):
    bid = (request.query_params.get("id", "") or request.query_params.get("name", "") or "").strip()
    if bid:
        name, typ = _parse_bid(bid)
        store.delete_backend(name, typ)
        _apply_backends()
    return RedirectResponse("/ui/backends", status_code=303)


# ── Tab: Input ──────────────────────────────────────────────────────────────────

async def input_page(request: Request):
    info = _gateway_info()
    gen = sorted(store.list_aliases().keys()) if store.is_active() else []
    llm = _llm_backends()

    def chips(items):
        return " ".join(f"<code>{_esc(i)}</code>" for i in items) or '<span class="muted">none</span>'
    # Pass-through: every discovered model is callable WITHOUT an alias — bare (routed
    # by priority across backends that expose it) or as backend/model. Grouped per
    # backend so the backend/model form is obvious.
    # model → hosting backends (a bare id routes across all of them by priority;
    # backend/model pins one). Chat models only — image models (flux.* on localai etc.)
    # are filtered out here. The backends column shows just the host names (chips), the
    # model name lives in the first column, so backend/model is easy to read.
    model_hosts: dict = {}
    for b in llm:
        for m in b.get("models", []):
            if not _is_image_model(m):
                model_hosts.setdefault(m, []).append(b["name"])
    if model_hosts:
        mrows = ""
        for m in sorted(model_hosts):
            hosts = " ".join(f'<span class="badge muted">{_esc(bn)}</span>'
                             for bn in sorted(model_hosts[m]))
            mrows += f'<tr><td><code>{_esc(m)}</code></td><td>{hosts}</td></tr>'
        models_tbl = (f'<table><tr><th>model id</th>'
                      f'<th>on backends — call bare (priority) or <code>backend/id</code></th>'
                      f'</tr>{mrows}</table>')
    else:
        models_tbl = '<p class="muted">none discovered</p>'
    body = (f"<h2>Input — what clients can call</h2>"
            f"<p class='hint'>Anything below can be the request <code>model</code>. Aliases are "
            f"shortcuts; every discovered model is <b>also callable without an alias</b> — bare "
            f"(routed by priority across its backends, with failover) or pinned as "
            f"<code>backend/model</code>.</p>"
            + _field("Chat aliases", chips(info.get("virtual_models", [])))
            + _field("Generation models", chips(gen))
            + _field("Endpoints", chips(info.get("endpoints", [])))
            + "<h2>Chat models · bare or backend/model</h2>"
            + models_tbl)
    return HTMLResponse(_page("Input", body, "input"))


# ── Tab: Routing (models + chat routing overview) ───────────────────────────────

def _route_status(r: dict) -> str:
    """Status badges for one alias→backend route from routing_snapshot()."""
    if r.get("routable"):
        out = _badge("routable", "ok")
    elif not r.get("enabled"):
        out = _badge("disabled")
    elif not r.get("healthy"):
        out = _badge("down", "bad")
    elif not r.get("present"):
        out = _badge("model absent", "warn")
    else:
        out = _badge("—")
    if r.get("busy"):
        out += _badge("busy", "warn")
    return out


def _host_chip(h: dict) -> str:
    """A backend chip in the models table, coloured by health/busy."""
    if not h.get("healthy"):
        kind, suffix = "bad", " down"
    elif h.get("busy"):
        kind, suffix = "warn", " busy"
    else:
        kind, suffix = "ok", ""
    return f'<span class="badge {kind}">{_esc(h["backend"])} · p{h.get("priority")}{suffix}</span>'


def _models_table(models: list) -> str:
    rows = ""
    for m in models:
        chips = " ".join(_host_chip(h) for h in m["hosts"]) or '<span class="muted">none</span>'
        sh = _badge("shadowed by alias", "warn") if m.get("shadowed_by_alias") else ""
        rows += f'<tr><td><code>{_esc(m["model"])}</code>{sh}</td><td>{chips}</td></tr>'
    return (f'<table><tr><th>model</th><th>backends (priority order)</th></tr>{rows}</table>'
            if rows else "<p class='muted'>none discovered yet</p>")


def _img_status(bm: Optional[dict]) -> str:
    if not bm:
        return _badge("unknown")
    if not bm.get("enabled"):
        return _badge("disabled")
    if not bm.get("healthy"):
        return _badge("down", "bad")
    return _badge("healthy", "ok")


async def routing_page(request: Request):
    snap = _routing_snapshot()
    info = _gateway_info()
    # generation aliases reference ComfyUI backends → resolve names within that type.
    bmeta = {b["name"]: b for b in info.get("backends", []) if b.get("type") == "comfyui"}

    # ── LLM ──: chat aliases → routes, LLM models → backends, collisions
    arows = ""
    for a in snap.get("aliases", []):
        arows += f'<tr class="grp"><td colspan="4">{_esc(a["alias"])}</td></tr>'
        if not a["routes"]:
            arows += '<tr><td colspan="4" class="muted">no mapped backends</td></tr>'
        for r in a["routes"]:
            prio = (f'{r["priority"]}' +
                    (" <span class='muted'>(override)</span>" if r.get("overridden") else ""))
            arows += (f'<tr><td>{_esc(r["backend"])}</td><td><code>{_esc(r["model"])}</code></td>'
                      f'<td>{prio}</td><td>{_route_status(r)}</td></tr>')
    aliases_html = ("<h2>Chat aliases → routes</h2>" + (
        f'<table><tr><th>backend</th><th>model</th><th>priority</th><th>status</th></tr>{arows}</table>'
        if arows else "<p class='muted'>No chat aliases configured.</p>"))
    # split LLM-backend models into chat vs image (name heuristic — these backends
    # carry no type metadata). Image ones move to the Image group below.
    on_llm = [m for m in snap.get("models", []) if any(h.get("type") != "comfyui" for h in m["hosts"])]
    llm_models = [m for m in on_llm if not _is_image_model(m["model"])]
    img_on_llm = [m for m in on_llm if _is_image_model(m["model"])]
    llm_models_html = ("<h2>LLM models → backends</h2>"
                       "<p class='hint'>A bare model id routes to these in priority order, failing over. "
                       "<b>shadowed by alias</b> = a chat alias of the same name intercepts the bare id.</p>"
                       + _models_table(llm_models))
    conf = snap.get("conflicts", [])
    crows = ""
    for c in conf:
        covered = ", ".join(c["covered"]) or "—"
        shadowed = (f'<span class="bad">{_esc(", ".join(c["shadowed"]))}</span> {_badge("unreachable", "bad")}'
                    if c["shadowed"] else '<span class="muted">—</span>')
        crows += f'<tr><td><code>{_esc(c["name"])}</code></td><td>{_esc(covered)}</td><td>{shadowed}</td></tr>'
    conflicts_html = ""
    if conf:
        conflicts_html = ("<h2>Alias / model collisions</h2>"
                          "<p class='hint'>An alias named like a real model shadows it. <b>shadowed</b> "
                          "backends host that exact model id but the alias doesn't map them → unreachable "
                          "by that name.</p>"
                          f'<table><tr><th>alias</th><th>covered</th><th>shadowed</th></tr>{crows}</table>')

    # ── Image ──: generation aliases → backends, image models → backends
    grows = ""
    gen_aliases = store.list_aliases() if store.is_active() else {}
    for alias, cands in sorted(gen_aliases.items()):
        grows += f'<tr class="grp"><td colspan="4">{_esc(alias)}</td></tr>'
        ordered = sorted(cands, key=lambda c: bmeta.get(c.get("backend"), {}).get("priority", 100))
        for c in ordered:
            bn = c.get("backend", "")
            bm = bmeta.get(bn)
            grows += (f'<tr><td>{_esc(bn)}</td><td>{_esc(c.get("task", ""))}</td>'
                      f'<td>{bm.get("priority", "?") if bm else "?"}</td><td>{_img_status(bm)}</td></tr>')
    gen_html = ("<h2>Generation aliases → backends</h2>"
                "<p class='hint'>Tried in backend-priority order with failover (see Image Mapping).</p>"
                + (f'<table><tr><th>backend</th><th>task</th><th>priority</th><th>status</th></tr>{grows}</table>'
                   if grows else "<p class='muted'>No generation aliases configured.</p>"))
    img_models = [m for m in snap.get("models", []) if any(h.get("type") == "comfyui" for h in m["hosts"])]
    img_models_html = ("<h2>Image models → backends</h2>"
                       "<p class='hint'>ComfyUI checkpoints/models, plus image models served by LLM "
                       "backends (e.g. flux.* on localai — matched by name, no type metadata).</p>"
                       + _models_table(img_models + img_on_llm))

    intro = ("<h2>Routing Overview</h2><p class='hint'>How requests resolve to backends, grouped by "
             "kind.</p>")
    body = (intro
            + '<div class="grouphdr">LLM</div>' + aliases_html + llm_models_html + conflicts_html
            + '<div class="grouphdr">Image</div>' + gen_html + img_models_html)
    return HTMLResponse(_page("Routing Overview", body, "routing"))


# ── Tab: Chat (LLM alias management) ────────────────────────────────────────────
# A chat alias maps to either one model id (same on every backend → stored as a
# string) or a per-backend table {backend: model | {model, priority}}. config aliases
# are the base; UI entries (this store) merge over them — exactly the router's shapes.

def _chat_summary(value) -> str:
    """One-line routing summary for the list sub-line."""
    if isinstance(value, str):
        return f"all backends → {value}"
    if isinstance(value, dict):
        parts = []
        for bn, entry in value.items():
            if isinstance(entry, dict):
                p = entry.get("priority")
                parts.append(f"{bn}→{entry.get('model')}" + (f" (p{p})" if p is not None else ""))
            else:
                parts.append(f"{bn}→{entry}")
        return " · ".join(parts) or "—"
    return "—"


def _datalist(dlid: str, models: list) -> str:
    return f'<datalist id="{_esc(dlid)}">' + "".join(f'<option value="{_esc(m)}">' for m in models) + "</datalist>"


def _dl_input(name: str, value, dlid: str, placeholder: str = "model id") -> str:
    return (f'<input type="text" name="{_esc(name)}" value="{_esc(value)}" list="{_esc(dlid)}" '
            f'placeholder="{_esc(placeholder)}">')


def _chat_value_for(alias: str) -> dict:
    """An alias's per-backend mapping for editing: the store entry if present, else
    the config value materialised to per-backend form (a config string expands to
    every LLM backend that currently exposes that model)."""
    v = store.get_chat_alias(alias)
    if v is None:
        v = _config_chat_aliases().get(alias)
    if isinstance(v, dict):
        return dict(v)
    if isinstance(v, str):
        return {b["name"]: v for b in _llm_backends() if v in b.get("models", [])}
    return {}


def _chat_new_form() -> str:
    """Minimal create form — like registering a generation alias. Backends/priorities
    are assigned in the editor afterwards."""
    llm = _llm_backends()
    all_models = sorted({m for b in llm for m in b.get("models", [])})
    bopts = [b["name"] for b in llm] or [("", "(no LLM backends)")]
    return ('<form action="/ui/chat/create" method="post">'
            f'<div class="formbar"><h2>New Chat Alias</h2>{_btn("Create", submit=True)}'
            f'{_btn("Cancel", "/ui/mapping", "secondary")}</div>'
            + _field("alias name", _inp("alias", placeholder="fast"), short=True)
            + _field("backend", _select("backend", bopts), short=True)
            + _field("model", _dl_input("model", "", "cm_all"), short=True)
            + "<p class='hint'>Pick a first backend + model. Assign more backends and "
              "priority overrides after creating.</p>"
            + "</form>" + _datalist("cm_all", all_models))


def _chat_editor(alias: str) -> str:
    """Editor for an existing alias — same logic as the Mapping editor: a list of
    assigned backends, each with a model and an optional priority override; add via
    dropdown, remove via ✕. Model/priority save on Save; add/remove are immediate."""
    llm = _llm_backends()
    meta = {b["name"]: b for b in llm}
    assigned = _chat_value_for(alias)
    rows, dls = "", ""
    for i, (bn, entry) in enumerate(assigned.items()):
        model = entry.get("model", "") if isinstance(entry, dict) else entry
        prio = entry.get("priority") if isinstance(entry, dict) else None
        b, dlid = meta.get(bn, {}), f"cm_{i}"
        off = "" if b.get("enabled", True) else " <span class='muted'>(disabled)</span>"
        head = f"{_esc(bn)}{off}<br><span class='muted'>global prio {b.get('priority', '?')}</span>"
        rm = (_btn("✕", f"/ui/chat/bdel?alias={_esc(alias)}&backend={_esc(bn)}", "danger",
                   sm=True, icon=True, title="Remove this backend")
              if len(assigned) > 1 else "<span class='muted' title='alias needs ≥1 backend'>—</span>")
        rows += (f"<tr><td>{head}</td><td>{_dl_input('model__' + bn, model, dlid)}</td>"
                 f"<td>{_inp('prio__' + bn, '' if prio is None else prio, typ='number')}</td>"
                 f"<td class='acts'>{rm}</td></tr>")
        dls += _datalist(dlid, b.get("models", []))
    rows = rows or "<tr><td colspan=4 class='muted'>no backends — add one below</td></tr>"
    add_opts = [b["name"] for b in llm if b["name"] not in assigned]
    add_sel = ""
    if add_opts:
        opts = "".join(f"<option>{_esc(o)}</option>" for o in add_opts)
        add_sel = ('<select class="addsel" onchange="if(this.value)location.href=\'/ui/chat/badd?alias='
                   f"{_esc(alias)}&amp;backend='+encodeURIComponent(this.value)\">"
                   f'<option value="">+ Add backend…</option>{opts}</select>')
    return ('<form action="/ui/chat/save" method="post">'
            f'<input type="hidden" name="orig" value="{_esc(alias)}">'
            f'<div class="formbar"><h2>Edit Chat Alias</h2>{_btn("Save", submit=True)}'
            f'{_btn("Cancel", "/ui/mapping", "secondary")}</div>'
            + _field("alias name", _inp("alias", alias, placeholder="fast"), short=True)
            + "<h2>Backends</h2>"
            + "<p class='hint'>Assign backends to this alias, pick the model on each, and optionally "
              "override that backend's global priority for this alias only. Tried in priority order "
              "with failover.</p>"
            + f"<table class='pins'><tr><th>backend</th><th>model</th><th>priority</th><th></th></tr>{rows}</table>"
            + add_sel
            + "</form>" + dls)


async def chat_create(request: Request):
    f = await _form(request)
    alias = (f.get("alias", "") or "").strip()
    backend = (f.get("backend", "") or "").strip()
    model = (f.get("model", "") or "").strip()
    if not alias or not backend:
        return RedirectResponse("/ui/mapping?cnew=1", status_code=303)
    store.upsert_chat_alias(alias, {backend: model})
    _apply_chat_aliases()
    logger.info(f"ui: chat alias '{alias}' created → {backend}/{model or '(no model)'}")
    return RedirectResponse(f"/ui/mapping?cedit={alias}", status_code=303)


async def chat_save(request: Request):
    f = await _form(request)
    alias = (f.get("alias", "") or "").strip()
    orig = (f.get("orig", "") or "").strip()
    value = {}                                # one entry per rendered backend row
    for key in f:
        if not key.startswith("model__"):
            continue
        bn = key[len("model__"):]
        m = (f.get(key) or "").strip()
        p = (f.get(f"prio__{bn}", "") or "").strip()
        if p:
            try:
                value[bn] = {"model": m, "priority": int(p)}
            except ValueError:
                value[bn] = m
        else:
            value[bn] = m
    if not alias or not value:
        return RedirectResponse(f"/ui/mapping?cedit={orig}" if orig else "/ui/mapping", status_code=303)
    if orig and orig != alias and store.get_chat_alias(orig) is not None:
        store.delete_chat_alias(orig)         # renamed a store entry → move it
    store.upsert_chat_alias(alias, value)
    _apply_chat_aliases()
    logger.info(f"ui: chat alias '{alias}' = {value}")
    return RedirectResponse("/ui/mapping", status_code=303)


async def chat_badd(request: Request):
    """Assign a backend to an alias (immediate, like the Mapping editor's + Add)."""
    alias, backend = _qp(request, "alias"), _qp(request, "backend")
    valid = {b["name"] for b in _llm_backends()}
    cur = _chat_value_for(alias)
    if backend in valid and backend not in cur:
        cur[backend] = ""                     # model filled in the editor, then Save
        store.upsert_chat_alias(alias, cur)
        _apply_chat_aliases()
    return RedirectResponse(f"/ui/mapping?cedit={alias}", status_code=303)


async def chat_bdel(request: Request):
    """Unassign a backend from an alias (an alias keeps at least one)."""
    alias, backend = _qp(request, "alias"), _qp(request, "backend")
    cur = _chat_value_for(alias)
    if backend in cur and len(cur) > 1:
        del cur[backend]
        store.upsert_chat_alias(alias, cur)
        _apply_chat_aliases()
    return RedirectResponse(f"/ui/mapping?cedit={alias}", status_code=303)


async def chat_del(request: Request):
    alias = (request.query_params.get("alias", "") or "").strip()
    if alias:
        store.delete_chat_alias(alias)
        _apply_chat_aliases()
        logger.info(f"ui: chat alias '{alias}' deleted")
    return RedirectResponse("/ui/mapping", status_code=303)


# ── Tab: Chat Playground ────────────────────────────────────────────────────────

# Heuristic to spot image-generation models served by an OpenAI-compatible backend
# (e.g. localai exposes flux.* with no `type` field). Name-based, since these backends
# carry no metadata to classify by — extend the hint list as needed.
_IMG_MODEL_HINTS = ("flux", "stable-diffusion", "sdxl", "sd3", "sd-", "dall-e", "dalle",
                    "pixart", "kandinsky", "playground-v", "kolors", "wan2", "hidream")


def _is_image_model(mid: str) -> bool:
    m = (mid or "").lower()
    return any(h in m for h in _IMG_MODEL_HINTS)


def _chat_models() -> list:
    """Everything callable as a chat `model`: aliases first, then bare model ids
    (priority-routed) and backend/model forms — feeds the model datalist."""
    aliases = list(_gateway_info().get("virtual_models", []))
    llm = _llm_backends()
    bare = sorted({m for b in llm for m in b.get("models", [])})
    prefixed = sorted(f"{b['name']}/{m}" for b in llm for m in b.get("models", []))
    out, seen = [], set()
    for x in aliases + bare + prefixed:
        if x not in seen:
            seen.add(x)
            out.append(x)
    return out


def _chatplay_form(vals: dict) -> str:
    v = lambda k: vals.get(k, "")
    return ('<form action="/ui/chatplay/send" method="post">'
            f'<div class="formbar"><h2>Chat Playground</h2>{_btn("Send", submit=True)}</div>'
            + _field("model", _dl_input("model", v("model"), "cpmodels", "alias or model id"), short=True)
            + _field("system", _textarea("system", v("system"), 2, "optional system prompt"))
            + _field("message", _textarea("user", v("user"), 6, "your message"))
            + _field("max tokens", _inp("max_tokens", v("max_tokens"), typ="number"), short=True)
            + _field("temperature", _inp("temperature", v("temperature"), typ="number"), short=True)
            + "<p class='hint'>Non-streaming. Routes by priority with failover, exactly like the API.</p>"
            + "</form>" + _datalist("cpmodels", _chat_models()))


def _chatplay_body(vals: dict, result_html: str) -> str:
    return (f'<div class="cols"><div class="col">{_chatplay_form(vals)}</div>'
            f'<div class="col">{result_html}</div></div>')


def _chat_result_html(res: dict) -> str:
    data, status = res.get("response") or {}, res.get("status")
    meta = (f"backend <b>{_esc(res.get('backend'))}</b> · model {_esc(res.get('model'))} · "
            f"HTTP {status}")
    if status != 200:
        return (f"<h2>Response</h2><p class='bad'>{meta}</p>"
                f"<pre class='err'>{_esc(json.dumps(data, indent=2)[:4000])}</pre>")
    try:
        content = data["choices"][0]["message"]["content"]
    except Exception:
        content = json.dumps(data, indent=2)[:4000]
    usage = data.get("usage") or {}
    utxt = (f" · tokens {usage.get('prompt_tokens', '?')}+{usage.get('completion_tokens', '?')}"
            if usage else "")
    return (f"<h2>Response</h2><p class='muted'>{meta}{utxt}</p>"
            f"<div class='chatout'>{_esc(content)}</div>")


_CHATPLAY_KEYS = ("model", "system", "user", "max_tokens", "temperature")


async def chatplay_page(request: Request):
    vals = {k: request.query_params.get(k, "") for k in _CHATPLAY_KEYS}
    result_html = "<h2>Response</h2><p class='hint'>Send a message to see the reply here.</p>"
    return HTMLResponse(_page("Chat Playground", _chatplay_body(vals, result_html), "chatplay"))


async def chatplay_send(request: Request):
    if _run_chat is None:
        raise HTTPException(503, "chat not wired")
    f = await _form(request)
    vals = {k: (f.get(k, "") or "") for k in _CHATPLAY_KEYS}
    model, user = vals["model"].strip(), vals["user"].strip()
    if not model or not user:
        result = "<h2>Response</h2><p class='bad'>model and message are required</p>"
        return HTMLResponse(_page("Chat Playground", _chatplay_body(vals, result), "chatplay"))
    messages = []
    if vals["system"].strip():
        messages.append({"role": "system", "content": vals["system"]})
    messages.append({"role": "user", "content": vals["user"]})
    params = {}
    if vals["max_tokens"].strip():
        try:
            params["max_tokens"] = int(vals["max_tokens"])
        except ValueError:
            pass
    if vals["temperature"].strip():
        try:
            params["temperature"] = float(vals["temperature"])
        except ValueError:
            pass
    try:
        res = await _run_chat(model, messages, params)
        result = _chat_result_html(res)
    except HTTPException as e:
        result = f"<h2>Response</h2><p class='bad'>Error {e.status_code}: {_esc(e.detail)}</p>"
    return HTMLResponse(_page("Chat Playground", _chatplay_body(vals, result), "chatplay"))


# ── Tab: Mapping ────────────────────────────────────────────────────────────────

def _register_form() -> str:
    backend_opts = [b["name"] for b in _comfy_backends()] or [("", "(no comfyui backends)")]
    return ('<form action="/ui/mapping/register" method="post" enctype="multipart/form-data">'
            f'<div class="formbar"><h2>Register Workflow</h2>{_btn("Register", submit=True)}'
            f'{_btn("Cancel", "/ui/mapping", "secondary")}</div>'
            "<p class='hint'>The gateway <b>owns</b> the API JSON once registered — independent of "
            "later ComfyUI-GUI edits. You'll map fields after registering.</p>"
            + _field("alias", _inp("alias", placeholder="flux"))
            + _field("backend", _select("backend", backend_opts))
            + _field("task", _inp("task", "text2img"))
            + _field("API JSON file", '<input type="file" name="workflow_file" accept=".json,application/json">')
            + _field("…or share path", _inp("workflow_path", placeholder="/mnt/share/flux_api.json"))
            + "</form>")


def _mapping_list(cedit: str, iedit: str) -> str:
    """The shared left column: chat aliases and generation aliases, grouped, with
    their own edit targets (?cedit= for chat, ?edit= for image)."""
    # Chat group (config + UI-managed, merged)
    cfg, ui = _config_chat_aliases(), store.list_chat_aliases()
    chat_items = ""
    for name in sorted(set(cfg) | set(ui)):
        in_ui = name in ui
        val = ui.get(name) if in_ui else cfg.get(name)
        src = (_badge("ui", "ok", "Defined/edited in this UI (stored in the gateway; overrides config.yaml)")
               if in_ui else
               _badge("config", "muted", "From config.yaml (read-only base; Edit creates a UI override)"))
        specs = [("✎", f"/ui/mapping?cedit={_esc(name)}", "secondary", "Edit")]
        if in_ui:
            lbl = "Delete override (revert to config)" if name in cfg else f"Delete {name}?"
            specs.append(("✕", f"/ui/chat/delete?alias={_esc(name)}", "danger", "Delete", lbl))
        chat_items += _item(f"{_esc(name)} {src}", _chat_summary(val), _icon_acts(*specs),
                            sel=(name == cedit))
    chat_items = chat_items or "<p class='muted'>No chat aliases — + Chat alias.</p>"
    # Image group (generation aliases)
    img_items = ""
    for alias, cands in store.list_aliases().items():
        c = cands[0]
        mapped = ", ".join((c.get("mapping") or {}).keys()) or "auto"
        backends = ", ".join(x.get("backend", "") for x in cands)
        acts = _icon_acts(
            ("✎", f"/ui/mapping?edit={_esc(alias)}", "secondary", "Edit"),
            ("⧉", f"/ui/mapping/copy?alias={_esc(alias)}", "secondary", "Copy"),
            ("✕", f"/ui/mapping/delete?alias={_esc(alias)}", "danger", "Delete", f"Delete {alias}?"))
        img_items += _item(_esc(alias), f"{backends} · {c.get('task')} · {mapped}", acts,
                           sel=(alias == iedit))
    img_items = img_items or "<p class='muted'>No workflows — + Workflow.</p>"
    bar = ('<div class="bar"><h2>Mapping</h2>'
           f'<div style="display:flex;gap:8px">{_btn("+ Chat alias", "/ui/mapping?cnew=1", "secondary")}'
           f'{_btn("+ Workflow", "/ui/mapping?new=1")}</div></div>')
    legend = ("<p class='hint' style='margin:2px 0 6px'>"
              + _badge("config") + " from config.yaml · "
              + _badge("ui", "ok") + " created/edited here (overrides config)</p>")
    return (bar + '<div class="grouphdr">Chat</div>' + legend + chat_items
            + '<div class="grouphdr">Image</div>' + img_items)


async def mapping_page(request: Request):
    if not store.is_active():
        return _inactive()
    qp = request.query_params
    cedit, iedit = qp.get("cedit", ""), qp.get("edit", "")
    list_html = _mapping_list(cedit, iedit)

    def cols(*panels):
        return ('<div class="cols">'
                + "".join(f'<div class="col">{p}</div>' for p in panels) + "</div>")

    chat_names = set(_config_chat_aliases()) | set(store.list_chat_aliases())
    if cedit and cedit in chat_names:
        body = cols(list_html, _chat_editor(cedit))          # chat editor (2 cols)
    elif qp.get("cnew"):
        body = cols(list_html, _chat_new_form())
    elif iedit and store.get(iedit):
        editor, available = await _alias_editor(iedit)        # image editor (3 cols)
        body = cols(list_html, editor, available)
    elif qp.get("new"):
        body = cols(list_html, _register_form())
    else:
        detail = ("<h2>Details</h2><p class='hint'>Pick an alias to <b>Edit</b>, or add a "
                  "<b>+ Chat alias</b> / <b>+ Workflow</b>.</p>")
        body = cols(list_html, detail)
    return HTMLResponse(_page("Mapping", body, "mapping"))


async def register_post(request: Request):
    f = await _multipart(request)
    alias = str(f.get("alias", "")).strip()
    backend = str(f.get("backend", "")).strip()
    task = (str(f.get("task", "")).strip() or "text2img")
    upload = f.get("workflow_file")
    path = str(f.get("workflow_path", "")).strip()

    def err(msg):
        return HTMLResponse(_page("Register", f'<p class="bad">{_esc(msg)}</p>'
                            f'<div class="actions" style="padding-left:0">{_btn("← Back", "/ui/mapping", "secondary")}</div>', "mapping"))
    if not alias or not backend:
        return err("alias and backend are required")
    try:
        if isinstance(upload, (bytes, bytearray)) and upload.strip():
            wf = json.loads(upload.decode("utf-8"))
        elif path:
            with open(path) as fh:
                wf = json.load(fh)
        else:
            return err("upload an API JSON file or give a share path")
    except Exception as e:
        return err(f"invalid workflow JSON: {e}")
    if not isinstance(wf, dict):
        return err("workflow JSON must be an object of nodes (API format)")
    oi = await _object_info(backend, wf)
    cand = {"backend": backend, "task": task, "workflow_json": wf,
            "mapping": adapters.suggest_mapping(wf), "fixed": _detect_model_bindings(wf, oi)}
    store.upsert(alias, [cand])
    logger.info(f"ui: registered '{alias}' → {backend} ({len(wf)} nodes, {len(cand['fixed'])} model slots)")
    return RedirectResponse(f"/ui/mapping?edit={alias}", status_code=303)


async def update_workflow(request: Request):
    """Replace an existing alias's workflow_json, **keeping** mapping + fixed +
    backends/retries. Lets the user re-export a tweaked ComfyUI workflow without
    redoing the mapping — only bindings whose node id changed need re-pointing."""
    f = await _multipart(request)
    alias = str(f.get("alias", "")).strip()
    cands = store.get(alias)

    def err(msg):
        return HTMLResponse(_page("Update workflow", f'<p class="bad">{_esc(msg)}</p>'
                            f'<div class="actions" style="padding-left:0">'
                            f'{_btn("← Back", f"/ui/mapping?edit={quote(alias)}", "secondary")}</div>', "mapping"))
    if not cands:
        return err(f"alias '{alias}' not found")
    upload = f.get("workflow_file")
    path = str(f.get("workflow_path", "")).strip()
    try:
        if isinstance(upload, (bytes, bytearray)) and upload.strip():
            wf = json.loads(upload.decode("utf-8"))
        elif path:
            with open(path) as fh:
                wf = json.load(fh)
        else:
            return err("upload an API JSON file or give a share path")
    except Exception as e:
        return err(f"invalid workflow JSON: {e}")
    if not isinstance(wf, dict):
        return err("workflow JSON must be an object of nodes (API format)")
    base_mapping = cands[0].get("mapping") or {}
    for c in cands:
        c["workflow_json"] = wf                          # workflow + mapping are backend-independent →
        c["mapping"] = base_mapping                      # keep synced; `fixed` stays per-backend
    store.upsert(alias, cands)
    mapping = base_mapping
    stale = [p for p, m in mapping.items() if (m or {}).get("node") not in wf]
    logger.info(f"ui: workflow updated for '{alias}' ({len(wf)} nodes); {len(stale)} stale binding(s): {stale}")
    return RedirectResponse(f"/ui/mapping?edit={quote(alias)}", status_code=303)


def _reorder_js(alias: str) -> str:
    """Vanilla drag-to-reorder for the request-fields rows. On drop, if the order
    changed, persist it via /ui/mapping/field-order (one ?order= per param) and the
    redirect reloads the editor — the same order then drives the Playground."""
    a = json.dumps(alias)        # safe JS string literal
    return ("<script>(function(){"
            "var tb=document.getElementById('reqfields');if(!tb)return;"
            "function ord(){return [].map.call(tb.querySelectorAll('tr[data-p]'),"
            "function(r){return r.getAttribute('data-p')});}"
            "var start=ord().join('\\u0001'),drag=null;"
            "tb.querySelectorAll('tr[draggable]').forEach(function(tr){"
            "tr.addEventListener('dragstart',function(){drag=tr;tr.classList.add('dragging');});"
            "tr.addEventListener('dragend',function(){tr.classList.remove('dragging');});"
            "tr.addEventListener('dragover',function(e){e.preventDefault();"
            "if(!drag||drag===tr)return;var b=tr.getBoundingClientRect();"
            "tb.insertBefore(drag,(e.clientY-b.top)/b.height>0.5?tr.nextSibling:tr);});"
            "tr.addEventListener('drop',function(e){e.preventDefault();"
            "if(ord().join('\\u0001')===start)return;"
            "location.href='/ui/mapping/field-order?alias='+encodeURIComponent(" + a + ")+"
            "'&'+ord().map(function(p){return 'order='+encodeURIComponent(p);}).join('&');});"
            "});})();</script>")


def _backends_section(alias: str, cands: list) -> str:
    """Allowed backends for an alias — a flat list (no primary/fallback). They are
    tried in **backend-priority** order; on error the job runner moves to the next.
    Add/remove only; adding copies the existing workflow + mapping onto that backend."""
    prio = {b["name"]: b.get("priority", 100) for b in _comfy_backends()}
    used = [c.get("backend") for c in cands]
    rows = ""
    for bn in sorted(used, key=lambda n: prio.get(n, 100)):
        p = prio.get(bn)
        badge = f" <span class='muted'>prio {p}</span>" if p is not None else ""
        rm = (_btn("✕", f"/ui/mapping/cand-del?alias={_esc(alias)}&backend={_esc(bn)}",
                   "danger", sm=True, icon=True, title="Remove this backend")
              if len(used) > 1 else "<span class='muted' title='an alias needs ≥1 backend'>—</span>")
        rows += f"<tr><td>{_esc(bn)}{badge}</td><td class='acts'>{rm}</td></tr>"
    add_opts = [b["name"] for b in _comfy_backends() if b["name"] not in used]
    add_sel = ""
    if add_opts:
        opts = "".join(f"<option>{_esc(o)}</option>" for o in add_opts)
        add_sel = ('<select class="addsel" onchange="if(this.value)location.href=\'/ui/mapping/cand-add?alias='
                   f"{_esc(alias)}&amp;backend='+encodeURIComponent(this.value)\">"
                   f'<option value="">+ Add backend…</option>{opts}</select>')
    return (f"<table class='pins'><tr><th>allowed backend</th><th></th></tr>{rows}</table>{add_sel}")


async def _alias_editor(alias: str) -> str:
    """The alias editor as a single-column fragment for the master-detail right
    side (request fields + pinned values in one form, available fields below)."""
    cands = store.get(alias)
    if not cands:
        return f'<p class="bad">alias \'{_esc(alias)}\' not found</p>'
    cand = cands[0]
    wf = cand.get("workflow_json")
    if wf is None and cand.get("workflow"):
        try:
            with open(cand["workflow"]) as fh:
                wf = json.load(fh)
        except Exception:
            wf = {}
    wf = wf or {}
    oi = await _object_info(cand.get("backend", ""), wf)
    mapping = cand.get("mapping") or {}
    fixed = cand.get("fixed") or []
    mapped = ({(m["node"], m["field"]) for m in mapping.values()}
              | {(b["node"], b["field"]) for b in fixed})

    def dl(qs):
        return _btn("✕", f"/ui/mapping/field-del?alias={_esc(alias)}&{qs}", "danger", sm=True,
                    icon=True, title="Remove")

    # LEFT — request fields: one row PER MAPPED param (dynamic — promoted from the
    # Available fields list via →; NOT a fixed list). Rendered in mapping order (NOT
    # sorted) so drag-to-reorder sticks; that order drives the Playground. node/field
    # stay editable, ∅ clears the workflow default, ✕ removes the param.
    req_rows = ""
    for p in mapping:
        m = mapping.get(p) or {}
        node, fld = m.get("node", ""), m.get("field", "")
        is_img = adapters.is_image_field(wf, node)
        cur = (wf.get(node, {}).get("inputs") or {}).get(fld)
        cur_disp = ("image upload" if is_img else
                    "(linked)" if isinstance(cur, list) else ("" if cur is None else str(cur)))
        tag = " <span class='tag'>image</span>" if is_img else ""
        if node and node not in wf:                      # node vanished after a workflow update
            tag += " <span class='badge bad' title='this node no longer exists in the workflow'>stale</span>"
        actions = ((_btn("∅", f"/ui/mapping/field-clear?alias={_esc(alias)}&param={_esc(p)}",
                         "secondary", sm=True, icon=True, title="Clear the workflow default")
                    if not is_img else "")
                   + dl("param=" + _esc(p)))
        req_rows += (f'<tr draggable="true" data-p="{_esc(p)}">'
                     f"<td><span class='grip' title='Drag to reorder'>⠿</span> {_esc(p)}{tag}</td>"
                     f"<td>{_inp('label__' + p, m.get('label', ''), placeholder=p)}</td>"
                     f"<td>{_inp('node__' + p, node)}</td>"
                     f"<td>{_inp('field__' + p, fld)}</td>"
                     f"<td class='muted'>{_esc(cur_disp[:60])}</td>"
                     f"<td class='acts'>{actions}</td></tr>")
    req_rows = req_rows or ("<tr><td colspan=6 class='muted'>none yet — promote a field "
                            "from Available fields below (→)</td></tr>")

    # Pinned values as per-backend tabs — SAME layout in every tab (.ovrow). The
    # PRIMARY (first backend) tab is the editor: value + ✕ delete per slot. Extra
    # backends override only the VALUE (no delete; the slot set is shared). A value
    # equal to the primary's is flagged "inherited" and dimmed; a differing one "override".
    async def _pin_tab(c, is_primary, oi_bn):
        rows = ""
        for b in fixed:
            nid, fld = str(b["node"]), str(b["field"])
            cls = wf.get(nid, {}).get("class_type", "")
            if is_primary:
                cur, name, inherited = b.get("value"), f"fixed__{nid}__{fld}", False
                acts = f"<span class='ovact'>{dl('node=' + _esc(nid) + '&field=' + _esc(fld))}</span>"
            else:
                cv = next((x.get("value") for x in (c.get("fixed") or [])
                           if str(x.get("node")) == nid and str(x.get("field")) == fld), None)
                inherited = cv is None or cv == b.get("value")
                cur, name, acts = (cv if cv is not None else b.get("value")), \
                    "ovr__" + str(c.get("backend")) + "__" + nid + "__" + fld, ""
            ctl = _value_control(name, nid, fld, cur, wf, oi_bn)
            tag = "" if is_primary else (" <span class='ovtag inh'>inherited</span>" if inherited
                                         else " <span class='ovtag set'>override</span>")
            title = wf.get(nid, {}).get("_meta", {}).get("title", "")
            ttl_html = f' <span class="ntitle">“{_esc(title)}”</span>' if title else ""
            rows += (f'<div class="ovrow{" inherited" if inherited else ""}">'
                     f'<label><code>{_esc(nid)}</code> {_esc(cls)}{ttl_html} '
                     f'<span class="muted">· {_esc(fld)}</span>{tag}{acts}</label>{ctl}</div>')
        return rows or "<p class='muted'>none pinned — add from Available fields below</p>"

    primary_rows = await _pin_tab(cands[0], True, oi)
    if len(cands) > 1:
        bn0 = str(cands[0].get("backend"))
        tabs = "".join(
            f'<button type="button" class="ptab{" on" if i == 0 else ""}" '
            f"onclick=\"pinTab(this,'{_esc(str(c.get('backend')))}')\">{_esc(str(c.get('backend')))}</button>"
            for i, c in enumerate(cands))
        panels = f'<div class="ppanel" data-pt="{_esc(bn0)}">{primary_rows}</div>'
        for c in cands[1:]:
            bn = str(c.get("backend"))
            oi_bn = await _object_info(bn, wf) or oi      # override backend's own models; fall back to primary
            rows = await _pin_tab(c, False, oi_bn)
            panels += (f'<div class="ppanel" data-pt="{_esc(bn)}" style="display:none">'
                       f"<p class='hint'>Models/values installed on <b>{_esc(bn)}</b> "
                       f"(slots from primary <b>{_esc(bn0)}</b>; “inherited” = same as primary).</p>{rows}</div>")
        pinned_block = (f"<h2>Pinned values <span class='muted' style='font-weight:normal'>— tab per backend</span></h2>"
                        f'<div class="ptabs">{tabs}</div>{panels}')
    else:
        pinned_block = f"<h2>Pinned values</h2><div class='ppanel'>{primary_rows}</div>"
    pin_extra = ("<style>.ptabs{display:flex;gap:4px;margin:10px 0 0;flex-wrap:wrap}"
                 ".ptab{padding:6px 14px;background:#0c0e12;border:1px solid #242a33;border-bottom:none;"
                 "border-radius:8px 8px 0 0;color:#8b97a4;cursor:pointer;font:inherit}"
                 ".ptab.on{color:#dce4ec;background:#11151b;border-color:#3b82f6}"
                 ".ppanel{border:1px solid #242a33;border-radius:0 8px 8px 8px;padding:10px 12px}"
                 ".ovrow{margin:9px 0}.ovrow label{display:block;font-size:12px;color:#8b97a4;margin-bottom:3px}"
                 ".ovrow input,.ovrow select{width:100%;max-width:560px;box-sizing:border-box}"
                 ".ovrow.inherited{opacity:.5}.ovrow.inherited:focus-within{opacity:1}"
                 ".ovact{float:right}.ovtag{font-size:10px;padding:1px 6px;border-radius:6px;margin-left:6px}"
                 ".ovtag.inh{background:#1a1f27;color:#7e8b99}.ovtag.set{background:#15301f;color:#7fd6a0}"
                 ".ntitle{color:#cbd4de;font-style:italic}</style>"
                 "<script>function pinTab(b,p){var f=b.closest('form');"
                 "f.querySelectorAll('.ptab').forEach(function(x){x.classList.toggle('on',x===b);});"
                 "f.querySelectorAll('.ppanel').forEach(function(x){x.style.display="
                 "x.getAttribute('data-pt')===p?'':'none';});}</script>")

    retries = next((c.get("retries") for c in cands if c.get("retries") not in (None, "")), "")
    form = (f'<form action="/ui/mapping/update" method="post"><input type="hidden" name="alias" value="{_esc(alias)}">'
            f'<div class="formbar"><h2 style="margin:0">{_esc(alias)}</h2>'
            f'{_btn("Save", submit=True)}{_btn("Cancel", "/ui/mapping", "secondary")}</div>'
            + _field("alias name", _inp("new_alias", alias), short=True)
            + '<h2 style="margin-top:18px">Update workflow</h2>'
            + "<p class='hint'>Replace the ComfyUI API JSON — request fields + pinned values are kept; "
              "bindings whose node vanished are flagged <span class='badge bad'>stale</span> in Request fields.</p>"
            + _field("API JSON", '<input type="file" name="workflow_file" accept="application/json,.json">')
            + _field("…or share path", _inp("workflow_path", "", placeholder="/mnt/share/flux_api.json"))
            + '<div class="field"><label></label><div class="control">'
              '<button class="btn secondary" formaction="/ui/mapping/update-workflow" '
              'formenctype="multipart/form-data">Update workflow</button></div></div>'
            + "<h2>Backends</h2>"
            + "<p class='hint'>Allowed backends for this alias — tried in backend-priority order; "
              "on a connection error the next one is used.</p>"
            + _backends_section(alias, cands)
            + _field("retries", _inp("retries", str(retries), typ="number"), short=True)
            + "<p class='hint'>Backends to try after the first on error. Blank = try all eligible · 0 = no failover.</p>"
            + f'<h2>Request fields <span class="muted" style="font-weight:normal">— drag ⠿ to set Playground order</span></h2>'
            "<p class='hint'>label overrides the Playground label / external API field name (blank = param).</p>"
            f'<table class="reqf"><thead><tr><th>param</th>'
            f'<th title="Playground label / API field name (blank = param)">label</th>'
            f'<th>node</th><th>field</th>'
            f'<th title="workflow default value">=</th><th></th></tr></thead>'
            f'<tbody id="reqfields">{req_rows}</tbody></table>'
            + pinned_block
            + '</form>' + pin_extra + _reorder_js(alias))

    # available scalar fields (not yet mapped) → Add — stacked below the form
    avail = ""
    for nid, n in sorted(wf.items(), key=lambda kv: (len(kv[0]), kv[0])):
        arows = ""
        for f2, v2 in (n.get("inputs") or {}).items():
            if isinstance(v2, list) or (nid, f2) in mapped:    # links / already-mapped → skip
                continue
            add = _btn("+", f"/ui/mapping/field-add?alias={_esc(alias)}&node={_esc(nid)}&field={_esc(f2)}",
                       "secondary", sm=True, icon=True, title="Add as pinned value")
            req_btn = _btn("→", f"/ui/mapping/field-map?alias={_esc(alias)}&node={_esc(nid)}"
                           f"&field={_esc(f2)}", "secondary", sm=True, icon=True,
                           title="Add as request field")
            fopts = oi.get(n.get("class_type", ""), {}).get(f2)   # discovery hint: dropdown? bool?
            if isinstance(fopts, list) and _is_model_field(fopts, v2):
                fhint = f" <span class='badge ok' title='becomes a discovery dropdown (e.g. LoRA / model) when pinned or promoted'>▾ {len(fopts)}</span>"
            elif isinstance(v2, bool):
                fhint = " <span class='muted'>bool</span>"
            else:
                fhint = ""
            arows += (f"<tr><td>{_esc(f2)} <span class='muted'>= {_esc(str(v2))[:22]}</span>{fhint}</td>"
                      f"<td class='acts'>{add}{req_btn}</td></tr>")
        if arows:
            title = n.get("_meta", {}).get("title", "")
            head = f"<code>{_esc(nid)}</code> {_esc(n.get('class_type'))}" + (f" · {_esc(title)}" if title else "")
            avail += f"<tr class='node'><td colspan=2>{head}</td></tr>{arows}"
    available = (f"<h2>Available fields</h2>"
                 f"<p class='hint'>Add a field to pin it (Switch boolean, reference image, …). "
                 f"Unmapped fields keep the workflow's value.</p>"
                 f"<table class='avail'>{avail or '<tr><td>all scalar fields mapped</td></tr>'}</table>")
    return form, available


async def edit_add(request: Request):
    alias = _qp(request, "alias")
    node, fld = _qp(request, "node"), _qp(request, "field")
    cands = store.get(alias)
    if cands and node and fld:
        cand = cands[0]
        wf = cand.get("workflow_json") or {}
        cur = (wf.get(node, {}).get("inputs") or {}).get(fld)
        fixed = cand.get("fixed") or []
        if not any(b["node"] == node and b["field"] == fld for b in fixed):
            fixed.append({"node": node, "field": fld, "value": cur})
            cand["fixed"] = fixed
            store.upsert(alias, cands)
    return RedirectResponse(f"/ui/mapping?edit={alias}", status_code=303)


async def edit_del(request: Request):
    alias = _qp(request, "alias")
    cands = store.get(alias)
    if cands:
        cand = cands[0]
        param, node, fld = _qp(request, "param"), _qp(request, "node"), _qp(request, "field")
        if param:
            (cand.get("mapping") or {}).pop(param, None)
        elif node and fld:
            cand["fixed"] = [b for b in (cand.get("fixed") or [])
                             if not (b["node"] == node and b["field"] == fld)]
        store.upsert(alias, cands)
    return RedirectResponse(f"/ui/mapping?edit={alias}", status_code=303)


async def field_map(request: Request):
    """Promote an available node.field to a request param. The param name defaults to
    the field name (node-qualified on collision); any scalar field is eligible — there
    is no fixed param list. Rename it afterwards by editing the node/field cells."""
    alias, node = _qp(request, "alias"), _qp(request, "node")
    fld = _qp(request, "field")
    cands = store.get(alias)
    if cands and node and fld:
        cand = cands[0]
        mp = cand.setdefault("mapping", {})
        param = fld if fld not in mp else f"{fld}_{node}"
        mp[param] = {"node": node, "field": fld}
        store.upsert(alias, cands)
    return RedirectResponse(f"/ui/mapping?edit={alias}", status_code=303)


async def field_clear(request: Request):
    """Clear the workflow default of a mapped request field (sets the gateway-owned
    JSON's node.field to empty, so the param defaults to blank in the playground)."""
    alias, param = _qp(request, "alias"), _qp(request, "param")
    cands = store.get(alias)
    if cands and param:
        cand = cands[0]
        m = (cand.get("mapping") or {}).get(param)
        wf = cand.get("workflow_json")
        if m and wf and m.get("node") in wf:
            wf[m["node"]].setdefault("inputs", {})[m["field"]] = ""
            store.upsert(alias, cands)
    return RedirectResponse(f"/ui/mapping?edit={alias}", status_code=303)


async def cand_add(request: Request):
    """Add an allowed backend to an alias — an independent snapshot of the existing
    workflow+mapping on that backend (deduped by backend name)."""
    alias, backend = _qp(request, "alias"), _qp(request, "backend")
    cands = store.get(alias)
    valid = {b["name"] for b in _comfy_backends()}
    if cands and backend in valid and backend not in [c.get("backend") for c in cands]:
        new = json.loads(json.dumps(cands[0]))     # snapshot workflow+mapping (+retries)
        new["backend"] = backend
        new.pop("fallback", None)                  # legacy flag, no longer used
        cands.append(new)
        store.upsert(alias, cands)
        logger.info(f"ui: alias '{alias}' + backend '{backend}'")
    return RedirectResponse(f"/ui/mapping?edit={alias}", status_code=303)


async def cand_del(request: Request):
    """Remove an allowed backend (an alias must keep at least one)."""
    alias, backend = _qp(request, "alias"), _qp(request, "backend")
    cands = store.get(alias)
    if cands and len(cands) > 1:
        kept = [c for c in cands if c.get("backend") != backend]
        if kept and len(kept) != len(cands):
            store.upsert(alias, kept)
            logger.info(f"ui: alias '{alias}' − backend '{backend}'")
    return RedirectResponse(f"/ui/mapping?edit={alias}", status_code=303)


async def field_order(request: Request):
    """Reorder the request fields (drag & drop). Rebuilds the mapping dict in the
    given param order; that order is what the editor and Playground render."""
    alias = _qp(request, "alias")
    order = request.query_params.getlist("order")
    cands = store.get(alias)
    if cands and order:
        cand = cands[0]
        m = cand.get("mapping") or {}
        new = {p: m[p] for p in order if p in m}
        for p in m:                       # keep any param not in the posted order
            new.setdefault(p, m[p])
        cand["mapping"] = new
        store.upsert(alias, cands)
    return RedirectResponse(f"/ui/mapping?edit={alias}", status_code=303)


async def update(request: Request):
    f = await _form(request)
    alias = f.get("alias", "").strip()
    cands = store.get(alias)
    if not alias or not cands:
        raise HTTPException(404, "alias not found")
    cand = cands[0]
    # Request fields are dynamic: one node__<param>/field__<param> pair per mapped
    # param the editor rendered (no fixed list). Slice after the prefix so param
    # names may themselves contain underscores.
    mapping = {}
    for key, val in f.items():
        if key.startswith("node__"):
            p = key[len("node__"):]
            node = (val or "").strip()
            fld = (f.get(f"field__{p}", "") or "").strip()
            if node and fld:
                entry = {"node": node, "field": fld}
                lbl = (f.get(f"label__{p}", "") or "").strip()
                if lbl:                       # label overrides Playground label / API field name
                    entry["label"] = lbl
                mapping[p] = entry
    fixed = []
    for key, val in f.items():
        if key.startswith("fixed__") and val != "":
            _, nid, fld = key.split("__", 2)
            fixed.append({"node": nid, "field": fld, "value": val})
    # per-backend overrides of pinned values: ovr__<backend>__<node>__<field>
    ovr = {}
    for key, val in f.items():
        if key.startswith("ovr__") and (val or "").strip() != "":
            parts = key.split("__", 3)
            if len(parts) == 4:
                ovr[(parts[1], parts[2], parts[3])] = val
    # mapping is backend-independent → sync to every candidate. `fixed` (pinned model
    # values) is per-backend: the primary holds the edited values; extra backends use
    # the same slots with optional per-backend value overrides (blank = inherit).
    for c in cands:
        c["mapping"] = mapping
    cands[0]["fixed"] = fixed
    for c in cands[1:]:
        bn = c.get("backend")
        c["fixed"] = [{"node": b["node"], "field": b["field"],
                       "value": ovr.get((bn, b["node"], b["field"]), b["value"])} for b in fixed]
    # retries is a per-alias setting (blank = try all) — keep it on every candidate so
    # it survives whichever backend is removed first.
    retries = (f.get("retries", "") or "").strip()
    for c in cands:
        c["retries"] = retries
    new_alias = (f.get("new_alias", "") or "").strip()
    if new_alias and new_alias != alias and not store.get(new_alias):
        store.delete(alias)            # rename: move under the new name
        alias = new_alias
    store.upsert(alias, cands)         # cand is cands[0] — keeps the other allowed backends
    logger.info(f"ui: updated '{alias}' ({len(mapping)} params, {len(fixed)} pinned, retries={retries or 'all'})")
    return RedirectResponse("/ui/mapping", status_code=303)


async def delete(request: Request):
    alias = request.query_params.get("alias", "").strip()
    if alias:
        store.delete(alias)
    return RedirectResponse("/ui/mapping", status_code=303)


async def copy(request: Request):
    alias = _qp(request, "alias")
    cands = store.get(alias)
    if not cands:
        return RedirectResponse("/ui/mapping", status_code=303)
    new = f"{alias}-copy"
    i = 2
    while store.get(new):
        new, i = f"{alias}-copy{i}", i + 1
    store.upsert(new, json.loads(json.dumps(cands)))     # deep copy of the candidate(s)
    logger.info(f"ui: copied alias '{alias}' → '{new}'")
    return RedirectResponse(f"/ui/mapping?edit={new}", status_code=303)


# ── Tab: Playground ─────────────────────────────────────────────────────────────

# Reference images stick across generations: stashed in memory per (user, alias) so
# the file-input (which the browser can't pre-fill) doesn't have to be re-picked each
# time. A new upload replaces; the "clear" checkbox drops it. Lost on restart (fine).
_pg_images: dict = {}


def _alias_defaults(cand: dict) -> dict:
    """Default value per request param, read from the workflow at the mapped node."""
    wf = cand.get("workflow_json") or {}
    out = {}
    for param, m in (cand.get("mapping") or {}).items():
        v = (wf.get(m.get("node"), {}).get("inputs") or {}).get(m.get("field"))
        if v is not None and not isinstance(v, list):
            out[param] = v
    return out


def _playground_form(aliases: list, vals: dict, cand: Optional[dict], oi: Optional[dict] = None,
                     kept: Optional[set] = None) -> str:
    v = lambda k: str(vals.get(k) if vals.get(k) is not None else "")
    # selecting an alias reloads with its workflow defaults pre-filled
    opts = "".join(f'<option value="{_esc(a)}"{" selected" if a == vals.get("model") else ""}>{_esc(a)}</option>'
                   for a in aliases)
    alias_select = ('<select name="model" onchange="location.href=\'/ui/playground?model=\'+'
                    f'encodeURIComponent(this.value)">{opts}</select>')
    # one input per MAPPED param of the selected alias (dynamic — mirrors the request
    # fields configured in Mapping, in drag-set order). Image-loader fields render as
    # file uploads (img__<param>, empty → 8×8 placeholder); scalars as p__<param>
    # (number when the default is numeric, else text). The label override (else the
    # param name) is what the user sees.
    mapping = (cand.get("mapping") if cand else {}) or {}
    wf = (cand.get("workflow_json") if cand else {}) or {}
    imgset = set(adapters.image_params(wf, mapping))
    defaults = _alias_defaults(cand) if cand else {}
    rows = ""
    for p, m in mapping.items():
        label = (m or {}).get("label") or p
        if p in imgset:
            if kept and p in kept:
                extra = (' <span class="badge ok">✓ kept</span> <label class="muted" '
                         f'style="font-weight:normal"><input type="checkbox" name="clear__{_esc(p)}"> clear</label>')
            else:
                extra = ' <span class="muted">empty → 8×8 placeholder</span>'
            rows += _field(label, f'<input type="file" name="img__{_esc(p)}" accept="image/*">{extra}')
        else:
            dv = defaults.get(p)
            node, field = (m or {}).get("node"), (m or {}).get("field")
            opts = (oi or {}).get((wf.get(node) or {}).get("class_type", ""), {}).get(field)
            if opts and _is_model_field(opts, dv):       # combo/model field (lora_name, …) → dropdown
                cur = v(p) or (str(dv) if dv is not None else "")
                o = list(opts)
                if cur and cur not in o:
                    o = [cur] + o
                rows += _field(label, _select(f"p__{p}", o, cur))
            else:
                typ = "number" if isinstance(dv, (int, float)) and not isinstance(dv, bool) else "text"
                rows += _field(label, _inp(f"p__{p}", v(p), typ=typ))
    rows = rows or "<p class='hint'>This alias has no request fields — add some in Mapping.</p>"
    bk_list = [c.get("backend") for c in (store.get(vals.get("model")) or [])]
    sel_bk = vals.get("backend", "")
    bk_opts = ('<option value="">(auto · priority)</option>'
               + "".join(f'<option value="{_esc(b)}"{" selected" if b == sel_bk else ""}>{_esc(b)}</option>'
                         for b in bk_list))
    backend_field = _field("backend", f'<select name="backend">{bk_opts}</select>') if bk_list else ""
    return ('<form action="/ui/playground/generate" method="post" enctype="multipart/form-data">'
            f'<div class="formbar"><h2>Playground</h2>{_btn("Generate", submit=True)}</div>'
            + _field("alias", alias_select)
            + backend_field
            + rows
            + '<p class="hint">synchronous; image backends ~1 min · each image reuses one slot</p></form>')


# Polls ONLY the result column (not a full-page meta-refresh), so the form stays
# editable while a job runs — pick new params and Generate again without losing them.
_PG_POLL_JS = ("<script>(function(){var rc=document.getElementById('resultcol');"
               "if(!rc)return;var job=rc.getAttribute('data-poll-job');if(!job)return;"
               "var t=setInterval(function(){"
               "fetch('/ui/playground/status/'+encodeURIComponent(job),{cache:'no-store'})"
               ".then(function(r){return r.text();}).then(function(h){rc.innerHTML=h;"
               "if(h.indexOf('data-jobdone')>=0){clearInterval(t);}}).catch(function(){});"
               "},2000);})();</script>")


def _playground_body(aliases: list, vals: dict, cand: Optional[dict], result_html: str,
                     oi: Optional[dict] = None, kept: Optional[set] = None, poll_job: str = "") -> str:
    pa = f' data-poll-job="{_esc(poll_job)}"' if poll_job else ""
    return (f'<div class="cols"><div class="col">{_playground_form(aliases, vals, cand, oi, kept)}</div>'
            f'<div class="col"><div id="resultcol"{pa}>{result_html}</div></div></div>{_PG_POLL_JS}')


def _job_result_html(job_id: str, job: Optional[dict]):
    """(right-column HTML, refresh-seconds-or-None) for a generation job."""
    if not job:
        return f"<h2>Result</h2><p class='bad'>job {_esc(job_id)} not found</p>", None
    st = job.get("status")
    if st in ("queued", "running"):
        return (f"<h2>Result</h2><p>⏳ <b>Generating…</b></p>"
                f"<p class='muted'>job {_esc(job_id)} · {st} · this view auto-updates</p>"), 2
    if st == "failed":
        return (f"<h2>Result</h2><p class='bad'>✗ failed · job {_esc(job_id)}</p>"
                f"<pre class='err'>{_esc(job.get('error'))}</pre>"), None
    imgs = "".join(f'<div><img class="result" src="/ui/playground/result/{_esc(job_id)}/{r["n"]}"></div>'
                   for r in job.get("results", []))
    return (f"<h2>Result</h2><p>✓ done · job {_esc(job_id)} · "
            f"backend {_esc(job.get('backend'))}</p>{imgs or '<p class=muted>No artifacts.</p>'}"), None


async def playground_page(request: Request):
    if not store.is_active():
        return _inactive()
    aliases = list(store.list_aliases().keys())
    if not aliases:
        return HTMLResponse(_page("Playground", "<h2>Playground</h2><p class='hint'>Register an alias in "
            "the <a href='/ui/mapping'>Mapping</a> tab first.</p>", "playground"))
    qp = request.query_params
    model = qp.get("model", "") or aliases[0]   # first load: pick the first alias
    cand = (store.get(model) or [None])[0]
    # values per request param, keyed by param name; query (p__<param>) overrides the
    # alias's workflow default. Params are dynamic — whatever Mapping configured.
    vals = {"model": model, "backend": qp.get("backend", "")}
    defaults = _alias_defaults(cand) if cand else {}
    for p in ((cand.get("mapping") if cand else {}) or {}):
        q = qp.get(f"p__{p}", "")
        vals[p] = q if q != "" else ("" if defaults.get(p) is None else str(defaults[p]))
    job_id = qp.get("job", "")
    refresh = None
    if job_id:
        result_html, refresh = _job_result_html(job_id, jobs.get(job_id))
    else:
        result_html = "<h2>Result</h2><p class='hint'>Generate to see the image here.</p>"
    wf = (cand.get("workflow_json") if cand else {}) or {}
    oi = await _object_info(cand.get("backend", ""), wf) if cand else {}
    kept = set(_pg_images.get((_session_user(request) or "default", model), {}).keys())
    poll_job = job_id if refresh else ""        # poll only the result column; form stays editable
    return HTMLResponse(_page("Playground", _playground_body(aliases, vals, cand, result_html, oi, kept, poll_job),
                              "playground"))


async def generate(request: Request):
    if _run_generation is None:
        raise HTTPException(503, "generation not wired")
    f = await _multipart(request)
    model = str(f.get("model", ""))
    force_bk = str(f.get("backend", "")).strip()       # pin to one backend (testing per-backend)
    # dynamic request fields arrive as p__<param>. prompt/negative_prompt feed the
    # request's inputs; everything else goes into params (numeric-coerced when it
    # parses). The mapping in the alias decides which node each lands on.
    submitted = {k[len("p__"):]: str(f.get(k, "")) for k in f if k.startswith("p__")}
    body = {"model": model, "mode": "async", "params": {}}     # async → instant job id
    if force_bk:
        body["backend"] = force_bk
    for p, raw in submitted.items():
        if raw.strip() == "":
            continue
        if p in ("prompt", "negative_prompt"):
            body[p] = raw
        else:
            body["params"][p] = _num(raw)
    # per-field image uploads (img__<param>); empty inputs fall back to the 8×8
    # placeholder downstream, so they're simply omitted here.
    # reference images persist across generations (stash per user+alias); a new upload
    # replaces, a checked clear__<param> drops the kept one.
    user = _session_user(request) or "default"
    stash = _pg_images.setdefault((user, model), {})
    for k in f:
        if k.startswith("img__"):
            val = f.get(k)
            if isinstance(val, (bytes, bytearray)) and val.strip():
                stash[k[len("img__"):]] = bytes(val)
        elif k.startswith("clear__"):
            stash.pop(k[len("clear__"):], None)
    images = dict(stash)
    cand = (store.get(model) or [None])[0]
    vals = {"model": model, "backend": force_bk, **submitted}
    try:
        view = await _run_generation(body, request, upload_images=images)
    except HTTPException as e:
        aliases = list(store.list_aliases().keys())
        result_html = f'<h2>Result</h2><p class="bad">Error {e.status_code}: {_esc(e.detail)}</p>'
        return HTMLResponse(_page("Playground", _playground_body(aliases, vals, cand, result_html,
                                  kept=set(stash.keys())), "playground"))
    # Redirect to the GET view (form re-populated + auto-polling) — instant feedback.
    q = urlencode({"model": model, "backend": force_bk, "job": view.get("job_id", ""),
                   **{f"p__{p}": v for p, v in submitted.items() if v}})
    return RedirectResponse(f"/ui/playground?{q}", status_code=303)


async def result(job_id: str, n: int):
    rp = jobs.result_path(job_id, n)
    if rp is None:
        raise HTTPException(404, "result not found")
    path, mime = rp
    return FileResponse(path, media_type=mime)


async def playground_status(job_id: str):
    """Result-column fragment for the JS poller (so the form isn't reloaded mid-edit)."""
    html, refresh = _job_result_html(job_id, jobs.get(job_id))
    if not refresh:                              # done/failed → marker tells the poller to stop
        html += "<span data-jobdone hidden></span>"
    return HTMLResponse(html)


# ── Image Jobs tab (G1): inspect a generation's inputs + outputs within its TTL ──

_JOB_TICK = ("<script>function _fd(ms){ms=ms|0;if(ms<1000)return ms+' ms';var s=ms/1000;"
             "return s<60?s.toFixed(1)+' s':(s/60).toFixed(1)+' min';}"
             "function _td(){var n=Date.now()/1000;document.querySelectorAll('.jdur[data-since]')"
             ".forEach(function(e){e.textContent=_fd((n-parseFloat(e.getAttribute('data-since')))*1000);});}"
             "setInterval(_td,1000);_td();</script>")
_JOB_SCLS = {"done": "ok", "failed": "bad", "running": "warn", "queued": "warn"}


async def jobs_page(request: Request):
    """List of generation jobs (image/video/audio), newest first, each linking to its
    detail page. Excludes parked-chat jobs (those live under Statistic)."""
    if not jobs.is_active():
        return _inactive()
    rows = [j for j in jobs.recent(200) if j.get("task") != "chat"]
    if not rows:
        return HTMLResponse(_page("Image Jobs", "<h2>Image Jobs</h2><p class='hint'>No generation "
            "jobs yet. Run one in the <a href='/ui/playground'>Image Playground</a>.</p>", "jobs"))
    now = int(time.time())
    tr = ""
    for j in rows:
        st = j["status"]
        scls = _JOB_SCLS.get(st, "muted")
        cr, upd = int(j.get("created") or 0), int(j.get("updated") or 0)
        if st in ("done", "failed") and upd >= cr:
            dcell = f"<td class='muted'>{_dur((upd - cr) * 1000)}</td>"
        elif st == "running":
            dcell = f"<td class='muted jdur' data-since='{cr}'>{_dur((now - cr) * 1000)}</td>"
        else:
            dcell = "<td class='muted'>—</td>"
        jid = j["id"]
        tr += (f"<tr><td><a href='/ui/job/{_esc(jid)}'><code>{_esc(jid[:8])}</code></a></td>"
               f"<td>{_esc(j.get('task'))}</td><td>{_esc(j.get('alias'))}</td>"
               f"<td>{_esc(j.get('backend'))}</td>"
               f"<td><span class='badge {scls}'>{_esc(st)}</span></td>"
               f"<td class='muted'>{j.get('result_count') or 0}</td>"
               f"<td class='muted'>{_age(j.get('created'))}</td>{dcell}"
               f"<td class='muted'>{_esc(j.get('owner'))}</td>"
               f"<td style='text-align:right;white-space:nowrap'>{_btn('view', '/ui/job/' + jid, 'secondary', sm=True)}</td></tr>")
    tbl = (f"<table><tr><th>id</th><th>task</th><th>alias</th><th>backend</th><th>status</th>"
           f"<th>imgs</th><th>age</th><th>dur</th><th>owner</th><th></th></tr>{tr}</table>")
    refresh = 5 if any(j["status"] in ("running", "queued") for j in rows) else None
    return HTMLResponse(_page("Image Jobs", f"<h2>Image Jobs</h2>{tbl}{_JOB_TICK}", "jobs", refresh=refresh))


def _job_thumbs(jid: str, kind: str, entries: list) -> str:
    """Gallery of <img> thumbnails linking to the full image (kind = 'input'|'result')."""
    base = f"/ui/job/{_esc(jid)}/input/" if kind == "input" else f"/ui/playground/result/{_esc(jid)}/"
    cells = "".join(f"<a href='{base}{r['n']}' target='_blank'><img src='{base}{r['n']}' "
                    f"style='max-width:240px;max-height:240px;border:1px solid #313a46;border-radius:8px'></a>"
                    for r in entries)
    return f"<div style='display:flex;gap:10px;flex-wrap:wrap;margin:8px 0'>{cells}</div>"


async def job_detail_page(job_id: str, request: Request):
    """Input (prompt/params/reference images) + output artifacts of one job."""
    if not jobs.is_active():
        return _inactive()
    job = jobs.get(job_id)
    back = _btn("← Back to Jobs", "/ui/jobs", "secondary")
    if job is None:
        return HTMLResponse(_page("Job", f"<div class='bar'><h2>Job</h2>{back}</div>"
            f"<p class='bad'>job {_esc(job_id)} not found (or pruned past its TTL).</p>", "jobs"), status_code=404)
    st = job["status"]
    meta = job.get("meta") or {}
    inp = meta.get("inputs") or {}
    prompt, neg = inp.get("prompt") or "", inp.get("negative_prompt") or ""
    params = inp.get("params") or {}
    prows = "".join(f"<tr><td><code>{_esc(k)}</code></td><td>{_esc(str(v))}</td></tr>" for k, v in params.items())
    in_imgs = meta.get("input_images", [])
    inbox = ""
    if prompt:
        inbox += f"<h3>Prompt</h3><pre class='chatout'>{_esc(prompt)}</pre>"
    if neg:
        inbox += f"<h3>Negative</h3><pre class='chatout'>{_esc(neg)}</pre>"
    # request params + the alias's pinned values (for the backend this job ran on) side
    # by side — quick overview of the full effective input.
    pinned = []
    if store.is_active():
        cs = store.get(job["alias"]) or []
        c = next((x for x in cs if x.get("backend") == job["backend"]), cs[0] if cs else None)
        pinned = (c or {}).get("fixed") or []
    frows = "".join(f"<tr><td><code>{_esc(b.get('field'))}</code></td>"
                    f"<td>{_esc(str(b.get('value')))}</td></tr>" for b in pinned)
    ptbl = f"<h3>Params</h3><table>{prows}</table>" if prows else ""
    ftbl = (f"<h3>Pinned values <span class='muted' style='font-weight:normal'>· {_esc(job['backend'])}</span></h3>"
            f"<table>{frows}</table>") if frows else ""
    if ptbl or ftbl:
        inbox += (f"<div style='display:flex;gap:28px;flex-wrap:wrap;align-items:flex-start'>"
                  f"<div>{ptbl}</div><div>{ftbl}</div></div>")
    if in_imgs:
        inbox += f"<h3>Reference images</h3>{_job_thumbs(job_id, 'input', in_imgs)}"
    if not inbox:
        inbox = "<p class='muted'>No stored inputs (job predates this feature).</p>"
    if st in ("queued", "running"):
        outbox = f"<p>⏳ <b>{_esc(st)}</b> · this view auto-updates</p>"
    elif st == "failed":
        outbox = f"<p class='bad'>✗ failed</p><pre class='err'>{_esc(job.get('error'))}</pre>"
    elif job.get("results"):
        outbox = _job_thumbs(job_id, "result", job["results"])
    else:
        outbox = "<p class='muted'>No artifacts.</p>"
    exp = " · <span class='bad'>expired</span>" if job.get("expired") else ""
    info = (f"<p class='muted'>task {_esc(job['task'])} · alias {_esc(job['alias'])} · backend "
            f"{_esc(job['backend'])} · owner {_esc(job['owner'])} · {_age(job['created'])}{exp}</p>")
    page = (f"<div class='bar'><h2>Job <code>{_esc(job_id[:12])}</code> "
            f"<span class='badge {_JOB_SCLS.get(st, 'muted')}'>{_esc(st)}</span></h2>{back}</div>{info}"
            f"<div style='display:flex;gap:28px;flex-wrap:wrap;align-items:flex-start'>"
            f"<div style='flex:1;min-width:320px'><h2>Input</h2>{inbox}</div>"
            f"<div style='flex:1;min-width:320px'><h2>Output</h2>{outbox}</div></div>"
            + (_JOB_TICK if st in ("queued", "running") else ""))
    return HTMLResponse(_page("Job", page, "jobs", refresh=(2 if st in ("queued", "running") else None)))


async def job_input(job_id: str, n: int):
    ip = jobs.input_path(job_id, n)
    if ip is None:
        raise HTTPException(404, "input not found")
    path, mime = ip
    return FileResponse(path, media_type=mime)


# ── Tabs: stubs ─────────────────────────────────────────────────────────────────

def _cost(v) -> str:
    return f"${float(v or 0):.4f}"


def _ts(ts) -> str:
    return time.strftime("%m-%d %H:%M:%S", time.localtime(int(ts)))


def _age(ts) -> str:
    s = max(0, int(time.time()) - int(ts or 0))
    return f"{s}s" if s < 60 else (f"{s // 60}m" if s < 3600 else f"{s // 3600}h")


def _dur(ms) -> str:
    """Duration in a fitting unit: <1s → ms, <60s → seconds (1 dp), else minutes (1 dp)."""
    ms = int(ms or 0)
    if ms < 1000:
        return f"{ms} ms"
    s = ms / 1000
    return f"{s:.1f} s" if s < 60 else f"{s / 60:.1f} min"


def _looks_like_ip(s: str) -> bool:
    try:
        ipaddress.ip_address(s)
        return True
    except ValueError:
        return False


def _src_name(src: str, aliases: dict) -> str:
    """Display name for a stats source: its IP alias if set, else the raw source."""
    return aliases.get(src) or src


async def _reverse_dns(ip: str) -> str:
    try:
        host = await asyncio.wait_for(asyncio.to_thread(socket.gethostbyaddr, ip), timeout=1.5)
        return host[0]
    except Exception:
        return ""


async def _autoresolve_ips() -> None:
    """Best-effort: for caller IPs seen in stats with no alias yet, reverse-DNS them
    and persist the hostname (or '' to mark 'attempted', so we don't retry forever)."""
    if not stats.is_active():
        return
    aliases = store.get_ip_aliases()
    seen = [r[0] for r in stats.summary()["by_source"]]
    todo = [s for s in seen if _looks_like_ip(s) and s not in aliases][:20]
    if not todo:
        return
    names = await asyncio.gather(*[_reverse_dns(ip) for ip in todo])
    for ip, name in zip(todo, names):
        aliases[ip] = name
    store.save_ip_aliases(aliases)


async def dashboard_page(request: Request):
    d = _dashboard_snapshot()
    bes = d.get("backends", [])
    up = sum(1 for b in bes if b.get("healthy"))
    jc = d.get("jobs_counts", {})
    active_jobs = (jc.get("queued", 0) or 0) + (jc.get("running", 0) or 0)

    def card(num, lbl):
        return f"<div class='card'><div class='cnum'>{num}</div><div class='clbl'>{_esc(lbl)}</div></div>"
    cards = ("<div class='cards'>"
             + card(d.get("llm_inflight", 0), "LLM in flight")
             + card(d.get("parked", 0), "calls parked")
             + card(active_jobs, "image jobs active")
             + card(f"{up}/{len(bes)}", "backends up")
             + (card(d.get("calls_24h", 0), "calls · 24h") if d.get("stats_active") else "")
             + "</div>")

    def _srank(b):                                  # sort order: ready → busy → off → disabled
        if not b.get("enabled"):
            return 3
        if not b.get("healthy"):
            return 2
        return 1 if b.get("busy") else 0

    def bstatus(b):
        if not b.get("enabled"):
            return _badge("disabled")
        if not b.get("healthy"):
            return _badge("off", "bad")
        return _badge("busy", "warn") if b.get("busy") else _badge("ready", "ok")
    brows = ""
    for b in sorted(bes, key=lambda x: (_srank(x), x.get("name", "").lower())):
        cap = b.get("max_concurrent")
        inf = f"{b.get('inflight', 0)}" + (f" / {cap}" if cap else "")
        brows += (f"<tr><td>{_esc(b['name'])}</td><td class='muted'>{_esc(b.get('type'))}</td>"
                  f"<td>{bstatus(b)}</td><td>{inf}</td><td>{b.get('models', 0)}</td>"
                  f"<td>{b.get('priority')}</td></tr>")
    backends_tbl = (f"<h2>Backends</h2><table><tr><th>backend</th><th>type</th><th>status</th>"
                    f"<th>in flight</th><th>models</th><th>prio</th></tr>{brows}</table>")

    if d.get("jobs_active"):
        order = (("running", "warn"), ("queued", "warn"), ("done", "ok"), ("failed", "bad"))
        badges = " ".join(_badge(f"{k} {jc.get(k, 0)}", kind) for k, kind in order if jc.get(k))
        jr = ""
        for j in d.get("jobs_recent", []):
            st = j["status"]
            scls = {"done": "ok", "failed": "bad", "running": "warn", "queued": "warn"}.get(st, "muted")
            cr, upd = int(j.get("created") or 0), int(j.get("updated") or 0)
            if st in ("done", "failed") and upd >= cr:
                dcell = f"<td class='muted'>{_dur((upd - cr) * 1000)}</td>"
            elif st == "running":                       # live tick-up via JS (data-since)
                dcell = f"<td class='muted jdur' data-since='{cr}'>{_dur((int(time.time()) - cr) * 1000)}</td>"
            else:
                dcell = "<td class='muted'>—</td>"
            jr += (f"<tr><td><a href='/ui/job/{_esc(j['id'])}'><code>{_esc(j['id'][:8])}</code></a></td>"
                   f"<td>{_esc(j.get('alias'))}</td>"
                   f"<td>{_esc(j.get('backend'))}</td><td><span class='badge {scls}'>{_esc(st)}</span></td>"
                   f"<td class='muted'>{_age(j.get('created'))}</td>{dcell}"
                   f"<td class='muted'>{_esc(j.get('owner'))}</td></tr>")
        jobs_html = (f"<h2>Image jobs {badges}</h2>"
                     + (f"<table><tr><th>id</th><th>alias</th><th>backend</th><th>status</th>"
                        f"<th>age</th><th>dur</th><th>owner</th></tr>{jr}</table>" if jr
                        else "<p class='muted'>no jobs yet</p>"))
    else:
        jobs_html = "<h2>Image jobs</h2><p class='hint'>Image generation is off.</p>"

    # No "Recent calls" here — that lives in the Statistic tab (avoid duplication).
    tick = ("<script>function _fd(ms){ms=ms|0;if(ms<1000)return ms+' ms';var s=ms/1000;"
            "return s<60?s.toFixed(1)+' s':(s/60).toFixed(1)+' min';}"
            "function _td(){var n=Date.now()/1000;document.querySelectorAll('.jdur[data-since]')"
            ".forEach(function(e){e.textContent=_fd((n-parseFloat(e.getAttribute('data-since')))*1000);});}"
            "setInterval(_td,1000);_td();</script>")
    body = ("<h2>Dashboard <span class='muted' style='font-weight:normal'>· live · auto-refresh 4s</span></h2>"
            + cards + backends_tbl + jobs_html + tick)
    return HTMLResponse(_page("Dashboard", body, "dashboard", refresh=4))


async def statistic_page(request: Request):
    if not stats.is_active():
        return HTMLResponse(_page("Statistic", "<h2>Statistic</h2><p class='hint'>Call recording is off. "
            "Enable <b>stats</b> in the <a href='/ui/server'>Server</a> tab (needs a restart) to collect "
            "per-call stats here.</p>", "statistic"))
    user = (request.query_params.get("user") or "").strip() or None
    s = stats.summary(user=user)
    aliases = store.get_ip_aliases()
    _box = ("padding:7px 10px;background:#0c0e12;border:1px solid #242a33;border-radius:8px;color:#cdd6e0")
    opts = "<option value=''>all users</option>" + "".join(
        f"<option value='{_esc(r[0])}'{' selected' if r[0] == user else ''}>{_esc(_src_name(r[0], aliases))}</option>"
        for r in s["by_source"])
    picker = (f"<select style=\"width:auto;{_box}\" onchange=\"location.href='/ui/statistic'+"
              f"(this.value?('?user='+encodeURIComponent(this.value)):'')\">{opts}</select>")
    search = (f"<input id='sf' autocomplete='off' oninput='sfRun()' "
              f"placeholder='filter rows: backend / alias / model / user…' "
              f"style=\"flex:1;min-width:220px;max-width:420px;{_box}\">")
    scope = (f" · <span class='muted' style='font-weight:normal'>user <b>{_esc(user)}</b> · "
             f"<a href='/ui/statistic'>clear</a></span>") if user else ""
    cards = (f"<div class='cards'>"
             f"<div class='card'><div class='cnum'>{s['total_count']}</div><div class='clbl'>calls total</div></div>"
             f"<div class='card'><div class='cnum'>{_cost(s['total_cost'])}</div><div class='clbl'>cost total</div></div>"
             f"<div class='card'><div class='cnum'>{s['h24_count']}</div><div class='clbl'>calls · 24h</div></div>"
             f"<div class='card'><div class='cnum'>{_cost(s['h24_cost'])}</div><div class='clbl'>cost · 24h</div></div>"
             f"</div>")
    be = "".join(f"<tr><td>{_esc(r[0])}</td><td>{r[1]}</td><td>{r[2]}</td><td>{r[3]}</td>"
                 f"<td>{_cost(r[4])}</td><td>{_dur(r[5])}</td></tr>" for r in s["by_backend"])
    by_backend = (f"<h2>By backend</h2><table class='filterable'><tr><th>backend</th><th>calls</th><th>in tok</th>"
                  f"<th>out tok</th><th>cost</th><th>avg</th></tr>{be}</table>" if be
                  else "<h2>By backend</h2><p class='muted'>no calls yet</p>")
    mo = "".join(f"<tr><td>{_esc(r[0]) or '—'}</td><td><code>{_esc(r[1])}</code></td><td>{r[2]}</td>"
                 f"<td>{r[3]}</td><td>{r[4]}</td><td>{_cost(r[5])}</td></tr>" for r in s["by_model"])
    by_model = (f"<h2>By alias / model</h2><table class='filterable'><tr><th>alias</th><th>model</th><th>calls</th>"
                f"<th>in</th><th>out</th><th>cost</th></tr>{mo}</table>" if mo else "")
    so = "".join(f"<tr><td>{_esc(_src_name(r[0], aliases))}</td><td>{r[1]}</td><td>{_cost(r[2])}</td></tr>" for r in s["by_source"])
    by_source = (f"<h2>By user / source</h2><table class='filterable'><tr><th>source</th><th>calls</th><th>cost</th></tr>"
                 f"{so}</table>" if so else "")
    rec = ""
    for r in s["recent"]:
        cid, ts, dur, backend, source, alias, model, endpoint, status, intk, outk, cost, prev, has_body = r
        scls = "ok" if (status and 200 <= int(status) < 300) else "bad"
        if has_body:
            view = f"<a href='/ui/call/{cid}' title='{_esc(prev or '')}'>view</a>"
        elif prev:
            view = f"<span class='muted' title='{_esc(prev)}'>{_esc(prev[:30])}…</span>"
        else:
            view = "<span class='muted'>—</span>"
        rec += (f"<tr><td class='muted'>{_ts(ts)}</td><td>{_esc(_src_name(source, aliases))}</td><td>{_esc(backend)}</td>"
                f"<td>{_esc(alias) or ''}{('→' + _esc(model)) if model else ''}</td>"
                f"<td class='muted'>{_esc((endpoint or '').replace('/v1/', ''))}</td>"
                f"<td><span class='badge {scls}'>{_esc(status)}</span></td>"
                f"<td>{_dur(dur)}</td><td>{intk}/{outk}</td><td>{_cost(cost)}</td><td>{view}</td></tr>")
    recent = (f"<h2>Recent calls</h2><table class='recent filterable'><tr><th>time</th><th>source</th><th>backend</th>"
              f"<th>alias→model</th><th>endpoint</th><th>status</th><th>dur</th><th>tok i/o</th><th>cost</th><th>req</th></tr>"
              f"{rec}</table>" if rec else "<h2>Recent calls</h2><p class='muted'>no calls yet</p>")
    sfjs = ("<script>function sfRun(){var q=(document.getElementById('sf').value||'').toLowerCase();"
            "document.querySelectorAll('.filterable tr').forEach(function(r){"
            "if(r.getElementsByTagName('th').length)return;"
            "r.style.display=r.textContent.toLowerCase().indexOf(q)>-1?'':'none';});}</script>")
    head = (f"<h2>Statistic{scope}</h2>"
            f"<div style='display:flex;gap:10px;align-items:center;margin:6px 0 10px'>{picker}{search}</div>")
    body = head + cards + by_backend + by_model + by_source + recent + sfjs
    return HTMLResponse(_page("Statistic", body, "statistic"))


async def call_view(call_id: int, request: Request):
    """Full stored request + response body for one call (E3)."""
    body = stats.get_body(call_id)
    if body is None:
        inner = "<p class='muted'>No stored body for this call (predates the feature, or pruned).</p>"
    else:
        req = json.dumps(body.get("request"), indent=2, ensure_ascii=False)
        resp = json.dumps(body.get("response"), indent=2, ensure_ascii=False)
        inner = (f"<h3>Request</h3><pre class='chatout'>{_esc(req)}</pre>"
                 f"<h3>Response</h3><pre class='chatout'>{_esc(resp)}</pre>")
    page = (f"<div class='bar'><h2>Call #{call_id}</h2>"
            f"{_btn('← Back to Statistic', '/ui/statistic', 'secondary')}</div>{inner}")
    return HTMLResponse(_page("Call", page, "statistic"))


def _user_form(u: Optional[dict]) -> str:
    g = lambda k, d="": str((u or {}).get(k) if (u or {}).get(k) is not None else d)
    has_key = bool((u or {}).get("api_key"))
    orig = f'<input type="hidden" name="orig" value="{_esc((u or {}).get("name", ""))}">' if u else ""
    # model allow-list as a table — ALIASES only (chat + image generation aliases),
    # since access is granted at the alias level; raw model ids would be noise.
    # Empty selection = all allowed.
    allowed = set((u or {}).get("models") or [])
    chat_al = sorted(set(_gateway_info().get("virtual_models", [])))
    img_al = sorted(store.list_aliases().keys()) if store.is_active() else []
    bk_al = sorted(b.get("name", "") for b in _gateway_info().get("backends", []) if b.get("name"))
    rows = ""
    # chat/image aliases granted by name; a backend grants ALL of its models (and filters
    # what this user's key sees in /v1/models).
    for a, kind in ([(a, "chat") for a in chat_al] + [(a, "image") for a in img_al]
                    + [(b, "backend") for b in bk_al]):
        ck = " checked" if a in allowed else ""
        rows += (f'<tr><td><input type="checkbox" name="model" value="{_esc(a)}"{ck}></td>'
                 f'<td><code>{_esc(a)}</code></td><td class="muted">{kind}</td></tr>')
    acc = (f'<div class="acctbl"><table><thead><tr><th title="allow this entry">✓</th>'
           f'<th>name</th><th>kind</th></tr></thead><tbody>{rows}</tbody></table></div>' if rows
           else "<p class='muted'>no aliases or backends yet</p>")
    return (f'<form action="/ui/users/save" method="post">{orig}'
            f'<div class="formbar"><h2>{"Edit User" if u else "Add User"}</h2>'
            f'{_btn("Save", submit=True)}{_btn("Cancel", "/ui/users", "secondary")}</div>'
            + _field("name", _inp("name", g("name"), placeholder="alice"))
            + _field("API key", _inp("api_key", "", placeholder=("•••• set — blank keeps it" if has_key
                                                                 else "the user's bearer token")))
            + _field("role", _select("role", ["user", "admin"], g("role", "user")))
            + _field("enabled", _checkbox("enabled", (u or {}).get("enabled", True), "enabled"))
            + _field("quota req/day", _inp("quota_req_day", g("quota_req_day"),
                                           placeholder="blank = unlimited", typ="number"))
            + _field("quota cost/month ($)", _inp("quota_cost_month", g("quota_cost_month"),
                                                  placeholder="blank = unlimited, e.g. 5.00"))
            + "<h2>Model access</h2>"
            + "<p class='hint'>Leave <b>all unchecked</b> to allow every model (default). Checking rows "
              "restricts this user to them <b>and</b> filters what their key sees in <code>/v1/models</code>. "
              "A <b>backend</b> grants all of its models; an <b>image</b> alias is what an image client "
              "(e.g. anima-verse) should be limited to.</p>"
            + acc
            + "</form>")


async def users_page(request: Request):
    qp = request.query_params
    edit = qp.get("edit", "")
    open_auth = (not store.list_users()) and not _server_info().get("effective", {}).get("api_key_set")
    items = ""
    for u in store.list_users():
        role_b = _badge(u.get("role", "user"), "ok" if u.get("role") == "admin" else "muted")
        st = "" if u.get("enabled", True) else _badge("disabled")
        key_b = _badge("key set", "ok") if u.get("api_key") else _badge("no key", "warn")
        acts = _icon_acts(("✎", f"/ui/users?edit={_esc(u['name'])}", "secondary", "Edit"),
                          ("✕", f"/ui/users/delete?name={_esc(u['name'])}", "danger", "Delete",
                           f"Delete user {u['name']}?"))
        allow = u.get("models") or []
        cq = u.get('quota_cost_month')
        sub = (f"quota/day {u.get('quota_req_day') or '∞'} · "
               f"cost/mo {('$' + format(float(cq), '.2f')) if cq else '∞'} · "
               f"models {', '.join(allow) if allow else 'all'}")
        items += _item(f"{_esc(u['name'])} {role_b}{st} {key_b}", sub, acts, sel=(u["name"] == edit))
    items = items or "<p class='muted'>No users — the gateway is open (bootstrap). Add one to require keys.</p>"
    warn = ("<p class='ok-banner'>Bootstrap mode: no users and no master key → the API and /ui are "
            "open. Add an <b>admin</b> user (or set a master API key in Server) to lock it down.</p>"
            if open_auth else "")
    list_html = (f'<div class="bar"><h2>Users</h2>{_btn("+ New user", "/ui/users?new=1")}</div>'
                 "<p class='hint'>Each user authenticates with their API key; calls are attributed to "
                 "them (stats, job ownership). Empty model list = all allowed.</p>" + items)
    if edit and store.get_user(edit):
        detail = _user_form(store.get_user(edit))
    elif qp.get("new"):
        detail = _user_form(None)
    else:
        detail = "<h2>Details</h2><p class='hint'>Select a user's <b>Edit</b>, or <b>+ New user</b>.</p>"
    await _autoresolve_ips()
    ipa = store.get_ip_aliases()
    seen_ips = sorted({r[0] for r in (stats.summary()["by_source"] if stats.is_active() else [])
                       if _looks_like_ip(r[0])} | set(ipa.keys()))
    iprows = ""
    for ip in seen_ips:
        iprows += (f"<tr><td><code>{_esc(ip)}</code></td>"
                   f"<td><form action='/ui/ipalias/save' method='post' style='display:flex;gap:6px;align-items:center;margin:0'>"
                   f"<input type='hidden' name='ip' value='{_esc(ip)}'>{_inp('name', ipa.get(ip, ''), placeholder='alias')}"
                   f"{_btn('Save', submit=True)}</form></td>"
                   f"<td style='text-align:right;white-space:nowrap'>"
                   f"{_icon_acts(('✕', f'/ui/ipalias/delete?ip={quote(ip)}', 'danger', 'Delete', f'Delete IP alias {ip}?'))}</td></tr>")
    ip_section = (f"<h2 style='margin-top:26px'>IP aliases</h2>"
                  f"<p class='hint'>Friendly names for caller IPs (unauthenticated / <code>x-source</code> calls) as shown in "
                  f"Statistic. Hostnames are auto-resolved via reverse DNS on load — edit or clear as needed.</p>"
                  + (f"<table><tr><th>IP</th><th>alias</th><th></th></tr>{iprows}</table>" if iprows
                     else "<p class='muted'>No caller IPs seen yet (calls are currently attributed to authenticated users).</p>"))
    body = (warn + f'<div class="cols"><div class="col">{list_html}</div>'
            f'<div class="col">{detail}</div></div>' + ip_section)
    return HTMLResponse(_page("Users", body, "users"))


async def ipalias_save(request: Request):
    f = await _form(request)
    ip = (f.get("ip", "") or "").strip()
    name = (f.get("name", "") or "").strip()
    if ip:
        store.set_ip_alias(ip, name)
        logger.info(f"ui: ip alias '{ip}' → '{name or '(cleared)'}'")
    return RedirectResponse("/ui/users", status_code=303)


async def ipalias_del(request: Request):
    ip = (request.query_params.get("ip", "") or "").strip()
    if ip:
        store.delete_ip_alias(ip)
        logger.info(f"ui: ip alias '{ip}' deleted")
    return RedirectResponse("/ui/users", status_code=303)


async def users_save(request: Request):
    # parse_qs directly: the model allow-list is a multi-value field (one per checkbox),
    # which _form() would collapse to a single value.
    qs = parse_qs((await request.body()).decode("utf-8", "replace"), keep_blank_values=True)
    g = lambda k, d="": (qs.get(k) or [d])[-1]
    name = g("name").strip()
    if not name:
        return RedirectResponse("/ui/users?new=1", status_code=303)
    orig = g("orig").strip()
    u = dict(store.get_user(orig) or store.get_user(name) or {})
    u["name"] = name
    u["role"] = (g("role", "user") or "user").strip()
    u["enabled"] = bool(qs.get("enabled"))
    q = g("quota_req_day").strip()
    u["quota_req_day"] = int(q) if q.isdigit() else None
    qc = g("quota_cost_month").strip()
    try:
        u["quota_cost_month"] = float(qc) if qc else None
    except ValueError:
        u["quota_cost_month"] = None
    u["models"] = [m for m in qs.get("model", []) if m]
    ak = g("api_key").strip()
    if ak:
        u["api_key"] = ak
    if orig and orig != name and store.get_user(orig) is not None:
        store.delete_user(orig)
    store.upsert_user(u)
    _apply_users()
    logger.info(f"ui: user '{name}' saved (role={u['role']}, models={len(u['models'])})")
    return RedirectResponse("/ui/users", status_code=303)


async def users_del(request: Request):
    name = (request.query_params.get("name", "") or "").strip()
    if name:
        store.delete_user(name)
        _apply_users()
        logger.info(f"ui: user '{name}' deleted")
    return RedirectResponse("/ui/users", status_code=303)


# Server settings, split by when they take effect. Each: (key, kind, label, note).
_SRV_RUNTIME = [
    ("health_check_interval", "int", "health check interval", "seconds"),
    ("max_concurrent", "int", "default max_concurrent", "blank = unlimited"),
]
_SRV_RESTART = [
    ("__grp", "", "Gateway", ""),
    ("port", "int", "port", "set by launch cmd (uvicorn --port / systemd)"),
    ("__grp", "", "Stats (call log)", ""),
    ("stats_enabled", "bool", "enabled", "record calls (dashboard in Statistic tab)"),
    ("stats_db_path", "text", "db path", ""),
    ("stats_retention_days", "int", "retention days", "0 = keep forever"),
    ("__grp", "", "Jobs (image/video generation)", ""),
    ("jobs_enabled", "bool", "enabled", "auto-on when image models exist"),
    ("jobs_db_path", "text", "db path", ""),
    ("jobs_blob_dir", "text", "blob dir", ""),
    ("jobs_default_ttl_s", "int", "default TTL", "hours — how long a generation job + its images are kept before deletion"),
    ("jobs_prune_interval_s", "int", "prune interval", "minutes — how often expired jobs are purged"),
]
_SRV_RESTART_KEYS = [k for k, kind, *_ in _SRV_RESTART if kind]
# These settings are stored (and used) in SECONDS but shown/edited in a friendlier unit.
_SRV_UNITS = {"jobs_default_ttl_s": 3600, "jobs_prune_interval_s": 60}


def _srv_disp(k, v):
    """Seconds-stored setting → its display unit (TTL→hours, prune→minutes)."""
    if k in _SRV_UNITS and isinstance(v, (int, float)) and v not in ("", None):
        q = v / _SRV_UNITS[k]
        return int(q) if q == int(q) else round(q, 2)
    return v


async def server_page(request: Request):
    si = _server_info()
    eff, rt = si.get("effective", {}), si.get("runtime", {})
    running_port = request.url.port      # the port THIS request hit = the gateway port
    saved = request.query_params.get("saved")

    def rdiff(key):                      # configured value differs from what's running
        return key in rt and str(eff.get(key)) != str(rt.get(key))
    port_diff = bool(running_port and str(eff.get("port")) != str(running_port))
    any_restart = port_diff or any(rdiff(k) for k in _SRV_RESTART_KEYS if k != "port")
    mark = lambda cond: (" " + _badge("↻ restart", "warn")) if cond else ""
    note = lambda n: f" <span class='muted'>{_esc(n)}</span>" if n else ""

    banner = ""
    if saved == "1":
        banner = "<p class='ok-banner'>✓ Saved — runtime settings applied live.</p>"
    elif saved == "restart":
        banner = "<p class='bad'>✓ Saved — port/stats/jobs changes need a <b>restart</b> to apply.</p>"

    runtime_rows = (
        _field("API key (client auth)",
               _inp("api_key", "", placeholder=("•••• set — blank keeps it" if eff.get("api_key_set")
                                                else "unset — clients need no key")))
        + "".join(_field(lbl, _inp(k, "" if eff.get(k) in (None, "") else eff.get(k), typ="number") + note(n))
                  for k, kind, lbl, n in _SRV_RUNTIME)
        + _field("flags",
                 _checkbox("log_per_call", bool(eff.get("log_per_call")), "log_per_call",
                           "one log line per forwarded request")
                 + _checkbox("model_prefix", bool(eff.get("model_prefix")), "model_prefix",
                             "list models as backend/model in /v1/models")))
    runtime_form = (
        '<form action="/ui/server/save" method="post"><input type="hidden" name="_form" value="runtime">'
        f'<div class="formbar"><h2>Runtime settings</h2>{_btn("Save", submit=True)}</div>'
        "<p class='hint'>Applied immediately on Save (no restart).</p>" + runtime_rows + "</form>")

    restart_rows = ""
    for k, kind, lbl, n in _SRV_RESTART:
        if kind == "":                                   # subsection header
            restart_rows += f'<div class="grouphdr">{_esc(lbl)}</div>'
        elif kind == "bool":
            restart_rows += _field(lbl, _checkbox(k, bool(eff.get(k)), "enabled", n) + mark(rdiff(k)))
        else:
            d = port_diff if k == "port" else rdiff(k)
            restart_rows += _field(lbl, _inp(k, _srv_disp(k, eff.get(k, "")), typ=("number" if kind == "int" else "text"))
                                   + mark(d) + note(n))
    restart_form = (
        '<form action="/ui/server/save" method="post"><input type="hidden" name="_form" value="restart">'
        f'<div class="formbar"><h2>Restart-required{mark(any_restart)}</h2>{_btn("Save", submit=True)}</div>'
        f"<p class='hint'>Gateway is listening on port <b>{_esc(running_port or '?')}</b>. "
        "These take effect on the next restart.</p>" + restart_rows + "</form>")

    info = ("<h2>Server</h2><p class='hint'>These override <code>config.yaml</code> and are stored in "
            "the gateway (API key encrypted at rest), so config.yaml only needs backends, aliases and "
            "the launch port.</p>")
    body = (info + banner
            + f'<div class="cols"><div class="col">{runtime_form}</div>'
            f'<div class="col">{restart_form}</div></div>')
    return HTMLResponse(_page("Server", body, "server"))


async def server_save(request: Request):
    f = await _form(request)
    which = f.get("_form", "")
    spec = _SRV_RUNTIME if which == "runtime" else _SRV_RESTART
    bools = {"log_per_call", "model_prefix"} if which == "runtime" else set()
    vals = {}
    for k, kind, *_ in spec:
        if not kind:
            continue
        if kind == "bool":
            vals[k] = bool(f.get(k))
        elif kind == "int":
            raw = (f.get(k, "") or "").strip()
            try:                                          # unit fields (TTL/prune) edited in hours/min → store seconds
                vals[k] = int(round(float(raw) * _SRV_UNITS.get(k, 1)))
            except ValueError:
                vals[k] = ""
        else:
            vals[k] = (f.get(k, "") or "").strip()
    if which == "runtime":
        for b in bools:
            vals[b] = bool(f.get(b))
        ak = (f.get("api_key", "") or "").strip()
        if ak:
            vals["api_key"] = ak
    store.set_settings(vals)
    _apply_server_settings()
    logger.info(f"ui: server settings saved ({which}: {', '.join(vals)})")
    return RedirectResponse(f"/ui/server?saved={'1' if which == 'runtime' else 'restart'}",
                            status_code=303)


# ── Registration ────────────────────────────────────────────────────────────────

def register(app) -> None:
    app.middleware("http")(_ui_guard)          # admin-login guard for /ui
    app.add_api_route("/", root_redirect, methods=["GET"], include_in_schema=False)
    app.add_api_route("/ui", ui_root, methods=["GET"], include_in_schema=False)
    app.add_api_route("/ui/login", login_page, methods=["GET"])
    app.add_api_route("/ui/login", login_post, methods=["POST"])
    app.add_api_route("/ui/logout", logout, methods=["GET"])
    app.add_api_route("/ui/backends", backends_page, methods=["GET"])
    app.add_api_route("/ui/backends/save", backend_save, methods=["POST"])
    app.add_api_route("/ui/backends/delete", backend_del, methods=["GET"])
    app.add_api_route("/ui/input", input_page, methods=["GET"])
    app.add_api_route("/ui/routing", routing_page, methods=["GET"])
    app.add_api_route("/ui/chat/create", chat_create, methods=["POST"])
    app.add_api_route("/ui/chat/save", chat_save, methods=["POST"])
    app.add_api_route("/ui/chat/badd", chat_badd, methods=["GET"])
    app.add_api_route("/ui/chat/bdel", chat_bdel, methods=["GET"])
    app.add_api_route("/ui/chat/delete", chat_del, methods=["GET"])
    app.add_api_route("/ui/chatplay", chatplay_page, methods=["GET"])
    app.add_api_route("/ui/chatplay/send", chatplay_send, methods=["POST"])
    app.add_api_route("/ui/mapping", mapping_page, methods=["GET"])
    app.add_api_route("/ui/mapping/register", register_post, methods=["POST"])
    app.add_api_route("/ui/mapping/update-workflow", update_workflow, methods=["POST"])
    app.add_api_route("/ui/mapping/field-add", edit_add, methods=["GET"])
    app.add_api_route("/ui/mapping/field-map", field_map, methods=["GET"])
    app.add_api_route("/ui/mapping/field-clear", field_clear, methods=["GET"])
    app.add_api_route("/ui/mapping/field-del", edit_del, methods=["GET"])
    app.add_api_route("/ui/mapping/cand-add", cand_add, methods=["GET"])
    app.add_api_route("/ui/mapping/cand-del", cand_del, methods=["GET"])
    app.add_api_route("/ui/mapping/field-order", field_order, methods=["GET"])
    app.add_api_route("/ui/mapping/update", update, methods=["POST"])
    app.add_api_route("/ui/mapping/copy", copy, methods=["GET"])
    app.add_api_route("/ui/mapping/delete", delete, methods=["GET"])
    app.add_api_route("/ui/playground", playground_page, methods=["GET"])
    app.add_api_route("/ui/playground/generate", generate, methods=["POST"])
    app.add_api_route("/ui/playground/result/{job_id}/{n}", result, methods=["GET"])
    app.add_api_route("/ui/playground/status/{job_id}", playground_status, methods=["GET"])
    app.add_api_route("/ui/jobs", jobs_page, methods=["GET"])
    app.add_api_route("/ui/job/{job_id}", job_detail_page, methods=["GET"])
    app.add_api_route("/ui/job/{job_id}/input/{n}", job_input, methods=["GET"])
    app.add_api_route("/ui/dashboard", dashboard_page, methods=["GET"])
    app.add_api_route("/ui/statistic", statistic_page, methods=["GET"])
    app.add_api_route("/ui/call/{call_id}", call_view, methods=["GET"])
    app.add_api_route("/ui/users", users_page, methods=["GET"])
    app.add_api_route("/ui/users/save", users_save, methods=["POST"])
    app.add_api_route("/ui/users/delete", users_del, methods=["GET"])
    app.add_api_route("/ui/ipalias/save", ipalias_save, methods=["POST"])
    app.add_api_route("/ui/ipalias/delete", ipalias_del, methods=["GET"])
    app.add_api_route("/ui/server", server_page, methods=["GET"])
    app.add_api_route("/ui/server/save", server_save, methods=["POST"])
