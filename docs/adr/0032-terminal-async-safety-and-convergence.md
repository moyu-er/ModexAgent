# Terminal backend async-safety and behavior convergence

**Status**: Accepted
**Date**: 2026-07-20
**Supersedes**: ADR-0010 (partially — see ADR-0010 disposition; D1–D7 of
ADR-0010 stand; this ADR revises backend-layer I/O safety, shared-behavior
placement, and tmux correctness)

## Context

The terminal system shipped by ADR-0010 was architecturally sound (two design
axes, ABC-driven backend split, single poll loop) but the *backend
implementations* shipped with three classes of latent defect that surfaced in
production as "页签卡住" (tab stuck) and "指令输入到一半卡住需要手动 Enter"
(command typed but not submitted). Diagnostic grilling (see
`docs/handoff/2026-07-terminal-grilling-transcript.md`) traced the symptoms to
three independent root causes, all in `src/modex_agent/tools/terminal/backends/`:

### Root cause 1 — synchronous blocking I/O inside `async def`

`WinptyConsoleWindowBackend.read` / `.write` (visible Windows path) call
`socket.recv` / `socket.sendall` directly inside `async def` methods with **no
`loop.run_in_executor` wrapping**. `WinptyHiddenBackend.write` (hidden Windows)
and `PexpectPtyBackend.write` (hidden Linux/macOS) call `proc.write(data)` /
`proc.send(data)` directly inside `async def` with the same defect. Reads on
the two hidden backends were correctly wrapped — only writes were missed —
producing a subtle asymmetry: read paths did not block, write paths blocked the
entire event loop whenever the PTY input buffer filled (e.g. a shell running a
long computation and not draining stdin).

In `WinptyConsoleWindowBackend` the impact was compounded by the tight poll
loop: `poll_until_settled` calls `session.poll_once(timeout=0.05)` up to 200
times per 10-second yield window. Each call blocks the event loop for up to
50 ms. A single `bash.execute("...")` can therefore freeze the agent runtime
for the full yield window — WebSocket messages queue, other tools cannot run,
the WebUI shows the tab as hung.

### Root cause 2 — `socket.settimeout` leak → partial `sendall` → lost Enter

`WinptyConsoleWindowBackend.read()` calls `self._sock.settimeout(timeout)` to
apply a per-call read timeout, but `socket.settimeout` mutates the socket
**globally and persistently** — it is not a per-call parameter. After
`_discard_pending_output(timeout=0.05)` returns, the socket's timeout is left
at 0.05 s. `submit_command` then calls `self._backend.write(command + "\r")`,
which calls `sendall` on the same socket **still timed out at 0.05 s**.

If the host-side `socket_to_pty` thread is slow (blocked on `proc.write` to a
slow shell) the TCP send buffer fills, `sendall` raises `socket.timeout` after
50 ms, and — critically — **`sendall` may have already sent a partial
payload**. The command bytes go out; the trailing `"\r"` does not. The shell
receives the command text without the Enter, leaves it sitting on the readline
input line, and the poll loop then sees the idle prompt, returns
`PROMPT_DETECTED`, and reports "completed" to the agent. The agent proceeds to
its next step believing the command ran. The user sees a half-typed command
and must press Enter manually.

### Root cause 3 — tmux `_diff_output` mismatches on scroll

`TmuxPtyBackend.read()` derives new output by tail-comparing
`capture_pane()` snapshots (`_diff_output`). The comparison matches lines
from the end of the previous snapshot to the end of the current one. When the
pane scrolls (output exceeds the visible window, default 30 rows) the same
logical lines shift position; tail matching fails; the diff returns the
**entire current snapshot** as "new" output, producing duplicates and
occasionally echoed prompt fragments. Any command emitting more than 30 lines
triggers this. Additionally, `is_alive()` spawns a `tmux ls` subprocess via
`run_in_executor` on every call; the poll loop invokes it ~20 times per
second, causing measurable CPU and latency overhead on Linux/tmux deployments.

### Root cause 4 — divergence in copy-pasted backend behavior

Three behaviors that should be identical across backends were independently
copy-pasted, producing three subtly different implementations:

- `current_segment()` — byte-stream backends (visible-windows, hidden-windows,
  pexpect) share an identical 3-line body (`extract_current_segment_from_buffer`
  over `self._output_buffer.text`); tmux has its own variant over
  `capture_pane()`. Three identical copies for the byte-stream path.
- `clear_input_line()` — all four backends implement the same intent ("write
  `\x01\x0b` if the shell uses readline, else no-op") with **three different
  shell-detection heuristics**: visible-windows checks `"bash" in
  self._shell.lower()` (fragile — misses `zsh`, fails on
  `/usr/local/bin/bash` if a future variant doesn't contain the literal
  substring); hidden-windows and pexpect use the correct
  `_family_from_path(self._shell).uses_readline()`; tmux inlines a suffix
  tuple. The fragile check is a latent bug on any shell path that doesn't
  contain "bash" as a substring.
- `drain_startup()` — three backends call the shared `drain_windows_startup`
  helper with identical arguments modulo the `uses_readline` computation
  (subject to the same divergence as above); tmux has a 25-line inline loop
  that re-implements the same logic against `capture_pane()`.

### Reference survey

Two external references were evaluated during grilling:

- **opencode-pty** (`references/opencode-pty`) — a TypeScript plugin using
  `bun-pty` with a single `onExit` event and no prompt detection. It avoids
  the Enter-loss problem by not using PTY write for command submission at all;
  the human watches the panel. It has nothing the framework can borrow for
  autonomous agent reliability.
- **OpenCode CLI** (`references/opencode`) — its bash tool spawns a child
  process with `stdin: "ignore"` and detects completion via
  `Effect.raceAll([exitCode, abort, timeout])`; its PTY session manager is a
  human-facing passthrough with no completion detection. Both avoid the
  framework's reliability problems by **not having** the framework's
  capabilities (interactive PTY, prompt detection, tab management). Borrowing
  OpenCode's architecture would mean abandoning `ProcessTool`, pager handling,
  password prompts, and TUI support — explicitly the wrong direction given the
  stated goal of strengthening the existing system.

Both references confirmed: the framework's PTY-based autonomous-agent terminal
is solving a harder problem than either reference attempts. The fix is to
repair the existing implementation, not to borrow an architecture that avoids
the problem by removing capability.

## Decision

### D1 — Async-safety contract enforced by template method + opt-in hooks

`TerminalBackend` (the ABC in `backends/base.py`) is revised so that the
async-safety contract is **structural, not convention**:

1. `TerminalBackend.write(data)` and `TerminalBackend.read_pending(timeout,
   max_size)` become **concrete template methods** in the base class. They
   delegate to two opt-in hooks:
   - `_write_blocking(self, data: str) -> None`
   - `_read_blocking(self, timeout: float, max_size: int) -> str`

   The default hook implementations `raise NotImplementedError`. Backends
   whose I/O is fundamentally synchronous (the three pywinpty/pexpect/tmux
   paths) implement the hooks; the template wraps them in
   `loop.run_in_executor(None, ...)` and the contract is satisfied
   structurally.

2. Backends with **native async I/O** (the visible-windows path after D2
   below) **override `write` and `read_pending` directly** and do not
   implement the hooks. This is the explicit escape hatch for transports
   whose underlying API is already `await`-shaped (asyncio `StreamWriter` /
   `StreamReader`).

3. Backends with a **fundamentally different I/O model** (tmux, which uses a
   control protocol of `send_keys` / `capture_pane` rather than a byte
   stream — see D5) **override `write` and `read_pending` directly** and do
   not implement the hooks.

4. The hooks are **opt-in, not abstract**: a backend that overrides
   `write`/`read_pending` natively is not forced to implement
   `_write_blocking`/`_read_blocking`. This prevents tmux and visible-windows
   from being required to provide no-op stubs for hooks they do not use.

### D2 — `WinptyConsoleWindowBackend` rewritten with asyncio native streams

The visible Windows backend is rewritten to use `asyncio.open_connection` /
`asyncio.start_server` instead of raw `socket.socket`. Concretely:

- Parent side (`visible_windows.py`) replaces `socket.socket` + `server.accept()`
  with `asyncio.start_server` + `await reader.read(n)` / `await writer.write()`
  + `await writer.drain()`.
- Host side (`visible_windows_host.py`) replaces `socket.connect` +
  `sock.recv` / `sock.sendall` with `asyncio.open_connection` + the
  corresponding `StreamReader` / `StreamWriter` calls. The two existing
  forwarding threads (`pty_to_socket`, `socket_to_pty`) become asyncio tasks
  within a single event loop in the host process.
- `TCP_NODELAY` is set on both sides' transports (via
  `sock.setsockopt` after `asyncio.open_connection` exposes the underlying
  socket, or via the `Transport`'s `set_tcp_nodelay` helper).
- `socket.settimeout` is **removed entirely** — asyncio streams have no
  per-call socket timeout state to leak. Read timeouts are expressed as
  `asyncio.wait_for(reader.read(n), timeout=…)` which wraps in a `Future`
  without mutating the transport.

This structurally eliminates root cause 2 (the `settimeout` leak that produced
partial `sendall` and lost Enter). It also removes root cause 1 for the
visible-windows path: `await writer.drain()` is genuinely non-blocking;
`await reader.read(n)` is genuinely non-blocking.

The host-side script remains a separate process spawned with
`CREATE_NEW_CONSOLE` — the visible-window architecture is unchanged. Only the
IPC layer (parent ↔ host) moves from raw sockets to asyncio streams.

### D3 — `WinptyHiddenBackend.write` and `PexpectPtyBackend.write` wrapped via the hook contract

Both backends implement `_write_blocking` (their existing one-line body —
`self._proc.write(data)` / `self._proc.send(data)`) and **delete their
`async def write` overrides**. The base-class template method then wraps the
hook in `run_in_executor`, fixing root cause 1 for the two hidden paths.

`_read_blocking` is similarly extracted from each backend's existing
`_do_read` closure. The `async def read_pending` override is deleted; the
base-class template handles the executor wrapping, the buffer append, and the
`TerminalRead` construction. The base class owns the `SlidingOutputBuffer`
(D4 below) so the buffer append is also unified.

### D4 — Shared behavior converges to `TerminalBackend` concrete methods

The three copy-pasted behaviors move to `TerminalBackend` as concrete methods
backed by an abstract shell-detection hook:

1. **`current_segment()`** — concrete in the base class. Default
   implementation: `extract_current_segment_from_buffer(self._output_buffer.text)`
   (the byte-stream variant currently duplicated 3×). `TmuxPtyBackend`
   overrides it to use `capture_pane()` (D5 below). All three byte-stream
   backends delete their identical overrides.

2. **`clear_input_line()`** — concrete in the base class. Implementation
   uses the abstract hook `_shell_family() -> ShellFamily` (D4.1 below) and
   writes `"\x01\x0b"` iff `_shell_family().uses_readline()`. All four
   backends delete their overrides.

3. **`drain_startup()`** — concrete in the base class. Default
   implementation calls the shared `drain_windows_startup` helper, passing
   `self.read`, `self.write`, `self.is_alive`, and
   `uses_readline=_shell_family().uses_readline()`. `TmuxPtyBackend` overrides
   it because tmux's I/O model requires `capture_pane`-based prompt detection
   rather than byte-stream prompt detection (D5). The three byte-stream
   backends delete their identical overrides.

4. **Buffer management** — `TerminalBackend.__init__` allocates
   `self._output_buffer: SlidingOutputBuffer | None = None`. The three
   byte-stream backends set it to a fresh `SlidingOutputBuffer()` in their
   own `__init__` (via `super().__init__()` then assignment).
   `TmuxPtyBackend` leaves it `None` and uses `_last_capture` (D5) — the base
   class's `output_buffer_text` / `current_segment` / `buffer_size` /
   `clear_buffer` already handle `None` gracefully (returning empty / falling
   through to the override).

#### D4.1 — Abstract hook `_shell_family() -> ShellFamily`

`TerminalBackend` gains the abstract method:

```python
@abstractmethod
def _shell_family(self) -> ShellFamily:
    """Return the shell family of the running shell."""
```

Each backend implements it, typically as a one-liner:

```python
def _shell_family(self) -> ShellFamily:
    return _family_from_path(self._shell or "")
```

This is preferred over declaring `self._shell: str | None` in the base class
(D4.1 rejected sub-option) because it makes the contract explicit: the base
class asks "what shell family are you?" and the subclass answers, rather than
the base class reaching into a private field that may or may not be set. This
matches the project's existing rule "ABCs before implementations" and the
type-safety rule "no `getattr`/`hasattr`/`isinstance` except at real
extension boundaries" — `_shell` is set inside each backend's `start()` and
its presence/absence is an implementation detail the base class should not
probe.

### D5 — `TmuxPtyBackend` keeps its snapshot I/O model; diff and `is_alive` fixed in-place

Tmux's I/O model is fundamentally a **control-protocol snapshot** (send_keys
writes; capture_pane reads a pane snapshot), not a byte stream. Forcing tmux
into a byte-stream shape via `pipe-pane` + temp-file + truncation was
considered (sub-option P) and rejected: it would add a `cat` subprocess, a
temp file, a truncation strategy, and an offset manager per session — all to
make tmux "look like" the other backends. That is over-convergence: it trades
real complexity for surface uniformity. The stated goal is correctness and
usability, with convergence as a means rather than an end.

Tmux therefore remains a **snapshot backend** that overrides `write`,
`read_pending`, `current_segment`, and `drain_startup` directly (per D1
point 3). Two correctness fixes land inside it:

1. **diff bug fix** — `capture_pane` switches from the default visible-window
   snapshot to `capture-pane -p -S -` (full scrollback, default 2000 lines).
   `_diff_output` switches from tail-matching to **prefix-matching**: check
   whether the previous snapshot's lines are a prefix of the current
   snapshot's lines; if so, return the suffix as new output. This matches the
   actual semantics of pane output (lines are appended at the bottom; lines
   that scroll off the top disappear from the visible window but remain in
   scrollback under `-S -`). Prefix-matching failure (rare: output scrolls
   beyond the 2000-line scrollback) falls back to returning the entire
   current snapshot — same as today's behavior, but only in the genuine edge
   case rather than on every >30-line command.

2. **`is_alive()` cache** — a 1-second TTL cache on the session-existence
   check. The poll loop calls `is_alive()` ~20×/s; without the cache each
   call spawns a `tmux ls` subprocess via `run_in_executor`. The cache stores
   `(timestamp, bool)`; calls within the TTL return the cached bool, calls
   past the TTL re-query `self._server.sessions`.

Tmux's `current_segment()` and `drain_startup()` overrides remain — they are
genuinely different from the byte-stream defaults because they read from
`capture_pane()` rather than `self._output_buffer`. The shared
`clear_input_line()` (D4.2) and the abstract `_shell_family()` (D4.1) apply
to tmux like any other backend.

### D6 — Architecture guard test for async-safety contract

A new test `tests/architecture/test_terminal_async_safety.py` asserts, per
`TerminalBackend` subclass:

1. If the subclass does **not** override `write` or `read_pending`, then it
   must implement `_write_blocking` / `_read_blocking` respectively (so the
   base-class template has something to wrap). This catches the regression
   "deleted the hook but didn't override the template."
2. The subclass's source for `write` and `read_pending` (whichever it
   overrides, or the base template if it doesn't) must contain either
   `run_in_executor` or `await` — i.e. there must be evidence of non-blocking
   I/O. Bare `socket.sendall` / `proc.write` / `proc.send` / `pane.send_keys`
   calls inside an overridden `write` or `read_pending` are flagged, unless
   they appear inside a `_write_blocking` / `_read_blocking` definition (where
   the base-class template will wrap them).
3. `_shell_family` is implemented on every concrete subclass.

This is an AST/regex guard, ~50 LOC, no runtime cost. It catches the exact
class of regression that produced root cause 1.

### D7 — No echo verification

An echo-verification step ("after `write(command + "\r")`, read 500 ms and
confirm the shell echoed the command back; retry up to N times") was
considered as defense-in-depth against the lost-Enter failure mode and
**rejected**:

- D2 structurally eliminates the lost-Enter failure mode on the
  visible-windows path (no `settimeout` leak, no partial `sendall`).
- D3 structurally eliminates blocking-write stalls on the two hidden paths.
- D5's tmux path uses `send_keys(enter=False)` followed by a separate
  `send_keys("", enter=True)` — tmux's control protocol either delivers the
  command or raises; there is no partial-write surface.
- Echo verification would add ~500 ms latency per command on the happy path,
  would misfire on non-echoing shells, and would require shell-family
  detection to disable — adding complexity for defense against a failure mode
  that D1–D5 already eliminate structurally.

Echo verification remains **not** part of the terminal system.

## Considered Options

### Async-safety contract placement

- **(Chosen) D1 — template method + opt-in hooks.** Three of four backends
  share the wrapping pattern; the base-class template captures it. The
  visible-windows and tmux backends override the template directly because
  their I/O shape is genuinely different. The hooks are opt-in (not abstract)
  so neither escape-hatch backend is forced to provide no-op stubs.
- **Pure ABC + `_run_blocking` helper + convention + guard test.** Rejected:
  convention is what produced root cause 1. The guard test (D6) is added
  regardless, but convention alone does not structurally prevent the
  regression class.
- **Separate `AsyncPtyTransport` ABC below `TerminalBackend`.** Rejected: the
  visible-windows backend's `asyncio.StreamWriter`/`StreamReader` is already a
  transport interface; wrapping it in another `AsyncPtyTransport` is pure
  indirection. D1's template captures the same contract with less surface.

### Visible-windows IPC rewrite

- **(Chosen) D2 — asyncio native streams.** Eliminates both root cause 1 and
  root cause 2 on this path. Same TCP transport, same host-process
  architecture; only the I/O API changes.
- **Minimal patch — wrap `recv`/`sendall` in `run_in_executor`, reset
  `settimeout(None)` before each `sendall`, set `TCP_NODELAY`.** Rejected:
  leaves the `run_in_executor` thread-pool pattern (latency under load) and
  the raw-socket fragility. D2 is cleaner for the same effort.
- **Windows named pipes.** Rejected: would replace one IPC mechanism with a
  Windows-specific one; the host-process rewrite cost is the same as D2 but
  the API surface is less portable and less well-understood.
- **Abandon the host process + socket bridge entirely.** Rejected: the
  visible-window architecture exists precisely because Windows requires
  `CREATE_NEW_CONSOLE` to give the human a visible window, and that flag
  forces a separate process. D2 keeps the architecture and fixes the I/O.

### Shared-behavior placement

- **(Chosen) D4 — concrete methods on the base class + abstract
  `_shell_family` hook.** Single source of truth; the fragile
  `"bash" in self._shell.lower()` check is replaced by the correct
  `_family_from_path` everywhere. The base class holds the byte-stream
  defaults; tmux overrides the methods where its I/O model diverges.
- **Intermediate mixins (`BufferedBackend` / `ReadlineBackend` /
  `StartupDrainBackend`).** Rejected: mixins depend on subclass methods
  (`read`/`write`/`is_alive`) without expressing the contract; the
  `drain_startup` mixin would need to call `self.read` / `self.write` /
  `self.is_alive` which the mixin cannot guarantee at composition time. The
  base-class approach uses abstract methods to express the contract cleanly.
- **Status quo + tmux-only fix.** Rejected: leaves the three divergent
  shell-detection heuristics in place, including the fragile substring
  check. The convergence goal is explicitly endorsed; status quo contradicts
  it.

### Tmux I/O model

- **(Chosen) D5 — keep snapshot I/O; fix diff and `is_alive` in-place.**
  Tmux's control-protocol I/O is its essential shape; forcing byte-stream
  convergence would add complexity without correctness gain. The diff bug and
  `is_alive` performance bug are localized fixes that do not change the
  backend's I/O model.
- **`pipe-pane` byte-stream conversion.** Rejected: per-session `cat`
  subprocess + temp file + truncation strategy + offset manager is
  over-convergence. Replaces a 25-line diff with ~80 lines of file management
  for no correctness gain beyond what the prefix-match diff already provides.
- **Status quo (no fixes).** Rejected: the diff bug is a correctness defect
  on any command emitting >30 lines; `is_alive` performance is a measurable
  regression under poll-loop load.

### Echo verification

- **(Chosen) D7 — no echo verification.** D1–D5 eliminate the lost-Enter
  failure mode structurally. Echo verification adds latency, complexity, and
  shell-detection burden for defense against an already-eliminated failure.
- **Echo verification as defense-in-depth.** Rejected: defense-in-depth is
  justified when the primary defense is probabilistic; D1–D5 are structural
  (not probabilistic) eliminations of the root cause.

## Consequences

### Direct structural consequences

- `backends/base.py` — `TerminalBackend` gains:
  - Concrete `write(data)` and `read_pending(timeout, max_size)` template
    methods delegating to `_write_blocking` / `_read_blocking` via
    `run_in_executor`.
  - Concrete `current_segment()`, `clear_input_line()`, `drain_startup()`
    using the byte-stream defaults.
  - Abstract `_shell_family() -> ShellFamily`.
  - Opt-in `_write_blocking` / `_read_blocking` hooks (default
    `raise NotImplementedError`).
- `backends/visible_windows.py` + `backends/visible_windows_host.py` —
  rewritten per D2. Both use `asyncio.open_connection` /
  `asyncio.start_server`. The host process runs its own asyncio event loop
  with the two forwarding coroutines as tasks. `TCP_NODELAY` set on both
  sides. `socket.settimeout` deleted entirely.
- `backends/windows_hidden.py` — implements `_write_blocking` (was
  `self._proc.write(data)`) and `_read_blocking` (was the body of the
  `_do_read` closure). Deletes `async def write`, `async def read`,
  `async def read_pending`, `async def current_segment`,
  `async def clear_input_line`, `async def drain_startup`. Keeps `start`,
  `interrupt`, `is_alive`, `terminate`, `kill`, `_uses_readline` (now unused
  but retained for one release as a deprecated private API),
  `_shell_family`.
- `backends/pexpect_pty.py` — same shape as `windows_hidden.py`: implements
  the two hooks, deletes the six overrides, keeps `start` / `interrupt` /
  `is_alive` / `terminate` / `kill` / `_shell_family`.
- `backends/tmux_pty.py` — overrides `write`, `read_pending`,
  `current_segment`, `drain_startup` directly (no hooks). Fixes
  `_diff_output` to prefix-matching with `capture-pane -p -S -`. Adds
  `_alive_cache: tuple[float, bool] | None` and a 1-second TTL check in
  `is_alive`. Implements `_shell_family`. Deletes `_uses_readline` (now
  unused). The inline shell-suffix tuple is removed.
- `backends/winpty_transport.py` — `WinptyBackend` umbrella unchanged (it
  already exists only for the factory capability table to refer to "the
  winpty transport").
- `backends/factory.py` — unchanged. The capability table already
  references `WinptyConsoleWindowBackend`, `WinptyHiddenBackend`,
  `PexpectPtyBackend`, `TmuxPtyBackend` by name; the per-decision changes
  above are internal to each backend.
- `tests/architecture/test_terminal_async_safety.py` — new guard test (D6).
- `tests/architecture/test_terminal_backend_contract.py` (if it exists) or a
  new sibling — asserts that every concrete `TerminalBackend` subclass
  implements `_shell_family`, and that the three shared behaviors
  (`current_segment` / `clear_input_line` / `drain_startup`) are either
  inherited from the base class or explicitly overridden (no accidental
  mid-tier duplication).
- Existing unit tests of `WinptyConsoleWindowBackend` /
  `WinptyHiddenBackend` / `PexpectPtyBackend` / `TmuxPtyBackend` — the
  external behavior (start, write, read, is_alive, terminate, interrupt)
  is unchanged; tests that poked `settimeout` directly or asserted on raw
  socket internals are updated. The `_diff_output` test for tmux is
  rewritten to cover the prefix-match semantics, including the scroll case.

### Behavior changes

- **Visible Windows path**: no more "command typed but not submitted" — D2
  eliminates the `settimeout` leak and partial `sendall`. No more
  "tab stuck" — D2's `await writer.drain()` / `await reader.read(n)` is
  genuinely non-blocking.
- **Hidden Windows / pexpect paths**: no more event-loop stalls under
  write-buffer pressure — D3's `run_in_executor` wrapping of `_write_blocking`
  prevents the event loop from blocking when the PTY input pipe is full.
- **Tmux path**: no more duplicate output on >30-line commands — D5's
  prefix-match diff over full scrollback is correct under scroll. Lower
  CPU/latency under poll-loop load — D5's `is_alive` 1-second cache.
- **All paths**: `clear_input_line` now uses the correct shell-family
  detection everywhere — the fragile `"bash" in self._shell.lower()` check
  is gone. zsh and sh are correctly handled on the visible-windows path.
- **All paths**: `current_segment` / `drain_startup` are single-source on
  the base class — future fixes land in one place.

### Non-changes (explicit)

- The three-tool layer (`CommandTool` / `ProcessTool` / `TerminalTool`) is
  unchanged. The poll loop (`poll_until_settled`) is unchanged. The prompt
  detection (`is_prompt_ready` / `is_waiting_for_input` /
  `detect_pager_entry`) is unchanged. The `SlidingOutputBuffer` is
  unchanged. The `TerminalSession` is unchanged. The
  `BaseTerminalManager` is unchanged. ADR-0010's L2/L4/L5 decisions stand.
- `SubprocessTool` is unchanged (it is not a `TerminalBackend`).
- The `WinptyBackend` umbrella (`backends/winpty_transport.py`) is
  unchanged — it carries no I/O logic today and continues to carry none.
- The factory's capability table is unchanged.
- The `visible_windows_host.py` host process still uses
  `CREATE_NEW_CONSOLE` and still owns the winpty `PtyProcess`. Only the
  parent↔host IPC layer changes.
- No new domain term is added for "snapshot backend" or "byte-stream
  backend" — these are implementation shapes, not language. The
  `Async-Safety Contract`, `Blocking-IO Hook`, and `Shell Family Hook`
  glossary entries (see `CONTEXT.md` update) name the contract concepts;
  the backend shapes are described in this ADR but not elevated to
  ubiquitous-language terms.

### Open questions deferred

- **Web UI terminal panel** (xterm.js + WebSocket) — out of scope for this
  ADR. Tracked separately as a candidate follow-up; the ADR-0032 changes do
  not block it and are not blocked by it.
- **`notifyOnExit` equivalent** — out of scope. The current synchronous
  poll-loop completion detection stands; an async notification path would be
  a separate ADR.
- **Per-tab visibility override** (relaxing the manager-level
  single-visibility constraint) — out of scope. ADR-0010 Decision 1 stands;
  per-tab visibility would be a separate ADR if a real use case emerges.

### Migration

- One-release deprecated alias window is **not** required: no public class
  names change (`WinptyConsoleWindowBackend`, `WinptyHiddenBackend`,
  `PexpectPtyBackend`, `TmuxPtyBackend` all keep their names). The
  `VisibleWindowsPtyBackend` / `WindowsHiddenPtyBackend` deprecated aliases
  from ADR-0010 continue to work unchanged.
- Internal call sites that reached into `self._sock.settimeout` or
  `self._proc.write` directly (none in production code outside the
  backends themselves) are unaffected.
- The `_uses_readline` private method on the three byte-stream backends is
  retained for one release as a deprecated private API (no callers after
  D4) and removed in the next release. It is not exported and not part of
  any interface.
