"""Tests for bot runtime defaults."""

from __future__ import annotations

import sys
from pathlib import Path
from typing import AsyncIterator

sys.path.insert(0, str(Path(__file__).parent.parent))

from bot.service.core import BotService
from framework.core.types import InputMessage
from framework.interceptor.builtin import (
    ToolResultLimitInterceptor,
    ToolTimeoutInterceptor,
    TurnTimeoutInterceptor,
)
from framework.pipeline.adapters import InputAdapter, NullOutputAdapter


class _InputAdapter(InputAdapter):
    @property
    def name(self) -> str:
        return "test"

    async def start(self) -> None:
        pass

    async def stop(self) -> None:
        pass

    async def receive(self) -> AsyncIterator[InputMessage]:
        if False:
            yield InputMessage(content="", session_id="s1")


def test_default_interceptor_chain_keeps_only_effective_defaults() -> None:
    service = BotService(
        config_dir=Path("examples/bot_project/config"),
        input_adapter=_InputAdapter(),
        output_adapter=NullOutputAdapter(),
        emitter_factory=lambda _session_id: None,
        config={"tools": {}},
    )

    chain = service._build_interceptor_chain()
    interceptors = chain.interceptors

    assert any(isinstance(item, ToolResultLimitInterceptor) for item in interceptors)
    assert not any(isinstance(item, TurnTimeoutInterceptor) for item in interceptors)
    assert not any(isinstance(item, ToolTimeoutInterceptor) for item in interceptors)
