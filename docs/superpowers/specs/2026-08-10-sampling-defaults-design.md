# Sampling-Defaults pro Backend und Alias

*Design, 2026-08-10*

## Problem

Call #12469 (Playground → `Infermatic/TheDrummer-Anubis-70B-v1.1-FP8-Dynamic`)
lieferte Token-Salat: eingestreute fremdsprachige und Code-Tokens mitten im
deutschen Text. Kein Encoding-Schaden und keine kaputte Quantisierung — die
Umlaute waren intakt, es waren echte Tokens aus dem Long Tail.

Ursache: Der Playground sendet `temperature`/`max_tokens` nur, wenn die Felder
ausgefüllt sind. Der geforwardete Body war exakt
`{"model": …, "messages": […], "stream": false}`. Infermatics vLLM (0.23.0)
sampelt dann mit nackten Defaults — temperature ≈ 1.0 **ohne jeden Truncation-
Sampler** (top_p=1, top_k=-1, min_p=0).

Messung, identischer Prompt, Anteil Fremdzeichen, je zwei Läufe:

| Parameter | Lauf 1 | Lauf 2 |
|---|---|---|
| keine (wie #12469) | 1,63 % | 2,45 % |
| `temperature=1.0` nackt | 2,71 % | 3,51 % |
| `temperature=0.85` | 0 % | 0 % |
| `temperature=1.0` + `top_p=0.9` | 0 % | 0 % |
| `temperature=1.0` + `min_p=0.05` | 0 % | 0 % |
| nur `top_p=0.9` | 0,05 % | 0,13 % |
| nur `min_p=0.05` | 0 % | 0 % |
| `temperature` 0.1 / 0.3 / 0.7 nackt | 0 % | 0 % |

Entscheidend ist der Truncation-Sampler, nicht die Temperatur allein: `min_p`
reicht bei Default-Temperatur, und unterhalb von ~0.7 kippt es ohnehin nicht.

Das Gateway reicht Bodies unverändert durch — es gibt heute keine Möglichkeit,
für ein Backend oder einen Alias Sampling-Werte zu hinterlegen, die greifen,
wenn der Client nichts schickt. `alias_reasoning`, `alias_voice` und
`alias_park` sind die etablierten Muster für genau diese Art von Default,
decken aber andere Felder ab.

## Ziel

Konfigurierbare Sampling-Defaults, die **nur** greifen, wenn der Client den
jeweiligen Key nicht selbst sendet. Zwei Ebenen, beide frei befüllbar.

**Präzedenz: Client > Alias > Backend.**

Nicht-Ziel: eine feste Parameter-Whitelist. Jeder Key ist erlaubt, bis auf die
wenigen, die die Gateway-Mechanik zerlegen würden (siehe *Validierung*).

## Konfiguration

| Ebene | Speicherort | UI |
|---|---|---|
| Backend | Backend-Feld `sampling_defaults` (Store-Tabelle `backends` und `config.yaml`, wie `self_retries`) | Textarea im Backend-Editor |
| Alias | Store-Settings-Key `alias_sampling`, gecacht in `main.alias_sampling` | Textarea im Chat-Alias-Editor |

Beispiel Backend `Infermatic`:

```json
{"temperature": 0.85, "min_p": 0.05}
```

Beispiel Alias `rp-kreativ`:

```json
{"temperature": 1.0}
```

Ergebnis für einen Client-Request ohne Sampling-Parameter auf diesem Alias:
`temperature` 1.0 (Alias) + `min_p` 0.05 (Backend).

Leeres Feld = Funktion aus. Der Alias-Cache wird in `rebuild_virtual_models()`
neben `alias_reasoning`/`alias_voice` gefüllt, ist also hot-reload-fähig.

## Anwendung

Die Präzedenz ergibt sich allein aus der Reihenfolge — kein Marker, keine
Sonderlogik. Beide Stufen setzen einen Key nur, wenn er noch fehlt:

1. **Alias-Stufe** — `main.route()`, nach Auth und vor `_normalize_reasoning()`.
   Setzt alle Keys aus `alias_sampling[alias]`, die der Client nicht geschickt
   hat. Ab hier ist ein Alias-Wert vom Client-Wert ununterscheidbar; genau das
   ist gewollt, weil beide über der Backend-Stufe stehen.
2. **Backend-Stufe** — `adapters.OpenAIAdapter._prepare()`, direkt nach dem
   `fwd`-Bau (heute Zeile 509). Setzt alle Keys aus
   `backend["sampling_defaults"]`, die dann immer noch fehlen.

Die Backend-Stufe sitzt bewusst in `_prepare()`: dort läuft sie — wie
`apply_reasoning` — pro Backend auf einer Kopie des Bodys und wird beim
**Failover neu abgeleitet**. Ein Failover von Backend A nach B nimmt also Bs
Defaults, nicht As.

`/v1/responses` baut seinen Chat-Body an `route()` vorbei und ruft
`_dispatch_or_park()` direkt. Beide Aufrufer teilen sich deshalb eine
Hilfsfunktion `_apply_alias_sampling(alias, body)`.

### Geltungsbereich

Nur Text-Endpunkte: `/v1/chat/completions`, `/v1/completions`, `/v1/responses`.

Ausgenommen: `/v1/embeddings` (kennt keine Sampling-Parameter) und
`/v1/audio/*` (binärer TTS-Passthrough; `route()` behandelt den Pfad ohnehin
gesondert). ComfyUI-Backends sind nicht betroffen — die laufen über
`generate()`, nicht über `dispatch()`.

## Validierung

Beim Speichern im UI abgelehnt, mit Fehlermeldung statt stillem Verwerfen:

- ungültiges JSON
- Top-Level ist kein Objekt
- die Keys `model`, `messages`, `stream`, `stream_options` sowie alles mit
  `_`-Präfix — die würden Routing, Streaming, Reasoning-Übergabe oder die
  Stats-Erfassung zerlegen

Werte selbst werden nicht typgeprüft: Listen (`stop`) und Objekte
(`logit_bias`) sind legitim, und das Backend meldet Unsinn ohnehin
verständlich zurück.

Zur Laufzeit defensiv: Ist ein gespeicherter Eintrag kein Dict (manuell
editierter Store), wird er ignoriert und einmal geloggt — nie ein
Request-Fehler.

## Nachvollziehbarkeit

Keine neue UI nötig. `stats.record_call` protokolliert bereits `call.req_text`,
und das ist der **geforwardete** Body — im Tab „LLM Calls" ist also direkt
sichtbar, welche Werte tatsächlich rausgingen. Genau darüber wurde die Ursache
dieses Bugs auch gefunden.

Zusätzlich wandert `sampling_defaults` in die `/health`-Backend-Zeile, damit
der Backends-Tab es anzeigt (wie `self_retries`).

## Fehlerfälle

| Fall | Verhalten |
|---|---|
| Feld leer / Key fehlt | No-op |
| Store-Eintrag kein Dict | ignorieren, einmal loggen, Request läuft normal |
| Client sendet den Key selbst | Client gewinnt, auf beiden Ebenen |
| Failover auf anderes Backend | Defaults des neuen Backends, neu abgeleitet |
| Alias umbenannt | `alias_sampling`-Eintrag wandert mit (wie `alias_reasoning`) |
| Alias gelöscht | Eintrag wird mitgelöscht |

## Verifikation

Das Repo hat kein Testframework; verifiziert wird per Compile-Gate und live.

1. `venv/bin/python -m py_compile main.py adapters.py store.py admin.py`
2. Server starten, `/ui` Backends- und Chat-Alias-Editor rendern und speichern
3. Live gegen Infermatic auf .10, jeweils der geloggte Body als Beleg:
   - Backend-Default `{"temperature": 0.85, "min_p": 0.05}`, nackter Request
     → 0 % Fremdzeichen (Ausgangslage: 1,6–3,5 %)
   - Request mit `temperature: 1.5` → Client-Wert bleibt stehen
   - Alias mit `{"temperature": 0.4}` → Alias schlägt Backend, `min_p` des
     Backends wird ergänzt
   - Backend ohne Feld → Body unverändert (Regressionsschutz)

## Betroffene Dateien

| Datei | Änderung |
|---|---|
| `store.py` | `get_alias_sampling` / `set_alias_sampling` (analog `alias_reasoning`) |
| `main.py` | Cache `alias_sampling`, Füllung in `rebuild_virtual_models()`, `_apply_alias_sampling()`, Aufruf in `route()` und im Responses-Pfad, `/health`-Feld |
| `adapters.py` | Backend-Stufe in `_prepare()` |
| `admin.py` | Textarea + Validierung im Backend-Editor und im Chat-Alias-Editor, Löschen/Umbenennen mitziehen |
| `README.md`, `CLAUDE.md` | Dokumentation des neuen Knopfs |

Umfang: rund 90 Zeilen.

## Verworfene Alternativen

**Nur Alias-Defaults** (das ursprünglich vorgeschlagene Muster analog
`alias_reasoning`): hätte Call #12469 nicht verhindert. Dort war `model` =
`Infermatic/TheDrummer-Anubis-70B-v1.1-FP8-Dynamic`, also Backend-Pin plus
nackte Modell-ID — überhaupt kein Alias im Spiel.

**Nur Backend-Defaults**: deckt die Ursache ab, erlaubt aber kein Tuning pro
Anwendungsfall, wenn ein Backend mehrere sehr unterschiedliche Aliase bedient.

**Feste Parameter-Whitelist**: würde bei jedem neuen Sampler des Backends
nachgezogen werden müssen. Die Sperrliste der vier gefährlichen Keys ist der
kleinere und stabilere Eingriff.
