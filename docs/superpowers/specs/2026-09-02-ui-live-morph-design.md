# Live-Morph: ein Auto-Update-Mechanismus für die /ui-Konsole

*Design, 2026-09-02*

## Problem

Die Konsole aktualisiert sich an sechs Stellen selbsttätig. Vier davon laden die
**gesamte Seite** neu (`<meta http-equiv="refresh">`), und das sind ausgerechnet die
Ansichten, in denen man beim Zusehen etwas tut:

| Ort | Mechanismus | Aktiv wenn |
|---|---|---|
| Dashboard (`admin.py` `dashboard_page`) | Meta-Refresh 4 s | immer |
| Media Jobs, Liste (`jobs_page`) | Meta-Refresh 5 s | ein Job ist `queued`/`running` |
| Media Job, Detail (`job_detail_page`) | Meta-Refresh 2 s | Job ist `queued`/`running` |
| Backends (`backends_page`) | Meta-Refresh 4 s | ein Backend drainiert |
| Media Playground, Ergebnisspalte (`_PG_POLL_JS`) | fetch-Fragment 2 s | laufender Job |
| Voice-Upload, Fortschritt (`_VU_POLL_JS`) | fetch-Fragment 1 s, danach `location.replace` | laufender Upload |

Der Vollreload kostet bei jedem Takt den gesamten Interaktionszustand: Scrollposition,
Sortierung, Filtereingabe samt Cursor, offene Dropdowns, halb ausgefüllte Formulare,
laufende `<video>`/`<audio>`-Wiedergabe und die Kameraposition des `model-viewer` in
der Mesh-Vorschau.

Der Beleg dafür, wie teuer das ist, steht bereits im Code: drei JS-Blöcke existieren
ausschließlich, um Teile dieses Zustands über den Reload zu retten — `_SCROLL_JS`
(Scrollposition), `_SORT_JS` (Sortierung; Kommentar: *„so it survives the dashboard's
4s auto-refresh"*) und `_FILTER_JS` (Filtertext, Fokus und Caret, mit einem
15-Sekunden-Fenster). Was sich nicht serialisieren lässt — Medienwiedergabe,
Kamerapose — ist schlicht verloren.

## Ziel

**Ein** Mechanismus für alle sechs Stellen, der nur das aktualisiert, was sich
tatsächlich geändert hat, und den Interaktionszustand unangetastet lässt. Kein neuer
Endpunkt je Ansicht, keine Abhängigkeit von außen (die Konsole ist bewusst
dependency-frei), und die bestehende `refresh=N`-Signatur bleibt Aufrufern erhalten.

## Nicht-Ziele

- Kein Server-Push (SSE/WebSocket). Verworfen: verlangt Change-Signale aus `main.py`
  und `jobs.py` in die Konsole sowie eine offene Verbindung je Tab — viele bewegliche
  Teile für einen Nutzen, den ein 2–5-Sekunden-Takt praktisch auch liefert.
- Keine Fragment-Endpunkte je Ansicht. Verworfen: sechs Refactorings jetzt und je
  künftiger Live-Ansicht ein weiteres; der Traffic-Vorteil wiegt das nicht auf.
- Kein Live-Update für Ansichten, die heute keines haben (LLM Calls, Statistic,
  Routing, Mapping bleiben statisch).

## Architektur

### Serverseite

`_page()` gibt statt des Meta-Tags ein Attribut auf dem Scroll-Container aus:

```
vorher:  <head> … <meta http-equiv="refresh" content="4"> … </head>
nachher: <main data-live="4"> … </main>
```

`_LIVE_JS` wird wie `_SORT_JS` global am Body-Ende eingebunden. Die vier Aufrufer
mit `refresh=…` bleiben unverändert. `refresh=None` behält seine Semantik: fehlt
`data-live` in der Antwort, stellt der Poller sich ab — so, wie heute der Meta-Refresh
mit dem letzten Reload verschwindet. `<main>` ist laut CSS (`main{overflow-y:auto}`)
der Scroll-Container; weil er nie ersetzt wird, bleibt die Scrollposition ohne jedes
Zutun erhalten.

Bewusst **kein** `X-Gw-Live`-Header, der serverseitig die Navigation wegließe: die
Ersparnis liegt im niedrigen einstelligen Kilobyte-Bereich und der Meta-Refresh
rendert heute ohnehin die volle Seite. Die Serverlast ändert sich durch dieses Design
nicht.

### Clientseite: `_LIVE_JS`

Poll-Schleife pro Takt:

1. `document.hidden` → Takt überspringen, Nachhol-Flag setzen (`visibilitychange`
   pollt dann sofort).
2. `fetch(location.href, {cache:'no-store', credentials:'same-origin'})`.
3. `r.redirected` und abweichender Pfad → echtes `location.href = r.url`, Poller
   stoppt. Fängt die abgelaufene Session ab (`_ui_guard` leitet auf `/ui/login` um);
   ohne diesen Zweig würde das Login-Formular in den Content gemorpht.
4. `!r.ok` → alten DOM stehen lassen, Intervall verdoppeln bis maximal 30 s, bei der
   nächsten erfolgreichen Antwort zurück auf den Ausgangswert.
5. `DOMParser` → neues `<main>`. Fehlt es, Poller stoppen.
6. `morph(altesMain, neuesMain)`.
7. `data-live` aus dem neuen `<main>` übernehmen; fehlt es oder ist es `0`, Poller
   stoppen.
8. Post-Morph-Hooks aus `window.gwLiveHooks` ausführen.

### Der Morph-Algorithmus

Kinderabgleich schlüsselbasiert: Schlüssel ist `id`, sonst `data-k`, sonst gilt
Position plus Tagname. Zwei Knoten gelten als derselbe, wenn ihre Schlüssel
übereinstimmen oder (schlüssellos) Position, `nodeType` und `tagName` gleich sind.

- derselbe Knoten → Attribute synchronisieren (geänderte setzen, fehlende entfernen),
  dann rekursiv über die Kinder
- Textknoten → `data` nur zuweisen, wenn er abweicht
- passender Schlüssel an anderer Stelle → Knoten verschieben, dann morphen
- sonst → ersetzen
- übrige alte Kinder entfernen, übrige neue anhängen

Fünf Schutzregeln, jede gegen einen konkreten Schaden:

| Regel | Verhindert |
|---|---|
| `<script>` wird nie ersetzt und nie eingefügt | dass `_JOB_TICK`s `setInterval` sich pro Update vervielfacht; per `innerHTML` eingefügte Skripte liefen ohnehin nicht |
| `[data-live-skip]` überspringt den ganzen Teilbaum, Attribute inklusive | Notausgang für alles, was ein Update nicht verträgt |
| `input`/`textarea`/`select`: `value`/`checked` wird nicht geschrieben, wenn das Element `document.activeElement` oder dirty ist (`value !== defaultValue`) | dass ein halb getippter Filter oder ein Formular überschrieben wird |
| `video`/`audio`/`img`/`model-viewer`/`iframe` mit unveränderter `src` bleiben unberührt; bei geänderter `src` wird ersetzt | Wiedergabeabbruch und Kamerareset in der Mesh-Vorschau |
| `open` auf `<details>` wird nicht synchronisiert | dass ein aufgeklappter Abschnitt zuklappt — der Server kennt diesen Zustand nicht |

### Post-Morph-Hooks

`window.gwLiveHooks` ist ein Array von Funktionen, das `_LIVE_JS` nach jedem Morph
abarbeitet. Zwei Registrierungen:

- `_SORT_JS` trägt ein erneutes `sortIt()` ein. Nötig, weil der Server stets
  Einfügereihenfolge liefert und der Morph die clientseitige Sortierung sonst
  zurückdrehen würde.
- `_FILTER_JS` trägt `window.sfRun()` ein, damit neu eingemorphte Zeilen dem aktiven
  Filter unterliegen.

## Aufräumen

Entfernt wird, was ausschließlich den Reload kompensiert:

- `_FILTER_JS`: der Fokus-und-Caret-Restore samt 15-Sekunden-Fenster und der
  `beforeunload`-Save.
- `_SCROLL_JS`: die Restaurierung von `<main>.scrollTop` (der Container wird nie mehr
  ersetzt).

Erhalten bleibt, was **echte** Navigation bedient und mit Auto-Refresh nichts zu tun
hat: die Sortier- und Filterpersistenz in `sessionStorage` sowie der `.col`-Scroll mit
dem `|master`-Schlüssel, der bewusst die Query-String weglässt, damit die linke Spalte
beim Durchklicken einer Master/Detail-Liste ihre Position behält. Die Features
Sortieren und Filtern selbst bleiben unverändert.

## Umsetzung in zwei Phasen

**Phase 1 — die vier Meta-Refresh-Seiten.** `_LIVE_JS` schreiben, `_page()` umstellen,
Hooks in `_SORT_JS`/`_FILTER_JS` registrieren, die zwei Reload-Krücken entfernen.
Dashboard, Media Jobs, Job-Detail und Backends werden dadurch ohne eine einzige
Änderung an ihren Seitenfunktionen live. Wird verifiziert, bevor Phase 2 beginnt.

**Phase 2 — die zwei Fragment-Poller.**

- Media Playground: `_PG_POLL_JS` und die Route `/ui/playground/status/{job}` entfallen;
  `playground_page` setzt `refresh=2`, solange ein Job läuft. Die Dirty-Input-Regel
  leistet dann, was der heutige Kommentar bei `_PG_POLL_JS` verspricht („so the form
  stays editable"). Das `data-poll-job`-Attribut und der `data-jobdone`-Marker
  entfallen mit.
- Voice-Upload: `_VU_POLL_JS`, die Route `/ui/playground/voice-upload-status` und das
  abschließende `location.replace(…&vu=done)` entfallen; `voiceplay` setzt `refresh=1`,
  solange der Fortschritt des angemeldeten Nutzers nicht `done` ist. Die
  Bibliothekstabelle aktualisiert sich im selben Morph mit, statt einen Vollreload zu
  brauchen.

Danach existiert in der gesamten Konsole genau ein Auto-Update-Weg.

## Fehlerverhalten

Jeder Fehlerfall lässt den angezeigten Zustand stehen, statt ihn zu leeren:
Netzwerkfehler und Nicht-200-Antworten überspringen den Takt und gehen ins Backoff;
eine Antwort ohne `<main>` stoppt den Poller; ein Redirect auf das Login führt zu
echter Navigation. Ein Morph-Fehler wird gefangen — dann wird als Fallback der
gesamte `<main>`-Inhalt per `replaceChildren` getauscht, was schlimmstenfalls das
heutige Verhalten reproduziert.

## Verifikation

Ein Python-Test ist nicht möglich (der Code ist JS), deshalb real im
Headless-Chromium über CDP gegen eine Testinstanz mit kopierter `store.db` — der in
diesem Projekt etablierte Weg. Geprüft wird an einem laufenden Media-Job:

1. Scrollposition bleibt über mehrere Takte stehen.
2. `<video>` läuft ununterbrochen weiter, `model-viewer` behält seine Kamerapose.
3. Die Filtereingabe behält Text, Fokus und Cursorposition, während sich Zeilen
   darunter aktualisieren.
4. Die angeklickte Sortierung bleibt erhalten.
5. Beim Wechsel des Jobstatus auf `done` verschwindet `data-live` und der Poller
   stoppt (kein weiterer Netzwerk-Request).
6. Ein neuer Job erscheint in der Liste, ohne dass die Seite springt.

Dazu `venv/bin/python -m py_compile admin.py` als Gate vor jedem Deploy.
