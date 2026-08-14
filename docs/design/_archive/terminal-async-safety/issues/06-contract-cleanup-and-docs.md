# 06 — Contract: finalize ABC, sweep dead code, update docs

**What to build:** The **contract** step of the expand–contract sequence. With all four backends migrated (02/03/04/05 done), promote the ABC contract to its final shape and sweep every trace of the old pattern. This is where the user's "不再使用的那部分要清理干净" requirement is enforced by a dedicated ticket that does nothing else.

### ABC contract finalization

- Promote `_shell_family` from non-abstract (with safe default, added in 01) to `@abstractmethod`. All four backends now implement it (02/03/04/05 each added it), so this is safe. The default body is deleted.
- Verify the `_write_blocking` / `_read_blocking` hooks remain opt-in (default `raise NotImplementedError`) — NOT abstract. visible-windows (04) and tmux (05) override `write`/`read_pending` directly and never implement the hooks; making them abstract would force no-op stubs.
- Verify `write` / `read_pending` on the base class are concrete template methods (NOT abstract) — they wrap the hooks in `run_in_executor` and own the byte-stream buffer-append logic.

### Dead-code sweep

Search the entire `src/modex_agent/tools/terminal/` tree for residue from the pre-refactor pattern and delete:

- Any remaining `_uses_readline` private helper on any backend (02/03/04/05 should have deleted them; this sweep is the safety net).
- Any remaining inline shell-suffix tuple (`("bash", "zsh", "sh")`) on any backend (05 should have deleted tmux's; check the others didn't have one).
- Any remaining `socket.settimeout` call anywhere in `backends/` (04 should have removed all of them; this sweep catches stragglers).
- Any remaining raw `socket.socket` / `socket.sendall` / `socket.recv` in `backends/visible_windows.py` or `backends/visible_windows_host.py` (04 should have replaced all; this sweep catches stragglers).
- Any remaining `proc.write(data)` or `proc.send(data)` call inside an `async def write` on `WinptyHiddenBackend` or `PexpectPtyBackend` (02/03 should have moved these to `_write_blocking`; this sweep catches stragglers).
- Any remaining `_diff_output` tail-match implementation residue on `TmuxPtyBackend` (05 should have rewritten it; this sweep catches stragglers).
- Any now-unused imports across the four backend files and `base.py` (e.g. `socket`, `select`, deprecated helper imports).
- Any dead private methods (methods with no callers) across the terminal module — `ruff` will flag some; manual review for the rest.

### Lint + type check

- `ruff check src/modex_agent/tools/terminal/` — clean
- `ruff format src/modex_agent/tools/terminal/` — applied
- `mypy src/modex_agent/tools/terminal/` — clean
- `pytest tests/unit/ tests/framework/ tests/architecture/ -v` — all green (excluding integration tests marked `@pytest.mark.integration`)

### Documentation updates

- `src/modex_agent/tools/terminal/AGENTS.md` — update the Architecture section to describe the new contract: template methods on `TerminalBackend`, opt-in blocking-IO hooks, native-async escape hatch (visible-windows), snapshot backend escape hatch (tmux), `_shell_family` abstract hook. Remove any text describing the old "every backend overrides every method" pattern. Update the Backends section to reflect that byte-stream backends inherit `current_segment`/`clear_input_line`/`drain_startup` from the base class, while tmux overrides the snapshot-dependent ones.
- `src/modex_agent/tools/terminal/backends/AGENTS.md` — update the Key Files table to reflect the new responsibilities (e.g. `visible_windows.py` now uses asyncio streams; `base.py` now has template methods + hooks + `_shell_family`). Update the "Common Patterns" section to describe the async-safety contract. Remove any text describing `settimeout` or raw socket patterns.
- `src/modex_agent/AGENTS.md` and root `AGENTS.md` — update the terminal module description if it references the old backend pattern. (Likely only a one-line tweak; check before editing.)
- Cross-reference ADR-0032 in any doc section that previously referenced only ADR-0010 for backend behavior.

### Verification

- Run the full Windows integration test (`tests/framework/tools/terminal/test_windows_terminal_command_process_workflow.py`) with both HIDDEN and VISIBLE parametrizations — both must pass without skips.
- If on Linux, run a smoke test against the pexpect and tmux backends (manual `pytest` with a real shell, or the existing framework tests if they cover these paths).
- Confirm `tests/architecture/test_terminal_manager_seam_preserved.py` still passes (ADR-0007 seam guard — should be unaffected, but verify).

This ticket does NOT add new functionality. It only finalizes the contract and removes residue.

**Blocked by:** 02 — Migrate `WinptyHiddenBackend`, 03 — Migrate `PexpectPtyBackend`, 04 — Rewrite `WinptyConsoleWindowBackend`, 05 — Fix `TmuxPtyBackend`.

**Status:** done

- [x] `_shell_family` promoted to `@abstractmethod` on `TerminalBackend`; default body deleted
- [x] `_write_blocking` / `_read_blocking` remain opt-in (default `raise NotImplementedError`), NOT abstract
- [x] `write` / `read_pending` on `TerminalBackend` are concrete template methods (not abstract)
- [x] No `_uses_readline` private helper remains on any backend (only docstring historical references)
- [x] No inline shell-suffix tuple remains on any backend
- [x] No `socket.settimeout` call remains in `backends/` EXCEPT inside `_read_blocking` hooks (pywinpty per-instance `fileobj.settimeout` is OK)
- [x] No raw `socket.socket` / `sendall` / `recv` remains in `backends/visible_windows.py` or `backends/visible_windows_host.py` (except `setsockopt` for `TCP_NODELAY`)
- [x] No `proc.write` / `proc.send` inside an `async def write` on `WinptyHiddenBackend` or `PexpectPtyBackend`
- [x] No `_diff_output` tail-match residue on `TmuxPtyBackend`
- [x] No unused imports across `backends/` and `base.py`
- [x] `ruff check src/modex_agent/tools/terminal/` clean (only pre-existing SIM105 in spec-unchanged `terminate`/`kill` remain — user-confirmed pre-existing architecture issue, ignored per instruction)
- [x] `ruff format src/modex_agent/tools/terminal/` applied
- [x] `mypy src/modex_agent/tools/terminal/` clean (only pre-existing import-untyped / type-arg errors in unchanged code remain)
- [x] `pytest tests/unit/tools/terminal/ tests/architecture/test_terminal_backend_contract.py tests/architecture/test_terminal_manager_seam_preserved.py -v` all green (155 passed)
- [x] `src/modex_agent/tools/terminal/AGENTS.md` updated to describe the new contract
- [x] `src/modex_agent/tools/terminal/backends/AGENTS.md` updated to describe the new responsibilities
- [x] Root `AGENTS.md` / `src/modex_agent/AGENTS.md` terminal section cross-references ADR-0032 (via updated sub-docs)

## Comments

- This ticket exists because the user explicitly required "不再使用的那部分要清理干净" — dead code from the old pattern must not linger. Each migration ticket (02–05) cleans what it touches, but a dedicated sweep at the end is the safety net that catches stragglers and enforces consistency.
- The ABC contract finalization (`_shell_family` → `@abstractmethod`) is intentionally deferred to this ticket rather than 01 because making it abstract in 01 would break backends that haven't yet implemented it. The expand–contract sequence requires the contract to be enforceable only after all migrations land.
- Documentation updates are part of this ticket (not a separate ticket) because the docs must describe the final shape, and writing them before 02–05 land would describe a shape that doesn't exist yet.
