# Terminal Status Detection & Content Retrieval Redesign

**Date**: 2026-06-05
**Status**: Draft

## Problem

The terminal tool system has four root issues:

1. **False "stuck" detection**: `CommandTool` check #4 uses idle-time thresholds (5s/15s) to declare `input_wait`, falsely flagging slow-but-active commands (builds, npm install, slow downloads).
2. **Stale output in `process log` / `terminal current`**: After CommandTool's poll loop stops, no one reads from the PTY. These tools return snapshots, not current data.
3. **Missing terminal state in running output**: `_format_running()` only returns screen content for APPLICATION cursor mode (vim/htop). Normal commands show raw output without any indication of where the terminal is.
4. **Tool overlap**: `process log` and `terminal current` return overlapping content from different sources, confusing the LLM.

## Design

### Status Model

Replace the current ad-hoc status strings with a precise enum:

```python
class TerminalCommandStatus(StrEnum):
    UNKNOWN       = "unknown"        # Default / tool cannot determine; LLM decides
    IDLE          = "idle"           # Prompt returned, no command running
    EXECUTING     = "executing"      # Command running, PTY bytes flowing (slow OK, in-place refresh OK)
    WAITING_INPUT = "waiting_input"  # Content marker detected (password, [y/n], etc.) — fast path
    STUCK         = "stuck"          # 15s no raw bytes AND no content markers
    COMPLETED     = "completed"      # Process exited
    TIMED_OUT     = "timed_out"      # Hard timeout elapsed (preserved from current)
    PAGINATED     = "paginated"      # Pager detected and auto-scrolled (preserved)
```

`UNKNOWN` is the safety net: when the tool cannot determine state, it returns `UNKNOWN` and the LLM reads the output content to decide for itself. This prevents false judgments from misleading the model.

### Detection Priority

Evaluated in this order (highest priority first):

| Priority | Condition | Status | Speed |
|----------|-----------|--------|-------|
| 0 | Initial state / no data yet | `UNKNOWN` | Instant |
| 1 | `is_alive() == False` | `COMPLETED` | Instant |
| 2 | Content marker in output (`password`, `[y/n]`, etc.) | `WAITING_INPUT` | Instant (fast path) |
| 3 | Prompt stable for 100ms + no active command | `IDLE` | Fast |
| 4 | Pager cursor (`:`) detected | `PAGINATED` | Fast |
| 5 | Yield window elapsed + raw bytes flowing | `EXECUTING` | At yield window (10s) |
| 6 | 15s no raw bytes AND no content markers | `STUCK` | At 15s idle |
| 7 | Hard timeout elapsed | `TIMED_OUT` | At timeout (60s) |

Key principle: **content-based detection is fast and instant**. Idle-time thresholds only apply when content analysis is inconclusive.

### Raw Byte Activity Tracking

New field on `TerminalSession`: `_last_byte_at: float`

- Updated every time `poll_once()` returns non-empty `stdout`
- Includes repaint data (progress bars with `\r`, spinners) — any bytes from PTY count
- `raw_idle_ms = (now - _last_byte_at) * 1000`
- `STUCK` fires when `raw_idle_ms >= 15000` AND `not is_waiting_for_input(output)`

This is cross-backend: buffer-based backends read from socket in `poll_once()`, tmux backend does `capture_pane` diff. Non-empty return = active.

### Tool Consolidation

**Remove `process log`**. Merge its functionality into `terminal current`.

`terminal current` becomes the single query endpoint:

```xml
<terminal_result>
  <action>current</action>
  <terminal>default</terminal>
  <status>executing</status>
  <cursor>PS F:\tool\pythonProject\ModexAgent></cursor>
  <idle_ms>3000</idle_ms>
  <output>
PS F:\tool\pythonProject\ModexAgent> npm install
added 50 packages in 12s, checking vulnerabilities
downloading package 51...
  </output>
</terminal_result>
```

**Process tool action changes:**

| Keep | Remove (merged into terminal current) |
|------|---------------------------------------|
| `write` | ~~`log`~~ |
| `submit` | ~~`list`~~ → move to `terminal list` |
| `send_keys` | |
| `paste` | |
| `interrupt` | |
| `kill` | |
| `clear` | |
| `remove` | |

### Content Scope: Second-to-Last Prompt

`terminal current` returns output from the **second-to-last prompt** to the end of the buffer.

Current `extract_current_segment_from_buffer()` only returns from the last prompt, missing the command itself. New function `extract_last_command_output()` finds the second-to-last prompt:

| Scenario | Buffer content | Returned range |
|----------|---------------|----------------|
| Command running | `PS F:\> cmd\noutput...\n` | From only prompt → everything |
| Command completed | `PS F:\> cmd\noutput\nPS F:\> ` | From 2nd-to-last prompt → includes command + output + new prompt |
| Idle, no command | `PS F:\> ` | Single prompt → return it |

This ensures the LLM always sees: prompt-before-command → command output → prompt-after-command (if done).

### Refresh Mechanism

`terminal current` calls `session.refresh_output()` before reading, ensuring up-to-date data regardless of backend:

- Buffer-based backends (winpty, pexpect): flushes socket into buffer
- Tmux backend: updates diff tracker
- Cross-backend, no backend-specific logic in tools

### Layered Implementation

#### Layer 1: `prompt.py` — Detection (shell-agnostic, system-agnostic)

| Change | Detail |
|--------|--------|
| Add `INPUT_PROMPT_MARKERS` | Move from `session.py` class attribute to module-level public constant |
| Add `is_waiting_for_input(output)` | Public function: strip ANSI → find last non-empty line → match markers |
| Add `extract_last_command_output(text)` | Find second-to-last prompt in buffer, return from there to end |

#### Layer 2: `backends/` — Consistency

| Change | Detail |
|--------|--------|
| Fix `TmuxPtyBackend.current_segment()` | Use `extract_current_segment_from_buffer()` on captured pane text, matching other backends. Adds `cursor_line` and `is_empty_prompt` that were previously missing. |
| Other backends | No changes needed (already correct) |

#### Layer 3: `session.py` — Session (backend-agnostic)

| Change | Detail |
|--------|--------|
| Delegate `_INPUT_PROMPT_MARKERS` | Reference `prompt.py` constant instead of class attribute |
| Add `_last_byte_at: float` | Raw byte activity timestamp, initialized to session creation time |
| Modify `poll_once()` | Update `_last_byte_at` when `read.stdout` is non-empty |
| Add `refresh_output(timeout=0.1)` | Call `poll_once()` to flush PTY data into buffers. Safe to call when dead. |
| Add `command_status()` | Compute `TerminalCommandStatus` using priority rules above |
| Add `last_command_output()` | Call `refresh_output()` then `extract_last_command_output()` |

#### Layer 4: Tools — LLM-facing

| Change | Detail |
|--------|--------|
| `command_tool.py` — check #4 | Replace idle-time `waiting_for_input` with `is_waiting_for_input()` content scan |
| `command_tool.py` — stuck detection | Add check: `raw_idle_ms >= 15000` AND no markers → `STUCK` |
| `command_tool.py` — `running` → `executing` | Rename status from `running` to `executing` in XML output |
| `command_tool.py` — cursor state | Always include `<cursor_line>` via `resolve_cursor_line()` (cross-backend) |
| `process_tool.py` — remove `log` | Action removed; functionality merged into `terminal current` |
| `process_tool.py` — remove `list` | Move to `terminal list` with session info |
| `tool.py` — `terminal current` rewrite | Call `refresh_output()` → `command_status()` → `last_command_output()` → return full result |
| `tool.py` — `terminal list` extension | Include process session info (running/completed commands in each tab) |
| `poll_loop.py` — new shared module | Extract common poll loop from CommandTool and `_drain_terminal_after_action()` |

#### New Types (`types.py`)

```python
class TerminalCommandStatus(StrEnum):
    UNKNOWN       = "unknown"
    IDLE          = "idle"
    EXECUTING     = "executing"
    WAITING_INPUT = "waiting_input"
    STUCK         = "stuck"
    COMPLETED     = "completed"
    TIMED_OUT     = "timed_out"
    PAGINATED     = "paginated"
```

### Files Changed

| File | Layer | Changes |
|------|-------|---------|
| `framework/tools/terminal/prompt.py` | L1 | +3 exports: `INPUT_PROMPT_MARKERS`, `is_waiting_for_input()`, `extract_last_command_output()` |
| `framework/tools/terminal/backends/tmux_pty.py` | L2 | `current_segment()` uses `extract_current_segment_from_buffer()` |
| `framework/tools/terminal/session.py` | L3 | Delegate markers, add `_last_byte_at`, `refresh_output()`, `command_status()`, `last_command_output()` |
| `framework/tools/terminal/types.py` | L3 | Add `TerminalCommandStatus` enum |
| `framework/tools/terminal/command_tool.py` | L4 | Detection rewrite, status rename, cursor state, stuck detection |
| `framework/tools/terminal/process_tool.py` | L4 | Remove `log`/`list` actions, refactor drain |
| `framework/tools/terminal/tool.py` | L4 | Rewrite `current`, extend `list` |
| `framework/tools/terminal/poll_loop.py` | L4 | New file: shared poll loop |

**Unchanged files**: `config.py`, `process_registry.py`, `visible_windows.py`, `visible_windows_host.py`, `windows_hidden.py`, `pexpect_pty.py`

### Verification

```bash
pytest tests/framework/tools/terminal/ -v
```

Scenarios to verify:
- Build command (one line every 8s) → `executing` (not `stuck`, not `waiting_input`)
- `echo "Password:"` → `waiting_input` (fast path, instant)
- Command with progress bar (in-place `\r` refresh) → `executing` (bytes flowing)
- Command silent for 16s → `stuck`
- After `executing`, call `terminal current` → sees output produced after CommandTool returned
- After command completes → `idle` with prompt line visible in output
- Tmux backend `terminal current` → `idle` status correct (not always `active`)
- `UNKNOWN` returned when session is brand new / no data yet
