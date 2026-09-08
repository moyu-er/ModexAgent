"""Spawn-seam argv tests for the persistent shell session.

The construction seam takes a full spawn command (``shell_argv``) instead
of a shell path: a sandbox runtime can prepend a launcher prefix
(``["docker", "exec", "-it", "<ctr>", "/bin/bash", ...]``) while host
mode composes the identical ``[shell, "--noprofile", "--norc", "-i"]``
shape the pre-argv spawn produced (that host-mode matrix is pinned in
``test_persistent_bash_platform.py``). These pin the explicit-argv half:
the pure resolution table (every host) and a REAL prefix spawn through a
launcher that records the argv it received and execs bash (POSIX only,
same skip discipline as the other real-pexpect suites).
"""

from __future__ import annotations

import importlib.util
import shutil
import sys
from pathlib import Path

import pytest

from modex_agent.tools.terminal._persistent_session import (
    PersistentShellManager,
    PersistentShellSession,
    _resolve_spawn_argv,
)

_HAS_PERSISTENT_BASH = (
    sys.platform != "win32"
    and shutil.which("bash") is not None
    and importlib.util.find_spec("pexpect") is not None
)


# ── resolver: explicit argv wins verbatim, bash-ness from innermost ──


@pytest.mark.parametrize(
    ("argv", "expected_bash"),
    [
        (["/opt/custom/bash"], True),
        (["/bin/zsh"], False),
        (["docker", "exec", "-it", "ctr", "/bin/bash", "--noprofile", "--norc", "-i"], True),
        (["bwrap", "--ro-bind", "/", "/", "/bin/zsh"], False),
        (["/usr/bin/env", "bash"], True),
    ],
)
def test_explicit_argv_wins_verbatim_with_innermost_bashness(
    argv: list[str], expected_bash: bool
) -> None:
    resolved, is_bash = _resolve_spawn_argv(argv)
    assert resolved == tuple(argv)
    assert is_bash is expected_bash


def test_empty_argv_rejected_at_construction() -> None:
    with pytest.raises(ValueError, match="non-empty"):
        PersistentShellSession(shell_argv=[])


# ── manager: the argv flows to every lazily materialized session ──


def test_manager_forwards_shell_argv_to_every_session() -> None:
    argv = ["docker", "exec", "-it", "ctr", "/bin/bash"]
    manager = PersistentShellManager(shell_argv=argv)
    assert manager.session_for("a").shell_argv == tuple(argv)
    assert manager.session_for("b").shell_argv == tuple(argv)
    assert manager.session_for(None).shell_argv == tuple(argv)


# ── real prefix spawn: launcher records the argv tail then execs bash ──


@pytest.mark.skipif(
    not _HAS_PERSISTENT_BASH, reason="real prefix spawn requires POSIX pexpect + bash"
)
async def test_prefix_argv_reaches_the_spawned_process(tmp_path: Path) -> None:
    """A launcher prefix (the sandbox shape) receives the argv tail
    verbatim: the recorded args prove the injected prefix flowed to the
    spawned process."""
    bash_path = shutil.which("bash")
    assert bash_path is not None
    argv_log = tmp_path / "argv.log"
    launcher = tmp_path / "fake-sandbox-launcher.sh"
    launcher.write_text(
        f'#!/bin/sh\nprintf \'%s\\n\' "$@" > "{argv_log}"\nexec "$@"\n',
        encoding="utf-8",
    )
    launcher.chmod(0o755)

    session = PersistentShellSession(
        shell_argv=[str(launcher), bash_path, "--noprofile", "--norc", "-i"],
        timeout_seconds=15,
    )
    try:
        await session.run_command("echo through-prefix")
        assert argv_log.read_text(encoding="utf-8").splitlines() == [
            bash_path,
            "--noprofile",
            "--norc",
            "-i",
        ]
    finally:
        await session.close()
