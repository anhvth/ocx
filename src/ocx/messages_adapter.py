"""Anthropic Messages <-> OpenAI Responses, so Claude clients (Claude Code, Claude Desktop) can use
ChatGPT models through the Codex login.

`to_responses` turns a Messages request into a ChatGPT-backend Responses request (always streamed);
`Translator` turns the streamed Responses events back into Messages SSE events (or one Message).
Thinking blocks are not replayed: OpenAI reasoning can't be rebuilt from Claude thinking text.
"""

from __future__ import annotations

import json
import uuid
from typing import Any

EFFORTS = ("minimal", "low", "medium", "high", "xhigh")


def _text(content: Any) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "\n".join(b.get("text", "") for b in content if isinstance(b, dict) and b.get("type") == "text")
    return ""


def _image(block: dict[str, Any]) -> dict[str, Any] | None:
    src = block.get("source") or {}
    if src.get("type") == "base64":
        return {"type": "input_image", "image_url": f"data:{src.get('media_type', 'image/png')};base64,{src.get('data', '')}"}
    if src.get("type") == "url":
        return {"type": "input_image", "image_url": src.get("url", "")}
    return None


def _effort(body: dict[str, Any], efforts: tuple[str, ...] = EFFORTS, default: str = "medium") -> str:
    want = (body.get("output_config") or {}).get("effort") or body.get("effort")
    if want in efforts:
        return want
    if want == "max" and "xhigh" in efforts:
        return "xhigh"
    thinking = body.get("thinking") or {}
    effort = default
    if thinking.get("type") == "enabled":
        budget = int(thinking.get("budget_tokens") or 0)
        effort = "low" if budget < 4096 else "medium" if budget < 16384 else "high"
    return effort if effort in efforts else default


def _blocks(content: Any) -> list[dict[str, Any]]:
    return [{"type": "text", "text": content}] if isinstance(content, str) else list(content or [])


def fold_system_messages(messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Claude Code puts role "system" entries (mid-conversation reminders) inside `messages`.
    Anthropic accepts them; other Messages endpoints (Alibaba, vLLM) reject the request. Fold each
    one into the neighbouring user turn as text."""
    out: list[dict[str, Any]] = []
    pending: list[dict[str, Any]] = []
    for m in messages:
        if m.get("role") == "system":
            text = _text(m.get("content"))
            if text:
                block = {"type": "text", "text": f"<system-reminder>\n{text}\n</system-reminder>"}
                if out and out[-1].get("role") == "user":
                    out[-1] = {**out[-1], "content": _blocks(out[-1].get("content")) + [block]}
                else:
                    pending.append(block)
            continue
        if pending and m.get("role") == "user":
            m = {**m, "content": pending + _blocks(m.get("content"))}
            pending = []
        out.append(m)
    if pending:
        out.append({"role": "user", "content": pending})
    return out


def to_responses(body: dict[str, Any], model: str, route: Any = None) -> dict[str, Any]:
    items: list[dict[str, Any]] = []
    for m in body.get("messages") or []:
        role = m.get("role", "user")
        if role == "system":
            items.append({"role": "developer", "content": [{"type": "input_text", "text": _text(m.get("content"))}]})
            continue
        if role not in ("user", "assistant"):
            role = "user"
        part = "output_text" if role == "assistant" else "input_text"
        content = m.get("content")
        if isinstance(content, str):
            items.append({"role": role, "content": [{"type": part, "text": content}]})
            continue
        parts: list[dict[str, Any]] = []

        def flush() -> None:
            if parts:
                items.append({"role": role, "content": list(parts)})
                parts.clear()

        for b in content or []:
            t = b.get("type")
            if t == "text":
                parts.append({"type": part, "text": b.get("text", "")})
            elif t == "image" and role == "user":
                img = _image(b)
                if img:
                    parts.append(img)
            elif t == "document":
                src = b.get("source") or {}
                if src.get("type") == "text":
                    parts.append({"type": part, "text": src.get("data", "")})
            elif t == "tool_use":
                flush()
                items.append({"type": "function_call", "call_id": b.get("id"), "name": b.get("name"),
                              "arguments": json.dumps(b.get("input") or {})})
            elif t == "tool_result":
                flush()
                out = _text(b.get("content"))
                if b.get("is_error"):
                    out = f"[tool error]\n{out}"
                items.append({"type": "function_call_output", "call_id": b.get("tool_use_id"), "output": out})
            # thinking / redacted_thinking / server tool blocks: not replayable to OpenAI
        flush()

    tools = [{"type": "function", "name": t["name"], "description": t.get("description", ""),
              "parameters": t.get("input_schema") or {"type": "object", "properties": {}}, "strict": False}
             for t in body.get("tools") or [] if t.get("name") and "input_schema" in t]
    out: dict[str, Any] = {
        "model": model,
        "instructions": _text(body.get("system")),
        "input": items,
        "tools": tools,
        "parallel_tool_calls": True,
        "reasoning": {"effort": _effort(body, tuple(route.efforts), route.default_effort) if route else _effort(body)},
        "store": False,
        "stream": True,
    }
    choice = body.get("tool_choice") or {}
    if tools and choice.get("type") in ("any", "tool", "none"):
        out["tool_choice"] = {"any": "required", "none": "none"}.get(choice["type"]) or \
            {"type": "function", "name": choice.get("name")}
    else:
        out["tool_choice"] = "auto"
    return out


def sse(event: str, data: dict[str, Any]) -> bytes:
    return f"event: {event}\ndata: {json.dumps(data)}\n\n".encode()


class Translator:
    """Feed Responses SSE events in, get Messages SSE events out."""

    def __init__(self, model: str):
        self.model = model
        self.id = f"msg_{uuid.uuid4().hex[:24]}"
        self.blocks: dict[int, int] = {}  # Responses output_index -> Messages content index
        self.open: set[int] = set()
        self.content: list[dict[str, Any]] = []  # for non-streamed replies
        self.tool_used = False
        self.usage = {"input_tokens": 0, "output_tokens": 0}
        self.stop_reason = "end_turn"
        self.error: str | None = None

    def start(self) -> bytes:
        return sse("message_start", {"type": "message_start", "message": {
            "id": self.id, "type": "message", "role": "assistant", "model": self.model, "content": [],
            "stop_reason": None, "stop_sequence": None, "usage": dict(self.usage)}})

    def _open(self, oi: int, block: dict[str, Any]) -> bytes:
        idx = len(self.content)
        self.blocks[oi] = idx
        self.open.add(idx)
        self.content.append(json.loads(json.dumps(block)))
        if block["type"] == "tool_use":
            self.content[idx]["_json"] = ""
        return sse("content_block_start", {"type": "content_block_start", "index": idx, "content_block": block})

    def _close(self, oi: int) -> bytes:
        idx = self.blocks.get(oi)
        if idx is None or idx not in self.open:
            return b""
        self.open.discard(idx)
        blk = self.content[idx]
        if blk["type"] == "tool_use":
            raw = blk.pop("_json", "")
            try:
                blk["input"] = json.loads(raw) if raw else {}
            except ValueError:
                blk["input"] = {}
        return sse("content_block_stop", {"type": "content_block_stop", "index": idx})

    def feed(self, ev: dict[str, Any]) -> bytes:
        t = ev.get("type", "")
        oi = ev.get("output_index", 0)
        if t == "response.output_item.added":
            item = ev.get("item") or {}
            if item.get("type") == "function_call":
                self.tool_used = True
                return self._open(oi, {"type": "tool_use", "id": item.get("call_id") or item.get("id"),
                                       "name": item.get("name"), "input": {}})
        elif t == "response.output_text.delta":
            out = b""
            if oi not in self.blocks:
                out += self._open(oi, {"type": "text", "text": ""})
            idx = self.blocks[oi]
            self.content[idx]["text"] = self.content[idx].get("text", "") + ev.get("delta", "")
            return out + sse("content_block_delta", {"type": "content_block_delta", "index": idx,
                                                     "delta": {"type": "text_delta", "text": ev.get("delta", "")}})
        elif t == "response.function_call_arguments.delta":
            idx = self.blocks.get(oi)
            if idx is not None:
                self.content[idx]["_json"] += ev.get("delta", "")
                return sse("content_block_delta", {"type": "content_block_delta", "index": idx,
                                                   "delta": {"type": "input_json_delta", "partial_json": ev.get("delta", "")}})
        elif t == "response.output_item.done":
            return self._close(oi)
        elif t in ("response.completed", "response.incomplete"):
            resp = ev.get("response") or {}
            u = resp.get("usage") or {}
            self.usage = {"input_tokens": u.get("input_tokens", 0), "output_tokens": u.get("output_tokens", 0)}
            if t == "response.incomplete":
                self.stop_reason = "max_tokens"
            elif self.tool_used:
                self.stop_reason = "tool_use"
        elif t in ("response.failed", "error"):
            resp = ev.get("response") or {}
            self.error = ((resp.get("error") or {}).get("message") or (ev.get("error") or {}).get("message")
                          or ev.get("message") or "upstream error")
        return b""

    def finish(self) -> bytes:
        out = b"".join(self._close(oi) for oi in list(self.blocks))
        if self.error:
            return out + sse("error", {"type": "error", "error": {"type": "api_error", "message": self.error}})
        return out + sse("message_delta", {"type": "message_delta",
                                           "delta": {"stop_reason": self.stop_reason, "stop_sequence": None},
                                           "usage": dict(self.usage)}) + \
            sse("message_stop", {"type": "message_stop"})

    def message(self) -> dict[str, Any]:
        for oi in list(self.blocks):
            self._close(oi)
        return {"id": self.id, "type": "message", "role": "assistant", "model": self.model,
                "content": self.content, "stop_reason": self.stop_reason, "stop_sequence": None,
                "usage": self.usage}
