# opencodex-desktop

Use **Qwen (Alibaba), Claude (Anthropic), local vLLM, or any OpenAI-compatible model** in the **Codex
Desktop app** and the `codex` CLI, side by side with your normal ChatGPT models (fast mode included),
and the same models (GPT included) in **Pi**, **Claude Code** and the **Claude desktop app**.
One YAML file, one command.

```
Codex app / codex CLI ──┐
Claude Code / Claude app ┴► opencodex-desktop router (127.0.0.1:11111)
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
opencodex-desktop start

# 2. Add other models: write the config, edit it, start again
opencodex-desktop init
$EDITOR ~/.config/opencodex-desktop/config.yaml
opencodex-desktop start

# 3. Back to plain Codex at any time
opencodex-desktop off
```

Then restart the Codex app (or open a new `codex` session) and pick a model.

No install? Run any command as
`uvx --from git+https://github.com/anhvth/opencodex-desktop opencodex-desktop <command>`.

## Commands

| command | what it does |
|---|---|
| `start` (default) | reads the YAML, checks every provider, configures Codex, Claude and Pi, and (re)starts the router in the background |
| `off` | stops the router, removes its Codex/Claude settings, and restores Pi's previous model scope and defaults |
| `status` | shows the login, the router, what Codex points at, and the routed models with their context windows |
| `stop` | stops only the router (Codex stays pointed at it) |
| `init` | writes an example config (`--force` overwrites an existing one) |
| `serve` | runs the router in the foreground (for debugging / systemd) |

Run `start` again after any change (new model, new key, new machine). It is idempotent.
Use `--config PATH` or `$OPENCODEX_DESKTOP_CONFIG` to point at a different YAML.

## Config (`~/.config/opencodex-desktop/config.yaml`)

```yaml
codex_home: [~/.codex]          # $CODEX_HOME wins; first existing entry otherwise
listen: 127.0.0.1:11111
openai: {enabled: true}         # GPT models from your Codex login
# default_model: alibaba/qwen4-max-preview

anthropic: {}                   # claude-* from $ANTHROPIC_API_KEY; or set api_key/api_key_file below

providers:
  alibaba:
    base_url: https://token-plan.maas.qwencloudapi.com/compatible-mode/v1
    api_key_file: ~/.config/claude-alibaba/token   # or api_key_env: VAR, or api_key: sk-...
    models:
      - qwen4-max-preview: {context_window: 1000000, max_output_tokens: 393216}
      - qwen3.8-max:       {context_window: 1000000, max_output_tokens: 131072}
  local-vllm:
    base_url: http://127.0.0.1:7788/v1
    models: auto                # everything the server lists
```

Also supported (see [`examples/config.yaml`](examples/config.yaml) for all fields):

- `anthropic` — built-in Claude provider: `api_key` / `api_key_env` (default `ANTHROPIC_API_KEY`) /
  `api_key_file`, `base_url` (any Anthropic-compatible gateway), `models: auto` or a list, `include`,
  `enabled`. Needs an Anthropic **API key**, not a Claude subscription.
- `openai.include` — optional allow-list of GPT slugs to expose. Omit it to include newly released GPT
  models automatically.
- Per provider: `enabled`, `headers`, `wire_api` (`responses`/`chat`/`anthropic`, auto-detected if omitted),
  and `include` — substring filters when `models: auto`.
- Per model: `display_name`, `reasoning_efforts`, `default_effort`, `input_modalities`.

### What `start` checks

- **OpenAI auth**: reads `$CODEX_HOME/auth.json` (written by the Codex app or `codex login`), the same way
  Codex does. If you are signed in, your GPT models appear; the router forwards Codex's own auth header, so
  token refresh keeps working. If you are not, you get a warning and only the other providers are served.
- **Model updates**: the router checks the live GPT catalog and providers every 60 seconds, falling back
  to Codex's cached GPT list if the live catalog is unavailable. Changes update the generated catalog and
  Pi configuration. Restart the Codex app (or `codex app-server daemon restart` for CLI/TUI) and Pi to
  load an updated list; an `openai.include` allow-list still limits which GPT models appear.
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

GPT requests pass through untouched (fast mode / `service_tier` included).

For Claude, the router translates Responses ↔ Anthropic Messages: tool calls (including Codex's
freeform `apply_patch`), images, and system/developer messages; reasoning effort becomes an
extended-thinking budget (`low` 4k, `medium` 16k, `high` 32k tokens, capped to fit `max_tokens`).

## Files

| path | contents |
|---|---|
| `~/.config/opencodex-desktop/config.yaml` | your config (the only file you edit) |
| `~/.local/state/opencodex-desktop/` | `router.log`, `router.pid`, `routes.json`, `model-catalog.json`, `token`, `tunnel.{pid,url,log}` |
| `~/.claude/settings.json` | `env` (router URL + token header), `modelPicker.options`, `modelSettings`; backed up as `settings.json.bak-opencodex-desktop-*` |
| `~/Library/Application Support/Claude-3p/configLibrary/` | the Claude app's `opencodex-desktop` gateway profile (macOS) |
| `$CODEX_HOME/config.toml` | gets `model_provider`, `model_catalog_json`, `[model_providers.opencodex-desktop]`; backed up as `config.toml.bak-opencodex-desktop-*` before every change |

API keys are read when needed and never written into Codex's config or the state dir.

## Pi

If Pi's agent directory exists, `start` also configures Pi using the **same discovery results as Codex**.
The YAML above controls the list; do not maintain a separate list in Pi.

- Adds one `opencodex-desktop` provider to `~/.pi/agent/models.json`, using the router's Responses API.
  Model names are the canonical slugs, with context, output limits and reasoning levels from discovery.
- Sets `enabledModels` to `opencodex-desktop/*`, so `/model` defaults to a clean, project-managed list.
  Pi's Tab key can still switch the picker to all providers; existing providers and logins are preserved.
- Selects an available startup model (retains the current slug if available, otherwise the YAML default
  or first discovered model). Resumed sessions retain their model: use `/model` to switch old sessions.
- Honors `PI_CODING_AGENT_DIR`. Saves previous managed settings in `opencodex-desktop-backup.json`
  inside that directory; `off` restores them without reverting unrelated settings.
- Provider health checks synchronize Pi's list too. Restart Pi after a model-list change to refresh
  its scope (Pi resolves scoped patterns at startup). GPT models use the existing Codex login through
  the local router.

Pi reads the local router token at request time; upstream API keys are not copied into Pi's config.
Unknown maximum output limits use Pi's conservative 16k default. Pricing is not imported, so Pi's
cost display for these models is not a billing estimate.

## Claude Code and the Claude desktop app

`start` also writes `~/.claude/settings.json` (backed up first; `off` removes exactly what it added):
`ANTHROPIC_BASE_URL` = the router, an `x-opencodex-token` header (the router's local token,
`~/.local/state/opencodex-desktop/token`), `/model` rows for every GPT and routed model, and an
`autoCompactWindow` per model. On macOS it adds a gateway profile `opencodex-desktop` to the Claude
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

A remote Desktop chat runs its own `codex app-server` on the remote host, so run `opencodex-desktop start`
**on that host** too (with its own config, e.g. `models: auto` against a local vLLM).

## Troubleshooting

- **Model missing from the picker** — read the `start` output: unreachable providers, bad keys and
  unserved models are dropped with a ✗ line. If a provider was down during `start`, run `start` again
  once it is back.
- **What is happening** — `opencodex-desktop status`; the log is `~/.local/state/opencodex-desktop/router.log`.
- **Port already in use** — `start` refuses; set a different `listen:` address.
- **Tiny context windows** — below ~32k Codex cannot run (its prompt and tools take ~20k).
- **Old custom-model threads after `off`** — a Desktop chat stays bound to its model's provider, so
  those threads will not continue; start a new chat.

The repo also ships an agent skill ([`skills/opencodex-desktop/`](skills/opencodex-desktop/SKILL.md))
that teaches coding agents to set up, verify and debug this tool.

## License

MIT
