"""Test that BotService starts DreamEngine background loop.

Regression test for: BotService never starts DreamEngine, causing knowledge
files to never update even when archives accumulate.
"""

from __future__ import annotations

import asyncio
import contextlib
import sys
from pathlib import Path
from typing import AsyncIterator
from unittest.mock import AsyncMock, MagicMock

import pytest

_BOT_PROJECT = Path(__file__).parent.parent.parent.parent / "examples" / "bot_project"
if str(_BOT_PROJECT) not in sys.path:
    sys.path.insert(0, str(_BOT_PROJECT))

from framework.core.session_id import SessionId
from framework.core.types import InputMessage, OutputMessage
from framework.ioc.configs.app import AppConfig
from framework.ioc.configs.llm import LLMConfig
from framework.ioc.configs.memory import DreamEngineConfig, MemoryConfig
from framework.ioc.configs.pool import PoolConfig
from framework.pipeline.adapters import InputAdapter, OutputAdapter


class _StubInput(InputAdapter):
    name = "stub"

    async def start(self) -> None:
        pass

    async def stop(self) -> None:
        pass

    async def receive(self) -> AsyncIterator[InputMessage]:
        if False:
            yield InputMessage(content="", session=SessionId.from_str("", default_agent_name="main"))

    async def send_reply(self, msg: OutputMessage, session_id: str) -> None:
        pass


class _StubOutput(OutputAdapter):
    name = "stub"

    async def start(self) -> None:
        pass

    async def stop(self) -> None:
        pass

    async def send(self, msg: OutputMessage, session_id: str) -> None:
        pass

    async def send_streaming(self, stream, session_id: str) -> None:
        pass


@pytest.fixture
def pool_mode_config_with_dream() -> AppConfig:
    """AppConfig with pool mode and dream_engine enabled."""
    return AppConfig(
        llm=LLMConfig(model="test-model", api_key="k"),
        multi_agent=AppConfig.model_fields["multi_agent"].default,
        pools={
            "main": PoolConfig(
                llm=LLMConfig(model="test-model", api_key="k"),
                memory=MemoryConfig(
                    dream_engine=DreamEngineConfig(
                        enabled=True,
                        interval=1,
                        max_consume_per_run=3,
                    )
                ),
                agents=[{"name": "main", "role": "main", "max_steps": 5}],
            )
        },
    )


class TestBotServiceDreamEngineStartup:
    """Verify BotService starts DreamEngine background loop."""

    @pytest.mark.asyncio
    async def test_starts_dream_background_loop(
        self, pool_mode_config_with_dream: AppConfig
    ) -> None:
        """start() must create a background task for DreamEngine.

        Regression: Previously, start() only started PoolRouter
        but never called _dream_background_loop(), leaving DreamEngine idle
        and knowledge files never updating.
        """
        from bot.service.core import BotService

        bot = BotService(
            config_dir=Path("."),
            input_adapter=_StubInput(),
            output_adapter=_StubOutput(),
            emitter_factory=lambda s: None,
            app_config=pool_mode_config_with_dream,
        )

        # Initialize pools (normally done by initialize())
        bot.broker = MagicMock()
        bot.broker.start = AsyncMock()
        bot.broker.stop = AsyncMock()

        # Mock pool structures
        mock_pool = MagicMock()
        mock_pool.broker_bridge.start = AsyncMock()
        mock_pool.broker_bridge.stop = AsyncMock()
        bot._pools = {"main": mock_pool}

        # Mock pool_router
        bot.pool_router = MagicMock()
        bot.pool_router.run = AsyncMock()

        # Mock dream_engine and _dream_background_loop
        bot.dream_engine = MagicMock()
        bot.dream_engine.scan_all = AsyncMock(return_value=[])

        loop_started = asyncio.Event()
        original_dream_loop = bot._dream_background_loop

        async def patched_dream_loop(interval: int = 300) -> None:
            loop_started.set()
            # Don't run the real loop, just verify it was called
            await asyncio.sleep(0)

        bot._dream_background_loop = patched_dream_loop

        # Start (should trigger dream loop)
        start_task = asyncio.create_task(bot.start())

        # Wait a bit for start() to execute
        await asyncio.sleep(0.1)

        # Verify dream background loop was started
        assert loop_started.is_set(), (
            "start() did NOT start DreamEngine background loop. "
            "Regression: _dream_background_loop() was never called."
        )

        # Clean up
        bot._shutdown_event.set()
        start_task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await start_task

    @pytest.mark.asyncio
    async def test_without_dream_engine_does_not_crash(self) -> None:
        """BotService without dream_engine config should not crash."""
        from bot.service.core import BotService

        cfg = AppConfig(
            llm=LLMConfig(model="test-model", api_key="k"),
            multi_agent=AppConfig.model_fields["multi_agent"].default,
            pools={
                "main": PoolConfig(
                    llm=LLMConfig(model="test-model", api_key="k"),
                    agents=[{"name": "main", "role": "main", "max_steps": 5}],
                )
            },
        )

        bot = BotService(
            config_dir=Path("."),
            input_adapter=_StubInput(),
            output_adapter=_StubOutput(),
            emitter_factory=lambda s: None,
            app_config=cfg,
        )

        bot.broker = MagicMock()
        bot.broker.start = AsyncMock()
        bot.broker.stop = AsyncMock()

        mock_pool = MagicMock()
        mock_pool.broker_bridge.start = AsyncMock()
        mock_pool.broker_bridge.stop = AsyncMock()
        bot._pools = {"main": mock_pool}

        bot.pool_router = MagicMock()
        bot.pool_router.run = AsyncMock()

        # No dream_engine configured
        assert bot.dream_engine is None

        # Should not crash
        start_task = asyncio.create_task(bot.start())
        await asyncio.sleep(0.1)

        bot._shutdown_event.set()
        start_task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await start_task

    def test_has_dream_engine_methods(self) -> None:
        """BotService should expose DreamEngine lifecycle methods."""
        from bot.service.core import BotService

        bot = BotService(
            config_dir=Path("."),
            input_adapter=_StubInput(),
            output_adapter=_StubOutput(),
            emitter_factory=lambda s: None,
        )

        assert hasattr(bot, "_init_pool_dream_engine")
        assert hasattr(bot, "_dream_background_loop")


class TestBotServiceDreamEngineStop:
    """Verify BotService properly stops DreamEngine background loop."""

    @pytest.mark.asyncio
    async def test_stop_cancels_dream_task(self) -> None:
        """stop() must cancel the DreamEngine background task if running."""
        from bot.service.core import BotService

        bot = BotService(
            config_dir=Path("."),
            input_adapter=_StubInput(),
            output_adapter=_StubOutput(),
            emitter_factory=lambda s: None,
        )

        # Simulate a running dream task
        async def fake_dream_loop():
            await asyncio.sleep(3600)

        bot._dream_task = asyncio.create_task(fake_dream_loop())
        await asyncio.sleep(0.05)  # let it start

        # stop() should cancel the dream task
        await bot.stop()

        assert bot._dream_task.cancelled() or bot._dream_task.done()
