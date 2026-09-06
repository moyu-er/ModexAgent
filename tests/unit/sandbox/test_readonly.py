"""Read-only command classification — profiles, family resolution, and
the SecurityDecisionService fast path (the shell-world parallel twin).

Fail-closed contract: every disqualifier (substitution, redirect,
assignment, unknown command, unknown construct, parse failure) returns
False and the call takes the ordinary exclusive path.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from modex_agent.sandbox.decision import (
    GuardCategory,
    SecurityDecisionService,
)
from modex_agent.sandbox.readonly import classify_readonly, resolve_command_family
from modex_agent.sandbox.settings import (
    GuardSettings,
    SandboxBackend,
    SandboxSettings,
    WriteSurface,
)
from modex_agent.tools.terminal.types import (
    Platform as TerminalPlatform,
)
from modex_agent.tools.terminal.types import (
    ShellFamily,
    ShellInfo,
)
from modex_agent.tools.workspace_scoped import WorkspaceRootProvider

WS = Path("/ws/project")


class _FixedRoot(WorkspaceRootProvider):
    def __init__(self, root: Path) -> None:
        self._root = root

    def current(self) -> Path:
        return self._root


def _service(
    write_surface: WriteSurface = WriteSurface.WORKSPACE,
    guard: GuardSettings | None = None,
) -> SecurityDecisionService:
    kwargs: dict[str, object] = {
        "backend": "host",
        "exclusive": {"write_surface": write_surface.value},
    }
    if guard is not None:
        kwargs["guard"] = guard
    settings = SandboxSettings.model_validate(kwargs)
    return SecurityDecisionService(
        settings=settings,
        workspace_root_provider=_FixedRoot(WS),
    )


def _platform(family: ShellFamily) -> ShellInfo:
    return ShellInfo(
        family=family, path="/shells/shell", platform=TerminalPlatform.LINUX
    )


class TestBashProfile:
    @pytest.mark.parametrize("command", [
        "ls -la",
        "cat ../../etc/passwd",
        "tail -f x.log | grep yyy",
        "grep -r pattern src/",
        "cat a && cat b",
        "ls ~; pwd",
        "git status",
        "git log --oneline | head -5",
        "find . -name '*.py'",
        "cat $FILE",
        "which python",
    ])
    def test_readonly_commands_pass(self, command: str) -> None:
        assert classify_readonly(command, ShellFamily.BASH)

    @pytest.mark.parametrize("command", [
        "cat $(rm -rf x)",  # command substitution
        "echo `rm -rf x`",  # backtick substitution
        "diff <(ls a) <(ls b)",  # process substitution
        "echo hi > out.txt",  # redirect
        "cat a << EOF",  # heredoc
        "FOO=1 ls",  # assignment
        "a && rm -rf /",  # one non-readonly element
        "rm file",  # unknown command
        "sudo ls",
        "xargs ls",
        "sed -i 's/a/b/' f",
        "find . -delete",  # write-capable find flag
        "find . -exec rm {} ;",
        "git push",  # non-readonly git subcommand
        "git branch dev",
        "$CMD arg",  # dynamic command name
        "if ls; then cat b; fi",  # compound construct
    ])
    def test_disqualified_commands_fail_closed(self, command: str) -> None:
        assert not classify_readonly(command, ShellFamily.BASH)

    def test_empty_command_is_not_readonly(self) -> None:
        assert not classify_readonly("", ShellFamily.BASH)


class TestCmdProfile:
    @pytest.mark.parametrize("command", [
        "dir /b",
        "type a.txt",
        "findstr pattern file",
        "dir & type a.txt",
        "dir && type a.txt",
        "dir \"Program Files\"",
        "dir | more",
        "tasklist",
        "@echo off",
    ])
    def test_readonly_commands_pass(self, command: str) -> None:
        assert classify_readonly(command, ShellFamily.CMD)

    @pytest.mark.parametrize("command", [
        "echo hi > out.txt",
        "del a.txt",
        "copy a b",
        "dir %TEMP%",  # expansion
        "reg query HKLM",
        "powershell ls",  # nested interpreter
        "dir & del b",
    ])
    def test_disqualified_commands_fail_closed(self, command: str) -> None:
        assert not classify_readonly(command, ShellFamily.CMD)


class TestFamilyDispatch:
    def test_powershell_fails_closed(self) -> None:
        # No sound PowerShell profile yet — no executor routes there.
        assert not classify_readonly("ls", ShellFamily.POWERSHELL)

    def test_kernel_backends_spawn_bash(self) -> None:
        assert resolve_command_family(SandboxBackend.LOCAL) is ShellFamily.BASH
        assert resolve_command_family(SandboxBackend.OCI) is ShellFamily.BASH

    def test_host_follows_detected_shell(self, monkeypatch) -> None:
        monkeypatch.setattr(
            "modex_agent.sandbox.readonly.detect_platform_shell",
            lambda: _platform(ShellFamily.POWERSHELL),
        )
        assert resolve_command_family(SandboxBackend.HOST) is ShellFamily.POWERSHELL

    def test_host_without_shell_falls_back_to_subprocess_family(
        self, monkeypatch
    ) -> None:
        monkeypatch.setattr(
            "modex_agent.sandbox.readonly.detect_platform_shell", lambda: None
        )
        import modex_agent.sandbox.readonly as ro

        monkeypatch.setattr(ro, "get_platform", lambda: ro.Platform.WINDOWS)
        assert resolve_command_family(SandboxBackend.HOST) is ShellFamily.CMD


class TestDecisionFastPath:
    def test_readonly_command_outside_envelope_is_clean(self) -> None:
        # The shell-world parallel twin: a provable read is unrestricted
        # like file-tool reads — no boundary, no card.
        assert _service().evaluate_command("cat /etc/passwd").is_clean

    def test_write_command_outside_envelope_still_boundary(self) -> None:
        # touch is not provably read-only → ordinary exclusive path.
        verdict = _service().evaluate_command("touch /etc/x")
        assert verdict.category is GuardCategory.BOUNDARY

    def test_ssrf_command_is_not_readonly(self) -> None:
        verdict = _service().evaluate_command(
            "curl http://169.254.169.254/latest/meta-data"
        )
        assert verdict.category is GuardCategory.SSRF

    def test_readonly_command_with_url_text_is_clean(self) -> None:
        # grep performs no network I/O; the URL is matched text. The
        # fast path precedes the SSRF layer.
        assert _service().evaluate_command(
            "grep http://169.254.169.254 a.log"
        ).is_clean

    def test_toggle_off_restores_boundary(self) -> None:
        service = _service(guard=GuardSettings(read_only_bypass=False))
        verdict = service.evaluate_command("cat /etc/passwd")
        assert verdict.category is GuardCategory.BOUNDARY

    def test_explicit_foreign_cwd_readonly_passes(self) -> None:
        # A provable read writes nothing, cwd included.
        call_args: dict[str, object] = {"command": "cat notes.md", "working_dir": "/elsewhere"}
        assert _service().evaluate_tool_call("bash", call_args).is_clean

    def test_explicit_foreign_cwd_write_still_boundary(self) -> None:
        call_args: dict[str, object] = {"command": "touch notes.md", "working_dir": "/elsewhere"}
        verdict = _service().evaluate_tool_call("bash", call_args)
        assert verdict.category is GuardCategory.BOUNDARY
