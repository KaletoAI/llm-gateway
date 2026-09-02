# Tripo3D-Backend (`type: tripo`) — Design

Datum: 2026-09-03 · Status: Entwurf (autonom nach dem Meshy-Vorbild entschieden)

## 1. Ziel

Tripo3D (https://developers.tripo3d.ai/en/docs, **API V3**) als drittes
Generierungs-Backend neben ComfyUI und Meshy — mit **demselben Feature-Set wie das
Meshy-Backend** (Spec 2026-09-02 Phase 1 + 2): Image → 3D, Multi-Image → 3D, Cloud-
Rigging, beide Chain-Rollen, Admin-Optionen statt Workflow, Credits im `/health`,
Playground/Schema/Shims unverändert. Neu gegenüber Meshy: das Rigging liefert auf
Wunsch ein **Mixamo-kompatibles Skelett** (`spec: mixamo`, Default) — die offene
Meshy-Frage aus dem Speicher ist damit bei Tripo gelöst.

Die zweite, gleich wichtige Anforderung: **möglichst viel gemeinsamer Code.** Meshy
ist heute an ~60 Stellen über `cand.get("meshy") is not None` und
`type == "meshy"` verdrahtet; das darf für Tripo nicht ein zweites Mal kopiert werden.
Diese Spec führt deshalb eine **Cloud-Kind-Abstraktion** ein (§3), unter der Meshy
und Tripo zwei Ausprägungen EINES Mechanismus sind.

Öffentliche Gateway-API: unverändert. Ein Client spricht `Tripo-Object` wie
`Meshy-Object` oder `Trellis2-Generic-High` an.

## 2. Was Tripo liefert (Doku-Stand 2026-09-03, `docs/tripo-api-v3-notes.md`)

| Aspekt | Tripo V3 |
|---|---|
| Base-URL | `https://openapi.tripo3d.ai/v3` — **nur V3**; V2 (`api.tripo3d.ai/v2/openapi`) endet 2026-11-01 |
| Auth | `Authorization: Bearer <key>` |
| Hülle | JEDE Antwort `{"code": 0, "data": {…}}`; `code != 0` → `{code, message, suggestion, request_id}` |
| Eingaben | **kein Base64**. `POST /v3/files` (multipart `file`, PNG/JPEG ≤ 20 MB, GLB/FBX/OBJ/STL ≤ 150 MB) → `data.file_token`; danach `input: "<file_token>"` (oder public URL / `task_id`) |
| Image → 3D | `POST /v3/generation/image-to-model` `{input, model, texture, pbr, texture_quality, texture_alignment, geometry_quality, face_limit, quad, smart_low_poly, generate_parts, auto_size, orientation, enable_image_autofix, compress, model_seed, texture_seed}` → `data.task_id` |
| Multiview → 3D | `POST /v3/generation/multiview-to-model`, gleiche Optionen, `inputs: [{"front": tok}, {"back": tok}, …]` — `front` Pflicht, **mindestens 2 Bilder** |
| Task | `GET /v3/tasks/{id}` → `data.{status, progress, output, credits_consumed, error_code, error_message}`; `status` ∈ `queued, running, success, failed, cancelled` (V2 kannte `banned, expired, unknown` — unbekannt = terminal) |
| Ergebnis | `output.model_url` (**immer GLB**), `output.rendered_image_url`; signierte URLs, ~5 min gültig, Download ohne Auth-Header |
| Andere Formate | nur über `POST /v3/models/convert` `{input: task_id, format: "FBX"\|"OBJ"\|"STL"\|"USDZ"\|"3MF"\|"GLTF", with_animation}` (5 Credits, eigener Task) |
| Rig-Check | `POST /v3/animations/rig-check` `{input}` → Task, `output.{riggable, rig_type}`; **0 Credits** |
| Auto-Rig | `POST /v3/animations/rig` `{input, model: v1.0-20240301\|v2.5-20260210, rig_type: biped\|quadruped\|hexapod\|octopod\|avian\|serpentine\|aquatic, spec: tripo\|mixamo, out_format: glb\|fbx}` → `output.model_url`; 25 Credits |
| Retarget | `POST /v3/animations/retarget` `{input: <rig task id>, animation: "preset:walk", out_format}` → `output.model_url`; 10 Credits je Clip |
| Kontostand | `GET /v3/account/balance` → `data.{balance, frozen}` (Dezimal) |
| Fehler | kein Guthaben: **403 / code 2010**; Concurrency voll: **429 / code 2000** (+`Retry-After`); Request-Rate: 429 / 1007; 401 / 1000–1001 Key |
| Concurrency | pro Account: H-Serie 10, P-Serie 5, Animation 10, Model-Processing 5 |
| Cancel | kein Endpoint in V3 gefunden — wie Meshy: ein laufender Task läuft weiter und wird berechnet |

Modelle (`model`): `v3.1-20260211` (Default), `v3.0-20250812`, `v2.5-20250123`,
`P1-20260311`, `P2-20260801`. `face_limit`-Obergrenzen: v3.1 1.5 M, v3.0 1 M,
v2.5 500 k, P-Serie 50 k; mit `quad` 150 k. Credits: Image/Multiview 30 (mit Textur,
20 ohne), `detailed` +10, `extreme` +20, `quad` +5, `smart_low_poly` +10.

## 3. Architektur: die Cloud-Kind-Abstraktion

### 3.1 Begriffe

- **Cloud-Kind** = der Backend-`type` eines Cloud-Task-Backends: `meshy` | `tripo`.
  `adapters.CLOUD_TYPES` (frozenset) wird aus den Adapter-Klassen abgeleitet
  (`cls.cloud is True`), analog zu `GEN_TYPES`.
- **Cloud-Kandidat** = ein Gen-Alias-Kandidat mit einem Block unter dem Kind als
  Schlüssel: `cand["meshy"] = {endpoint, options}` (bestehend, unverändert) bzw.
  `cand["tripo"] = {endpoint, options}`. **Keine Store-Migration.**
- `adapters.cloud_kind(cand) -> Optional[str]` — das Kind, dessen Block vorhanden
  ist, sonst `None` (= ComfyUI-Kandidat).
- `adapters.cand_kind(cand) -> str` (`"comfyui"` oder das Cloud-Kind) und
  `adapters.backend_kind(b) -> str` (`b["type"]` wenn in `CLOUD_TYPES`, sonst
  `"comfyui"`). Ein Kandidat passt zu einem Backend gdw. `cand_kind == backend_kind`.
  Das ersetzt `(b.get("type") == "meshy") == want_meshy` in `main._gen_backend_for`
  und `admin._same_kind`.
- `adapters.cloud_module(kind)` → das pure Modul (`meshy` / `tripo`).
  `adapters.cloud_block(cand)` → der Block des Kandidaten (für `NormalizedRequest`).

### 3.2 Die pure Modul-Schnittstelle (meshy.py, tripo.py)

Beide Module exportieren dieselben Namen; Adapter, Editor und `main` sprechen nur
diese Schnittstelle an (Duck-Typing, kein ABC — sie sind Module):

| Name | Bedeutung |
|---|---|
| `KIND`, `VENDOR`, `URL` | `"tripo"`, `"Tripo"`, `"https://openapi.tripo3d.ai"` — Kind-Schlüssel, Anzeigename, Default-URL fürs Backend-Formular |
| `ENDPOINTS`, `RIG_ENDPOINT` | Endpunkt-Namen; welcher davon riggt (`"rigging"` bei Meshy, `"rig"` bei Tripo) |
| `AI_MODELS`, `FORMATS`, `RIG_FORMATS` | Modell-Auswahl, lieferbare Formate, Formate des Rig-Endpunkts |
| `OPTION_DEFAULTS`, `OPTION_FIELDS` | Admin-Optionen + ihr **Formular-Schema** (§6) |
| `SLOTS`, `FILES`, `IGNORED_PARAMS` | Bild-Slots / Datei-Felder je Endpunkt / angenommene, wirkungslose Labels |
| `endpoint_of`, `options_of`, `default_candidate`, `public_fields` | wie bei Meshy |
| `build_request(cand, values, images, files)` | Request-Body; **Bild-/Datei-Werte sind bei Tripo `file_token`-Strings, bei Meshy Bytes** — was der Adapter vor dem Bau einsetzt (§4) |
| `request_summary(body)` | Body fürs Job-Meta (Meshy: Bytes → Größe; Tripo: Tokens bleiben) |
| `parse_task(task, formats, endpoint, options) -> TaskState` | Task-Objekt → Zustand (`options` statt Meshys `animations: bool`, damit die Signatur für beide gleich ist; Meshy liest `options["animations"]`) |
| `<Kind>Input(RuntimeError)` | Inhaltsfehler, final |
| `BACKEND_HINT`, `ENDPOINT_HINT`, `CHAIN_HINT` | Hilfetexte fürs Backend-Formular / den Alias-Editor (§6) |

`TaskState` wandert nach **`cloudtask.py`** (neues pures Leaf: `TaskState` +
`parse_options(fields, form)`, §6). `meshy.py` importiert es von dort und
re-exportiert es (`from cloudtask import TaskState`), damit `test_meshy.py`
unverändert bleibt.

### 3.3 `CloudTaskAdapter` (adapters.py)

Neue Basisklasse `CloudTaskAdapter(BackendAdapter)`, `serves_generation = True`,
`cloud = True`; `MeshyAdapter` und `TripoAdapter` sind Unterklassen. Sie hält alles,
was heute in `MeshyAdapter` NICHT meshy-spezifisch ist:

- `__init__` (credits / credits_at), `_headers()` (Bearer), `mod` (Klassenattribut:
  das pure Modul), `vendor` (= `mod.VENDOR`), `type` (= `mod.KIND`).
- `generate(req)`: Slot (`slot_held`), Zeit, Client, `_run()` (Vendor-Hook), Download
  aller `state.downloads` in Reihenfolge, Thumbnail (Vendor-Hook `_thumb_name`,
  nur außerhalb des Rig-Endpunkts), Meta (§5), `inflight_dec` im `finally`.
- `_poll(client, endpoint, task_id, formats, opts, poll_interval, max_wait)`: die
  bestehende Grace-Logik (4xx dreimal = Urteil über den Task; Transport/5xx/429 =
  Service, `disconnect_grace`) — **unverändert übernommen**, mit zwei Vendor-Hooks:
  `_task_request(client, endpoint, task_id)` (Meshy: `GET /openapi/v1/<ep>/<id>`;
  Tripo: `GET /v3/tasks/<id>`) und `_task_body(r) -> dict` (Meshy: das JSON; Tripo:
  `data`, wobei `code != 0` mit HTTP 200 wie ein 4xx-Urteil zählt).
- `_create(client, url, body, mb) -> task_id`: der POST mit größenskaliertem
  Timeout und der Klassifikation der Antwort über `_classify_create(r) ->
  ("nocredits"|"busy"|"server"|"rejected"|None)` (Meshy: 402/429/5xx; Tripo:
  403+2010/429/5xx, sonst `code != 0`) und `_task_id_of(json)` (Meshy `result`,
  Tripo `data.task_id`). `_msg(r)` (Meshy `message`; Tripo `message` +
  `suggestion`).
- `_download(client, url)`: unverändert (ohne Auth-Header).
- Chain-Hooks `chain_export` / `chain_take_mesh` / `chain_feed_mesh`: der heutige
  Meshy-Code, verallgemeinert über `mod.RIG_ENDPOINT`, `mod.options_of` und
  `self.vendor` in den Meldungen. Beide Kinds verhalten sich identisch: Stage 1 muss
  `glb` liefern, ein Rig-Alias kann nicht Stage 1 sein, Stage 2 nimmt die Bytes unter
  `upload_files[mesh_param]`.

Vendor-Hooks, die JEDE Unterklasse implementiert:

- `discover(client)` — Meshy `GET /openapi/v1/balance`; Tripo `GET /v3/account/balance`
  → `int(data.balance)`; 0 → `CloudNoCredits`.
- `_run(client, req, cand, opts, poll_interval, max_wait) -> RunResult(task_id,
  endpoint, body, state)` — der Ablauf zwischen Request-Bau und Download.
  Meshy: Body bauen (Bytes → Data-URI im Modul) → `_create` → `_poll`.
  Tripo (§4): Uploads → Body → optional Rig-Check → `_create` → `_poll` →
  Folge-Tasks (Convert je Extra-Format, Retarget je Clip), deren Downloads an
  `state.downloads` angehängt werden.

Ausnahmen: `MeshyNoCredits`/`MeshyBusy` werden zu **`CloudNoCredits`/`CloudBusy`**
(`ConnectionError`-Unterklassen wie bisher, mit Attribut `vendor`); die alten Namen
bleiben als Aliase (`MeshyNoCredits = CloudNoCredits`), damit bestehende Tests und
Aufrufer weiterlaufen. `main._fault_label`/`_gen_exhausted_msg` nennen `e.vendor`.

`NormalizedRequest.meshy` wird zu **`cloud: Optional[dict]`** (der Kandidaten-Block,
kind-neutral); die drei Setz-Stellen in `main` (`_run_chain` ×2, `run_generation`)
übergeben `adapters.cloud_block(cand)`. Der Adapter baut daraus
`{"model": req.real_model, self.mod.KIND: req.cloud or {}}`.

### 3.4 Was in `main.py` und `admin.py` generisch wird

Jede Stelle, die heute `cand.get("meshy") is not None` oder `type == "meshy"`
prüft, wird über die Helfer aus §3.1 kind-neutral. Kein Verhalten ändert sich für
Meshy. Liste (vollständig, aus `grep -n meshy`):

`main.py`: `paid`-Normalisierung (`type in CLOUD_TYPES`), `_gen_backend_for`
(Kinds), `_fault_label`/`_gen_exhausted_msg` (`CloudNoCredits`/`CloudBusy` +
`vendor`), `_chain_mesh_param_error` (`cloud_kind(s2)`), `_run_chain`
(`_kind_cloud` erzwingt `upload`; `.glb`-Prüfung für einen Cloud-Nachfolger; die
Stage-1-Meta-Schlüssel §5), `_decode_upload_files` (Fehlertext nennt den Vendor),
`_gen_image_slots`, `run_generation` (`cloud=`), `_meshy_info` → `_cloud_info`
(Credits für jeden Cloud-Typ; `/health` + Backends-Tab).

`admin.py`: Badge je Kind, Backend-Formular (`#cloudopts` statt `#meshyopts`,
Felder `cloud_max_wait`/`cloud_poll_interval`, URL-Vorbelegung aus `mod.URL` je Typ,
`paid` gesperrt für `CLOUD_TYPES`, Hinweistext `mod.BACKEND_HINT`), `backend_save`,
Alias-Listen (`f"{kind} · {endpoint}"`), `register` (Backend-Typ in `CLOUD_TYPES` →
`mod.default_candidate`), `_same_kind` (Kinds), der Alias-Editor (§6), die
Chain-Felder des ComfyUI-Editors (Nachfolger ist Cloud → `upload` erzwungen,
Vendor-Name im Hinweis; Default-`mesh_param` = erstes Datei-Feld des Nachfolgers),
`chain_rig`-Optionen (+ `tripo`), Playground (`public_fields` über `cloud_kind`),
Job-Ansicht (`_meshy_table` → `_cloud_table`, `_stage2_section` nennt den Vendor).

## 4. Tripo-Ablauf im Adapter (`TripoAdapter._run`)

1. **Uploads.** Jeder belegte Bild-Slot und jedes Datei-Feld wird per
   `POST /v3/files` (multipart, Feldname `file`, Dateiname `<label>.<ext>` aus dem
   Magic-Sniff: PNG/JPEG für Bilder, `glTF` für das Mesh — ein anderes Format wird
   VOR dem Upload als `TripoInput` abgewiesen) hochgeladen; Timeout
   `_upload_timeout_for(len)`. Ergebnis `{label: file_token}`. Ein Upload-Fehler
   (Transport/5xx) ist `ConnectionError` → Failover; ein 4xx ist final.
2. **Body** = `tripo.build_request(cand, values, tokens, file_tokens)`.
3. **Rig-Check** (nur `endpoint == "rig"` und Option `rig_check`, Default an):
   `POST /v3/animations/rig-check {input}` → Task → `_poll`; `riggable: false` →
   `RuntimeError("Tripo rig-check: mesh is not riggable (detected rig_type=…)")`,
   final, BEVOR 25 Credits ausgegeben werden. Der erkannte `rig_type` landet im Meta.
4. **Haupt-Task** `_create` + `_poll` → `TaskState` mit `downloads =
   [("model.glb" | "rigged.<out_format>", model_url)]`, `thumbnail =
   rendered_image_url`, `credits = credits_consumed`.
5. **Extra-Formate**: für jedes Format in `target_formats` außer dem nativen
   (Generierung: `glb`; Rig: `out_format`) ein `POST /v3/models/convert
   {input: task_id, format: <FMT upper>, with_animation: <endpoint == rig>}` →
   `_poll` → `("model.<fmt>" | "rigged.<fmt>", model_url)` angehängt. Ein Convert,
   der fehlschlägt, lässt den Job fehlschlagen (eine angeforderte Lieferung darf
   nicht stillschweigend kleiner werden — dieselbe Regel wie Meshys fehlende URL).
6. **Animationen** (Rig): für jeden Preset-Namen in `animations`
   (`preset:walk`, …) ein `POST /v3/animations/retarget {input: <rig task id>,
   animation, out_format}` → `_poll` → `("<name>.<out_format>", model_url)`, `name` =
   Preset ohne `preset:`-Präfix und mit `:` → `_`. Ein fehlgeschlagener Clip wird
   mit Warnung übersprungen (Courtesy, wie Meshys Clips), nie der Job.
7. Credits im Meta = Summe aller `credits_consumed` der beteiligten Tasks;
   `cloud_task_id` = der Haupt-Task; Folge-Task-Ids unter `meta.tasks`
   (`[{role, task_id, credits}]`), damit die Job-Ansicht jeden berechneten Task
   nennen kann.

`max_wait` gilt für die GESAMTE Generierung (alle Tasks zusammen), gemessen ab
Start — ein Alias mit vielen Extra-Formaten braucht ein höheres `max_wait`. Das
Backend-Formular sagt das.

Defaults im Backend: `poll_interval` 2 (Doku-Empfehlung), `max_wait` 900,
`max_concurrent` ≤ 10 (H-Serie/Animation), `disconnect_grace` 30.

## 5. Datenmodell

### 5.1 Backend

```yaml
- name: tripo
  type: tripo
  url: https://openapi.tripo3d.ai     # Default, im Formular vorbelegt
  api_key: tsk_…                        # Tripo-Console → API Keys
  max_concurrent: 4
  poll_interval: 2
  max_wait: 900
```

`paid` immer wahr (wie Meshy — `type in CLOUD_TYPES`).

### 5.2 Kandidat

```json
{"backend": "tripo", "task": "img2mesh", "model": "v3.1-20260211",
 "tripo": {"endpoint": "image-to-model",
           "options": {"texture": true, "pbr": true, "texture_quality": "standard",
                       "texture_alignment": "original_image", "geometry_quality": "standard",
                       "face_limit": null, "quad": false, "smart_low_poly": false,
                       "generate_parts": false, "auto_size": false, "orientation": "default",
                       "enable_image_autofix": false, "compress": "",
                       "target_formats": ["glb"], "thumbnail": true,
                       "rig_check": true, "rig_model": "v1.0-20240301", "rig_type": "biped",
                       "spec": "mixamo", "animations": []}},
 "retries": ""}
```

- `endpoint` ∈ `image-to-model | multiview-to-model | rig`.
- `model` = Tripos `model` (nur Generierung; das Rig-Modell ist die Option
  `rig_model`, weil der Rig-Endpunkt eine ANDERE Modellreihe hat).
- `face_limit` (Admin-Face-Budget, Gegenstück zu Meshys `target_polycount`): `null`
  = Tripos adaptiver Default; gesetzt → im Request, ein Client-`input_face_num`
  gewinnt. Gültig 100 … Obergrenze des Modells (§2; bei `quad` 150 000), sonst
  ignoriert (wie `opt_polycount`).
- `animations`: Liste von Preset-Namen (`preset:walk`), nur Rig; leer = keine.
- `spec` Default **`mixamo`** — der Auftrag; `tripo` wählbar.

### 5.3 Job-Meta (beide Kinds)

`cloud` (Kind), `cloud_task_id`, `endpoint`, `ai_model`, `request`
(`request_summary`), `consumed_credits`, `elapsed_ms`, bei Rig zusätzlich
`rig: "<kind>"` und `rig_spec` (Tripo: `mixamo|tripo`; Meshy: `meshy`),
`rig_type` (Tripo), `tasks` (Tripo, §4.7). **Meshy schreibt weiterhin auch
`meshy_task_id`** (bestehende Job-Zeilen und Views lesen es); die Job-Ansicht
liest `cloud_task_id or meshy_task_id`. `_run_chain` behält die Stage-1-Meta
(`chain_stage1`) für JEDEN Cloud-Task (Schlüssel: `backend, cloud, cloud_task_id,
meshy_task_id, endpoint, consumed_credits, request, tasks`).

`rig` bekommt den vierten Wert **`tripo`**: wie `meshy` nur getaggt, nie
normalisiert/validiert (`normalize_delivery`/`validate_delivery` laufen weiter nur
für `generic`/`mixamo`). Die Bone-Namen sagt `rig_spec`. Begründung: ein Tripo-GLB
mit Mixamo-Bones ist KEIN Make-It-Animatable-GLB — die harte Skin/Textur-Validierung
der `mixamo`-Lieferung ist auf dessen bekannten 2×2-Dummy-Bug zugeschnitten und
würde ein korrektes Cloud-Rig an ComfyUI-Konventionen messen.

## 6. Öffentliche Felder und der schema-getriebene Editor

### 6.1 Label-Tabelle Tripo

| Public Param | Typ | image-to-model | multiview-to-model | rig | Bemerkung |
|---|---|---|---|---|---|
| `input_image` | Bild, required | `input` | – | – | |
| `input_image_front` | Bild, required | – | `inputs[{front}]` | – | |
| `input_image_back/_left/_right` | Bild, optional | – | `inputs[{back}]` … | – | leer → weggelassen; **weniger als 2 Bilder → `TripoInput`** (Tripo verlangt mindestens 2) |
| `input_mesh_path` | Datei, required, accept glb | – | – | `input` (file_token) | |
| `input_face_num` | int | `face_limit` | gleich | – | geklemmt auf 100…Modell-Max |
| `input_texture_resolution` | int | `texture_quality` | gleich | – | ≤2048 → `standard`, ≤4096 → `detailed`, sonst `extreme`; Default im Schema aus der Option (2048/4096/8192) |
| `input_rig_type` | string, choices | – | – | `rig_type` | Default = Option; unbekannter Wert → `TripoInput` |
| `input_name` | string | — | — | — | angenommen, ignoriert (Tripo hat kein Namensfeld) |
| `input_remove_background`, `input_no_fingers` | bool | — | — | — | angenommen, ignoriert (wie Meshy) |

Nicht advertised (und daher wie jeder unbekannte Param still ignoriert):
`input_texture_prompt`, `input_pose`, `input_height_m` — Tripo hat dafür kein
Feld; sie zu listen wäre ein Versprechen, das der Builder nicht hält.

### 6.2 `OPTION_FIELDS` — ein Editor für beide Kinds

`_meshy_editor` + `meshy_update` werden durch **`_cloud_editor(kind, alias, cands,
saved)`** + **`cloud_update`** (`POST /ui/mapping/cloud-update`) ersetzt. Der
Vendor-Block des Formulars wird aus `mod.OPTION_FIELDS` gerendert und in
`cloud_update` per `cloudtask.parse_options(mod.OPTION_FIELDS, form)` gelesen; das
Ergebnis geht durch `mod.options_of(...)` (dieselbe Normalisierung, die auch der
Request-Builder anwendet — Editor und Builder können nicht auseinanderlaufen).

Ein Feld: `{"key", "label", "type": "bool"|"select"|"tristate"|"int"|"text"|"list",
"choices"?: [(value, text)], "placeholder"?, "hint"?, "group"?, "rig_only"?: bool}`.
`tristate` = `("", "model default"), ("true", …), ("false", …)` → `None/True/False`
(Meshys `should_remesh`); `list` = kommagetrennte Strings (Tripos `animations`).

Vom Editor selbst (nicht aus dem Schema) gerendert, weil strukturell: Alias-Name,
Task, **endpoint** (`ENDPOINTS`), **model** (`AI_MODELS`), **deliver formats**
(`FORMATS`, beim Rig-Endpunkt `RIG_FORMATS`), retries, der Chain-Block (Nachfolger,
mesh param, keep, rig type — identisch für beide Kinds; `rig_opts` + `tripo`), die
Request-Felder-Tabelle (`public_fields`) und der Backends-Abschnitt. Hinweistexte:
`mod.ENDPOINT_HINT` unter dem Endpunkt, `mod.CHAIN_HINT` im Chain-Block (die
vendor-spezifischen Sätze; der gemeinsame Teil bleibt im Editor).

Meshys `OPTION_FIELDS` bildet den heutigen Editor 1:1 ab (gleiche Formularnamen
`opt__<key>`, `fmt__<f>`), sodass gespeicherte Aliase und die Optik erhalten bleiben.

Tripo-Editor-Hinweise (Inhalt, kein Layout): `texture_quality` → Credits;
`generate_parts` schließt `texture/pbr/quad/smart_low_poly` aus (Tripo weist den
Task ab — `options_of` setzt bei `generate_parts` die vier auf `false`, damit ein
gespeicherter Alias nie einen abgewiesenen Request baut); jedes Extra-Format kostet
einen Convert (5 Credits); `quad` ist praktisch nur per FBX transportierbar; Rig:
`spec mixamo` = Mixamo-Bone-Namen, `rig_model v1.0` = nur Biped/90+ Presets,
`v2.5` = alle Kreaturen/16 Presets; Clips 10 Credits je Stück.

## 7. Routing, Chains, Fehler

- Routing/Scheduler: unverändert (`paid`, `_gen_routes` über Kinds, LoRA-Logik
  greift nicht). Ein Alias bleibt homogen (ein Kind). `_same_kind` über Kinds — ein
  Meshy-Alias kann kein Tripo-Backend bekommen und umgekehrt.
- Chains: alle vier Kombinationen ComfyUI/Meshy/Tripo × ComfyUI/Meshy/Tripo laufen
  über die Basisklassen-Hooks; Tripo → Tripo-Rig lädt das GLB als Datei hoch
  (≤150 MB; ein Task-Id-Relay wäre ein Sonderfall ohne Bedarf — YAGNI).
- Fehlerklassen (Failover vs. final) wie Meshy: `CloudNoCredits` (403/2010, Balance
  0), `CloudBusy` (429), `ConnectionError` (5xx, Transport, Upload-Transport),
  `TimeoutError` (`max_wait`); final: `TripoInput` (Inhalt), abgelehnter Task
  (`code != 0` bei 4xx), `failed/cancelled/unbekannt`, Rig-Check negativ, Convert
  fehlgeschlagen.
- `/health` + Backends-Tab: `credits` + `credits_at` + `fail_rate` für jeden
  Cloud-Typ (`_cloud_info`).

## 8. Tests (stdlib unittest, wie bisher)

- `test_tripo.py` (pur): `build_request` je Endpunkt (Token-Einsatz, Multiview
  mit < 2 Bildern abgewiesen, `front` Pflicht, Klemmen von `input_face_num`,
  `texture_quality`-Buckets, `generate_parts`-Ausschluss, Rig-Body mit
  `spec: mixamo`), `parse_task` (success/failed/cancelled/unbekannt, fehlende
  `model_url` raises), `public_fields` je Endpunkt, `options_of`-Normalisierung,
  `default_candidate` (Deep-Copy), `request_summary`.
- `test_tripo_adapter.py` (HTTP-Stub nach `test_meshy_adapter.py`): Balance-Discovery
  (0 → `CloudNoCredits`), Upload → Token → Body, Poll bis `success` mit Download,
  403/2010 → `CloudNoCredits`, 429 → `CloudBusy`, `code != 0` → final, Rig-Check
  negativ → final ohne Rig-POST, Rig + Retarget + Convert liefern
  `rigged.glb` + `walk.glb` + `rigged.fbx`, Chain-Hooks (`chain_export` glb-Regel,
  `chain_feed_mesh`), Slot inc/dec auch bei Fehler.
- `test_cloudtask.py`: `parse_options` für beide `OPTION_FIELDS`
  (Roundtrip Defaults → Form → Defaults), `_cloud_editor` rendert für einen
  Meshy- und einen Tripo-Alias ohne Ausnahme und enthält jedes `opt__<key>`.
- Bestehende `test_meshy.py` / `test_meshy_adapter.py`: laufen unverändert
  (Aliase `MeshyNoCredits`/`MeshyBusy`, `parse_task`-Signatur erweitert um
  `options` mit Default `None` → Meshy liest `animations` daraus ODER aus dem alten
  bool-Argument).

## 9. Dokumentation

README (Abschnitt „Tripo3D" neben Meshy, Chain-Tabelle, Alias-Set `Tripo-Object`,
`Tripo-Multiview`, `Tripo-Humanoid` → `Tripo-Rig`, `Tripo-Rig`), `config.example.yaml`
(Backend + Aliase), `docs/mesh-client-spec.md` (`rig: tripo` + `rig_spec`,
Alias-Tabelle), `CLAUDE.md` (Cloud-Kind-Abstraktion, `tripo.py`, `cloudtask.py`,
`CloudTaskAdapter`, die Tripo-Eigenheiten: Token-Upload, `{code,data}`-Hülle,
403/2010, Folge-Tasks unter `max_wait`).

## 10. Nicht-Ziele

Text-to-Model, P-Serie-Besonderheiten über die Modellwahl hinaus, `style`,
Seeds als Client-Labels, Large-File-STS-Upload (>60 MB Empfehlung; `POST /v3/files`
nimmt bis 150 MB), Webhooks, Task-Id-Relay in Tripo→Tripo-Chains, Cancel (kein
Endpoint), Kostenbuchung nach `stats`.
