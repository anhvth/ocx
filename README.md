# ocx

Use **Qwen (Alibaba), Claude (Anthropic), local vLLM, or any OpenAI-compatible model** in the **Codex
Desktop app** and the `codex` CLI, side by side with your normal ChatGPT models (fast mode included),
and the same models (GPT included) in **Pi**, **Claude Code** and the **Claude desktop app**.
One YAML file, one command.

```
Codex app / codex CLI ──┐
Claude Code / Claude app ┴► ocx router (127.0.0.1:11111)
                              ├─ gpt-*            ──► ChatGPT (your existing Codex login, untouched)
                              ├─ alibaba/qwen…    ──► Alibaba Token Plan
                              ├─ anthropic/claude…──► Anthropic Messages API
                              └─ local-vllm/…     ──► http://127.0.0.1:7788/v1
```

Requirements: the Codex Desktop app or `codex` CLI, Python 3.10+, and [uv](https://docs.astral.sh/uv/).
A ChatGPT sign-in (`codex login` or the app) is only needed for the GPT models.

## Development checks

Install the Git hooks once per clone before committing:

```bash
uv sync --group dev
uv run pre-commit install --install-hooks
uv run pre-commit run --all-files
uv run pytest -q
```

The commit hooks scan staged changes with Gitleaks, detect private keys, reject merge-conflict
markers and oversized files, validate YAML/TOML, and fix trailing whitespace and missing final
newlines. Hook versions are pinned to commit hashes. Keep credentials outside the checkout; common
local secret files are also ignored by Git. Prefer `api_key_env` or `api_key_file` over inline keys.

Hooks are local and can be bypassed; they do not guarantee that every secret will be detected or
remove secrets from existing Git history. If a real credential was committed, revoke/rotate it first.

## Quick start

```bash
uv tool install git+https://github.com/anhvth/opencodex-desktop   # once

# 1. ChatGPT models only: works with no config if you are signed in to Codex
#    (claude-* models join automatically when $ANTHROPIC_API_KEY is set)
ocx start

# 2. Add other models: write the config, edit it, start again
ocx init
$EDITOR ~/.config/ocx/config.yaml
ocx start

# 3. Back to plain Codex at any time
ocx off
```

Then restart the Codex app (or open a new `codex` session) and pick a model.

No install? Run any command as
`uvx --from git+https://github.com/anhvth/opencodex-desktop ocx <command>`.

### Upgrading from opencodex-desktop

If you installed the old tool with uv, stop its router and uninstall the old package before
installing OCX. Both packages provide the legacy `opencodex-desktop` command, so removing the
old package first avoids an executable conflict.

```bash
opencodex-desktop stop
uv tool uninstall opencodex-desktop
uv tool install --refresh git+https://github.com/anhvth/opencodex-desktop.git
ocx start
```

OCX reads the old config when no new config exists and reuses existing router state. It
preserves legacy provider IDs for saved threads and accepts the old token header.

## Commands

OCX checks GitHub for newer versions when you run commands like `status` and, while the router
is running, shortly after startup and once a day. Updates appear on the local dashboard and in
`router.log`. Checks have a two-second timeout and never install anything automatically.
Run `ocx update` on each machine to install the latest published code with uv and start the router
with the new code. Existing YAML is reused, not rewritten.
If updating from OCX 0.4.4 or earlier, run `ocx start` once after `ocx update`; the old updater only
installs the package. Restart Codex and Pi when needed to reload model lists.

| command | what it does |
|---|---|
| `start` (default) | reads the YAML, checks every provider, configures Codex, Claude and Pi, and (re)starts the router in the background |
| `off` | stops the router, removes its Codex/Claude settings, and restores Pi's previous model registry |
| `status` | checks the GPT model list and shows catalog gaps, exclusions, and routed models |
| `update` | installs from GitHub, refreshes managed client config, and (re)starts the router |
| `stop` | stops only the router (Codex stays pointed at it) |
| `init` | writes an example config (`--force` overwrites an existing one) |
| `serve` | runs the router in the foreground (for debugging / systemd) |

Run `start` again after any change (new model, new key, new machine). It is idempotent.
Use `--config PATH` or `$OCX_CONFIG` to point at a different YAML.

OCX configures routing and model lists; Codex, Pi, and Claude Code manage their own model defaults.
The old YAML `default_model` setting is ignored. If you turn OCX off while a routed model is
selected, pick a model the client can use without OCX.

## Web dashboard

Open `http://127.0.0.1:11111/` (or your configured listen address). Alongside account limits and
models, **Token usage per response** shows the latest 100 upstream API responses: model, response ID,
input tokens, cached input tokens, output tokens and status. It refreshes every 30 seconds.

Cached tokens are part of total input, not extra tokens. Anthropic's input total includes cache
reads and cache writes. Missing counters are shown as `—`, not zero. One chat turn can make several
API calls (for example, tool calls), so it can produce several rows. Counts come from upstream usage,
not estimates or billing calculations. ChatGPT streams labeled `application/octet-stream` are parsed
as SSE when streaming was requested. Responses that end before final usage arrives can lack counts.
Capture is bounded to 2 MiB per JSON body or SSE event; oversized data can also leave counts unavailable.

Only metadata is kept in RAM; prompts, response text and credentials are not stored in this table.
History starts when the router starts, is cleared on restart, and is not backfilled from old chats.
The dashboard and its read-only JSON are blocked through the public Claude tunnel.

## Config (`~/.config/ocx/config.yaml`)

```yaml
codex_home: [~/.codex]          # $CODEX_HOME wins; first existing entry otherwise
listen: 127.0.0.1:11111
openai: {enabled: true}         # GPT models from your Codex login

anthropic: {}                   # claude-* from $ANTHROPIC_API_KEY; or set api_key/api_key_file below

providers:
  alibaba:
    base_url: https://token-plan.maas.qwencloudapi.com/compatible-mode/v1
    api_key_file: ~/.config/claude-alibaba/token   # or api_key_env: VAR, or api_key: sk-...
    anthropic_url: https://token-plan.maas.qwencloudapi.com/apps/anthropic/v1
    models: auto
    exclude_regex: '(?i)(embedding|rerank|tts|asr)'
  local-vllm:
    base_url: http://127.0.0.1:7788/v1
    models: auto                # everything the server lists
```

Also supported (see [`examples/config.yaml`](examples/config.yaml) for all fields):

- `anthropic` — built-in Claude provider: `api_key` / `api_key_env` (default `ANTHROPIC_API_KEY`) /
  `api_key_file`, `base_url` (any Anthropic-compatible gateway), and the provider options below.
  Needs an Anthropic **API key**, not a Claude subscription.
- `exclude_regex` — optional Python regex under `openai`, `anthropic`, or a provider. A match anywhere
  in the upstream model ID (the bare slug for OpenAI) excludes it. Use `(?i)` for case-insensitive matching.
- Providers default to `models: auto`, which discovers every model returned by `/models`. Replace a
  `models` list with `models: auto` and remove any `include` allowlist to discover new models automatically.
  Existing `include` filters (provider ID substrings in auto mode, exact GPT slugs for OpenAI) and explicit
  `models` lists still work; `exclude_regex` can narrow them further.
- Per provider: `enabled`, `headers`, `wire_api` (`responses`/`chat`/`anthropic`, auto-detected if omitted).
- In an explicit `models` list, entries can set `context_window`, `max_output_tokens`, `display_name`,
  `reasoning_efforts`, `default_effort`, and `input_modalities`.

### What `start` checks

- **OpenAI auth**: reads `$CODEX_HOME/auth.json` (written by the Codex app or `codex login`), the same way
  Codex does. If you are signed in, your GPT models appear; the router forwards Codex's own auth header, so
  token refresh keeps working. If you are not, you get a warning and only the other providers are served.
- **Model updates**: the router checks the live GPT catalog and providers every 60 seconds, falling back
  to Codex's cached GPT list if the live catalog is unavailable. Changes update the generated catalog and
  Pi configuration. Restart the Codex app (or `codex app-server daemon restart` for CLI/TUI) and Pi to
  load an updated list. Configured filters still apply on refresh.
- **Unreachable provider / bad key / model not served**: you get a ✗ line and those models are **dropped**,
  so Codex never shows a model that cannot answer.
- **Context window**: taken from the config if set, else from what the provider's `/models` reports (vLLM's
  `max_model_len`, etc.), else 128k with a warning. A config value larger than what the provider reports
  is capped. Codex's auto-compaction is set to 90% of it.
- **Wire API**: `/responses` is used when the provider has it; otherwise it falls back to
  `/chat/completions` automatically. Anthropic endpoints (`wire_api: anthropic`) use the native
  `/v1/messages` API with `x-api-key` auth.

## What the router fixes for non-OpenAI models

Codex sends OpenAI-only data that other providers reject. For routed models the router:
- turns Codex Desktop's `codex_apps_*` context items (a `function_call_output` with no `call_id`, which
  Alibaba rejects with "call_id is required") into plain user messages;
- drops OpenAI-encrypted reasoning, hosted tools (web_search, image_generation), and ChatGPT-only fields
  (`service_tier`, `include`, `prompt_cache_key`, …);
- handles Codex context compaction (both `/responses/compact` and in-stream `compaction_trigger`)
  by asking the same model for a summary.

GPT requests preserve fast mode / `service_tier` and native reasoning. Foreign reasoning items with
incompatible IDs are dropped before forwarding, so switching a third-party thread to GPT works.

For Claude, the router translates Responses ↔ Anthropic Messages: tool calls (including Codex's
freeform `apply_patch`), images, and system/developer messages; reasoning effort becomes an
extended-thinking budget (`low` 4k, `medium` 16k, `high` 32k tokens, capped to fit `max_tokens`).

## Files

| path | contents |
|---|---|
| `~/.config/ocx/config.yaml` | your config (the only file you edit) |
| `~/.local/state/ocx/` | `router.log`, `router.pid`, `routes.json`, `model-catalog.json`, `token`, `tunnel.{pid,url,log}` |
| `~/.claude/settings.json` | `env` (router URL + token header), `modelPicker.options`, `modelSettings`; backed up as `settings.json.bak-ocx-*` |
| `~/Library/Application Support/Claude-3p/configLibrary/` | the Claude app's `ocx` gateway profile (macOS) |
| `$CODEX_HOME/config.toml` | gets `model_provider`, `model_catalog_json`, `[model_providers.ocx]`; backed up as `config.toml.bak-ocx-*` before every change |

Existing installs may still use `~/.config/opencodex-desktop/config.yaml` and
`~/.local/state/opencodex-desktop/`. `ocx status` shows the config in use.

API keys are read when needed and never written into Codex's config or the state dir.

## Pi

If Pi's agent directory exists, `start` also configures Pi using the **same discovery results as Codex**.
The YAML above controls the list; do not maintain a separate list in Pi.

- Adds one `ocx` provider to `~/.pi/agent/models.json`, using the router's Responses API.
  Model names are the canonical slugs, with context, output limits and reasoning levels from discovery.
- Leaves Pi's saved default provider, startup model, and picker scope under Pi's control. Use `/model`
  to switch models; press Tab in the picker to view all providers if you have a scope set.
- Honors `PI_CODING_AGENT_DIR`. `off` restores the previous provider entry from `ocx-backup.json`.
  On upgrade, OCX restores the previous picker scope only if it still matches OCX's old generated list.
- Provider health checks synchronize Pi's list too. Restart Pi after model-list changes. GPT models
  use the existing Codex login through the local router.

Pi reads the local router token at request time; upstream API keys are not copied into Pi's config.
Unknown maximum output limits use Pi's conservative 16k default. Pricing is not imported, so Pi's
cost display for these models is not a billing estimate.

## Claude Code and the Claude desktop app

`start` also writes `~/.claude/settings.json` (backed up first; `off` removes exactly what it added):
`ANTHROPIC_BASE_URL` = the router, an `x-ocx-token` header (the router's local token,
`~/.local/state/ocx/token`), `/model` rows for every GPT and routed model, and an
`autoCompactWindow` per model. On macOS it adds a gateway profile `ocx` to the Claude
app (third-party mode) with the Claude, GPT and routed models; restart the app to load it.

- `claude-*` from Claude Code go to api.anthropic.com unchanged, with Claude Code's own login (that
  token goes nowhere else). The Claude app's gateway profile lists `claude-*` only when an Anthropic
  API key is configured: a Claude subscription login is for Claude Code, and Anthropic rejects it
  from a gateway (HTTP 429). Without a key, switch the app back to your Claude account for Claude models.
- GPT models use the Codex login; the router translates Messages ↔ Responses.
- Routed models are sent to the provider's Anthropic Messages endpoint, set as `anthropic_url:`
  (defaults to `base_url` for local endpoints such as vLLM). Providers without one are Codex-only.
- Claude Code warns `unrecognized_model` for custom ids; harmless. 1M-context models get a `[1m]` suffix.
- Claude app **SSH sessions** call the gateway from the remote host, and the app refuses a localhost
  gateway for them. Opt in with
  ```yaml
  claude_app:
    ssh_hosts: [h76, h2-14, h200-login]   # ssh aliases; list jump hosts too
  ```
  and `start` puts the router behind a Cloudflare quick tunnel (`cloudflared`, `brew install cloudflared`),
  points the app's profile at it and sets `sshHostAllowlist` (aliases + resolved HostNames + ProxyJump
  hosts). Through the tunnel only `/v1/messages` and `/v1/models` answer, and only with the router token;
  everything else is 403. The URL changes when the tunnel restarts (`start` updates the profile;
  reopen the app). `stop`/`off`, or removing `claude_app`, close it. SSH sessions see the Mac's models
  (not a remote's local vLLM).

Anthropic does not support non-Claude models behind Claude Code; treat that part as best effort.

## Remote machines (Codex Desktop over SSH)

A remote Desktop chat runs its own `codex app-server` on the remote host, so run `ocx start`
**on that host** too (with its own config, e.g. `models: auto` against a local vLLM).

## Troubleshooting

- **Model missing from the picker** — read the `start` output: unreachable providers, bad keys and
  unserved models are dropped with a ✗ line. If a provider was down during `start`, run `start` again
  once it is back.
- **What is happening** — `ocx status`; the log is `~/.local/state/ocx/router.log`.
- **Port already in use** — `start` refuses; set a different `listen:` address.
- **Tiny context windows** — below ~32k Codex cannot run (its prompt and tools take ~20k).
- **Old custom-model threads after `off`** — a Desktop chat stays bound to its model's provider, so
  those threads will not continue; start a new chat.

The repo also ships an agent skill ([`skills/ocx/`](skills/ocx/SKILL.md))
that teaches coding agents to set up, verify and debug this tool.

## License

MIT
