"""Tests for PTY backends. Skips if platform libraries not installed."""

import importlib.util
import sys

import pytest


class TestTmuxBackendImport:
    @pytest.mark.skipif(sys.platform == "win32", reason="Unix only")
    def test_import(self) -> None:
        if importlib.util.find_spec("libtmux") is None:
            pytest.skip("libtmux not installed")

        from framework.tools.terminal.backends.tmux_pty import TmuxPtyBackend

        backend = TmuxPtyBackend()
        assert backend is not None


class TestBackendFactory:
    def test_factory_creates_backend(self) -> None:
        """Factory returns a backend for the current platform."""
        from framework.tools.terminal.backends.factory import create_pty_backend

        backend = create_pty_backend()
        assert backend is not None
