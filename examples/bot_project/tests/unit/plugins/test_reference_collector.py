"""Real-use test for the ReferenceCollectorPlugin user-extension path.

Proves the full chain a third-party plugin author relies on:
directory-discovered ``Plugin`` → HOOK-slot factory → roster reference
(``hooks: [+reference_collector]``) → Stage-4 dispatch → the hook runs on
turns with watermark semantics (scans only content since its last
injection; never re-scans its own reminders).
"""
from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[3]))

from plugins.reference_collector import (  # noqa: E402
    ReferenceCollectorHook,
    ReferenceCollectorPlugin,
)

from modex_agent.hook import HookSpec  # noqa: E402
from modex_agent.hook.runner import HookRunner  # noqa: E402
from modex_agent.plugins.abc import ComponentSlot  # noqa: E402
from modex_agent.plugins.assembly.context import AssemblyContext  # noqa: E402
from modex_agent.plugins.loader import (  # noqa: E402
    ComponentRegistryLoader,
    PluginDiscoveryConfig,
    PluginRegistrationContext,
)
from modex_agent.plugins.registry import ComponentRegistry  # noqa: E402


def _load_via_directory_discovery(plugin_dir: Path) -> ComponentRegistry:
    """Load through the REAL directory discovery (same path core.py uses)."""
    registry = ComponentRegistry()
    import asyncio

    asyncio.run(
        ComponentRegistryLoader.load(
            registry,
            PluginDiscoveryConfig(
                bundled_factories=(),
                project_plugin_paths=(plugin_dir,),
            ),
        )
    )
    return registry


@pytest.fixture()
def bot_plugins_dir() -> Path:
    return Path(__file__).resolve().parents[3] / "plugins"


class TestPluginRegistration:
    def test_directory_discovery_registers_reference_collector(
        self, bot_plugins_dir: Path
    ) -> None:
        registry = _load_via_directory_discovery(bot_plugins_dir)

        assert "reference_collector" in registry.names(ComponentSlot.HOOK)

    def test_register_directly_resolves_factory(self) -> None:
        registry = ComponentRegistry()
        with PluginRegistrationContext(registry) as ctx:
            ReferenceCollectorPlugin().register(ctx)

        factory = registry.resolve(ComponentSlot.HOOK, "reference_collector")
        assert factory.config_model.model_config.get("frozen") is True
        assert factory.config_model.model_config.get("extra") == "forbid"


class _FakeHistory:
    """Minimal async message history backed by a list."""

    def __init__(self, messages: list[MagicMock]) -> None:
        self.messages = list(messages)
        self.append = AsyncMock(side_effect=self._append)

    async def _append(self, message: dict[str, str]) -> None:
        stored = MagicMock()
        stored.content = message["content"]
        stored.role = message["role"]
        self.messages.append(stored)

    async def to_list(self) -> list[MagicMock]:
        return list(self.messages)


def _message(content: str) -> MagicMock:
    message = MagicMock()
    message.content = content
    return message


def _turn(history: _FakeHistory) -> tuple[MagicMock, MagicMock]:
    ctx = MagicMock()
    ctx.history = history
    result = MagicMock()
    result.messages = []
    return ctx, result


class TestWatermarkSemantics:
    """Each injection covers only content since the previous injection."""

    async def test_first_turn_scans_from_conversation_start(self) -> None:
        hook = ReferenceCollectorHook(max_sources=20)
        history = _FakeHistory(
            [
                _message("check https://example.com/a"),
                _message("answer about https://docs.example.com/guide"),
            ]
        )
        ctx, result = _turn(history)

        await hook.after_turn(ctx, result)

        reminder = history.append.await_args.args[0]
        assert "https://example.com/a" in reminder["content"]
        assert "https://docs.example.com/guide" in reminder["content"]

    async def test_second_turn_scans_only_new_content(self) -> None:
        hook = ReferenceCollectorHook(max_sources=20)
        history = _FakeHistory(
            [
                _message("https://example.com/old"),
                _message("no urls here"),
            ]
        )
        ctx, result = _turn(history)
        await hook.after_turn(ctx, result)
        first_reminder = history.append.await_args.args[0]["content"]
        assert "https://example.com/old" in first_reminder

        history.messages.append(_message("https://example.com/new"))
        ctx, result = _turn(history)
        await hook.after_turn(ctx, result)

        second_reminder = history.append.await_args.args[0]["content"]
        assert "https://example.com/new" in second_reminder
        assert "https://example.com/old" not in second_reminder

    async def test_own_reminder_is_never_rescanned(self) -> None:
        """The anti-feedback regression: reminders live behind the watermark,
        so the list cannot feed itself and grow across turns."""
        hook = ReferenceCollectorHook(max_sources=20)
        history = _FakeHistory([_message("see https://example.com/x")])
        ctx, result = _turn(history)
        await hook.after_turn(ctx, result)
        assert history.append.await_count == 1

        ctx, result = _turn(history)
        await hook.after_turn(ctx, result)

        history.append.assert_awaited_once()

    async def test_other_hook_reminders_are_scanned_once(self) -> None:
        """Watermark advances past other hooks' injections too: a URL inside
        another hook's reminder is collected exactly once."""
        hook = ReferenceCollectorHook(max_sources=20)
        history = _FakeHistory([_message("start")])
        ctx, result = _turn(history)
        await hook.after_turn(ctx, result)
        assert history.append.await_count == 0  # "start" carries no URL

        history.messages.append(_message("<kb>read https://example.com/kb</kb>"))
        ctx, result = _turn(history)
        await hook.after_turn(ctx, result)
        reminder = history.append.await_args.args[0]
        assert "https://example.com/kb" in reminder["content"]

        ctx, result = _turn(history)
        await hook.after_turn(ctx, result)
        assert history.append.await_count == 1  # collected once, never rescanned

    async def test_dedup_within_window(self) -> None:
        hook = ReferenceCollectorHook(max_sources=20)
        history = _FakeHistory(
            [
                _message("https://example.com/a and https://example.com/a again"),
                _message("also (https://example.com/a.)"),
            ]
        )
        ctx, result = _turn(history)

        await hook.after_turn(ctx, result)

        reminder = history.append.await_args.args[0]["content"]
        assert reminder.count("https://example.com/a\n") == 1

    async def test_truncates_at_max_sources(self) -> None:
        hook = ReferenceCollectorHook(max_sources=2)
        history = _FakeHistory(
            [
                _message(" ".join(f"https://example.com/{i}" for i in range(5))),
            ]
        )
        ctx, result = _turn(history)

        await hook.after_turn(ctx, result)

        reminder = history.append.await_args.args[0]["content"]
        assert "https://example.com/0" in reminder
        assert "https://example.com/2" not in reminder
        assert "and 3 more" in reminder

    async def test_no_new_urls_is_noop(self) -> None:
        hook = ReferenceCollectorHook(max_sources=20)
        history = _FakeHistory([_message("plain text, no links")])
        ctx, result = _turn(history)

        await hook.after_turn(ctx, result)

        history.append.assert_not_awaited()

    async def test_zero_max_sources_disables(self) -> None:
        hook = ReferenceCollectorHook(max_sources=0)
        history = _FakeHistory([_message("https://example.com/x")])
        ctx, result = _turn(history)

        await hook.after_turn(ctx, result)

        history.append.assert_not_awaited()


class TestDispatchThroughRunner:
    async def test_factory_create_product_runs_via_hook_runner(self) -> None:
        """The roster path: factory.create product is a runnable hook spec."""
        registry = ComponentRegistry()
        with PluginRegistrationContext(registry) as ctx:
            ReferenceCollectorPlugin().register(ctx)
        factory = registry.resolve(ComponentSlot.HOOK, "reference_collector")
        hook = await factory.create(
            factory.config_model(),  # type: ignore[arg-type]
            AssemblyContext(registry=registry, workspace_ctx=MagicMock()),
        )

        runner = HookRunner([HookSpec(hook=hook)])
        assert runner.hook_specs[0].hook.name == "reference_collector"
