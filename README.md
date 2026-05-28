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
- **Priority + failover.** Backend with `priority: 1` is preferred; the
  gateway falls back to the next on connection errors or when a model
  isn't available on the preferred backend.
- **Virtual models.** Aliases like `fast`, `vision`, `translator` map to
  different real model IDs per backend. Swap the underlying model without
  changing client code.
- **Cloud-as-backend.** Per-backend `api_key` lets you wire in
  OpenAI-compatible cloud providers (together.ai, OpenAI, OpenRouter,
  DeepInfra, …) as just another backend with its own priority.
- **Hot config reload.** `config.yaml` changes are picked up live; no
  restart needed.

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

virtual_models:
  "translator":  "Aya-Expanse-8B"       # same model on every backend
  "fast":                                # per-backend mapping
    local-gpu:   "Qwen3.5-9B"
    local-cpu:   "gemma-3-9b-it"
    together:    "meta-llama/Meta-Llama-3.1-8B-Instruct-Turbo"
```

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
| `GET`  | `/health` | Per-backend health/model/priority snapshot + virtual_models dump |

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
