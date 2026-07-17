# Review-Backlog (xhigh-Review vom 2026-07-15, Bereich `0d80ee3..HEAD`)

Rest-Findings des Multi-Agent-Reviews: 56 Kandidaten wurden verifiziert, die 15
schwersten Root-Causes sind bereits gefixt (Commit `c4b73ad`), 2 wurden widerlegt.
Übrig bleiben die 15 Einträge unten (3 Symlink-Duplikate zusammengefasst) —
überwiegend Robustheit/Effizienz/Duplikation, nichts davon blockiert den Betrieb.

## Arbeitsregeln

- **Zeilennummern sind Stand des Review-Baums und damit STALE.** Seit dem Review
  wurden `_run_chain` komplett umgebaut und die Artefakt-Delivery restrukturiert
  (Commits `c4b73ad`, `ed19146`, `b6ff280`). Jedes Finding ZUERST gegen HEAD
  verifizieren; erledigte/hinfällige direkt hier in der Datei als `~~erledigt~~`
  mit Ein-Zeilen-Begründung markieren statt sie zu fixen.
- Konventionen aus CLAUDE.md gelten: kein Test-Framework — verifizieren per
  `venv/bin/python -m py_compile *.py` + App-Import/Route-Check (und für reine
  Funktionen gern ein Wegwerf-Skript unter /tmp); `jobs`/`store`/`stats`/
  `reasoning`/`responses_bridge` bleiben dependency-frei; `adapters` ist die
  Protokoll-Naht (kein ComfyUI-HTTP in `main.py` NEU einführen).
- **Nicht deployen, nicht die laufende Instanz anfassen** — nur committen; der
  User deployt selbst.
- Reihenfolge: Abschnitt A vor B vor C.

## A — Robustheit (können Jobs/Features real brechen) — ✓ ERLEDIGT

1. ~~**adapters.py:679 — GLB-Textur-Erkennung kennt nur PNG + Baseline-JPEG (erste 4 KB).**~~
   ✓ `_glb_info` zählt jetzt `embedded_images` separat von lesbaren Dims (Präsenz ≠
   Dims); `validate_delivery` prüft Präsenz darüber, Scan-Fenster 4 KB→64 KB,
   `_image_dims` kann zusätzlich WebP (VP8/VP8L/VP8X). Unbekannt (KTX2) → present-but-unknown,
   kein „no embedded texture" mehr.

2. ~~**adapters.py:1488 — 2×2-Dummy-Check ohne Opt-out im Flat-Modus.**~~
   ✓ Per-Alias-Opt-out `dummy_check` (NormalizedRequest-Feld, default on; Output-Checkbox
   „texture check"); flat-Mode `_check_glb_not_dummy` nur noch wenn `req.dummy_check`.

3. ~~**admin.py:2762/2772/2773 — abspath/realpath-Mismatch in `static_asset`.**~~
   ✓ `_STATIC_DIR = os.path.realpath(...)` — beide Seiten kanonisch.

4. ~~**previewanim.py:86 — `add_idle` nimmt Buffer 0 = BIN-Chunk an.**~~
   ✓ Guard: `_parse` liefert `had_bin`; `add_idle` gibt unverändert zurück, wenn kein
   BIN-Chunk ODER buffer[0] eine `uri` trägt.

5. ~~**previewanim.py:90 — Bone-Lookup verfehlt kolonlose Mixamo-Namen.**~~
   ✓ Neuer `_bone_key` strippt `mixamorig`-Präfix in allen Schreibweisen (`:`/`_`/keins).

6. ~~**adapters.py:1670 — nichtdeterministische Delivery-Reihenfolge bei Glob-Siblings.**~~
   ✓ Set-Literal → geordnete Liste: reported file zuerst, Siblings nach `sorted(glob_exts)`.

7. ~~**admin.py:2788 — `?anim=idle` blockiert den Event-Loop.**~~
   ✓ `jobs.result_path` + File-Read + `add_idle` laufen über `asyncio.to_thread`.

## B — Effizienz

8. **adapters.py:1533 — `/queue`-Poll auf jedem Tick.**
   Die Vanished-Prompt-Erkennung GETtet `/queue` bei jedem Poll (1 s) über die
   gesamte Laufzeit — ~600 Extra-Requests pro 10-min-Job, doppelte Poll-Last.
   Die Grace ist 30 s; alle 5–10 Ticks reicht.

9. **main.py:1961 — path-Relay lädt das Mesh komplett, braucht aber nur Existenz.**
   Seit `ed19146` braucht das upload-Relay die Bytes wirklich; im path-Relay
   werden 30–100 MB nur für den HTTP-200-Check transferiert und bleiben über
   die ganze Stage-2-Laufzeit referenziert. Fix: im path-Modus Existenz billig
   prüfen (Range-/Head-artig oder kleiner Range-GET), Bytes nur bei upload.

## C — Duplikation / Altitude (Cleanup)

10. **adapters.py:1298 — `upload_input` dupliziert `_upload_image`** (zwei Kopien
    des `/upload/image`-POSTs; eine soll an die andere delegieren).
11. **adapters.py:691 — 2×2-Dummy-Prädikat doppelt** (`_check_glb_not_dummy` vs.
    `validate_delivery`) + Single-Use-Wrapper `_glb_texture_dims` → ein
    gemeinsames `_is_dummy(dims)`.
12. **previewanim.py:34 — GLB-Chunk-Walk re-implementiert** (`_parse` vs.
    `adapters._glb_info`) → eine gemeinsame Leaf-Helper-Funktion (Import-Richtung
    beachten: beide dürfen kein `main` importieren).
13. **main.py:1931 — ComfyUI-Protokoll leckt in `main._run_chain`**
    (`filename_prefix`, `_00001_`-Konvention, roher `/view`-Fetch) → als
    Adapter-Methode kapseln (Geschwister von `upload_input`), damit Chains
    adapter-agnostisch bleiben.
14. **main.py:1928 — `_gen_inputs_params`/`_apply_seconds` doppelt berechnet**
    (run_generation UND _run_chain). Achtung: die Chain-Wiederholung von
    `_apply_seconds` pro Attempt ist Absicht (Failover-Frische) — prüfen, ob
    Durchreichen der fertigen inputs/params die Semantik hält.
15. **main.py:1734 — `view['workflow']` dupliziert `view['alias']`** im
    Job-View-Payload; admin.py:3254 — vier fast identische Download-Karten-
    HTML-Blöcke → ein `_download_card()`-Helper.

## Abschluss

Am Ende: `py_compile` über alle Module, App-Import-Check, erledigte Punkte in
dieser Datei abhaken, EIN Commit (oder einer je Abschnitt). Nicht deployen.
