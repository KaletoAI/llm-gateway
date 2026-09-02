# Tripo3D Backend Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Tripo3D (API V3) als Generierungs-Backend `type: tripo` mit dem Feature-Set des Meshy-Backends (Image/Multiview → 3D, Cloud-Rigging mit Mixamo-Skelett, beide Chain-Rollen), aufgebaut auf einer Cloud-Kind-Abstraktion, die Meshy und Tripo als zwei Ausprägungen EINES Mechanismus behandelt.

**Architecture:** Ein pures `tripo.py` mit derselben Modul-Schnittstelle wie `meshy.py`; ein neues pures Leaf `cloudtask.py` (`TaskState`, Formular-Schema-Parser); in `adapters.py` eine Basisklasse `CloudTaskAdapter` (Slot, Poll-Grace-Logik, Download, Chain-Hooks), auf die `MeshyAdapter` refaktoriert wird und die `TripoAdapter` erbt; `main.py`/`admin.py` ersetzen jede `meshy`-Sonderabfrage durch `adapters.cloud_kind()`/`CLOUD_TYPES`; der Meshy-Alias-Editor wird zum schema-getriebenen `_cloud_editor` für beide Kinds.

**Tech Stack:** Python 3, FastAPI/Starlette, httpx, stdlib `unittest` (kein pytest), SQLite-Store (schemalos, keine Migration).

**Spec:** `docs/superpowers/specs/2026-09-03-tripo-backend-design.md` (Design) und `docs/tripo-api-v3-notes.md` (API-Referenz, aus der Doku extrahiert). Ausführende lesen beide; die Meshy-Spec `docs/superpowers/specs/2026-09-02-meshy-backend-design.md` erklärt das Vorbild.

## Global Constraints

- Das venv liegt NUR im Haupt-Checkout: immer `/home/dev/projekte/llm-gateway/venv/bin/python` verwenden (im Worktree gibt es kein `venv/`). Kein Test-Runner außer `… -m unittest <modul> -v`; kein Linter, kein Build. Vor jedem Commit: `/home/dev/projekte/llm-gateway/venv/bin/python -m py_compile *.py` und `… -m unittest discover -p 'test_*.py'` (alle 13 bestehenden Testdateien + die neuen müssen grün sein; `ls test_*.py` zählt sie).
- Arbeitsverzeichnis ist der Worktree `/home/dev/projekte/llm-gateway/.claude/worktrees/tripo-backend` (Branch `worktree-tripo-backend`). Alle Pfade unten sind relativ dazu. Kein `cd` in den Haupt-Checkout.
- `tripo.py`, `cloudtask.py`, `meshy.py` importieren **nie** `main`/`adapters`; `adapters.py` importiert `meshy`, `tripo`, `cloudtask`.
- Tripo: nur **API V3**, Base-URL `https://openapi.tripo3d.ai` + Pfadpräfix `/v3`. Kein Base64 — Eingaben per `POST /v3/files` (multipart, Feld `file`) → `data.file_token`. Antworthülle IMMER `{"code": 0, "data": {…}}`; `code != 0` = Fehler mit `message`/`suggestion`. Kein Guthaben = HTTP 403 + `code 2010`; Concurrency voll = HTTP 429 (+ `code 2000`).
- Ein Cloud-Backend (`type in adapters.CLOUD_TYPES`) ist **immer** `paid`.
- Öffentliche Labels Tripo (Spec §6.1): `input_image`, `input_image_front/back/left/right`, `input_mesh_path` (Datei, rig), `input_face_num`, `input_texture_resolution`, `input_rig_type`; angenommen+ignoriert: `input_name`, `input_remove_background`, `input_no_fingers`. NICHT advertised: `input_texture_prompt`, `input_pose`, `input_height_m`.
- Defaults Tripo-Backend: `poll_interval` 2, `max_wait` 900, `disconnect_grace` 30. Rig-Defaults: `spec: mixamo`, `rig_type: biped`, `rig_model: v1.0-20240301`, `rig_check: true`.
- Meshy-Verhalten ändert sich NICHT (die bestehenden Tests `test_meshy.py`, `test_meshy_adapter.py` bleiben unverändert grün; `MeshyNoCredits`/`MeshyBusy` bleiben als Aliase, `meshy_task_id` bleibt im Meta).
- Aliase bleiben homogen (ein Kind je Alias). `rig` bekommt den vierten Wert `tripo` (tag-only wie `meshy`).
- Nie `config.yaml`, `store.db`, `secret.key`, `*.db*` committen. Commit-Trailer:
  `Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>` und
  `Claude-Session: https://claude.ai/code/session_01EePvuMtr5Jw36ALZ49CKPb`.
- Kommentare/Docstrings im Code auf Englisch (wie der Bestand), Erklärung des WARUM, nicht des WAS.

---

### Task 1: `cloudtask.py` (gemeinsames Leaf) + Meshy-Modul auf die Cloud-Schnittstelle

**Files:**
- Create: `cloudtask.py`
- Create: `test_cloudtask.py`
- Modify: `meshy.py` (Konstanten am Kopf, `TaskState`-Import, `parse_task`-Signatur, `OPTION_FIELDS` + Hinweistexte am Ende)

**Interfaces:**
- Produces (Task 2, 3, 7 nutzen es):
  - `cloudtask.TaskState` — die Dataclass, die heute in `meshy.py` steht (`status, progress=0, error=None, downloads=[], thumbnail=None, credits=None`), unverändert; `meshy.TaskState` ist derselbe Typ (`from cloudtask import TaskState`).
  - `cloudtask.parse_options(fields: list[dict], form: dict, defaults: dict) -> dict` — liest `opt__<key>`-Formularwerte nach dem Feldschema; Ergebnis = `defaults`-Kopie mit den gelesenen Werten.
  - `cloudtask.field_value_str(field: dict, value) -> str` — Wert → Formular-String (für `select`/`tristate`/`int`/`text`/`list`; `bool` nutzt Checkbox).
  - Feldschema (Dict je Feld): `key` (str, Options-Schlüssel), `label` (str, linke Spalte), `type` ∈ `bool | select | tristate | int | text | list`, `choices` (nur `select`: Liste von `(value, text)`-Tupeln ODER Strings), `placeholder` (optional), `hint` (optional, `<p class='hint'>` unter dem Feld), `rig_only` (optional bool — nur relevant im Rig-Endpunkt; der Editor rendert es trotzdem, der Hinweis sagt es), `checkbox_text` (nur `bool`: Text neben der Box, Default = `key`).
  - `meshy.KIND = "meshy"`, `meshy.VENDOR = "Meshy"`, `meshy.URL = "https://api.meshy.ai"`, `meshy.RIG_ENDPOINT = "rigging"`, `meshy.OPTION_FIELDS: list[dict]`, `meshy.BACKEND_HINT: str`, `meshy.ENDPOINT_HINT: str`, `meshy.CHAIN_HINT: str`.
  - `meshy.parse_task(task, formats, endpoint="image-to-3d", animations=False, options=None)` — neuer optionaler `options`-Parameter; wenn gegeben, gilt `bool(options.get("animations"))` statt `animations`.

- [ ] **Step 1: Failing tests schreiben** — `test_cloudtask.py`:

```python
"""Unit tests for cloudtask.py — run: /home/dev/projekte/llm-gateway/venv/bin/python -m unittest test_cloudtask -v"""
import unittest

import cloudtask
import meshy

FIELDS = [
    {"key": "flag", "label": "flag", "type": "bool"},
    {"key": "mode", "label": "mode", "type": "select", "choices": [("a", "A"), ("b", "B")]},
    {"key": "tri", "label": "tri", "type": "tristate"},
    {"key": "n", "label": "n", "type": "int"},
    {"key": "txt", "label": "txt", "type": "text"},
    {"key": "lst", "label": "lst", "type": "list"},
]
DEFAULTS = {"flag": True, "mode": "a", "tri": None, "n": None, "txt": "", "lst": []}


class ParseOptions(unittest.TestCase):
    def test_reads_every_type(self):
        form = {"opt__flag": "on", "opt__mode": "b", "opt__tri": "false", "opt__n": "42",
                "opt__txt": " hi ", "opt__lst": "preset:walk, preset:run,,"}
        out = cloudtask.parse_options(FIELDS, form, DEFAULTS)
        self.assertEqual(out, {"flag": True, "mode": "b", "tri": False, "n": 42,
                               "txt": "hi", "lst": ["preset:walk", "preset:run"]})

    def test_missing_checkbox_is_false_and_blank_is_default_shape(self):
        out = cloudtask.parse_options(FIELDS, {"opt__mode": "zzz", "opt__n": "x", "opt__tri": ""}, DEFAULTS)
        self.assertFalse(out["flag"])                  # an unchecked box is not submitted
        self.assertEqual(out["mode"], "a")             # unknown choice → default
        self.assertIsNone(out["n"])                    # garbage int → None (module validates later)
        self.assertIsNone(out["tri"])
        self.assertEqual(out["lst"], [])

    def test_does_not_mutate_defaults(self):
        d = dict(DEFAULTS)
        cloudtask.parse_options(FIELDS, {"opt__lst": "x"}, d)
        self.assertEqual(d, DEFAULTS)

    def test_field_value_str(self):
        self.assertEqual(cloudtask.field_value_str({"type": "tristate"}, None), "")
        self.assertEqual(cloudtask.field_value_str({"type": "tristate"}, True), "true")
        self.assertEqual(cloudtask.field_value_str({"type": "int"}, None), "")
        self.assertEqual(cloudtask.field_value_str({"type": "int"}, 7), "7")
        self.assertEqual(cloudtask.field_value_str({"type": "list"}, ["a", "b"]), "a, b")


class MeshyFields(unittest.TestCase):
    def test_meshy_option_fields_roundtrip_defaults(self):
        """Rendering the defaults into a form and parsing them back yields the defaults —
        the editor and the request builder read the same table."""
        form = {}
        for fld in meshy.OPTION_FIELDS:
            v = meshy.OPTION_DEFAULTS[fld["key"]]
            if fld["type"] == "bool":
                if v:
                    form[f"opt__{fld['key']}"] = "on"
            else:
                form[f"opt__{fld['key']}"] = cloudtask.field_value_str(fld, v)
        out = cloudtask.parse_options(meshy.OPTION_FIELDS, form, meshy.OPTION_DEFAULTS)
        for fld in meshy.OPTION_FIELDS:
            self.assertEqual(out[fld["key"]], meshy.OPTION_DEFAULTS[fld["key"]], fld["key"])

    def test_meshy_fields_cover_every_option_except_formats(self):
        keys = {f["key"] for f in meshy.OPTION_FIELDS}
        self.assertEqual(keys, set(meshy.OPTION_DEFAULTS) - {"target_formats"})

    def test_meshy_module_constants(self):
        self.assertEqual((meshy.KIND, meshy.VENDOR, meshy.RIG_ENDPOINT), ("meshy", "Meshy", "rigging"))
        self.assertTrue(meshy.URL.startswith("https://"))
        self.assertIs(meshy.TaskState, cloudtask.TaskState)

    def test_parse_task_options_kwarg(self):
        task = {"status": "SUCCEEDED", "result": {"rigged_character_glb_url": "u",
                                                  "basic_animations": {"walking_glb_url": "w"}}}
        st = meshy.parse_task(task, ["glb"], "rigging", options={"animations": True})
        self.assertEqual([n for n, _ in st.downloads], ["rigged.glb", "walking.glb"])
        st = meshy.parse_task(task, ["glb"], "rigging", options={"animations": False})
        self.assertEqual([n for n, _ in st.downloads], ["rigged.glb"])


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: Test laufen lassen, Fehlschlag sehen**

Run: `/home/dev/projekte/llm-gateway/venv/bin/python -m unittest test_cloudtask -v`
Expected: `ModuleNotFoundError: No module named 'cloudtask'`

- [ ] **Step 3: `cloudtask.py` schreiben**

```python
"""Shared pure pieces of the cloud task backends (Meshy, Tripo): the task state the
adapters poll towards, and the admin-option form schema every cloud kind declares
(`OPTION_FIELDS`) so ONE console editor serves all of them. No main/adapters imports."""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Optional


@dataclass
class TaskState:
    status: str
    progress: int = 0
    error: Optional[str] = None
    downloads: list = field(default_factory=list)      # [(filename, url)] in delivery order
    thumbnail: Optional[str] = None
    credits: Optional[float] = None


# ── the option form schema ────────────────────────────────────────────────────
# A field: {key, label, type: bool|select|tristate|int|text|list, choices?, placeholder?,
# hint?, rig_only?, checkbox_text?}. Form names are `opt__<key>` (Meshy's existing names).

_TRISTATE = {"true": True, "false": False}


def _choice_values(fld: dict) -> list:
    return [c[0] if isinstance(c, (tuple, list)) else c for c in (fld.get("choices") or [])]


def parse_options(fields: list, form: dict, defaults: dict) -> dict:
    """Read `opt__<key>` form values by schema. Unknown/garbage values fall back to the
    DEFAULT for that key (never raise on a form) — the module's `options_of` is the
    validator of record and runs on every read anyway. Returns a fresh dict."""
    out = dict(defaults)
    for fld in fields:
        k, t = fld["key"], fld["type"]
        raw = form.get(f"opt__{k}")
        if t == "bool":
            out[k] = bool(raw)                          # an unchecked box is absent
            continue
        s = (raw or "").strip() if isinstance(raw, str) else ""
        if t == "select":
            out[k] = s if s in _choice_values(fld) else defaults.get(k)
        elif t == "tristate":
            out[k] = _TRISTATE.get(s)                   # "" / unknown → None
        elif t == "int":
            try:
                out[k] = int(float(s)) if s else None
            except ValueError:
                out[k] = None
        elif t == "list":
            out[k] = [x.strip() for x in re.split(r"[\s,]+", s) if x.strip()]
        else:                                           # text
            out[k] = s
    return out


def field_value_str(fld: dict, value) -> str:
    """A stored option value as the form control's string (the inverse of parse_options)."""
    t = fld.get("type")
    if t == "tristate":
        return {True: "true", False: "false"}.get(value, "")
    if t == "list":
        return ", ".join(str(x) for x in (value or []))
    if value is None:
        return ""
    return str(value)
```

- [ ] **Step 4: `meshy.py` anpassen** — am Kopf nach den Imports (`TaskState`-Dataclass aus der Datei ENTFERNEN und importieren):

```python
from cloudtask import TaskState                # shared with tripo.py; re-exported for callers

KIND, VENDOR, URL = "meshy", "Meshy", "https://api.meshy.ai"
RIG_ENDPOINT = "rigging"
```

`ENDPOINTS` etc. bleiben. `parse_task` bekommt den Parameter `options: Optional[dict] = None` hinter `animations` und direkt am Anfang:

```python
    if options is not None:
        animations = bool(options.get("animations"))
```

Am Dateiende `OPTION_FIELDS` + Hinweise — sie bilden den heutigen `_meshy_editor` 1:1 ab (Reihenfolge = Formularreihenfolge; Formularnamen bleiben `opt__<key>`):

```python
# The console's option form for a Meshy alias — rendered and parsed by admin's cloud
# editor from THIS table (cloudtask.parse_options). Order = form order.
OPTION_FIELDS: list = [
    {"key": "should_texture", "label": "texture", "type": "bool"},
    {"key": "enable_pbr", "label": "", "type": "bool", "checkbox_text": "enable_pbr (PBR maps)"},
    {"key": "texture_resolution", "label": "texture resolution", "type": "select",
     "choices": list(TEXTURE_RES),
     "hint": "Default when the client sends no <code>input_texture_resolution</code> "
             "(≤2048 → 2k, ≤4096 → 4k, else 8k). 4k/8k need Meshy-6+."},
    {"key": "topology", "label": "topology", "type": "select", "choices": list(TOPOLOGIES)},
    {"key": "should_remesh", "label": "remesh", "type": "tristate",
     "hint": "A client <code>input_face_num</code> always turns remesh on for that request "
             "(a polycount needs the remesh pass)."},
    {"key": "target_polycount", "label": "target polycount", "type": "int",
     "placeholder": "blank = Meshy default / no remesh",
     "hint": "Face budget applied when the client sends no <code>input_face_num</code> "
             "(100–300000; turns remesh on). A client value still wins. An alias that "
             "<b>chains into a rigger</b> should stay ≤ 300000: Meshy's rigging endpoint refuses "
             "more, and a no-remesh humanoid came back at <b>70 MB</b> (measured 2026-09-02)."},
    {"key": "pose_mode", "label": "pose", "type": "select", "choices": [(p, p or "none") for p in POSES]},
    {"key": "image_enhancement", "label": "input", "type": "bool"},
    {"key": "remove_lighting", "label": "", "type": "bool"},
    {"key": "moderation", "label": "", "type": "bool"},
    {"key": "ultra_mode", "label": "ultra", "type": "bool", "checkbox_text": "ultra_mode (+5 credits, Meshy-7 only)"},
    {"key": "animations", "label": "animations", "type": "bool", "rig_only": True,
     "checkbox_text": "rigging only: also deliver walking/running clips"},
    {"key": "thumbnail", "label": "thumbnail", "type": "bool",
     "checkbox_text": "deliver Meshy's preview.png as an extra image artifact"},
]

BACKEND_HINT = (
    "<b>api key</b> (above) is the Meshy key (<code>msy_…</code>, Meshy dashboard → API). "
    "<b>max_concurrent</b> should stay at or below your Meshy tier's concurrent-task limit — "
    "this account is on <b>Pro (10)</b> (Studio 20 · Premium 30 · Ultra 100; the limit is shared "
    "by every key of the account) — beyond it Meshy answers 429 and the job fails over. "
    "<b>max wait s</b> caps one task incl. Meshy's own queue (blank = 900); <b>poll interval s</b> "
    "is the gap between task polls (blank = 5). Credits: 20 (no texture) / 30 (textured) / "
    "35 (8K) per Meshy-6/7 task, +5 ultra; refunded when a task fails. The current balance shows "
    "in the backend list after the next health poll.")
ENDPOINT_HINT = (
    "<b>image-to-3d</b> takes <code>input_image</code>; <b>multi-image-to-3d</b> takes "
    "<code>input_image_front</code> (required) plus optional <code>_back/_left/_right</code> — the "
    "same slot names as the Trellis2 multiview alias. <b>rigging</b> takes no image at all: it rigs "
    "an uploaded <code>input_mesh_path</code> (a <code>.glb</code> biped, 5 credits) and ignores every "
    "option below except <b>deliver formats</b> (glb/fbx) and <b>animations</b>.")
CHAIN_HINT = (
    "The successor may itself be a <b>Meshy alias</b> (e.g. <code>Meshy-Rig</code>, endpoint "
    "<code>rigging</code>) — it then takes the mesh as its file field <code>input_mesh_path</code>, "
    "and the <b>delivered rig type</b> is <code>meshy</code>. Meshy embeds its texture in the GLB, so "
    "<b>keep from this stage</b> is usually empty — <code>preview.png</code> is the one candidate. "
    "Any OTHER format in <b>deliver formats</b> is wasted on a chained alias: Meshy bills every one "
    "of them, but only the successor's result (plus what <b>keep from this stage</b> matches) is "
    "delivered.")
```

Wichtig: `"label": ""` bedeutet „in derselben Zeile wie das vorige bool-Feld" (der Editor in Task 7 gruppiert aufeinanderfolgende bool-Felder, deren `label` leer ist, in EINE `_field`-Zeile — so bleiben Meshys Zeilen `texture` = `should_texture + enable_pbr`, `input` = drei Boxen).

Die Default-Werte `poll_interval`/`max_wait` je Kind: `meshy.POLL_INTERVAL_DEFAULT = 5.0`, `meshy.MAX_WAIT_DEFAULT = 900` ebenfalls am Kopf ergänzen (Task 3 liest sie im Adapter, Task 6 als Platzhalter im Formular).

- [ ] **Step 5: Tests laufen lassen**

Run: `/home/dev/projekte/llm-gateway/venv/bin/python -m unittest test_cloudtask test_meshy test_meshy_adapter -v`
Expected: alle PASS (Meshy-Tests unverändert grün).

- [ ] **Step 6: Commit**

```bash
git add cloudtask.py test_cloudtask.py meshy.py
git commit -m "cloudtask: shared TaskState + option-form schema; meshy declares its cloud-kind interface"
```

---

### Task 2: `tripo.py` — das pure Tripo-Modul mit Tests

**Files:**
- Create: `tripo.py`
- Create: `test_tripo.py`

**Interfaces:**
- Consumes: `cloudtask.TaskState` (Task 1).
- Produces (Task 4, 6, 7 nutzen es) — exakt dieselben Namen wie `meshy.py`:
  - `KIND = "tripo"`, `VENDOR = "Tripo"`, `URL = "https://openapi.tripo3d.ai"`, `API = "/v3"`, `POLL_INTERVAL_DEFAULT = 2.0`, `MAX_WAIT_DEFAULT = 900`
  - `ENDPOINTS = ("image-to-model", "multiview-to-model", "rig")`, `RIG_ENDPOINT = "rig"`
  - `AI_MODELS = ("v3.1-20260211", "v3.0-20250812", "v2.5-20250123", "P1-20260311", "P2-20260801")`
  - `RIG_MODELS = ("v1.0-20240301", "v2.5-20260210")`, `RIG_TYPES = ("biped", "quadruped", "hexapod", "octopod", "avian", "serpentine", "aquatic")`, `SPECS = ("mixamo", "tripo")`
  - `FORMATS = ("glb", "fbx", "obj", "stl", "usdz", "3mf", "gltf")`, `RIG_FORMATS = ("glb", "fbx")`, `NATIVE_FORMAT = "glb"`
  - `TEXTURE_QUALITIES = ("standard", "detailed", "extreme")`, `TEXTURE_ALIGNMENTS = ("original_image", "geometry")`, `GEOMETRY_QUALITIES = ("standard", "detailed")`, `ORIENTATIONS = ("default", "align_image")`, `COMPRESS = ("", "geometry")`
  - `FACE_MAX: dict[str, int]` (`v3.1-20260211: 1_500_000, v3.0-20250812: 1_000_000, v2.5-20250123: 500_000, P1-20260311: 50_000, P2-20260801: 50_000`), `FACE_MAX_QUAD = 150_000`, `_FACE_MIN = 100`
  - `OPTION_DEFAULTS`, `OPTION_FIELDS`, `SLOTS`, `FILES`, `IGNORED_PARAMS = ("input_name", "input_remove_background", "input_no_fingers")`
  - `class TripoInput(RuntimeError)`
  - `endpoint_of(cand)`, `options_of(cand)`, `default_candidate(backend)`, `public_fields(cand) -> (params, images, files)`
  - `texture_quality(px) -> str`, `face_limit_for(model: str, quad: bool, v) -> Optional[int]` (klemmt auf `[100, max]`; `None`/leer/garbage → `None`), `opt_face_limit(model, quad, v) -> Optional[int]` (Admin-Option: außerhalb → `None`, wie `meshy.opt_polycount`)
  - `image_ext(data: bytes) -> str` (`"png"`/`"jpg"`, sonst `TripoInput`), `mesh_ext(data: bytes) -> str` (`"glb"` bei `glTF`-Magic, sonst `TripoInput`)
  - `build_request(cand, values, images: dict[str, str], files: dict[str, str]) -> dict` — **Werte sind `file_token`-Strings**
  - `build_rig_check(token: str) -> dict`, `build_convert(task_id: str, fmt: str, animated: bool) -> dict`, `build_retarget(task_id: str, preset: str, fmt: str) -> dict`
  - `request_summary(body) -> dict` (Kopie; Tokens bleiben lesbar)
  - `parse_task(task: dict, formats: list, endpoint: str = "image-to-model", options: Optional[dict] = None) -> TaskState` — `task` ist das **`data`-Objekt** (ohne Hülle)
  - `clip_name(preset: str) -> str` (`"preset:walk"` → `"walk"`, `"preset:quadruped:walk"` → `"quadruped_walk"`)
  - `BACKEND_HINT`, `ENDPOINT_HINT`, `CHAIN_HINT`

- [ ] **Step 1: Failing tests schreiben** — `test_tripo.py`:

```python
"""Unit tests for tripo.py — run: /home/dev/projekte/llm-gateway/venv/bin/python -m unittest test_tripo -v"""
import unittest

import cloudtask
import tripo

PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 32
JPG = b"\xff\xd8\xff\xe0" + b"\x00" * 32
GLB = b"glTF" + b"\x00" * 60


def cand(endpoint="image-to-model", model="v3.1-20260211", **opts):
    c = tripo.default_candidate("tripo")
    c["model"] = model
    c["tripo"]["endpoint"] = endpoint
    c["tripo"]["options"].update(opts)
    return c


class Sniff(unittest.TestCase):
    def test_image_ext(self):
        self.assertEqual(tripo.image_ext(PNG), "png")
        self.assertEqual(tripo.image_ext(JPG), "jpg")
        with self.assertRaises(tripo.TripoInput):
            tripo.image_ext(b"RIFF....WEBPVP8 ")

    def test_mesh_ext(self):
        self.assertEqual(tripo.mesh_ext(GLB), "glb")
        with self.assertRaises(tripo.TripoInput):
            tripo.mesh_ext(b"# obj\nv 0 0 0\n")


class Options(unittest.TestCase):
    def test_defaults_are_deep_copied(self):
        a, b = tripo.default_candidate("t"), tripo.default_candidate("t")
        a["tripo"]["options"]["target_formats"].append("fbx")
        self.assertEqual(b["tripo"]["options"]["target_formats"], ["glb"])
        self.assertEqual(tripo.OPTION_DEFAULTS["target_formats"], ["glb"])
        self.assertEqual(a["task"], "img2mesh")
        self.assertEqual(a["model"], "v3.1-20260211")

    def test_options_of_normalizes(self):
        o = tripo.options_of(cand(texture_quality="ultra", target_formats=["xyz", "fbx"],
                                  face_limit="999999999", spec="nope", rig_type="dragon",
                                  compress="zip"))
        self.assertEqual(o["texture_quality"], "standard")
        self.assertEqual(o["target_formats"], ["fbx"])
        self.assertIsNone(o["face_limit"])              # out of range → ignored, not clamped
        self.assertEqual(o["spec"], "mixamo")
        self.assertEqual(o["rig_type"], "biped")
        self.assertEqual(o["compress"], "")

    def test_rig_endpoint_narrows_formats(self):
        o = tripo.options_of(cand("rig", target_formats=["obj", "fbx", "glb"]))
        self.assertEqual(o["target_formats"], ["fbx", "glb"])   # stored order, filtered to RIG_FORMATS
        o = tripo.options_of(cand("rig", target_formats=["obj"]))
        self.assertEqual(o["target_formats"], ["glb"])

    def test_generate_parts_excludes_texture_pbr_quad_lowpoly(self):
        o = tripo.options_of(cand(generate_parts=True, texture=True, pbr=True, quad=True, smart_low_poly=True))
        self.assertTrue(o["generate_parts"])
        self.assertFalse(o["texture"]); self.assertFalse(o["pbr"])
        self.assertFalse(o["quad"]); self.assertFalse(o["smart_low_poly"])

    def test_face_limit_for(self):
        self.assertEqual(tripo.face_limit_for("v3.1-20260211", False, "2500000"), 1_500_000)
        self.assertEqual(tripo.face_limit_for("v3.1-20260211", True, "2500000"), 150_000)
        self.assertEqual(tripo.face_limit_for("P2-20260801", False, 10), 100)
        self.assertIsNone(tripo.face_limit_for("v3.1-20260211", False, ""))
        self.assertIsNone(tripo.face_limit_for("v3.1-20260211", False, "abc"))
        self.assertEqual(tripo.face_limit_for("unknown-model", False, 5000), 5000)

    def test_option_fields_cover_options(self):
        self.assertEqual({f["key"] for f in tripo.OPTION_FIELDS}, set(tripo.OPTION_DEFAULTS) - {"target_formats"})
        form = {}
        for fld in tripo.OPTION_FIELDS:
            v = tripo.OPTION_DEFAULTS[fld["key"]]
            if fld["type"] == "bool":
                if v:
                    form[f"opt__{fld['key']}"] = "on"
            else:
                form[f"opt__{fld['key']}"] = cloudtask.field_value_str(fld, v)
        out = cloudtask.parse_options(tripo.OPTION_FIELDS, form, tripo.OPTION_DEFAULTS)
        self.assertEqual(out, tripo.OPTION_DEFAULTS)


class BuildRequest(unittest.TestCase):
    def test_image_to_model_defaults(self):
        body = tripo.build_request(cand(), {}, {"input_image": "tok1"}, {})
        self.assertEqual(body["input"], "tok1")
        self.assertEqual(body["model"], "v3.1-20260211")
        self.assertTrue(body["texture"]); self.assertTrue(body["pbr"])
        self.assertEqual(body["texture_quality"], "standard")
        self.assertNotIn("face_limit", body)              # adaptive default: not sent
        self.assertNotIn("compress", body)                # "" = leave it out
        self.assertNotIn("inputs", body)

    def test_image_required(self):
        with self.assertRaises(tripo.TripoInput):
            tripo.build_request(cand(), {}, {}, {})

    def test_client_labels(self):
        body = tripo.build_request(cand(), {"input_face_num": "20000", "input_texture_resolution": 4096,
                                            "input_name": "x", "input_texture_prompt": "shiny"},
                                   {"input_image": "tok"}, {})
        self.assertEqual(body["face_limit"], 20000)
        self.assertEqual(body["texture_quality"], "detailed")
        self.assertNotIn("name", body)
        self.assertNotIn("texture_prompt", body)

    def test_admin_face_limit_is_a_default_not_a_pin(self):
        body = tripo.build_request(cand(face_limit=30000), {}, {"input_image": "t"}, {})
        self.assertEqual(body["face_limit"], 30000)
        body = tripo.build_request(cand(face_limit=30000), {"input_face_num": 500}, {"input_image": "t"}, {})
        self.assertEqual(body["face_limit"], 500)

    def test_texture_quality_buckets(self):
        self.assertEqual(tripo.texture_quality(1024), "standard")
        self.assertEqual(tripo.texture_quality(2048), "standard")
        self.assertEqual(tripo.texture_quality(4096), "detailed")
        self.assertEqual(tripo.texture_quality(8192), "extreme")
        self.assertEqual(tripo.texture_quality("extreme"), "extreme")
        self.assertEqual(tripo.texture_quality("zzz"), "standard")

    def test_multiview_shape_and_minimum(self):
        body = tripo.build_request(cand("multiview-to-model"), {},
                                   {"input_image_front": "f", "input_image_back": "b"}, {})
        self.assertEqual(body["inputs"], [{"front": "f"}, {"back": "b"}])
        self.assertNotIn("input", body)
        with self.assertRaises(tripo.TripoInput):          # fewer than two views
            tripo.build_request(cand("multiview-to-model"), {}, {"input_image_front": "f"}, {})
        with self.assertRaises(tripo.TripoInput):          # front missing
            tripo.build_request(cand("multiview-to-model"), {}, {"input_image_back": "b", "input_image_left": "l"}, {})

    def test_rig_body(self):
        body = tripo.build_request(cand("rig"), {}, {}, {"input_mesh_path": "mtok"})
        self.assertEqual(body, {"input": "mtok", "model": "v1.0-20240301", "rig_type": "biped",
                                "spec": "mixamo", "out_format": "glb"})
        body = tripo.build_request(cand("rig", target_formats=["fbx"], rig_model="v2.5-20260210", spec="tripo"),
                                   {"input_rig_type": "quadruped"}, {}, {"input_mesh_path": "mtok"})
        self.assertEqual((body["out_format"], body["model"], body["spec"], body["rig_type"]),
                         ("fbx", "v2.5-20260210", "tripo", "quadruped"))
        with self.assertRaises(tripo.TripoInput):
            tripo.build_request(cand("rig"), {"input_rig_type": "dragon"}, {}, {"input_mesh_path": "m"})
        with self.assertRaises(tripo.TripoInput):
            tripo.build_request(cand("rig"), {}, {}, {})

    def test_follow_up_bodies(self):
        self.assertEqual(tripo.build_rig_check("t"), {"input": "t"})
        self.assertEqual(tripo.build_convert("task_1", "fbx", False), {"input": "task_1", "format": "FBX", "with_animation": False})
        self.assertEqual(tripo.build_convert("task_1", "gltf", True), {"input": "task_1", "format": "GLTF", "with_animation": True})
        self.assertEqual(tripo.build_retarget("task_r", "preset:walk", "glb"),
                         {"input": "task_r", "animation": "preset:walk", "out_format": "glb"})
        self.assertEqual(tripo.clip_name("preset:walk"), "walk")
        self.assertEqual(tripo.clip_name("preset:quadruped:walk"), "quadruped_walk")

    def test_request_summary_is_a_copy(self):
        body = {"input": "tok", "model": "m"}
        s = tripo.request_summary(body)
        self.assertEqual(s, body)
        self.assertIsNot(s, body)


class PublicFields(unittest.TestCase):
    def test_image_endpoint(self):
        params, images, files = tripo.public_fields(cand())
        self.assertEqual([i["name"] for i in images], ["input_image"])
        self.assertEqual(images[0]["on_empty"], "required")
        self.assertEqual(files, [])
        names = {p["name"] for p in params}
        self.assertEqual(names, {"input_name", "input_face_num", "input_texture_resolution",
                                 "input_remove_background", "input_no_fingers"})
        tr = next(p for p in params if p["name"] == "input_texture_resolution")
        self.assertEqual(tr["default"], 2048)
        fn = next(p for p in params if p["name"] == "input_face_num")
        self.assertNotIn("default", fn)
        fn = next(p for p in tripo.public_fields(cand(face_limit=30000))[0] if p["name"] == "input_face_num")
        self.assertEqual(fn["default"], 30000)

    def test_multiview_endpoint(self):
        _, images, _ = tripo.public_fields(cand("multiview-to-model"))
        self.assertEqual([i["name"] for i in images],
                         ["input_image_front", "input_image_back", "input_image_left", "input_image_right"])
        self.assertEqual([i["on_empty"] for i in images], ["required", "skip", "skip", "skip"])

    def test_rig_endpoint(self):
        params, images, files = tripo.public_fields(cand("rig"))
        self.assertEqual(images, [])
        self.assertEqual(files, [{"name": "input_mesh_path", "required": True, "accept": ["glb"]}])
        rt = next(p for p in params if p["name"] == "input_rig_type")
        self.assertEqual(rt["default"], "biped")
        self.assertEqual(rt["choices"], list(tripo.RIG_TYPES))
        self.assertEqual({p["name"] for p in params}, {"input_rig_type", "input_name", "input_no_fingers"})


class ParseTask(unittest.TestCase):
    def test_running(self):
        st = tripo.parse_task({"status": "running", "progress": 40}, ["glb"])
        self.assertEqual((st.status, st.progress, st.error, st.downloads), ("running", 40, None, []))

    def test_success_generation(self):
        st = tripo.parse_task({"status": "success", "progress": 100,
                               "output": {"model_url": "u/m.glb", "rendered_image_url": "u/p.png"},
                               "credits_consumed": 30.0}, ["glb"])
        self.assertEqual(st.downloads, [("model.glb", "u/m.glb")])
        self.assertEqual(st.thumbnail, "u/p.png")
        self.assertEqual(st.credits, 30.0)

    def test_success_rig_names_by_out_format(self):
        st = tripo.parse_task({"status": "success", "output": {"model_url": "u/r.fbx"}}, ["fbx"], "rig")
        self.assertEqual(st.downloads, [("rigged.fbx", "u/r.fbx")])

    def test_success_without_model_url_raises(self):
        with self.assertRaises(tripo.TripoInput):
            tripo.parse_task({"status": "success", "output": {}}, ["glb"])

    def test_failed_cancelled_unknown_are_terminal(self):
        st = tripo.parse_task({"status": "failed", "error_code": 2018, "error_message": "too complex"}, ["glb"])
        self.assertIn("too complex", st.error)
        self.assertIn("2018", st.error)
        st = tripo.parse_task({"status": "cancelled"}, ["glb"])
        self.assertEqual(st.error, "cancelled")
        st = tripo.parse_task({"status": "banned"}, ["glb"])
        self.assertIn("banned", st.error)
        st = tripo.parse_task({"status": "success", "progress": "x", "output": {"model_url": "u"}}, ["glb"])
        self.assertEqual(st.progress, 0)


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: Test laufen lassen, Fehlschlag sehen**

Run: `/home/dev/projekte/llm-gateway/venv/bin/python -m unittest test_tripo -v`
Expected: `ModuleNotFoundError: No module named 'tripo'`

- [ ] **Step 3: `tripo.py` schreiben.** Struktur nach `meshy.py` (Docstring erklärt, dass Eingaben Tokens sind, keine Bytes). Kopf:

```python
"""Tripo3D (https://developers.tripo3d.ai/en/docs, API V3) as a generation backend — the PURE half.

Same shape as meshy.py, one difference that matters: Tripo takes NO inline bytes. Every
image and mesh is uploaded first (POST /v3/files → file_token, done by the adapter), so
`build_request` receives TOKENS where meshy.build_request receives bytes. No main/adapters
imports, no I/O. Covered by test_tripo.py."""
from __future__ import annotations

import copy
from typing import Optional

from cloudtask import TaskState

KIND, VENDOR, URL, API = "tripo", "Tripo", "https://openapi.tripo3d.ai", "/v3"
POLL_INTERVAL_DEFAULT, MAX_WAIT_DEFAULT = 2.0, 900
ENDPOINTS = ("image-to-model", "multiview-to-model", "rig")
RIG_ENDPOINT = "rig"
AI_MODELS = ("v3.1-20260211", "v3.0-20250812", "v2.5-20250123", "P1-20260311", "P2-20260801")
RIG_MODELS = ("v1.0-20240301", "v2.5-20260210")
RIG_TYPES = ("biped", "quadruped", "hexapod", "octopod", "avian", "serpentine", "aquatic")
SPECS = ("mixamo", "tripo")
FORMATS = ("glb", "fbx", "obj", "stl", "usdz", "3mf", "gltf")
RIG_FORMATS = ("glb", "fbx")
NATIVE_FORMAT = "glb"                    # what every generation task delivers itself
TEXTURE_QUALITIES = ("standard", "detailed", "extreme")
TEXTURE_ALIGNMENTS = ("original_image", "geometry")
GEOMETRY_QUALITIES = ("standard", "detailed")
ORIENTATIONS = ("default", "align_image")
COMPRESS = ("", "geometry")
FACE_MAX = {"v3.1-20260211": 1_500_000, "v3.0-20250812": 1_000_000, "v2.5-20250123": 500_000,
            "P1-20260311": 50_000, "P2-20260801": 50_000}
FACE_MAX_QUAD, _FACE_MIN, _FACE_MAX_UNKNOWN = 150_000, 100, 2_000_000
_RES_PX = {"standard": 2048, "detailed": 4096, "extreme": 8192}

OPTION_DEFAULTS: dict = {
    "texture": True, "pbr": True, "texture_quality": "standard",
    "texture_alignment": "original_image", "geometry_quality": "standard",
    "face_limit": None, "quad": False, "smart_low_poly": False, "generate_parts": False,
    "auto_size": False, "orientation": "default", "enable_image_autofix": False, "compress": "",
    "target_formats": ["glb"], "thumbnail": True,
    "rig_check": True, "rig_model": "v1.0-20240301", "rig_type": "biped", "spec": "mixamo",
    "animations": [],
}
_PASSTHROUGH = ("texture", "pbr", "texture_quality", "texture_alignment", "geometry_quality",
                "quad", "smart_low_poly", "generate_parts", "auto_size", "orientation",
                "enable_image_autofix")
SLOTS = {"image-to-model": ["input_image"],
         "multiview-to-model": ["input_image_front", "input_image_back", "input_image_left", "input_image_right"],
         "rig": []}
_VIEW_OF = {"input_image_front": "front", "input_image_back": "back",
            "input_image_left": "left", "input_image_right": "right"}
FILES = {"image-to-model": [], "multiview-to-model": [], "rig": ["input_mesh_path"]}
IGNORED_PARAMS = ("input_name", "input_remove_background", "input_no_fingers")


class TripoInput(RuntimeError):
    """A request Tripo cannot run (missing image, unsupported format, bad enum, fewer
    than two views) — a content error, final, never failed over."""
```

`options_of(cand)`: Overlay wie bei Meshy (nur bekannte Schlüssel); Normalisierung: `texture_quality`/`texture_alignment`/`geometry_quality`/`orientation`/`compress`/`spec`/`rig_type`/`rig_model` gegen ihre Tupel (sonst Default); `target_formats` gegen `FORMATS` (Rig-Endpunkt: `RIG_FORMATS`), leer → `["glb"]`; `face_limit = opt_face_limit(cand.get("model"), quad, v)`; `animations` = Liste nicht-leerer Strings; `generate_parts` → `texture = pbr = quad = smart_low_poly = False`.

`face_limit_for(model, quad, v)`: `None`/`""`/garbage → `None`; sonst `max(_FACE_MIN, min(cap, int(float(v))))` mit `cap = FACE_MAX_QUAD if quad else FACE_MAX.get(model, _FACE_MAX_UNKNOWN)`. `opt_face_limit`: gleiche Cap, aber außerhalb `[_FACE_MIN, cap]` → `None` (Admin-Eingabe wird nicht umgeschrieben — Begründung wie `meshy.opt_polycount`).

`build_request`:

```python
def build_request(cand: dict, values: dict, images: dict, files: Optional[dict] = None) -> dict:
    ep = endpoint_of(cand)
    opts = options_of(cand)
    if ep == RIG_ENDPOINT:
        tok = (files or {}).get("input_mesh_path")
        if not tok:
            raise TripoInput("`files.input_mesh_path` is required")
        rt = values.get("input_rig_type")
        if rt in (None, ""):
            rt = opts["rig_type"]
        if rt not in RIG_TYPES:
            raise TripoInput("`input_rig_type` must be one of " + ", ".join(RIG_TYPES))
        return {"input": tok, "model": opts["rig_model"], "rig_type": rt,
                "spec": opts["spec"], "out_format": opts["target_formats"][0]}
    m = cand.get("model")
    body: dict = {"model": m if m in AI_MODELS else AI_MODELS[0]}
    if ep == "image-to-model":
        tok = images.get("input_image")
        if not tok:
            raise TripoInput("`images.input_image` is required")
        body["input"] = tok
    else:
        if not images.get("input_image_front"):
            raise TripoInput("`images.input_image_front` is required")
        views = [{_VIEW_OF[s]: images[s]} for s in SLOTS[ep] if images.get(s)]
        if len(views) < 2:
            raise TripoInput("Tripo multiview needs at least two views (front plus one more)")
        body["inputs"] = views
    for k in _PASSTHROUGH:
        body[k] = opts[k]
    if opts["compress"]:
        body["compress"] = opts["compress"]
    if opts["face_limit"] is not None:          # admin budget — a DEFAULT; the client label below wins
        body["face_limit"] = opts["face_limit"]
    fn = face_limit_for(body["model"], opts["quad"], values.get("input_face_num"))
    if fn is not None:
        body["face_limit"] = fn
    tr = values.get("input_texture_resolution")
    if tr not in (None, ""):
        body["texture_quality"] = texture_quality(tr)
    return body
```

`texture_quality(px)`: Bucket-Name passt durch; sonst `int(float)`: ≤2048 → `standard`, ≤4096 → `detailed`, sonst `extreme`; garbage → `standard`.

`parse_task(task, formats, endpoint="image-to-model", options=None)`: `status = str(task.get("status") or "").lower()`; `progress` tolerant (nicht-int → 0); `credits = task.get("credits_consumed")`; `thumbnail = (task.get("output") or {}).get("rendered_image_url") or None`; `queued`/`running` → kein Fehler; `success` → `model_url` Pflicht (`TripoInput("Tripo task succeeded but has no model_url")`), `stem = "rigged" if endpoint == RIG_ENDPOINT else "model"`, `downloads = [(f"{stem}.{formats[0]}", url)]` — die Extra-Formate hängt der Adapter aus Convert-Tasks an; `failed` → `error = f"{msg} (code {code})"` wenn `error_code` gesetzt, sonst `msg` (`msg = task.get("error_message") or "failed"`); `cancelled` → `"cancelled"`; jeder andere Status (`banned`, `expired`, `unknown`, `""`) → `f"unknown task status {status!r}"` (terminal, wie Meshy).

`public_fields(cand)`: Rig: `params = [{"name": "input_rig_type", "type": "string", "default": opts["rig_type"], "choices": list(RIG_TYPES)}, {"name": "input_name", "type": "string", "default": ""}, {"name": "input_no_fingers", "type": "bool", "default": False}]`, `images = []`, `files = [{"name": "input_mesh_path", "required": True, "accept": ["glb"]}]`. Generierung: `input_name` (string ""), `input_face_num` (int; `default` NUR wenn `opts["face_limit"]` gesetzt), `input_texture_resolution` (int, default `_RES_PX[opts["texture_quality"]]`), `input_remove_background` (bool True), `input_no_fingers` (bool False); `images` wie Meshy (`on_empty: required` erster Slot, `skip` die anderen, `required` nur beim ersten).

`OPTION_FIELDS` (Reihenfolge = Formular; `"label": ""` = gleiche Zeile wie das vorige bool-Feld):

```python
OPTION_FIELDS: list = [
    {"key": "texture", "label": "texture", "type": "bool"},
    {"key": "pbr", "label": "", "type": "bool", "checkbox_text": "pbr (PBR maps)"},
    {"key": "texture_quality", "label": "texture quality", "type": "select", "choices": list(TEXTURE_QUALITIES),
     "hint": "Default when the client sends no <code>input_texture_resolution</code> (≤2048 → standard, "
             "≤4096 → detailed, else extreme). detailed +10, extreme +20 credits."},
    {"key": "texture_alignment", "label": "texture alignment", "type": "select", "choices": list(TEXTURE_ALIGNMENTS)},
    {"key": "geometry_quality", "label": "geometry quality", "type": "select", "choices": list(GEOMETRY_QUALITIES),
     "hint": "detailed: +20 credits, v3.0+ only."},
    {"key": "face_limit", "label": "face limit", "type": "int", "placeholder": "blank = Tripo adaptive default",
     "hint": "Face budget applied when the client sends no <code>input_face_num</code> (100 … the model's "
             "maximum: v3.1 1.5M, v3.0 1M, v2.5 500k, P-series 50k; 150k with quad). A client value still wins."},
    {"key": "quad", "label": "topology", "type": "bool", "checkbox_text": "quad (+5 credits; quads only travel well as FBX)"},
    {"key": "smart_low_poly", "label": "", "type": "bool", "checkbox_text": "smart_low_poly (+10 credits)"},
    {"key": "generate_parts", "label": "parts", "type": "bool",
     "checkbox_text": "generate_parts (+20 credits; forces texture/pbr/quad/smart_low_poly off — Tripo rejects the combination)"},
    {"key": "auto_size", "label": "input", "type": "bool", "checkbox_text": "auto_size (real-world scale)"},
    {"key": "enable_image_autofix", "label": "", "type": "bool"},
    {"key": "orientation", "label": "orientation", "type": "select", "choices": list(ORIENTATIONS)},
    {"key": "compress", "label": "compress", "type": "select", "choices": [("", "none"), ("geometry", "geometry")]},
    {"key": "thumbnail", "label": "thumbnail", "type": "bool",
     "checkbox_text": "deliver Tripo's rendered preview as an extra image artifact"},
    {"key": "rig_check", "label": "rig check", "type": "bool", "rig_only": True,
     "checkbox_text": "run the free rig-check first and refuse an unriggable mesh before the 25 credits"},
    {"key": "rig_model", "label": "rig model", "type": "select", "rig_only": True, "choices": list(RIG_MODELS),
     "hint": "v1.0: biped only, 90+ animation presets · v2.5: every rig type, 16 presets."},
    {"key": "rig_type", "label": "rig type", "type": "select", "rig_only": True, "choices": list(RIG_TYPES),
     "hint": "Default; a client <code>input_rig_type</code> wins."},
    {"key": "spec", "label": "skeleton", "type": "select", "rig_only": True,
     "choices": [("mixamo", "mixamo — Mixamo-compatible bone names"), ("tripo", "tripo — Tripo's own skeleton")]},
    {"key": "animations", "label": "animations", "type": "list", "rig_only": True,
     "placeholder": "preset:walk, preset:run",
     "hint": "Retarget clips delivered as extra files (<code>walk.glb</code>, …), 10 credits each; "
             "the preset catalogue depends on the rig model — see the Tripo docs."},
]
```

Hinweistexte (englisch, HTML wie bei Meshy): `BACKEND_HINT` — Key aus der Tripo-Console (API Keys); `max_concurrent` ≤ 10 (H-Serie / Animation) bzw. 5 (P-Serie), darüber 429 → Failover; `max wait s` deckelt die GESAMTE Generierung inkl. Rig-Check, Convert- und Clip-Tasks (blank = 900); `poll interval s` blank = 2; Credits: image/multiview 30 (20 ohne Textur), Rig 25, Clip 10, Convert 5; 1 Credit = 0.01 USD; Balance erscheint nach dem nächsten Health-Poll. `ENDPOINT_HINT` — `image-to-model` nimmt `input_image`; `multiview-to-model` nimmt `input_image_front` (Pflicht) + mindestens einen weiteren Slot (Tripo verlangt zwei Ansichten); `rig` nimmt `input_mesh_path` (glb), Rig-Check gratis, dann 25 Credits; `deliver formats`: alles außer dem nativen Format (glb; beim Rig das ERSTE angehakte) ist ein Convert-Task à 5 Credits. `CHAIN_HINT` — der Nachfolger kann `Tripo-Rig` sein (endpoint `rig`, Datei-Feld `input_mesh_path`), dann ist der `delivered rig type` `tripo` und die Bone-Namen folgen dem `skeleton` (mixamo Default); Tripo bettet Texturen ins GLB — `keep from this stage` meist leer (`preview.png` der eine Kandidat); jedes Extra-Format eines verketteten Alias ist ein bezahlter Convert, dessen Ergebnis verworfen wird.

- [ ] **Step 4: Tests laufen lassen**

Run: `/home/dev/projekte/llm-gateway/venv/bin/python -m unittest test_tripo test_cloudtask -v`
Expected: alle PASS.

- [ ] **Step 5: Commit**

```bash
git add tripo.py test_tripo.py
git commit -m "tripo: the pure half — label table, request builders, task parser, option schema"
```

---

### Task 3: `CloudTaskAdapter` in `adapters.py` — Meshy auf die Basisklasse refaktorieren (Verhalten unverändert)

**Files:**
- Modify: `adapters.py` — Zeilen 40 (`import meshy` → plus `import cloudtask, tripo`), 69–80 (Ausnahmen), 229–230 (`NormalizedRequest.meshy`), 1553–1565 (`public_fields`), 3023–3244 (`MeshyAdapter` → Basisklasse + Unterklasse), 3247–3260 (Registry)
- Modify: `main.py` — die drei `meshy=cand.get("meshy")`-Stellen (`_run_chain` Zeilen ~2970 und ~3038, `run_generation` ~3386) → `cloud=adapters.cloud_block(cand)`
- Test: `test_meshy_adapter.py` (bestehend, unverändert grün), `test_cloudtask.py` (Ergänzung unten)

**Interfaces:**
- Consumes: `meshy.KIND/VENDOR/RIG_ENDPOINT/POLL_INTERVAL_DEFAULT/MAX_WAIT_DEFAULT`, `cloudtask.TaskState` (Task 1); `tripo` nur importiert (Task 2), noch kein Adapter.
- Produces (Task 4–7 nutzen es):
  - `class CloudNoCredits(ConnectionError)` und `class CloudBusy(ConnectionError)`, beide mit `__init__(self, msg: str = "", vendor: str = "cloud")` und Attribut `.vendor`; `MeshyNoCredits = CloudNoCredits`, `MeshyBusy = CloudBusy` (Aliase, Modul-Ebene).
  - `CLOUD_TYPES: frozenset[str]` (aus `ADAPTERS`: Klassen mit `cloud = True`), `CLOUD_MODULES: dict[str, module]` (`{"meshy": meshy, "tripo": tripo}` — Task 4 trägt tripo über den Adapter ein; in Task 3 explizit `{"meshy": meshy, "tripo": tripo}` setzen, damit `cloud_kind` schon Tripo-Kandidaten erkennt).
  - `cloud_kind(cand: dict) -> Optional[str]`: erster Schlüssel aus `CLOUD_MODULES`, für den `cand.get(k) is not None`.
  - `cand_kind(cand) -> str` = `cloud_kind(cand) or "comfyui"`; `backend_kind(b: dict) -> str` = `b.get("type")` wenn in `CLOUD_TYPES`, sonst `"comfyui"`.
  - `cloud_module(kind: str)` = `CLOUD_MODULES[kind]`; `cloud_block(cand) -> Optional[dict]` = `cand.get(kind)` (Dict) oder `None`.
  - `NormalizedRequest.cloud: Optional[dict] = None` (ersetzt `meshy`).
  - `class CloudTaskAdapter(BackendAdapter)`: Klassenattribute `cloud = True`, `serves_generation = True`, `mod` (Modul), `type = mod.KIND`; Instanz: `credits`, `credits_at`, `vendor`. Methoden: `_headers()`, `generate(req)`, `_poll(client, endpoint, task_id, formats, opts, poll_interval, max_wait) -> TaskState`, `_create(client, url, body, endpoint) -> str`, `_download(client, url) -> bytes` (static), Chain-Hooks. Vendor-Hooks (abstrakt via `NotImplementedError`): `discover`, `_run(client, req, cand, opts, poll_interval, max_wait) -> RunResult`, `_task_request(client, endpoint, task_id)`, `_task_body(r) -> dict`, `_classify_create(r) -> Optional[str]`, `_task_id_of(js) -> str`, `_msg(r) -> str`.
  - `@dataclass RunResult: task_id: str; endpoint: str; body: dict; state: TaskState; extra_meta: dict = {}`.
  - `class MeshyAdapter(CloudTaskAdapter)` — identisches Außenverhalten wie heute.

- [ ] **Step 1: Zusatztest schreiben** (an `test_cloudtask.py` anhängen — die Helfer sind pur genug, um ohne Server geprüft zu werden):

```python
class AdapterHelpers(unittest.TestCase):
    def test_kinds(self):
        import adapters
        self.assertEqual(adapters.cloud_kind({"meshy": {}}), "meshy")
        self.assertEqual(adapters.cloud_kind({"tripo": {"endpoint": "rig"}}), "tripo")
        self.assertIsNone(adapters.cloud_kind({"workflow_json": {}}))
        self.assertEqual(adapters.cand_kind({}), "comfyui")
        self.assertEqual(adapters.backend_kind({"type": "meshy"}), "meshy")
        self.assertEqual(adapters.backend_kind({"type": "comfyui"}), "comfyui")
        self.assertEqual(adapters.backend_kind({"type": "openai"}), "comfyui")
        self.assertIn("meshy", adapters.CLOUD_TYPES)
        self.assertTrue(adapters.CLOUD_TYPES <= adapters.GEN_TYPES)
        self.assertIs(adapters.cloud_module("meshy"), meshy)
        self.assertEqual(adapters.cloud_block({"tripo": {"endpoint": "rig"}}), {"endpoint": "rig"})
        self.assertIsNone(adapters.cloud_block({}))

    def test_exception_aliases_and_vendor(self):
        import adapters
        self.assertIs(adapters.MeshyNoCredits, adapters.CloudNoCredits)
        self.assertIs(adapters.MeshyBusy, adapters.CloudBusy)
        e = adapters.CloudNoCredits("x", vendor="Tripo")
        self.assertIsInstance(e, ConnectionError)
        self.assertEqual(e.vendor, "Tripo")
        self.assertEqual(adapters.CloudBusy("y").vendor, "cloud")
```

- [ ] **Step 2: Test laufen lassen, Fehlschlag sehen**

Run: `/home/dev/projekte/llm-gateway/venv/bin/python -m unittest test_cloudtask.AdapterHelpers -v`
Expected: FAIL (`AttributeError: module 'adapters' has no attribute 'cloud_kind'`).

- [ ] **Step 3: Ausnahmen und Helfer in `adapters.py`** — Zeilen 69–80 ersetzen:

```python
class CloudNoCredits(ConnectionError):
    """A cloud task account has no credits (balance 0 on discovery; Meshy 402 / Tripo
    403+2010 on submit). A ConnectionError on purpose: _GEN_FAILOVER_ERRORS moves the job
    to the next candidate without touching that tuple; _fault_label names it apart and
    `vendor` says whose account."""
    def __init__(self, msg: str = "", vendor: str = "cloud"):
        super().__init__(msg)
        self.vendor = vendor


class CloudBusy(ConnectionError):
    """The cloud refused the task with 429: the account's concurrency limit is full —
    other API keys of the same account fill it too, so the gateway's own max_concurrent
    cannot rule it out. Failover-class."""
    def __init__(self, msg: str = "", vendor: str = "cloud"):
        super().__init__(msg)
        self.vendor = vendor


MeshyNoCredits, MeshyBusy = CloudNoCredits, CloudBusy      # pre-Tripo names (tests, main)
```

Nach dem `import meshy` (Zeile 40): `import cloudtask` und `import tripo`. Direkt hinter `NormalizedRequest` (nach der Dataclass) die Kind-Helfer:

```python
# ── cloud task kinds (Meshy, Tripo): the one seam main/admin ask "which kind?" ──
CLOUD_MODULES: dict = {meshy.KIND: meshy, tripo.KIND: tripo}     # kind → pure module


def cloud_kind(cand: dict) -> Optional[str]:
    """The cloud kind of a generation alias candidate (the key its block sits under),
    None for a ComfyUI (workflow) candidate."""
    for k in CLOUD_MODULES:
        if (cand or {}).get(k) is not None:
            return k
    return None


def cand_kind(cand: dict) -> str:
    return cloud_kind(cand) or "comfyui"


def backend_kind(b: dict) -> str:
    """A generation backend's kind: its type for a cloud backend, else comfyui. A
    candidate may run on a backend iff cand_kind(cand) == backend_kind(b) — backends are
    keyed (name, type), so a bare-name match could route a cloud alias onto a GPU box."""
    t = (b or {}).get("type")
    return t if t in CLOUD_MODULES else "comfyui"


def cloud_module(kind: str):
    return CLOUD_MODULES[kind]


def cloud_block(cand: dict) -> Optional[dict]:
    k = cloud_kind(cand)
    return dict(cand.get(k) or {}) if k else None
```

`NormalizedRequest`: Feld `meshy: Optional[dict] = None` → `cloud: Optional[dict] = None` (Kommentar: „cloud alias candidate block {endpoint, options} of whatever kind — `cloud_block(cand)`; None on ComfyUI candidates"). `public_fields` (Zeile 1563): `if cand.get("meshy") is not None: return meshy.public_fields(cand)` → `k = cloud_kind(cand); if k: return cloud_module(k).public_fields(cand)`. Am Ende von `adapters.py`: `CLOUD_TYPES: frozenset = frozenset(t for t, cls in ADAPTERS.items() if getattr(cls, "cloud", False))` direkt nach `GEN_TYPES`.

- [ ] **Step 4: `CloudTaskAdapter` schreiben und `MeshyAdapter` darauf setzen.** Den Block `# ── Meshy.ai (cloud image → 3D)` (Zeilen 3023–3244) ersetzen durch die Basisklasse + die schlanke Meshy-Unterklasse. Die Grace-Logik in `_poll`, die Timeout-Berechnung in `_create`, `_download` und die Chain-Hooks werden aus dem heutigen `MeshyAdapter` **übernommen** (Kommentare mitnehmen — sie tragen die gemessenen Begründungen), nur die Meshy-spezifischen Zeilen werden zu Hooks:

```python
# ── Cloud task backends (Meshy, Tripo): POST a task, poll, download ─────────────

_CLOUD_DISCOVERY_TIMEOUT = 8.0
_CLOUD_HTTP_TIMEOUT = 30.0
_CLOUD_DOWNLOAD_TIMEOUT = 120.0


@dataclass
class RunResult:
    """What a vendor's `_run` hands back: the PRIMARY task (its id, endpoint and the body
    sent) and the final TaskState whose `downloads` already include every follow-up task's
    file (converts, clips). `extra_meta` is merged into the job meta."""
    task_id: str
    endpoint: str
    body: dict
    state: "cloudtask.TaskState"
    extra_meta: dict = field(default_factory=dict)


class CloudTaskAdapter(BackendAdapter):
    """Everything a cloud task API shares: the in-flight slot, the create/poll/download
    loop with its grace rules, the job meta and the chain roles. A vendor subclass
    supplies the URLs, the response envelope and the run order (see the hooks)."""

    cloud = True
    serves_generation = True
    mod = meshy                      # the pure module; subclasses override

    def __init__(self, backend: dict, ctx: AdapterContext):
        super().__init__(backend, ctx)
        self.credits: Optional[float] = None
        self.credits_at: float = 0.0
        self.vendor: str = self.mod.VENDOR

    def _headers(self) -> dict:
        key = (self.backend.get("api_key") or "").strip()
        return {"Authorization": f"Bearer {key}"} if key else {}

    # ── vendor hooks ──
    async def discover(self, client: httpx.AsyncClient) -> Capabilities:
        raise NotImplementedError

    async def _run(self, client, req, cand: dict, opts: dict, poll_interval: float, max_wait: float) -> RunResult:
        raise NotImplementedError

    async def _task_request(self, client, endpoint: str, task_id: str):
        raise NotImplementedError

    def _task_body(self, r) -> dict:
        """The task object out of a 200 poll response; raise _TaskVerdict for a body that
        is a verdict about the task (Tripo's code != 0)."""
        return r.json() or {}

    def _classify_create(self, r) -> Optional[str]:
        """'nocredits' | 'busy' | 'server' | 'rejected' | None (= accepted) for a create response."""
        raise NotImplementedError

    def _task_id_of(self, js: dict) -> str:
        raise NotImplementedError

    def _msg(self, r) -> str:
        try:
            return str((r.json() or {}).get("message") or r.text[:200])
        except Exception:
            return r.text[:200]

    def _thumb_name(self) -> str:
        return "preview.png"
```

`generate(req)`:

```python
    async def generate(self, req: NormalizedRequest) -> GenOutput:
        b, mod = self.backend, self.mod
        cand = {"model": req.real_model, mod.KIND: req.cloud or {}}
        endpoint = mod.endpoint_of(cand)
        opts = mod.options_of(cand)
        poll_interval = float(b.get("poll_interval", mod.POLL_INTERVAL_DEFAULT))
        max_wait = float(b.get("max_wait", mod.MAX_WAIT_DEFAULT))
        if not req.slot_held:
            self.ctx.inflight_inc(self.bid)
        started = time.monotonic()
        log_on = self.ctx.log_enabled()
        try:
            async with httpx.AsyncClient(timeout=_CLOUD_HTTP_TIMEOUT) as client:
                run = await self._run(client, req, cand, opts, poll_interval, max_wait)
                state = run.state
                blobs = []
                for name, url in state.downloads:          # the module named them
                    data = await self._download(client, url)
                    mime, kind = _mime_and_kind(name)
                    blobs.append(GenBlob(data=data, mime=mime, kind=kind, name=name))
                if endpoint != mod.RIG_ENDPOINT and opts.get("thumbnail") and state.thumbnail:
                    try:
                        thumb = await self._download(client, state.thumbnail)
                        blobs.append(GenBlob(data=thumb, mime="image/png", kind="image", name=self._thumb_name()))
                    except Exception as e:               # a preview is a courtesy, the mesh is the job
                        logger.warning(f"[{self.name}] {mod.KIND} thumbnail download failed: {e}")
        finally:
            if not req.slot_held:
                self.ctx.inflight_dec(self.bid)
        elapsed_ms = int((time.monotonic() - started) * 1000)
        if log_on:
            logger.info(f"← [{self.name}] {len(blobs)} artifact(s) in {elapsed_ms} ms, {state.credits} credits")
        meta = {"backend": self.name, "cloud": mod.KIND, "cloud_task_id": run.task_id,
                "endpoint": endpoint, "ai_model": run.body.get("ai_model") or run.body.get("model"),
                "request": mod.request_summary(run.body), "consumed_credits": state.credits,
                "elapsed_ms": elapsed_ms, **run.extra_meta}
        if endpoint == mod.RIG_ENDPOINT:
            # A standalone rig job is a rig delivery like a chain's stage 2 — main._job_view
            # reads meta["rig"]; without it the one endpoint that always rigs shows none.
            meta.setdefault("rig", mod.KIND)
        return GenOutput(blobs=blobs, meta=meta)
```

`_create(client, url, body, endpoint) -> str`: der heutige POST-Block (JSON einmal serialisieren, größenskaliertes Timeout `httpx.Timeout(connect=30.0, read=max(120.0, mb*4), write=max(60.0, mb*4), pool=30.0)`), dann `verdict = self._classify_create(pr)`: `"nocredits"` → `raise CloudNoCredits(f"{self.vendor}: {self._msg(pr)}", vendor=self.vendor)`; `"busy"` → `CloudBusy(f"{self.vendor} queue full: {…}", vendor=…)`; `"server"` → `ConnectionError(f"{self.vendor} {pr.status_code}: {…}")`; `"rejected"` → `RuntimeError(f"{self.vendor} rejected the task ({pr.status_code}): {…}")`; `None` → `task_id = self._task_id_of(pr.json() or {})`, leer → `RuntimeError(f"{self.vendor} returned no task id")`; Log `→ [{name}] {kind} {endpoint} task {id}`.

`_poll(client, endpoint, task_id, formats, opts, poll_interval, max_wait) -> TaskState`: identisch zum heutigen Code mit `r = await self._task_request(client, endpoint, task_id)`, `task = self._task_body(r)` (eine `_TaskVerdict(RuntimeError)`-Ausnahme aus `_task_body` zählt wie ein 4xx: `client_errs += 1`, bei 3 → `RuntimeError(f"{vendor} task {id}: {e}")`), `state = self.mod.parse_task(task, formats, endpoint, options=opts)`, Erfolg = `state.error is None and state.downloads` → **nein**: Erfolg = Status terminal-ok. Damit beide Module ohne Statusnamen-Wissen funktionieren, definiert jedes Modul `SUCCESS_STATUS` (`meshy.SUCCESS_STATUS = "SUCCEEDED"`, `tripo.SUCCESS_STATUS = "success"`; in Task 1 bzw. 2 ergänzen — Task 2 ist noch nicht committed? Doch: Task 2 liegt vor Task 3; `tripo.SUCCESS_STATUS` in Task 2 mit aufnehmen, `meshy.SUCCESS_STATUS` hier in Task 3 nachtragen). Alle Meldungen mit `self.vendor` statt „Meshy". `max_wait` misst ab dem Aufruf von `_poll`; für Tripo (mehrere Tasks) gibt `_run` ein **Restbudget** weiter (`deadline`-Parameter: `_poll(..., max_wait=deadline - time.monotonic())`).

`_download`: unverändert (Kommentar: kein Auth-Header — signierte URLs).

Chain-Hooks — heutiger Code, generalisiert:

```python
    def chain_export(self, cand, succ, params, prefix) -> ChainExport:
        mod = self.mod
        if mod.endpoint_of(cand) == mod.RIG_ENDPOINT:
            return ChainExport("", error=f"chain: a {self.vendor} rigging alias cannot be stage 1 — "
                                         f"it rigs an existing mesh; use an image-to-3d alias")
        opts = mod.options_of({mod.KIND: cand.get(mod.KIND) or {}, "model": cand.get("model")})
        if "glb" not in opts["target_formats"]:
            return ChainExport("", error=f"chain: {self.vendor} stage 1 must deliver glb — add it to "
                                         f"target_formats (now {opts['target_formats']})")
        return ChainExport(f"{prefix}.glb")

    async def chain_take_mesh(self, out, export, want_bytes):
        blob = next((b for b in (out.blobs or []) if (b.name or "") == "model.glb"), None)
        if blob is None:
            return None
        return blob.data if want_bytes else b""

    async def chain_feed_mesh(self, req2, backend2, mesh_param, mesh_name, mesh_bytes, outdir) -> str:
        if mesh_bytes is None:
            raise RuntimeError(f"chain: a {self.vendor} stage 2 needs the mesh bytes (upload relay)")
        req2.upload_files[mesh_param] = (mesh_name, mesh_bytes)
        return f"<upload:{mesh_name} ({len(mesh_bytes) / (1024 * 1024):.1f} MB)>"
```

**Achtung Tripo-Stage-1**: `chain_take_mesh` sucht `model.glb` — Tripos `parse_task` liefert genau diesen Namen (Task 2), also passt es für beide Kinds.

`MeshyAdapter(CloudTaskAdapter)`: `mod = meshy`, `type = "meshy"`; `_api(path)`; `discover` (heutiger Code, `CloudNoCredits(..., vendor=self.vendor)`); `_run`: `body = meshy.build_request(cand, _gen_values(req), req.upload_images or {}, req.upload_files or {})`, `task_id = await self._create(client, self._api(f"/{endpoint}"), body, endpoint)`, `state = await self._poll(client, endpoint, task_id, opts["target_formats"], opts, poll_interval, max_wait)`, `return RunResult(task_id, endpoint, body, state, {"meshy_task_id": task_id})` (der alte Schlüssel bleibt im Meta — bestehende Job-Zeilen und die Job-Ansicht lesen ihn); `_task_request` = `client.get(self._api(f"/{endpoint}/{task_id}"), headers=self._headers())`; `_classify_create`: 402 → `nocredits`, 429 → `busy`, ≥500 → `server`, nicht-2xx → `rejected`, sonst `None`; `_task_id_of` = `str(js.get("result") or "")`. Das Meta-Feld `rig` für Meshy bleibt `"meshy"` (Basisklasse setzt `mod.KIND`).

Registry: `"meshy": MeshyAdapter` bleibt; `_meshy_msg` entfällt (Basis `_msg`).

- [ ] **Step 5: `main.py` — die drei `meshy=`-Übergaben** (grep `meshy=cand.get("meshy")` / `meshy=stage1_cand.get("meshy")` / `meshy=s2.get("meshy")`) → `cloud=adapters.cloud_block(<cand>)`.

- [ ] **Step 6: Tests laufen lassen**

Run: `/home/dev/projekte/llm-gateway/venv/bin/python -m py_compile adapters.py main.py && /home/dev/projekte/llm-gateway/venv/bin/python -m unittest test_cloudtask test_meshy test_meshy_adapter -v`
Expected: alle PASS. `test_meshy_adapter.py` prüft u. a. Meta-Schlüssel `meshy_task_id`, `rig: "meshy"`, Slot inc/dec, 402/429-Klassen — sie müssen ohne Änderung am Test grün sein. Falls ein Test auf `_meshy_msg` oder ein anderes internes Symbol zugreift: das Symbol als Alias erhalten, nicht den Test ändern.

- [ ] **Step 7: Commit**

```bash
git add adapters.py main.py test_cloudtask.py meshy.py
git commit -m "adapters: CloudTaskAdapter base — Meshy becomes one cloud kind, behaviour unchanged"
```

---

### Task 4: `TripoAdapter` mit HTTP-Stub-Tests

**Files:**
- Modify: `adapters.py` (nach `MeshyAdapter`: `TripoAdapter`; Registry `"tripo": TripoAdapter`)
- Create: `test_tripo_adapter.py`

**Interfaces:**
- Consumes: `CloudTaskAdapter`, `RunResult`, `CloudNoCredits`, `CloudBusy`, `_upload_timeout_for` (Task 3); `tripo.*` (Task 2).
- Produces: `class TripoAdapter(CloudTaskAdapter)`, `type = "tripo"`; Meta-Zusatz `tasks: [{role, task_id, credits}]`, `rig_spec`, `rig_type` (nur Rig), `meshy_task_id` NICHT.

- [ ] **Step 1: Failing tests schreiben** — `test_tripo_adapter.py` nach dem Muster von `test_meshy_adapter.py` (Stub-Server mit `BaseHTTPRequestHandler`, `_ctx()` mit Zählern, `_backend(port, **kw)`, `_req(**kw)` → `NormalizedRequest(task="img2mesh", real_model="v3.1-20260211", cloud={...}, upload_images={...}, upload_files={...})`). Der Stub:

```python
class _Stub(BaseHTTPRequestHandler):
    """Scripted Tripo V3: POST /v3/files → token; POST /v3/generation/* etc. → task id;
    GET /v3/tasks/<id> walks `script[<id>]` (last entry repeats); assets under /asset/."""
    script: dict = {}            # task id → list of `data` objects for successive polls
    posted: list = []            # (path, json body)
    uploads: list = []           # (filename, nbytes) seen at /v3/files
    balance = 500.0
    create_status = 200          # HTTP status of a task create
    create_code = 0              # envelope code of a task create
    seq = 0

    def log_message(self, *a):
        pass

    def _json(self, code, obj):
        body = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        if self.path == "/v3/account/balance":
            return self._json(200, {"code": 0, "data": {"balance": _Stub.balance, "frozen": 0}})
        if self.path.startswith("/asset/"):
            payload = GLB if self.path.endswith((".glb", ".fbx")) else PNG
            self.send_response(200); self.send_header("Content-Length", str(len(payload))); self.end_headers()
            return self.wfile.write(payload)
        if self.path.startswith("/v3/tasks/"):
            tid = self.path.rsplit("/", 1)[1]
            seq = _Stub.script.get(tid)
            if not seq:
                return self._json(404, {"code": 2001, "message": "task not found", "suggestion": ""})
            t = seq.pop(0) if len(seq) > 1 else seq[0]
            return self._json(200, {"code": 0, "data": {"task_id": tid, **t}})
        self._json(404, {"code": 1, "message": "nope"})

    def do_POST(self):
        n = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(n)
        if self.path == "/v3/files":
            ctype = self.headers.get("Content-Type", "")
            m = re.search(rb'filename="([^"]+)"', raw)
            _Stub.uploads.append((m.group(1).decode() if m else "", len(raw), ctype.split(";")[0]))
            _Stub.seq += 1
            return self._json(200, {"code": 0, "data": {"file_token": f"tok{_Stub.seq}"}})
        body = json.loads(raw or b"{}")
        _Stub.posted.append((self.path, body))
        if _Stub.create_status != 200 or _Stub.create_code != 0:
            return self._json(_Stub.create_status, {"code": _Stub.create_code, "message": "refused",
                                                    "suggestion": "top up"})
        _Stub.seq += 1
        return self._json(200, {"code": 0, "data": {"task_id": f"task_{_Stub.seq}"}})
```

Die Tests (jeder mit frischem `_Stub`-Zustand in `setUp`):

1. `test_discover_reads_balance_and_zero_is_no_credits` — `discover` setzt `credits == 500`, Modelle = `set(tripo.AI_MODELS)`; `balance = 0` → `adapters.CloudNoCredits` mit `.vendor == "Tripo"`.
2. `test_image_to_model_uploads_then_creates_then_downloads` — `upload_images={"input_image": PNG}`; Script `task_2: [{"status": "running", "progress": 10}, {"status": "success", "output": {"model_url": "http://127.0.0.1:<port>/asset/m.glb", "rendered_image_url": ".../asset/p.png"}, "credits_consumed": 30}]` (Task-Id ist `task_2`, weil der Upload `seq` 1 verbraucht — im Test statt Vorhersage: die Task-Id aus `_Stub.posted` ist nicht bekannt, bevor der Poll startet → `script` per Default-Factory: benutze `script = {"*": [...]}` und im Stub `seq = _Stub.script.get(tid) or _Stub.script.get("*")`). Erwartung: `_Stub.uploads == [("input_image.png", ANY, "multipart/form-data")]`, `posted[0][0] == "/v3/generation/image-to-model"`, `posted[0][1]["input"] == "tok1"`, Blobs `["model.glb", "preview.png"]`, `meta["cloud"] == "tripo"`, `meta["cloud_task_id"]` gesetzt, `"meshy_task_id" not in meta`, `meta["consumed_credits"] == 30`, Slot inc == dec == 1, Authorization-Header `Bearer <key>` auf dem Upload UND dem Create.
3. `test_multiview_needs_two_views_before_any_upload` — nur `input_image_front` → `tripo.TripoInput`, `_Stub.uploads == []` (die Prüfung passiert im Modul, aber der Adapter muss die Uploads NACH der Slot-Zählung machen: implementiere `_run` so, dass bei Multiview zuerst `tripo.build_request` mit Platzhalter-Tokens validiert? Nein — einfacher und ehrlich: der Adapter zählt die belegten Slots vor dem Upload selbst (`len(present) < 2` → `TripoInput`), damit kein Upload für einen Request stattfindet, der ohnehin abgewiesen wird). Und ein Nicht-PNG/JPEG (`b"RIFF...WEBP"`) → `TripoInput` ohne Upload.
4. `test_create_403_2010_is_no_credits_and_429_is_busy` — `create_status=403, create_code=2010` → `CloudNoCredits`; `create_status=429, create_code=2000` → `CloudBusy`; `create_status=500` → `ConnectionError` (kein `CloudNoCredits`); `create_status=200, create_code=2002` → `RuntimeError` (final, Nachricht enthält „refused" und „top up").
5. `test_failed_task_is_final` — Script `[{"status": "failed", "error_code": 2018, "error_message": "too complex"}]` → `RuntimeError` mit „too complex"; Slot dec == 1.
6. `test_poll_code_nonzero_three_times_is_final` — Stub-Variante: `GET /v3/tasks/*` antwortet 200 mit `{"code": 2001, "message": "gone"}` → nach 3 Polls `RuntimeError` (nicht `TimeoutError`); `poll_interval=0.01`.
7. `test_max_wait_timeout` — Script nur `running`; `max_wait=0.05` → `TimeoutError`, Nachricht nennt `max_wait`.
8. `test_rig_check_refuses_before_rig` — `cloud={"endpoint": "rig", "options": {...}}`, `upload_files={"input_mesh_path": ("m.glb", GLB)}`; Script für den Rig-Check-Task `[{"status": "success", "output": {"riggable": False, "rig_type": "quadruped"}}]` → `RuntimeError` mit „not riggable" und „quadruped"; `posted` enthält NUR `/v3/animations/rig-check`, kein `/v3/animations/rig`.
9. `test_rig_with_clips_and_extra_format` — Optionen `target_formats=["glb", "fbx"], animations=["preset:walk"], spec="mixamo"`; Script generisch `"*": [{"status": "success", "output": {"model_url": ".../asset/r.glb", "riggable": True, "rig_type": "biped"}, "credits_consumed": 25}]` (der Stub liefert für jeden Task dasselbe; Dateinamen kommen aus dem Adapter). Erwartung: `posted`-Pfade in dieser Reihenfolge: `rig-check`, `rig`, `convert` (`format == "FBX"`, `with_animation is True`), `retarget` (`animation == "preset:walk"`, `out_format == "glb"`); Rig-Body `{"input": "tok1", "model": "v1.0-20240301", "rig_type": "biped", "spec": "mixamo", "out_format": "glb"}`; Blobs `["rigged.glb", "rigged.fbx", "walk.glb"]`; `meta["rig"] == "tripo"`, `meta["rig_spec"] == "mixamo"`, `meta["rig_type"] == "biped"`, `meta["consumed_credits"] == 100` (4 Tasks × 25 im Stub — der Stub gibt überall 25 zurück; die Summe zählt), `len(meta["tasks"]) == 4` mit `role` ∈ `rig-check, rig, convert:fbx, clip:preset:walk`. Kein `preview.png` (Rig).
10. `test_generation_extra_format_convert_failure_fails_job` — `target_formats=["glb", "obj"]`, Stub-Script pro Task-Id: erste Task success, Convert-Task `failed` → `RuntimeError` mit „convert" und „obj". (Task-Ids sind sequenziell `task_<n>`; im Stub `script` per Id setzen: `{"task_2": [success], "task_3": [failed]}` — der Upload nimmt `seq` 1.)
11. `test_chain_hooks` — `chain_export` refuses `rig`-Endpunkt und einen Kandidaten ohne `glb`; akzeptiert mit `glb` → `mesh_name == "gwchain_x.glb"`; `chain_take_mesh` findet `model.glb`; `chain_feed_mesh` ohne Bytes → `RuntimeError`, mit Bytes → `req2.upload_files[mesh_param] == (name, bytes)`.
12. `test_upload_transport_error_fails_over` — Backend-URL auf einen geschlossenen Port → `ConnectionError` (httpx.ConnectError ist eine Unterklasse? Nein: `httpx.ConnectError` erbt von `httpx.TransportError`, nicht von `ConnectionError`. `_GEN_FAILOVER_ERRORS` in `main` enthält `httpx.HTTPError`? Prüfe `grep -n "_GEN_FAILOVER_ERRORS" main.py` und lasse den Adapter Upload-Transportfehler als `ConnectionError(f"Tripo upload failed: {e}")` neu werfen, damit die Klasse eindeutig ist).

- [ ] **Step 2: Test laufen lassen, Fehlschlag sehen**

Run: `/home/dev/projekte/llm-gateway/venv/bin/python -m unittest test_tripo_adapter -v`
Expected: FAIL (`AttributeError: module 'adapters' has no attribute 'TripoAdapter'`).

- [ ] **Step 3: `TripoAdapter` schreiben**

```python
class _TaskVerdict(RuntimeError):
    """A 200 poll whose body says the task cannot be read (Tripo `code != 0`) — counted
    like a 4xx by CloudTaskAdapter._poll (three in a row = final)."""


class TripoAdapter(CloudTaskAdapter):
    """Tripo3D V3: upload every input to /v3/files first (no inline bytes), then one
    task per endpoint under /v3/generation|animations|models, all polled at /v3/tasks/{id}.
    A rig job is several tasks (rig-check → rig → converts → clips) under ONE max_wait."""

    mod = tripo
    type = "tripo"

    def _api(self, path: str) -> str:
        return f"{self.backend['url'].rstrip('/')}{tripo.API}{path}"

    async def discover(self, client):
        r = await client.get(self._api("/account/balance"), headers=self._headers(),
                             timeout=_CLOUD_DISCOVERY_TIMEOUT)
        r.raise_for_status()
        js = r.json() or {}
        if js.get("code", 0) != 0:
            raise RuntimeError(f"Tripo balance: {js.get('message') or js.get('code')}")
        bal = float((js.get("data") or {}).get("balance") or 0)
        self.credits, self.credits_at = bal, time.time()
        if bal <= 0:
            raise CloudNoCredits("no credits left on the Tripo account", vendor=self.vendor)
        return Capabilities(models=set(tripo.AI_MODELS), pricing={}, loras=set())

    def _msg(self, r) -> str:
        try:
            js = r.json() or {}
            m = str(js.get("message") or "")
            s = str(js.get("suggestion") or "")
            return (m + (f" — {s}" if s else "")) or r.text[:200]
        except Exception:
            return r.text[:200]

    def _classify_create(self, r):
        code = None
        try:
            code = (r.json() or {}).get("code")
        except Exception:
            pass
        if r.status_code == 403 and code == 2010:
            return "nocredits"
        if r.status_code == 429:
            return "busy"
        if r.status_code >= 500:
            return "server"
        if not 200 <= r.status_code < 300 or (code not in (None, 0)):
            return "rejected"
        return None

    def _task_id_of(self, js):
        return str(((js.get("data") or {}).get("task_id")) or "")

    async def _task_request(self, client, endpoint, task_id):
        return await client.get(self._api(f"/tasks/{task_id}"), headers=self._headers())

    def _task_body(self, r):
        js = r.json() or {}
        if js.get("code", 0) != 0:
            raise _TaskVerdict(f"{js.get('message') or 'code ' + str(js.get('code'))}")
        return js.get("data") or {}
```

`_upload(client, name, data) -> str`: `client.post(self._api("/files"), files={"file": (name, data, mime)}, headers=self._headers(), timeout=_upload_timeout_for(len(data)))`; `httpx.HTTPError` → `ConnectionError(f"Tripo upload of {name} failed: {type(e).__name__}: {e}")`; Status ≥500 → `ConnectionError`; anderer Nicht-2xx oder `code != 0` → `RuntimeError(f"Tripo refused upload {name}: {self._msg(r)}")`; Token leer → `RuntimeError`. MIME aus der Endung (`png` → `image/png`, `jpg` → `image/jpeg`, `glb` → `model/gltf-binary`).

`_run(client, req, cand, opts, poll_interval, max_wait)`:

```python
    async def _run(self, client, req, cand, opts, poll_interval, max_wait) -> RunResult:
        endpoint = tripo.endpoint_of(cand)
        deadline = time.monotonic() + max_wait
        left = lambda: max(0.0, deadline - time.monotonic())
        tasks: list = []                                 # every paid/free task of this job
        # 1. inputs → tokens. Validate BEFORE uploading: a request the module will refuse
        #    (missing front, one view, a WebP) must not leave files behind at Tripo.
        images = {s: req.upload_images[s] for s in tripo.SLOTS[endpoint] if (req.upload_images or {}).get(s)}
        if endpoint == "multiview-to-model" and ("input_image_front" not in images or len(images) < 2):
            raise tripo.TripoInput("Tripo multiview needs `input_image_front` plus at least one more view")
        if endpoint == "image-to-model" and "input_image" not in images:
            raise tripo.TripoInput("`images.input_image` is required")
        exts = {s: tripo.image_ext(b) for s, b in images.items()}          # TripoInput → final
        files, fexts = {}, {}
        for fname in tripo.FILES[endpoint]:
            f = (req.upload_files or {}).get(fname)
            data = f[1] if isinstance(f, tuple) else f
            if not data:
                raise tripo.TripoInput(f"`files.{fname}` is required")
            files[fname], fexts[fname] = data, tripo.mesh_ext(data)
        tokens = {s: await self._upload(client, f"{s}.{exts[s]}", b) for s, b in images.items()}
        ftokens = {n: await self._upload(client, f"{n}.{fexts[n]}", b) for n, b in files.items()}
        body = tripo.build_request(cand, _gen_values(req), tokens, ftokens)
        extra: dict = {}
        # 2. rig-check (free) — a verdict about the mesh, before the 25 credits
        if endpoint == tripo.RIG_ENDPOINT and opts.get("rig_check"):
            cid = await self._create(client, self._api("/animations/rig-check"), tripo.build_rig_check(body["input"]), "rig-check")
            cst = await self._poll(client, "rig-check", cid, ["glb"], opts, poll_interval, left())
            tasks.append({"role": "rig-check", "task_id": cid, "credits": cst.credits})
            if not cst.riggable:
                raise RuntimeError(f"Tripo rig-check: mesh is not riggable (detected rig_type={cst.rig_type or '?'})")
            extra["rig_type"] = cst.rig_type or body.get("rig_type")
        # 3. the primary task
        path = {"image-to-model": "/generation/image-to-model", "multiview-to-model": "/generation/multiview-to-model",
                "rig": "/animations/rig"}[endpoint]
        task_id = await self._create(client, self._api(path), body, endpoint)
        formats = opts["target_formats"]
        state = await self._poll(client, endpoint, task_id, formats, opts, poll_interval, left())
        tasks.append({"role": endpoint, "task_id": task_id, "credits": state.credits})
        credits = float(state.credits or 0)
        stem = "rigged" if endpoint == tripo.RIG_ENDPOINT else "model"
        # 4. extra formats — each a convert task; a failed one fails the job (a requested
        #    delivery must never silently shrink)
        for fmt in formats[1:]:
            cbody = tripo.build_convert(task_id, fmt, endpoint == tripo.RIG_ENDPOINT)
            cid = await self._create(client, self._api("/models/convert"), cbody, f"convert:{fmt}")
            try:
                cst = await self._poll(client, "convert", cid, [fmt], opts, poll_interval, left())
            except RuntimeError as e:
                raise RuntimeError(f"Tripo convert to {fmt} failed: {e}") from e
            tasks.append({"role": f"convert:{fmt}", "task_id": cid, "credits": cst.credits})
            credits += float(cst.credits or 0)
            state.downloads.append((f"{stem}.{fmt}", cst.downloads[0][1]))
        # 5. clips — a courtesy: a failed clip is skipped with a warning, never the job
        if endpoint == tripo.RIG_ENDPOINT:
            extra["rig_spec"] = body.get("spec")
            extra.setdefault("rig_type", body.get("rig_type"))
            for preset in opts.get("animations") or []:
                rbody = tripo.build_retarget(task_id, preset, formats[0])
                try:
                    cid = await self._create(client, self._api("/animations/retarget"), rbody, f"clip:{preset}")
                    cst = await self._poll(client, "retarget", cid, [formats[0]], opts, poll_interval, left())
                except (RuntimeError, ConnectionError) as e:
                    logger.warning(f"[{self.name}] tripo clip {preset} skipped: {e}")
                    continue
                tasks.append({"role": f"clip:{preset}", "task_id": cid, "credits": cst.credits})
                credits += float(cst.credits or 0)
                state.downloads.append((f"{tripo.clip_name(preset)}.{formats[0]}", cst.downloads[0][1]))
        state.credits = credits
        extra["tasks"] = tasks
        return RunResult(task_id, endpoint, body, state, extra)
```

Dafür braucht `tripo.parse_task` für den Rig-Check zwei Zusatzfelder auf `TaskState`: `cloudtask.TaskState` bekommt `riggable: Optional[bool] = None` und `rig_type: Optional[str] = None` (in `cloudtask.py` ergänzen — Default `None`, bricht nichts), und `tripo.parse_task` setzt sie aus `output.riggable`/`output.rig_type`; bei `endpoint == "rig-check"` ist `model_url` NICHT Pflicht (Sonderfall im Parser: `if endpoint == "rig-check": st.riggable = bool(out.get("riggable")); st.rig_type = out.get("rig_type"); return st`). Diese Ergänzung in `tripo.py` + ein Test in `test_tripo.py` (`parse_task({"status":"success","output":{"riggable":False,"rig_type":"avian"}}, ["glb"], "rig-check")` → `riggable False`, `rig_type "avian"`, kein Raise) gehören zu DIESEM Task.

Registry: `"tripo": TripoAdapter` in `ADAPTERS` (damit `CLOUD_TYPES` und `GEN_TYPES` es enthalten).

- [ ] **Step 4: Tests laufen lassen**

Run: `/home/dev/projekte/llm-gateway/venv/bin/python -m unittest test_tripo_adapter test_tripo test_meshy_adapter -v`
Expected: alle PASS.

- [ ] **Step 5: Commit**

```bash
git add adapters.py tripo.py cloudtask.py test_tripo.py test_tripo_adapter.py
git commit -m "tripo: the adapter — token uploads, envelope-aware create/poll, rig-check → rig → converts → clips"
```

---

### Task 5: `main.py` kind-neutral — Routing, Chain, Fehlerbenennung, `/health`

**Files:**
- Modify: `main.py` — alle `meshy`-Fundstellen (`grep -n -i meshy main.py`): Zeilen ~143–144, ~2154–2163, ~2430–2433, ~2455–2458, ~2662–2670, ~2742–2749, ~2928–2936, ~3013–3020, ~3130 (Kommentar), ~3497–3512, ~3676–3678, ~3856–3870, ~3892, ~4149
- Test: `test_cloudtask.py` (Ergänzung), bestehende `test_scheduler.py`/`test_chain_export_node.py` bleiben grün

**Interfaces:**
- Consumes: `adapters.CLOUD_TYPES`, `cloud_kind`, `cand_kind`, `backend_kind`, `cloud_module`, `CloudNoCredits`, `CloudBusy` (Task 3).
- Produces: `main._cloud_info(b) -> dict` (ersetzt `_meshy_info`), `main._chain_mesh_param_error` kind-neutral (pur, wird getestet), `main._kind_cloud` innerhalb `_run_chain`.

- [ ] **Step 1: Test schreiben** — an `test_gen_backend_for.py` anhängen (die Datei importiert `main` + `admin` bereits über einen Temp-`config.yaml`-Harness am Dateikopf; `import tripo`/`import meshy` dort ergänzen). Die `_chain_mesh_param_error`-Fälle gehören zusätzlich als Tripo-Varianten in `test_chain_mesh_param.py` (gleiches Muster wie dessen Meshy-Fälle):

```python
class MainKindNeutral(unittest.TestCase):
    def test_chain_mesh_param_error_for_cloud_successors(self):
        import main, tripo
        s2 = tripo.default_candidate("tripo"); s2["tripo"]["endpoint"] = "rig"
        self.assertIsNone(main._chain_mesh_param_error(s2, "input_mesh_path", "Tripo-Rig"))
        err = main._chain_mesh_param_error(s2, "mesh_path", "Tripo-Rig")
        self.assertIn("Tripo", err); self.assertIn("input_mesh_path", err)
        s2m = meshy.default_candidate("meshy"); s2m["meshy"]["endpoint"] = "rigging"
        self.assertIsNone(main._chain_mesh_param_error(s2m, "input_mesh_path", "Meshy-Rig"))
        gen = tripo.default_candidate("tripo")            # an image endpoint takes no file at all
        self.assertIn("no file input", main._chain_mesh_param_error(gen, "input_mesh_path", "Tripo-Object"))

    def test_gen_backend_for_matches_kind(self):
        import main, tripo
        pool = [{"name": "gpu", "type": "comfyui"}, {"name": "gpu", "type": "tripo"}, {"name": "gpu", "type": "meshy"}]
        self.assertEqual(main._gen_backend_for("gpu", tripo.default_candidate("gpu"), pool)["type"], "tripo")
        self.assertEqual(main._gen_backend_for("gpu", meshy.default_candidate("gpu"), pool)["type"], "meshy")
        self.assertEqual(main._gen_backend_for("gpu", {"workflow_json": {}}, pool)["type"], "comfyui")

    def test_fault_labels_name_the_vendor(self):
        import main, adapters
        self.assertIn("Tripo", main._gen_exhausted_msg(adapters.CloudNoCredits("x", vendor="Tripo")))
        self.assertIn("Meshy", main._gen_exhausted_msg(adapters.CloudBusy("x", vendor="Meshy")))
        self.assertEqual(main._fault_label(adapters.CloudNoCredits("x", vendor="Tripo")), "no credits left")
        self.assertEqual(main._fault_label(adapters.CloudBusy("x", vendor="Tripo")), "Tripo queue full")
```

- [ ] **Step 2: Test laufen lassen, Fehlschlag sehen**

Run: `/home/dev/projekte/llm-gateway/venv/bin/python -m unittest test_gen_backend_for.MainKindNeutral -v`
Expected: FAIL (Tripo-Kandidat wird nicht erkannt / Vendor fehlt in der Meldung).

- [ ] **Step 3: Stellen umbauen** (Ersetzungsregeln; Kommentare mitziehen — „Meshy" → „a cloud backend (Meshy, Tripo)" wo der Satz generisch gilt):

1. `load_config` ~144: `b["paid"] = True if b.get("type") in adapters.CLOUD_TYPES else bool(b.get("paid"))`.
2. `_gen_backend_for` ~2153–2163: Docstring „SAME KIND"; Rumpf: `want = adapters.cand_kind(cand or {})`; `if b.get("name") == name and adapters.backend_kind(b) == want: return b`.
3. `_fault_label` ~2430: `if isinstance(e, adapters.CloudNoCredits): return "no credits left"`; `if isinstance(e, adapters.CloudBusy): return f"{e.vendor} queue full"`.
4. `_gen_exhausted_msg` ~2455: `f"no candidate backend could run it — {last.vendor} account out of credits: {txt}"` / `f"… — {last.vendor} queue limit reached: {txt}"`.
5. `_chain_mesh_param_error` ~2665: `k = adapters.cloud_kind(s2)`; `if k: vendor = adapters.cloud_module(k).VENDOR; s2_files = [...]; … f"chain mesh param '{mesh_param}' is not a file field of the {vendor} successor '{succ_alias}' — it takes " + (…, "no file input at all (only a rigging alias does)")`.
6. `_run_chain` ~2742–2749: `_kind_meshy` → `_kind_cloud(alias_name) -> bool` (`adapters.cloud_kind(c[0]) is not None`); Kommentar „A cloud stage shares no disk with anything".
7. `_run_chain` ~2928–2936: `if adapters.cloud_kind(s2) and not export.mesh_name.lower().endswith(".glb")` mit Meldung `f"chain: successor '{succ_alias}' runs on {adapters.cloud_module(adapters.cloud_kind(s2)).VENDOR} and takes a .glb mesh, …"`.
8. `_run_chain` ~3013–3020: `s1_meta` — Schlüssel `("backend", "cloud", "cloud_task_id", "meshy_task_id", "endpoint", "consumed_credits", "request", "tasks")`, Bedingung `if out1.meta.get("cloud_task_id") or out1.meta.get("meshy_task_id")`. Kommentar: „A cloud stage 1 is a PAID task".
9. `_run_chain` ~3130 Kommentar: „`meshy`/`tripo` are rigs the cloud built to its own conventions" (Code unverändert: `if chain_rig in ("generic", "mixamo")`).
10. `_decode_upload_files` ~3497–3512: `k = adapters.cloud_kind(cands[0]) if cands else None`; `if k:` … Meldungen mit `adapters.cloud_module(k).VENDOR` statt „Meshy".
11. `_gen_image_slots` ~3676: `if adapters.cloud_kind(cand):`.
12. `_meshy_info` ~3856 → `_cloud_info(b)`: `if b.get("type") not in adapters.CLOUD_TYPES: return {}`; Rest gleich; beide Aufrufer (~3892, ~4149) umbenennen.
13. `grep -n -i meshy main.py` danach: nur noch Kommentare, die Meshy als BEISPIEL nennen, und `meshy_task_id` als Meta-Schlüssel dürfen bleiben.

- [ ] **Step 4: Tests + Compile**

Run: `/home/dev/projekte/llm-gateway/venv/bin/python -m py_compile main.py && /home/dev/projekte/llm-gateway/venv/bin/python -m unittest discover -p 'test_*.py'`
Expected: alle PASS (inkl. `test_chain_export_node`, `test_scheduler`, `test_prune_branch`).

- [ ] **Step 5: Commit**

```bash
git add main.py test_gen_backend_for.py test_chain_mesh_param.py
git commit -m "main: generation routing, chains and fault naming are cloud-kind neutral (Meshy, Tripo)"
```

---

### Task 6: `admin.py` kind-neutral — Backend-Formular, Listen, Registrierung, Chain-Felder, Job-Ansicht

**Files:**
- Modify: `admin.py` — Zeilen 33–42 (Imports, `_MESHY_URL`), 792–793 (`_type_badge`), 1085–1170 (Backend-Formular), 1240–1262 (`_type_select`), 1329–1335 (Credits in der Backend-Liste — Kommentar), 1563–1564 + 1619–1632 (`backend_save`), 1961–1963 + 2590–2592 (Alias-Listen), 2662–2678 (`register_post`), 2779–2786 (`_same_kind`), 3014–3022 + 3030–3064 (Chain-Felder + Rig-Optionen des ComfyUI-Editors), 3755–3762 (`update`: Default-`mesh_param`), 3985–3996 + 4168–4176 + 4258–4266 (Playground), 4861–4880 (`_meshy_table`), 4892–4950 (`_stage2_section`), 5051–5055 (Job-Ansicht), 5126–5134 (Kommentar)
- Test: neu `test_cloud_editor.py` (Kopf = der `config.yaml`-Harness aus `test_gen_backend_for.py`, damit `import admin`/`import main` funktioniert; Task 7 hängt seine Tests an dieselbe Datei), `test_admin_live.py` bleibt grün

**Interfaces:**
- Consumes: `adapters.CLOUD_TYPES/CLOUD_MODULES/cloud_kind/cand_kind/backend_kind/cloud_module` (Task 3), `mod.URL/VENDOR/BACKEND_HINT/POLL_INTERVAL_DEFAULT/MAX_WAIT_DEFAULT/default_candidate/endpoint_of` (Task 1/2).
- Produces: `admin._cloud_table(title, m) -> str` (ersetzt `_meshy_table`; liest `m.get("cloud_task_id") or m.get("meshy_task_id")`, Vendor aus `m.get("cloud")` bzw. `"meshy"` als Fallback), `admin._cloud_urls() -> dict` (`{kind: mod.URL}`), Formularfelder `cloud_max_wait`, `cloud_poll_interval`, Block-Id `cloudopts`, `_type_select` JS mit `{"meshy": url, "tripo": url}`-Map, Rig-Optionen `["", "mixamo", "generic", "meshy", "tripo"]`. `_alias_editor` ruft weiterhin `_meshy_editor` (Task 7 ersetzt es) — in DIESEM Task nur die Bedingung: `k = adapters.cloud_kind(cand); if k: return _meshy_editor(alias, cands, saved), _MESHY_SIDE` (Meshy) — für `tripo` würde `_meshy_editor` falsche Felder zeigen; deshalb hier für `k != "meshy"` vorübergehend `f"<p class='hint'>{VENDOR} alias editor arrives in the next task</p>"` rendern. Task 7 entfernt das.

- [ ] **Step 1: Tests schreiben** — `test_cloud_editor.py` anlegen (Harness-Kopf aus `test_gen_backend_for.py` kopieren, dann `import meshy, tripo`):

```python
class AdminKindNeutral(unittest.TestCase):
    def test_cloud_table_reads_both_id_keys(self):
        import admin
        html = admin._cloud_table("Cloud", {"meshy_task_id": "m1", "request": {"a": 1}, "endpoint": "image-to-3d"})
        self.assertIn("m1", html); self.assertIn("Meshy", html)
        html = admin._cloud_table("Cloud", {"cloud": "tripo", "cloud_task_id": "t1", "request": {"input": "tok"},
                                           "endpoint": "rig", "tasks": [{"role": "rig-check", "task_id": "t0", "credits": 0}]})
        self.assertIn("t1", html); self.assertIn("Tripo", html); self.assertIn("rig-check", html); self.assertIn("t0", html)
        self.assertEqual(admin._cloud_table("x", {"request": {}}), "")

    def test_same_kind_matches_backend_type(self):
        import admin, tripo
        admin._gen_backends = lambda: [{"name": "x", "type": "comfyui"}, {"name": "x", "type": "tripo"},
                                       {"name": "m", "type": "meshy"}]
        self.assertTrue(admin._same_kind([tripo.default_candidate("x")], "x"))
        self.assertFalse(admin._same_kind([tripo.default_candidate("x")], "m"))
        self.assertTrue(admin._same_kind([{"workflow_json": {}}], "x"))
        self.assertFalse(admin._same_kind([meshy.default_candidate("m")], "x"))

    def test_type_select_knows_every_cloud_url(self):
        import admin, tripo
        html = admin._type_select("tripo")
        self.assertIn(tripo.URL, html); self.assertIn(meshy.URL, html)
        self.assertIn('value="tripo" selected', html)
        self.assertIn("cloudopts", html)
```

- [ ] **Step 2: Test laufen lassen, Fehlschlag sehen**

Run: `/home/dev/projekte/llm-gateway/venv/bin/python -m unittest test_cloud_editor.AdminKindNeutral -v`
Expected: FAIL (`_cloud_table` fehlt).

- [ ] **Step 3: Umbau** (Regeln; die Hilfetexte NICHT verkürzen, nur vendor-neutral machen oder aus `mod.*` beziehen):

1. Imports: `import meshy` bleibt (Task 7 braucht es noch), zusätzlich `import tripo`; `_MESHY_URL` → `def _cloud_urls() -> dict: return {k: m.URL for k, m in adapters.CLOUD_MODULES.items()}`.
2. `_type_badge` 792: `if t in adapters.CLOUD_TYPES: mod = adapters.cloud_module(t); return _badge(f"☁ {t}", "img", f"{mod.VENDOR} cloud mesh generation (paid, per task)")`.
3. Backend-Formular: Typ-Hinweis um `<b>tripo</b> = Tripo3D cloud mesh generation + Mixamo-spec rigging (image / multi-image → 3D), billed per task — always paid` ergänzen; `paid`-Zeile: `if g("type", "openai") in adapters.CLOUD_TYPES` mit Text `paid — always, a cloud backend bills per task`; der Block `#meshyopts` → `#cloudopts`, sichtbar wenn Typ in `CLOUD_TYPES`, Überschrift `<div class="grouphdr">Cloud task API</div>`, Felder `cloud_max_wait` (placeholder = `MAX_WAIT_DEFAULT` des aktuellen Typs, bei Nicht-Cloud „900") und `cloud_poll_interval` (placeholder `POLL_INTERVAL_DEFAULT`); darunter je Cloud-Kind ein `<p class='hint' data-cloud-hint="<kind>" style="display:none">{mod.BACKEND_HINT}</p>`, der per JS für den gewählten Typ eingeblendet wird.
4. `_type_select` 1244–1262: Optionen `("comfyui", "meshy", "tripo", "openai", "anthropic")`; JS: `var cloudUrls={"meshy":"…","tripo":"…"}` (aus `json.dumps(_cloud_urls())`), `m=document.getElementById('cloudopts')`, `if(m)m.style.display=cloudUrls[t]?'':'none'`, Hinweise `document.querySelectorAll('[data-cloud-hint]').forEach(function(h){h.style.display=h.dataset.cloudHint===t?'':'none'})`, URL-Prefill `if(cloudUrls[t]){if(u&&!u.value)u.value=cloudUrls[t]; …paid forcing…}`. Beim initialen Rendern den passenden Hint sichtbar setzen (serverseitig `style=""` für `g("type")`).
5. `backend_save` 1563: `if not url and new_type in adapters.CLOUD_TYPES: url = _cloud_urls()[new_type]`; 1619–1632: `if new_type in adapters.CLOUD_TYPES:` mit `("cloud_max_wait", "max_wait", int), ("cloud_poll_interval", "poll_interval", float)`.
6. Alias-Listen 1961 + 2590: `k = adapters.cloud_kind(c); mapped = (f"{k} · {adapters.cloud_module(k).endpoint_of(c)}" if k else …)`.
7. `register_post` 2662–2678: `bt = next((b.get("type") for b in _gen_backends() if b["name"] == backend and b.get("type") in adapters.CLOUD_TYPES), None)`; `if bt: cand = adapters.cloud_module(bt).default_candidate(backend)` … Log `f"ui: registered '{alias}' -> {backend} ({bt}, no workflow)"`.
8. `_same_kind` 2779: `want = adapters.cand_kind(cands[0]) if cands else "comfyui"; return any(x["name"] == backend_name and adapters.backend_kind(x) == want for x in _gen_backends())`.
9. ComfyUI-Editor Chain-Felder 3014–3022: `succ_kind = adapters.cloud_kind((store.get(…) or [{}])[0])`; `if succ_kind:` … Hinweis `f"upload — forced: the successor runs on {adapters.cloud_module(succ_kind).VENDOR}"`. Hinweistexte 3030–3064: „a Meshy rigging alias" → „a cloud rigging alias (Meshy-Rig, Tripo-Rig)"; `rig_opts` + `"tripo"`, Text „`meshy`/`tripo` (a cloud rig) are only tagged, never normalized".
10. `update` 3755–3762: `succ_kind = adapters.cloud_kind(…)`; Default-`mesh_param`: `files = adapters.public_fields(succ_cand)[2] if succ_kind else []` → `(files[0]["name"] if files else ("input_mesh_path" if succ_kind else "mesh_path"))`.
11. Playground 3985, 4168, 4258: `if cand and adapters.cloud_kind(cand):` (Kommentare: „A cloud alias has no workflow…", „Meshy/Tripo read the bytes out of the request").
12. `_meshy_table` → `_cloud_table(title, m)`: `tid = m.get("cloud_task_id") or m.get("meshy_task_id")`; `if not tid or not m.get("request"): return ""`; `kind = m.get("cloud") or "meshy"`; `vendor = adapters.cloud_module(kind).VENDOR if kind in adapters.CLOUD_MODULES else kind`; Titel `title.replace("Meshy", vendor)`; unter der Request-Tabelle, wenn `m.get("tasks")`: eine kleine Tabelle `role · task id · credits`. Aufrufer 5054–5055: `_cloud_table("Cloud · stage 1", …)`, `_cloud_table("Cloud", meta)` (der Vendor-Name wird aus dem Meta eingesetzt: `f"{vendor} · stage 1"` / `vendor`).
13. `_stage2_section` 4900–4947: `meshy2` → `cloud2 = adapters.cloud_kind(c2)`; Texte `f"handed · {VENDOR} fixed table"`, `f"{VENDOR} takes its fixed label table …"`.
14. `_alias_editor` 3296: siehe Interfaces (temporärer Platzhalter für Nicht-Meshy-Kinds).

- [ ] **Step 4: Tests + Compile**

Run: `/home/dev/projekte/llm-gateway/venv/bin/python -m py_compile admin.py && /home/dev/projekte/llm-gateway/venv/bin/python -m unittest discover -p 'test_*.py'`
Expected: alle PASS (`test_admin_live.py` prüft ES5-Gültigkeit der JS-Konstanten — das neue `_type_select`-JS ist Inline-Attribut, aber `var`/`function` statt `const`/Arrow verwenden).

- [ ] **Step 5: Commit**

```bash
git add admin.py test_cloud_editor.py
git commit -m "ui: backends, alias lists, chain fields and the job view know every cloud kind (Meshy, Tripo)"
```

---

### Task 7: Der schema-getriebene Cloud-Alias-Editor (`_cloud_editor` + `cloud_update`)

**Files:**
- Modify: `admin.py` — `_meshy_editor` (3161–3278) → `_cloud_editor`, `_MESHY_SIDE` → `_cloud_side(kind)`, `_alias_editor` 3296–3297, `meshy_update` (3783–3841) → `cloud_update`, Route 6410 (`/ui/mapping/meshy-update` → `/ui/mapping/cloud-update`)
- Test: `test_cloud_editor.py` (Ergänzung)

**Interfaces:**
- Consumes: `mod.OPTION_FIELDS/OPTION_DEFAULTS/ENDPOINTS/RIG_ENDPOINT/AI_MODELS/FORMATS/RIG_FORMATS/VENDOR/ENDPOINT_HINT/CHAIN_HINT/endpoint_of/options_of/public_fields/default_candidate` (Task 1/2), `cloudtask.parse_options/field_value_str` (Task 1), `adapters.cloud_kind/cloud_module/public_fields` (Task 3).
- Produces: `admin._cloud_editor(kind: str, alias: str, cands: list, saved: bool = False) -> str`, `admin._cloud_side(kind) -> str`, `admin.cloud_update(request)` (POST `/ui/mapping/cloud-update`, Formularfelder: `alias`, `new_alias`, `task`, `cloud_endpoint`, `cloud_model`, `opt__<key>`, `fmt__<format>`, `retries`, `successor`, `chain_mesh_param`, `chain_keep`, `chain_rig`).

- [ ] **Step 1: Tests schreiben** (an `test_cloud_editor.py`):

```python
class CloudEditor(unittest.TestCase):
    def _render(self, mod, endpoint):
        import admin
        c = mod.default_candidate("b"); c[mod.KIND]["endpoint"] = endpoint
        admin._gen_backends = lambda: [{"name": "b", "type": mod.KIND}]
        html = admin._cloud_editor(mod.KIND, "A-1", [c])
        for fld in mod.OPTION_FIELDS:
            self.assertIn(f'name="opt__{fld["key"]}"', html, fld["key"])
        for f in (mod.RIG_FORMATS if endpoint == mod.RIG_ENDPOINT else mod.FORMATS):
            self.assertIn(f'name="fmt__{f}"', html)
        self.assertIn('name="cloud_endpoint"', html); self.assertIn('name="cloud_model"', html)
        self.assertIn('action="/ui/mapping/cloud-update"', html)
        self.assertIn('name="chain_rig"', html); self.assertIn('value="tripo"', html)
        self.assertIn(mod.VENDOR, html)
        return html

    def test_renders_meshy_and_tripo_every_endpoint(self):
        import tripo
        for mod in (meshy, tripo):
            for ep in mod.ENDPOINTS:
                self._render(mod, ep)

    def test_bool_fields_with_blank_label_share_a_row(self):
        import tripo
        html = self._render(tripo, "image-to-model")
        # `pbr` (label "") rides in the `texture` row: exactly one field row carries both boxes
        row = html[html.index('name="opt__texture"'):html.index('name="opt__texture_quality"')]
        self.assertIn('name="opt__pbr"', row)

    def test_tripo_defaults_show_mixamo(self):
        import tripo
        html = self._render(tripo, "rig")
        self.assertIn('value="mixamo" selected', html)
```

Und für `cloud_update` ein Test über die Starlette-App-freie Logik: `cloud_update` in zwei Teile schneiden — `_cloud_update_apply(kind, cands, form) -> (alias_renamed_to, cands)` (pur über die Kandidaten) und den Request-Handler. Test:

```python
    def test_cloud_update_apply_normalizes_and_copies(self):
        import admin, tripo
        c = tripo.default_candidate("b"); cands = [c, dict(c, backend="b2")]
        form = {"cloud_endpoint": "rig", "cloud_model": "v3.0-20250812", "opt__spec": "tripo",
                "opt__rig_type": "avian", "opt__animations": "preset:walk preset:run", "opt__face_limit": "9",
                "fmt__fbx": "on", "task": "mesh2rig", "retries": "1",
                "successor": "", "chain_mesh_param": "", "chain_keep": "", "chain_rig": ""}
        admin._cloud_update_apply("tripo", cands, form)
        for x in cands:
            self.assertEqual(x["tripo"]["endpoint"], "rig")
            self.assertEqual(x["model"], "v3.0-20250812")
            self.assertEqual(x["tripo"]["options"]["spec"], "tripo")
            self.assertEqual(x["tripo"]["options"]["rig_type"], "avian")
            self.assertEqual(x["tripo"]["options"]["animations"], ["preset:walk", "preset:run"])
            self.assertIsNone(x["tripo"]["options"]["face_limit"])      # 9 < 100 → ignored by options_of
            self.assertEqual(x["tripo"]["options"]["target_formats"], ["fbx"])
            self.assertEqual(x["task"], "mesh2rig"); self.assertEqual(x["retries"], "1")
            self.assertNotIn("successor", x)
        self.assertIsNot(cands[0]["tripo"]["options"], cands[1]["tripo"]["options"])
        form.update({"successor": "mesh-mia", "chain_rig": "mixamo", "chain_keep": "preview.png"})
        admin._cloud_update_apply("tripo", cands, form)
        self.assertEqual(cands[0]["successor"], {"alias": "mesh-mia", "mesh_param": "input_mesh_path",
                                                 "keep_from_mesh": ["preview.png"], "rig": "mixamo"})
        self.assertIsNot(cands[0]["successor"], cands[1]["successor"])
```

- [ ] **Step 2: Test laufen lassen, Fehlschlag sehen**

Run: `/home/dev/projekte/llm-gateway/venv/bin/python -m unittest test_cloud_editor.CloudEditor -v`
Expected: FAIL (`_cloud_editor` fehlt).

- [ ] **Step 3: `_cloud_editor` schreiben** — Struktur wie `_meshy_editor` (dessen Code als Vorlage nehmen und ersetzen; `_meshy_editor` und `_MESHY_SIDE` löschen):

```python
def _option_rows(mod, opts: dict, ep: str) -> str:
    """The vendor option block of a cloud alias editor, rendered from mod.OPTION_FIELDS.
    Consecutive bool fields whose label is "" share the previous field's row (Meshy's
    `texture` = should_texture + enable_pbr). rig_only fields get a muted marker outside
    the rig endpoint — they are still saved, so switching endpoints loses nothing."""
    rows, pending_label, pending_ctrls, pending_hint = [], None, [], ""

    def flush():
        if pending_ctrls:
            rows.append(_field(pending_label or "", "".join(pending_ctrls)))
            if pending_hint:
                rows.append(f"<p class='hint' style='margin:-4px 0 10px'>{pending_hint}</p>")

    for fld in mod.OPTION_FIELDS:
        k, t, label = fld["key"], fld["type"], fld.get("label", k)
        rig_mark = (" <span class='muted'>(rig)</span>" if fld.get("rig_only") and ep != mod.RIG_ENDPOINT else "")
        if t == "bool":
            ctrl = _checkbox(f"opt__{k}", bool(opts.get(k)), (fld.get("checkbox_text") or k) + rig_mark)
            if label == "" and pending_ctrls:
                pending_ctrls.append(ctrl)
                pending_hint = fld.get("hint") or pending_hint
                continue
            flush()
            pending_label, pending_ctrls, pending_hint = label, [ctrl], fld.get("hint") or ""
            continue
        flush()
        pending_label, pending_ctrls, pending_hint = None, [], ""
        if t == "select":
            choices = [tuple(c) if isinstance(c, (tuple, list)) else (c, c) for c in fld["choices"]]   # _select wants TUPLES
            ctrl = _select(f"opt__{k}", choices, cloudtask.field_value_str(fld, opts.get(k)))
        elif t == "tristate":
            ctrl = _select(f"opt__{k}", [("", "model default"), ("true", "always"), ("false", "never")],
                           cloudtask.field_value_str(fld, opts.get(k)))
        else:                                   # int | text | list
            ctrl = _inp(f"opt__{k}", cloudtask.field_value_str(fld, opts.get(k)),
                        placeholder=fld.get("placeholder", ""), typ="number" if t == "int" else "text")
        rows.append(_field(label + rig_mark, ctrl, short=(t == "int")))
        if fld.get("hint"):
            rows.append(f"<p class='hint' style='margin:-4px 0 10px'>{fld['hint']}</p>")
    flush()
    return "".join(rows)
```

(Prüfe `_select`'s Signatur: `options` ist eine Liste von Strings ODER `(value, text)`-Tupeln — Zeile 569; `_checkbox(name, checked, label)`.)

`_cloud_editor(kind, alias, cands, saved=False)`: `mod = adapters.cloud_module(kind)`; `cand = cands[0]`; Successor/keep/rig wie heute, `rig_opts = [("", "blank — trust the successor"), "mixamo", "generic", "meshy", "tripo"]` (+ unbekannter gespeicherter Wert); `ep = mod.endpoint_of(cand)`, `opts = mod.options_of(cand)`, `model = cand.get("model") if cand.get("model") in mod.AI_MODELS else mod.AI_MODELS[0]`; Format-Checkboxen `fmt__<f>` über `mod.RIG_FORMATS if ep == mod.RIG_ENDPOINT else mod.FORMATS`; Request-Felder-Tabelle über `adapters.public_fields(cand)` (unverändert); Formular `action="/ui/mapping/cloud-update"`, Felder `cloud_endpoint`, `cloud_model`; Überschrift `<h2>{VENDOR}</h2>`; Hinweis `mod.ENDPOINT_HINT` unter dem Endpunkt; dann `_option_rows(mod, opts, ep)`; `deliver formats`; `retries`; Chain-Block wie heute mit dem gemeinsamen Text (Hand-off als Bytes, mesh param, keep, rig type: `generic`/`mixamo` normalisiert+validiert, `meshy`/`tripo` nur getaggt, glb Pflicht) + `mod.CHAIN_HINT`; Request-Felder; Backends-Abschnitt (Text: „Only {VENDOR} backends can be added to a {VENDOR} alias."). `_cloud_side(kind)` = der heutige `_MESHY_SIDE`-Text mit `VENDOR`. `_alias_editor`: `k = adapters.cloud_kind(cand); if k: return _cloud_editor(k, alias, cands, saved), _cloud_side(k)` (den Platzhalter aus Task 6 entfernen).

`_cloud_update_apply(kind, cands, form)`:

```python
def _cloud_update_apply(kind: str, cands: list, f: dict) -> None:
    """Apply a cloud alias form to EVERY candidate (they are the alias's shape, not
    per-backend). Options go through parse_options (schema) and then the module's
    options_of (the same normalization the request builder applies), so what is stored
    is exactly what will be sent."""
    mod = adapters.cloud_module(kind)
    ep = (f.get("cloud_endpoint", "") or "").strip()
    ep = ep if ep in mod.ENDPOINTS else mod.ENDPOINTS[0]
    model = (f.get("cloud_model", "") or "").strip()
    model = model if model in mod.AI_MODELS else mod.AI_MODELS[0]
    opts = cloudtask.parse_options(mod.OPTION_FIELDS, f, mod.OPTION_DEFAULTS)
    opts["target_formats"] = [x for x in mod.FORMATS if f.get(f"fmt__{x}")] or ["glb"]
    opts = mod.options_of({mod.KIND: {"endpoint": ep, "options": opts}, "model": model})
    task = (f.get("task", "") or "").strip()
    retries = (f.get("retries", "") or "").strip()
    succ_alias = (f.get("successor", "") or "").strip()
    keep = [g.strip() for g in re.split(r"[\r\n,]+", f.get("chain_keep", "") or "") if g.strip()]
    rig = (f.get("chain_rig", "") or "").strip()
    succ = ({"alias": succ_alias,
             "mesh_param": (f.get("chain_mesh_param", "") or "").strip() or "input_mesh_path",
             **({"keep_from_mesh": keep} if keep else {}),
             **({"rig": rig} if rig else {})}
            if succ_alias else None)
    for c in cands:
        c[mod.KIND] = {"endpoint": ep, "options": json.loads(json.dumps(opts))}   # own copy per candidate
        c["model"] = model
        c["retries"] = retries
        if succ:
            c["successor"] = json.loads(json.dumps(succ))
        else:
            c.pop("successor", None)
        if task:
            c["task"] = task
```

`cloud_update(request)`: Form lesen, `alias`, `cands = store.get(alias)`, `kind = adapters.cloud_kind(cands[0]) if cands else None`, kein Kind → `HTTPException(404, "cloud alias not found")`; `_cloud_update_apply(kind, cands, f)`; Rename wie heute; `store.upsert`; Log `f"ui: updated {kind} alias '{alias}' ({cands[0][kind]['endpoint']}, {cands[0]['model']})"`; Redirect wie heute. Route: `app.add_api_route("/ui/mapping/cloud-update", cloud_update, methods=["POST"])` (die alte `meshy-update`-Route entfernen — kein Client außer dem eigenen Formular nutzt sie).

Meshys `should_remesh` als `tristate`, `target_polycount` als `int` → `meshy.options_of` normalisiert (`opt_polycount`) — identisch zum heutigen `meshy_update`. `import cloudtask` in `admin.py` ergänzen.

- [ ] **Step 4: Tests + Compile**

Run: `/home/dev/projekte/llm-gateway/venv/bin/python -m py_compile admin.py && /home/dev/projekte/llm-gateway/venv/bin/python -m unittest discover -p 'test_*.py'`
Expected: alle PASS. Danach `grep -n "meshy_update\|_meshy_editor\|_MESHY_SIDE\|meshy-update" admin.py` → keine Treffer.

- [ ] **Step 5: Commit**

```bash
git add admin.py test_cloud_editor.py
git commit -m "ui: one schema-driven cloud alias editor serves Meshy and Tripo"
```

---

### Task 8: Dokumentation — README, config.example.yaml, mesh-client-spec, CLAUDE.md

**Files:**
- Modify: `README.md` (Abschnitt „Meshy.ai (cloud mesh generation)" ~614–690, Chain-Tabelle ~760–795, Endpunkt-Tabellen ~843–851, Konsole ~894–897)
- Modify: `config.example.yaml` (~174–235)
- Modify: `docs/mesh-client-spec.md` (Zeilen ~63, ~89, ~150–163, ~179–181, ~330)
- Modify: `CLAUDE.md` (Abschnitt `meshy.py` → `meshy.py` / `tripo.py` / `cloudtask.py`, `adapters.py`-Absatz, Test-Liste „six" → „nine")

**Interfaces:** keine (Text).

- [ ] **Step 1: README** — nach dem Meshy-Abschnitt ein Abschnitt `### Tripo3D (cloud mesh generation + Mixamo rigging)` mit: Backend-YAML (`type: tripo`, `url: https://openapi.tripo3d.ai`, `api_key`, `max_concurrent: 4`, `poll_interval: 2`, `max_wait: 900`), was anders ist als Meshy (Uploads statt Base64 — kein Verhalten für den Client; nur GLB nativ, jedes weitere Format = Convert-Task 5 Credits; Rig-Check gratis vor dem Rig; `spec: mixamo` Default → Mixamo-Bone-Namen, `rig_spec` im Job-Meta; Clips per Preset-Liste, 10 Credits je Clip, `walk.glb`; `max_wait` deckelt alle Tasks eines Jobs), die Label-Tabelle (§6.1 der Spec), ein `curl`-Beispiel für `Tripo-Rig` (`files.input_mesh_path`, `params.input_rig_type`), das Alias-Set (`Tripo-Object`, `Tripo-Multiview`, `Tripo-Humanoid` → `Tripo-Rig` mit `rig: tripo`, `Tripo-Rig` task `mesh2rig`). Chain-Tabelle: Zeilen für Tripo (ComfyUI → Tripo-Rig; Tripo → ComfyUI; Tripo → Tripo; Meshy → Tripo-Rig). Überall, wo „Meshy" als Gattung gemeint ist („a Meshy stage shares no disk"): „a cloud stage (Meshy, Tripo)". Konsole-Tabelle: Backends `(LLM, ComfyUI, Meshy, Tripo)`. `rig`-Werte: `generic|mixamo|meshy|tripo`.
- [ ] **Step 2: `config.example.yaml`** — nach dem Meshy-Block: Tripo-Backend + Aliase `Tripo-Object` (image-to-model), `Tripo-Rig` (endpoint `rig`, options `spec: mixamo`, `animations: [preset:walk]`), `Tripo-Humanoid` (image-to-model, `face_limit: 150000`, successor `Tripo-Rig`, `mesh_param: input_mesh_path`, `rig: tripo`) mit denselben Kommentar-Konventionen wie der Meshy-Block.
- [ ] **Step 3: `docs/mesh-client-spec.md`** — `rig: "generic|mixamo|meshy|tripo"`; Dateinamen-Tabelle: Zeile Tripo (`model.glb`, `preview.png`; Rig `rigged.glb`/`rigged.fbx`, Clips `<preset>.<fmt>` z. B. `walk.glb`; Extra-Formate `model.<fmt>`); `rig`-Tabelle: Zeile `tripo` — Tripo Auto-Rig, Bone-Namen laut `rig_spec` (`mixamo` = Mixamo-kompatibel, `tripo`), GLB/FBX mit eingebetteter Textur, nicht normalisiert/validiert; Alias-Tabelle: `Tripo-Object`, `Tripo-Multiview` (Front + mindestens eine weitere Ansicht!), `Tripo-Humanoid` (→ `Tripo-Rig`, `rig: tripo`, `rig_spec: mixamo`; `input_rig_type` durchgereicht), `Tripo-Rig` (3.5). Meta-Feld `rig_spec` dokumentieren.
- [ ] **Step 4: `CLAUDE.md`** — im `meshy.py`-Absatz die Cloud-Kind-Abstraktion erklären (`cloud_kind`/`cand_kind`/`backend_kind`/`CLOUD_TYPES`/`CLOUD_MODULES`, die Modul-Schnittstelle, `cloudtask.py`, `CloudTaskAdapter` mit den Vendor-Hooks, `OPTION_FIELDS` → `_cloud_editor`), einen `tripo.py`-Absatz (V3, Token-Uploads, `{code,data}`-Hülle → `_task_body`/`_TaskVerdict`, 403+2010 = `CloudNoCredits`, Folge-Tasks unter EINEM `max_wait`, Rig-Check vor dem Rig, `rig: tripo` + `rig_spec`, `meta.tasks`), Test-Liste aktualisieren (`ls test_*.py` ist die Wahrheit — CLAUDE.md nennt noch "six"; die vier neuen: `test_cloudtask.py`, `test_tripo.py`, `test_tripo_adapter.py`, `test_cloud_editor.py`) mit dem jeweiligen „fails silently"-Grund (ein Editor, der ein `opt__`-Feld verliert, speichert still den Default; ein Convert, der still fehlt, ist eine kleinere Lieferung).
- [ ] **Step 5: Commit**

```bash
git add README.md config.example.yaml docs/mesh-client-spec.md CLAUDE.md
git commit -m "docs: Tripo3D backend — README, config example, mesh client spec, CLAUDE.md"
```

---

### Task 9: Abschlussprüfung — Suite, Compile, App-Start mit Stub-Konfiguration

**Files:** keine Änderungen erwartet (Fixes, falls etwas auffällt, gehören in einen eigenen Commit).

- [ ] **Step 1: Volle Suite + Compile**

Run: `/home/dev/projekte/llm-gateway/venv/bin/python -m py_compile *.py && /home/dev/projekte/llm-gateway/venv/bin/python -m unittest discover -p 'test_*.py' 2>&1 | tail -5`
Expected: `OK` mit der Gesamtzahl der Tests (13 bestehende Dateien + `test_cloudtask`, `test_tripo`, `test_tripo_adapter`, `test_cloud_editor`).

- [ ] **Step 2: Rest-Grep** — `grep -n "meshy" main.py admin.py adapters.py | grep -v -i "meshy_task_id\|import meshy\|meshy\.\|Meshy-\|meshy,\|(Meshy\|Meshy)\|Meshy/\|Meshy or\|e\.g\."` → jede verbliebene Zeile muss entweder Meshy als Beispiel nennen oder Meshy-spezifischen Code im `MeshyAdapter` sein; eine Kind-Abfrage (`== "meshy"`, `get("meshy") is not None`) außerhalb von `adapters.MeshyAdapter`/`meshy.py` ist ein Fehler → beheben.

- [ ] **Step 3: App-Start mit Stub** — eine isolierte Instanz im Job-Tmp-Verzeichnis starten (Muster aus dem Speicher „Repro-Harness: Stub-Instanz": Verzeichnis mit Symlinks auf die `.py`-Dateien, eigene `config.yaml` mit `api_key: test`, einem `tripo`-Backend auf `http://127.0.0.1:<stub-port>` und einem `image_models`-Eintrag `Tripo-Object` (`tripo: {endpoint: image-to-model, options: {}}`); der Stub aus `test_tripo_adapter.py` als eigener Prozess auf dem Port). `uvicorn main:app --port 4111` im Hintergrund, dann:
  - `curl -s localhost:4111/health | python3 -m json.tool | grep -A3 tripo` → Backend healthy, `credits` sichtbar.
  - `curl -s -H 'Authorization: Bearer test' localhost:4111/v1/generations/Tripo-Object/schema` → `images[0].name == input_image`, `params` enthält `input_face_num`, `input_texture_resolution`.
  - `curl -s -H 'Authorization: Bearer test' -X POST localhost:4111/v1/generations -H 'Content-Type: application/json' -d '{"model":"Tripo-Object","images":{"input_image":"data:image/png;base64,<PNG>"},"mode":"async"}'` → Job-Id; nach 3 s `GET /v1/jobs/<id>` → `done`, Results `model.glb` + `preview.png`, `meta.cloud == "tripo"`.
  - `/ui`: `curl -s localhost:4111/ui/mapping?edit=Tripo-Object` → enthält `opt__texture_quality` und `cloud-update`; `curl -s localhost:4111/ui/backends` → Badge `☁ tripo`.
  Instanz und Stub danach beenden. Was hier auffällt, in einem Fix-Commit beheben (`fix: …`).

- [ ] **Step 4: Ergebnis festhalten** — im Abschlussbericht: Testzahl, die geprüften Endpunkte, offene Punkte aus `docs/tripo-api-v3-notes.md` §13, die nur gegen die echte API klärbar sind (Upload-Antwortform, `rendered_image_url` beim Rig, Clip-Download-URLs, `credits_consumed` als Dezimal).
