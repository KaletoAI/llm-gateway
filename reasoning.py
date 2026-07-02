"""Normalized reasoning toggle.

Translate one per-request control — `reasoning: "off" | "on" | "auto"` (or the
OpenAI `reasoning_effort` alias) — into the correct per-(model, backend) mechanism,
or honestly report it as unsupported. The naive `chat_template_kwargs:{enable_thinking:false}`
is a leaky abstraction (silent no-op on models that don't honor it); here every rule
is an explicit, curated (model-glob × backend-set) → adapter mapping, and what was
actually applied is reported back via the `x-reasoning-control` response header.

Pure functions only — no imports from `main`/`adapters`, so it stays hot-reload- and
test-friendly. `main` owns the rule store and injects `apply_reasoning` into the
adapter context; the adapter calls it on the outgoing body per backend.

A rule (stored as JSON, edited in the /ui Reasoning tab) looks like:
    {"match": "qwen3.5*", "backends": ["localai-strix", "phoenix"],
     "adapter": "prefill", "param": {"content": "<think>\\n\\n</think>"}}
`backends` = a list of LLM backend names, or ["*"] for all. First rule (in order)
whose model-glob matches AND whose backend-set contains the dispatch backend wins.
"""
from __future__ import annotations

from fnmatch import fnmatch
from typing import Optional

ADAPTERS = ("enable_thinking", "reasoning_effort", "nothink_token", "prefill", "none")


def resolve(rules: list, backend: str, model: str) -> Optional[dict]:
    """First rule whose model-glob matches `model` AND whose backend set contains
    `backend` (or is ["*"]). None if nothing matches."""
    if not model:
        return None
    for r in rules or []:
        if not isinstance(r, dict):
            continue
        if r.get("enabled") is False:            # disabled rules are skipped (kept for re-enabling)
            continue
        if not fnmatch(model, r.get("match") or "*"):
            continue
        bks = r.get("backends") or ["*"]
        if "*" in bks or backend in bks:
            return r
    return None


def _append_to_last_user(messages: list, token: str) -> list:
    """Append `token` to the last user message (string or multimodal content list);
    if there is none, add a trailing user message. Returns a new list (no mutation)."""
    msgs = [dict(m) if isinstance(m, dict) else m for m in (messages or [])]
    idx = next((i for i in range(len(msgs) - 1, -1, -1)
                if isinstance(msgs[i], dict) and msgs[i].get("role") == "user"), -1)
    if idx < 0:
        msgs.append({"role": "user", "content": token})
        return msgs
    c = msgs[idx].get("content")
    if isinstance(c, str):
        msgs[idx]["content"] = (c + " " + token).strip()
    elif isinstance(c, list):
        msgs[idx]["content"] = list(c) + [{"type": "text", "text": token}]
    else:
        msgs[idx]["content"] = token
    return msgs


def apply(rule: Optional[dict], requested: Optional[str], payload: dict) -> tuple[dict, Optional[str]]:
    """Apply the reasoning control to a COPY of `payload`.

    `requested` ∈ {"off", "on", None}. None (auto) → payload untouched, control None
    (full back-compat). Otherwise returns (new_payload, control) where control is the
    `x-reasoning-control` string, e.g. "off:prefill" / "on:enable_thinking" /
    "unsupported" (no matching rule or adapter "none").
    """
    if requested not in ("off", "on"):
        return payload, None                          # auto → no change
    adapter = (rule or {}).get("adapter") or "none"
    if rule is None or adapter == "none" or adapter not in ADAPTERS:
        return payload, "unsupported"
    param = (rule or {}).get("param") or {}
    body = dict(payload)

    if adapter == "enable_thinking":
        kw = dict(body.get("chat_template_kwargs") or {})
        kw["enable_thinking"] = (requested == "on")
        body["chat_template_kwargs"] = kw
        return body, f"{requested}:enable_thinking"

    if adapter == "reasoning_effort":
        body["reasoning_effort"] = (param.get("on") or "high") if requested == "on" \
            else (param.get("off") or "minimal")
        return body, f"{requested}:reasoning_effort"

    if adapter == "nothink_token":
        if requested == "off":
            body["messages"] = _append_to_last_user(body.get("messages") or [],
                                                     param.get("token") or "/nothink")
            return body, "off:nothink_token"
        return body, "on:noop"                        # model thinks by default → nothing to force

    if adapter == "prefill":
        if requested == "off":
            content = param.get("content") or "<think>\n\n</think>"
            body["messages"] = list(body.get("messages") or []) + [{"role": "assistant", "content": content}]
            return body, "off:prefill"
        return body, "on:noop"

    return payload, "unsupported"
