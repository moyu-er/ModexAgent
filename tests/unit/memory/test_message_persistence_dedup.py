"""验证 Pipeline 流程中消息持久化不重不漏。

Round4 评审质疑：ReAct agent 的 context.history.append() 实时写入存储，
导致 save() 再次保存 assistant_result.messages 时产生重复。

本测试证明：Pipeline 流程中 context.history 是 ListMessageHistory（内存列表），
append() 不持久化；save() 中的 assistant_result.messages 持久化是唯一路径。
"""

import tempfile
from pathlib import Path

import pytest

from framework.core.agent import Agent, AgentContext
from framework.core.emitter import AgentResult, ContentEmitter
from framework.core.events import AgentEvent, EmitterConfig
from framework.core.tool_manager import InMemoryToolManager
from framework.core.types import InputMessage
from framework.memory.core.scope import MemoryContext
from framework.memory.history import ListMessageHistory, ShortTermMessageHistory
from framework.memory.system import MemorySystem, MemorySystemContextManager


class _FakeEvent(AgentEvent):
    TEST = "test"


class _FakeEmitter(ContentEmitter[_FakeEvent]):
    def __init__(self):
        super().__init__(EmitterConfig())

    async def emit_delta(self, delta: str) -> None:
        pass

    async def emit_error(self, error: str) -> None:
        pass

    async def emit_complete(self, result: AgentResult) -> None:
        pass


class _ToolCallingAgent(Agent[_FakeEvent]):
    """模拟 ReAct agent：产生 assistant + tool result 消息。"""

    event_enum = _FakeEvent
    max_iterations = 3

    @property
    def name(self) -> str:
        return "tool_caller"

    async def run(self, context: AgentContext, emitter: ContentEmitter[_FakeEvent], streaming: bool = True):
        # 模拟 ReAct agent 的行为：append 到 context.history
        await context.history.append({"role": "assistant", "content": "thinking..."})
        await context.history.append({"role": "tool", "content": "tool output", "tool_call_id": "tc1"})
        await context.history.append({"role": "assistant", "content": "final answer"})

        return AgentResult(
            content="final answer",
            messages=[
                {"role": "assistant", "content": "thinking..."},
                {"role": "tool", "content": "tool output", "tool_call_id": "tc1"},
                {"role": "assistant", "content": "final answer"},
            ],
        )


class TestMessagePersistenceDedup:
    """P0: 验证 Pipeline 流程中消息不重复写入。"""

    @pytest.mark.asyncio
    async def test_pipeline_history_is_list_message_history(self):
        """Pipeline 包装的 history 是 ListMessageHistory，append() 不持久化。"""
        with tempfile.TemporaryDirectory() as tmp:
            ms = MemorySystem(workspace=Path(tmp))
            await ms.initialize()
            adapter = MemorySystemContextManager(ms)

            # 预存一条用户消息
            ctx = MemoryContext(session_id="s1", user_id="u1")
            await ms.add_message(ctx, {"role": "user", "content": "hello"})

            # 模拟 Pipeline 的 load + wrap 流程
            state = await adapter.load("s1")
            assert not isinstance(state.history, ShortTermMessageHistory)
            state.history = ListMessageHistory(list(state.history))

            # 模拟 ReAct agent 的 append
            await state.history.append({"role": "assistant", "content": "reply"})

            # 验证：append 后存储中**没有**新增消息
            stored = await ms.get_history(ctx)
            assert len(stored) == 1  # 只有原始的 user 消息
            assert stored[0]["content"] == "hello"

            await ms.close()

    @pytest.mark.asyncio
    async def test_save_persists_assistant_messages(self):
        """save() 的 assistant_result.messages 是唯一的持久化路径。"""
        with tempfile.TemporaryDirectory() as tmp:
            ms = MemorySystem(workspace=Path(tmp))
            await ms.initialize()
            adapter = MemorySystemContextManager(ms)

            # Step 1: save user message
            await adapter.save(
                "s1",
                {"role": "user", "content": "hello"},
                AgentResult(),
            )

            # Step 2: save assistant result (模拟 Pipeline 的行为)
            await adapter.save(
                "s1",
                user_message=None,
                assistant_result=AgentResult(
                    content="final",
                    messages=[
                        {"role": "assistant", "content": "thinking..."},
                        {"role": "tool", "content": "output", "tool_call_id": "tc1"},
                        {"role": "assistant", "content": "final"},
                    ],
                ),
            )

            # Step 3: 验证存储中的消息
            ctx = MemoryContext(session_id="s1", user_id="default")
            stored = await ms.get_history(ctx)

            # 期望 4 条：user + assistant + tool + assistant
            assert len(stored) == 4
            assert stored[0]["role"] == "user"
            assert stored[1]["role"] == "assistant"
            assert stored[2]["role"] == "tool"
            assert stored[3]["role"] == "assistant"

            await ms.close()

    @pytest.mark.asyncio
    async def test_full_turn_no_duplicates(self):
        """完整 turn：agent append(内存) + save(持久化) → 存储中无重复。"""
        with tempfile.TemporaryDirectory() as tmp:
            ms = MemorySystem(workspace=Path(tmp))
            await ms.initialize()
            adapter = MemorySystemContextManager(ms)

            # 模拟 Pipeline 步骤 ①：save user message
            await adapter.save(
                "s1",
                {"role": "user", "content": "hello"},
                AgentResult(),
            )

            # 模拟 Pipeline 步骤 ②：load + wrap history
            state = await adapter.load("s1")
            state.history = ListMessageHistory(list(state.history))

            # 模拟 ReAct agent 步骤 ②③：append 到内存 history
            agent = _ToolCallingAgent()
            emitter = _FakeEmitter()
            agent_context = AgentContext(
                system_prompt="",
                history=state.history,
                tool_manager=InMemoryToolManager(),
                session_id="s1",
            )
            result = await agent.run(agent_context, emitter)

            # 验证内存中有 4 条（1 user + 3 agent）
            mem_msgs = await state.history.to_list()
            assert len(mem_msgs) == 4

            # 模拟 Pipeline 步骤 ④：save assistant result
            await adapter.save("s1", user_message=None, assistant_result=result)

            # 验证存储中也有恰好 4 条 —— 无重复
            ctx = MemoryContext(session_id="s1", user_id="default")
            stored = await ms.get_history(ctx)
            assert len(stored) == 4

            # 内容一一对应
            for i, (mem, disk) in enumerate(zip(mem_msgs, stored)):
                assert mem["role"] == disk["role"], f"msg {i}: role mismatch"
                assert mem["content"] == disk["content"], f"msg {i}: content mismatch"

            await ms.close()
