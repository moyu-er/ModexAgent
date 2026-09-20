"""Probe stdin/console isolation for the Windows shell-detection probes.

Regression for the ACP-stdio ``new_session`` hang: ``_verify_bash`` spawned
``bash --version`` with ``capture_output=True`` but NO explicit ``stdin`` —
on Windows the probe inherited the host's stdin (the ACP JSON-RPC pipe when
booted over stdio) and, launched console-less without
``CREATE_NO_WINDOW``, made the msys2/WSL wrapper spawn helpers holding the
inherited pipe handles. On ``timeout=5`` ``subprocess.run`` kills only the
direct child and then joins the reader threads with NO timeout (the CPython
bpo-9140 Windows workaround) — surviving helpers kept the stdout write-end
open, so the join hung forever (faulthandler: ``_communicate readerthread
join``). The 5 s budget only guards ``wait()``, never the drain join.

The fix converges the probes onto the repo's existing isolation mechanisms:
``stdin=DEVNULL`` (like ``opencode server_manager`` / ``spawn_process_group``)
plus ``creationflags=_CREATE_NO_WINDOW`` (like ``_verify_wsl`` in the same
module — ``_verify_bash`` was the single divergent spawn site).

Two layers:

- Contract tests assert the spawn kwargs (fast, deterministic; FAIL on the
  current unfixed kwargs because ``stdin`` is absent).
- A real-inheritance test runs ``_verify_bash`` inside a child interpreter
  whose own stdin is a PIPE (simulating the ACP stdio host) against a fake
  ``bash.cmd`` that records what stdin IT actually received. FAIL on the
  unfixed code (the fake sees a pipe); passes once the probe detaches stdin.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from modex_agent.tools.terminal import types as terminal_types
from modex_agent.tools.terminal.types import _verify_bash, _verify_wsl

_IS_WINDOWS = sys.platform == "win32"


# ---------------------------------------------------------------------------
# Contract layer — the probe must detach stdin and (Windows) the console
# ---------------------------------------------------------------------------


def test_verify_bash_probe_detaches_stdin() -> None:
    with patch("modex_agent.tools.terminal.types.subprocess.run") as run:
        run.return_value = SimpleNamespace(returncode=0, stdout="GNU bash")
        assert _verify_bash(r"X:\Git\bin\bash.exe") is not None
        assert run.call_args.kwargs.get("stdin") is subprocess.DEVNULL


def test_verify_wsl_probe_detaches_stdin() -> None:
    with patch("modex_agent.tools.terminal.types.subprocess.run") as run:
        run.return_value = SimpleNamespace(returncode=0, stdout="Ubuntu")
        assert _verify_wsl(r"C:\Windows\System32\wsl.exe") is True
        assert run.call_args.kwargs.get("stdin") is subprocess.DEVNULL


@pytest.mark.skipif(not _IS_WINDOWS, reason="CREATE_NO_WINDOW is Windows-only")
def test_verify_bash_probe_creates_no_window() -> None:
    from modex_agent.tools.terminal.types import _CREATE_NO_WINDOW

    with patch("modex_agent.tools.terminal.types.subprocess.run") as run:
        run.return_value = SimpleNamespace(returncode=0, stdout="GNU bash")
        assert _verify_bash(r"X:\Git\bin\bash.exe") is not None
        assert run.call_args.kwargs.get("creationflags", 0) & _CREATE_NO_WINDOW


# ---------------------------------------------------------------------------
# Real-inheritance layer — the fake bash records the stdin it actually got
# ---------------------------------------------------------------------------

# cmd wrapper: the python child inherits the wrapper's stdin, fstat's it, and
# records 'chardev' (NUL device — the fixed behavior) or 'pipe' (an inherited
# pipe — the bug). Prints a line containing "bash" so the probe accepts it.
_FAKE_BASH_CMD = (
    '@echo off\n'
    '"{python}" -c "import os,sys; '
    "open(sys.argv[1], 'w').write('chardev' "
    "if (os.fstat(0).st_mode & 0o170000) == 0o020000 else 'pipe'); "
    "sys.stdout.write('fake bash ok')\" \"{marker}\"\n"
)

# Runs inside a child interpreter whose stdin is a PIPE held open by the
# test — the ACP stdio host shape. argv[1]=fake bash path, argv[2]=import
# root (the dir containing the modex_agent package).
_DRIVER = """
import json, sys
sys.path.insert(0, sys.argv[2])
from modex_agent.tools.terminal.types import _verify_bash
info = _verify_bash(sys.argv[1])
print(json.dumps({"verified": info is not None}))
"""


@pytest.mark.skipif(not _IS_WINDOWS, reason="fake bash.cmd harness is Windows-only")
def test_verify_bash_does_not_inherit_parent_stdin_pipe(tmp_path: Path) -> None:
    marker = tmp_path / "stdin_kind.txt"
    fake_bash = tmp_path / "bin" / "bash.cmd"
    fake_bash.parent.mkdir(parents=True, exist_ok=True)
    fake_bash.write_text(
        _FAKE_BASH_CMD.format(python=sys.executable, marker=marker),
        encoding="utf-8",
    )
    driver = tmp_path / "driver.py"
    driver.write_text(_DRIVER, encoding="utf-8")
    pkg_root = Path(terminal_types.__file__).resolve().parents[2]

    # Simulate the ACP stdio host: the interpreter running the probe has a
    # PIPE as its own stdin, with the write end held open by this test.
    child = subprocess.Popen(
        [sys.executable, str(driver), str(fake_bash), str(pkg_root)],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        cwd=str(tmp_path),
    )
    try:
        out, err = child.communicate(timeout=60)
    finally:
        if child.poll() is None:  # pragma: no cover - defensive
            child.kill()
            child.wait(timeout=10)

    assert child.returncode == 0, err
    assert '"verified": true' in out, err  # fake satisfied the bash probe
    # The probe must give the fake bash DEVNULL (char device), NOT the
    # inherited ACP-style pipe. This is the precondition broken by the bug
    # that reproduced the ACP-stdio new_session hang.
    assert marker.read_text(encoding="utf-8") == "chardev"
