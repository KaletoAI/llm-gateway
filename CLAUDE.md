# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

An OpenAI-compatible reverse proxy that fans one OpenAI endpoint out across many
LLM backends (llama.cpp / llama-swap / vLLM / Ollama locally, plus cloud APIs
like Together.ai / OpenAI / OpenRouter). It does auto-discovery of each backend's
models, priority routing with failover, virtual model aliases, per-backend
concurrency caps, and an optional SQLite call-stats dashboard. Read `README.md`
first — it is unusually complete and documents every config knob and routing rule.

## Run / develop

```bash
python3 -m venv venv && venv/bin/pip install -r requirements.txt
cp config.example.yaml config.yaml          # then edit backends + api_key
venv/bin/uvicorn main:app --host 0.0.0.0 --port 4000   # add --reload for dev
```

- `config.yaml` is gitignored and **hot-reloaded** on save (watchfiles) — no
  restart needed for backend/alias/priority changes. Exceptions read only at
  startup: `stats.enabled` and the stats port/bind.
- There is **no test suite, linter, or build step.** Verify changes by running
  the server and hitting endpoints with `curl` (examples in README "Try it"),
  or `curl localhost:4000/health` for a routing snapshot.
- `requirements.txt` omits `watchfiles`; it ships transitively with
  `uvicorn[standard]`. Keep that extra.
- Deploy with `DEPLOY_HOST=root@host ./deploy.sh` (rsync-or-tar over SSH, remote
  venv install, systemd unit sync, restart). Note the prod box has no rsync from
  dev — see project memory for the scp+restart path.

## Architecture

Six files hold everything (the gateway is mid-migration to a multimodal,
multi-backend design — see `docs/multimodal-gateway-plan.md`):

- **`main.py`** — config loading, health/discovery loop, routing, all HTTP
  endpoints, and the Responses-API bridge. Module-level globals (`backends`,
  `virtual_models`, `backend_models`, `backend_healthy`, `backend_pricing`,
  `backend_inflight`, `backend_adapters`) are the entire app state; `load_config()`
  rebinds the config ones, `refresh_backend()` populates the discovery ones, and
  `build_backend_adapters()` (re)binds one adapter per backend at start + reload.
- **`adapters.py`** — the pluggable per-backend protocol seam (Phase 0 of
  `docs/multimodal-gateway-plan.md`). `BackendAdapter` ABC with `discover()` /
  `dispatch()`; `OpenAIAdapter` is the only impl today (a verbatim move of the
  former `proxy()` + the `/v1/models` discovery helpers `extract_models` /
  `extract_pricing` / `_is_chat_model`). `AdapterContext` injects app services
  (in-flight counter, stats sink, pricing, log flag via a callable) so adapters
  never import `main` and stay hot-reload-safe. New protocols (ComfyUI, …)
  register in `ADAPTERS`; backends pick one via a `type:` field (default `openai`).
  `ComfyUIAdapter` (`type: comfyui`) is the second impl — `discover()` via
  `/object_info`, and a `generate()` path (async submit `/prompt` → poll
  `/history` → fetch `/view`) instead of `dispatch()`. `NormalizedRequest` carries
  both the chat body and the generation fields (`task`/`inputs`/`params`/`workflow`).
  Workflow injection is **mapping-driven, convention-free**: `_apply_mapping` sets
  `workflow[node].inputs[field]` from an explicit per-workflow `{param: {node, field}}`
  table (config `image_models[*].mapping`, later UI-authored). `suggest_mapping()` is
  only an auto-detect fallback/UI pre-fill, not the runtime mechanism.
- **`jobs.py`** — generation job store (Phase 1), self-contained like `stats.py`:
  SQLite metadata + on-disk artifacts under `jobs/<id>/<n>.<ext>`, lifecycle
  `queued→running→done|failed→expired` with TTL pruning. Carries `owner` already
  (Phase-3 multi-user). Auto-initialised when `image_models` are configured.
- **`store.py`** — writable generation-alias store (SQLite), the UI's source of
  truth. Seeded once from config `image_models` (one-way bootstrap), then
  `get_gen_routes()` resolves from it. Self-contained like `jobs.py`.
- **`admin.py`** — tabbed management UI at `/ui` (mounted via `admin.register(app)`
  + `add_api_route`, *not* `include_router` — that's broken in this starlette
  build). The nav shell (`TABS` + `_page`) is the extension point. Tabs: Backends,
  Input (what clients can call), Mapping, Playground, Statistic/Users/Server
  (scaffolded stubs). Registration v2: paste (or share-path) a ComfyUI **API JSON**
  → the gateway **owns** it (`workflow_json` in the store, independent of GUI
  edits) → auto-suggest the request mapping + auto-detect model loader slots →
  edit page with **discovery-fed dropdowns** (from `/object_info`) that flag stale
  model names. POST bodies are parsed by hand (`parse_qs`) to stay
  `python-multipart`-free; still unauthenticated (Phase-3 hardening).
- **`stats.py`** — optional, self-contained. SQLite (stdlib `sqlite3`, WAL)
  call log + a plain-HTML dashboard (f-strings, no template/JS/chart deps) served
  on its own uvicorn server (default port 4001) with **Stats** and **Routing**
  tabs. Zero new dependencies is a deliberate constraint — keep it that way.

### Request flow

`route()` (used by `/v1/chat/completions`, `/v1/completions`, `/v1/embeddings`)
→ `get_routes_for(alias)` returns `(backend, real_model)` candidates in effective
priority order → `backend_adapters[name].dispatch(NormalizedRequest)` forwards to
the first, falling back to the next only on `ConnectError`/`TimeoutException` (an
HTTP error status is returned as-is, not retried). `/v1/responses` has its own loop
(it translates bodies, still calls backends directly — not yet adapter-routed) but
uses the same `get_routes_for()`.

Generation (`POST /v1/generations`) is a parallel, job-based path: `get_gen_routes()`
resolves a generation alias via the **separate** `image_models` config section
(kept apart from `virtual_models` until Phase 3) → `adapter.generate()` runs the
job → results are persisted by `jobs.py` and returned inline (sync) or via
`GET /v1/jobs/{id}` + `/result/{n}` (async, retrievable until TTL).

### Routing rules (the core logic, all in `get_routes_for` + `alias_entry`)

A backend is a candidate only if it is enabled, healthy (last `/v1/models` poll
succeeded), **not busy** (below its `max_concurrent` in-flight cap), maps the
alias, and actually exposes the resolved real model. Key concepts that recur:

- **Aliases** (`virtual_models`): a string (same model everywhere) or a per-backend
  dict whose value is either a model-id string or `{model, priority}` — the latter
  overrides that backend's global priority **for this alias only**. `alias_entry()`
  resolves all four shapes to `(real_model, priority_override)`.
- **Backend prefixing** (`split_backend_prefix`): an incoming `<backend>/<model>`
  id routes to exactly that backend; a bare id/alias routes by priority. The split
  only fires when the first path segment is a known backend name, so vendor
  prefixes (`moonshotai/…`) pass through untouched. `/v1/models` lists ids
  prefixed when `model_prefix: true` (default). A backend with `local: true`
  *additionally* lists each model under its **bare** id, so several `local`
  backends sharing a model id collapse to one entry that routes by priority and
  fails over — like an implicit cross-backend alias.
- **Concurrency/busy** (`backend_inflight`, `backend_busy`): live per-backend
  in-flight counter, incremented in `OpenAIAdapter.dispatch()`, decremented on
  completion — critically including when a **streamed** response finishes (in the
  generator's `finally`), not when headers are sent. At/above the cap → skipped,
  request spills to the next backend.
- **Alias/model-name collisions** (`alias_model_conflicts`): naming an alias the
  same as a real model id shadows that model. Surfaced in `/health` and the
  Routing tab, split into `covered` vs actionable `shadowed` hosts.

### Stats recording

Every forward calls `stats.record_call(...)` fire-and-forget via
`asyncio.create_task` — it must never raise into the request path. Cost comes
from pricing cached at discovery time (`normalize_pricing` handles Together's
per-million and OpenRouter's per-token schemas). Streaming calls record `0`
tokens (backends omit `usage` in stream chunks).

## Conventions

- Two pricing schemas and two `/v1/models` payload shapes (`{"data":[…]}` vs a
  bare list) are handled defensively throughout — preserve that when touching
  discovery (`extract_models`, `extract_pricing`, `_is_chat_model`, now in
  `adapters.py`).
- The Responses↔Chat bridge (`responses_to_chat` / `chat_to_responses`) is
  non-streaming only; `stream: true` is silently downgraded. SSE translation is
  not implemented.
- Keep `stats.py` dependency-free and config hot-reload-safe (mutate globals via
  `load_config`, don't cache config values at import time).
