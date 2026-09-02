# Meshy Phase 2 — Chain-Hooks und Meshy-Rigging Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Geriggte NPCs aus Meshy-Meshes — Meshy als Chain-Stufe 1 vor dem lokalen Rigger (`mesh-mia`) und Meshys Rigging-API als eigener Alias `Meshy-Rig`, nutzbar standalone und als Stufe 2 hinter Meshy oder ComfyUI.

**Architecture:** `_run_chain` verliert seine drei ComfyUI-Sonderstellen an drei Adapter-Hooks (`chain_export`, `chain_take_mesh`, `chain_feed_mesh`); ComfyUI implementiert sie mit dem heutigen Code, Meshy mit Blob-Übernahme und Data-URI-Upload. `meshy.py` bekommt den Endpoint `rigging`; `public_fields` liefert ein Tripel `(params, images, files)`, das Schema, Playground und Editor gemeinsam lesen.

**Tech Stack:** Python 3.12, FastAPI, httpx, stdlib unittest (kein pytest). venv NUR im Haupt-Checkout: `/home/dev/projekte/llm-gateway/venv/bin/python`.

**Spec:** `docs/superpowers/specs/2026-09-02-meshy-chain-rig-design.md` (Phase 1: `2026-09-02-meshy-backend-design.md`).

## Global Constraints

- Arbeitsverzeichnis ist der Worktree `/home/dev/projekte/llm-gateway/.claude/worktrees/meshy-backend-spec` (Branch `worktree-meshy-backend-spec`); nie in den Haupt-Checkout wechseln.
- Vor jedem Commit: `/home/dev/projekte/llm-gateway/venv/bin/python -m py_compile *.py` und alle Suiten mit `-W error::ResourceWarning`: `test_meshy test_meshy_adapter test_gen_backend_for test_playground_files test_scheduler test_prune_branch test_chain_export_node test_ratelimit_headers test_anthropic_bridge` (+ neue).
- `meshy.py` importiert nie `main`/`adapters`; `adapters.py` importiert `meshy`; `jobs.py` bleibt dependency-frei.
- Verhalten bestehender ComfyUI-Chains (`Trellis2-Humanoid-* → mesh-mia`, `*-Generic → mesh-rig-unirig`, `relay: path` und `upload`) muss byte-identisch bleiben: gleiche Pins, gleicher `mesh_name` (`gwchain_<job>_00001_.<ext>`), gleicher `mesh_ref`, gleiches `meta.chain_stage2`.
- Ist Stufe 1 ODER Stufe 2 ein Meshy-Kandidat, ist das Relay immer `upload` (Bytes), unabhängig vom gespeicherten Wert.
- Meshy-Stufe-1-Mesh: der Blob `model.glb` aus `out1.blobs`; `mesh_name = f"gwchain_{job_id}.glb"`; ohne `glb` in `target_formats` scheitert der Job VOR dem Start, namentlich.
- Meshy-Stufe 2 erhält das Mesh als `req2.upload_files[mesh_param] = (mesh_name, bytes)` und bettet es als Data-URI (`model/gltf-binary`) in `model_url` ein; `mesh_ref` im Job-Meta lautet `<upload:gwchain_<job>.glb (N.N MB)>`.
- Rigging-Endpoint: `POST /openapi/v1/rigging` mit `{model_url, height_meters, name?}`; Ergebnis-URLs aus `task["result"]` (`rigged_character_glb_url`, `rigged_character_fbx_url`, `basic_animations.walking_glb_url`, `walking_fbx_url`, `running_glb_url`, `running_fbx_url`); Blob-Namen `rigged.glb`, `rigged.fbx`, `walking.<fmt>`, `running.<fmt>`; 5 Credits; nur Bipeds; Meshy antwortet 200 (der 2xx-Check aus 4735c7d deckt es).
- Öffentliche Felder Rigging: `files.input_mesh_path` (required, glb), `params.input_name`, `params.input_height_m` (float, Default 1.7), `input_no_fingers` angenommen/ignoriert. Kein `input_task_id`.
- Rig-Wert `meshy` (neben `generic`, `mixamo`): keine `normalize_delivery`/`validate_delivery` (Texturen eingebettet), `meta.rig = "meshy"`.
- Aliase sind homogen (ein Alias = ein Backend-Kind); ein Successor darf das ANDERE Kind sein.
- Nie committen: `config.yaml`, `*.db*`, `secret.key`, `jobs/`, `.superpowers/`. Commit-Trailer: `Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>`.

---

### Task 1: Chain-Hooks auf dem Adapter — verhaltensneutraler Refactor von `_run_chain`

**Files:**
- Modify: `adapters.py` (`BackendAdapter` ~289–330; `ComfyUIAdapter` bei `export_pin`/`export_node_error`/`pinned_output_name`/`fetch_output`/`upload_input` ~2300–2370)
- Modify: `main.py` `_run_chain` (2655–3110): die Blöcke „export node / ext / mesh_name" (2868–2897), `req1.fixed` (2925), `fetch_output` (2963–2968), Hand-off `mesh_ref` (3001–3009)
- Create: `test_chain_hooks.py`

**Interfaces:**
- Produces: `adapters.ChainExport(mesh_name: str, extra_fixed: list, error: Optional[str])`; `BackendAdapter.chain_export(cand, succ, params, prefix) -> ChainExport`; `async BackendAdapter.chain_take_mesh(out, export, want_bytes) -> Optional[bytes]`; `async BackendAdapter.chain_feed_mesh(req2, backend2, mesh_param, mesh_name, mesh_bytes, outdir) -> str`.

- [ ] **Step 1: Failing tests**

```python
"""Chain hooks: what a stage-1 / stage-2 adapter contributes to a workflow chain.
run: /home/dev/projekte/llm-gateway/venv/bin/python -m unittest test_chain_hooks -v"""
import asyncio
import unittest

import adapters
from adapters import ComfyUIAdapter, GenBlob, GenOutput, NormalizedRequest

WF = {
    "47": {"inputs": {"value": "Kai"}, "class_type": "PrimitiveString", "_meta": {"title": "input_name"}},
    "100": {"inputs": {"filename_prefix": ["47", 0], "file_format": "glb", "trimesh": ["107", 0]},
            "class_type": "Trellis2ExportMesh", "_meta": {"title": "Output"}},
}


def _ctx():
    return adapters.AdapterContext(
        auth_headers=lambda b: {}, inflight_inc=lambda bid: None, inflight_dec=lambda bid: None,
        cost_usd=lambda *a: 0.0, source_of=lambda r: "test", record_call=lambda *a, **k: None,
        log_enabled=lambda: False)


def _comfy():
    return ComfyUIAdapter({"name": "gpu", "type": "comfyui", "url": "http://127.0.0.1:1",
                           "comfy_input_dir": "/srv/comfy/input"}, _ctx())


class ComfyChainExport(unittest.TestCase):
    def test_pins_export_node_and_names_mesh(self):
        ex = _comfy().chain_export({"workflow_json": WF}, {"export_node": "100"}, {}, "gwchain_j1")
        self.assertIsNone(ex.error)
        self.assertEqual(ex.mesh_name, "gwchain_j1_00001_.glb")
        self.assertEqual(ex.extra_fixed, [{"node": "100", "field": "filename_prefix", "value": "gwchain_j1"}])

    def test_mapped_file_format_overrides_ext(self):
        cand = {"workflow_json": WF, "mapping": {"fmt": {"node": "100", "field": "file_format", "label": "input_format"}}}
        ex = _comfy().chain_export(cand, {"export_node": "100"}, {"input_format": "obj"}, "gwchain_j1")
        self.assertEqual(ex.mesh_name, "gwchain_j1_00001_.obj")

    def test_pinned_file_format_beats_mapping(self):
        cand = {"workflow_json": WF,
                "mapping": {"fmt": {"node": "100", "field": "file_format"}},
                "fixed": [{"node": "100", "field": "file_format", "value": "fbx"}]}
        ex = _comfy().chain_export(cand, {"export_node": "100"}, {"fmt": "obj"}, "gwchain_j1")
        self.assertEqual(ex.mesh_name, "gwchain_j1_00001_.fbx")

    def test_bad_export_node_is_an_error_not_a_crash(self):
        ex = _comfy().chain_export({"workflow_json": WF}, {"export_node": "47"}, {}, "gwchain_j1")
        self.assertIsNotNone(ex.error)
        self.assertIn("filename_prefix", ex.error)

    def test_no_workflow_is_an_error(self):
        ex = _comfy().chain_export({}, {"export_node": "100"}, {}, "gwchain_j1")
        self.assertIsNotNone(ex.error)


class ComfyChainFeed(unittest.TestCase):
    def test_path_relay_returns_shared_disk_path(self):
        req2 = NormalizedRequest(alias="rig")
        ref = asyncio.run(_comfy().chain_feed_mesh(req2, {"name": "gpu"}, "input_mesh_path",
                                                   "gwchain_j1_00001_.glb", None, "/srv/comfy/output"))
        self.assertEqual(ref, "/srv/comfy/output/gwchain_j1_00001_.glb")
        self.assertEqual(req2.upload_files, {})


class BaseDefaults(unittest.TestCase):
    def test_base_adapter_refuses_chain_roles(self):
        base = adapters.OpenAIAdapter({"name": "llm", "type": "openai", "url": "http://x"}, _ctx())
        ex = base.chain_export({}, {}, {}, "p")
        self.assertIsNotNone(ex.error)
        self.assertIn("openai", ex.error)
        with self.assertRaises(RuntimeError):
            asyncio.run(base.chain_feed_mesh(NormalizedRequest(), {}, "m", "n", b"x", ""))
        self.assertIsNone(asyncio.run(base.chain_take_mesh(GenOutput(blobs=[]), ex, True)))


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: Run — fails with `AttributeError: ... has no attribute 'chain_export'`**

- [ ] **Step 3: Hooks in `adapters.py`**

Nach `GenOutput`:

```python
@dataclass
class ChainExport:
    """What a stage-1 adapter contributes before the chain runs: the name the mesh
    will have (`mesh_name`), pins the stage-1 request needs (`extra_fixed`) and, when
    the candidate cannot be a stage 1 as configured, a NAMED `error` — the chain fails
    the job with it before any GPU-minutes or credits are spent."""
    mesh_name: str
    extra_fixed: list = field(default_factory=list)
    error: Optional[str] = None
```

Auf `BackendAdapter` (nach `cancel`):

```python
    # ── workflow chains (main._run_chain) — the three places a stage is backend-specific ──
    def chain_export(self, cand: dict, succ: dict, params: dict, prefix: str) -> ChainExport:
        """Stage 1: how this backend will name/export the mesh. Default: not a stage 1."""
        return ChainExport("", error=f"a {self.type} backend cannot be a chain stage 1")

    async def chain_take_mesh(self, out: GenOutput, export: ChainExport, want_bytes: bool) -> Optional[bytes]:
        """Stage 1: the produced mesh (bytes; b'' when only existence is asked for), None if absent."""
        return None

    async def chain_feed_mesh(self, req2: NormalizedRequest, backend2: dict, mesh_param: str,
                              mesh_name: str, mesh_bytes: Optional[bytes], outdir: str) -> str:
        """Stage 2: put the mesh where THIS backend reads it and return the `mesh_ref`
        recorded on the job (a path, or a marker for an embedded upload)."""
        raise RuntimeError(f"a {self.type} backend cannot be a chain stage 2")
```

`ComfyUIAdapter` (neben `restart`/`export_pin`) — die Logik ist 1:1 aus `main._run_chain` 2868–2897 und 3001–3009 verschoben:

```python
    def chain_export(self, cand: dict, succ: dict, params: dict, prefix: str) -> ChainExport:
        wf = cand.get("workflow_json") or {}
        node = str(succ.get("export_node") or "").strip()
        why = self.export_node_error(wf, node)
        if why:
            return ChainExport("", error=why)
        # The extension is what ComfyUI will WRITE: the node's file_format as applied —
        # a mapped request param overrides the workflow value, an admin pin beats both.
        ext = str(((wf.get(node) or {}).get("inputs") or {}).get("file_format") or "glb")
        for p, m in (cand.get("mapping") or {}).items():
            m = m or {}
            if str(m.get("node")) == node and m.get("field") == "file_format":
                v = params.get(p)
                if v in (None, "") and (m.get("label") or "").strip():
                    v = params.get((m.get("label") or "").strip())
                if v not in (None, ""):
                    ext = str(v)
        for fx in (cand.get("fixed") or []):
            if (str(fx.get("node")) == node and fx.get("field") == "file_format"
                    and fx.get("value") not in (None, "")):
                ext = str(fx["value"])
        return ChainExport(self.pinned_output_name(prefix, ext), [self.export_pin(node, prefix)])

    async def chain_take_mesh(self, out: GenOutput, export: ChainExport, want_bytes: bool) -> Optional[bytes]:
        return await self.fetch_output(export.mesh_name, want_bytes=want_bytes)

    async def chain_feed_mesh(self, req2, backend2, mesh_param, mesh_name, mesh_bytes, outdir) -> str:
        if mesh_bytes is None:                       # path relay: shared disk, absolute path
            return f"{outdir}/{mesh_name}"
        return input_path_ref(backend2, await self.upload_input(mesh_bytes, mesh_name))
```

- [ ] **Step 4: `_run_chain` auf die Hooks umstellen (main.py)**

Ersetze 2868–2897 (von `s1_wf = …` bis `mesh_name = adapter.pinned_output_name(prefix, ext)`) durch:

```python
            s1_wf = stage1_cand.get("workflow_json") or {}
            # Stage-1 export is backend-specific (ComfyUI pins an export node; Meshy
            # delivers the mesh as a blob) — the adapter decides, and a candidate that
            # cannot export as configured is refused HERE, before GPU-minutes/credits.
            export = adapter.chain_export(stage1_cand, succ, params, prefix)
            if export.error:
                await asyncio.to_thread(jobs.fail, job_id, export.error)
                return
            mesh_name = export.mesh_name
```

`req1`: `fixed=list(stage1_cand.get("fixed") or []) + list(export.extra_fixed),` und `meshy=stage1_cand.get("meshy"),`.

Ersetze `mesh = await adapter.fetch_output(mesh_name, want_bytes=need_bytes)` durch `mesh = await adapter.chain_take_mesh(out1, export, need_bytes)`.

Hand-off: `req2` wird VOR dem Feed gebaut (verschiebe den `req2 = NormalizedRequest(...)`-Block vor die `if cross:`-Verzweigung, mit `upload_files={}` und `meshy=s2.get("meshy")`), dann ersetzen die drei `mesh_ref`-Zuweisungen zu:

```python
                    mesh_ref = await adapter2.chain_feed_mesh(req2, backend2, mesh_param, mesh_name, mesh_bytes, outdir)
```
(cross und same-backend-upload: `mesh_bytes` gesetzt; path: `mesh_bytes is None`, `outdir` gesetzt). `s2_params[mesh_param] = mesh_ref` und `req2.params = s2_params` danach setzen (req2 wurde vorher mit `params={}` gebaut → nach dem Feed `req2.params = s2_params` zuweisen).

- [ ] **Step 5: Tests + Compile; Chain-Verhalten prüfen**

`test_chain_hooks`, `test_chain_export_node`, alle Suiten. Zusätzlich ein Diff-Blick: `git diff main.py` darf keine Zeile ändern, die den `mesh_name`, die Pins oder den `mesh_ref` für ComfyUI anders berechnet.

- [ ] **Step 6: Commit** — `chain: stage export/take/feed become adapter hooks (ComfyUI behaviour unchanged)`

---

### Task 2: Meshy als Chain-Stufe 1 (→ `mesh-mia`)

**Files:**
- Modify: `adapters.py` `MeshyAdapter` (chain_export, chain_take_mesh); `main.py` `_run_chain` (Relay-Erzwingung, `chain_stage1`-Meta, Rig-Gating), `run_generation` (Guard entfernen: der Block mit `runs on Meshy and cannot be a`); `admin.py` `_meshy_editor` + `meshy_update` (Successor), `_chain_section` (Rig-Platzhalter), `job_detail_page` (Stufe-1-Meshy-Tabelle)
- Test: `test_chain_hooks.py`

**Interfaces:**
- Consumes: Task 1 Hooks.
- Produces: `meta.chain_stage1 = {"backend", "meshy_task_id", "endpoint", "consumed_credits", "request"}` wenn Stufe 1 Meshy war; `successor` auf Meshy-Kandidaten `{alias, mesh_param, keep_from_mesh?, rig?}` (kein export_node, kein relay).

- [ ] **Step 1: Failing tests** (an `test_chain_hooks.py` anhängen)

```python
class MeshyChainStage1(unittest.TestCase):
    def _ad(self):
        return adapters.MeshyAdapter({"name": "meshy", "type": "meshy", "url": "http://127.0.0.1:1"}, _ctx())

    def test_export_names_glb_without_pins(self):
        cand = {"meshy": {"endpoint": "image-to-3d", "options": {"target_formats": ["glb"]}}}
        ex = self._ad().chain_export(cand, {"alias": "mesh-mia"}, {}, "gwchain_j1")
        self.assertIsNone(ex.error)
        self.assertEqual(ex.mesh_name, "gwchain_j1.glb")
        self.assertEqual(ex.extra_fixed, [])

    def test_export_requires_glb_format(self):
        cand = {"meshy": {"endpoint": "image-to-3d", "options": {"target_formats": ["fbx"]}}}
        ex = self._ad().chain_export(cand, {"alias": "mesh-mia"}, {}, "gwchain_j1")
        self.assertIn("glb", ex.error)

    def test_take_mesh_from_blobs(self):
        out = GenOutput(blobs=[GenBlob(b"glTFxxxx", "model/gltf-binary", "file", "model.glb"),
                               GenBlob(b"png", "image/png", "image", "preview.png")])
        ex = adapters.ChainExport("gwchain_j1.glb")
        self.assertEqual(asyncio.run(self._ad().chain_take_mesh(out, ex, True)), b"glTFxxxx")
        self.assertEqual(asyncio.run(self._ad().chain_take_mesh(out, ex, False)), b"")
        self.assertIsNone(asyncio.run(self._ad().chain_take_mesh(GenOutput(blobs=[]), ex, True)))
```

- [ ] **Step 2: Run — fails (`chain_export` returns the base error)**

- [ ] **Step 3: `MeshyAdapter` Hooks**

```python
    def chain_export(self, cand: dict, succ: dict, params: dict, prefix: str) -> ChainExport:
        opts = meshy.options_of({"meshy": cand.get("meshy") or {}})
        if "glb" not in opts["target_formats"]:
            return ChainExport("", error=f"chain: Meshy stage 1 must deliver glb — add it to "
                                         f"target_formats (now {opts['target_formats']})")
        return ChainExport(f"{prefix}.glb")             # no pins: the mesh is a result blob

    async def chain_take_mesh(self, out: GenOutput, export: ChainExport, want_bytes: bool) -> Optional[bytes]:
        blob = next((b for b in (out.blobs or []) if (b.name or "") == "model.glb"), None)
        if blob is None:
            return None
        return blob.data if want_bytes else b""
```

- [ ] **Step 4: `_run_chain` — Relay-Erzwingung, Meta, Rig-Gating**

Nach der `relay = …`-Zeile (Kopf der Funktion):

```python
    # A Meshy stage has no shared disk: with Meshy on EITHER side the mesh travels as
    # bytes, whatever the stored relay says (the editor hides the field for such aliases).
    def _kind_meshy(alias_name: str) -> bool:
        c = (store.get(alias_name) if store.is_active() else None) or image_models.get(alias_name) or []
        return bool(c) and c[0].get("meshy") is not None
    s1_meshy = await asyncio.to_thread(_kind_meshy, alias)
    s2_meshy = await asyncio.to_thread(_kind_meshy, succ_alias)
    if s1_meshy or s2_meshy:
        relay = "upload"
```

Nach `out1 = await adapter.generate(req1)` … nach `s1_done = True`: `s1_meta = {k: out1.meta.get(k) for k in ("backend", "meshy_task_id", "endpoint", "consumed_credits", "request") if out1.meta.get(k) is not None} if out1.meta.get("meshy_task_id") else None`.
Bei `meta = {**out2.meta, …}`: `**({"chain_stage1": s1_meta} if s1_meta else {})`.
Rig-Block: `if chain_rig:` → `meta["rig"] = chain_rig` immer; `normalize_delivery`/`validate_delivery` nur `if chain_rig in ("generic", "mixamo")`.

`run_generation`: den Guard-Block (`_succ0 = …` bis `raise HTTPException(400, … cannot be a chain stage …)`) entfernen.

- [ ] **Step 5: Konsole**

`_meshy_editor`: vor `<h2>Request fields</h2>` einen Abschnitt „Chain (successor)": `_inp("successor", s.get("alias",""))`, `_inp("chain_mesh_param", s.get("mesh_param",""), placeholder="input_mesh_path")`, `_inp("chain_keep", ", ".join(keep))`, `_select("chain_rig", [("", "blank"), "mixamo", "generic", "meshy"], s.get("rig",""))`, Hint: „the mesh (glb) is relayed as bytes to the successor's backend; no export node, no relay choice". `s = next((c.get("successor") for c in cands if c.get("successor")), None) or {}`.
`meshy_update`: wie der Successor-Block in `update` (3503–3518), ohne `export_node`/`relay`: `succ = {"alias", "mesh_param" or "input_mesh_path", keep?, rig?}`.
`_chain_section` (ComfyUI): Platzhalter `chain_rig` → `"blank · mixamo · generic · meshy"`; Hint ergänzen: „A Meshy successor (Meshy-Rig) always receives the mesh as bytes — the hand-off setting is ignored then."
`job_detail_page`: nach dem `mrq`-Block: wenn `meta.get("chain_stage1")` → dieselbe Tabelle mit Überschrift „Meshy · stage 1 · task … · credits".

- [ ] **Step 6: Tests, Compile, Render-Check** (`_meshy_editor` enthält `name="successor"`; `_chain_section` enthält `meshy`), Commit — `chain: a Meshy alias can be stage 1 (mesh from the result blob, relay forced to upload)`

---

### Task 3: Endpoint `rigging` in `meshy.py`, `files[]`-Tripel, `Meshy-Rig` standalone

**Files:**
- Modify: `meshy.py`, `adapters.py` (`MeshyAdapter.generate`/`_poll`, `public_fields`), `main.py` (`gen_alias_schema` 3490–3500, `_decode_upload_files` 3405–3425, `_gen_image_slots` 3591), `admin.py` (`_meshy_editor` 2982 + Endpoint-Select + `animations`, Playground 3718/3892/3979 — Meshy-Datei-Zeilen wie die ComfyUI-`file__`-Zeile, `meshy_update` Formate je Endpoint)
- Test: `test_meshy.py`, `test_meshy_adapter.py`

**Interfaces:**
- Produces: `meshy.FILES`, `meshy.RIG_FORMATS = ("glb","fbx")`, `meshy.OPTION_DEFAULTS["animations"] = False`; `meshy.public_fields(cand) -> (params, images, files)` mit `files[] = [{"name","required","accept"}]`; `meshy.build_request(cand, values, images, files=None)`; `meshy.parse_task(task, formats, endpoint="image-to-3d", animations=False)` mit `downloads = [(filename, url)]` (Dateinamen `model.<fmt>` / `rigged.<fmt>` / `walking.<fmt>` / `running.<fmt>`); `adapters.public_fields(cand) -> (params, images, files)` (ComfyUI: `files` aus `file_params` → `[{"name": label, "required": False}]`).

- [ ] **Step 1: Failing tests** (`test_meshy.py`)

```python
GLB = b"glTF" + b"\x00" * 60

class TestRigging(unittest.TestCase):
    C = {"backend": "meshy", "task": "mesh2rig", "model": "latest",
         "meshy": {"endpoint": "rigging", "options": {"target_formats": ["glb"]}}}

    def test_build_request(self):
        body = meshy.build_request(self.C, {"input_name": "Kai", "input_height_m": 1.8},
                                   {}, {"input_mesh_path": ("hero.glb", GLB)})
        self.assertTrue(body["model_url"].startswith("data:model/gltf-binary;base64,"))
        self.assertEqual(body["height_meters"], 1.8)
        self.assertEqual(body["name"], "Kai")
        for k in ("ai_model", "image_url", "should_texture", "target_formats"):
            self.assertNotIn(k, body)

    def test_default_height(self):
        body = meshy.build_request(self.C, {}, {}, {"input_mesh_path": ("h.glb", GLB)})
        self.assertEqual(body["height_meters"], 1.7)

    def test_missing_mesh_and_non_glb(self):
        with self.assertRaises(meshy.MeshyInput):
            meshy.build_request(self.C, {}, {}, {})
        with self.assertRaises(meshy.MeshyInput):
            meshy.build_request(self.C, {}, {}, {"input_mesh_path": ("h.obj", b"v 0 0 0")})

    def test_public_fields_triple(self):
        params, images, files = meshy.public_fields(self.C)
        self.assertEqual(images, [])
        self.assertEqual(files, [{"name": "input_mesh_path", "required": True, "accept": ["glb"]}])
        names = [p["name"] for p in params]
        self.assertEqual(names, ["input_name", "input_height_m", "input_no_fingers"])
        p2, i2, f2 = meshy.public_fields(_cand())
        self.assertEqual(f2, [])
        self.assertEqual([i["name"] for i in i2], ["input_image"])

    def test_parse_rigging_result(self):
        task = {"status": "SUCCEEDED", "progress": 100, "consumed_credits": 5,
                "result": {"rigged_character_glb_url": "https://a/r.glb", "rigged_character_fbx_url": "https://a/r.fbx",
                           "basic_animations": {"walking_glb_url": "https://a/w.glb", "running_glb_url": "https://a/x.glb",
                                                "walking_fbx_url": "https://a/w.fbx", "running_fbx_url": "https://a/x.fbx"}}}
        st = meshy.parse_task(task, ["glb"], "rigging")
        self.assertEqual(st.downloads, [("rigged.glb", "https://a/r.glb")])
        st = meshy.parse_task(task, ["glb", "fbx"], "rigging", animations=True)
        self.assertEqual([n for n, _ in st.downloads],
                         ["rigged.glb", "rigged.fbx", "walking.glb", "running.glb", "walking.fbx", "running.fbx"])
        with self.assertRaises(meshy.MeshyInput):
            meshy.parse_task({"status": "SUCCEEDED", "result": {}}, ["glb"], "rigging")

    def test_image_endpoint_downloads_are_named(self):
        st = meshy.parse_task({"status": "SUCCEEDED", "model_urls": {"glb": "https://a/m.glb"}}, ["glb"])
        self.assertEqual(st.downloads, [("model.glb", "https://a/m.glb")])

    def test_request_summary_hides_mesh(self):
        body = meshy.build_request(self.C, {}, {}, {"input_mesh_path": ("h.glb", GLB)})
        self.assertEqual(meshy.request_summary(body)["model_url"], f"<{len(GLB)} bytes>")
```
Bestehende Tests, die `downloads == [("glb", url)]` erwarten (`TestParseTask.test_succeeded`), auf `("model.glb", url)` anpassen; `TestPublicFields`-Tests auf das Tripel (`params, images, _ = …`).

`test_meshy_adapter.py`: Stub-Handler ergänzen: `POST /openapi/v1/rigging` → `self._json(200, {"result": "rig-1"})`; Test `test_rigging_flow`: script `[{"status":"SUCCEEDED","progress":100,"consumed_credits":5,"result":{"rigged_character_glb_url": f"{url}/asset/glb"}}]`, Request mit `meshy={"endpoint":"rigging","options":{}}`, `upload_files={"input_mesh_path": ("h.glb", GLB)}` → ein Blob `rigged.glb` (`model/gltf-binary`, kind file), `meta["endpoint"] == "rigging"`, `meta["request"]["model_url"].startswith("<")`; und `test_rigging_missing_mesh_is_input_error` (kein POST).

- [ ] **Step 2: Run — fails**

- [ ] **Step 3: `meshy.py`**

```python
ENDPOINTS = ("image-to-3d", "multi-image-to-3d", "rigging")
RIG_FORMATS = ("glb", "fbx")
FILES: dict[str, list[str]] = {"image-to-3d": [], "multi-image-to-3d": [], "rigging": ["input_mesh_path"]}
SLOTS["rigging"] = []
OPTION_DEFAULTS["animations"] = False        # rigging: also deliver Meshy's walking/running clips
_HEIGHT_DEFAULT = 1.7
```
`options_of`: nach der `target_formats`-Bereinigung: `if endpoint_of(cand) == "rigging": out["target_formats"] = [f for f in out["target_formats"] if f in RIG_FORMATS] or ["glb"]`.

```python
def glb_data_uri(data: bytes) -> str:
    if data[:4] != b"glTF":
        raise MeshyInput("Meshy rigging takes a binary glTF (.glb) mesh")
    return f"data:model/gltf-binary;base64,{base64.b64encode(data).decode()}"


def _build_rigging(cand: dict, values: dict, files: dict) -> dict:
    f = (files or {}).get("input_mesh_path")
    data = f[1] if isinstance(f, tuple) else f
    if not data:
        raise MeshyInput("`files.input_mesh_path` is required")
    body: dict = {"model_url": glb_data_uri(data)}
    h = values.get("input_height_m")
    try:
        body["height_meters"] = float(h) if h not in (None, "") else _HEIGHT_DEFAULT
    except (TypeError, ValueError):
        body["height_meters"] = _HEIGHT_DEFAULT
    name = values.get("input_name")
    if name not in (None, ""):
        body["name"] = str(name)[:_NAME_MAX]
    return body
```
`build_request(cand, values, images, files=None)`: `if endpoint_of(cand) == "rigging": return _build_rigging(cand, values, files or {})` als erste Zeile. `request_summary`: auch `model_url` → `_sz`. `public_fields` → Tripel: `files = [{"name": n, "required": True, "accept": ["glb"]} for n in FILES[ep]]`; für `rigging` `params = [input_name (string ""), input_height_m (float, default 1.7), input_no_fingers (bool False)]`, `images = []`.

`parse_task(task, formats, endpoint="image-to-3d", animations=False)`: im SUCCEEDED-Zweig:
```python
        if endpoint == "rigging":
            res = task.get("result") or {}
            urls = {f: res.get(f"rigged_character_{f}_url") for f in formats}
            anim = res.get("basic_animations") or {}
        else:
            urls = task.get("model_urls") or {}
        missing = [f for f in formats if not urls.get(f)]
        if missing: raise MeshyInput(...)
        stem = "rigged" if endpoint == "rigging" else "model"
        st.downloads = [(f"{stem}.{f}", urls[f]) for f in formats]
        if endpoint == "rigging" and animations:
            for f in formats:
                for clip in ("walking", "running"):
                    u = anim.get(f"{clip}_{f}_url")
                    if u:
                        st.downloads.append((f"{clip}.{f}", u))
```

- [ ] **Step 4: Adapter + Seams**

`MeshyAdapter.generate`: `body = meshy.build_request(cand, _gen_values(req), req.upload_images or {}, req.upload_files or {})`; `_poll(..., endpoint, opts)` → `meshy.parse_task(r.json() or {}, opts["target_formats"], endpoint, bool(opts.get("animations")))`; Blob-Schleife `for name, url in state.downloads: mime, kind = _mime_and_kind(name); GenBlob(..., name=name)`; Thumbnail nur `if endpoint != "rigging"`.
`adapters.public_fields` → Tripel; ComfyUI-Zweig: `files = [{"name": (m.get("label") or "").strip() or p, "required": False} for p in file_params(wf, mapping) for m in [mapping.get(p) or {}]]`.
`main.py`: Schema `params, images, files = …; out["files"] = files`; `_gen_image_slots` `[1]` bleibt; `_decode_upload_files`: Meshy-Zweig → `allowed = {f["name"] for f in adapters.public_fields(cands[0])[2]}`; leer → bisheriger 400-Text; `key not in allowed` → 400 „unknown `files` key"; sonst `_decode_ref_blob` + Größenlimit + `out[key] = (f"gwup_{slug}.{ext}", data)` und `return out` (die ComfyUI-Mapping-Logik darunter nicht durchlaufen).
`admin.py`: alle vier `public_fields`-Aufrufer auf das Tripel; Meshy-Editor: Endpoint-Select mit `rigging`, Checkbox `opt__animations`, Formate für `rigging` nur glb/fbx (im Editor per JS ausblenden ist optional — `options_of` filtert serverseitig), Feldtabelle listet `files` als „file · required"; Playground Meshy-Zweig rendert je `files`-Eintrag die Datei-Zeile (`file__<name>` + `hist__<name>` + kept-Badge, kein Pfad-Textfeld — Meshy nimmt keinen Pfad); Playground-POST: `fset = {f["name"] for f in files}` für Meshy (statt `set()`); `pnames` um `files`-Namen ergänzen; `meshy_update`: `animations` + Endpoint.

- [ ] **Step 5: Tests, Compile, Render-Check** (`_playground_form` für einen rigging-Kandidaten enthält `file__input_mesh_path` und KEIN `p__input_mesh_path`), Commit — `meshy: rigging endpoint (Meshy-Rig), files[] in the public fields and schema`

---

### Task 4: Meshy als Chain-Stufe 2 (ComfyUI oder Meshy → `Meshy-Rig`)

**Files:**
- Modify: `adapters.py` `MeshyAdapter.chain_feed_mesh`; `main.py` `_run_chain` (mesh_param-Validierung für Meshy-Successor); `admin.py` `_stage2_section` (Successor ohne Mapping), `_chain_section`/`_meshy_editor` Successor-Auswahl darf Meshy-`mesh2rig`-Aliase nennen (Freitext bleibt; Hint)
- Test: `test_chain_hooks.py`

- [ ] **Step 1: Failing test**

```python
class MeshyChainStage2(unittest.TestCase):
    def test_feed_embeds_upload(self):
        ad = adapters.MeshyAdapter({"name": "meshy", "type": "meshy", "url": "http://127.0.0.1:1"}, _ctx())
        req2 = NormalizedRequest(alias="Meshy-Rig", meshy={"endpoint": "rigging", "options": {}})
        ref = asyncio.run(ad.chain_feed_mesh(req2, {"name": "meshy"}, "input_mesh_path",
                                             "gwchain_j1.glb", b"glTF" + b"\0" * 1_048_576, ""))
        self.assertEqual(req2.upload_files["input_mesh_path"][0], "gwchain_j1.glb")
        self.assertEqual(len(req2.upload_files["input_mesh_path"][1]), 1_048_580)
        self.assertEqual(ref, "<upload:gwchain_j1.glb (1.0 MB)>")

    def test_feed_needs_bytes(self):
        ad = adapters.MeshyAdapter({"name": "meshy", "type": "meshy", "url": "http://127.0.0.1:1"}, _ctx())
        with self.assertRaises(RuntimeError):
            asyncio.run(ad.chain_feed_mesh(NormalizedRequest(), {}, "input_mesh_path", "n", None, "/out"))
```

- [ ] **Step 2: Implement**

```python
    async def chain_feed_mesh(self, req2, backend2, mesh_param, mesh_name, mesh_bytes, outdir) -> str:
        if mesh_bytes is None:
            raise RuntimeError("chain: a Meshy stage 2 needs the mesh bytes (upload relay)")
        req2.upload_files[mesh_param] = (mesh_name, mesh_bytes)     # embedded as model_url data URI
        return f"<upload:{mesh_name} ({len(mesh_bytes) / (1024 * 1024):.1f} MB)>"
```
`_run_chain` mesh_param-Validierung (2856–2867): für `s2.get("meshy") is not None` gegen `{f["name"] for f in adapters.public_fields(s2)[2]}` prüfen (Fehlertext: „… is not a file field of Meshy successor …"); sonst wie heute gegen das Mapping.
`_stage2_section`: wenn `s2` keine Mapping-Bindung liefern kann, weil der Successor Meshy ist (`store.get(alias2)[0].get("meshy")`), Zeilen als „handed · Meshy fixed table" statt „dropped" markieren, `mesh_ref` unverändert anzeigen.
Hints in `_chain_section` und `_meshy_editor`: „successor may be a Meshy alias (e.g. Meshy-Rig, endpoint rigging) — the mesh is then sent to Meshy as bytes; rig type `meshy`".

- [ ] **Step 3: Tests, Compile, Commit** — `chain: a Meshy alias can be stage 2 (mesh embedded as model_url), rig type meshy`

---

### Task 5: Dokumentation

**Files:** `README.md` (Meshy-Abschnitt: Chains, `Meshy-Rig`, Aliase-Tabelle, `rig: meshy`, Schema `files[]`), `docs/mesh-client-spec.md` (§3.2 Familien: `Meshy-Humanoid*`, `Meshy-Rig` in §3.1/Rig-Stufe; §3.3 Rig-Werte um `meshy`; Schema-Feld `files[]`), `CLAUDE.md` (Chain-Absatz: Hooks; `meshy.py`-Absatz: rigging, files-Tripel), `config.example.yaml` (Alias-Beispiel `Meshy-Rig`, Successor auf Meshy-Alias).

- [ ] Texte einfügen, Markdown prüfen, Commit — `docs: Meshy chains and Meshy-Rig`

---

### Task 6: Deploy, Aliase anlegen, Live-Tests

- [ ] **Step 1:** Suiten + Compile; FF-Push master auf origin/github/lxc; gezielter tar-Deploy der geänderten Dateien auf .10 (Backup vorher wie am 2026-09-02), `py_compile`, Restart, `/health`.
- [ ] **Step 2: Aliase über die Konsolen-Handler auf .10** (Login mit Master-Key + Cookie-Jar, Muster vom 2026-09-02): `Meshy-Rig` (register backend meshy, task mesh2rig, meshy-update endpoint rigging, fmt glb); `Meshy-Humanoid` (register, meshy-update endpoint image-to-3d, `opt__pose_mode=t-pose`, successor `mesh-mia`, `chain_mesh_param=input_mesh_path`, `chain_rig=mixamo`); `Meshy-Humanoid-Multiview` (multi-image-to-3d, sonst gleich); `Meshy-Humanoid-Cloud` (image-to-3d, t-pose, successor `Meshy-Rig`, `chain_rig=meshy`).
- [ ] **Step 3: Live (Reihenfolge, Abbruch bei Fehler):**
  1. `Meshy-Rig` standalone über `POST /v1/generations` mit `files.input_mesh_path` = GLB aus Job fbcfb5e0 (`jobs/fbcfb5e0936b492ba38ff1b4ce03ce61/0.glb`), `params.input_name`, async + Poll → `rigged.glb`, `meta.consumed_credits == 5`, `rig == "meshy"`.
  2. `Meshy-Humanoid` mit `jobs/a4f64b64…/in_0.png`, `input_name`, `input_no_fingers: 0`, async + Poll (parkt, wenn k12-gpu belegt) → Stufe „1/2" → „2/2" → Ergebnis von mesh-mia (`*_rigged.glb`), `meta.chain == ["Meshy-Humanoid", "mesh-mia"]`, `chain_stage1.meshy_task_id`, `chain_stage2.mesh_ref == "<upload:gwchain_<job>.glb (…)>"`, `rig == "mixamo"`.
  3. `Meshy-Humanoid-Cloud` → `rigged.glb`, `chain_stage2.backend == "meshy"`, 35 Credits gesamt.
  Ergebnisse (Job-IDs, Laufzeiten, Credits vor/nach) in den Report; Abweichungen fixen und committen.

---

## Self-Review

- **Spec-Abdeckung:** §3.1 Hooks → T1/T2/T4; §3.2 rigging → T3; §3.3 rig meshy → T2 (Gating) + T4 + T5; §3.4 Aliase → T6; §3.5 Konsole → T2 (Editor Successor, Job-Ansicht), T3 (Playground/Schema), T4 (`_stage2_section`); §3.6 Fehlerbild → T2 (glb-Check), T3 (MeshyInput), Adapter aus Phase 1; §3.7 Tests → T1–T4, Live T6; §5 Reihenfolge = Tasks.
- **Platzhalter:** keine.
- **Typ-Konsistenz:** `ChainExport(mesh_name, extra_fixed, error)`; `chain_take_mesh(out, export, want_bytes)`; `chain_feed_mesh(req2, backend2, mesh_param, mesh_name, mesh_bytes, outdir) -> str`; `public_fields -> (params, images, files)` in `meshy` UND `adapters`; `parse_task(task, formats, endpoint, animations)` mit `downloads = [(filename, url)]`; `build_request(cand, values, images, files)`.
