# Sampling-Defaults Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Sampling-Parameter (temperature, min_p, …) pro Backend und pro Chat-Alias hinterlegen, die nur greifen, wenn der Client den jeweiligen Key nicht selbst sendet.

**Architecture:** Zwei Stufen, deren Reihenfolge die Präzedenz Client > Alias > Backend erzeugt — beide setzen einen Key nur, wenn er fehlt. Die Alias-Stufe läuft in `main.route()` bzw. im Responses-Endpunkt über die gemeinsame Hilfsfunktion `_apply_alias_sampling()`; die Backend-Stufe läuft in `adapters.OpenAIAdapter._prepare()`, wo sie — wie `apply_reasoning` — pro Backend auf einer Body-Kopie arbeitet und beim Failover neu abgeleitet wird.

**Tech Stack:** Python 3, FastAPI/Starlette, SQLite (stdlib), keine neuen Dependencies.

## Global Constraints

- **Kein Testframework im Repo.** Verifikation = `venv/bin/python -m py_compile` plus Live-Checks gegen den laufenden Server. Niemals pytest o. Ä. hinzufügen.
- `store.py` bleibt dependency-frei und hot-reload-sicher (keine Config-Werte beim Import cachen).
- `adapters.py` darf `main` nicht importieren — alles kommt über das Backend-Dict bzw. `AdapterContext`.
- Präzedenz ist **Client > Alias > Backend**, ausnahmslos.
- Geltungsbereich: nur `/v1/chat/completions`, `/v1/completions`, `/v1/responses`. Nicht `/v1/embeddings`, nicht `/v1/audio/*`, nicht ComfyUI.
- Gesperrte Keys in beiden Editoren: `model`, `messages`, `stream`, `stream_options` und alles mit `_`-Präfix.
- Deutsche Kommunikation mit dem Nutzer; Code-Kommentare auf Englisch (wie im Rest des Repos).

---

### Task 1: Store-Ebene für Alias-Sampling

**Files:**
- Modify: `store.py` (nach `set_alias_reasoning`, ~Zeile 451)

**Interfaces:**
- Produces: `get_alias_sampling() -> dict`, `set_alias_sampling(alias: str, params) -> None`

- [ ] **Step 1: Funktionen einfügen**

Direkt nach `set_alias_reasoning` (vor dem `# ── Per-key routing mode` Block):

```python
# ── Per-alias sampling defaults ──────────────────────────────────────────────────
# One settings dict {alias: {param: value}} filled into chat bodies when the CLIENT
# omits the key (client always wins). Backends carry their own `sampling_defaults`,
# which apply after these — precedence is client > alias > backend.

def get_alias_sampling() -> dict:
    return get_setting("alias_sampling") or {}


def set_alias_sampling(alias: str, params) -> None:
    """Set/clear an alias's sampling defaults. Empty/None/non-dict clears."""
    m = get_alias_sampling()
    d = dict(params) if isinstance(params, dict) else {}
    if d:
        m[alias] = d
    elif alias in m:
        del m[alias]
    else:
        return
    set_settings({"alias_sampling": m})
```

- [ ] **Step 2: Compile-Gate**

Run: `venv/bin/python -m py_compile store.py`
Expected: keine Ausgabe (Erfolg)

- [ ] **Step 3: Verhalten prüfen**

Run:
```bash
venv/bin/python -c "
import store; store.init('store.db')
store.set_alias_sampling('_probe', {'temperature': 0.5})
print(store.get_alias_sampling().get('_probe'))
store.set_alias_sampling('_probe', {})
print(store.get_alias_sampling().get('_probe'))"
```
Expected: `{'temperature': 0.5}` dann `None`

- [ ] **Step 4: Commit**

```bash
git add store.py && git commit -m "store: per-alias sampling defaults"
```

---

### Task 2: Alias-Stufe im Request-Pfad

**Files:**
- Modify: `main.py` — Cache-Global (~Zeile 331), `rebuild_virtual_models()` (~183), neue Hilfsfunktion vor `route()` (~1368), `route()` (~1392), Responses-Endpunkt (~1613)

**Interfaces:**
- Consumes: `store.get_alias_sampling()` aus Task 1
- Produces: `alias_sampling: dict` (Modul-Global), `_apply_alias_sampling(alias: str, body: dict) -> None` (mutiert `body` in-place)

- [ ] **Step 1: Cache-Global anlegen**

Nach `alias_voice: dict = {}` (~Zeile 331):

```python
alias_sampling: dict = {}               # alias → {param: value} sampling defaults (store). Client wins.
```

- [ ] **Step 2: Cache befüllen**

In `rebuild_virtual_models()`: `alias_sampling` in die `global`-Zeile aufnehmen und nach der `alias_voice`-Zeile ergänzen:

```python
    alias_sampling = store.get_alias_sampling() if store.is_active() else {}
```

- [ ] **Step 3: Hilfsfunktion einfügen**

Direkt vor `async def route(...)`:

```python
def _apply_alias_sampling(alias: str, body: dict) -> None:
    """Fill this alias's sampling defaults into a chat body — only keys the CLIENT
    did not send (client always wins). Backend-level defaults apply later, in the
    adapter, so precedence ends up client > alias > backend. A malformed store
    entry is ignored rather than failing the request."""
    d = alias_sampling.get(alias)
    if not isinstance(d, dict):
        if d is not None:
            logger.warning(f"alias '{alias}': sampling defaults are not a dict — ignored")
        return
    for k, v in d.items():
        if k not in body:
            body[k] = v
```

- [ ] **Step 4: In `route()` aufrufen**

In `route()` direkt vor `r = _normalize_reasoning(body)` einfügen:

```python
    if not path.startswith("/v1/audio/") and not path.startswith("/v1/embeddings"):
        _apply_alias_sampling(alias, body)      # per-alias defaults; client fields win
```

- [ ] **Step 5: Im Responses-Endpunkt aufrufen**

Im Responses-Handler direkt nach `await gate_request(...)` und vor `r = _normalize_reasoning(raw_body)`:

```python
    _apply_alias_sampling(alias, chat_body)     # same defaults as the chat path
```

- [ ] **Step 6: Compile-Gate**

Run: `venv/bin/python -m py_compile main.py`
Expected: keine Ausgabe

- [ ] **Step 7: Commit**

```bash
git add main.py && git commit -m "gen: alias-level sampling defaults on chat + responses"
```

---

### Task 3: Backend-Stufe im Adapter

**Files:**
- Modify: `adapters.py` — `OpenAIAdapter._prepare()` (~Zeile 509)

**Interfaces:**
- Consumes: `self.backend["sampling_defaults"]` (Dict aus Store/config)
- Produces: keine neuen Symbole — `fwd` trägt die Defaults

- [ ] **Step 1: Defaults nach dem `fwd`-Bau anwenden**

In `_prepare()` zwischen `fwd = {...}` und `fwd, reasoning_ctl = ctx.apply_reasoning(...)`:

```python
        # Backend sampling defaults: fill keys the request does NOT carry (the
        # client's — and any alias default main.py already folded in — always win).
        # Sits here so it is derived PER BACKEND: a failover re-derives it from the
        # backend actually serving the call. Text endpoints only.
        sd = b.get("sampling_defaults")
        if sd and req.path not in ("/v1/embeddings",) and not req.path.startswith("/v1/audio/"):
            if isinstance(sd, dict):
                for k, v in sd.items():
                    if k not in fwd:
                        fwd[k] = v
            else:
                logger.warning(f"backend '{self.name}': sampling_defaults is not a dict — ignored")
```

- [ ] **Step 2: Compile-Gate**

Run: `venv/bin/python -m py_compile adapters.py`
Expected: keine Ausgabe

- [ ] **Step 3: Commit**

```bash
git add adapters.py && git commit -m "adapters: per-backend sampling defaults, failover-safe"
```

---

### Task 4: Backend-Editor im /ui

**Files:**
- Modify: `admin.py` — gemeinsamer Parser (vor `backend_save`, ~Zeile 1084), Formularfeld (~Zeile 852), `backend_save()` (~Zeile 1127)

**Interfaces:**
- Produces: `_parse_sampling(raw: str) -> tuple[dict, str]` — `(werte, fehlermeldung)`; bei Fehler ist `werte` leer und die Meldung nicht leer

- [ ] **Step 1: Parser + Sperrliste einfügen**

Vor `async def backend_save(request: Request):`:

```python
# Keys a sampling default must never set: they drive routing, streaming, the
# reasoning hand-off and the stats body — a default here would corrupt dispatch.
_SAMPLING_BLOCKED = ("model", "messages", "stream", "stream_options")


def _parse_sampling(raw: str) -> tuple:
    """Parse a sampling-defaults JSON object from a form field.
    Returns (values, error) — error non-empty means reject the save."""
    s = (raw or "").strip()
    if not s:
        return {}, ""
    try:
        d = json.loads(s)
    except Exception as e:
        return {}, f"sampling defaults: invalid JSON ({e})"
    if not isinstance(d, dict):
        return {}, "sampling defaults: must be a JSON object, e.g. {\"temperature\": 0.85}"
    bad = [k for k in d if k in _SAMPLING_BLOCKED or k.startswith("_")]
    if bad:
        return {}, f"sampling defaults: these keys are not allowed: {', '.join(sorted(bad))}"
    return d, ""
```

- [ ] **Step 2: Formularfeld ergänzen**

Im Backend-Editor innerhalb des `llmopts`-Blocks (nach dem `route_speed`-Hint, direkt vor `"</div></form>"`):

```python
            + _field("sampling defaults",
                     _textarea("sampling_defaults",
                               json.dumps(g("sampling_defaults"), ensure_ascii=False)
                               if g("sampling_defaults") else "", 2,
                               '{"temperature": 0.85, "min_p": 0.05}'))
            + "<p class='hint'><b>sampling defaults</b>: JSON object filled into every chat request "
              "to this backend for keys the caller did <b>not</b> send (an explicit client value, and "
              "an alias default, always win). For backends whose server samples with bare defaults — "
              "vLLM without a truncation sampler (top_p=1, min_p=0) degenerates into token salad at "
              "temperature ≈ 1. Re-derived per backend, so a failover uses the new backend's values. "
              "Chat/completions/responses only.</p>"
```

- [ ] **Step 3: Speichern verdrahten**

In `backend_save()` nach der `for nkey in (...)`-Schleife:

```python
    sd, sd_err = _parse_sampling(f.get("sampling_defaults", ""))
    if sd_err:
        return HTMLResponse(_page("Backends", f'<p class="bad">{_esc(sd_err)}</p>'
            f'<div class="actions">{_btn("← Back", "/ui/backends", "secondary")}</div>', "backends"))
    if sd:
        b["sampling_defaults"] = sd
    else:
        b.pop("sampling_defaults", None)
```

- [ ] **Step 4: Compile-Gate**

Run: `venv/bin/python -m py_compile admin.py`
Expected: keine Ausgabe

- [ ] **Step 5: Commit**

```bash
git add admin.py && git commit -m "ui: sampling defaults on the backend editor"
```

---

### Task 5: Chat-Alias-Editor im /ui

**Files:**
- Modify: `admin.py` — Editor-Formular (~Zeile 1544, nach `voice_field`), `chat_save()` (~Zeile 1612), Rename/Delete-Pfade (~Zeilen 1607, 1650)

**Interfaces:**
- Consumes: `_parse_sampling()` aus Task 4, `store.set_alias_sampling()` aus Task 1

- [ ] **Step 1: Feld im Alias-Editor**

Nach dem `voice_field`-Block:

```python
    cur_smp = store.get_alias_sampling().get(alias) or {}
    smp_field = (_field("sampling defaults",
                        _textarea("sampling", json.dumps(cur_smp, ensure_ascii=False) if cur_smp else "",
                                  2, '{"temperature": 0.85, "min_p": 0.05}'))
                 + "<p class='hint' style='margin:-4px 0 10px'>JSON object filled into requests on this "
                   "alias for keys the client did <b>not</b> send. Precedence: client &gt; alias &gt; "
                   "the serving backend's own <a href='/ui/backends'>sampling defaults</a>. Applies to "
                   "chat/completions/responses only.</p>")
```

Und in das `return`-Formular aufnehmen: `+ park_field + rsn_field + rmode_field + voice_field + smp_field`

- [ ] **Step 2: Speichern verdrahten**

In `chat_save()` — bei den anderen `f.get(...)`-Zeilen (neben `rsn`/`rmode`):

```python
    smp, smp_err = _parse_sampling(f.get("sampling", ""))
    if smp_err:
        return HTMLResponse(_page("Chat aliases", f'<p class="bad">{_esc(smp_err)}</p>'
            f'<div class="actions">{_btn("← Back", "/ui/mapping", "secondary")}</div>', "routing"))
```

Nach `store.set_alias_voice(...)`:

```python
    store.set_alias_sampling(alias, smp)                             # blank clears
```

- [ ] **Step 3: Rename und Delete mitziehen**

Beim Rename (neben `store.set_alias_reasoning(orig, None)`):

```python
        store.set_alias_sampling(orig, None)
```

Im Delete-Pfad (neben `store.set_alias_reasoning(alias, None)`):

```python
        store.set_alias_sampling(alias, None)
```

- [ ] **Step 4: Compile-Gate**

Run: `venv/bin/python -m py_compile admin.py`
Expected: keine Ausgabe

- [ ] **Step 5: Commit**

```bash
git add admin.py && git commit -m "ui: sampling defaults on the chat-alias editor"
```

---

### Task 6: Sichtbarkeit in /health und im Backends-Tab

**Files:**
- Modify: `main.py` — `gateway_info()` (~Zeile 3022) und die `/health`-Backend-Zeile (~Zeile 2968)

- [ ] **Step 1: Feld in beide Backend-Zeilen aufnehmen**

In `gateway_info()`s Backend-Dict (nach `"route_speed": ...`) und in der `/health`-Zeile (nach `"reqs_1h": ...`):

```python
            "sampling_defaults": b.get("sampling_defaults") or None,
```

- [ ] **Step 2: Compile-Gate + Sichtprüfung**

Run: `venv/bin/python -m py_compile main.py`
Expected: keine Ausgabe

- [ ] **Step 3: Commit**

```bash
git add main.py && git commit -m "gen: surface sampling_defaults in /health"
```

---

### Task 7: Dokumentation

**Files:**
- Modify: `README.md` (Backend-Optionen + Chat-Alias-Optionen), `CLAUDE.md` (Request-flow-Abschnitt)

- [ ] **Step 1: README ergänzen**

Bei den Backend-Optionen (neben `route_speed`/`self_retries`) und bei den Alias-Optionen (neben `alias_reasoning`) je ein Absatz: Zweck, JSON-Form, Präzedenz Client > Alias > Backend, Geltungsbereich (chat/completions/responses), gesperrte Keys.

- [ ] **Step 2: CLAUDE.md ergänzen**

Im Abschnitt „Request flow / Chat/LLM" einen Satz nach der `_normalize_reasoning`-Beschreibung:

> Sampling-Defaults werden zweistufig gefüllt — `_apply_alias_sampling()` in `route()`/Responses für den Alias, dann `sampling_defaults` des Backends in `adapters._prepare()` (pro Backend, failover-sicher). Beide setzen nur fehlende Keys: Client > Alias > Backend.

- [ ] **Step 3: Commit**

```bash
git add README.md CLAUDE.md && git commit -m "docs: sampling defaults"
```

---

### Task 8: Live-Verifikation auf .10

**Files:** keine — Verifikation gegen den laufenden Server

- [ ] **Step 1: Alles kompilieren**

Run: `venv/bin/python -m py_compile main.py adapters.py store.py admin.py jobs.py stats.py`
Expected: keine Ausgabe

- [ ] **Step 2: Server lokal starten und UI rendern**

Startet auf einem freien Port (nicht 4000 — dort läuft ggf. der echte Dienst) und prüft, dass Backend- und Alias-Editor rendern und ein Speichern durchläuft.

- [ ] **Step 3: Regressionsschutz — Backend ohne Feld**

Ein Request an ein Backend ohne `sampling_defaults`: geloggter Body im Tab „LLM Calls" muss unverändert sein (keine zusätzlichen Keys).

- [ ] **Step 4: Wirkung messen**

Auf .10 mit `sampling_defaults = {"temperature": 0.85, "min_p": 0.05}` am Backend `Infermatic`:
- nackter Request → Fremdzeichen-Anteil 0 % (Ausgangslage 1,6–3,5 %)
- Request mit `temperature: 1.5` → im geloggten Body steht 1.5, `min_p` 0.05 ergänzt
- Alias mit `{"temperature": 0.4}` → Body zeigt 0.4 plus `min_p` 0.05 des Backends

- [ ] **Step 5: Ergebnis berichten**

Messwerte und geloggte Bodies als Beleg zusammenfassen.
