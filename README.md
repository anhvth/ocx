# ocx

OCX routes ChatGPT, Claude, Qwen, and OpenAI-compatible models to Codex, Pi,
and Claude clients from one YAML file.

## Install

With [uv](https://docs.astral.sh/uv/) installed, run this on the machine where
your coding client runs:

```sh
uv tool install https://github.com/anhvth/ocx/archive/refs/heads/main.tar.gz
export PATH="$(uv tool dir --bin):$PATH"
ocx --version
```

The archive install works on workers whose Git lacks HTTPS support. If uv
reports an executable conflict and the existing commands belong to an older
OCX install, upgrade them in place:

```sh
uv tool install --force --refresh https://github.com/anhvth/ocx/archive/refs/heads/main.tar.gz
```

If a new shell cannot find OCX, add the directory printed by `uv tool dir --bin`
to your shell's PATH.

## Configure and run

Sign in to Codex if you want ChatGPT models. For other providers, run
`ocx init` on a fresh install, then edit `~/.config/ocx/config.yaml`. Use the
[example config](examples/config.yaml) for provider URLs, API key files, and
model filters. If OCX reports an existing config, edit that file instead.

```sh
ocx start
ocx status
```

Start OCX on the remote host for a Codex session over SSH. After starting,
reopen your client if its model picker still shows an older list. The client
controls its own default model.

Open the [local dashboard](http://127.0.0.1:11111/) on the router host to see
models and recent token usage. If a model is missing, check the provider
messages from `ocx start` and the router log in `~/.local/state/ocx/`.

Run `ocx off` to stop the router and restore client settings. Existing installs
can reuse the legacy config and state directories.

## Reference

- [Configuration example](examples/config.yaml)
- [Client setup and troubleshooting](skills/ocx/SKILL.md)
- [MIT license](LICENSE)
