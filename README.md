# LLM Gateway

Leichtgewichtiger OpenAI-kompatibler Proxy der mehrere llama-swap Instanzen zusammenfasst.

## Features

- **Auto-Discovery** — fragt `/v1/models` von jedem Backend ab, kein manuelles Eintragen
- **Priority Routing** — Backend mit `priority: 1` wird immer bevorzugt, Fallback auf `priority: 2` wenn down
- **Virtual Models** — Pseudo-Modelle die auf echte Modelle zeigen, einfach austauschbar
- **Health Checks** — Backends werden regelmäßig geprüft und automatisch aus dem Pool genommen

## Konfiguration

`config.yaml` anpassen:

```yaml
backends:
  - name: evo-x2
    url: http://192.168.8.XXX:8080
    priority: 1
  - name: ubuntu-gpu
    url: http://192.168.8.XXX:8080
    priority: 2

virtual_models:
  "translator": "Aya-Expanse-8B"
  "fast":       "Qwen3.5-9B-heretic"
```

## Deploy (LXC)

```bash
# Auf dem LXC
apt install -y python3-venv

mkdir -p /opt/llm-gateway
cp -r . /opt/llm-gateway/
cd /opt/llm-gateway

python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt

cp llm-gateway.service /etc/systemd/system/
systemctl daemon-reload
systemctl enable --now llm-gateway
```

## Endpoints

| Endpoint | Beschreibung |
|---|---|
| `GET /v1/models` | Alle Modelle aller gesunden Backends |
| `POST /v1/chat/completions` | Chat, priority-routed |
| `POST /v1/completions` | Completions, priority-routed |
| `GET /health` | Status aller Backends + Modelle |

## Testen

```bash
# Welche Modelle sind verfügbar?
curl http://lxc-ip:4000/v1/models -H "Authorization: Bearer sk-lokal-geheim"

# Health Check
curl http://lxc-ip:4000/health

# Request (landet auf EVO X2 wenn online)
curl http://lxc-ip:4000/v1/chat/completions \
  -H "Authorization: Bearer sk-lokal-geheim" \
  -H "Content-Type: application/json" \
  -d '{"model": "fast", "messages": [{"role": "user", "content": "test"}]}'
```
