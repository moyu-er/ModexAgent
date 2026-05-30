"""Platform-agnostic prompt detection and Windows startup drain.

Two-layer filter:
1. Fast suffix pre-filter (covers 99% of non-prompt output)
2. Regex confirmation (only runs when pre-filter passes)
"""

from __future__ import annotations

import asyncio
import re
import time as _time
from collections.abc import Awaitable, Callable

from framework.tools.terminal.results import TerminalSegment

# DA1 (Device Attributes) pollution from conpty/PSReadLine.
_DA1_PATTERN = re.compile(r"\x1b\[\?[\d;]+c")

# ANSI escape sequences: colours, cursor moves, DECSET/DECRST, etc.
_ANSI_PATTERN = re.compile(
    r"\x1b\["  # CSI
    r"(?:[\d;]*[A-Za-z]"  # sequences like \x1b[32m, \x1b[2J, \x1b[?25h
    r"|\?\d+[hl])"  # DECSET/DECRST like \x1b[?25l
)

# OSC title sequences: \x1b]0;title\x07 or \x1b]0;title\x1b\\
_OSC_PATTERN = re.compile(r"\x1b\][^\x07]*\x07|\x1b\][^\x1b]*\x1b\\")


def _strip_ansi_and_da1(text: str) -> str:
    """Remove ANSI escape sequences and DA1 pollution from terminal output.

    Produces plain text suitable for prompt detection and input-prompt
    heuristics without being confused by conpty colour codes or cursor moves.
    """
    text = _DA1_PATTERN.sub("", text)
    text = _ANSI_PATTERN.sub("", text)
    return text


def sanitize_terminal_output(text: str) -> str:
    """Strip terminal control protocols from PTY output for model-facing tool results.

    Removes DA1, CSI (cursor/color/erase/DEC private), OSC title sequences,
    and carriage-return repaint noise.  Preserves Unicode text, real command
    output, intentional line breaks, and literal escaped text.
    """
    text = _OSC_PATTERN.sub("", text)
    text = _ANSI_PATTERN.sub("", text)
    text = _DA1_PATTERN.sub("", text)
    # Normalize real line endings first, then handle standalone \r repaint.
    text = text.replace("\r\n", "\n")
    # Standalone \r repaint: per logical line, keep only text after the last \r.
    lines = text.split("\n")
    lines = [line.rsplit("\r", 1)[-1] for line in lines]
    return "\n".join(lines)

PROMPT_SUFFIXES: tuple[str, ...] = (
    "$ ",  "# ",  "> ",  "% ",  ": ",
    "$",   "#",   ">",   "%",   ":",
)

PROMPT_PATTERNS: list[re.Pattern[str]] = [
    # General shell prompts
    re.compile(r'\$\s*$'),
    re.compile(r'#\s*$'),
    re.compile(r'%\s*$'),
    # Path-based prompts (need path context)
    re.compile(r'PS\s+\S*>\s*$'),
    re.compile(r'[A-Za-z]:\\[^>\n]*>\s*$'),
    re.compile(r'/[^>\n]*>\s*$'),
    # user@host patterns
    re.compile(r'\S+@\S+[^$\n]*\$\s*$'),
    re.compile(r'\S+@\S+[^#\n]*#\s*$'),
    # Colon-ending prompts
    re.compile(r'\S+@\S*[^:\n]*:\s*$'),
    # ^ anchor required: prevents matching trailing "user: " inside "[sudo] password for user: "
    re.compile(r'^\w+:\s*$'),
    # Python REPL (must be exactly 3 >, NOT PowerShell continuation >>)
    re.compile(r'>>>\s*$'),
    # Single-char no-trailing-space (most permissive, last)
    re.compile(r'[^>$\n#%]\$$'),
    re.compile(r'[^>\n]>$'),
]


def is_prompt_ready(text: str) -> bool:
    """Check whether the last non-empty line of *text* looks like a shell prompt.

    Returns True if the terminal is ready for user/agent input.
    """
    if not text:
        return False
    lines = text.splitlines()
    last = ""
    for line in reversed(lines):
        stripped = line.rstrip()
        if stripped:
            last = stripped
            break
    if not last:
        return False

    # Layer 1: fast suffix pre-filter
    if not any(last.endswith(s) for s in PROMPT_SUFFIXES):
        return False

    # Layer 2: regex confirmation
    return any(pat.search(last) for pat in PROMPT_PATTERNS)


async def drain_windows_startup(
    read_fn: Callable[[float, int], Awaitable[str]],
    write_fn: Callable[[str], Awaitable[None]],
    is_alive_fn: Callable[[], Awaitable[bool]],
    *,
    uses_readline: bool = True,
) -> None:
    """Block until terminal startup is fully complete, then clear the command line.

    Uses a conservative two-phase drain:
    1. Read until a prompt appears (startup banner consumed).
    2. Wait for a quiet period (3 consecutive empty reads) to catch any
       late ANSI/DA1 sequences that cross chunk boundaries or arrive
       after the initial prompt.
    3. Clear the current readline buffer, then wait for another quiet
       period so any trailing pollution is fully consumed.

    This is intentionally conservative: we trade a few hundred milliseconds
    of startup time for the guarantee that the first command is not
    corrupted by leftover startup sequences.
    """
    deadline = _time.monotonic() + 10.0

    _da1_pattern = re.compile(r"\x1b\[\?[\d;]+c")

    def _is_clean_prompt(text: str) -> bool:
        """Strip DA1 pollution before checking for a ready prompt."""
        return is_prompt_ready(_da1_pattern.sub("", text))

    async def _drain_until_quiet(
        max_empty_reads: int = 3,
        read_timeout: float = 0.3,
    ) -> str:
        """Read repeatedly until *max_empty_reads* consecutive reads return empty.

        Returns the last non-empty chunk received (may be useful for logging).
        """
        empty_count = 0
        last_chunk = ""
        while _time.monotonic() < deadline and empty_count < max_empty_reads:
            if not await is_alive_fn():
                return ""
            chunk = await read_fn(read_timeout, 65536)
            if chunk:
                last_chunk = chunk
                empty_count = 0
            else:
                empty_count += 1
        return last_chunk

    # Phase 1: read until prompt appears (banner consumed)
    accumulated = ""
    while _time.monotonic() < deadline:
        if not await is_alive_fn():
            return
        chunk = await read_fn(0.5, 65536)
        if not chunk:
            continue
        accumulated += chunk
        if _is_clean_prompt(accumulated):
            break

    # Phase 2: wait for complete quiet — catches late DA1/ANSI that arrives
    # after the prompt but before we declare startup done.
    await _drain_until_quiet(max_empty_reads=3, read_timeout=0.3)

    if uses_readline:
        # Phase 3: clear anything that may be sitting on the command line
        # without interrupting a foreground process.  Readline-only: \x01\x0b
        # is beginning-of-line + kill-line in bash/zsh but garbage in cmd.exe.
        await write_fn("\x01\x0b")
        await asyncio.sleep(0.3)

        # Phase 4: consume any trailing sequences until quiet again
        await _drain_until_quiet(max_empty_reads=3, read_timeout=0.3)

        # Phase 5: handshake - send empty command to confirm bash is truly ready.
    # After line clearing, bash may still have delayed bracketed-paste sequences
    # or readline state transitions pending.  An empty command forces bash to
    # process anything in its input buffer and emit a fresh prompt.  If there
    # is still trailing pollution it gets consumed here, not mixed with the
    # first real command.
    await write_fn("\n")
    await asyncio.sleep(0.2)
    await _drain_until_quiet(max_empty_reads=3, read_timeout=0.3)


_PAGER_ENTRY_MARKER = ":"


def detect_pager_entry(cursor_line: str) -> bool:
    """Detect if cursor line is a pager entry prompt (less colon).

    Only matches bare ":" on its own line. Excludes "config:", "error:", etc.
    """
    return cursor_line.strip() == _PAGER_ENTRY_MARKER


def resolve_cursor_line(segment: TerminalSegment) -> str:
    """Get cursor line, falling back to last non-empty text line.

    The tmux backend does not populate cursor_line (defaults to "").
    This helper provides a consistent fallback.
    """
    if segment.cursor_line:
        return segment.cursor_line
    lines = segment.text.splitlines()
    for line in reversed(lines):
        if line.strip():
            return line
    return ""
