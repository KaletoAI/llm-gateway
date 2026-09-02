# Meshy-Backend (`type: meshy`) — Design

Datum: 2026-09-02 · Status: Entwurf zur Review · Phase 1

## 1. Ziel

Meshys Cloud-API (https://docs.meshy.ai/en/api) als **weiteres Generierungs-Backend**
neben ComfyUI anbinden, zunächst für zwei Endpunkte:

- **Image to 3D** (`POST /openapi/v1/image-to-3d`) — ein Bild → Mesh
- **Multi-Image to 3D** (`POST /openapi/v1/multi-image-to-3d`) — 1–4 Ansichten → Mesh

Die **öffentliche API des Gateways bleibt unverändert**: ein Client spricht
`POST /v1/generations` mit `model: "Meshy-Object"` bzw. `"Meshy-Multiview"` genauso an
wie heute `Trellis2-Generic-High` oder `Trellis2-Multiview` — dieselben `input_*`-Labels,
dieselben Bild-Slots, dieselben Job-Endpunkte (`GET /v1/jobs/{id}`, Cancel, Results).
Meshy ist ein Backend, kein neues Produkt.

Nicht-Ziele in Phase 1 (siehe §11): Workflow-Chains (Rig) mit Meshy als Stufe 1,
gemischte Aliase (ComfyUI + Meshy im selben Alias), Kostenbuchung nach `stats`,
Meshys Rigging/Remesh/Retexture-Endpunkte, `files`-Uploads auf Meshy-Aliase.

## 2. Was Meshy liefert (Doku-Stand 2026-09-02)

| Aspekt | Meshy |
|---|---|
| Base-URL | `https://api.meshy.ai` |
| Auth | `Authorization: Bearer msy_…` (Key aus dem Dashboard, nicht wieder abrufbar) |
| Task-Lifecycle | `POST …` → `{"result": "<task id>"}`; `GET …/{id}` → Task-Objekt mit `status` ∈ `PENDING, IN_PROGRESS, SUCCEEDED, FAILED, CANCELED`, `progress` 0–100, `preceding_tasks`, `task_error.message` |
| Ergebnis | `model_urls.{glb,fbx,obj,usdz,stl,pre_remeshed_glb}` (signierte URLs, ablaufend), `thumbnail_url`, optional `thumbnail_urls.{front,right,back,left}`, `texture_urls[]`, `consumed_credits` |
| Bilder | `image_url` bzw. `image_urls[]` als **öffentliche URL oder Base64-Data-URI**, nur JPG/JPEG/PNG. Multi-Image: 1–4, das erste ist die Front, Reihenfolge der übrigen egal |
| Kontostand | `GET /openapi/v1/balance` → `{"balance": <int>}` |
| Rate-Limits | 20 req/s pro Account; **gleichzeitige Queue-Tasks** je Tier: Pro 10, Studio 20, Premium 30, Ultra 100 — Überschreitung `429` mit `NoMoreConcurrentTasks`; keine Credits: `402` |
| Credits | Meshy-6/7: 20 ohne Textur, 30 mit, 35 bei 8K; Ultra +5; `texture_prompt`/`texture_image_url` +10. Bei `FAILED` werden Credits erstattet |
| Cancel | kein Cancel-Endpunkt; `DELETE …/{id}` löscht den Task-Datensatz |
| Streaming | SSE `…/{id}/stream` — wird **nicht** genutzt (Polling wie bei `/history`) |

Wesentliche Request-Parameter (beide Endpunkte identisch, außer `image_url` vs
`image_urls`): `ai_model` (`meshy-5|meshy-6|meshy-7|latest`), `ultra_mode`,
`should_texture`, `texture_resolution` (`2k|4k|8k`), `enable_pbr`, `texture_prompt`,
`should_remesh`, `topology` (`quad|triangle`), `target_polycount` (100–300000),
`pose_mode` (`a-pose|t-pose|""`), `image_enhancement`, `remove_lighting`,
`moderation`, `target_formats[]`, `name`, `save_pre_remeshed_model`.

## 3. Warum ein eigener Adapter (und nicht ein Pseudo-Workflow)

Drei Wege wurden abgewogen:

1. **Eigener `MeshyAdapter` (`type: meshy`)** — gewählt. Ein Alias-Kandidat auf einem
   Meshy-Backend trägt kein `workflow_json`, sondern einen kleinen `meshy`-Block
   (Endpunkt + Admin-Defaults). Die Abbildung der `input_*`-Labels auf
   Meshy-Parameter ist eine **feste Tabelle im Gateway** (§5), kein editierbares
   Node-Mapping. Sauber testbar (pures Modul), das `paid`-Tier des Schedulers greift,
   kein GPU-Host wird belegt.
2. Meshy als synthetisches Workflow-JSON, damit Mapping-Editor und `_apply_mapping`
   unverändert laufen — verworfen: der Editor fragt `/object_info`, Bypass/Prune sind
   bedeutungslos, alles wird zur Attrappe.
3. ComfyUI-Custom-Node auf einem bestehenden Host — verworfen: Key auf dem GPU-Host,
   ein Cloud-Call belegt einen ComfyUI-Slot, `paid` würde den ganzen Host markieren.

## 4. Datenmodell

Store-Blobs sind schemalos — **keine Migration**.

### 4.1 Backend (`backends`-Tabelle, config.yaml)

```yaml
- name: meshy
  type: meshy
  url: https://api.meshy.ai        # Default, in der Konsole vorbelegt
  api_key: msy_…
  max_concurrent: 4                # ≤ Queue-Limit des Meshy-Tiers (Pro 10)
  poll_interval: 5                 # s zwischen GET …/{id}; Default 5 (Tasks dauern Minuten)
  max_wait: 900                    # s bis TimeoutError; Default 900
```

- `paid` ist für `type: meshy` **immer wahr**: `load_config()` normalisiert es
  (`b["paid"] = True`), das Formular zeigt die Box angehakt und gesperrt. Ein
  Meshy-Backend ist nie ein Kandidat, solange ein unbezahltes frei ist (Scheduler-Spec
  2026-09-01).
- `host`: entfällt inhaltlich (kein GPU-Host). Die URL-Ableitung ergibt `api.meshy.ai`,
  wie heute bei OpenRouter — harmlos, Host-Policies greifen dort nicht (§7.3).
- Nicht angeboten (Formular blendet aus): `self_retries`, `comfy_*`, `auto_restart`,
  `stuck_after_s`, `read_timeout`, `disconnect_grace`.

### 4.2 Generierungs-Alias-Kandidat (`gen_aliases.candidates_json`)

```json
{"backend": "meshy", "task": "img2mesh", "model": "latest",
 "meshy": {
   "endpoint": "multi-image-to-3d",
   "options": {"should_texture": true, "enable_pbr": false, "texture_resolution": "2k",
               "topology": "triangle", "should_remesh": false, "ultra_mode": false,
               "pose_mode": "", "image_enhancement": true, "remove_lighting": true,
               "moderation": false, "target_formats": ["glb"], "thumbnail": true}},
 "retries": ""}
```

- `model` = Meshys `ai_model` (nutzt das bestehende `real_model`-Feld; die Alias-Liste
  zeigt es wie ein ComfyUI-Modell).
- `meshy.endpoint` ∈ `image-to-3d | multi-image-to-3d` — bestimmt die Bild-Slots (§5).
- `meshy.options` sind **Admin-Defaults**. Was der Client davon überschreiben darf,
  legt allein die Tabelle in §5 fest; alles andere ist Admin-Sache (Gegenstück zu
  `fixed`-Pins — ein Client kann `enable_pbr` nicht anfordern).
- `thumbnail: true` liefert Meshys `preview.png` als zusätzliches Bild-Artefakt mit —
  sonst hätte ein Mesh-Job in der Media-Liste kein Vorschaubild.
- Ein Alias ist in Phase 1 **homogen**: entweder nur ComfyUI-Kandidaten oder nur
  Meshy-Kandidaten (Editor lehnt das Mischen ab, §8.2). Grund: `_gen_alias_mapping`,
  `_decode_upload_files`, Schema und Playground lesen „den ersten Kandidaten" als
  backend-unabhängige Wahrheit.

## 5. Öffentliche Felder — die feste Label-Tabelle

Die Labels folgen `docs/mesh-client-spec.md` (`input_*`, `input_image` Pflicht). Ein
Client, der heute `Trellis2-Multiview` bedient, schickt denselben Body an
`Meshy-Multiview`.

| Public Param | Typ | `image-to-3d` | `multi-image-to-3d` | Bemerkung |
|---|---|---|---|---|
| `input_image` | Bild, **required** | `image_url` (Data-URI) | – | |
| `input_image_front` | Bild, **required** | – | `image_urls[0]` | Slot-Namen identisch zu `img2mesh-trellis2_multiview` |
| `input_image_back`, `input_image_left`, `input_image_right` | Bild, optional | – | `image_urls[1..]` | leer → weggelassen (Meshy: 1–4 Bilder) |
| `input_name` | string | `name` | `name` | max. 100 Zeichen, gekürzt |
| `input_face_num` | int | `target_polycount` **+ `should_remesh: true`** | gleich | auf 100–300000 geklemmt |
| `input_texture_resolution` | int | `texture_resolution` | gleich | ≤2048 → `2k`, ≤4096 → `4k`, sonst `8k` |
| `input_texture_prompt` | string | `texture_prompt` | gleich | neu, optional, max. 800 Zeichen; +10 Credits |
| `input_pose` | enum `a-pose|t-pose|""` | `pose_mode` | gleich | neu, optional |
| `input_remove_background` | bool | — | — | **angenommen und ignoriert** (Meshy freistellt selbst); bleibt im Schema, damit bestehende Clients nicht stolpern |
| `input_no_fingers` | bool | — | — | angenommen und ignoriert (wirkt nur in einer Rig-Stufe) |

Unbekannte `params` werden wie überall still ignoriert (mesh-client-spec §1).

**Schema** (`GET /v1/generations/{alias}/schema`) für einen Meshy-Alias listet genau
diese Felder: `params[]` mit `name/type/default` (Default aus `meshy.options` für
`input_texture_resolution`, sonst Meshy-Default), `images[]` mit
`on_empty: required` für den Pflicht-Slot und `on_empty: skip` für die optionalen
(`skip` ist ein neuer, nur von Meshy-Aliasen gelieferter Modus: der Slot wird im
Request weggelassen — es gibt weder Platzhalter noch einen Zweig zu deaktivieren).
Enum-Felder tragen zusätzlich `choices[]` (neu, optional; ComfyUI-Aliase liefern es
nicht). `loras` bleibt aus Kompatibilität im Schema, die Liste ist leer.

## 6. Neues pures Modul `meshy.py`

Nach dem Muster der Bridges: **keine** `main`-/`adapters`-Importe, hot-reload-sicher,
mit `unittest` abgedeckt (`test_meshy.py`).

```python
ENDPOINTS = ("image-to-3d", "multi-image-to-3d")
OPTION_DEFAULTS: dict            # §4.2 options, mit Meshy-Defaults
def public_fields(cand) -> tuple[list, list]     # (params[], images[]) — die Schema-Form
def build_request(cand, values: dict, images: dict[str, bytes]) -> dict
    # values = _gen_values-Ergebnis (params + inputs), images = upload_images (Label → Bytes)
    # → JSON-Body für POST; raises MeshyInput (final) bei fehlender Front / unbekanntem
    #   Bildformat (Magic-Bytes: nur PNG/JPEG → data:image/png;base64,…)
def parse_task(task: dict) -> TaskState          # status, progress, error, downloads[{fmt,url}], thumbnail, credits
def texture_res(px: int) -> str                  # 1024→"2k", 4096→"4k", 8192→"8k"
class MeshyInput(RuntimeError)                   # Content-Fehler, final
```

`build_request` ist die einzige Stelle, die die Tabelle aus §5 kennt. `parse_task` ist
die einzige, die das Task-Objekt liest.

## 7. Adapter und Routing

### 7.1 `MeshyAdapter` (in `adapters.py`, registriert in `ADAPTERS["meshy"]`)

- Klassenattribut `serves_generation = True` (neu; `ComfyUIAdapter` bekommt es auch,
  `OpenAIAdapter`/`AnthropicAdapter` `False`). `adapters.GEN_TYPES` = die Typen, deren
  Adapter es setzt. Das ersetzt `type == "comfyui"` überall dort, wo „Generierungs-
  Backend" gemeint ist (§7.3).
- `discover(client)`: `GET {url}/openapi/v1/balance` mit Bearer. 200 → `Capabilities(
  models={"meshy-5","meshy-6","meshy-7","latest"}, pricing={}, loras=set())`, der
  Stand landet in `self.credits` (+ Zeitstempel). `balance == 0` → `raise
  MeshyNoCredits("no credits left")` → Backend DOWN mit lesbarem Grund (der normale
  Weg in `refresh_backend`; `fast_probe_loop` holt es zurück, sobald wieder Credits da
  sind — es pollt `/balance` dann alle `fast_probe_interval_s`, solange Jobs warten;
  bei 20 req/s Limit unkritisch). 401 → DOWN „invalid api key" (bestehende
  Fehlerklassifikation).
- `generate(req)`:
  1. `values = _gen_values(req)`, `body = meshy.build_request(cand, values, req.upload_images)`.
     Der Kandidat reist als `req.meshy` (neues optionales Feld auf `NormalizedRequest`,
     analog `node_mapping`/`fixed`), gesetzt in `build_req`.
  2. Inflight wie ComfyUI: `inflight_inc` nur ohne `slot_held`, `finally: inflight_dec`.
  3. `POST {url}/openapi/v1/{endpoint}` → Task-ID. Antworten: `402` →
     `MeshyNoCredits`, `429` → `MeshyBusy`, `4xx` sonst → `RuntimeError(message)`
     (final, Content-Fehler), `5xx`/Transport → `ConnectionError`/httpx-Fehler.
  4. Poll `GET …/{id}` alle `poll_interval` s bis `max_wait`. `FAILED`/`CANCELED` →
     `RuntimeError(task_error.message)` (final; Credits erstattet Meshy). Ablauf von
     `max_wait` → `TimeoutError` mit der Task-ID im Text; kein DELETE (die Credits sind
     ohnehin verbucht, das Ergebnis lässt sich mit der ID noch abholen).
  5. `SUCCEEDED`: für jedes Format aus `options.target_formats` die URL aus
     `model_urls` laden (**ohne** Bearer — signierte Asset-URLs auf anderem Host),
     `GenBlob(kind="file", mime per _mime_and_kind, name="model.<fmt>")`; dazu das
     Thumbnail als `GenBlob(kind="image", mime="image/png", name="preview.png")`, wenn
     `thumbnail` an ist. Ein Format ohne URL im Ergebnis ist ein Fehler, nie eine
     stille kleinere Lieferung (dieselbe Regel wie bei `/view`).
  6. `GenOutput.meta = {"backend", "meshy_task_id", "endpoint", "ai_model",
     "request": <Body ohne Bilddaten, Bilder als "<n bytes>">, "consumed_credits",
     "elapsed_ms"}` — die Job-Ansicht zeigt damit, WAS an Meshy ging (Gegenstück zu
     `applied`/`summary` bei ComfyUI).
- `cancel(job_backend_name)` — neuer optionaler Adapter-Hook (§7.3): Meshy = no-op
  (das Polling endet mit dem Task-Cancel des Workers; Meshy rechnet weiter).
- `MeshyNoCredits` und `MeshyBusy` sind Unterklassen von `ConnectionError`, damit
  `_GEN_FAILOVER_ERRORS` unverändert bleibt und ein anderer Kandidat drankommt.
  `_fault_label` benennt beide eigens („no credits" / „Meshy queue full"), damit
  `_gen_exhausted_msg` nicht „unreachable (connection)" meldet.
- `normalize_delivery`, `validate_delivery`, `_check_glb_not_dummy`, `_cleanup_uploads`
  laufen **nicht** (kein V-Flip nötig — Meshy-GLBs tragen eingebettete Texturen; keine
  Backend-Eingabedateien, die Isolation brauchen: jedes Bild reist als Data-URI im
  Request).

### 7.2 Timing-Konstanten

| | ComfyUI | Meshy | Grund |
|---|---|---|---|
| `poll_interval` | 1 s | 5 s | Tasks dauern 1–5 min; 20 req/s-Limit gilt pro Account |
| `max_wait` | 600 s | 900 s | Warteschlange bei Meshy (`preceding_tasks`) plus Laufzeit |

### 7.3 `main.py` — Audit der ComfyUI-Hardcodings

`_comfy_backends` wird zu `_gen_backends` (`type in adapters.GEN_TYPES`),
`_llm_backends` zu `type not in GEN_TYPES`. Entscheidung je Stelle (Zeilennummern:
`master` vom 2026-09-02, Commit 5d42ca5):

| Stelle | Bedeutung | Neu |
|---|---|---|
| 987/988 `rebuild_route_index`, 2166 `_gen_routes` | Generierungs-Kandidaten | `GEN_TYPES` |
| 829, 1100, 1128, 1220, 1317, 1784, 1826, 3947, 3969 | „kein LLM-Backend" (Kataloge, Aliase, `/v1/models`) | `not in GEN_TYPES` |
| 667 gen_speed-Seed, 3675/3686 Dashboard-Split | Generierungs-Backend | `GEN_TYPES` |
| 3952 `admin.bind(comfy_backends=…)` | Konsole | zusätzlich `gen_backends=…`; `comfy_backends` bleibt für `/object_info` |
| 3083 `cancel_generation` | `/interrupt` | Adapter-Hook `cancel()`; ComfyUI implementiert `/interrupt`, Meshy no-op |
| 2479 `_free_comfy_vram`, 2500 `_unload_host_llms`, 2451 `_wait_backend_up` | Host-/VRAM-Policies, `/system_stats` | **explizit `type == "comfyui"`-gated** — heute würde `_free_comfy_vram` sonst `POST https://api.meshy.ai/free` senden |
| 169 Host-Policy, 480 Restart, 2476 `_shared_host`, 3712 `_comfy_watch_info` | GPU-Host-Semantik | bleibt ComfyUI-only |
| neu `_meshy_info(b)` | `/health` | `{"credits": n, "credits_at": ts}` für `type == "meshy"` |

Unverändert und bereits passend: `_run_job` (Failover-Schleife, `slot_held`,
`jobs.set_backend`), `_run_gen_parked`, `_gen_pick`, `_lora_eligible_names` (keine LoRAs →
keine Einschränkung), `scheduler.order_ready` (`paid`), `gen_speed`-EMA, `jobs.*`,
`_job_view`, `jobs.set_inputs` (Referenzbilder werden wie heute gespeichert).

### 7.4 Der eine neue Seam: `public_fields(cand)`

Vier Stellen lesen heute `mapping.items()` + `image_params(wf, mapping)`, um die
öffentlichen Felder eines Alias zu bestimmen: der Schema-Endpoint (`gen_alias_schema`),
das Playground-Formular (`_playground_form`), die Slot-Liste der OpenAI-Shims
(`_gen_image_slots`) und die Label→Param-Übersetzung von „Send to Playground".
Sie wechseln auf **eine** Funktion `adapters.public_fields(cand) -> (params, images)`,
die für einen ComfyUI-Kandidaten das heutige Verhalten liefert (Code zieht aus
`gen_alias_schema` dorthin um) und für einen Meshy-Kandidaten `meshy.public_fields`
aufruft. Damit rendert der Playground einen Meshy-Alias ohne eigenen Zweig:
Bild-Slots als Upload, Enum-Felder (`choices`) als Dropdown, der Rest wie bisher.

`_decode_upload_files` liefert für einen Meshy-Alias bei nicht-leerem `files` ein
`400 … accepts no files` statt sie still zu verwerfen (Strenge-Regel der
Upload-Spec 2026-08-03).

## 8. Konsole (`admin.py`)

### 8.1 Backends-Tab

- `_type_select`: Option `meshy`; ein vierter Block `#meshyopts` (`max_concurrent` mit
  Tier-Hinweis, `poll_interval`, `max_wait`), `api_key` als Pflichtfeld, `url`
  vorbelegt mit `https://api.meshy.ai`, `paid` angehakt + gesperrt. Der Save-Handler
  parst die Meshy-Felder wie die ComfyUI-Felder (Blank → weglassen).
- Backend-Liste/Hosts-Panel: Zeile zeigt **Credits** (aus `/health`) statt
  `exec_stuck`/Restart-Aktion.

### 8.2 Media-Aliase (Mapping-Tab)

- **Registrieren**: die Backend-Auswahl listet `gen_backends`. Ist das gewählte
  Backend vom Typ `meshy`, ist das Workflow-JSON nicht erforderlich (Feld bleibt
  sichtbar, wird ignoriert); es entsteht der Kandidat aus §4.2 mit `endpoint`
  `image-to-3d`, `model: latest`, Default-Options, `task: img2mesh`.
- **Editor**: `_alias_editor` verzweigt bei `cands[0].get("meshy")` in `_meshy_editor`:
  Endpoint-Select, `ai_model`-Select, ein Options-Formular (Checkboxen/Selects gemäß
  §4.2), `target_formats` als Mehrfachauswahl, `retries`, Backend-Liste (nur
  Meshy-Backends hinzufügbar). Keine Mapping-/Pins-/Bypass-/Chain-Abschnitte.
  „Add backend" auf einem ComfyUI-Alias listet keine Meshy-Backends und umgekehrt
  (Homogenität, §4.2).
- Die Media-Liste gruppiert weiter nach `task`; die Zeile zeigt statt „mapped params"
  den Endpoint.

### 8.3 Jobs & Calls › Media

Die Job-Ansicht rendert `meta.request` (was an Meshy ging) und `meta.consumed_credits`
neben den heutigen Feldern; `meshy_task_id` verlinkt auf nichts, ist aber sichtbar
(Abholen/Debuggen im Meshy-Dashboard).

## 9. Fehlerbild und Betrieb

| Ereignis | Verhalten |
|---|---|
| Key ungültig (401) | Backend DOWN, Grund im Backends-Tab/`/health` |
| Credits 0 (Discovery) | Backend DOWN „no credits left"; kommt zurück, sobald `balance > 0` |
| 402 beim POST | `MeshyNoCredits` → Failover auf den nächsten Kandidaten, sonst Job `failed: no credits`; nächste Discovery setzt DOWN |
| 429 `NoMoreConcurrentTasks` | `MeshyBusy` → Failover; Job-Fehlertext nennt das Queue-Limit. Normalfall wird durch `max_concurrent` verhindert; tritt auf, wenn andere Keys desselben Accounts die Queue füllen |
| 429 `RateLimitExceeded` | wie `MeshyBusy` (Polling-Frequenz liegt weit unter 20 req/s) |
| Task `FAILED` | Job `failed` mit Meshys Meldung, final, kein Failover (Content-Fehler; Credits erstattet) |
| `max_wait` erreicht | `TimeoutError` → Failover; Meldung enthält die Task-ID |
| Cancel durch Nutzer | Worker-Task abgebrochen, Job `failed: cancelled by user`; Meshy rechnet fertig, Credits bleiben verbraucht (dokumentiert) |
| Download-Fehler (Asset-URL) | `RuntimeError` mit Status, final — nie eine kleinere Lieferung |
| Gateway-Neustart | `reconcile_orphans` markiert laufende Meshy-Jobs `failed` (wie ComfyUI); die Task-ID ist ab Job-Erzeugung nicht bekannt — Phase 1 akzeptiert das |

Datenschutz-Hinweis: Eingabebilder und Ergebnisse liegen nach dem Job bei Meshy
(Task-Liste, ablaufende URLs). Phase 1 löscht nichts; ein optionales
`delete_after_download` ist in §11 notiert.

## 10. Tests und Verifikation

- `test_meshy.py` (stdlib `unittest`, wie `test_anthropic_bridge.py`):
  `build_request` — Einzelbild → `image_url`-Data-URI; Multiview mit Front-Pflicht,
  Reihenfolge Front zuerst, leere optionale Slots weggelassen, fehlende Front →
  `MeshyInput`; `input_face_num` → `target_polycount` + `should_remesh`; Klemmung
  100–300000; `texture_res` 1024/2048/4096/8192; Options-Defaults und Admin-Overrides;
  unbekannte `input_*` ignoriert; WebP → `MeshyInput`; Name auf 100 Zeichen gekürzt.
  `parse_task` — SUCCEEDED mit/ohne Format, FAILED-Meldung, PENDING → kein Ergebnis.
  `public_fields` — Schema-Form beider Endpunkte.
- `test_scheduler.py` bleibt; ein Fall „Meshy-Kandidat sortiert hinter freiem ComfyUI"
  ist durch `paid` bereits abgedeckt.
- Adapter-I/O gegen einen HTTP-Stub (Muster: Repro-Harness mit Stub-Instanz):
  POST→ID, zwei Polls, SUCCEEDED, Download; 402; 429; FAILED; Timeout.
- Live: ein echter Key in der Testinstanz, `Meshy-Object` und `Meshy-Multiview` über
  den Playground und per `curl` (`mode: async`, Job-Poll); Schema-Endpoint prüfen;
  `/health` zeigt Credits; Credits vor/nach vergleichen mit `consumed_credits`.
- Compile-Gate `venv/bin/python -m py_compile *.py` vor jedem Deploy (kein Build-Step).

## 11. Nicht in Phase 1 (Folgearbeit)

- **Chain mit Meshy als Stufe 1** (Mesh → ComfyUI-Rigger via `relay: upload`): der
  Adapter bräuchte `fetch_output` (erneuter GLB-Download über die Task-ID) und
  `_run_chain` müsste die Workflow-Primitiven (`export_pin`, `export_node_error`,
  `pinned_output_name`) für einen workflow-losen Kandidaten überspringen; `relay: path`
  ist unmöglich. Alternativ Meshys eigenes Rigging (5 Credits, `biped|quadruped`) als
  `mesh2rig`-Alias mit `files.input_mesh_path` → `model_url`-Data-URI.
- **Gemischte Aliase** (`Mesh-Multiview-Any`: ComfyUI unbezahlt, Meshy als bezahlter
  Fallback): technisch trägt der Scheduler das schon; nötig ist, dass Schema/Playground/
  `_decode_upload_files` nicht mehr „erster Kandidat" lesen, sondern die Felder über
  alle Kandidaten vereinigen oder je Backend auflösen.
- **Kosten nach `stats`**: Backend-Feld `credit_usd`, `stats.record_call(cost_usd=…)`
  pro Job, Anzeige im Statistic-Tab; Grundlage für `next-steps.md` §E1
  (Kosten-Quota pro User).
- `delete_after_download` (DELETE des Meshy-Tasks nach erfolgreicher Lieferung).
- Fortschritt (`progress`, `preceding_tasks`) in den Job-Status spiegeln.
- Weitere Meshy-Endpunkte (Remesh, Retexture, Text-to-3D) als eigene `endpoint`-Werte.

## 12. Betroffene Dateien

| Datei | Änderung |
|---|---|
| `meshy.py` (neu) | pures Modul §6 |
| `test_meshy.py` (neu) | §10 |
| `adapters.py` | `MeshyAdapter`, `serves_generation`/`GEN_TYPES`, `cancel()`-Hook (ComfyUI: `/interrupt`), `public_fields(cand)`, `NormalizedRequest.meshy`, `_fault_label`-Beitrag |
| `main.py` | §7.3-Audit, `build_req` setzt `req.meshy`, `_meshy_info` in `/health`, `paid`-Normalisierung, Schema-Endpoint und `_gen_image_slots` über `public_fields`, `_decode_upload_files`-Strenge |
| `admin.py` | §8: Typ-Select + `#meshyopts`, Save-Handler, Registrieren ohne Workflow, `_meshy_editor`, Backend-Zeile mit Credits, Playground über `public_fields`, Job-Ansicht `request`/`consumed_credits` |
| `README.md` | Backend-Typ `meshy` (Config-Block), Abschnitt „Media generation": Meshy-Aliase, Label-Tabelle, Credits/Limits |
| `docs/mesh-client-spec.md` | Familie `Meshy-Object` / `Meshy-Multiview` in der Familien-Tabelle (§3.2), Hinweis zu ignorierten Parametern |
| `config.example.yaml` | Beispiel-Backend + Beispiel-Alias |
| `CLAUDE.md` | Architektur-Absatz zu `meshy.py`/`MeshyAdapter` |
