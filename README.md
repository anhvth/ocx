# opencodex-desktop

Use **Qwen (Alibaba), local vLLM, or any OpenAI-compatible model** in the **Codex Desktop app** and the
`codex` CLI, side by side with your normal ChatGPT models (fast mode included). There is one YAML file
and one command.

```
Codex app / codex CLI ──► opencodex-desktop router (127.0.0.1:11111)
                              ├─ gpt-*            ──► ChatGPT (your existing Codex login, untouched)
                              ├─ alibaba/qwen…    ──► Alibaba Token Plan
                              └─ local-vllm/…     ──► http://127.0.0.1:7788/v1
```

## Quick start

```bash
# 1. ChatGPT models only: works with no config if you are signed in to Codex (app or `codex login`)
uvx --from git+https://github.com/anhvth/opencodex-desktop opencodex-desktop start

# 2. Add other models: write the config, edit it, start again
uvx --from git+https://github.com/anhvth/opencodex-desktop opencodex-desktop init
$EDITOR ~/.config/opencodex-desktop/config.yaml
uvx --from git+https://github.com/anhvth/opencodex-desktop opencodex-desktop start

# 3. Back to plain Codex at any time
uvx --from git+https://github.com/anhvth/opencodex-desktop opencodex-desktop off
```

Then restart the Codex app (or open a new `codex` session) and pick a model.

Tip: `uv tool install git+https://github.com/anhvth/opencodex-desktop` gives you a plain `opencodex-desktop` command.

## Commands

| command | what it does |
|---|---|
| `start` (default) | reads the YAML, checks every provider, writes the model catalog and `$CODEX_HOME/config.toml`, and (re)starts the router in the background |
| `off` | stops the router and removes its settings from `config.toml`, putting Codex back to its defaults |
| `status` | shows the login, the router, what Codex points at, and the routed models with their context windows |
| `stop` | stops only the router (Codex stays pointed at it) |
| `init` | writes an example config to `~/.config/opencodex-desktop/config.yaml` |
| `serve` | runs the router in the foreground (for debugging / systemd) |

Run `start` again after any change (new model, new key, new machine). It is idempotent.

## Config (`~/.config/opencodex-desktop/config.yaml`)

```yaml
codex_home: [~/.codex]          # $CODEX_HOME wins; first existing entry otherwise
listen: 127.0.0.1:11111
openai: {enabled: true}         # GPT models from your Codex login
# default_model: alibaba/qwen4-max-preview

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

See [`examples/config.yaml`](examples/config.yaml) for all fields.

### What `start` checks

- **OpenAI auth**: reads `$CODEX_HOME/auth.json` (written by the Codex app or `codex login`), the same way
  Codex does. If you are signed in, your GPT models appear; the router forwards Codex's own auth header, so
  token refresh keeps working. If you are not, you get a warning and only the other providers are served.
- **Unreachable provider / bad key / model not served**: you get a ✗ line and those models are **dropped**,
  so Codex never shows a model that cannot answer.
- **Context window**: taken from the config if set, else from what the provider's `/models` reports (vLLM's
  `max_model_len`, etc.), else 128k with a warning. A config value larger than what the provider reports
  is capped. Codex's auto-compaction is set to 90% of it.
- **Wire API**: `/responses` is used when the provider has it; otherwise it falls back to
  `/chat/completions` automatically.

## What the router fixes for non-OpenAI models

Codex sends OpenAI-only data that other providers reject. For routed models the router:
- turns Codex Desktop's `codex_apps_*` context items (a `function_call_output` with no `call_id`, which
  Alibaba rejects with "call_id is required") into plain user messages;
- drops OpenAI-encrypted reasoning, hosted tools (web_search, image_generation), and ChatGPT-only fields
  (`service_tier`, `include`, `prompt_cache_key`, …);
- handles Codex context compaction (both `/responses/compact` and in-stream `compaction_trigger`)
  by asking the same model for a summary.

GPT requests pass through untouched (fast mode / `service_tier` included).

## Files

| path | contents |
|---|---|
| `~/.config/opencodex-desktop/config.yaml` | your config (the only file you edit) |
| `~/.local/state/opencodex-desktop/` | `router.log`, `router.pid`, `routes.json`, `model-catalog.json` |
| `$CODEX_HOME/config.toml` | gets `model_provider`, `model_catalog_json`, `[model_providers.opencodex-desktop]`; backed up as `config.toml.bak-opencodex-desktop-*` before every change |

API keys are read when needed and never written into Codex's config or the state dir.

## Remote machines (Codex Desktop over SSH)

A remote Desktop chat runs its own `codex app-server` on the remote host, so run `opencodex-desktop start`
**on that host** too (with its own config, e.g. `models: auto` against a local vLLM).

## License

MIT
