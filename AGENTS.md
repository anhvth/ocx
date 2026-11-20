# AGENTS.md

Notes for coding agents working on ocx. User-facing docs are in README.md; the
operator runbook is `skills/ocx/SKILL.md`.

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

## Model capabilities and configuration ownership

**`ocx start` owns generating and writing harness configuration.** Coding agents maintain the
source configuration, capability rules and regression tests; do not manually patch each harness's
live generated settings. For an operator-authorized local override, edit the ocx YAML, then run
`ocx start` to regenerate. Preserve unrelated settings, credentials and the user's startup model.
Do not restart active harness sessions or shared services just to refresh a picker without permission.

### Source of truth and discovery

- Source config: `~/.config/ocx/config.yaml` (respect configured/XDG paths and legacy installations).
- Capability precedence: explicit per-model YAML (`reasoning_efforts`, `default_effort`) > verified
  provider/model capability mapping > existing generic defaults for unknown models.
- Keep verified entries keyed by provider + exact upstream model ID: different serving implementations
  can support different efforts for the same model. This is the intended mapping policy; discovery
  currently uses model-specific conditionals, not a general capability registry.
- Store supported efforts and a default, not a second rejected-effort configuration list. Keep rejected
  values as regression cases. A configured default must belong to the supported list.
- Unknown-model defaults are **unverified**, not a claim of compatibility. When an option fails,
  verify the upstream behavior, update the shared capability rule/config and add a regression test.
  Do not invent a supported list from a model's name or duplicate it in each harness.
- `/models` checks provider reachability and model presence, not successful inference. Context limits
  are discovered/configured separately; unreachable providers and models below 32k are omitted.
- There is no standard OpenAI-compatible per-model reasoning-capabilities endpoint. Use explicit
  capability metadata when available; a generic OpenAPI enum is not model-specific evidence.
  Do not add automatic inference probes during discovery. Existing wire-format detection can send
  a small inference request when uncached; that is separate from capability verification.
- Authorized direct API probes can verify accepted/rejected values. HTTP 200 alone does not prove
  that an effort changes model behavior; inspect serving/template code for that stronger claim.

### Generation and placement

`ocx start` runs discovery, saves routes, builds the Codex catalog, and calls the Codex, Claude and
Pi setup functions. Each adapter translates the same shared capabilities into its harness's format:

| Consumer | Generated files and effort representation |
|---|---|
| Router | `$XDG_STATE_HOME/ocx/routes.json` (default `~/.local/state/ocx/routes.json`); route efforts/default used to normalize unsupported client values |
| Codex CLI/Desktop | State directory's `model-catalog.json`, referenced by `$CODEX_HOME/config.toml`; `supported_reasoning_levels` and `default_reasoning_level` |
| Pi | `$PI_CODING_AGENT_DIR/models.json` (default `~/.pi/agent/models.json`); `thinkingLevelMap`, with unsupported levels mapped to `null` |
| Claude Code | `$CLAUDE_CONFIG_DIR/settings.json` (default `~/.claude/settings.json`); model-picker entries and per-model default `effortLevel`, not a complete supported-effort picker restriction |
| Claude Desktop, third-party mode | `~/Library/Application Support/Claude-3p/configLibrary/`; gateway profile when that app directory exists on macOS |

Existing installs may retain `~/.local/state/opencodex-desktop/`; use the resolved `STATE_DIR`, not
an assumed new path. Pi config is generated only when its agent directory already exists. Other
clients consume the router's API; do not claim every harness has generated local settings.

Codex caches its catalog at app-server startup (see below). Pi reloads `models.json` when `/model`
opens; reselect the model to use refreshed metadata. Generating config does not guarantee an already
running client has refreshed its in-memory model.

Pi treats a non-empty `enabledModels` as an allowlist, so a scope written from an older discovery
hides every model added since. Pi sync therefore removes an OCX-only scope that names a model no
longer discovered, restoring the full list; a live selection, or one naming another provider's
models, is the user's choice and stays untouched.

### Verified local DeepSeek behavior

Direct checks against `http://127.0.0.1:7788/v1/responses` for
`deepseek-ai/DeepSeek-V4.1-Flash-1017` accepted `none`, `low`, `high`, `xhigh`, `max`, and rejected
`minimal`, `medium` with HTTP 400. The server's error explicitly lists `low`, `high`, `xhigh`, `max`
and integer efforts 1–100 in `chat_template_kwargs` (integer values were not tested).

**Do not remove `high` on the assumption that it is unsupported.** The earlier Pi/tmux test expecting
`high` absent encoded a mistaken contract; capability tests must follow verified upstream behavior.
These observations apply to that deployment, not every DeepSeek server. Choosing the default effort
is policy; the API checks did not discover the server's default. Updating this document alone does
not update discovery rules or regenerate installed harness configs.

## Router endpoints (one address, two API formats)

`http://127.0.0.1:11111`; authenticated calls use the router token (`~/.local/state/ocx/token`
on new installs) as `x-ocx-token`, `x-api-key` or `Authorization: Bearer`. Codex may send its own
login instead. Keep accepting `x-opencodex-token` and preserve provider IDs used by existing threads.

- OpenAI Responses (Codex, Pi): `POST /v1/responses`, `/v1/responses/compact`; other paths pass
  through to OpenAI. `/v1/chat/completions` is not translated for GPT.
- Anthropic Messages (Claude Code, Claude app, OpenClaude `--provider custom-anthropic`): base URL
  without `/v1`; `POST /v1/messages`, `/v1/messages/count_tokens`, `GET /v1/models` with
  `anthropic-version`. `claude-*` -> api.anthropic.com (Claude Code's own login, others need an API
  key); `gpt-*` -> translated to Responses; `<provider>/*` -> that provider's `anthropic_url`
  (local vLLM defaults to its own `/v1/messages`; providers without one are not listed).
- No token: `GET /` status page, `/healthz`, `HEAD` probes. Through the tunnel only the Claude
  endpoints with the token work.

## Things that are easy to get wrong

- Codex only reads `$CODEX_HOME/config.toml` (the Desktop app-server takes no `--profile`) and reads
  `model_catalog_json` **once when an app-server starts**. After changing models: restart the Codex app,
  `codex app-server daemon restart` for the CLI/TUI, and on SSH remotes kill the remote app-server so
  the Desktop respawns it. A stale app-server shows old slugs; that isn't a naming bug.
- Custom catalog entries need `model_messages` (or `base_instructions`), or Codex refuses the catalog.
- Codex uses `priority` for both the TUI `/model` picker (which shows ~8 rows) and automatic model
  selection. Keep the leading GPT model first and custom models immediately below it.
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
`skills/ocx/scripts/app_server_turn.py`.
