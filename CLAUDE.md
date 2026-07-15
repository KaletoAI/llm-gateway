# CLAUDE.md

Guidance for Claude Code (claude.ai/code) when working in this repository.

## What this is

An OpenAI-compatible reverse proxy that fans one endpoint out across many
backends: local LLM servers (llama.cpp / llama-swap / vLLM / Ollama), cloud APIs
(Together.ai / OpenAI / OpenRouter), **and ComfyUI image-generation servers**. It
does per-backend auto-discovery, priority routing with failover, virtual aliases,
per-backend concurrency caps, an optional multi-user auth layer, **call parking**
(a default FIFO queue instead of 503 when busy; per-alias park time; async via the
Responses background mode), a full **media-generation subsystem** (image/video/
audio; workflow mapping, LoRAs, jobs), a **normalized reasoning toggle** (one
`reasoning: off|on|auto` control mapped to the right per-(model,backend)
mechanism, plus per-alias defaults), and a server-rendered `/ui` console. Read
`README.md` first — it documents every config knob, endpoint, and routing rule.

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

Ten self-contained Python files hold everything. `main.py` owns app state; the
others (`adapters`, `jobs`, `store`, `stats`, `admin`, `reasoning`,
`responses_bridge`, `openai_image_bridge`, `previewanim`) never import `main` — they
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
  owns the in-flight counter incl. the streamed-`finally` decrement; streamed
  chat SSE is rewritten to strict OpenAI shape by `_StreamNormalizer` — no
  null-valued delta keys, the terminal usage chunk only for clients that sent
  `stream_options.include_usage`; strict clients like Hermes abort on the raw
  LocalAI shape) and
  `ComfyUIAdapter` (`type: comfyui`; `discover()` via `/object_info` →
  models + **installed LoRAs**; `generate()` submits a parametrised workflow,
  polls `/history`, fetches `/view`). `AdapterContext` injects app services.
  Workflow injection is **mapping-driven, convention-free** (`_apply_mapping`
  sets `workflow[node].inputs[field]`); a mapping `label` is the param's public
  API name — incoming values are accepted under label OR param, and the
  auto-random seed keys on that effective name (`''` counts as unset);
  `_apply_lora_cascade` drops client LoRAs into free stack slots; `_apply_fixed`
  applies admin pins (the API can't override a pinned `(node,field)`);
  `suggest_mapping()` is only an auto-detect pre-fill. ComfyUI `/prompt`
  rejections are translated to readable per-node errors (node title, class,
  field, offending request param) via `_comfy_prompt_error`; raw body stays the
  fallback. Artifact delivery: a `/view` 404 means "file absent" (probe on), any
  OTHER status raises — a backend error must never silently shrink a delivery;
  `output_ext` prefers the same-stem sibling but ships the reported file when the
  sibling is missing; `output_cases` fetches a case ONCE and checks the detect
  glob on the result; plain `output_globs` may accompany cases as unconditional
  extras. A relative `/view` path keeps its dirs as the subfolder (`_view_params`).
- **`jobs.py`** — generation job store: SQLite metadata + on-disk artifacts under
  `jobs/<id>/<n>.<ext>` (image/video/audio; manifest carries `kind`+`mime`),
  lifecycle `queued→running→done|failed`, TTL pruning. Also persists job **inputs**
  (`set_inputs`: prompt/params/reference images) and `reconcile_orphans()` (startup:
  mark interrupted `running`/`queued` as failed). Carries `owner`. Reused for
  **background Responses** jobs (task type `response`, result via `complete_json`).
- **`store.py`** — writable SQLite store, the console's source of truth: backends,
  chat aliases, generation aliases (+ workflow_json/mapping/fixed), users (api keys
  **encrypted** via `secret.key`), IP aliases, server settings. Seeded once from
  config, then authoritative.
- **`admin.py`** — the `/ui` console (mounted via `admin.register(app)` +
  `add_api_route`, *not* `include_router` — broken in this starlette build;
  callbacks injected via `admin.bind(...)`). Session-gated by `_ui_guard` once
  locked. Tabs in `TABS`; a top tab can group child views via `SUBTABS` +
  `_with_subnav()` (`?sub=` on the parent route, first child = default —
  Playground: Chat | Media | Voice, Jobs & Calls: LLM | Media | Voice,
  Input & Routing: Input | Chat aliases | LLM models | Media aliases |
  Image models | LoRAs); the workflow Mapping editor owns a pasted
  ComfyUI API JSON and offers discovery-fed dropdowns. POST bodies parsed by
  hand (`parse_qs`) to stay `python-multipart`-free.
- **`stats.py`** — optional SQLite (WAL) call log + body store. The dashboard is
  **in the `/ui` Statistic/Routing tabs** (no separate port — the old standalone
  :4001 server was folded into the console). Zero new dependencies — keep it.
  The `calls` row carries the applied `reasoning` control (shown in LLM Calls).
- **`responses_bridge.py`** — pure Responses↔Chat translation functions (no
  gateway state): `responses_to_chat` / `chat_to_responses` / `responses_stream`
  (chat SSE → Responses SSE) and `response_shell()`, the ONE Responses-object
  skeleton every state (completed / stream events / background queued-failed)
  builds on. `main.py` keeps the endpoints, dispatch/parking, and background
  mode; the adapter attaches `resp.parsed_json` so the bridge never re-parses
  the raw body.
- **`openai_image_bridge.py`** — pure request/response plumbing for the OpenAI
  image shims (`multipart_list`, `parse_size`, `coerce_scalar`, `images_uploads`
  slot mapping, `images_response`); imports only the leaf `jobs`. `main.py`
  keeps the endpoints and passes the alias's image `slots` in (one
  `_gen_image_slots` lookup per request).
- **`reasoning.py`** — pure functions for the normalized thinking toggle, no
  `main`/`adapters` imports (hot-reload/test-friendly). Rules are an ordered list
  of `{match(model-glob), backends[], adapter, param}`; `resolve()` picks the
  first rule whose glob matches AND whose backend-set contains the dispatch
  backend, `apply()` rewrites a **copy** of the outgoing chat body per the chosen
  `adapter` (`enable_thinking` / `reasoning_effort` / `nothink_token` / `prefill`
  / `none`) and returns the `x-reasoning-control` string. `none`/no-match →
  `unsupported` (never fails). Rules live in `store` (settings key
  `reasoning_rules`), are cached in `main.reasoning_rules` (refreshed on save via
  `apply_reasoning_rules()`), and are edited in the `/ui` **Reasoning** tab.
  Additionally a **per-alias default** (`alias_reasoning`, store settings; edited
  in the chat-alias editor) supplies off/on when the client sends nothing — so
  `tool`/`tool-thinking` can share one backend+model; an explicit client
  `reasoning` always wins.
- **`previewanim.py`** — injects a short looping idle animation into a rigged GLB for
  the `/ui` inspection view ONLY (`add_idle(glb) → glb`), so bad skin weights show as
  spikes/rings once it deforms. Pure struct/json on the glTF binary, append-only; the
  result route applies it on `?anim=idle`, never to the delivered file.

### Request flow

- **Chat/LLM** (`/v1/chat/completions`, `/v1/completions`, `/v1/embeddings`,
  and `/v1/audio/speech` — a binary TTS passthrough: `route()` strips `stream`
  on `/v1/audio/*`, fills per-alias `alias_voice` defaults, and the adapter
  skips body parsing/stats blobs for non-text responses — via `route()`): all
  funnel through **`_dispatch_or_park()`** — `resolve_routes()` →
  ready vs busy split → `backend_adapters[bid].dispatch(NormalizedRequest)` to the
  first ready, failing over on connection/timeout errors and on llama-swap's
  "unable to start process" 502 (backend-local load failure, `_retryable_upstream_error`);
  other HTTP error statuses return as-is. The adapter opens the upstream stream
  BEFORE answering, so streamed upstream errors carry their real status too. All busy → **park by default** (FIFO queue, per-alias `park_s`)
  until a backend frees, else 503; no client field. Before dispatch,
  `_normalize_reasoning()` folds the client `reasoning`/`reasoning_effort` control
  to `off|on|None` and stashes it in `body["_reasoning"]`; the adapter strips all
  `_`-prefixed keys and runs `apply_reasoning` **per backend** on a copy of the
  body (so failover re-derives it). `/v1/responses` translates bodies and shares
  `_dispatch_or_park()` too (also parks, and normalizes reasoning incl. the
  Responses `{effort}` shape); `background:true` runs it async via the official
  Responses background mode (see below).
- **Generation** (`POST /v1/generations`, and the OpenAI shims
  `/v1/images/generations` + `/v1/images/edits`): `get_gen_routes(alias)` resolves
  the alias via the **separate** generation store (`image_models`/store), filtered
  to enabled+healthy comfy backends; LoRA-aware preference + busy→park; a
  `jobs.py` job runs via `adapter.generate()` (sync inline or async job-id). A
  running job can be cancelled (`cancel_generation` → ComfyUI `/interrupt` + task
  cancel).
- **Workflow chains** (`_run_chain`): a gen alias's stage-1 config carries a
  `successor` (`{alias, export_node, mesh_param, relay?, keep_from_mesh?, rig?}`);
  stage 1 exports a mesh under a gateway-pinned filename (`gwchain_<jobid>`) and
  ONLY stage 2's result is delivered (+ any `keep_from_mesh` files). Stage 1 gets
  the normal routing guarantees: candidates re-resolved while parked (force pin +
  LoRA eligibility kept), misconfigured candidates skipped, connection errors fail
  over (stage-2/hand-off errors are FINAL); `mesh_param` is validated against the
  successor's mapping (param or label) and the mesh extension honors pins/mapped
  params on the export node's `file_format`. The job row's `backend` is re-pointed
  at claim and hand-off (`jobs.set_backend`) so cancel interrupts the LIVE backend.
  Chain stages run with `slot_held` (the chain claims the one slot itself — no
  double count). Two hand-offs:
  `relay: path` (default) keeps both stages on ONE backend (shared disk, one slot
  held across both — queue-isolated) and passes the mesh's absolute output path;
  `relay: upload` lets the successor run on a **different** backend — the gateway
  fetches the mesh (`/view`), uploads it into the stage-2 backend's input dir
  (`adapter.upload_input` → ComfyUI `/upload/image`) and passes the file's
  **absolute input-dir path** (backend `comfy_input_dir`, blank = derived from
  `comfy_output_dir`'s `…/input` sibling) — the successor consumes it exactly like
  a path hand-off; only with no input dir known does the bare stored name go over
  (then a load-from-input node is required). Cross-backend releases the stage-1
  slot AND frees its ComfyUI VRAM once the mesh is in hand, then claims the
  stage-2 slot.

### Routing rules (`get_routes_for`/`get_gen_routes` + `alias_entry`)

A backend is a candidate only if enabled, healthy, **not busy** (in-flight cap),
maps the alias, and exposes the resolved model. Recurring concepts:

- **Route index** (`_route_index` + `rebuild_route_index()`): alias→candidates and
  bare-model-id pass-through are **precomputed** (pre-sorted by effective priority);
  `resolve_routes()` only evaluates the live flags (healthy/busy/draining) per
  request. Rebuilt by `rebuild_backends()`/`rebuild_virtual_models()` and on every
  discovery model-set change — never mutate `backends`/`virtual_models` outside
  those functions or the index goes stale.

- **Aliases** (`virtual_models`): string (same everywhere) or per-backend dict whose
  value is a model id or `{model, priority}` (per-alias priority override).
  `alias_entry()` resolves all shapes.
- **Backend prefixing** (`split_backend_prefix`): `<backend>/<model>` pins that
  backend; a bare id/alias routes by priority. `local: true` *also* lists models
  bare (cross-backend implicit alias). `model_prefix` toggles prefixed listing.
- **Concurrency/busy** (`backend_inflight`, `backend_busy`): incremented in
  `dispatch()`/`generate()`, decremented on completion incl. the streamed `finally`.
- **Parking** vs 503: "all busy" is distinguished from "no backend" — only the
  former parks (the default). The queue is `_parked` (rich entries: alias, source,
  deadline, `asyncio.Event`); `_inflight_dec`→`_notify_slot_free` wakes all in FIFO
  order so the oldest eligible claims the freed slot (the invariant: dispatch's
  `inflight_inc` runs with no `await` between it and `resolve_routes`). Park time
  per alias via `alias_park_s` (store `alias_park` + config), else `park_timeout_s`
  (default 60); `0` disables. Timeout → 503 + `Retry-After`. "Parked calls" panel
  on the Dashboard. **Async chat has no OpenAI spec** — async lives on the Responses
  background mode: `POST /v1/responses {background:true}` → `resp_<jobid>` queued →
  `GET /v1/responses/{id}` poll → `POST …/cancel`; the worker (`_run_bg_response`)
  parks in the same queue (jobs.py task `response`).
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
- **Host coordination** (shared-GPU boxes; `docs/host-coordination-plan.md`):
  backends group by physical box (`backend_host`: explicit `host` field, else URL
  IP → `backend_hosts`/`host_backends`, shown in `/health` and the Backends tab's
  Hosts panel). Per-host policies (store settings `hosts`, cached `hosts_meta`):
  chat candidates on a host with a RUNNING media job sort LAST in `resolve_routes`
  (never dropped; flag `avoid_llm_during_media`, default on); after a media job
  ends, ComfyUI gets `POST /free` (`_free_comfy_vram`; default on for shared
  hosts only — ComfyUI never frees VRAM itself, a llama-swap load would abort);
  opt-in `llm_unload_before_media` GETs llama-swap `/unload` first.

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
per-token). Streaming records the backend's usage chunk (the adapter always
requests `include_usage` upstream); a backend that reports zeros/nothing
(LocalAI streams all-zero usage — measured) gets gateway estimates instead
(content-delta count ≈ completion tokens, ~chars/4 for the prompt).

## Conventions

- Two pricing schemas and two `/v1/models` payload shapes (`{"data":[…]}` vs bare
  list) are handled defensively in discovery (`extract_models`/`extract_pricing`/
  `_is_chat_model`). Preserve that.
- The Responses↔Chat bridge supports `stream:true` (chat SSE → Responses SSE) and
  `background:true` (async). What's **not** built yet: streaming-reconnect for a
  background response (poll-only). Keep in sync when touching `/v1/responses`.
- Keep `stats.py`/`jobs.py`/`store.py`/`reasoning.py`/`responses_bridge.py`
  dependency-free and hot-reload-safe (no caching config values at import time);
  `reasoning.py` and `responses_bridge.py` must stay pure (no `main`/`adapters`
  imports) — they're called on the request path.
- Generation: workflow + mapping are **backend-independent** (shared across an
  alias's candidates); only **pinned values** (`fixed`) are per-backend. A pinned
  `(node,field)` is authoritative — never overridden by an API request param.
- Single instance only; verify with compile + a route/render check; restart the
  one instance when the user says idle. Never commit `config.yaml`, `store.db`
  (+ `secret.key`), `stats.db*`, `jobs.db*`, `jobs/`, `voiceref/`, `*.key`.
- Voice cloning (`/v1/audio/speech`): TTS backends read `voice` strictly as a
  file on THEIR host (no base64/URL/upload API — measured). The voice library
  (`voiceref/` blobs + store `voice_library`, UI in the Voice sub-tab) therefore
  ships references via scp to EVERY target in `voice_ref_hosts` (settings;
  comma-separated `user@host:/abs/host/dir`, host-side dirs may differ — docker
  mounts), while `voice_ref_dir` is the single model-visible path written into
  `voice`; `voice:"lib:<name>"` resolves to the shipped path + ref_text in
  `route()`. An empty ref_text is auto-transcribed: local faster-whisper first
  (lazy CPU import; the one heavyweight entry in `requirements.txt`), a backend
  whisper model as fallback.
