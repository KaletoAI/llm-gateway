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
| Job starten | `POST /v1/generations` mit `{"model": "<alias>", "mode": "sync"\|"async", "params": {…}, "images": {…}, "files": {…}}` |
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

**Dateien** reisen nie in `params`, sondern in eigenen Objekten: Bilder unter
`images`, alle übrigen Dateien (Meshes) unter `files` — Schlüssel ist der
öffentliche Parametername, Wert Base64, ein data-URI oder eine http(s)-URL, die
das Gateway selbst abholt. Das Gateway lädt die Datei auf genau das Backend, auf
dem der Job dann läuft; Parken und Failover ändern daran nichts, der Client sieht
nie einen Backend-Pfad. `files` ist bewusst **streng** (anders als `params`):
unbekannter Schlüssel oder unlesbarer Wert → `400`, Datei über **64 MB** → `413`
— eine still verschluckte Datei würde einen technisch erfolgreichen Job mit
falschem Eingang liefern. Die Bytes werden nicht als Job-Input gespeichert; in
der Job-Ansicht steht nur ein Vermerk `<upload:… (N MB)>`.

### Antwortform (Job-View)

```json
{
  "job_id": "…", "status": "queued|running|done|failed",
  "alias": "…", "backend": "…", "error": null,
  "rig": "generic|mixamo",              // nur bei Rigging-Ketten
  "warnings": ["x.glb is 41 MB (> 30 MB guideline)"],
  "results": [
    {"n": 0, "name": "Held_articulationxl.fbx",   "mime": "application/octet-stream", "kind": "file",  "sha256": "…", "url": "…/result/0"},
    {"n": 1, "name": "Held_basecolor_00001_.png", "mime": "image/png",                "kind": "image", "sha256": "…", "url": "…/result/1"},
    {"n": 2, "name": "Held_metallic_00001_.jpg",  "mime": "image/jpeg",               "kind": "image", "sha256": "…", "url": "…/result/2"}
  ]
}
```

(Beispiel: eine `-Generic`-Kette. `kind` ist bei Meshes — GLB wie FBX — immer
`"file"`, bei Karten `"image"`; ein Filter auf `kind == "model"` findet nichts.)

---

## 2. Artefakte identifizieren und speichern — die zwei Grundregeln

**Regel 1 — Zuordnung über das Artefakt-Token im Namen**, nie über Position oder
Endung. Basis ist `<name>` = Wert von `input_name`; das Token steht als Suffix
davor der Endung. Match als Substring, z. B. `*_basecolor*`:

| Quelle | Namensmuster | Zähler? |
|---|---|---|
| ComfyUI-Export (Meshes, Texturkarten) | `<name>_00001_.glb`, `<name>_basecolor_00001_.png`, `<name>_metallic_00001_.jpg` | ja — ComfyUI hängt `_00001_` an, **auch ans Haupt-GLB** |
| UniRig (`-Generic`) | `<name>_articulationxl.fbx` | nein |
| Make-It-Animatable (`-Humanoid`) | `…_rigged.glb` — in Ketten mit **technischem** Präfix `gwchain_<jobid>…`, nicht `<name>` | nein |

Tokens: `_basecolor`, `_metallic`, `_articulationxl` (FBX), `_rigged` (Humanoid-GLB).
Verlasse dich für das Haupt-Asset nicht auf `<name>` — bei Humanoid-Ketten trägt
das GLB den internen Ketten-Namen; das Mesh ist zuverlässig das Result mit
`kind: "file"`.

**Regel 2 — Format, Endung und MIME kommen ausschließlich aus der Antwort**
(`name` + `mime` je Result), nie aus einer Annahme. Grund: die Umkodierung nach
JPEG (nur bei `-Generic`-Auslieferungen, siehe unten) ist **pro Datei**, nicht
pro Job: das Gateway kodiert nur die Texturen um, die **keinen echten
Alphakanal** nutzen; eine Basecolor mit Transparenz bleibt PNG, während die
Metallic-Map im selben Job als JPEG (`.jpg`, `image/jpeg`, Qualität 90)
rausgeht. Ein Satz wie „bei diesem Alias sind Texturen JPEG" ist deshalb
grundsätzlich falsch. Name und MIME wechseln dabei immer gemeinsam (gleiche
Quelle) — sie können nicht auseinanderlaufen.

Weitere Zusicherungen des Gateways:

* **Nur bei `rig: "generic"`-Auslieferungen** (die `-Generic`-Ketten) normalisiert
  das Gateway die Texturkarten: V-Flip passend zu den FBX-UVs plus die
  JPEG-Umkodierung von oben — der Client speichert sie unverändert und
  kompensiert nichts. Bei `-Object`-, `-Humanoid`- und Shrink-Auslieferungen
  werden die Karten **unverändert wie vom Backend erzeugt** durchgereicht
  (immer PNG); dort gilt die Orientierung des jeweiligen Bakes.
* Dateien über ~30 MB erzeugen einen Eintrag in `warnings` (Hinweis auf
  Web-Tauglichkeit, kein Fehler).
* `sha256` je Result für Integritätsprüfung.
* **Eingangs-Isolation (Zusicherung):** Gleichzeitig laufende Jobs teilen sich
  keinen Eingangs-Zustand — auch nicht bei verschiedenen Aliasen auf derselben
  Backend-Instanz. Jede hochgeladene Datei (Bild wie Mesh) liegt unter einem
  job-eindeutigen Namen; schlägt ein Upload fehl, scheitert der Job, statt mit
  fremden Bytes weiterzurechnen. Muss serialisiert werden, wird der zweite Job
  wie gehabt **geparkt** (`queued`), nie abgelehnt.
* **`input_images[]` in der Job-View** führt `sha256` und `bytes` je
  Referenzbild — analog zu `results[]`. Damit lässt sich nachweisen, welche
  Bytes tatsächlich in den Job gingen: den lokal berechneten Hash des
  hochgeladenen Bildes dagegen prüfen. (Jobs von vor dem 03.08.2026 haben den
  Wert nicht und liefern `null`.)

---

## 3. Alias-Katalog

### 3.1 Varianten-Schema

Jede `img2mesh`-Familie gibt es in bis zu drei Varianten, die über das
**Speicher-Kontrakt** entscheiden:

| Variante | Pipeline | Auslieferung | Pflicht zu speichern |
|---|---|---|---|
| `-Object` | nur Mesh | `<name>_00001_.glb` (Textur eingebettet) + `*_basecolor*.png` + `*_metallic*.png` (unverändert, siehe Abschnitt 2) | **das GLB**; Karten optional (nur wenn die Engine eigene Maps will) |
| `-Generic` | Mesh → UniRig (Auto-Rig, FBX) | `<name>_articulationxl.fbx` + `*_basecolor*` + `*_metallic*` (V-korrigiert, ggf. JPEG); Job-Feld `rig: "generic"` | **FBX + Basecolor-Bild, untrennbar**: die FBX referenziert ihre Textur nur über einen Temp-Pfad, der beim Client ins Leere zeigt — über den gelieferten Dateinamen neu binden. Ohne das Bild ist das Asset unbrauchbar und nicht wiederherstellbar. Das Gateway garantiert das Paar in der Auslieferung (sonst schlägt der Job fehl) — das Speichern liegt beim Client. |
| `-Humanoid` | Mesh → Make-It-Animatable (mixamo-Rig, GLB) | ein GLB `*_rigged.glb` (technischer Name!) mit mixamorig-Skin und **eingebetteter** Textur (+ `*_metallic*.png` optional); Job-Feld `rig: "mixamo"` | **das GLB — genügt allein.** Das Gateway validiert Skin und Textur hart (bekannter Node-Bug „2×2-Dummy-Textur" → Job `failed` statt kaputtem Asset). |

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
| `Hunyuan3D-Generic`, `Hunyuan3D-Humanoid`, `Hunyuan3D-Object` | 40000 | 1024 | ⚠ **`input_face_num` nie über 40000** — größere Werte frieren das Backend ein (kein Fehler, der Job hängt bis zum Timeout). 40000 ist der höchste nachweislich stabile Wert. `-Object` liefert zusätzlich frei wählbare LOD-Stufen, siehe unten. |

#### LOD-Stufen bei `Hunyuan3D-Object`

Dieser Alias liefert neben dem Hauptergebnis **beliebig viele reduzierte Fassungen**
desselben Modells. Gesteuert wird das über den Parameter **`input_lod_faces`** —
eine kommaseparierte Liste von Ziel-Dreieckszahlen, **als String gesendet**:

```json
"params": {"input_name": "Held", "input_face_num": 20000, "input_lod_faces": "8000,4000,2000"}
```

Ein einzelner Wert (`"5000"`, der Default) ist ebenso gültig. Die Stufen werden
**nicht nachträglich verkleinert**, sondern aus denselben generierten Ansichten neu
aufgebacken — jede ist qualitativ eigenständig und **selbsttragend** (Texturen
eingebettet, keine Begleitdateien nötig). Der Job liefert **alle** angeforderten
Stufen aus; was davon behalten wird, entscheidet der Client. Jede zusätzliche Stufe
kostet nur wenige Sekunden.

Regeln für die Auswertung:

* **Dateiname = angeforderter Wert**, nicht der tatsächliche: `<name>_<zahl>.glb`.
  Wer die echte Dreieckszahl braucht, liest sie aus dem GLB.
* **Stufen oberhalb der Ausgangsgröße liefern eine Kopie in Originalgröße.**
  `input_face_num` ist beim Hauptergebnis nur eine **Obergrenze**; liefert das
  Modell von sich aus weniger, laufen darüberliegende Stufen ins Leere (gemessen:
  Hauptergebnis 4.972 Dreiecke → `_8000.glb` enthält ebenfalls 4.972, `_4000.glb`
  und `_2000.glb` treffen exakt).
* **Die Reihenfolge in der Antwort ist alphabetisch nach Dateiname**, nicht die der
  Eingabe: `"8000,4000,2000"` kommt als `_2000`, `_4000`, `_8000` zurück. Ordne über
  den Dateinamen zu, nie über die Position.
* **`input_name` pro Job eindeutig wählen.** Die Stufen liegen backendseitig unter
  dem Namen; ein zweiter Lauf mit demselben Namen und weniger Stufen liefert die
  älteren Stufen mit aus.
* Die LOD-Dateien sind wegen der Textur-Einbettung als Data-URI oft **größer** als
  das Hauptergebnis — sie sind nicht als „kleine Datei" gedacht, sondern als
  geometrisch leichteres Modell.

Erkennung in der Antwort: das Hauptergebnis endet auf `_00001_.glb`, die LOD-Dateien
auf `_<zahl>.glb` (also Ziffer direkt vor der Endung).

V-Flip und JPEG-Umkodierung (Abschnitt 2) greifen nur bei den
`-Generic`-Aliasen; bei `-Object` und `-Humanoid` kommen die Karten unverändert
als PNG.

### 3.3 `Triposplat-Object` — der Sonderfall

Gaussian-Splat-Pipeline, andere Parameter: **kein** `input_face_num` und kein
`input_no_fingers`; stattdessen `input_num_gaussians` (Default 10000) und
`input_texture_resolution` (Default 512, steuert Mesh-Auflösung **und**
Preprocess-Größe). Sonst wie oben (`input_image`, `input_name`,
`input_remove_background`).

Auslieferung: **nur** `<name>_00001_.glb`, und dieses GLB ist technisch anders
gebaut als das aller anderen Aliase: die Farbe steckt **in den Vertices**
(`COLOR_0`), es hat **keine UVs und keine Texturbilder**. Für den Client heißt
das:

* Es gibt **keine** getrennten Basecolor-/Metallic-Karten und keinen Weg, sie
  nachträglich zu erzeugen — `mesh-shrink` kann Triposplat-Meshes **nicht**
  verarbeiten (es braucht UVs, siehe 3.4).
* Es gibt bewusst **keine** `Triposplat-Generic`-Variante — FBX-Rigging ohne
  separate Basecolor ist unmöglich.
* Beim Rendern beachten: Vertex-Farben brauchen einen Shader, der `COLOR_0`
  auswertet (das Material ist `KHR_materials_unlit`); Engines, die nur
  Texturmaterialien erwarten, zeigen das Modell sonst einfarbig.
* Wer texturierte Meshes mit separaten Karten braucht, nimmt eine der anderen
  Familien (`Trellis2-*`, `Pixal3D-*`, `Hunyuan3D-*`).

### 3.4 `mesh-shrink` und `mesh-shrink-quad` — Nachverdichtung

Nehmen ein vorhandenes GLB und reduzieren die Polygonzahl inkl. Neu-Bake der
Texturen. Gleiche Parameter:

| Alias | Verfahren | Ergebnis-Topologie | Status |
|---|---|---|---|
| `mesh-shrink` | Dezimierung (Quadric Edge Collapse) | Tris, formtreu | **verfügbar**, trifft `input_face_num` exakt (gemessen: 5.000 angefordert → 5.000 Dreiecke) |
| `mesh-shrink-quad` | Remesh (Instant Meshes) | Quad-Topologie (z. B. für Sculpting/Subdivision) | verfügbar, aber `input_face_num` wirkt nur als **grober Richtwert** (gemessen: 5.000 angefordert → ~38.000 Dreiecke). Wer eine verlässliche Zielgröße braucht, nimmt `mesh-shrink`. |

**Voraussetzung an das Eingangs-Mesh:** es muss **UVs und eine eingebettete
Textur** mitbringen — die Verkleinerung backt die Textur auf die neue Topologie
um und braucht dafür das texturierte Original. Ergebnisse von `Trellis2-*`,
`Pixal3D-*` und `Hunyuan3D-*` erfüllen das. **Triposplat-GLBs nicht** (nur
Vertex-Farben, siehe 3.3): der Job scheitert dann mit
`node 21 Trellis2RenderMultiViewNvdiffrast: expected np.ndarray (got NoneType)`
— das ist genau dieser Fall, kein vorübergehender Fehler, ein Retry hilft nicht.

Parameter: `input_mesh_path` (Pflicht — das Mesh selbst, siehe unten),
`input_name`, `input_face_num` (Default **5000**), `input_texture_resolution`
(Default 1024), `input_no_fingers` (nur Durchreichung, hier ohne Wirkung).

Das Eingangs-Mesh kommt als **Datei im Request** — unter `files`, nicht in
`params`:

```json
{"model":"mesh-shrink","mode":"async",
 "params":{"input_name":"Held","input_face_num":5000},
 "files":{"input_mesh_path":"data:model/gltf-binary;base64,…"}}
```

Erlaubt sind Base64, ein data-URI (die Endung leitet das Gateway aus dem MIME
ab, sonst `.glb`) und eine http(s)-URL, die das Gateway abholt; Grenze 64 MB
(Abschnitt 1). Damit ist jedes GLB verdichtbar — auch eines, das nie über dieses
Gateway lief: Upload-Ziel, Backend-Wahl und Pfad regelt das Gateway pro Job
selbst. Ein vorheriges Job-Ergebnis wird also normal abgeholt
(`GET /v1/jobs/{id}/result/{n}`) und beim Shrink-Aufruf wieder mitgeschickt.

(Ein direkt in `params` gesetzter `input_mesh_path` bleibt weiterhin ein
Dateipfad auf dem Backend — nützlich für Server-Admins mit Zugriff auf dessen
Dateisystem, für Clients ist `files` der Weg.)

Auslieferung: `<name>_00001_.glb` + `*_basecolor*.png` + `*_metallic*.png` — hier
**ohne** JPEG-Umkodierung, die Karten bleiben PNG.

**Dreiecke vs. Vertices.** `input_face_num` steuert die **Dreiecke** und wird exakt
getroffen. Die Vertex-Zahl bleibt deutlich darüber (gemessen: 5.000 Dreiecke →
11.129 Vertices): Das Texturbacken trennt das Mesh entlang der UV-Nähte auf, und
jeder Naht-Vertex existiert danach mehrfach. Das ist unvermeidlich, wenn ein Mesh
eine Textur tragen soll — wer Vertices zählt, misst das UV-Layout mit, nicht die
Geometrie.

### 3.5 Rigger direkt (`mesh-rig-unirig`, `mesh-mia`)

Beide sind API-sichtbar, primär aber die Stage-2 der `-Generic`/`-Humanoid`-
Ketten. Direktaufruf ist möglich (Mesh über `files.input_mesh_path` wie in 3.4),
liefert aber **nur** das Rig-Ergebnis: `mesh-rig-unirig` die nackte FBX (ohne
Basecolor — das Pflichtpaar aus 3.1 muss der Client dann selbst sicherstellen),
`mesh-mia` das geriggte GLB `*_rigged.glb` (Parameter nur `input_mesh_path`,
`input_no_fingers`; kein `input_name`). Achtung Namensfalle: der Alias
`mesh-mia` führt das **Make-It-Animatable**-Workflow aus (mixamo-Rig → GLB) —
der gleichnamige FBX-Rigger „MIA" (`MIAAutoRig`, Workflow `mesh-reg-mia`) ist
auf dem Gateway **nicht registriert**. Im Zweifel die Ketten-Aliase verwenden —
dort garantiert das Gateway die vollständige Auslieferung.

---

## 4. Fehlerbilder

| Symptom | Bedeutung / Reaktion |
|---|---|
| `status: "failed"` + `error` | Workflow-/Validierungsfehler (z. B. „no basecolor PNG", „embedded texture is a 2x2 dummy", per-Node-Fehler des Backends). Nicht blind retrien — Fehlertext auswerten. |
| `503` + `Retry-After` beim Start | Park-Zeit abgelaufen, alle Backends belegt — nach `Retry-After` neu einreichen. |
| Job hängt lange in `running` | Mesh-Jobs dauern Minuten; `progress` beachten. Hunyuan3D mit `face_num` > 40000: siehe 3.2 — vermeiden. |
| `404` auf `result/{n}` | Job-TTL abgelaufen (Default **24 h**, `ttl_s` 86400) — Artefakte werden serverseitig aufgeräumt; Ergebnisse zeitnah abholen und selbst speichern. |

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

Dasselbe GLB nachträglich verdichten — das Mesh geht als Datei mit, ein
Backend-Pfad ist nicht nötig:

```bash
# GLB als data-URI einbetten (Base64 ohne Zeilenumbrüche)
MESH="data:model/gltf-binary;base64,$(base64 -w0 Held.glb)"

SHRINK=$(jq -n --arg m "$MESH" \
  '{model:"mesh-shrink",mode:"async",
    params:{input_name:"Held_lo",input_face_num:5000,input_texture_resolution:1024},
    files:{input_mesh_path:$m}}' \
  | curl -s $B/v1/generations -H "Authorization: Bearer $K" \
         -H "Content-Type: application/json" -d @- | jq -r .job_id)

curl -s $B/v1/jobs/$SHRINK -H "Authorization: Bearer $K" | jq '{status, error}'
```
