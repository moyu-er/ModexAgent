# Handoff: Terminal System Improvements

**Date**: 2026-05-30
**Branch**: `develop_gyt`
**Last Commit**: `11cd29e` — Tasks 1-6

## Documents

| Doc | Path |
|-----|------|
| **Spec (design)** | `docs/superpowers/specs/2026-05-30-terminal-system-improvements-design.md` |
| **Implementation Plan** | `docs/superpowers/plans/2026-05-30-terminal-system-improvements.md` |

## Completion Status

| Task | Description | Status | Notes |
|------|-------------|--------|-------|
| 1 | SlidingOutputBuffer class | ✅ Complete | 7 tests pass |
| 2 | Base class promotion (backends) | ✅ Complete | base.py, visible_windows.py, windows_hidden.py refactored |
| 3 | Config additions | ✅ Complete | New fields added |
| 4 | Prompt utilities (pager detection) | ✅ Complete | 9 tests pass |
| 5 | Session changes (submit_command + PAGER) | ❌ Not started | `session.py`: add `submit_command()`, `mark_command_boundary()` call, remove PAGER suppression from `_startup_env()` |
| 6 | Tiered idle timeout | ⚠️ Preliminary | `running_runtime()` updated, test updated. **May need review** — tiered threshold logic should be verified against real-world scenarios |
| 7 | CommandTool overhaul | ❌ Not started | Largest task: pager auto-scroll, submit_command, XML format. Touches `types.py`, `command_tool.py`, tests |
| 8 | ProcessTool XML format | ❌ Not started | `process_tool.py` + tests |
| 9 | terminal current XML | ❌ Not started | `tool.py` + `test_terminal_tool_current.py` |
| 10 | Manager memory pressure | ❌ Not started | `manager.py` — add `_check_memory_pressure()` |
| 11 | TODO comments | ❌ Not started | `visible_windows_host.py` — human-agent detection TODO |
| 12 | Final verification | ❌ Not started | Full test suite + lint + mypy |

## What's Done (Tasks 1-6)

### Task 1: SlidingOutputBuffer
- New class in `framework/tools/terminal/results.py`
- Dual-constraint: 200K chars + 100 commands (via `deque(maxlen=100)`)
- Methods: `append()`, `mark_command_boundary()`, `text`, `total_chars`, `clear()`

### Task 2: Base Class Promotion
- `TerminalBackend.__init__` sets `_output_buffer: SlidingOutputBuffer | None = None`
- New methods: `mark_command_boundary()`, `_append_to_buffer()`
- `extract_current_segment_from_buffer()` moved to `base.py`
- `VisibleWindowsPtyBackend` and `WindowsHiddenPtyBackend` use base class
- Test imports updated

### Task 3: Config
- Removed `input_wait_early_min_elapsed_ms`
- Added: `initial_idle_threshold_ms` (5s), `active_idle_threshold_ms` (15s), `max_total_buffer_chars` (1M), `pager_auto_scroll_*` fields

### Task 4: Prompt Utilities
- `detect_pager_entry(cursor_line)` — matches bare `:`
- `resolve_cursor_line(segment)` — fallback when backend doesn't provide cursor_line

### Task 6: Tiered Idle Timeout
- `running_runtime()` uses `initial_idle_threshold_ms` (no output yet) or `active_idle_threshold_ms` (had output before)
- Test updated from removed field

## Remaining Tasks (in dependency order)

**Task 5** must complete before Task 7 (CommandTool needs `submit_command()`).

**Task 7** (CommandTool overhaul) is the largest remaining task — pager auto-scroll, submit_command integration, all `_format_*` methods return `<command_result>` XML. See plan for exact code.

**Tasks 8-12** are sequential but independent of each other.

## Key Architecture Notes

- **XML format**: All agent-facing tool returns use `<command_result>`, `<process_result>`, or `<terminal_result>`. `CommandResultStatus` enum in `types.py`. Use `xml_escape()` from `xml.sax.saxutils`.
- **Type safety**: Enums over strings, typed parameters/returns, no bare `Any`/`dict`, avoid `getattr`/`hasattr`.
- **No git worktree**: Work directly on `develop_gyt` branch.

## Known Issues / Watch Points

1. **Task 5 pre-requisite**: `session.execute()` currently doesn't call `mark_command_boundary()`. This must be added BEFORE the pager auto-scroll (Task 7) which depends on correct command boundary tracking in the sliding buffer.

2. **PAGER suppression reversal**: `session._startup_env()` currently sets `PAGER=cat`, `GIT_PAGER=cat`, `LESS=FRX`. These must be REMOVED per spec §2e. The pager auto-scroll in Task 7 handles pager output programmatically.

3. **XML format test updates**: Existing tests in `test_command_tool.py` and `test_process_tool.py` assert natural language strings (e.g., `"Command still running"`). These must be updated to assert XML tags (`<status>running</status>`).

4. **FakeBackend compatibility**: Test fakes must have `mark_command_boundary()` method. Task 5 step 4 adds this.

5. **ProcessTool test failures**: 2 process_tool tests failed in the pre-Task-6 run (`test_process_log_reads_from_registry`, `test_process_log_reads_aggregated_output`). These may be pre-existing — verify after Task 8 XML format changes.
