"""Rewrite Codex's Responses requests so third-party upstreams accept them.

Each rule exists because a real upstream rejected the unmodified request:
- Codex Desktop adds context items (codex_apps_*) as function_call_output WITHOUT call_id from the
  2nd turn on. Alibaba: "call_id is required for function_call_output". -> become user messages.
- reasoning items carry OpenAI-encrypted content no other model can read -> dropped.
- compaction items (from ChatGPT or from our own summarizer) -> plain-text summary message.
- item ids reference server-side storage that does not exist with store=false -> stripped.
- hosted OpenAI tools (web_search, image_generation, ...) are not available elsewhere -> stripped.
- ChatGPT-only request fields (service_tier, include, reasoning.summary, text.verbosity, ...) -> stripped.
"""

from __future__ import annotations

import base64
import copy
from typing import Any

from .discovery import Route

ENVELOPE = "ocd1:"
LEGACY_ENVELOPES = ("ocx1:",)

COMPACT_PROMPT = """You are performing a CONTEXT CHECKPOINT COMPACTION. Create a handoff summary for another LLM that will resume the task.

Include:
- Current progress and key decisions made
- Important context, constraints, or user preferences
- What remains to be done (clear next steps)
- Any critical data, examples, or references needed to continue

Be concise, structured, and focused on helping the next LLM seamlessly continue the work."""

SUMMARY_PREFIX = ("Another language model started to solve this problem and produced a summary of its thinking "
                  "process. You also have access to the state of the tools that were used by that language model. "
                  "Use this to build on the work that has already been done and avoid duplicating work. Here is the "
                  "summary produced by the other language model, use the information in this summary to assist with "
                  "your own analysis:")
OPAQUE_NOTE = "[earlier conversation was compacted by another model; its summary is not readable here]"

KEEP_TOOL_TYPES = {"function", "custom"}
DROP_FIELDS = ("service_tier", "include", "prompt_cache_key", "client_metadata", "metadata",
               "previous_response_id", "background", "safety_identifier", "user", "prompt")
COMPACTION_TYPES = {"compaction", "compaction_summary", "context_compaction"}
# Input item types the OpenAI Responses API defines (a missing type means a message).
STANDARD_TYPES = {None, "message", "function_call", "function_call_output", "custom_tool_call",
                  "custom_tool_call_output", "local_shell_call", "local_shell_call_output"}


def encode_summary(text: str) -> str:
    return ENVELOPE + base64.b64encode(text.encode()).decode()


def decode_summary(blob: Any) -> str | None:
    if not isinstance(blob, str):
        return None
    for prefix in (ENVELOPE, *LEGACY_ENVELOPES):
        if blob.startswith(prefix):
            try:
                return base64.b64decode(blob[len(prefix):]).decode()
            except ValueError:
                return None
    return None


def user_message(text: str) -> dict[str, Any]:
    return {"type": "message", "role": "user", "content": [{"type": "input_text", "text": text}]}


def output_text(output: Any) -> str:
    if isinstance(output, str):
        return output
    if isinstance(output, list):
        return "\n".join(p.get("text", "") for p in output if isinstance(p, dict) and p.get("text"))
    if isinstance(output, dict) and isinstance(output.get("content"), (str, list)):
        return output_text(output["content"])
    return str(output or "")


def is_compaction_request(body: dict[str, Any]) -> bool:
    inp = body.get("input")
    return isinstance(inp, list) and bool(inp) and isinstance(inp[-1], dict) and inp[-1].get("type") == "compaction_trigger"


def sanitize_input(items: list[Any]) -> list[Any]:
    calls = {i.get("call_id") for i in items if isinstance(i, dict)
             and i.get("type") in ("function_call", "custom_tool_call", "local_shell_call") and i.get("call_id")}
    out: list[Any] = []
    for item in items:
        if not isinstance(item, dict):
            out.append(item)
            continue
        t = item.get("type")
        if t == "reasoning" or t in ("compaction_trigger", "additional_tools"):
            continue
        if t in COMPACTION_TYPES:
            decoded = decode_summary(item.get("encrypted_content"))
            out.append(user_message(f"{SUMMARY_PREFIX}\n{decoded}" if decoded else OPAQUE_NOTE))
            continue
        if t in ("function_call_output", "custom_tool_call_output") and item.get("call_id") not in calls:
            label = item.get("name") or item.get("call_id") or "context"
            out.append(user_message(f"[{label}]\n{output_text(item.get('output'))}"))
            continue
        if t not in STANDARD_TYPES:
            # Codex-internal items (e.g. Desktop's `agent_message` between sub-agents). Strict servers
            # such as vLLM reject the whole request on an unknown type, so pass the text along as a
            # user message instead and drop items that carry no text.
            text = _item_text(item)
            if text:
                who = item.get("author") or item.get("agent") or item.get("name") or item.get("sender")
                out.append(user_message(f"[{t}{f' from {who}' if isinstance(who, str) else ''}]\n{text}"))
            continue
        item = {k: v for k, v in item.items() if k != "id" and not k.startswith("internal_")}
        out.append(item)
    return out


def _item_text(item: dict[str, Any]) -> str:
    for key in ("content", "text", "message", "output", "summary"):
        v = item.get(key)
        if v:
            t = output_text(v).strip()
            if t:
                return t
    return ""


def sanitize_request(body: dict[str, Any], route: Route) -> dict[str, Any]:
    b = copy.deepcopy(body)
    b["model"] = route.upstream_model
    for k in DROP_FIELDS:
        b.pop(k, None)
    if isinstance(b.get("input"), list):
        b["input"] = sanitize_input(b["input"])
    if isinstance(b.get("tools"), list):
        b["tools"] = [t for t in b["tools"] if isinstance(t, dict) and t.get("type") in KEEP_TOOL_TYPES]
        if not b["tools"]:
            b.pop("tools")
            b.pop("tool_choice", None)
    b.pop("parallel_tool_calls", None)
    reasoning = b.get("reasoning")
    if isinstance(reasoning, dict):
        effort = reasoning.get("effort")
        if effort and effort not in route.efforts:
            effort = route.efforts[-1] if route.efforts else None
        b["reasoning"] = {"effort": effort} if effort else None
        if b["reasoning"] is None:
            b.pop("reasoning")
    text = b.get("text")
    if isinstance(text, dict):
        text.pop("verbosity", None)
        if not text:
            b.pop("text")
    b["store"] = False
    return b


def compaction_body(body: dict[str, Any], route: Route) -> dict[str, Any]:
    """A plain summarization request: same history, no tools, compaction prompt appended."""
    b = sanitize_request(body, route)
    b.pop("tools", None)
    b.pop("tool_choice", None)
    b["stream"] = False
    b["input"] = [i for i in b.get("input") or [] if not (isinstance(i, dict) and i.get("type") == "compaction_trigger")]
    b["input"].append(user_message(COMPACT_PROMPT))
    return b


def response_text(resp: Any) -> str:
    """Concatenate assistant output_text from a non-streaming Responses payload."""
    parts: list[str] = []
    for item in (resp or {}).get("output") or []:
        if isinstance(item, dict) and item.get("type") == "message":
            for c in item.get("content") or []:
                if isinstance(c, dict) and c.get("type") in ("output_text", "text") and c.get("text"):
                    parts.append(c["text"])
    return "\n".join(parts).strip()


def user_texts(items: list[Any]) -> list[str]:
    out = []
    for i in items or []:
        if isinstance(i, dict) and i.get("type", "message") == "message" and i.get("role") == "user":
            t = output_text(i.get("content"))
            if t.strip():
                out.append(t)
    return out


def compact_v1_output(user_msgs: list[str], summary: str, budget_chars: int = 80_000) -> list[dict[str, Any]]:
    """Mirror codex-rs build_compacted_history: recent user messages within ~20k tokens + summary."""
    kept: list[str] = []
    remaining = budget_chars
    for msg in reversed(user_msgs):
        if remaining <= 0:
            break
        kept.append(msg if len(msg) <= remaining else msg[-remaining:])
        remaining -= len(msg)
    kept.reverse()
    summary_text = f"{SUMMARY_PREFIX}\n{summary}" if summary.strip() else "(no summary available)"
    return [user_message(m) for m in kept] + [user_message(summary_text)]
