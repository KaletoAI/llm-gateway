# CLAUDE.md

Guidance for Claude Code (claude.ai/code) when working in this repository.

## What this is

An OpenAI-compatible reverse proxy that fans one endpoint out across many
backends: local LLM servers (llama.cpp / llama-swap / vLLM / Ollama), cloud APIs
(Together.ai / OpenAI / OpenRouter), **and ComfyUI image-generation servers**. It
does per-backend auto-discovery, priority routing with failover, virtual aliases,
per-backend concurrency caps, an optional multi-user auth layer, **call parking**
(queue instead of 503 when busy), a full **image-generation subsystem** (workflow
mapping, LoRAs, jobs), and a server-rendered `/ui` console. Read `README.md`
first — it documents every config knob, endpoint, and routing rule.

## Run / develop

```bash
python3 -m venv venv && venv/bin/pip install -r requirements.txt
cp config.example.yaml config.yaml          # then edit backends + api_key
venv/bin/uvicorn main:app --host 0.0.0.0 --port 4000   # add --reload for dev
```

- `config.yaml` is gitignored and **hot-reloaded** on save (watchfiles) — no
  restart for backend/alias/priority changes. Read **only at startup**:
  `stats.enabled` and the stats/jobs DB paths.
- **No test suite, linter, or build step.** Verify by running the server and
  hitting endpoints with `curl` (README "Try it"), `curl localhost:4000/health`
  for a routing snapshot, or compile-gating (`venv/bin/python -m py_compile *.py`)
  before deploy.
- `requirements.txt` omits `watchfiles`; it ships with `uvicorn[standard]`. Keep it.
- Deploy with `DEPLOY_HOST=root@host ./deploy.sh` (rsync/tar over SSH, remote venv
  install, systemd sync, restart). The prod box has no rsync from dev — see project
  memory for the scp+restart path. Always compile-gate before scp (a broken file
  silently fails the restart).

## Architecture

Six self-contained Python files hold everything. `main.py` owns app state; the
others (`adapters`, `jobs`, `store`, `stats`, `admin`) never import `main` — they
receive what they need via injected callables, staying hot-reload-safe.

- **`main.py`** — config loading, health/discovery loop, routing, all HTTP
  endpoints, auth/quotas, call parking, generation orchestration, the Responses
  bridge. Module-level globals are the entire app state: config-bound
  (`backends`, `virtual_models`), discovery-bound (`backend_models`,
  `backend_pricing`, `backend_loras`, `backend_healthy`), live (`backend_inflight`,
  `_gen_tasks`, `users`/`_users_by_key`), and `backend_adapters`. `load_config()`
  rebinds config globals, `refresh_backend()` populates discovery ones, and
  `build_backend_adapters()` (re)binds one adapter per backend.
- **`adapters.py`** — the pluggable per-backend protocol seam. `BackendAdapter`
  ABC; `OpenAIAdapter` (`dispatch()` forwards chat/completions/embeddings,
  owns the in-flight counter incl. the streamed-`finally` decrement) and
  `ComfyUIAdapter` (`type: comfyui`; `discover()` via `/object_info` →
  models + **installed LoRAs**; `generate()` submits a parametrised workflow,
  polls `/history`, fetches `/view`). `AdapterContext` injects app services.
  Workflow injection is **mapping-driven, convention-free** (`_apply_mapping`
  sets `workflow[node].inputs[field]`); `_apply_lora_cascade` drops client LoRAs
  into free stack slots; `_apply_fixed` applies admin pins (the API can't override
  a pinned `(node,field)`); `suggest_mapping()` is only an auto-detect pre-fill.
- **`jobs.py`** — generation job store: SQLite metadata + on-disk artifacts under
  `jobs/<id>/<n>.<ext>`, lifecycle `queued→running→done|failed`, TTL pruning. Also
  persists job **inputs** (`set_inputs`: prompt/params/reference images) and
  `reconcile_orphans()` (startup: mark interrupted `running`/`queued` as failed).
  Carries `owner`. Reused for parked **chat** jobs (task type `chat`).
- **`store.py`** — writable SQLite store, the console's source of truth: backends,
  chat aliases, generation aliases (+ workflow_json/mapping/fixed), users (api keys
  **encrypted** via `secret.key`), IP aliases, server settings. Seeded once from
  config, then authoritative.
- **`admin.py`** — the `/ui` console (mounted via `admin.register(app)` +
  `add_api_route`, *not* `include_router` — broken in this starlette build;
  callbacks injected via `admin.bind(...)`). Session-gated by `_ui_guard` once
  locked. Tabs in `TABS`; the workflow Mapping editor owns a pasted ComfyUI API
  JSON and offers discovery-fed dropdowns. POST bodies parsed by hand (`parse_qs`)
  to stay `python-multipart`-free.
- **`stats.py`** — optional SQLite (WAL) call log + body store. The dashboard is
  **in the `/ui` Statistic/Routing tabs** (no separate port — the old standalone
  :4001 server was folded into the console). Zero new dependencies — keep it.

### Request flow

- **Chat/LLM** (`/v1/chat/completions`, `/v1/completions`, `/v1/embeddings` via
  `route()`): `resolve_routes()`/`get_routes_for(alias)` → ready vs busy candidate
  split → `backend_adapters[bid].dispatch(NormalizedRequest)` to the first, failing
  over only on connection/timeout errors (HTTP error status returned as-is). All
  busy → **park** (sync block or async job) instead of 503. `/v1/responses` has its
  own loop (translates bodies) but shares `get_routes_for()`.
- **Generation** (`POST /v1/generations`, and the OpenAI shims
  `/v1/images/generations` + `/v1/images/edits`): `get_gen_routes(alias)` resolves
  the alias via the **separate** generation store (`image_models`/store), filtered
  to enabled+healthy comfy backends; LoRA-aware preference + busy→park; a
  `jobs.py` job runs via `adapter.generate()` (sync inline or async job-id). A
  running job can be cancelled (`cancel_generation` → ComfyUI `/interrupt` + task
  cancel).

### Routing rules (`get_routes_for`/`get_gen_routes` + `alias_entry`)

A backend is a candidate only if enabled, healthy, **not busy** (in-flight cap),
maps the alias, and exposes the resolved model. Recurring concepts:

- **Aliases** (`virtual_models`): string (same everywhere) or per-backend dict whose
  value is a model id or `{model, priority}` (per-alias priority override).
  `alias_entry()` resolves all shapes.
- **Backend prefixing** (`split_backend_prefix`): `<backend>/<model>` pins that
  backend; a bare id/alias routes by priority. `local: true` *also* lists models
  bare (cross-backend implicit alias). `model_prefix` toggles prefixed listing.
- **Concurrency/busy** (`backend_inflight`, `backend_busy`): incremented in
  `dispatch()`/`generate()`, decremented on completion incl. the streamed `finally`.
- **Parking** vs 503: "all busy" is distinguished from "no backend" — only the
  former parks.
- **LoRA-aware generation routing**: a backend lacking a requested LoRA is dropped
  from candidates (decided over all candidates incl. busy → parks for the
  LoRA-backend rather than spilling); a LoRA on no backend is ignored (priority
  wins); an explicit `backend` force is never overridden. Per-backend LoRA sets
  come from discovery (`backend_loras`).
- **Allow-list filtering**: `/v1/models` authenticates the caller and filters by
  their allow-list (entries may be aliases, model ids, or **backend names** =
  all that backend's models); image aliases are included; `?type=chat|image`.
- **Alias/model-name collisions** (`alias_model_conflicts`): surfaced in `/health`
  + Routing tab, split `covered` vs actionable `shadowed`.

### Auth / multi-user

`authenticate()` resolves a Bearer token to a user (`_users_by_key`) or the
master `_MASTER_ADMIN` (the top-level `api_key`); `gate_request()` enforces the
allow-list (`_model_allowed`, incl. whole-backend grants) + quotas and attributes
the call. Bootstrap-open with no users and no master key. The `/ui` console
session is gated by `_ui_guard` once locked.

### Stats recording

Every forward calls `stats.record_call(...)` fire-and-forget via
`asyncio.create_task` — never raises into the request path. Cost from pricing
cached at discovery (`normalize_pricing`: Together per-million, OpenRouter
per-token). Streaming records `0` tokens.

## Conventions

- Two pricing schemas and two `/v1/models` payload shapes (`{"data":[…]}` vs bare
  list) are handled defensively in discovery (`extract_models`/`extract_pricing`/
  `_is_chat_model`). Preserve that.
- The Responses↔Chat bridge is non-streaming only; `stream:true` is downgraded
  (no SSE translation).
- Keep `stats.py`/`jobs.py`/`store.py` dependency-free and hot-reload-safe (no
  caching config values at import time).
- Generation: workflow + mapping are **backend-independent** (shared across an
  alias's candidates); only **pinned values** (`fixed`) are per-backend. A pinned
  `(node,field)` is authoritative — never overridden by an API request param.
- Single instance only; verify with compile + a route/render check; restart the
  one instance when the user says idle. Never commit `config.yaml`, `store.db`
  (+ `secret.key`), `stats.db*`, `jobs.db*`, `jobs/`, `*.key`.
