---
name: opencodex-desktop
description: Set up, change, debug or turn off custom models (Alibaba Qwen, Anthropic Claude, local vLLM, any OpenAI-compatible endpoint) in the Codex Desktop app and codex CLI next to ChatGPT models, using the opencodex-desktop tool (one YAML file + `opencodex-desktop start`). Use when the user wants Desktop/Codex to see or accept Qwen/Claude/vLLM/custom models, add a model or provider, fix context windows, set it up on a new machine or SSH remote, or go back to plain Codex; or when Desktop shows "Unknown model", "call_id is required for function_call_output", "missing both base_instructions", missing models in the picker, or a provider 400.
---

# opencodex-desktop

Repo: https://github.com/anhvth/opencodex-desktop (source in `~/projects/opencodex-desktop`).

## Mental model (read first)

- The Codex Desktop app-server reads ONLY `$CODEX_HOME/config.toml` (no `--profile`). So the
  provider must be in the base config. `opencodex-desktop start` writes exactly three things there:
  `model_provider`, `model_catalog_json`, `[model_providers.opencodex-desktop]` (backup first).
- The router (`127.0.0.1:11111`) sends `gpt-*` to ChatGPT with Codex's own auth header (fast mode
  passes through), and `<provider>/<model>` to that provider after sanitizing the request.
- `anthropic/claude-*` uses the native Anthropic Messages API (`x-api-key`, `/v1/messages`): the
  router translates Responses ↔ Messages, including tool calls and reasoning effort → thinking
  budget. Enabled automatically when `$ANTHROPIC_API_KEY` is set, or via the `anthropic:` config
  block. Other providers speak `/responses` or `/chat/completions` (auto-detected).
- The model picker and context window come from the catalog JSON the tool writes
  (`~/.local/state/opencodex-desktop/model-catalog.json`).
- One router per machine. A Desktop chat on an SSH remote runs its app-server on the remote host,
  so the remote needs its own `start`.

## Everything the user edits

`~/.config/opencodex-desktop/config.yaml` (create with `opencodex-desktop init`). Fields: `codex_home`
(list of candidates; `$CODEX_HOME` wins), `listen`, `openai.{enabled,include}`,
`anthropic.{enabled, api_key|api_key_env|api_key_file, base_url, models, include}` (built-in Claude
provider; key defaults to `$ANTHROPIC_API_KEY`, base_url to `https://api.anthropic.com/v1`),
`default_model`,
`providers.<name>.{base_url, api_key|api_key_env|api_key_file, models: auto|[...], include, wire_api,
headers, enabled}` (`wire_api: responses|chat|anthropic`), per model `{context_window,
max_output_tokens, reasoning_efforts, default_effort, display_name, input_modalities}`.
See `examples/config.yaml` in the repo.

## Common tasks

| want | do |
|---|---|
| first setup / after any change | `opencodex-desktop start`, then the user restarts the Codex app |
| add a model/provider | edit the YAML, `start` again |
| add Claude models | set `$ANTHROPIC_API_KEY` (or `anthropic: {api_key_file: ...}`), `start` again; pick `anthropic/claude-*` |
| fix a context window | set `context_window` on the model (vLLM reports it itself with `models: auto`) |
| back to plain Codex | `opencodex-desktop off` (stops router, removes the 3 keys, resets a routed `model`) |
| what is going on | `opencodex-desktop status`; log `~/.local/state/opencodex-desktop/router.log` |
| new machine | `uvx --from git+https://github.com/anhvth/opencodex-desktop opencodex-desktop start` |

Not installed? `uv tool install git+https://github.com/anhvth/opencodex-desktop` (or run via `uvx`).
Never print API keys; show the last 4 characters at most.

## Verify like the Desktop does

```bash
S=~/projects/opencodex-desktop/skills/opencodex-desktop/scripts
# fresh app-server with the Desktop's bundled codex + desktop flags, two turns
python3 $S/app_server_turn.py stdio /Applications/ChatGPT.app/Contents/Resources/codex-cli/CodexCLI.app/Contents/MacOS/codex alibaba/qwen3.8-max --desktop-flags
# live Desktop app-server (creates a visible test thread)
python3 $S/app_server_turn.py socket $CODEX_HOME/app-server-control/app-server-control.sock alibaba/qwen3.8-max
# CLI
codex exec --skip-git-repo-check -m alibaba/qwen3.8-max "Reply exactly: OK" </dev/null
```
Expect `RESULT: PASS`. `codex exec` without `</dev/null` waits on stdin. `codex exec resume` uses the
config's model unless you pass `-m`.

## Gotchas

1. `start` drops anything it can't use (provider unreachable, 401/403, model not in `/models`) with a ✗
   line. A missing model in the picker means: read the `start` output.
2. Provider 503/blip during `start` drops it until the next `start`.
3. Context under 32k cannot run Codex (its prompt and tools take ~20k).
4. Custom catalog entries must keep `model_messages` (or have `base_instructions`), or Codex fails with
   "missing both base_instructions and model_messages.instructions_template".
5. A Desktop chat stays bound to its model's provider. After `off`, old Qwen threads won't continue.
6. The Desktop rewrites `model` in config.toml when the user picks a model; that's expected.
7. Port taken by another program → `start` refuses; set `listen:`.
8. Hosts that can't reach GitHub over HTTPS (h2-14 behind a proxy): `uv build --wheel` on the Mac,
   `scp` the wheel, then `uv tool install --force <wheel>` there.
9. On NFS homes, `opencodex-desktop stop` before reinstalling, or uv fails with "Directory not
   empty / resource busy" (the running router holds `.nfs*` files). Then `start` again.
10. After `start` (or a restart of the router), run `opencodex-desktop start` again on a host whose
    vLLM was down: its models come back only after a new `start`.
11. Claude needs an Anthropic **API key** (console.anthropic.com), not a Claude Pro/Max subscription.
    For an Anthropic-compatible gateway set `anthropic.base_url` (wire stays `anthropic`).
12. Claude thinking budgets are capped to `max_tokens - 1024`; if `max_output_tokens` is tiny,
    thinking is silently disabled. Forced tool use (`tool_choice: required`) is downgraded to auto
    while thinking is on (Anthropic rejects the combination).
11. The `codex` TUI (and `cdx`) attaches to a long-lived app-server daemon that loaded the catalog when it
    started (can be days old). New models missing from `/model` → quit codex sessions, then
    `codex app-server daemon restart`. The picker shows ~8 rows; custom models are sorted first.
12. Old slugs (e.g. `qwen-token-plan/qwen4-max-preview`) are matched by model name to the one route serving
    that model; unknown `x/y` slugs get a 404 listing the available models.
