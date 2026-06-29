# Terminal system design axes: two visible axes, OS collapsed to implementation

The terminal tool chain historically presented three orthogonal axes to
upstream code: **operating system** (Windows / Linux, materialised as
`WindowsHiddenTerminalManager` / `WindowsVisibleTerminalManager` /
`LinuxTerminalManager`), **visibility** (visible / hidden, materialised as
specific backend subclasses), and **shell family** (bash / …, materialised as
`ShellInfo`). The first axis leaked an implementation concern into the
manager layer; the second mixed a *semantic* property with the *mechanism*
each OS uses to realise it. This ADR records the resulting shape and the
diagnostic for how visibility differences should be expressed.

## Decision

1. **Upstream code sees two design axes, not three.** The terminal system is
   parameterised by **Shell Family** (behavioural shape of the shell — prompt
   pattern, line ending, readline capability) × **Visibility** (whether a
   human can observe or intervene in the window). The operating system is
   *not* a design axis: it is an implementation concern collapsed entirely
   into `TerminalBackend` subclasses.

2. **OS-named manager subclasses are removed.** `WindowsHiddenTerminalManager`,
   `WindowsVisibleTerminalManager`, `LinuxTerminalManager` are deleted (their
   capabilities move to one manager taking `shell_family` + `visibility`;
   `create_terminal_manager(from_kind_string, ...)` is replaced or renamed to
   `create_terminal_manager(shell_family, visibility, ...)`).

3. **Backend subclasses are named by transport, not by OS, not by visibility.**
   The shipped transports: `WinptyBackend` (Windows), `PexpectBackend`
   (Unix), `TmuxBackend` (Unix). Each declares which `visibility` values it
   can serve; unsupported combinations are rejected at the factory, never
   silently fallen-back.

4. **A visibility difference is expressed by subclass when it is structural,
   by parameter when it is a switch.** The diagnostic:
   - **Structural** — different I/O architecture (e.g. in-process WinPTY vs
     a separate host process with a visible OS console window and a local
     TCP socket bridging it). The two paths share an OS but share almost
     nothing else; pretending they are one class hides a fork inside
     `start()`. Expressed as two subclasses of the same transport:
     `WinptyHiddenBackend` and `WinptyConsoleWindowBackend`.
   - **Switch** — one flag, same I/O path (e.g. tmux `new_session(attach=…)`).
     Expressed as a `visibility` parameter to a single `TmuxBackend`.

5. **`PexpectBackend` is visible-only-rejected.** pexpect is a PTY reader;
   making its sessions visible to a human would require a different transport
   (X11 forwarding, an attachable multiplexer, or spawning an `xterm`). Those
   would be new transports, not extensions to `PexpectBackend`. The factory
   rejects `(pexpect, VISIBLE)` explicitly so the type contract does not lie.

6. **Linux-visible MVP is `TmuxBackend(visibility=VISIBLE)`** (current
   `TmuxPtyBackend` already wraps tmux; today it hard-codes
   `new_session(attach=False)`, changing the flag realises visibility). This
   choice is reversible — adding a non-tmux Linux-visible transport later
   means adding one row to the factory table, not editing any transport.

7. **L2 Session owns state and primitives; L5 Tools own orchestration.**
   `TerminalSession.execute(command, timeout)` is deleted. Its former
   responsibility ("submit a command and settle until completion") is an
   orchestration belonging to `CommandTool`, the same way `ProcessTool`
   already composes `session.write` + `poll_until_settled` rather than
   calling an in-Session helper. All three terminal tools converge on one
   pattern:

   ```
   CommandTool.execute(cmd, timeout):
     guard → ensure_started_or_restart → clear_input_line_if_needed
            → session.submit(cmd) → poll_until_settled
            → session.apply_outcome(result) → record_history → format XML

   ProcessTool._do_write/_do_submit/_do_send_keys/_do_paste/_do_interrupt/_do_kill:
     guard → session.write / send_interrupt / kill
            → _drain_terminal_after_action → poll_until_settled → format XML

   TerminalTool.open/close/list/select/interrupt:
     manager.get_or_create / close / list / select / session.send_interrupt
     (no poll, no drain)
   ```

   The `Session` surface narrows to state-machine primitives and last-mile
   mutation:

   - **Primitives** (single step, no orchestration): `submit(command)`,
     `write(data)`, `poll_once(timeout, max_size) -> TerminalRead`,
     `command_status(config) -> TerminalCommandStatus`, `current_segment()`,
     `last_command_output()`, `interrupt()`, `kill()`, `is_alive()`,
     `ensure_started_or_restart()`, `clear_input_line()`.
- **State events** (single setter, semantically meaningful): one entry
      point `apply_outcome(result: PollResult)` that takes the L3 poll result
      and updates `_busy_after_timeout`, `_last_status`, `_command_started_at`
      per the outcome — Session owns the state-transition table, Tools do
      not poke private fields.
      **`set_expected_state(status)` is preserved** alongside `apply_outcome`
      because the two setters write **disjoint slots**: `set_expected_state`
      writes `self._expected_state` (the interference-detection slot consumed
      by `TerminalSession.detect_interference`, itself consumed by
      `TerminalTool` to detect when a human interferes with a visible
      terminal); `apply_outcome` writes `_busy_after_timeout` /
      `_last_status` / `_command_started_at`. Neither subsumes the other;
      both coexist under this decision. Tools continue to call
      `set_expected_state(...)` for interference detection AND
      `apply_outcome(result)` for the busy/last_status/command_started slot.
   - **History** lives on Session (it is cross-command state); it is
     appended by `record_history(command, output)` called *by CommandTool*
     at the end of its orchestration, because recording is a CommandTool
     decision (a "command" is the unit of history), not a Session invariant.
   - **Guard** (L3) reads `session.command_status()`,
     `session.current_segment()`, `session.last_command_output()`, and
     `session.timing()` — a new public read-only view returning
     `(elapsed_ms, idle_ms)` — so it no longer pokes `_command_started_at`
     and `_last_byte_at`.
   - **`exit` / `logout` / `quit`** special-case: the early-drain-on-dead-PTY
     path (write the command → drain ~3s on a dying PTY → mark
     `_needs_restart` → terminate backend) is a *CommandTool* concern, not
     a Session primitive; it composes Session primitives.

   This decision aligns the three tools to one structural pattern (`ProcessTool`
   is already on it, `TerminalTool` is a degenerate case of it, only
   `CommandTool` had diverged) and closes the dual-poll-loop leak that
   `TerminalSession.execute`'s in-line read loop had created (`PollOutcome`
   values PAGINATED / LONG_RUNNING / STUCK were not visible to that loop).

## Considered Options

### Design axes for upstream code

- **(Chosen) Two design axes (Shell Family × Visibility); OS at
  implementation layer; visibility differences split structural (subclass)
  vs switch (parameter); unsupported visibility/transport combos rejected
  at factory.** Strips the OS-named subclasses from the manager layer;
  keeps the I/O-architecture-level visibility differences honestly split
  as subclasses; lets switch-level differences share one transport.

- **Three design axes including OS.** Rejected: keeps `WindowsTerminalManager` /
  `LinuxTerminalManager` as named types upstream. Today's `WindowsTerminalManager`
  already has to specialise into Hidden/Visible sub-subclasses, so "OS axis"
  was not collapsing any real variance — it was naming an implementation
  fork as if it were a design dimension.

- **Visibility as a backend immutable property, fixed per subclass — Option A
  in grilling.** Rejected: forces a new transport subclass per (`transport`,
  `visibility`) cell of the matrix even when the two cells share I/O
  architecture (tmux attached vs detached). Doubles the subclass count for
  no locality benefit.

- **Visibility as a constructor parameter on every backend — Option B in
  grilling.** Rejected: the API would advertise visible-`PexpectBackend` as
  legal. The factory would have to silently fall back, lying through its
  type signature.

### Should there be an upper ABC over terminal, subprocess, and process tooling?

The terminal system covers three L5 tools (`CommandTool`, `ProcessTool`,
`TerminalTool`) for *one* execution shape, and `SubprocessTool` is a separate
single-tool shape for *another*. An upper ABC was considered to unify them.

- **(Chosen) No upper ABC. L5 keeps its current shape; L1-L4 are where the
  real refactoring effort goes.** The terminal three-tool set and the
  SubprocessTool single tool expose genuinely *different* capabilities to the
  LLM; terminal's PTY path has `ProcessTool` (interact with a running
  process) and `TerminalTool` (multi-tab / window) that have no subprocess
  equivalent. Wrapping them in a single upper ABC (be it per-tool facades or
  a master `CommandExecutor`) would drag methods like `read_pending` /
  `interrupt` / `current_segment` into an interface that timespec-pure
  SubprocessTool genuinely cannot implement; or — if narrowed down to just
  `execute(cmd)` — would have only two callers (`CommandTool` and
  `SubprocessTool`) out of the broader four tools, and even then only for
  one shape; subagents never cross the seam because they always get
  `SubprocessTool` directly (see "Scope of the terminal system" in
  Consequences). The seam would not earn its keep: it has one real
  implementation per side instead of "two or more genuine subclass
  implementations" required by ADR-0007. Putting effort into L1-L4
  convergence (dual `execute` path in L2, dual-manager duplication in L4)
  is the higher-leverage move.

- **Per-tool upper ABCs (`CommandExecutor` / `ProcessController` /
  `TerminalRegistry`) with Null/Noop implementations on the subprocess
  side.** Rejected: a Null/Noop object is not the same thing as "no tool
  registered." The agent's tool list should actually be empty for
  `process`/`terminal` on subprocess-only deployments — a Noop that
  returns UNSUPPORTED still costs one LLM tool slot and one round-trip per
  confused tool call. Honest registration (don't register the tool at all
  when its backing capability is absent) is cleaner and matches the
  existing pool-builder branch.

- **Fold subprocess as one more backend under `TerminalBackend`.** Rejected
  on naming: subprocess is not terminal. Forcing a "Terminal" subclass that
  has neither a PTY nor visible windows would misname the abstraction and
  push the `read_pending` / `interrupt` shape into a class that cannot
  honour it. The result is a "TerminalBackend" that means nothing — the
  domain term loses its meaning.

### Where does "submit-and-settle until completion" live?

`TerminalSession.execute(command, timeout)` historically owned the
"submit-and-settle" loop, with its own inline read/prompt-detect cycle
that diverged from the newer `poll_loop.poll_until_settled` (which knows
about `PollOutcome.PAGINATED` / `LONG_RUNNING` / `STUCK`; the inline loop
does not). `ProcessTool` and `TerminalTool` never went through
`Session.execute` — only `CommandTool` did. So `Session.execute` served
exactly one caller, with stale semantics.

- **(Chosen) Delete `Session.execute`. Compose the orchestration inside
  `CommandTool` using `Session.submit` + `poll_until_settled` + state
  events (matches `ProcessTool`'s existing pattern).** Session narrows to
  state + primitives + state-event entry points; `poll_until_settled`
  remains the single settle helper shared by CommandTool and ProcessTool.
  This removes the dual loop, removes the divergent semantics, aligns
  the three tools to one structural pattern. Layout-side helpers
  (`ensure_started_or_restart`, `clear_input_line_if_needed`) become thin
  orchestration hooks on Session or in CommandTool; the `exit`/`logout`
  early-drain path moves to CommandTool because it is "submit a command
  that happens to kill the shell" — a CommandTool branch, not a primitive.
  Guard stops poking private fields via a new `Session.timing()` read view
  and a single `Session.apply_outcome(result)` setter.

- **Keep `Session.execute` as an *internal-only* deprecated path; let
  `CommandTool` call `poll_until_settled` instead.** Rejected: keeping a
  second settle loop — even one labelled "internal" — leaves the dual-loop
  bug alive; any future caller of Session helpers might regress to it, and
  tests must still cover the stale semantics. The fix is to remove the
  stale code, not to quarantine it.

- **Move the settle helper into `Session` (as `Session.poll_until_settled`)
  instead of keeping it in L3.** Rejected: it widens Session's surface
  (from primitives + state events to a 130-line settle orchestrator), and
  `ProcessTool._drain_terminal_after_action` would still need the same
  helper for slightly different parameters — having it on Session creates
  pressure for a `drain()` near-helper inside Session anyway. Keeping the
  settle helper in L3 (stateless) and Session primitives/state-machine
  in L2 is a cleaner seam.

- **Keep `Session.execute` but rewrite its body to call
  `poll_until_settled`.** Rejected: `Session.execute` then becomes a one-line
  thunk open-coded for exactly one caller (`CommandTool`). The deletion test
  wins: collapse the thunk into its caller; Session's interface stays deep.

8. **One terminal manager. Optional LRU + persistence + memory-pressure
   are capability flags on `BaseTerminalManager`, not a second class.**
   The historical `TerminalManager` (`manager.py`, LRU + JSON persistence +
   memory-pressure buffer clearing) had zero production callers but real
   per-ADR-0007 helpers; that ADR named "folding capability inward" as the
   alternative to "clarifying roles". This ADR folds inward.

   `BaseTerminalManager.__init__` gains three optional capability
   parameters, all default-off — when all three are off the manager is
   behaviour-equivalent to today's lean form:

   ```
   BaseTerminalManager(
       *, shell_family, visibility, backend_factory, config=None,
       default_cwd=None,
       max_terminals: int | None = None,        # None → never evict
       storage_dir: Path | None = None,         # None → no JSON persistence
       enable_memory_pressure: bool = False,    # False → skip memory-pressure hook
   )
   ```

   LRU eviction (`_evict_oldest`), JSON save/load (`save_state` /
   `load_state`), and memory-pressure buffer clearing
   (`_check_memory_pressure`) move *as private methods* into
   `BaseTerminalManager`, guarded by their flags. The `TerminalManager`
   class is deleted from `manager.py`; the file retains a single line
   `TerminalManager = BaseTerminalManager  # deprecated alias` for a
   one-to-two release test migration window (existing e2e tests
   `tests/verify_terminal_e2e_cmd.py` etc. reference `TerminalManager`
   by name; they continue to work through the alias).

   `tests/architecture/test_terminal_manager_seam_preserved.py` is
   updated to guard the *intent* of ADR-0007's seam under the new
   structure: the `TerminalManagerBase` ABC still exists, at least one
   production subclass (`BaseTerminalManager`) still realises it, and
   the three capability helpers (LRU / persistence / memory-pressure)
   still live as real implementations in the module — not as stubs and
   not deleted.

   This decision follows ADR-0007's explicit "folding capability inward"
   branch and is also consistent with Decision 1 of this ADR
   (OS-named manager subclasses were removed for the same reason: the
   manager layer should not fork per-axis-or-capability).

### Should `TerminalManager` (LRU + persistence) survive as a second class?

ADR-0007 left the `TerminalManager` vs `BaseTerminalManager` duplication
to be resolved in "that candidate's grilling" — this is that grilling.

- **(Chosen) Fold the capabilities into `BaseTerminalManager` as
  optional flags; delete the `TerminalManager` class; keep a deprecated
  import alias for the test-migration window.** Single manager with
  capability flags; behaviour-equivalent to lean form when all flags
  are off; ADR-0007's seam intent (real ABC + real capability helper
  implementations preserved) honoured. Consistent with Decision 1
  (treat axes by folding inward, not by multiplying upstream-named
  subclasses).

- **Keep `TerminalManager` as a subclass of `BaseTerminalManager`, LRU /
  persistence / memory-pressure expressed as mixins.** Rejected: ADR-0007
  offered "clarifying roles **or** folding capability inward" — folding
  inward is the branch that matches Decision 1's spirit (collapse upstream
  forks); keeping `TerminalManager` as a subclass continues the
  "Manager-named classes for capability variants" pattern that this ADR
  is unwinding at the OS-named layer. Python mixin composition adds MRO
  complexity for marginal reuse, and the mixins would only have one
  user each.

- **Leave both `TerminalManager` and `BaseTerminalManager` indefinitely.**
  Rejected: ADR-0007 explicitly named the duplication as something to
  resolve in this grilling, not as something to preserve permanently.
  "Real seam" guards the ABC + the capability implementations, not the
  shape of *two distinct classes at the same level*. The architectural
  test is updated to point at the new structure.

- **Delete `TerminalManager` cold (no alias).** Rejected for
  test-surface churn: the e2e verification tests (`verify_terminal_e2e_*`,
  `verify_interaction`) instantiate `TerminalManager` by name with
  `storage_dir` / `max_terminals`. A one- or two-release deprecated
  alias at the old import path is cheap migration scaffolding, not
  architectural debt.

## Consequences

### Scope of the terminal system (clarified)

The terminal system (CommandTool + ProcessTool + TerminalTool) is opt-in
*per main agent* (`PoolConfig.agents[].use_terminal`). Subagents never enter
the terminal system — `src/modex_agent/multi_agent/communication.py:_build_subagent_tool_manager`
explicitly constructs `SubprocessTool` for them, stateless, with no
interactive-process capability. This is deliberate: subagents are
ephemeral, isolated by memory and tools, and have no need for persistent
shell state or visible windows.

When terminal is enabled for a main agent, it may *still* degrade to
`SubprocessTool` (e.g. WSL bash unavailable on Windows, pexpect+tmux both
absent on Linux). The degradation path today is in
`examples/bot_project/bot/service/pool_builder.py` (~line 289); ADR-0010
does not move this path — it remains a per-pool business-layer decision
to keep the framework / examples separation (type-safety rule 5).

This scope is *not itself* recorded in a glossary term. It is a deployment
fact that constrains any future "upper ABC over command execution": such
an ABC would have at most two implementations (`CommandTool` and `SubprocessTool`),
and even the SubprocessTool caller only exists in subagent paths — a true
seam needs "two or more genuine subclass implementations" (ADR-0007), and
unifying tools that are *already different shapes for the LLM* via a
forced single contract would put broad UNSUPPORTED surface on the
subprocess side. The deployment fact therefore *reinforces* the "no upper
ABC" choice above.

### Direct structural consequences

- `managers.py` loses the three OS-named subclasses; manager construction
  switches to `shell_family` + `visibility` parameters. `create_terminal_manager`
  is renamed or replaced to take `shell_family` + `visibility` directly
  (the `kind` string `"windows_hidden"` / `"windows_visible"` / `"linux"` is
  retired).
- `manager.py` loses `TerminalManager` class; an import alias keeps old
  test call sites (`tests/verify_terminal_e2e_cmd.py`,
  `tests/verify_terminal_e2e_bash.py`, `tests/verify_interaction.py`)
  compiling for one-to-two releases. LRU + JSON persistence +
  memory-pressure helpers move into `BaseTerminalManager` as flag-guarded
  private methods. `BaseTerminalManager`'s new optional parameters
  (`max_terminals`, `storage_dir`, `enable_memory_pressure`) all default
  off; the lean form is preserved unchanged. See Decision 8.
- `tests/architecture/test_terminal_manager_seam_preserved.py` is updated
  to assert the *intent* of ADR-0007's seam in the new structure:
  `TerminalManagerBase` ABC continues to exist; at least one subclass
  (`BaseTerminalManager`) realises it; LRU / persistence /
  memory-pressure helper implementations continue to exist as real code
  (not stubs) in the module.
- `backends/visible_windows.py` and `backends/windows_hidden.py` move under a
  transport umbrella (`WinptyBackend` base, two subclasses) — they share the
  transport name but keep their distinct `start()` paths, since the visible
  one spawns an external `visible_windows_host.py` host process with a
  socket bridge, and the hidden one does not.
- `backends/tmux_pty.py` is renamed to `backends/tmux.py`, class
  `TmuxBackend`, and constructs with `visibility`; `new_session(attach=…)`
  takes the flag accordingly.
- `backends/pexpect_pty.py` stays (renamed `PexpectBackend`); its `visibility`
  property returns `HIDDEN` and the factory rejects any other value.
- `backends/factory.py` gains a small capability table
  (`{("winpty", VISIBLE): WinptyConsoleWindowBackend,
  ("winpty", HIDDEN): WinptyHiddenBackend, ("tmux", *): TmuxBackend,
  ("pexpect", HIDDEN): PexpectBackend}`) and raises
  `UnsupportedVisibilityForTransport` for the rest.
- `CONTEXT.md` gains **Shell Family**, **Terminal Visibility**,
  **CommandTool**, **ProcessTool**, **TerminalTool** glossary entries;
  the **Terminal Visibility** entry records the structural-vs-switch
  diagnostic so the next engineer does not propose merging
  `WinptyConsoleWindowBackend` into `WinptyHiddenBackend` "to reduce classes".
- `TerminalSession.execute(command, timeout)` is deleted. Its body —
  in-line read/prompt-detect cycle + timeout/busy/waiting-input/ended
  XML production + history recording — moves into `CommandTool.execute`
  as `Session.submit` + `poll_until_settled` +
  `Session.apply_outcome(PollResult)` + `Session.record_history(...)`.
  The existing `set_expected_state(...)` call sites in `CommandTool` /
  `ProcessTool` are **kept** (they write the `_expected_state` slot that
  `detect_interference` consumes on visible sessions via `TerminalTool`);
  `apply_outcome(PollResult)` is **added** alongside them, writing the
  three orthogonal busy/last_status/command_started slots per the
  outcome→state table above. `set_expected_state` is NOT removed.
  `PollOutcome.YIELDED` and `PollOutcome.LONG_RUNNING` become first-class
  XML outcomes (one `_format_*` each), with the existing status strings
  preserved — output disambiguation only, no new status strings.
  See Decision 7.
- The migration path for callers (`examples/bot_project/bot/service/core.py`
  and `pool_builder.py`) is to detect `shell_family` and `visibility` once
  at startup and construct the manager directly with the two-axis
  constructor.
- Test impact: unit tests that today assert "instance of
  `WindowsHiddenTerminalManager`" must be re-pointed at the two-axis
  constructor and the produced backend type. Unit tests of
  `CommandTool.execute`'s indistinguishable-from-Session-execute behaviour
  (status XML strings, exit/logout early drain, busy/timeout recovery)
  remain unchanged at the string level — only their call site moves from
  `Session.execute` to `CommandTool.execute`'s orchestration.
- No new domain term is added for the factory's capability table — it is
  implementation, not language.