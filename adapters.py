"""Backend adapters — the pluggable per-backend protocol layer.

Phase 0 of the multimodal-gateway plan (see docs/multimodal-gateway-plan.md):
introduce the seam *without any behaviour change*. `OpenAIAdapter` is a 1:1 move
of the former module-level `proxy()` (dispatch) and the `/v1/models` discovery
helpers (`extract_models` / `extract_pricing` / …). New protocols (ComfyUI, …)
implement `BackendAdapter` and register in `ADAPTERS`.

Design notes:
- Adapters never import `main` (avoids an import cycle). Everything they need
  from the app — the in-flight counter, stats sink, pricing, log flag — is
  injected via `AdapterContext`, with hot-reloadable values read through
  callables so nothing is cached at construction time.
- The discovery helpers below are pure functions of (payload, backend dict);
  they live here because they are OpenAI-`/v1/models`-payload-specific.
"""
from __future__ import annotations

import asyncio
import copy
import fnmatch
import json
import logging
import os
import random
import re
import struct
import time
import zlib
from abc import ABC, abstractmethod
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from typing import Any, Callable, Optional

import httpx
from fastapi import Request
from fastapi.responses import Response, StreamingResponse

logger = logging.getLogger(__name__)

# Outbound timeouts (seconds): discovery is short (health tick must stay snappy),
# chat generous (long completions), ComfyUI uploads sized for LAN image posts.
_CHAT_TIMEOUT = 300.0
_DISCOVERY_TIMEOUT = 5.0
_COMFY_DISCOVERY_TIMEOUT = 8.0
_UPLOAD_TIMEOUT = 20.0


# ── OpenAI /v1/models discovery helpers (moved verbatim from main.py) ──────────

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


# ── Adapter contracts ─────────────────────────────────────────────────────────

@dataclass
class Capabilities:
    """What a backend exposes, as seen by a discovery poll. Phase 0 carries the
    OpenAI shape (models + pricing); later phases add tasks/workflows."""
    models: set[str]
    pricing: dict[str, dict[str, float]]
    loras: set = field(default_factory=set)         # ComfyUI: installed LoRA filenames


@dataclass
class NormalizedRequest:
    """Container threaded from input adapter → router → backend adapter.

    Chat/LLM path (Phase 0) uses path/alias/real_model/body/raw/stream and the
    OpenAI adapter forwards `body` verbatim. The generation path (Phase 1+) adds
    task/inputs/params/output/workflow, populated by the native input adapter and
    rendered into a backend protocol by e.g. ComfyUIAdapter.
    """
    path: str = ""
    alias: str = ""
    real_model: Optional[str] = None
    body: dict = field(default_factory=dict)
    raw: Optional[Request] = None
    stream: bool = False
    stats_endpoint: Optional[str] = None            # stats label when it differs from path (e.g. /v1/responses)
    # CONVENTION: body keys starting with "_" are gateway-private control fields
    # (e.g. route() stashes body["_reasoning"]); _prepare() strips them all before
    # forwarding, so they never reach a backend.
    reasoning: Optional[str] = None                 # normalized reasoning control: "off" | "on" | None(auto)
    # ── generation extension ──
    task: str = "chat"                              # text2img | img2video | tts | …
    inputs: dict = field(default_factory=dict)      # prompt, negative_prompt, …
    params: dict = field(default_factory=dict)      # width, height, steps, cfg, seed, …
    output: dict = field(default_factory=dict)      # n, format, mode, ttl_s
    workflow: Optional[str] = None                  # workflow file path (share/legacy)
    workflow_json: Optional[dict] = None            # gateway-owned API workflow (preferred)
    node_mapping: dict = field(default_factory=dict)  # {param: {node, field}} request-time
    fixed: list = field(default_factory=list)       # [{node, field, value}] admin-pinned (models, …)
    upload_image: Optional[bytes] = None            # legacy single reference image (back-compat)
    upload_images: dict = field(default_factory=dict)  # {param: bytes} per request-field image uploads
    loras: Optional[list] = None                    # [{name, strength}] — pairs resolved server-side
    output_node: Optional[str] = None               # alias setting: ONLY this node's artifacts count
    output_ext: Optional[str] = None                # alias setting: fetch the sibling with THIS extension
    output_globs: Optional[list] = None             # alias setting: deliver every file matching these globs
    output_cases: Optional[list] = None             # alias setting: [{rig, globs}] — first detected case wins
    texture_format: Optional[str] = None            # alias setting: "jpeg" transcodes generic texture PNGs
    slot_held: bool = False                         # caller already holds the in-flight slot (chain) —
                                                    # generate() must not inc/dec it a second time


@dataclass
class GenBlob:
    """One produced artifact (image/video/audio/file) as raw bytes + type hints."""
    data: bytes
    mime: str
    kind: str                                       # "image" | "video" | "audio" | "file"
    name: Optional[str] = None                      # original ComfyUI filename (display/download)


@dataclass
class GenOutput:
    """Result of a generation dispatch: the artifacts plus run metadata."""
    blobs: list[GenBlob]
    meta: dict = field(default_factory=dict)


@dataclass
class AdapterContext:
    """App services injected into every adapter. Keeps adapters import-cycle-free
    and hot-reload-safe: `log_enabled` is a callable so the live flag value is
    read per-call, never cached."""
    auth_headers: Callable[[dict], dict]
    inflight_inc: Callable[[str], None]
    inflight_dec: Callable[[str], None]
    cost_usd: Callable[[str, Optional[str], int, int], float]
    source_of: Callable[[Request], str]
    record_call: Callable[..., Any]
    log_enabled: Callable[[], bool]
    # Live LLM-call registry (dashboard "running calls"). Same lifecycle as the
    # in-flight counter — register at dispatch start, drop on completion incl. the
    # streamed-finally. Default no-ops so non-main constructions stay valid.
    active_register: Callable[[dict], Any] = lambda meta: None
    active_done: Callable[[Any], None] = lambda token: None
    # Normalized reasoning toggle: (backend, model, requested, payload) -> (payload, control).
    # Default no-op (auto) so non-main constructions stay valid.
    apply_reasoning: Callable[[dict, Optional[str], Optional[str], dict], Any] = \
        lambda backend, model, requested, payload: (payload, None)
    # App-shared pooled httpx client (connection reuse / keep-alive across requests).
    # Callable so hot paths always see the live instance; None → adapters fall back
    # to a transient per-call client, keeping non-main constructions valid.
    http_client: Callable[[], Optional[httpx.AsyncClient]] = lambda: None
    # Installed LoRAs of a backend (discovery) — the `loras:[…]` pair resolution
    # validates against this. Default empty = no validation (non-main constructions).
    loras_of: Callable[[str], set] = lambda bid: set()


@asynccontextmanager
async def _pooled_client(ctx: AdapterContext):
    """The app-shared pooled client when injected (NOT closed here — main owns its
    lifecycle), else a transient one closed on exit. The shared client carries no
    per-client timeout, so every call site passes its own `timeout=`."""
    shared = ctx.http_client()
    if shared is not None:
        yield shared
    else:
        async with httpx.AsyncClient() as client:
            yield client


class BackendAdapter(ABC):
    """One instance per configured backend. Owns protocol-specific discovery and
    dispatch; the router (main.py) stays protocol-agnostic."""

    type: str = "base"

    def __init__(self, backend: dict, ctx: AdapterContext):
        self.backend = backend
        self.ctx = ctx
        self.name = backend["name"]                 # display label (stats / logs)
        # stable id = type:name — keys in-flight + pricing so an LLM and a ComfyUI
        # backend can share a name without colliding.
        self.bid = f'{backend.get("type", "openai")}:{backend["name"]}'

    @abstractmethod
    async def discover(self, client: httpx.AsyncClient) -> Capabilities:
        """Poll liveness + capabilities. Raise on failure → caller marks DOWN."""
        raise NotImplementedError

    async def dispatch(self, req: NormalizedRequest):
        """Forward an OpenAI-shaped (chat/completions/embeddings) request and
        return a Starlette Response. Owns the in-flight counter lifecycle —
        critically including decrementing when a *streamed* response finishes (in
        the generator's `finally`), not when headers are sent. May raise
        httpx.ConnectError/TimeoutException for the router to fail over.

        Default: unsupported (e.g. a pure image backend has no chat path)."""
        raise NotImplementedError(f"{self.type} adapter has no chat dispatch")

    async def generate(self, req: NormalizedRequest) -> GenOutput:
        """Run a generation task (image/video/audio) and return its artifacts.
        Owns the in-flight counter lifecycle. Default: unsupported."""
        raise NotImplementedError(f"{self.type} adapter has no generate path")


# ── OpenAI-compatible adapter (the only one in Phase 0) ───────────────────────

def _estimate_prompt_tokens(fwd: dict) -> int:
    """~chars/4 over the request's text parts — the fallback when a backend
    streams its usage block as all zeros (LocalAI does; measured 2026-07-09,
    non-streaming reports real numbers). Image parts are skipped: a base64
    image would explode a char-based estimate."""
    texts = []
    if isinstance(fwd.get("prompt"), str):
        texts.append(fwd["prompt"])
    for m in fwd.get("messages") or []:
        c = m.get("content") if isinstance(m, dict) else None
        if isinstance(c, str):
            texts.append(c)
        elif isinstance(c, list):
            texts.extend(p["text"] for p in c
                         if isinstance(p, dict) and isinstance(p.get("text"), str))
    chars = sum(len(t) for t in texts)
    return (chars // 4 + 1) if chars else 0


class _StreamNormalizer:
    """Rewrites a backend's chat SSE into strict OpenAI shape. Strict clients
    (Hermes) abort mid-stream on the deviations LocalAI ships, so:

    - `delta` never carries null-valued keys (LocalAI puts `content:null` on
      the role/finish chunks) or an empty-string `content` — the keys are
      dropped; the finish chunk becomes the spec's `"delta":{}`.
    - the terminal usage chunk (`"choices":[]`) reaches the client only if it
      asked via `stream_options.include_usage` (WE always ask upstream, for
      stats); without the flag a stray `usage` key on delta chunks is dropped
      too. A client that did ask gets the chunk exactly once, directly before
      [DONE] — synthesized from the last chunk's id/model if the backend never
      sent one.
    - usage numbers are the backend's; if it only ever reported zeros, the
      fallback is counted content/reasoning deltas (llama.cpp-family streams
      ≈ one token per delta) + the caller's prompt estimate.

    Non-`data:` lines, [DONE], unparseable JSON and non-chunk payloads pass
    through verbatim; buffering is bytes-based so multi-byte UTF-8 split
    across network chunks survives. `flush()` returns the unterminated tail
    (error bodies aren't newline-framed)."""

    def __init__(self, want_usage: bool, prompt_estimate: int):
        self.want_usage = want_usage
        self.prompt_est = prompt_estimate
        self.buf = b""
        self.in_tok = self.out_tok = 0      # best backend-reported usage so far
        self.pieces = 0                     # content-bearing deltas ≈ completion tokens
        self.meta: dict = {}                # id/model/created of the last chunk seen
        self.usage_sent = False

    def feed(self, chunk: bytes) -> bytes:
        self.buf += chunk
        out = []
        while b"\n" in self.buf:
            raw, self.buf = self.buf.split(b"\n", 1)
            out.append(self._line(raw.decode("utf-8", "ignore")))
        return "".join(out).encode()

    def flush(self) -> bytes:
        tail, self.buf = self.buf, b""
        return tail

    def tokens(self) -> tuple:
        """(prompt, completion) for stats + the client chunk — backend numbers,
        zeros replaced by the estimates."""
        return (self.in_tok or self.prompt_est, self.out_tok or self.pieces)

    def _usage_json(self) -> str:
        it, ot = self.tokens()
        return json.dumps({**self.meta, "object": "chat.completion.chunk",
                           "choices": [],
                           "usage": {"prompt_tokens": it, "completion_tokens": ot,
                                     "total_tokens": it + ot}},
                          ensure_ascii=False, separators=(",", ":"))

    def _line(self, line: str) -> str:
        data = line.strip()
        if not data.startswith("data:"):
            return line + "\n"
        payload = data[5:].strip()
        if payload == "[DONE]":
            if self.want_usage and not self.usage_sent:      # backend never sent one
                self.usage_sent = True
                return f"data: {self._usage_json()}\n\ndata: [DONE]\n"
            return line + "\n"
        try:
            obj = json.loads(payload)
        except Exception:
            return line + "\n"
        if not isinstance(obj, dict) or "choices" not in obj:
            return line + "\n"
        for k in ("id", "model", "created", "system_fingerprint"):
            if k in obj and obj[k] is not None:
                self.meta[k] = obj[k]
        u = obj.get("usage")
        if isinstance(u, dict):
            self.in_tok = max(self.in_tok, int(u.get("prompt_tokens") or 0))
            self.out_tok = max(self.out_tok, int(u.get("completion_tokens") or 0))
        if not obj.get("choices"):                            # terminal usage chunk
            if u is None:
                return line + "\n"                            # odd keepalive — leave it
            if self.want_usage and not self.usage_sent:
                self.usage_sent = True
                return f"data: {self._usage_json()}\n"        # own blank line follows
            return ""                                         # client didn't ask → swallow
        changed = False
        for ch in obj["choices"]:
            d = ch.get("delta") if isinstance(ch, dict) else None
            if isinstance(d, dict):
                for k in [k for k, v in d.items()
                          if v is None or (k == "content" and v == "")]:
                    del d[k]
                    changed = True
                if any(isinstance(d.get(k), str) and d[k]
                       for k in ("content", "reasoning", "reasoning_content")):
                    self.pieces += 1
            if isinstance(ch, dict) and isinstance(ch.get("text"), str) and ch["text"]:
                self.pieces += 1                              # legacy /v1/completions
        if "usage" in obj and not self.want_usage:            # no flag → no usage anywhere
            del obj["usage"]
            changed = True
        if not changed:
            return line + "\n"
        return "data: " + json.dumps(obj, ensure_ascii=False,
                                     separators=(",", ":")) + "\n"


@dataclass
class _Call:
    """Per-dispatch bookkeeping shared by the stream and non-stream paths —
    built once in OpenAIAdapter._prepare()."""
    url: str
    headers: dict                       # outgoing headers (client's, minus hop-by-hop, plus backend auth)
    fwd: dict                           # outgoing payload (private keys stripped, reasoning applied)
    real_model: Optional[str]
    reasoning_ctl: Optional[str]        # x-reasoning-control value ("off:prefill", …) or None
    rheaders: dict                      # gateway response headers (serving backend + reasoning control)
    source: str
    req_text: str
    started: float
    log_on: bool
    act: Any                            # live-call registry token


class OpenAIAdapter(BackendAdapter):
    """llama.cpp / llama-swap / vLLM / Ollama / Together / OpenRouter / OpenAI —
    anything that speaks `/v1/models` + `/v1/chat|completions|embeddings`."""

    type = "openai"

    async def discover(self, client: httpx.AsyncClient) -> Capabilities:
        b = self.backend
        resp = await client.get(
            f"{b['url']}/v1/models", headers=self.ctx.auth_headers(b), timeout=_DISCOVERY_TIMEOUT
        )
        resp.raise_for_status()
        payload = resp.json()
        return Capabilities(models=extract_models(payload, b),
                            pricing=extract_pricing(payload))

    async def dispatch(self, req: NormalizedRequest):
        call = self._prepare(req)                 # claims the in-flight slot
        if req.body.get("stream"):
            return await self._dispatch_stream(req, call)
        return await self._dispatch_once(req, call)

    def _prepare(self, req: NormalizedRequest) -> _Call:
        """Shared per-dispatch setup: outgoing headers + payload (gateway-private
        `_` keys stripped, normalized reasoning applied for THIS backend — a
        per-backend, failover-safe copy), stats fields, live-call registry entry.
        Claims the in-flight slot LAST (after anything that could raise), with no
        await before dispatch uses it — the dispatching path's `finally` MUST
        release it via _finish()."""
        b = self.backend
        ctx = self.ctx
        headers = {
            k: v for k, v in req.raw.headers.items()
            if k.lower() not in ("host", "content-length", "authorization", "x-park-mode")
        }
        headers.update(ctx.auth_headers(b))
        real_model = req.body.get("model")
        fwd = {k: v for k, v in req.body.items() if not k.startswith("_")}
        fwd, reasoning_ctl = ctx.apply_reasoning(b, real_model, req.reasoning, fwd)
        ctx.inflight_inc(self.bid)        # released on completion (stream close / return / error)
        act = ctx.active_register({       # dropped on completion (both finally paths)
            "alias": req.alias, "model": real_model, "backend": self.name,
            "source": ctx.source_of(req.raw), "endpoint": (req.stats_endpoint or req.path),
            "stream": bool(req.body.get("stream")),
        })
        # Response headers: which backend served the call (so API clients — incl. the
        # /ui playgrounds, which are plain clients — can show routing), plus the
        # reasoning control actually applied.
        rheaders = {"x-gateway-backend": self.name}
        if reasoning_ctl:
            rheaders["x-reasoning-control"] = reasoning_ctl
        return _Call(
            url=f"{b['url']}{req.path}", headers=headers, fwd=fwd, real_model=real_model,
            reasoning_ctl=reasoning_ctl, rheaders=rheaders,
            source=ctx.source_of(req.raw), req_text=json.dumps(fwd, ensure_ascii=False),
            started=time.monotonic(), log_on=ctx.log_enabled(), act=act,
        )

    def _finish(self, call: _Call) -> None:
        """Release the in-flight slot + live-registry entry (every path's finally)."""
        self.ctx.inflight_dec(self.bid)
        self.ctx.active_done(call.act)

    def _record(self, req: NormalizedRequest, call: _Call, status: int,
                in_tok: int, out_tok: int, response_text: Optional[str] = None,
                response_audio: Optional[tuple] = None) -> int:
        """Fire-and-forget stats row for this dispatch; returns the elapsed ms."""
        ctx = self.ctx
        elapsed_ms = int((time.monotonic() - call.started) * 1000)
        asyncio.create_task(ctx.record_call(
            duration_ms=elapsed_ms, backend=self.name, source=call.source,
            alias=req.alias, model=call.real_model, endpoint=(req.stats_endpoint or req.path),
            status=status, input_tokens=in_tok, output_tokens=out_tok,
            cost_usd=ctx.cost_usd(self.bid, call.real_model, in_tok, out_tok),
            request_text=call.req_text, response_text=response_text,
            response_audio=response_audio,
            reasoning=call.reasoning_ctl,
        ))
        return elapsed_ms

    async def _dispatch_stream(self, req: NormalizedRequest, call: _Call) -> Response:
        ctx = self.ctx
        # Ask the backend to emit a final usage chunk so streamed calls record
        # real tokens/cost (graceful: backends that ignore the field just yield
        # no usage line → the normalizer's estimates kick in). The client only
        # sees that chunk if IT asked (call.fwd still holds its stream_options).
        body = {**call.fwd,
                "stream_options": {**(call.fwd.get("stream_options") or {}), "include_usage": True}}
        want_usage = bool((call.fwd.get("stream_options") or {}).get("include_usage"))
        norm = _StreamNormalizer(want_usage, _estimate_prompt_tokens(call.fwd))

        # Open the upstream stream BEFORE building the client response: status +
        # headers arrive first, so a backend that answers with an error (llama-swap
        # "unable to start process" 502 when the model can't load) returns a plain
        # Response with its REAL status — previously the SSE shell went out as
        # HTTP 200 before upstream ever replied — and _dispatch_over can fail over
        # on it. Connect/timeout raise into _dispatch_over's failover exactly like
        # the non-stream path; every error exit releases the in-flight slot.
        client_cm = _pooled_client(ctx)
        client = None
        try:
            client = await client_cm.__aenter__()
            stream_cm = client.stream("POST", call.url, json=body,
                                      headers=call.headers, timeout=_CHAT_TIMEOUT)
            resp = await stream_cm.__aenter__()
        except BaseException:
            if client is not None:
                await client_cm.__aexit__(None, None, None)
            self._finish(call)
            raise
        if call.log_on:
            logger.info(f"← [{self.name}] {req.path} HTTP {resp.status_code} "
                        f"(stream open, {(time.monotonic() - call.started):.2f}s)")
        if resp.status_code >= 400:               # error body, not SSE — real status through
            try:
                err = await resp.aread()
            finally:
                await stream_cm.__aexit__(None, None, None)
                await client_cm.__aexit__(None, None, None)
                self._finish(call)
            self._record(req, call, resp.status_code, 0, 0,
                         response_text=err.decode("utf-8", "ignore"))
            return Response(content=err, status_code=resp.status_code,
                            media_type=resp.headers.get("content-type"),
                            headers=call.rheaders)

        async def generate():
            try:
                async for chunk in resp.aiter_bytes():
                    out = norm.feed(chunk)
                    if out:
                        yield out
                tail = norm.flush()
                if tail:
                    yield tail
            finally:
                await stream_cm.__aexit__(None, None, None)
                await client_cm.__aexit__(None, None, None)
                self._finish(call)
            in_tok, out_tok = norm.tokens()
            self._record(req, call, resp.status_code, in_tok, out_tok)

        return StreamingResponse(generate(), media_type="text/event-stream", headers=call.rheaders)

    async def _dispatch_once(self, req: NormalizedRequest, call: _Call) -> Response:
        try:
            async with _pooled_client(self.ctx) as client:
                resp = await client.post(call.url, json=call.fwd,
                                         headers=call.headers, timeout=_CHAT_TIMEOUT)
        finally:
            self._finish(call)
        # Parse ONCE (usage → tokens/cost); the body itself passes through as the
        # backend's raw bytes — the gateway doesn't transform it, so the old
        # parse → re-serialize (stats) → re-serialize (JSONResponse) round-trip
        # is gone. Internal callers (Responses bridge) still read resp.body.
        # Binary bodies (e.g. /v1/audio/speech WAV) are neither parsed nor stored
        # in the stats blob — charset-decoding audio bytes is garbage + waste.
        ct = resp.headers.get("content-type", "")
        is_texty = ct.startswith(("application/json", "text/"))
        resp_json = {}
        if is_texty:
            try:
                resp_json = resp.json()
            except Exception:
                pass
        usage = (resp_json.get("usage") or {}) if isinstance(resp_json, dict) else {}
        in_tok = int(usage.get("prompt_tokens") or 0)
        out_tok = int(usage.get("completion_tokens") or 0)
        # Successful binary audio (TTS) → stored as its own stats blob so the call
        # view can play it back; other binary bodies stay unstored.
        resp_audio = ((resp.content, ct) if ct.startswith("audio/") and resp.status_code == 200
                      else None)
        elapsed_ms = self._record(req, call, resp.status_code, in_tok, out_tok,
                                  response_text=(resp.text if is_texty else None),
                                  response_audio=resp_audio)
        if call.log_on:
            logger.info(f"← [{self.name}] {req.path} HTTP {resp.status_code} ({elapsed_ms} ms)")
        out = Response(resp.content, status_code=resp.status_code,
                       media_type=resp.headers.get("content-type", "application/json"),
                       headers=call.rheaders)
        out.parsed_json = resp_json    # internal callers (Responses bridge) reuse this — no re-parse
        return out


# ── ComfyUI adapter ───────────────────────────────────────────────────────────
# A focused async port of anima-verse's ComfyUIBackend (text2img path). Workflows
# are stored API-format JSON whose injectable knobs are *named Primitive nodes*
# (by `_meta.title`) with class-based fallbacks. Dispatch is submit → poll → view.

_MIME_BY_EXT = {
    ".png": ("image/png", "image"), ".jpg": ("image/jpeg", "image"),
    ".jpeg": ("image/jpeg", "image"), ".webp": ("image/webp", "image"),
    ".gif": ("image/gif", "image"), ".mp4": ("video/mp4", "video"),
    ".webm": ("video/webm", "video"), ".wav": ("audio/wav", "audio"),
    ".mp3": ("audio/mpeg", "audio"), ".flac": ("audio/flac", "audio"),
    # 3D artifacts (Trellis/rigging workflows) — kind "file" renders as download
    ".glb": ("model/gltf-binary", "file"), ".gltf": ("model/gltf+json", "file"),
    ".obj": ("model/obj", "file"), ".fbx": ("application/octet-stream", "file"),
    ".ply": ("application/octet-stream", "file"), ".stl": ("model/stl", "file"),
}


def _mime_and_kind(filename: str) -> tuple[str, str]:
    return _MIME_BY_EXT.get(os.path.splitext(filename)[1].lower(),
                            ("application/octet-stream", "image"))


def _image_dims(b: bytes) -> Optional[tuple]:
    """(width, height) from a PNG or baseline-JPEG header, else None. Enough to
    catch the 2x2 dummy — no image library needed."""
    if b[:8] == b"\x89PNG\r\n\x1a\n" and len(b) >= 24:
        w, h = struct.unpack(">II", b[16:24])
        return int(w), int(h)
    if b[:2] == b"\xff\xd8":                          # JPEG: scan for a SOF marker
        i = 2
        while i + 9 < len(b):
            if b[i] != 0xFF:
                i += 1
                continue
            marker = b[i + 1]
            if marker in (0xC0, 0xC1, 0xC2, 0xC3):
                h, w = struct.unpack(">HH", b[i + 5:i + 9])
                return int(w), int(h)
            if marker in (0xD8, 0xD9) or 0xD0 <= marker <= 0xD7:
                i += 2
                continue
            i += 2 + struct.unpack(">H", b[i + 2:i + 4])[0]
    return None


def _glb_info(data: bytes) -> Optional[dict]:
    """Parse a GLB in pure Python (JSON + BIN chunks): `{texture_dims:[(w,h),…],
    mixamorig_joints:int, skins:int, meshes:int}`, or None if `data` isn't a
    parseable glTF-binary. Backs the delivery validation (2x2 dummy, 52-joint
    mixamorig skin, embedded texture present)."""
    try:
        if data[:4] != b"glTF" or len(data) < 20:
            return None
        off, json_chunk, bin_chunk = 12, None, None
        while off + 8 <= len(data):
            clen, ctype = struct.unpack_from("<I4s", data, off)
            body = data[off + 8: off + 8 + clen]
            if ctype == b"JSON":
                json_chunk = body
            elif ctype == b"BIN\x00":
                bin_chunk = body
            off += 8 + clen                          # chunkLength already includes padding
        if json_chunk is None:
            return None
        gltf = json.loads(json_chunk)
        bufviews = gltf.get("bufferViews") or []
        dims = []
        for im in (gltf.get("images") or []):
            bv_i = im.get("bufferView")
            if bv_i is None or bin_chunk is None or bv_i >= len(bufviews):
                continue
            bv = bufviews[bv_i]
            start = bv.get("byteOffset", 0)
            head = bin_chunk[start: start + min(bv.get("byteLength", 0), 4096)]
            wh = _image_dims(head)
            if wh:
                dims.append(wh)
        joints = sum(1 for n in (gltf.get("nodes") or [])
                     if str(n.get("name", "")).lower().startswith("mixamorig"))
        return {"texture_dims": dims, "mixamorig_joints": joints,
                "skins": len(gltf.get("skins") or []), "meshes": len(gltf.get("meshes") or [])}
    except Exception:
        return None


def _glb_texture_dims(data: bytes) -> Optional[list]:
    """Embedded-texture dimensions of a GLB (thin wrapper over _glb_info)."""
    info = _glb_info(data)
    return info["texture_dims"] if info is not None else None


def _check_glb_not_dummy(blobs) -> None:
    """Safety net for the flat/single-file modes: a GLB whose ONLY embedded texture
    is the 2x2 dummy is the known texture-export node bug, not a result — fail the
    job clearly (raises → final content error, never retried across backends).
    Case mode runs the fuller _validate_delivery instead."""
    for b in blobs:
        if b.mime != "model/gltf-binary":
            continue
        dims = _glb_texture_dims(b.data)
        if dims and max(max(w, h) for w, h in dims) <= 2:
            w, h = dims[0]
            raise RuntimeError(f"GLB '{b.name or 'output'}' carries only a {w}x{h} dummy texture "
                               "(known texture-export node bug) — no real texture was embedded; "
                               "treated as a failed generation")


_SIZE_LIMIT_MB = 30                                  # web-suitability guideline (warn, not fail)


def normalize_delivery(blobs: list, rig: Optional[str],
                       texture_format: Optional[str] = None) -> None:
    """Fix known mesh-pipeline artifacts IN PLACE, before validation.

    generic (FBX + separate texture PNGs): the bake writes the textures
    V-flipped relative to the FBX UVs — a native consumer (the /ui preview,
    the 3D client, a DCC import) sees the mapping mirrored; only GL-style
    loaders that flip images on upload happen to render it right. The gateway
    is the ONE place that corrects this, so every consumer renders the pair
    as delivered, without client-side compensation. All PNGs of a generic
    delivery share the FBX's UV set (basecolor, metallic, …), so all are
    flipped. A blob flag guards against double application (the adapter
    normalizes case deliveries, the chain level normalizes the combined one).
    A failed normalize delivers the original bytes instead of failing the job.

    `texture_format="jpeg"` (alias Output option) additionally transcodes the
    flipped texture PNGs to JPEG (quality 90) in the same decode pass —
    ComfyUI has no JPEG export, so the gateway is the one place to shrink a
    multi-MB bake for delivery. A texture with a REAL alpha channel keeps PNG
    (JPEG has none). The FBX references its texture by a temp path anyway
    (clients re-bind by delivered name), so the extension change is safe.
    """
    if rig != "generic":
        return
    for b in blobs:
        if getattr(b, "_normalized", False):
            continue
        if not (b.name or "").lower().endswith(".png"):
            continue
        try:
            import io
            from PIL import Image
            img = Image.open(io.BytesIO(b.data)).transpose(Image.FLIP_TOP_BOTTOM)
            fmt = "PNG"
            if (texture_format or "").lower() in ("jpg", "jpeg"):
                if img.mode == "P" and "transparency" in img.info:
                    img = img.convert("RGBA")
                translucent = ("A" in img.getbands()
                               and img.getchannel("A").getextrema()[0] < 255)
                if not translucent:
                    img = img.convert("RGB")
                    fmt = "JPEG"
            buf = io.BytesIO()
            img.save(buf, format=fmt, **({"quality": 90} if fmt == "JPEG" else {}))
            b.data = buf.getvalue()
            if fmt == "JPEG":
                b.name = b.name[:-len(".png")] + ".jpg"
                b.mime = "image/jpeg"
            b._normalized = True
        except Exception as e:
            logger.warning(f"normalize_delivery: texture normalize failed for "
                           f"'{b.name}' — delivering as produced: {e}")


def validate_delivery(blobs: list, rig: Optional[str]) -> list:
    """Gate a case-mode delivery before reporting success (returns warnings; raises
    on hard failure). Per the character-model spec:
    - mixamo (humanoid): a valid GLB with a mixamorig skin and a real embedded
      texture; a 2x2 texture is the known node bug → fail.
    - generic (non-humanoid): a rigged FBX AND its basecolor PNG (the FBX only
      references the texture by a temp path, so the PNG must ship with it).
    - both: files over ~30 MB warn (web-suitability guideline, not a hard fail).
    Orientation/scale are the client's to normalise — not checked."""
    warnings = []
    for b in blobs:
        mb = len(b.data) / (1024 * 1024)
        if mb > _SIZE_LIMIT_MB:
            warnings.append(f"{b.name} is {mb:.0f} MB (> {_SIZE_LIMIT_MB} MB guideline)")
    if rig == "mixamo":
        glb = next((b for b in blobs if b.mime == "model/gltf-binary"), None)
        if glb is None:
            raise RuntimeError("humanoid (mixamo) result: no GLB was delivered")
        info = _glb_info(glb.data)
        if info is None:
            raise RuntimeError(f"humanoid result '{glb.name}': not a valid GLB")
        if info["skins"] == 0 or info["mixamorig_joints"] == 0:
            raise RuntimeError(f"humanoid result '{glb.name}': no mixamorig skin "
                               f"(skins={info['skins']}, mixamorig joints={info['mixamorig_joints']})")
        dims = info["texture_dims"]
        if not dims:
            raise RuntimeError(f"humanoid result '{glb.name}': no embedded texture")
        if max(max(w, h) for w, h in dims) <= 2:
            raise RuntimeError(f"humanoid result '{glb.name}': embedded texture is a 2x2 dummy "
                               "(known node bug) — no result")
        if info["mixamorig_joints"] < 50:
            warnings.append(f"{glb.name}: {info['mixamorig_joints']} mixamorig joints (expected ~52)")
    elif rig == "generic":
        fbx = next((b for b in blobs if (b.name or "").lower().endswith(".fbx")), None)
        png = next((b for b in blobs if (b.mime or "").startswith("image/")), None)
        if fbx is None:
            raise RuntimeError("non-humanoid (generic) result: no rigged FBX was delivered")
        if not (fbx.data[:23].find(b"Kaydara FBX") != -1 or fbx.data[:4] == b"; FB"):
            warnings.append(f"{fbx.name}: does not look like an FBX (magic missing)")
        if png is None:
            raise RuntimeError("non-humanoid (generic) result: no basecolor PNG — the FBX only "
                               "references its texture by a temp path, so the PNG must ship with it")
    return warnings


def _view_params(item) -> Optional[tuple]:
    """Map ONE item from a ComfyUI output list to (filename, /view params), or
    None when it isn't a fetchable file. Two shapes seen in the wild:

    - dict `{filename, subfolder?, type?}` — core save nodes (SaveImage/Video/…).
    - str absolute/relative PATH — custom nodes emit the on-disk path instead
      (UniRig's `fbx_file`: '/…/ComfyUI/output/x_mia.fbx'; Preview3D's `result`).
      ComfyUI still serves it via /view by basename; the dir before an
      output/temp/input segment becomes the subfolder + type. Only paths with a
      known artifact extension are taken, so booleans/status strings are ignored."""
    if isinstance(item, dict):
        fn = item.get("filename")
        if not fn:
            return None
        view = {"filename": fn, "type": item.get("type", "output")}
        if item.get("subfolder"):
            view["subfolder"] = item["subfolder"]
        return fn, view
    if isinstance(item, str) and item:
        p = item.replace("\\", "/")
        if os.path.splitext(p)[1].lower() not in _MIME_BY_EXT:   # not a recognised artifact
            return None
        # Default when no output/temp/input segment names the dir type: an absolute
        # path is reduced to its basename (best guess — /view can't take its dirs),
        # but a RELATIVE path is already output-relative, so its dirs are the subfolder.
        typ = "output"
        rel = p.rsplit("/", 1)[-1] if (p.startswith("/") or ":" in p.split("/", 1)[0]) else p
        for t in ("output", "temp", "input"):
            i = p.rfind(f"/{t}/")
            if i != -1:
                typ, rel = t, p[i + len(t) + 2:]
                break
        fn = rel.rsplit("/", 1)[-1]
        sub = rel[:len(rel) - len(fn)].strip("/")
        view = {"filename": fn, "type": typ}
        if sub:
            view["subfolder"] = sub
        return fn, view
    return None


def _comfy_prompt_error(status: int, text: str, wf: dict, mapping: dict) -> str:
    """Human-readable ComfyUI /prompt rejection. Raw node_errors name only node ids —
    meaningless in the console, where everything is wired by node TITLE. Each error
    line therefore carries the node's title, class, field, the REQUEST PARAM mapped
    onto that field (the thing the user actually typed), and the received value.
    Falls back to the raw body when the payload shape is unknown."""
    try:
        err = json.loads(text or "")
        node_errors = err.get("node_errors") or {}
    except (ValueError, AttributeError):
        return f"ComfyUI /prompt HTTP {status}: {text}"
    lines = []
    for nid, ne in node_errors.items():
        node = (wf or {}).get(str(nid)) or {}
        title = (node.get("_meta") or {}).get("title") or ""
        cls = ne.get("class_type") or node.get("class_type") or "?"
        where = f"node {nid} “{title}”" if title else f"node {nid}"
        for e in ne.get("errors") or []:
            extra = e.get("extra_info") or {}
            fldn = extra.get("input_name") or ""
            param = next((p for p, m in (mapping or {}).items()
                          if str((m or {}).get("node")) == str(nid) and (m or {}).get("field") == fldn), None)
            via = f" ← request param '{param}'" if param else ""
            recv = extra.get("received_value")
            recv_s = f" (received {recv!r})" if recv is not None else ""
            lines.append(f"{where} ({cls}).{fldn}{via}: {e.get('message', 'error')}{recv_s}")
    if not lines:
        msg = (err.get("error") or {}).get("message") or text
        return f"ComfyUI /prompt HTTP {status}: {msg}"
    head = (err.get("error") or {}).get("message") or "prompt rejected"
    return f"ComfyUI {head} (HTTP {status}): " + " · ".join(lines)


def _node_by_title(wf: dict, title: str) -> Optional[str]:
    t = title.lower()
    for nid, node in wf.items():
        if node.get("_meta", {}).get("title", "").lower() == t:
            return nid
    return None


def _node_by_class(wf: dict, class_type: str) -> Optional[str]:
    for nid, node in wf.items():
        if node.get("class_type") == class_type:
            return nid
    return None


def _clip_nodes(wf: dict, want: str) -> Optional[str]:
    """Positive/negative CLIPTextEncode by title hint, falling back to order
    (first = positive, second = negative)."""
    found = []
    for nid, node in wf.items():
        if node.get("class_type") == "CLIPTextEncode":
            title = node.get("_meta", {}).get("title", "").lower()
            if want == "positive" and ("positive" in title or "prompt" in title):
                return nid
            if want == "negative" and "negative" in title:
                return nid
            found.append(nid)
    if want == "positive":
        return found[0] if found else None
    return found[1] if len(found) > 1 else None


_IMG_LOADER_CLASSES = ("LoadImage", "LoadAndResizeImage", "LoadImageMask")
PLACEHOLDER_SENTINEL = "__gw_placeholder__"          # fixed-binding value → upload + use an 8×8 image
UPLOAD_SENTINEL = "__gw_upload__"                    # fixed-binding value → use the playground upload


def is_img_loader_class(cls) -> bool:
    """Image-loader detection: the known core classes plus any class whose name
    carries 'LoadImage' — custom node packs bring their own loaders (Trellis2's
    Trellis2LoadImageWithTransparency) that must behave like LoadImage: upload
    slot in playground/mapping, placeholder/required empty handling."""
    return cls in _IMG_LOADER_CLASSES or "loadimage" in str(cls or "").lower()


def is_image_field(wf: dict, node: str) -> bool:
    """True if a mapped node is an image-loader (its request field takes an uploaded
    image, not a scalar). Drives the per-field file inputs in the playground."""
    return is_img_loader_class((wf or {}).get(node, {}).get("class_type"))


def image_params(wf: dict, mapping: dict) -> list:
    """Request params whose target node is an image loader → rendered as uploads and
    filled per-field (uploaded file, else an 8×8 placeholder)."""
    return [p for p, m in (mapping or {}).items() if is_image_field(wf, (m or {}).get("node"))]


def slot_empty_mode(m: dict) -> str:
    """What a mapped image slot does when the request sends no image for it:
    'placeholder' (8×8 black, the default) · 'required' (no fallback → ComfyUI errors
    if it is missing) · 'disable' (drop the loader node and any link to it, so an
    OPTIONAL downstream input runs without this image). Reads the `on_empty` field,
    falling back to the legacy `no_placeholder` boolean."""
    mode = (m or {}).get("on_empty")
    if mode in ("placeholder", "required", "disable"):
        return mode
    return "required" if (m or {}).get("no_placeholder") else "placeholder"


def _prune_node(wf: dict, nid: str) -> None:
    """Deactivate a node: remove it from the prompt and drop any input link that points
    at its output (`[nid, slot]`). The consuming node must accept the now-missing input
    (an optional socket), else ComfyUI errors — that contract is on the workflow."""
    if wf.pop(nid, None) is None:
        return
    for n in wf.values():
        inp = n.get("inputs")
        if isinstance(inp, dict):
            for fld in [k for k, v in inp.items()
                        if isinstance(v, list) and v and str(v[0]) == str(nid)]:
                inp.pop(fld, None)


def _img_slug(s: str) -> str:
    """A safe, stable per-param input filename so each image field reuses one slot."""
    keep = "".join(ch if ch.isalnum() else "_" for ch in (s or "img"))
    return f"gw_{keep[:40]}.png"


def _placeholder_png(size: int = 8) -> bytes:
    """A tiny valid gray PNG, so an admin can pin an unused reference-image node to
    a real (but empty) image instead of letting ComfyUI choke on an empty input."""
    def chunk(tag: bytes, data: bytes) -> bytes:
        body = tag + data
        return struct.pack(">I", len(data)) + body + struct.pack(">I", zlib.crc32(body) & 0xFFFFFFFF)
    raw = b"".join(b"\x00" + b"\x7f\x7f\x7f" * size for _ in range(size))   # filter byte + RGB row
    return (b"\x89PNG\r\n\x1a\n"
            + chunk(b"IHDR", struct.pack(">IIBBBBB", size, size, 8, 2, 0, 0, 0))
            + chunk(b"IDAT", zlib.compress(raw))
            + chunk(b"IEND", b""))


_PLACEHOLDER_PNG = _placeholder_png(8)


def _coerce(value, current):
    """Coerce a string form value to the type of the field it overrides, so a
    pinned Switch boolean / int stays the right JSON type for ComfyUI."""
    if isinstance(current, bool):
        return str(value).strip().lower() in ("true", "1", "yes", "on")
    if isinstance(current, int) and not isinstance(current, bool):
        try:
            return int(value)
        except (TypeError, ValueError):
            return value
    if isinstance(current, float):
        try:
            return float(value)
        except (TypeError, ValueError):
            return value
    return value


def _apply_fixed(wf: dict, fixed: list) -> dict:
    """Apply admin-pinned constant bindings: each `{node, field, value}` sets
    `workflow[node].inputs[field] = value` (type-coerced to the field's current
    type). This is the general mechanism for pre-setting nodes the admin chose —
    model/clip/vae loader slots, a Switch's boolean, a reference image, etc. — so
    a registered workflow runs without the gateway guessing."""
    applied = {}
    for b in fixed or []:
        node, fieldn, value = b.get("node"), b.get("field"), b.get("value")
        if node in wf and fieldn and value is not None and value != "":
            inputs = wf[node].setdefault("inputs", {})
            inputs[fieldn] = _coerce(value, inputs.get(fieldn))
            applied[f"{node}.{fieldn}"] = inputs[fieldn]
    return applied


def _apply_mapping(wf: dict, mapping: dict, values: dict, protected: Optional[set] = None) -> dict:
    """The runtime injector: dumb and convention-free. For each logical param
    bound to `{node, field}`, set `workflow[node].inputs[field] = values[param]`.

    `mapping` is an explicit per-workflow binding table (authored in config or,
    later, in the registration UI). `values` is the merged logical params
    (prompt, negative_prompt, width, height, steps, cfg, seed, sampler, model, …
    plus any `params.extra`). Missing values are skipped. Returns what was set.

    `protected` is the set of admin-pinned `(node, field)` pairs: a request param
    that maps onto a pinned node/field is **ignored** — a pin is authoritative and
    the API must not override it."""
    protected = protected or set()
    applied = {}
    for param, binding in mapping.items():
        if values.get(param) is None:
            continue
        node, field = binding.get("node"), binding.get("field")
        if node in wf and field and (node, field) not in protected:
            wf[node].setdefault("inputs", {})[field] = values[param]
            applied[param] = values[param]
    return applied


def _format_comfy_error(messages) -> str:
    """Readable one-liner from ComfyUI /history error `messages` (a list of
    [type, data] pairs): the failing node + exception, else a trimmed raw fallback."""
    for m in (messages or []):
        if isinstance(m, (list, tuple)) and len(m) == 2 and m[0] == "execution_error":
            d = m[1] or {}
            where = " ".join(str(x) for x in (d.get("node_id"), d.get("node_type")) if x)
            msg = d.get("exception_message") or "execution error"
            return f"node {where}: {msg}" if where else msg
    try:
        return json.dumps(messages)[:600]
    except Exception:
        return str(messages)[:600]


def _prompt_in_queue(queue: dict, prompt_id: str) -> bool:
    """Is `prompt_id` still running or pending in a ComfyUI /queue response? Each entry
    is [number, prompt_id, prompt, extra, …]; the id sits at index 1."""
    for key in ("queue_running", "queue_pending"):
        for item in (queue.get(key) or []):
            if isinstance(item, (list, tuple)) and len(item) > 1 and item[1] == prompt_id:
                return True
    return False


_LORA_SLOT_RE = re.compile(r"^lora_0*(\d+)$")          # rgthree stack: lora_01..lora_NN
_STR_SLOT_RE = re.compile(r"^strength_0*(\d+)$")
# standalone high/low token in a LoRA filename — '-HIGH.', '_High_', '-low-' …
# but NOT substrings inside words ('Thigh Gap' carries no token).
_LORA_TOKEN_RE = re.compile(r"(?i)(?<![a-z0-9])(high|low)(?![a-z0-9])")


def lora_counterpart(name: str) -> Optional[str]:
    """The other half of a high/low LoRA pair: the standalone high/low token in
    the filename swapped case-preservingly (…-HIGH.safetensors ↔ …-LOW.…,
    …-High-i2v_e70 ↔ …-Low-i2v_e70). None when the name carries no token."""
    def swap(m):
        t = m.group(1)
        other = {"high": "low", "low": "high"}[t.lower()]
        return other.upper() if t.isupper() else other.capitalize() if t[0].isupper() else other
    new, n = _LORA_TOKEN_RE.subn(swap, name or "")
    return new if n else None


def _lora_kind(name: str) -> Optional[str]:
    m = _LORA_TOKEN_RE.search(name or "")
    return m.group(1).lower() if m else None


def lora_groups(wf: dict, mapping: dict) -> list:
    """The workflow's LoRA stack nodes as placement groups [(node_id, kind)].
    kind 'high'/'low'/None comes from the node title (Wan: input_lora_high/_low),
    else from a mapping label bound to that node ('lora1_high'), else None."""
    groups = []
    for nid, n in wf.items():
        inp = n.get("inputs") or {}
        if not any(_LORA_SLOT_RE.match(f) for f in inp):
            continue
        kind = _lora_kind(((n.get("_meta") or {}).get("title") or ""))
        if kind is None:
            for p, m in (mapping or {}).items():
                if str((m or {}).get("node")) == str(nid) and _LORA_SLOT_RE.match(str((m or {}).get("field") or "")):
                    kind = _lora_kind(((m or {}).get("label") or "")) or _lora_kind(p)
                    if kind:
                        break
        groups.append((nid, kind))
    return sorted(groups, key=lambda g: (len(g[0]), g[0]))


def _free_stack_slots(wf: dict, nid: str) -> list:
    """(lora_field, strength_field) pairs currently empty/'None' on one stack node."""
    inp = wf.get(nid, {}).get("inputs") or {}
    out = []
    for f in sorted((f for f in inp if _LORA_SLOT_RE.match(f)),
                    key=lambda f: int(_LORA_SLOT_RE.match(f).group(1))):
        if inp.get(f) in (None, "", "None"):
            idx = int(_LORA_SLOT_RE.match(f).group(1))
            sf = next((s for s in (f"strength_{idx:02d}", f"strength_{idx}") if s in inp), None)
            out.append((f, sf))
    return out


def _apply_lora_list(wf: dict, mapping: dict, loras: list, avail: set) -> list:
    """The `loras: [{name, strength}]` request shape: place each logical LoRA into
    EVERY stack group, resolving high/low pairs server-side — the client sends ONE
    name, the group whose kind differs gets the token-swapped counterpart (checked
    against the backend's installed set). Clients never learn the pairing. Raises
    RuntimeError (→ job fails with the message) when a name/counterpart is not
    installed or the stacks run out of free slots. Returns [(node, field, value)]."""
    if not loras:
        return []
    groups = lora_groups(wf, mapping)
    if not groups:
        raise RuntimeError("this workflow has no LoRA stack")
    free = {nid: _free_stack_slots(wf, nid) for nid, _ in groups}
    placed = []
    for entry in loras:
        if isinstance(entry, str):
            entry = {"name": entry}
        name = str((entry or {}).get("name") or "").strip()
        if not name or name == "None":
            continue
        strength = (entry or {}).get("strength", 1.0)
        base_kind = _lora_kind(name)
        counterpart = lora_counterpart(name)
        for nid, kind in groups:
            if kind is None or base_kind is None or kind == base_kind:
                val = name                       # kindless file goes into every group as-is
            else:
                val = counterpart
                if val is None or (avail and val not in avail):
                    raise RuntimeError(f"LoRA pair incomplete: '{name}' needs a {kind} "
                                       f"counterpart ('{counterpart or '?'}' is not installed)")
            if avail and val not in avail:
                raise RuntimeError(f"LoRA '{val}' is not installed on this backend")
            slots = free.get(nid) or []
            if not slots:
                raise RuntimeError(f"no free LoRA slot left on stack node {nid}")
            lf, sf = slots.pop(0)
            inp = wf[nid].setdefault("inputs", {})
            inp[lf] = val
            if sf:
                try:
                    inp[sf] = float(strength)
                except (TypeError, ValueError):
                    inp[sf] = 1.0
            placed.append((nid, lf, val))
    return placed


def _apply_lora_cascade(wf: dict, values: dict) -> list:
    """Place client-supplied LoRAs into the next FREE stack slots, never overwriting an
    occupied (pinned/baked) one.

    A client numbers its loras from 1 and cannot know which physical slot is reserved,
    so each client `lora_N` (with optional `strength_N`) is dropped onto the next slot
    whose current value is empty/'None'; occupied slots are skipped. Consumes the
    matched `lora_*`/`strength_*` keys from `values` so the normal mapping doesn't also
    place them at fixed slots. Returns [(node, field, value)] placed."""
    client, cstr = [], {}
    for k in [k for k in values if _LORA_SLOT_RE.match(k)]:
        idx, val = int(_LORA_SLOT_RE.match(k).group(1)), values.pop(k)
        if val is not None and str(val) not in ("", "None"):
            client.append((idx, val))
    for k in [k for k in values if _STR_SLOT_RE.match(k)]:
        cstr[int(_STR_SLOT_RE.match(k).group(1))] = values.pop(k)
    if not client:
        return []
    client.sort(key=lambda t: t[0])
    free = []                                          # (node, lora_field, strength_field) currently empty
    for nid, n in wf.items():
        inp = n.get("inputs") or {}
        for f in sorted((f for f in inp if _LORA_SLOT_RE.match(f)),
                        key=lambda f: int(_LORA_SLOT_RE.match(f).group(1))):
            if inp.get(f) in (None, "", "None"):
                idx = int(_LORA_SLOT_RE.match(f).group(1))
                sf = next((s for s in (f"strength_{idx:02d}", f"strength_{idx}") if s in inp), None)
                free.append((nid, f, sf))
    placed = []
    for (cidx, val), (nid, lf, sf) in zip(client, free):
        inp = wf[nid].setdefault("inputs", {})
        inp[lf] = val
        if sf:
            inp[sf] = cstr.get(cidx, 1.0)
        placed.append((nid, lf, val))
    return placed


def suggest_mapping(wf: dict) -> dict:
    """Convention-based *suggestion* of a {param: {node, field}} binding table —
    the seed for a "register workflow" UI to pre-fill, NOT the runtime mechanism.
    Also used as a zero-config fallback when a candidate omits an explicit mapping.

    Heuristics mirror common ComfyUI templates (named `input_*` Primitive nodes
    by `_meta.title`, with class-based fallbacks). Anything it gets wrong, the
    explicit mapping overrides."""
    m: dict = {}

    def _prompt_binding(titles, want):
        for title in titles:
            nid = _node_by_title(wf, title)
            if nid is not None:
                inputs = wf[nid].get("inputs", {})
                field = "value" if "value" in inputs else ("prompt" if "prompt" in inputs else "text")
                return {"node": nid, "field": field}
        nid = _clip_nodes(wf, want)
        return {"node": nid, "field": "text"} if nid else None

    if (b := _prompt_binding(["input - prompt - positive", "input_prompt_positiv"], "positive")):
        m["prompt"] = b
    if (b := _prompt_binding(["input - prompt - negative", "input_prompt_negativ"], "negative")):
        m["negative_prompt"] = b

    for which in ("width", "height"):
        nid = _node_by_title(wf, f"input_{which}")
        if nid is not None:
            m[which] = {"node": nid, "field": "value"}
        else:
            nid = _node_by_class(wf, "EmptyLatentImage")
            if nid is not None:
                m[which] = {"node": nid, "field": which}

    seed_node = _node_by_title(wf, "input_seed")
    if seed_node is not None:
        m["seed"] = {"node": seed_node, "field": "value"}
    elif (ks := _node_by_class(wf, "KSampler")) is not None:
        m["seed"] = {"node": ks, "field": "seed"}
    elif (rn := _node_by_class(wf, "RandomNoise")) is not None:
        m["seed"] = {"node": rn, "field": "noise_seed"}

    if (ks := _node_by_class(wf, "KSampler")) is not None:
        for param, field in (("steps", "steps"), ("cfg", "cfg"),
                             ("sampler", "sampler_name"), ("scheduler", "scheduler")):
            m[param] = {"node": ks, "field": field}

    # Note: the model loader is NOT mapped as a request param here — it's an
    # admin-fixed binding (detected separately) / Pinned values dropdown, since the
    # model is a per-alias choice, not something varied per request.
    return m


def _comfy_models(object_info: dict) -> set[str]:
    """Best-effort: pull available checkpoint/unet filenames out of /object_info
    so routing can validate a workflow's model binding exists on the backend."""
    out: set[str] = set()
    for cls, fieldn in (("CheckpointLoaderSimple", "ckpt_name"), ("UNETLoader", "unet_name"),
                        ("UnetLoaderGGUF", "unet_name"), ("VAELoader", "vae_name")):
        spec = (object_info.get(cls, {}).get("input", {}).get("required", {}).get(fieldn))
        if isinstance(spec, list) and spec and isinstance(spec[0], list):
            out.update(str(x) for x in spec[0])
    return out


def _comfy_loras(object_info: dict) -> set[str]:
    """Installed LoRA filenames from /object_info — the combo options of any LoRA
    loader's `lora_name` / `lora_NN` field ('None' sentinel excluded)."""
    out: set[str] = set()
    for cls, spec in (object_info or {}).items():
        if "lora" not in str(cls).lower():
            continue
        inp = spec.get("input", {}) or {}
        for fn, fspec in {**(inp.get("required") or {}), **(inp.get("optional") or {})}.items():
            if not (fn == "lora_name" or _LORA_SLOT_RE.match(fn)):
                continue
            if isinstance(fspec, list) and fspec and isinstance(fspec[0], list):
                out.update(str(x) for x in fspec[0] if x and x != "None")
    return out


def _gen_values(req: NormalizedRequest) -> dict:
    """Flatten a request's logical generation values into one {param: value} dict
    the mapping can draw from: inputs (prompt/negative), params (width/steps/…),
    `params.extra` (workflow-specific knobs), and the resolved model."""
    values = {k: v for k, v in req.inputs.items() if v is not None}
    params = dict(req.params)
    extra = params.pop("extra", None) or {}
    values.update({k: v for k, v in params.items() if v is not None})
    values.update(extra)
    if req.real_model:
        values["model"] = req.real_model
    return values


class ComfyUIAdapter(BackendAdapter):
    """ComfyUI image/video/audio backend. discover() via /object_info; generate()
    submits a parametrized workflow and polls /history, then fetches /view."""

    type = "comfyui"

    async def discover(self, client: httpx.AsyncClient) -> Capabilities:
        url = self.backend["url"].rstrip("/")
        resp = await client.get(f"{url}/object_info", timeout=_COMFY_DISCOVERY_TIMEOUT)
        resp.raise_for_status()
        oi = resp.json()
        return Capabilities(models=_comfy_models(oi), loras=_comfy_loras(oi), pricing={})

    def _load_workflow(self, path: Optional[str]) -> dict:
        if not path:
            raise FileNotFoundError(f"{self.name}: no workflow configured for this alias")
        if not os.path.exists(path):
            raise FileNotFoundError(f"{self.name}: workflow file not found: {path}")
        with open(path) as f:
            return json.load(f)

    def _workflow_for(self, req: NormalizedRequest) -> dict:
        """A fresh, mutable copy of the workflow to inject into — preferring the
        gateway-owned JSON (registered via the UI), else a file path (share)."""
        if req.workflow_json is not None:
            return copy.deepcopy(req.workflow_json)            # never mutate the stored one
        return self._load_workflow(req.workflow)

    async def _upload_image(self, client: httpx.AsyncClient, data: bytes, name: str) -> str:
        """Upload an image to ComfyUI's input dir under a fixed, reused name
        (overwrite=true), so playground uploads never accumulate — one slot, no
        garbage (ComfyUI has no delete-input API). Returns the stored name."""
        url = self.backend["url"].rstrip("/")
        try:
            r = await client.post(f"{url}/upload/image",
                                  files={"image": (name, data, "image/png")},
                                  data={"overwrite": "true"}, timeout=_UPLOAD_TIMEOUT)
            return (r.json() or {}).get("name", name) if r.status_code == 200 else name
        except Exception:
            return name

    async def upload_input(self, data: bytes, name: str,
                           content_type: str = "application/octet-stream") -> str:
        """Upload an arbitrary file (a chain's intermediate mesh) into this backend's
        ComfyUI input dir and return the stored name. Lets a two-stage chain relay
        stage 1's mesh to a stage-2 backend that does NOT share disk — the successor
        workflow then loads it from input/ by this name (overwrite=true, one slot)."""
        url = self.backend["url"].rstrip("/")
        async with _pooled_client(self.ctx) as c:
            r = await c.post(f"{url}/upload/image",
                             files={"image": (name, data, content_type)},
                             data={"overwrite": "true"}, timeout=_UPLOAD_TIMEOUT)
        if r.status_code != 200:
            raise RuntimeError(f"mesh upload to '{self.backend.get('name')}' failed "
                               f"(HTTP {r.status_code}: {r.text[:200]})")
        j = r.json() or {}
        sub = (j.get("subfolder") or "").strip("/")
        nm = j.get("name", name)
        return f"{sub}/{nm}" if sub else nm

    async def _resolve_image_sentinels(self, fixed: list, upload: Optional[bytes]) -> list:
        """Replace image sentinels in fixed bindings with real uploaded names.
        A node pinned to the playground upload uses the uploaded image, or falls
        back to the 8×8 placeholder when nothing was uploaded — so the placeholder
        is simply the 'no reference' alternative right at the upload step."""
        has_ph = any(b.get("value") == PLACEHOLDER_SENTINEL for b in fixed)
        has_up = any(b.get("value") == UPLOAD_SENTINEL for b in fixed)
        if not (has_ph or has_up):
            return fixed
        need_ph = has_ph or (has_up and not upload)        # placeholder also backs an empty upload
        async with _pooled_client(self.ctx) as c:
            ph = await self._upload_image(c, _PLACEHOLDER_PNG, "gw_placeholder.png") if need_ph else None
            up = await self._upload_image(c, bytes(upload), "gw_upload.png") if (has_up and upload) else None
        def sub(b):
            v = b.get("value")
            if v == PLACEHOLDER_SENTINEL:
                return {**b, "value": ph} if ph else b
            if v == UPLOAD_SENTINEL:
                name = up or ph                             # uploaded image, else 8×8 placeholder
                return {**b, "value": name} if name else b
            return b
        return [sub(b) for b in fixed]

    async def _apply_image_params(self, wf: dict, mapping: dict, params: list,
                                  uploads: dict) -> list:
        """Per request-field image params: upload that field's image (or the 8×8
        placeholder when none was provided) and set the loader node's field to the
        stored name. Each param reuses its own input slot, so several images coexist
        without overwriting each other and without accumulating garbage."""
        if not params:
            return []
        applied = []
        async with _pooled_client(self.ctx) as c:
            for p in params:
                m = mapping.get(p) or {}
                nid, fld = m.get("node"), m.get("field")
                if not (nid and fld):
                    continue
                data = uploads.get(p)
                if data:
                    name = await self._upload_image(c, bytes(data), _img_slug(p))
                else:
                    mode = slot_empty_mode(m)
                    if mode == "required":
                        continue              # no 8×8 fallback (e.g. inpaint image/mask) →
                                              # keep the workflow's own value / error if empty
                    if mode == "disable":     # drop the loader node + its links → optional consumer
                        _prune_node(wf, nid)  # runs without this image
                        applied.append(p)
                        continue
                    name = await self._upload_image(c, _PLACEHOLDER_PNG, "gw_placeholder.png")
                wf.setdefault(nid, {}).setdefault("inputs", {})[fld] = name
                applied.append(p)
        return applied

    async def _autofill_empty_images(self, wf: dict, mapping: dict) -> list:
        """Any image-loader node still left with an empty `image` (not a mapped
        request field) → fill it with the 8×8 placeholder, so ComfyUI never tries to
        open the input/ directory. Mapped image fields are handled per-field above.

        Slots a `required`/`disable` request-field maps to are never auto-filled:
        `required` is left empty so ComfyUI errors clearly, `disable` was already pruned."""
        skip = {(m or {}).get("node") for m in (mapping or {}).values()
                if slot_empty_mode(m) in ("required", "disable")}
        empty = [nid for nid, n in wf.items()
                 if is_img_loader_class(n.get("class_type"))
                 and not (n.get("inputs") or {}).get("image")
                 and nid not in skip]
        if not empty:
            return []
        async with _pooled_client(self.ctx) as c:
            name = await self._upload_image(c, _PLACEHOLDER_PNG, "gw_placeholder.png")
        for nid in empty:
            wf[nid].setdefault("inputs", {})["image"] = name
        return empty

    async def generate(self, req: NormalizedRequest) -> GenOutput:
        b = self.backend
        url = b["url"].rstrip("/")
        bname = self.name
        wf = self._workflow_for(req)
        fixed = await self._resolve_image_sentinels(list(req.fixed or []), req.upload_image)
        fixed_applied = _apply_fixed(wf, fixed)                # pin models / switches / ref-images
        protected = {(b.get("node"), b.get("field")) for b in fixed   # pins the API cannot override
                     if b.get("node") and b.get("field")}
        values = _gen_values(req)
        mapping = req.node_mapping or suggest_mapping(wf)      # explicit wins, convention is fallback
        # Labels are the EXTERNAL field names (mapping editor: "label overrides the
        # Playground label / API field name") — honor them as aliases: a client may
        # send {"seed": …} when the param is wired as e.g. `value` labelled "seed".
        for p, m in mapping.items():
            lbl = ((m or {}).get("label") or "").strip()
            if lbl and lbl != p and lbl in values and p not in values:
                values[p] = values.pop(lbl)
        # Auto-seed keys on the EFFECTIVE name (param or label), so a PrimitiveInt
        # wired as `value` + label "seed" gets a random seed instead of int('') 400s.
        seed_param = next((p for p, m in mapping.items()
                           if p == "seed" or ((m or {}).get("label") or "").strip().lower() == "seed"),
                          None)
        if seed_param and values.get(seed_param) in (None, ""):
            values[seed_param] = random.randint(0, 2**63 - 1)
        # image request-fields are filled per-field (upload or 8×8 placeholder), not
        # via the scalar mapping — keep them out of _apply_mapping.
        img_params = image_params(wf, mapping)
        uploads = dict(req.upload_images or {})
        # Label aliasing for image slots (same as scalar params above): the schema
        # advertises a slot under its LABEL, so a client sends the image under the
        # label (`input_image`); remap it to the param the upload path keys on
        # (`image`), else the loader keeps its baked-in value.
        for p in img_params:
            lbl = ((mapping.get(p) or {}).get("label") or "").strip()
            if lbl and lbl != p and lbl in uploads and p not in uploads:
                uploads[p] = uploads.pop(lbl)
        if req.upload_image and len(img_params) == 1 and img_params[0] not in uploads:
            uploads[img_params[0]] = req.upload_image          # back-compat single upload
        for p in img_params:
            values.pop(p, None)
        # `loras:[{name,strength}]` first (pairs resolved per group), then the flat
        # legacy lora_N params → remaining free slots.
        lora_placed = _apply_lora_list(wf, mapping, req.loras or [], self.ctx.loras_of(self.bid))
        lora_placed += _apply_lora_cascade(wf, values)    # client loras → next free stack slots
        applied = _apply_mapping(wf, mapping, values, protected)
        img_applied = await self._apply_image_params(wf, mapping, img_params, uploads)
        autofilled = await self._autofill_empty_images(wf, mapping)
        summary = {"applied": sorted(applied.keys()),
                   "seed": values.get(seed_param) if seed_param else None,
                   "fixed": sorted(fixed_applied.keys()),
                   "loras": [f"{n}.{f}={v}" for n, f, v in lora_placed],
                   "images": sorted(img_applied), "autofilled_images": autofilled}

        poll_interval = float(b.get("poll_interval", 1.0))
        max_wait = float(b.get("max_wait", 600))
        if not req.slot_held:            # a chain claims the slot itself and holds it across stages
            self.ctx.inflight_inc(self.bid)
        started = time.monotonic()
        log_on = self.ctx.log_enabled()
        try:
            # short per-request read timeout (NOT max_wait): a hung /history or /view read
            # then fails fast → the disconnect-grace/failover logic kicks in instead of
            # blocking the whole job for max_wait. The overall budget is the poll deadline.
            # Deliberately a per-job client (not the shared pool): the client-level
            # timeout shapes every submit/poll/fetch call below, and one client per
            # multi-second generation job costs nothing.
            timeout = httpx.Timeout(30.0, read=float(b.get("read_timeout", 60)))
            async with httpx.AsyncClient(timeout=timeout) as client:
                pr = await client.post(f"{url}/prompt", json={"prompt": wf})
                if pr.status_code != 200:
                    raise RuntimeError(_comfy_prompt_error(pr.status_code, pr.text, wf, mapping))
                prompt_id = (pr.json() or {}).get("prompt_id")
                if not prompt_id:
                    raise RuntimeError("ComfyUI returned no prompt_id")
                if log_on:
                    logger.info(f"→ [{bname}] queued {prompt_id} (workflow {os.path.basename(req.workflow or '?')})")
                outputs = await self._poll(client, url, prompt_id, poll_interval, max_wait, started)
                rig, warnings = None, []
                if req.output_cases:                 # conditional case delivery (+ rig + validation)
                    blobs, rig = await self._fetch_by_cases(client, url, outputs, req.output_cases)
                    if not blobs:
                        raise RuntimeError(f"no output case matched (rigs: "
                                           f"{[c.get('rig') for c in req.output_cases]}; nodes with "
                                           f"outputs: {', '.join(sorted(outputs)) or 'none'})")
                    if req.output_globs:             # plain glob lines mixed with cases: unconditional extras
                        have = {b.name for b in blobs}
                        blobs += [b for b in await self._fetch_by_globs(client, url, outputs, req.output_globs)
                                  if b.name not in have]
                    normalize_delivery(blobs, rig, req.texture_format)   # V-flip (+ optional jpeg) textures
                    warnings = validate_delivery(blobs, rig)
                else:
                    blobs = await self._fetch_outputs(client, url, wf, outputs, req.output_node,
                                                      req.output_ext, req.output_globs)
                    if req.output_globs and not blobs:
                        raise RuntimeError(f"no output file matched {req.output_globs} "
                                           f"(nodes with outputs: {', '.join(sorted(outputs)) or 'none'})")
                    if req.output_node and not blobs:
                        extra = (f" as a '.{req.output_ext}' sibling" if req.output_ext else "")
                        raise RuntimeError(f"configured output node {req.output_node} produced "
                                           f"no fetchable artifact{extra} (no matching file in its outputs)")
                    _check_glb_not_dummy(blobs)  # 2x2-dummy safety net (case mode does it in validate)
        finally:
            if not req.slot_held:
                self.ctx.inflight_dec(self.bid)

        elapsed_ms = int((time.monotonic() - started) * 1000)
        if log_on:
            logger.info(f"← [{bname}] {len(blobs)} artifact(s) in {elapsed_ms} ms")
        return GenOutput(blobs=blobs, meta={
            "backend": bname, "workflow": req.workflow,
            "elapsed_ms": elapsed_ms, **summary,
            **({"rig": rig} if rig else {}),
            **({"warnings": warnings} if warnings else {}),
        })

    async def _poll(self, client, url, prompt_id, poll_interval, max_wait, started) -> dict:
        # If /history stops responding *after* the run was reachable for a while,
        # ComfyUI most likely crashed ("Reconnecting"). Fail fast with a clear
        # ConnectionError (→ the router can fail over) instead of waiting out the
        # full timeout. `disconnect_grace` = seconds of continuous unreachability
        # tolerated before declaring the backend gone.
        grace = float(self.backend.get("disconnect_grace", 30))
        deadline = time.monotonic() + max_wait
        last_ok = time.monotonic()
        gone_since = None
        last_exc = None
        while time.monotonic() < deadline:
            await asyncio.sleep(poll_interval)
            try:
                hr = await client.get(f"{url}/history/{prompt_id}")
                last_ok = time.monotonic()
                if hr.status_code != 200:
                    continue
                hist = hr.json()
                if prompt_id in hist:
                    entry = hist[prompt_id]
                    status = entry.get("status", {})
                    if status.get("status_str") == "error":
                        raise RuntimeError(f"ComfyUI: {_format_comfy_error(status.get('messages'))}")
                    return entry.get("outputs", {})
                # Not done yet. Confirm it's still queued/running — if ComfyUI restarted,
                # the prompt is gone from BOTH history and queue, yet /history keeps
                # answering 200 (so the disconnect-grace never fires). Detecting the
                # vanished prompt fails fast instead of polling out the full max_wait.
                still = None
                try:
                    qr = await client.get(f"{url}/queue")
                    if qr.status_code == 200:
                        still = _prompt_in_queue(qr.json(), prompt_id)
                except Exception:
                    still = None                # unknown → leave the gone-timer as is
                if still is True:
                    gone_since = None
                elif still is False:
                    gone_since = gone_since or time.monotonic()
                    if time.monotonic() - gone_since > grace:
                        raise ConnectionError(
                            f"ComfyUI prompt {prompt_id} gone from history AND queue for "
                            f">{grace:.0f}s — ComfyUI likely restarted mid-job")
                continue
            except (RuntimeError, ConnectionError):
                raise
            except Exception as e:
                last_exc = e
                if time.monotonic() - last_ok > grace:
                    raise ConnectionError(
                        f"ComfyUI unreachable for >{grace:.0f}s during execution "
                        f"(likely crashed/restarting): {type(e).__name__}: {e}")
                continue
        try:                                    # free the GPU: stop the still-running prompt
            await client.post(f"{url}/interrupt")
        except Exception:
            pass
        raise TimeoutError(f"ComfyUI timeout after {max_wait:.0f}s (prompt {prompt_id}); "
                           f"last poll error: {last_exc}")

    async def _fetch_outputs(self, client, url, wf, outputs,
                             output_node: Optional[str] = None,
                             output_ext: Optional[str] = None,
                             output_globs: Optional[list] = None) -> list[GenBlob]:
        # Glob mode (multi-file): deliver EVERY file matching the alias's patterns,
        # gathered across ALL output nodes. A workflow that rigs twice + bakes
        # textures registers several files (…_articulationxl.fbx, …_basecolor_*.png,
        # …_mia.fbx); the client wants a specific subset AND a sibling (…_mia.glb,
        # written next to …_mia.fbx but not registered). For each reported file we
        # also try its stem swapped to each glob's extension, so a glob picks up an
        # on-disk sibling via /view. Unmatched globs are simply absent (a later
        # split workflow producing only some files still works). Takes precedence
        # over output_node/output_ext.
        if output_globs:
            return await self._fetch_by_globs(client, url, outputs, output_globs)
        # Single-node mode: the alias's explicit output node (mapping editor
        # "Output" section) is authoritative — a workflow may export intermediate
        # files from several nodes, and only the configured one is the result. It
        # producing nothing is an ERROR, not a fallback. Without the setting: the
        # node titled `output_final`, else everything (legacy behaviour).
        if output_node:
            if output_node not in outputs:
                raise RuntimeError(f"configured output node {output_node} produced no output "
                                   f"(nodes with outputs: {', '.join(sorted(outputs)) or 'none'})")
            targets = {output_node: outputs[output_node]}
        else:
            final = _node_by_title(wf, "output_final")
            targets = {final: outputs[final]} if (final and final in outputs) else outputs
        blobs: list[GenBlob] = []
        seen: set = set()
        for _nid, out in targets.items():
            # Scan every list-valued output key (images / gifs / videos / audio /
            # fbx_file / result / …), not just images+gifs, so SaveVideo/SaveAudio
            # AND custom nodes that report an on-disk PATH (UniRig's `fbx_file`,
            # Preview3D's `result`) are all caught regardless of the key. Two item
            # shapes: a {filename, subfolder?, type?} dict (core save nodes) or a
            # bare path string (custom nodes) — _view_params handles both and
            # ignores non-file items (booleans, status strings). Deduped by
            # (filename, subfolder).
            for _key, items in out.items():
                if not isinstance(items, list):
                    continue
                for item in items:
                    parsed = _view_params(item)
                    if parsed is None:
                        continue
                    fn, view = parsed
                    # With output_ext, prefer the SIBLING with that extension over the
                    # reported file: UniRig registers only `fbx_file` but writes
                    # <stem>.fbx AND <stem>.glb (+ a .fbm folder); the client wants
                    # the textured .glb. A reported file WITHOUT such a sibling (the
                    # basecolor PNG next to the mesh) still ships itself — swapping
                    # it away would silently drop it from the delivery.
                    tries = [(fn, view)]
                    if output_ext:
                        stem = fn[:-(len(fn.split(".")[-1]) + 1)] if "." in fn else fn
                        sib = f"{stem}.{output_ext}"
                        if sib != fn:
                            tries.insert(0, (sib, {**view, "filename": sib}))
                    r = None
                    for fn, view in tries:
                        key = (fn, view.get("subfolder", ""))
                        if key in seen:          # already delivered/probed for an earlier item
                            r = None
                            break
                        seen.add(key)
                        r = await client.get(f"{url}/view", params=view)
                        if r.status_code == 200:
                            break
                        if r.status_code != 404:   # only "no such file" may probe on — a backend
                            raise RuntimeError(    # error must not silently shrink the delivery
                                f"/view '{fn}' → HTTP {r.status_code}")
                        r = None
                    if r is None:
                        continue
                    mime, kind = _mime_and_kind(fn)
                    if mime == "application/octet-stream" and isinstance(item, dict):
                        fmt = item.get("format")               # dict items carry a format hint
                        if isinstance(fmt, str) and "/" in fmt:
                            top = fmt.split("/", 1)[0].lower()
                            if top in ("video", "audio"):
                                kind = top
                                mime = {"video": "video/mp4", "audio": "audio/mpeg"}[top]
                    blobs.append(GenBlob(data=r.content, mime=mime, kind=kind, name=fn))
        return blobs

    async def _fetch_by_cases(self, client, url, outputs, cases) -> tuple:
        """Conditional delivery: pick the FIRST case whose detect file (its first
        glob) actually exists in this run, then deliver all of that case's globs.
        Two riggers may share a workflow (humanoid MIA vs non-humanoid UniRig) or
        live in split workflows — either way exactly one case's detect file is
        present, so the client gets that case's files + its rig type. Returns
        (blobs, rig).

        A case is fetched ONCE and the detect condition checked on the result —
        a separate detect probe would download the (often large) detect file
        twice per job. A /view failure that is not a plain 404 raises inside
        _fetch_by_globs, so a transiently unreachable file fails the job loudly
        instead of silently selecting the wrong case."""
        for case in cases:
            globs = case.get("globs") or []
            if not globs:
                continue
            blobs = await self._fetch_by_globs(client, url, outputs, globs)
            if not any(fnmatch.fnmatch((b.name or "").lower(), globs[0].lower()) for b in blobs):
                continue                              # detect file absent → this case didn't run
            return blobs, case.get("rig")
        return [], None

    async def _fetch_by_globs(self, client, url, outputs, globs) -> list[GenBlob]:
        """Deliver every registered file (or a same-stem sibling) matching a glob.
        Order follows the globs, so results come out predictable for the client."""
        glob_exts = {g.rsplit(".", 1)[-1].lower() for g in globs if "." in g}
        # collect (filename, view-params) candidates across all output nodes: the
        # reported file plus its stem swapped to each requested extension (siblings).
        cands: list = []
        seen_cand: set = set()
        for out in outputs.values():
            for items in out.values():
                if not isinstance(items, list):
                    continue
                for item in items:
                    parsed = _view_params(item)
                    if parsed is None:
                        continue
                    fn, view = parsed
                    stem = fn[:-(len(fn.rsplit(".", 1)[-1]) + 1)] if "." in fn else fn
                    for cfn in {fn, *(f"{stem}.{e}" for e in glob_exts)}:
                        key = (cfn, view.get("subfolder", ""))
                        if key not in seen_cand:
                            seen_cand.add(key)
                            cands.append((cfn, {**view, "filename": cfn}))
        blobs, fetched = [], set()
        for g in globs:                                  # glob order → stable result order
            for cfn, view in cands:
                if (cfn, view.get("subfolder", "")) in fetched:
                    continue
                if not fnmatch.fnmatch(cfn.lower(), g.lower()):
                    continue
                r = await client.get(f"{url}/view", params=view)
                if r.status_code == 404:                 # sibling for this glob doesn't exist
                    continue
                if r.status_code != 200:                 # a backend error is not "file absent" —
                    raise RuntimeError(                  # it must not shrink or mis-detect a delivery
                        f"/view '{cfn}' → HTTP {r.status_code}")
                fetched.add((cfn, view.get("subfolder", "")))
                mime, kind = _mime_and_kind(cfn)
                blobs.append(GenBlob(data=r.content, mime=mime, kind=kind, name=cfn))
        return blobs


# ── Registry ──────────────────────────────────────────────────────────────────

ADAPTERS: dict[str, type[BackendAdapter]] = {
    "openai": OpenAIAdapter,
    "comfyui": ComfyUIAdapter,
}


def make_adapter(backend: dict, ctx: AdapterContext) -> BackendAdapter:
    """Instantiate the adapter for a backend, dispatched on its `type` field
    (default `openai` → unchanged behaviour for every existing backend)."""
    cls = ADAPTERS.get(backend.get("type", "openai"), OpenAIAdapter)
    return cls(backend, ctx)
