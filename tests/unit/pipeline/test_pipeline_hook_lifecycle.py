from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

from modex_agent.core.agent import Agent
from modex_agent.core.tool_manager import ToolManager
from modex_agent.hook import ClosableHook, HookRunner, HookSpec
from modex_agent.adapters.output import OutputAdapter
from modex_agent.pipeline.adapters import InputAdapter
from tests.unit.pipeline._helpers import _make_react_pipeline


class _RecordingClosableHook(ClosableHook):
    def __init__(self) -> None:
        self.closed = False

    async def aclose(self) -> None:
        self.closed = True


async def test_pipeline_stop_closes_hook_owned_resources() -> None:
    agent: Agent = MagicMock(spec=Agent)
    agent.stop = AsyncMock()
    tool_manager: ToolManager = MagicMock(spec=ToolManager)
    input_adapter: InputAdapter = MagicMock(spec=InputAdapter)
    output_adapter: OutputAdapter = MagicMock(spec=OutputAdapter)
    hook = _RecordingClosableHook()
    hook_runner = HookRunner([HookSpec(hook=hook)])
    pipeline = _make_react_pipeline(
        agent=agent,
        tool_manager=tool_manager,
        input_adapter=input_adapter,
        output_adapter=output_adapter,
        hook_runner=hook_runner,
    )

    await pipeline.stop()

    assert hook.closed is True
