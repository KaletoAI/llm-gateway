import asyncio
import logging
import time
from contextlib import asynccontextmanager
from typing import Optional

import httpx
import yaml
from fastapi import FastAPI, Header, HTTPException, Request
from fastapi.responses import JSONResponse, StreamingResponse

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger(__name__)

# ── Config ────────────────────────────────────────────────────────────────────

with open("config.yaml") as f:
    config = yaml.safe_load(f)

backends: list[dict] = sorted(config["backends"], key=lambda b: b["priority"])
virtual_models: dict[str, str] = config.get("virtual_models", {})
health_check_interval: int = config.get("health_check_interval", 30)
api_key: Optional[str] = config.get("api_key")

# ── State ─────────────────────────────────────────────────────────────────────

backend_models: dict[str, set[str]] = {}   # name → {model_id, ...}
backend_healthy: dict[str, bool] = {}       # name → bool

# ── Health / Discovery ────────────────────────────────────────────────────────

async def refresh_backend(backend: dict, client: httpx.AsyncClient) -> None:
    name, url = backend["name"], backend["url"]
    try:
        resp = await client.get(f"{url}/v1/models", timeout=5.0)
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
            for backend in backends:
                await refresh_backend(backend, client)
            await asyncio.sleep(health_check_interval)


@asynccontextmanager
async def lifespan(app: FastAPI):
    logger.info("Starting LLM Gateway — discovering backends...")
    async with httpx.AsyncClient() as client:
        await asyncio.gather(*[refresh_backend(b, client) for b in backends])
    task = asyncio.create_task(health_loop())
    yield
    task.cancel()


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


def resolve_model(model: str) -> str:
    """Resolve virtual alias → real model name."""
    return virtual_models.get(model, model)


def get_backends_for_model(model: str) -> list[dict]:
    """Healthy backends that serve this model, in priority order."""
    return [
        b for b in backends
        if backend_healthy.get(b["name"])
        and model in backend_models.get(b["name"], set())
    ]


async def proxy(backend: dict, path: str, request: Request, body: dict):
    url = f"{backend['url']}{path}"
    headers = {
        k: v for k, v in request.headers.items()
        if k.lower() not in ("host", "content-length", "authorization")
    }

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

    real_model = resolve_model(body.get("model", ""))
    body["model"] = real_model

    candidates = get_backends_for_model(real_model)
    if not candidates:
        raise HTTPException(503, f"No healthy backend for model '{real_model}'")

    last_error: Exception = Exception("unknown")
    for backend in candidates:
        try:
            logger.info(f"→ [{backend['name']}] {real_model}")
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

    for backend in backends:
        for mid in sorted(backend_models.get(backend["name"], set())):
            if mid not in seen:
                seen.add(mid)
                data.append({"id": mid, "object": "model", "created": int(time.time()), "owned_by": "llm-gateway"})

    # Virtual models (if alias not already exposed by a backend)
    for alias in virtual_models:
        if alias not in seen:
            data.append({"id": alias, "object": "model", "created": int(time.time()), "owned_by": "llm-gateway (virtual)"})

    return {"object": "list", "data": data}


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
                "healthy": backend_healthy.get(b["name"], False),
                "priority": b["priority"],
                "models": sorted(backend_models.get(b["name"], set())),
            }
            for b in backends
        },
        "virtual_models": virtual_models,
    }
