# Meshy Phase 2 — NPCs mit Rig: Chain über Backend-Typen und Meshy-Rigging

Datum: 2026-09-02 · Status: Entwurf zur Review · baut auf `2026-09-02-meshy-backend-design.md` (Phase 1, live seit 2026-09-02)

## 1. Ziel

Aus einem Bild einen **geriggten Charakter (NPC)** erzeugen, mit Meshy als Mesh-Erzeuger und
wahlweise zwei Riggern:

- **A. lokal:** Meshy → ComfyUI-Rigger (`mesh-mia`, Mixamo-Skelett, oder `mesh-rig-unirig`),
  exakt die Pipeline der heutigen `Trellis2-Humanoid-*`-Aliase — dieselbe Client-Seite,
  dieselbe Animationsbibliothek (`rig: mixamo`).
- **B. Cloud:** Meshys Rigging-API (`POST /openapi/v1/rigging`, 5 Credits, Bipeds) als
  eigener Alias `Meshy-Rig` — standalone für jedes GLB (auch lokal erzeugte) und als
  Chain-Stufe 2 hinter Meshy **oder** ComfyUI.

Die öffentliche API bleibt: `POST /v1/generations` mit `model: "Meshy-Humanoid"` liefert
am Ende das geriggte Ergebnis, genau wie `Trellis2-Humanoid-High` heute; `Meshy-Rig`
nimmt `files.input_mesh_path` wie `mesh-mia`.

Nicht-Ziele: Meshys Animation-API (3 Credits je Clip) und Text-to-Motion; die
`input_task_id`-Abkürzung (Rig direkt am Meshy-Task statt per Upload); Quadrupeds.

## 2. Was heute fehlt (Ist, Stand 4735c7d)

`_run_chain` (main.py ~2655–3100) ist ComfyUI-spezifisch an genau drei Stellen:

1. **Stufe-1-Export:** `adapter.export_node_error(s1_wf, export_node)`, die Endungs-
   Entscheidung über `file_format`, `adapter.export_pin(export_node, prefix)` als Extra-
   Pin und `mesh_name = adapter.pinned_output_name(prefix, ext)`. Das Mesh wird danach
   NICHT aus `out1.blobs` genommen, sondern per `adapter.fetch_output(mesh_name)` von
   `/view` geholt. Ein Meshy-Kandidat hat weder Workflow noch Export-Node; sein Mesh liegt
   aber bereits als `model.glb`-Blob in `out1.blobs`.
2. **Stufe-2-Übergabe:** `adapter2.upload_input(mesh_bytes, mesh_name)` +
   `input_path_ref(backend2, …)` schreibt das Mesh in das ComfyUI-Input-Verzeichnis und
   übergibt einen absoluten Pfad in `params[mesh_param]`. Ein Meshy-Rigger braucht die
   Bytes als Data-URI im Request, keinen Pfad.
3. `run_generation` lehnt einen `successor` auf einem Meshy-Alias heute mit 400 ab
   (Phase-1-Guard), der Meshy-Editor hat keinen Successor-Abschnitt.

Alles andere in `_run_chain` ist bereits backend-agnostisch: Kandidaten-Parken,
Slot-Halten (`_wait_and_hold`), `jobs.set_backend`/`set_stage`, Parameter-Threading nach
Label, `meta.chain_stage2`, `keep_from_mesh` über `out1.blobs`, `fail_meta`.

## 3. Design

### 3.1 Zwei Adapter-Hooks statt Sonderfällen in `_run_chain`

`BackendAdapter` bekommt zwei überschreibbare Methoden; `_run_chain` ruft nur noch diese.

```python
@dataclass
class ChainExport:
    mesh_name: str                 # gateway-pinned name of the stage-1 mesh
    extra_fixed: list              # pins added to stage 1 (ComfyUI: the export pin)
    error: Optional[str] = None    # misconfiguration → job fails up front, named

class BackendAdapter:
    def chain_export(self, cand: dict, succ: dict, params: dict, prefix: str) -> ChainExport
    async def chain_take_mesh(self, out: GenOutput, export: ChainExport, want_bytes: bool) -> Optional[bytes]
    def chain_feed_mesh(self, req2: NormalizedRequest, backend2: dict, mesh_param: str,
                        mesh_name: str, mesh_bytes: Optional[bytes]) -> str   # returns mesh_ref
```

| Hook | ComfyUI | Meshy |
|---|---|---|
| `chain_export` | heutige Logik: `export_node_error`, `file_format`-Endung, `export_pin` → `pinned_output_name` | `mesh_name = f"{prefix}.glb"`, keine Pins; Fehler wenn `target_formats` kein `glb` enthält |
| `chain_take_mesh` | `fetch_output(mesh_name, want_bytes)` (heute) | der Blob aus `out.blobs`, dessen `name == "model.glb"` (`want_bytes=False` → `b""` als Existenznachweis) |
| `chain_feed_mesh` (Stufe 2) | `upload_input(bytes, mesh_name)` → `input_path_ref` (heute), bzw. Pfad bei `relay: path` | `req2.upload_files[mesh_param] = (mesh_name, bytes)`; `mesh_ref = f"<upload:{mesh_name} ({MB} MB)>"` |

Regeln in `_run_chain`, die daraus folgen:

- **Relay:** `relay: path` ist nur möglich, wenn beide Stufen ComfyUI auf demselben Backend
  sind (heute schon `usable()`). Ist Stufe 1 ODER Stufe 2 Meshy, erzwingt `_run_chain`
  `relay = "upload"` (mit `mesh_bytes`), unabhängig vom gespeicherten Wert — der Editor
  zeigt das Feld dann gar nicht.
- **Cross-Backend** (`bid2 != bid`) ist bei Meshy-Beteiligung immer gegeben: Stufe-1-Slot
  wird nach `chain_take_mesh` freigegeben, `_free_comfy_vram` bleibt ComfyUI-gated.
- `req1` bekommt `meshy=cand.get("meshy")` (fehlt heute im Chain-Pfad) und `req2` ebenso.
- `keep_from_mesh` filtert weiterhin `out1.blobs` — bei Meshy also `preview.png` per Glob
  `preview.png`, wenn gewünscht; Texturen sind im GLB eingebettet, `keep` bleibt leer.
- Der Phase-1-Guard (400 „cannot be a chain stage") entfällt; an seine Stelle tritt die
  Validierung in `chain_export` (Fehler namentlich in `jobs.fail`).

### 3.2 Meshy-Rigging als Endpoint `rigging` (`meshy.py` + `MeshyAdapter`)

- `ENDPOINTS = ("image-to-3d", "multi-image-to-3d", "rigging")`; `SLOTS["rigging"] = []`;
  neu `FILES = {"rigging": ["input_mesh_path"]}` (Datei-Parameter je Endpoint).
- `public_fields(cand)` liefert neu ein **Tripel** `(params, images, files)`;
  `files[] = [{"name": "input_mesh_path", "required": True, "accept": ["glb"]}]` für
  `rigging`, `[]` sonst. Die fünf Aufrufer (Schema-Endpoint, Playground-Formular,
  Playground-POST, `_gen_image_slots`, Meshy-Editor) werden angepasst; das Schema
  bekommt `files[]` als neues Feld (ComfyUI-Kandidaten: aus `adapters.file_params`).
- Öffentliche Parameter für `rigging`: `input_name` → `name`, `input_height_m` (float,
  Default 1.7) → `height_meters`. `input_no_fingers` wird angenommen und ignoriert.
- `build_request(cand, values, images, files)` für `rigging`:
  `{"model_url": "data:model/gltf-binary;base64,…", "height_meters": …, "name": …}`;
  fehlendes `input_mesh_path` → `MeshyInput`; nur `.glb` (Magic `glTF`) → sonst
  `MeshyInput` „Meshy rigging takes a GLB". Admin-Optionen: `target_formats` ⊆
  `{glb, fbx}` (Default `["glb"]`), `animations: false` (liefert zusätzlich
  `walking.glb`/`running.glb` (+fbx je `target_formats`) als Blobs), `thumbnail` entfällt.
- `parse_task(task, formats, endpoint)`: für `rigging` stammen die URLs aus
  `task["result"]` (`rigged_character_glb_url`, `rigged_character_fbx_url`,
  `basic_animations.walking_glb_url` …) statt `model_urls`; die „fehlendes Format = Fehler"-
  Regel gilt weiter. Blob-Namen: `rigged.glb`, `rigged.fbx`, `walking.glb`, `running.glb`.
- Meshy antwortet auf `POST /rigging` laut Doku mit 200 (Image-to-3D: 202) — der 2xx-Fix
  aus 4735c7d deckt beides; Limit 300 000 Faces, nur Bipeds mit klarer Gliedmaßenstruktur,
  Blick in +Z. Ein `FAILED` bleibt final; Credits erstattet Meshy.
- `_decode_upload_files`: die Meshy-400 wird zu „Key muss in `public_fields(cand).files`
  stehen", sonst 400 wie bei ComfyUI (unbekannter Key). `files.input_mesh_path` landet in
  `req.upload_files` und wird vom Adapter als Data-URI eingebettet (kein Backend-Upload).

### 3.3 Rig-Kennzeichnung für den Client

`rig` bekommt den dritten Wert **`meshy`** (neben `generic`, `mixamo`): Meshys eigenes
Skelett mit optionalen Walk/Run-Clips. `_run_chain`s Rig-Normalisierung akzeptiert ihn,
`admin._chain_rig_warning` kennt ihn, `docs/mesh-client-spec.md` §3.3 dokumentiert ihn.
Ein `Meshy-Rig`-Standalone-Job trägt `rig: meshy` im Job-Meta wie ein Chain-Ergebnis.

### 3.4 Aliase (Store, angelegt über die Konsole)

| Alias | Task | Kandidat | Successor |
|---|---|---|---|
| `Meshy-Humanoid` | img2mesh | meshy · image-to-3d · `pose_mode: t-pose` | `mesh-mia`, `mesh_param: input_mesh_path`, `rig: mixamo` |
| `Meshy-Humanoid-Multiview` | img2mesh | meshy · multi-image-to-3d · `pose_mode: t-pose` | wie oben |
| `Meshy-Rig` | mesh2rig | meshy · rigging | – |
| `Meshy-Humanoid-Cloud` | img2mesh | meshy · image-to-3d · t-pose | `Meshy-Rig`, `rig: meshy` |

`Meshy-Object`/`Meshy-Multiview` bleiben ohne Rig. Ein ComfyUI-Alias darf `Meshy-Rig` als
Successor tragen (Cloud-Rig für lokale Meshes) — Relay dann automatisch `upload`.

### 3.5 Konsole

- **Meshy-Editor:** Abschnitt „Successor" mit Alias (nur `mesh2rig`-Aliase beider Kinds),
  `mesh_param` (Default `input_mesh_path`), `keep_from_mesh`, `rig` (`generic|mixamo|meshy`);
  kein `export_node`, kein `relay`. `meshy_update` speichert `successor` wie `update`.
- **ComfyUI-Chain-Abschnitt:** die Successor-Auswahl listet auch Meshy-`mesh2rig`-Aliase;
  ist der Successor Meshy, wird `relay` auf `upload` fixiert und ausgeblendet.
- **Playground:** `Meshy-Rig` rendert `input_mesh_path` als Datei-Upload + Job-History-
  Auswahl (das Playground-Feature vom selben Tag), aus `public_fields(...).files`.
- **Job-Ansicht:** `chain_stage2.mesh_ref` zeigt bei Meshy-Stufe-2 `<upload:gwchain_….glb (6.4 MB)>`;
  die Meshy-Tabelle (Task-ID, Request, Credits) erscheint für jede Meshy-Stufe (Stufe 1
  und/oder 2 — `meta.meshy` wird zur Liste `[{stage, task_id, endpoint, credits, request}]`).

### 3.6 Fehlerbild

| Ereignis | Verhalten |
|---|---|
| Meshy-Stufe 1 ohne `glb` in `target_formats` | Job scheitert vor dem Start: „chain needs glb in target_formats" |
| Meshy-Stufe 1 liefert kein `model.glb` (sollte nicht vorkommen) | `RuntimeError` „stage-1 produced no mesh" wie heute, final |
| Rigging `FAILED` (kein Humanoid, >300k Faces) | Job `failed` mit Meshys Meldung, final; Credits erstattet |
| Rigging 402/429 | Failover auf anderen Stufe-2-Kandidaten (falls `mesh-mia` im selben Alias — nicht vorgesehen), sonst final |
| `mesh-mia` kann Meshys GLB nicht laden | Stufe-2-Fehler final, Meldung aus ComfyUI; Stufe-1-Credits sind verbraucht — Live-Test klärt das VOR dem Alias-Rollout |
| Cancel während Stufe 1 (Meshy) | Worker-Cancel, Meshy rechnet fertig; während Stufe 2 (ComfyUI) `/interrupt` wie heute |

### 3.7 Tests

- `test_meshy.py`: `build_request` rigging (Data-URI, height, name, fehlende Datei, Nicht-GLB),
  `parse_task` rigging (result-URLs, Animationen an/aus, fehlendes Format), `public_fields`
  Tripel für alle drei Endpoints.
- `test_meshy_adapter.py`: Stub `POST /openapi/v1/rigging` (200) + Poll mit `result` →
  Blobs `rigged.glb` (+ `walking.glb` bei `animations`).
- `test_chain_hooks.py` (neu, stdlib): `ComfyUIAdapter.chain_export` reproduziert die
  heutige Export-Logik (Fixture aus `test_chain_export_node.py`), `MeshyAdapter.chain_export`
  /`chain_take_mesh` aus einem `GenOutput`, `chain_feed_mesh` beider Kinds.
- Bestehende Suiten grün; `py_compile`.
- **Live (in dieser Reihenfolge):** (1) `Meshy-Rig` standalone mit dem GLB aus Job
  fbcfb5e0 (5 Credits) — klärt, ob Meshys GLB-Format als `model_url` akzeptiert wird;
  (2) `Meshy-Humanoid` → `mesh-mia` (30 Credits + k12-gpu) — klärt, ob mia ein GLB mit
  eingebetteten Texturen riggt; (3) `Trellis2-Humanoid-High` → `Meshy-Rig` optional.

## 4. Betroffene Dateien

| Datei | Änderung |
|---|---|
| `adapters.py` | `ChainExport`, drei Hooks auf `BackendAdapter`, ComfyUI-Implementierung (verschoben aus `_run_chain`), Meshy-Implementierung; `public_fields` Tripel; `file_params` für `files[]` |
| `meshy.py` | Endpoint `rigging`, `FILES`, `build_request(…, files)`, `parse_task(…, endpoint)`, Optionen `animations`, `target_formats` je Endpoint, `public_fields` Tripel |
| `main.py` | `_run_chain` über die Hooks, Relay-Erzwingung, `req1/req2.meshy`, Rig-Wert `meshy`, `_decode_upload_files` über `files[]`, Schema `files[]`, Guard entfernt |
| `admin.py` | Meshy-Editor Successor-Abschnitt + `meshy_update`; ComfyUI-Chain-Abschnitt (Meshy-Successor, Relay-Fixierung); Playground/Schema-Aufrufer des Tripels; Job-Ansicht (`meta.meshy` Liste, `mesh_ref`) |
| `test_meshy.py`, `test_meshy_adapter.py`, `test_chain_hooks.py` | s. 3.7 |
| `README.md`, `docs/mesh-client-spec.md`, `CLAUDE.md`, `config.example.yaml` | Aliase, `rig: meshy`, `files[]` im Schema, `Meshy-Rig` |

## 5. Umsetzungsreihenfolge

1. Hooks + ComfyUI-Implementierung (reiner Refactor, Verhalten identisch, `test_chain_hooks`).
2. Meshy-Stufe-1 (`chain_export`/`chain_take_mesh`), Editor-Successor, Guard raus → Live-Test (2).
3. Endpoint `rigging` in `meshy.py`/Adapter, `files[]`-Tripel, `Meshy-Rig` standalone → Live-Test (1).
4. Meshy als Stufe 2 (`chain_feed_mesh`), `rig: meshy`, ComfyUI→Meshy-Rig → Live-Test (3), Doku.
