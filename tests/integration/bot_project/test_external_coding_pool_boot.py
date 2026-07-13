"""Integration tests for bot-layer external_coding pool boot.

Validates T10 acceptance:
- pool_builder dispatches ``execution_strategy: external_coding`` to the
  external-coding builder.
- Provider availability gates main-agent registration.
- Cross-pool peer wiring lets the default main agent send to pool_pi.
"""

from __future__ import annotations

import asyncio
import shutil
from collections.abc import Callable
from pathlib import Path

import pytest

from modex_agent.core.agent import AgentCommKind, AgentContext
from modex_agent.core.emitter import AgentResult, ContentEmitter
from modex_agent.core.events import AgentEvent
from modex_agent.core.session_id import SessionInfo
from modex_agent.core.tool_manager import InMemoryToolManager
from modex_agent.memory.history import ListMessageHistory
from tests.integration.bot_project._external_coding_fixtures import (
    _MockInputAdapter,
    _MockOutputAdapter,
    _MockProvider,
)

pytestmark = pytest.mark.integration


class _NoopEmitter(ContentEmitter[AgentEvent]):
    """Emitter that does nothing; satisfies the factory type."""

    async def emit_delta(self, delta: str) -> None:
        pass

    async def emit_content(self, full_content: str) -> None:
        pass

    async def emit_complete(self, result: AgentResult) -> None:
        pass

    async def emit_error(self, error: str) -> None:
        pass


def _emitter_factory(session_id: str) -> ContentEmitter[AgentEvent]:
    return _NoopEmitter()


def _make_context(session_id: str) -> AgentContext:
    return AgentContext(
        system_prompt="test",
        history=ListMessageHistory([]),
        tool_manager=InMemoryToolManager(),
        session=SessionInfo.from_str(session_id),
        comm_kind=AgentCommKind.NORMAL,
    )


_ORIGINAL_WHICH = shutil.which


def _fake_which(provider_available: bool) -> Callable[..., str | None]:
    """Return a patched ``shutil.which`` that fakes the Pi CLI lookup."""

    def _which(cmd: str, mode: int = 1, path: str | None = None) -> str | None:
        if cmd == "pi":
            return "/usr/bin/pi" if provider_available else None
        return _ORIGINAL_WHICH(cmd, mode=mode, path=path)

    return _which


@pytest.mark.asyncio
async def test_pool_pi_boots_with_external_coding_strategy(
    bot_service_config: Path,
    mock_input_adapter: _MockInputAdapter,
    mock_output_adapter: _MockOutputAdapter,
) -> None:
    """When the Pi CLI is present, pool_pi's main agent is registered."""
    from unittest.mock import patch

    from bot.service.core import BotService

    with patch.object(BotService, "_project_dir", property(lambda self: bot_service_config.parent)), patch(
        "bot.service.pool_builder._build_llm_provider", return_value=_MockProvider()
    ), patch("bot.service.pool_builder._load_agent_mcp_tools", return_value=([], None)), patch(
        "shutil.which", side_effect=_fake_which(provider_available=True)
    ):
        service = BotService(
            config_dir=bot_service_config,
            input_adapter=mock_input_adapter,
            output_adapter=mock_output_adapter,
            emitter_factory=_emitter_factory,
        )
        await service.initialize()

        try:
            assert "default" in service._pools
            assert "pool_pi" in service._pools

            default_pi = service._pools["default"]
            pool_pi = service._pools["pool_pi"]

            # Default main agent has a peer target for pi.
            assert default_pi.target_store.has("pi")
            target = default_pi.target_store.get("pi")
            assert target is not None
            assert target.pool_name == "pool_pi"
            assert target.bus_ref is pool_pi.agent_bus

            # pool_pi main agent registered as a resident.
            resident_names = [d.address.name for d in pool_pi.pool.list_agents()]
            assert "pi" in resident_names

            # send_to_agent tool is present in the default main agent's tool set.
            assert "send_to_agent" in default_pi.tool_manager.list_tools()
        finally:
            await service.stop()


@pytest.mark.asyncio
async def test_pool_pi_skips_main_agent_when_provider_missing(
    bot_service_config: Path,
    mock_input_adapter: _MockInputAdapter,
    mock_output_adapter: _MockOutputAdapter,
) -> None:
    """Missing Pi CLI leaves the pool wired but without a resident main agent."""
    from unittest.mock import patch

    from bot.service.core import BotService

    with patch.object(BotService, "_project_dir", property(lambda self: bot_service_config.parent)), patch(
        "bot.service.pool_builder._build_llm_provider", return_value=_MockProvider()
    ), patch("bot.service.pool_builder._load_agent_mcp_tools", return_value=([], None)), patch(
        "shutil.which", side_effect=_fake_which(provider_available=False)
    ):
        service = BotService(
            config_dir=bot_service_config,
            input_adapter=mock_input_adapter,
            output_adapter=mock_output_adapter,
            emitter_factory=_emitter_factory,
        )
        await service.initialize()

        try:
            assert "default" in service._pools
            assert "pool_pi" in service._pools

            pool_pi = service._pools["pool_pi"]

            # No resident pi agent, but the pool and its bus/inbox exist.
            resident_names = [d.address.name for d in pool_pi.pool.list_agents()]
            assert "pi" not in resident_names

            # The communication tool is still wired so peer sends can land here.
            assert "send_to_agent" in pool_pi.tool_manager.list_tools()
        finally:
            await service.stop()


@pytest.mark.asyncio
async def test_default_pool_can_send_to_pool_pi_inbox(
    bot_service_config: Path,
    mock_input_adapter: _MockInputAdapter,
    mock_output_adapter: _MockOutputAdapter,
) -> None:
    """A peer send from default reaches pool_pi's inbox even when pi is not resident."""
    from unittest.mock import patch

    from bot.service.core import BotService

    with patch.object(BotService, "_project_dir", property(lambda self: bot_service_config.parent)), patch(
        "bot.service.pool_builder._build_llm_provider", return_value=_MockProvider()
    ), patch("bot.service.pool_builder._load_agent_mcp_tools", return_value=([], None)), patch(
        "shutil.which", side_effect=_fake_which(provider_available=False)
    ):
        service = BotService(
            config_dir=bot_service_config,
            input_adapter=mock_input_adapter,
            output_adapter=mock_output_adapter,
            emitter_factory=_emitter_factory,
        )
        await service.initialize()

        try:
            default_pi = service._pools["default"]
            pool_pi = service._pools["pool_pi"]

            target = default_pi.target_store.get("pi")
            assert target is not None, "default pool must know pi as a peer target"

            ctx = _make_context("conv.default")
            result = await default_pi.communication_service.send_async(
                target=target,
                content="hello pi",
                invocation_id=None,
                context=ctx,
            )
            assert "Error" not in result

            # Wait for the envelope to land in pool_pi's per-pool inbox.
            pending: set[str] = set()
            for _ in range(60):
                pending = set(await pool_pi.pool.sessions_with_pending())
                if any(s.endswith(".pi") for s in pending):
                    break
                await asyncio.sleep(0.05)

            assert any(
                s.endswith(".pi") for s in pending
            ), f"expected pool_pi inbox to have a .pi session, got {pending}"
        finally:
            await service.stop()
