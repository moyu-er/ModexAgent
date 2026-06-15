"""Tests for adapter auto-discovery in WebUIService."""

from __future__ import annotations

import sys
import tempfile
import types
from pathlib import Path

import pytest

from bot.adapters import channels
from bot.service.web_ui_service import WebUIService


@pytest.fixture(autouse=True)
def _snapshot_adapters():
    """Save and restore the global ADAPTERS registry around each test."""
    original = list(channels.ADAPTERS)
    original_modules = dict(sys.modules)
    yield
    channels.ADAPTERS[:] = original
    # Remove any modules added by the discovery tests.
    for name in list(sys.modules):
        if name not in original_modules:
            del sys.modules[name]


def test_import_adapter_registration_modules_discovers_new_files():
    """Dropping a register_<name>.py file into bot/adapters/ auto-registers it."""
    with tempfile.TemporaryDirectory() as tmp:
        tmp_path = Path(tmp)
        fake_channels = types.ModuleType("fake_channels")
        fake_channels.__file__ = str(tmp_path / "channels.py")

        (tmp_path / "register_pp.py").write_text(
            "from bot.adapters.channels import register\n"
            "\n"
            "@register('pp', enabled=True)\n"
            "def build_pp(ctx):\n"
            "    return None\n",
            encoding="utf-8",
        )

        before = len(channels.ADAPTERS)
        WebUIService._import_adapter_registration_modules(fake_channels)
        after = len(channels.ADAPTERS)

        assert after == before + 1
        assert channels.ADAPTERS[-1].name == "pp"
        assert channels.ADAPTERS[-1].enabled is True


def test_import_adapter_registration_modules_skips_already_imported():
    """Modules already in sys.modules are not re-imported."""
    with tempfile.TemporaryDirectory() as tmp:
        tmp_path = Path(tmp)
        fake_channels = types.ModuleType("fake_channels")
        fake_channels.__file__ = str(tmp_path / "channels.py")

        (tmp_path / "register_once.py").write_text(
            "from bot.adapters.channels import register\n"
            "\n"
            "@register('once', enabled=True)\n"
            "def build_once(ctx):\n"
            "    return None\n",
            encoding="utf-8",
        )

        before = len(channels.ADAPTERS)
        WebUIService._import_adapter_registration_modules(fake_channels)
        first_addition = len(channels.ADAPTERS) - before

        # A second call should see the module already in sys.modules and skip it.
        WebUIService._import_adapter_registration_modules(fake_channels)
        second_addition = len(channels.ADAPTERS) - before

        assert first_addition == 1
        assert second_addition == 1


def test_import_adapter_registration_modules_logs_broken_modules():
    """A broken register_*.py file is logged and skipped, not crashed."""
    with tempfile.TemporaryDirectory() as tmp:
        tmp_path = Path(tmp)
        fake_channels = types.ModuleType("fake_channels")
        fake_channels.__file__ = str(tmp_path / "channels.py")

        (tmp_path / "register_broken.py").write_text(
            "raise RuntimeError('boom')\n",
            encoding="utf-8",
        )

        before = len(channels.ADAPTERS)
        WebUIService._import_adapter_registration_modules(fake_channels)
        after = len(channels.ADAPTERS)

        # The broken module must not add anything to the registry.
        assert after == before
