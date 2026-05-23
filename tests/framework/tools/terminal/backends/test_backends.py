"""Tests for PTY backends. Skips if platform libraries not installed."""

import sys

import pytest


class TestWindowsPtyBackend:
    @pytest.mark.skipif(sys.platform != "win32", reason="Windows only")
    def test_import(self):
        try:
            import pywinpty
        except ImportError:
            pytest.skip("pywinpty not installed")
        from framework.tools.terminal.backends.windows_pty import WindowsPtyBackend
        backend = WindowsPtyBackend()
        assert backend is not None


class TestUnixPtyBackend:
    @pytest.mark.skipif(sys.platform == "win32", reason="Unix only")
    def test_import(self):
        try:
            import pexpect
        except ImportError:
            pytest.skip("pexpect not installed")
        from framework.tools.terminal.backends.unix_pty import UnixPtyBackend
        backend = UnixPtyBackend()
        assert backend is not None
