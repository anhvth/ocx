# AGENTS.md

Notes for coding agents working on opencodex-desktop. User-facing docs are in README.md; the
operator runbook is `skills/opencodex-desktop/SKILL.md`.

## Model naming (canonical slugs)

Every non-OpenAI model Codex sees is named `<provider>/<model-id>`:

- `<provider>` is the key under `providers:` in the YAML and names **who serves it** (the vendor or
  host): `alibaba`, `anthropic`, `local-vllm`. Name it after the vendor/host, not after a plan, gateway or
  product tier (`alibaba`, not `qwen-token-plan` / `dashscope-intl` / `opencodex-local`).
- `<model-id>` is the upstream id **exactly as the provider's `/models` returns it** (no renaming, no
  case changes, slashes kept): `alibaba/qwen4-max-preview`, `alibaba/glm-5.3`,
  `alibaba/deepseek-v4-pro`, `local-vllm/deepseek-ai/DeepSeek-V4.1-Flash-1017`.
- So the model family is readable from the id: `alibaba/qwen*`, `alibaba/glm*`, `alibaba/deepseek*`.
- OpenAI models keep their bare slugs (`gpt-6.1-sol`); a slug without `/` always means ChatGPT/OpenAI.
  Never add `/` to an OpenAI slug, and never route a bare slug to another provider.
- `display_name` defaults to the slug. Only set it to add information, and keep the slug visible in it.
- Renaming a provider or model breaks existing Codex threads that stored the old slug. The router maps an
  unknown `x/<model-id>` to the single route serving `<model-id>` (`resolve_alias`); keep that working.
  Don't add per-name alias tables.

## Things that are easy to get wrong

- Codex only reads `$CODEX_HOME/config.toml` (the Desktop app-server takes no `--profile`) and reads
  `model_catalog_json` **once when an app-server starts**. After changing models: restart the Codex app,
  `codex app-server daemon restart` for the CLI/TUI, and on SSH remotes kill the remote app-server so
  the Desktop respawns it. A stale app-server shows old slugs; that isn't a naming bug.
- Custom catalog entries need `model_messages` (or `base_instructions`), or Codex refuses the catalog.
- The TUI `/model` picker shows ~8 rows sorted by `priority`; custom models get lower priorities so they
  are listed first.
- Models below 32k context, or from an unreachable provider, are not listed (warning, not error).
- Codex sends its own item types in `input` (Desktop sub-agents add `agent_message`). Lenient providers
  (Alibaba) ignore them, but vLLM rejects the whole request ("N validation errors ... EasyInputMessageParam").
  `sanitize_input` keeps only standard Responses types and turns the rest into text; test new
  transforms against vLLM, not just Alibaba.
- Claude face (`messages_adapter.py`, `claude_setup.py`): Claude Code's own OAuth token is passed
  through to api.anthropic.com only, never to another provider. Never use a Claude subscription login
  for anything but Claude Code's own requests (the Claude app's gateway needs an Anthropic API key).
  The rule is about the credential, not the model: claude-* reach Codex, Pi, OpenClaude and the Claude
  app only through an `anthropic:` API key. Without one (the owner's choice: no key) those clients list
  GPT + `<provider>/*` only, and Claude models stay in Claude Code. Don't add an OAuth fallback, don't
  present requests as Claude Code's, and don't add an `anthropic:` block whose key file is missing
  (`start` fails for every model). The other direction is fine: Claude Code may use any routed model. Test Claude Code with `claude -p ... --settings <file>` pointing at an
  isolated router port, never by editing the user's live `~/.claude/settings.json` by hand.
- Never print or commit API keys; show at most the last 4 characters.

## Testing

`uv run pytest -q`. For behaviour Codex depends on, test with real Codex traffic against an isolated
`CODEX_HOME` + `XDG_STATE_HOME` and a non-default `listen:` port, never the user's live router:
`codex exec --skip-git-repo-check -m <slug> "..." </dev/null`, the TUI in tmux, and
`skills/opencodex-desktop/scripts/app_server_turn.py`.
