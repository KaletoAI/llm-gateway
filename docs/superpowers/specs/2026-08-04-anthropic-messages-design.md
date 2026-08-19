# Anthropic Messages API im llm-gateway — Design

**Status: UMGESETZT (2026-08-18).** Phase 1 ist gebaut, getestet und dokumentiert
(README „Claude Code / Anthropic Messages"). Abschnitt 1 wurde unverändert
umgesetzt; die offenen Abschnitte 2–4 sind unten als **entschieden** ausgeführt —
mit den Abweichungen/Ergänzungen, die das Review gegen den echten Code ergeben hat.

Historie: Entwurf vom 2026-08-04, Brainstorming nach Abschnitt 1 von 4
unterbrochen.

## Ziel

Claude Code weiter benutzen, aber über das llm-gateway, im Mischbetrieb aus
Anthropic-Modellen (über die eigene Claude-Subscription) und Open-Weight-Modellen
über OpenRouter.

## Getroffene Entscheidungen

| # | Frage | Entscheidung | Begründung |
|---|---|---|---|
| 1 | Wie kommt das Subscription-Credential zu Anthropic? | **Gateway hält ein `setup-token`.** Einmal `claude setup-token` ausführen, Token liegt als Backend-Credential im Store (verschlüsselt wie User-Keys). Claude Code authentifiziert sich beim Gateway mit einem normalen Gateway-Key. | Saubere Trennung Gateway-Auth ≠ Anthropic-Auth. Quotas, Stats und Allow-Lists funktionieren weiter. Kein Header-Konflikt. |
| 2 | Welche Übersetzungsrichtungen? | **Phase 1: nur Messages als Frontdoor.** Die Gegenrichtung (Claude-Modelle über `/v1/chat/completions` und `/v1/responses`) ist bewusst vertagt, ggf. eigene Spec. | Kleinste Fläche, löst den Anwendungsfall vollständig. Die Gegenrichtung ist außerdem genau der Pfad, der die Subscription zur API machen würde (siehe Lizenz-Anforderung). |
| 3 | Wo lebt die Übersetzung? | **Ansatz A: im Adapter.** Siehe Abschnitt 1. | Routing/Parking/Failover/Stats/Quotas bleiben unangetastet; die Entscheidung „nativ oder übersetzt" fällt dort, wo das Protokollwissen sitzt; gemischte Aliase (Anthropic + OpenRouter) failovern automatisch. |
| 4 | Wie verifizieren? | **Eigene Test-Datei** `test_anthropic_bridge.py`, stdlib-only, per `venv/bin/python -m unittest` lauffähig. Fixtures aus echten aufgezeichneten Claude-Code-Requests: Tool-Use-Roundtrip, `tool_result`-Blöcke, System-Blöcke, komplette SSE-Event-Sequenz. | Das Repo hat bewusst keine Testsuite, aber eine Streaming-Bridge mit Tool-Calls tut still das Falsche statt zu crashen. Keine neue Dependency, kein Build-Step. |
| 5 | Lizenz-/ToS-Grenze | **Sichtbar UND technisch durchgesetzt.** Siehe Abschnitt 3. | Das Repo ist öffentlich. Der Nutzer will nicht, dass das Projekt anderen hilft, Anthropics Subscription-Lizenz zu umgehen. |

### Verworfen

- **Ansatz B (Übersetzung im Endpoint):** bricht den Failover. `resolve_routes()`
  läuft *innerhalb* von `_dispatch_or_park` (`main.py:1271`); welcher Backend-Typ
  tatsächlich bedient, steht vorher nicht fest. Man müsste die Routen-Auflösung
  duplizieren oder gemischte Aliase verbieten.
- **Ansatz C (Chat als internes Kanonformat):** doppelte Übersetzung auf dem
  Anthropic-Pfad. `cache_control` überlebt das nicht — und ohne Cache-Breakpoints
  liest Claude Code bei jedem Turn den vollen Kontext zum vollen Preis neu ein.
  Harte Nutzer-Anforderung: *„so bauen, dass es funktioniert wie nativ. Es darf
  nicht teuer in der Nutzung werden!"* Ebenfalls verloren gingen
  Thinking-Signaturen und feingranulares Tool-Streaming.

## Befunde aus der Code-Erkundung

- **Frontdoor fehlt komplett.** Claude Code spricht `POST /v1/messages`,
  `POST /v1/messages/count_tokens`, Header `x-api-key` / `anthropic-version` /
  `anthropic-beta`. Das Gateway hat nur OpenAI-Endpoints (`main.py:1409`–`2809`);
  `authenticate()` (`main.py:591`) liest ausschließlich `Authorization: Bearer`.
- **Vorbild für die Bridge existiert.** `responses_bridge.py` ist strukturell
  exakt das Gesuchte: reine Funktionen, kein `main`-Import, Request/Response/
  SSE-Übersetzung.
- **Backend-Seam passt.** `BackendAdapter` (ABC, `adapters.py:272`) +
  `make_adapter()` (`adapters.py:2345`) erlauben `type: anthropic` sauber neben
  `openai`/`comfyui`.
- **OpenRouter ist bereits ein normales `openai`-Backend**
  (`config.example.yaml:103`).
- **Header-Passthrough-Muster existiert schon** in `OpenAIAdapter._prepare()`
  (`adapters.py:501`–`506`): alle Client-Header außer `host`,
  `content-length`, `authorization`, `x-park-mode` werden übernommen.
- **Offizieller Subscription-Weg bestätigt:** die lokal installierte CLI hat
  `claude setup-token` — *„Set up a long-lived authentication token (requires
  Claude subscription)"*.

## Abschnitt 1 — Architektur & Komponenten (vorgelegt, nicht freigegeben)

Ein neues Modul, ein neuer Adapter, zwei neue Endpoints. Nichts am bestehenden
Routing.

### `anthropic_bridge.py` (neu)

Reine Funktionen, keine `main`/`adapters`-Imports (hot-reload-sicher, liegt auf
dem Request-Pfad).

| Funktion | Aufgabe |
|---|---|
| `messages_to_chat(body)` | Messages-Request → OpenAI-Chat (System-Blöcke, `tool_use`/`tool_result` → `tool_calls`/`role:tool`, Bild-Blöcke) |
| `chat_to_messages(chat, model)` | Chat-Antwort → Messages-Objekt inkl. `stop_reason`/`usage` |
| `messages_stream(chat_sse, model)` | Chat-SSE → Anthropic-SSE (`message_start` … `input_json_delta` … `message_stop`) |
| `estimate_input_tokens(body)` | `count_tokens` für Backends ohne nativen Endpoint |
| `message_shell(...)` | die eine Objekt-Skelett-Funktion, analog `response_shell()` |

### `adapters.py`

Neue Klasse `AnthropicAdapter` (`type: anthropic`).

- `discover()` über Anthropics `GET /v1/models` (liefert `{"data":[…]}`, passt
  ohne Änderung in `extract_models`), mit Rückfall auf eine konfigurierte
  Modell-Liste, falls das Subscription-Token diesen Endpoint nicht bedienen darf.
- `dispatch()` ist **echter Passthrough**: Claude Codes Header werden wie in
  `_prepare()` übernommen — nur `authorization`/`x-api-key`/`host`/
  `content-length` fallen raus, das Subscription-Token kommt rein. Body und
  SSE-Strom bleiben unangetastet.

Drei Dinge dürfen auf diesem Pfad **nicht** laufen, sonst ist es nicht mehr nativ:

1. `_StreamNormalizer` (OpenAI-SSE-spezifisch),
2. `apply_reasoning()` (Claude Code setzt `thinking` selbst),
3. jede Body-Umschrift.

Damit überleben `cache_control`, Thinking-Signaturen und feingranulares
Tool-Streaming unverändert.

Derselbe `OpenAIAdapter` erkennt `req.path == "/v1/messages"` und ruft für Rein-
und Rückweg die Bridge auf. Dadurch kann ein Alias Anthropic **und** OpenRouter
als Kandidaten führen; fällt Anthropic aus, greift der bestehende Failover, ohne
dass der Router von Protokollen wissen muss.

### `main.py`

`POST /v1/messages` und `POST /v1/messages/count_tokens`. Beide gehen durch das
unveränderte `_dispatch_or_park()`, erben also Parking, Failover, Stats und
Quotas. Claude Code authentifiziert sich mit `x-api-key` — umgesetzt als
`_client_credential()`, das beide Header-Formen auf die Bearer-Form normalisiert;
`authenticate()` selbst blieb unangetastet.

## Review-Befunde (2026-08-18, vor der Umsetzung)

Drei Dinge, die der Entwurf nicht abdeckte und die die Umsetzung mit erledigt hat:

1. **Die Routen-Auflösung war protokollblind.** `resolve_routes()` kannte den Pfad
   nicht — ein Anthropic-Backend wäre auch Kandidat für `/v1/chat/completions`
   gewesen und hätte dort einen Chat-Body roh an api.anthropic.com geschickt. Die
   Lizenz-Sperre aus Abschnitt 3 ist damit **auch technisch notwendig**, nicht nur
   eine ToS-Maßnahme. Umgesetzt als `serves_path(backend, path)`, ausgewertet in
   `resolve_routes()` (also auch beim Parking-Rewake).
2. **Header-Leak.** `_prepare()` reichte alle Client-Header außer
   `host`/`content-length`/`authorization` weiter. Sobald Claude Code sich mit
   `x-api-key` anmeldet, wäre der **Gateway-Key** an OpenRouter bzw. Anthropic
   gegangen. `_HOP_BY_HOP` filtert ihn jetzt mit.
3. **Token-Buchhaltung.** Anthropic meldet `usage.input_tokens/output_tokens`, der
   Stats-Pfad las `prompt_tokens/completion_tokens` — ohne Mapping wäre jeder
   Subscription-Call mit 0 Tokens in Statistik und Kosten gelandet
   (`_usage_of()`-Hook, Cache-Reads zählen mit).

## Abschnitt 2 — Datenfluss (entschieden)

```
Claude Code  → POST /v1/messages  (x-api-key: <Gateway-Key>)
             → gate_request()  → alias = body["model"]
             → _dispatch_or_park(alias, "/v1/messages", body, request)   # unverändert
             → resolve_routes() → Kandidat
                ├── AnthropicAdapter  → roher Passthrough an api.anthropic.com
                └── OpenAIAdapter     → messages_to_chat → OpenRouter → chat_to_messages
```

**Modellwahl:** Claude Code schickt seinen Modellnamen im Body — also ist jeder
Gateway-Alias direkt nutzbar: `ANTHROPIC_MODEL=<alias>`, `/model <alias>` zur
Laufzeit. Kein Gateway-Code nötig. Das **Hintergrundmodell** (Titel/Zusammen-
fassungen) wird über `ANTHROPIC_SMALL_FAST_MODEL=<alias>` auf ein billiges lokales
oder OpenRouter-Modell gelegt — Konfiguration, kein Code; im README dokumentiert.

**Auth:** Claude Code sendet `x-api-key`; `ANTHROPIC_AUTH_TOKEN` sendet stattdessen
`Authorization: Bearer`. Beide tragen einen **Gateway**-Key. `_client_credential()`
normalisiert sie, `authenticate()` bleibt unverändert.

**Reasoning:** `thinking:{type:enabled}` → `body["_reasoning"]="on"`, greift nur auf
dem übersetzten Pfad (der Passthrough setzt `thinking` ja selbst).

**Kein Alias-Sampling** auf diesem Pfad: Claude Code schickt einen vollständigen,
bewussten Request; ein chat-förmiges `min_p` würde gegen Anthropic 400 auslösen.
Backend-`sampling_defaults` greifen weiterhin auf dem übersetzten Pfad (im Adapter,
also per Backend und failover-fest) und **nicht** im Passthrough — beides ist im
e2e-Test festgenagelt.

## Abschnitt 3 — Lizenz-/ToS-Durchsetzung (umgesetzt)

Nutzer-Anforderung, wörtlich: *„Das Projekt ist öffentlich und ich will nicht,
dass andere mit meiner Hilfe die Lizenzen umgehen!"*

Umgesetzt, technisch zuerst:

- `serves_path()`: ein `anthropic`-Backend ist **ausschließlich** auf
  `/v1/messages` routbar — unerreichbar über `/v1/chat/completions`,
  `/v1/responses`, `/v1/embeddings` und den Playground, egal welcher Alias darauf
  zeigt. Eine Tür, die zu ist, ist stärker als ein Hinweis, den man wegklickt.
- Ein Alias, den nur Anthropic-Backends bedienen, wird im Playground **ausgeblendet**
  (`_anthropic_only()`) und antwortet auf anderen Endpoints mit
  `404 … reachable through POST /v1/messages only` statt eines irreführenden 503.
- Warn-Text direkt am Credential-Feld im Backends-Tab, plus Absätze in `README.md`
  und `config.example.yaml`.
- Die Gegenrichtung (Claude-Modelle hinter `/v1/chat/completions`) bleibt dauerhaft
  ungebaut — genau sie würde die Subscription in eine API verwandeln.

## Abschnitt 4 — Fehlerbehandlung & Verifikation (umgesetzt)

**Übersetzungspolitik** (Leitlinie aus dem Entwurf, jetzt konkret): verwerfen, was
folgenlos ist — `cache_control`, `thinking`-Blöcke aus der History, server-seitige
Tools (`web_search` & Co. haben kein OpenAI-Äquivalent und würden den ganzen
Request 400en). **Fehler**, wo Verwerfen ein falsches Ergebnis erzeugt:
`document`/`search_result`-Blöcke → `UnsupportedContent` → HTTP 400 mit dem Hinweis,
das Modell auf ein Anthropic-Backend zu routen. Eine Antwort über ein PDF, das das
Modell nie gesehen hat, ist schlimmer als ein klarer Fehler.

**Fehlerform:** alle Gateway-Fehler auf diesem Pfad werden zu
`{"type":"error","error":{"type":…,"message":…}}` — Claude Code liest
`error.message`; ein FastAPI-`detail`-Body erscheint dort leer.

**Robustheit:** unparsbare Tool-Argumente (abgeschnittener Stream) ergeben ein
leeres `input` statt einer geplatzten Antwort; ein abbrechender Upstream-Stream
schließt trotzdem jeden offenen Content-Block und sendet `message_delta` +
`message_stop`, damit die Blockbuchführung des Clients nicht desynchronisiert.

**Verifikation** (beides bei jeder Änderung wiederholbar):

- `test_anthropic_bridge.py` — 37 stdlib-`unittest`-Fälle über beide Richtungen und
  die komplette SSE-Sequenz (`venv/bin/python -m unittest test_anthropic_bridge`).
- Ein e2e-Lauf gegen ein Fake-Upstream, das Anthropic **und** OpenAI spielt und
  zurückmeldet, was oben ankam: 37 Checks über Passthrough-Treue (`cache_control`
  überlebt, Backend-Token statt Gateway-Key, OAuth-Beta an die Client-Betas
  angehängt), übersetzten Pfad inkl. Tool-Roundtrip und Streaming, `count_tokens`
  beidseitig, die geschlossene Tür (und dass sie nur das Anthropic-Backend trifft)
  sowie beide Auth-Header. Skripte: `fake_upstream.py`, `e2e_messages.py`,
  `run_e2e.sh` (im Job-Verzeichnis der Sitzung, nicht im Repo — sie brauchen freie
  Ports und eine Wegwerf-DB).

## Noch offen (bewusst)

- **Live-Test gegen echtes `api.anthropic.com`** steht aus: dafür braucht es ein
  echtes `claude setup-token`. Die drei „zu verifizierenden Annahmen" unten sind
  daher weiterhin unverifiziert — die Umsetzung ist aber so gebaut, dass sie in
  jedem Fall trägt (Discovery fällt auf die konfigurierte Modell-Liste zurück, die
  Client-Header inkl. `anthropic-version`/`anthropic-beta` gehen durch).
- Streaming-Reconnect (`GET /v1/messages/{id}`) gibt es bei Anthropic nicht; nichts
  zu tun.

## Annahmen — am 2026-08-19 empirisch geklärt

Gegen `api.anthropic.com` mit einem echten Subscription-Token (Abo „max") gemessen:

1. **`GET /v1/models` WIRD bedient** — 200, 10 Modelle. Die ursprüngliche Sorge war
   unbegründet; die konfigurierte `models`-Liste bleibt reiner Fallback (und ist
   nicht nötig, damit ein Backend hochkommt).
2. **Es braucht gar keinen Zusatz-Header.** Der Bearer-Token allein genügt:
   `/v1/models` und `/v1/messages` antworten mit und ohne `anthropic-beta:
   oauth-2025-04-20` identisch. Der Adapter hängt den Beta-Wert weiterhin an die
   Client-Liste an (statt sie zu ersetzen) — schadet nicht, schützt vor künftigem
   Pflichtwerden.
3. **Der Claude-Code-Systemprompt WIRD verlangt — für die starken Modelle.**
   (Korrigiert: die erste Messung dazu lief versehentlich gegen einen ungültigen
   Modellnamen und war deshalb wertlos.) Sauber nachgemessen mit
   `claude-sonnet-5`:

   | Request | Ergebnis |
   |---|---|
   | nur Bearer | 429 `rate_limit_error` |
   | + `anthropic-beta: claude-code-20250219` | 429 |
   | + `user-agent: claude-cli/2.1.235` | 429 |
   | + `x-app: cli` | 429 |
   | + `system: "You are Claude Code, Anthropic's official CLI for Claude."` | **200** |

   Es hängt also am **Systemprompt**, nicht an Headern. `claude-haiku-4-5` ist
   ausgenommen (geht auch ohne). Der 429 ist irreführend: die Rate-Limit-Header
   melden bei Haiku zeitgleich 5h-Auslastung 0.1 — es ist kein Verbrauchslimit,
   sondern eine Zugriffsentscheidung.

   **Konsequenz fürs Gateway: nichts tun.** Der Systemprompt kommt vom echten
   Client und geht durch den Passthrough unverändert mit; ihn selbst einzusetzen
   hieße, beliebige Clients als Claude Code zu tarnen — genau die Grenze, die
   dieser Backend-Typ respektieren soll. Anthropic setzt sie damit teilweise auch
   upstream durch; die Sperre aus Abschnitt 3 bleibt trotzdem nötig (Haiku wäre
   sonst offen, und die Grenze darf nicht von Anthropics Verhalten abhängen).

Weitere Messungen derselben Sitzung:

- **Prompt-Caching greift erst ab einer Mindestgröße.** Bei
  `claude-haiku-4-5-20251001` blieb ein ~2 210-Token-Prompt mit `cache_control`
  vollständig ungecacht (write=0, read=0), ein ~6 000-Token-Prompt wurde gecacht
  (write=6002, danach read=6002). Wer die Cache-Spalten testet, muss groß genug
  bauen — 0 bedeutet dort nicht „kaputt".
- **Der Passthrough ist auch praktisch byte-identisch:** ein über das Gateway
  gesendeter Request TRAF den Cache-Eintrag, den ein direkter Request an Anthropic
  zuvor angelegt hatte. Ein abweichendes Präfix hätte den Cache verfehlt.
- **Der Token-Flow braucht keinen Browser auf der Maschine.** `claude setup-token`
  nutzt `redirect_uri=https://platform.claude.com/oauth/code/callback` mit
  `code=true` — die CLI zeigt eine URL, man meldet sich irgendwo an und fügt den
  angezeigten Code zurück in die CLI. Also headless-tauglich (Gateway-Container),
  kein SSH-Tunnel nötig. Gültigkeit: 1 Jahr.
- **Nicht verwenden:** `claudeAiOauth.accessToken` aus
  `~/.claude/.credentials.json` — läuft nach Stunden ab, und das Gateway kann ihn
  nicht per Refresh-Token erneuern.

## Nächster Schritt

Erledigt: Backend `claude` läuft auf .10 (UP, 10 Modelle), `/v1/messages` liefert
echte Antworten, `/v1/chat/completions` bleibt 404, und die Cache-Spalten füllen
sich mit echten Anthropic-Zahlen. Offen ist nur noch, den kurzlebigen
Access-Token dort durch einen `setup-token` (1 Jahr) zu ersetzen.
