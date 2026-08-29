"""Persistent PTY bash tools with a deterministic marker protocol.

``PersistentBashTool`` (registered name ``bash``) runs commands in
long-lived interactive shells — cwd, environment variables, and
background processes persist across calls.  ``BashInputTool``
(``bash_input``) answers commands that block reading stdin, or
interrupts them with the ``^C`` convention.

Session ownership lives in :class:`PersistentShellManager`
(``_persistent_session.py``) — the terminal-trio ``BaseTerminalManager``
shape applied to the persistent pair: ONE shell PER CONVERSATION keyed
by the routing session_id (``_current_session_id`` — note ``CommandTool``
does NOT route by it: the terminal trio always targets the manager's
shared default tab), lazily materialized, LRU-bounded.  The tools
are stateless routing shells: ``execute`` resolves the caller's shell
from the manager and delegates.  With no routing context (benchmark /
direct construction) every call shares the ``__default__`` shell — the
single-session contract the terminal-bench preset wires.

Protocol details (paired markers, phase machine, OR-fused stdin-wait
evidence, deadline-bounded silence) live in ``_persistent_session.py``.
POSIX-only (pexpect). This pair is the production fallback bash when a
pool has no terminal manager (terminal trio first, persistent shell
below it)::

    bash = PersistentBashTool(initial_cwd=workspace_root)
    bash_input = BashInputTool(bash.manager)
"""

from __future__ import annotations

import sys
from collections.abc import Callable
from typing import Any

from modex_agent.core.tool_manager import Tool, ToolManager
from modex_agent.tools.terminal._persistent_session import (
    PersistentShellManager,
    PersistentShellSession,
    PersistentShellUnsupportedError,
)

__all__ = [
    "BashInputTool",
    "PersistentBashTool",
    "PersistentShellManager",
    "PersistentShellUnsupportedError",
    "ensure_input_companion",
    "persistent_bash_supported",
]


def persistent_bash_supported() -> bool:
    """True when this host can spawn a POSIX pty shell (pexpect)."""
    return sys.platform != "win32"


def _routed_session(manager: PersistentShellManager) -> PersistentShellSession:
    """The shell for the CURRENT caller's conversation (default shell
    without a routing context)."""
    from modex_agent.runtime.env_context import _current_session_id

    return manager.session_for(_current_session_id.get())


def ensure_input_companion(
    manager: ToolManager,
    bash_tool: Tool | None,
    *,
    tool_transform: Callable[[Tool], Tool] | None = None,
) -> None:
    """Register the ``bash_input`` companion when *bash_tool* is a
    :class:`PersistentBashTool`.

    The pair is structural: a persistent shell without its stdin-answer
    tool deadlocks on interactive prompts (commands have no default
    timeout, so the shell stays blocked on the pending command). The
    companion registers under its own name directly into *manager* —
    never via roster/preset expansion — and shares *bash_tool*'s shell
    MANAGER so both tools route to the same conversation's shell.

    A pre-registered ``bash_input`` bound to a DIFFERENT manager is
    replaced: it belongs to a swapped-out bash (e.g. a roster swap that
    unregistered ``bash`` but missed its companion) and would answer into
    shells that never run. A same-manager companion is left untouched
    (idempotent).

    No-ops: ``bash_tool=None`` (no bash in the roster), and any
    non-persistent bash — terminal-manager ``CommandTool`` (its stdin
    path is process write) and the POSIX-less ``SubprocessTool`` fallback.
    """
    if not isinstance(bash_tool, PersistentBashTool):
        return
    existing = manager.get_tool("bash_input")
    if existing is not None:
        if isinstance(existing, BashInputTool) and existing._manager is bash_tool._manager:
            return
        manager.unregister("bash_input")
    companion: Tool = BashInputTool(manager=bash_tool._manager)
    manager.register(tool_transform(companion) if tool_transform is not None else companion)


class PersistentBashTool(Tool):
    """Execute commands in persistent interactive bash shells (stateful).

    Stateless routing shell: the caller's conversation session_id
    selects one of the manager's per-conversation shells. Safety checks
    (dangerous-command blocking) are not in the tool layer; they are
    handled at the ToolNode level via the approval system, same as
    SubprocessTool.
    """

    def __init__(
        self,
        initial_cwd: str | None = None,
        timeout_seconds: int | None = 480,
        max_output_chars: int | None = 16_000,
        manager: PersistentShellManager | None = None,
        max_sessions: int = 8,
    ) -> None:
        super().__init__()
        self._manager = manager if manager is not None else PersistentShellManager(
            initial_cwd=initial_cwd,
            timeout_seconds=timeout_seconds,
            max_output_chars=max_output_chars,
            max_sessions=max_sessions,
        )

    @property
    def manager(self) -> PersistentShellManager:
        """The pool-level shell registry — the pair's shared routing base."""
        return self._manager

    @property
    def session(self) -> PersistentShellSession:
        """The DEFAULT shell (no routing context) — attribute reads and
        single-session benchmark use; conversation routing goes through
        the manager."""
        return self._manager.session_for(None)

    @property
    def name(self) -> str:
        return "bash"

    @property
    def description(self) -> str:
        timeout_line = ""
        if self._manager.timeout_seconds is not None:
            timeout_line = (
                f"* Each command has a {self._manager.timeout_seconds}s timeout. On timeout the "
                "command is killed, the shell is reset (cwd and environment variables are NOT "
                "preserved), and partial output is returned.\n"
            )
        clip_line = ""
        if self._manager.max_output_chars is not None:
            clip_line = (
                f"* Output longer than {self._manager.max_output_chars} characters is truncated "
                "with the head and tail kept and an explicit elision marker.\n"
            )
        return (
            "Run commands in a persistent bash shell.\n"
            "* The shell is stateful: the current directory, environment variables, and "
            "background processes persist across calls.\n"
            f"{timeout_line}{clip_line}"
            "* stdout and stderr are interleaved in the returned output.\n"
            "* A command with no output returns `[no output]`.\n"
            "* A trailing `[exit code: N]` line marks a failed (non-zero exit) command.\n"
            "* When a command blocks reading input (a confirmation or password prompt), the "
            "call returns the output printed so far plus a trailing `[hint: ...]` line.\n"
            "Never let a needed command hit the timeout — background it and poll:\n"
            "* Long job (build/train/test): `python train.py > train.log 2>&1 &`, then check\n"
            "  progress with `tail -n 20 train.log` (bounded reads only — `tail -f` never "
            "returns).\n"
            "* Service that must stay up (servers/daemons; a timeout or session reset kills\n"
            "  plain background jobs): `setsid nohup python -m http.server 8000 > server.log 2>&1 &`\n"
            "* Wait until a service is ready, bounded: `for i in $(seq 1 30); do\n"
            "  curl -sf localhost:8000/ >/dev/null && break; sleep 1; done`\n"
            "* Check background work through its log or a client call (`curl`, `ps -p <pid>`) —\n"
            "  never by re-running it in the foreground.\n"
            "* Memory is a hard limit: combined allocations of concurrent commands\n"
            "  must stay well under physical RAM — exceeding it gets the whole\n"
            "  process OOM-killed and ALL work is lost. For CPU-bound batches, keep\n"
            "  parallelism near the core count, e.g.\n"
            "  `for f in *; do cmd $f & (( ++n % $(nproc) == 0 )) && wait; done; wait`\n"
            "  (IO-bound tasks may sensibly run more).\n"
            "* When an interactive program takes over the terminal (a remote login, a\n"
            "  REPL, a pager), the call returns with a trailing [hint: ...]\n"
            "  line explaining who appears to own the terminal: for a remote shell or REPL keep\n"
            "  issuing bash commands — they execute inside that session;\n"
            "  for a full-screen program (a pager or editor) answer or quit it with\n"
            "  a simple bash_input line such as 'q' (Enter-submitted; full key\n"
            "  semantics are not offered)."
        )

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "command": {
                    "type": "string",
                    "description": "The bash command to run. Relative paths are preferred.",
                },
            },
            "required": ["command"],
        }

    async def execute(self, command: str = "", **kwargs: object) -> str:
        if not command.strip():
            return "[Error] command must be a non-empty string"
        return await _routed_session(self._manager).run_command(command)

    async def close(self) -> None:
        """Terminate every pooled shell (call at shutdown)."""
        await self._manager.close_all()


class BashInputTool(Tool):
    """Send one stdin line to the caller's persistent bash shell."""

    def __init__(self, manager: PersistentShellManager) -> None:
        super().__init__()
        self._manager = manager

    @property
    def manager(self) -> PersistentShellManager:
        """The shared shell registry (identity for companion pairing)."""
        return self._manager

    @property
    def name(self) -> str:
        return "bash_input"

    @property
    def description(self) -> str:
        return (
            "Answer an interactive prompt from the persistent bash shell.\n"
            "Use this ONLY when a previous bash call stopped at a prompt that needs "
            "terminal input the command itself cannot supply — a yes/no confirmation, "
            "a password request, or a question the command is asking. The bash result "
            "tells you: it ends with a trailing `[hint: ...]` line.\n"
            "Do NOT use it to run commands, chain extra commands, or 'send' anything "
            "when the shell is not asking — the line goes to whatever program is "
            "reading the terminal, not to a fresh shell.\n"
            "Send '^C' (or 'ctrl+c') to interrupt a stuck foreground command; the "
            "shell recovers to a clean prompt.\n"
            "Under a terminal takeover (remote login / full-screen program), '^C' is "
            "forwarded as a byte to the program that owns the terminal — that "
            "program decides what the byte means (a local shell still treats it as "
            "SIGINT).\n"
            "Line-oriented: passwords, confirmations, and single-line answers are its "
            "domain; full-screen TUI programs (vim, less) need key semantics it does "
            "not offer.\n"
            "Returns the continued output of the command once it completes — a new "
            "trailing `[hint: ...]` line means it is asking again."
        )

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "line": {
                    "type": "string",
                    "description": "One line of text to send to the shell's stdin.",
                },
            },
            "required": ["line"],
        }

    async def execute(self, line: str = "", **kwargs: object) -> str:
        # An empty line is a legitimate keypress (Enter) — allowed.
        return await _routed_session(self._manager).send_input(line)
