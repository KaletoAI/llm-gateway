# Meshy Backend Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Meshy.ai (Image-to-3D und Multi-Image-to-3D) als Generierungs-Backend `type: meshy` neben ComfyUI, ohne Änderung der öffentlichen Gateway-API.

**Architecture:** Ein pures Modul `meshy.py` (Label-Tabelle, Request-Builder, Task-Parser, Schema) + `MeshyAdapter` in `adapters.py` (Balance-Discovery, POST/Poll/Download). `main.py` ersetzt „type == comfyui" durch „Generierungs-Typ" überall dort, wo kein GPU-Host gemeint ist. Die Konsole bekommt einen Meshy-Backend-Block und einen eigenen, workflow-losen Alias-Editor.

**Tech Stack:** Python 3, FastAPI/Starlette, httpx, stdlib `unittest` (kein pytest im Repo), SQLite-Store (schemalos, keine Migration).

**Spec:** `docs/superpowers/specs/2026-09-02-meshy-backend-design.md` — der Plan argumentiert aus der Spec; Ausführende lesen beide.

## Global Constraints

- Das venv liegt NUR im Haupt-Checkout: immer `/home/dev/projekte/llm-gateway/venv/bin/python` verwenden (im Worktree gibt es kein `venv/`). Kein Test-Runner außer `… -m unittest <modul> -v`; kein Linter, kein Build. Vor jedem Commit: `/home/dev/projekte/llm-gateway/venv/bin/python -m py_compile *.py`.
- `meshy.py` importiert **nie** `main`/`adapters` (pure Leaf wie `anthropic_bridge.py`); `adapters.py` importiert `meshy`.
- Ein Meshy-Backend ist **immer** `paid` (Spec §4.1).
- Öffentliche Labels exakt wie Spec §5: `input_image`, `input_image_front/back/left/right`, `input_name`, `input_face_num`, `input_texture_resolution`, `input_texture_prompt`, `input_pose`; `input_remove_background` und `input_no_fingers` werden angenommen und ignoriert.
- Defaults: `poll_interval` 5 s, `max_wait` 900 s, URL `https://api.meshy.ai`.
- Aliase sind homogen (nur ComfyUI- oder nur Meshy-Kandidaten).
- Bilder an Meshy nur als Base64-Data-URI, nur PNG/JPEG; Download der Ergebnis-URLs **ohne** Bearer-Header.
- Nie `config.yaml`, `store.db`, `secret.key`, `*.db*` committen.
- Commits im Worktree-Branch `worktree-meshy-backend-spec`; Commit-Trailer: `Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>`.

---

### Task 1: `meshy.py` — pures Modul mit Tests

**Files:**
- Create: `meshy.py`
- Create: `test_meshy.py`

**Interfaces:**
- Produces (von Task 2–5 genutzt):
  - `ENDPOINTS: tuple[str, str]`, `AI_MODELS: tuple`, `FORMATS: tuple`, `OPTION_DEFAULTS: dict`, `SLOTS: dict[str, list[str]]`, `IGNORED_PARAMS: tuple`
  - `class MeshyInput(RuntimeError)`
  - `endpoint_of(cand: dict) -> str`, `options_of(cand: dict) -> dict`
  - `texture_res(px) -> str`
  - `data_uri(data: bytes) -> str` (raises `MeshyInput`)
  - `public_fields(cand: dict) -> tuple[list[dict], list[dict]]`
  - `build_request(cand: dict, values: dict, images: dict[str, bytes]) -> dict` (raises `MeshyInput`)
  - `request_summary(body: dict) -> dict`
  - `@dataclass TaskState(status, progress, error, downloads: list[tuple[str, str]], thumbnail: Optional[str], credits: Optional[int])`
  - `parse_task(task: dict, formats: list[str]) -> TaskState` (raises `MeshyInput` bei SUCCEEDED ohne angefordertes Format)
  - `default_candidate(backend: str) -> dict`

- [ ] **Step 1: Failing tests schreiben**

```python
"""Unit tests for meshy.py — run: /home/dev/projekte/llm-gateway/venv/bin/python -m unittest test_meshy -v"""
import base64
import unittest

import meshy

PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 32
JPG = b"\xff\xd8\xff\xe0" + b"\x00" * 32
WEBP = b"RIFF\x00\x00\x00\x00WEBPVP8 " + b"\x00" * 16


def _cand(endpoint="image-to-3d", **opts):
    return {"backend": "meshy", "task": "img2mesh", "model": "latest",
            "meshy": {"endpoint": endpoint, "options": opts}}


class TestTextureRes(unittest.TestCase):
    def test_buckets(self):
        self.assertEqual(meshy.texture_res(1024), "2k")
        self.assertEqual(meshy.texture_res(2048), "2k")
        self.assertEqual(meshy.texture_res(4096), "4k")
        self.assertEqual(meshy.texture_res(8192), "8k")
        self.assertEqual(meshy.texture_res("4k"), "4k")      # already a bucket → verbatim
        self.assertEqual(meshy.texture_res("garbage"), "2k")  # unknown → default


class TestDataUri(unittest.TestCase):
    def test_png_and_jpeg(self):
        self.assertTrue(meshy.data_uri(PNG).startswith("data:image/png;base64,"))
        self.assertTrue(meshy.data_uri(JPG).startswith("data:image/jpeg;base64,"))
        self.assertEqual(base64.b64decode(meshy.data_uri(PNG).split(",", 1)[1]), PNG)

    def test_webp_rejected(self):
        with self.assertRaises(meshy.MeshyInput):
            meshy.data_uri(WEBP)


class TestBuildRequestSingle(unittest.TestCase):
    def test_minimal(self):
        body = meshy.build_request(_cand(), {}, {"input_image": PNG})
        self.assertTrue(body["image_url"].startswith("data:image/png;base64,"))
        self.assertEqual(body["ai_model"], "latest")
        self.assertTrue(body["should_texture"])
        self.assertEqual(body["texture_resolution"], "2k")
        self.assertEqual(body["target_formats"], ["glb"])
        self.assertNotIn("image_urls", body)
        self.assertNotIn("should_remesh", body)        # untouched → Meshy's model default

    def test_missing_image_is_input_error(self):
        with self.assertRaises(meshy.MeshyInput):
            meshy.build_request(_cand(), {}, {})

    def test_face_num_sets_polycount_and_remesh(self):
        body = meshy.build_request(_cand(), {"input_face_num": 20000}, {"input_image": PNG})
        self.assertEqual(body["target_polycount"], 20000)
        self.assertTrue(body["should_remesh"])

    def test_face_num_clamped(self):
        lo = meshy.build_request(_cand(), {"input_face_num": 5}, {"input_image": PNG})
        hi = meshy.build_request(_cand(), {"input_face_num": "999999"}, {"input_image": PNG})
        self.assertEqual(lo["target_polycount"], 100)
        self.assertEqual(hi["target_polycount"], 300000)

    def test_client_params_and_ignored(self):
        body = meshy.build_request(_cand(), {
            "input_name": "x" * 150, "input_texture_resolution": 4096,
            "input_texture_prompt": "rusty metal", "input_pose": "t-pose",
            "input_remove_background": False, "input_no_fingers": True,
            "input_bogus": 1, "prompt": ""}, {"input_image": JPG})
        self.assertEqual(len(body["name"]), 100)
        self.assertEqual(body["texture_resolution"], "4k")
        self.assertEqual(body["texture_prompt"], "rusty metal")
        self.assertEqual(body["pose_mode"], "t-pose")
        for k in ("input_bogus", "input_remove_background", "input_no_fingers", "prompt"):
            self.assertNotIn(k, body)

    def test_bad_pose_is_input_error(self):
        with self.assertRaises(meshy.MeshyInput):
            meshy.build_request(_cand(), {"input_pose": "sitting"}, {"input_image": PNG})

    def test_admin_options_and_model(self):
        c = _cand(enable_pbr=True, topology="quad", ultra_mode=True, target_formats=["glb", "fbx"],
                  texture_resolution="8k")
        c["model"] = "meshy-7"
        body = meshy.build_request(c, {}, {"input_image": PNG})
        self.assertEqual(body["ai_model"], "meshy-7")
        self.assertTrue(body["enable_pbr"])
        self.assertEqual(body["topology"], "quad")
        self.assertTrue(body["ultra_mode"])
        self.assertEqual(body["target_formats"], ["glb", "fbx"])
        self.assertEqual(body["texture_resolution"], "8k")          # admin default …
        body2 = meshy.build_request(c, {"input_texture_resolution": 1024}, {"input_image": PNG})
        self.assertEqual(body2["texture_resolution"], "2k")         # … the client may override

    def test_thumbnail_option_not_sent(self):
        body = meshy.build_request(_cand(thumbnail=False), {}, {"input_image": PNG})
        self.assertNotIn("thumbnail", body)


class TestBuildRequestMulti(unittest.TestCase):
    C = _cand("multi-image-to-3d")

    def test_front_first_optional_dropped(self):
        body = meshy.build_request(self.C, {}, {"input_image_left": JPG, "input_image_front": PNG})
        self.assertEqual(len(body["image_urls"]), 2)
        self.assertTrue(body["image_urls"][0].startswith("data:image/png"))
        self.assertTrue(body["image_urls"][1].startswith("data:image/jpeg"))
        self.assertNotIn("image_url", body)

    def test_all_four_in_order(self):
        imgs = {"input_image_right": PNG, "input_image_back": PNG,
                "input_image_left": PNG, "input_image_front": JPG}
        body = meshy.build_request(self.C, {}, imgs)
        self.assertEqual(len(body["image_urls"]), 4)
        self.assertTrue(body["image_urls"][0].startswith("data:image/jpeg"))

    def test_missing_front_is_input_error(self):
        with self.assertRaises(meshy.MeshyInput):
            meshy.build_request(self.C, {}, {"input_image_back": PNG})

    def test_empty_bytes_count_as_absent(self):
        body = meshy.build_request(self.C, {}, {"input_image_front": PNG, "input_image_back": b""})
        self.assertEqual(len(body["image_urls"]), 1)


class TestPublicFields(unittest.TestCase):
    def test_single(self):
        params, images = meshy.public_fields(_cand())
        self.assertEqual([i["name"] for i in images], ["input_image"])
        self.assertEqual(images[0]["on_empty"], "required")
        self.assertTrue(images[0]["required"])
        names = [p["name"] for p in params]
        for n in ("input_name", "input_face_num", "input_texture_resolution",
                  "input_texture_prompt", "input_pose", "input_remove_background", "input_no_fingers"):
            self.assertIn(n, names)
        pose = next(p for p in params if p["name"] == "input_pose")
        self.assertEqual(pose["choices"], ["", "a-pose", "t-pose"])
        tex = next(p for p in params if p["name"] == "input_texture_resolution")
        self.assertEqual(tex["default"], 2048)
        self.assertEqual(tex["type"], "int")

    def test_multi(self):
        _, images = meshy.public_fields(_cand("multi-image-to-3d"))
        self.assertEqual([i["name"] for i in images],
                         ["input_image_front", "input_image_back", "input_image_left", "input_image_right"])
        self.assertEqual([i["on_empty"] for i in images], ["required", "skip", "skip", "skip"])

    def test_default_from_admin_option(self):
        params, _ = meshy.public_fields(_cand(texture_resolution="4k"))
        tex = next(p for p in params if p["name"] == "input_texture_resolution")
        self.assertEqual(tex["default"], 4096)


class TestParseTask(unittest.TestCase):
    OK = {"id": "t1", "status": "SUCCEEDED", "progress": 100, "consumed_credits": 30,
          "model_urls": {"glb": "https://a/x.glb?e=1", "fbx": "https://a/x.fbx"},
          "thumbnail_url": "https://a/p.png", "task_error": {"message": ""}}

    def test_succeeded(self):
        st = meshy.parse_task(self.OK, ["glb"])
        self.assertEqual(st.status, "SUCCEEDED")
        self.assertEqual(st.downloads, [("glb", "https://a/x.glb?e=1")])
        self.assertEqual(st.thumbnail, "https://a/p.png")
        self.assertEqual(st.credits, 30)
        self.assertIsNone(st.error)

    def test_succeeded_missing_format_is_error(self):
        with self.assertRaises(meshy.MeshyInput):
            meshy.parse_task(self.OK, ["glb", "usdz"])

    def test_failed(self):
        st = meshy.parse_task({"status": "FAILED", "progress": 30,
                               "task_error": {"message": "bad image"}}, ["glb"])
        self.assertEqual(st.status, "FAILED")
        self.assertEqual(st.error, "bad image")
        self.assertEqual(st.downloads, [])

    def test_pending(self):
        st = meshy.parse_task({"status": "PENDING", "progress": 0, "preceding_tasks": 3}, ["glb"])
        self.assertEqual(st.status, "PENDING")
        self.assertEqual(st.downloads, [])
        self.assertIsNone(st.error)


class TestRequestSummary(unittest.TestCase):
    def test_images_replaced(self):
        body = meshy.build_request(_cand("multi-image-to-3d"), {"input_name": "n"},
                                   {"input_image_front": PNG, "input_image_back": JPG})
        s = meshy.request_summary(body)
        self.assertEqual(s["name"], "n")
        self.assertEqual(s["image_urls"], [f"<{len(PNG)} bytes>", f"<{len(JPG)} bytes>"])
        self.assertNotIn("data:", str(s))


class TestDefaultCandidate(unittest.TestCase):
    def test_shape(self):
        c = meshy.default_candidate("meshy-cloud")
        self.assertEqual(c["backend"], "meshy-cloud")
        self.assertEqual(c["task"], "img2mesh")
        self.assertEqual(c["model"], "latest")
        self.assertEqual(c["meshy"]["endpoint"], "image-to-3d")
        self.assertEqual(c["meshy"]["options"], meshy.OPTION_DEFAULTS)


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: Tests laufen lassen — sie müssen fehlschlagen**

Run: `/home/dev/projekte/llm-gateway/venv/bin/python -m unittest test_meshy -v 2>&1 | tail -3`
Expected: `ModuleNotFoundError: No module named 'meshy'`

- [ ] **Step 3: `meshy.py` schreiben**

```python
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
    return {"backend": backend, "task": "img2mesh", "model": "latest",
            "meshy": {"endpoint": ENDPOINTS[0], "options": dict(OPTION_DEFAULTS)}}


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
        {"name": "input_face_num", "type": "int", "default": 30000},
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
```

- [ ] **Step 4: Tests laufen lassen — alle grün**

Run: `/home/dev/projekte/llm-gateway/venv/bin/python -m unittest test_meshy -v 2>&1 | tail -3`
Expected: `OK` (26 Tests)

- [ ] **Step 5: Commit**

```bash
/home/dev/projekte/llm-gateway/venv/bin/python -m py_compile meshy.py test_meshy.py
git add meshy.py test_meshy.py
git commit -m "meshy: pure request/task/schema module for the Meshy backend

Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>"
```

---

### Task 2: `adapters.py` — `MeshyAdapter`, Generierungs-Typen, `cancel()`-Hook, `public_fields`

**Files:**
- Modify: `adapters.py` (Exceptions bei Zeile ~53; `NormalizedRequest` ~153–209; `BackendAdapter` ~274–310; `ComfyUIAdapter` ~2043; Registry ~2845)
- Create: `test_meshy_adapter.py`

**Interfaces:**
- Consumes: `meshy.*` aus Task 1.
- Produces:
  - `class MeshyNoCredits(ConnectionError)`, `class MeshyBusy(ConnectionError)`
  - `BackendAdapter.serves_generation: bool = False`; `async def cancel(self) -> None` (Default no-op); `ComfyUIAdapter.serves_generation = True`, `ComfyUIAdapter.cancel()` = `POST /interrupt`
  - `NormalizedRequest.meshy: Optional[dict] = None`
  - `class MeshyAdapter(BackendAdapter)`: `type = "meshy"`, `serves_generation = True`, Attribute `credits: Optional[int]`, `credits_at: float`; `discover()`, `generate()`
  - `ADAPTERS["meshy"] = MeshyAdapter`; `GEN_TYPES: frozenset[str]`
  - `public_fields(cand: dict) -> tuple[list, list]` (ComfyUI: aus Workflow+Mapping; Meshy: `meshy.public_fields`)

- [ ] **Step 1: Failing test für den Adapter (HTTP-Stub im Thread)**

```python
"""Adapter I/O tests for MeshyAdapter against a local HTTP stub.
run: /home/dev/projekte/llm-gateway/venv/bin/python -m unittest test_meshy_adapter -v"""
import asyncio
import json
import threading
import unittest
from http.server import BaseHTTPRequestHandler, HTTPServer

import httpx

import adapters
import meshy

PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 32
GLB = b"glTF" + b"\x00" * 60


class _Stub(BaseHTTPRequestHandler):
    """Scripted Meshy: POST → id; GET polls walk `script`; assets under /asset/<fmt>."""
    script: list = []          # task objects returned by successive GETs (last one repeats)
    post_status = 200
    posted: list = []
    balance = 120
    seen_auth: list = []

    def log_message(self, *a):  # silence
        pass

    def _json(self, code, obj):
        body = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        _Stub.seen_auth.append((self.path, self.headers.get("Authorization")))
        if self.path == "/openapi/v1/balance":
            return self._json(200, {"balance": _Stub.balance})
        if self.path.startswith("/asset/"):
            self.send_response(200)
            self.send_header("Content-Length", str(len(GLB)))
            self.end_headers()
            return self.wfile.write(GLB)
        if self.path.startswith("/openapi/v1/"):
            t = _Stub.script.pop(0) if len(_Stub.script) > 1 else _Stub.script[0]
            return self._json(200, t)
        self._json(404, {"message": "nope"})

    def do_POST(self):
        n = int(self.headers.get("Content-Length") or 0)
        _Stub.posted.append((self.path, json.loads(self.rfile.read(n) or b"{}")))
        if _Stub.post_status != 200:
            return self._json(_Stub.post_status, {"message": "NoMoreConcurrentTasks"
                                                  if _Stub.post_status == 429 else "no credits"})
        self._json(200, {"result": "task-1"})


def _ctx():
    counts = {"inc": 0, "dec": 0}
    return adapters.AdapterContext(
        auth_headers=lambda b: {}, inflight_inc=lambda bid: counts.__setitem__("inc", counts["inc"] + 1),
        inflight_dec=lambda bid: counts.__setitem__("dec", counts["dec"] + 1),
        cost_usd=lambda *a: 0.0, source_of=lambda r: "test", record_call=lambda *a, **k: None,
        log_enabled=lambda: False), counts


def _task(status, **extra):
    return {"id": "task-1", "status": status, "progress": 50, **extra}


class TestMeshyAdapter(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.srv = HTTPServer(("127.0.0.1", 0), _Stub)
        threading.Thread(target=cls.srv.serve_forever, daemon=True).start()
        cls.url = f"http://127.0.0.1:{cls.srv.server_port}"

    @classmethod
    def tearDownClass(cls):
        cls.srv.shutdown()

    def setUp(self):
        _Stub.script, _Stub.posted, _Stub.seen_auth = [], [], []
        _Stub.post_status, _Stub.balance = 200, 120
        self.ctx, self.counts = _ctx()
        self.backend = {"name": "meshy", "type": "meshy", "url": self.url, "api_key": "msy_test",
                        "poll_interval": 0.01, "max_wait": 2}
        self.ad = adapters.MeshyAdapter(self.backend, self.ctx)

    def _req(self, images=None, values=None, endpoint="image-to-3d"):
        cand = meshy.default_candidate("meshy")
        cand["meshy"]["endpoint"] = endpoint
        return adapters.NormalizedRequest(alias="Meshy-Object", real_model="latest", task="img2mesh",
                                          params=dict(values or {}), upload_images=dict(images or {}),
                                          meshy=cand["meshy"], upload_prefix="gw_j1")

    def _run(self, coro):
        return asyncio.get_event_loop().run_until_complete(coro)

    def test_discover_reports_credits(self):
        caps = self._run(self.ad.discover(httpx.AsyncClient()))
        self.assertIn("latest", caps.models)
        self.assertEqual(self.ad.credits, 120)
        self.assertEqual(_Stub.seen_auth[0][1], "Bearer msy_test")

    def test_discover_zero_credits_is_down(self):
        _Stub.balance = 0
        with self.assertRaises(adapters.MeshyNoCredits):
            self._run(self.ad.discover(httpx.AsyncClient()))

    def test_generate_success(self):
        _Stub.script = [_task("PENDING"), _task("IN_PROGRESS"),
                        _task("SUCCEEDED", progress=100, consumed_credits=30,
                              model_urls={"glb": f"{self.url}/asset/glb"},
                              thumbnail_url=f"{self.url}/asset/png")]
        out = self._run(self.ad.generate(self._req({"input_image": PNG}, {"input_name": "hero"})))
        self.assertEqual([b.name for b in out.blobs], ["model.glb", "preview.png"])
        self.assertEqual(out.blobs[0].mime, "model/gltf-binary")
        self.assertEqual(out.blobs[0].kind, "file")
        self.assertEqual(out.blobs[1].kind, "image")
        self.assertEqual(out.meta["meshy_task_id"], "task-1")
        self.assertEqual(out.meta["consumed_credits"], 30)
        self.assertEqual(out.meta["request"]["name"], "hero")
        self.assertNotIn("data:", json.dumps(out.meta))
        self.assertEqual(_Stub.posted[0][0], "/openapi/v1/image-to-3d")
        self.assertEqual((self.counts["inc"], self.counts["dec"]), (1, 1))
        # asset downloads carry NO bearer (signed URLs on another host)
        self.assertTrue(all(a is None for p, a in _Stub.seen_auth if p.startswith("/asset/")))

    def test_slot_held_does_not_double_count(self):
        _Stub.script = [_task("SUCCEEDED", model_urls={"glb": f"{self.url}/asset/glb"})]
        req = self._req({"input_image": PNG})
        req.slot_held = True
        self._run(self.ad.generate(req))
        self.assertEqual((self.counts["inc"], self.counts["dec"]), (0, 0))

    def test_failed_task_is_final_runtime_error(self):
        _Stub.script = [_task("FAILED", task_error={"message": "bad input"})]
        with self.assertRaises(RuntimeError) as cm:
            self._run(self.ad.generate(self._req({"input_image": PNG})))
        self.assertIn("bad input", str(cm.exception))
        self.assertNotIsInstance(cm.exception, ConnectionError)

    def test_402_fails_over(self):
        _Stub.post_status = 402
        with self.assertRaises(adapters.MeshyNoCredits):
            self._run(self.ad.generate(self._req({"input_image": PNG})))

    def test_429_fails_over(self):
        _Stub.post_status = 429
        with self.assertRaises(adapters.MeshyBusy):
            self._run(self.ad.generate(self._req({"input_image": PNG})))

    def test_timeout_names_task(self):
        _Stub.script = [_task("IN_PROGRESS")]
        self.backend["max_wait"] = 0.05
        with self.assertRaises(TimeoutError) as cm:
            self._run(self.ad.generate(self._req({"input_image": PNG})))
        self.assertIn("task-1", str(cm.exception))

    def test_missing_image_is_input_error_before_post(self):
        with self.assertRaises(meshy.MeshyInput):
            self._run(self.ad.generate(self._req({})))
        self.assertEqual(_Stub.posted, [])


class TestGenTypesAndFields(unittest.TestCase):
    def test_registry(self):
        self.assertIs(adapters.ADAPTERS["meshy"], adapters.MeshyAdapter)
        self.assertEqual(adapters.GEN_TYPES, frozenset({"comfyui", "meshy"}))
        self.assertFalse(adapters.OpenAIAdapter.serves_generation)

    def test_public_fields_meshy(self):
        params, images = adapters.public_fields(meshy.default_candidate("m"))
        self.assertEqual([i["name"] for i in images], ["input_image"])
        self.assertTrue(any(p["name"] == "input_face_num" for p in params))

    def test_public_fields_comfy(self):
        wf = {"1": {"class_type": "LoadImage", "inputs": {"image": "x.png"}},
              "2": {"class_type": "KSampler", "inputs": {"steps": 20, "seed": 1}}}
        mapping = {"image": {"node": "1", "field": "image", "label": "input_image", "on_empty": "required"},
                   "steps": {"node": "2", "field": "steps"},
                   "seed": {"node": "2", "field": "seed"}}
        params, images = adapters.public_fields({"workflow_json": wf, "mapping": mapping})
        self.assertEqual(images, [{"name": "input_image", "on_empty": "required", "required": True}])
        self.assertEqual(params[0], {"name": "steps", "type": "int", "default": 20})
        self.assertEqual(params[1]["auto"], "random unless sent")


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: Test laufen lassen — muss fehlschlagen**

Run: `/home/dev/projekte/llm-gateway/venv/bin/python -m unittest test_meshy_adapter -v 2>&1 | tail -3`
Expected: `AttributeError: module 'adapters' has no attribute 'MeshyAdapter'`

- [ ] **Step 3: Exceptions, `serves_generation`, `cancel()`-Hook, `NormalizedRequest.meshy`**

In `adapters.py` direkt nach `class ComfyExecutorStuck` (Zeile ~53):

```python
class MeshyNoCredits(ConnectionError):
    """Meshy account has no credits (balance 0 on discovery, 402 on submit). A
    ConnectionError on purpose: _GEN_FAILOVER_ERRORS moves the job to the next
    candidate without touching that tuple; _fault_label names it apart."""


class MeshyBusy(ConnectionError):
    """Meshy refused the task with 429 (NoMoreConcurrentTasks / RateLimitExceeded):
    the account's queue limit is full — other API keys of the same account fill it
    too, so the gateway's own max_concurrent cannot rule it out. Failover-class."""
```

In `NormalizedRequest` nach `bypass`:

```python
    meshy: Optional[dict] = None                    # Meshy alias candidate block {endpoint, options}
                                                    # (cand["meshy"]); None on ComfyUI candidates
```

In `BackendAdapter` nach `type: str = "base"`:

```python
    serves_generation: bool = False   # True → the backend is a POST /v1/generations candidate
```

und nach `generate()`:

```python
    async def cancel(self) -> None:
        """Best-effort: stop whatever this backend is running for the gateway (a
        cancelled job). Default no-op — a cloud task API has nothing to interrupt."""
        return None
```

In `ComfyUIAdapter` nach `type = "comfyui"`:

```python
    serves_generation = True
```

und als Methode (neben `restart()`):

```python
    async def cancel(self) -> None:
        """POST /interrupt — frees the GPU of the running prompt (main.cancel_generation)."""
        try:
            async with _pooled_client(self.ctx) as client:
                await client.post(f"{self.backend['url'].rstrip('/')}/interrupt", timeout=5.0)
        except Exception:
            pass
```

- [ ] **Step 4: `MeshyAdapter` schreiben** (vor dem Registry-Block, nach `ComfyUIAdapter`)

```python
# ── Meshy.ai (cloud image → 3D) ───────────────────────────────────────────────

_MESHY_API = "/openapi/v1"
_MESHY_DISCOVERY_TIMEOUT = 8.0
_MESHY_HTTP_TIMEOUT = 30.0
_MESHY_DOWNLOAD_TIMEOUT = 120.0


class MeshyAdapter(BackendAdapter):
    """Meshy.ai task API: POST a task, poll GET …/{id}, download the model urls.
    The request/response SHAPE lives in meshy.py (pure); this class owns the HTTP,
    the in-flight slot and the credit balance seen at discovery."""

    type = "meshy"
    serves_generation = True

    def __init__(self, backend: dict, ctx: AdapterContext):
        super().__init__(backend, ctx)
        self.credits: Optional[int] = None
        self.credits_at: float = 0.0

    def _headers(self) -> dict:
        key = (self.backend.get("api_key") or "").strip()
        return {"Authorization": f"Bearer {key}"} if key else {}

    def _api(self, path: str) -> str:
        return f"{self.backend['url'].rstrip('/')}{_MESHY_API}{path}"

    async def discover(self, client: httpx.AsyncClient) -> Capabilities:
        r = await client.get(self._api("/balance"), headers=self._headers(),
                             timeout=_MESHY_DISCOVERY_TIMEOUT)
        r.raise_for_status()                       # 401 → auth kind in main._classify_error
        bal = int((r.json() or {}).get("balance") or 0)
        self.credits, self.credits_at = bal, time.time()
        if bal <= 0:
            raise MeshyNoCredits("no credits left on the Meshy account")
        return Capabilities(models=set(meshy.AI_MODELS), pricing={}, loras=set())

    async def generate(self, req: NormalizedRequest) -> GenOutput:
        b = self.backend
        cand = {"model": req.real_model, "meshy": req.meshy or {}}
        endpoint = meshy.endpoint_of(cand)
        opts = meshy.options_of(cand)
        body = meshy.build_request(cand, _gen_values(req), req.upload_images or {})   # MeshyInput → final
        poll_interval = float(b.get("poll_interval", 5.0))
        max_wait = float(b.get("max_wait", 900))
        if not req.slot_held:
            self.ctx.inflight_inc(self.bid)
        started = time.monotonic()
        log_on = self.ctx.log_enabled()
        try:
            async with httpx.AsyncClient(timeout=_MESHY_HTTP_TIMEOUT) as client:
                pr = await client.post(self._api(f"/{endpoint}"), json=body, headers=self._headers())
                if pr.status_code == 402:
                    raise MeshyNoCredits(f"Meshy: {_meshy_msg(pr)}")
                if pr.status_code == 429:
                    raise MeshyBusy(f"Meshy queue full: {_meshy_msg(pr)}")
                if pr.status_code >= 500:
                    raise ConnectionError(f"Meshy {pr.status_code}: {_meshy_msg(pr)}")
                if pr.status_code != 200:
                    raise RuntimeError(f"Meshy rejected the task ({pr.status_code}): {_meshy_msg(pr)}")
                task_id = str((pr.json() or {}).get("result") or "")
                if not task_id:
                    raise RuntimeError("Meshy returned no task id")
                if log_on:
                    logger.info(f"→ [{self.name}] meshy {endpoint} task {task_id}")
                state = await self._poll(client, endpoint, task_id, opts["target_formats"],
                                         poll_interval, max_wait)
                blobs = []
                for fmt, url in state.downloads:
                    data = await self._download(client, url)
                    mime, kind = _mime_and_kind(f"model.{fmt}")
                    blobs.append(GenBlob(data=data, mime=mime, kind=kind, name=f"model.{fmt}"))
                if opts.get("thumbnail") and state.thumbnail:
                    try:
                        thumb = await self._download(client, state.thumbnail)
                        blobs.append(GenBlob(data=thumb, mime="image/png", kind="image", name="preview.png"))
                    except Exception as e:           # a preview is a courtesy, the mesh is the job
                        logger.warning(f"[{self.name}] meshy thumbnail download failed: {e}")
        finally:
            if not req.slot_held:
                self.ctx.inflight_dec(self.bid)
        elapsed_ms = int((time.monotonic() - started) * 1000)
        if log_on:
            logger.info(f"← [{self.name}] {len(blobs)} artifact(s) in {elapsed_ms} ms, "
                        f"{state.credits} credits")
        return GenOutput(blobs=blobs, meta={
            "backend": self.name, "meshy_task_id": task_id, "endpoint": endpoint,
            "ai_model": body.get("ai_model"), "request": meshy.request_summary(body),
            "consumed_credits": state.credits, "elapsed_ms": elapsed_ms,
        })

    async def _poll(self, client, endpoint, task_id, formats, poll_interval, max_wait) -> "meshy.TaskState":
        deadline = time.monotonic() + max_wait
        while time.monotonic() < deadline:
            await asyncio.sleep(poll_interval)
            try:
                r = await client.get(self._api(f"/{endpoint}/{task_id}"), headers=self._headers())
            except httpx.HTTPError:
                continue                            # one failed poll is not a verdict
            if r.status_code != 200:
                continue
            state = meshy.parse_task(r.json() or {}, formats)     # MeshyInput → final
            if state.status in ("FAILED", "CANCELED"):
                raise RuntimeError(f"Meshy task {task_id} {state.status.lower()}: {state.error}")
            if state.status == "SUCCEEDED":
                return state
        raise TimeoutError(f"Meshy task {task_id} not finished within max_wait={max_wait:.0f}s "
                           f"(still running at Meshy — fetch it by id from the Meshy dashboard)")

    @staticmethod
    async def _download(client, url: str) -> bytes:
        # NO auth header: signed asset URLs on assets.meshy.ai; the bearer must not leak there.
        r = await client.get(url, timeout=_MESHY_DOWNLOAD_TIMEOUT, follow_redirects=True)
        if r.status_code != 200:
            raise RuntimeError(f"Meshy asset download failed ({r.status_code}) for {url.split('?')[0]}")
        return r.content


def _meshy_msg(r) -> str:
    try:
        return str((r.json() or {}).get("message") or r.text[:200])
    except Exception:
        return r.text[:200]
```

Oben in `adapters.py` bei den Imports: `import meshy`.

- [ ] **Step 5: Registry, `GEN_TYPES`, `public_fields`**

Registry ergänzen:

```python
ADAPTERS: dict[str, type[BackendAdapter]] = {
    "openai": OpenAIAdapter,
    "anthropic": AnthropicAdapter,
    "comfyui": ComfyUIAdapter,
    "meshy": MeshyAdapter,
}

# Backend types that take POST /v1/generations work (main routes generation to these
# and keeps them OUT of the chat catalogs). Derived from the classes, not listed twice.
GEN_TYPES: frozenset = frozenset(t for t, cls in ADAPTERS.items() if cls.serves_generation)
```

`public_fields` (Modulfunktion, neben `image_params`/`slot_empty_mode`) — der ComfyUI-Zweig ist der bisherige Rumpf von `main.gen_alias_schema` (Zeile 3435–3462), unverändert übernommen:

```python
def public_fields(cand: dict) -> tuple[list, list]:
    """The public request fields of a generation alias candidate — what the schema
    endpoint advertises, the playground renders and the OpenAI shims map reference
    images onto. ONE seam for both candidate kinds: a ComfyUI candidate derives them
    from workflow + mapping (labels are the external names), a Meshy candidate from
    its fixed label table (meshy.public_fields)."""
    if cand.get("meshy") is not None:
        return meshy.public_fields(cand)
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
        if cur is not None:
            entry["default"] = cur
        if name == "seed" or (p == "seed" and name == p):
            entry["auto"] = "random unless sent"
        params.append(entry)
    return params, images
```

- [ ] **Step 6: Tests laufen lassen**

Run: `/home/dev/projekte/llm-gateway/venv/bin/python -m py_compile adapters.py && /home/dev/projekte/llm-gateway/venv/bin/python -m unittest test_meshy_adapter test_meshy test_prune_branch test_chain_export_node -v 2>&1 | tail -3`
Expected: `OK`

- [ ] **Step 7: Commit**

```bash
git add adapters.py test_meshy_adapter.py
git commit -m "adapters: MeshyAdapter (balance discovery, task poll, asset download), GEN_TYPES, cancel() hook, public_fields seam

Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>"
```

---

### Task 3: `main.py` — Routing-Audit, Health, Schema, Cancel

**Files:**
- Modify: `main.py` — Zeilen (Stand 5d42ca5): 140–143 (`paid`), 165–171, 480, 667, 829, 987–988, 1100, 1128, 1220, 1317, 1784, 1826, 2166, 2385–2415 (`_fault_label`/`_gen_exhausted_msg`), 2451–2520 (Host-Helfer), 3075–3095 (`cancel_generation`), 3247–3268 (`build_req`), 3350–3378 (`_decode_upload_files`), 3420–3470 (Schema), 3541–3550 (`_gen_image_slots`), 3675–3690 (Dashboard), 3712 (`_comfy_watch_info`), 3730–3745 (`gateway_info`), 3947–3978 (`admin.bind`)

**Interfaces:**
- Consumes: `adapters.GEN_TYPES`, `adapters.MeshyNoCredits`, `adapters.MeshyBusy`, `adapters.public_fields`, `adapter.cancel()`, `MeshyAdapter.credits/credits_at`.
- Produces: `_gen_backends` (ersetzt `_comfy_backends`), `_is_gen(b) -> bool`, `_meshy_info(b) -> dict`, `admin.bind(gen_backends=…)`; `/health` und `gateway_info()` liefern `credits`/`credits_at` für Meshy-Backends.

- [ ] **Step 1: Helfer und `paid`-Normalisierung**

Nahe `backend_id()` (oben in `main.py`):

```python
def _is_gen(b: dict) -> bool:
    """A generation backend (ComfyUI, Meshy): routed by POST /v1/generations, never
    listed in the chat catalogs. Type-agnostic replacement for `type == "comfyui"`."""
    return b.get("type") in adapters.GEN_TYPES
```

Zeile 140–143 (`rebuild_backends`):

```python
    for b in backends:
        # Cost tier for the scheduler (spec 2026-09-01): a paid backend is a candidate
        # only when no unpaid one is free. Normalized here — config and store entries
        # may omit the key entirely. A Meshy backend bills per task, so it is ALWAYS paid.
        b["paid"] = True if b.get("type") == "meshy" else bool(b.get("paid"))
```

- [ ] **Step 2: `_comfy_backends` → `_gen_backends` und die Katalog-Stellen**

`rebuild_route_index()` (987–988):

```python
    global _backend_names, _llm_backends, _gen_backends, _route_index
    _backend_names = {b["name"] for b in backends}
    _llm_backends = [b for b in enabled_backends() if not _is_gen(b)]
    _gen_backends = [b for b in enabled_backends() if _is_gen(b)]
```

Modul-Global-Deklaration von `_comfy_backends` (grep `_comfy_backends` — die Initialisierung oben in der Datei) ebenfalls umbenennen. `_gen_routes` (2164–2166):

```python
    # generation routes only to generation backends (ComfyUI, Meshy), so a name shared
    # with an LLM backend resolves to the right one.
    gen = [b for b in _gen_backends if not is_draining(b)]
    allc = []
    for cand in candidates:
        b = next((b for b in gen if b["name"] == cand.get("backend")), None)
```

Jede der folgenden Stellen: den Vergleich ersetzen, Rest der Zeile unverändert.

| Zeile | alt | neu |
|---|---|---|
| 667 | `and b.get("type") == "comfyui"` | `and _is_gen(b)` |
| 829 | `b.get("type", "openai") == "comfyui" or` | `_is_gen(b) or` |
| 1100 | `if b.get("type", "openai") != "comfyui"` | `if not _is_gen(b)` |
| 1128 | `if b.get("type", "openai") == "comfyui":` | `if _is_gen(b):` |
| 1220 | `and x.get("type", "openai") != "comfyui"` | `and not _is_gen(x)` |
| 1317 | `if b.get("type", "openai") == "comfyui":` | `if _is_gen(b):` |
| 1784 | `(backend.get("type", "openai") == "comfyui" or` | `(_is_gen(backend) or` |
| 1826 | `if b.get("type", "openai") != "comfyui"]` | `if not _is_gen(b)]` |
| 3675 | `if b.get("type") == "comfyui" else calls_1h` | `if _is_gen(b) else calls_1h` |
| 3686 | `is_comfy = lambda b: b.get("type") == "comfyui"` | `is_comfy = _is_gen` (Variable darf so heißen bleiben) |
| 3947 | `if b.get("type", "openai") != "comfyui"]` | `if not _is_gen(b)]` |
| 3969 | `if b.get("type", "openai") != "comfyui"}),` | `if not _is_gen(b)}),` |

**Unverändert lassen** (GPU-Host-Semantik): 169, 480, 2476, 3083-Restart-Logik, 3978 (`backend_loras` nur ComfyUI).

- [ ] **Step 3: Host-Helfer explizit auf ComfyUI gaten**

Am Anfang von `_wait_backend_up`, `_free_comfy_vram`, `_unload_host_llms` je als erste Zeile:

```python
    if backend.get("type") != "comfyui":
        return                      # a cloud task API has no VRAM to free / no host siblings
```

- [ ] **Step 4: `_fault_label` / `_gen_exhausted_msg`**

```python
def _fault_label(e: BaseException) -> str:
    if isinstance(e, adapters.MeshyNoCredits):
        return "no credits left"
    if isinstance(e, adapters.MeshyBusy):
        return "Meshy queue full"
    if isinstance(e, TimeoutError):            # the adapter's own max_wait cap
        return "did not finish in time"
    if isinstance(e, httpx.TimeoutException):  # a single HTTP round trip timed out
        return "timed out mid-request"
    return "connection issue"
```

In `_gen_exhausted_msg` VOR dem `TimeoutError`-Zweig:

```python
    if isinstance(last, adapters.MeshyNoCredits):
        return f"no candidate backend could run it — Meshy account out of credits: {last}"
    if isinstance(last, adapters.MeshyBusy):
        return f"no candidate backend could run it — Meshy queue limit reached: {last}"
```

- [ ] **Step 5: `cancel_generation` über den Adapter-Hook**

```python
async def cancel_generation(job_id: str) -> bool:
    """Cancel a queued/running generation job: best-effort interrupt on the backend
    (adapter.cancel — ComfyUI /interrupt frees the GPU; a cloud task API has nothing
    to stop, Meshy finishes and bills the task), cancel the worker task, mark the job
    failed. Returns False if the job is already finished/unknown."""
    job = await asyncio.to_thread(jobs.get, job_id)
    if not job or job.get("status") not in ("queued", "running"):
        return False
    b = next((x for x in backends if x.get("name") == job.get("backend") and _is_gen(x)), None)
    adapter = backend_adapters.get(backend_id(b)) if b else None
    if adapter is not None:
        await adapter.cancel()
    t = _gen_tasks.get(job_id)
    if t and not t.done():
        t.cancel()
    await asyncio.to_thread(jobs.fail, job_id, "cancelled by user")
    if b:
        asyncio.create_task(_free_comfy_vram(b, "cancel"))     # no-op for non-ComfyUI (step 3)
    return True
```

- [ ] **Step 6: `build_req`, Schema, `_gen_image_slots`, `_decode_upload_files`**

`build_req` (3247): Argument `bypass=(cand.get("bypass") or []),` ergänzen um

```python
            meshy=cand.get("meshy"),                              # Meshy candidate block (None on ComfyUI)
```

`gen_alias_schema` (3420–3470): den Block von `wf = cand.get("workflow_json") or {}` bis einschließlich `params.append(entry)` ersetzen durch

```python
    params, images = adapters.public_fields(cand)
    wf = cand.get("workflow_json") or {}
    mapping = cand.get("mapping") or {}
```

(`kinds = sorted(k for _, k in lora_groups(wf, mapping) if k)` bleibt — ohne Workflow leer.)

`_gen_image_slots` (3541):

```python
    _, cand = routes[0]
    if cand.get("meshy") is not None:
        return [i["name"] for i in adapters.public_fields(cand)[1]]   # labels ARE the params
    return image_params(cand.get("workflow_json") or {}, cand.get("mapping") or {})
```

`_decode_upload_files` (3350): nach `wf, mapping = await asyncio.to_thread(_gen_alias_mapping, alias)`:

```python
    cands = (await asyncio.to_thread(store.get, alias)) if store.is_active() else None
    if cands and cands[0].get("meshy") is not None:
        raise HTTPException(400, f"generation alias '{alias}' runs on Meshy and accepts no `files`"
                                 f" — send images under `images`")
```

- [ ] **Step 7: `/health`, `gateway_info`, `admin.bind`**

Neben `_comfy_watch_info`:

```python
def _meshy_info(b: dict) -> dict:
    """Credit balance seen at the last discovery of a Meshy backend (merged into
    /health + the UI snapshot); {} for every other type."""
    if b.get("type") != "meshy":
        return {}
    ad = backend_adapters.get(backend_id(b))
    if ad is None:
        return {}
    return {"credits": getattr(ad, "credits", None),
            "credits_at": int(getattr(ad, "credits_at", 0) or 0) or None}
```

In `gateway_info()` und in `/health` (beide Backend-Listen, die `**_comfy_watch_info(b)` enthalten — grep bestätigt die Stellen): `**_comfy_watch_info(b), **_meshy_info(b),`.

`admin.bind(...)`: neue Zeile

```python
           gen_backends=lambda: [b for b in backends if _is_gen(b)],
```

- [ ] **Step 8: Compile + Bestandstests + Routing-Smoke**

Run:
```bash
/home/dev/projekte/llm-gateway/venv/bin/python -m py_compile main.py && \
grep -n "_comfy_backends" main.py ; \
/home/dev/projekte/llm-gateway/venv/bin/python -m unittest test_scheduler test_ratelimit_headers test_meshy test_meshy_adapter 2>&1 | tail -2
```
Expected: kein `_comfy_backends`-Treffer mehr in `main.py`; `OK`.

Dann ein Import-Smoke ohne Server: `/home/dev/projekte/llm-gateway/venv/bin/python -c "import main; print(main._is_gen({'type':'meshy'}), main._is_gen({'type':'openai'}))"` → `True False`.

- [ ] **Step 9: Commit**

```bash
git add main.py
git commit -m "main: route generation to every GEN_TYPES backend, Meshy credits in /health, adapter cancel hook, public_fields for schema/slots

Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>"
```

---

### Task 4: `admin.py` — Backends-Tab (Typ, Formularblock, Speichern, Liste)

**Files:**
- Modify: `admin.py` — `_gen_backends`-Binding (~94, `bind()`), `_type_badge` (601), `_backend_form` (880–1023), `_type_select` (1028–1040), `backends_page` (1050–1130), `backend_save` (1324–1395), Routing-Tab (1781–1802)

**Interfaces:**
- Consumes: `adapters.GEN_TYPES`, `gateway_info`-Felder `credits`/`credits_at`, `bind(gen_backends=…)` aus Task 3.
- Produces: `_gen_backends: Callable[[], list]` (modulglobal, per `bind`), `_type_select` mit `meshy`, `#meshyopts`.

- [ ] **Step 1: Binding**

Neben `_comfy_backends: Callable[[], list] = lambda: []` (Zeile 94):

```python
_gen_backends: Callable[[], list] = lambda: []      # every generation backend (ComfyUI + Meshy)
```

`bind()` (Zeile 130) setzt jedes `_<name>`-Global automatisch, sobald es existiert — die Deklaration oben genügt; `main.admin.bind(gen_backends=…)` aus Task 3 bindet dann.

- [ ] **Step 2: `_type_badge` und `_type_select`**

`_type_badge`: vor dem `anthropic`-Zweig

```python
    if t == "meshy":
        return _badge("☁ meshy", "img", "Meshy.ai cloud mesh generation (paid, per task)")
```

`_type_select`:

```python
def _type_select(current: str) -> str:
    """Backend type select that shows/hides the type-specific option blocks on change
    (LLM / ComfyUI / Meshy / Anthropic — the form renders all, only one is ever visible)."""
    opts = "".join(f'<option value="{t}"{" selected" if t == current else ""}>{t}</option>'
                   for t in ("comfyui", "meshy", "openai", "anthropic"))
    return ('<select name="type" onchange="var t=this.value,'
            "l=document.getElementById('llmopts'),c=document.getElementById('comfyopts'),"
            "m=document.getElementById('meshyopts'),a=document.getElementById('anthopts'),"
            "u=document.querySelector('input[name=url]'),p=document.querySelector('input[name=paid]');"
            "if(l)l.style.display=t==='openai'?'':'none';"
            "if(c)c.style.display=t==='comfyui'?'':'none';"
            "if(m)m.style.display=t==='meshy'?'':'none';"
            "if(a)a.style.display=t==='anthropic'?'':'none';"
            "if(t==='meshy'){if(u&&!u.value)u.value='https://api.meshy.ai';if(p){p.checked=true;p.disabled=true}}"
            "else if(p){p.disabled=false}\">" + opts + "</select>")
```

- [ ] **Step 3: `#meshyopts`-Block in `_backend_form`**

Hint-Text unter dem Typ ergänzen: `… <b>meshy</b> = Meshy.ai cloud mesh generation (image / multi-image → 3D), billed per task in credits — always <b>paid</b>.`

Beim `paid`-Checkbox: wenn `g("type") == "meshy"` ist die Box `checked` und `disabled` (die JS-Umschaltung greift nur bei Wechsel). `_checkbox` hat evtl. kein `disabled`-Argument — dann den HTML-String direkt bauen: `<label><input type="checkbox" name="paid" checked disabled> paid — always, Meshy bills per task</label>` für den Meshy-Fall, sonst wie bisher. Ein disabled-Checkbox wird **nicht** gesendet — deshalb setzt `backend_save` (Step 4) `paid` für Meshy selbst.

Nach dem `</div>` des `#comfyopts`-Blocks:

```python
            # Meshy-only options — a cloud task API: no dirs, no watchdog, no self-retry
            + f'<div id="meshyopts" style="{"" if g("type", "openai") == "meshy" else "display:none"}">'
            + '<div class="grouphdr">Meshy</div>'
            + _field("max wait s", _inp("meshy_max_wait", g("max_wait"), placeholder="900", typ="number"))
            + _field("poll interval s", _inp("meshy_poll_interval", g("poll_interval"),
                     placeholder="5", typ="number", step="0.5"))
            + "<p class='hint' style='margin:-4px 0 10px'><b>api key</b> (above) is the Meshy key "
              "(<code>msy_…</code>, Meshy dashboard → API). <b>max_concurrent</b> should stay at or below "
              "your Meshy tier's concurrent-task limit (Pro 10 · Studio 20 · Premium 30 · Ultra 100; "
              "shared by every key of the account) — beyond it Meshy answers 429 and the job fails over. "
              "<b>max wait s</b> caps one task incl. Meshy's own queue (blank = 900); <b>poll interval s</b> "
              "is the gap between task polls (blank = 5). Credits: 20 (no texture) / 30 (textured) / "
              "35 (8K) per Meshy-6/7 task, +5 ultra; refunded when a task fails. The current balance shows "
              "in the backend list after the next health poll.</p>"
            + "</div>"
```

Die Meshy-Felder heißen `meshy_max_wait`/`meshy_poll_interval`, weil `#comfyopts` dieselben Store-Schlüssel unter `max_wait`/`poll_interval` rendert und ein Formular jeden Namen nur einmal führen darf.

- [ ] **Step 4: `backend_save`**

Nach der `poll_interval`-Verarbeitung:

```python
    if new_type == "meshy":
        b["paid"] = True                       # bills per task — never an unpaid candidate
        if not url:
            b["url"] = "https://api.meshy.ai"
        for src, dst, cast in (("meshy_max_wait", "max_wait", int), ("meshy_poll_interval", "poll_interval", float)):
            v = (f.get(src, "") or "").strip()
            try:
                val = cast(float(v))
            except ValueError:
                val = 0
            if val > 0:
                b[dst] = val
            else:
                b.pop(dst, None)               # blank = defaults (900 / 5)
        for k in ("comfy_output_dir", "comfy_input_dir", "auto_restart", "restart_cooldown_s",
                  "stuck_after_s", "self_retries"):
            b.pop(k, None)
```

Achtung: die Validierung `if not name or not url` am Anfang läuft VOR diesem Block — für Meshy ist die URL im Formular vorbelegt; zusätzlich die Prüfung so ändern, dass bei `type == "meshy"` eine leere URL erlaubt ist (`url = url or ("https://api.meshy.ai" if new_type == "meshy" else "")` vor der Prüfung, `new_type` dafür vorziehen).

- [ ] **Step 5: `backends_page`-Liste und Routing-Tab**

In `render(b)`: nach `rst = …`

```python
        cr = (f" · credits {b['credits']}" if b.get("credits") is not None else "")
```

und `cr` in `sub` einfügen (`…{fr}{cr}{rst}{src}`). Gruppierung:

```python
    llm = [b for b in binfo if b.get("type", "openai") not in adapters.GEN_TYPES]
    img = [b for b in binfo if b.get("type") in adapters.GEN_TYPES]
```

Der `⟳`-Restart bleibt `type == "comfyui"`. Zeile 1125/1153 (`shared`) bleibt ComfyUI.

Routing-Tab (1781, 1786, 1800, 1802): `bmeta` und `img_models` mit `in adapters.GEN_TYPES`, `on_llm` mit `not in adapters.GEN_TYPES`.

- [ ] **Step 6: Render-Check**

```bash
/home/dev/projekte/llm-gateway/venv/bin/python -m py_compile admin.py && /home/dev/projekte/llm-gateway/venv/bin/python - <<'EOF'
import admin
html = admin._backend_form({"name": "meshy", "type": "meshy", "url": "https://api.meshy.ai",
                            "paid": True, "max_wait": 900}, [])
assert 'id="meshyopts" style=""' in html and 'id="comfyopts" style="display:none"' in html
assert "meshy_max_wait" in html and 'value="900"' in html
print(admin._type_badge("meshy"))
print("ok")
EOF
```
Expected: `ok` und ein Badge-String mit `meshy`.

- [ ] **Step 7: Commit**

```bash
git add admin.py
git commit -m "ui: meshy backend type in the Backends tab (options block, forced paid, credits in the list)

Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>"
```

---

### Task 5: `admin.py` — Meshy-Alias-Editor, Registrieren, Playground, Job-Ansicht

**Files:**
- Modify: `admin.py` — `_register_form` (2257), `register_post` (2385–2424), `_alias_editor` (2855), `_cand_backends`-Tabelle (~2500–2515, Funktion mit `add_opts`), `cand_add` (3115–3125), Routing-Tab-Zeile 1706, `_playground_form` (3385–3440), Playground-POST-Slotfilter (3596–3600), „Send to Playground" (4386–4400), `job_detail_page` (4225+), Route-Registrierung (~5650)

**Interfaces:**
- Consumes: `meshy.default_candidate`, `meshy.ENDPOINTS/AI_MODELS/FORMATS/TOPOLOGIES/TEXTURE_RES/POSES/OPTION_DEFAULTS`, `meshy.options_of`, `adapters.public_fields`, `_gen_backends()` (Task 4).
- Produces: `_meshy_editor(alias, cands, saved) -> str`, Handler `meshy_update` an `POST /ui/mapping/meshy-update`, `_same_kind(cands, backend) -> bool`.

- [ ] **Step 1: Registrieren ohne Workflow**

`_register_form`: `backend_opts = [b["name"] for b in _gen_backends()] or [("", "(no generation backends)")]`; Hint ergänzen: „For a <b>meshy</b> backend no JSON is needed — the alias is created with Meshy defaults and edited next."

`register_post`: nach `if not alias or not backend: return err(…)`:

```python
    bk = next((b for b in _gen_backends() if b["name"] == backend), None)
    if bk is not None and bk.get("type") == "meshy":
        cand = meshy.default_candidate(backend)
        cand["task"] = task or "img2mesh"
        store.upsert(alias, [cand])
        logger.info(f"ui: registered '{alias}' → {backend} (meshy, no workflow)")
        return RedirectResponse(f"/ui/mapping?edit={alias}", status_code=303)
```

(`import meshy` oben in `admin.py`.)

- [ ] **Step 2: Homogenität bei „Add backend"**

Helfer:

```python
def _same_kind(cands: list, backend_name: str) -> bool:
    """An alias is homogeneous: ComfyUI candidates only or Meshy candidates only (the
    editor, schema and playground read the FIRST candidate as the alias's shape)."""
    want_meshy = bool(cands) and cands[0].get("meshy") is not None
    b = next((x for x in _gen_backends() if x["name"] == backend_name), None)
    return b is not None and ((b.get("type") == "meshy") == want_meshy)
```

In der Backend-Tabelle des Editors (`add_opts = [b["name"] for b in _comfy_backends() if b["name"] not in used]`):
`add_opts = [b["name"] for b in _gen_backends() if b["name"] not in used and _same_kind(cands, b["name"])]`.
In `cand_add`: `valid = {b["name"] for b in _gen_backends() if _same_kind(cands or [], b["name"])}`.

- [ ] **Step 3: `_meshy_editor`**

In `_alias_editor` direkt nach `cand = cands[0]`:

```python
    if cand.get("meshy") is not None:
        return _meshy_editor(alias, cands, saved)
```

Neue Funktion (vor `_alias_editor`); `_backends_table(alias, cands)` ist die bestehende Funktion, die die „allowed backend"-Tabelle rendert (die mit `add_opts` — Name per grep bestätigen und hier einsetzen):

```python
def _meshy_editor(alias: str, cands: list, saved: bool = False) -> str:
    """Editor for a Meshy alias: endpoint, model and the admin option defaults — no
    workflow, no mapping, no pins (the public fields are a fixed table, see meshy.py)."""
    cand = cands[0]
    ep = meshy.endpoint_of(cand)
    opts = meshy.options_of(cand)
    model = cand.get("model") if cand.get("model") in meshy.AI_MODELS else "latest"
    retries = next((c.get("retries") for c in cands if c.get("retries") not in (None, "")), "")
    cur_task = next((c.get("task") for c in cands if c.get("task")), "") or "img2mesh"
    fmts = "".join(f'<label style="margin-right:10px"><input type="checkbox" name="fmt__{f}"'
                   f'{" checked" if f in opts["target_formats"] else ""}> {f}</label>'
                   for f in meshy.FORMATS)
    cb = lambda k, txt: _checkbox(f"opt__{k}", bool(opts.get(k)), txt)
    remesh = {None: "", True: "true", False: "false"}.get(opts.get("should_remesh"), "")
    params, images = meshy.public_fields(cand)
    fields = "".join(f"<tr><td><code>{_esc(i['name'])}</code></td><td>image · {_esc(i['on_empty'])}</td></tr>"
                     for i in images)
    fields += "".join(f"<tr><td><code>{_esc(p['name'])}</code></td><td>{_esc(p['type'])}"
                      f"{' · default ' + _esc(str(p['default'])) if p.get('default') not in (None, '') else ''}"
                      f"{' · ' + '/'.join(_esc(c or 'none') for c in p['choices']) if p.get('choices') else ''}"
                      "</td></tr>" for p in params)
    form = (f'<form action="/ui/mapping/meshy-update" method="post"><input type="hidden" name="alias" value="{_esc(alias)}">'
            f'<div class="formbar"><h2 style="margin:0">{_esc(alias)}</h2>'
            f'{_btn("Save", submit=True)}{_btn("Cancel", "/ui/mapping?sub=media", "secondary")}'
            + ("<span class='ok-chip fade'>✓ Saved</span>" if saved else "") + "</div>"
            + _field("alias name", _inp("new_alias", alias), short=True)
            + _field("task", _task_select(cur_task), short=True)
            + '<h2 style="margin-top:18px">Meshy</h2>'
            + _field("endpoint", _select("meshy_endpoint", list(meshy.ENDPOINTS), ep))
            + "<p class='hint' style='margin:-4px 0 10px'><b>image-to-3d</b> takes <code>input_image</code>; "
              "<b>multi-image-to-3d</b> takes <code>input_image_front</code> (required) plus optional "
              "<code>_back/_left/_right</code> — the same slot names as the Trellis2 multiview alias.</p>"
            + _field("ai model", _select("meshy_model", list(meshy.AI_MODELS), model))
            + _field("texture", cb("should_texture", "should_texture") + cb("enable_pbr", "enable_pbr (PBR maps)"))
            + _field("texture resolution", _select("opt__texture_resolution", list(meshy.TEXTURE_RES), opts["texture_resolution"]))
            + "<p class='hint' style='margin:-4px 0 10px'>Default when the client sends no "
              "<code>input_texture_resolution</code> (≤2048 → 2k, ≤4096 → 4k, else 8k). 4k/8k need Meshy-6+.</p>"
            + _field("topology", _select("opt__topology", list(meshy.TOPOLOGIES), opts["topology"]))
            + _field("remesh", _select("opt__should_remesh", [("", "model default"), ("true", "always"), ("false", "never")], remesh))
            + "<p class='hint' style='margin:-4px 0 10px'>A client <code>input_face_num</code> always turns "
              "remesh on for that request (a polycount needs the remesh pass).</p>"
            + _field("pose", _select("opt__pose_mode", [(p, p or "none") for p in meshy.POSES], opts.get("pose_mode") or ""))
            + _field("input", cb("image_enhancement", "image_enhancement") + cb("remove_lighting", "remove_lighting")
                     + cb("moderation", "moderation"))
            + _field("ultra", cb("ultra_mode", "ultra_mode (+5 credits, Meshy-7 only)"))
            + _field("deliver formats", fmts)
            + _field("thumbnail", cb("thumbnail", "deliver Meshy's preview.png as an extra image artifact"))
            + _field("retries", _inp("retries", str(retries), placeholder="blank = try all backends", typ="number"), short=True)
            + '<h2 style="margin-top:18px">Request fields</h2>'
            + "<p class='hint'>Fixed for Meshy aliases — what <code>GET /v1/generations/{alias}/schema</code> "
              "advertises. <code>input_remove_background</code> / <code>input_no_fingers</code> are accepted and ignored.</p>"
            + f"<table class='pins'><tr><th>name</th><th>type</th></tr>{fields}</table>"
            + "</form>"
            + '<h2 style="margin-top:18px">Backends</h2>' + _backends_table(alias, cands))
    return form
```

Falls `_select` nur `list[str]` oder `list[tuple]` akzeptiert: die Signatur in `admin.py` prüfen (grep `def _select`) und die Aufrufe daran anpassen; gemischte Listen wie oben nur, wenn `_select` Tupel `(value, label)` versteht — sonst zwei Listen.

- [ ] **Step 4: Handler `meshy_update`**

```python
async def meshy_update(request: Request):
    f = await _form(request)
    alias = (f.get("alias", "") or "").strip()
    cands = store.get(alias)
    if not alias or not cands or cands[0].get("meshy") is None:
        raise HTTPException(404, "meshy alias not found")
    ep = (f.get("meshy_endpoint", "") or "").strip()
    model = (f.get("meshy_model", "") or "").strip()
    opts = dict(meshy.OPTION_DEFAULTS)
    for k in ("should_texture", "enable_pbr", "ultra_mode", "image_enhancement",
              "remove_lighting", "moderation", "thumbnail"):
        opts[k] = bool(f.get(f"opt__{k}"))
    tr = (f.get("opt__texture_resolution", "") or "").strip()
    opts["texture_resolution"] = tr if tr in meshy.TEXTURE_RES else "2k"
    tp = (f.get("opt__topology", "") or "").strip()
    opts["topology"] = tp if tp in meshy.TOPOLOGIES else "triangle"
    opts["should_remesh"] = {"true": True, "false": False}.get((f.get("opt__should_remesh", "") or "").strip())
    pm = (f.get("opt__pose_mode", "") or "").strip()
    opts["pose_mode"] = pm if pm in meshy.POSES else ""
    opts["target_formats"] = [x for x in meshy.FORMATS if f.get(f"fmt__{x}")] or ["glb"]
    task = (f.get("task", "") or "").strip()
    retries = (f.get("retries", "") or "").strip()
    for c in cands:
        c["meshy"] = {"endpoint": ep if ep in meshy.ENDPOINTS else meshy.ENDPOINTS[0], "options": opts}
        c["model"] = model if model in meshy.AI_MODELS else "latest"
        c["retries"] = retries
        if task:
            c["task"] = task
    new_alias = (f.get("new_alias", "") or "").strip()
    if new_alias and new_alias != alias and not store.get(new_alias):
        store.delete(alias)
        alias = new_alias
    store.upsert(alias, cands)
    logger.info(f"ui: updated meshy alias '{alias}' ({cands[0]['meshy']['endpoint']}, {cands[0]['model']})")
    return RedirectResponse(f"/ui/mapping?edit={quote(alias)}&saved=1", status_code=303)
```

Route registrieren neben `/ui/mapping/update`:
`app.add_api_route("/ui/mapping/meshy-update", meshy_update, methods=["POST"])`.

- [ ] **Step 5: Routing-Tab-Zeile, Playground, Send-to-Playground, Job-Ansicht**

Zeile 1706: `mapped = ", ".join((c.get("mapping") or {}).keys()) or "auto"` →

```python
            mapped = (f"meshy · {meshy.endpoint_of(c)}" if c.get("meshy") is not None
                      else ", ".join((c.get("mapping") or {}).keys()) or "auto")
```

`_playground_form`: vor der `for p, m in mapping.items()`-Schleife einen Meshy-Zweig, der `rows` aus `adapters.public_fields(cand)` baut und die Schleife überspringt:

```python
    if cand and cand.get("meshy") is not None:
        params, images = adapters.public_fields(cand)
        for i in images:
            extra = (' <span class="badge ok">✓ kept</span> <label class="muted" style="font-weight:normal">'
                     f'<input type="checkbox" name="clear__{_esc(i["name"])}"> clear</label>'
                     if kept and i["name"] in kept else
                     ' <span class="muted">required</span>' if i["required"] else
                     ' <span class="muted">optional · empty → not sent</span>')
            rows += _field(i["name"], f'<input type="file" name="img__{_esc(i["name"])}" accept="image/png,image/jpeg">{extra}')
        for p in params:
            cur = v(p["name"]) or ("" if p.get("default") in (None, "") else str(p["default"]))
            if p.get("choices"):
                rows += _field(p["name"], _select(f"p__{p['name']}", [(c, c or "none") for c in p["choices"]], cur))
            elif p["type"] == "bool":
                rows += _field(p["name"], _checkbox(f"p__{p['name']}", cur.lower() in ("true", "1", "on"), p["name"]))
            else:
                rows += _field(p["name"], _inp(f"p__{p['name']}", cur, typ="number" if p["type"] in ("int", "float") else "text"))
        mapping = {}                       # skip the workflow-driven loop below
```

Playground-POST (3596–3600): `slots = set(adapters.image_params(wf_i, map_i)) if wf_i else set()` →

```python
    if cand and cand.get("meshy") is not None:
        slots = {i["name"] for i in adapters.public_fields(cand)[1]}
    else:
        slots = set(adapters.image_params(wf_i, map_i)) if wf_i else set()
```

Send-to-Playground (4386–4400): nach `mapping = …` — für einen Meshy-Kandidaten ist `ext2param` die Identität:

```python
    cand0 = (store.get(alias) or [{}])[0]
    if cand0.get("meshy") is not None:
        params0, images0 = adapters.public_fields(cand0)
        ext2param = {x["name"]: x["name"] for x in params0 + images0}
```

(die bestehende Schleife danach nur laufen lassen, wenn `ext2param` noch leer ist).

`job_detail_page`: nach der Parameter-/Pins-Tabelle einen Abschnitt, wenn `meta.get("meshy_task_id")`:

```python
    mrq = meta.get("request") if meta.get("meshy_task_id") else None
    meshy_html = ""
    if mrq:
        rows_m = "".join(f"<tr><td><code>{_esc(str(k))}</code></td><td>{_esc(json.dumps(v) if isinstance(v, (list, dict)) else str(v))}</td></tr>"
                         for k, v in mrq.items())
        cr = meta.get("consumed_credits")
        meshy_html = (f"<h3>Meshy <span class='muted' style='font-weight:normal'>· task "
                      f"<code>{_esc(str(meta['meshy_task_id']))}</code> · {_esc(str(meta.get('endpoint') or ''))}"
                      f"{' · ' + str(cr) + ' credits' if cr is not None else ''}</span></h3>"
                      f"<table>{rows_m}</table>")
```

und `meshy_html` an der Stelle in die Seite einfügen, an der `_stage2_section` eingefügt wird (grep `_stage2_section(` im Body von `job_detail_page`).

- [ ] **Step 6: Render-Check**

```bash
/home/dev/projekte/llm-gateway/venv/bin/python -m py_compile admin.py && /home/dev/projekte/llm-gateway/venv/bin/python - <<'EOF'
import asyncio, admin, meshy, store
store.init("/tmp/meshy-plan-check.db")
store.upsert("Meshy-Object", [meshy.default_candidate("meshy")])
html = asyncio.get_event_loop().run_until_complete(admin._alias_editor("Meshy-Object"))
assert "meshy-update" in html and "input_image" in html and "target_formats" not in html
pg = admin._playground_form(["Meshy-Object"], {"model": "Meshy-Object"}, store.get("Meshy-Object")[0])
assert 'name="img__input_image"' in pg and "p__input_face_num" in pg
print("ok")
EOF
rm -f /tmp/meshy-plan-check.db*
```
Expected: `ok`. (Falls `store.init` eine andere Signatur hat: `grep -n "def init" store.py`.)

- [ ] **Step 7: Commit**

```bash
git add admin.py
git commit -m "ui: workflow-less Meshy alias editor, register on a meshy backend, playground/schema/job view via public_fields

Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>"
```

---

### Task 6: Dokumentation

**Files:**
- Modify: `README.md` (Abschnitt „Media generation" ab Zeile 566; Endpunkt-Tabelle ~868), `config.example.yaml` (Generation-Block ab 155), `docs/mesh-client-spec.md` (§3.2 Familien-Tabelle, Zeile ~142), `CLAUDE.md` (Architektur-Liste)

- [ ] **Step 1: README**

Nach dem ComfyUI-YAML-Block einen Unterabschnitt:

```markdown
### Meshy.ai (cloud mesh generation)

A Meshy backend (`type: meshy`, https://docs.meshy.ai) serves **image → 3D** and
**multi-image → 3D** through the same `POST /v1/generations` API as a ComfyUI mesh
alias — same `input_*` labels, same image slots, same job endpoints. It is always a
**paid** backend: the scheduler reaches for it only when no unpaid backend is free.

```yaml
backends:
  - name: meshy
    type: meshy
    url: https://api.meshy.ai
    api_key: msy_…              # Meshy dashboard → API
    max_concurrent: 4           # ≤ your tier's concurrent-task limit (Pro 10, Studio 20, …)
    # poll_interval: 5          # seconds between task polls
    # max_wait: 900             # cap for one task incl. Meshy's queue
```

Register an alias on the Meshy backend in **Mapping › Media** (no workflow JSON) and
pick the endpoint: `image-to-3d` takes `images.input_image`; `multi-image-to-3d`
takes `images.input_image_front` (required) plus optional `input_image_back`,
`input_image_left`, `input_image_right` — the slot names of the Trellis2 multiview
alias, so a client can switch by changing `model` only. Client params: `input_name`,
`input_face_num` (→ `target_polycount` + remesh), `input_texture_resolution` (pixels →
2k/4k/8k), `input_texture_prompt`, `input_pose` (`a-pose`/`t-pose`);
`input_remove_background` and `input_no_fingers` are accepted and ignored. Textures,
PBR, topology, ultra mode, delivered formats and the preview thumbnail are alias
defaults set by the admin. The job records what was sent (`meta.request`), the Meshy
task id and `consumed_credits`; `/health` and the Backends tab show the credit balance
(0 credits = backend down with that reason). A failed Meshy task is final (credits are
refunded by Meshy); 402/429 fail over to the next candidate; a cancel stops the
gateway's job but Meshy finishes and bills the task.
```

In der Backend-Typ-Aufzählung (wo `openai`/`anthropic`/`comfyui` genannt werden, grep `type: comfyui` im README-Kopf) `meshy` ergänzen.

- [ ] **Step 2: `config.example.yaml`**

Unter dem ComfyUI-Backend-Beispiel:

```yaml
#   - name: meshy
#     type: meshy                     # Meshy.ai cloud mesh generation — always paid
#     url: https://api.meshy.ai
#     api_key: msy_...
#     max_concurrent: 4               # ≤ Meshy tier's concurrent-task limit
#     poll_interval: 5
#     max_wait: 900
#
# A Meshy alias carries no workflow — endpoint + admin option defaults instead
# (edit in the console; the option keys are meshy.OPTION_DEFAULTS):
#   "Meshy-Multiview":
#     - backend: meshy
#       task: img2mesh
#       model: latest                 # ai_model: latest | meshy-7 | meshy-6 | meshy-5
#       meshy:
#         endpoint: multi-image-to-3d # or image-to-3d (input_image)
#         options: {should_texture: true, enable_pbr: false, texture_resolution: "2k",
#                   topology: triangle, target_formats: [glb], thumbnail: true}
```

- [ ] **Step 3: `docs/mesh-client-spec.md`**

Familien-Tabelle in §3.2 um eine Zeile ergänzen und danach einen Absatz:

```markdown
| `Meshy-Object`, `Meshy-Multiview` | 30000 | 2048 | Cloud (Meshy.ai, bezahlt pro Task, nur als Fallback oder gezielt). `-Multiview` nimmt `input_image_front` (Pflicht) + optional `_back/_left/_right` wie `Trellis2-Multiview`. Zusätzlich `input_texture_prompt` (string) und `input_pose` (`a-pose`/`t-pose`). `input_remove_background`/`input_no_fingers` werden angenommen, wirken nicht. Kein `files`-Upload (`400`). Liefert `model.glb` (Texturen eingebettet) + `preview.png`. |
```

- [ ] **Step 4: `CLAUDE.md`**

In der Architektur-Liste nach dem `adapters.py`-Absatz einen Eintrag:

```markdown
- **`meshy.py`** — pure half of the Meshy.ai backend (`type: meshy`, `MeshyAdapter` in
  `adapters.py`): the fixed `input_*` label table → Meshy request body
  (`build_request`), task-object parsing (`parse_task`), the advertised schema
  (`public_fields`) and `default_candidate`. No `main`/`adapters` imports; covered by
  `test_meshy.py` + `test_meshy_adapter.py` (HTTP stub). A Meshy alias candidate has
  no workflow — `cand["meshy"] = {endpoint, options}`; `adapters.public_fields(cand)`
  is the ONE seam schema/playground/shims read for both candidate kinds.
  `adapters.GEN_TYPES` (types whose adapter sets `serves_generation`) replaces
  `type == "comfyui"` in `main` wherever "generation backend" is meant; the GPU-host
  sites (`/free`, `/interrupt` via `adapter.cancel()`, restart, watchdog) stay ComfyUI.
  A Meshy backend is always `paid`; discovery = `GET /openapi/v1/balance` (0 → DOWN
  "no credits"); 402/429 raise `MeshyNoCredits`/`MeshyBusy` (ConnectionError
  subclasses → failover, named by `_fault_label`); a FAILED task is final.
```

Ebenso im `Eleven self-contained Python files`-Satz die Zahl auf zwölf setzen und `meshy` in die Klammer-Liste aufnehmen.

- [ ] **Step 5: Commit**

```bash
git add README.md config.example.yaml docs/mesh-client-spec.md CLAUDE.md
git commit -m "docs: Meshy backend type (README, config example, mesh client spec, CLAUDE.md)

Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>"
```

---

### Task 7: End-to-End gegen den Stub und Compile-Gate

**Files:**
- Create: `/home/dev/.claude/jobs/797a04b5/tmp/meshy_e2e.py` (Wegwerf-Skript, nicht committen)

- [ ] **Step 1: Gesamte Test-Suite und Compile-Gate**

```bash
/home/dev/projekte/llm-gateway/venv/bin/python -m py_compile *.py && \
/home/dev/projekte/llm-gateway/venv/bin/python -m unittest test_meshy test_meshy_adapter test_scheduler test_prune_branch \
  test_chain_export_node test_ratelimit_headers test_anthropic_bridge 2>&1 | tail -3
```
Expected: `OK`.

- [ ] **Step 2: Gateway-Instanz mit Stub als Backend**

Isolierte Instanz nach dem Muster der Memory-Notiz „Repro-Harness: Stub-Instanz" (Symlink-Verzeichnis, eigene `config.yaml`, eigene DB-Pfade). Stub = die `_Stub`-Klasse aus `test_meshy_adapter.py` in einem eigenen Prozess mit Script `[PENDING, SUCCEEDED{glb+thumbnail}]` auf Port 8799; `config.yaml`:

```yaml
api_key: test
backends:
  - name: meshy
    type: meshy
    url: http://127.0.0.1:8799
    api_key: msy_stub
    max_concurrent: 2
    poll_interval: 0.2
    max_wait: 30
```

Alias per Store anlegen (`store.upsert("Meshy-Multiview", [{**meshy.default_candidate("meshy"), "meshy": {"endpoint": "multi-image-to-3d", "options": meshy.OPTION_DEFAULTS}}])`) — oder über die Konsole `Mapping › Media › Register` mit Backend `meshy`.

Prüfen:

```bash
curl -s localhost:4100/health | python3 -c "import json,sys; b=[x for x in json.load(sys.stdin)['backends'] if x['type']=='meshy'][0]; print(b['healthy'], b['paid'], b.get('credits'))"
# → True True 120
curl -s -H "Authorization: Bearer test" localhost:4100/v1/generations/Meshy-Multiview/schema | python3 -m json.tool | head -40
# → images: input_image_front required, back/left/right skip; params incl. input_pose choices
PNG=$(python3 -c "import base64;print(base64.b64encode(b'\x89PNG\r\n\x1a\n'+b'\0'*32).decode())")
curl -s -H "Authorization: Bearer test" -H "Content-Type: application/json" localhost:4100/v1/generations \
  -d "{\"model\":\"Meshy-Multiview\",\"mode\":\"sync\",\"params\":{\"input_name\":\"hero\",\"input_face_num\":20000},\"images\":{\"input_image_front\":\"$PNG\",\"input_image_back\":\"$PNG\"}}" | python3 -m json.tool
# → status done, results[0] model.glb (model/gltf-binary, kind file), results[1] preview.png,
#   meta.request.image_urls == ["<40 bytes>","<40 bytes>"], meta.consumed_credits, meta.meshy_task_id
curl -s -H "Authorization: Bearer test" -H "Content-Type: application/json" localhost:4100/v1/generations \
  -d "{\"model\":\"Meshy-Multiview\",\"images\":{\"input_image_back\":\"$PNG\"}}"
# → 502 with "input_image_front is required" (final content error, one attempt)
curl -s -H "Authorization: Bearer test" -H "Content-Type: application/json" localhost:4100/v1/generations \
  -d "{\"model\":\"Meshy-Multiview\",\"files\":{\"x\":\"$PNG\"}}"
# → 400 "… accepts no `files`"
```

Stub-Script auf `post_status = 402` umstellen → Job `failed`, Fehlertext „no credits"; Stub `balance = 0` → nächster Health-Tick zeigt das Backend DOWN mit `no credits left`.

Konsole (`/ui`): Backends-Tab zeigt „☁ meshy", `credits 120`, Formular mit Meshy-Block; Mapping › Media zeigt den Meshy-Editor; Playground rendert Upload-Felder `input_image_front…` und den `input_pose`-Dropdown; Jobs › Media zeigt Task-ID, Request-Tabelle, Credits.

- [ ] **Step 3: Ergebnis notieren**

Abweichungen oder Fehler aus Step 2 fixen und committen (Fix-Commits pro Datei, Trailer wie oben). Keine Deploy-Aktion — Deploy auf `.10` ist ein eigener Auftrag (Prod-Stand vorher prüfen, siehe Memory).

---

## Self-Review (durchgeführt beim Schreiben)

- **Spec-Abdeckung:** §4.1 Backend-Felder → Task 3 (paid) + 4 (Formular); §4.2 Kandidat → Task 1 (`default_candidate`) + 5; §5 Tabelle + Schema → Task 1 + 3; §6 Modul → Task 1; §7.1 Adapter → Task 2; §7.2 Timing → Task 2 Defaults + Task 4 Hints; §7.3 Audit → Task 3 Step 2/3; §7.4 Seam → Task 2 (`public_fields`) + Task 3 + 5; §8 Konsole → Task 4 + 5; §9 Fehlerbild → Task 2 (Exceptions, Tests) + Task 3 (`_fault_label`); §10 Tests → Task 1, 2, 7; §12 Dateien → alle Tasks, Doku Task 6.
- **Platzhalter:** keine offenen „TBD"; zwei Stellen verlangen einen Grep (Name der Backends-Tabellen-Funktion in Task 5 Step 3, `_select`-Signatur) — mit dem exakten Suchbefehl benannt.
- **Typ-Konsistenz:** `public_fields` liefert überall `(params, images)` mit `images[i]["name"/"on_empty"/"required"]`; `NormalizedRequest.meshy` ist `cand["meshy"]` (Dict mit `endpoint`/`options`), der Adapter baut daraus `{"model": req.real_model, "meshy": …}` für `meshy.build_request`; `MeshyNoCredits`/`MeshyBusy` sind `ConnectionError`-Unterklassen in `adapters`, nicht in `meshy` (dort nur `MeshyInput`).
