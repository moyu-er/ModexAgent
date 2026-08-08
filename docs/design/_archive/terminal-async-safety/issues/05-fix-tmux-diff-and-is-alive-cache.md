# 05 — Fix `TmuxPtyBackend` diff bug and add `is_alive` cache

**What to build:** Fix two correctness/performance defects on the tmux backend without changing its snapshot I/O model (ADR-0032 D5 explicitly rejects forcing tmux into byte-stream shape via `pipe-pane` — over-convergence).

### Fix 1 — diff bug (correctness)

Currently `_diff_output` tail-matches lines from the end of the previous `capture_pane()` snapshot to the end of the current one. When the pane scrolls (output exceeds the visible window, default 30 rows), the same logical lines shift position; tail matching fails; the diff returns the entire current snapshot as "new" output, producing duplicates and occasionally echoed prompt fragments. Any command emitting more than 30 lines triggers this.

Fix:

- `capture_pane` switches from default visible-window snapshot to `capture-pane -p -S -` (full scrollback, default 2000 lines). This gives the diff a stable, larger window to match against.
- `_diff_output` switches from tail-matching to **prefix-matching**: check whether the previous snapshot's lines are a prefix of the current snapshot's lines; if so, return the suffix as new output. This matches the actual semantics of pane output (lines are appended at the bottom; lines that scroll off the top disappear from the visible window but remain in scrollback under `-S -`).
- Prefix-match failure (rare: output scrolls beyond the 2000-line scrollback between two `capture_pane` calls) falls back to returning the entire current snapshot — same as today's behavior, but only in the genuine edge case rather than on every >30-line command.

### Fix 2 — `is_alive` cache (performance)

Currently `is_alive()` spawns a `tmux ls` subprocess (via `run_in_executor` over `self._server.sessions`) on every call. The poll loop calls `is_alive()` ~20×/second. Each call is a subprocess spawn — measurable CPU and latency overhead on Linux/tmux deployments.

Fix:

- Add `self._alive_cache: tuple[float, bool] | None = None` field.
- `is_alive()` checks the cache: if the cached timestamp is within 1 second of `time.monotonic()`, return the cached bool. Otherwise re-query `self._server.sessions` (via `run_in_executor`), update the cache, return the result.
- Cache is invalidated on `terminate()` / `kill()` (set to `None` so the next call re-queries — but those methods also set `self._session = None` / `self._pane = None` so subsequent `is_alive` returns `False` directly via the `if self._session_name is None` guard).

### D4 convergence (shell family hook)

`TmuxPtyBackend` is a snapshot backend — it overrides `write`/`read_pending`/`current_segment`/`drain_startup` directly (D1 point 3, no blocking-IO hooks). But it still participates in D4's shell-family convergence:

- Implements `_shell_family(self) -> ShellFamily` — one-liner: `return _family_from_path(self._shell or "")`.
- `clear_input_line` is inherited from the base class (the base's `"\x01\x0b"` write path works for tmux because `write` is overridden to call `pane.send_keys(data, enter=False)`).
- **Deletes** the inline shell-suffix tuple (`if any(name.endswith(s) for s in ("bash", "zsh", "sh"))`) currently duplicated in `clear_input_line` and `drain_startup`.
- **Deletes** `_uses_readline` private helper if present.
- Keeps `drain_startup` override (genuinely different — uses `capture_pane` for prompt detection rather than byte-stream prompt detection).
- Keeps `current_segment` override (genuinely different — uses `capture_pane` rather than `self._output_buffer`).
- Keeps `read_pending` override (snapshot diff model, not byte stream).
- Keeps `write` override (`send_keys(enter=False)`).

Add `tests/unit/tools/terminal/backends/test_tmux_pty.py`:

- Test `_diff_output` prefix-match semantics:
  - previous is a line-prefix of current → returns the suffix
  - previous is empty → returns all of current
  - previous is not a prefix (scroll beyond scrollback) → returns all of current
  - **the regression case**: command emits 60 lines on a 30-row pane → no duplicates (verify prefix-match succeeds because full scrollback under `-S -` retains all 60 lines)
- Test `is_alive` 1-second TTL:
  - First call queries `self._server.sessions` (mocked) and caches
  - Second call within 1 second returns cached value without querying
  - Third call after 1 second re-queries
  - `terminate()` invalidates the cache
- Test `_shell_family` returns correct `ShellFamily` for bash / zsh / sh / unknown paths.
- Test `clear_input_line` inherited from base class writes `"\x01\x0b"` for bash (via the overridden `write` → `pane.send_keys`).
- Test `capture_pane` invocation uses `-p -S -` flags (assert on the `capture_pane` call args, or on the underlying `cmd` if libtmux wraps it).

This ticket does NOT touch the visible-windows, hidden-windows, or pexpect backends.

**Blocked by:** 01 — Prefactor: expand `TerminalBackend` with async-safety scaffolding.

**Status:** done

- [x] `TmuxPtyBackend.read` uses `capture-pane -p -S -` (full scrollback)
- [x] `_diff_output` rewritten to prefix-match (returns suffix if previous is a line-prefix of current; falls back to all-current on prefix failure)
- [x] `is_alive` has 1-second TTL cache (`_alive_cache: tuple[float, bool] | None`); cache invalidated on `terminate`/`kill`
- [x] `TmuxPtyBackend` implements `_shell_family`
- [x] `TmuxPtyBackend` deletes the inline shell-suffix tuple from `clear_input_line` and `drain_startup`
- [x] `TmuxPtyBackend` deletes `_uses_readline` private helper if present
- [x] `TmuxPtyBackend` keeps `write`/`read_pending`/`current_segment`/`drain_startup` overrides (snapshot backend — D5)
- [x] `tests/unit/tools/terminal/backends/test_tmux_pty.py` covers prefix-match diff (including 60-line scroll case), `is_alive` TTL, shell-family, inherited `clear_input_line`
- [x] `ruff check src/modex_agent/tools/terminal/backends/tmux_pty.py` clean
- [x] `mypy src/modex_agent/tools/terminal/backends/tmux_pty.py` clean

## Comments

- ADR-0032 D5 Considered Options records why `pipe-pane` byte-stream conversion was rejected: per-session `cat` subprocess + temp file + truncation strategy + offset manager is over-convergence for no correctness gain beyond what the prefix-match diff provides. Tmux's snapshot I/O is its essential shape; this ticket fixes the diff algorithm and adds a cache without changing the shape.
- The 2000-line scrollback default (`-S -` with no numeric argument) is tmux's `history-limit` setting. If a deployment sets a smaller `history-limit`, prefix-match may fall back to all-current more often — acceptable degradation, not a correctness defect (no duplicates, just more conservative new-output reporting).
- Manual verification (beyond automated tests): on a Linux/tmux deployment, run `bash.execute("for i in $(seq 1 100); do echo line $i; done")` and confirm the output contains exactly 100 unique lines (no duplicates) and the prompt returns.
