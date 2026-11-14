"""Responses <-> Chat Completions translation for upstreams without /responses.

The upstream call is non-streaming; the result is replayed to Codex as a Responses SSE stream.
Codex's freeform ("custom") tools such as apply_patch become a function with one string
argument `input` and are turned back into custom_tool_call items.
"""

from __future__ import annotations

import json
import uuid
from typing import Any

from .transforms import output_text


def _new_id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex[:24]}"


def _content_parts(content: Any) -> Any:
    if isinstance(content, str):
        return content
    parts: list[dict[str, Any]] = []
    for c in content or []:
        if not isinstance(c, dict):
            continue
        if c.get("type") in ("input_text", "output_text", "text"):
            parts.append({"type": "text", "text": c.get("text", "")})
        elif c.get("type") == "input_image" and c.get("image_url"):
            parts.append({"type": "image_url", "image_url": {"url": c["image_url"]}})
    if all(p["type"] == "text" for p in parts):
        return "".join(p["text"] for p in parts)
    return parts


def tools_for_chat(body: dict[str, Any]) -> tuple[list[dict[str, Any]], set[str]]:
    """Responses tools -> Chat Completions tools; custom tools become single-string functions."""
    custom_tools: set[str] = set()
    tools: list[dict[str, Any]] = []
    for t in body.get("tools") or []:
        if t.get("type") == "function":
            tools.append({"type": "function", "function": {
                "name": t["name"], "description": t.get("description", ""),
                "parameters": t.get("parameters") or {"type": "object", "properties": {}}}})
        elif t.get("type") == "custom":
            custom_tools.add(t["name"])
            fmt = t.get("format") or {}
            hint = f"\nInput format ({fmt.get('syntax', fmt.get('type', 'text'))}):\n{fmt.get('definition', '')}" if fmt else ""
            tools.append({"type": "function", "function": {
                "name": t["name"], "description": (t.get("description") or "") + hint,
                "parameters": {"type": "object", "properties": {"input": {"type": "string", "description": "Raw tool input"}},
                               "required": ["input"]}}})
    return tools, custom_tools


def to_chat(body: dict[str, Any]) -> tuple[dict[str, Any], set[str]]:
    tools, custom_tools = tools_for_chat(body)

    messages: list[dict[str, Any]] = []
    if body.get("instructions"):
        messages.append({"role": "system", "content": body["instructions"]})
    for item in body.get("input") or []:
        if isinstance(item, str):
            messages.append({"role": "user", "content": item})
            continue
        t = item.get("type", "message")
        if t == "message":
            role = {"developer": "system"}.get(item.get("role"), item.get("role", "user"))
            messages.append({"role": role, "content": _content_parts(item.get("content"))})
        elif t in ("function_call", "custom_tool_call"):
            args = item.get("arguments") if t == "function_call" else json.dumps({"input": item.get("input", "")})
            call = {"id": item.get("call_id"), "type": "function", "function": {"name": item.get("name"), "arguments": args or "{}"}}
            if messages and messages[-1]["role"] == "assistant" and "tool_calls" in messages[-1]:
                messages[-1]["tool_calls"].append(call)
            else:
                messages.append({"role": "assistant", "content": None, "tool_calls": [call]})
        elif t in ("function_call_output", "custom_tool_call_output"):
            messages.append({"role": "tool", "tool_call_id": item.get("call_id"), "content": output_text(item.get("output"))})

    chat: dict[str, Any] = {"model": body["model"], "messages": messages, "stream": False}
    if tools:
        chat["tools"] = tools
        if body.get("tool_choice") in ("auto", "none", "required"):
            chat["tool_choice"] = body["tool_choice"]
    effort = (body.get("reasoning") or {}).get("effort")
    if effort:
        chat["reasoning_effort"] = effort
    if body.get("max_output_tokens"):
        chat["max_tokens"] = body["max_output_tokens"]
    for k in ("temperature", "top_p"):
        if k in body:
            chat[k] = body[k]
    return chat, custom_tools


def from_chat(resp: dict[str, Any], custom_tools: set[str]) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    choice = (resp.get("choices") or [{}])[0]
    msg = choice.get("message") or {}
    items: list[dict[str, Any]] = []
    if msg.get("content"):
        items.append({"type": "message", "id": _new_id("msg"), "role": "assistant", "status": "completed",
                      "content": [{"type": "output_text", "text": msg["content"], "annotations": []}]})
    for call in msg.get("tool_calls") or []:
        fn = call.get("function") or {}
        name, args = fn.get("name", ""), fn.get("arguments") or "{}"
        call_id = call.get("id") or _new_id("call")
        if name in custom_tools:
            try:
                raw = json.loads(args).get("input", "")
            except (ValueError, AttributeError):
                raw = args
            items.append({"type": "custom_tool_call", "id": _new_id("ctc"), "call_id": call_id, "name": name,
                          "input": raw, "status": "completed"})
        else:
            items.append({"type": "function_call", "id": _new_id("fc"), "call_id": call_id, "name": name,
                          "arguments": args, "status": "completed"})
    u = resp.get("usage") or {}
    usage = {"input_tokens": u.get("prompt_tokens", 0), "output_tokens": u.get("completion_tokens", 0),
             "total_tokens": u.get("total_tokens", 0),
             "input_tokens_details": {"cached_tokens": (u.get("prompt_tokens_details") or {}).get("cached_tokens", 0)},
             "output_tokens_details": {"reasoning_tokens": (u.get("completion_tokens_details") or {}).get("reasoning_tokens", 0)}}
    return items, usage


def sse_events(model: str, items: list[dict[str, Any]], usage: dict[str, Any] | None = None) -> list[bytes]:
    """A complete Responses SSE stream for already-finished output items."""
    rid = _new_id("resp")
    base = {"id": rid, "object": "response", "model": model}
    events: list[tuple[str, dict[str, Any]]] = [
        ("response.created", {"response": {**base, "status": "in_progress", "output": []}})]
    for idx, item in enumerate(items):
        events.append(("response.output_item.added", {"output_index": idx, "item": item}))
        if item.get("type") == "message":
            for c_idx, c in enumerate(item.get("content") or []):
                if c.get("text"):
                    events.append(("response.output_text.delta", {"item_id": item.get("id"), "output_index": idx,
                                                                   "content_index": c_idx, "delta": c["text"]}))
        events.append(("response.output_item.done", {"output_index": idx, "item": item}))
    events.append(("response.completed", {"response": {**base, "status": "completed", "output": items,
                                                       "usage": usage or {"input_tokens": 0, "output_tokens": 0, "total_tokens": 0}}}))
    out = []
    for seq, (etype, data) in enumerate(events):
        payload = {"type": etype, "sequence_number": seq, **data}
        out.append(f"event: {etype}\ndata: {json.dumps(payload)}\n\n".encode())
    return out
