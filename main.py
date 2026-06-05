import asyncio
import logging
import time
import uuid
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, Optional

import httpx
import uvicorn
import yaml
from fastapi import FastAPI, Header, HTTPException, Request
from fastapi.responses import JSONResponse, StreamingResponse
from watchfiles import awatch

import stats

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger(__name__)

# ── Config ────────────────────────────────────────────────────────────────────

CONFIG_PATH = Path("config.yaml")

def load_config() -> None:
    """Read config.yaml and (re)bind module-level config values."""
    global config, backends, virtual_models, health_check_interval, api_key
    global stats_cfg, log_per_call, model_prefix
    with open(CONFIG_PATH) as f:
        config = yaml.safe_load(f)
    backends = sorted(config["backends"], key=lambda b: b["priority"])
    virtual_models = config.get("virtual_models", {})
    health_check_interval = config.get("health_check_interval", 30)
    api_key = config.get("api_key")
    stats_cfg = config.get("stats") or {}
    log_per_call = config.get("log_per_call", True)
    model_prefix = config.get("model_prefix", True)


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
stats_cfg: dict
log_per_call: bool
model_prefix: bool
load_config()

# ── State ─────────────────────────────────────────────────────────────────────

backend_models: dict[str, set[str]] = {}                       # name → {model_id, ...}
backend_healthy: dict[str, bool] = {}                          # name → bool
backend_pricing: dict[str, dict[str, dict[str, float]]] = {}   # name → {model_id → {input, output}}

# ── Health / Discovery ────────────────────────────────────────────────────────

def is_enabled(backend: dict) -> bool:
    return backend.get("enabled", True)


def enabled_backends() -> list[dict]:
    return [b for b in backends if is_enabled(b)]


def backend_auth_headers(backend: dict) -> dict:
    key = backend.get("api_key")
    return {"authorization": f"Bearer {key}"} if key else {}


def _to_float(v) -> float:
    try:
        return float(v)
    except (TypeError, ValueError):
        return 0.0


def normalize_pricing(m: dict) -> Optional[dict[str, float]]:
    """Per-model pricing normalized to USD per **million** tokens, or None.

    Handles the two upstream schemas we see in the wild:
      - Together:   pricing.input / pricing.output  — numbers, already per-million
      - OpenRouter: pricing.prompt / pricing.completion — strings, per *single*
                    token, so multiplied by 1e6 to match the per-million convention
    Local backends (llama-swap / vLLM) carry no pricing → None.
    """
    p = m.get("pricing")
    if not isinstance(p, dict):
        return None
    if "input" in p or "output" in p:            # Together-style (per-million)
        return {"input": _to_float(p.get("input")), "output": _to_float(p.get("output"))}
    if "prompt" in p or "completion" in p:        # OpenRouter-style (per-token)
        return {"input": _to_float(p.get("prompt")) * 1_000_000,
                "output": _to_float(p.get("completion")) * 1_000_000}
    return None


def _is_priced(m: dict) -> bool:
    pr = normalize_pricing(m)
    return bool(pr and (pr["input"] or pr["output"]))


def extract_pricing(payload) -> dict[str, dict[str, float]]:
    """Per-model pricing from a /v1/models response, keyed by model id. Values
    are USD per million tokens (see normalize_pricing). Together + OpenRouter
    expose pricing; OpenAI/llama-swap don't → empty dict."""
    data = payload["data"] if isinstance(payload, dict) else payload
    out: dict[str, dict[str, float]] = {}
    for m in data or []:
        if "id" not in m:
            continue
        if _is_priced(m):
            out[m["id"]] = normalize_pricing(m)  # type: ignore[assignment]
    return out


def extract_models(payload, backend: dict) -> set[str]:
    """Return model IDs from a /v1/models response, applying per-backend filters.

    Accepts both `{"data": [...]}` (OpenAI / llama-swap) and a bare `[...]`
    (e.g. together.ai). Optional per-backend filters:
      - chat_only:       keep only models with type=="chat" (skip image/video/embedding/…)
      - serverless_only: keep only models with non-zero token pricing
                         (excludes Together's dedicated-only endpoints)
    Filters are skipped silently for backends that don't expose the relevant
    metadata field — llama-swap etc. stay unaffected.
    """
    data = payload["data"] if isinstance(payload, dict) else payload
    if backend.get("chat_only"):
        data = [m for m in data if _is_chat_model(m)]
    if backend.get("serverless_only"):
        data = [m for m in data if _is_priced(m)]
    return {m["id"] for m in data if "id" in m}


def _is_chat_model(m: dict) -> bool:
    """True unless the model is clearly not chat-completions routable.

    - Together tags models with `type`; keep only type=="chat".
    - OpenRouter has no `type` but exposes architecture.output_modalities;
      drop models that can't emit text (image-/audio-only output).
    - Backends without either field (llama-swap, vLLM) always pass.
    """
    if m.get("type", "chat") != "chat":
        return False
    om = (m.get("architecture") or {}).get("output_modalities")
    if om is not None and "text" not in om:
        return False
    return True


async def refresh_backend(backend: dict, client: httpx.AsyncClient) -> None:
    name, url = backend["name"], backend["url"]
    try:
        resp = await client.get(f"{url}/v1/models", headers=backend_auth_headers(backend), timeout=5.0)
        resp.raise_for_status()
        payload = resp.json()
        models = extract_models(payload, backend)
        backend_models[name] = models
        backend_pricing[name] = extract_pricing(payload)
        if not backend_healthy.get(name):
            logger.info(f"[{name}] UP  — {len(models)} models, {len(backend_pricing[name])} priced")
        backend_healthy[name] = True
    except Exception as e:
        if backend_healthy.get(name, True):
            logger.warning(f"[{name}] DOWN — {e}")
        backend_healthy[name] = False
        backend_models[name] = set()
        backend_pricing[name] = {}


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
        backend_pricing.pop(stale, None)
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

    stats_server: Optional[uvicorn.Server] = None
    stats_task: Optional[asyncio.Task] = None
    prune_task: Optional[asyncio.Task] = None
    if stats_cfg.get("enabled"):
        stats.init(stats_cfg.get("db_path", "stats.db"))
        scfg = uvicorn.Config(
            stats.stats_app,
            host=stats_cfg.get("bind", "0.0.0.0"),
            port=stats_cfg.get("port", 4001),
            log_level="warning",
        )
        stats_server = uvicorn.Server(scfg)
        stats_task = asyncio.create_task(stats_server.serve())
        logger.info(f"stats: dashboard on http://{scfg.host}:{scfg.port}/")
        prune_task = asyncio.create_task(stats.prune_loop(stats_cfg.get("retention_days", 0)))

    yield
    health_task.cancel()
    watch_task.cancel()
    if stats_server is not None:
        stats_server.should_exit = True
        if stats_task is not None:
            await stats_task
    if prune_task is not None:
        prune_task.cancel()


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


def get_routes_for(alias: str) -> list[tuple[dict, str]]:
    """(backend, real_model) pairs to try, in priority order.

    A '<backend>/<model>' alias routes explicitly to that one backend (prefix
    stripped before forwarding). A bare alias falls back to priority routing
    across every backend that exposes it.
    """
    bname, bare = split_backend_prefix(alias)
    if bname is not None:
        b = next((b for b in enabled_backends() if b["name"] == bname), None)
        if b is None or not backend_healthy.get(bname):
            return []
        real = resolve_for_backend(bare, bname)
        if real is not None and real in backend_models.get(bname, set()):
            return [(b, real)]
        return []

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


def _source_of(request: Request) -> str:
    return request.headers.get("x-source") or (request.client.host if request.client else "unknown")


def _cost_usd(backend_name: str, model_id: Optional[str], in_tok: int, out_tok: int) -> float:
    """USD cost for a call from cached pricing (Together-style /v1/models). 0 if unknown."""
    if not model_id:
        return 0.0
    p = backend_pricing.get(backend_name, {}).get(model_id, {})
    return ((in_tok or 0) * p.get("input", 0.0) + (out_tok or 0) * p.get("output", 0.0)) / 1_000_000


async def proxy(backend: dict, path: str, request: Request, body: dict, alias: str):
    url = f"{backend['url']}{path}"
    headers = {
        k: v for k, v in request.headers.items()
        if k.lower() not in ("host", "content-length", "authorization")
    }
    headers.update(backend_auth_headers(backend))
    bname = backend["name"]
    real_model = body.get("model")
    source = _source_of(request)
    started = time.monotonic()

    if body.get("stream"):
        async def generate():
            stream_status = 0
            async with httpx.AsyncClient() as client:
                async with client.stream("POST", url, json=body, headers=headers, timeout=300.0) as resp:
                    stream_status = resp.status_code
                    if log_per_call:
                        logger.info(f"← [{bname}] {path} HTTP {stream_status} (stream open, {(time.monotonic()-started):.2f}s)")
                    async for chunk in resp.aiter_bytes():
                        yield chunk
            elapsed_ms = int((time.monotonic() - started) * 1000)
            asyncio.create_task(stats.record_call(
                duration_ms=elapsed_ms, backend=bname, source=source,
                alias=alias, model=real_model, endpoint=path,
                status=stream_status, input_tokens=0, output_tokens=0, cost_usd=0.0,
            ))
        return StreamingResponse(generate(), media_type="text/event-stream")

    async with httpx.AsyncClient() as client:
        resp = await client.post(url, json=body, headers=headers, timeout=300.0)
    elapsed_ms = int((time.monotonic() - started) * 1000)
    if log_per_call:
        logger.info(f"← [{bname}] {path} HTTP {resp.status_code} ({elapsed_ms} ms)")
    try:
        resp_json = resp.json()
    except Exception:
        resp_json = {}
    usage = (resp_json.get("usage") or {}) if isinstance(resp_json, dict) else {}
    in_tok = int(usage.get("prompt_tokens") or 0)
    out_tok = int(usage.get("completion_tokens") or 0)
    asyncio.create_task(stats.record_call(
        duration_ms=elapsed_ms, backend=bname, source=source,
        alias=alias, model=real_model, endpoint=path,
        status=resp.status_code,
        input_tokens=in_tok, output_tokens=out_tok,
        cost_usd=_cost_usd(bname, real_model, in_tok, out_tok),
    ))
    return JSONResponse(resp_json, status_code=resp.status_code)


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
            if log_per_call:
                logger.info(f"→ [{backend['name']}] {alias} → {real_model}")
            return await proxy(backend, path, request, body, alias)
        except (httpx.ConnectError, httpx.TimeoutException) as e:
            logger.warning(f"✗ [{backend['name']}] {e} — trying next")
            last_error = e

    raise HTTPException(503, f"All backends failed: {last_error}")


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
async def list_models(authorization: Optional[str] = Header(None)):
    check_auth(authorization)
    now = int(time.time())
    seen: set[str] = set()
    data = []

    for backend in enabled_backends():
        bname = backend["name"]
        for mid in sorted(backend_models.get(bname, set())):
            # With model_prefix on, ids are '<backend>/<model>' so the same model
            # on two backends stays distinct and the provider is visible. Off →
            # legacy behaviour: bare ids, deduplicated across backends.
            disp = f"{bname}/{mid}" if model_prefix else mid
            if disp not in seen:
                seen.add(disp)
                data.append({"id": disp, "object": "model", "created": now, "owned_by": bname})

    # Virtual aliases are cross-backend → always listed bare (no prefix).
    for alias in virtual_models:
        if alias not in seen:
            data.append({"id": alias, "object": "model", "created": now, "owned_by": "llm-gateway (virtual)"})

    return {"object": "list", "data": data}


@app.get("/v1/models/{model_id:path}")
async def get_model(model_id: str, authorization: Optional[str] = Header(None)):
    check_auth(authorization)
    now = int(time.time())
    if model_id in virtual_models:
        return {"id": model_id, "object": "model", "created": now, "owned_by": "llm-gateway (virtual)"}
    bname, bare = split_backend_prefix(model_id)
    if bname is not None:
        if bare in backend_models.get(bname, set()):
            return {"id": model_id, "object": "model", "created": now, "owned_by": bname}
    else:
        for backend in enabled_backends():
            if model_id in backend_models.get(backend["name"], set()):
                return {"id": model_id, "object": "model", "created": now, "owned_by": backend["name"]}
    raise HTTPException(404, f"Model '{model_id}' not found")


@app.post("/v1/chat/completions")
async def chat_completions(request: Request, authorization: Optional[str] = Header(None)):
    return await route("/v1/chat/completions", request, authorization)


@app.post("/v1/responses")
async def responses(request: Request, authorization: Optional[str] = Header(None)):
    """OpenAI Responses API → Chat Completions bridge.

    LangChain.js (N8N's AI Agent) calls this endpoint by default. Backends that
    only speak Chat Completions still work — request and response are translated
    transparently. Streaming is downgraded to non-streaming; SSE event-stream
    translation isn't implemented.
    """
    check_auth(authorization)
    raw_body = await request.json()
    chat_body = responses_to_chat(raw_body)
    alias = chat_body.get("model", "")

    candidates = get_routes_for(alias)
    if not candidates:
        raise HTTPException(503, f"No healthy backend for model '{alias}'")

    source = _source_of(request)
    last_error: Exception = Exception("unknown")
    for backend, real_model in candidates:
        chat_body["model"] = real_model
        bname = backend["name"]
        url = f"{backend['url']}/v1/chat/completions"
        headers = {"content-type": "application/json"}
        headers.update(backend_auth_headers(backend))
        started = time.monotonic()
        try:
            if log_per_call:
                logger.info(f"→ [{bname}] {alias} → {real_model}  (responses→chat)")
            async with httpx.AsyncClient() as client:
                resp = await client.post(url, json=chat_body, headers=headers, timeout=300.0)
            elapsed_ms = int((time.monotonic() - started) * 1000)
            if resp.status_code >= 400:
                if log_per_call:
                    logger.warning(f"✗ [{bname}] /v1/responses HTTP {resp.status_code} ({elapsed_ms} ms) — trying next")
                asyncio.create_task(stats.record_call(
                    duration_ms=elapsed_ms, backend=bname, source=source,
                    alias=alias, model=real_model, endpoint="/v1/responses",
                    status=resp.status_code, input_tokens=0, output_tokens=0, cost_usd=0.0,
                ))
                last_error = HTTPException(resp.status_code, resp.text[:500])
                continue
            if log_per_call:
                logger.info(f"← [{bname}] /v1/responses HTTP {resp.status_code} ({elapsed_ms} ms)")
            chat_resp_json = resp.json()
            usage = (chat_resp_json.get("usage") or {})
            in_tok = int(usage.get("prompt_tokens") or 0)
            out_tok = int(usage.get("completion_tokens") or 0)
            asyncio.create_task(stats.record_call(
                duration_ms=elapsed_ms, backend=bname, source=source,
                alias=alias, model=real_model, endpoint="/v1/responses",
                status=resp.status_code,
                input_tokens=in_tok, output_tokens=out_tok,
                cost_usd=_cost_usd(bname, real_model, in_tok, out_tok),
            ))
            return JSONResponse(chat_to_responses(chat_resp_json))
        except (httpx.ConnectError, httpx.TimeoutException) as e:
            logger.warning(f"✗ [{bname}] {e} — trying next")
            last_error = e

    if isinstance(last_error, HTTPException):
        raise last_error
    raise HTTPException(503, f"All backends failed: {last_error}")


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
