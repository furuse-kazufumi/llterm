[日本語](README.md) | **English**

# llterm

**A self-driving terminal host dedicated to Claude Code** — hosts Claude Code (or any CLI) in a shrunken PTY, providing an IME-stable separated input field and a structured control plane (llterm-ctl) that lets Claude control its own session.

## Why this exists

Typing long, multi-line, or Japanese-IME text into Claude Code's TUI can corrupt input when the screen redraws mid-composition. Also, for Claude to "self-drive" — rotating its own sessions and injecting follow-up tasks — it needs an **out-of-band structured control channel**, not parsing of spoken strings. llterm solves both in a single host process.

- **Display is fully compatible**: the child (claude) runs on a PTY 4 rows shorter than the real terminal, and the TUI passes straight through to the upper area
- **Input is fully separated**: the bottom 4 rows are llterm's own input field. The child's redraws never reach it (isolated via DECSTBM + shrunken PTY)
- **Control is fail-closed**: a file protocol under the `.llterm/` directory. Unknown actions and broken JSON are never executed — they are quarantined, and every event is recorded in an append-only ledger

## Installation

```
py -3.11 -m pip install -e .[dev]
```

The only dependency is `pywinpty` (Windows only). Tests: `py -3.11 -m pytest tests/ -v`

## Usage

```
llterm                      # launch and host claude
llterm -- pwsh -NoLogo      # host any child command (for debugging)
py -3.11 -m llterm.app      # direct script launch works the same
```

### Key bindings (input field)

| Key | Action |
|---|---|
| Printable keys / IME commit | Insert character (composition is shown at the input-field cursor) |
| Enter | **Insert newline** (does not send) |
| Ctrl+Enter / Shift+Enter | **Send** (injected in one shot via bracketed paste) |
| ↑↓←→ | Cursor movement only |
| Ctrl+↑ / Ctrl+↓ (Shift also works) | Input history previous / next |
| Backspace | Delete one character (joins lines at line start) |

Multi-line pastes are kept as a single input (CRLF/CR normalized to LF, re-converted to CR for the TUI on send).

**Empty-field passthrough**: only while the input field is **empty**, plain arrow keys / Enter are forwarded directly to claude.
Use this to operate claude's selection UIs (initial trust prompt, menus, model selection).
As soon as one character is typed, the local editing rules above (R12/R13) take over — no accidental sends during multi-line editing.

**Automatic forwarding of terminal query replies**: the real terminal's responses to claude's capability queries (DA1 / OSC 11, etc.) are forwarded to claude separately from key input (they never leak into the input field).

## Display language (i18n)

UI strings (GUI labels / tooltips / CLI error messages) are resolved via a lightweight
message table (`llterm.i18n`). The display locale is resolved in this order:

1. environment variable `LLTERM_LANG` (e.g. `ja` / `en`)
2. OS locale auto-detection
3. default `ja`

zh / ko can be added later by extending the message table (the structure is already in place).
The instruction prompts sent to Claude by the loop driver are intentionally **not** localized —
they are agent instructions, not user-facing display text.

## Control plane (llterm-ctl)

Claude (or a human) posts control commands with the emit CLI:

```
py -3.11 -m llterm.ctl emit rotate --reason "context 80%"
py -3.11 -m llterm.ctl emit inject-task --reason "follow-up" --arg "title=do X" --arg "priority=5"
py -3.11 -m llterm.ctl emit query-state --reason "health check"
```

- Allowed actions: `rotate` / `set-effort` / `inject-task` / `fork-session` / `query-state` / `shutdown` (the v1 executor implements only rotate / inject-task / query-state; the gate REJECTs the rest)
- `--reason` is audit-mandatory. Omitting it is rejected with exit 2 (fail-closed)
- `shutdown` always has `requires_human` forced on and is queued for human approval (`pending_human`)
- Results are written back to `.llterm/results/<id>.json` (Claude reads them on its next turn)
- Every event (received / held / rejected / executed / error) is appended to `.llterm/ledger.jsonl`

Example call intended for the Claude-side CLAUDE.md:

> When context pressure rises, run `py -3.11 -m llterm.ctl emit rotate --reason "<situation>"` and
> note the id from the result JSON. For rotate, llterm notifies the restart wrapper via exit code 75.

## Acceptance checklist (spec R10-R13)

60 automated tests green. Items to verify in a live smoke run (real claude / pwsh):

- [ ] **R10**: Japanese IME composition is stable in the input field (committed characters reach the input field)
- [x] **R11**: multi-line paste is kept as one input (verified by automated tests + spike)
- [x] **R12**: Enter = insert newline, Ctrl/Shift+Enter = send (automated tests + spike proved modifier-key detection)
- [x] **R13**: arrows = cursor movement, Ctrl/Shift+↑↓ = history (automated tests)
- [x] Child TUI stays within the upper area and never invades the input field (spike proved with pwsh / heavy output / claude --help)
- [ ] One end-to-end turn with real claude (send → response display → /exit)

## Known lessons from the design (proven by spikes)

- **Read hang after child exit**: winpty's blocking read does not return after the child exits (ConPTY drain problem). Isolate the reader into a daemon thread and escape via an EOF flag
- **Input starvation**: putting a blocking read in a single-threaded loop stalls key handling while the child is silent. Make reads non-blocking (via a deque) and run the loop on a 10ms tick
- **Modifier-key repeat flood**: standalone Ctrl/Shift keydowns arrive in large numbers due to key repeat. Do not redraw on `Action.NONE`

## References

- Spec: fullsense research `llterm_spec_2026_06_06.md` (R1-R13)
- Implementation plan: `docs/plans/2026-06-06-llterm-v1.md`
- Spike evidence: `docs/spike_results.md`
