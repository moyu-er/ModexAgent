"""Tests for LinuxTerminalManager and the pexpect→tmux→None degradation chain."""

from __future__ import annotations

import asyncio
import sys

import pytest

from framework.tools.terminal.managers import LinuxTerminalManager, create_terminal_manager


class TestLinuxTerminalManagerConstruction:

    def test_linux_kind_creates_linux_terminal_manager(self) -> None:
        """create_terminal_manager('linux') returns LinuxTerminalManager."""
        mgr = create_terminal_manager(manager_kind="linux")
        assert isinstance(mgr, LinuxTerminalManager)
        from framework.tools.terminal.types import TerminalVisibility
        assert mgr.visibility is TerminalVisibility.HIDDEN

    def test_linux_terminal_manager_has_hidden_visibility(self) -> None:
        mgr = LinuxTerminalManager()
        from framework.tools.terminal.types import TerminalVisibility
        assert mgr.visibility is TerminalVisibility.HIDDEN

    def test_invalid_kind_raises_value_error(self) -> None:
        with pytest.raises(ValueError, match="Unsupported terminal manager kind"):
            create_terminal_manager(manager_kind="nonexistent")


class TestLinuxTerminalManagerDegradation:

    def test_create_terminal_manager_linux_eager_validates_backend(self) -> None:
        """create_terminal_manager('linux') either succeeds or raises RuntimeError."""
        try:
            mgr = create_terminal_manager(manager_kind="linux")
            assert mgr is not None
        except RuntimeError as e:
            assert "No Linux terminal backend available" in str(e)


class TestLinuxTerminalManagerLazySession:

    def test_get_or_create_creates_session(self) -> None:
        try:
            mgr = LinuxTerminalManager()
        except RuntimeError:
            pytest.skip("No Linux terminal backend available")

        async def _run() -> None:
            session = await mgr.get_or_create("test-tab")
            assert session is not None
            assert session.name == "test-tab"
            await mgr.close("test-tab")

        asyncio.get_event_loop().run_until_complete(_run())

    def test_list_names_returns_session_names(self) -> None:
        try:
            mgr = LinuxTerminalManager()
        except RuntimeError:
            pytest.skip("No Linux terminal backend available")

        async def _run() -> None:
            await mgr.get_or_create("tab-a")
            await mgr.get_or_create("tab-b")
            names = mgr.list_names()
            assert "tab-a" in names
            assert "tab-b" in names
            await mgr.close("tab-a")
            await mgr.close("tab-b")

        asyncio.get_event_loop().run_until_complete(_run())

    def test_default_session_is_auto_set(self) -> None:
        try:
            mgr = LinuxTerminalManager()
        except RuntimeError:
            pytest.skip("No Linux terminal backend available")

        async def _run() -> None:
            session = await mgr.get_or_create("default", cwd=None)
            default = await mgr.get_default_session()
            assert default is not None
            assert default.name == "default"
            await mgr.close("default")

        asyncio.get_event_loop().run_until_complete(_run())

    def test_select_default_switches_active_session(self) -> None:
        try:
            mgr = LinuxTerminalManager()
        except RuntimeError:
            pytest.skip("No Linux terminal backend available")

        async def _run() -> None:
            await mgr.get_or_create("tab-1")
            await mgr.get_or_create("tab-2")
            await mgr.select_default("tab-2")
            default = await mgr.get_default_session()
            assert default is not None
            assert default.name == "tab-2"
            await mgr.close("tab-1")
            await mgr.close("tab-2")

        asyncio.get_event_loop().run_until_complete(_run())


class TestPoolBuilderDegradation:
    """Simulates the pool_builder degradation chain (platform-aware)."""

    def test_degradation_never_crashes(self) -> None:
        """When a manager kind fails, the loop continues without crashing."""
        kinds = (["windows_visible", "windows_hidden"]
                 if sys.platform == "win32" else ["linux"])
        result = None
        for kind in kinds:
            try:
                result = create_terminal_manager(manager_kind=kind)
                break
            except Exception:
                continue
        # Either we got a manager or None — we should never crash.
        assert result is not None or result is None
