"""Platform-guard tests for the persistent bash tooling.

Companion to ``test_persistent_bash.py`` (the real-protocol suite, skipped
off POSIX): these run on EVERY host and pin the Windows-host regression
fix — ``PersistentBashTool`` cannot spawn a POSIX pty on win32, so the
fallback seams must hand out :class:`SubprocessTool` there, and a
misrouted persistent shell must fail with a typed, diagnosable error.
"""

from __future__ import annotations

import sys

import pytest

from modex_agent.tools.terminal._persistent_session import (
    PersistentShellSession,
    PersistentShellUnsupportedError,
)
from modex_agent.tools.terminal.persistent_bash import (
    BashInputTool,
    PersistentBashTool,
    ensure_input_companion,
    persistent_bash_supported,
)
from modex_agent.tools.terminal.types import (
    Platform,
    ShellFamily,
    ShellInfo,
)

# ── persistent_bash_supported ──


@pytest.mark.skipif(sys.platform != "win32", reason="direct win32-host assertion")
def test_persistent_bash_supported_false_on_win32_host() -> None:
    """This host IS win32 — the guard must report unsupported."""
    assert persistent_bash_supported() is False


def test_persistent_bash_supported_false_when_win32(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(sys, "platform", "win32")
    assert persistent_bash_supported() is False


def test_persistent_bash_supported_true_on_linux(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(sys, "platform", "linux")
    assert persistent_bash_supported() is True


# ── shell resolution ──


def test_explicit_shell_path_is_used() -> None:
    session = PersistentShellSession(shell="/opt/custom/bash")
    assert session.shell_path == "/opt/custom/bash"
    assert session._is_bash_shell is True  # noqa: SLF001


def test_explicit_non_bash_shell_uses_plain_spawn_mode() -> None:
    session = PersistentShellSession(shell="/bin/zsh")
    assert session.shell_path == "/bin/zsh"
    assert session._is_bash_shell is False  # noqa: SLF001


def test_detection_failure_falls_back_to_bin_bash(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        "modex_agent.tools.terminal._persistent_session.detect_platform_shell",
        lambda: None,
    )
    session = PersistentShellSession()
    assert session.shell_path == "/bin/bash"
    assert session._is_bash_shell is True  # noqa: SLF001


def test_windows_family_detection_falls_back_to_bin_bash(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        "modex_agent.tools.terminal._persistent_session.detect_platform_shell",
        lambda: ShellInfo(family=ShellFamily.POWERSHELL, path="pwsh", platform=Platform.WINDOWS),
    )
    session = PersistentShellSession()
    assert session.shell_path == "/bin/bash"


def test_detected_bash_family_is_used_directly(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        "modex_agent.tools.terminal._persistent_session.detect_platform_shell",
        lambda: ShellInfo(family=ShellFamily.BASH, path="/usr/bin/bash", platform=Platform.LINUX),
    )
    session = PersistentShellSession()
    assert session.shell_path == "/usr/bin/bash"
    assert session._is_bash_shell is True  # noqa: SLF001


def test_detected_zsh_prefers_real_bash_then_resolved_shell(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """zsh/sh results defer to a which("bash") hit first (marker protocol
    and spawn flags are bash-specific); without one, the resolved shell
    runs plainly."""
    monkeypatch.setattr(
        "modex_agent.tools.terminal._persistent_session.detect_platform_shell",
        lambda: ShellInfo(family=ShellFamily.ZSH, path="/bin/zsh", platform=Platform.DARWIN),
    )
    monkeypatch.setattr(
        "modex_agent.tools.terminal._persistent_session.shutil.which",
        lambda name: "/usr/bin/bash" if name == "bash" else None,
    )
    session = PersistentShellSession()
    assert session.shell_path == "/usr/bin/bash"
    assert session._is_bash_shell is True  # noqa: SLF001

    monkeypatch.setattr(
        "modex_agent.tools.terminal._persistent_session.shutil.which", lambda name: None
    )
    session = PersistentShellSession()
    assert session.shell_path == "/bin/zsh"
    assert session._is_bash_shell is False  # noqa: SLF001


# ── spawn-time typed error (construction stays safe / lazy) ──


def test_default_timeout_480_and_description_declares_it() -> None:
    """Default construction carries the 480s per-command deadline
    (kill-and-reset contract); the description advertises it."""
    tool = PersistentBashTool()
    assert tool.session.timeout_seconds == 480
    assert "480s timeout" in tool.description
    assert "reset" in tool.description

    opted_in = PersistentBashTool(timeout_seconds=30)
    assert opted_in.session.timeout_seconds == 30
    assert "30s timeout" in opted_in.description

    disabled = PersistentBashTool(timeout_seconds=None)
    assert disabled.session.timeout_seconds is None
    # The timeout LINE drops; the unconditional background guidance still
    # says "timeout", so the absence check targets the line, not the word.
    assert "Each command has a" not in disabled.description


def test_max_output_chars_default_and_none() -> None:
    """Default clips oversized output (head+tail elision contract); explicit
    None disables internal clipping (the overflow interceptor owns it)."""
    tool = PersistentBashTool()
    assert tool.session.max_output_chars == 16_000
    assert "16000 characters" in tool.description

    unclipped = PersistentBashTool(max_output_chars=None)
    assert unclipped.session.max_output_chars is None
    assert "characters" not in unclipped.description


def test_construction_never_spawns_or_raises() -> None:
    """Lazy spawn: building the session/tool is safe on any host."""
    PersistentShellSession()
    PersistentBashTool()


async def test_run_command_raises_typed_error_when_win32(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Shell detection is stubbed too: a mocked win32 platform on a POSIX
    # host would otherwise break shutil.which's _winapi branch.
    monkeypatch.setattr(sys, "platform", "win32")
    monkeypatch.setattr(
        "modex_agent.tools.terminal._persistent_session.detect_platform_shell",
        lambda: None,
    )
    tool = PersistentBashTool()
    with pytest.raises(PersistentShellUnsupportedError, match="POSIX pty"):
        await tool.execute(command="echo hi")


async def test_run_command_raises_typed_error_when_pexpect_missing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(sys, "platform", "linux")
    monkeypatch.setitem(sys.modules, "pexpect", None)
    tool = PersistentBashTool()
    with pytest.raises(PersistentShellUnsupportedError, match="POSIX pty"):
        await tool.execute(command="echo hi")


@pytest.mark.skipif(sys.platform != "win32", reason="direct win32-host assertion")
async def test_run_command_raises_typed_error_on_win32_host() -> None:
    """The production regression: unmocked win32 first bash call must fail
    with the typed error (not a pexpect traceback)."""
    tool = PersistentBashTool()
    with pytest.raises(PersistentShellUnsupportedError, match="SubprocessTool fallback"):
        await tool.execute(command="echo hi")


# ── ensure_input_companion (structural bash + bash_input pair) ──


def test_ensure_input_companion_registers_bash_input_sharing_session() -> None:
    from modex_agent.core.tool_manager import InMemoryToolManager

    manager = InMemoryToolManager()
    bash = PersistentBashTool()
    ensure_input_companion(manager, bash)
    companion = manager.get_tool("bash_input")
    assert isinstance(companion, BashInputTool)
    assert companion._manager is bash._manager  # noqa: SLF001


def test_ensure_input_companion_noop_for_non_persistent_bash() -> None:
    from modex_agent.core.tool_manager import InMemoryToolManager
    from modex_agent.tools.terminal.subprocess_tool import (
        SubprocessTool,
        create_subprocess_executor,
    )

    manager = InMemoryToolManager()
    ensure_input_companion(manager, None)
    ensure_input_companion(manager, SubprocessTool(executor=create_subprocess_executor()))
    assert manager.get_tool("bash_input") is None


def test_ensure_input_companion_is_idempotent() -> None:
    from modex_agent.core.tool_manager import InMemoryToolManager

    manager = InMemoryToolManager()
    bash = PersistentBashTool()
    ensure_input_companion(manager, bash)
    first = manager.get_tool("bash_input")
    ensure_input_companion(manager, bash)
    assert manager.get_tool("bash_input") is first


def test_ensure_input_companion_replaces_stale_session_companion() -> None:
    """A pre-registered bash_input bound to a DIFFERENT session is replaced.

    The benchmark-roster regression: the roster swap unregistered ``bash``
    but left the pool's companion (bound to a never-started session), and
    the idempotency guard preserved it — every bash_input call then hit a
    dead session while the live shell waited for answers.
    """
    from modex_agent.core.tool_manager import InMemoryToolManager

    manager = InMemoryToolManager()
    stale_bash = PersistentBashTool()
    manager.register(BashInputTool(manager=stale_bash.manager))
    fresh_bash = PersistentBashTool()
    ensure_input_companion(manager, fresh_bash)
    companion = manager.get_tool("bash_input")
    assert isinstance(companion, BashInputTool)
    assert companion._manager is fresh_bash._manager


# ── tool descriptions: terminal-takeover semantics ──


def test_description_teaches_takeover_semantics() -> None:
    """A takeover ``[hint: ...]`` return must teach the split: commands keep
    executing inside a remote shell / REPL session, while a full-screen
    program is answered or quit through bash_input keystrokes."""
    tool = PersistentBashTool()
    desc = tool.description
    assert "they execute inside that session" in desc
    assert "for a full-screen program (a pager or editor)" in desc


def test_description_declares_strict_resource_limits() -> None:
    """TB2.1: the parallelism bullet is a STRICT limit statement, not an
    advisory note — overcommit's tool result is a silent OOM kill (exit
    137), so the warning must be unambiguous."""
    tool = PersistentBashTool()
    desc = tool.description
    assert "STRICT resource limits" in desc
    assert "OOM-killed" in desc
    assert "$(nproc)" in desc


def test_bash_input_description_documents_ctrl_c_forwarding() -> None:
    """Under a takeover, '^C' is one forwarded byte interpreted by whoever
    owns the terminal — not a guaranteed SIGINT (a local shell still treats
    it as SIGINT)."""
    bash = PersistentBashTool()
    tool = BashInputTool(manager=bash.manager)
    assert "forwarded as a byte to the program that owns the terminal" in tool.description
