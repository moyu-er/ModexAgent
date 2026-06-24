"""Integration tests for session isolation.

验证:
- 同一 session_id 下不同 agent 的 history 互不干扰
- coder 和 planner 使用不同的 agent_session_id
"""

import pytest

pytestmark = pytest.mark.integration

from modex_agent.core.emitter import AgentResult
from modex_agent.multi_agent import (
    AgentDescriptor,
    AgentLLMConfig,
    DefaultAgentFactory,
)
from modex_agent.multi_agent.address import AgentAddress


@pytest.mark.asyncio
async def test_coder_and_planner_history_isolation():
    """coder 和 planner 应在不同的 agent_session_id 中保存历史。"""
    factory = DefaultAgentFactory()
    session_id = "conv_001"

    coder_descriptor = AgentDescriptor(
        address=AgentAddress(name="coder"),
        llm_config=AgentLLMConfig(),
        system_prompt_template="You are a coder.",
    )
    planner_descriptor = AgentDescriptor(
        address=AgentAddress(name="planner"),
        llm_config=AgentLLMConfig(),
        system_prompt_template="You are a planner.",
    )

    coder_instance = await factory.create_agent(
        coder_descriptor, session_id=session_id,
    )
    planner_instance = await factory.create_agent(
        planner_descriptor, session_id=session_id,
    )

    # 各自使用独立的 session_id
    coder_session = f"{session_id}:coder"
    planner_session = f"{session_id}:planner"

    # 保存 coder 的用户消息
    await coder_instance.context_manager.save(
        session_id=coder_session,
        user_message={"role": "user", "content": "Write Python code."},
        assistant_result=AgentResult(content="print('hello')"),
    )

    # 保存 planner 的用户消息
    await planner_instance.context_manager.save(
        session_id=planner_session,
        user_message={"role": "user", "content": "Plan the project."},
        assistant_result=AgentResult(content="Step 1: design"),
    )

    # 加载并验证隔离
    coder_state = await coder_instance.context_manager.load(coder_session)
    planner_state = await planner_instance.context_manager.load(planner_session)

    coder_history_list = await coder_state.history.to_list()
    planner_history_list = await planner_state.history.to_list()
    coder_history = [m["content"] for m in coder_history_list if m.get("role") == "user"]
    planner_history = [m["content"] for m in planner_history_list if m.get("role") == "user"]

    assert "Write Python code." in coder_history
    assert "Plan the project." not in coder_history

    assert "Plan the project." in planner_history
    assert "Write Python code." not in planner_history


@pytest.mark.asyncio
async def test_same_agent_session_concurrency_protected():
    """同一 agent_session_id 的并发请求应被串行化。"""
    from modex_agent.messaging.broker_memory import InMemoryMessageBroker
    from modex_agent.multi_agent import AgentPool

    broker = InMemoryMessageBroker()
    pool = AgentPool(broker=broker, agent_factory=DefaultAgentFactory())

    lock1 = pool.get_lock("session_001:coder")
    lock2 = pool.get_lock("session_001:coder")
    lock3 = pool.get_lock("session_001:planner")

    assert lock1 is lock2, "同一 session 应返回同一个锁"
    assert lock1 is not lock3, "不同 session 应使用不同锁"
