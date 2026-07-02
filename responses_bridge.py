"""Responses API ↔ Chat Completions translation layer.

Pure functions only — no imports from `main`/`adapters`, no gateway state — so the
bridge stays hot-reload- and test-friendly (same rule as reasoning.py). `main.py`
owns the /v1/responses endpoints, dispatch/parking, and the background mode; this
module owns every body/stream translation those endpoints need:

- request:  `responses_to_chat()`   Responses request body → Chat Completions body
- response: `chat_to_responses()`   Chat Completions response → full Responses object
- shell:    `response_shell()`      the ONE Responses-object skeleton (completed
            bodies, stream events, and background/queued states all build on it)
- stream:   `responses_stream()`    backend chat SSE → Responses API SSE events
"""
from __future__ import annotations

import json
import logging
import time
import uuid
from typing import Any, Optional

logger = logging.getLogger(__name__)


def _oid(prefix: str) -> str:
    """OpenAI-style object id: <prefix>_<24 hex chars> (msg_/fc_/resp_…)."""
    return f"{prefix}_{uuid.uuid4().hex[:24]}"


def _content_parts_to_text(content: Any) -> Any:
    """Flatten a Responses-style content array to a chat-completions content value."""
    if not isinstance(content, list):
        return content
    text_pieces: list[str] = []
    image_parts: list[dict] = []
    for part in content:
        ptype = part.get("type")
        if ptype in ("input_text", "output_text", "text"):
            text_pieces.append(part.get("text", ""))
        elif ptype == "input_image":
            url = part.get("image_url") or part.get("url")
            if url:
                image_parts.append({"type": "image_url", "image_url": {"url": url}})
    if not image_parts:
        return "".join(text_pieces)
    parts: list[dict] = []
    if text_pieces:
        parts.append({"type": "text", "text": "".join(text_pieces)})
    parts.extend(image_parts)
    return parts


def responses_to_chat(body: dict) -> dict:
    """Translate an OpenAI Responses API request body to Chat Completions."""
    passthrough = {
        "model", "temperature", "top_p", "stop", "seed", "user", "metadata",
        "presence_penalty", "frequency_penalty", "logit_bias",
        "parallel_tool_calls", "response_format",
    }
    chat: dict = {k: v for k, v in body.items() if k in passthrough}

    if "max_output_tokens" in body:
        chat["max_tokens"] = body["max_output_tokens"]
    # stream: silently downgrade — translating SSE event streams isn't supported yet
    chat["stream"] = False

    # Tools: Responses uses flat {type, name, description, parameters};
    #        Chat uses nested {type, function: {name, description, parameters}}.
    if tools := body.get("tools"):
        chat_tools = []
        for t in tools:
            if t.get("type") != "function":
                continue  # skip built-in tools (web_search, code_interpreter, …)
            fn = {k: t[k] for k in ("name", "description", "parameters", "strict") if k in t}
            chat_tools.append({"type": "function", "function": fn})
        if chat_tools:
            chat["tools"] = chat_tools
    if "tool_choice" in body:
        chat["tool_choice"] = body["tool_choice"]

    messages: list[dict] = []
    if instructions := body.get("instructions"):
        messages.append({"role": "system", "content": instructions})

    inp = body.get("input")
    if isinstance(inp, str):
        messages.append({"role": "user", "content": inp})
    elif isinstance(inp, list):
        for item in inp:
            itype = item.get("type", "message")
            if itype == "message":
                role = item.get("role", "user")
                if role == "developer":
                    role = "system"
                content = _content_parts_to_text(item.get("content", ""))
                messages.append({"role": role, "content": content})
            elif itype == "function_call":
                messages.append({
                    "role": "assistant",
                    "content": None,
                    "tool_calls": [{
                        "id": item.get("call_id") or item.get("id"),
                        "type": "function",
                        "function": {
                            "name": item.get("name", ""),
                            "arguments": item.get("arguments", "{}"),
                        },
                    }],
                })
            elif itype == "function_call_output":
                messages.append({
                    "role": "tool",
                    "tool_call_id": item.get("call_id"),
                    "content": item.get("output", ""),
                })
    chat["messages"] = messages
    return chat


def output_text_of(output: Optional[list]) -> str:
    """Concatenated `output_text` parts of a Responses `output` array."""
    return "".join(p.get("text", "") for o in (output or []) if o.get("type") == "message"
                   for p in (o.get("content") or []) if p.get("type") == "output_text")


def response_shell(rid: str, status: str, model: Optional[str], created: int,
                   output: Optional[list] = None, usage: Optional[dict] = None,
                   error: Optional[dict] = None, background: bool = False,
                   **extra) -> dict:
    """The one OpenAI Responses-object skeleton. Completed bodies, stream-event
    snapshots, and background queued/failed states all build on this; `extra`
    lays additional top-level fields (e.g. `_finish_reason`) over the base."""
    out = output or []
    shell = {
        "id": rid, "object": "response", "created_at": created, "status": status,
        "error": error, "incomplete_details": None, "model": model,
        "output": out, "output_text": output_text_of(out),
        "usage": usage, "metadata": {}, "parallel_tool_calls": True,
        "tool_choice": "auto", "tools": [], "temperature": 1.0, "top_p": 1.0,
    }
    if background:
        shell["background"] = True
    shell.update(extra)
    return shell


def chat_to_responses(chat_resp: dict) -> dict:
    """Translate a Chat Completions response body to a Responses API body."""
    choice = (chat_resp.get("choices") or [{}])[0]
    message = choice.get("message") or {}

    output: list[dict] = []

    text_content = message.get("content")
    if text_content:
        output.append({
            "type": "message",
            "id": _oid("msg"),
            "role": message.get("role", "assistant"),
            "status": "completed",
            "content": [{"type": "output_text", "text": text_content, "annotations": []}],
        })

    for tc in message.get("tool_calls") or []:
        fn = tc.get("function") or {}
        output.append({
            "type": "function_call",
            "id": _oid("fc"),
            "call_id": tc.get("id"),
            "name": fn.get("name", ""),
            "arguments": fn.get("arguments", ""),
            "status": "completed",
        })

    usage_in = chat_resp.get("usage") or {}
    usage = {
        "input_tokens": usage_in.get("prompt_tokens", 0),
        "output_tokens": usage_in.get("completion_tokens", 0),
        "total_tokens": usage_in.get("total_tokens", 0),
    }

    return response_shell(
        chat_resp.get("id") or _oid("resp"), "completed",
        chat_resp.get("model"), chat_resp.get("created", int(time.time())),
        output=output, usage=usage, _finish_reason=choice.get("finish_reason"),
    )


async def responses_stream(chat_resp, raw_body: dict, alias: str):
    """A3: translate a backend chat-completion SSE stream into Responses API SSE
    events. Consumes the adapter StreamingResponse's body_iterator (so in-flight
    accounting + stats still fire in the adapter when it drains)."""
    resp_id = _oid("resp")
    item_id = _oid("msg")
    created = int(time.time())
    model = raw_body.get("model") or alias
    seq = 0

    def ev(etype: str, payload: dict) -> str:
        nonlocal seq
        body = {"type": etype, "sequence_number": seq, **payload}
        seq += 1
        return f"event: {etype}\ndata: {json.dumps(body, ensure_ascii=False)}\n\n"

    yield ev("response.created",
             {"response": response_shell(resp_id, "in_progress", model, created)})
    yield ev("response.in_progress",
             {"response": response_shell(resp_id, "in_progress", model, created)})
    yield ev("response.output_item.added", {"output_index": 0, "item": {
        "id": item_id, "type": "message", "status": "in_progress", "role": "assistant", "content": []}})
    yield ev("response.content_part.added", {"item_id": item_id, "output_index": 0,
             "content_index": 0, "part": {"type": "output_text", "text": "", "annotations": []}})

    full, buf, usage = "", "", None
    try:
        async for chunk in chat_resp.body_iterator:
            buf += chunk.decode("utf-8", "ignore") if isinstance(chunk, (bytes, bytearray)) else chunk
            while "\n" in buf:
                line, buf = buf.split("\n", 1)
                line = line.strip()
                if not line.startswith("data:"):
                    continue
                data = line[5:].strip()
                if not data or data == "[DONE]":
                    continue
                try:
                    obj = json.loads(data)
                except Exception:
                    continue
                if not isinstance(obj, dict):
                    continue
                if obj.get("model"):
                    model = obj["model"]
                if obj.get("usage"):
                    usage = obj["usage"]
                delta = ((obj.get("choices") or [{}])[0].get("delta") or {}).get("content")
                if delta:
                    full += delta
                    yield ev("response.output_text.delta", {"item_id": item_id,
                             "output_index": 0, "content_index": 0, "delta": delta})
    except Exception as e:
        logger.warning(f"responses SSE translate aborted: {e}")

    part = {"type": "output_text", "text": full, "annotations": []}
    yield ev("response.output_text.done", {"item_id": item_id, "output_index": 0,
             "content_index": 0, "text": full})
    yield ev("response.content_part.done", {"item_id": item_id, "output_index": 0,
             "content_index": 0, "part": part})
    final_item = {"id": item_id, "type": "message", "status": "completed",
                  "role": "assistant", "content": [part]}
    yield ev("response.output_item.done", {"output_index": 0, "item": final_item})
    u = None
    if usage:
        u = {"input_tokens": usage.get("prompt_tokens", 0),
             "output_tokens": usage.get("completion_tokens", 0),
             "total_tokens": usage.get("total_tokens", 0)}
    yield ev("response.completed",
             {"response": response_shell(resp_id, "completed", model, created,
                                         output=[final_item], usage=u)})
