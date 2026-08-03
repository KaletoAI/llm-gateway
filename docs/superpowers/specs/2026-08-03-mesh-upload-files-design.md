# Client-Datei-Upload für Generierungs-Params (`files`): Meshes ohne Backend-Pfade

Datum: 2026-08-03 · Status: freigegeben (Kai, 2026-08-03 — Umsetzung beauftragt)

## Anlass

`mesh-shrink`/`mesh-shrink-quad` (und die Rigger direkt) nehmen ihr Eingangs-Mesh
über `input_mesh_path` — einen **Dateipfad auf dem ComfyUI-Backend**. Ein Client
kann damit nur Meshes verkleinern, die auf demselben Gateway erzeugt wurden UND
deren Output-Datei auf genau dem Backend noch liegt, das sie erzeugt hat. Das
koppelt den Client an ein bestimmtes Backend, funktioniert nach Backend-Wechsel/
Aufräumen nicht mehr, und Workarounds (Dateien liegen lassen) müllen die
Backends voll. Vorgabe von Kai: **keine Abhängigkeiten von Gateway- oder
Backend-Zustand — alle Dateien kommen vom Client, pro Request.**

Verworfene Alternativen: Job-Referenz (`job:<id>`, Gateway-Job-Store als Quelle)
— vom Client-Modell ausgeschlossen; eigener Upload-Endpoint mit TTL-Zwischen-
speicher — neuer Lifecycle im Gateway; Multipart-Variante von `/v1/generations`
— zweite Parse-Logik am nativen JSON-Endpoint. Gewählt: JSON-`files`-Feld,
symmetrisch zur bestehenden `images`-Mechanik.

## Ziel

1. Ein Client liefert das Eingangs-Mesh im Generierungs-Request selbst mit; das
   Gateway bringt es auf das Backend, auf dem der Job tatsächlich läuft
   (Routing, Parken und Failover unverändert und für den Client unsichtbar).
2. Auf den Backends entsteht dabei **kein wachsender** Datei-Müll.

Nicht-Ziel: Upload-Unterstützung in den OpenAI-Shims (`/v1/images/*`); Umbau
der Chain-Hand-offs (nutzen weiter `gwchain_<jobid>`-Namen — Follow-up-Kandidat
für dieselbe bounded-Naming-Konvention); Persistieren der Mesh-Bytes als
Job-Input im Gateway.

## 1. API (`main.py`, `POST /v1/generations`)

- Neues optionales Body-Feld `files: {"<param-oder-label>": "<base64 | data-URI | URL>"}`
  neben `images`. Für die Mesh-Aliase heißt der Key `input_mesh_path`.
- Auflösung des Keys gegen das Mapping des Alias wie überall: Label ODER
  Param-Key, Ziel muss ein **Nicht-Bild-Feld** sein (Bild-Slots laufen weiter
  über `images`).
- Wert-Dekodierung über eine verallgemeinerte Variante von `_decode_ref_image`
  (base64, data-URI, http/https-URL — ohne Bild-Sniffing). Dateiendung: aus dem
  data-URI-MIME (`model/gltf-binary` → `.glb`) bzw. dem URL-Pfad; Default `.glb`.
- Fehlerpolitik (bewusst strenger als `params`, die still ignorieren):
  - unbekannter `files`-Key → **400** mit Klartext (ein still verworfenes Mesh
    ergäbe garantiert einen kaputten Job);
  - dekodierte Datei > **64 MB** → **413**;
  - nicht dekodier-/ladbarer Wert → **400**.

## 2. Ablauf (`adapters.py`, `ComfyUIAdapter`)

- `NormalizedRequest` bekommt `upload_files: {param: (dateiname, bytes)}`
  (analog `upload_images`). `run_generation` befüllt es einmal; der Adapter
  verarbeitet es **pro dispatchtem Backend** — Failover lädt die Datei damit
  automatisch auf dem Ausweich-Backend neu hoch.
- Upload über den bestehenden `_post_upload` (`/upload/image`,
  `overwrite=true`), wie beim Chain-Relay `upload`.
- Danach wird der **absolute** Input-Pfad in den gemappten Param injiziert:
  Input-Dir des Backends = `comfy_input_dir`, sonst `…/output`→`…/input`-
  Ableitung (Logik von `main._comfy_input_dir` wandert als Adapter-Helfer nach
  `adapters.py`, main-Aufrufer bleiben funktionsgleich).
- Ist **kein** Input-Dir bekannt, schlägt der Job mit klarer Meldung fehl
  („backend has no comfy_input_dir/comfy_output_dir — cannot resolve uploaded
  file to an absolute path") statt einen nackten gespeicherten Namen zu
  übergeben, den ein Pfad-Loader (`PrimitiveString`/`GeomPackLoadMeshPath`)
  nicht auflösen kann.
- Upload-Fehler → Job `failed` mit Backend-Antwort im Fehlertext (Vorbild
  `upload_input`: ein verlorenes Mesh darf den Job nicht blind laufen lassen).

## 3. Müllvermeidung (Slot-Naming)

- Upload-Name fest pro Backend+Param: `gwup_<param>.<ext>` mit
  `overwrite=true` → pro Backend liegt **konstant genau eine** Datei je Param,
  kein Wachstum. ComfyUI hat keine Lösch-API; Überschreiben ist der einzige
  müllfreie Weg.
- Race-Betrachtung: Der Upload passiert erst nach Slot-Claim, und alle
  ComfyUI-Backends fahren `max_concurrent: 1` — während ein Job läuft, kann
  kein zweiter dasselbe Backend dispatchen, also wird die Datei nie unter einem
  laufenden Job überschrieben. Für ein Backend mit `max_concurrent > 1` fällt
  der Code auf eindeutige Namen `gwup_<jobid>_<param>.<ext>` zurück
  (dokumentierte Ausnahme: dort wächst Müll wie bei den Chains heute).
- Gateway-seitig werden die Mesh-Bytes **nicht** in `jobs/` persistiert
  (40-MB-Meshes × Jobs würden die Platte fluten); im Job-`params`-Abbild steht
  stattdessen `<param>: "<upload:dateiname (N MB)>"` für die Inspektion.

## 4. Doku

- `docs/mesh-client-spec.md`: §3.4/§3.5 umschreiben — `input_mesh_path` wird
  für Clients über `files` bedient; die Passagen „kein Upload möglich /
  Backend-Pfad nötig / output/<name> referenzieren" entfallen. §1-Tabelle um
  `files` ergänzen, §5-Beispiel um einen `mesh-shrink`-Aufruf mit `files`
  erweitern.
- README („Native job API") um das `files`-Feld ergänzen.

## Verifikation

- Compile-Gate (alle 10 Module), Deploy auf .10.
- Live-Smoke ohne GPU-Last: `files` mit ungültigem Key → 400; überlanger
  Payload → 413.
- Echter Durchstich (mit Kai abgestimmt, GPU-Zeit): kleines GLB per `files` an
  `mesh-shrink`, Ergebnis-Manifest prüfen; zweiter Lauf bestätigt, dass auf dem
  Backend weiterhin nur eine `gwup_input_mesh_path.glb` liegt.
