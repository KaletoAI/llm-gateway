"""OpenAI Images API ↔ native generation translation helpers (C4 shims).

Pure request/response plumbing for `/v1/images/generations` and
`/v1/images/edits` — no gateway state, no `main`/`adapters` imports (same purity
rule as responses_bridge.py; `jobs` is a leaf module and fine to import).
`main.py` keeps the endpoints themselves (auth, routing, `run_generation`) and
passes the alias's image-input `slots` in, so this module never touches routing.
"""
from __future__ import annotations

import base64
import re
import time
from typing import Optional

from fastapi import HTTPException, Request

import jobs

# Known OpenAI image-request keys; everything else a client sends is forwarded as a
# native workflow param (loras, seed, steps, cfg, …) → dynamic control, no presets.
OAI_IMG_KEYS = {"prompt", "model", "n", "size", "response_format", "negative_prompt",
                "ref_images", "quality", "style", "background", "output_format", "user",
                "mode", "ttl_s", "params", "stream"}
EDIT_KNOWN = {"model", "prompt", "negative_prompt", "size", "n", "response_format", "image", "mask"}


def coerce_scalar(s):
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


def parse_size(size: Optional[str]) -> tuple[int, int]:
    """OpenAI `size` ('1024x1024' | 'auto' | None) -> (width, height)."""
    if not size or str(size).lower() == "auto":
        return 1024, 1024
    try:
        w, h = str(size).lower().split("x")
        return int(w), int(h)
    except (ValueError, AttributeError):
        return 1024, 1024


async def multipart_list(request: Request) -> dict:
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


def images_uploads(images: list, slots: list) -> dict:
    """Map reference-image blobs positionally onto the alias's ordered image-input
    slots. The cap is the alias's slot count (not a fixed number): extra images are
    ignored, unfilled slots get the adapter's 8x8 placeholder."""
    uploads: dict = {}
    for i, slot in enumerate(slots):
        if i < len(images) and images[i]:
            uploads[slot] = bytes(images[i])
    return uploads


def images_response(view: dict, response_format: str) -> dict:
    """Native job view -> OpenAI images response {created, data:[{url|b64_json}]}.
    Each entry also carries `mime` so a client can tell a video/audio artifact from
    an image (video aliases are better consumed via the native /v1/generations,
    which returns per-result kind+mime). Blocking in b64 mode (file reads) — call
    via asyncio.to_thread."""
    data = []
    for r in view.get("results", []):
        entry = {"mime": r.get("mime")}
        if response_format == "b64_json":
            rp = jobs.result_path(view["job_id"], r["n"])
            if not rp:
                continue
            with open(rp[0], "rb") as fh:
                entry["b64_json"] = base64.b64encode(fh.read()).decode()
        else:
            entry["url"] = r["url"]
        data.append(entry)
    return {"created": int(time.time()), "data": data}


def gen_done_or_502(view: dict) -> dict:
    if view.get("status") != "done":
        raise HTTPException(502, f"image generation {view.get('status')}: {view.get('error')}")
    return view
