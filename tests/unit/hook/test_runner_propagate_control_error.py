"""HookRunner must propagate AgentControlError instead of swallowing it."""
import pytest

from modex_agent.control.exceptions import AgentCancelled, LoopDetectedError
from modex_agent.hook.abc import AfterLLMResponseHook, HookPoint, HookSpec
from modex_agent.hook.runner import HookRunner


class _LoopDetectedHook(AfterLLMResponseHook):
    @property
    def name(self) -> str:
        return "loop"

    async def after_llm_response(self, ctx, response) -> None:  # noqa: ANN001
        raise LoopDetectedError(user_content="<x/>", loop_type="content")


class _CancelledHook(AfterLLMResponseHook):
    @property
    def name(self) -> str:
        return "cancel"

    async def after_llm_response(self, ctx, response) -> None:  # noqa: ANN001
        raise AgentCancelled()


@pytest.mark.asyncio
async def test_runner_propagates_loop_detected_error():
    runner = HookRunner([HookSpec(hook=_LoopDetectedHook())])
    with pytest.raises(LoopDetectedError):
        await runner.dispatch(HookPoint.AFTER_LLM_RESPONSE, ctx=None, payload=None)


@pytest.mark.asyncio
async def test_runner_still_propagates_agent_cancelled():
    runner = HookRunner([HookSpec(hook=_CancelledHook())])
    with pytest.raises(AgentCancelled):
        await runner.dispatch(HookPoint.AFTER_LLM_RESPONSE, ctx=None, payload=None)
