"""Tests for platform-agnostic prompt detection, Windows drain, and output sanitization."""

import pytest

from framework.tools.terminal.prompt import drain_windows_startup, is_prompt_ready, sanitize_terminal_output

DA1_RESPONSE = "\x1b[?1;2;3c"
READY_PROMPT = "user@example:/workspace$ "


class TestPromptReady:
    """Two-layer prompt detection: suffix pre-filter -> regex confirmation."""

    @pytest.mark.parametrize("text", [
        "user@host:~$ ",
        "user@host:~$",
        "root@host:~# ",
        "user@host:~% ",
        "PS C:\\Users\\test> ",
        "C:\\Users\\test>",
        "/home/user> ",
        "user@host's password: ",
        "myapp: ",
        ">>> ",
    ])
    def test_common_prompts_match(self, text: str) -> None:
        assert is_prompt_ready(text) is True

    def test_multiline_output_ends_with_prompt(self) -> None:
        text = "output\nline2\n$ "
        assert is_prompt_ready(text) is True

    def test_bash_no_trailing_space(self) -> None:
        assert is_prompt_ready("user@host:~$") is True

    @pytest.mark.parametrize("text", [
        "hello world",
        "if x > y",
        "",
        "output\nline2\ndone",
        "some normal text\nmore text",
    ])
    def test_non_prompts_dont_match(self, text: str) -> None:
        assert is_prompt_ready(text) is False

    def test_empty_string(self) -> None:
        assert is_prompt_ready("") is False

    def test_continuation_prompt_not_ready(self) -> None:
        """The '>>' continuation prompt must NOT be treated as command complete.

        When a shell enters multi-line input mode (e.g. python, bash),
        the '>>' prompt means the command is INCOMPLETE — more input is needed.
        If is_prompt_ready matches '>>', execute() would truncate output.
        """
        assert is_prompt_ready(">> ") is False
        assert is_prompt_ready(">>") is False
        # Multi-line context: command output ends with continuation prompt
        assert is_prompt_ready("bash$ python\n>> ") is False

    def test_da1_suffix_not_prompt(self) -> None:
        """DA1 device attribute sequences ending with 'c' must not be mistaken for prompts."""
        assert is_prompt_ready(DA1_RESPONSE) is False
        # When DA1 leaks onto the command line (the log.md / log2.md bug)
        assert is_prompt_ready(f"{READY_PROMPT}{DA1_RESPONSE}") is False


class MockDrainIo:
    """Minimal mock with read/write/is_alive for testing drain_windows_startup."""

    def __init__(self, reads: list[str], alive: bool = True) -> None:
        self._reads = list(reads)
        self._writes: list[str] = []
        self._alive = alive

    async def read(self, timeout: float = 5.0, max_size: int = 65536) -> str:
        if self._reads:
            return self._reads.pop(0)
        return ""

    async def write(self, data: str) -> None:
        self._writes.append(data)

    async def is_alive(self) -> bool:
        return self._alive


class TestWindowsDrainStartup:
    """Verify drain_windows_startup clears the line without interrupting."""

    @pytest.mark.asyncio
    async def test_normal_startup(self) -> None:
        io = MockDrainIo(reads=[
            READY_PROMPT,
            "",
            READY_PROMPT,
        ])
        await drain_windows_startup(io.read, io.write, io.is_alive)
        assert "\x03" not in io._writes
        assert "\x01\x0b" in io._writes

    @pytest.mark.asyncio
    async def test_da1_delayed_then_cleared(self) -> None:
        io = MockDrainIo(reads=[
            READY_PROMPT,
            DA1_RESPONSE,
            "",
            READY_PROMPT,
        ])
        await drain_windows_startup(io.read, io.write, io.is_alive)
        assert "\x03" not in io._writes
        assert "\x01\x0b" in io._writes

    @pytest.mark.asyncio
    async def test_startup_timeout_no_prompt(self) -> None:
        io = MockDrainIo(reads=[
            "still starting...",
            "still starting...",
            "still starting...",
        ])
        import time
        start = time.monotonic()
        await drain_windows_startup(io.read, io.write, io.is_alive)
        elapsed = time.monotonic() - start
        assert elapsed < 15.0

    @pytest.mark.asyncio
    async def test_dead_backend_exits_early(self) -> None:
        io = MockDrainIo(reads=[], alive=False)
        await drain_windows_startup(io.read, io.write, io.is_alive)
        assert "\x03" not in io._writes

    @pytest.mark.asyncio
    async def test_da1_mixed_with_text_consumed(self) -> None:
        """Phase 2 must consume DA1 even when it appears mid-chunk (not at start).

        log2.md bug: DA1 was concatenated with command text because Phase 2
        only discarded chunks starting with \\x1b.  A chunk like
        'some-text\\x1b[?61;...c' would slip through.
        """
        io = MockDrainIo(reads=[
            READY_PROMPT,
            f"junk-before{DA1_RESPONSE}",
            "",
            READY_PROMPT,
        ])
        await drain_windows_startup(io.read, io.write, io.is_alive)
        assert "\x03" not in io._writes
        assert "\x01\x0b" in io._writes
        # The line clear should remove whatever leaked through; final prompt must be clean.
        # (We verify drain completes without raising; the test above checks writes.)

    @pytest.mark.asyncio
    async def test_da1_after_prompt_consumed(self) -> None:
        """DA1 appearing right after the prompt (log2.md line 8) must be consumed."""
        io = MockDrainIo(reads=[
            f"{READY_PROMPT}{DA1_RESPONSE}",
            "",
            READY_PROMPT,
        ])
        await drain_windows_startup(io.read, io.write, io.is_alive)
        assert "\x03" not in io._writes
        assert "\x01\x0b" in io._writes


class TestSanitizeTerminalOutput:
    """Model-facing output sanitizer — strips terminal control protocols,
    preserves real command text and Unicode."""

    def test_strips_sgr_color_codes(self) -> None:
        raw = "\x1b[0;32;92mgyt@XXSDDM\x1b[0m:\x1b[0;34;94m/mnt/f\x1b[0m$"
        assert sanitize_terminal_output(raw) == "gyt@XXSDDM:/mnt/f$"

    def test_strips_cursor_visibility(self) -> None:
        assert sanitize_terminal_output("\x1b[?25lhello\x1b[?25h") == "hello"

    def test_strips_erase_line(self) -> None:
        assert sanitize_terminal_output("foo\x1b[0Kbar") == "foobar"

    def test_strips_da1(self) -> None:
        assert sanitize_terminal_output("ready\x1b[?1;2;3c") == "ready"

    def test_strips_osc_title(self) -> None:
        assert sanitize_terminal_output("\x1b]0;title\x07prompt$ ") == "prompt$ "
        assert sanitize_terminal_output("\x1b]0;title\x1b\\prompt$ ") == "prompt$ "

    def test_strips_absolute_cursor_position(self) -> None:
        assert sanitize_terminal_output("output\x1b[71G") == "output"

    def test_strips_carriage_return_repaint(self) -> None:
        assert sanitize_terminal_output("  50%\r100%") == "100%"

    def test_preserves_newlines(self) -> None:
        assert sanitize_terminal_output("line1\nline2\n") == "line1\nline2\n"

    def test_preserves_unicode(self) -> None:
        assert sanitize_terminal_output("你好世界") == "你好世界"

    def test_preserves_git_log_content(self) -> None:
        """Real git log output from issue.md — colors stripped, text kept."""
        raw = (
            "\x1b[0;33m95691ee\x1b[0m fix(terminal): stabilize\x1b[0K\r\n"
            "\x1b[0;33m972c77d\x1b[0m docs: remove outdated\x1b[0K\r\n"
            "\x1b[0;32;92mgyt@XXSDDM\x1b[0m$\x1b[0K\x1b[71G\x1b[?25h"
        )
        clean = sanitize_terminal_output(raw)
        assert "\x1b[" not in clean
        assert "95691ee" in clean
        assert "stabilize" in clean
        assert "972c77d" in clean
        assert "gyt@XXSDDM$" in clean

    def test_preserves_escaped_literal_text(self) -> None:
        r"""Text like '\\x1b[31m' printed by a command must survive."""
        raw = r"the pattern is \x1b[31m"
        assert sanitize_terminal_output(raw) == raw

    def test_empty_string(self) -> None:
        assert sanitize_terminal_output("") == ""

    def test_plain_text_unchanged(self) -> None:
        text = "hello world\nline 2\n$ "
        assert sanitize_terminal_output(text) == text
