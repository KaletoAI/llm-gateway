# llm-gateway

A small OpenAI-compatible proxy that fans out across multiple local LLM
backends (llama.cpp / llama-swap / vLLM / Ollama / …) and cloud APIs
(together.ai, OpenAI, OpenRouter …), with auto-discovery, priority routing,
virtual model aliases, and failover.

It sits between OpenAI-compatible clients (N8N AI Agent, LibreChat, Open
WebUI, custom LangChain code, …) and a fleet of backends, so callers see a
single OpenAI endpoint and the gateway handles the routing.

## Why

- **One endpoint for many backends.** Point N8N / your tools at one URL;
  add/remove backends in YAML without touching clients.
- **Auto-discovery.** Each backend's `/v1/models` is polled; no manual model
  registry to maintain.
- **Strict priority routing across backends sharing the same alias.**
  Unlike LiteLLM's `fallbacks` (which maps one *model name* to another
  *model name* on failure), the gateway treats `priority` as a
  first-class deployment ordering. One alias `fast` can route to a
  local llama.cpp box first and a cloud provider as fallback — and
  that ordering is exactly what runs, every time, with no routing-
  strategy ceremony (rpm weights, latency routing, cooldowns) to
  configure.
- **Virtual models.** Aliases like `fast`, `vision`, `translator` map to
  different real model IDs per backend. Swap the underlying model without
  changing client code. An alias can also **override a backend's priority for
  itself only** — so `cheap` can prefer the CPU box even though the GPU box is
  globally #1.
- **Cloud-as-backend.** Per-backend `api_key` lets you wire in
  OpenAI-compatible cloud providers (together.ai, OpenAI, OpenRouter,
  DeepInfra, …) as just another backend with its own priority.
- **Hot config reload.** `config.yaml` changes are picked up live; no
  restart needed.
- **Optional call stats + routing view.** SQLite-backed per-call log + minimal
  HTML dashboard on a separate port, with two tabs: **Stats** (backend, source,
  model, tokens, duration, USD cost from each backend's published pricing) and
  **Routing** (a searchable live map of how every alias and discovered model
  resolves, by priority, plus alias/model-name collision warnings). Off by
  default. Zero new dependencies.

## Quick start

```bash
git clone https://github.com/KaletoAI/llm-gateway.git
cd llm-gateway
python3 -m venv venv && venv/bin/pip install -r requirements.txt
cp config.example.yaml config.yaml
$EDITOR config.yaml                    # set backends + api_key
venv/bin/uvicorn main:app --host 0.0.0.0 --port 4000
```

Then point any OpenAI-compatible client at `http://<host>:4000/v1` using the
`api_key` you set in `config.yaml`.

## Configuration

`config.example.yaml` is the documented template. Copy to `config.yaml`
(which is gitignored) and edit. The file is hot-reloaded on save.

```yaml
api_key: "sk-change-me"                # client-side gateway auth (optional)
health_check_interval: 30

backends:
  - name: local-gpu
    url: http://192.168.1.10:8080      # llama-swap / llama.cpp / vLLM / …
    priority: 1
  - name: local-cpu
    url: http://192.168.1.11:8080
    priority: 2
    # enabled: false                    # take out of rotation
  - name: together                      # cloud fallback
    url: https://api.together.xyz
    priority: 99
    api_key: "tgp_v1_…"                 # injected as Bearer to this backend
    chat_only: true                     # filter out image/video/embedding models
    serverless_only: true               # filter out dedicated-endpoint-only models
  - name: openrouter                    # another cloud fallback
    url: https://openrouter.ai/api      # /api only — the gateway appends /v1/…
    priority: 98
    api_key: "sk-or-v1-…"
    chat_only: true                     # drop image-/audio-output-only models
    serverless_only: false              # keep :free models (true = paid-only)

virtual_models:
  "translator":  "Aya-Expanse-8B"       # same model on every backend
  "fast":                                # per-backend mapping
    local-gpu:   "Qwen3.5-9B"
    local-cpu:   "gemma-3-9b-it"
    together:    "meta-llama/Meta-Llama-3.1-8B-Instruct-Turbo"
  "cheap":                               # per-alias priority override
    local-cpu:                           #   prefer the CPU box for this alias…
      model:     "gemma-3-9b-it"
      priority:  1
    local-gpu:                           #   …even though local-gpu is global #1
      model:     "Qwen3.5-9B"
      priority:  2
  "embedding":                           # embedding alias → bge-m3 on one host
    local-gpu:   "bge-m3"                #   (host must NOT set chat_only)
```

A backend's value under an alias is normally just the model name. Make it an
object `{model, priority}` to override that backend's priority **for this alias
only** — handy when the globally-preferred backend isn't the one you want for a
specific alias. Backends without an override keep their global priority (same
numeric scale), so you only need to annotate the ones you're reordering.

### Provider-prefixed model names

With `model_prefix: true` (default), `/v1/models` lists every model prefixed
with its backend name — `together/moonshotai/Kimi-K2.5-fp4`,
`openrouter/nvidia/nemotron-3-ultra-550b-a55b`, `local-gpu/qwen3.5-9b` — so you
can tell at a glance which provider a model comes from (handy once two cloud
backends both expose overlapping catalogs). The prefix is stripped again before
the request is forwarded upstream.

Input is liberal: a prefixed id (`openrouter/…`) routes to exactly that
backend; a bare id or a virtual alias (`fast`) still routes by priority across
all backends as before. Backend names never collide with vendor prefixes
(`moonshotai/`, `nvidia/`, …), so the leading path segment disambiguates
cleanly. Set `model_prefix: false` for the legacy bare, de-duplicated listing.

### How routing picks a backend

For each incoming request, the gateway walks backends in priority order and
takes the first one that:

1. is enabled,
2. is currently healthy (last poll of `/v1/models` succeeded),
3. is mapped for this alias in `virtual_models` (or the model name is a
   real model that backend exposes — direct, un-aliased requests work too),
4. has the resolved real model in its model list.

If that backend errors during the actual forward, the remaining matching
backends are tried in order.

### Per-backend model filters

Two optional boolean flags on a backend filter its model list at discovery time:

| Flag | Effect |
|---|---|
| `chat_only`       | Keep only models where `type == "chat"` (skips image/video/embedding/transcribe). Backends without a `type` field on their models (llama-swap, vLLM, …) are unaffected. |
| `serverless_only` | Keep only models with non-zero pricing. Designed for Together.ai, where dedicated-endpoint-only models have `0/0` pricing and would fail at request time. On OpenRouter this drops the `:free` catalog — leave it `false` if you want the free models. |

`chat_only` understands both Together's `type` field and OpenRouter's
`architecture.output_modalities` (dropping image-/audio-only-output models).
Backends exposing neither (llama-swap, vLLM) are unaffected.

Both default off. Most useful when bridging a cloud provider that returns a mixed catalog (chat + image + dedicated-only + …) and you only want chat-completions-routable models exposed.

### Call stats + dashboard

Opt-in SQLite call log + minimal HTML dashboard on a separate port (default
4001). When enabled, every call is recorded with: timestamp, duration,
backend, source, alias, real model, endpoint, HTTP status, input/output
tokens, and USD cost.

```yaml
stats:
  enabled: false        # default off
  port: 4001
  bind: "0.0.0.0"
  db_path: stats.db
  retention_days: 0     # 0 = unlimited; otherwise prune older rows hourly

log_per_call: true      # set false when using stats to keep the log clean
```

- **Cost**: computed from each backend's pricing metadata, cached at discovery
  time and normalized to USD per million tokens. Two upstream schemas are
  understood: Together.ai's `pricing.input` / `pricing.output` (numbers, already
  per-million) and OpenRouter's `pricing.prompt` / `pricing.completion`
  (strings, per single token — scaled up ×1e6). Local backends (llama-swap,
  llama.cpp, vLLM) don't expose pricing → cost = 0.
- **Source**: defaults to the client IP. Override per-call by sending an
  `X-Source: my-workflow-name` header to tag, e.g., individual N8N
  workflows.
- **Auth**: none. Bind to `127.0.0.1` and put behind a reverse proxy if
  you need access control.
- **Hot-reload**: stats `enabled` is read at startup only; toggling it
  requires a full service restart.
- **Streaming requests** are recorded but with `0` tokens (most backends
  don't include `usage` in stream chunks). Use non-streaming if you want
  accurate per-call cost.

The dashboard has two tabs:

- **Stats** — the call log summaries above (auto-refreshes every 30 s).
- **Routing** — a live map of how every alias and discovered model resolves:
  each alias's backends in effective-priority order (with a badge when a
  per-alias priority override applies, and whether each route is currently
  routable / backend-down / model-missing), every discovered model id and the
  hosts that serve it by priority, and a **collisions** panel. There's a search
  box that filters aliases, models, and hosts client-side.

**Alias / model-name collisions.** Naming an alias the same as a real model id
*shadows* that model: a bare request for the name routes only via the alias
mapping (the pass-through to the real model is disabled), and `<backend>/<name>`
fails on any backend the alias doesn't map. The Routing tab (and `/health`'s
`alias_model_conflicts`) flag every collision, splitting hosts into **covered**
(in the mapping → still routable) and **shadowed** (host the real model but
aren't mapped → unreachable by that name). Shadowed hosts are the actionable
case — add them to the mapping or rename the alias. A collision with no shadowed
hosts (e.g. one id mapped across exactly the backends that serve it) is
intentional and harmless.

### Per-backend `api_key`

When a backend has `api_key`, the gateway sends `Authorization: Bearer
<key>` on both the health-check poll and forwarded chat/completion
requests. Anything OpenAI-compatible works — together.ai, OpenAI, OpenRouter,
DeepInfra, Groq, Fireworks, and similar.

This turns the gateway into a uniform OpenAI-style entrypoint for tools that
otherwise can't talk to a given provider directly.

### Client-side `api_key`

The top-level `api_key` is the *client-facing* gateway auth. Clients send
`Authorization: Bearer <that-key>`. Leave empty/unset to disable auth.

## Endpoints

| Method | Path | Description |
|---|---|---|
| `GET`  | `/v1/models` | All real models on healthy backends + virtual aliases |
| `GET`  | `/v1/models/{id}` | Single-model lookup (some clients verify before calling) |
| `POST` | `/v1/chat/completions` | OpenAI chat, routed by priority + failover; streaming supported |
| `POST` | `/v1/completions` | OpenAI completions, same routing |
| `POST` | `/v1/embeddings` | OpenAI embeddings, same priority routing + failover. Routes to whichever backend serves the requested model (set `chat_only: false` on that backend so its embedding models stay in discovery) |
| `POST` | `/v1/responses` | OpenAI Responses API, bridged to `/v1/chat/completions` on the backend (request + response translated transparently incl. tool-calls; non-streaming) |
| `GET`  | `/health` | Per-backend health/model/priority snapshot + virtual_models dump + alias/model-name conflicts |

### Responses API bridge

Clients using LangChain.js (N8N's AI Agent and similar) call `/v1/responses`
by default. Most local backends (llama-swap / llama.cpp / vLLM) and even
Together.ai only speak `/v1/chat/completions`. The gateway translates
between the two transparently:

- Request: `input` / `instructions` / `tools` / `function_call` items →
  `messages` / system prompt / nested-tool schema / assistant `tool_calls` /
  `tool` messages.
- Response: `choices[0].message` → `output[type=message|function_call]`,
  with `usage.prompt_tokens` etc. renamed to `input_tokens` / `output_tokens`.
- `stream: true` is silently downgraded to a non-streaming call. SSE
  event-stream translation isn't implemented yet.

### Embeddings

`/v1/embeddings` uses the same priority routing + failover as chat: the
request routes to whichever healthy backend serves `model` (bare id, virtual
alias, or `<backend>/<model>`). Embedding responses report only
`usage.prompt_tokens`, so cost falls out of the input-price path and
`output_tokens` is logged as 0. For an embedding model to be routable the
hosting backend must keep it in discovery — i.e. **not** set `chat_only: true`
(that filter drops `type != "chat"` models, which includes embeddings).

## Try it

```bash
# List models
curl http://localhost:4000/v1/models \
  -H "Authorization: Bearer sk-change-me"

# Chat through an alias
curl http://localhost:4000/v1/chat/completions \
  -H "Authorization: Bearer sk-change-me" \
  -H "Content-Type: application/json" \
  -d '{"model":"fast","messages":[{"role":"user","content":"hi"}]}'

# Embeddings through an alias
curl http://localhost:4000/v1/embeddings \
  -H "Authorization: Bearer sk-change-me" \
  -H "Content-Type: application/json" \
  -d '{"model":"embedding","input":["hallo welt","zweiter satz"]}'

# Backend health snapshot
curl http://localhost:4000/health
```

## Running as a service

`llm-gateway.service` is an example systemd unit assuming
`/opt/llm-gateway` with a `venv/` next to `main.py`. Adapt to taste:

```bash
sudo install -m 0644 llm-gateway.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now llm-gateway
journalctl -u llm-gateway -f
```

## Deploy script (optional)

`deploy.sh` is a small rsync-over-SSH deploy helper. It syncs code (excluding
`config.yaml`, `.env`, `venv/`), pip-installs requirements in a remote venv,
installs/updates the systemd unit if changed, and restarts the service.

```bash
DEPLOY_HOST=root@your-host ./deploy.sh
```

Use it if you like, ignore it if you don't — it's not required to run the
gateway.

## License

MIT — see [LICENSE](LICENSE).
