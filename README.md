# llm-gateway

An OpenAI-compatible reverse proxy that fans one endpoint out across many
backends — local LLM servers (llama.cpp / llama-swap / vLLM / Ollama …), cloud
APIs (together.ai, OpenAI, OpenRouter …), **and ComfyUI image-generation
servers**. Callers see a single OpenAI endpoint; the gateway handles discovery,
priority routing, failover, virtual aliases, per-backend concurrency, an
optional multi-user layer, call parking, and a built-in management console.

It sits between OpenAI-compatible clients (N8N, LibreChat, Open WebUI, LangChain
code, image clients like anima-verse, …) and a fleet of backends.

---

## Contents

- [Why](#why)
- [Quick start](#quick-start)
- [Configuration](#configuration) — backends, aliases, knobs
- [Authentication & multi-user](#authentication--multi-user) — keys, allow-lists, quotas
- [Routing](#routing) — priority, prefixing, `local`, concurrency, parking
- [Call parking](#call-parking) — queue instead of `503` when busy
- [Reasoning control](#reasoning-control) — thinking on/off per request, per alias, per model×backend
- [Claude Code / Anthropic Messages](#claude-code--anthropic-messages) — `/v1/messages`, mixed Anthropic + open-weight
- [Media generation](#media-generation) — ComfyUI image/video/audio, aliases, mapping, LoRA, jobs
- [The `/ui` console](#the-ui-console)
- [Stats & routing dashboard](#stats--routing-dashboard)
- [Endpoint reference](#endpoint-reference)
- [Try it](#try-it)
- [Running & deploying](#running--deploying)

---

## Why

- **One endpoint for many backends.** Point your tools at one URL; add/remove
  backends without touching clients. Chat, embeddings, the Responses API, *and*
  image generation all go through the same gateway.
- **Auto-discovery.** Each backend's catalog is polled — `/v1/models` for LLMs,
  `/object_info` for ComfyUI (models + installed LoRAs). No manual registry.
- **Strict priority routing + failover.** `priority` is a first-class deployment
  ordering: alias `fast` routes to a local box first, a cloud provider as
  fallback — exactly that, every time, no routing-strategy ceremony.
- **Virtual aliases.** `fast`, `vision`, `translator` map to different real model
  IDs per backend; an alias can even override a backend's priority for itself.
- **Cloud-as-backend.** A per-backend `api_key` wires in any OpenAI-compatible
  provider as just another prioritised backend.
- **Multi-user.** Optional per-user API keys with model/alias/backend allow-lists
  (which also filter what each key sees in `/v1/models`) and monthly cost quotas.
- **Call parking (default).** When every matching backend is busy, the call
  queues until one frees instead of returning `503` — no client change needed.
  Park time is per-alias; async is the standard Responses background mode.
- **Media generation.** ComfyUI workflows exposed as OpenAI image endpoints + a
  native job API — **image, video, and audio** outputs — with a convention-free
  node mapping, dynamic LoRAs, and LoRA-aware backend routing.
- **Built-in console at `/ui`.** Manage backends, aliases, workflow mappings,
  users, server settings; run a chat/media playground; watch jobs, stats, parked
  calls, and the live routing map. Server-rendered, zero JS framework.
- **Hot config reload.** `config.yaml` changes apply live; most management also
  lives in a writable store edited from the console.

---

## Quick start

```bash
git clone https://github.com/KaletoAI/llm-gateway.git
cd llm-gateway
python3 -m venv venv && venv/bin/pip install -r requirements.txt
cp config.example.yaml config.yaml
$EDITOR config.yaml                    # set backends + api_key
venv/bin/uvicorn main:app --host 0.0.0.0 --port 4000   # add --reload for dev
```

Point any OpenAI-compatible client at `http://<host>:4000/v1` with the `api_key`
you set. Open `http://<host>:4000/ui` for the management console.

> `requirements.txt` omits `watchfiles`; it ships transitively with
> `uvicorn[standard]` and powers the hot-reload of `config.yaml`. Keep that extra.

---

## Configuration

`config.example.yaml` is the documented template. Copy to `config.yaml`
(gitignored, hot-reloaded on save). Two things read **only at startup**:
`stats.enabled` and the jobs/stats DB paths.

**Config vs. store.** `config.yaml` is the bootstrap source. Once the console is
used, most state (backends, chat aliases, generation aliases + mappings, users,
server settings) lives in a writable SQLite **store** (`store.db`) which then
becomes the source of truth. The store is seeded once from config and merged
over it; you can run almost entirely from config or almost entirely from the
console — both work.

```yaml
api_key: "sk-change-me"                # master/admin key (see Authentication)
health_check_interval: 30              # seconds between backend liveness polls
log_per_call: true                     # one log line per forwarded request
model_prefix: true                     # list models as <backend>/<model>
# max_concurrent: 1                    # global default in-flight cap per backend

backends:
  - name: local-gpu
    url: http://192.168.1.10:8080      # llama-swap / llama.cpp / vLLM / …
    priority: 1                        # 1 = preferred
    max_concurrent: 1                  # single-slot llama.cpp → one at a time
    # local: true                      # ALSO list its models bare (see below)
  - name: together                     # cloud fallback (OpenAI-compatible)
    url: https://api.together.xyz
    priority: 99
    api_key: "tgp_v1_…"                # sent as Bearer to this backend
    chat_only: true                    # drop non-chat models at discovery
    serverless_only: true              # drop dedicated-endpoint-only models

virtual_models:                        # chat aliases
  "translator": "Aya-Expanse-8B"       # same model on every backend
  "fast":                              # per-backend mapping
    local-gpu: "Qwen3.5-9B"
    together:  "meta-llama/Meta-Llama-3.1-8B-Instruct-Turbo"
  "cheap":                             # per-alias priority override
    local-cpu: { model: "gemma-3-9b-it", priority: 1 }
    local-gpu: { model: "Qwen3.5-9B",    priority: 2 }
```

A backend's value under an alias is normally just the model name; make it
`{model, priority}` to override that backend's priority **for this alias only**.

---

## Authentication & multi-user

Two layers, both optional:

- **Master key** — the top-level `api_key`. Clients send
  `Authorization: Bearer <key>`. Also unlocks the `/ui` console (sign in with an
  admin key). Leave empty to run fully open (bootstrap mode).
- **Per-user keys** — created in the **Users** tab. Each user has its own API
  key (generate one in the form, or paste your own), a role (`user` / `admin`),
  an enabled flag, an optional model **allow-list**, and an optional **monthly
  cost quota**. Calls are attributed to the user (stats source, job owner).

**Bootstrap-open → locked.** With no users *and* no master key, the gateway and
console are fully open. Add an admin user (or set a master key) to lock it down.

**Job ownership.** Generation jobs and background responses are owner-gated:
`GET`/cancel of a job (and its result/input artifacts) is allowed only for its
owner; admin/master see all. Authenticated calls are owned by the user. In
bootstrap-open mode an anonymous caller is owned by its **client IP** (`ip:<addr>`),
so keyless LAN services get a best-effort separation — each sees only its own
jobs. This is convenience, **not** a security boundary (NAT/spoofing); for a real
boundary give each service its own user + key. Legacy/`default`-owned jobs stay
visible to everyone.

### Allow-list (what a key may use — and see)

A user's allow-list can contain any mix of:

| Entry kind | Grants |
|---|---|
| **chat alias** (`fast`) | that chat alias |
| **image alias** (`Qwen`) | that generation alias |
| **backend name** (`together`) | **all** of that backend's models |
| model id (`together/llama-3…` or bare) | that specific model |

An **empty** allow-list = everything allowed (the default). A non-empty list both
**restricts usage** (a disallowed model → `403`) **and filters `/v1/models`** so
the key only sees what it's allowed. This is how you point an image client at the
gateway and have it see just the image aliases instead of the whole 400-model
catalog: give it a key whose allow-list is the image alias(es) (or the ComfyUI
backend), and `GET /v1/models` returns only those. `?type=image` / `?type=chat`
narrows by namespace too.

### Quotas

- **`quota_req_day`** — requests per day (in-memory counter) → `429` when exceeded.
- **`quota_cost_month`** — summed USD cost for the month (from the stats log) →
  blocked when exceeded. Needs stats enabled and priced backends; streaming calls
  count as `0` (no `usage` in stream chunks).

---

## Routing

For each request the gateway walks backends in **priority order** (1 = first) and
takes the first that is (1) enabled, (2) healthy (last discovery poll ok), (3)
**not busy** (below its `max_concurrent` in-flight cap), (4) mapped for the alias
(or exposes the bare/real model), and (5) actually has the resolved model. If
that backend errors on the forward, the remaining matching backends are tried in
order. When every match is busy the call **parks** (queues) by default until a
backend frees, and only `503`s if the park time runs out (below).

### Provider-prefixed model names

With `model_prefix: true` (default), `/v1/models` lists every model as
`<backend>/<model>` so the provider is visible. Input is liberal: a prefixed id
routes to exactly that backend; a bare id or an alias routes by priority. Backend
names never collide with vendor prefixes (`moonshotai/…`), so the leading segment
disambiguates. `model_prefix: false` → legacy bare, de-duplicated listing.

**`local: true`** on a backend *additionally* lists its models bare (alongside
the prefixed id). A bare request then routes by priority across every `local`
backend that serves it — same failover/busy-spill as a virtual alias; shared ids
collapse to one entry. Independent of `model_prefix`.

### Per-backend concurrency cap (`max_concurrent`)

A live per-backend in-flight counter; at/above the cap the backend is **busy** and
skipped, so the request spills to the next backend instead of overloading a slow
one. Match it to real parallelism (`1` for `llama.cpp --parallel 1`; unset for a
cloud API). Missing/`0` = unlimited. The counter is released when the response
**completes** — including when a streamed response finishes, not when headers are
sent. Busy state shows in `/health` and the Routing tab.

### Per-backend model filters

| Flag | Effect (at discovery) |
|---|---|
| `chat_only` | keep only `type == "chat"` models (drops image/video/embedding). Understands Together's `type` and OpenRouter's `architecture.output_modalities`. Backends without those fields (llama-swap, vLLM) are unaffected — so **don't** set it on a backend whose embedding models you want routable. |
| `serverless_only` | keep only models with non-zero pricing (Together's dedicated-only models are `0/0`; on OpenRouter this also drops `:free`). |

### Sampling defaults (`sampling_defaults`)

Some backends sample with bare server defaults when a request carries no sampling
parameters. vLLM without a truncation sampler (`top_p=1`, `top_k=-1`, `min_p=0`)
at temperature ≈ 1 will emit token salad — foreign-script and code fragments
spliced into the text (measured on an FP8 70B: 1.6–3.5 % non-Latin characters,
0 % with `min_p: 0.05` or `temperature: 0.85`). Clients that send nothing —
OpenWebUI, the `/ui` Chat Playground with its fields left blank — hit exactly
that.

Both a **backend** (Backends tab) and a **chat alias** (chat-alias editor) can
carry defaults, filled into a request only for keys the caller did **not** send.
The editors show one input per common sampler — `temperature`, `top_p`, `top_k`,
`min_p`, `repetition_penalty`, `presence_penalty`, `frequency_penalty` — plus a
**more (JSON)** box for anything else a backend understands (`typical_p`,
`stop`, `logit_bias`, …). A decimal comma is accepted; a key that has its own
input is rejected in the JSON box, so a value can never be set twice.

Stored shape (this is also the `config.yaml` form on a backend):

```yaml
# Backend "Infermatic"
sampling_defaults: {temperature: 0.85, min_p: 0.05}
```

**Precedence: client > alias > backend.** With the backend above and an alias
carrying `temperature: 1.0`, a request that sends no sampling parameters runs at
`temperature` 1.0 (alias) plus `min_p` 0.05 (backend); a client sending
`temperature: 0.2` keeps 0.2.

Any key is allowed except `model`, `messages`, `stream`, `stream_options` and
anything starting with `_` — those drive routing, streaming and the reasoning
hand-off, and are rejected when you save. Values may be scalars, lists (`stop`)
or objects (`logit_bias`).

Applies to `/v1/chat/completions`, `/v1/completions` and `/v1/responses` only —
not `/v1/embeddings`, not `/v1/audio/*`, not generation. The backend stage is
derived **per backend**, so a failover uses the values of the backend that
actually serves the call. The forwarded body is what gets logged, so the
**LLM Calls** tab shows exactly which values went out.

### Alias / model-name collisions

Naming an alias the same as a real model id *shadows* that model. `/health`'s
`alias_model_conflicts` and the Routing tab flag every collision, split into
**covered** (in the mapping → still routable) and **shadowed** (hosts the model
but isn't mapped → unreachable by that name) — the latter is the actionable case.

---

## Call parking

When **all** backends that map an alias are busy (at their in-flight cap), the
call is **held in a FIFO queue** until a mapping backend frees (then dispatched)
instead of returning `503`. This is the **default** — no client field needed, so
callers stay plain-OpenAI. A standard request just sees a slightly slower `200`,
or a `503` (with `Retry-After`) if the wait runs out.

- **Park time is per-alias** (`park_s` in the chat-alias editor, or config
  `alias_park`): blank = the global default (`park_timeout_s`, **60 s**, Server
  tab), `0` = parking off for that alias (immediate `503` when busy). `max_parked`
  caps the queue.
- **Fair:** when a slot frees, the oldest waiting call whose alias can use that
  backend is dispatched first (no head-of-line blocking across aliases). Live
  queue is visible in the **Parked calls** panel on the Dashboard.
- **On timeout** the call leaves the queue with a `503` + `Retry-After`.

Applies to `/v1/chat/completions`, `/v1/completions`, `/v1/embeddings`,
`/v1/responses`, and the generation path (a busy ComfyUI backend queues the job
rather than `503`-ing). The distinction "all busy" vs. "no backend at all" is
explicit — a genuine no-backend still `503`s.

**Async LLM requests** don't use a custom field: they follow the official OpenAI
**Responses background mode** — `POST /v1/responses` with `background:true`
returns immediately with `{id, status:"queued"}`; poll `GET /v1/responses/{id}`
(→ `in_progress`/`completed`/`failed`/`cancelled`) and cancel via
`POST /v1/responses/{id}/cancel`. The background worker parks in the same queue.

---

## Reasoning control

One normalized switch turns a thinking model's reasoning **off/on** — regardless
of which mechanism the model actually needs. Clients send a single field:

```jsonc
{ "model": "tool", "reasoning": "off", "messages": [...] }   // "off" | "on" | "auto"
```

`"auto"` (or omitting the field) leaves the request untouched. The OpenAI
`reasoning_effort` field works as an alias (`minimal` → off, anything else → on),
and `/v1/responses` also accepts the `reasoning: {effort}` object shape.

**Rules decide the mechanism.** The switch is translated per (model × backend)
by an ordered rule list (UI → **Reasoning** tab; stored, hot). The first enabled
rule whose **model glob** matches the real model and whose **backend set**
contains the serving backend wins; its *adapter* does the work:

| Adapter | What it does |
|---|---|
| `enable_thinking` | sets `chat_template_kwargs: {enable_thinking: bool}` (vLLM; llama.cpp with `--jinja`) |
| `reasoning_effort` | sets `reasoning_effort` (off → `minimal`, on → `high`; overridable per rule) |
| `nothink_token` | appends a token (default `/nothink`) to the last user message |
| `prefill` | appends a closed `<think>…</think>` assistant turn |
| `none` | no mechanism — reported as `unsupported` |

No matching rule → the request is forwarded unchanged and the control is
reported as `unsupported` — **it never fails a call**. What was actually applied
comes back in the **`x-reasoning-control`** response header (e.g. `off:prefill`,
`on:noop`, `unsupported`) and is logged per call in the **LLM Calls** tab.

**Per-alias default.** A chat alias can carry a reasoning default (chat-alias
editor → `reasoning: auto|on|off`), applied when the client sends nothing — so
`tool` (off) and `tool-thinking` (auto) can point at the **same backend and
model**. An explicit client `reasoning` field always wins.

**Thinking output on `/v1/responses`.** Models that stream their thinking in the
`reasoning` delta channel are translated to Responses-API reasoning events
(`response.reasoning_summary_text.delta`) and a `reasoning` output item —
`output_text` stays answer-only; clients that don't know reasoning events simply
ignore them.

---

## Claude Code / Anthropic Messages

The gateway speaks Anthropic's Messages protocol as a **frontdoor**, so
[Claude Code](https://claude.com/claude-code) can run through it — mixing Claude
models with open-weight models behind gateway aliases, while keeping routing,
parking, failover, quotas and stats:

```bash
export ANTHROPIC_BASE_URL=http://gateway:4000
export ANTHROPIC_AUTH_TOKEN=<your gateway key>   # or ANTHROPIC_API_KEY (sent as x-api-key)
export ANTHROPIC_MODEL=claude-sub                # any gateway alias
claude
```

Two kinds of backend can serve `POST /v1/messages`:

| Backend | What happens | Why |
|---|---|---|
| `type: anthropic` | **verbatim passthrough** to `api.anthropic.com` | `cache_control` breakpoints, thinking signatures and fine-grained tool streaming survive untouched — without cache breakpoints, Claude Code re-reads the whole context at full price every turn |
| `type: openai` (OpenRouter, LocalAI, vLLM …) | **translated** by `anthropic_bridge.py` — Messages → chat and back, streaming included | one alias can list both, so a failing Anthropic backend fails over to an open-weight model |

Nothing on the passthrough path rewrites body or stream: the SSE normalizer,
the reasoning rewrite and `sampling_defaults` are all skipped there. On the
translated path they apply as usual, and Claude Code's `thinking: {type:
enabled}` maps onto the gateway's [reasoning control](#reasoning-control) so an
open-weight model thinks when asked to.

### Prompt caching and cost

Claude Code marks cache breakpoints with `cache_control`, and they are what keeps
a long session cheap — without them the whole context is billed again every turn.

- **Passthrough** (`type: anthropic`): the request body reaches Anthropic
  byte-for-byte, with the alias resolved to the real model id as the only change.
  Breakpoints, thinking signatures and tool streaming all survive.
- **Translated** (`type: openai`): breakpoints are dropped by default, because a
  chat backend has no field for them. That costs nothing for local models (no
  token billing) or OpenAI models (they cache automatically) — but it *does* cost
  money on **OpenRouter**, which forwards `cache_control` to Anthropic and Gemini
  models. Set `prompt_cache: true` on such a backend and the breakpoints are
  carried into the translated body. It stays opt-in because they turn a message
  into a content-part list, which a strict server may reject.

`POST /v1/messages/count_tokens` is passed through to Anthropic and answered from
an estimate for chat backends (they have no such endpoint).

**Point Claude Code's background model somewhere cheap** — it runs a small model
for titles and summaries:

```bash
export ANTHROPIC_SMALL_FAST_MODEL=cheap          # a gateway alias on a local/OpenRouter model
```

### Licence boundary (please keep it)

A Claude **subscription** token (`claude setup-token`) is licensed for *your own*
use of Claude Code — not for re-serving Claude as a general-purpose API to other
clients or other people. This is enforced in the gateway, not just documented:

- an `anthropic` backend is routable **only** on `/v1/messages` (`serves_path()`);
  it can never be reached through `/v1/chat/completions`, `/v1/responses`,
  `/v1/embeddings` or the console's Playground, whatever alias points at it,
- an alias served exclusively by Anthropic backends is hidden from the Playground
  and answers other endpoints with `404 … reachable through POST /v1/messages only`,
- the Backends tab states the same rule at the credential field.

The reverse direction (Claude models behind `/v1/chat/completions`) is
deliberately **not** built — that is precisely the path that would turn a
subscription into an API. If you have a paid API key from console.anthropic.com,
set the backend's auth mode to `api key`; the same endpoint restriction still
applies.

### Backend setup

```yaml
backends:
  - name: anthropic-sub
    type: anthropic
    url: https://api.anthropic.com
    api_key: <output of `claude setup-token`>
    auth_mode: subscription        # → Authorization: Bearer + OAuth beta header
    # auth_mode: api_key           # → x-api-key (console.anthropic.com key)
    models: [claude-sonnet-5]      # fallback list; discovery tries GET /v1/models first
    priority: 1
```

Discovery asks Anthropic's `GET /v1/models` and falls back to `models:` when the
token isn't allowed there — so a 401 on that endpoint doesn't take the backend
down. Then map an alias to it (console → **Input & Routing → Chat aliases**, or
`virtual_models`) and hand that alias to Claude Code as `ANTHROPIC_MODEL`.

---

## Media generation

A ComfyUI backend speaks a different protocol, so it declares `type: comfyui`.
Discovery is via `/object_info` (checkpoints/UNETs/VAEs **and** installed LoRAs);
dispatch submits a parametrised workflow, polls `/history`, and fetches whatever
artifacts it produced — **image, video (e.g. SaveVideo), or audio** — with each
artifact's kind/mime carried through the API and rendered in the console.

```yaml
backends:
  - name: gpu-3090
    type: comfyui
    url: http://192.168.1.20:8188
    priority: 1
    max_concurrent: 1        # one generation at a time on this GPU
    # poll_interval: 1.0     # seconds between /history polls
    # max_wait: 600          # hard cap for a single generation
    # read_timeout: 60       # per-HTTP-request read timeout (hung read → failover)
    # disconnect_grace: 30   # tolerated unreachability before failing over
    # stuck_after_s: 90      # executor watchdog: pending prompts + idle executor
    #                        # this long → backend goes down (see below)
    # auto_restart: true     # opt-in: restart the ComfyUI service when stuck
    # restart_cooldown_s: 600  # at most one auto-restart per this window
```

**Executor watchdog.** ComfyUI's HTTP server keeps answering even when its
prompt executor has died (e.g. after a CUDA fault) — prompts then pile up in
`queue_pending` while nothing runs, and every generation runs into its poll
timeout. The health loop therefore also checks `/queue`: the same head prompt
pending with an idle executor across ≥2 checks and ≥`stuck_after_s` (default
90 s) marks the backend **down** (`exec_stuck: true` in `/health`, "executor
stuck" badge in the Backends tab). The ⟳ action there — or `auto_restart` —
restarts the service via the **ComfyUI-Manager** reboot endpoint (requires the
Manager extension and a systemd unit with `Restart=always`); auto-restart fires
at most once per `restart_cooldown_s` (default 600 s). A GPU that fell off the
bus needs a host reboot instead — the backend then simply stays down.

### Generation aliases + mapping

A **generation alias** (the `model` of a generation request) maps to an ordered
list of candidate backends. Each candidate carries the **workflow** (a ComfyUI
**API-format JSON**) and a **mapping** that binds logical params to concrete
workflow nodes+fields:

```yaml
image_models:
  "flux":
    - backend: gpu-3090
      task: text2img
      workflow_json: { … }          # the ComfyUI API JSON (owned by the gateway)
      mapping:
        prompt:          { node: "6", field: "text" }
        width:           { node: "5", field: "width" }
        seed:            { node: "3", field: "seed" }
      fixed:                          # pinned node values (models, switches, …)
        - { node: "4", field: "unet_name", value: "flux1-dev.safetensors" }
```

The mapping is **convention-free** — it works with any workflow regardless of node
naming. (An auto-detect heuristic pre-fills it for templated workflows; the
explicit mapping always wins.) In practice you author all this in the **Mapping**
tab of the console rather than by hand: paste the ComfyUI API JSON, the gateway
owns it, auto-suggests the mapping, and gives you discovery-fed dropdowns.

Key mapping concepts:

- **Workflow + mapping are backend-independent** (shared across an alias's
  candidates). Only **Pinned values** are per-backend (one tab per backend), so
  the same alias can use a different checkpoint on each GPU while looking
  identical from outside. A request param that targets a pinned node/field is
  **ignored** — a pin is authoritative; the API can't override it.
- **Image input slots** (a `LoadImage` / `LoadImageMask` node) become file-upload
  request fields. By default an unfilled slot gets an 8×8 placeholder; mark a slot
  **`required`** (Mapping checkbox) to leave it empty instead so ComfyUI errors
  clearly when a needed image/mask is missing (inpaint).
- **Numeric fields** (strength, steps, cfg) render with `min`/`max`/`step` pulled
  live from `/object_info`.

### LoRAs

LoRAs are first-class:

- **Pinned LoRA** — a `fixed` binding on a LoRA-loader slot; the API can't change it.
- **Dynamic LoRAs** — the client sends `lora_1`, `lora_2`, … (+ optional
  `strength_N`). The gateway **cascades** them into the next *free* slots of the
  workflow's LoRA stack, never overwriting a pinned/occupied slot — so a client
  needn't know which slot is reserved.
- **LoRA-aware routing** — a backend that lacks a requested LoRA is dropped from
  the candidate set (decided over all candidates incl. busy, so the request parks
  for the backend that has it rather than spilling to one that doesn't). A LoRA
  installed on no backend is ignored (priority decides). An explicit `backend`
  pin is never overridden.
- **`GET /v1/generations/{alias}/loras`** returns the LoRA filenames valid for an
  alias (the union installed across its backends) — for building a correct picker.

### Jobs & TTL

Every generation is a **job**: SQLite metadata + on-disk artifacts under
`jobs/<id>/<n>.<ext>` (**image, video, or audio** — the artifact's kind/mime flow
through the API and the console), lifecycle `queued → running → done|failed`,
retrievable by id until its TTL (default 24 h), then pruned. The job also keeps
its **inputs** (prompt, params, reference images) so it stays inspectable in the
**Media Jobs** tab. A running job can be cancelled (`POST /v1/jobs/{id}/cancel` or the ✕ button),
which interrupts the ComfyUI prompt to free the GPU. On a restart, any job left
`running`/`queued` is reconciled to `failed`.

### Two ways to call it

- **OpenAI Images API** (for OpenAI image clients):
  - `POST /v1/images/generations` — JSON, text→image. Bonus: LocalAI-style
    `ref_images` (base64/URL list). Extra keys pass through as workflow params.
  - `POST /v1/images/edits` — multipart; `image` file(s) + the OpenAI `mask` field
    map positionally onto the workflow's image slots. `response_format` = `url`
    (job-result URL, needs the Bearer key to fetch) or `b64_json` (inline).
  These are **synchronous** and block until the image is ready (and **park** if
  the backend is busy rather than `503`-ing).
- **Native job API** — `POST /v1/generations` with `{model, prompt, mode, params}`.
  `mode: "async"` returns `202 {job_id}`; poll `GET /v1/jobs/{id}` and fetch
  `GET /v1/jobs/{id}/result/{n}`. `mode: "sync"` blocks and returns inline.
  Files ride along in their own fields, keyed by param or label: `images:
  {param: base64|data-URI|URL}` for image slots, `files: {param: …}` for any
  other file input (a mesh to shrink/rig). A `files` entry is uploaded into the
  input dir of whichever backend runs the job — after parking and across
  failover — and the param gets that file's absolute path, so a client never
  needs a path on a backend. The bytes are not kept as a job input.
  Unlike `params`, `files` is strict: unknown key or unreadable value → `400`,
  over 64 MB → `413`.

**Input isolation (guarantee).** Every file the gateway uploads into a backend
(`images`, `files`, chain hand-off meshes) is named per job —
`gw_<job id>_<param>.<ext>` — so **no two jobs ever share input state**, not
across aliases, not across clients, not on the same backend. This is a
correctness requirement, not tidiness: ComfyUI opens an input file when the
prompt *executes*, not when it is submitted, so a name two jobs can both write
is a window in which one job's reference image is silently swapped for
another's. An upload that fails now **fails the job** instead of running on
whatever bytes were already there. After a clean success the job's input files
are overwritten with a 72-byte placeholder (ComfyUI has no delete-input API), so
they do not accumulate; after a timeout or cancel they are left alone, because
the prompt may still be running. The one deliberately shared input file is
`gw_placeholder.png`, the 8×8 filler for empty slots — its content is constant,
so overwriting it can mix nothing up. Job inputs are recorded with their
**sha256** (`input_images[].sha256`, same as `results[]`), so a client can prove
which bytes a delivered artifact was made from.

See [docs/anima-versa-integration.md](docs/anima-versa-integration.md) for a full
client-integration walkthrough.

---

## The `/ui` console

A server-rendered console mounted at `/ui` (sign in with an admin key once
locked). Tabs:

| Tab | What |
|---|---|
| **Dashboard** | live per-backend status + in-flight, parked calls, media-job counts/recent, recent LLM calls |
| **Server** | runtime + restart-required settings (API key, caps, park time/queue, stats/jobs, TTL/prune) |
| **Backends** | add/edit/remove backends (LLM + ComfyUI) |
| **Input** | what clients can call — chat aliases, generation models, endpoints |
| **Routing Overview** | the live alias→backend map + collisions (searchable) |
| **Mapping** | register a ComfyUI workflow, wire its node mapping, pin values; chat-alias editor (per-alias `park_s` + reasoning default) |
| **Reasoning** | the normalized-thinking rule list (model glob × backend set → adapter) + test resolver |
| **Playground** | one tab, sub-tabs **Media** (generation via `POST /v1/generations` — image/video/audio, upload refs), **Chat** (chat completion through `/v1/chat/completions`) and **Voice** (TTS via `POST /v1/audio/speech`, inline player + download) — all as **real API clients** (auth, routing, parking, stats all apply) |
| **Media Jobs** | list + detail of generation jobs (inputs + outputs, within TTL) |
| **LLM Calls** | per-call history with stored request/response bodies |
| **Statistic** | the call-stats dashboard (search, aggregates, drilldown) |
| **Users** | multi-user keys, allow-lists, quotas, IP aliases |

---

## Stats & routing dashboard

Opt-in SQLite call log, surfaced in the **Statistic** and **Routing** tabs of the
console (no separate port — the old standalone dashboard was folded into `/ui`).
Every call records timestamp, duration, backend, source, alias, model, endpoint,
HTTP status, tokens, and USD cost.

```yaml
stats:
  enabled: false        # read at startup only — toggling needs a restart
  db_path: stats.db
  retention_days: 0     # 0 = keep forever; else prune older rows hourly
```

- **Cost** comes from each backend's pricing (cached at discovery, normalised to
  USD/million tokens — Together's per-million and OpenRouter's per-token schemas).
  Local backends → 0.
- **Source** is the authenticated user, else the `X-Source` header, else client IP
  (IP aliases give those friendly names; reverse-DNS is auto-resolved).
- **Streaming** calls record real tokens when the backend honors
  `stream_options.include_usage` (requested automatically), else `0`.
- The applied **reasoning control** is logged per call (LLM Calls tab column).
- Recent calls store the full request/response body (large/binary bodies on disk),
  viewable per-call, pruned with the same retention.

---

## Endpoint reference

### OpenAI-compatible

| Method | Path | Notes |
|---|---|---|
| `GET` | `/v1/models` | catalog filtered by the caller's allow-list; `?type=chat\|image` |
| `GET` | `/v1/models/{id}` | single-model lookup |
| `POST` | `/v1/chat/completions` | chat; priority + failover; streaming; parking; `reasoning: off\|on\|auto` |
| `POST` | `/v1/completions` | completions; same routing |
| `POST` | `/v1/embeddings` | embeddings; same routing |
| `POST` | `/v1/audio/speech` | TTS / voice cloning; same routing; binary audio passthrough (WAV …); `voice` + `params.ref_text` forward verbatim, per-alias voice defaults fill them in |
| `POST` | `/v1/responses` | Responses API ↔ chat bridge; streaming; parking; `background:true` (async) |
| `GET` | `/v1/responses/{id}` | poll a background response (queued→…→completed/failed/cancelled) |
| `POST` | `/v1/responses/{id}/cancel` | cancel a background response |
| `POST` | `/v1/images/generations` | text→image (sync); may return a video/audio URL for such aliases |
| `POST` | `/v1/images/edits` | multipart image+mask edit (sync) |

### Anthropic-compatible

| Method | Path | Notes |
|---|---|---|
| `POST` | `/v1/messages` | [Claude Code frontdoor](#claude-code--anthropic-messages); verbatim on `anthropic` backends, translated on chat backends; streaming; parking; auth via `x-api-key` **or** `Authorization: Bearer` |
| `POST` | `/v1/messages/count_tokens` | native upstream count, estimated for chat backends |

### Voice cloning & the reference library

**Backend constraint (measured, not assumed):** LocalAI has **no upload API**, and
cloning-capable TTS models (e.g. `qwen3-tts-cpp-customvoice`) read `voice` strictly
as a **local file on the backend host** — base64/data-URIs are ignored and URLs are
treated as file names. (`omnivoice-cpp` ignores `voice` entirely — it never clones.)

The gateway therefore keeps a **voice reference library** (Playground → Voice):

- **Upload a WAV once** — the master copy lives on the gateway (`voiceref/`, kept
  out of git and deploys). Listen, re-ship or delete entries from the panel.
- An empty *ref text* is **auto-transcribed** by the gateway's local faster-whisper
  (CPU; `whisper_model` setting, default `small`). A backend serving a `whisper*`
  model is used as fallback.
- The file is **shipped via scp to every configured target** (one per LocalAI host
  that serves a cloning model — routing/failover may pick any of them). A target is
  `user@host:/abs/host/dir` — the **host-side** dir, e.g. the source of the docker
  bind mount (`/root/localai/models/voices`). Separately, *voice dir (model view)*
  is the path **as the model sees it** (the container path, e.g. `/models/voices`);
  that single path goes into `voice`, so it must be identical on every host.
  One-time setup per host: `ssh-copy-id` from the gateway host (root login via
  password is usually disabled — `PermitRootLogin prohibit-password`; append the
  gateway's `/root/.ssh/id_ed25519.pub` to the host's `authorized_keys` instead).
- Use an entry as `voice: "lib:<name>"` — API body, playground picker, or an
  alias's voice default; the gateway substitutes the shipped path + ref text
  (explicit client fields always win). Not-yet-shipped entries return a clear 409.

### Native generation + jobs

| Method | Path | Notes |
|---|---|---|
| `POST` | `/v1/generations` | run a generation alias (sync or `mode:"async"`); per-field reference images via `images: {param: base64\|URL}`, other file inputs (meshes) via `files: {param: base64\|URL}` |
| `GET` | `/v1/generations/{alias}/loras` | LoRAs valid for an alias |
| `GET` | `/v1/jobs/{id}` | job status + results |
| `GET` | `/v1/jobs/{id}/result/{n}` | a result artifact (owner-gated) |
| `GET` | `/v1/jobs/{id}/input/{n}` | a stored reference image (owner-gated) |
| `POST` | `/v1/jobs/{id}/cancel` | cancel a queued/running job (interrupts ComfyUI) |

### Other

| Method | Path | Notes |
|---|---|---|
| `GET` | `/health` | per-backend health/model/priority + busy/inflight + conflicts |
| `*` | `/ui/**` | the management console |

Every proxied LLM response carries **`x-gateway-backend`** (which backend served
the call) and, when a reasoning switch was requested, **`x-reasoning-control`**
(what was actually applied).

### Responses API bridge

Clients on LangChain.js (N8N's AI Agent, …) call `/v1/responses`; most backends
only speak `/v1/chat/completions`. The gateway translates request
(`input`/`instructions`/`tools` → `messages`/system/tool schema) and response
(`choices[0].message` → `output[…]`, token field renames) transparently, and
routes through the same dispatch/parking path as chat. `stream: true` is
supported (chat SSE → Responses SSE events). **`background: true`** runs the
request asynchronously per the official OpenAI pattern: it returns immediately
with a `queued` response object; poll `GET /v1/responses/{id}` until a terminal
state and cancel via `POST /v1/responses/{id}/cancel`. The background worker
parks in the shared queue (longer async window) — so long-running or busy
requests never time out the client connection. Thinking-model output arrives as
Responses-API **reasoning items/events** (see [Reasoning control](#reasoning-control)).

---

## Try it

```bash
KEY=sk-change-me ; B=http://localhost:4000

# List models (filtered by your key's allow-list)
curl $B/v1/models -H "Authorization: Bearer $KEY"

# Chat through an alias
curl $B/v1/chat/completions -H "Authorization: Bearer $KEY" \
  -H "Content-Type: application/json" \
  -d '{"model":"fast","messages":[{"role":"user","content":"hi"}]}'

# Embeddings
curl $B/v1/embeddings -H "Authorization: Bearer $KEY" \
  -H "Content-Type: application/json" \
  -d '{"model":"embedding","input":["hallo welt"]}'

# Text→image (sync, inline base64)
curl $B/v1/images/generations -H "Authorization: Bearer $KEY" \
  -H "Content-Type: application/json" \
  -d '{"model":"flux","prompt":"a red apple","size":"1024x1024","response_format":"b64_json"}'

# LoRAs valid for an alias
curl $B/v1/generations/flux/loras -H "Authorization: Bearer $KEY"

# Backend health snapshot
curl $B/health
```

---

## Running & deploying

`llm-gateway.service` is an example systemd unit (assumes `/opt/llm-gateway` with
`venv/` next to `main.py`):

```bash
sudo install -m 0644 llm-gateway.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now llm-gateway
journalctl -u llm-gateway -f
```

`deploy.sh` is an rsync-over-SSH helper (`DEPLOY_HOST=root@host ./deploy.sh`):
syncs code (excluding `config.yaml`, `venv/`), installs requirements in a remote
venv, syncs the systemd unit, restarts.

> **Secrets & data never to commit:** `config.yaml`, `store.db` (+ `secret.key` —
> they travel together, keys encrypted at rest), `stats.db*`, `jobs.db*`,
> `jobs/`, `*.key`. All gitignored.

## License

MIT — see [LICENSE](LICENSE).
