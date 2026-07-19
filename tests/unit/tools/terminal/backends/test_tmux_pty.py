"""Unit tests for ``TmuxPtyBackend`` — ADR-0032 D5 snapshot backend fix.

Exercises:
- ``_diff_output`` prefix-match semantics (Fix 1) including the 60-line
  scroll regression case.
- ``is_alive`` 1-second TTL cache (Fix 2) and cache invalidation on
  ``terminate`` / ``kill``.
- D4 convergence: ``_shell_family`` implemented; ``clear_input_line``
  inherited from base; ``_uses_readline`` deleted.
- D5 snapshot shape: ``write`` / ``read`` / ``read_pending`` /
  ``current_segment`` / ``drain_startup`` overrides retained;
  ``_write_blocking`` / ``_read_blocking`` hooks NOT implemented
  (no hooks — snapshot I/O model).
- ``read`` invokes ``capture_pane("-S", "-")`` for full scrollback.

Mock-based: a fake ``libtmux`` module is installed into ``sys.modules``
before ``TmuxPtyBackend`` is imported, so the suite runs on any platform
without a real tmux/libtmux install.
"""

from __future__ import annotations

import sys
import time
from unittest.mock import MagicMock

import pytest

# Install a fake ``libtmux`` module before importing the backend. The
# backend's ``__init__`` does ``import libtmux`` eagerly and calls
# ``libtmux.Server()``. ``side_effect=lambda: MagicMock()`` ensures each
# ``Server()`` call returns a fresh MagicMock so tests don't share state.
if "libtmux" not in sys.modules:
    _fake_libtmux = MagicMock()
    _fake_libtmux.Server = MagicMock(side_effect=lambda: MagicMock())
    sys.modules["libtmux"] = _fake_libtmux

from modex_agent.tools.terminal.backends.tmux_pty import TmuxPtyBackend
from modex_agent.tools.terminal.types import ShellFamily


def _make_backend() -> TmuxPtyBackend:
    """Construct a backend without calling ``start()`` (no real tmux needed)."""
    return TmuxPtyBackend()


def _make_backend_with_pane() -> tuple[TmuxPtyBackend, MagicMock]:
    """Construct a backend with a mocked ``_pane`` for I/O tests."""
    backend = _make_backend()
    mock_pane: MagicMock = MagicMock()
    backend._pane = mock_pane
    return backend, mock_pane


class _FakeServer:
    """Stand-in for ``libtmux.Server`` whose ``sessions`` property is observable.

    Counts property accesses so tests can assert whether ``is_alive``
    queried the (real or mocked) session list, distinguishing a cache
    hit (no access) from a cache miss (one access).
    """

    def __init__(self, sessions_list: list[object]) -> None:
        self._sessions_list = sessions_list
        self.access_count = 0

    @property
    def sessions(self) -> list[object]:
        self.access_count += 1
        return self._sessions_list


class _ExplodingServer:
    """Server whose ``sessions`` property always raises."""

    @property
    def sessions(self) -> list[object]:
        raise RuntimeError("tmux died")


# ── _diff_output prefix-match (Fix 1) ──


def test_diff_output_empty_previous_returns_all_current() -> None:
    """Empty previous snapshot → entire current snapshot is new output."""
    backend = _make_backend()
    assert backend._diff_output("", "line1\nline2\nline3") == "line1\nline2\nline3"


def test_diff_output_prefix_match_returns_suffix() -> None:
    """Previous is a line-prefix of current → return the suffix as new output."""
    backend = _make_backend()
    prev = "line1\nline2\nline3"
    curr = "line1\nline2\nline3\nline4\nline5"
    assert backend._diff_output(prev, curr) == "line4\nline5"


def test_diff_output_identical_returns_empty() -> None:
    """Previous == current → no new output."""
    backend = _make_backend()
    text = "line1\nline2\nline3"
    assert backend._diff_output(text, text) == ""


def test_diff_output_60_line_scroll_regression() -> None:
    """Regression: 60-line command on a 30-row pane — no duplicates.

    Before Fix 1, ``capture_pane()`` returned only the visible 30-row
    window. After the first 30 lines, the snapshot shifted; tail-match
    against the previous 30-row snapshot failed and the diff returned
    the entire current snapshot as new output — producing duplicates.

    With ``capture-pane -p -S -`` (full scrollback) and prefix-match,
    the previous 30 lines are a prefix of the current 60-line
    scrollback snapshot, so the diff returns only lines 31-60.
    """
    backend = _make_backend()
    prev_lines = [f"line {i}" for i in range(1, 31)]
    curr_lines = [f"line {i}" for i in range(1, 61)]
    prev = "\n".join(prev_lines)
    curr = "\n".join(curr_lines)
    expected = "\n".join(curr_lines[30:])
    assert backend._diff_output(prev, curr) == expected


def test_diff_output_identical_60_lines_returns_empty() -> None:
    """60-line previous == 60-line current → no new output."""
    backend = _make_backend()
    text = "\n".join(f"line {i}" for i in range(1, 61))
    assert backend._diff_output(text, text) == ""


def test_diff_output_non_prefix_falls_back_to_all_current() -> None:
    """Prefix-match failure (content changed completely) → entire current snapshot.

    This is the fallback path: output scrolled beyond the 2000-line
    scrollback between two ``capture_pane`` calls, so the previous
    snapshot's lines are no longer a prefix of the current snapshot.
    Returning the entire current snapshot is the conservative fallback
    (same as today's tail-match behavior, but only in the genuine edge
    case rather than on every >30-line command).
    """
    backend = _make_backend()
    prev = "alpha\nbeta\ngamma"
    curr = "delta\nepsilon\nzeta"
    assert backend._diff_output(prev, curr) == "delta\nepsilon\nzeta"


def test_diff_output_previous_longer_than_current_falls_back() -> None:
    """Previous has more lines than current (pane cleared) → all current."""
    backend = _make_backend()
    prev = "line1\nline2\nline3"
    curr = "line1"
    assert backend._diff_output(prev, curr) == "line1"


# ── is_alive 1-second TTL cache (Fix 2) ──


async def test_is_alive_false_when_session_name_none() -> None:
    """``is_alive`` returns False without querying when ``_session_name`` is None."""
    backend = _make_backend()
    backend._session_name = None
    backend._server = _ExplodingServer()  # Would raise if queried.
    assert await backend.is_alive() is False


async def test_is_alive_first_call_queries_and_caches() -> None:
    """First ``is_alive`` call queries ``server.sessions`` and caches the result."""
    backend = _make_backend()
    backend._session_name = "agent_test"
    fake_session = MagicMock()
    fake_session.name = "agent_test"
    fake_server = _FakeServer([fake_session])
    backend._server = fake_server
    assert await backend.is_alive() is True
    assert fake_server.access_count == 1
    assert backend._alive_cache is not None
    cached_at, cached_bool = backend._alive_cache
    assert cached_bool is True


async def test_is_alive_second_call_within_ttl_returns_cached() -> None:
    """Second call within 1-second TTL returns cached value without querying."""
    backend = _make_backend()
    backend._session_name = "agent_test"
    fake_session = MagicMock()
    fake_session.name = "agent_test"
    fake_server = _FakeServer([fake_session])
    backend._server = fake_server
    await backend.is_alive()  # queries + caches
    assert fake_server.access_count == 1
    # Second call immediately — within 1s TTL.
    assert await backend.is_alive() is True
    assert fake_server.access_count == 1  # No additional query.


async def test_is_alive_call_after_ttl_requeries() -> None:
    """Call after 1-second TTL re-queries and refreshes the cache."""
    backend = _make_backend()
    backend._session_name = "agent_test"
    fake_session = MagicMock()
    fake_session.name = "agent_test"
    fake_server = _FakeServer([fake_session])
    backend._server = fake_server
    # Pre-seed an expired cache entry (2 seconds old).
    backend._alive_cache = (time.monotonic() - 2.0, False)
    assert await backend.is_alive() is True
    assert fake_server.access_count == 1  # Re-queried because cache was expired.
    cached_at, cached_bool = backend._alive_cache
    assert cached_bool is True
    assert time.monotonic() - cached_at < 1.0


async def test_is_alive_exception_returns_false_and_caches_false() -> None:
    """``server.sessions`` raising → ``is_alive`` returns False and caches False."""
    backend = _make_backend()
    backend._session_name = "agent_test"
    backend._server = _ExplodingServer()
    assert await backend.is_alive() is False
    assert backend._alive_cache is not None
    _, cached_bool = backend._alive_cache
    assert cached_bool is False


async def test_is_alive_no_match_returns_false() -> None:
    """``is_alive`` returns False when no session matches ``_session_name``."""
    backend = _make_backend()
    backend._session_name = "agent_test"
    other_session = MagicMock()
    other_session.name = "other_session"
    backend._server = _FakeServer([other_session])
    assert await backend.is_alive() is False


async def test_terminate_invalidates_alive_cache() -> None:
    """``terminate`` clears ``_alive_cache`` so the next call re-queries."""
    backend = _make_backend()
    backend._session_name = "agent_test"
    backend._session = MagicMock()
    fake_session = MagicMock()
    fake_session.name = "agent_test"
    backend._server = _FakeServer([fake_session])
    await backend.is_alive()
    assert backend._alive_cache is not None
    await backend.terminate()
    assert backend._alive_cache is None


async def test_kill_invalidates_alive_cache() -> None:
    """``kill`` (which delegates to ``terminate``) clears ``_alive_cache``."""
    backend = _make_backend()
    backend._session_name = "agent_test"
    backend._session = MagicMock()
    fake_session = MagicMock()
    fake_session.name = "agent_test"
    backend._server = _FakeServer([fake_session])
    await backend.is_alive()
    assert backend._alive_cache is not None
    await backend.kill()
    assert backend._alive_cache is None


# ── _shell_family hook (D4 convergence) ──


@pytest.mark.parametrize(
    "shell_path, expected",
    [
        ("/usr/bin/bash", ShellFamily.BASH),
        ("/usr/bin/zsh", ShellFamily.ZSH),
        ("/bin/sh", ShellFamily.SH),
        ("bash", ShellFamily.BASH),
        ("zsh", ShellFamily.ZSH),
        ("sh", ShellFamily.SH),
        ("cmd.exe", ShellFamily.CMD),
    ],
)
def test_shell_family_detected_from_path(shell_path: str, expected: ShellFamily) -> None:
    """``_shell_family`` returns the correct ``ShellFamily`` for known paths.

    Note: ``_family_from_path`` (in read-only ``types.py``) does not strip
    ``.exe`` suffixes, so ``bash.exe`` falls through to ``SH``. This test
    uses paths the production helper actually recognises.
    """
    backend = _make_backend()
    backend._shell = shell_path
    assert backend._shell_family() == expected


def test_shell_family_defaults_to_sh_when_shell_none() -> None:
    """``_shell_family`` falls back to ``SH`` when ``_shell`` is ``None``."""
    backend = _make_backend()
    backend._shell = None
    assert backend._shell_family() == ShellFamily.SH


def test_shell_family_defaults_to_sh_for_unknown_shell() -> None:
    """``_shell_family`` falls back to ``SH`` for unrecognized shell names."""
    backend = _make_backend()
    backend._shell = "/usr/local/bin/fish"
    assert backend._shell_family() == ShellFamily.SH


# ── Structural inheritance (the D5 contract assertion) ──


def test_backend_overrides_write() -> None:
    """``write`` IS overridden (D5 snapshot backend — ``send_keys(enter=False)``)."""
    assert "write" in vars(TmuxPtyBackend)


def test_backend_overrides_read() -> None:
    """``read`` IS overridden (snapshot diff model — ``read_pending`` calls ``self.read``)."""
    assert "read" in vars(TmuxPtyBackend)


def test_backend_overrides_read_pending() -> None:
    """``read_pending`` IS overridden (D5 snapshot backend)."""
    assert "read_pending" in vars(TmuxPtyBackend)


def test_backend_overrides_current_segment() -> None:
    """``current_segment`` IS overridden (D5 — uses ``capture_pane``)."""
    assert "current_segment" in vars(TmuxPtyBackend)


def test_backend_overrides_drain_startup() -> None:
    """``drain_startup`` IS overridden (D5 — ``capture_pane``-based prompt detection)."""
    assert "drain_startup" in vars(TmuxPtyBackend)


def test_backend_does_not_override_clear_input_line() -> None:
    """``clear_input_line`` is inherited from ``TerminalBackend`` (D4 convergence)."""
    assert "clear_input_line" not in vars(TmuxPtyBackend)


def test_backend_implements_shell_family_hook() -> None:
    """``_shell_family`` hook is implemented on this backend."""
    assert "_shell_family" in vars(TmuxPtyBackend)


def test_backend_does_not_have_uses_readline_helper() -> None:
    """``_uses_readline`` private helper is absent (replaced by ``_shell_family``)."""
    assert "_uses_readline" not in vars(TmuxPtyBackend)


def test_backend_does_not_implement_write_blocking_hook() -> None:
    """``_write_blocking`` hook is NOT implemented (D5 — no hooks, snapshot I/O)."""
    assert "_write_blocking" not in vars(TmuxPtyBackend)


def test_backend_does_not_implement_read_blocking_hook() -> None:
    """``_read_blocking`` hook is NOT implemented (D5 — no hooks, snapshot I/O)."""
    assert "_read_blocking" not in vars(TmuxPtyBackend)


# ── capture-pane uses -S - (full scrollback, Fix 1) ──


async def test_read_uses_full_scrollback_capture() -> None:
    """``read`` calls ``capture_pane("-S", "-")`` for full scrollback (Fix 1)."""
    backend, mock_pane = _make_backend_with_pane()
    backend._last_capture = ""  # Initialize so we go through the diff path.
    mock_pane.capture_pane.return_value = ["line1", "line2"]
    await backend.read(timeout=0.0)
    mock_pane.capture_pane.assert_called_once_with("-S", "-")


# ── Inherited clear_input_line behavior (D4 convergence) ──


async def test_clear_input_line_writes_ctrl_a_ctrl_k_for_bash() -> None:
    """Inherited ``clear_input_line`` sends ``\\x01\\x0b`` via ``send_keys`` for bash."""
    backend, mock_pane = _make_backend_with_pane()
    backend._shell = "/usr/bin/bash"
    await backend.clear_input_line()
    mock_pane.send_keys.assert_called_once_with("\x01\x0b", enter=False)


async def test_clear_input_line_writes_ctrl_a_ctrl_k_for_zsh() -> None:
    """Inherited ``clear_input_line`` sends ``\\x01\\x0b`` for zsh."""
    backend, mock_pane = _make_backend_with_pane()
    backend._shell = "zsh"
    await backend.clear_input_line()
    mock_pane.send_keys.assert_called_once_with("\x01\x0b", enter=False)


async def test_clear_input_line_is_noop_for_cmd() -> None:
    """Inherited ``clear_input_line`` is a no-op for non-readline shells (cmd)."""
    backend, mock_pane = _make_backend_with_pane()
    backend._shell = "cmd.exe"
    await backend.clear_input_line()
    mock_pane.send_keys.assert_not_called()
