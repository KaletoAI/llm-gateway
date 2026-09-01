# CLAUDE.md

Guidance for Claude Code (claude.ai/code) when working in this repository.

## What this is

An OpenAI-compatible reverse proxy that fans one endpoint out across many
backends: local LLM servers (llama.cpp / llama-swap / vLLM / Ollama), cloud APIs
(Together.ai / OpenAI / OpenRouter), **and ComfyUI image-generation servers**. It
does per-backend auto-discovery, one queued scheduler (fastest free unpaid backend,
freed-backend type affinity, overdue guard — `scheduler.py`) with failover, virtual aliases,
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
  restart for backend/alias changes. Read **only at startup**:
  `stats.enabled` and the stats/jobs DB paths.
- **No test suite, linter, or build step.** Verify by running the server and
  hitting endpoints with `curl` (README "Try it"), `curl localhost:4000/health`
  for a routing snapshot, or compile-gating (`venv/bin/python -m py_compile *.py`)
  before deploy.
- `requirements.txt` omits `watchfiles`; it ships with `uvicorn[standard]`. Keep it.
- Deploy with `DEPLOY_HOST=root@host ./deploy.sh` (rsync/tar over SSH, remote venv
  install, systemd sync, restart). rsync IS present on both dev and the prod box
  (re-checked 2026-08-18; an older note claiming otherwise was stale). Always
  compile-gate first — a broken file fails the restart silently.

## Architecture

Eleven self-contained Python files hold everything. `main.py` owns app state; the
others (`adapters`, `jobs`, `store`, `stats`, `admin`, `reasoning`,
`responses_bridge`, `anthropic_bridge`, `openai_image_bridge`, `previewanim`) never
import `main` — they receive what they need via injected callables, staying
hot-reload-safe.

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
  LocalAI shape; a backend's `stream_reasoning_as_content` model-globs relabel
  stream `reasoning` deltas as `content` — LocalAI marks EVERY delta as
  reasoning when the rendered prompt contains a thinking marker, e.g. Gemma-4's
  pre-closed `<|channel>thought` tail, although the model can only emit plain
  answer text then) and
  `ComfyUIAdapter` (`type: comfyui`; `discover()` via `/object_info` →
  models + **installed LoRAs**, plus an **executor watchdog** via `/queue`:
  same head prompt pending with an idle executor across ≥2 checks and
  ≥`stuck_after_s` (default 90) → `ComfyExecutorStuck` → the normal DOWN path
  in `refresh_backend` (ComfyUI answers HTTP even when its prompt worker died —
  measured 2026-07-30, CUDA fault); `restart()` = ComfyUI-Manager
  `/manager/reboot` (transport error on the POST is the expected success
  signal, 404 = Manager missing, then ≤120 s wait for `/object_info` to
  return); `refresh_backend` triggers opt-in `auto_restart` per backend
  (`restart_cooldown_s`, default 600, one attempt per cooldown,
  `_comfy_restarting` guard; deliberately not gated on inflight — stuck means
  nothing executes). `exec_stuck`/`last_restart*` surface in `/health` + the
  Backends tab (⟳ restart action). `generate()` submits a parametrised
  workflow, polls `/history` every `poll_interval` (default 1) until the backend's
  `max_wait` (default 600) — the gateway's cap on ONE generation; ComfyUI itself has
  none — then `/interrupt`s and raises `TimeoutError`, and fetches `/view`. Both
  fields are edited in the Backends tab: a store backend replaces a same-named config
  entry WHOLESALE (`rebuild_backends`), so config.yaml cannot supply them for a
  UI-managed backend. `TimeoutError` sits in `_GEN_FAILOVER_ERRORS` so it fails over,
  but `_fault_label`/`_gen_exhausted_msg` keep it NAMED apart from a real connection
  fault and from an httpx transport timeout — reporting a `max_wait` expiry as
  "unreachable (connection)" sends you diagnosing the network instead of the
  workflow, and the cap is spent PER CANDIDATE.) `AdapterContext` injects app services.
  `AnthropicAdapter` (`type: anthropic`, subclasses `OpenAIAdapter`) serves
  **`/v1/messages` only** — a licence boundary enforced in routing
  (`main.serves_path`), not just documented: a Claude subscription token covers
  Claude Code, not re-serving Claude as an API. It is a VERBATIM passthrough, so
  three things that run for every other backend must not run there —
  `_StreamNormalizer`, `apply_reasoning()` and `sampling_defaults` — else
  `cache_control` breakpoints (full-price context re-reads without them), thinking
  signatures and fine-grained tool streaming are lost. The three seams that make
  this a subclass instead of a fork: `_payload()` (verbatim vs. sampling+reasoning),
  `_backend_auth()` (`x-api-key` vs. OAuth bearer + `oauth-2025-04-20` beta appended
  to the CLIENT's beta list) and `_usage_of()` (`input_tokens`/`output_tokens`
  incl. cache reads, else every subscription call books 0 tokens). The same
  `OpenAIAdapter` also serves `/v1/messages` for chat backends by calling
  `anthropic_bridge` in both directions, which is what lets ONE alias hold an
  Anthropic backend AND an OpenRouter model with normal failover between them.
  `_HOP_BY_HOP` drops `x-api-key` alongside `authorization` — both are GATEWAY
  credentials (Claude Code sends the former), and forwarding either would hand the
  caller's key to the backend.
  Workflow injection is **mapping-driven, convention-free** (`_apply_mapping`
  sets `workflow[node].inputs[field]`); a mapping `label` is the param's public
  API name — incoming values are accepted under label OR param, and the
  auto-random seed keys on that effective name (`''` counts as unset);
  `_apply_lora_cascade` drops client LoRAs into free stack slots; `_apply_fixed`
  applies admin pins (the API can't override a pinned `(node,field)`);
  `_apply_bypass` runs LAST (per-backend `bypass` node ids): ComfyUI mode-4 —
  remove each node and reconnect its consumers to the same-typed input (k-th
  same-typed output → k-th same-typed link input, via cached `/object_info`
  slot types; single-link fallback when types are unknown);
  an image slot mapped `on_empty: disable` and left empty prunes a whole DEAD
  BRANCH (`_prune_branch`, not just the loader): the cascade follows consumers that
  lose a **required** input — ComfyUI aborts the prompt on one, so they cannot run
  either — and stops at an **optional** socket, which is the point of the mode
  (measured 2026-08-30 on trellis2-multiview: the back loader's
  `Trellis2PreProcessImage.image` is required and must go, the generator's
  `back_image` is optional and stays). `required` per class comes from the same
  `/object_info` cache the bypass rewiring uses (`_node_type_entry`'s `req`), loaded
  only when such a slot is actually empty; an unknown class stops the cascade
  (pre-cascade behaviour — never guess what a node needs). A branch that would take
  the alias's `output_node` with it fails the job UP FRONT naming the slot, instead
  of submitting a workflow that cannot deliver;
  `suggest_mapping()` is only an auto-detect pre-fill. ComfyUI `/prompt`
  rejections are translated to readable per-node errors (node title, class,
  field, offending request param) via `_comfy_prompt_error`; raw body stays the
  fallback. Artifact delivery: a `/view` 404 means "file absent" (probe on), any
  OTHER status raises — a backend error must never silently shrink a delivery;
  `output_ext` prefers the same-stem sibling but ships the reported file when the
  sibling is missing; `output_cases` fetches a case ONCE and checks the detect
  glob on the result; plain `output_globs` may accompany cases OR an
  `output_node` as unconditional extras (the node's result stays authoritative:
  an empty node still errors, never an extras-only delivery; globs WITHOUT a
  node are the whole delivery). A relative `/view` path keeps its dirs as the
  subfolder (`_view_params`).
  `normalize_delivery` (case mode + chain level, normalize-once flagged) V-flips
  generic texture PNGs and — alias Output option `texture_format: jpeg` —
  transcodes them to JPEG q90 (real alpha keeps PNG; ComfyUI has no JPEG export).
  **Input isolation** (`upload_prefix` on `NormalizedRequest`, `upload_prefix_for`
  + `upload_slot_name`): EVERY uploaded input is named `gw_<job id>[_s1|_s2]_<param>
  .<ext>` — no two jobs share input state, ever. ComfyUI reads an input file at
  EXECUTION time, so a shared name is a corruption window the gateway's one-slot
  cap does NOT close (a poll timeout frees the slot while the prompt runs on) —
  measured 2026-08: a client job was delivered another subject's mesh. A blank
  prefix mints a random one; never add a shared fallback. `_upload_image` RAISES
  on failure (silently keeping the intended name meant running on foreign bytes);
  only `_upload_placeholder` is best-effort, because `gw_placeholder.png` is a
  shared CONSTANT. `_cleanup_uploads` overwrites the job's inputs with that 72-byte
  placeholder after a CLEAN success only (a timed-out prompt may still read them);
  it never raises.
- **`jobs.py`** — generation job store: SQLite metadata + on-disk artifacts under
  `jobs/<id>/<n>.<ext>` (image/video/audio; manifest carries `kind`+`mime`),
  lifecycle `queued→running→done|failed`, TTL pruning. Also persists job **inputs**
  (`set_inputs`: prompt/params/reference images, each with `sha256`+`bytes` like a
  result entry — the job view proves WHICH bytes ran; JSON in meta, no migration)
  and `reconcile_orphans()` (startup:
  mark interrupted `running`/`queued` as failed). Carries `owner`. Reused for
  **background Responses** jobs (task type `response`, result via `complete_json`).
- **`store.py`** — writable SQLite store, the console's source of truth: backends,
  chat aliases, generation aliases (+ workflow_json/mapping/fixed), users (api keys
  **encrypted** via `secret.key`, and therefore re-readable: the user editor
  pre-fills an existing key so it can be copied again — masked, gated by the
  `show_user_keys` setting, default ON, `admin._show_user_keys()`), IP aliases,
  server settings. Seeded once from
  config, then authoritative.
- **`admin.py`** — the `/ui` console (mounted via `admin.register(app)` +
  `add_api_route`, *not* `include_router` — broken in this starlette build;
  callbacks injected via `admin.bind(...)`). Session-gated by `_ui_guard` once
  locked. Tabs in `TABS`; a top tab can group child views via `SUBTABS` +
  `_with_subnav()` (`?sub=` on the parent route, first child = default —
  Playground: Chat | Media | Voice, Jobs & Calls: LLM | Media | Voice,
  Mapping: Chat | Media, Input & Routing: Input | Chat aliases | LLM models |
  Media aliases | Image models | LoRAs); the workflow Mapping editor owns a pasted
  ComfyUI API JSON and offers discovery-fed dropdowns. Mapping's sub-tab is derived
  when `?sub=` is absent (`?edit=`/`?new=` → media, else chat), so the dozens of
  existing action links keep working unchanged and still land in the right tab.
  The Media list groups by `task` in `_TASK_OPTIONS` order (unknown tasks trail
  alphabetically, so a typo stays visible) with a per-group count; the task is the
  header, so the row shows what differs WITHIN a group (backends · mapped params). POST bodies parsed by
  hand (`parse_qs`) to stay `python-multipart`-free.
- **`stats.py`** — optional SQLite (WAL) call log + body store. The dashboard is
  **in the `/ui` Statistic/Routing tabs** (no separate port — the old standalone
  :4001 server was folded into the console). Zero new dependencies — keep it.
  The `calls` row carries the applied `reasoning` control (shown in LLM Calls) and
  the prompt-cache split `cache_read`/`cache_write` — both SUBSETS of
  `input_tokens` (which stays the total the model processed), so
  `input - read - write` is what was billed fresh. Fed by the adapters'
  `_cache_of()` hook (Anthropic: `cache_read_input_tokens` /
  `cache_creation_input_tokens`; OpenAI-shaped: `prompt_tokens_details.
  cached_tokens`) on all four paths — streamed and not, both protocols.
  `cache_trend()` buckets that per backend over 24h and drives the Statistic tab's
  sparkline; a hit rate collapsing while input keeps rising is the signal that a
  long Claude Code session started paying full price again. New columns are added
  by the same `ALTER TABLE` migration list as the earlier ones (an existing prod
  stats.db migrates in place; pre-existing rows read 0, never NULL).
- **`responses_bridge.py`** — pure Responses↔Chat translation functions (no
  gateway state): `responses_to_chat` / `chat_to_responses` / `responses_stream`
  (chat SSE → Responses SSE) and `response_shell()`, the ONE Responses-object
  skeleton every state (completed / stream events / background queued-failed)
  builds on. `main.py` keeps the endpoints, dispatch/parking, and background
  mode; the adapter attaches `resp.parsed_json` so the bridge never re-parses
  the raw body.
- **`anthropic_bridge.py`** — pure Messages↔Chat translation (no gateway state,
  no `main`/`adapters` imports): `messages_to_chat` / `chat_to_messages` /
  `messages_stream` (chat SSE → Anthropic SSE) / `estimate_input_tokens` and
  `message_shell()`. Used ONLY when a non-Anthropic backend serves `/v1/messages`;
  an `anthropic` backend forwards verbatim (see `AnthropicAdapter`). Translation
  policy: drop what is inert (`cache_control`, history `thinking` blocks,
  server-side tools), raise `UnsupportedContent` → 400 where dropping would
  silently answer about content the model never saw (documents/PDFs). Covered by
  `test_anthropic_bridge.py` (stdlib `unittest` — a streaming tool-call bridge fails
  silently rather than crashing). The repo's only other test file is
  `test_prune_branch.py`, for the same reason: a dead-branch prune that cascades one
  node too far or too few surfaces as an aborted generation, not an exception.
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
  **sampling defaults** are folded in two stages whose ORDER is the precedence
  (client > alias > backend; each stage only fills keys that are still absent):
  `_apply_alias_sampling()` in `route()`/`/v1/responses` applies the alias's
  `alias_sampling` (store settings, cached in `main.alias_sampling`), then
  `adapters._prepare()` applies the backend's `sampling_defaults` — there, so it
  is derived PER BACKEND and a failover re-derives it. Text endpoints only (never
  embeddings/audio). Rationale: a backend whose server samples with bare defaults
  (vLLM, no truncation sampler, temp ≈ 1) degenerates into token salad when a
  client sends no sampling params — measured 2026-08-10 on Infermatic/Anubis-70B.
  Also before dispatch, `_normalize_reasoning()` folds the client `reasoning`/`reasoning_effort` control
  to `off|on|None` and stashes it in `body["_reasoning"]`; the adapter strips all
  `_`-prefixed keys and runs `apply_reasoning` **per backend** on a copy of the
  body (so failover re-derives it). `/v1/responses` translates bodies and shares
  `_dispatch_or_park()` too (also parks, and normalizes reasoning incl. the
  Responses `{effort}` shape); `background:true` runs it async via the official
  Responses background mode (see below).
- **Messages** (`POST /v1/messages`, `POST /v1/messages/count_tokens` — the Claude
  Code frontdoor): `_messages_route()` authenticates (`x-api-key` OR `Authorization:
  Bearer` — Claude Code uses the former; both carry a GATEWAY key), folds
  `thinking:{type:enabled|disabled}` into `body["_reasoning"]` (an explicit client
  control beats the per-alias default, as on the chat path; honoured by translated
  backends only) and hands over to the SAME `_dispatch_or_park()`.
  `count_tokens` for a chat backend is answered from the bridge's estimate BEFORE
  dispatch, so sizing a context never queues for an in-flight slot. Deliberately no
  `_apply_alias_sampling()` here: Claude Code sends a complete request, and a
  chat-shaped `min_p` would 400 against Anthropic. Errors are re-shaped to
  `{"type":"error","error":{…}}` — Claude Code reads `error.message` and renders a
  FastAPI `detail` body as blank. Routing filters candidates by `serves_path()`, so
  an Anthropic backend is invisible everywhere else and an alias served only by
  such backends answers `404 … reachable through POST /v1/messages only` instead of
  a misleading 503.
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
  bare-model-id pass-through are **precomputed** (insertion order, no sort);
  `resolve_routes()` evaluates the live flags (healthy/busy/draining) per request and
  applies the scheduler ordering — unpaid before paid, then fastest (`backend_tps` /
  `gen_speed`), unmeasured first. A freed backend takes the waiter the scheduler
  designates for it (`scheduler.designated_taker`: overdue > same type key > oldest;
  `affinity_max_wait_s`, default 120, Server tab). `priority` routes nothing any more —
  it only keeps the backend LIST order stable. Rebuilt by `rebuild_backends()`/`rebuild_virtual_models()` and on every
  discovery model-set change — never mutate `backends`/`virtual_models` outside
  those functions or the index goes stale.

- **Aliases** (`virtual_models`): string (same everywhere) or per-backend dict whose
  value is a model id (old entries may still be `{model, priority}` — `alias_entry()`
  parses that shape, the priority is ignored).
- **Backend prefixing** (`split_backend_prefix`): `<backend>/<model>` pins that
  backend; a bare id/alias goes through the scheduler. `local: true` *also* lists models
  bare (cross-backend implicit alias). `model_prefix` toggles prefixed listing.
- **Concurrency/busy** (`backend_inflight`, `backend_busy`): incremented in
  `dispatch()`/`generate()`, decremented on completion incl. the streamed `finally`.
- **Re-routing onto a returning backend**: waiting work is never pinned to the
  backend it queued for. `refresh_backend` calls `_notify_slot_free()` on DOWN→UP
  and on a model-set change (parked calls re-evaluate); `apply_backend_change` and
  `cancel_drain` do the same; parked gen jobs re-resolve routes every 2 s. The slow
  part was NOTICING, so `health_loop` polls backends concurrently (a sequential loop
  added every dead backend's connect timeout to the cycle) and `fast_probe_loop`
  re-polls only UNHEALTHY backends every `fast_probe_interval_s` (default 3, Server
  tab, 0 = off) while `_capacity_wanted()` — `_parked` non-empty, or a gen park loop
  pinged `_gen_wait_ping()` within 5 s. A TIMESTAMP, not a counter: a cancelled job
  task cannot leave a phantom waiter. `_probing` guards against two concurrent polls
  of one backend. `_run_job` re-points the job row (`jobs.set_backend`) at the
  backend that actually claims it — a parked job routinely lands elsewhere, and a row
  naming the wrong backend sends you reading the wrong ComfyUI's log.
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
  LoRA-backend rather than spilling); a LoRA on no backend is ignored (the normal
  ordering wins); an explicit `backend` force is never overridden. Per-backend LoRA sets
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
`asyncio.create_task` — never raises into the request path. **Refused** calls are
recorded too, by the app-wide `HTTPException` handler (`_rejected_call` →
`_record_rejected`): anything turned away before a backend saw it (no healthy
backend, park timeout, quota, unknown alias, bad key) used to leave NO trace at
all, which is precisely the call you go looking for in LLM Calls. Such rows carry
backend `stats.REFUSED_BACKEND` = `(refused)` (defined THERE, not in `main`, because
the aggregates are what must exclude it), an empty `model` (none was ever resolved),
the status, and the reason as the stored response body. That marker is a call-log
entry only: `summary()` keeps it out of `by_backend`/`by_model` — a pseudo-backend
with 0 tokens/0 cost/0 ms says nothing about any backend, and its empty model splits
the alias into two rows — and reports it as `refused_count`/`refused_24h`, which the
Statistic tab shows as its own card (totals and `by_source` still count it: a refused
call is real traffic from that user). `request.state.gw_dispatched` (set in
`_dispatch_over` once an adapter answered, and in `run_generation` right after
`jobs.create`) prevents a second row when an endpoint re-raises an upstream error —
and `gw_alias`/`gw_body`/`gw_endpoint` carry the context the handler cannot see.
The generation arm of that flag is not cosmetic: `gen_done_or_502` raises AFTER a
job ran, so without it a **failed media job** was logged a second time as
`(refused)`, 0 ms, no backend — a request that was in fact served and failed
(measured 2026-08-25). The invariant: once a job row exists, the JOB owns the
outcome; only refusals BEFORE it (no eligible backend, quota, malformed request)
belong in the call log. `admin._call_kind()` partitions that log into
`voice`/`media`/`llm` so each row has exactly one home — media refusals show under
Media Jobs, not LLM Calls. The same handler renders `/v1/messages` errors in
Anthropic shape, so that form lives in ONE place. Cost from pricing
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
  alias's candidates); **pinned values** (`fixed`) AND **per-node bypass**
  (`bypass`: node ids) are per-backend. A pinned `(node,field)` is authoritative —
  never overridden by an API request param.
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
