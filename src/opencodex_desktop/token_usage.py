"""Bounded, metadata-only usage capture; never retain prompts or response text."""

from __future__ import annotations

import json
import time
from typing import Any

# shortcut: bodies/events over 2 MiB may omit usage; add incremental JSON parsing if needed.
_LIMIT = 2 * 1024 * 1024


def _count(value: Any) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) and value >= 0 else None


class TokenUsage:
    def __init__(self, model: str, streaming: bool = False):
        self.model = model[:200]
        self.streaming = streaming
        self.at = time.time()
        self.response_id: str | None = None
        self.status: str | None = None
        self.usage: dict[str, Any] = {}
        self._buffer = b""
        self._data: list[bytes] = []
        self._size = 0
        self._skip = False

    def event(self, event: Any) -> None:
        if not isinstance(event, dict):
            return
        kind = event.get("type")
        payload = event.get("response") or event.get("message") or event
        if not isinstance(payload, dict):
            return
        if isinstance(payload.get("id"), str):
            self.response_id = payload["id"][:200]
        usage = payload.get("usage")
        if isinstance(usage, dict):
            for key in ("input_tokens", "output_tokens", "prompt_tokens", "completion_tokens",
                        "cache_read_input_tokens", "cache_creation_input_tokens"):
                if key in usage:
                    self.usage[key] = _count(usage[key])
            for key in ("input_tokens_details", "prompt_tokens_details"):
                if isinstance(usage.get(key), dict):
                    self.usage[key] = {"cached_tokens": _count(usage[key].get("cached_tokens"))}
        if payload.get("status") in ("completed", "failed", "incomplete"):
            self.status = payload["status"]
        if kind in ("response.completed", "message_stop"):
            self.status = "completed"
        elif kind in ("response.failed", "error") or payload.get("error"):
            self.status = "failed"
        elif kind == "response.incomplete":
            self.status = "incomplete"
        elif isinstance(event.get("choices"), list) and any(
                c.get("finish_reason") for c in event["choices"] if isinstance(c, dict)):
            self.status = "completed"

    def feed(self, chunk: bytes) -> None:
        if not self.streaming:
            if not self._skip and len(self._buffer) + len(chunk) <= _LIMIT:
                self._buffer += chunk
            else:
                self._buffer = b""
                self._skip = True
            return
        self._buffer += chunk
        while b"\n" in self._buffer:
            line, self._buffer = self._buffer.split(b"\n", 1)
            line = line.rstrip(b"\r")
            if not line:
                if self._data and not self._skip:
                    self._json(b"\n".join(self._data))
                self._data = []
                self._size = 0
                self._skip = False
            elif line.startswith(b"data:") and not self._skip:
                self._size += len(line)
                if self._size <= _LIMIT:
                    self._data.append(line[5:].lstrip(b" "))
                else:
                    self._data = []
                    self._skip = True
        if len(self._buffer) > _LIMIT:
            self._buffer = b""
            self._data = []
            self._skip = True

    def _json(self, data: bytes) -> None:
        try:
            self.event(json.loads(data))
        except (ValueError, RecursionError):
            pass

    def result(self, http_status: int) -> dict[str, Any]:
        if not self.streaming and self._buffer:
            self._json(self._buffer)
        # Anthropic input excludes both cache reads and writes; Responses input includes them.
        u = self.usage
        inp = _count(u.get("input_tokens", u.get("prompt_tokens")))
        cached = None
        if "cache_read_input_tokens" in u or "cache_creation_input_tokens" in u:
            cached = _count(u.get("cache_read_input_tokens"))
            parts = [_count(u.get(k, 0)) for k in ("cache_read_input_tokens", "cache_creation_input_tokens")]
            inp = inp + sum(parts) if inp is not None and all(p is not None for p in parts) else None
        else:
            details = u.get("input_tokens_details", u.get("prompt_tokens_details"))
            if isinstance(details, dict):
                cached = _count(details.get("cached_tokens"))
        return {"at": self.at, "model": self.model, "response_id": self.response_id,
                "http_status": http_status,
                "status": "failed" if http_status >= 400 else (self.status or ("incomplete" if self.streaming else "completed")),
                "input_tokens": inp, "cached_tokens": cached,
                "output_tokens": _count(u.get("output_tokens", u.get("completion_tokens")))}
