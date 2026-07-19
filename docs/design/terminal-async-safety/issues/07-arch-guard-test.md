# 07 — Architecture guard test for async-safety contract

**What to build:** The final ticket. Add `tests/architecture/test_terminal_async_safety.py` — an AST/regex-based architecture guard test that enforces the ADR-0032 D1/D4/D6 contract on every `TerminalBackend` subclass. This catches the exact regression class that produced the original "synchronous blocking I/O inside `async def`" defect.

The test asserts, per concrete `TerminalBackend` subclass found in `src/modex_agent/tools/terminal/backends/`:

### Assertion 1 — hook-or-override consistency

If the subclass does NOT override `write`, then it MUST implement `_write_blocking` (so the base-class template has something to wrap). Symmetrically for `read_pending` / `_read_blocking`. Catches the regression "deleted the hook but didn't override the template."

### Assertion 2 — async-safety evidence

For each `write` / `read_pending` method (whether overridden on the subclass or inherited from the base), the source must contain evidence of non-blocking I/O:

- If the method is overridden on the subclass: its source must contain either `run_in_executor` or `await`. Bare `socket.sendall` / `proc.write` / `proc.send` / `pane.send_keys` calls inside an overridden `write` or `read_pending` are flagged as violations, UNLESS they appear inside a `_write_blocking` / `_read_blocking` definition (where the base-class template will wrap them).
- If the method is inherited from the base class: no per-subclass assertion needed (the base-class template is checked once globally).

### Assertion 3 — `_shell_family` implementation

Every concrete `TerminalBackend` subclass implements `_shell_family`. After ticket 06 promoted it to `@abstractmethod`, this is partly enforced by Python at instantiation time — but the architecture test adds a static structural check (subclass source contains a `_shell_family` definition) so the regression is caught at test time, not at runtime instantiation.

### Assertion 4 — no `settimeout` leak surface

No `socket.settimeout` call anywhere in `backends/`. This is the specific API that produced the original "lost Enter" defect (ADR-0032 root cause 2). Banning it structurally prevents the regression class.

### Implementation notes

- Use `inspect.getsource` + `ast.parse` for source inspection (matches the pattern in existing `tests/architecture/` tests like `test_terminal_manager_seam_preserved.py`).
- Discover concrete `TerminalBackend` subclasses via `TerminalBackend.__subclasses__()` (recursive) or by walking the `backends/` package — match the pattern used by other architecture tests in this repo.
- ~50–80 LOC total. No runtime cost (test-time only).
- The test starts strict (no `EXPECTED_OFFENDERS` allowlist) because all four backends are migrated before this ticket lands.

This ticket does NOT touch production code. It only adds the guard test.

**Blocked by:** 06 — Contract: finalize ABC, sweep dead code, update docs.

**Status:** ready-for-agent

- [ ] `tests/architecture/test_terminal_async_safety.py` exists
- [ ] Assertion 1: hook-or-override consistency per subclass
- [ ] Assertion 2: async-safety evidence (`run_in_executor` or `await`) in every overridden `write` / `read_pending`
- [ ] Assertion 3: `_shell_family` implementation present on every concrete subclass
- [ ] Assertion 4: no `socket.settimeout` call anywhere in `backends/`
- [ ] Test passes against the post-06 codebase (all 4 backends in their final shape)
- [ ] Test fails deliberately if any backend regresses (manually verify by temporarily reverting one backend to the old pattern, then restore)
- [ ] `ruff check tests/architecture/test_terminal_async_safety.py` clean
- [ ] `mypy tests/architecture/test_terminal_async_safety.py` clean

## Comments

- ADR-0032 D6 mandates this guard test. The original defect (synchronous blocking I/O inside `async def`) was not detectable by any existing test — the architecture guard is the structural prevention.
- Lands last (blocked by 06) so it guards the final shape. Landing it earlier would require an `EXPECTED_OFFENDERS` allowlist that shrinks as migrations land — more complexity for no benefit given the linear execution model.
- The four assertions are independent: each can fail without affecting the others. This makes failure messages precise ("Assertion 2 failed for `WinptyHiddenBackend.write`: no `run_in_executor` or `await` found, bare `proc.write` call detected").
