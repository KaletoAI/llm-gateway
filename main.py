import asyncio
import base64
import calendar
import json
import logging
import re
import time
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, Optional
from urllib.parse import urlparse

import httpx
import uvicorn
import yaml
from fastapi import FastAPI, Header, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse
from watchfiles import awatch

import admin
import jobs
import reasoning
import stats
import store
from adapters import (AdapterContext, NormalizedRequest, image_params, is_image_field,
                      make_adapter, slot_empty_mode)
from openai_image_bridge import (EDIT_KNOWN, OAI_IMG_KEYS, coerce_scalar, gen_done_or_502,
                                 images_response, images_uploads, multipart_list, parse_size)
from responses_bridge import (chat_to_responses, response_shell, responses_stream,
                              responses_to_chat)

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger(__name__)

# ── Config ────────────────────────────────────────────────────────────────────

CONFIG_PATH = Path("config.yaml")

def load_config() -> None:
    """Read config.yaml and (re)bind module-level config values."""
    global config, config_backends, config_virtual_models, virtual_models
    global health_check_interval, api_key
    global stats_cfg, log_per_call, model_prefix, max_concurrent_default
    global image_models, jobs_cfg
    with open(CONFIG_PATH) as f:
        config = yaml.safe_load(f)
    config_backends = sorted(config["backends"], key=lambda b: b["priority"])
    # Chat aliases: config is the base, UI-managed store entries merge over it (see
    # rebuild_virtual_models). `virtual_models` is the effective dict the router reads.
    config_virtual_models = config.get("virtual_models", {})
    virtual_models = dict(config_virtual_models)
    health_check_interval = config.get("health_check_interval", 30)
    api_key = config.get("api_key")
    stats_cfg = config.get("stats") or {}
    log_per_call = config.get("log_per_call", True)
    model_prefix = config.get("model_prefix", True)
    # Global in-flight cap applied to every backend that doesn't set its own
    # `max_concurrent`. None = unlimited (legacy behaviour).
    max_concurrent_default = config.get("max_concurrent")
    # Generation (image/video/tts) aliases → ordered backend+workflow candidates.
    # Kept separate from `virtual_models` (LLM routing) in Phase 1; unified into
    # the capability router in Phase 3. Hot-reloaded. `jobs` is startup-only.
    image_models = config.get("image_models") or {}
    jobs_cfg = config.get("jobs") or {}


def log_config_summary() -> None:
    logger.info(f"Loaded {len(backends)} backend(s):")
    for b in backends:
        state = "ENABLED " if is_enabled(b) else "DISABLED"
        cap = backend_max_concurrent(b)
        cap_s = f"  max_concurrent={cap}" if cap is not None else ""
        logger.info(f"  [{state}] {b['name']:25} priority={b['priority']}  url={b['url']}{cap_s}")
    logger.info(f"Loaded {len(virtual_models)} virtual alias(es):")
    for alias, mapping in virtual_models.items():
        if isinstance(mapping, dict):
            for bname, entry in mapping.items():
                if isinstance(entry, dict):
                    real, prio = entry.get("model"), entry.get("priority")
                    suffix = f"  (priority={prio})" if prio is not None else ""
                    logger.info(f"  {alias:15} → [{bname}] {real}{suffix}")
                else:
                    logger.info(f"  {alias:15} → [{bname}] {entry}")
        else:
            logger.info(f"  {alias:15} → {mapping}  (all backends)")
    logger.info(f"health_check_interval={health_check_interval}s  api_key={'set' if api_key else 'unset'}")


# Initial load — populates module globals
config: dict
config_backends: list[dict]                                    # backends from config.yaml
backends: list[dict]                                           # effective = config + store (UI-added)
config_virtual_models: dict                                    # chat aliases from config.yaml
virtual_models: dict                                           # effective = config + store (UI-managed)
health_check_interval: int
api_key: Optional[str]
stats_cfg: dict
log_per_call: bool
model_prefix: bool
max_concurrent_default: Optional[int]
image_models: dict
jobs_cfg: dict
load_config()
backends = list(config_backends)                              # store merges in once it's active (lifespan)


def backend_host(b: dict) -> str:
    """The physical box a backend runs on: the explicit `host` field, else the
    URL's hostname/IP. Backends sharing an IP group automatically — k12/evo run
    llama-swap AND ComfyUI on one GPU, and host-level policies (see
    docs/host-coordination-plan.md) need to know they belong together."""
    h = (b.get("host") or "").strip()
    if h:
        return h
    try:
        return urlparse(b.get("url") or "").hostname or b["name"]
    except Exception:
        return b["name"]


def rebuild_backends() -> None:
    """Effective backend list = config backends, with UI-added (store) backends
    merged in by name (store overrides config for the same name). Re-sorted by
    priority. Also rebuilds the host grouping maps. Call after config reload or
    any store backend change."""
    global backends, backend_hosts, host_backends
    merged = {backend_id(b): b for b in config_backends}
    if store.is_active():
        for b in store.list_backends():
            merged[backend_id(b)] = b      # store overrides config per (name, type)
    backends = sorted(merged.values(), key=lambda b: b.get("priority", _DEFAULT_PRIORITY))
    backend_hosts = {backend_id(b): backend_host(b) for b in backends}
    host_backends = {}
    for bid, h in backend_hosts.items():
        host_backends.setdefault(h, []).append(bid)
    apply_hosts()
    rebuild_route_index()                  # backend set/enabled flags changed


def apply_hosts() -> None:
    """Refresh the per-host settings cache (labels + policy flags) from the store —
    read on the request path by _media_busy_hosts, so no DB hit per request."""
    global hosts_meta
    hosts_meta = store.get_hosts() if store.is_active() else {}


def _media_busy_hosts() -> set:
    """Hosts whose ComfyUI backend is generating RIGHT NOW. Their LLM siblings
    share the box's GPU — a llama-swap model (re)load then aborts on the VRAM
    ComfyUI holds (measured on k12-gpu, see docs/host-coordination-plan.md).
    resolve_routes sorts chat candidates on these hosts LAST (never drops them:
    with alternatives the collision never happens, without them best effort +
    the 502-failover still apply). Per-host opt-out: avoid_llm_during_media
    false (Hosts editor); absent = on — it only ever bites shared boxes."""
    out = set()
    for bid, n in backend_inflight.items():
        if n > 0 and bid.startswith("comfyui:"):
            h = backend_hosts.get(bid)
            if h and (hosts_meta.get(h) or {}).get("avoid_llm_during_media", True):
                out.add(h)
    return out


def rebuild_virtual_models() -> None:
    """Effective chat aliases = config `virtual_models`, with UI-managed (store)
    entries merged over them by alias (store overrides config for the same name).
    Also refreshes the per-alias park times and reasoning defaults. Call after
    config reload or any store chat-alias change."""
    global virtual_models, alias_park_s, alias_reasoning, alias_voice
    merged = dict(config_virtual_models)
    if store.is_active():
        merged.update(store.list_chat_aliases())
    virtual_models = merged
    park = dict(config.get("alias_park") or {}) if isinstance(config, dict) else {}
    if store.is_active():
        park.update(store.get_alias_park())
    alias_park_s = park
    alias_reasoning = store.get_alias_reasoning() if store.is_active() else {}
    alias_voice = store.get_alias_voice() if store.is_active() else {}
    apply_voice_library()
    apply_reasoning_rules()                # refresh the store-backed reasoning rule cache
    rebuild_route_index()                  # alias mappings changed

# ── State ─────────────────────────────────────────────────────────────────────

_DEFAULT_PRIORITY = 100                 # backends without an explicit priority sort last-ish

backend_models: dict[str, set[str]] = {}                       # name → {model_id, ...}
backend_healthy: dict[str, bool] = {}                          # name → bool
backend_pricing: dict[str, dict[str, dict[str, float]]] = {}   # name → {model_id → {input, output}}
backend_loras: dict[str, set[str]] = {}                        # id → {lora filename, ...} (ComfyUI)
backend_inflight: dict[str, int] = {}                          # name → current in-flight requests
backend_hosts: dict[str, str] = {}                             # bid → host (explicit `host` or URL IP)
host_backends: dict[str, list] = {}                            # host → [bid, …] (rebuild_backends)
hosts_meta: dict[str, dict] = {}                               # host → {label, avoid_llm_during_media, …}
backend_adapters: dict = {}                                    # name → BackendAdapter instance

# ── Health / Discovery ────────────────────────────────────────────────────────

def is_enabled(backend: dict) -> bool:
    return backend.get("enabled", True)


def backend_id(backend: dict) -> str:
    """Stable unique key for a backend = type:name. The *name* is only a display label
    and a type-scoped routing reference, so an LLM and a ComfyUI backend may share a
    name. All runtime state (models/health/inflight/adapters) is keyed by this id."""
    return f'{backend.get("type", "openai")}:{backend["name"]}'


def enabled_backends() -> list[dict]:
    return [b for b in backends if is_enabled(b)]


def backend_auth_headers(backend: dict) -> dict:
    key = backend.get("api_key")
    return {"authorization": f"Bearer {key}"} if key else {}


# ── In-flight cap / "busy" routing ─────────────────────────────────────────────
# Per-backend live request counter. A backend at/above its `max_concurrent` cap is
# "busy": priority routing skips it (spilling to the next backend) and the routing
# dashboard flags it. Lets one slow llama.cpp box (--parallel 1) shed concurrent
# load onto the rest of the fleet instead of queueing/overflowing.

def backend_max_concurrent(backend: dict) -> Optional[int]:
    """In-flight cap for this backend: its own `max_concurrent`, else the global
    default, else None (unlimited)."""
    v = backend.get("max_concurrent", max_concurrent_default)
    return v if isinstance(v, int) and v > 0 else None


def backend_busy(backend: dict) -> bool:
    """True when the backend is at/above its in-flight cap → temporarily skipped."""
    cap = backend_max_concurrent(backend)
    return cap is not None and backend_inflight.get(backend_id(backend), 0) >= cap


# ── Graceful drain (take a backend offline once idle) ─────────────────────────
# A draining backend takes NO new requests (excluded from routing) but lets its
# in-flight requests finish; once in-flight hits 0 it is disabled (persisted) — so a
# backend can be pulled for maintenance without aborting running requests.
_draining: set = set()                  # backend ids currently draining


def is_draining(backend: dict) -> bool:
    return backend_id(backend) in _draining


def _inflight_inc(name: str) -> None:
    backend_inflight[name] = backend_inflight.get(name, 0) + 1


def _inflight_dec(name: str) -> None:
    backend_inflight[name] = max(0, backend_inflight.get(name, 0) - 1)
    if name in _draining and backend_inflight.get(name, 0) <= 0:
        _finalize_drain(name)            # last in-flight request finished → go offline
    _notify_slot_free()


# ── Live LLM-call registry (dashboard "running calls") ────────────────────────
# Currently-running chat/completions/embeddings forwards, so the console can show
# what's in flight right now. Registered when dispatch starts, dropped on
# completion (incl. the streamed-finally) — same lifecycle as the in-flight
# counter. Once finished, the call lands in stats; the dashboard's 5-minute
# "recently ended" view reads from there, so this holds only the live set.
_active_calls: dict = {}                 # token → {alias, model, backend, source, endpoint, stream, started}
_active_seq: list = [0]


def _active_register(meta: dict) -> int:
    _active_seq[0] += 1
    token = _active_seq[0]
    _active_calls[token] = {**meta, "started": time.time()}
    return token


def _active_done(token) -> None:
    _active_calls.pop(token, None)


# ── Call parking (a queue: hold instead of 503 when all backends are busy) ─────
# Parking is the DEFAULT for chat: when every backend mapping an alias is at its
# in-flight cap, the call is held in a FIFO queue until a mapping backend frees
# (then dispatched) or its park time elapses (→ 503). Each entry stays in `_parked`
# for its whole wait — keeping its FIFO position and staying visible in the console.
# `_inflight_dec` wakes all waiters in order; the event loop is single-threaded, so
# the oldest resumes first and claims the freed slot (dispatch increments in-flight
# before its first await), the rest re-check and wait again. Park time is per-alias
# (`alias_park_s`), else the global default below; 0 disables parking for an alias.
park_timeout_s: float = 60.0            # global default park time (Server tab); per-alias overrides in alias_park_s
async_park_timeout_s: float = 600.0
max_parked: int = 100
alias_park_s: dict = {}                 # alias → park seconds (config + store); absent → default, 0 → off
alias_reasoning: dict = {}              # alias → "off"|"on" default (store); absent → auto. Client wins.
alias_voice: dict = {}                  # alias → {voice, ref_text} TTS defaults (store). Client wins.
voice_library: dict = {}                # name → {ref_text, file, remote, shipped} (store voice_library)
_parked: list = []                     # ordered FIFO of live parked-call entries (rich, for the console)
_park_seq: list = [0]

# Normalized reasoning rules (store-backed, UI-editable). Cached here and refreshed on
# save so the resolver doesn't hit the DB per request. See reasoning.py.
reasoning_rules: list = []


def apply_reasoning_rules() -> None:
    global reasoning_rules
    reasoning_rules = store.get_reasoning_rules() if store.is_active() else []


# Shared outbound HTTP client — ONE connection pool (keep-alive, no TLS re-handshake)
# for every proxied call, discovery poll, and ComfyUI helper. Constructed at import
# (connections open lazily), closed in lifespan shutdown. Carries only a safety-net
# timeout: every hot call site passes its own `timeout=` per request.
http_client = httpx.AsyncClient(
    limits=httpx.Limits(max_connections=200, max_keepalive_connections=50,
                        keepalive_expiry=30.0),
    timeout=httpx.Timeout(30.0),
)


def _next_park_id() -> int:
    _park_seq[0] += 1
    return _park_seq[0]


def _park_time_for(alias: str) -> float:
    v = alias_park_s.get(alias)
    if v is None:
        return park_timeout_s
    try:
        return max(0.0, float(v))
    except (TypeError, ValueError):
        return park_timeout_s


def _notify_slot_free() -> None:
    # Wake every parked call in FIFO order; each re-checks its own alias's routes.
    # The oldest resumes first and claims the freed slot; the rest wait again.
    for entry in _parked:
        ev = entry.get("event")
        if ev is not None and not ev.is_set():
            ev.set()


async def refresh_backend(backend: dict, client: httpx.AsyncClient) -> None:
    """Poll a backend's capabilities via its adapter and update discovery state.

    Discovery is protocol-specific (delegated to the adapter); the up/down
    logging and the module-global state (`backend_models` / `backend_pricing` /
    `backend_healthy`) stay owned here so routing keeps a single source of truth.
    """
    bid, label = backend_id(backend), backend["name"]
    adapter = backend_adapters.get(bid)
    if adapter is None:
        return
    try:
        caps = await adapter.discover(client)
        changed = caps.models != backend_models.get(bid)
        if changed and store.is_active():
            await asyncio.to_thread(store.save_backend_models, bid, caps.models)  # persist on change
        backend_models[bid] = caps.models
        backend_pricing[bid] = caps.pricing
        backend_loras[bid] = getattr(caps, "loras", set()) or set()
        if changed:
            rebuild_route_index()          # model set changed → refresh routing candidates
        if not backend_healthy.get(bid):
            logger.info(f"[{label}] UP  — {len(caps.models)} models, {len(caps.pricing)} priced")
        backend_healthy[bid] = True
    except Exception as e:
        if backend_healthy.get(bid, True):
            logger.warning(f"[{label}] DOWN — {e}")
        backend_healthy[bid] = False
        backend_pricing[bid] = {}
        backend_loras[bid] = set()
        # backend_models is intentionally NOT cleared — keep the last-known (persisted)
        # set so a bare model id still resolves to this offline backend → 503, not 403.


async def health_loop() -> None:
    while True:
        for backend in enabled_backends():
            await refresh_backend(backend, http_client)
        await asyncio.sleep(health_check_interval)


def reload_config() -> None:
    """Re-read config.yaml and apply. Keeps old config on parse error."""
    old_ids = {backend_id(b) for b in backends}
    try:
        load_config()
    except Exception as e:
        logger.error(f"Config reload FAILED, keeping previous config: {e}")
        return
    rebuild_backends()                 # re-merge config + store backends
    rebuild_virtual_models()           # re-merge config + store chat aliases
    apply_server_settings()            # re-apply UI server overrides over fresh config
    rebuild_users()                    # reload multi-user identities
    # Drop state for backends removed from config
    new_ids = {backend_id(b) for b in backends}
    for stale in old_ids - new_ids:
        backend_healthy.pop(stale, None)
        backend_models.pop(stale, None)
        backend_pricing.pop(stale, None)
        backend_loras.pop(stale, None)
        backend_inflight.pop(stale, None)
        logger.info(f"  removed backend [{stale}] — state cleared")
    build_backend_adapters()       # rebind adapters to the new backend dicts
    logger.info("Config reloaded.")
    log_config_summary()


async def watch_config_loop() -> None:
    async for _ in awatch(CONFIG_PATH):
        logger.info(f"Detected change in {CONFIG_PATH} — reloading")
        reload_config()


@asynccontextmanager
async def lifespan(app: FastAPI):
    logger.info("Starting LLM Gateway")

    # Writable store backs all UI-managed state (backends, chat aliases, generation
    # aliases) — always on so the /ui console works regardless of image_models.
    # Brought up BEFORE discovery so UI-added (store) backends are merged in and
    # discovered too.
    store.init(jobs_cfg.get("store_path", "store.db"))
    store.bootstrap(image_models)
    backend_models.update(store.load_backend_models())   # seed last-known models (offline → 503, not 403)
    apply_server_settings()            # overlay UI-managed server settings onto config
    rebuild_users()                    # load multi-user identities from the store
    rebuild_backends()                 # merge UI-added backends from the store
    rebuild_virtual_models()           # merge UI-managed chat aliases from the store
    build_backend_adapters()           # rebind adapters to the merged backend list
    logger.info("UI: console at /ui")

    # Generation job store: on whenever image_models are configured (or forced via
    # `jobs.enabled`).
    jobs_prune_task: Optional[asyncio.Task] = None
    if image_models or jobs_cfg.get("enabled"):
        jobs.init(jobs_cfg.get("db_path", "jobs.db"),
                  jobs_cfg.get("blob_dir", "jobs"),
                  jobs_cfg.get("default_ttl_s", 86400))
        jobs_prune_task = asyncio.create_task(jobs.prune_loop(jobs_cfg.get("prune_interval_s", 3600)))

    log_config_summary()
    await asyncio.gather(*[refresh_backend(b, http_client) for b in enabled_backends()])
    health_task = asyncio.create_task(health_loop())
    watch_task = asyncio.create_task(watch_config_loop())

    # Stats: record calls + prune, but NO separate server — the dashboard lives in
    # /ui → Statistic now (so no extra port/bind).
    prune_task: Optional[asyncio.Task] = None
    if stats_cfg.get("enabled"):
        stats.init(stats_cfg.get("db_path", "stats.db"), stats_cfg.get("blob_dir", "calls"))
        prune_task = asyncio.create_task(stats.prune_loop(stats_cfg.get("retention_days", 0)))
        logger.info("stats: recording on; dashboard at /ui → Statistic")
    # snapshot the restart-only server state actually in effect, so the UI can flag
    # when an edited setting needs a restart to apply.
    _server_runtime.update(
        stats_enabled=bool(stats_cfg.get("enabled")),
        stats_db_path=stats_cfg.get("db_path", "stats.db"),
        stats_retention_days=stats_cfg.get("retention_days", 0),
        jobs_enabled=jobs_prune_task is not None,        # actually running
        jobs_db_path=jobs_cfg.get("db_path", "jobs.db"),
        jobs_blob_dir=jobs_cfg.get("blob_dir", "jobs"),
        jobs_default_ttl_s=jobs_cfg.get("default_ttl_s", 86400),
        jobs_prune_interval_s=jobs_cfg.get("prune_interval_s", 3600),
    )

    yield
    health_task.cancel()
    watch_task.cancel()
    if jobs_prune_task is not None:
        jobs_prune_task.cancel()
    if prune_task is not None:
        prune_task.cancel()
    await http_client.aclose()         # drain the shared connection pool last


# ── App ───────────────────────────────────────────────────────────────────────

app = FastAPI(title="LLM Gateway", lifespan=lifespan)
admin.register(app)                     # generation management UI at /ui

# ── Helpers ───────────────────────────────────────────────────────────────────

# ── Multi-user auth ──────────────────────────────────────────────────────────────
# Each request's Bearer token resolves to a user (own key). The global `api_key` acts
# as a master-admin key. **Bootstrap-open**: with no users AND no master key set, the
# gateway is fully open (today's behaviour). A user carries a role, an optional model
# allow-list (empty = all), and an optional per-day request quota.
users: list = []
_users_by_key: dict = {}
_MASTER_ADMIN = {"name": "admin", "role": "admin", "models": [], "_master": True}
_usage: dict = {}                # (user_name, utc_date) → request count (in-memory)


def rebuild_users() -> None:
    global users, _users_by_key
    users = store.list_users() if store.is_active() else []
    _users_by_key = {u["api_key"]: u for u in users if u.get("api_key") and u.get("enabled", True)}


def apply_users() -> None:
    rebuild_users()
    logger.info(f"users changed → {len(users)} user(s)")


def authenticate(authorization: Optional[str]) -> Optional[dict]:
    """Resolve the Bearer token to a user. Returns None only in bootstrap-open mode
    (no users + no master key). Raises 401 on a missing/invalid token otherwise."""
    token = authorization[7:] if authorization and authorization.startswith("Bearer ") else None
    if not users and not api_key:
        return None
    if not token:
        raise HTTPException(401, "Missing Authorization header")
    if api_key and token == api_key:
        return _MASTER_ADMIN
    u = _users_by_key.get(token)
    if u:
        return u
    raise HTTPException(401, "Invalid API key")


def check_auth(authorization: Optional[str]) -> None:
    authenticate(authorization)


def _model_allowed(user: dict, model: Optional[str]) -> bool:
    allow = user.get("models") or []
    if not allow or not model:
        return True                              # empty allow-list = all models
    if model in allow:                           # exact id / chat alias / image alias
        return True
    bname, bare = split_backend_prefix(model)    # backend/model
    if bname and bname in allow:                 # whole-backend grant (prefixed request)
        return True
    if bare in allow:                            # bare model id explicitly granted
        return True
    # Otherwise allow if a GRANTED backend either HOSTS this model (a bare real id) or
    # MAPS it as a virtual alias — so a whole-backend grant also covers the bare ids and
    # the aliases that route to it. Routing then decides availability (503 when the host
    # is offline) instead of a misleading 403. Disabled backends keep their last model set.
    is_alias = model in virtual_models
    for b in backends:
        if b.get("type", "openai") == "comfyui" or b.get("name") not in allow:
            continue
        served = backend_models.get(backend_id(b), set())
        if bare in served:                                       # real model on a granted backend
            return True
        if is_alias and alias_entry(model, b["name"])[0] in served:   # alias → granted backend's model
            return True
    return False


async def gate_request(authorization: Optional[str], request: Request, model: Optional[str]) -> Optional[dict]:
    """Authenticate + enforce model allow-list and per-day quota; attribute the call
    to the user (stats source / job owner read it off request.state). Returns the user
    (None = anonymous bootstrap mode). Async only for the monthly-cost DB scan (run
    off-loop); the daily-quota check+increment stays await-free → atomic."""
    user = authenticate(authorization)
    if user is None:
        return None
    request.state.gw_user = user["name"]
    if not _model_allowed(user, model):
        raise HTTPException(403, f"user '{user['name']}' is not allowed model '{model}'")
    cap = user.get("quota_cost_month")                 # monthly cost/credit quota (E1)
    if cap and stats.is_active():
        t = time.gmtime()
        month_start = calendar.timegm((t.tm_year, t.tm_mon, 1, 0, 0, 0, 0, 0, 0))
        spent = await asyncio.to_thread(stats.month_cost, user["name"], month_start)
        if spent >= float(cap):
            raise HTTPException(402, f"monthly cost quota (${float(cap):.4f}) exceeded for "
                                     f"'{user['name']}' — spent ${spent:.4f}")
    limit = user.get("quota_req_day")
    day = time.strftime("%Y-%m-%d", time.gmtime())
    if limit and _usage.get((user["name"], day), 0) >= int(limit):
        raise HTTPException(429, f"daily request quota ({limit}) exceeded for '{user['name']}'")
    _usage[(user["name"], day)] = _usage.get((user["name"], day), 0) + 1
    return user


def _check_owner(job: Optional[dict], user: Optional[dict], *, status: int, detail: str) -> None:
    """Non-admin users may only touch their own jobs/responses; admin/master/anonymous
    pass. `status` picks the flavor: jobs answer 403, background responses hide foreign
    ids as 404 (no existence leak). Owner None/'default' (legacy/anonymous) is open."""
    if not user or user.get("_master") or user.get("role") == "admin":
        return
    if job and job.get("owner") not in (user.get("name"), None, "default"):
        raise HTTPException(status, detail)


def resolve_admin(key: Optional[str]) -> Optional[dict]:
    """Resolve a bare key to an admin (master api_key or an admin-role user). Drives
    the /ui login. Returns None if the key isn't an admin credential."""
    if not key:
        return None
    if api_key and key == api_key:
        return _MASTER_ADMIN
    u = _users_by_key.get(key)
    return u if (u and u.get("role") == "admin") else None


def ui_locked() -> bool:
    """True once an admin credential exists (master key or an admin user) → /ui needs
    login. Bootstrap-open (no admin anywhere) returns False so you can't lock yourself
    out before setting one up."""
    return bool(api_key) or any(u.get("role") == "admin" for u in users)


def alias_entry(alias: str, backend_name: str) -> tuple[Optional[str], Optional[int]]:
    """(real_model, priority_override) for this alias on this backend.

    real_model is None when the alias isn't mapped to this backend.
    priority_override is None unless this alias sets one for this backend.

    - alias not in virtual_models → (alias, None)         pass-through
    - alias maps to string         → (string, None)        same model everywhere
    - alias maps to dict, value …
        … string                   → (string, None)        per-backend model
        … object {model, priority} → (model, priority)     model + per-alias prio
    """
    mapping = virtual_models.get(alias)
    if mapping is None:
        return alias, None
    if isinstance(mapping, str):
        return mapping, None
    if isinstance(mapping, dict):
        entry = mapping.get(backend_name)
        if isinstance(entry, dict):
            return entry.get("model"), entry.get("priority")
        return entry, None        # string value, or None if backend absent
    return None, None


def resolve_for_backend(alias: str, backend_name: str) -> Optional[str]:
    """Real model name for this alias on this backend, or None if not mapped here."""
    return alias_entry(alias, backend_name)[0]


def split_backend_prefix(model: str) -> tuple[Optional[str], str]:
    """Split a '<backend-name>/<real-model>' id into (backend_name, real_model).

    Returns (None, model) when the first path segment isn't a known backend
    name — so bare aliases ('fast') and vendor-prefixed ids ('moonshotai/Kimi…')
    are left untouched. Backend names (together, openrouter, dx-10-1, …) never
    collide with vendor prefixes, so the first '/' disambiguates cleanly.
    """
    if "/" in model:
        prefix, rest = model.split("/", 1)
        if prefix in _backend_names:
            return prefix, rest
    return None, model


# ── Route index (precomputed candidates; health/busy/drain stay live checks) ────
# The static routing inputs — which enabled LLM backend maps an alias to which real
# model at what effective priority, and which bare model ids pass through — change
# only on config/store edits or a discovery model-set change. They are precomputed
# here (pre-sorted by priority) instead of rescanned and re-sorted on every request;
# resolve_routes() then only evaluates the live flags (healthy / busy / draining).
# Rebuilt by rebuild_backends() / rebuild_virtual_models() and by refresh_backend()
# whenever a backend's model set changes. Swapped atomically (built local, then
# assigned) — safe for the off-loop readers (get_gen_routes runs in a thread).
_backend_names: set = set()            # all backend names — split_backend_prefix test
_llm_backends: list = []               # enabled non-ComfyUI backends, priority order
_comfy_backends: list = []             # enabled ComfyUI backends (generation routing)
_route_index: dict = {}                # alias/model-id → [(backend, real_model)] pre-sorted


def rebuild_route_index() -> None:
    global _backend_names, _llm_backends, _comfy_backends, _route_index
    _backend_names = {b["name"] for b in backends}
    _llm_backends = [b for b in enabled_backends() if b.get("type", "openai") != "comfyui"]
    _comfy_backends = [b for b in enabled_backends() if b.get("type") == "comfyui"]
    index: dict[str, list] = {}
    for alias in virtual_models:                       # aliases (they shadow same-named real ids)
        for b in _llm_backends:
            real, prio = alias_entry(alias, b["name"])
            if real is not None and real in backend_models.get(backend_id(b), set()):
                index.setdefault(alias, []).append(
                    (prio if prio is not None else b["priority"], b, real))
    for b in _llm_backends:                            # bare model ids → pass-through routing
        for mid in backend_models.get(backend_id(b), set()):
            if mid not in virtual_models:
                index.setdefault(mid, []).append((b["priority"], b, mid))
    _route_index = {k: [(b, r) for _, b, r in sorted(rows, key=lambda x: x[0])]
                    for k, rows in index.items()}


def resolve_routes(alias: str) -> tuple[list, list]:
    """(ready, busy) (backend, real_model) candidate lists, each in priority order.

    `ready` is routable now; `busy` maps + serves the alias but sits at its
    in-flight cap (→ parkable). A '<backend>/<model>' alias resolves to a single
    backend in whichever bucket. Drives both normal routing (ready) and call
    parking (busy). Candidates come pre-resolved and pre-sorted from
    `_route_index`; only healthy/busy/draining are evaluated per request.
    """
    bname, bare = split_backend_prefix(alias)
    if bname is not None:
        # chat routing only considers LLM backends, so a name shared with a ComfyUI
        # backend is unambiguous here.
        b = next((b for b in _llm_backends if b["name"] == bname), None)
        if b is None or not backend_healthy.get(backend_id(b)) or is_draining(b):
            return [], []
        real = resolve_for_backend(bare, bname)
        if real is None or real not in backend_models.get(backend_id(b), set()):
            return [], []
        return ([], [(b, real)]) if backend_busy(b) else ([(b, real)], [])

    ready, busy = [], []
    for b, real in _route_index.get(alias, ()):
        if not backend_healthy.get(backend_id(b)) or is_draining(b):
            continue
        (busy if backend_busy(b) else ready).append((b, real))
    if len(ready) > 1:
        # Shared-GPU consideration: candidates whose host is generating media go
        # LAST (stable — priority order kept within both groups), never dropped.
        mb = _media_busy_hosts()
        if mb:
            ready.sort(key=lambda br: backend_hosts.get(backend_id(br[0]), "") in mb)
    return ready, busy


def get_routes_for(alias: str) -> list[tuple[dict, str]]:
    """(backend, real_model) pairs to try, in priority order — ready (non-busy)
    backends only. Thin wrapper over resolve_routes() preserving prior behaviour."""
    return resolve_routes(alias)[0]


def alias_model_conflicts() -> list[dict]:
    """Aliases whose name also exists as a real model id on some backend.

    Setting an alias named like a real model *shadows* that model: a bare
    request for the name routes only via the alias mapping (the pass-through
    that would otherwise reach the real model is disabled), and even the
    '<backend>/<name>' form fails on backends the alias doesn't map (because
    resolve_for_backend returns None there). So any backend that actually hosts
    a model of that exact id but is absent from the alias mapping becomes
    unreachable by that name.

    Returns one entry per colliding alias with the hosting backends split into
    `covered` (in the mapping → still routable) and `shadowed` (hosting the real
    model but not mapped → unreachable by that name). `shadowed` non-empty is the
    actionable conflict; empty means the alias intentionally shadows a model it
    fully covers (e.g. one id mapped across exactly the backends that serve it).
    """
    out = []
    for name in virtual_models:
        hosting = [b["name"] for b in enabled_backends()
                   if b.get("type", "openai") != "comfyui"
                   and name in backend_models.get(backend_id(b), set())]
        if not hosting:
            continue
        covered = [bn for bn in hosting if alias_entry(name, bn)[0] is not None]
        shadowed = [bn for bn in hosting if alias_entry(name, bn)[0] is None]
        out.append({
            "name": name,
            "hosting_backends": hosting,
            "covered": covered,
            "shadowed": shadowed,
        })
    return out


def routing_snapshot() -> dict:
    """Diagnostic view of how every alias and discovered model resolves.

    Unlike get_routes_for(), this keeps unhealthy backends and not-yet-discovered
    models in the result (flagged), so the dashboard shows the full configured
    picture rather than only what's routable right now.
    """
    enabled = enabled_backends()

    aliases = []
    for name in virtual_models:
        rows = []
        for b in backends:                     # all (incl. disabled) LLM backends, so a
            if b.get("type", "openai") == "comfyui":   # mapping doesn't vanish when off
                continue                       # chat aliases route only to LLM backends
            real, prio = alias_entry(name, b["name"])
            if real is None:
                continue                       # alias not mapped to this backend
            bid, enbl = backend_id(b), is_enabled(b)
            healthy = enbl and backend_healthy.get(bid, False)
            present = real in backend_models.get(bid, set())
            busy = enbl and healthy and backend_busy(b)
            rows.append({
                "backend": b["name"],
                "model": real,
                "priority": prio if prio is not None else b["priority"],
                "overridden": prio is not None,
                "enabled": enbl,
                "healthy": healthy,
                "present": present,
                "busy": busy,
                "routable": enbl and healthy and present,
            })
        rows.sort(key=lambda r: r["priority"])
        aliases.append({"alias": name, "routes": rows})
    aliases.sort(key=lambda a: a["alias"].lower())

    model_hosts: dict[str, list] = {}
    for b in enabled:
        healthy = backend_healthy.get(backend_id(b), False)
        for mid in backend_models.get(backend_id(b), set()):
            model_hosts.setdefault(mid, []).append({
                "backend": b["name"],
                "type": b.get("type", "openai"),
                "priority": b["priority"],
                "healthy": healthy,
                "busy": healthy and backend_busy(b),
            })
    models = []
    for mid, hosts in sorted(model_hosts.items(), key=lambda kv: kv[0].lower()):
        hosts.sort(key=lambda h: h["priority"])
        models.append({
            "model": mid,
            "hosts": hosts,
            "shadowed_by_alias": mid in virtual_models,
        })

    return {"aliases": aliases, "models": models, "conflicts": alias_model_conflicts()}


def _source_of(request: Request) -> str:
    u = getattr(request.state, "gw_user", None)        # authenticated user wins
    if u:
        return u
    return request.headers.get("x-source") or (request.client.host if request.client else "unknown")


def _normalize_reasoning(body: dict) -> Optional[str]:
    """Client reasoning control → 'off' | 'on' | None(auto). Pops the gateway control
    field `reasoning` (string, or a Responses-style object {effort}); falls back to the
    OpenAI `reasoning_effort` alias (minimal→off, else→on). `reasoning_effort` itself is
    left in the body (it's a real field a native-effort backend may use)."""
    v = body.pop("reasoning", None)
    if isinstance(v, str):
        v = v.strip().lower()
        if v in ("off", "on"):
            return v
        return None                                  # "auto"/unknown → default
    if isinstance(v, dict):                          # Responses API shape: {"effort": ...}
        eff = v.get("effort")
        if isinstance(eff, str):
            return "off" if eff.strip().lower() == "minimal" else "on"
        return None
    eff = body.get("reasoning_effort")
    if isinstance(eff, str):
        return "off" if eff.strip().lower() == "minimal" else "on"
    return None


def _reasoning_apply(backend: dict, model: Optional[str], requested: Optional[str], payload: dict):
    """Adapter-context hook: resolve the reasoning rule for (backend, model) and apply
    it. Rules live in the store (UI-editable, hot); empty → everything 'unsupported'."""
    if requested not in ("off", "on"):
        return payload, None
    rule = reasoning.resolve(reasoning_rules, backend.get("name", ""), model or "")
    return reasoning.apply(rule, requested, payload)


async def probe_reasoning(backend_name: str, model: str, adapter: str, param: dict,
                          requested: str, prompt: str) -> dict:
    """Live-test a reasoning adapter against ONE (backend, model): fire a baseline call
    and a call with `adapter` applied, and report whether the model's reasoning channel
    was actually suppressed. Goes DIRECT to the backend (bypasses routing + the stored
    rules) so a candidate mechanism can be validated BEFORE a rule exists. Never raises —
    the /ui Reasoning-tab live test drives this. Returns a plain dict for the UI."""
    requested = requested if requested in ("off", "on") else "off"
    b = next((x for x in backends if x.get("name") == backend_name
              and x.get("type", "openai") != "comfyui"), None)
    if b is None:
        return {"error": f"backend '{backend_name}' not found or not an LLM backend"}
    url = f"{b['url']}/v1/chat/completions"
    headers = {"content-type": "application/json", **backend_auth_headers(b)}
    base_body = {"model": model, "stream": False, "temperature": 0.1, "max_tokens": 300,
                 "messages": [{"role": "user",
                               "content": (prompt or "").strip() or "Say hello in one short sentence."}]}
    rule = {"adapter": adapter, "param": param or {}}
    cand_body, control = reasoning.apply(rule, requested, dict(base_body))

    async def _one(body: dict) -> dict:
        try:
            r = await http_client.post(url, headers=headers, json=body, timeout=120.0)
            try:
                data = r.json()
            except Exception:
                data = {"raw": r.text[:600]}
            msg = ((data.get("choices") or [{}])[0].get("message") or {}) if isinstance(data, dict) else {}
            return {"status": r.status_code,
                    "content": (msg.get("content") or ""),
                    "reasoning": (msg.get("reasoning") or msg.get("reasoning_content") or ""),
                    "err": None if r.status_code == 200 else json.dumps(data, ensure_ascii=False)[:400]}
        except Exception as e:
            return {"status": 0, "content": "", "reasoning": "", "err": f"{type(e).__name__}: {e}"}

    # Sequential, not concurrent: single-slot backends (LocalAI / llama.cpp --parallel 1)
    # give unreliable results when baseline + candidate hit them at once.
    base = await _one(base_body)
    cand = await _one(cand_body)
    return {
        "backend": backend_name, "model": model, "adapter": adapter,
        "control": control, "requested": requested,
        "baseline": {"status": base["status"], "reasoning_len": len(base["reasoning"]),
                     "content_len": len(base["content"]), "err": base["err"]},
        "candidate": {"status": cand["status"], "reasoning_len": len(cand["reasoning"]),
                      "content_len": len(cand["content"]),
                      "content_preview": cand["content"][:240], "err": cand["err"]},
    }


# ── Voice reference library (TTS voice cloning) ─────────────────────────────────
# WAV blobs live on the GATEWAY (voiceref/ — gitignored, deploy-excluded) and are
# additionally SHIPPED via scp to the TTS backend host: qwen3-tts-style models read
# `voice` strictly as a local file (measured: no base64/data-URI, no URL, no files
# API). API/UI reference entries as voice:"lib:<name>"; route() resolves that to the
# shipped path + fills ref_text.

VOICE_REF_DIR = Path("voiceref")


def _voice_safe(name: str) -> str:
    keep = "".join(ch if ch.isalnum() or ch in "-_" else "_" for ch in (name or "voice"))
    return keep[:48] or "voice"


def apply_voice_library() -> None:
    global voice_library
    voice_library = store.get_voice_library() if store.is_active() else {}


def voice_ship_config() -> tuple[list[str], str]:
    """(targets, voice_dir) for shipping references.

    `voice_ref_hosts` = comma-separated scp TARGETS 'user@host:/abs/host/dir' — one
    per host serving a cloning model; the host-side dir may differ per host (e.g. a
    docker bind-mount source). `voice_ref_dir` = the dir AS THE MODEL SEES IT (e.g.
    the container path '/models/voices') — this single path goes into `voice`, so
    it must be identical on every host (failover may pick any of them)."""
    s = store.get_settings() if store.is_active() else {}
    targets = [h.strip() for h in str(s.get("voice_ref_hosts") or "").split(",") if h.strip()]
    return targets, str(s.get("voice_ref_dir") or "").rstrip("/")


_whisper_model = None                       # lazy faster-whisper instance (CPU, loaded on first use)


def _local_whisper():
    global _whisper_model
    if _whisper_model is None:
        from faster_whisper import WhisperModel        # heavy import — only on first transcription
        s = store.get_settings() if store.is_active() else {}
        size = str(s.get("whisper_model") or "small")
        _whisper_model = WhisperModel(size, device="cpu", compute_type="int8")
    return _whisper_model


def _whisper_route() -> Optional[tuple[dict, str]]:
    """First healthy LLM backend serving a whisper* model (fallback transcription)."""
    for b in enabled_backends():
        if b.get("type", "openai") == "comfyui":
            continue
        for m in sorted(backend_models.get(backend_id(b), set())):
            if "whisper" in m.lower() and backend_healthy.get(backend_id(b)):
                return b, m
    return None


async def transcribe_audio(data: bytes, filename: str = "ref.wav") -> tuple[Optional[str], str]:
    """(text, source-or-error). Local faster-whisper on the gateway CPU first (small,
    lazy-loaded); a backend's /v1/audio/transcriptions as fallback when the local
    model is unavailable."""
    import io
    try:
        def _run():
            segs, _info = _local_whisper().transcribe(io.BytesIO(data))
            return " ".join(s.text.strip() for s in segs).strip()
        txt = await asyncio.to_thread(_run)
        if txt:
            return txt, "gateway whisper"
    except ImportError:
        pass                                            # not installed → try a backend
    except Exception as ex:
        logger.warning(f"local whisper failed: {type(ex).__name__}: {ex}")
    hit = _whisper_route()
    if hit is None:
        return None, "no local faster-whisper and no whisper model on any backend"
    b, model = hit
    try:
        r = await http_client.post(f"{b['url']}/v1/audio/transcriptions",
                                   headers=backend_auth_headers(b), data={"model": model},
                                   files={"file": (filename, data, "audio/wav")}, timeout=180.0)
        if r.status_code == 200:
            txt = str((r.json() or {}).get("text") or "").strip()
            return (txt or None), f"{b['name']}/{model}"
        return None, f"{b['name']}/{model} HTTP {r.status_code}: {r.text[:150]}"
    except Exception as ex:
        return None, f"{type(ex).__name__}: {ex}"


async def _scp(src: Path, host: str, remote: str) -> tuple[bool, str]:
    try:
        proc = await asyncio.create_subprocess_exec(
            "scp", "-o", "BatchMode=yes", "-o", "ConnectTimeout=8",
            str(src), f"{host}:{remote}",
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT)
        out, _ = await proc.communicate()
        return proc.returncode == 0, (out or b"").decode(errors="replace")[-200:]
    except Exception as ex:
        return False, f"{type(ex).__name__}: {ex}"


async def ship_voice_ref(name: str, notify=None) -> tuple[bool, str]:
    """scp the library WAV to EVERY configured target (host-side dirs may differ —
    docker bind mounts); the entry's `remote` is the MODEL-VISIBLE path
    (voice_ref_dir/<file>), identical across hosts. (all_ok, message) — never
    raises (UI renders the message). `notify(kind, text)` (kind ok|err|run)
    streams per-step progress to the UI poller."""
    nb = notify or (lambda k, t: None)
    e = (store.get_voice_library() if store.is_active() else {}).get(name)
    if not e:
        nb("err", f"unknown voice '{name}'")
        return False, f"unknown voice '{name}'"
    targets, vdir = voice_ship_config()
    bad = [t for t in targets if ":" not in t or not t.split(":", 1)[1].startswith("/")]
    if not targets or bad or not vdir.startswith("/"):
        msg = ("no/invalid ship config — targets are 'user@host:/abs/host/dir' (comma-"
               "separated) + a model-visible voice dir (e.g. /models/voices)")
        nb("err", msg)
        return False, msg
    src = Path(e.get("file") or "")
    if not src.is_file():
        nb("err", f"gateway blob missing: {src}")
        return False, f"gateway blob missing: {src}"
    remote = f"{vdir}/{src.name}"                       # what goes into `voice`
    results = {}
    for t in targets:
        host, hdir = t.split(":", 1)
        hdir = hdir.rstrip("/")
        nb("run", f"upload → {host}")
        try:                                            # scp can't create dirs — mkdir -p first
            proc = await asyncio.create_subprocess_exec(
                "ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=8", host, f"mkdir -p {hdir}",
                stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL)
            await proc.communicate()
        except Exception:
            pass                                        # scp below reports the real error
        ok, msg = await _scp(src, host, f"{hdir}/{src.name}")
        results[t] = "ok" if ok else (msg or "scp failed")
        nb("ok" if ok else "err", f"{host}: {'ok' if ok else (msg or 'scp failed')[:120]}")
    all_ok = all(v == "ok" for v in results.values())
    e.update({"remote": remote if all_ok else e.get("remote", ""),
              "shipped": all_ok, "hosts": results})
    store.set_voice_entry(name, e)
    apply_voice_library()
    summary = " · ".join(f"{t.split('@')[-1].split(':')[0]}: {v if v == 'ok' else v[:80]}"
                         for t, v in results.items())
    return all_ok, (f"voice={remote} · {summary}" if all_ok else summary)


async def save_voice_ref(name: str, data: bytes, ref_text: str = "", notify=None) -> dict:
    """Create/replace a library entry: write the gateway blob, auto-transcribe when
    ref_text is empty (whisper backend, if any), then try to ship. Returns UI status;
    `notify(kind, text)` streams per-step progress (store → whisper → per-host scp)."""
    nb = notify or (lambda k, t: None)
    VOICE_REF_DIR.mkdir(exist_ok=True)
    path = VOICE_REF_DIR / (_voice_safe(name) + ".wav")
    path.write_bytes(data)
    nb("ok", f"stored on the gateway ({len(data) // 1024} KB)")
    note = ""
    if not (ref_text or "").strip():
        nb("run", "whisper — transcribing the reference")
        txt, src = await transcribe_audio(data, path.name)
        ref_text = txt or ""
        note = f"ref_text transcribed via {src}" if txt else f"ref_text empty — {src}"
        nb("ok" if txt else "err", note + (f": “{txt[:80]}…”" if txt and len(txt) > 80
                                           else (f": “{txt}”" if txt else "")))
    store.set_voice_entry(name, {"ref_text": (ref_text or "").strip(), "file": str(path),
                                 "remote": "", "shipped": False})
    apply_voice_library()                         # cache now — ship's early returns don't refresh
    ok, msg = await ship_voice_ref(name, notify=notify)
    return {"shipped": ok, "ship_msg": msg, "note": note,
            "entry": voice_library.get(name) or {}}


def delete_voice_ref(name: str) -> None:
    e = (store.get_voice_library() if store.is_active() else {}).get(name) or {}
    try:
        p = Path(e.get("file") or "")
        if p.is_file():
            p.unlink()
    except OSError:
        pass
    store.set_voice_entry(name, None)
    apply_voice_library()


def _cost_usd(bid: str, model_id: Optional[str], in_tok: int, out_tok: int) -> float:
    """USD cost for a call from cached pricing (Together-style /v1/models), keyed by
    the backend id (`type:name`, same as `backend_pricing`). 0 if unknown."""
    if not model_id:
        return 0.0
    p = backend_pricing.get(bid, {}).get(model_id, {})
    return ((in_tok or 0) * p.get("input", 0.0) + (out_tok or 0) * p.get("output", 0.0)) / 1_000_000


# ── Adapter wiring ─────────────────────────────────────────────────────────────
# Services handed to every backend adapter so it stays import-cycle-free and
# hot-reload-safe (the log flag is read per-call via a callable, never cached).

adapter_ctx = AdapterContext(
    auth_headers=backend_auth_headers,
    inflight_inc=_inflight_inc,
    inflight_dec=_inflight_dec,
    cost_usd=_cost_usd,
    source_of=_source_of,
    record_call=stats.record_call,
    log_enabled=lambda: log_per_call,
    active_register=_active_register,
    active_done=_active_done,
    apply_reasoning=_reasoning_apply,
    http_client=lambda: http_client,   # shared pool; callable so adapters never cache it
)


def build_backend_adapters() -> None:
    """(Re)instantiate one adapter per configured backend. Called at import and
    after every config reload so adapters point at the current backend dicts."""
    backend_adapters.clear()
    for b in backends:
        backend_adapters[backend_id(b)] = make_adapter(b, adapter_ctx)


build_backend_adapters()
rebuild_route_index()                      # initial index (config backends; models fill in via discovery)


async def _dispatch_or_park(alias, path, body, request, stats_endpoint=None, deadline=None):
    """Forward to a ready backend; if all mapping backends are busy, hold the call in
    the park queue until one frees (then dispatch) or 503. Park window = the alias's
    park time, or an explicit `deadline` (background responses use the longer async
    window). Shared by chat routing, the Responses bridge, and background responses."""
    ready, busy = resolve_routes(alias)
    if ready:
        return await _dispatch_over(ready, path, alias, body, request, stats_endpoint=stats_endpoint)
    if not busy:
        raise HTTPException(503, f"No healthy backend for model '{alias}'")
    if deadline is None:
        ptime = _park_time_for(alias)
        if ptime <= 0:                                 # parking disabled for this alias → 503 now
            raise HTTPException(503, f"all backends for '{alias}' are busy (parking disabled)",
                                headers={"Retry-After": "1"})
        deadline = time.monotonic() + ptime
    if len(_parked) >= max_parked:
        raise HTTPException(503, f"park queue full ({max_parked}) — retry later", headers={"Retry-After": "2"})
    return await _park_and_dispatch(alias, path, body, request, deadline,
                                    source=_source_of(request), stats_endpoint=stats_endpoint)


def _retryable_upstream_error(resp) -> bool:
    """A 502 whose body is llama-swap's "unable to start process" — the backend
    is alive but can't load the model right now (VRAM held by another process on
    the shared GPU, measured on k12-gpu). Backend-local by definition, so another
    candidate may well serve the call — treated like connect/timeout in
    _dispatch_over. Plain Response only (streams surface errors that way since
    the adapter opens upstream before answering)."""
    if getattr(resp, "status_code", 0) != 502:
        return False
    return b"unable to start process" in (getattr(resp, "body", b"") or b"")[:300]


async def _dispatch_over(candidates, path, alias, body, request, stats_endpoint=None):
    """Forward to the first candidate, failing over to the next only on
    connect/timeout or a backend-local load failure (_retryable_upstream_error);
    other HTTP errors return as-is. The first candidate's dispatch increments
    in-flight before its first await, so a parked waiter claims that slot
    atomically here. `stats_endpoint` overrides the recorded endpoint label
    (e.g. /v1/responses)."""
    last_error: Exception = Exception("unknown")
    last_resp = None
    for backend, real_model in candidates:
        cand_body = dict(body, model=real_model)  # per-candidate copy — the shared body stays untouched
        adapter = backend_adapters.get(backend_id(backend))
        if adapter is None:                       # config raced a reload — skip
            continue
        try:
            if log_per_call:
                logger.info(f"→ [{backend['name']}] {alias} → {real_model}")
            req = NormalizedRequest(
                path=path, alias=alias, real_model=real_model,
                body=cand_body, raw=request, stream=bool(body.get("stream")),
                stats_endpoint=stats_endpoint, reasoning=body.get("_reasoning"),
            )
            resp = await adapter.dispatch(req)
            if _retryable_upstream_error(resp):
                logger.warning(f"✗ [{backend['name']}] upstream can't start the model (502) — trying next")
                last_resp = resp
                continue
            return resp
        except (httpx.ConnectError, httpx.TimeoutException) as e:
            logger.warning(f"✗ [{backend['name']}] {e} — trying next")
            last_error = e
    if last_resp is not None:                     # every candidate failed to load → the real 502
        return last_resp
    raise HTTPException(503, f"All backends failed: {last_error}")


async def _park_and_dispatch(alias, path, body, request, deadline, source="?", stats_endpoint=None):
    """Hold a request in the park queue until a mapping backend frees (then dispatch),
    or until `deadline` (→ 503). The entry stays in `_parked` for the whole wait so it
    keeps its FIFO position and shows in the console; `_notify_slot_free` wakes it."""
    entry = {"id": _next_park_id(), "alias": alias, "source": source,
             "enqueued": time.time(), "deadline": deadline, "event": asyncio.Event()}
    _parked.append(entry)
    try:
        while True:
            entry["event"].clear()                     # arm before checking → no lost wakeup
            ready, busy = resolve_routes(alias)
            if ready:
                return await _dispatch_over(ready, path, alias, body, request, stats_endpoint=stats_endpoint)
            if not busy:
                raise HTTPException(503, f"No healthy backend for model '{alias}'")
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise HTTPException(503, f"all backends for '{alias}' are busy — parked with no free "
                                    "backend in time", headers={"Retry-After": "2"})
            try:
                await asyncio.wait_for(entry["event"].wait(), remaining)
            except asyncio.TimeoutError:
                raise HTTPException(503, f"all backends for '{alias}' are busy — parked with no free "
                                    "backend in time", headers={"Retry-After": "2"})
    finally:
        try:
            _parked.remove(entry)
        except ValueError:
            pass


async def route(path: str, request: Request, authorization: Optional[str]) -> JSONResponse | StreamingResponse:
    body = await request.json()
    alias = body.get("model", "")
    await gate_request(authorization, request, alias)        # auth + model allow-list + quota
    body.pop("park", None)                              # legacy control field — parking is automatic; never forward
    if path.startswith("/v1/audio/"):
        body.pop("stream", None)                        # audio is a binary passthrough — never SSE
        av = alias_voice.get(alias)
        if av:                                          # per-alias TTS defaults; explicit client fields win
            if av.get("voice") and not body.get("voice"):
                body["voice"] = av["voice"]
            if av.get("ref_text") and not (body.get("params") or {}).get("ref_text"):
                body.setdefault("params", {})["ref_text"] = av["ref_text"]
        v = body.get("voice")                           # voice:"lib:<name>" → shipped path + ref_text
        if isinstance(v, str) and v.startswith("lib:"):
            e = voice_library.get(v[4:])
            if not e:
                raise HTTPException(400, f"unknown voice library entry '{v[4:]}'")
            if not (e.get("shipped") and e.get("remote")):
                raise HTTPException(409, f"voice '{v[4:]}' is not on the backend host yet — "
                                         "configure the scp target / retry ship in the Voice tab")
            body["voice"] = e["remote"]
            if e.get("ref_text") and not (body.get("params") or {}).get("ref_text"):
                body.setdefault("params", {})["ref_text"] = e["ref_text"]
    r = _normalize_reasoning(body)                      # off|on|None; strips `reasoning`, stashes for dispatch
    if r is None:
        r = alias_reasoning.get(alias)                  # per-alias default (tool vs tool-thinking)
    if r is not None:
        body["_reasoning"] = r
    # Sync park is the default: a ready backend dispatches now; all busy → queue until one
    # frees (per-alias park time) or 503. Async is not on chat/completions — it lives on the
    # standard /v1/responses background mode.
    return await _dispatch_or_park(alias, path, body, request)


# The Responses API ↔ Chat Completions translation layer (request/response/
# stream/shell builders) lives in responses_bridge.py — pure functions, no
# gateway state. The endpoints below own dispatch/parking + background mode.


# ── Routes ────────────────────────────────────────────────────────────────────

@app.get("/v1/models")
async def list_models(request: Request, authorization: Optional[str] = Header(None)):
    user = authenticate(authorization)
    # Per-user allow-list FILTERS the catalog (empty = all). Entries may be a model id,
    # a chat/image alias, or a backend name (grants all of that backend's models).
    allow = (user.get("models") if user else None) or []
    typ = (request.query_params.get("type") or "").lower()    # ""=both, "chat", "image"
    now = int(time.time())
    seen: set[str] = set()
    data = []

    def visible(keys: set) -> bool:                           # any grant-key in the allow-list?
        return (not allow) or any(k in allow for k in keys)

    # CHAT/LLM catalog = LLM backends only (ComfyUI "models" are checkpoints, not
    # chat-callable). Names are unique within the LLM type, so the prefix is unambiguous.
    if typ != "image":
        for backend in enabled_backends():
            if (backend.get("type", "openai") == "comfyui" or is_draining(backend)
                    or not backend_healthy.get(backend_id(backend))):
                continue                          # comfy / draining / down → not offered
            bname = backend["name"]
            expose_bare = backend.get("local", False)
            for mid in sorted(backend_models.get(backend_id(backend), set())):
                # model_prefix on → '<backend>/<model>' (provider visible, distinct across
                # backends). off → legacy bare ids deduplicated across backends.
                disp = f"{bname}/{mid}" if model_prefix else mid
                if disp not in seen and visible({disp, mid, bname}):
                    seen.add(disp)
                    data.append({"id": disp, "object": "model", "created": now, "owned_by": bname})
                # `local: true` backends ALSO list the bare id; a bare request routes by
                # priority across every backend that exposes it (like a virtual alias).
                if expose_bare and mid not in seen and visible({mid, bname}):
                    seen.add(mid)
                    data.append({"id": mid, "object": "model", "created": now, "owned_by": bname})
        # Virtual chat aliases are cross-backend → always listed bare (no prefix).
        for alias in virtual_models:
            if alias not in seen and visible({alias}):
                seen.add(alias)
                data.append({"id": alias, "object": "model", "created": now, "owned_by": "llm-gateway (virtual)"})

    # IMAGE generation aliases (separate namespace) — listed so image clients (anima-verse)
    # can discover them; granted by alias name. `?type=image` returns only these.
    if typ != "chat":
        img_aliases = (list((await asyncio.to_thread(store.list_aliases)).keys())
                       if store.is_active() else list(image_models.keys()))
        for alias in img_aliases:
            if alias not in seen and visible({alias}):
                seen.add(alias)
                data.append({"id": alias, "object": "model", "created": now, "owned_by": "llm-gateway (image)"})

    return {"object": "list", "data": data}


@app.get("/v1/models/{model_id:path}")
async def get_model(model_id: str, authorization: Optional[str] = Header(None)):
    check_auth(authorization)
    now = int(time.time())
    if model_id in virtual_models:
        return {"id": model_id, "object": "model", "created": now, "owned_by": "llm-gateway (virtual)"}
    llm = [b for b in enabled_backends() if b.get("type", "openai") != "comfyui"]
    bname, bare = split_backend_prefix(model_id)
    if bname is not None:
        b = next((b for b in llm if b["name"] == bname), None)
        if b is not None and bare in backend_models.get(backend_id(b), set()):
            return {"id": model_id, "object": "model", "created": now, "owned_by": bname}
    else:
        for backend in llm:
            if model_id in backend_models.get(backend_id(backend), set()):
                return {"id": model_id, "object": "model", "created": now, "owned_by": backend["name"]}
    raise HTTPException(404, f"Model '{model_id}' not found")


@app.post("/v1/chat/completions")
async def chat_completions(request: Request, authorization: Optional[str] = Header(None)):
    return await route("/v1/chat/completions", request, authorization)


def _playground_key(name: str) -> Optional[str]:
    """API key the /ui playgrounds use to call the gateway's OWN endpoints as a real
    client (they test the API, so they go through it — dispatch, parking, reasoning,
    stats, quotas). The logged-in admin's own key wins (correct attribution), else the
    master key; None in bootstrap-open mode (anonymous, x-source attributes it)."""
    u = next((u for u in users if u.get("name") == name and u.get("api_key")), None)
    return (u or {}).get("api_key") or api_key


# ── Responses background mode (official async) ────────────────────────────────
# `POST /v1/responses` with `background: true` returns immediately with a queued
# response object; the worker parks/dispatches in the same queue and stores the
# result. Poll `GET /v1/responses/{id}`; cancel `POST /v1/responses/{id}/cancel`.
_bg_tasks: dict = {}                        # response_id → asyncio.Task (for cancellation)
_RESP_PREFIX = "resp_"                      # background response id = resp_<job_id>


def _bg_job_id(response_id: str) -> str:
    """jobs.py id behind a background response id (strips the resp_ prefix)."""
    return response_id[len(_RESP_PREFIX):]


def _dispatch_json(resp) -> dict:
    """Parsed JSON of a non-stream dispatch result. The adapter attaches the body it
    already parsed for usage as `resp.parsed_json` — reuse it instead of re-parsing
    the raw bytes; fall back to the body for foreign Response objects."""
    parsed = getattr(resp, "parsed_json", None)
    if isinstance(parsed, dict):
        return parsed
    try:
        return json.loads(bytes(resp.body)) if getattr(resp, "body", None) else {}
    except Exception:
        return {}


async def _run_bg_response(job_id, rid, alias, chat_body, request, created):
    """Worker for a background response: dispatch (parking in the shared queue up to the
    async window), translate the chat completion → Responses object, store it on the job."""
    try:
        resp = await _dispatch_or_park(alias, "/v1/chat/completions", chat_body, request,
                                       stats_endpoint="/v1/responses",
                                       deadline=time.monotonic() + async_park_timeout_s)
        chat_json = _dispatch_json(resp)
        if getattr(resp, "status_code", 200) >= 400:
            await asyncio.to_thread(jobs.fail, job_id,
                                    f"{resp.status_code}: {(json.dumps(chat_json) or '')[:300]}")
            return
        obj = chat_to_responses(chat_json)
        obj["id"], obj["background"], obj["created_at"] = rid, True, created
        await asyncio.to_thread(jobs.complete_json, job_id, obj,
                                meta={"model": chat_json.get("model")})
    except asyncio.CancelledError:
        jobs.fail(job_id, "cancelled")   # sync on purpose (no await while being cancelled);
        raise                            # GET maps this back to status "cancelled"
    except HTTPException as e:
        await asyncio.to_thread(jobs.fail, job_id, f"{e.status_code}: {e.detail}")
    except Exception as e:                              # never let a background task vanish silently
        logger.warning(f"background response {rid} failed: {e}")
        await asyncio.to_thread(jobs.fail, job_id, str(e))


async def _create_bg_response(alias, chat_body, request) -> JSONResponse:
    if not jobs.is_active():
        raise HTTPException(503, "background responses unavailable (job store off)")
    if len(_parked) >= max_parked:
        raise HTTPException(503, f"too many queued ({max_parked}) — retry later", headers={"Retry-After": "2"})
    owner = getattr(request.state, "gw_user", None) or "default"
    job_id = await asyncio.to_thread(jobs.create, "response", alias, "(background)", owner=owner)
    rid, created = f"{_RESP_PREFIX}{job_id}", int(time.time())
    body = dict(chat_body); body["stream"] = False     # background result is fetched, not streamed
    t = asyncio.create_task(_run_bg_response(job_id, rid, alias, body, request, created))
    _bg_tasks[rid] = t
    t.add_done_callback(lambda _: _bg_tasks.pop(rid, None))
    if log_per_call:
        logger.info(f"→ background response {rid} (alias '{alias}') queued")
    return JSONResponse(response_shell(rid, "queued", alias, created, background=True),
                        status_code=200)


async def _bg_job_for(response_id: str) -> dict:
    if not response_id.startswith(_RESP_PREFIX) or not jobs.is_active():
        raise HTTPException(404, f"response '{response_id}' not found")
    job = await asyncio.to_thread(jobs.get, _bg_job_id(response_id))
    if job is None or job.get("task") != "response":
        raise HTTPException(404, f"response '{response_id}' not found")
    return job


def _bg_owner_check(job: dict, user: Optional[dict]) -> None:
    # non-admin users can only touch their own responses; hide others as 404 (no leak).
    _check_owner(job, user, status=404, detail="response not found")


def _bg_view(response_id: str, job: dict) -> dict:
    status = job["status"]
    created = int(job.get("created") or time.time())
    model = (job.get("meta") or {}).get("model") or job.get("alias")
    shell = lambda st, **kw: response_shell(response_id, st, model, created, background=True, **kw)
    if status == "done":
        return (job.get("results") or [None])[0] or shell("completed")
    if status == "failed":
        err = job.get("error") or "failed"
        if err == "cancelled":
            return shell("cancelled")
        return shell("failed", error={"message": err})
    return shell("in_progress" if status == "running" else "queued")


@app.post("/v1/responses")
async def responses(request: Request, authorization: Optional[str] = Header(None)):
    """OpenAI Responses API → Chat Completions bridge.

    LangChain.js (N8N's AI Agent) calls this endpoint by default. Backends that
    only speak Chat Completions still work — request and response are translated
    transparently. `stream:true` translates the backend's chat SSE into Responses
    SSE (A3); `background:true` runs it async (queued → poll GET /v1/responses/{id}).
    Like chat, a busy backend parks instead of 503.
    """
    raw_body = await request.json()
    chat_body = responses_to_chat(raw_body)
    alias = chat_body.get("model", "")
    await gate_request(authorization, request, alias)        # auth + model allow-list + quota

    r = _normalize_reasoning(raw_body)                 # honors reasoning + reasoning_effort + {effort}
    if r is None:
        r = alias_reasoning.get(alias)                 # per-alias default (tool vs tool-thinking)
    if r is not None:
        chat_body["_reasoning"] = r

    if raw_body.get("background") is True:             # official async: immediate queued object
        return await _create_bg_response(alias, chat_body, request)

    wants_stream = bool(raw_body.get("stream"))
    chat_body["stream"] = wants_stream
    if wants_stream:
        # The bridge consumes the chat usage chunk for `response.completed`;
        # since the adapter now hides it from clients that don't ask (strict
        # OpenAI shape), ask explicitly — it never reaches the client raw.
        chat_body["stream_options"] = {"include_usage": True}
    # Shared dispatch/park path (failover + in-flight + stats, labelled /v1/responses).
    resp = await _dispatch_or_park(alias, "/v1/chat/completions", chat_body, request,
                                   stats_endpoint="/v1/responses")
    if wants_stream:                                   # A3: translate chat SSE → Responses SSE
        if isinstance(resp, StreamingResponse):
            return StreamingResponse(responses_stream(resp, raw_body, alias),
                                     media_type="text/event-stream")
        err = _dispatch_json(resp)
        raise HTTPException(resp.status_code, (json.dumps(err) or "")[:500])
    chat_resp_json = _dispatch_json(resp)
    if resp.status_code >= 400:
        raise HTTPException(resp.status_code, (json.dumps(chat_resp_json) or "")[:500])
    return JSONResponse(chat_to_responses(chat_resp_json), status_code=resp.status_code)


@app.get("/v1/responses/{response_id}")
async def get_response(response_id: str, request: Request, authorization: Optional[str] = Header(None)):
    """Poll a background response: queued → in_progress → completed | failed | cancelled."""
    user = authenticate(authorization)
    job = await _bg_job_for(response_id)
    _bg_owner_check(job, user)
    return JSONResponse(_bg_view(response_id, job))


@app.post("/v1/responses/{response_id}/cancel")
async def cancel_response(response_id: str, request: Request, authorization: Optional[str] = Header(None)):
    """Cancel a background response (no-op if already terminal)."""
    user = authenticate(authorization)
    job = await _bg_job_for(response_id)
    _bg_owner_check(job, user)
    if job["status"] in ("queued", "running"):
        t = _bg_tasks.get(response_id)
        if t and not t.done():
            t.cancel()                                 # stop a parked/in-flight worker at its next await
        # Mark cancelled right away so the response reflects it now — but only if it
        # hasn't just reached a terminal state (cancel racing completion).
        cur = await asyncio.to_thread(jobs.get, _bg_job_id(response_id))
        if cur and cur.get("status") in ("queued", "running"):
            await asyncio.to_thread(jobs.fail, _bg_job_id(response_id), "cancelled")
        job = await _bg_job_for(response_id)           # re-read post-cancel
    return JSONResponse(_bg_view(response_id, job))


@app.post("/v1/completions")
async def completions(request: Request, authorization: Optional[str] = Header(None)):
    return await route("/v1/completions", request, authorization)


@app.post("/v1/embeddings")
async def embeddings(request: Request, authorization: Optional[str] = Header(None)):
    # Same priority routing as chat: body["model"] is the alias/model, picked up
    # by get_routes_for(). Embedding responses carry usage.prompt_tokens only
    # (no completion_tokens) → cost falls out of the input-price path for free.
    # Backends that filter out embedding models (chat_only) simply won't be
    # candidates here, so the request routes to a backend that actually serves it.
    return await route("/v1/embeddings", request, authorization)


@app.post("/v1/audio/speech")
async def audio_speech(request: Request, authorization: Optional[str] = Header(None)):
    # OpenAI-shaped TTS / voice cloning: routed like chat by body["model"] (bare id
    # or alias) through the same dispatch/parking/failover machinery. The backend's
    # binary audio (audio/wav etc.) passes through untouched — route() strips any
    # `stream` flag (audio is never SSE) and the adapter skips body parsing/stats
    # blobs for non-text responses. Extra fields (`voice`, `params.ref_text`, …)
    # are forwarded verbatim; a per-alias voice default may fill them in.
    return await route("/v1/audio/speech", request, authorization)


# ── Generation (image / video / TTS) ───────────────────────────────────────────
# Native job-based API. A generation alias resolves via `image_models` to ordered
# (backend, workflow) candidates; the request is rendered into the backend's
# protocol by its adapter. Results are persisted in the job store and retrievable
# by id (TTL) — sync mode also returns them inline. No VRAM coordinator yet
# (Phase 2): candidates are filtered by health + the existing busy cap only.

def _gen_routes(alias: str) -> tuple[list, list]:
    """(ready, all) (backend, candidate) pairs for a generation alias, each ordered
    by **backend priority** and filtered to enabled + healthy backends; `ready`
    additionally drops busy ones. ONE store read serves both lists.

    An alias holds a flat list of *allowed* backends (no primary/fallback). They are
    tried in backend-priority order; on a connection error the job runner moves to the
    next. An optional per-alias `retries` caps how many backends are attempted (1 +
    retries); blank = try all eligible. Each list is capped independently — same
    result as the old per-include_busy filtering.

    Reads from the writable store (UI source of truth) when active, falling back
    to the `image_models` config for aliases the store doesn't hold. Blocking
    (store read) — call via asyncio.to_thread from async code."""
    candidates = store.get(alias) if store.is_active() else None
    if candidates is None:
        candidates = image_models.get(alias, [])
    # generation routes only to ComfyUI backends, so a name shared with an LLM backend
    # resolves to the right one.
    comfy = [b for b in _comfy_backends if not is_draining(b)]
    allc = []
    for cand in candidates:
        b = next((b for b in comfy if b["name"] == cand.get("backend")), None)
        if b is None or not backend_healthy.get(backend_id(b)):
            continue
        allc.append((b, cand))
    allc.sort(key=lambda bc: bc[0].get("priority", _DEFAULT_PRIORITY))   # backend priority decides order
    ready = [r for r in allc if not backend_busy(r[0])]     # busy → only parkable, not ready
    raw = next((c.get("retries") for c in candidates if c.get("retries") not in (None, "")), None)
    if raw is not None:
        try:
            cap = max(1, int(raw) + 1)                      # first attempt + N retries
            allc, ready = allc[:cap], ready[:cap]
        except (ValueError, TypeError):
            pass
    return ready, allc


def get_gen_routes(alias: str, include_busy: bool = False) -> list[tuple[dict, dict]]:
    """Single-list view of _gen_routes() — kept for callers that need only one side
    (image slots, LoRA listing, admin)."""
    ready, allc = _gen_routes(alias)
    return allc if include_busy else ready


def _force_filter(routes: list, force: str) -> list:
    """Keep only the force-pinned backend's candidates (no-op without a pin)."""
    return [r for r in routes if r[0].get("name") == force] if force else routes


def _gen_inputs_params(body: dict) -> tuple[dict, dict]:
    inputs = {
        "prompt": body.get("prompt", ""),
        "negative_prompt": body.get("negative_prompt", ""),
    }
    params = dict(body.get("params") or {})
    for k in ("width", "height", "steps", "cfg", "seed", "sampler", "scheduler", "seconds"):
        if k in body and k not in params:        # top-level convenience knobs
            params[k] = body[k]
    return inputs, params


def _mapping_param(mapping: dict, name: str) -> Optional[str]:
    """The mapping param whose EXTERNAL name (label, else param) is `name`."""
    for p, m in (mapping or {}).items():
        if p == name or ((m or {}).get("label") or "").strip().lower() == name:
            return p
    return None


def _apply_seconds(params: dict, cand: dict) -> None:
    """Video convenience: `seconds` → the alias's `frames` param, when the alias
    declares `fps` (mapping editor). Explicit frames always win; `frames_snap: S`
    rounds onto the S·k+1 raster (Wan). A mapping that itself exposes a param
    named `seconds` is left alone; `seconds` without alias fps is a clear 400
    instead of a silent ignore."""
    if _mapping_param(cand.get("mapping") or {}, "seconds"):
        return                                   # the workflow maps it directly
    sec = params.pop("seconds", None)
    if sec in (None, ""):
        return
    fps = cand.get("fps")
    mapping = cand.get("mapping") or {}
    fparam = _mapping_param(mapping, "frames")
    if not fps or fparam is None:
        raise HTTPException(400, f"'seconds' is not supported for this alias "
                                 f"({'no fps configured' if not fps else 'no frames param'}) — send frames directly")
    lbl = ((mapping.get(fparam) or {}).get("label") or "").strip()
    if params.get(fparam) not in (None, "") or (lbl and params.get(lbl) not in (None, "")):
        return                                   # explicit frames beats the convenience knob
    try:
        frames = max(1, round(float(sec) * float(fps)))
    except (TypeError, ValueError):
        raise HTTPException(400, f"'seconds' must be a number (got {sec!r})")
    snap = cand.get("frames_snap")
    if snap:
        s = max(1, int(snap))
        frames = max(1, round((frames - 1) / s) * s + 1)
    params[fparam] = frames


async def _job_view(job_id: str, request: Request) -> dict:
    job = await asyncio.to_thread(jobs.get, job_id)
    if job is None:
        raise HTTPException(404, f"job '{job_id}' not found")
    view = {
        "job_id": job_id, "status": job["status"], "task": job["task"],
        "alias": job["alias"], "backend": job["backend"], "error": job["error"],
        "meta": job["meta"],
    }
    if job["task"] == "chat":                           # parked chat → inline completion JSON
        view["completion"] = job["results"][0] if job["results"] else None
        return view
    base = str(request.base_url).rstrip("/")
    view["results"] = [{
        "n": r["n"], "mime": r["mime"], "kind": r["kind"],
        "url": f"{base}/v1/jobs/{job_id}/result/{r['n']}",
    } for r in job["results"]]
    meta = job.get("meta") or {}
    view["inputs"] = meta.get("inputs")
    view["input_images"] = [{
        "n": r["n"], "slot": r.get("slot"), "mime": r["mime"],
        "url": f"{base}/v1/jobs/{job_id}/input/{r['n']}",
    } for r in meta.get("input_images", [])]
    return view


# Connection-type errors that warrant failing over to the next candidate backend.
# A crashed/unreachable ComfyUI raises these; a content error (ComfyUI validation/
# execution → RuntimeError) does not — it would fail identically elsewhere.
_GEN_FAILOVER_ERRORS = (httpx.ConnectError, httpx.TimeoutException, httpx.ReadError,
                        ConnectionError, TimeoutError)


def _host_flag(host: str, key: str, default: bool) -> bool:
    v = (hosts_meta.get(host) or {}).get(key)
    return default if v is None else bool(v)


def _shared_host(host: str) -> bool:
    """A host running BOTH an LLM and a ComfyUI backend — they share its GPU."""
    kinds = {bid.split(":", 1)[0] for bid in host_backends.get(host, ())}
    return "comfyui" in kinds and len(kinds) > 1


async def _free_comfy_vram(backend: dict, why: str) -> None:
    """POST /free to a ComfyUI backend after its media job ended (fire-and-forget
    via create_task). ComfyUI caches models in VRAM indefinitely; on a shared box
    the next llama-swap load then aborts on that memory (phase 3, host plan).
    Skipped while ANOTHER generation runs there — the free would drop its cache.
    Policy: host flag `comfy_free_after_job`; absent = ON for shared hosts only
    (a dedicated comfy box keeps its cache for speed)."""
    bid = backend_id(backend)
    host = backend_hosts.get(bid, "")
    if not _host_flag(host, "comfy_free_after_job", _shared_host(host)):
        return
    if backend_inflight.get(bid, 0) > 0:
        return
    try:
        await http_client.post(f"{backend['url'].rstrip('/')}/free",
                               json={"unload_models": True, "free_memory": True}, timeout=10.0)
        logger.info(f"host: freed ComfyUI VRAM on [{backend['name']}] after {why}")
    except Exception as e:
        logger.warning(f"host: /free on [{backend['name']}] failed: {e}")


async def _unload_host_llms(backend: dict) -> None:
    """Best-effort GET /unload on the LLM siblings of a ComfyUI backend's host
    right before a media job runs there, so the generation doesn't start against
    VRAM a loaded llama-swap model holds. Host flag `llm_unload_before_media`,
    default OFF — llama-swap's TTL usually clears the model anyway (endpoint
    verified on k12-gpu: llama-swap GET /unload → 200; other servers ignore it)."""
    bid = backend_id(backend)
    host = backend_hosts.get(bid, "")
    if not _host_flag(host, "llm_unload_before_media", False):
        return
    for obid in host_backends.get(host, ()):
        if not obid.startswith("openai:"):
            continue
        ob = next((x for x in backends if backend_id(x) == obid), None)
        if ob and ob.get("url"):
            try:
                await http_client.get(f"{ob['url'].rstrip('/')}/unload", timeout=8.0)
                logger.info(f"host: unloaded LLMs on [{ob['name']}] before media job")
            except Exception:
                pass


async def _run_job(job_id: str, candidates: list, build_req) -> None:
    """Run a generation job, failing over to the next candidate on connection-type
    errors. Content errors are final (not retried). Stops at the first success."""
    await asyncio.to_thread(jobs.set_status, job_id, "running")
    last = None
    for backend, cand in candidates:
        adapter = backend_adapters.get(backend_id(backend))
        if adapter is None:
            continue
        try:
            await _unload_host_llms(backend)         # opt-in host policy, no-op by default
            out = await adapter.generate(build_req(backend, cand))
            await asyncio.to_thread(jobs.complete, job_id, out.blobs, out.meta)
            if log_per_call:
                logger.info(f"✓ job {job_id} done on [{backend['name']}] — {len(out.blobs)} artifact(s)")
            asyncio.create_task(_free_comfy_vram(backend, "job done"))
            return
        except _GEN_FAILOVER_ERRORS as e:
            logger.warning(f"✗ job {job_id} [{backend['name']}] connection issue "
                           f"({type(e).__name__}: {e}) — failing over")
            last = e
            continue
        except Exception as e:
            logger.warning(f"✗ job {job_id} [{backend['name']}] failed: {e}")
            await asyncio.to_thread(jobs.fail, job_id, str(e))
            asyncio.create_task(_free_comfy_vram(backend, "job failure"))
            return
    await asyncio.to_thread(jobs.fail, job_id,
                            f"all candidate backends unreachable (connection): {last}")


_gen_tasks: dict = {}                       # job_id → asyncio.Task (for cancellation)


def _spawn_gen(job_id: str, coro) -> None:
    """Run a generation coroutine as a tracked background task so it can be cancelled."""
    t = asyncio.create_task(coro)
    _gen_tasks[job_id] = t
    t.add_done_callback(lambda _: _gen_tasks.pop(job_id, None))


async def cancel_generation(job_id: str) -> bool:
    """Cancel a queued/running generation job: best-effort interrupt the ComfyUI prompt
    (free the GPU), cancel the worker task, mark the job failed. Returns False if the job
    is already finished/unknown."""
    job = await asyncio.to_thread(jobs.get, job_id)
    if not job or job.get("status") not in ("queued", "running"):
        return False
    b = next((x for x in backends if x.get("name") == job.get("backend")
              and x.get("type") == "comfyui"), None)
    if b and b.get("url"):
        try:
            await http_client.post(f"{b['url'].rstrip('/')}/interrupt", timeout=5.0)
        except Exception:
            pass
    t = _gen_tasks.get(job_id)
    if t and not t.done():
        t.cancel()
    await asyncio.to_thread(jobs.fail, job_id, "cancelled by user")
    if b:
        asyncio.create_task(_free_comfy_vram(b, "cancel"))
    return True


async def _run_gen_parked(job_id, alias, force, build_req, eligible: Optional[set] = None):
    """Hold a generation job until a backend frees (polls backend-busy), then run it —
    so a busy backend queues instead of 503'ing (async/playground). `eligible` keeps
    the LoRA constraint through the park: the job waits for a LoRA-capable backend
    instead of spilling to whichever frees first."""
    deadline = time.monotonic() + async_park_timeout_s
    while True:
        ready, allc = await asyncio.to_thread(_gen_routes, alias)   # fresh health/busy per poll
        ready, allc = _force_filter(ready, force), _force_filter(allc, force)
        if eligible is not None:
            ready = [r for r in ready if r[0].get("name") in eligible]
            allc = [r for r in allc if r[0].get("name") in eligible]
        if ready:
            await _run_job(job_id, ready, build_req)
            return
        if not allc:
            await asyncio.to_thread(jobs.fail, job_id,
                                    f"no healthy backend for '{alias}'" + (f" on '{force}'" if force else ""))
            return
        if time.monotonic() > deadline:
            await asyncio.to_thread(jobs.fail, job_id,
                                    f"park timeout: backend busy for >{async_park_timeout_s:.0f}s")
            return
        await asyncio.sleep(2.0)


def _requested_loras(body: dict) -> set:
    """LoRA filenames a request asks for (lora_* params, top-level or in `params`),
    excluding None/blank — used for LoRA-aware backend preference."""
    out = set()
    merged = {**body, **(body.get("params") or {})}
    for k, v in merged.items():
        if isinstance(v, str) and v and v != "None" and re.match(r"^lora_0*\d+$", str(k)):
            out.add(v)
    return out


def _lora_eligible_names(all_cands: list, body: dict) -> Optional[set]:
    """LoRA-aware backend eligibility: backends lacking a requested LoRA are dropped —
    but only for LoRAs installed on SOME candidate; a LoRA installed nowhere is ignored
    so priority still decides (per spec). Decided over ALL candidates (incl. busy), so
    the eligible backend is parked-for rather than spilling to a backend without the
    LoRA. None = no constraint."""
    req_loras = _requested_loras(body)
    if not req_loras or not all_cands:
        return None
    avail = set().union(*(backend_loras.get(backend_id(b), set()) for b, _ in all_cands))
    need = req_loras & avail
    if not need:
        return None
    elig = [r for r in all_cands if need <= backend_loras.get(backend_id(r[0]), set())]
    if not elig:                                          # loras split across backends → no constraint
        return None
    return {r[0].get("name") for r in elig}


async def _gen_pick(alias: str, force: str, body: dict) -> tuple[list, bool, Optional[set]]:
    """Resolve a generation request's candidates ONCE (a single store read):
    force-pin filter, LoRA eligibility, ready/busy split. Returns
    (routes, parked, eligible_names); raises 503 when nothing is eligible."""
    ready, allc = await asyncio.to_thread(_gen_routes, alias)
    ready, allc = _force_filter(ready, force), _force_filter(allc, force)
    if not allc:
        raise HTTPException(503, f"No healthy backend for generation model '{alias}'"
                                 + (f" on backend '{force}'" if force else ""))
    eligible = None if force else _lora_eligible_names(allc, body)   # a pin is never overridden
    if eligible is not None:
        ready = [r for r in ready if r[0].get("name") in eligible]
        allc = [r for r in allc if r[0].get("name") in eligible]
    if ready:                                            # something is free → dispatch now
        return ready, False, eligible
    return allc, True, eligible                          # all busy → park (async: queue, sync: block)


async def run_generation(body: dict, request: Request,
                         upload_images: Optional[dict] = None) -> dict:
    """Resolve a generation alias, create a job, and run it (sync) or schedule it
    (async). Returns a job view (sync) or `{job_id, status:"queued"}` (async).
    Fails over across candidate backends on connection errors (e.g. a crashed
    ComfyUI). Shared by the HTTP endpoint and the UI playground."""
    alias = body.get("model", "")
    output = dict(body.get("output") or {})
    mode = output.get("mode") or body.get("mode") or "sync"
    ttl_s = output.get("ttl_s") or body.get("ttl_s")
    force = (body.get("backend") or "").strip()          # pin to one backend (playground testing)

    routes, parked, eligible = await _gen_pick(alias, force, body)
    inputs, params = _gen_inputs_params(body)
    _apply_seconds(params, routes[0][1])         # seconds → frames (alias fps; 400 if unsupported)

    def build_req(backend: dict, cand: dict) -> NormalizedRequest:
        return NormalizedRequest(
            alias=alias, real_model=cand.get("model"),
            task=cand.get("task", body.get("task", "text2img")),
            inputs=inputs, params=params, output=output,
            workflow=cand.get("workflow"), workflow_json=cand.get("workflow_json"),
            node_mapping=cand.get("mapping") or {}, fixed=cand.get("fixed") or [],
            upload_images=dict(upload_images or {}), raw=request,
        )

    first, cand0 = routes[0]
    task = cand0.get("task", body.get("task", "text2img"))
    owner = getattr(request.state, "gw_user", None) or "default"
    job_id = await asyncio.to_thread(jobs.create, task, alias, first["name"], owner=owner, ttl_s=ttl_s)
    # persist the request inputs so the job stays inspectable in the UI within its TTL
    ref_blobs = [(slot, data) for slot, data in (upload_images or {}).items() if data]
    await asyncio.to_thread(jobs.set_inputs, job_id,
                            {"prompt": inputs.get("prompt", ""),
                             "negative_prompt": inputs.get("negative_prompt", ""),
                             "params": params}, ref_blobs)
    if log_per_call:
        cands = ", ".join(b["name"] for b, _ in routes)
        logger.info(f"→ generation '{alias}' ({task}) job {job_id} mode={mode}"
                    f"{' PARKED' if parked else ''} candidates=[{cands}]")

    if parked:
        if mode == "async":                              # queue and hand back a job id
            _spawn_gen(job_id, _run_gen_parked(job_id, alias, force, build_req, eligible))
            return {"job_id": job_id, "status": "queued"}
        await _run_gen_parked(job_id, alias, force, build_req, eligible)   # sync: block through the park
        return await _job_view(job_id, request)
    if mode == "async":
        _spawn_gen(job_id, _run_job(job_id, routes, build_req))
        return {"job_id": job_id, "status": "queued"}

    await _run_job(job_id, routes, build_req)      # sync: block until done/failed
    return await _job_view(job_id, request)


@app.post("/v1/generations")
async def generations(request: Request, authorization: Optional[str] = Header(None)):
    body = await request.json()
    await gate_request(authorization, request, body.get("model"))    # auth + allow-list + quota
    # Optional per-field reference images: {"images": {<image-param>: <base64|data-URI|URL>}}
    # — the native counterpart of the playground's per-field uploads and the OpenAI
    # shims' positional ref_images.
    uploads = None
    imgs = body.pop("images", None)
    if isinstance(imgs, dict):
        uploads = {}
        for param, val in imgs.items():
            data = await _decode_ref_image(val)
            if data:
                uploads[param] = data
    view = await run_generation(body, request, upload_images=uploads)
    code = {"queued": 202, "done": 200}.get(view.get("status"), 502)
    return JSONResponse(view, status_code=code)


@app.get("/v1/generations/{alias}/loras")
async def gen_alias_loras(alias: str, request: Request, authorization: Optional[str] = Header(None)):
    """LoRA filenames valid for a generation alias — the union of what's installed on
    the alias's backends. Lets a client present a valid LoRA picker per alias."""
    await gate_request(authorization, request, alias)                # auth + allow-list
    known = (await asyncio.to_thread(store.get, alias)) if store.is_active() else None
    if not (known or image_models.get(alias)):
        raise HTTPException(404, f"generation alias '{alias}' not found")
    loras: set = set()
    for b, _ in await asyncio.to_thread(get_gen_routes, alias, include_busy=True):
        loras |= backend_loras.get(backend_id(b), set())
    return {"object": "list", "alias": alias, "loras": sorted(loras)}


@app.get("/v1/generations/{alias}/schema")
async def gen_alias_schema(alias: str, request: Request, authorization: Optional[str] = Header(None)):
    """Self-description of a generation alias — enough for a client (or an agent)
    to build a valid request without out-of-band docs: params under their EXTERNAL
    names (label, else param; both are accepted on requests) with type + default
    from the workflow, image slots with their empty behaviour, fps/frames raster,
    and where to list valid LoRAs."""
    await gate_request(authorization, request, alias)                # auth + allow-list
    cands = ((await asyncio.to_thread(store.get, alias)) if store.is_active() else None) \
        or image_models.get(alias)
    if not cands:
        raise HTTPException(404, f"generation alias '{alias}' not found")
    cand = cands[0]
    wf = cand.get("workflow_json") or {}
    mapping = cand.get("mapping") or {}
    params, images = [], []
    for p, m in mapping.items():
        m = m or {}
        name = (m.get("label") or "").strip() or p
        node, fld = m.get("node"), m.get("field")
        if is_image_field(wf, node):
            mode = slot_empty_mode(m)
            images.append({"name": name, "on_empty": mode, "required": mode == "required"})
            continue
        cur = (wf.get(node, {}).get("inputs") or {}).get(fld)
        if isinstance(cur, list):                # linked to another node — no scalar default
            cur = None
        typ = ("bool" if isinstance(cur, bool) else
               "int" if isinstance(cur, int) else
               "float" if isinstance(cur, float) else "string")
        entry: dict = {"name": name, "type": typ}
        if name != p:
            entry["param"] = p                   # legacy/internal name, also accepted
        if cur is not None:
            entry["default"] = cur
        if name == "seed" or (p == "seed" and name == p):
            entry["auto"] = "random unless sent"
        params.append(entry)
    out: dict = {"object": "generation.schema", "alias": alias,
                 "backends": [c.get("backend") for c in cands],
                 "params": params, "images": images,
                 "modes": ["sync", "async"],
                 "loras_url": f"/v1/generations/{alias}/loras"}
    if cand.get("fps"):
        out["fps"] = cand["fps"]
        if cand.get("frames_snap"):
            out["frames_snap"] = cand["frames_snap"]     # frames land on snap·k+1
        if _mapping_param(mapping, "frames"):
            out["seconds_supported"] = True              # params.seconds → frames via fps
    return out


async def _require_job_owner(authorization: Optional[str], request: Request, job_id: str) -> None:
    """A non-admin user may only touch their own jobs; admin/master/anonymous see all."""
    user = authenticate(authorization)
    if user and not user.get("_master") and user.get("role") != "admin":
        job = await asyncio.to_thread(jobs.get, job_id)
        _check_owner(job, user, status=403, detail="not your job")


@app.get("/v1/jobs/{job_id}")
async def get_job(job_id: str, request: Request, authorization: Optional[str] = Header(None)):
    await _require_job_owner(authorization, request, job_id)
    return await _job_view(job_id, request)


@app.get("/v1/jobs/{job_id}/result/{n}")
async def get_job_result(job_id: str, n: int, request: Request, authorization: Optional[str] = Header(None)):
    await _require_job_owner(authorization, request, job_id)
    rp = await asyncio.to_thread(jobs.result_path, job_id, n)
    if rp is None:
        raise HTTPException(404, f"result {n} of job '{job_id}' not found")
    path, mime = rp
    return FileResponse(path, media_type=mime)


@app.get("/v1/jobs/{job_id}/input/{n}")
async def get_job_input(job_id: str, n: int, request: Request, authorization: Optional[str] = Header(None)):
    """Reference image `n` that was submitted with a generation job (kept within TTL)."""
    await _require_job_owner(authorization, request, job_id)
    ip = await asyncio.to_thread(jobs.input_path, job_id, n)
    if ip is None:
        raise HTTPException(404, f"input {n} of job '{job_id}' not found")
    path, mime = ip
    return FileResponse(path, media_type=mime)


@app.post("/v1/jobs/{job_id}/cancel")
async def cancel_job(job_id: str, request: Request, authorization: Optional[str] = Header(None)):
    """Cancel a queued/running generation job (interrupts the backend, frees the GPU)."""
    await _require_job_owner(authorization, request, job_id)
    if not await cancel_generation(job_id):
        raise HTTPException(409, f"job '{job_id}' is not cancellable (already done/failed/unknown)")
    return {"job_id": job_id, "status": "failed", "cancelled": True}


# ── OpenAI-compatible image endpoints (C4) ───────────────────────────────────
# Thin shims over the native job path so OpenAI image clients (anima-verse's image
# provider, SDKs) reach the gateway's ComfyUI generation aliases.
#  /v1/images/generations : JSON, text->image (+ bonus LocalAI-style ref_images)
#  /v1/images/edits       : multipart, reference image(s) -> alias image-input slots

# Request/response plumbing (multipart parsing, size/scalar coercion, the OpenAI
# images response shape) lives in openai_image_bridge.py; main keeps only what
# needs gateway state: the slot lookup below and the endpoints.

def _gen_image_slots(alias: str) -> list:
    """Ordered image-input param names of a generation alias (its workflow's image
    loaders per the mapping) — reference images map onto these positionally. Includes
    busy backends: slots are a workflow property, not gated on backend availability
    (else a busy backend would silently drop the uploaded reference images)."""
    routes = get_gen_routes(alias, include_busy=True)
    if not routes:
        return []
    _, cand = routes[0]
    return image_params(cand.get("workflow_json") or {}, cand.get("mapping") or {})


async def _decode_ref_image(ref) -> Optional[bytes]:
    """Reference image as base64 data-URI / raw base64 / http(s) URL -> bytes."""
    if not isinstance(ref, str) or not ref:
        return None
    if ref.startswith("data:"):
        ref = ref.split(",", 1)[-1]
    if ref.startswith(("http://", "https://")):
        try:
            r = await http_client.get(ref, timeout=20.0)
            return r.content if r.status_code == 200 else None
        except Exception:
            return None
    try:
        return base64.b64decode(ref)
    except Exception:
        return None


@app.post("/v1/images/generations")
async def images_generations(request: Request, authorization: Optional[str] = Header(None)):
    """OpenAI Images API (text->image). Bonus: LocalAI-style `ref_images`
    (base64/URL list) are accepted and mapped onto the alias's image slots."""
    body = await request.json()
    alias = body.get("model", "")
    await gate_request(authorization, request, alias)
    if not body.get("prompt"):
        raise HTTPException(400, "`prompt` is required")
    w, h = parse_size(body.get("size"))
    refs = body.get("ref_images") or []
    decoded = [await _decode_ref_image(r) for r in refs]
    slots = await asyncio.to_thread(_gen_image_slots, alias)   # ONE lookup — uploads + log
    uploads = images_uploads(decoded, slots) if refs else None
    extra = {k: v for k, v in body.items() if k not in OAI_IMG_KEYS}   # dynamic workflow params
    logger.info(f"images/generations '{alias}': ref_images={len(refs)} "   # where client images land
                f"decoded_ok={sum(1 for d in decoded if d)} slots={slots} "
                f"filled={sorted((uploads or {}).keys())} extra_keys={sorted(extra)}")
    native = {
        "model": alias, "mode": "sync",
        "prompt": body.get("prompt", ""), "negative_prompt": body.get("negative_prompt", ""),
        "params": {"width": w, "height": h, **(body.get("params") or {}), **extra},
        "output": {"n": int(body.get("n") or 1), "mode": "sync"},
    }
    view = gen_done_or_502(await run_generation(native, request, upload_images=uploads))
    return JSONResponse(await asyncio.to_thread(          # b64 branch reads result files
        images_response, view, body.get("response_format") or "url"))


@app.post("/v1/images/edits")
async def images_edits(request: Request, authorization: Optional[str] = Header(None)):
    """OpenAI Images Edit API (multipart): `image` field(s) carry reference images,
    mapped positionally onto the alias's declared image-input slots (cap = slot count;
    OpenAI itself allows 1 for dall-e-2, up to 16 for gpt-image-1)."""
    f = await multipart_list(request)
    one = lambda k, d="": (f.get(k) or [d])[0]
    alias = (one("model") or "").strip()
    await gate_request(authorization, request, alias)
    images = [v for v in (f.get("image") or []) if isinstance(v, (bytes, bytearray))]
    if not images:
        raise HTTPException(400, "at least one `image` file is required")
    masks = [v for v in (f.get("mask") or []) if isinstance(v, (bytes, bytearray))]
    slots = await asyncio.to_thread(_gen_image_slots, alias)   # ONE lookup — uploads + log
    logger.info(f"images/edits '{alias}': image_files={len(images)} mask_files={len(masks)} "  # where they land
                f"slots={slots} "
                f"scalar_keys={sorted(k for k, vs in f.items() if not isinstance((vs or [None])[0], (bytes, bytearray)))}")
    # OpenAI `mask` field → the next positional slot (the mask slot is normally last in
    # the mapping order, after the reference image[s]).
    images = (images + masks)[:16]                      # OpenAI gpt-image-1 max; workflow may use fewer
    w, h = parse_size(one("size") or None)
    extra = {k: coerce_scalar(one(k)) for k, vs in f.items()  # dynamic scalar params (loras, seed, …)
             if k not in EDIT_KNOWN and not isinstance((vs or [None])[0], (bytes, bytearray))}
    native = {
        "model": alias, "mode": "sync", "task": "img2img",
        "prompt": one("prompt"), "negative_prompt": one("negative_prompt"),
        "params": {"width": w, "height": h, **extra},
        "output": {"n": int((one("n") or "1") or 1), "mode": "sync"},
    }
    view = gen_done_or_502(await run_generation(native, request, upload_images=images_uploads(images, slots)))
    return JSONResponse(await asyncio.to_thread(          # b64 branch reads result files
        images_response, view, one("response_format") or "url"))


def dashboard_snapshot() -> dict:
    """Live 'what's happening now' for the dashboard: per-backend status + in-flight,
    LLM/image activity totals, image-job counts + recent, and recent calls."""
    hour_ago = int(time.time()) - 3600
    calls_1h = stats.count_by_backend_since(hour_ago) if stats.is_active() else {}
    jobs_1h = jobs.count_by_backend_since(hour_ago) if jobs.is_active() else {}
    bes = []
    for b in backends:
        bid, en = backend_id(b), is_enabled(b)
        # requests handled in the last hour: LLM calls from stats, image jobs from the
        # job store (an LLM and a ComfyUI backend may share a name → pick by type).
        src_1h = jobs_1h if b.get("type") == "comfyui" else calls_1h
        bes.append({
            "name": b["name"], "type": b.get("type", "openai"), "priority": b["priority"],
            "enabled": en, "healthy": en and backend_healthy.get(bid, False),
            "busy": en and backend_busy(b), "inflight": backend_inflight.get(bid, 0),
            "draining": is_draining(b),
            "max_concurrent": backend_max_concurrent(b),
            "models": len(backend_models.get(bid, set())),
            "reqs_1h": src_1h.get(b["name"], 0),
        })
    is_comfy = lambda b: b.get("type") == "comfyui"
    return {
        "backends": bes,
        "llm_inflight": sum(backend_inflight.get(backend_id(b), 0) for b in backends if not is_comfy(b)),
        "img_inflight": sum(backend_inflight.get(backend_id(b), 0) for b in backends if is_comfy(b)),
        "parked": len(_parked),
        "parked_calls": [{
            "id": e["id"], "alias": e["alias"], "source": e["source"],
            "waited_s": max(0.0, time.time() - e["enqueued"]),
            "remaining_s": max(0.0, e["deadline"] - time.monotonic()),
        } for e in list(_parked)],
        "jobs_active": jobs.is_active(),
        "jobs_counts": jobs.counts(media_only=True),     # the dashboard cards/panel are MEDIA
        "jobs_recent": jobs.recent(15, media_only=True),  # jobs — chat/response rows stay out
        "stats_active": stats.is_active(),
        "calls_24h": stats.count_since(int(time.time()) - 86400),
        # Recent LLM calls = currently running (live registry) + ended in the last 5 min (stats).
        "llm_running": sorted(_active_calls.values(), key=lambda c: c.get("started", 0)),
        "llm_recent": stats.recent_since(int(time.time()) - 300) if stats.is_active() else [],
    }


def gateway_info() -> dict:
    """Snapshot the UI's Backends/Input/Server tabs read from."""
    config_ids = {backend_id(b) for b in config_backends}
    return {
        "backends": [{
            "name": b["name"], "type": b.get("type", "openai"), "priority": b["priority"],
            "enabled": is_enabled(b), "healthy": backend_healthy.get(backend_id(b), False),
            "inflight": backend_inflight.get(backend_id(b), 0), "draining": is_draining(b),
            "models": len(backend_models.get(backend_id(b), set())), "url": b["url"],
            "max_concurrent": b.get("max_concurrent"),
            "chat_only": bool(b.get("chat_only")), "serverless_only": bool(b.get("serverless_only")),
            "local": bool(b.get("local")),
            "host": backend_hosts.get(backend_id(b), ""),
            "host_explicit": bool((b.get("host") or "").strip()),
            "source": "config" if backend_id(b) in config_ids else "ui",
        } for b in backends],
        "virtual_models": list(virtual_models.keys()),
        "endpoints": ["/v1/chat/completions", "/v1/completions", "/v1/embeddings",
                      "/v1/responses", "/v1/models", "/v1/generations", "/v1/jobs/{id}"],
    }


def apply_backend_change() -> None:
    """Re-merge config + store backends, rebind adapters, and kick an immediate
    discovery — called by the UI after a backend is added/edited/deleted."""
    rebuild_backends()
    build_backend_adapters()

    async def _discover():
        await asyncio.gather(*[refresh_backend(b, http_client) for b in enabled_backends()])
    try:
        asyncio.get_running_loop().create_task(_discover())
    except RuntimeError:
        pass


def begin_drain(bid: str) -> bool:
    """Take a backend offline gracefully: stop routing new requests to it now, and
    disable it once its in-flight requests finish. False if unknown/already disabled."""
    b = next((x for x in backends if backend_id(x) == bid), None)
    if b is None or not is_enabled(b):
        return False
    _draining.add(bid)
    n = backend_inflight.get(bid, 0)
    logger.info(f"[{b['name']}] draining — {n} in-flight; goes offline when idle")
    if n <= 0:
        _finalize_drain(bid)
    return True


def cancel_drain(bid: str) -> bool:
    """Abort a drain → the backend rejoins rotation. False if it wasn't draining."""
    if bid not in _draining:
        return False
    _draining.discard(bid)
    nm = next((x["name"] for x in backends if backend_id(x) == bid), bid)
    logger.info(f"[{nm}] drain cancelled — back in rotation")
    return True


def set_backend_enabled(bid: str, on: bool) -> bool:
    """Persist a backend's `enabled` flag (store) and rebuild. Backs the drain-finalize
    and the UI take-offline / bring-online actions. False if the backend is unknown."""
    b = next((x for x in backends if backend_id(x) == bid), None)
    if b is None:
        return False
    entry = dict(store.get_backend(b["name"], b.get("type", "openai")) or
                 {k: b[k] for k in ("name", "type", "url", "priority", "max_concurrent",
                                    "api_key", "chat_only", "serverless_only", "local") if k in b})
    entry.update({"name": b["name"], "type": b.get("type", "openai"), "enabled": bool(on)})
    store.upsert_backend(entry)
    logger.info(f"[{b['name']}] {'enabled' if on else 'disabled'} via console")
    apply_backend_change()
    return True


def _finalize_drain(bid: str) -> None:
    """Backend is idle → take it offline (persist enabled=false) and rebuild."""
    _draining.discard(bid)
    set_backend_enabled(bid, False)
    logger.info(f"backends changed → {len(backends)} effective")


def apply_chat_aliases() -> None:
    """Re-merge config + store chat aliases into the live router — called by the UI
    after a chat alias is added/edited/deleted. No discovery/adapter rebind needed;
    routing reads `virtual_models` directly."""
    rebuild_virtual_models()
    logger.info(f"chat aliases changed → {len(virtual_models)} effective")


# Restart-only server state actually in effect (snapshotted at startup), so the UI
# can flag settings whose change needs a restart.
_server_runtime: dict = {}


def apply_server_settings() -> None:
    """Overlay UI-managed server settings (store) onto the live config globals.

    Runtime knobs (api_key, log_per_call, model_prefix, max_concurrent, health
    interval) take effect immediately. stats.* are overlaid too but only bite on the
    next restart (the stats server is built once at startup). The gateway listening
    port is set by the launch command, so it is informational here."""
    global api_key, log_per_call, model_prefix, max_concurrent_default, health_check_interval
    global park_timeout_s, async_park_timeout_s, max_parked
    s = store.get_settings() if store.is_active() else {}
    if "api_key" in s:
        api_key = s["api_key"] or None
    if "log_per_call" in s:
        log_per_call = bool(s["log_per_call"])
    if "model_prefix" in s:
        model_prefix = bool(s["model_prefix"])
    if "max_concurrent" in s:
        max_concurrent_default = s["max_concurrent"] if s["max_concurrent"] not in ("", None) else None
    if "health_check_interval" in s:
        try:
            health_check_interval = int(s["health_check_interval"])
        except (TypeError, ValueError):
            pass
    if "park_timeout_s" in s:
        try:
            park_timeout_s = float(s["park_timeout_s"])
        except (TypeError, ValueError):
            pass
    if "async_park_timeout_s" in s:
        try:
            async_park_timeout_s = float(s["async_park_timeout_s"])
        except (TypeError, ValueError):
            pass
    if "max_parked" in s:
        try:
            max_parked = int(s["max_parked"])
        except (TypeError, ValueError):
            pass
    # restart-only: overlaid onto stats_cfg / jobs_cfg so the next start picks them up
    # (these init once at startup). Lets config.yaml shed the jobs/stats db knobs.
    for skey, ckey in (("stats_enabled", "enabled"),
                       ("stats_db_path", "db_path"), ("stats_retention_days", "retention_days")):
        if skey in s:
            stats_cfg[ckey] = s[skey]
    for skey, ckey in (("jobs_enabled", "enabled"), ("jobs_db_path", "db_path"),
                       ("jobs_blob_dir", "blob_dir"), ("jobs_default_ttl_s", "default_ttl_s"),
                       ("jobs_prune_interval_s", "prune_interval_s")):
        if skey in s:
            jobs_cfg[ckey] = s[skey]
    if s:
        logger.info(f"server settings: applied {len(s)} UI override(s)")


def server_info() -> dict:
    """Effective server settings + the restart-only values actually running, for the
    UI's Server tab."""
    return {
        "effective": {
            "api_key_set": bool(api_key),
            "log_per_call": bool(log_per_call),
            "model_prefix": bool(model_prefix),
            "max_concurrent": max_concurrent_default,
            "health_check_interval": health_check_interval,
            "port": (config or {}).get("port", 4000),
            "stats_enabled": bool(stats_cfg.get("enabled")),
            "stats_db_path": stats_cfg.get("db_path", "stats.db"),
            "stats_retention_days": stats_cfg.get("retention_days", 0),
            "jobs_enabled": bool(jobs_cfg.get("enabled")),
            "jobs_db_path": jobs_cfg.get("db_path", "jobs.db"),
            "jobs_blob_dir": jobs_cfg.get("blob_dir", "jobs"),
            "jobs_default_ttl_s": jobs_cfg.get("default_ttl_s", 86400),
            "jobs_prune_interval_s": jobs_cfg.get("prune_interval_s", 3600),
        },
        "runtime": dict(_server_runtime),     # restart-only state in effect now
    }


def apply_server_settings_hook() -> None:
    """UI save hook: re-apply settings + log."""
    apply_server_settings()


def llm_backends_info() -> list[dict]:
    """LLM (non-ComfyUI) backends + their discovered model ids — feeds the chat-alias
    editor's per-backend model pickers."""
    return [{"name": b["name"], "type": b.get("type", "openai"),
             "priority": b["priority"], "enabled": is_enabled(b),
             "models": sorted(backend_models.get(backend_id(b), set()))}
            for b in backends if b.get("type", "openai") != "comfyui"]


# Wire the UI to the generation core, the ComfyUI backends, the status snapshot,
# and the backend-change hook.
admin.bind(comfy_backends=lambda: [b for b in backends if b.get("type") == "comfyui"],
           gateway_info=gateway_info,
           apply_backends=apply_backend_change,
           llm_backends=llm_backends_info,
           config_chat_aliases=lambda: dict(config_virtual_models),
           apply_chat_aliases=apply_chat_aliases,
           playground_key=_playground_key,
           routing_snapshot=routing_snapshot,
           server_info=server_info,
           apply_server_settings=apply_server_settings_hook,
           apply_users=apply_users,
           resolve_admin=resolve_admin, ui_locked=ui_locked,
           dashboard_snapshot=dashboard_snapshot, cancel_generation=cancel_generation,
           drain_backend=begin_drain, cancel_drain=cancel_drain,
           set_backend_enabled=set_backend_enabled,
           llm_backend_names=lambda: sorted({b["name"] for b in backends
                                             if b.get("type", "openai") != "comfyui"}),
           resolve_for_backend=resolve_for_backend,
           apply_reasoning=apply_reasoning_rules,
           probe_reasoning=probe_reasoning,
           voice_lib_save=save_voice_ref, voice_lib_delete=delete_voice_ref,
           voice_lib_ship=ship_voice_ref, voice_ship_config=voice_ship_config,
           apply_voice_library=apply_voice_library,
           apply_hosts=apply_hosts,
           backend_loras=lambda: {b["name"]: sorted(backend_loras.get(backend_id(b), set()))
                                  for b in backends if b.get("type") == "comfyui"})


@app.get("/health")
async def health():
    return {
        "status": "ok",
        "parked": len(_parked),
        "backends": {
            backend_id(b): {
                "name": b["name"], "type": b.get("type", "openai"),
                "enabled": is_enabled(b),
                "healthy": is_enabled(b) and backend_healthy.get(backend_id(b), False),
                "busy": is_enabled(b) and backend_busy(b),
                "inflight": backend_inflight.get(backend_id(b), 0),
                "max_concurrent": backend_max_concurrent(b),
                "priority": b["priority"],
                "models": sorted(backend_models.get(backend_id(b), set())) if is_enabled(b) else [],
            }
            for b in backends
        },
        "virtual_models": virtual_models,
        # Physical-box grouping (explicit `host` field or URL IP) — which backends
        # share a machine/GPU. Basis for the host-level policies (see
        # docs/host-coordination-plan.md).
        "hosts": host_backends,
        # Aliases that shadow a real model on a backend they don't map (→ that
        # model is unreachable by its bare name). Empty list = no such conflict.
        "alias_model_conflicts": [c for c in alias_model_conflicts() if c["shadowed"]],
    }
