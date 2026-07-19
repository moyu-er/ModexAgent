# 03 — Migrate `PexpectPtyBackend` to blocking-IO hooks

**What to build:** Same mechanical migration as 02, applied to the Linux/macOS hidden pexpect backend. Activate the ADR-0032 D1/D3 contract: implement the two blocking-IO hooks + `_shell_family`, delete the 6 overrides + `_uses_readline`. The base-class template wraps `proc.send` and `proc.read_nonblocking` in `run_in_executor`, eliminating the synchronous-write-blocks-event-loop defect on the pexpect path.

Concretely, `PexpectPtyBackend`:

- Implements `_write_blocking(self, data: str) -> None` — body is the current `async def write` body without `async`/`await` (i.e. `self._proc.send(data)`).
- Implements `_read_blocking(self, timeout: float, max_size: int) -> str` — body is the current `_do_read` closure body inside `read` (calls `proc.read_nonblocking(max_size, timeout=timeout)`, catches `pexpect.exceptions.TIMEOUT` / `EOF` → `""`).
- Implements `_shell_family(self) -> ShellFamily` — one-liner: `return _family_from_path(self._shell or "")`.
- **Deletes** its `async def write`, `async def read`, `async def read_pending`, `async def current_segment`, `async def clear_input_line`, `async def drain_startup` overrides — all six now inherited from the base class.
- **Deletes** its `_uses_readline` private helper — replaced by `_shell_family` consumed by the base class.
- Keeps `start`, `interrupt`, `is_alive`, `terminate`, `kill` unchanged.

Add `tests/unit/tools/terminal/backends/test_pexpect_pty.py`:

- Test the `_write_blocking` hook calls `proc.send` with the data.
- Test the `_read_blocking` hook handles `pexpect.exceptions.TIMEOUT` / `EOF` by returning `""`.
- Test `_shell_family` returns the correct `ShellFamily` for bash / zsh / sh / unknown paths.
- Test that the backend does NOT override `write` (structural assertion).
- Test `clear_input_line` inherited from base class writes `"\x01\x0b"` for bash/zsh/sh.

This ticket does NOT touch the visible-windows or tmux backends.

**Blocked by:** 01 — Prefactor: expand `TerminalBackend` with async-safety scaffolding.

**Status:** ready-for-agent

- [ ] `PexpectPtyBackend` implements `_write_blocking`, `_read_blocking`, `_shell_family`
- [ ] `PexpectPtyBackend` deletes its 6 overrides (`write`, `read`, `read_pending`, `current_segment`, `clear_input_line`, `drain_startup`) — all inherited from `TerminalBackend`
- [ ] `PexpectPtyBackend` deletes `_uses_readline` private helper
- [ ] `tests/unit/tools/terminal/backends/test_pexpect_pty.py` covers hook contract, shell-family detection, error handling, base-class inheritance
- [ ] `ruff check src/modex_agent/tools/terminal/backends/pexpect_pty.py` clean
- [ ] `mypy src/modex_agent/tools/terminal/backends/pexpect_pty.py` clean

## Comments

- Same mechanical pattern as 02 — verifies the contract generalizes across the second blocking-IO backend.
- Cleanup: 6 override deletions + 1 private helper deletion, ~100 lines net removed from this file.
