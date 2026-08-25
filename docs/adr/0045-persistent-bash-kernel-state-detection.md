# Kernel terminal-state detection for the persistent bash pair

**Status**: Accepted
**Date**: 2026-08-25

## Context

The persistent bash pair (`bash`/`PersistentBashTool` with its `bash_input`/`BashInputTool`
companion, driven by `PersistentShellSession` in `_persistent_session.py`) runs each command
on a pexpect pty inside a paired START/END printf marker protocol. Its `_collect` read loop
decides when a command completes, when the session waits for terminal input, and when the
480 s deadline is the only exit. Up to now, both completion and wait detection judged the
terminal from the shape of output bytes. That guess fails in opposite directions on the two
platforms.

On macOS the pair hangs after the agent answers an ssh password. The login succeeds, ssh puts
the terminal into raw mode, and control never returns to the local marker wrapper, so the END
marker of the `bash_input` transaction can never appear. The remaining detector, a
content-shape whitelist for prompts, misses zsh `%` prompts, fish prompts, and unicode
prompts; nothing fires. The only exit is the 480 s deadline, which returns partial output
after eight minutes and SIGKILLs the whole session on the way out: dead shell, lost
`cd`/environment state, and a transaction that took eight minutes to report "an interactive
shell is now active".

On Linux the defect runs the other way. A STRONG prompt-shape trigger fires without any
kernel gating, and its false-positive direction is real: slow-command output lines that
happen to end in `#` or `$` can be mistaken for a shell prompt, closing a still-running
command early.

The kernel already knows what the output bytes only approximate. Two signals are readable on
the pty master, on both macOS and Linux, with ordinary syscalls:

- the termios `ICANON` bit: canonical (line-buffered) mode versus raw mode on the terminal
  slave;
- `tcgetpgrp` on the master compared with the shell's own process group: which process group
  owns the terminal foreground.

The two signals were validated empirically on macOS and Linux (arm64) with real pty probes
across idle-shell, password-prompt, raw-remote-session, long-task, pipeline, and
nested-shell states.

## Decision

Detect interactive state from kernel terminal facts, not output shapes.

A single probe, `PersistentShellSession._terminal_state()`, reads both signals and feeds the
pure classifier `_classify_terminal_state(icanon_on, shell_owns_foreground)`, yielding four
states (`_TerminalSignal`):

| State | ICANON | Foreground group | Meaning |
|-------|--------|------------------|---------|
| `SHELL_READLINE` | off | shell | shell prompt with readline active |
| `SHELL_CANONICAL` | on | shell | builtin execution or builtin `read` |
| `CHILD_RAW` | off | child | interactive takeover: remote login, REPL, TUI |
| `CHILD_CANONICAL` | on | child | ordinary child command reading stdin |

The probe imports `termios` inside the method (the module is imported on Windows, where
`termios` does not exist), never raises (any `OSError`, `ImportError`, or `AttributeError`,
including the reap/close race, maps to `None`, meaning evidence absent), and costs
microseconds. This primitive landed in commit 21c734a0; the read-loop integration is the
rest of the design.

`_collect` consumes the matrix as follows, in priority order:

1. **The END marker stays the absolute first completion signal.** Kernel state decides waits
   and abnormal completions; it never overrides a well-formed marker pair.
2. **The PS1-token abnormal-completion layer hardens.** It fires only when the shell owns
   the foreground in readline mode (`SHELL_READLINE`), or when the probe is unavailable.
   While a builtin executes or a builtin `read` blocks (`SHELL_CANONICAL`), or while a child
   owns the foreground, a PS1 token in the output is printed text or stale noise, not
   evidence that the shell reclaimed its prompt.
3. **A new interactive-takeover exit covers `CHILD_RAW`.** When a raw-mode child owns the
   foreground, the output has been quiet for 0.25 s, two consecutive 25 ms poll iterations
   agree on the state, and the buffer is non-empty, the loop returns partial output with an
   interactive-shell `[hint: ...]` advisory and keeps the transaction answerable: the
   session enters WAITING with the pending marker preserved and the process alive, so
   further `bash` commands pass through into the interactive session and `bash_input` can
   answer it.
4. **WAITING kind comes from the kernel signal.** After a Linux `/proc` stdin-wait probe
   hit, the wait is shell-kind (bash may pass through) when the kernel reports `CHILD_RAW`,
   and prompt-kind (the guard stays up; answer via `bash_input`) otherwise.

The keyword layer and the weak prompt-shape layer remain, demoted to fallbacks for the
states the kernel matrix structurally cannot see: canonical prompts (a canonical stdin
reader and a long task look identical to the kernel) and builtin reads where the shell
itself owns the foreground. Both still pass the quiet-window gate.

Two boundaries hold the design together:

- **Single foreground source.** Every new foreground-group check flows through
  `_terminal_state()`; no second `tcgetpgrp`/tpgid read path may appear. The existing
  `/proc` `foreground_pgid` helper stays internal to the Linux probe.
- **Silence is never settlement.** A silent foreground command waits for its marker or the
  deadline. The takeover exit requires positive kernel evidence (a raw-mode child owning the
  foreground), never mere quiet.

## Consequences

- Canonical silent stdin readers remain indistinguishable from long tasks on macOS: the
  kernel reports `CHILD_CANONICAL` in both cases, and only the Linux `/proc` syscall scan
  separates them. Unchanged from before.
- A raw channel cannot distinguish "remote interactive session" from "remote running a long
  command": `ssh host` at its prompt and `ssh host "long-cmd"` both present `CHILD_RAW` with
  quiet output. A human at a real terminal has the same blind spot and resolves it by typing
  ahead; the agent does the same, guided by the hint advisory. Accepted.
- Timeout still SIGKILLs the session. The deadline remains the final backstop and its
  semantics are untouched.
- Raw non-shell programs (vim, less) are classified shell-kind, and `bash` passes through
  into them. On Linux this turns a prompt-kind rejection into passthrough: the command lands
  in the program's input buffer and is recoverable (e.g. vim's `u`). On macOS it replaces
  the eight-minute hang with an immediate takeover return, and the hint tells the agent to
  drive full-screen programs with `bash_input` keystrokes instead. Strictly better on both
  platforms, at the cost of one wasted passthrough keystroke.
- `bash_input("^C")` under takeover is byte-forwarding: `\x03` goes to whichever program
  owns the terminal, and that program interprets it. A `killpg`-SIGINT alternative was
  considered and rejected as out of scope for this round; it would widen the interrupt
  contract of the pair beyond the approved design.
- Non-readline fallback shells (the dash-class rare path with no bash on the platform) sit
  permanently in `SHELL_CANONICAL` at their prompt, so they lose the PS1
  abnormal-completion early return and fall back to the deadline. Accepted for a path that
  is rare by construction.
