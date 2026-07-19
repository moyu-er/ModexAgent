# 01 — Prefactor: expand `TerminalBackend` with async-safety scaffolding

**What to build:** Introduce the new `TerminalBackend` ABC contract shape from ADR-0032 D1/D4 *alongside* the existing per-backend overrides, without activating it on any backend. This is the **expand** step of the expand–contract sequence — after this ticket the new contract exists but no backend uses it yet, so CI stays green and behavior is unchanged.

Concretely, `TerminalBackend` gains:

- Opt-in hooks `_write_blocking(self, data: str) -> None` and `_read_blocking(self, timeout: float, max_size: int) -> str`, both with default body `raise NotImplementedError`. These are NOT abstract — backends that override `write`/`read_pending` natively (visible-windows post-04, tmux post-05) never implement them.
- Concrete `write(self, data)` and `read_pending(self, timeout, max_size)` template methods that call the hooks via `loop.run_in_executor(None, ...)`. The template body is the standard byte-stream path: read into `self._output_buffer`, return a `TerminalRead`. The template is dormant this ticket — every backend's existing override still wins.
- Concrete `current_segment(self)`, `clear_input_line(self)`, `drain_startup(self)` with byte-stream default implementations. `current_segment` uses `extract_current_segment_from_buffer(self._output_buffer.text)`. `clear_input_line` writes `"\x01\x0b"` iff `_shell_family().uses_readline()`. `drain_startup` calls the shared `drain_windows_startup` helper passing `self.read`, `self.write`, `self.is_alive`, `uses_readline=_shell_family().uses_readline()`. All three are dormant this ticket — backends' existing overrides win.
- `_shell_family(self) -> ShellFamily` as a non-abstract method with a safe default (e.g. `return ShellFamily.SH`). This ticket it is NOT `@abstractmethod` — that would break the 4 backends until they implement it. Ticket 06 promotes it to abstract after all migrations land.

The four backends' existing `write`/`read_pending`/`current_segment`/`clear_input_line`/`drain_startup` overrides remain **untouched**. Their existing `_uses_readline` private helper remains untouched. No production behavior changes.

Add `tests/architecture/test_terminal_backend_contract.py` (lightweight): assert the base class has the new methods (template + hooks + `_shell_family`), and that the hooks default to raising `NotImplementedError`. This guards the scaffolding exists before any migration begins.

**Blocked by:** None — can start immediately.

**Status:** done

- [x] `TerminalBackend` ABC has `_write_blocking` / `_read_blocking` opt-in hooks (default `raise NotImplementedError`)
- [x] `TerminalBackend` ABC has concrete `write` / `read_pending` template methods wrapping the hooks in `loop.run_in_executor`
- [x] `TerminalBackend` ABC has concrete `current_segment` / `clear_input_line` / `drain_startup` (byte-stream defaults, calling `_shell_family()` for readline detection)
- [x] `_shell_family` exists on `TerminalBackend` as a non-abstract method with a safe default
- [x] All four backends' existing overrides remain intact and unchanged — no production behavior changes
- [x] New `tests/architecture/test_terminal_backend_contract.py` asserts the scaffolding shape; existing tests pass
- [x] `ruff check src/modex_agent/tools/terminal/backends/` clean
- [x] `mypy src/modex_agent/tools/terminal/backends/` clean

## Comments

- This ticket intentionally does NOT delete or modify any backend. It only adds the new shape to the base class. Each subsequent migration ticket (02–05) activates the new shape on one backend and removes that backend's overrides.
- The expand–contract pattern is mandated because the base-class refactor has blast radius across all 4 backends; sequential per-backend migration keeps each step independently green.
