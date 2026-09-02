"""Meshy.ai (https://docs.meshy.ai) as a generation backend — the PURE half.

Everything here is a function of dicts and bytes: the public `input_*` label table
(spec 2026-09-02 §5), the request body Meshy's image-to-3d / multi-image-to-3d
endpoints take, the task object they hand back, and the schema the gateway
advertises for a Meshy alias. No `main`/`adapters` imports, no I/O — the adapter in
`adapters.py` does the HTTP, this module decides WHAT to send and what came back.
Covered by test_meshy.py (stdlib unittest).
"""
from __future__ import annotations

import base64
import copy
from dataclasses import dataclass, field
from typing import Optional

ENDPOINTS = ("image-to-3d", "multi-image-to-3d")
AI_MODELS = ("latest", "meshy-7", "meshy-6", "meshy-5")
FORMATS = ("glb", "obj", "fbx", "stl", "usdz", "3mf")
TOPOLOGIES = ("triangle", "quad")
TEXTURE_RES = ("2k", "4k", "8k")
POSES = ("", "a-pose", "t-pose")

# Admin defaults of a Meshy alias candidate (`cand["meshy"]["options"]`). Which of
# these a CLIENT may override is decided by the label table in build_request — the
# rest is admin-only, the counterpart of a ComfyUI `fixed` pin.
OPTION_DEFAULTS: dict = {
    "should_texture": True, "enable_pbr": False, "texture_resolution": "2k",
    "topology": "triangle", "should_remesh": None,          # None = Meshy's per-model default
    "ultra_mode": False, "pose_mode": "", "image_enhancement": True,
    "remove_lighting": True, "moderation": False,
    "target_formats": ["glb"], "thumbnail": True,
}
# Options that are copied into the request body verbatim (None = leave it out).
_PASSTHROUGH = ("should_texture", "enable_pbr", "topology", "should_remesh", "ultra_mode",
                "image_enhancement", "remove_lighting", "moderation", "target_formats")

SLOTS: dict[str, list[str]] = {
    "image-to-3d": ["input_image"],
    "multi-image-to-3d": ["input_image_front", "input_image_back",
                          "input_image_left", "input_image_right"],
}
IGNORED_PARAMS = ("input_remove_background", "input_no_fingers")   # accepted, no effect

_POLY_MIN, _POLY_MAX = 100, 300_000
_NAME_MAX, _TEXTURE_PROMPT_MAX = 100, 800
_RES_PX = {"2k": 2048, "4k": 4096, "8k": 8192}


class MeshyInput(RuntimeError):
    """A request Meshy cannot run (missing front image, unsupported image format,
    bad enum) — a content error, final, never failed over."""


def endpoint_of(cand: dict) -> str:
    ep = ((cand.get("meshy") or {}).get("endpoint") or "").strip()
    return ep if ep in ENDPOINTS else ENDPOINTS[0]


def options_of(cand: dict) -> dict:
    """OPTION_DEFAULTS overlaid with the candidate's stored options (unknown keys dropped)."""
    stored = (cand.get("meshy") or {}).get("options") or {}
    out = dict(OPTION_DEFAULTS)
    for k in OPTION_DEFAULTS:
        if k in stored:
            out[k] = stored[k]
    if not isinstance(out["target_formats"], list) or not out["target_formats"]:
        out["target_formats"] = ["glb"]
    out["target_formats"] = [f for f in out["target_formats"] if f in FORMATS] or ["glb"]
    if out["texture_resolution"] not in TEXTURE_RES:
        out["texture_resolution"] = "2k"
    return out


def default_candidate(backend: str) -> dict:
    """The candidate the console creates when registering an alias on a Meshy backend."""
    # deepcopy, not dict(): a shallow copy hands every candidate the SAME
    # `target_formats` list object as the module constant, so one in-place edit on a
    # stored candidate would rewrite the default for every future alias.
    return {"backend": backend, "task": "img2mesh", "model": "latest",
            "meshy": {"endpoint": ENDPOINTS[0], "options": copy.deepcopy(OPTION_DEFAULTS)}}


def texture_res(px) -> str:
    """Public `input_texture_resolution` (pixels, as the ComfyUI aliases take it) →
    Meshy bucket: ≤2048 → 2k, ≤4096 → 4k, else 8k. A bucket name passes through."""
    if isinstance(px, str) and px.strip().lower() in TEXTURE_RES:
        return px.strip().lower()
    try:
        n = int(float(px))
    except (TypeError, ValueError):
        return "2k"
    return "2k" if n <= 2048 else "4k" if n <= 4096 else "8k"


def data_uri(data: bytes) -> str:
    """Bytes → base64 data URI. Meshy accepts JPG/JPEG/PNG only; sniffed by magic."""
    if data[:8] == b"\x89PNG\r\n\x1a\n":
        mime = "image/png"
    elif data[:3] == b"\xff\xd8\xff":
        mime = "image/jpeg"
    else:
        raise MeshyInput("Meshy accepts PNG or JPEG images only")
    return f"data:{mime};base64,{base64.b64encode(data).decode()}"


def _slot_images(endpoint: str, images: dict) -> list[str]:
    """Data URIs in SLOT order (front first); empty slots dropped; first slot required."""
    slots = SLOTS[endpoint]
    present = [(s, images.get(s)) for s in slots if images.get(s)]
    if not present or present[0][0] != slots[0]:
        raise MeshyInput(f"`images.{slots[0]}` is required")
    return [data_uri(b) for _, b in present]


def build_request(cand: dict, values: dict, images: dict) -> dict:
    """The JSON body for POST /openapi/v1/<endpoint>.

    `values` is the flattened request (params + inputs, public labels as keys);
    `images` is {label: bytes}. Admin options come from the candidate; the client may
    set only what the label table below names. Unknown params are ignored, as on
    every generation alias."""
    ep = endpoint_of(cand)
    opts = options_of(cand)
    m = cand.get("model")
    body: dict = {"ai_model": m if m in AI_MODELS else "latest"}
    uris = _slot_images(ep, images)
    if ep == "image-to-3d":
        body["image_url"] = uris[0]
    else:
        body["image_urls"] = uris
    for k in _PASSTHROUGH:
        if opts.get(k) is not None:
            body[k] = opts[k]
    body["texture_resolution"] = opts["texture_resolution"]
    if opts.get("pose_mode"):
        body["pose_mode"] = opts["pose_mode"]
    # ── client-settable labels ──
    name = values.get("input_name")
    if name not in (None, ""):
        body["name"] = str(name)[:_NAME_MAX]
    fn = values.get("input_face_num")
    if fn not in (None, ""):
        try:
            body["target_polycount"] = max(_POLY_MIN, min(_POLY_MAX, int(float(fn))))
            body["should_remesh"] = True          # a polycount only applies with a remesh pass
        except (TypeError, ValueError):
            pass
    tr = values.get("input_texture_resolution")
    if tr not in (None, ""):
        body["texture_resolution"] = texture_res(tr)
    tp = values.get("input_texture_prompt")
    if tp not in (None, ""):
        body["texture_prompt"] = str(tp)[:_TEXTURE_PROMPT_MAX]
    pose = values.get("input_pose")
    if pose not in (None, ""):
        if pose not in POSES:
            raise MeshyInput("`input_pose` must be one of " + ", ".join(p or "none" for p in POSES))
        body["pose_mode"] = pose
    return body


def request_summary(body: dict) -> dict:
    """The request as recorded on the job: image data replaced by its byte size."""
    def _sz(uri: str) -> str:
        try:
            raw = uri.split(",", 1)[1]
            return f"<{len(base64.b64decode(raw))} bytes>"
        except Exception:
            return "<image>"
    out = dict(body)
    if "image_url" in out:
        out["image_url"] = _sz(out["image_url"])
    if "image_urls" in out:
        out["image_urls"] = [_sz(u) for u in out["image_urls"]]
    return out


def public_fields(cand: dict) -> tuple[list, list]:
    """(params, images) in the shape GET /v1/generations/{alias}/schema advertises.
    Image entries: {name, on_empty, required}; params: {name, type, default?, choices?}."""
    ep = endpoint_of(cand)
    opts = options_of(cand)
    images = [{"name": s, "on_empty": "required" if i == 0 else "skip", "required": i == 0}
              for i, s in enumerate(SLOTS[ep])]
    params = [
        {"name": "input_name", "type": "string", "default": ""},
        # No `default`: build_request sets target_polycount ONLY when the client sends
        # this label, and doing so also forces should_remesh — advertising a default
        # would promise a value the request builder never applies. Left out, Meshy's
        # own per-model default decides.
        {"name": "input_face_num", "type": "int"},
        {"name": "input_texture_resolution", "type": "int", "default": _RES_PX[opts["texture_resolution"]]},
        {"name": "input_texture_prompt", "type": "string", "default": ""},
        {"name": "input_pose", "type": "string", "default": opts.get("pose_mode") or "",
         "choices": list(POSES)},
        {"name": "input_remove_background", "type": "bool", "default": True},
        {"name": "input_no_fingers", "type": "bool", "default": False},
    ]
    return params, images


@dataclass
class TaskState:
    status: str
    progress: int = 0
    error: Optional[str] = None
    downloads: list = field(default_factory=list)      # [(fmt, url)] in requested order
    thumbnail: Optional[str] = None
    credits: Optional[int] = None


def parse_task(task: dict, formats: list) -> TaskState:
    """Read a task object (GET …/{id}). On SUCCEEDED every requested format must have
    a URL — a missing one raises, never a silently smaller delivery."""
    status = str(task.get("status") or "").upper()
    st = TaskState(status=status, progress=int(task.get("progress") or 0),
                   credits=task.get("consumed_credits"),
                   thumbnail=task.get("thumbnail_url") or None)
    if status in ("FAILED", "CANCELED"):
        st.error = ((task.get("task_error") or {}).get("message") or status.lower())
        return st
    if status == "SUCCEEDED":
        urls = task.get("model_urls") or {}
        missing = [f for f in formats if not urls.get(f)]
        if missing:
            raise MeshyInput(f"Meshy task succeeded but has no url for {', '.join(missing)}")
        st.downloads = [(f, urls[f]) for f in formats]
    return st
