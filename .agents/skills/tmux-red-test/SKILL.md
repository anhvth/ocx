---
name: tmux-red-test
description: Write and run a failing regression test against a real interactive terminal app before fixing it. Use when asked for a red test, a TUI reproduction, keyboard or picker testing, or a restart/persistence check using tmux.
---

# Write a red test with tmux

## Red first, then fix

1. State the exact user flow and expected outcome. Specify what must survive a restart and what is intentionally session-only. Read the app's UI contract so the test doesn't turn intended behavior into a bug.
2. Write the smallest automated test that drives the real TUI and asserts the expected outcome. Use the actual launcher when it may affect the result. Create a disposable working directory, config, and session store; use fixture auth or an isolated mock endpoint if needed. Don't mutate live settings, send real prompts, or restart a running service just to exercise the UI.
3. Run the test **before editing production code**. A valid red run reaches the intended UI state and fails the behavior assertion with expected and actual values. A startup error, missing fixture, wrong key, or timeout waiting for the picker is a test setup failure, not a red regression.
4. Fix the cause and rerun the same test against the same workflow. If the red test exposed a misunderstanding instead of a bug, keep that evidence and demonstrate the supported workflow separately. Don't silently change an assertion or keystroke and call it a code fix.

## Drive and observe the pane

Use a private tmux socket and a unique session name. Disable user tmux config for a clean reproduction unless that config is relevant to the failure. Retain the transcript on failure and clean up only the test's server.

```bash
scratch=$(mktemp -d "${TMPDIR:-/tmp}/tui-red.XXXXXX")
socket="tui-red-$$"
session=code-agent-codex-tui-red
t() { tmux -L "$socket" "$@"; }
trap 't capture-pane -pt "$session" -S -150 > "$scratch/pane.log" 2>/dev/null || :; t kill-server 2>/dev/null || :; printf "Evidence: %s\n" "$scratch"' EXIT
t -f /dev/null new-session -d -s "$session" -x 110 -y 28 -c "$scratch"
# After the shell is ready, type literal text separately from named keys.
t send-keys -t "$session" -l 'your-launcher'
t send-keys -t "$session" Enter
```

- Poll `capture-pane -pt "$session"` with a bounded deadline for the expected prompt, picker, selected row, or status. Wait for each transition before sending the next key. Don't make long sleeps the test's synchronization.
- Use `send-keys -l` for text and ordinary `send-keys` for key names (`Enter`, `C-s`, `C-d`). Use exact or anchored checks when shell input or prior output could match. Check the visible pane for current UI state; use `-S -150` for failure diagnostics and restart markers. Avoid retaining secrets in transcripts.
- For relaunch tests, run the launcher twice in the same shell with a unique marker printed **between processes** (for example, `your-launcher; echo __APP_EXITED__; your-launcher`). Assert the startup state after the marker, not a stale line from the first run. Send quit keys only while the intended process is active. If persistence is the claim, also read the resulting preference when possible, but don't use the file value as a substitute for observing the relaunched UI.

## Example: Pi's startup model

Seed a scratch `PI_CODING_AGENT_DIR` with two available models and a known default. Start `pi`, open `/model`, choose the second model, check the status/footer, exit, then start a **fresh** `pi` and check its footer after the restart marker. On Pi 1.5.2, Enter changes only the current session; `Ctrl+S` saves the highlighted model as the default. A test expecting Enter to persist must record that mismatch rather than treating intended session-only behavior as a production bug.
