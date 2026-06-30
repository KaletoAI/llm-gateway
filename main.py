import asyncio
import base64
import calendar
import json
import logging
import re
import time
import uuid
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, Optional

import httpx
import uvicorn
import yaml
from fastapi import FastAPI, Header, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse
from watchfiles import awatch

import admin
import jobs
import stats
import store
from adapters import AdapterContext, NormalizedRequest, image_params, make_adapter

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


def rebuild_backends() -> None:
    """Effective backend list = config backends, with UI-added (store) backends
    merged in by name (store overrides config for the same name). Re-sorted by
    priority. Call after config reload or any store backend change."""
    global backends
    merged = {backend_id(b): b for b in config_backends}
    if store.is_active():
        for b in store.list_backends():
            merged[backend_id(b)] = b      # store overrides config per (name, type)
    backends = sorted(merged.values(), key=lambda b: b.get("priority", 100))


def rebuild_virtual_models() -> None:
    """Effective chat aliases = config `virtual_models`, with UI-managed (store)
    entries merged over them by alias (store overrides config for the same name).
    Call after config reload or any store chat-alias change."""
    global virtual_models
    merged = dict(config_virtual_models)
    if store.is_active():
        merged.update(store.list_chat_aliases())
    virtual_models = merged

# ── State ─────────────────────────────────────────────────────────────────────

backend_models: dict[str, set[str]] = {}                       # name → {model_id, ...}
backend_healthy: dict[str, bool] = {}                          # name → bool
backend_pricing: dict[str, dict[str, dict[str, float]]] = {}   # name → {model_id → {input, output}}
backend_loras: dict[str, set[str]] = {}                        # id → {lora filename, ...} (ComfyUI)
backend_inflight: dict[str, int] = {}                          # name → current in-flight requests
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


# ── Call parking (queue instead of immediate busy/503) ────────────────────────
# When every backend mapping an alias is at its in-flight cap, hold the request
# until a slot frees (sync) instead of returning 503. One global FIFO of waiter
# futures; _inflight_dec wakes them. The event loop is single-threaded, so each
# woken waiter atomically re-checks resolve_routes() and claims a slot (dispatch
# increments in-flight before its first await) before the next runs — so wake-all
# cannot burst past the cap.
park_timeout_s: float = 30.0
async_park_timeout_s: float = 600.0
max_parked: int = 100
_park_waiters: list = []


def _notify_slot_free() -> None:
    if not _park_waiters:
        return
    waiters, _park_waiters[:] = list(_park_waiters), []
    for fut in waiters:
        if not fut.done():
            fut.set_result(None)


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
        if store.is_active() and caps.models != backend_models.get(bid):
            store.save_backend_models(bid, caps.models)   # persist on change (survives restart)
        backend_models[bid] = caps.models
        backend_pricing[bid] = caps.pricing
        backend_loras[bid] = getattr(caps, "loras", set()) or set()
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
    async with httpx.AsyncClient() as client:
        while True:
            for backend in enabled_backends():
                await refresh_backend(backend, client)
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
    async with httpx.AsyncClient() as client:
        await asyncio.gather(*[refresh_backend(b, client) for b in enabled_backends()])
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


def gate_request(authorization: Optional[str], request: Request, model: Optional[str]) -> Optional[dict]:
    """Authenticate + enforce model allow-list and per-day quota; attribute the call
    to the user (stats source / job owner read it off request.state). Returns the user
    (None = anonymous bootstrap mode)."""
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
        spent = stats.month_cost(user["name"], month_start)
        if spent >= float(cap):
            raise HTTPException(402, f"monthly cost quota (${float(cap):.4f}) exceeded for "
                                     f"'{user['name']}' — spent ${spent:.4f}")
    limit = user.get("quota_req_day")
    day = time.strftime("%Y-%m-%d", time.gmtime())
    if limit and _usage.get((user["name"], day), 0) >= int(limit):
        raise HTTPException(429, f"daily request quota ({limit}) exceeded for '{user['name']}'")
    _usage[(user["name"], day)] = _usage.get((user["name"], day), 0) + 1
    return user


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
        if any(b["name"] == prefix for b in backends):
            return prefix, rest
    return None, model


def resolve_routes(alias: str) -> tuple[list, list]:
    """(ready, busy) (backend, real_model) candidate lists, each in priority order.

    `ready` is routable now; `busy` maps + serves the alias but sits at its
    in-flight cap (→ parkable). A '<backend>/<model>' alias resolves to a single
    backend in whichever bucket. Drives both normal routing (ready) and call
    parking (busy).
    """
    # chat routing only considers LLM (non-ComfyUI) backends, so a name shared with a
    # ComfyUI backend is unambiguous here.
    llm = [b for b in enabled_backends()
           if b.get("type", "openai") != "comfyui" and not is_draining(b)]
    bname, bare = split_backend_prefix(alias)
    if bname is not None:
        b = next((b for b in llm if b["name"] == bname), None)
        if b is None or not backend_healthy.get(backend_id(b)):
            return [], []
        real = resolve_for_backend(bare, bname)
        if real is None or real not in backend_models.get(backend_id(b), set()):
            return [], []
        return ([], [(b, real)]) if backend_busy(b) else ([(b, real)], [])

    # Bare alias → priority routing. The alias may override a backend's priority
    # for this alias only; backends without an override keep their global prio.
    ready, busy = [], []
    for b in llm:
        if not backend_healthy.get(backend_id(b)):
            continue
        real, prio = alias_entry(alias, b["name"])
        if real is None or real not in backend_models.get(backend_id(b), set()):
            continue
        eff_prio = prio if prio is not None else b["priority"]
        (busy if backend_busy(b) else ready).append((eff_prio, b, real))
    ready.sort(key=lambda r: r[0])
    busy.sort(key=lambda r: r[0])
    return ([(b, r) for _, b, r in ready], [(b, r) for _, b, r in busy])


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


def _cost_usd(backend_name: str, model_id: Optional[str], in_tok: int, out_tok: int) -> float:
    """USD cost for a call from cached pricing (Together-style /v1/models). 0 if unknown."""
    if not model_id:
        return 0.0
    p = backend_pricing.get(backend_name, {}).get(model_id, {})
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
)


def build_backend_adapters() -> None:
    """(Re)instantiate one adapter per configured backend. Called at import and
    after every config reload so adapters point at the current backend dicts."""
    backend_adapters.clear()
    for b in backends:
        backend_adapters[backend_id(b)] = make_adapter(b, adapter_ctx)


build_backend_adapters()


def _park_mode(body: dict, request: Request) -> Optional[str]:
    """Parking mode chosen by the client: body `park` ("sync"|"async"|true) or the
    `X-Park-Mode` header. None → today's behaviour (no parking → 503 when busy)."""
    v = body.get("park")
    if v is True:
        return "sync"
    if isinstance(v, str) and v.strip().lower() in ("sync", "async"):
        return v.strip().lower()
    h = (request.headers.get("x-park-mode") or "").strip().lower()
    return h if h in ("sync", "async") else None


async def _dispatch_over(candidates, path, alias, body, request, stats_endpoint=None):
    """Forward to the first candidate, failing over to the next only on
    connect/timeout. The first candidate's dispatch increments in-flight before
    its first await, so a parked waiter claims that slot atomically here.
    `stats_endpoint` overrides the recorded endpoint label (e.g. /v1/responses)."""
    last_error: Exception = Exception("unknown")
    for backend, real_model in candidates:
        body["model"] = real_model
        adapter = backend_adapters.get(backend_id(backend))
        if adapter is None:                       # config raced a reload — skip
            continue
        try:
            if log_per_call:
                logger.info(f"→ [{backend['name']}] {alias} → {real_model}")
            req = NormalizedRequest(
                path=path, alias=alias, real_model=real_model,
                body=body, raw=request, stream=bool(body.get("stream")),
                stats_endpoint=stats_endpoint,
            )
            return await adapter.dispatch(req)
        except (httpx.ConnectError, httpx.TimeoutException) as e:
            logger.warning(f"✗ [{backend['name']}] {e} — trying next")
            last_error = e
    raise HTTPException(503, f"All backends failed: {last_error}")


async def _park_and_dispatch(alias, path, body, request, deadline):
    """Sync parking: hold the request until a backend slot frees, then dispatch.
    Raises 504 on timeout, 503 if nothing maps the alias anymore."""
    while True:
        ready, busy = resolve_routes(alias)
        if ready:
            return await _dispatch_over(ready, path, alias, body, request)
        if not busy:
            raise HTTPException(503, f"No healthy backend for model '{alias}'")
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise HTTPException(504, f"park timeout: backends for '{alias}' busy for {park_timeout_s:.0f}s")
        fut = asyncio.get_event_loop().create_future()
        _park_waiters.append(fut)
        ready, _ = resolve_routes(alias)          # re-check after registering → no lost wakeup
        if ready:
            if fut in _park_waiters:
                _park_waiters.remove(fut)
            return await _dispatch_over(ready, path, alias, body, request)
        try:
            await asyncio.wait_for(fut, remaining)
        except asyncio.TimeoutError:
            raise HTTPException(504, f"park timeout: backends for '{alias}' busy for {park_timeout_s:.0f}s")
        finally:
            if fut in _park_waiters:
                _park_waiters.remove(fut)


async def _run_parked_job(job_id, alias, path, body, request):
    """Background worker for async-parked chat calls: park until a slot frees,
    dispatch, then store the completion JSON on the job (else mark it failed).
    Non-streaming — the result is fetched via GET /v1/jobs/{id}, not streamed."""
    try:
        resp = await _park_and_dispatch(alias, path, body, request,
                                        time.monotonic() + async_park_timeout_s)
        data = json.loads(bytes(resp.body)) if getattr(resp, "body", None) else {}
        jobs.complete_json(job_id, data, meta={"model": data.get("model")})
    except HTTPException as e:
        jobs.fail(job_id, f"{e.status_code}: {e.detail}")
    except Exception as e:                              # never let a background task vanish silently
        logger.warning(f"parked job {job_id} failed: {e}")
        jobs.fail(job_id, str(e))


async def route(path: str, request: Request, authorization: Optional[str]) -> JSONResponse | StreamingResponse:
    body = await request.json()
    alias = body.get("model", "")
    gate_request(authorization, request, alias)        # auth + model allow-list + quota
    park = _park_mode(body, request)
    body.pop("park", None)                              # control field — never forward to backends

    ready, busy = resolve_routes(alias)
    if ready:
        return await _dispatch_over(ready, path, alias, body, request)
    if busy and park == "sync":
        if len(_park_waiters) >= max_parked:
            raise HTTPException(503, f"park queue full ({max_parked}) — retry later")
        return await _park_and_dispatch(alias, path, body, request, time.monotonic() + park_timeout_s)
    if busy and park == "async":
        if not jobs.is_active():
            raise HTTPException(503, "async parking unavailable (job store off)")
        if len(_park_waiters) >= max_parked:
            raise HTTPException(503, f"park queue full ({max_parked}) — retry later")
        owner = getattr(request.state, "gw_user", None) or "default"
        job_id = jobs.create("chat", alias, "(parked)", owner=owner)
        body["stream"] = False                         # async result is fetched, not streamed
        asyncio.create_task(_run_parked_job(job_id, alias, path, dict(body), request))
        return JSONResponse({"job_id": job_id, "status": "queued", "task": "chat"}, status_code=202)
    if busy:
        raise HTTPException(503, f"all backends for '{alias}' are busy — retry or set \"park\":\"sync\"")
    raise HTTPException(503, f"No healthy backend for model '{alias}'")


# ── Responses API ↔ Chat Completions bridge ──────────────────────────────────
# Translation layer that lets clients hitting OpenAI's newer /v1/responses
# endpoint reach backends that only speak /v1/chat/completions (Together,
# llama-swap, vLLM, etc.). Non-streaming only — stream=true is silently
# downgraded to a non-streaming call; the response is returned in one shot.

def _content_parts_to_text(content: Any) -> Any:
    """Flatten a Responses-style content array to a chat-completions content value."""
    if not isinstance(content, list):
        return content
    text_pieces: list[str] = []
    image_parts: list[dict] = []
    for part in content:
        ptype = part.get("type")
        if ptype in ("input_text", "output_text", "text"):
            text_pieces.append(part.get("text", ""))
        elif ptype == "input_image":
            url = part.get("image_url") or part.get("url")
            if url:
                image_parts.append({"type": "image_url", "image_url": {"url": url}})
    if not image_parts:
        return "".join(text_pieces)
    parts: list[dict] = []
    if text_pieces:
        parts.append({"type": "text", "text": "".join(text_pieces)})
    parts.extend(image_parts)
    return parts


def responses_to_chat(body: dict) -> dict:
    """Translate an OpenAI Responses API request body to Chat Completions."""
    passthrough = {
        "model", "temperature", "top_p", "stop", "seed", "user", "metadata",
        "presence_penalty", "frequency_penalty", "logit_bias",
        "parallel_tool_calls", "response_format",
    }
    chat: dict = {k: v for k, v in body.items() if k in passthrough}

    if "max_output_tokens" in body:
        chat["max_tokens"] = body["max_output_tokens"]
    # stream: silently downgrade — translating SSE event streams isn't supported yet
    chat["stream"] = False

    # Tools: Responses uses flat {type, name, description, parameters};
    #        Chat uses nested {type, function: {name, description, parameters}}.
    if tools := body.get("tools"):
        chat_tools = []
        for t in tools:
            if t.get("type") != "function":
                continue  # skip built-in tools (web_search, code_interpreter, …)
            fn = {k: t[k] for k in ("name", "description", "parameters", "strict") if k in t}
            chat_tools.append({"type": "function", "function": fn})
        if chat_tools:
            chat["tools"] = chat_tools
    if "tool_choice" in body:
        chat["tool_choice"] = body["tool_choice"]

    messages: list[dict] = []
    if instructions := body.get("instructions"):
        messages.append({"role": "system", "content": instructions})

    inp = body.get("input")
    if isinstance(inp, str):
        messages.append({"role": "user", "content": inp})
    elif isinstance(inp, list):
        for item in inp:
            itype = item.get("type", "message")
            if itype == "message":
                role = item.get("role", "user")
                if role == "developer":
                    role = "system"
                content = _content_parts_to_text(item.get("content", ""))
                messages.append({"role": role, "content": content})
            elif itype == "function_call":
                messages.append({
                    "role": "assistant",
                    "content": None,
                    "tool_calls": [{
                        "id": item.get("call_id") or item.get("id"),
                        "type": "function",
                        "function": {
                            "name": item.get("name", ""),
                            "arguments": item.get("arguments", "{}"),
                        },
                    }],
                })
            elif itype == "function_call_output":
                messages.append({
                    "role": "tool",
                    "tool_call_id": item.get("call_id"),
                    "content": item.get("output", ""),
                })
    chat["messages"] = messages
    return chat


def chat_to_responses(chat_resp: dict) -> dict:
    """Translate a Chat Completions response body to a Responses API body."""
    choice = (chat_resp.get("choices") or [{}])[0]
    message = choice.get("message") or {}
    finish = choice.get("finish_reason")

    output: list[dict] = []

    text_content = message.get("content")
    if text_content:
        output.append({
            "type": "message",
            "id": f"msg_{uuid.uuid4().hex[:24]}",
            "role": message.get("role", "assistant"),
            "status": "completed",
            "content": [{"type": "output_text", "text": text_content, "annotations": []}],
        })

    for tc in message.get("tool_calls") or []:
        fn = tc.get("function") or {}
        output.append({
            "type": "function_call",
            "id": f"fc_{uuid.uuid4().hex[:24]}",
            "call_id": tc.get("id"),
            "name": fn.get("name", ""),
            "arguments": fn.get("arguments", ""),
            "status": "completed",
        })

    usage_in = chat_resp.get("usage") or {}
    usage = {
        "input_tokens": usage_in.get("prompt_tokens", 0),
        "output_tokens": usage_in.get("completion_tokens", 0),
        "total_tokens": usage_in.get("total_tokens", 0),
    }

    output_text = "".join(
        p.get("text", "") for o in output if o.get("type") == "message"
        for p in (o.get("content") or []) if p.get("type") == "output_text"
    )

    return {
        "id": chat_resp.get("id") or f"resp_{uuid.uuid4().hex[:24]}",
        "object": "response",
        "created_at": chat_resp.get("created", int(time.time())),
        "status": "completed",
        "error": None,
        "incomplete_details": None,
        "model": chat_resp.get("model"),
        "output": output,
        "output_text": output_text,
        "usage": usage,
        "metadata": {},
        "parallel_tool_calls": True,
        "tool_choice": "auto",
        "tools": [],
        "temperature": 1.0,
        "top_p": 1.0,
        "_finish_reason": finish,
    }


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
        img_aliases = list(store.list_aliases().keys()) if store.is_active() else list(image_models.keys())
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


async def run_chat(model: str, messages: list, params: Optional[dict] = None) -> dict:
    """Non-streaming chat completion for the UI playground. Routes like the API
    (priority order, fail over on connection errors) but returns the parsed response
    plus which backend actually ran it. Raises HTTPException on no-route/all-failed."""
    candidates = get_routes_for(model)
    if not candidates:
        raise HTTPException(503, f"No healthy backend for model '{model}'")
    base = {"messages": messages, "stream": False}
    for k, v in (params or {}).items():
        if v is not None:
            base[k] = v
    last = "unknown"
    for backend, real_model in candidates:
        body = dict(base, model=real_model)
        url = f"{backend['url']}/v1/chat/completions"
        headers = {"content-type": "application/json", **backend_auth_headers(backend)}
        try:
            async with httpx.AsyncClient(timeout=300.0) as c:
                r = await c.post(url, json=body, headers=headers)
        except (httpx.ConnectError, httpx.TimeoutException, httpx.ReadError) as e:
            last = f"{type(e).__name__}: {e}"
            continue
        try:
            data = r.json()
        except Exception:
            data = {"raw": r.text}
        return {"status": r.status_code, "backend": backend["name"],
                "model": real_model, "alias": model, "response": data}
    raise HTTPException(503, f"All backends failed: {last}")


async def _responses_stream(chat_resp, raw_body: dict, alias: str):
    """A3: translate a backend chat-completion SSE stream into Responses API SSE
    events. Consumes the adapter StreamingResponse's body_iterator (so in-flight
    accounting + stats still fire in the adapter when it drains)."""
    resp_id = f"resp_{uuid.uuid4().hex[:24]}"
    item_id = f"msg_{uuid.uuid4().hex[:24]}"
    created = int(time.time())
    model = raw_body.get("model") or alias
    seq = 0

    def ev(etype: str, payload: dict) -> str:
        nonlocal seq
        body = {"type": etype, "sequence_number": seq, **payload}
        seq += 1
        return f"event: {etype}\ndata: {json.dumps(body, ensure_ascii=False)}\n\n"

    def shell(status: str, output: list, usage=None) -> dict:
        return {
            "id": resp_id, "object": "response", "created_at": created, "status": status,
            "error": None, "incomplete_details": None, "model": model, "output": output,
            "output_text": "".join(p.get("text", "") for o in output if o.get("type") == "message"
                                   for p in (o.get("content") or [])),
            "usage": usage, "metadata": {}, "parallel_tool_calls": True,
            "tool_choice": "auto", "tools": [], "temperature": 1.0, "top_p": 1.0,
        }

    yield ev("response.created", {"response": shell("in_progress", [])})
    yield ev("response.in_progress", {"response": shell("in_progress", [])})
    yield ev("response.output_item.added", {"output_index": 0, "item": {
        "id": item_id, "type": "message", "status": "in_progress", "role": "assistant", "content": []}})
    yield ev("response.content_part.added", {"item_id": item_id, "output_index": 0,
             "content_index": 0, "part": {"type": "output_text", "text": "", "annotations": []}})

    full, buf, usage = "", "", None
    try:
        async for chunk in chat_resp.body_iterator:
            buf += chunk.decode("utf-8", "ignore") if isinstance(chunk, (bytes, bytearray)) else chunk
            while "\n" in buf:
                line, buf = buf.split("\n", 1)
                line = line.strip()
                if not line.startswith("data:"):
                    continue
                data = line[5:].strip()
                if not data or data == "[DONE]":
                    continue
                try:
                    obj = json.loads(data)
                except Exception:
                    continue
                if not isinstance(obj, dict):
                    continue
                if obj.get("model"):
                    model = obj["model"]
                if obj.get("usage"):
                    usage = obj["usage"]
                delta = ((obj.get("choices") or [{}])[0].get("delta") or {}).get("content")
                if delta:
                    full += delta
                    yield ev("response.output_text.delta", {"item_id": item_id,
                             "output_index": 0, "content_index": 0, "delta": delta})
    except Exception as e:
        logger.warning(f"responses SSE translate aborted: {e}")

    part = {"type": "output_text", "text": full, "annotations": []}
    yield ev("response.output_text.done", {"item_id": item_id, "output_index": 0,
             "content_index": 0, "text": full})
    yield ev("response.content_part.done", {"item_id": item_id, "output_index": 0,
             "content_index": 0, "part": part})
    final_item = {"id": item_id, "type": "message", "status": "completed",
                  "role": "assistant", "content": [part]}
    yield ev("response.output_item.done", {"output_index": 0, "item": final_item})
    u = None
    if usage:
        u = {"input_tokens": usage.get("prompt_tokens", 0),
             "output_tokens": usage.get("completion_tokens", 0),
             "total_tokens": usage.get("total_tokens", 0)}
    yield ev("response.completed", {"response": shell("completed", [final_item], u)})


@app.post("/v1/responses")
async def responses(request: Request, authorization: Optional[str] = Header(None)):
    """OpenAI Responses API → Chat Completions bridge.

    LangChain.js (N8N's AI Agent) calls this endpoint by default. Backends that
    only speak Chat Completions still work — request and response are translated
    transparently. `stream:true` is supported: the backend's chat SSE is
    translated into Responses API SSE events (A3).
    """
    raw_body = await request.json()
    chat_body = responses_to_chat(raw_body)
    alias = chat_body.get("model", "")
    gate_request(authorization, request, alias)        # auth + model allow-list + quota

    wants_stream = bool(raw_body.get("stream"))
    ready, busy = resolve_routes(alias)
    if not ready:
        if busy:
            raise HTTPException(503, f"all backends for '{alias}' are busy — retry or set \"park\"")
        raise HTTPException(503, f"No healthy backend for model '{alias}'")

    # A2: route through the shared dispatch path (failover + in-flight + stats);
    # stats keep the /v1/responses label via stats_endpoint.
    if wants_stream:                                   # A3: translate chat SSE → Responses SSE
        chat_body["stream"] = True
        resp = await _dispatch_over(ready, "/v1/chat/completions", alias, chat_body, request,
                                    stats_endpoint="/v1/responses")
        if isinstance(resp, StreamingResponse):
            return StreamingResponse(_responses_stream(resp, raw_body, alias),
                                     media_type="text/event-stream")
        err = json.loads(bytes(resp.body)) if getattr(resp, "body", None) else {}
        raise HTTPException(resp.status_code, (json.dumps(err) or "")[:500])

    chat_body["stream"] = False
    resp = await _dispatch_over(ready, "/v1/chat/completions", alias, chat_body, request,
                                stats_endpoint="/v1/responses")
    chat_resp_json = json.loads(bytes(resp.body)) if getattr(resp, "body", None) else {}
    if resp.status_code >= 400:
        raise HTTPException(resp.status_code, (json.dumps(chat_resp_json) or "")[:500])
    return JSONResponse(chat_to_responses(chat_resp_json), status_code=resp.status_code)


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


# ── Generation (image / video / TTS) ───────────────────────────────────────────
# Native job-based API. A generation alias resolves via `image_models` to ordered
# (backend, workflow) candidates; the request is rendered into the backend's
# protocol by its adapter. Results are persisted in the job store and retrievable
# by id (TTL) — sync mode also returns them inline. No VRAM coordinator yet
# (Phase 2): candidates are filtered by health + the existing busy cap only.

def get_gen_routes(alias: str, include_busy: bool = False) -> list[tuple[dict, dict]]:
    """(backend, candidate) pairs for a generation alias, ordered by **backend
    priority**, filtered to enabled + healthy + not-busy backends.

    An alias holds a flat list of *allowed* backends (no primary/fallback). They are
    tried in backend-priority order; on a connection error the job runner moves to the
    next. An optional per-alias `retries` caps how many backends are attempted (1 +
    retries); blank = try all eligible.

    Reads from the writable store (UI source of truth) when active, falling back
    to the `image_models` config for aliases the store doesn't hold."""
    candidates = store.get(alias) if store.is_active() else None
    if candidates is None:
        candidates = image_models.get(alias, [])
    # generation routes only to ComfyUI backends, so a name shared with an LLM backend
    # resolves to the right one.
    comfy = [b for b in enabled_backends()
             if b.get("type") == "comfyui" and not is_draining(b)]
    out = []
    for cand in candidates:
        b = next((b for b in comfy if b["name"] == cand.get("backend")), None)
        if b is None or not backend_healthy.get(backend_id(b)):
            continue
        if not include_busy and backend_busy(b):        # busy → skipped unless parking
            continue
        out.append((b, cand))
    out.sort(key=lambda bc: bc[0].get("priority", 100))     # backend priority decides order
    raw = next((c.get("retries") for c in candidates if c.get("retries") not in (None, "")), None)
    if raw is not None:
        try:
            out = out[: max(1, int(raw) + 1)]               # first attempt + N retries
        except (ValueError, TypeError):
            pass
    return out


def _gen_inputs_params(body: dict) -> tuple[dict, dict]:
    inputs = {
        "prompt": body.get("prompt", ""),
        "negative_prompt": body.get("negative_prompt", ""),
    }
    params = dict(body.get("params") or {})
    for k in ("width", "height", "steps", "cfg", "seed", "sampler", "scheduler"):
        if k in body and k not in params:        # top-level convenience knobs
            params[k] = body[k]
    return inputs, params


def _job_view(job_id: str, request: Request) -> dict:
    job = jobs.get(job_id)
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


async def _run_job(job_id: str, candidates: list, build_req) -> None:
    """Run a generation job, failing over to the next candidate on connection-type
    errors. Content errors are final (not retried). Stops at the first success."""
    jobs.set_status(job_id, "running")
    last = None
    for backend, cand in candidates:
        adapter = backend_adapters.get(backend_id(backend))
        if adapter is None:
            continue
        try:
            out = await adapter.generate(build_req(backend, cand))
            await asyncio.to_thread(jobs.complete, job_id, out.blobs, out.meta)
            if log_per_call:
                logger.info(f"✓ job {job_id} done on [{backend['name']}] — {len(out.blobs)} artifact(s)")
            return
        except _GEN_FAILOVER_ERRORS as e:
            logger.warning(f"✗ job {job_id} [{backend['name']}] connection issue "
                           f"({type(e).__name__}: {e}) — failing over")
            last = e
            continue
        except Exception as e:
            logger.warning(f"✗ job {job_id} [{backend['name']}] failed: {e}")
            jobs.fail(job_id, str(e))
            return
    jobs.fail(job_id, f"all candidate backends unreachable (connection): {last}")


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
    job = jobs.get(job_id)
    if not job or job.get("status") not in ("queued", "running"):
        return False
    b = next((x for x in backends if x.get("name") == job.get("backend")
              and x.get("type") == "comfyui"), None)
    if b and b.get("url"):
        try:
            async with httpx.AsyncClient(timeout=5.0) as c:
                await c.post(f"{b['url'].rstrip('/')}/interrupt")
        except Exception:
            pass
    t = _gen_tasks.get(job_id)
    if t and not t.done():
        t.cancel()
    jobs.fail(job_id, "cancelled by user")
    return True


async def _run_gen_parked(job_id, alias, force, build_req):
    """Hold a generation job until a backend frees (polls backend-busy), then run it —
    so a busy backend queues instead of 503'ing (async/playground)."""
    deadline = time.monotonic() + async_park_timeout_s
    while True:
        routes = get_gen_routes(alias)
        if force:
            routes = [r for r in routes if r[0].get("name") == force]
        if routes:
            await _run_job(job_id, routes, build_req)
            return
        allc = get_gen_routes(alias, include_busy=True)
        if force:
            allc = [r for r in allc if r[0].get("name") == force]
        if not allc:
            jobs.fail(job_id, f"no healthy backend for '{alias}'" + (f" on '{force}'" if force else ""))
            return
        if time.monotonic() > deadline:
            jobs.fail(job_id, f"park timeout: backend busy for >{async_park_timeout_s:.0f}s")
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

    force = (body.get("backend") or "").strip()         # pin to one backend (playground testing)
    all_cands = get_gen_routes(alias, include_busy=True)
    if force:
        all_cands = [r for r in all_cands if r[0].get("name") == force]
    if not all_cands:
        raise HTTPException(503, f"No healthy backend for generation model '{alias}'"
                                 + (f" on backend '{force}'" if force else ""))

    # LoRA-aware backend eligibility (skipped when a backend is force-pinned): a backend
    # that lacks a requested LoRA is dropped — but only for LoRAs installed on SOME
    # candidate; a LoRA installed nowhere is ignored so priority still decides (per spec).
    # Decided over ALL candidates (incl. busy), so the eligible backend is parked-for
    # rather than spilling to a backend that doesn't have the LoRA.
    eligible_names = None
    req_loras = _requested_loras(body)
    if req_loras and not force:
        avail = set().union(*(backend_loras.get(backend_id(b), set()) for b, _ in all_cands))
        need = req_loras & avail
        if need:
            elig = [r for r in all_cands if need <= backend_loras.get(backend_id(r[0]), set())]
            if elig:                                      # else loras split across backends → no constraint
                eligible_names = {r[0].get("name") for r in elig}

    def _pick(include_busy: bool) -> list:
        rs = get_gen_routes(alias, include_busy=include_busy)
        if force:
            rs = [r for r in rs if r[0].get("name") == force]
        if eligible_names is not None:
            rs = [r for r in rs if r[0].get("name") in eligible_names]
        return rs

    routes = _pick(False)                                # ready (not busy) + lora-eligible
    parked = False
    if not routes:                                       # nothing ready → busy-eligible, or none at all?
        busy = _pick(True)
        if not busy:
            raise HTTPException(503, f"No healthy backend for generation model '{alias}'"
                                     + (f" on backend '{force}'" if force else ""))
        parked, routes = True, busy                      # all busy → park (async: queue, sync: block)

    inputs, params = _gen_inputs_params(body)

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
    job_id = jobs.create(task, alias, first["name"], owner=owner, ttl_s=ttl_s)
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
            _spawn_gen(job_id, _run_gen_parked(job_id, alias, force, build_req))
            return {"job_id": job_id, "status": "queued"}
        await _run_gen_parked(job_id, alias, force, build_req)   # sync: block through the park
        return _job_view(job_id, request)
    if mode == "async":
        _spawn_gen(job_id, _run_job(job_id, routes, build_req))
        return {"job_id": job_id, "status": "queued"}

    await _run_job(job_id, routes, build_req)      # sync: block until done/failed
    return _job_view(job_id, request)


@app.post("/v1/generations")
async def generations(request: Request, authorization: Optional[str] = Header(None)):
    body = await request.json()
    gate_request(authorization, request, body.get("model"))    # auth + allow-list + quota
    view = await run_generation(body, request)
    code = {"queued": 202, "done": 200}.get(view.get("status"), 502)
    return JSONResponse(view, status_code=code)


@app.get("/v1/generations/{alias}/loras")
async def gen_alias_loras(alias: str, request: Request, authorization: Optional[str] = Header(None)):
    """LoRA filenames valid for a generation alias — the union of what's installed on
    the alias's backends. Lets a client present a valid LoRA picker per alias."""
    gate_request(authorization, request, alias)                # auth + allow-list
    if not ((store.get(alias) if store.is_active() else None) or image_models.get(alias)):
        raise HTTPException(404, f"generation alias '{alias}' not found")
    loras: set = set()
    for b, _ in get_gen_routes(alias, include_busy=True):
        loras |= backend_loras.get(backend_id(b), set())
    return {"object": "list", "alias": alias, "loras": sorted(loras)}


def _require_job_owner(authorization: Optional[str], request: Request, job_id: str) -> None:
    """A non-admin user may only touch their own jobs; admin/master/anonymous see all."""
    user = authenticate(authorization)
    if user and user.get("role") != "admin" and not user.get("_master"):
        job = jobs.get(job_id)
        if job and job.get("owner") not in (user["name"], None, "default"):
            raise HTTPException(403, "not your job")


@app.get("/v1/jobs/{job_id}")
async def get_job(job_id: str, request: Request, authorization: Optional[str] = Header(None)):
    _require_job_owner(authorization, request, job_id)
    return _job_view(job_id, request)


@app.get("/v1/jobs/{job_id}/result/{n}")
async def get_job_result(job_id: str, n: int, request: Request, authorization: Optional[str] = Header(None)):
    _require_job_owner(authorization, request, job_id)
    rp = jobs.result_path(job_id, n)
    if rp is None:
        raise HTTPException(404, f"result {n} of job '{job_id}' not found")
    path, mime = rp
    return FileResponse(path, media_type=mime)


@app.get("/v1/jobs/{job_id}/input/{n}")
async def get_job_input(job_id: str, n: int, request: Request, authorization: Optional[str] = Header(None)):
    """Reference image `n` that was submitted with a generation job (kept within TTL)."""
    _require_job_owner(authorization, request, job_id)
    ip = jobs.input_path(job_id, n)
    if ip is None:
        raise HTTPException(404, f"input {n} of job '{job_id}' not found")
    path, mime = ip
    return FileResponse(path, media_type=mime)


@app.post("/v1/jobs/{job_id}/cancel")
async def cancel_job(job_id: str, request: Request, authorization: Optional[str] = Header(None)):
    """Cancel a queued/running generation job (interrupts the backend, frees the GPU)."""
    _require_job_owner(authorization, request, job_id)
    if not await cancel_generation(job_id):
        raise HTTPException(409, f"job '{job_id}' is not cancellable (already done/failed/unknown)")
    return {"job_id": job_id, "status": "failed", "cancelled": True}


# ── OpenAI-compatible image endpoints (C4) ───────────────────────────────────
# Thin shims over the native job path so OpenAI image clients (anima-verse's image
# provider, SDKs) reach the gateway's ComfyUI generation aliases.
#  /v1/images/generations : JSON, text->image (+ bonus LocalAI-style ref_images)
#  /v1/images/edits       : multipart, reference image(s) -> alias image-input slots

# Known OpenAI image-request keys; everything else a client sends is forwarded as a
# native workflow param (loras, seed, steps, cfg, …) → dynamic control, no presets.
_OAI_IMG_KEYS = {"prompt", "model", "n", "size", "response_format", "negative_prompt",
                 "ref_images", "quality", "style", "background", "output_format", "user",
                 "mode", "ttl_s", "params", "stream"}
_EDIT_KNOWN = {"model", "prompt", "negative_prompt", "size", "n", "response_format", "image", "mask"}


def _coerce(s):
    """'12'->12, '0.8'->0.8, else unchanged ('None' / 'x.safetensors' stay strings).
    Multipart fields arrive as strings; the workflow wants real numbers for strengths."""
    if not isinstance(s, str):
        return s
    try:
        return int(s)
    except ValueError:
        pass
    try:
        return float(s)
    except ValueError:
        return s


def _parse_size(size: Optional[str]) -> tuple[int, int]:
    """OpenAI `size` ('1024x1024' | 'auto' | None) -> (width, height)."""
    if not size or str(size).lower() == "auto":
        return 1024, 1024
    try:
        w, h = str(size).lower().split("x")
        return int(w), int(h)
    except (ValueError, AttributeError):
        return 1024, 1024


async def _multipart_list(request: Request) -> dict:
    """multipart/form-data -> {name: [values]} (files->bytes, scalars->str),
    repeated fields preserved; `image[]` normalised to `image`."""
    ctype = request.headers.get("content-type", "")
    m = re.search(r"boundary=([^;]+)", ctype)
    if not m:
        return {}
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
        if not nm:
            continue
        val = content if 'filename="' in head_s else content.decode("utf-8", "replace")
        out.setdefault(nm.group(1).rstrip("[]"), []).append(val)
    return out


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
            async with httpx.AsyncClient(timeout=20.0) as c:
                r = await c.get(ref)
                return r.content if r.status_code == 200 else None
        except Exception:
            return None
    try:
        return base64.b64decode(ref)
    except Exception:
        return None


def _images_uploads(images: list, alias: str) -> dict:
    """Map reference-image blobs positionally onto the alias's ordered image-input
    slots. The cap is the alias's slot count (not a fixed number): extra images are
    ignored, unfilled slots get the adapter's 8x8 placeholder."""
    slots = _gen_image_slots(alias)
    uploads: dict = {}
    for i, slot in enumerate(slots):
        if i < len(images) and images[i]:
            uploads[slot] = bytes(images[i])
    return uploads


def _images_response(view: dict, response_format: str) -> dict:
    """Native job view -> OpenAI images response {created, data:[{url|b64_json}]}."""
    data = []
    for r in view.get("results", []):
        if response_format == "b64_json":
            rp = jobs.result_path(view["job_id"], r["n"])
            if rp:
                with open(rp[0], "rb") as fh:
                    data.append({"b64_json": base64.b64encode(fh.read()).decode()})
        else:
            data.append({"url": r["url"]})
    return {"created": int(time.time()), "data": data}


def _gen_done_or_502(view: dict) -> dict:
    if view.get("status") != "done":
        raise HTTPException(502, f"image generation {view.get('status')}: {view.get('error')}")
    return view


@app.post("/v1/images/generations")
async def images_generations(request: Request, authorization: Optional[str] = Header(None)):
    """OpenAI Images API (text->image). Bonus: LocalAI-style `ref_images`
    (base64/URL list) are accepted and mapped onto the alias's image slots."""
    body = await request.json()
    alias = body.get("model", "")
    gate_request(authorization, request, alias)
    if not body.get("prompt"):
        raise HTTPException(400, "`prompt` is required")
    w, h = _parse_size(body.get("size"))
    refs = body.get("ref_images") or []
    decoded = [await _decode_ref_image(r) for r in refs]
    uploads = _images_uploads(decoded, alias) if refs else None
    extra = {k: v for k, v in body.items() if k not in _OAI_IMG_KEYS}   # dynamic workflow params
    logger.info(f"images/generations '{alias}': ref_images={len(refs)} "   # where client images land
                f"decoded_ok={sum(1 for d in decoded if d)} slots={_gen_image_slots(alias)} "
                f"filled={sorted((uploads or {}).keys())} extra_keys={sorted(extra)}")
    native = {
        "model": alias, "mode": "sync",
        "prompt": body.get("prompt", ""), "negative_prompt": body.get("negative_prompt", ""),
        "params": {"width": w, "height": h, **(body.get("params") or {}), **extra},
        "output": {"n": int(body.get("n") or 1), "mode": "sync"},
    }
    view = _gen_done_or_502(await run_generation(native, request, upload_images=uploads))
    return JSONResponse(_images_response(view, body.get("response_format") or "url"))


@app.post("/v1/images/edits")
async def images_edits(request: Request, authorization: Optional[str] = Header(None)):
    """OpenAI Images Edit API (multipart): `image` field(s) carry reference images,
    mapped positionally onto the alias's declared image-input slots (cap = slot count;
    OpenAI itself allows 1 for dall-e-2, up to 16 for gpt-image-1)."""
    f = await _multipart_list(request)
    one = lambda k, d="": (f.get(k) or [d])[0]
    alias = (one("model") or "").strip()
    gate_request(authorization, request, alias)
    images = [v for v in (f.get("image") or []) if isinstance(v, (bytes, bytearray))]
    if not images:
        raise HTTPException(400, "at least one `image` file is required")
    masks = [v for v in (f.get("mask") or []) if isinstance(v, (bytes, bytearray))]
    logger.info(f"images/edits '{alias}': image_files={len(images)} mask_files={len(masks)} "  # where they land
                f"slots={_gen_image_slots(alias)} "
                f"scalar_keys={sorted(k for k, vs in f.items() if not isinstance((vs or [None])[0], (bytes, bytearray)))}")
    # OpenAI `mask` field → the next positional slot (the mask slot is normally last in
    # the mapping order, after the reference image[s]).
    images = (images + masks)[:16]                      # OpenAI gpt-image-1 max; workflow may use fewer
    w, h = _parse_size(one("size") or None)
    extra = {k: _coerce(one(k)) for k, vs in f.items()  # dynamic scalar params (loras, seed, …)
             if k not in _EDIT_KNOWN and not isinstance((vs or [None])[0], (bytes, bytearray))}
    native = {
        "model": alias, "mode": "sync", "task": "img2img",
        "prompt": one("prompt"), "negative_prompt": one("negative_prompt"),
        "params": {"width": w, "height": h, **extra},
        "output": {"n": int((one("n") or "1") or 1), "mode": "sync"},
    }
    view = _gen_done_or_502(await run_generation(native, request, upload_images=_images_uploads(images, alias)))
    return JSONResponse(_images_response(view, one("response_format") or "url"))


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
        "parked": len(_park_waiters),
        "jobs_active": jobs.is_active(),
        "jobs_counts": jobs.counts(),
        "jobs_recent": jobs.recent(15),
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
        async with httpx.AsyncClient() as client:
            await asyncio.gather(*[refresh_backend(b, client) for b in enabled_backends()])
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
admin.bind(lambda: [b for b in backends if b.get("type") == "comfyui"],
           run_generation, gateway_info, apply_backend_change,
           llm_backends=llm_backends_info,
           config_chat_aliases=lambda: dict(config_virtual_models),
           apply_chat_aliases=apply_chat_aliases,
           run_chat=run_chat,
           routing_snapshot=routing_snapshot,
           server_info=server_info,
           apply_server_settings=apply_server_settings_hook,
           apply_users=apply_users,
           resolve_admin=resolve_admin, ui_locked=ui_locked,
           dashboard_snapshot=dashboard_snapshot, cancel_generation=cancel_generation,
           drain_backend=begin_drain, cancel_drain=cancel_drain,
           set_backend_enabled=set_backend_enabled)


@app.get("/health")
async def health():
    return {
        "status": "ok",
        "parked": len(_park_waiters),
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
        # Aliases that shadow a real model on a backend they don't map (→ that
        # model is unreachable by its bare name). Empty list = no such conflict.
        "alias_model_conflicts": [c for c in alias_model_conflicts() if c["shadowed"]],
    }
