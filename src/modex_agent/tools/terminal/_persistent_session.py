"""Persistent bash PTY session driven by a deterministic marker protocol.

Internal companion of :mod:`modex_agent.tools.terminal.persistent_bash`.
The TB (terminal-bench) preset needs blocking, benchmark-style execution
against ONE long-lived interactive bash.  This module drives
``pexpect.spawn`` directly and deliberately does NOT reuse the
CommandTool / TerminalSession / TerminalBackend stack — that stack is
built around prompt-stability heuristics, yield windows, and guard
states for interactive UI terminals, the wrong semantics for benchmark
blocking execution.

Each command is wrapped into ONE physical line

    eval -- $'<escaped command>'; echo "__DONE_<id>__=$?"

so the shell emits a unique ``__DONE_<id>__=<exit code>`` line exactly
when the command finishes.  The single physical line matters twice:

* an interactive bash prints PS2 for embedded newlines, which would leak
  prompt noise into results;
* a stdin-reading command (``read``) would swallow a marker sent as a
  second physical line — with the wrapper on one line, ``read`` blocks
  until the ``bash_input`` tool supplies a line instead.

Echo suppression is pexpect ``spawn(echo=False)`` — the pty ECHO flag is
off from the start.  ``PS1`` is set to a controlled token (see below),
``PS2`` cleared, and history expansion disabled at startup so prompt
text and ``!`` expansion never pollute results.

Kernel terminal-state and stdin-wait detection:

* **Terminal-state matrix** (POSIX): the ``termios`` ``ICANON`` bit is
  combined with whether ``tcgetpgrp`` names the shell's process group.
  Shell ownership in raw mode is ``SHELL_READLINE``; shell ownership in
  canonical mode is ``SHELL_CANONICAL``; child ownership in raw mode is
  ``CHILD_RAW``; child ownership in canonical mode is ``CHILD_CANONICAL``.
* **Read-loop ordering**: the END marker has absolute priority. A controlled
  PS1 token closes an otherwise unmarked transaction only under
  ``SHELL_READLINE``, or when terminal-state evidence is unavailable.
  ``CHILD_RAW`` identifies interactive takeover after 0.25 seconds of quiet,
  two consecutive 25 ms read-loop observations, and a non-empty output buffer; it
  returns a shell-kind WAITING result that accepts command passthrough.
* **Canonical waits and fallbacks**: on Linux, every
  ``_STDIN_PROBE_INTERVAL_S`` the ``/proc`` probe remains the authority for
  foreground groups blocked reading terminal input. A probe hit classifies
  WAITING from the terminal signal: ``CHILD_RAW`` is shell-kind and every
  other state is prompt-kind. Keyword detection and the weak prompt-shape
  layer remain fallbacks for canonical interactive states and builtin reads;
  the weak layer requires probe absence or positive probe confirmation.
  Stale WAITING transactions use the same kernel signal and fall back to the
  trailing shell shape only when terminal-state evidence is unavailable.

Output silence alone is NEVER stdin-wait evidence — a silent transaction
waits for detector evidence or the command deadline. A WAITING result returns
partial output plus the kind-matched advisory: ``bash_input`` answers a
prompt-kind wait, while shell-kind waits accept command passthrough.

Timeout semantics (DSSH-aligned): every command has a wall-clock
deadline (default ``_DEFAULT_TIMEOUT_SECONDS``; ``None`` disables).  On
expiry the ENTIRE process session — the shell and every descendant
group — is SIGKILLed, the PTY is closed, and the next call lazily
spawns a fresh shell.  The result carries the partial output plus an
explicit reset notice (cwd/env were NOT preserved).  External
cancellation gets the same cleanup so a cancelled call never leaks a
zombie shell; the cancellation itself propagates.

Output handling: per-command output accumulates in a tail-keeping
rolling buffer (``_SCROLLBACK_MAX_CHARS``/``_SCROLLBACK_MAX_LINES``).
The response applies ``max_output_chars`` when set — head+tail with an
explicit elision marker via ``render_overflow_text`` — or returns the
full text when ``None`` so the framework's overflow interceptor owns
truncation.  A length-zero result becomes ``[no output]``: the content
part is never empty, so every trailing advisory (exit-code marker,
hint, notice) joins on a newline — never glued, never standalone.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import os
import re
import shutil
import sys
import uuid
from dataclasses import dataclass, replace
from enum import Enum, StrEnum
from pathlib import Path
from time import monotonic
from typing import Any

from modex_agent.runtime.env_context import _modex_env
from modex_agent.tools.overflow.truncate import render_overflow_text, split_head_tail
from modex_agent.tools.terminal._foreground_probe import (
    foreground_pgid,
    is_stdin_waiting,
    parse_proc_stat,
    stdin_probe_available,
)
from modex_agent.tools.terminal.env import build_full_env
from modex_agent.tools.terminal.prompt import _strip_ansi_and_da1, is_waiting_for_input
from modex_agent.tools.terminal.pty_keys import CTRL_C, ENTER_KEY
from modex_agent.tools.terminal.types import (
    ShellFamily,
    _family_from_path,
    detect_platform_shell,
)

logger = logging.getLogger(__name__)

_POLL_INTERVAL_S = 0.025
_READ_SIZE = 65_536
_SETTLE_QUIET_S = 0.25
_SETTLE_MAX_S = 1.5
# Kernel-probe cadence: how often the session asks /proc whether the
# foreground group blocks reading stdin.  Stdin waits are persistent
# states, so interval polling cannot miss one — it only delays the
# verdict by up to one interval.
_STDIN_PROBE_INTERVAL_S = 3.0
# Stdin-wait evidence quiet window: both the probe verdict and the content
# detectors require this much output silence first — a still-streaming
# command must not be truncated by an early verdict.
_PROMPT_SETTLE_S = 0.25
# Per-command output rolling buffer (tail-keeping; the marker always
# lands at the end, so completion survives any head dropping).
_SCROLLBACK_MAX_CHARS = 4_000_000
_SCROLLBACK_MAX_LINES = 10_000
_DEFAULT_TIMEOUT_SECONDS = 480
_DEFAULT_MAX_OUTPUT_CHARS = 16_000
_PS1_TOKEN = "__MODEX_PS1__"
_SIGKILL = 9  # numeric: signal.SIGKILL is absent from the Windows signal module

_STDIN_PROBE_ERROR = "[Error] no bash command is waiting for input"
_BUSY_WAITING_MESSAGE = (
    "[Error] the shell is stopped at a prompt from a previous command that needs "
    "interactive input — answer it with bash_input(line), or interrupt it with "
    "bash_input(^C), before running a new command"
)
_BUSY_RUNNING_MESSAGE = (
    "[Error] the previous command is still executing — wait for it to finish; "
    "only bash_input(^C) is accepted while it runs"
)
_STDIN_HINT = (
    "[hint: the command stopped at a prompt waiting for interactive input "
    "(password, yes/no confirmation, or a question) — answer it with "
    "bash_input(line), or interrupt it with bash_input(^C)]"
)
_SHELL_HINT = (
    "[hint: an interactive shell is active (remote login / REPL at its own prompt) — "
    "run further commands with the bash tool directly; they execute inside that "
    "shell; answer prompts or send keys (e.g. 'q' for a pager) with bash_input; '^C' "
    "is forwarded as a byte to the program that owns the terminal; log out (exit) to "
    "return to the local shell]"
)
_SHELL_EXITED_NOTICE = "[shell exited — it will restart fresh on the next call]"
_NO_OUTPUT = "[no output]"
_TRUNCATED_HEAD_NOTICE = (
    "[... earlier output of this command was dropped (output budget exceeded) ...]"
)
_TIMEOUT_MESSAGE = (
    "Your command timed out after {seconds} seconds or may have failed (e.g. OOM).\n"
    "Below is the partial output captured before termination:\n"
    "{partial}\n"
    "The persistent bash shell was reset: the working directory and environment "
    "variables were NOT preserved. Run long-lived commands in the background "
    "(e.g. 'long_command &')."
)


_CSI_PATTERN = re.compile(r"\x1b\[[0-9;?]*[A-Za-z]")
_OSC_PATTERN = re.compile(r"\x1b\][^\x07]*\x07|\x1b\][^\x1b]*\x1b\\")
_ESC_CHAR_PATTERN = re.compile(r"\x1b[ -/]*[@-~]")

# Marker-shaped lines NOT belonging to the current transaction: residue of
# an abandoned/foreign transaction (both protocols' shapes) or text the
# command prints itself.  Stripped from model-facing results.
_FOREIGN_MARKER_RE = re.compile(
    r"^\s*__(?:MODEX_(?:START|END)_[0-9a-f]{8}(?:__|:\d+)|DONE_[0-9a-f]{8}__=\d+)\s*$\n?",
    re.MULTILINE,
)

# Prompt shapes a foreign foreground program may print while OUR prompt is
# the controlled __MODEX_PS1__ token: bare prompts ("$", "#", ">>>"),
# user@host:path forms ("root@aliyun:~#"), bracketed forms
# ("[root@host ~]#"), and named CLI prompts ("mysql>").
_FOREIGN_PROMPT_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(r"^[#%>$]{1,3}\s*$"),
    # user@host:path$ / # forms — "%"/">" endings excluded here: a bare
    # "%"-suffixed line ("progress 42%") is ambiguous with progress
    # output; lone "%"/">" prompts are covered by the pattern above.
    re.compile(r"^[\w.@~:/\\ -]{1,64}[#$]\s*$"),
    re.compile(r"^[\w.@~:/ -]*\[[^\]\n]{1,64}\][#$%>]\s*$"),
    re.compile(r"^[\w.-]{1,32}>\s*$"),
)
# Generic input-prompt suffix (the shared detector's documented Layer 2,
# applied session-locally): "Continue?", "stuck?", "name:", "Enter choice)".
# "]" is deliberately excluded — bracketed log lines ("[OK]", "[WARNING]")
# are common command output and would false-positive.
_PROMPT_SUFFIX_CHARS: tuple[str, ...] = (":", "?", ")")

# \x03 bytes would be interpreted by readline (completion, SIGINT) and
# corrupt the wrapper line.
_CONTROL_ESCAPES = {chr(code): f"\\{code:03o}" for code in (*range(0x20), 0x7F)}
_CONTROL_TABLE = str.maketrans(_CONTROL_ESCAPES)

_INTERRUPT_LINES = frozenset({"^c", "ctrl+c", CTRL_C})


class _Phase(Enum):
    """Session transaction phase — the guard state shared by both tools."""

    IDLE = "idle"
    RUNNING = "running"
    WAITING = "waiting"


class _TerminalSignal(StrEnum):
    SHELL_READLINE = "shell_readline"
    SHELL_CANONICAL = "shell_canonical"
    CHILD_RAW = "child_raw"
    CHILD_CANONICAL = "child_canonical"


def _classify_terminal_state(icanon_on: bool, shell_owns_foreground: bool) -> _TerminalSignal:
    if shell_owns_foreground:
        return _TerminalSignal.SHELL_CANONICAL if icanon_on else _TerminalSignal.SHELL_READLINE
    return _TerminalSignal.CHILD_CANONICAL if icanon_on else _TerminalSignal.CHILD_RAW


def _is_interrupt_line(line: str) -> bool:
    return line.strip().lower() in _INTERRUPT_LINES


def _hint_for(waiting_shell: bool) -> str:
    """The advisory line matching the WAITING kind: shell-kind waits accept
    bash passthrough; prompt-kind waits eat wrappers (bash_input only)."""
    return _SHELL_HINT if waiting_shell else _STDIN_HINT


def _last_painted_line(text: str) -> str:
    """The most-recently-painted segment of the last non-empty line."""
    if not text:
        return ""
    last = _strip_ansi_and_da1(text.rstrip()).split("\n")[-1]
    if "\r" in last:
        last = last.rsplit("\r", 1)[-1]
    return last.strip()


def _looks_like_shell_prompt(text: str) -> bool:
    """SHELL-kind evidence: the trailing line looks like an interactive
    shell's own prompt (``$``, ``#``, ``%``, ``>`` glyph endings,
    ``user@host:~#``, bracketed ``[root@host ~]#``, ``mysql>`` forms).
    Such a waiter EXECUTES submitted lines — bash wrappers pass through
    and close via their own markers. ANSI escapes are stripped first:
    remote readlines emit bracketed-paste enables and SGR runs INSIDE
    the prompt line, and those bytes would blind the match.
    """
    last = _last_painted_line(text)
    if not last or _PS1_TOKEN in last:
        return False
    return any(pattern.match(last) is not None for pattern in _FOREIGN_PROMPT_PATTERNS)


def _looks_like_foreign_prompt(text: str) -> bool:
    """ANY-prompt evidence (shell OR input prompt): the trailing line
    looks like some foreign prompt — the shell shapes above, or a
    generic input-prompt suffix (``:``, ``?``, ``)`` — ``Continue?``,
    ``name:``, ``Enter choice)``). Callers that must distinguish the
    kinds (guard routing, hint choice) use
    :func:`_looks_like_shell_prompt` for the shell subset.
    """
    last = _last_painted_line(text)
    if not last or _PS1_TOKEN in last:
        return False
    if last.endswith(_PROMPT_SUFFIX_CHARS):
        return True
    return _looks_like_shell_prompt(text)


def _sanitize(text: str) -> str:
    """Strip terminal escapes and normalize PTY line endings for model output."""
    text = _OSC_PATTERN.sub("", text)
    text = _CSI_PATTERN.sub("", text)
    text = _ESC_CHAR_PATTERN.sub("", text)
    text = text.replace("\r\n", "\n")
    # Standalone \r repaint: per logical line keep only the text after the last \r.
    return "\n".join(line.rsplit("\r", 1)[-1] for line in text.split("\n"))


def _strip_ps1(text: str) -> str:
    """Remove the controlled prompt token from model-facing output."""
    return text.replace(_PS1_TOKEN, "")


def _bash_ansi_c_quote(value: str) -> str:
    """Escape *value* as a bash ``$'...'`` literal that stays one physical line."""
    escaped = value.replace("\\", "\\\\").replace("'", "\\'")
    return f"$'{escaped.translate(_CONTROL_TABLE)}'"


def _with_notice(output: str, notice: str) -> str:
    return f"{output}\n{notice}"


def _with_hint(output: str, hint: str) -> str:
    """Attach a hint advisory after a blank separator line (never glued)."""
    return f"{output}\n\n{hint}"


def _format_result(raw: str, exit_code: int | None, max_output_chars: int | None) -> str:
    """Sanitize, strip the PS1 token, clip per the overflow contract, add exit code.

    A LENGTH-ZERO result (after the single trailing-newline strip — the
    leading/trailing whitespace of real output is meaningful and preserved)
    becomes ``[no output]``, so the content part is never empty and every
    trailing advisory (exit-code marker, hint, notice) joins on a newline.
    """
    text = _strip_ps1(_sanitize(raw))
    if text.endswith("\n"):
        text = text[:-1]
    if max_output_chars is not None and len(text) > max_output_chars:
        head_chars, tail_chars = split_head_tail(max_output_chars)
        text = render_overflow_text(
            text,
            head_chars=head_chars,
            tail_chars=tail_chars,
            full_output_path=None,
        )
    if not text:
        text = _NO_OUTPUT
    if exit_code is not None and exit_code != 0:
        return f"{text}\n[exit code: {exit_code}]"
    return text


class _RollingBuffer:
    """Tail-keeping bounded text buffer.

    The completion marker is always the LAST line the shell prints for a
    command, so dropping the head never loses completion — the
    ``dropped_head`` flag drives the truncation notice instead.
    """

    def __init__(
        self,
        max_chars: int = _SCROLLBACK_MAX_CHARS,
        max_lines: int = _SCROLLBACK_MAX_LINES,
    ) -> None:
        self._max_chars = max_chars
        self._max_lines = max_lines
        self._text = ""
        self._dropped = False

    def append(self, chunk: str) -> None:
        if not chunk:
            return
        text = self._text + chunk
        lines = text.split("\n")
        if len(lines) > self._max_lines:
            text = "\n".join(lines[-self._max_lines :])
            self._dropped = True
        if len(text) > self._max_chars:
            text = text[-self._max_chars :]
            self._dropped = True
        self._text = text

    @property
    def text(self) -> str:
        return self._text

    @property
    def dropped_head(self) -> bool:
        return self._dropped


@dataclass(frozen=True)
class _PendingWait:
    """A command in flight: its marker pair, absolute deadline, budget.

    ``deadline``/``timeout_seconds`` are ``None`` when no per-command
    timeout is configured.
    """

    end_re: re.Pattern[str]
    start_token: str
    deadline: float | None
    timeout_seconds: int | None = None


class PersistentShellUnsupportedError(RuntimeError):
    """This host cannot provide the POSIX pty the persistent shell needs."""


_UNSUPPORTED_HOST_MESSAGE = (
    "PersistentBashTool requires a POSIX pty (pexpect); not available on "
    "this host — use the SubprocessTool fallback"
)

# Bash-specific spawn flags: they make the marker protocol deterministic
# (no profile/rc noise, interactive job control). Non-bash shells (zsh/sh)
# reject some of them, so those spawn with plain args.
_BASH_SPAWN_ARGS = ["--noprofile", "--norc", "-i"]


def _resolve_shell(shell: str | None) -> tuple[str, bool]:
    """Resolve ``(shell_path, is_bash)`` for the persistent session spawn.

    An explicit *shell* wins as-is (bash-ness inferred from its path).
    Otherwise ``detect_platform_shell()`` supplies the host shell: a bash
    family result is used directly; zsh/sh results defer to a
    ``shutil.which("bash")`` hit first because the marker protocol and the
    spawn flags are bash-specific. Detection failure or a Windows-family
    result falls back to ``/bin/bash``.
    """
    if shell is not None:
        return shell, _family_from_path(shell) is ShellFamily.BASH
    info = detect_platform_shell()
    if info is None or info.family not in (ShellFamily.BASH, ShellFamily.ZSH, ShellFamily.SH):
        return "/bin/bash", True
    if info.family is ShellFamily.BASH:
        return info.path, True
    bash_path = shutil.which("bash")
    if bash_path is not None:
        return bash_path, True
    return info.path, False


def _kill_process_session(session_pid: int) -> None:
    """SIGKILL every process in the PTY session (shell + descendant groups).

    On Linux the /proc scan finds members by session id — the only
    reliable way to reach foreground process groups that job control
    moved off the shell's own group. Elsewhere (macOS dev hosts) the
    session leader's process group is the best available target.
    Failures are swallowed: teardown is best-effort and idempotent.

    CONTRACT — descendants that LEFT the session are deliberately NOT
    chased. The scan matches ``stat.session == session_pid`` and nothing
    else, so a process started via ``setsid`` (its own session id, e.g.
    ``setsid nohup python -m http.server 8000 &``) survives this kill.
    That escape hatch is what lets agent-started services outlive a
    per-command timeout reset or a session teardown, while plain
    background jobs (``cmd &``) stay in the session and die with the
    shell. The bash tool description teaches the ``setsid`` pattern on
    the strength of this contract — do not widen the scan to hunt
    processes that deliberately left the session.
    """
    killed_any = False
    proc_dir = Path("/proc")
    if proc_dir.is_dir():
        try:
            entries = os.listdir(proc_dir)
        except OSError:
            entries = []
        for entry in entries:
            if not entry.isdigit():
                continue
            try:
                stat = parse_proc_stat((proc_dir / entry / "stat").read_text(encoding="utf-8"))
            except OSError:
                continue
            if stat is None or stat.session != session_pid:
                continue
            try:
                os.kill(stat.pid, _SIGKILL)
                killed_any = True
            except OSError:
                continue
    if not killed_any:
        # Negative pid = process group (POSIX); never reached on Windows.
        with contextlib.suppress(OSError):
            os.kill(-session_pid, _SIGKILL)


class PersistentShellManager:
    """Pool-level registry of per-conversation persistent shells.

    The per-owner model mapped onto this codebase's manager shape
    (``BaseTerminalManager`` is the terminal-trio counterpart): one
    ``PersistentShellSession`` per routing key (the conversation's
    session_id), so conversations never share a PTY, cwd, environment, or
    transaction state. Sessions materialize lazily on first use, are
    touched LRU on every access, and the idlest is reaped (process session
    killed) beyond *max_sessions* — a reaped conversation simply gets a
    fresh shell on its next call. ``None`` routes to the shared
    ``__default__`` session (benchmark / direct-construction callers with
    no routing context).
    """

    _DEFAULT_KEY = "__default__"

    def __init__(
        self,
        initial_cwd: str | None = None,
        timeout_seconds: int | None = _DEFAULT_TIMEOUT_SECONDS,
        max_output_chars: int | None = _DEFAULT_MAX_OUTPUT_CHARS,
        max_sessions: int = 8,
        shell: str | None = None,
    ) -> None:
        self._initial_cwd = initial_cwd
        self._timeout_seconds = timeout_seconds
        self._max_output_chars = max_output_chars
        self._max_sessions = max_sessions
        self._shell = shell
        self._sessions: dict[str, PersistentShellSession] = {}

    @property
    def timeout_seconds(self) -> int | None:
        return self._timeout_seconds

    @property
    def max_output_chars(self) -> int | None:
        return self._max_output_chars

    @property
    def session_count(self) -> int:
        return len(self._sessions)

    def session_for(self, session_id: str | None) -> PersistentShellSession:
        """Return the shell for *session_id* (None → the default shell).

        Lazy materialization + LRU touch + over-limit eviction of the
        idlest shell. Order tracks recency of ACCESS, not creation.
        """
        key = session_id if session_id else self._DEFAULT_KEY
        session = self._sessions.get(key)
        if session is None:
            session = PersistentShellSession(
                initial_cwd=self._initial_cwd,
                timeout_seconds=self._timeout_seconds,
                max_output_chars=self._max_output_chars,
                shell=self._shell,
            )
            self._sessions[key] = session
            self._reap_beyond_limit()
        # dict preserves insertion order; re-inserting moves the key to
        # the end — the recency order the reaper consumes.
        self._sessions[key] = self._sessions.pop(key)
        return session

    def _reap_beyond_limit(self) -> None:
        while len(self._sessions) > self._max_sessions:
            key = next(iter(self._sessions))
            victim = self._sessions.pop(key)
            victim._terminate_session_sync()  # noqa: SLF001 — same-module collaboration

    async def close_all(self) -> None:
        """Terminate every pooled shell (shutdown seam — no PTY leaks)."""
        sessions = list(self._sessions.values())
        self._sessions.clear()
        for session in sessions:
            await session.close()


class PersistentShellSession:
    """One long-lived interactive shell PTY driven by the marker protocol.

    The shell resolves at construction (bash-first via
    ``detect_platform_shell()``; see :func:`_resolve_shell`) and spawns
    lazily on the first command. All blocking pexpect calls (spawn, send,
    read_nonblocking, close) run through ``loop.run_in_executor`` — the
    same offload pattern as ``backends/pexpect_pty.py`` — so a full PTY
    buffer never stalls the event loop.  The shell spawns with the same
    env pipeline :class:`SubprocessTool` uses per command
    (``build_full_env`` + ``_modex_env`` overrides + ``NO_COLOR=1``);
    because the shell is persistent, the env is fixed at SPAWN time
    (first command) — later ``export`` commands mutate it naturally, but
    per-command env changes cannot reach an already-running shell.

    Commands run under a wall-clock deadline (``timeout_seconds``;
    ``None`` disables). On expiry — or on external cancellation — the
    whole process session is killed and the next call respawns a fresh
    shell (cwd/env reset).
    """

    def __init__(
        self,
        initial_cwd: str | None = None,
        timeout_seconds: int | None = _DEFAULT_TIMEOUT_SECONDS,
        max_output_chars: int | None = _DEFAULT_MAX_OUTPUT_CHARS,
        shell: str | None = None,
    ) -> None:
        self._initial_cwd = initial_cwd
        self._timeout_seconds = timeout_seconds
        self._max_output_chars = max_output_chars
        self._shell_path, self._is_bash_shell = _resolve_shell(shell)
        self._proc: Any = None  # pexpect.spawn handle (untyped third-party)
        self._pending: _PendingWait | None = None
        self._phase: _Phase = _Phase.IDLE
        self._waiting_shell = False
        self._wait_tail: str = ""
        self._call_lock = asyncio.Lock()

    @property
    def timeout_seconds(self) -> int | None:
        """Per-command wall-clock budget in seconds (None = no deadline)."""
        return self._timeout_seconds

    @property
    def max_output_chars(self) -> int | None:
        """Response clip budget (None = no internal clipping; the framework
        overflow interceptor owns truncation)."""
        return self._max_output_chars

    @property
    def shell_path(self) -> str:
        """Resolved shell executable this session spawns (lazily)."""
        return self._shell_path

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    async def run_command(self, command: str) -> str:
        """Run one command; completion is governed by the marker protocol.

        Rejected (with guidance) while a prompt-kind WAITING transaction
        is open — a wrapper submitted there is eaten by the foreground
        reader as input (the state-pollution failure mode). A SHELL-kind
        WAITING (remote login / REPL at its own prompt) passes through:
        the wrapper executes inside that shell and closes via its own
        markers. A stale WAITING whose command already ended self-heals.

        Serialized by the session's call lock — concurrent callers on the
        SAME session queue here (guard checks run under the lock); other
        sessions' locks are independent, so cross-session calls proceed
        in parallel on their own PTYs.
        """
        async with self._call_lock:
            return await self._run_command_locked(command)

    async def _run_command_locked(self, command: str) -> str:
        if self._phase is _Phase.WAITING:
            # The waiter may be an interactive SHELL (ssh remote prompt, a
            # REPL with a shell-glyph prompt): the wrapper executes THERE
            # and closes via its own markers — pass through. Genuine prompts
            # (password, confirmation) still eat wrappers; a stale wait
            # self-heals, and _heal_stale_wait reclassifies the kind from
            # fresh evidence before we decide.
            if (
                not self._waiting_shell
                and not await self._heal_stale_wait()
                and not self._waiting_shell
            ):
                return _BUSY_WAITING_MESSAGE
        elif self._phase is _Phase.RUNNING:
            return _BUSY_RUNNING_MESSAGE
        self._waiting_shell = False
        try:
            await self._ensure_started()
            # Discard output left by a previously timed-out / abandoned
            # command, reading until quiet so late residue cannot leak
            # into this command's window.
            await self._drain_stale()
            self._pending = None
            marker_id = uuid.uuid4().hex[:8]
            start_token = f"__MODEX_START_{marker_id}__"
            end_token = f"__MODEX_END_{marker_id}:"
            end_re = re.compile(re.escape(end_token) + r"(\d+)")
            wrapped = (
                f"printf '%s\\n' {_bash_ansi_c_quote(start_token)}; "
                f"eval -- {_bash_ansi_c_quote(command)}; "
                f"__modex_status=$?; "
                f"printf '%s%s\\n' {_bash_ansi_c_quote(end_token)} \"$__modex_status\""
            )
            self._phase = _Phase.RUNNING
            await self._send(wrapped + "\n")
            deadline = (
                monotonic() + self._timeout_seconds if self._timeout_seconds is not None else None
            )
            pending = _PendingWait(
                end_re=end_re,
                start_token=start_token,
                deadline=deadline,
                timeout_seconds=self._timeout_seconds,
            )
            return await self._collect(pending)
        except asyncio.CancelledError:
            self._terminate_session_sync()
            raise

    async def send_input(self, line: str) -> str:
        """Feed one stdin line to a stdin-waiting command and await its completion.

        ``^C`` / ``ctrl+c`` / ``\\x03`` (the terminal-trio interrupt
        convention) translate to the ``\\x03`` byte.  With the LOCAL
        shell in the foreground the terminal driver raises SIGINT: the
        foreground command dies and the wrapper's END marker still
        closes the transaction.  Under a raw-mode takeover (ISIG off)
        the byte is forwarded to the program that owns the terminal,
        and only that program decides its meaning.  Plain lines are
        accepted only while WAITING; an interrupt is the sole input
        accepted while RUNNING.  Serialized by the session's call lock
        like ``run_command``.
        """
        async with self._call_lock:
            if self._proc is None or not self._is_alive():
                return "[Error] no persistent bash shell is running — run a bash command first"
            interrupt = _is_interrupt_line(line)
            pending = self._pending
            if pending is None or self._phase is _Phase.IDLE:
                return _STDIN_PROBE_ERROR
            if self._phase is _Phase.RUNNING and not interrupt:
                return _BUSY_RUNNING_MESSAGE
            if self._timeout_seconds is not None:
                # The answer may unleash more work — give it a fresh deadline.
                pending = replace(pending, deadline=monotonic() + self._timeout_seconds)
                self._pending = pending
            self._phase = _Phase.RUNNING
            payload = CTRL_C if interrupt else line + ENTER_KEY
            try:
                await self._send(payload)
                return await self._collect(pending)
            except asyncio.CancelledError:
                self._terminate_session_sync()
                raise

    async def close(self) -> None:
        """Terminate the shell process session and release the PTY (shutdown seam)."""
        self._terminate_session_sync()

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    async def _ensure_started(self) -> None:
        if self._proc is not None and self._is_alive():
            return
        if sys.platform == "win32":
            raise PersistentShellUnsupportedError(_UNSUPPORTED_HOST_MESSAGE)
        self._terminate_session_sync()
        try:
            import pexpect
        except ImportError as exc:
            raise PersistentShellUnsupportedError(_UNSUPPORTED_HOST_MESSAGE) from exc

        # Env parity with SubprocessTool: the same full pipeline
        # (PAGER=cat, bundled-bin PATH, registry PATH, hook overrides) plus
        # NO_COLOR — read once at spawn; a persistent shell cannot take
        # per-command env changes (exports persist naturally instead).
        env = build_full_env(overrides=_modex_env.get())
        env["NO_COLOR"] = "1"

        spawn_args = _BASH_SPAWN_ARGS if self._is_bash_shell else []

        def _spawn() -> Any:
            proc = pexpect.spawn(
                self._shell_path,
                spawn_args,
                echo=False,
                dimensions=(50, 200),
                cwd=self._initial_cwd,
                env=env,
                encoding="utf-8",
                codec_errors="replace",
            )
            # Echo is off at the pty level from the start, so pexpect's 50 ms
            # pre-send delay (a password-echo race guard) is pure latency here.
            proc.delaybeforesend = None
            return proc

        loop = asyncio.get_running_loop()
        self._proc = await loop.run_in_executor(None, _spawn)
        # Controlled PS1: its appearance without a marker is the layer-2
        # "command returned abnormally" signal; PS2 cleared and history
        # expansion off (`set +H`, bash-only) keep results clean. The
        # settle drain absorbs the shell's initial banner.
        if self._is_bash_shell:
            await self._send(f"PS1='{_PS1_TOKEN} '; PS2=''; set +H\n")
        else:
            await self._send(f"PS1='{_PS1_TOKEN} '; PS2=''\n")
        await self._drain_until_quiet(_SETTLE_MAX_S)

    def _is_alive(self) -> bool:
        if self._proc is None:
            return False
        try:
            return bool(self._proc.isalive())
        except Exception:
            return False

    def _terminate_session_sync(self) -> None:
        """Kill the process session and close the PTY (idempotent, sync —
        safe inside cancellation cleanup)."""
        proc = self._proc
        self._proc = None
        self._pending = None
        self._waiting_shell = False
        self._wait_tail = ""
        self._phase = _Phase.IDLE
        if proc is None:
            return
        try:
            _kill_process_session(proc.pid)
        except Exception:
            logger.debug("persistent bash session kill failed", exc_info=True)
        try:
            proc.close(force=True)
        except Exception:
            logger.debug("persistent bash pty close failed", exc_info=True)

    # ------------------------------------------------------------------
    # PTY I/O (executor-offloaded)
    # ------------------------------------------------------------------

    async def _send(self, data: str) -> None:
        proc = self._proc
        if proc is None:
            raise RuntimeError("persistent bash shell not started")
        loop = asyncio.get_running_loop()
        await loop.run_in_executor(None, proc.send, data)

    async def _read_chunk(self) -> str | None:
        """Read for up to one poll interval: output, ``""`` when idle, ``None`` on EOF."""
        proc = self._proc
        if proc is None:
            return None
        import pexpect

        loop = asyncio.get_running_loop()
        try:
            return await loop.run_in_executor(
                None, proc.read_nonblocking, _READ_SIZE, _POLL_INTERVAL_S
            )
        except pexpect.exceptions.TIMEOUT:
            return ""
        except pexpect.exceptions.EOF:
            return None

    async def _drain_stale(self) -> None:
        """Discard unread residue left by a previous abandoned command.

        Fast path: an immediately-empty read means nothing is pending and
        the common case pays one poll interval.  When residue IS found,
        keep reading until quiet — a dying foreground process can keep
        emitting after the abandoned call returned, and that late output
        must not leak into the next command's window.
        """
        first = await self._read_chunk()
        if not first:
            return
        await self._drain_until_quiet(_SETTLE_MAX_S)

    async def _drain_until_quiet(self, max_seconds: float) -> None:
        """Read-and-discard until a quiet streak or *max_seconds* elapses."""
        deadline = monotonic() + max_seconds
        quiet = 0.0
        while monotonic() < deadline:
            chunk = await self._read_chunk()
            if chunk is None:
                return
            quiet = quiet + _POLL_INTERVAL_S if not chunk else 0.0
            if quiet >= _SETTLE_QUIET_S:
                return

    # ------------------------------------------------------------------
    # Terminal-state evidence
    # ------------------------------------------------------------------

    def _terminal_state(self) -> _TerminalSignal | None:
        """Classify terminal line mode and ownership, or return None without evidence.

        ICANON distinguishes canonical line mode from raw mode; comparing tcgetpgrp
        with the shell process group identifies whether the shell or a child owns the
        terminal. Missing platform support (ImportError), missing attributes, and
        process or descriptor races (OSError or termios.error) all return None.
        """
        if self._proc is None:
            return None
        try:
            import termios
        except ImportError:
            return None
        try:
            attrs = termios.tcgetattr(self._proc.child_fd)
            lflag = attrs[3]
            fg = os.tcgetpgrp(self._proc.child_fd)
            shell_pgid = os.getpgid(self._proc.pid)
        except (OSError, AttributeError, termios.error):
            return None
        return _classify_terminal_state(bool(lflag & termios.ICANON), fg == shell_pgid)

    # ------------------------------------------------------------------
    # Stdin-wait evidence
    # ------------------------------------------------------------------

    async def _probe_stdin_wait(self) -> bool:
        """Kernel evidence: is the foreground process group blocked reading stdin?"""
        proc = self._proc
        if proc is None:
            return False

        def _check() -> bool:
            pgid = foreground_pgid(proc.pid)
            return pgid is not None and is_stdin_waiting(pgid)

        loop = asyncio.get_running_loop()
        try:
            return await loop.run_in_executor(None, _check)
        except Exception:
            return False

    # ------------------------------------------------------------------
    # Marker-protocol read loop
    # ------------------------------------------------------------------

    async def _heal_stale_wait(self) -> bool:
        """Try to close a WAITING transaction whose command already ended.

        Reads until quiet: if the pending END marker (or a bare shell
        prompt — the wrapper was consumed) arrives, the old transaction
        completed while nobody was collecting; close it and report the
        session usable. Silence means the foreground reader is still
        alive — not healable; as a side effect the WAITING kind is
        reclassified from the current kernel state. When that evidence is
        unavailable, the trailing output shape preserves the prior behavior.
        """
        pending = self._pending
        if pending is None:
            self._waiting_shell = False
            self._phase = _Phase.IDLE
            return True
        chunks: list[str] = []
        deadline = monotonic() + _SETTLE_MAX_S
        quiet = 0.0
        while monotonic() < deadline:
            chunk = await self._read_chunk()
            if chunk is None:
                self._terminate_session_sync()
                return True
            quiet = quiet + _POLL_INTERVAL_S if not chunk else 0.0
            if chunk:
                chunks.append(chunk)
                text = "".join(chunks)
                if pending.end_re.search(text) is not None or _PS1_TOKEN in text:
                    self._pending = None
                    self._waiting_shell = False
                    self._phase = _Phase.IDLE
                    return True
            if quiet >= _SETTLE_QUIET_S:
                break
        evidence = "".join(chunks) if chunks else ""
        if not evidence:
            evidence = self._wait_tail or ""
        signal = self._terminal_state()
        self._waiting_shell = (
            signal is _TerminalSignal.CHILD_RAW
            if signal is not None
            else _looks_like_shell_prompt(evidence)
        )
        return False

    async def _collect(self, pending: _PendingWait) -> str:
        """Poll the PTY until the END marker, PS1-without-marker, stdin-wait
        evidence, deadline, or shell exit — whichever first.

        Completion via the END marker is event-driven. A PS1 token closes a
        transaction abnormally only when SHELL_READLINE proves bash regained
        control at readline, or when terminal-state evidence is unavailable;
        tokens seen during canonical shell work or child ownership wait for the
        END marker. The terminal-state matrix is sampled on every
        read-loop tick: two consecutive child-owned raw-mode observations,
        after a quiet window and with buffered output, identify interactive
        takeover and return a shell-kind WAITING result. Other matrix states
        continue through the stdin probe, keyword detector, and weak
        prompt-shape fallback. Those layers also require the output-quiet
        window so a still-streaming command is never truncated by an early
        verdict. Output silence alone is never stdin-wait evidence — a silent
        transaction waits for detector evidence or the command deadline.
        """
        accum = _RollingBuffer()
        idle_since = monotonic()
        probe_usable = stdin_probe_available()
        next_probe_at = monotonic() + _STDIN_PROBE_INTERVAL_S
        consecutive_raw = 0
        while True:
            chunk = await self._read_chunk()
            if chunk is None:
                partial = self._finalize(accum, pending, None)
                self._terminate_session_sync()
                return _with_notice(partial, _SHELL_EXITED_NOTICE)
            if chunk:
                accum.append(chunk)
                idle_since = monotonic()
                match = pending.end_re.search(accum.text)
                if match is not None:
                    self._pending = None
                    self._waiting_shell = False
                    self._phase = _Phase.IDLE
                    return self._finalize(accum, pending, int(match.group(1)), match)
                start_at = accum.text.find(pending.start_token)
                ps1_at = accum.text.find(_PS1_TOKEN)
                # PS1 before our START token is stale pre-command noise;
                # PS1 after it (or with the wrapper consumed — no START at
                # all) is abnormal completion only when readline is active,
                # or when terminal-state evidence is unavailable.
                if ps1_at >= 0 and (start_at < 0 or ps1_at > start_at):
                    signal = self._terminal_state()
                    if signal is _TerminalSignal.SHELL_READLINE or signal is None:
                        self._pending = None
                        self._waiting_shell = False
                        self._phase = _Phase.IDLE
                        return self._finalize(accum, pending, None)
            if pending.deadline is not None and monotonic() >= pending.deadline:
                partial = self._finalize(accum, pending, None)
                self._terminate_session_sync()
                partial_body = f"\n{partial}\n"
                return _TIMEOUT_MESSAGE.format(
                    seconds=pending.timeout_seconds, partial=partial_body
                )
            now = monotonic()
            quiet = now - idle_since >= _PROMPT_SETTLE_S
            signal = self._terminal_state()
            if signal is _TerminalSignal.CHILD_RAW:
                consecutive_raw += 1
            else:
                consecutive_raw = 0
            if consecutive_raw >= 2 and quiet and accum.text:
                self._waiting_shell = True
                self._pending = pending
                self._phase = _Phase.WAITING
                self._wait_tail = accum.text[-256:]
                return _with_hint(self._finalize(accum, pending, None), _SHELL_HINT)
            if probe_usable and now >= next_probe_at:
                next_probe_at = now + _STDIN_PROBE_INTERVAL_S
                if quiet and await self._probe_stdin_wait():
                    # A child-owned raw terminal accepts shell passthrough;
                    # every other state remains prompt-kind.
                    signal = self._terminal_state()
                    self._waiting_shell = signal is _TerminalSignal.CHILD_RAW
                    self._pending = pending
                    self._phase = _Phase.WAITING
                    self._wait_tail = accum.text[-256:]
                    return _with_hint(
                        self._finalize(accum, pending, None), _hint_for(self._waiting_shell)
                    )
            # Stdin-wait content evidence splits by strength. Keyword shapes
            # fire regardless of the kernel probe — they cover the /dev/tty
            # blind spot and builtin ``read`` waits. The WEAK generic
            # prompt-suffix heuristic (a trailing ``:``/``?``/``)`` — data
            # lines match it too) fires only when the probe is unavailable
            # or positively confirms a stdin wait; a slow compute-then-
            # print command (gpt2-codegolf) must not be misread as waiting.
            if accum.text and quiet and is_waiting_for_input(accum.text):
                self._waiting_shell = False
                self._pending = pending
                self._phase = _Phase.WAITING
                self._wait_tail = accum.text[-256:]
                return _with_hint(self._finalize(accum, pending, None), _STDIN_HINT)
            if accum.text and quiet and _looks_like_foreign_prompt(accum.text):
                weak_confirmed = not probe_usable
                if probe_usable and not weak_confirmed:
                    weak_confirmed = await self._probe_stdin_wait()
                if weak_confirmed:
                    self._waiting_shell = False
                    self._pending = pending
                    self._phase = _Phase.WAITING
                    self._wait_tail = accum.text[-256:]
                    return _with_hint(self._finalize(accum, pending, None), _STDIN_HINT)

    def _finalize(
        self,
        accum: _RollingBuffer,
        pending: _PendingWait,
        exit_code: int | None,
        match: re.Match[str] | None = None,
    ) -> str:
        """Format the command's response from the rolling buffer.

        With a *match*, output is sliced to the command's OWN marker pair
        (``START`` token up to the END match) — output printed before the
        START token belongs to an earlier transaction and never reaches
        the model.  Marker-shaped lines from any other source (foreign
        transaction residue, command-printed text) are stripped.  A
        dropped head prepends the truncation notice.
        """
        text = accum.text
        if match is not None:
            text = text[: match.start()]
            start_at = text.rfind(pending.start_token)
            if start_at >= 0:
                text = text[start_at + len(pending.start_token) :].lstrip("\r\n")
        text = _FOREIGN_MARKER_RE.sub("", text)
        result = _format_result(text, exit_code, self._max_output_chars)
        if accum.dropped_head:
            return _with_notice(_TRUNCATED_HEAD_NOTICE, result)
        return result
