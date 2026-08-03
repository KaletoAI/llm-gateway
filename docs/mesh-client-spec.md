# Mesh-Generierung über das Gateway — Client-Spezifikation

Stand 2026-08-03. Gilt für die Mesh-Aliase auf dem Prod-Gateway. Maschinenlesbare
Quelle der Wahrheit ist immer `GET /v1/generations/{alias}/schema` — dieses Dokument
erklärt die Semantik dahinter; bei Abweichungen gewinnt das Schema.

---

## 1. API-Mechanik

Alle Aufrufe mit `Authorization: Bearer <key>`.

| Schritt | Aufruf |
|---|---|
| Aliase entdecken | `GET /v1/models?type=image` |
| Parameter eines Alias | `GET /v1/generations/{alias}/schema` (Namen, Typen, Defaults, Bild-Slots) |
| Job starten | `POST /v1/generations` mit `{"model": "<alias>", "mode": "sync"\|"async", "params": {…}, "images": {"input_image": "<base64\|data-URI\|URL>"}}` |
| Status/Ergebnis | `GET /v1/jobs/{job_id}` — `sync` blockt und liefert dieselbe Job-View direkt (Status `done`), `async` antwortet `202 {job_id, status: "queued", …}` |
| Artefakt laden | `GET /v1/jobs/{job_id}/result/{n}` (owner-gated, gleicher Bearer-Key) |
| Abbrechen | `POST /v1/jobs/{job_id}/cancel` |

Mesh-Jobs laufen minutenlang — **`mode: "async"` verwenden** und `GET /v1/jobs/{id}`
pollen (`elapsed_s`/`progress` sind enthalten). Ist das Backend belegt, wird der Job
**geparkt** statt abgelehnt (Status bleibt `queued`); erst nach Ablauf der Park-Zeit
kommt `503` mit `Retry-After`.

Parameter werden unter ihrem **öffentlichen Namen** (dem Label aus dem Schema)
gesendet — bei den Mesh-Aliasen durchgehend `input_*`. Unbekannte Namen werden
**still ignoriert** (dann greift der Default); nach Client-Updates lohnt ein
Abgleich gegen das Schema. `input_image` ist bei allen `img2mesh`-Aliasen Pflicht
(`on_empty: required` — fehlt es, wird der Request abgelehnt, es entsteht kein
stilles Schwarz-Mesh).

### Antwortform (Job-View)

```json
{
  "job_id": "…", "status": "queued|running|done|failed",
  "alias": "…", "backend": "…", "error": null,
  "rig": "generic|mixamo",              // nur bei Rigging-Ketten
  "warnings": ["x.glb is 41 MB (> 30 MB guideline)"],
  "results": [
    {"n": 0, "name": "Held.glb",                 "mime": "model/gltf-binary", "kind": "model", "sha256": "…", "url": "…/result/0"},
    {"n": 1, "name": "Held_basecolor_00001_.png", "mime": "image/png",         "kind": "image", "sha256": "…", "url": "…/result/1"},
    {"n": 2, "name": "Held_metallic_00001_.jpg",  "mime": "image/jpeg",        "kind": "image", "sha256": "…", "url": "…/result/2"}
  ]
}
```

---

## 2. Artefakte identifizieren und speichern — die zwei Grundregeln

**Regel 1 — Zuordnung über das Artefakt-Token im Namen**, nie über Position oder
Endung. Dateinamen folgen `<name>` = Wert von `input_name`, Zusatzartefakte
`<name>_<token>_<zähler>.<ext>` (ComfyUI hängt einen laufenden Zähler wie
`_00001_` an). Tokens: `_basecolor`, `_metallic`, `_articulationxl` (UniRig-FBX).
Match als Substring, z. B. `*_basecolor*`.

**Regel 2 — Format, Endung und MIME kommen ausschließlich aus der Antwort**
(`name` + `mime` je Result), nie aus einer Annahme. Grund: die Umkodierung nach
JPEG ist **pro Datei**, nicht pro Job. Bei Aliasen mit JPEG-Auslieferung (siehe
Katalog) kodiert das Gateway nur die Texturen um, die **keinen echten Alphakanal**
nutzen; eine Basecolor mit Transparenz bleibt PNG, während die Metallic-Map im
selben Job als JPEG (`.jpg`, `image/jpeg`, Qualität 90) rausgeht. Ein Satz wie
„bei diesem Alias sind Texturen JPEG" ist deshalb grundsätzlich falsch. Name und
MIME wechseln dabei immer gemeinsam (gleiche Quelle) — sie können nicht
auseinanderlaufen.

Weitere Zusicherungen des Gateways:

* Texturen sind bereits **V-korrigiert** ausgeliefert — der Client speichert sie
  unverändert und kompensiert nichts.
* Dateien über ~30 MB erzeugen einen Eintrag in `warnings` (Hinweis auf
  Web-Tauglichkeit, kein Fehler).
* `sha256` je Result für Integritätsprüfung.

---

## 3. Alias-Katalog

### 3.1 Varianten-Schema

Jede `img2mesh`-Familie gibt es in bis zu drei Varianten, die über das
**Speicher-Kontrakt** entscheiden:

| Variante | Pipeline | Auslieferung | Pflicht zu speichern |
|---|---|---|---|
| `-Object` | nur Mesh | `<name>.glb` (Textur eingebettet) + `*_basecolor*` + `*_metallic*` | **das GLB**; Karten optional (nur wenn die Engine eigene Maps will) |
| `-Generic` | Mesh → UniRig (Auto-Rig, FBX) | `<name>_articulationxl*.fbx` + `*_basecolor*` + `*_metallic*`; Job-Feld `rig: "generic"` | **FBX + Basecolor-Bild, untrennbar**: die FBX referenziert ihre Textur nur über einen Temp-Pfad, der beim Client ins Leere zeigt — über den gelieferten Dateinamen neu binden. Ohne das Bild ist das Asset unbrauchbar und nicht wiederherstellbar. Das Gateway garantiert das Paar in der Auslieferung (sonst schlägt der Job fehl) — das Speichern liegt beim Client. |
| `-Humanoid` | Mesh → Make-It-Animatable (mixamo-Rig, GLB) | ein GLB mit mixamorig-Skin und **eingebetteter** Textur (+ `*_metallic*.png` optional); Job-Feld `rig: "mixamo"` | **das GLB — genügt allein.** Das Gateway validiert Skin und Textur hart (bekannter Node-Bug „2×2-Dummy-Textur" → Job `failed` statt kaputtem Asset). |

`input_no_fingers` wird bei allen Familien angenommen, wirkt aber **nur** in der
`-Humanoid`-Rig-Stufe (Hände ohne Einzelfinger riggen).

### 3.2 `img2mesh`-Familien

Gemeinsame Parameter (Defaults je Familie in der Tabelle): `input_image`
(Pflicht), `input_name` (string — **immer setzen**, er bestimmt die Dateinamen;
Leerlassen übernimmt Workflow-Reste), `input_face_num` (int),
`input_texture_resolution` (int), `input_remove_background` (bool, Default true —
bei Bildern mit sauberem Alphakanal auf false setzbar), `input_no_fingers` (bool).

| Familie (Aliase) | face_num | texture_resolution | Besonderheit |
|---|---|---|---|
| `Trellis2-Generic-High`, `Trellis2-Humanoid-High` | 20000 | 1024 | höchste Qualität, längste Laufzeit |
| `Trellis2-Generic-Low`, `Trellis2-Humanoid-Low`, `Trellis2-Object-Low` | 20000 | 1024 | schnellere Pipeline |
| `Pixal3D-Generic`, `Pixal3D-Humanoid`, `Pixal3D-Object` | 50000 | 2048 | höchste Auflösung |
| `Hunyuan3D-Generic`, `Hunyuan3D-Humanoid`, `Hunyuan3D-Object` | 40000 | 1024 | ⚠ **`input_face_num` nie über 40000** — größere Werte frieren das Backend ein (kein Fehler, der Job hängt bis zum Timeout). 40000 ist der höchste nachweislich stabile Wert. |

JPEG-Auslieferung (Regel 2 aus Abschnitt 2) ist bei allen `-Object`- und
`-Generic`-Aliasen aktiv; bei `-Humanoid` bleiben Karten PNG.

### 3.3 `Triposplat-Object` — der Sonderfall

Gaussian-Splat-Pipeline, andere Parameter: **kein** `input_face_num` und kein
`input_no_fingers`; stattdessen `input_num_gaussians` (Default 10000) und
`input_texture_resolution` (Default 512, steuert Mesh-Auflösung **und**
Preprocess-Größe). Sonst wie oben (`input_image`, `input_name`,
`input_remove_background`).

Auslieferung: **nur** `<name>.glb` — es gibt keine getrennten Basecolor-/
Metallic-Karten, die Textur steckt ausschließlich eingebettet im GLB. Folgen für
den Client:

* Wer separate Karten braucht: das GLB durch **`mesh-shrink`** schicken (erzeugt
  die PNGs aus dem GLB).
* Es gibt bewusst **keine** `Triposplat-Generic`-Variante — FBX-Rigging ohne
  separate Basecolor ist unmöglich.

### 3.4 `mesh-shrink` und `mesh-shrink-quad` — Nachverdichtung

Nehmen ein vorhandenes GLB und reduzieren die Polygonzahl inkl. Neu-Bake der
Texturen. Gleiche Parameter, austauschbar:

| Alias | Verfahren | Ergebnis-Topologie |
|---|---|---|
| `mesh-shrink` | Dezimierung (Quadric Edge Collapse) | Tris, formtreu |
| `mesh-shrink-quad` | Remesh (QuadriFlow) | Quad-Topologie (z. B. für Sculpting/Subdivision) |

Parameter: `input_mesh_path` (string, Pflicht — siehe unten), `input_name`,
`input_face_num` (Default **5000**), `input_texture_resolution` (Default 1024),
`input_no_fingers` (nur Durchreichung, hier ohne Wirkung).

`input_mesh_path` ist ein **Dateipfad auf dem ComfyUI-Backend** (absolut oder
relativ zu dessen Arbeitsverzeichnis), kein Upload. Der praktische Weg: das GLB
eines **vorherigen Jobs auf demselben Gateway** referenzieren — bei den hiesigen
`img2mesh`-Workflows liegt es unter `output/<gelieferter Dateiname>` (z. B.
`output/Held.glb`). Es gibt keine API, um fremde Meshes hochzuladen; wer ein
externes Mesh verdichten will, braucht einen Weg auf das Backend-Dateisystem
außerhalb dieser API.

Auslieferung: `<name>.glb` + `*_basecolor*.png` + `*_metallic*.png` — hier
**ohne** JPEG-Umkodierung, die Karten bleiben PNG.

### 3.5 Rigger direkt (`mesh-rig-unirig`, `mesh-mia`)

Beide sind API-sichtbar, primär aber die Stage-2 der `-Generic`/`-Humanoid`-
Ketten. Direktaufruf ist möglich (`input_mesh_path` wie in 3.4), liefert aber
**nur** das Rig-Ergebnis: `mesh-rig-unirig` die nackte FBX (ohne Basecolor —
das Pflichtpaar aus 3.1 muss der Client dann selbst sicherstellen), `mesh-mia`
das geriggte GLB (Parameter nur `input_mesh_path`, `input_no_fingers`; kein
`input_name`). Im Zweifel die Ketten-Aliase verwenden — dort garantiert das
Gateway die vollständige Auslieferung.

---

## 4. Fehlerbilder

| Symptom | Bedeutung / Reaktion |
|---|---|
| `status: "failed"` + `error` | Workflow-/Validierungsfehler (z. B. „no basecolor PNG", „embedded texture is a 2x2 dummy", per-Node-Fehler des Backends). Nicht blind retrien — Fehlertext auswerten. |
| `503` + `Retry-After` beim Start | Park-Zeit abgelaufen, alle Backends belegt — nach `Retry-After` neu einreichen. |
| Job hängt lange in `running` | Mesh-Jobs dauern Minuten; `progress` beachten. Hunyuan3D mit `face_num` > 40000: siehe 3.2 — vermeiden. |
| `404` auf `result/{n}` | Job-TTL abgelaufen (Artefakte werden serverseitig aufgeräumt) — Ergebnisse zeitnah abholen und selbst speichern. |

---

## 5. Minimalbeispiel

```bash
B=https://<gateway>; K=<key>

# Job starten (async)
JOB=$(curl -s $B/v1/generations -H "Authorization: Bearer $K" -H "Content-Type: application/json" \
  -d '{"model":"Trellis2-Humanoid-Low","mode":"async",
       "params":{"input_name":"Held","input_face_num":20000,"input_no_fingers":true},
       "images":{"input_image":"data:image/png;base64,…"}}' | jq -r .job_id)

# Pollen bis done/failed
curl -s $B/v1/jobs/$JOB -H "Authorization: Bearer $K" | jq '{status, progress, rig, warnings}'

# Ergebnisse: Token matchen, Name/MIME aus der Antwort übernehmen
curl -s $B/v1/jobs/$JOB -H "Authorization: Bearer $K" \
  | jq -r '.results[] | [.n, .name, .mime] | @tsv'
curl -s -o Held.glb $B/v1/jobs/$JOB/result/0 -H "Authorization: Bearer $K"
```
