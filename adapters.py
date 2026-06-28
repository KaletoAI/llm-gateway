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
import json
import logging
import os
import random
import re
import struct
import time
import zlib
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any, Callable, Optional

import httpx
from fastapi import Request
from fastapi.responses import JSONResponse, StreamingResponse

logger = logging.getLogger(__name__)


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


@dataclass
class GenBlob:
    """One produced artifact (image/video/audio) as raw bytes + type hints."""
    data: bytes
    mime: str
    kind: str                                       # "image" | "video" | "audio"


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

def _sse_usage(line: str) -> Optional[tuple]:
    """Extract (prompt_tokens, completion_tokens) from one SSE `data:` line that
    carries a `usage` block (emitted by the backend when we request
    `stream_options.include_usage`). Returns None for normal delta lines."""
    if not line.startswith("data:"):
        return None
    data = line[5:].strip()
    if not data or data == "[DONE]":
        return None
    try:
        obj = json.loads(data)
    except Exception:
        return None
    u = obj.get("usage") if isinstance(obj, dict) else None
    if not u:
        return None
    return int(u.get("prompt_tokens") or 0), int(u.get("completion_tokens") or 0)


class OpenAIAdapter(BackendAdapter):
    """llama.cpp / llama-swap / vLLM / Ollama / Together / OpenRouter / OpenAI —
    anything that speaks `/v1/models` + `/v1/chat|completions|embeddings`."""

    type = "openai"

    async def discover(self, client: httpx.AsyncClient) -> Capabilities:
        b = self.backend
        resp = await client.get(
            f"{b['url']}/v1/models", headers=self.ctx.auth_headers(b), timeout=5.0
        )
        resp.raise_for_status()
        payload = resp.json()
        return Capabilities(models=extract_models(payload, b),
                            pricing=extract_pricing(payload))

    async def dispatch(self, req: NormalizedRequest):
        b = self.backend
        ctx = self.ctx
        bname = self.name
        url = f"{b['url']}{req.path}"
        headers = {
            k: v for k, v in req.raw.headers.items()
            if k.lower() not in ("host", "content-length", "authorization")
        }
        headers.update(ctx.auth_headers(b))
        ctx.inflight_inc(self.bid)        # released on completion (stream close / return / error)
        real_model = req.body.get("model")
        source = ctx.source_of(req.raw)
        req_text = json.dumps(req.body, ensure_ascii=False)
        started = time.monotonic()
        log_on = ctx.log_enabled()

        if req.body.get("stream"):
            path, alias = req.path, req.alias
            # Ask the backend to emit a final usage chunk so streamed calls record
            # real tokens/cost instead of 0 (graceful: backends that ignore the
            # field just yield no usage line → tokens stay 0, as before).
            body = {**req.body,
                    "stream_options": {**(req.body.get("stream_options") or {}), "include_usage": True}}

            async def generate():
                stream_status = 0
                in_tok = out_tok = 0
                buf = ""
                try:
                    async with httpx.AsyncClient() as client:
                        async with client.stream("POST", url, json=body, headers=headers, timeout=300.0) as resp:
                            stream_status = resp.status_code
                            if log_on:
                                logger.info(f"← [{bname}] {path} HTTP {stream_status} (stream open, {(time.monotonic()-started):.2f}s)")
                            async for chunk in resp.aiter_bytes():
                                yield chunk
                                buf += chunk.decode("utf-8", "ignore")
                                while "\n" in buf:
                                    line, buf = buf.split("\n", 1)
                                    got = _sse_usage(line.strip())
                                    if got:
                                        in_tok, out_tok = got
                finally:
                    ctx.inflight_dec(self.bid)
                elapsed_ms = int((time.monotonic() - started) * 1000)
                cost = ctx.cost_usd(self.bid, real_model, in_tok, out_tok) if (in_tok or out_tok) else 0.0
                asyncio.create_task(ctx.record_call(
                    duration_ms=elapsed_ms, backend=bname, source=source,
                    alias=alias, model=real_model, endpoint=(req.stats_endpoint or path),
                    status=stream_status, input_tokens=in_tok, output_tokens=out_tok, cost_usd=cost,
                    request_text=req_text,
                ))

            return StreamingResponse(generate(), media_type="text/event-stream")

        try:
            async with httpx.AsyncClient() as client:
                resp = await client.post(url, json=req.body, headers=headers, timeout=300.0)
        finally:
            ctx.inflight_dec(self.bid)
        elapsed_ms = int((time.monotonic() - started) * 1000)
        if log_on:
            logger.info(f"← [{bname}] {req.path} HTTP {resp.status_code} ({elapsed_ms} ms)")
        try:
            resp_json = resp.json()
        except Exception:
            resp_json = {}
        usage = (resp_json.get("usage") or {}) if isinstance(resp_json, dict) else {}
        in_tok = int(usage.get("prompt_tokens") or 0)
        out_tok = int(usage.get("completion_tokens") or 0)
        asyncio.create_task(ctx.record_call(
            duration_ms=elapsed_ms, backend=bname, source=source,
            alias=req.alias, model=real_model, endpoint=(req.stats_endpoint or req.path),
            status=resp.status_code,
            input_tokens=in_tok, output_tokens=out_tok,
            cost_usd=ctx.cost_usd(self.bid, real_model, in_tok, out_tok),
            request_text=req_text, response_text=json.dumps(resp_json, ensure_ascii=False),
        ))
        return JSONResponse(resp_json, status_code=resp.status_code)


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
}


def _mime_and_kind(filename: str) -> tuple[str, str]:
    return _MIME_BY_EXT.get(os.path.splitext(filename)[1].lower(),
                            ("application/octet-stream", "image"))


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


def is_image_field(wf: dict, node: str) -> bool:
    """True if a mapped node is an image-loader (its request field takes an uploaded
    image, not a scalar). Drives the per-field file inputs in the playground."""
    return (wf or {}).get(node, {}).get("class_type") in _IMG_LOADER_CLASSES


def image_params(wf: dict, mapping: dict) -> list:
    """Request params whose target node is an image loader → rendered as uploads and
    filled per-field (uploaded file, else an 8×8 placeholder)."""
    return [p for p, m in (mapping or {}).items() if is_image_field(wf, (m or {}).get("node"))]


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


_LORA_SLOT_RE = re.compile(r"^lora_0*(\d+)$")          # rgthree stack: lora_01..lora_NN
_STR_SLOT_RE = re.compile(r"^strength_0*(\d+)$")


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
        resp = await client.get(f"{url}/object_info", timeout=8.0)
        resp.raise_for_status()
        return Capabilities(models=_comfy_models(resp.json()), pricing={})

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
            return json.loads(json.dumps(req.workflow_json))   # deep copy, never mutate the stored one
        return self._load_workflow(req.workflow)

    async def _upload_image(self, client: httpx.AsyncClient, data: bytes, name: str) -> str:
        """Upload an image to ComfyUI's input dir under a fixed, reused name
        (overwrite=true), so playground uploads never accumulate — one slot, no
        garbage (ComfyUI has no delete-input API). Returns the stored name."""
        url = self.backend["url"].rstrip("/")
        try:
            r = await client.post(f"{url}/upload/image",
                                  files={"image": (name, data, "image/png")},
                                  data={"overwrite": "true"})
            return (r.json() or {}).get("name", name) if r.status_code == 200 else name
        except Exception:
            return name

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
        async with httpx.AsyncClient(timeout=20.0) as c:
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
        async with httpx.AsyncClient(timeout=20.0) as c:
            for p in params:
                m = mapping.get(p) or {}
                nid, fld = m.get("node"), m.get("field")
                if not (nid and fld):
                    continue
                data = uploads.get(p)
                if data:
                    name = await self._upload_image(c, bytes(data), _img_slug(p))
                elif m.get("no_placeholder"):
                    continue                  # required slot (e.g. inpaint image/mask): no 8×8 fallback,
                                              # keep the workflow's own value
                else:
                    name = await self._upload_image(c, _PLACEHOLDER_PNG, "gw_placeholder.png")
                wf.setdefault(nid, {}).setdefault("inputs", {})[fld] = name
                applied.append(p)
        return applied

    async def _autofill_empty_images(self, wf: dict) -> list:
        """Any image-loader node still left with an empty `image` (not a mapped
        request field) → fill it with the 8×8 placeholder, so ComfyUI never tries to
        open the input/ directory. Mapped image fields are handled per-field above."""
        empty = [nid for nid, n in wf.items()
                 if n.get("class_type") in _IMG_LOADER_CLASSES
                 and not (n.get("inputs") or {}).get("image")]
        if not empty:
            return []
        async with httpx.AsyncClient(timeout=20.0) as c:
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
        if "seed" in mapping and values.get("seed") is None:
            values["seed"] = random.randint(0, 2**63 - 1)
        # image request-fields are filled per-field (upload or 8×8 placeholder), not
        # via the scalar mapping — keep them out of _apply_mapping.
        img_params = image_params(wf, mapping)
        uploads = dict(req.upload_images or {})
        if req.upload_image and len(img_params) == 1 and img_params[0] not in uploads:
            uploads[img_params[0]] = req.upload_image          # back-compat single upload
        for p in img_params:
            values.pop(p, None)
        lora_placed = _apply_lora_cascade(wf, values)     # client loras → next free stack slots
        applied = _apply_mapping(wf, mapping, values, protected)
        img_applied = await self._apply_image_params(wf, mapping, img_params, uploads)
        autofilled = await self._autofill_empty_images(wf)
        summary = {"applied": sorted(applied.keys()), "seed": values.get("seed"),
                   "fixed": sorted(fixed_applied.keys()),
                   "loras": [f"{n}.{f}={v}" for n, f, v in lora_placed],
                   "images": sorted(img_applied), "autofilled_images": autofilled}

        poll_interval = float(b.get("poll_interval", 1.0))
        max_wait = float(b.get("max_wait", 600))
        self.ctx.inflight_inc(self.bid)
        started = time.monotonic()
        log_on = self.ctx.log_enabled()
        try:
            timeout = httpx.Timeout(30.0, read=max_wait)
            async with httpx.AsyncClient(timeout=timeout) as client:
                pr = await client.post(f"{url}/prompt", json={"prompt": wf})
                if pr.status_code != 200:
                    raise RuntimeError(f"ComfyUI /prompt HTTP {pr.status_code}: {pr.text}")
                prompt_id = (pr.json() or {}).get("prompt_id")
                if not prompt_id:
                    raise RuntimeError("ComfyUI returned no prompt_id")
                if log_on:
                    logger.info(f"→ [{bname}] queued {prompt_id} (workflow {os.path.basename(req.workflow or '?')})")
                outputs = await self._poll(client, url, prompt_id, poll_interval, max_wait, started)
                blobs = await self._fetch_outputs(client, url, wf, outputs)
        finally:
            self.ctx.inflight_dec(self.bid)

        elapsed_ms = int((time.monotonic() - started) * 1000)
        if log_on:
            logger.info(f"← [{bname}] {len(blobs)} artifact(s) in {elapsed_ms} ms")
        return GenOutput(blobs=blobs, meta={
            "backend": bname, "workflow": req.workflow,
            "elapsed_ms": elapsed_ms, **summary,
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
        last_exc = None
        while time.monotonic() < deadline:
            await asyncio.sleep(poll_interval)
            try:
                hr = await client.get(f"{url}/history/{prompt_id}")
                last_ok = time.monotonic()
                if hr.status_code != 200:
                    continue
                hist = hr.json()
                if prompt_id not in hist:
                    continue
                entry = hist[prompt_id]
                status = entry.get("status", {})
                if status.get("status_str") == "error":
                    raise RuntimeError(f"ComfyUI execution error: {json.dumps(status.get('messages'))}")
                return entry.get("outputs", {})
            except RuntimeError:
                raise
            except Exception as e:
                last_exc = e
                if time.monotonic() - last_ok > grace:
                    raise ConnectionError(
                        f"ComfyUI unreachable for >{grace:.0f}s during execution "
                        f"(likely crashed/restarting): {type(e).__name__}: {e}")
                continue
        raise TimeoutError(f"ComfyUI timeout after {max_wait:.0f}s (prompt {prompt_id}); "
                           f"last poll error: {last_exc}")

    async def _fetch_outputs(self, client, url, wf, outputs) -> list[GenBlob]:
        final = _node_by_title(wf, "output_final")
        targets = {final: outputs[final]} if (final and final in outputs) else outputs
        blobs: list[GenBlob] = []
        for _nid, out in targets.items():
            for item in (out.get("images", []) + out.get("gifs", [])):
                fn = item.get("filename")
                if not fn:
                    continue
                view = {"filename": fn, "type": item.get("type", "output")}
                if item.get("subfolder"):
                    view["subfolder"] = item["subfolder"]
                r = await client.get(f"{url}/view", params=view)
                if r.status_code == 200:
                    mime, kind = _mime_and_kind(fn)
                    blobs.append(GenBlob(data=r.content, mime=mime, kind=kind))
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
