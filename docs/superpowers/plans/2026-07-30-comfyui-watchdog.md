# ComfyUI-Watchdog Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Der Gateway erkennt einen hängenden ComfyUI-Executor (Queue voll, nichts läuft) innerhalb weniger Health-Zyklen, markiert das Backend unhealthy und kann den Dienst per ComfyUI-Manager-Reboot neu starten (UI-Button + opt-in Auto-Restart mit Cooldown).

**Architecture:** Die Stuck-Erkennung lebt komplett im `ComfyUIAdapter.discover()` (wirft `ComfyExecutorStuck` → der bestehende DOWN-Pfad in `main.refresh_backend()` greift unverändert). Restart ist eine neue Adapter-Methode (`restart()`), die `main.py` als UI-Hook (`restart_comfy_backend`) und als Auto-Restart-Hook im Discovery-Except anbietet. Sichtbarkeit über drei Zusatzfelder in `/health` und dem UI-Snapshot.

**Tech Stack:** Bestand (FastAPI/httpx/uvicorn); keine neuen Dependencies. Verifikation über einen Mock-ComfyUI (FastAPI-Script im Scratchpad) + lokale Gateway-Instanz; `py_compile`-Gate.

**Spec:** `docs/superpowers/specs/2026-07-30-comfyui-watchdog-design.md`

## Global Constraints

- Kein Test-Framework im Repo: Verifikation = `venv/bin/python -m py_compile main.py adapters.py admin.py` + Mock-Server-Durchlauf (Task 6).
- `adapters.py` importiert nie `main` (Injection via `AdapterContext`); `admin.py` erhält Callables nur über `admin.bind(...)`.
- Kommentar-/Code-Stil der Umgebung matchen (knappe, Constraint-erklärende Kommentare).
- Defaults exakt: `stuck_after_s` 90, `restart_cooldown_s` 600, Restart-Wiederkehr-Wartezeit 120 s, `auto_restart` aus.
- Abweichung von der Spec (freigegeben nachzutragen, Task 3): Auto-Restart wird NICHT durch `backend_inflight > 0` blockiert — stuck impliziert „nichts läuft“, ein pending Gateway-Job ist ohnehin verloren; die Spec-Zeile wird im selben Commit angepasst.
- Deploy auf Prod ist NICHT Teil dieses Plans (erst nach Rücksprache; Prod-GPU ist ohnehin bis zum pveK12-Reboot down).

---

### Task 1: Stuck-Detection im ComfyUIAdapter

**Files:**
- Modify: `adapters.py` (Konstanten bei `_COMFY_DISCOVERY_TIMEOUT` ~Z. 45; `ComfyUIAdapter.__init__` ~Z. 1481; `discover()` ~Z. 1485)

**Interfaces:**
- Produces: `class ComfyExecutorStuck(Exception)` (Modul-Level, von `main.py` importierbar); Adapter-Attribute `exec_stuck: bool`, `last_restart: float`, `last_restart_result: str` (Task 2/4 lesen sie); `discover()` wirft `ComfyExecutorStuck` bei Hänger.

- [ ] **Step 1: Exception + Konstante ergänzen** — direkt unter `_COMFY_DISCOVERY_TIMEOUT = 8.0`:

```python
_COMFY_STUCK_AFTER_S = 90.0        # default: pending-with-idle-executor this long → stuck
_COMFY_RESTART_WAIT_S = 120.0      # restart(): max wait for the server to come back


class ComfyExecutorStuck(Exception):
    """ComfyUI answers HTTP but its prompt executor is not draining the queue
    (queue_pending non-empty, queue_running empty, same head across checks)."""
```

- [ ] **Step 2: Zustand im `__init__`** — in `ComfyUIAdapter.__init__` nach `self._node_types = {}`:

```python
        # executor watchdog (discover-driven): pending head seen while nothing ran
        self._stuck_head: Optional[str] = None
        self._stuck_since: float = 0.0
        self._stuck_checks: int = 0
        self.exec_stuck: bool = False
        self.last_restart: float = 0.0        # ts of the last restart() call (cooldown)
        self.last_restart_result: str = ""    # "" | running | ok | timeout | no-manager
```

- [ ] **Step 3: `discover()` erweitern + `_check_executor()`** — `discover()` ersetzen durch:

```python
    async def discover(self, client: httpx.AsyncClient) -> Capabilities:
        url = self.backend["url"].rstrip("/")
        resp = await client.get(f"{url}/object_info", timeout=_COMFY_DISCOVERY_TIMEOUT)
        resp.raise_for_status()
        oi = resp.json()
        self._node_types = _comfy_node_types(oi)      # cache slot types for bypass (free — same fetch)
        caps = Capabilities(models=_comfy_models(oi), loras=_comfy_loras(oi), pricing={})
        qr = await client.get(f"{url}/queue", timeout=_COMFY_DISCOVERY_TIMEOUT)
        qr.raise_for_status()
        self._check_executor(qr.json())               # raises ComfyExecutorStuck → DOWN path
        return caps

    def _check_executor(self, queue: dict) -> None:
        """Watchdog: prompts waiting while nothing runs, same head prompt across
        ≥2 consecutive checks AND ≥stuck_after_s → ComfyExecutorStuck. A transient
        idle moment between dequeues changes the head / empties pending → reset."""
        pending = queue.get("queue_pending") or []
        running = queue.get("queue_running") or []
        head = None
        if pending and not running:
            entry = pending[0]
            head = entry[1] if isinstance(entry, (list, tuple)) and len(entry) > 1 else str(entry)
        if head is None or head != self._stuck_head:
            self._stuck_head = head               # None (healthy) or new tracking baseline
            self._stuck_since = time.time()
            self._stuck_checks = 0
            self.exec_stuck = False
            return
        self._stuck_checks += 1                   # same head again, still nothing running
        after = float(self.backend.get("stuck_after_s") or _COMFY_STUCK_AFTER_S)
        if time.time() - self._stuck_since >= after:
            self.exec_stuck = True
            raise ComfyExecutorStuck(
                f"executor stuck: {len(pending)} prompt(s) pending, none running for "
                f"{int(time.time() - self._stuck_since)}s (head {head})")
```

- [ ] **Step 4: Compile-Gate**

Run: `cd /home/dev/projekte/llm-gateway && venv/bin/python -m py_compile adapters.py && echo OK`
Expected: OK

- [ ] **Step 5: Commit**

```bash
git add adapters.py
git commit -m "adapters: ComfyUI executor watchdog — /queue stuck detection in discover()"
```

### Task 2: `restart()` im ComfyUIAdapter (ComfyUI-Manager-Reboot)

**Files:**
- Modify: `adapters.py` (`ComfyUIAdapter`, direkt nach `discover()`/`_check_executor()`)

**Interfaces:**
- Consumes: `_pooled_client(self.ctx)` (bestehend), Watchdog-Attribute aus Task 1.
- Produces: `async def restart(self) -> str` — Rückgabe `"ok"` | `"timeout"`; wirft `RuntimeError` wenn `/manager/reboot` 404 liefert (Manager fehlt). Setzt `last_restart`/`last_restart_result`.

- [ ] **Step 1: Methode einfügen**

```python
    async def restart(self) -> str:
        """Restart the ComfyUI service via the ComfyUI-Manager reboot endpoint.
        The process kills itself mid-response, so a transport error on the POST is
        the EXPECTED success signal; systemd (Restart=always) brings it back. A 404
        means the Manager extension is missing → RuntimeError, no wait loop."""
        url = self.backend["url"].rstrip("/")
        self.last_restart = time.time()
        self.last_restart_result = "running"
        try:
            async with _pooled_client(self.ctx) as client:
                r = await client.post(f"{url}/manager/reboot", timeout=5.0)
            if r.status_code == 404:
                self.last_restart_result = "no-manager"
                raise RuntimeError("ComfyUI-Manager not installed (/manager/reboot → 404)")
        except (httpx.TransportError, httpx.TimeoutException):
            pass                                   # process died mid-response — expected
        deadline = time.time() + _COMFY_RESTART_WAIT_S
        while time.time() < deadline:
            await asyncio.sleep(3.0)
            try:
                async with _pooled_client(self.ctx) as client:
                    r = await client.get(f"{url}/object_info", timeout=_COMFY_DISCOVERY_TIMEOUT)
                if r.status_code == 200:
                    self._stuck_head, self._stuck_checks = None, 0
                    self.exec_stuck = False
                    self.last_restart_result = "ok"
                    logger.info(f"[{self.name}] ComfyUI back up after restart")
                    return "ok"
            except Exception:
                pass                               # still rebooting
        self.last_restart_result = "timeout"
        logger.warning(f"[{self.name}] ComfyUI not back {_COMFY_RESTART_WAIT_S:.0f}s after restart")
        return "timeout"
```

- [ ] **Step 2: Compile-Gate**

Run: `venv/bin/python -m py_compile adapters.py && echo OK`
Expected: OK

- [ ] **Step 3: Commit**

```bash
git add adapters.py
git commit -m "adapters: ComfyUIAdapter.restart() via ComfyUI-Manager reboot"
```

### Task 3: Auto-Restart-Hook + UI-Hook in main.py

**Files:**
- Modify: `main.py` (Import ~Z. 26; `refresh_backend` except-Zweig ~Z. 407–414; neue Helfer daneben; `admin.bind(...)` ~Z. 2942)
- Modify: `docs/superpowers/specs/2026-07-30-comfyui-watchdog-design.md` (inflight-Bedingung streichen, s. Global Constraints)

**Interfaces:**
- Consumes: `ComfyExecutorStuck`, `adapter.restart()`, `adapter.last_restart` (Task 1/2).
- Produces: `_maybe_auto_restart(backend: dict, adapter) -> None`; `restart_comfy_backend(bid: str) -> bool` (von `admin.py` als `_restart_comfy` gebunden, Task 5); Modul-Global `_comfy_restarting: set[str]`.

- [ ] **Step 1: Import ergänzen** — in der bestehenden `from adapters import (...)`-Zeile `ComfyExecutorStuck` mit aufnehmen.

- [ ] **Step 2: Helfer + Hook** — direkt VOR `refresh_backend` einfügen:

```python
_comfy_restarting: set[str] = set()      # backend ids with a restart() in flight


def _spawn_comfy_restart(backend: dict, adapter, why: str) -> None:
    bid = backend_id(backend)
    _comfy_restarting.add(bid)
    logger.warning(f"[{backend['name']}] restarting ComfyUI service ({why})")

    async def _run():
        try:
            await adapter.restart()
        except Exception as e:
            logger.warning(f"[{backend['name']}] ComfyUI restart failed: {e}")
        finally:
            _comfy_restarting.discard(bid)
    asyncio.create_task(_run())


def _maybe_auto_restart(backend: dict, adapter) -> None:
    """Opt-in: one restart attempt per cooldown when the executor is stuck.
    Deliberately NOT gated on inflight — stuck means nothing is executing, a
    pending gateway prompt is lost either way (its poll fails over/parks)."""
    bid = backend_id(backend)
    if not backend.get("auto_restart") or bid in _comfy_restarting:
        return
    cooldown = int(backend.get("restart_cooldown_s") or 600)
    if adapter.last_restart and time.time() - adapter.last_restart < cooldown:
        return
    _spawn_comfy_restart(backend, adapter, "executor stuck — auto-restart")


def restart_comfy_backend(bid: str) -> bool:
    """UI hook (Backends tab): fire-and-forget ComfyUI service restart."""
    b = next((x for x in backends if backend_id(x) == bid), None)
    adapter = backend_adapters.get(bid)
    if b is None or adapter is None or b.get("type") != "comfyui" or bid in _comfy_restarting:
        return False
    _spawn_comfy_restart(b, adapter, "manual via UI")
    return True
```

- [ ] **Step 3: Except-Zweig in `refresh_backend`** — am Ende des bestehenden `except Exception as e:`-Blocks (nach `backend_loras[bid] = set()`, Kommentar zu `backend_models` bleibt):

```python
        if isinstance(e, ComfyExecutorStuck):
            _maybe_auto_restart(backend, adapter)
```

- [ ] **Step 4: bind ergänzen** — in `admin.bind(...)`: `restart_comfy=restart_comfy_backend,` (z. B. nach `set_backend_enabled=...`).

- [ ] **Step 5: Spec-Zeile anpassen** — in der Spec §3 den Halbsatz „UND `backend_inflight == 0`“ ersetzen durch: „(bewusst nicht an `backend_inflight` gekoppelt — stuck heißt: nichts läuft; ein pending Gateway-Prompt ist ohnehin verloren)“.

- [ ] **Step 6: Compile-Gate**

Run: `venv/bin/python -m py_compile main.py && echo OK`
Expected: OK

- [ ] **Step 7: Commit**

```bash
git add main.py docs/superpowers/specs/2026-07-30-comfyui-watchdog-design.md
git commit -m "main: opt-in auto-restart for stuck ComfyUI executors + UI restart hook"
```

### Task 4: Watchdog-Felder in /health und gateway_info

**Files:**
- Modify: `main.py` (`gateway_info()` ~Z. 2741; `health()` ~Z. 2971; Helfer daneben)

**Interfaces:**
- Consumes: Adapter-Attribute aus Task 1/2.
- Produces: `_comfy_watch_info(b: dict) -> dict` — `{}` für Nicht-Comfy; sonst `{"exec_stuck": bool, "last_restart": int|None, "last_restart_result": str|None}`. Task 5 liest `exec_stuck`/`last_restart_result` aus dem UI-Snapshot.

- [ ] **Step 1: Helfer** — direkt vor `gateway_info()`:

```python
def _comfy_watch_info(b: dict) -> dict:
    """Executor-watchdog fields for comfy backends (merged into /health + UI snapshot)."""
    if b.get("type") != "comfyui":
        return {}
    ad = backend_adapters.get(backend_id(b))
    if ad is None:
        return {}
    return {"exec_stuck": bool(getattr(ad, "exec_stuck", False)),
            "last_restart": int(ad.last_restart) if getattr(ad, "last_restart", 0.0) else None,
            "last_restart_result": getattr(ad, "last_restart_result", "") or None}
```

- [ ] **Step 2: In beide Snapshots mergen** — im Backend-Dict von `gateway_info()` (nach `"source": ...`) und von `health()` (nach `"models": ...`) jeweils als letzte Zeile:

```python
                **_comfy_watch_info(b),
```

- [ ] **Step 3: Compile-Gate**

Run: `venv/bin/python -m py_compile main.py && echo OK`
Expected: OK

- [ ] **Step 4: Commit**

```bash
git add main.py
git commit -m "main: expose exec_stuck/last_restart in /health and the UI snapshot"
```

### Task 5: UI — Badge, Restart-Button, Editor-Felder (admin.py)

**Files:**
- Modify: `admin.py` (Callable-Default ~Z. 95; `backends_page.render()` ~Z. 830–861; `_backend_form` nach „comfy input dir“ ~Z. 771–776; `backend_save` ~Z. 1013–1030; Handler bei `backend_drain` ~Z. 1054; `register()` ~Z. 4725)

**Interfaces:**
- Consumes: `_restart_comfy` = `restart_comfy_backend(bid) -> bool` (Task 3); Snapshot-Felder `exec_stuck`, `last_restart_result` (Task 4).
- Produces: GET `/ui/backends/restart?id=<bid>`; Backend-Formularfelder `auto_restart` (Checkbox), `restart_cooldown_s`, `stuck_after_s` (number).

- [ ] **Step 1: Callable-Default** — neben `_drain_backend = None`: `_restart_comfy = None`.

- [ ] **Step 2: Badge + Button in `render()`** — nach der bestehenden badge-Kette (`else: badge = _badge("down", "bad")`):

```python
        if b.get("exec_stuck"):
            badge = _badge("⚠ executor stuck", "bad",
                           "ComfyUI answers HTTP but its executor is not draining the queue "
                           "— restart the service (⟳) or check the box/GPU")
```

und in `acts_list` (nach dem drain/enable-Zweig, vor dem delete-Zweig):

```python
        if b.get("type") == "comfyui" and b["enabled"]:
            acts_list.append(("⟳", f"/ui/backends/restart?id={quote(bid)}", "secondary",
                              "Restart the ComfyUI service (ComfyUI-Manager reboot)",
                              f"Restart ComfyUI on {b['name']}? Pending prompts there are lost."))
```

sowie in der `sub`-Zeile (vor `src`): `rst = f" · restart: {b['last_restart_result']}" if b.get("last_restart_result") else ""` und `{rst}` zwischen `{flags}` und `{src}` einfügen.

- [ ] **Step 3: Editor-Felder in `_backend_form`** — direkt nach dem „comfy input dir“-Hint-Absatz:

```python
            + _field("comfy watchdog",
                     _checkbox("auto_restart", gb("auto_restart"), "auto_restart",
                               "restart the ComfyUI service automatically when the executor is stuck"))
            + _field("restart cooldown s", _inp("restart_cooldown_s", g("restart_cooldown_s"),
                     placeholder="600", typ="number"))
            + _field("stuck after s", _inp("stuck_after_s", g("stuck_after_s"),
                     placeholder="90", typ="number"))
            + "<p class='hint' style='margin:-4px 0 10px'>Executor watchdog (comfyui only): the "
              "backend goes <b>down</b> when prompts wait while nothing runs for <b>stuck after s</b> "
              "seconds. <b>auto_restart</b> then reboots the service via the ComfyUI-Manager "
              "extension (requires it installed + a systemd unit with <code>Restart=always</code>), "
              "at most once per <b>restart cooldown s</b>.</p>"
```

- [ ] **Step 4: Speichern in `backend_save`** — `auto_restart` in die bestehende Flags-Schleife aufnehmen (`for flag in ("chat_only", "serverless_only", "local", "route_speed", "auto_restart"):`) und nach dem `comfy_input_dir`-Block:

```python
    for nkey in ("restart_cooldown_s", "stuck_after_s"):
        v = (f.get(nkey, "") or "").strip()
        if v.isdigit() and int(v) > 0:
            b[nkey] = int(v)
        else:
            b.pop(nkey, None)                  # blank = adapter/gateway defaults (90/600)
```

- [ ] **Step 5: Handler + Route** — neben `backend_drain`:

```python
async def backend_restart(request: Request):
    """Fire-and-forget ComfyUI service restart (ComfyUI-Manager reboot)."""
    bid = (request.query_params.get("id", "") or "").strip()
    if _restart_comfy and bid:
        _restart_comfy(bid)
    return RedirectResponse("/ui/backends", status_code=303)
```

und in `register()` nach der undrain-Route:
`app.add_api_route("/ui/backends/restart", backend_restart, methods=["GET"])`.

- [ ] **Step 6: Compile-Gate**

Run: `venv/bin/python -m py_compile admin.py && echo OK`
Expected: OK

- [ ] **Step 7: Commit**

```bash
git add admin.py
git commit -m "ui: ComfyUI watchdog — stuck badge, restart action, auto-restart settings"
```

### Task 6: End-to-End-Verifikation gegen Mock-ComfyUI

**Files:**
- Create: `<scratchpad>/mock_comfy.py` (NICHT ins Repo committen)
- Create: `<scratchpad>/gwtest/config.yaml` (NICHT ins Repo committen)

**Interfaces:**
- Consumes: alles aus Task 1–5.
- Produces: verifizierte Checkliste (unten); keine Repo-Artefakte.

- [ ] **Step 1: Mock schreiben** — `<scratchpad>/mock_comfy.py`:

```python
"""Controllable fake ComfyUI: /object_info, /queue, /manager/reboot + test hooks."""
from fastapi import FastAPI
import uvicorn

app = FastAPI()
STATE = {"mode": "idle", "reboots": 0}
# minimal object_info a real discover() finds ≥1 model in (shape must satisfy
# adapters._comfy_models — check that function and adjust before first run)
OBJECT_INFO = {
    "CheckpointLoaderSimple": {"input": {"required": {"ckpt_name": [["mock-model.safetensors"]]}},
                               "output": ["MODEL", "CLIP", "VAE"]},
    "LoraLoader": {"input": {"required": {"lora_name": [["mock-lora.safetensors"]]}},
                   "output": ["MODEL", "CLIP"]},
}

@app.get("/object_info")
def object_info(): return OBJECT_INFO

@app.get("/queue")
def queue():
    if STATE["mode"] == "stuck":
        return {"queue_running": [], "queue_pending": [[7, "deadbeef-prompt", {}]]}
    if STATE["mode"] == "busy":
        return {"queue_running": [[6, "live-prompt", {}]], "queue_pending": [[7, "deadbeef-prompt", {}]]}
    return {"queue_running": [], "queue_pending": []}

@app.post("/manager/reboot")
def reboot():
    STATE["reboots"] += 1; STATE["mode"] = "idle"; return {"ok": True}

@app.get("/_mode/{mode}")
def set_mode(mode: str):
    STATE["mode"] = mode; return STATE

@app.get("/_state")
def state(): return STATE

if __name__ == "__main__":
    uvicorn.run(app, host="127.0.0.1", port=4188)
```

Vor dem ersten Lauf `adapters._comfy_models`/`_comfy_loras` lesen und `OBJECT_INFO` so anpassen, dass discover ≥1 Modell + ≥1 LoRA liefert.

- [ ] **Step 2: Test-Gateway-Config** — `<scratchpad>/gwtest/config.yaml`:

```yaml
api_key: testkey
health_check_interval: 3
backends:
  - name: mock-comfy
    type: comfyui
    url: http://127.0.0.1:4188
    priority: 5
    stuck_after_s: 8
```

Vorher prüfen, wie `main.load_config()` den Config-Pfad auflöst (cwd-relativ vs. Modulpfad). Wenn cwd-relativ: Gateway mit `cwd=<scratchpad>/gwtest` und `--app-dir /home/dev/projekte/llm-gateway` starten, damit store.db/stats.db/jobs im Scratchpad landen. Wenn modulpfad-relativ: STOPP — nicht die Repo-config.yaml überschreiben; stattdessen Konfig-Mechanismus prüfen (env/Arg) und den Weg dokumentieren.

- [ ] **Step 3: Beide Prozesse starten** (Hintergrund):

```bash
venv/bin/python <scratchpad>/mock_comfy.py &
cd <scratchpad>/gwtest && /home/dev/projekte/llm-gateway/venv/bin/uvicorn main:app \
  --app-dir /home/dev/projekte/llm-gateway --port 4100 &
```

- [ ] **Step 4: Checkliste durchfahren** (curl; Soll-Ergebnisse):

1. `GET :4100/health` → `mock-comfy`: `healthy:true`, `exec_stuck:false`.
2. `GET :4188/_mode/busy`, 3 Ticks warten → weiterhin `healthy:true` (running nicht leer → kein Fehlalarm).
3. `GET :4188/_mode/stuck` → nach ~`stuck_after_s`+2 Ticks (≈15 s): `healthy:false`, `exec_stuck:true`; Gateway-Log enthält `DOWN — executor stuck`.
4. `GET :4100/ui/backends/restart?id=comfyui:mock-comfy` (Bootstrap-offen, kein Login) → `GET :4188/_state` → `reboots:1`; nach ≤2 Ticks `healthy:true`, `exec_stuck:false`, `last_restart_result:"ok"`.
5. `auto_restart: true` + `restart_cooldown_s: 30` in die Test-config.yaml (Hot-Reload) → `GET :4188/_mode/stuck` → OHNE UI-Aufruf steigt `reboots` auf 2, Backend erholt sich; Log enthält `auto-restart`.
6. Sofort wieder `_mode/stuck` → innerhalb der 30-s-Cooldown KEIN weiterer Reboot (`reboots` bleibt 2), danach genau einer.
7. UI-Renderprüfung: `GET :4100/ui/backends` liefert 200 und enthält „executor stuck“-Badge (während Schritt 3) und den ⟳-Button.

- [ ] **Step 5: Prozesse stoppen, Ergebnis dokumentieren** — beide Hintergrundprozesse beenden; Checklistenergebnis in die Commit-Message von Task 7 bzw. den Abschlussbericht übernehmen. Repo sauber (`git status`: nur beabsichtigte Änderungen).

### Task 7: Doku (CLAUDE.md + README)

**Files:**
- Modify: `CLAUDE.md` (Abschnitt `adapters.py` + Routing/Health-Erwähnung)
- Modify: `README.md` (Config-Knöpfe + /health-Felder + UI)

**Interfaces:** —

- [ ] **Step 1: CLAUDE.md** — im `adapters.py`-Absatz nach der `discover()`-Beschreibung ergänzen (sinngemäß, im Stil der Datei): discover() prüft zusätzlich `/queue` und wirft `ComfyExecutorStuck` (pending bei leerem running über ≥2 Checks und ≥`stuck_after_s`, Default 90) → normaler DOWN-Pfad; `restart()` = ComfyUI-Manager `/manager/reboot` (Transportfehler = Erfolgssignal, 404 = Manager fehlt, bis 120 s Warten auf Wiederkehr); `main.refresh_backend` triggert opt-in `auto_restart` (Cooldown `restart_cooldown_s`, Default 600, einmal pro Cooldown, `_comfy_restarting`-Guard); Felder `exec_stuck`/`last_restart*` in `/health` + Backends-Tab (⟳-Button).

- [ ] **Step 2: README** — beim ComfyUI-Backend-Abschnitt die drei neuen Backend-Felder (`auto_restart`, `restart_cooldown_s`, `stuck_after_s`) mit Defaults + Voraussetzung (ComfyUI-Manager installiert, systemd `Restart=always`) dokumentieren; `/health`-Beispiel um `exec_stuck` ergänzen.

- [ ] **Step 3: Commit**

```bash
git add CLAUDE.md README.md
git commit -m "docs: ComfyUI executor watchdog (stuck detection, restart, auto-restart)"
```
