import asyncio
import logging
import time
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Optional

import httpx
import yaml
from fastapi import FastAPI, Header, HTTPException, Request
from fastapi.responses import JSONResponse, StreamingResponse
from watchfiles import awatch

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger(__name__)

# ── Config ────────────────────────────────────────────────────────────────────

CONFIG_PATH = Path("config.yaml")

def load_config() -> None:
    """Read config.yaml and (re)bind module-level config values."""
    global config, backends, virtual_models, health_check_interval, api_key
    with open(CONFIG_PATH) as f:
        config = yaml.safe_load(f)
    backends = sorted(config["backends"], key=lambda b: b["priority"])
    virtual_models = config.get("virtual_models", {})
    health_check_interval = config.get("health_check_interval", 30)
    api_key = config.get("api_key")


def log_config_summary() -> None:
    logger.info(f"Loaded {len(backends)} backend(s):")
    for b in backends:
        state = "ENABLED " if is_enabled(b) else "DISABLED"
        logger.info(f"  [{state}] {b['name']:25} priority={b['priority']}  url={b['url']}")
    logger.info(f"Loaded {len(virtual_models)} virtual alias(es):")
    for alias, mapping in virtual_models.items():
        if isinstance(mapping, dict):
            for bname, real in mapping.items():
                logger.info(f"  {alias:15} → [{bname}] {real}")
        else:
            logger.info(f"  {alias:15} → {mapping}  (all backends)")
    logger.info(f"health_check_interval={health_check_interval}s  api_key={'set' if api_key else 'unset'}")


# Initial load — populates module globals
config: dict
backends: list[dict]
virtual_models: dict
health_check_interval: int
api_key: Optional[str]
load_config()

# ── State ─────────────────────────────────────────────────────────────────────

backend_models: dict[str, set[str]] = {}   # name → {model_id, ...}
backend_healthy: dict[str, bool] = {}       # name → bool

# ── Health / Discovery ────────────────────────────────────────────────────────

def is_enabled(backend: dict) -> bool:
    return backend.get("enabled", True)


def enabled_backends() -> list[dict]:
    return [b for b in backends if is_enabled(b)]


def backend_auth_headers(backend: dict) -> dict:
    key = backend.get("api_key")
    return {"authorization": f"Bearer {key}"} if key else {}


async def refresh_backend(backend: dict, client: httpx.AsyncClient) -> None:
    name, url = backend["name"], backend["url"]
    try:
        resp = await client.get(f"{url}/v1/models", headers=backend_auth_headers(backend), timeout=5.0)
        resp.raise_for_status()
        models = {m["id"] for m in resp.json().get("data", [])}
        backend_models[name] = models
        if not backend_healthy.get(name):
            logger.info(f"[{name}] UP  — {len(models)} models: {sorted(models)}")
        backend_healthy[name] = True
    except Exception as e:
        if backend_healthy.get(name, True):
            logger.warning(f"[{name}] DOWN — {e}")
        backend_healthy[name] = False
        backend_models[name] = set()


async def health_loop() -> None:
    async with httpx.AsyncClient() as client:
        while True:
            for backend in enabled_backends():
                await refresh_backend(backend, client)
            await asyncio.sleep(health_check_interval)


def reload_config() -> None:
    """Re-read config.yaml and apply. Keeps old config on parse error."""
    old_names = {b["name"] for b in backends}
    try:
        load_config()
    except Exception as e:
        logger.error(f"Config reload FAILED, keeping previous config: {e}")
        return
    # Drop state for backends removed from config
    new_names = {b["name"] for b in backends}
    for stale in old_names - new_names:
        backend_healthy.pop(stale, None)
        backend_models.pop(stale, None)
        logger.info(f"  removed backend [{stale}] — state cleared")
    logger.info("Config reloaded.")
    log_config_summary()


async def watch_config_loop() -> None:
    async for _ in awatch(CONFIG_PATH):
        logger.info(f"Detected change in {CONFIG_PATH} — reloading")
        reload_config()


@asynccontextmanager
async def lifespan(app: FastAPI):
    logger.info("Starting LLM Gateway")
    log_config_summary()
    async with httpx.AsyncClient() as client:
        await asyncio.gather(*[refresh_backend(b, client) for b in enabled_backends()])
    health_task = asyncio.create_task(health_loop())
    watch_task = asyncio.create_task(watch_config_loop())
    yield
    health_task.cancel()
    watch_task.cancel()


# ── App ───────────────────────────────────────────────────────────────────────

app = FastAPI(title="LLM Gateway", lifespan=lifespan)

# ── Helpers ───────────────────────────────────────────────────────────────────

def check_auth(authorization: Optional[str]) -> None:
    if not api_key:
        return
    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(401, "Missing Authorization header")
    if authorization[7:] != api_key:
        raise HTTPException(401, "Invalid API key")


def resolve_for_backend(alias: str, backend_name: str) -> Optional[str]:
    """Real model name for this alias on this backend, or None if not mapped here.

    - alias not in virtual_models → returns alias unchanged (pass-through)
    - alias maps to string         → that string on every backend
    - alias maps to dict           → looked up by backend name (may be absent)
    """
    mapping = virtual_models.get(alias)
    if mapping is None:
        return alias
    if isinstance(mapping, str):
        return mapping
    if isinstance(mapping, dict):
        return mapping.get(backend_name)
    return None


def get_routes_for(alias: str) -> list[tuple[dict, str]]:
    """(backend, real_model) pairs to try, in priority order."""
    routes = []
    for b in enabled_backends():
        if not backend_healthy.get(b["name"]):
            continue
        real = resolve_for_backend(alias, b["name"])
        if real is None:
            continue
        if real in backend_models.get(b["name"], set()):
            routes.append((b, real))
    return routes


async def proxy(backend: dict, path: str, request: Request, body: dict):
    url = f"{backend['url']}{path}"
    headers = {
        k: v for k, v in request.headers.items()
        if k.lower() not in ("host", "content-length", "authorization")
    }
    headers.update(backend_auth_headers(backend))

    if body.get("stream"):
        async def generate():
            async with httpx.AsyncClient() as client:
                async with client.stream("POST", url, json=body, headers=headers, timeout=300.0) as resp:
                    async for chunk in resp.aiter_bytes():
                        yield chunk
        return StreamingResponse(generate(), media_type="text/event-stream")

    async with httpx.AsyncClient() as client:
        resp = await client.post(url, json=body, headers=headers, timeout=300.0)
    return JSONResponse(resp.json(), status_code=resp.status_code)


async def route(path: str, request: Request, authorization: Optional[str]) -> JSONResponse | StreamingResponse:
    check_auth(authorization)
    body = await request.json()
    alias = body.get("model", "")

    candidates = get_routes_for(alias)
    if not candidates:
        raise HTTPException(503, f"No healthy backend for model '{alias}'")

    last_error: Exception = Exception("unknown")
    for backend, real_model in candidates:
        body["model"] = real_model
        try:
            logger.info(f"→ [{backend['name']}] {alias} → {real_model}")
            return await proxy(backend, path, request, body)
        except (httpx.ConnectError, httpx.TimeoutException) as e:
            logger.warning(f"✗ [{backend['name']}] {e} — trying next")
            last_error = e

    raise HTTPException(503, f"All backends failed: {last_error}")


# ── Routes ────────────────────────────────────────────────────────────────────

@app.get("/v1/models")
async def list_models(authorization: Optional[str] = Header(None)):
    check_auth(authorization)
    seen: set[str] = set()
    data = []

    for backend in enabled_backends():
        for mid in sorted(backend_models.get(backend["name"], set())):
            if mid not in seen:
                seen.add(mid)
                data.append({"id": mid, "object": "model", "created": int(time.time()), "owned_by": "llm-gateway"})

    # Virtual models (if alias not already exposed by a backend)
    for alias in virtual_models:
        if alias not in seen:
            data.append({"id": alias, "object": "model", "created": int(time.time()), "owned_by": "llm-gateway (virtual)"})

    return {"object": "list", "data": data}


@app.get("/v1/models/{model_id:path}")
async def get_model(model_id: str, authorization: Optional[str] = Header(None)):
    check_auth(authorization)
    if model_id in virtual_models:
        return {"id": model_id, "object": "model", "created": int(time.time()), "owned_by": "llm-gateway (virtual)"}
    for backend in enabled_backends():
        if model_id in backend_models.get(backend["name"], set()):
            return {"id": model_id, "object": "model", "created": int(time.time()), "owned_by": "llm-gateway"}
    raise HTTPException(404, f"Model '{model_id}' not found")


@app.post("/v1/chat/completions")
async def chat_completions(request: Request, authorization: Optional[str] = Header(None)):
    return await route("/v1/chat/completions", request, authorization)


@app.post("/v1/completions")
async def completions(request: Request, authorization: Optional[str] = Header(None)):
    return await route("/v1/completions", request, authorization)


@app.get("/health")
async def health():
    return {
        "status": "ok",
        "backends": {
            b["name"]: {
                "enabled": is_enabled(b),
                "healthy": is_enabled(b) and backend_healthy.get(b["name"], False),
                "priority": b["priority"],
                "models": sorted(backend_models.get(b["name"], set())) if is_enabled(b) else [],
            }
            for b in backends
        },
        "virtual_models": virtual_models,
    }
