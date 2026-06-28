"""Factory capability table — ADR-0010 supports (transport, visibility) combos.

Unsupported combinations must raise UnsupportedVisibilityForTransport rather
than silently falling back. This test pins the contract before any backend
is renamed.
"""

from __future__ import annotations

import pytest

from modex_agent.tools.terminal.backends.factory import (
    UnsupportedVisibilityForTransport,
    create_pty_backend,
)
from modex_agent.tools.terminal.types import TerminalVisibility


class TestFactoryCapabilityTable:
    def test_create_with_visibility_hidden_returns_backend_on_supported_platforms(self) -> None:
        # On any platform, HIDDEN is supported (windows_hidden on Win, pexpect/tmux on Unix)
        backend = create_pty_backend(visibility=TerminalVisibility.HIDDEN)
        assert backend.visibility == TerminalVisibility.HIDDEN

    def test_unsupported_combination_raises_typed_exception(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # Force the factory to consult the table for a combo that it cannot serve
        # (the factory will reject (pexpect, VISIBLE) regardless of platform).
        # We monkeypatch sys.platform to 'linux' so pexpect would be the preferred
        # transport; then we ask for VISIBLE, which pexpect cannot serve.
        # This is more robust than stubbing builtins.__import__ because:
        # (a) optional-dependency installs in CI envs do not silently re-allow libtmux
        #     and break the test, and (b) we directly exercise the factory's
        #     rejection logic rather than the I/O behaviour of __import__.
        import sys

        from modex_agent.tools.terminal.backends import factory

        monkeypatch.setattr(sys, "platform", "linux")
        monkeypatch.setattr(factory, "_is_libtmux_available", lambda: False)

        with pytest.raises(UnsupportedVisibilityForTransport) as exc_info:
            create_pty_backend(visibility=TerminalVisibility.VISIBLE)
        assert "pexpect" in str(exc_info.value).lower() or "visible" in str(exc_info.value).lower()
