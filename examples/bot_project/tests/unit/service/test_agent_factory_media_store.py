from __future__ import annotations

from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import pytest
from bot.service.pool import agent_factory as agent_factory_module

from modex_agent.media.store import LocalFileMediaStore


@pytest.mark.asyncio
async def test_agent_factory_wires_media_store_resolver_without_calling_it(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    builder = MagicMock()
    turn_runner = MagicMock(turn_context_builder=builder)
    instance = MagicMock()
    instance.pipeline._turn_runner = turn_runner

    class FakeFactory:
        def __init__(self, **_kwargs: Any) -> None:
            pass

        async def create_agent(self, *_args: Any, **_kwargs: Any) -> Any:
            return instance

    monkeypatch.setattr(agent_factory_module, "DefaultAgentFactory", FakeFactory)
    resolver = MagicMock(return_value=LocalFileMediaStore(tmp_path / "media"))
    factory = agent_factory_module._build_agent_factory(
        provider=None,
        tool_manager=None,
        inbox_server=None,
        inbox_consumer=None,
        shared_hooks=None,
        shared_hook_runner=None,
        shared_interceptor_chain=None,
        control_channel=None,
        workspace_resolver=None,
        pool_name="main",
        emitter_factory=None,
        media_store_resolver=resolver,
    )

    created = await factory.create_agent()

    assert created is instance
    assert builder.media_store_resolver is resolver
    resolver.assert_not_called()
