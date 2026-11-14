"""Responses <-> Anthropic Messages translation (wire_api: anthropic).

Same contract as chat_adapter: the upstream call is non-streaming and the result is
replayed to Codex as a Responses SSE stream. Codex reasoning efforts map to Anthropic
extended-thinking budgets; freeform ("custom") tools become a function with one string
argument `input`, exactly like the chat path.
"""

from __future__ import annotations

import json
from typing import Any

from .chat_adapter import _new_id, tools_for_chat
from .discovery import Route
from .transforms import output_text

ANTHROPIC_VERSION = "2023-06-01"
DEFAULT_MAX_TOKENS = 32_000
MIN_THINKING = 1024
THINKING_BUDGETS = {"low": 4096, "medium": 16_384, "high": 32_768, "xhigh": 49_152, "max": 65_536}


def _blocks(content: Any) -> list[dict[str, Any]]:
    """Responses message content -> Anthropic content blocks."""
    if isinstance(content, str):
        return [{"type": "text", "text": content}] if content else []
    parts: list[dict[str, Any]] = []
    for c in content or []:
        if not isinstance(c, dict):
            continue
        if c.get("type") in ("input_text", "output_text", "text") and c.get("text"):
            parts.append({"type": "text", "text": c["text"]})
        elif c.get("type") == "input_image" and c.get("image_url"):
            url = c["image_url"] if isinstance(c["image_url"], str) else c["image_url"].get("url", "")
            if url.startswith("data:"):
                header, _, data = url.partition(",")
                media = header[5:].split(";")[0] or "image/png"
                parts.append({"type": "image", "source": {"type": "base64", "media_type": media, "data": data}})
            elif url.startswith(("http://", "https://")):
                parts.append({"type": "image", "source": {"type": "url", "url": url}})
    return parts


def _text_of(content: Any) -> str:
    if isinstance(content, str):
        return content
    return "\n".join(b["text"] for b in _blocks(content) if b["type"] == "text")


def to_messages(body: dict[str, Any], route: Route) -> tuple[dict[str, Any], set[str]]:
    chat_tools, custom_tools = tools_for_chat(body)
    tools = [{"name": t["function"]["name"], "description": t["function"]["description"],
              "input_schema": t["function"]["parameters"]} for t in chat_tools]

    system: list[str] = [body["instructions"]] if body.get("instructions") else []
    messages: list[dict[str, Any]] = []

    def push(role: str, parts: list[dict[str, Any]]) -> None:
        if not parts:
            return
        if messages and messages[-1]["role"] == role:
            prev = messages[-1]["content"]
            if isinstance(prev, str):
                prev = [{"type": "text", "text": prev}] if prev else []
            messages[-1]["content"] = prev + parts
        else:
            messages.append({"role": role, "content": parts})

    for item in body.get("input") or []:
        if isinstance(item, str):
            push("user", [{"type": "text", "text": item}])
            continue
        t = item.get("type", "message")
        if t == "message":
            role = item.get("role", "user")
            if role in ("system", "developer"):
                text = _text_of(item.get("content"))
                if text:
                    system.append(text)
            elif role == "assistant":
                push("assistant", _blocks(item.get("content")))
            else:
                push("user", _blocks(item.get("content")))
        elif t in ("function_call", "custom_tool_call"):
            if t == "function_call":
                try:
                    inp = json.loads(item.get("arguments") or "{}")
                except ValueError:
                    inp = {}
            else:
                inp = {"input": item.get("input", "")}
            push("assistant", [{"type": "tool_use", "id": item.get("call_id") or _new_id("toolu"),
                                "name": item.get("name"), "input": inp}])
        elif t in ("function_call_output", "custom_tool_call_output"):
            push("user", [{"type": "tool_result", "tool_use_id": item.get("call_id"),
                           "content": output_text(item.get("output")) or "(no output)"}])

    max_tokens = int(body.get("max_output_tokens") or route.max_output_tokens or DEFAULT_MAX_TOKENS)
    if route.max_output_tokens:
        max_tokens = min(max_tokens, int(route.max_output_tokens))
    if not messages:
        messages = [{"role": "user", "content": [{"type": "text", "text": "(empty conversation)"}]}]
    payload: dict[str, Any] = {"model": body["model"], "messages": messages,
                               "max_tokens": max(1, max_tokens), "stream": False}
    if system:
        payload["system"] = "\n\n".join(system)

    effort = (body.get("reasoning") or {}).get("effort")
    budget = THINKING_BUDGETS.get(effort or "", 0)
    budget = min(budget, payload["max_tokens"] - MIN_THINKING)
    thinking = budget >= MIN_THINKING
    if thinking:
        payload["thinking"] = {"type": "enabled", "budget_tokens": budget}

    choice = body.get("tool_choice")
    if tools and choice != "none":
        payload["tools"] = tools
        if choice in ("auto", "required") and not (thinking and choice == "required"):
            # forced tool use is incompatible with extended thinking
            payload["tool_choice"] = {"type": "auto" if choice == "auto" else "any"}
    if not thinking:
        for k in ("temperature", "top_p"):
            if k in body:
                payload[k] = body[k]
    return payload, custom_tools


def from_messages(resp: dict[str, Any], custom_tools: set[str]) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    items: list[dict[str, Any]] = []
    texts: list[str] = []
    for block in resp.get("content") or []:
        if not isinstance(block, dict):
            continue
        if block.get("type") == "text" and block.get("text"):
            texts.append(block["text"])
        elif block.get("type") == "tool_use":
            name, call_id = block.get("name", ""), block.get("id") or _new_id("call")
            if name in custom_tools:
                raw = (block.get("input") or {}).get("input", "")
                items.append({"type": "custom_tool_call", "id": _new_id("ctc"), "call_id": call_id,
                              "name": name, "input": raw, "status": "completed"})
            else:
                items.append({"type": "function_call", "id": _new_id("fc"), "call_id": call_id, "name": name,
                              "arguments": json.dumps(block.get("input") or {}), "status": "completed"})
    if texts:
        items.insert(0, {"type": "message", "id": _new_id("msg"), "role": "assistant", "status": "completed",
                         "content": [{"type": "output_text", "text": "\n".join(texts), "annotations": []}]})
    u = resp.get("usage") or {}
    cached = u.get("cache_read_input_tokens", 0)
    # Anthropic reports cache reads/writes separately; Codex wants one input total.
    inp = u.get("input_tokens", 0) + cached + u.get("cache_creation_input_tokens", 0)
    out = u.get("output_tokens", 0)
    usage = {"input_tokens": inp, "output_tokens": out, "total_tokens": inp + out,
             "input_tokens_details": {"cached_tokens": cached},
             "output_tokens_details": {"reasoning_tokens": 0}}
    return items, usage
