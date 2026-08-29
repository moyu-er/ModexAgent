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

from modex_agent.tools.terminal.results import TerminalSegment

# DA1 (Device Attributes) pollution from conpty/PSReadLine.
_DA1_PATTERN = re.compile(r"\x1b\[\?[\d;]+c")

# ANSI escape sequences: colours, cursor moves, DECSET/DECRST, etc.
_ANSI_PATTERN = re.compile(
    r"\x1b\["  # CSI
    r"(?:[\d;]*[A-Za-z]"  # sequences like \x1b[32m, \x1b[2J, \x1b[?25h
    r"|\?\d+[hl])"  # DECSET/DECRST like \x1b[?25l
)

# Non-CSI escape sequences: ESC + single char (e.g. \x1b= enter keypad,
# \x1b> exit keypad, \x1bD index, \x1bM reverse index).  These are
# emitted by less/vim/etc. on entry/exit and leak into model-facing
# output if not stripped.
_ESC_CHAR_PATTERN = re.compile(r"\x1b[=>DMEcZ78]")

# OSC title sequences: \x1b]0;title\x07 or \x1b]0;title\x1b\\
_OSC_PATTERN = re.compile(r"\x1b\][^\x07]*\x07|\x1b\][^\x1b]*\x1b\\")


def _strip_ansi_and_da1(text: str) -> str:
    """Remove ANSI escape sequences and DA1 pollution from terminal output.

    Produces plain text suitable for prompt detection and input-prompt
    heuristics without being confused by conpty colour codes or cursor moves.
    """
    text = _DA1_PATTERN.sub("", text)
    text = _ANSI_PATTERN.sub("", text)
    text = _ESC_CHAR_PATTERN.sub("", text)
    return text


def sanitize_terminal_output(text: str) -> str:
    """Strip terminal control protocols from PTY output for model-facing tool results.

    Removes DA1, CSI (cursor/color/erase/DEC private), OSC title sequences,
    non-CSI escape chars (keypad mode etc.), and carriage-return repaint
    noise.  Preserves Unicode text, real command output, intentional line
    breaks, and literal escaped text.
    """
    text = _OSC_PATTERN.sub("", text)
    text = _ANSI_PATTERN.sub("", text)
    text = _DA1_PATTERN.sub("", text)
    text = _ESC_CHAR_PATTERN.sub("", text)
    # Normalize real line endings first, then handle standalone \r repaint.
    text = text.replace("\r\n", "\n")
    # Standalone \r repaint: per logical line, keep only text after the last \r.
    lines = text.split("\n")
    lines = [line.rsplit("\r", 1)[-1] for line in lines]
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Input-prompt detection
# ---------------------------------------------------------------------------

INPUT_PROMPT_MARKERS: tuple[str, ...] = (
    "password",
    "passphrase",
    "login:",
    "username:",
    "user name:",
    "enter password",
    "enter passphrase",
    "[y/n]",
    "[Y/n]",
    "[yes/no]",
    "(yes/no)",
    "(y/n)",
    "[y/N]",
    "(Y/n)",
    "pin:",
    "token:",
    "passcode",
    "code:",
    "verification code:",
    "2fa code:",
    "otp:",
    "press any key to continue",
    "overwrite",
    "replace",
    "confirm",
    "current password",
    "new password",
    "retype password",
    "repeat password",
    "continue?",
    "proceed?",
    "are you sure",
    "do you want",
    "select",
)

# Markers that can coincidentally appear in legitimate output
# (e.g. "Hashing password: 50% done" or "Confirming transaction...").
# These need extra validation: the line must end with prompt-ending
# punctuation (':', '?', ']', ')') — otherwise it's likely output, not a prompt.
# login:/username:/user name:/code:/token:/otp:/pin:/select joined this set:
# data lines like "Last login: Fri Aug 28 10:00 2026", "exit code: 0" and
# "selected 3 files" collide with real prompts of the same keywords.
_AMBIGUOUS_MARKERS: frozenset[str] = frozenset(
    {
        "password",
        "passphrase",
        "confirm",
        "overwrite",
        "replace",
        "passcode",
        "login:",
        "username:",
        "user name:",
        "code:",
        "token:",
        "otp:",
        "pin:",
        "select",
    }
)

# Characters that typically terminate an input prompt line.
_PROMPT_ENDING_CHARS: tuple[str, ...] = (":", "?", "]", ")")

# Layer 2's shell-prompt exclusion: the bare remote-shell prompt shape
# (``user@host:`` / ``root@host:~:``) — a single token, no spaces.  A line
# ending in ``:`` that merely CONTAINS ``@`` (e.g. ``user@host's password:``)
# is still very likely a real input prompt and must stay positive, so only
# this minimal shape is excluded.
_BARE_REMOTE_PROMPT_PATTERN = re.compile(r"^\S+@\S+:\S*$")


def is_waiting_for_input(output: str) -> bool:
    """Check if the last non-empty line of *output* looks like an input prompt.

    Strips ANSI escape sequences before checking.  Case-insensitive.

    Two detection layers (either triggers a positive):
    1. **Marker match**: the last line contains a known input-prompt keyword
       (password, passphrase, [y/n], confirm, etc.). Ambiguous markers
       (e.g. "password") additionally require prompt-ending punctuation.
    2. **Suffix match**: the last non-empty line ends with ``:``, ``?``,
       ``]`` or ``)`` AND does NOT look like a shell prompt (``$``/``#``/
       ``%``/``>`` suffix, or ``user@host`` form). This catches arbitrary
       custom prompts like ``x:``, ``Continue?``, ``[A]llow?`` without
       needing an exhaustive keyword list.

    Handles ``\\r`` repaint lines (progress bars, download indicators) by
    only inspecting the most-recently-painted segment.
    """
    if not output:
        return False
    plain = _strip_ansi_and_da1(output)
    lines = plain.splitlines()
    if not lines:
        return False

    last = lines[-1].lower()

    if "\r" in last:
        last = last.rsplit("\r", 1)[-1]

    if not last.strip():
        return False

    # Layer 1: known marker keywords
    for marker in INPUT_PROMPT_MARKERS:
        if marker not in last:
            continue
        if marker in _AMBIGUOUS_MARKERS:
            stripped = last.rstrip()
            if not stripped.endswith(_PROMPT_ENDING_CHARS) and stripped != marker:
                continue
        return True

    # Layer 1's ambiguous-marker gate and Layer 2 intentionally compose as a
    # union: ambiguous prose is skipped, while prompt-shaped punctuation remains evidence.
    # Layer 2: prompt-shaped suffix — catches arbitrary custom prompts
    # ("name:", "Ready?", "Choose [1]:", "Enter choice)") that no keyword
    # list covers; without it, non-keyword prompts produce zero evidence on
    # probe-less hosts and ride the poll loop to the command deadline.
    # False positives (data lines that merely end with ':', '?', ']' or
    # ')') are tolerated BY DESIGN: callers gate this behind an
    # output-quiet window and a soft-worded advisory, so the agent judges
    # from the output. Shell-prompt endings ('$'/'#'/'%'/'>' suffixes) are
    # disjoint from this suffix set by construction; the bare user@host
    # remote form is excluded separately.
    stripped = last.rstrip()
    if not stripped.endswith(_PROMPT_ENDING_CHARS):
        return False
    return _BARE_REMOTE_PROMPT_PATTERN.match(stripped) is None


PROMPT_SUFFIXES: tuple[str, ...] = (
    "$ ",
    "# ",
    "> ",
    "% ",
    ": ",
    "$",
    "#",
    ">",
    "%",
    ":",
)

PROMPT_PATTERNS: list[re.Pattern[str]] = [
    # General shell prompts
    re.compile(r"\$\s*$"),
    re.compile(r"#\s*$"),
    re.compile(r"%\s*$"),
    # Path-based prompts (need path context)
    re.compile(r"PS\s+\S*>\s*$"),
    re.compile(r"[A-Za-z]:\\[^>\n]*>\s*$"),
    re.compile(r"/[^>\n]*>\s*$"),
    # user@host patterns
    re.compile(r"\S+@\S+[^$\n]*\$\s*$"),
    re.compile(r"\S+@\S+[^#\n]*#\s*$"),
    # Colon-ending prompts
    re.compile(r"\S+@\S*[^:\n]*:\s*$"),
    # Python REPL (must be exactly 3 >, NOT PowerShell continuation >>)
    re.compile(r">>>\s*$"),
    # Single-char no-trailing-space (most permissive, last)
    re.compile(r"[^>$\n#%]\$$"),
    re.compile(r"[^>\n]>$"),
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

        # Phase 5: handshake — send empty command, then wait for the prompt
    # to actually appear in the output.  This is stricter than waiting for
    # silence: a slowly-starting bash may pause between bytes and trick the
    # silence detector, leaving the first real command corrupted.
    await write_fn("\n")
    await asyncio.sleep(0.2)
    # Read until a clean prompt appears — not just silence.
    post_handshake = ""
    while _time.monotonic() < deadline:
        if not await is_alive_fn():
            return
        chunk = await read_fn(0.3, 65536)
        if chunk:
            post_handshake += chunk
        if _is_clean_prompt(post_handshake):
            break
    # Drain remaining output after the prompt.
    await _drain_until_quiet(max_empty_reads=3, read_timeout=0.3)


_PAGER_PATTERNS: list[re.Pattern[str]] = [
    # less: bare ":" on its own line (waiting for command)
    re.compile(r"^:\s*$"),
    # less: "(END)" or "(END) - Press q to quit" style EOF markers
    re.compile(r"^\(END\)"),
    # more: "--More--" or "--More--(N%)" style prompts
    re.compile(r"^--More--"),
    # less: status line "lines N-M/L" or "lines N-M"
    re.compile(r"^lines\s+\d+"),
    # less: "Waiting for data... (interrupt to abort)" for pipe input
    re.compile(r"^Waiting for data"),
]


def detect_pager_entry(cursor_line: str) -> bool:
    """Detect if cursor line matches a known pager (less/more) prompt.

    Covers less's command prompt (:), EOF marker (END), status line,
    and more's --More-- prompt.  Excludes incidental colons in config
    output or error messages.
    """
    stripped = cursor_line.strip()
    if not stripped:
        return False
    return any(p.search(stripped) for p in _PAGER_PATTERNS)


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


def _is_prompt_with_command(line: str) -> bool:
    """Detect lines like 'PS F:\\project> npm install' where a prompt
    marker and command text appear on the same line.

    Extracts the prefix before the first prompt-marker + space and
    delegates to ``is_prompt_ready`` for accurate detection.

    Guard: text immediately after the marker+space must look like a real
    command (starts with a letter, dot, slash, or dash), not a number.
    This prevents false positives on output lines like
    "Total cost: $ 42.50" or "  step # 3 complete".
    """
    stripped = line.rstrip()
    for marker in ("> ", "$ ", "# "):
        idx = stripped.find(marker)
        if idx < 0:
            continue
        prefix = stripped[: idx + 1]  # include the marker character
        if not is_prompt_ready(prefix):
            continue
        # Text after the marker must look like a real command.
        # Digits immediately after "$ " suggest currency/number output,
        # not a command (e.g. "Total cost: $ 42.50").
        after = stripped[idx + len(marker) :]
        if after and after[0].isdigit():
            continue
        return True
    return False


def extract_last_command_output(text: str) -> str:
    """Extract terminal output from the second-to-last prompt to the end.

    Finds all lines that look like shell prompts and returns from the
    second-to-last one to the end.  Uses ``is_prompt_ready`` for bare
    prompts and ``_is_prompt_with_command`` for lines where the prompt
    marker and command text share a line.

    This captures:
    - The prompt before the command
    - The command output
    - The next prompt (if the command completed)

    Falls back to the only prompt or the full text.
    """
    if not text:
        return ""
    clean = _strip_ansi_and_da1(text)
    lines = clean.splitlines()
    if not lines:
        return ""

    prompt_indexes = [
        idx
        for idx, line in enumerate(lines)
        if is_prompt_ready(line) or _is_prompt_with_command(line)
    ]

    if len(prompt_indexes) >= 2:
        start = prompt_indexes[-2]
    elif len(prompt_indexes) == 1:
        start = prompt_indexes[0]
    else:
        start = max(0, len(lines) - 1)

    return "\n".join(lines[start:])
