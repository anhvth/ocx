---
name: opencodex-desktop
description: Set up, change, debug or turn off custom models (Alibaba Qwen, local vLLM, any OpenAI-compatible endpoint) in the Codex Desktop app and codex CLI next to ChatGPT models, using the opencodex-desktop tool (one YAML file + `opencodex-desktop start`). Use when the user wants Desktop/Codex to see or accept Qwen/vLLM/custom models, add a model or provider, fix context windows, set it up on a new machine or SSH remote, or go back to plain Codex; or when Desktop shows "Unknown model", "call_id is required for function_call_output", "missing both base_instructions", missing models in the picker, or a provider 400.
---

# opencodex-desktop

Repo: https://github.com/anhvth/opencodex-desktop (source in `~/projects/opencodex-desktop`).

## Mental model (read first)

- The Codex Desktop app-server reads ONLY `$CODEX_HOME/config.toml` (no `--profile`). So the
  provider must be in the base config. `opencodex-desktop start` writes exactly three things there:
  `model_provider`, `model_catalog_json`, `[model_providers.opencodex-desktop]` (backup first).
- The router (`127.0.0.1:11111`) sends `gpt-*` to ChatGPT with Codex's own auth header (fast mode
  passes through), and `<provider>/<model>` to that provider after sanitizing the request.
- The model picker and context window come from the catalog JSON the tool writes
  (`~/.local/state/opencodex-desktop/model-catalog.json`).
- One router per machine. A Desktop chat on an SSH remote runs its app-server on the remote host,
  so the remote needs its own `start`.

## Everything the user edits

`~/.config/opencodex-desktop/config.yaml` (create with `opencodex-desktop init`). Fields: `codex_home`
(list of candidates; `$CODEX_HOME` wins), `listen`, `openai.{enabled,include}`, `default_model`,
`providers.<name>.{base_url, api_key|api_key_env|api_key_file, models: auto|[...], include, wire_api,
headers, enabled}`, per model `{context_window, max_output_tokens, reasoning_efforts, default_effort,
display_name, input_modalities}`. See `examples/config.yaml` in the repo.

## Common tasks

| want | do |
|---|---|
| first setup / after any change | `opencodex-desktop start`, then the user restarts the Codex app |
| add a model/provider | edit the YAML, `start` again |
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
