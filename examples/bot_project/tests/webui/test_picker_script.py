"""Tests for the _PICKER_SCRIPT platform dispatch logic.

The picker script runs as ``python -c _PICKER_SCRIPT`` in a subprocess (see
:func:`bot.webui.routes.workspace.handle_workspace_pick`). These tests exec
the script string directly with mocked platform/subprocess/shutil to verify:

  - Darwin branch uses osascript; cancel normalizes to empty stdout + exit 0
  - Linux branch tries zenity -> kdialog -> tkinter with cancel stopping the chain
  - Windows branch is selected on Windows (regression guard)
  - Errors map to exit 1 + stderr (-> backend 503)

No real GUI is invoked; all OS-native pickers are mocked.
"""

from __future__ import annotations

import io
from subprocess import CompletedProcess
from unittest.mock import MagicMock, patch

from bot.webui.types import _PICKER_SCRIPT


def _cp(returncode: int = 0, stdout: str = "", stderr: str = "") -> CompletedProcess:
    """Build a CompletedProcess for mocking subprocess.run."""
    return CompletedProcess(args=[], returncode=returncode, stdout=stdout, stderr=stderr)


def _exec_picker(
    *,
    platform_system: str = "Darwin",
    run_side_effect=None,
    which_map: dict[str, str | None] | None = None,
    mock_tkinter: bool = False,
    tk_path: str = "",
    tk_side_effect=None,
) -> tuple[str, str, int | None]:
    """Exec _PICKER_SCRIPT with mocked platform/subprocess/shutil/tkinter.

    Returns (stdout_content, stderr_content, exit_code_or_None).
    ``exit_code`` is ``None`` when ``sys.exit`` was not called (script exited
    normally with implicit exit 0).
    """
    stdout = io.StringIO()
    stderr = io.StringIO()

    if run_side_effect is None:
        run_side_effect = [_cp(returncode=1, stderr="unexpected subprocess.run call")]

    def _which_side_effect(name: str):
        if which_map is not None:
            return which_map.get(name)
        return None

    patches = [
        patch("platform.system", return_value=platform_system),
        patch("sys.stdout", new=stdout),
        patch("sys.stderr", new=stderr),
        patch("subprocess.run", side_effect=run_side_effect),
        patch("shutil.which", side_effect=_which_side_effect),
    ]

    if mock_tkinter:
        if tk_side_effect:
            patches.append(patch("tkinter.Tk", side_effect=tk_side_effect))
        else:
            mock_root = MagicMock()
            patches.append(patch("tkinter.Tk", return_value=mock_root))
            patches.append(patch("tkinter.filedialog.askdirectory", return_value=tk_path))

    for p in patches:
        p.start()
    try:
        try:
            exec(_PICKER_SCRIPT, {"__name__": "__main__"})
            exit_code: int | None = None
        except SystemExit as e:
            exit_code = e.code if isinstance(e.code, int) else 1
    finally:
        for p in patches:
            p.stop()

    return stdout.getvalue(), stderr.getvalue(), exit_code


# ── Darwin (macOS) branch ───────────────────────────────────────────────────


def test_darwin_selected_path_written_to_stdout():
    stdout, stderr, exit_code = _exec_picker(
        platform_system="Darwin",
        run_side_effect=[_cp(0, stdout="/Users/test/myproject\n")],
    )
    assert stdout == "/Users/test/myproject"
    assert stderr == ""
    assert exit_code is None


def test_darwin_user_cancel_normalizes_to_empty_stdout_exit_0():
    # osascript cancel produces exit 1 + localized stderr with -128
    # (e.g. "用户已取消。 (-128)" / "User canceled. (-128)"). The -128 code
    # is the only cross-localization signal.
    stdout, stderr, exit_code = _exec_picker(
        platform_system="Darwin",
        run_side_effect=[_cp(1, stdout="", stderr="execution error: User canceled. (-128)")],
    )
    assert stdout == ""
    assert stderr == ""
    assert exit_code is None


def test_darwin_cancel_with_chinese_locale_normalizes():
    stdout, stderr, exit_code = _exec_picker(
        platform_system="Darwin",
        run_side_effect=[_cp(1, stdout="", stderr="execution error: 用户已取消。 (-128)")],
    )
    assert stdout == ""
    assert stderr == ""
    assert exit_code is None


def test_darwin_permission_denied_maps_to_exit_1():
    stdout, stderr, exit_code = _exec_picker(
        platform_system="Darwin",
        run_side_effect=[_cp(1, stdout="", stderr="Not authorized to send Apple events.")],
    )
    assert stdout == ""
    assert "Not authorized" in stderr
    assert exit_code == 1


def test_darwin_osascript_not_found_maps_to_exit_1():
    stdout, stderr, exit_code = _exec_picker(
        platform_system="Darwin",
        run_side_effect=FileNotFoundError("osascript not found"),
    )
    assert stdout == ""
    assert "osascript not found" in stderr
    assert exit_code == 1


# ── Linux branch ────────────────────────────────────────────────────────────


def test_linux_zenity_selected_writes_path():
    stdout, stderr, exit_code = _exec_picker(
        platform_system="Linux",
        run_side_effect=[_cp(0, stdout="/home/user/project\n")],
        which_map={"zenity": "/usr/bin/zenity", "kdialog": "/usr/bin/kdialog"},
    )
    assert stdout == "/home/user/project"
    assert exit_code is None


def test_linux_zenity_cancel_stops_chain():
    stdout, stderr, exit_code = _exec_picker(
        platform_system="Linux",
        run_side_effect=[_cp(1, stdout="", stderr="")],
        which_map={"zenity": "/usr/bin/zenity", "kdialog": "/usr/bin/kdialog"},
    )
    assert stdout == ""
    assert exit_code is None


def test_linux_zenity_not_installed_falls_to_kdialog():
    stdout, stderr, exit_code = _exec_picker(
        platform_system="Linux",
        run_side_effect=[_cp(0, stdout="/home/user/project\n")],
        which_map={"zenity": None, "kdialog": "/usr/bin/kdialog"},
    )
    assert stdout == "/home/user/project"
    assert exit_code is None


def test_linux_zenity_error_falls_to_kdialog():
    stdout, stderr, exit_code = _exec_picker(
        platform_system="Linux",
        run_side_effect=[
            _cp(-1, stdout="", stderr="zenity crashed"),
            _cp(0, stdout="/home/user/project\n"),
        ],
        which_map={"zenity": "/usr/bin/zenity", "kdialog": "/usr/bin/kdialog"},
    )
    assert stdout == "/home/user/project"
    assert exit_code is None


def test_linux_kdialog_cancel_stops_chain():
    stdout, stderr, exit_code = _exec_picker(
        platform_system="Linux",
        run_side_effect=[_cp(1, stdout="", stderr="")],
        which_map={"zenity": None, "kdialog": "/usr/bin/kdialog"},
    )
    assert stdout == ""
    assert exit_code is None


def test_linux_neither_installed_falls_to_tkinter_success():
    stdout, stderr, exit_code = _exec_picker(
        platform_system="Linux",
        which_map={"zenity": None, "kdialog": None},
        mock_tkinter=True,
        tk_path="/home/user/project",
    )
    assert stdout == "/home/user/project"
    assert exit_code is None


def test_linux_tkinter_cancel_maps_to_empty_stdout_exit_0():
    stdout, stderr, exit_code = _exec_picker(
        platform_system="Linux",
        which_map={"zenity": None, "kdialog": None},
        mock_tkinter=True,
        tk_path="",
    )
    assert stdout == ""
    assert exit_code is None


def test_linux_all_tools_fail_maps_to_exit_1():
    stdout, stderr, exit_code = _exec_picker(
        platform_system="Linux",
        which_map={"zenity": None, "kdialog": None},
        mock_tkinter=True,
        tk_side_effect=RuntimeError("Can't find a usable init.tcl"),
    )
    assert stdout == ""
    assert "init.tcl" in stderr
    assert exit_code == 1


# ── Windows branch (regression guard) ───────────────────────────────────────


def test_windows_selected_path_uses_tkinter_not_subprocess():
    stdout, stderr, exit_code = _exec_picker(
        platform_system="Windows",
        mock_tkinter=True,
        tk_path="C:\\Users\\test\\project",
    )
    assert stdout == "C:\\Users\\test\\project"
    assert exit_code is None


def test_windows_tkinter_cancel_maps_to_empty_stdout():
    stdout, stderr, exit_code = _exec_picker(
        platform_system="Windows",
        mock_tkinter=True,
        tk_path="",
    )
    assert stdout == ""
    assert exit_code is None
