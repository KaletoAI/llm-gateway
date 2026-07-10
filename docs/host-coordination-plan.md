# Plan: Host-Tabelle + koordiniertes GPU-Handling (ComfyUI ↔ llama-swap)

Stand 2026-07-10. Recherche-Basis: Live-Incident-Analyse (llama-swap `/logs`
auf .37, `jobs.db`/`stats.db` auf prod, ComfyUI `/system_stats`) + Code-Scan
Routing/Dispatch (`main.py`) und Adapter (`adapters.py`). Zeilenangaben sind
Anker auf dem Stand nach dem Stream-Failover-Fix — bei Drift nach Symbolnamen
suchen.

**Reihenfolge:** Phase 1 → Phase 2 → Phase 3. Jede Phase ist einzeln
deploybar und ohne die nächste nützlich.

---

## Problem (verifiziert 2026-07-10)

`k12-gpu` (.37) und `evo-x2-gpu` (.34) fahren **llama-swap UND ComfyUI auf
derselben GPU** (.37: eine RTX 3090, 25 GB). Der Gateway kennt sie als zwei
unabhängige Backends (`type: openai` :8080 / `type: comfyui` :8188) und weiß
nicht, dass sie sich VRAM teilen. Belegter Ablauf:

1. Chat-Modell wird per llama-swap-TTL (120 s) entladen.
2. img2img-Job lädt das Bildmodell → ComfyUI **cached es dauerhaft im VRAM**
   (nur ~4 GB blieben frei); ComfyUI gibt nie von selbst frei.
3. Nächster Chat-Call → llama-swap-Reload → llama-server stirbt
   (exit 1 / SIGABRT) → 502 `unable to start process: upstream command
   exited prematurely but successfully`.

**Bereits gebaut (Sicherheitsnetz, deployed 2026-07-10):** genau dieses 502
ist jetzt failover-würdig (`_retryable_upstream_error` in `main.py`,
`_dispatch_over` läuft zum nächsten Kandidaten weiter; Streams liefern
Upstream-Fehler dank Early-Open als echte `Response` mit echtem Status).
Damit ist der Client-Impact weg, aber die Kollision selbst bleibt — der Plan
hier behebt die Ursache.

## Idee

Eine **Host-Tabelle**: Hosts sind die physischen Kisten; Backends werden
einem Host zugeordnet. Damit weiß der Gateway, welche Backends
zusammengehören, und kann pro Host **Policies** (den „Handler") anwenden:
Routing-Rücksicht + aktive VRAM-Freigabe.

---

## Phase 1 — Datenmodell + UI (rein strukturell, keine Verhaltensänderung)

**Store** (`store.py`): Settings-Key `hosts` nach dem Muster von
`voice_library`/`alias_park`: dict `name → {label, …policy-flags}`
(Flags kommen in Phase 2/3; Phase 1 legt nur Name/Label an).
Backend-Zuordnung: neues Feld `host` im Backend-JSON (Spalte bleibt, wie
alles Backend-Config, im `json`-Blob).

**Seeding/Migration:** einmalig beim Laden — Backends ohne `host` bekommen
als Vorschlag den **Hostnamen aus ihrer URL** (`urlparse(url).hostname`,
z. B. `192.168.8.37`); gleiche IP = gleicher Host. Damit sind k12-gpu
(openai+comfyui) und die evo-Paare sofort korrekt gruppiert, ohne dass
jemand klicken muss. Manuell umbenennbar/änderbar im UI.

**main.py:** in `rebuild_backends()` zwei Maps ableiten:
`backend_hosts: bid → host` und `host_backends: host → [bids]`. Kein
Route-Index-Impact (Hosts ändern keine Kandidatenmenge in Phase 1).
`/health` bekommt die Host-Gruppierung dazu (Sichtbarkeit).

**UI** (`admin.py`, Backends-Tab): Host-Spalte in der Backend-Liste; im
Backend-Editor ein Host-Dropdown (+ Freitext „neuer Host"); darunter ein
Hosts-Panel nach den Admin-CRUD-Konventionen (volle Breite, gemeinsame
Add/Edit-Detailseite). Phase 1 editiert dort nur Name/Label.

Aufwand: klein. Risiko: keins (keine Verhaltensänderung).

## Phase 2 — Routing-Rücksicht („media-busy" Kandidaten ans Ende)

**Flag pro Host:** `avoid_llm_during_media` (default **an** für Hosts mit
llama-swap+comfy, sonst irrelevant).

**Mechanik:** `resolve_routes()` (main.py) bewertet heute pro Kandidat die
Live-Flags (healthy/busy/draining). Neu: ein Chat-Kandidat, dessen Host
gerade einen **laufenden Media-Job** hat (Gateway weiß das selbst:
`_gen_tasks`/Jobs mit Status `running` + `backend_hosts`-Lookup), wird
**nicht entfernt, sondern ans Ende der Ready-Liste sortiert**. Ergebnis:

- Gibt es Alternativen, gewinnen die — die Kollision entsteht gar nicht.
- Gibt es keine, wird der Host trotzdem versucht (best effort); crasht der
  Load, greift das Failover aus dem Sicherheitsnetz, am Ende steht das
  echte 502.

Bewusst NICHT als harter Skip: sortieren statt filtern hält die
Parking-Semantik unangetastet (media-busy ≠ busy; es wird nicht geparkt,
nur umsortiert).

Aufwand: klein–mittel (Job→Host-Lookup + Sortierung in `resolve_routes`,
Flag im Hosts-Editor). Risiko: gering, rein ordnend.

## Phase 3 — Aktive VRAM-Freigabe (der eigentliche „Handler")

Zwei Host-Flags, beide einzeln schaltbar:

1. **`comfy_free_after_job`** (default an für geteilte Hosts): wenn ein
   Media-Job auf dem Host **endet** (done/failed/cancelled), feuert der
   Gateway fire-and-forget `POST {comfy_url}/free`
   `{"unload_models": true, "free_memory": true}` — ComfyUI gibt das VRAM
   frei, der nächste LLM-Load hat Platz. Hook: Job-Abschlusspfad in
   `main.py` (dort, wo der Job-Status finalisiert wird), NICHT im Adapter —
   der Adapter bleibt protokoll-dumm. Kompromiss: unmittelbar
   aufeinanderfolgende Media-Jobs laden das Bildmodell neu (~Sekunden);
   akzeptabel, das Flag kann sonst aus bleiben.
2. **`llm_unload_before_media`** (default aus): vor dem Submit eines
   Comfy-Workflows auf dem Host das LLM aktiv entladen, damit die
   Generation nicht ihrerseits an VRAM-Mangel scheitert. **Zu
   verifizieren:** welchen Unload-Endpoint die deployte llama-swap-Version
   anbietet (`GET/POST /unload`? per-Modell?) — vor Implementierung auf
   .37 testen. Bis dahin reicht Flag 1: die TTL (120 s) entlädt das LLM
   ohnehin schnell.

Aufwand: mittel (Comfy-`/free`-Hook klein; llama-swap-Unload je nach
Endpoint). Risiko: gering — beide Aktionen sind idempotente Aufräum-Calls,
fire-and-forget mit Log, nie im Request-Pfad blockierend.

## Nicht-Ziele

- Kein VRAM-Messen/-Budgetieren (keine verlässliche Quelle über beide
  Welten; die Policies brauchen es nicht).
- Kein Cross-Host-Scheduling von LLM- gegen Media-Last (Parking + Prioritäten
  existieren und reichen).
- Keine ComfyUI-/llama-swap-Konfigänderung auf den Boxen (deren Setup bleibt
  wie es ist; der Gateway arbeitet drumherum).

## Offene Fragen

1. llama-swap-Unload-Endpoint auf .37/.34 verifizieren (nur für Phase-3-
   Flag 2 relevant).
2. Sollen `dx10-01/-02` (falls je eine Comfy-Instanz dazukommt) dieselben
   Defaults bekommen? (Host-Tabelle macht das später zum No-Brainer.)
3. Dashboard-Gruppierung nach Host (optional, jederzeit nachrüstbar).
