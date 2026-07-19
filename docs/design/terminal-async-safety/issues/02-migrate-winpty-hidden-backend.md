# 02 — Migrate `WinptyHiddenBackend` to blocking-IO hooks

**What to build:** Activate the ADR-0032 D1/D3 contract on the hidden Windows backend. The backend switches from "every method overridden, write blocks the event loop" to "only the two blocking-IO hooks + `_shell_family` implemented, the base-class template wraps them in `run_in_executor`". This structurally eliminates the synchronous-write-blocks-event-loop defect on the hidden Windows path.

Concretely, `WinptyHiddenBackend`:

- Implements `_write_blocking(self, data: str) -> None` — body is the current `async def write` body without the `await`/`async` (i.e. `self._proc.write(data)`).
- Implements `_read_blocking(self, timeout: float, max_size: int) -> str` — body is the current `_do_read` closure body inside `read_pending` (settimeout + recv + decode, with TimeoutError/OSError → "").
- Implements `_shell_family(self) -> ShellFamily` — one-liner: `return _family_from_path(self._shell or "")`.
- **Deletes** its `async def write`, `async def read`, `async def read_pending`, `async def current_segment`, `async def clear_input_line`, `async def drain_startup` overrides — all six now inherited from the base class.
- **Deletes** its `_uses_readline` private helper — replaced by `_shell_family` consumed by the base class.
- Keeps `start`, `interrupt`, `is_alive`, `terminate`, `kill` unchanged.

After deletion the backend is ~80 lines (from ~177), and the event-loop-blocking defect on this path is gone — `run_in_executor` offloads `proc.write` to a worker thread.

Add the first `tests/unit/tools/terminal/backends/test_windows_hidden.py`:

- Test the `_write_blocking` hook writes through to a mock `PtyProcess`.
- Test the `_read_blocking` hook handles `TimeoutError` / `OSError` by returning `""`.
- Test `_shell_family` returns the correct `ShellFamily` for bash / zsh / sh / unknown paths.
- Test that the base-class `write` template is invoked (i.e. the backend does NOT override `write`) — this is the structural assertion that the contract is honored.
- Test `clear_input_line` inherited from base class writes `"\x01\x0b"` for bash and is a no-op for cmd.

This ticket does NOT touch the visible-windows or pexpect or tmux backends.

**Blocked by:** 01 — Prefactor: expand `TerminalBackend` with async-safety scaffolding.

**Status:** ready-for-agent

- [ ] `WinptyHiddenBackend` implements `_write_blocking`, `_read_blocking`, `_shell_family`
- [ ] `WinptyHiddenBackend` deletes its 6 overrides (`write`, `read`, `read_pending`, `current_segment`, `clear_input_line`, `drain_startup`) — all inherited from `TerminalBackend`
- [ ] `WinptyHiddenBackend` deletes `_uses_readline` private helper
- [ ] `tests/unit/tools/terminal/backends/test_windows_hidden.py` covers hook contract, shell-family detection, error handling, base-class inheritance
- [ ] Existing `tests/framework/tools/terminal/test_windows_terminal_command_process_workflow.py` passes (the HIDDEN-parametrized case; VISIBLE remains skipped)
- [ ] `ruff check src/modex_agent/tools/terminal/backends/windows_hidden.py` clean
- [ ] `mypy src/modex_agent/tools/terminal/backends/windows_hidden.py` clean
- [ ] No `settimeout`-style global-state-mutating calls inside any remaining method on this backend

## Comments

- This is the first migration; it validates that the expand-step scaffolding from 01 actually works end-to-end on a real backend before the bigger migrations (03/04/05) rely on it.
- Cleanup is explicit: 6 override deletions + 1 private helper deletion, ~95 lines net removed from this file.
