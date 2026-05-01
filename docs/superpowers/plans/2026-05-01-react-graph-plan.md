# ReactGraph Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Refactor ReActAgent into node-based ReActGraph engine: LLMCallNode + ToolExecutionNode with two-phase approval (classify-all, execute-after-all-approved), eliminating the dual tool-execution-path inconsistency.

**Architecture:** ReActAgent becomes a thin shell holding ReActGraph. ReActGraph coordinates LLMCallNode and ToolExecutionNode in a loop. ToolExecutionNode uses LangGraph-inspired "replay from checkpoint" pattern — on first pass it classifies all tools and suspends if any need approval; on resume it re-enters with injected `_tool_decisions`. All tool execution flows through `_execute_tool() → interceptor_chain.around_tool_call() → emitter → hooks`.

**Tech Stack:** Python 3.13, asyncio, dataclasses, StateStore (existing)

---

### Task 1: Create `state.py` — TurnResumeState + TurnResumeStateStore

**Files:**
- Create: `framework/agents/react/state.py`

- [ ] **Step 1: Write the file**

```python
"""TurnResumeState — 断点状态持久化。"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any


@dataclass
class TurnResumeState:
    """在 ToolExecutionNode 暂停时保存的状态。恢复时从该状态重入。"""

    assistant_message: dict[str, Any]
    tool_calls: list[dict[str, Any]]
    iteration: int
    all_new_messages: list[dict[str, Any]] = field(default_factory=list)
    iteration_messages: list[dict[str, Any]] = field(default_factory=list)


class TurnResumeStateStore(ABC):
    """TurnResumeState 持久化抽象。"""

    @abstractmethod
    async def save(self, session_id: str, state: TurnResumeState) -> None: ...

    @abstractmethod
    async def load(self, session_id: str) -> TurnResumeState | None: ...

    @abstractmethod
    async def delete(self, session_id: str) -> None: ...


class InMemoryTurnResumeStateStore(TurnResumeStateStore):
    """进程内 dict，重启丢失。Inline 策略使用。"""

    def __init__(self) -> None:
        self._states: dict[str, TurnResumeState] = {}

    async def save(self, session_id: str, state: TurnResumeState) -> None:
        self._states[session_id] = state

    async def load(self, session_id: str) -> TurnResumeState | None:
        return self._states.get(session_id)

    async def delete(self, session_id: str) -> None:
        self._states.pop(session_id, None)


class StateStoreTurnResumeStateStore(TurnResumeStateStore):
    """基于 StateStore，重启可恢复。SuspendResume 策略使用。
    key 格式: turn_resume_state/{session_id}
    """

    def __init__(self, store: Any) -> None:
        self._store = store

    def _key(self, session_id: str) -> str:
        return f"turn_resume_state/{session_id}"

    async def save(self, session_id: str, state: TurnResumeState) -> None:
        await self._store.set(self._key(session_id), state)

    async def load(self, session_id: str) -> TurnResumeState | None:
        result = await self._store.get(self._key(session_id))
        return result  # type: ignore[return-value]

    async def delete(self, session_id: str) -> None:
        key = self._key(session_id)
        if await self._store.exists(key):
            await self._store.delete(key)
```

- [ ] **Step 2: Verify import works**

```bash
python -c "from framework.agents.react.state import TurnResumeState, InMemoryTurnResumeStateStore, StateStoreTurnResumeStateStore; print('OK')"
```

- [ ] **Step 3: Commit**

```bash
git add framework/agents/react/state.py
git commit -m "feat: add TurnResumeState + TurnResumeStateStore ABC with two impls"
```

---

### Task 2: Create `nodes/__init__.py`

**Files:**
- Create: `framework/agents/react/nodes/__init__.py`

- [ ] **Step 1: Create directory and empty init**

```bash
mkdir -p framework/agents/react/nodes
touch framework/agents/react/nodes/__init__.py
```

- [ ] **Step 2: Commit**

```bash
git add framework/agents/react/nodes/__init__.py
git commit -m "feat: add nodes package for ReActGraph"
```

---

### Task 3: Create `nodes/llm_node.py` — LLMCallNode

**Files:**
- Create: `framework/agents/react/nodes/llm_node.py`

- [ ] **Step 1: Write LLMCallNode with LLMCallResult**

```python
"""LLMCallNode — LLM 请求 + 流式处理。"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from framework.hook import HookPoint
from framework.interceptor.abc import InterceptorScope, LLMStreamChunk, LLMStreamContext

if TYPE_CHECKING:
    from framework.agents.react.agent import ReActAgent
    from framework.core.agent import AgentContext
    from framework.core.emitter import ContentEmitter, ToolCall
    from framework.core.types import LLMResponse

logger = logging.getLogger(__name__)


@dataclass
class LLMCallResult:
    content: str
    reasoning: str | None
    tool_calls: list[Any]  # list[ToolCall]
    finish_reason: str
    assistant_message: dict[str, Any]
    is_error: bool = False


class LLMCallNode:
    """LLM 调用节点。根据 emitter 偏好选择流式或非流式路径。"""

    def __init__(self, agent: "ReActAgent") -> None:
        self._agent = agent

    async def execute(
        self,
        context: "AgentContext",
        emitter: "ContentEmitter",
    ) -> LLMCallResult:
        from framework.core.constants import FinishReason

        await self._agent._call_hooks(HookPoint.BEFORE_ITERATION, context)
        await self._agent._drain_injections(context)

        messages = await context.to_messages()
        if context.governance is not None:
            messages = await context.governance.apply(messages)

        response = await self._request_llm(messages, context, emitter)
        await self._agent._call_hooks(HookPoint.AFTER_LLM_RESPONSE, context, response)

        content = response.content or ""
        reasoning = response.reasoning_content
        tool_calls = response.tool_calls or []

        is_error = response.finish_reason == FinishReason.ERROR.value
        if is_error:
            logger.warning(
                "LLMCallNode error: finish_reason=%s error=%s",
                response.finish_reason,
                (response.error or "")[:200],
            )

        assistant_message = self._agent._build_assistant_message(content, tool_calls)

        return LLMCallResult(
            content=content,
            reasoning=reasoning,
            tool_calls=tool_calls,
            finish_reason=response.finish_reason or "stop",
            assistant_message=assistant_message,
            is_error=is_error,
        )

    async def _request_llm(
        self,
        messages: list[dict[str, Any]],
        context: "AgentContext",
        emitter: "ContentEmitter",
    ) -> Any:  # LLMResponse
        from framework.core.provider import StreamingLLMProvider

        wants_streaming = emitter.wants_streaming()
        is_streaming = isinstance(self._agent.provider, StreamingLLMProvider)

        if wants_streaming and is_streaming:
            if (
                getattr(context, "interceptor_chain", None)
                and context.interceptor_chain.has_scope(InterceptorScope.LLM_STREAM)
            ):
                return await self._stream_with_interceptors(messages, context, emitter)
            return await self._stream_plain(messages, context, emitter)
        else:
            return await self._call_non_streaming(messages, context, emitter)

    async def _call_non_streaming(
        self,
        messages: list[dict[str, Any]],
        context: "AgentContext",
        emitter: "ContentEmitter",
    ) -> Any:
        from framework.agents.react.agent import ReActEvent

        response = await self._agent.provider.chat(
            messages=messages,
            tools=context.get_tool_descriptions() if context.tool_manager else None,
            temperature=context.temperature or 0.7,
            max_tokens=context.max_tokens,
        )
        if response.content:
            await emitter.emit_content(response.content)
            await emitter.emit(ReActEvent.MODEL_OUTPUT, response.content)
        if response.reasoning_content:
            await emitter.emit(ReActEvent.MODEL_REASONING, response.reasoning_content)
        await emitter.emit_stream_end(resuming=bool(response.tool_calls))
        return response

    async def _stream_plain(
        self,
        messages: list[dict[str, Any]],
        context: "AgentContext",
        emitter: "ContentEmitter",
    ) -> Any:
        from framework.agents.react.agent import ReActEvent
        from framework.core.provider import StreamingLLMProvider

        assert isinstance(self._agent.provider, StreamingLLMProvider)

        async def _on_content_delta(delta: str) -> None:
            if delta:
                await emitter.emit_delta(delta)
                await emitter.emit(ReActEvent.MODEL_OUTPUT, delta)

        async def _on_reasoning_delta(delta: str) -> None:
            if delta:
                await emitter.emit(ReActEvent.MODEL_REASONING, delta)

        response = await self._agent.provider.chat_stream(
            messages=messages,
            tools=context.get_tool_descriptions() if context.tool_manager else None,
            temperature=context.temperature or 0.7,
            max_tokens=context.max_tokens,
            on_content_delta=_on_content_delta,
            on_reasoning_delta=_on_reasoning_delta,
        )
        await emitter.emit_stream_end(resuming=bool(response.tool_calls))
        return response

    async def _stream_with_interceptors(
        self,
        messages: list[dict[str, Any]],
        context: "AgentContext",
        emitter: "ContentEmitter",
    ) -> Any:
        from framework.agents.react.agent import ReActEvent
        from framework.core.provider import StreamingLLMProvider

        assert isinstance(self._agent.provider, StreamingLLMProvider)

        stream_ctx = LLMStreamContext(
            messages=messages,
            model=getattr(self._agent.provider, "model", None),
            session_id=context.session_id,
        )

        accumulated_content = ""
        accumulated_reasoning = ""
        finish_reason = "stop"
        tool_calls_list: list[Any] = []

        async def _actual_stream():
            nonlocal tool_calls_list

            async def _on_content_delta(delta: str) -> None:
                if delta:
                    await emitter.emit_delta(delta)
                    await emitter.emit(ReActEvent.MODEL_OUTPUT, delta)

            async def _on_reasoning_delta(delta: str) -> None:
                if delta:
                    await emitter.emit(ReActEvent.MODEL_REASONING, delta)

            response = await self._agent.provider.chat_stream(
                messages=messages,
                tools=context.get_tool_descriptions() if context.tool_manager else None,
                temperature=context.temperature or 0.7,
                max_tokens=context.max_tokens,
                on_content_delta=_on_content_delta,
                on_reasoning_delta=_on_reasoning_delta,
            )
            tool_calls_list = list(response.tool_calls or [])
            yield LLMStreamChunk(
                content_delta=response.content,
                reasoning_delta=response.reasoning_content,
                finish_reason=response.finish_reason,
            )

        async for chunk in context.interceptor_chain.around_llm_stream(
            context,
            stream_ctx,
            _actual_stream,
        ):
            if chunk.control_action == "cancel":
                finish_reason = chunk.finish_reason or "cancelled"
                logger.warning(
                    "LLM stream cancelled session=%s", context.session_id,
                )
                break
            if chunk.content_delta:
                accumulated_content += chunk.content_delta
            if chunk.reasoning_delta:
                accumulated_reasoning += chunk.reasoning_delta
            if chunk.finish_reason:
                finish_reason = chunk.finish_reason

        has_tool_calls = bool(tool_calls_list)
        await emitter.emit_stream_end(resuming=has_tool_calls)

        from framework.core.types import LLMResponse

        return LLMResponse(
            content=accumulated_content or None,
            reasoning_content=accumulated_reasoning or None,
            finish_reason=finish_reason,
            tool_calls=tool_calls_list,
        )
```

- [ ] **Step 2: Verify import**

```bash
python -c "from framework.agents.react.nodes.llm_node import LLMCallNode, LLMCallResult; print('OK')"
```

- [ ] **Step 3: Commit**

```bash
git add framework/agents/react/nodes/llm_node.py
git commit -m "feat: add LLMCallNode with streaming/non-streaming dispatch"
```

---

### Task 4: Create `nodes/tool_node.py` — ToolExecutionNode

**Files:**
- Create: `framework/agents/react/nodes/tool_node.py`

- [ ] **Step 1: Write ToolExecutionNode**

```python
"""ToolExecutionNode — 两阶段 tool 处理：审批检查 + 批量执行。"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from framework.approval.state import ApprovalState
from framework.approval.types import ApprovalResolution, ApprovalTier
from framework.control.exceptions import AgentAwaitingApproval
from framework.hook import HookPoint

if TYPE_CHECKING:
    from framework.agents.react.agent import ReActAgent
    from framework.agents.react.nodes.llm_node import LLMCallResult
    from framework.agents.react.state import TurnResumeState
    from framework.core.agent import AgentContext
    from framework.core.emitter import ContentEmitter

logger = logging.getLogger(__name__)


@dataclass
class ToolExecutionResult:
    all_new_messages: list[dict[str, Any]] = field(default_factory=list)
    stop_reason: str = "completed"  # "completed" | "suspended"


class ToolExecutionNode:
    """两阶段 tool 执行节点。

    阶段 1（审批检查）：分类全部 tool，如有审批需求则 suspend
    阶段 2（批量执行）：全部审批通过后，逐个执行 tool（emitter + interceptor + hook）
    """

    def __init__(self, agent: "ReActAgent") -> None:
        self._agent = agent

    async def execute(
        self,
        context: "AgentContext",
        emitter: "ContentEmitter",
        llm_result: "LLMCallResult",
        resume_state: "TurnResumeState | None" = None,
    ) -> ToolExecutionResult:
        from framework.agents.react.agent import ReActEvent

        tool_calls = llm_result.tool_calls
        context.metadata["_pending_tool_calls"] = tool_calls

        if resume_state is not None:
            # ── 恢复路径：直接从阶段 2 开始 ──
            return await self._execute_batch(
                context, emitter, tool_calls,
                tool_decisions=context.metadata.get("_tool_decisions", {}),
            )

        # ── 首次执行：阶段 1（审批检查）──
        emitter.emit(ReActEvent.PROGRESS,
                     {"hint": self._agent._format_tool_hint(tool_calls), "tool_hint": True})
        await self._agent._call_hooks(HookPoint.BEFORE_TOOL_EXECUTION, context, tool_calls)

        # 分类全部 tool
        pending_tools: list[tuple[int, Any, ApprovalTier]] = []  # (index, tool_call, tier)
        resolutions: list[tuple[str, ApprovalResolution]] = []

        interceptor = self._get_approval_interceptor(context)
        for idx, tc in enumerate(tool_calls):
            tier = self._classify_tier(interceptor, context, tc)
            tc_id = tc.call_id or ""

            if tier == ApprovalTier.HARDLINE:
                resolutions.append((tc_id, ApprovalResolution.DENIED))
            elif tier == ApprovalTier.NORMAL:
                resolutions.append((tc_id, ApprovalResolution.ALLOWED))
            else:
                # SENSITIVE or DANGEROUS → pending
                pending_tools.append((idx, tc, tier))

        if not pending_tools:
            # 全部免审批 → 直接阶段 2
            return await self._execute_batch(context, emitter, tool_calls, tool_decisions={})

        # 有审批需求 → 构建 ApprovalState
        from framework.approval.abc import ApprovalRequest
        from types import MappingProxyType
        from uuid import uuid4

        requests = tuple(
            ApprovalRequest(
                request_id=uuid4().hex,
                tool_name=tc.tool_name,
                tool_call_id=tc.call_id or "",
                tier=tier,
                redacted_arguments=MappingProxyType(
                    interceptor._redact_args(dict(tc.arguments or {}))
                    if interceptor else {}
                ),
                session_id=context.session_id,
                turn_id=context.metadata.get("turn_id", ""),
                iteration=context.metadata.get("_iteration", 0),
                description=f"Tool '{tc.tool_name}' requires approval",
            )
            for _, tc, tier in pending_tools
        )

        state = ApprovalState(
            session_id=context.session_id,
            tool_requests=requests,
            current_index=0,
            resolutions=(),
        )

        # 保存到 state_manager
        state_manager = getattr(interceptor, "_state_manager", None) if interceptor else None
        if state_manager is not None:
            await state_manager.save(state)

        # 构建 TurnResumeState
        from framework.agents.react.state import TurnResumeState

        resume_state = TurnResumeState(
            assistant_message=llm_result.assistant_message,
            tool_calls=[
                {
                    "id": tc.call_id or "",
                    "type": "function",
                    "function": {
                        "name": tc.tool_name,
                        "arguments": str(tc.arguments or {}),
                    },
                }
                for tc in tool_calls
            ],
            iteration=context.metadata.get("_iteration", 0),
            all_new_messages=[],
            iteration_messages=[],
        )
        context.metadata["_turn_resume_state"] = resume_state

        # 发送第一个审批提示
        ui = getattr(interceptor, "_ui", None) if interceptor else None
        if ui is not None and requests:
            from framework.approval.builtin.interceptor import TieredToolApprovalInterceptor

            await ui.render_message(
                session_id=context.session_id,
                content=TieredToolApprovalInterceptor._format_approval_message(requests[0]),
            )

        # 等待审批
        wait_strategy = getattr(interceptor, "_wait", None) if interceptor else None
        if wait_strategy is not None:
            approval_timeout = getattr(interceptor, "_approval_timeout", 300.0)
            await wait_strategy.wait(
                session_id=context.session_id,
                ui=ui,
                timeout=approval_timeout,
            )

        # Inline: wait() 返回 → 阶段 2
        return await self._execute_batch(context, emitter, tool_calls, tool_decisions={})

    async def _execute_batch(
        self,
        context: "AgentContext",
        emitter: "ContentEmitter",
        tool_calls: list[Any],
        *,
        tool_decisions: dict[str, str],
    ) -> ToolExecutionResult:
        """阶段 2: 批量执行全部 tool（emitter + interceptor gate + executor 完整路径）。"""
        from framework.agents.react.agent import ReActEvent

        all_new_messages: list[dict[str, Any]] = []
        iteration_messages: list[dict[str, Any]] = []

        for tool_call in tool_calls:
            tc_id = tool_call.call_id or ""
            decision = tool_decisions.get(tc_id)
            if decision is not None:
                context.metadata["_tool_resolution"] = decision

            await emitter.emit(ReActEvent.TOOL_CALL_START, tool_call)
            result = await self._agent._execute_tool(tool_call, context)
            await emitter.emit(ReActEvent.TOOL_CALL_END, (tool_call, result))

            context.metadata.pop("_tool_resolution", None)

            tool_message = self._agent._build_tool_message(result, tc_id)
            await context.history.append(tool_message)
            all_new_messages.append(tool_message)
            iteration_messages.append(tool_message)
            await self._agent._save_checkpoint(all_new_messages, context)

        await self._agent._call_hooks(
            HookPoint.AFTER_TOOL_EXECUTION,
            context,
            [msg for msg in iteration_messages if msg.get("role") == "tool"],
        )
        await self._agent._drain_injections(context)

        return ToolExecutionResult(
            all_new_messages=all_new_messages,
            stop_reason="completed",
        )

    def _classify_tier(
        self,
        interceptor: Any,
        context: "AgentContext",
        tc: Any,
    ) -> ApprovalTier:
        """分类单个 tool_call 的 tier。"""
        if interceptor is None:
            return ApprovalTier.NORMAL
        return interceptor._classify_tier(context, tc)

    def _get_approval_interceptor(self, context: "AgentContext") -> Any | None:
        """从 interceptor_chain 获取 TieredToolApprovalInterceptor。"""
        chain = getattr(context, "interceptor_chain", None)
        if chain is None:
            return None
        for interceptor in getattr(chain, "_interceptors", []):
            from framework.approval.builtin.interceptor import TieredToolApprovalInterceptor

            if isinstance(interceptor, TieredToolApprovalInterceptor):
                return interceptor
        return None
```

- [ ] **Step 2: Verify import**

```bash
python -c "from framework.agents.react.nodes.tool_node import ToolExecutionNode, ToolExecutionResult; print('OK')"
```

- [ ] **Step 3: Commit**

```bash
git add framework/agents/react/nodes/tool_node.py
git commit -m "feat: add ToolExecutionNode with two-phase approval+execution"
```

---

### Task 5: Create `graph.py` — ReActGraph coordinator

**Files:**
- Create: `framework/agents/react/graph.py`

- [ ] **Step 1: Write ReActGraph**

```python
"""ReActGraph — ReAct 循环协调器。"""

from __future__ import annotations

import asyncio
import logging
from typing import TYPE_CHECKING, Any

from framework.control.exceptions import AgentAwaitingApproval, AgentControlError
from framework.hook import HookPoint

if TYPE_CHECKING:
    from framework.agents.react.agent import ReActAgent
    from framework.agents.react.state import TurnResumeState, TurnResumeStateStore
    from framework.core.agent import AgentContext
    from framework.core.emitter import AgentResult, ContentEmitter

logger = logging.getLogger(__name__)


class ReActGraph:
    """ReAct 循环协调器。管理 LLMCallNode ↔ ToolExecutionNode 转换。"""

    def __init__(self, agent: "ReActAgent") -> None:
        from framework.agents.react.nodes.llm_node import LLMCallNode
        from framework.agents.react.nodes.tool_node import ToolExecutionNode

        self._agent = agent
        self._llm_node = LLMCallNode(agent)
        self._tool_node = ToolExecutionNode(agent)
        self._resume_store: TurnResumeStateStore | None = None

    @property
    def resume_store(self) -> TurnResumeStateStore | None:
        return self._resume_store

    @resume_store.setter
    def resume_store(self, store: TurnResumeStateStore | None) -> None:
        self._resume_store = store

    async def run(
        self,
        context: "AgentContext",
        emitter: "ContentEmitter",
    ) -> "AgentResult":
        """主入口。检测 _turn_resume_state 后在 _run_loop 中处理。"""
        from framework.agents.react.agent import ReActEvent
        from framework.core.agent import current_agent_context

        context.attachments = []
        ctx_token = current_agent_context.set(context)

        result = await self._run_loop(context, emitter)

        current_agent_context.reset(ctx_token)
        return result

    async def _run_loop(
        self,
        context: "AgentContext",
        emitter: "ContentEmitter",
    ) -> "AgentResult":
        from framework.agents.react.agent import ReActEvent
        from framework.agents.react.nodes.llm_node import LLMCallResult
        from framework.core.constants import FinishReason
        from framework.core.emitter import AgentResult

        resume_state: TurnResumeState | None = context.metadata.get("_turn_resume_state")
        is_resuming = resume_state is not None

        if is_resuming:
            # 从断点恢复：恢复迭代状态，构造 synthetic LLM result（跳过真实 LLM 调用）
            iteration = resume_state.iteration
            all_new_messages = list(resume_state.all_new_messages)
            llm_result = LLMCallResult(
                content="",
                reasoning=None,
                tool_calls=self._deserialize_tool_calls(resume_state.tool_calls),
                finish_reason="stop",
                assistant_message=resume_state.assistant_message,
            )
            # assistant msg 已在首次执行时写入 history
        else:
            iteration = 0
            all_new_messages: list[dict[str, Any]] = []
            llm_result = None

        result = AgentResult(content="", stop_reason="error")

        if not is_resuming:
            await emitter.emit(ReActEvent.START)
            await self._agent._call_hooks(HookPoint.BEFORE_TURN, context)

        try:
            while iteration < context.max_iterations:
                if not is_resuming:
                    iteration += 1

                context.metadata["_iteration"] = iteration

                if is_resuming:
                    # 跳过 LLM，llm_result 已在上面构造好
                    is_resuming = False
                else:
                    await emitter.emit(ReActEvent.ITERATION_START, {"iteration": iteration})

                    # ── LLM Node ──
                    llm_result = await self._llm_node.execute(context, emitter)

                    if llm_result.is_error:
                        error_text = llm_result.content or "LLM request failed"
                        await emitter.emit(ReActEvent.ERROR, error_text)
                        result = AgentResult(
                            error=error_text, stop_reason="error",
                            messages=all_new_messages, attachments=context.attachments,
                        )
                        await emitter.emit_complete(result)
                        return result

                    # 限制每轮最大工具调用数
                    max_tools = context.max_tools_per_turn
                    if max_tools is not None and llm_result.tool_calls and len(llm_result.tool_calls) > max_tools:
                        error_msg = f"Exceeded max_tools_per_turn limit ({max_tools})"
                        await emitter.emit(ReActEvent.ERROR, error_msg)
                        result = AgentResult(
                            content=error_msg, stop_reason="error",
                            messages=all_new_messages, attachments=context.attachments,
                        )
                        await emitter.emit_complete(result)
                        return result

                    # 写入 assistant message（resume 路径已在首次执行时写入）
                    assistant_msg = llm_result.assistant_message
                    await context.history.append(assistant_msg)
                    all_new_messages.append(assistant_msg)
                    await self._agent._save_checkpoint(all_new_messages, context)

                if llm_result.tool_calls:
                    # ── Tool Node ──
                    tool_result = await self._tool_node.execute(
                        context, emitter, llm_result,
                        resume_state=resume_state if resume_state is not None else None,
                    )
                    resume_state = None  # 只使用一次
                    all_new_messages.extend(tool_result.all_new_messages)

                    if tool_result.stop_reason == "suspended":
                        self._save_resume_state(context)
                        return AgentResult(
                            stop_reason="approval_suspended",
                            messages=all_new_messages,
                        )

                    await self._agent._call_hooks(HookPoint.AFTER_ITERATION, context)
                    await emitter.emit(
                        ReActEvent.ITERATION_END,
                        {"iteration": iteration, "has_tool_calls": True},
                    )

                    if iteration >= context.max_iterations:
                        result = AgentResult(
                            content="达到最大迭代次数", stop_reason="max_iterations",
                            messages=all_new_messages, attachments=context.attachments,
                        )
                        await self._agent._clear_checkpoint(context)
                        await emitter.emit(ReActEvent.MAX_ITERATIONS, result)
                        await emitter.emit_complete(result)
                        return result
                else:
                    result = AgentResult(
                        content=llm_result.content,
                        reasoning=llm_result.reasoning,
                        messages=all_new_messages,
                        attachments=context.attachments,
                    )
                    await self._agent._clear_checkpoint(context)
                    await emitter.emit(ReActEvent.FINAL_OUTPUT, result)
                    await emitter.emit_complete(result)
                    return result

            result = AgentResult(
                content="达到最大迭代次数", stop_reason="max_iterations",
                messages=all_new_messages, attachments=context.attachments,
            )
            await self._agent._clear_checkpoint(context)
            await emitter.emit(ReActEvent.MAX_ITERATIONS, result)
            await emitter.emit_complete(result)
            return result

        except AgentAwaitingApproval:
            self._save_resume_state(context)
            raise

        except asyncio.CancelledError:
            logger.warning("ReActGraph cancelled (iteration=%d)", iteration)
            try:
                await asyncio.shield(self._agent._save_checkpoint(all_new_messages, context))
            except Exception:
                logger.warning("Checkpoint save during cancel failed", exc_info=True)
            raise

        except AgentControlError as e:
            logger.warning("ReActGraph control exit: %s", e.termination)
            try:
                await asyncio.shield(self._agent._save_checkpoint(all_new_messages, context))
            except Exception:
                logger.warning("Checkpoint save during control exit failed", exc_info=True)
            raise

        except Exception as e:
            logger.exception("ReActGraph error")
            await emitter.emit(ReActEvent.ERROR, str(e))
            await self._agent._save_checkpoint(all_new_messages, context)
            result = AgentResult(
                error=str(e), stop_reason="error",
                messages=all_new_messages, attachments=context.attachments,
            )
            await emitter.emit_complete(result)
            return result

        finally:
            context.metadata.pop("_approval_batch_denied", None)
            context.metadata.pop("_approval_denial", None)
            context.metadata.pop("_cancelled_tool_records", None)
            context.metadata.pop("_injection_cycle_count", None)
            context.metadata.pop("_iteration", None)
            context.metadata.pop("_turn_resume_state", None)
            context.metadata.pop("_tool_decisions", None)
            context.metadata.pop("_tool_resolution", None)
            context.metadata.pop("_pending_tool_calls", None)
            if not is_resuming:
                await self._agent._call_hooks(HookPoint.AFTER_TURN, context, result)

    def _deserialize_tool_calls(
        self,
        raw: list[dict[str, Any]],
    ) -> list[Any]:
        import json
        from framework.core.emitter import ToolCall

        result: list[Any] = []
        for tc in raw:
            func = tc.get("function", {})
            args_raw = func.get("arguments", "{}")
            arguments = json.loads(args_raw) if isinstance(args_raw, str) else args_raw
            result.append(ToolCall(
                tool_name=func.get("name", ""),
                call_id=tc.get("id", ""),
                arguments=arguments,
            ))
        return result

    def _save_resume_state(self, context: "AgentContext") -> None:
        resume_state = context.metadata.get("_turn_resume_state")
        if resume_state is None or self._resume_store is None:
            return
        session_id = context.session_id
        if session_id:
            import asyncio as _asyncio
            _asyncio.create_task(self._resume_store.save(session_id, resume_state))
```

- [ ] **Step 2: Verify import**

```bash
python -c "from framework.agents.react.graph import ReActGraph; print('OK')"
```

- [ ] **Step 3: Commit**

```bash
git add framework/agents/react/graph.py
git commit -m "feat: add ReActGraph coordinator with suspend/resume"
```

---

### Task 6: Refactor `agent.py` — slim to thin shell

**Files:**
- Modify: `framework/agents/react/agent.py`

- [ ] **Step 1: Replace the entire `run()` method and remove migrated methods**

Replace lines 125-843 (the `run()` method through `_drain_injections` plus `_stream_with_control`, `_request_llm`) with the thin shell. Keep `ReActEvent`, `_execute_tool`, `_execute_tool_raw`, `_build_assistant_message`, `_build_tool_message`, `_call_hooks`, `_save_checkpoint`, `_clear_checkpoint`, `_resolve_hook_timeout`, `_resolve_tool_timeout`, `_drain_injections`, `_format_tool_hint`, `_save_denial_checkpoint`.

```python
# ReActAgent.run() — replace entirely:

    async def run(
        self,
        context: AgentContext,
        emitter: ContentEmitter[ReActEvent],
    ) -> AgentResult:
        return await self._graph.run(context, emitter)
```

Then add the `_graph` initialization in `__init__`:

```python
def __init__(
    self,
    provider: LLMProvider,
    hook_timeout: float = _HOOK_TIMEOUT,
    tool_timeout: float = _TOOL_TIMEOUT,
):
    self.provider = provider
    self._hook_timeout = hook_timeout
    self._tool_timeout = tool_timeout
    from framework.agents.react.graph import ReActGraph
    self._graph = ReActGraph(self)
```

Remove these methods (move logic was already written in LLMCallNode):
- `_request_llm` (lines 540-606)
- `_stream_with_control` (lines 653-728)

Keep all other methods as-is. Add `graph` property:

```python
@property
def graph(self) -> "ReActGraph":
    return self._graph
```

Also remove the `AgentAwaitingApproval` catch block (lines 326-366) and `_save_denial_checkpoint` (lines 406-445) — these are replaced by `_save_resume_state` in ReActGraph.

- [ ] **Step 2: Verify agent.py still imports correctly after refactor**

```bash
python -c "from framework.agents.react.agent import ReActAgent, ReActEvent; print('OK')"
```

- [ ] **Step 3: Run agent and approval tests to verify**

```bash
python -m pytest tests/unit/approval/ -q --tb=short 2>&1 | tail -5
```

- [ ] **Step 4: Commit**

```bash
git add framework/agents/react/agent.py
git commit -m "refactor: slim ReActAgent to thin shell delegating to ReActGraph"
```

---

### Task 7: Update Interceptor — add Gate 1 (decision recovery)

**Files:**
- Modify: `framework/approval/builtin/interceptor.py`

- [ ] **Step 1: Add Gate 1 at the top of `around_tool_call`**

After the hardline check (Gate 0), insert Gate 1 before the original `_approval_batch_denied` check:

```python
async def around_tool_call(
    self,
    ctx: AgentContext,
    call: ToolCallContext,
    next_call: ToolCallNext,
) -> ToolResult:
    tool_name = call.tool_name
    tc_id = call.tool_call.call_id or ""

    # ── Gate 0: 硬阻断 ──
    if self._hardline_matcher and self._hardline_matcher.matches(tool_name):
        return ToolResult(
            tool_name=tool_name,
            call_id=tc_id,
            error=f"Error: '{tool_name}' is blocked by safety policy (hardline).",
        )

    # ── Gate 1: 决策恢复路径（ToolExecutionNode 阶段 2）──
    decisions: dict[str, str] = ctx.metadata.get("_tool_decisions", {})
    decision = decisions.get(tc_id) or ctx.metadata.get("_tool_resolution")
    if decision is not None:
        if decision in ("allowed", "allow"):
            return await next_call()
        else:
            error_text = (
                f"Tool '{tool_name}' was not approved by the user."
                if decision == "denied"
                else f"Tool '{tool_name}' was ignored (user sent unrelated message)."
                if decision == "ignored"
                else f"Tool '{tool_name}' was not executed — prior tool in batch was denied/ignored."
            )
            return ToolResult(
                tool_name=tool_name, call_id=tc_id, error=error_text,
            )

    # ── existing batch_denied / hardline / approval logic follows ──
    if ctx.metadata.get("_approval_batch_denied"):
        ...
```

- [ ] **Step 2: Run interceptor and approval tests**

```bash
python -m pytest tests/unit/approval/test_interceptor.py tests/unit/approval/ -q --tb=short 2>&1 | tail -5
```

- [ ] **Step 3: Commit**

```bash
git add framework/approval/builtin/interceptor.py
git commit -m "feat: add Gate1 decision recovery path to interceptor"
```

---

### Task 8: Update Pipeline — new `_resume_agent_turn`, simplify `_fill_batch_results`

**Files:**
- Modify: `framework/pipeline/pipeline.py`

- [ ] **Step 1: Replace `_resume_agent_turn` to inject decisions and call agent.run() once**

The new `_resume_agent_turn` builds AgentContext with `_turn_resume_state` + `_tool_decisions`, then calls `agent.run()` once. The agent's `_run_loop` detects the resume state, executes tools (ToolExecutionNode phase 2), then continues the while loop (LLM sees tool results and responds).

```python
async def _resume_agent_turn(self, session_id: str, state: Any) -> None:
    """ALLOW: 注入审批决策，agent.run() 一次完成 tool 执行 + LLM 推理。"""
    resume_store = self._turn_resume_store
    resume_state = await resume_store.load(session_id) if resume_store else None
    if resume_state is None:
        logger.error("Cannot resume turn for %s: no resume state", session_id)
        return

    tool_decisions = {}
    if state is not None:
        for tc_id, resolution in state.resolutions:
            tool_decisions[tc_id] = resolution.value

    ctx_mgr = (
        self.context_manager_factory(session_id)
        if self.context_manager_factory
        else self.context_manager
    )
    context_state = await ctx_mgr.load_with_metadata(session_id, {})
    if context_state is None:
        logger.error("Cannot resume turn for %s: context state is None", session_id)
        return

    from framework.multi_agent.subagent_manager import current_conversation_id

    conv_token = current_conversation_id.set(session_id)
    turn = self.safety.turn
    turn_start = time.monotonic()
    turn_clean = False

    injection_queue = self._injection_queues.get(session_id)
    agent_context = AgentContext(
        system_prompt=context_state.system_prompt,
        history=context_state.history,
        tool_manager=self.tool_manager,
        session_id=session_id,
        max_iterations=self.max_iterations,
        metadata={
            "session_id": session_id,
            "_turn_resume_state": resume_state,
            "_tool_decisions": tool_decisions,
        },
        hooks=self.hooks,
        hook_runner=self.hook_runner,
        interceptor_chain=self.interceptor_chain,
        checkpoint_store=self.checkpoint_store,
        runtime_context_manager=self.runtime_context_manager,
        governance=self.governance,
        safety=self.safety,
        injection_queue=injection_queue,
    )

    if self.emitter_factory:
        emitter = self.emitter_factory(session_id)
    else:
        emitter = StreamingAwareEmitter(
            output_adapter=self.output_adapter,
            session_id=session_id,
            send_timeout=self.safety.turn.output_send_timeout_seconds,
        )

    task = asyncio.current_task()
    if task is not None:
        self._session_tasks[session_id] = task

    try:
        result = await self.agent.run(agent_context, emitter)
        if result and result.attachments:
            await inject_attachments_to_history(context_state.history, result.attachments)
        await ctx_mgr.save(
            session_id=session_id,
            user_message=None,
            assistant_result=result,
            metadata={},
        )
        turn_clean = True
        elapsed = time.monotonic() - turn_start
        logger.info(
            "resume_turn_done session=%s stop_reason=%s elapsed=%.1fs",
            session_id,
            result.stop_reason if result else "none",
            elapsed,
        )
    except asyncio.CancelledError:
        logger.warning("Resumed turn cancelled session=%s", session_id)
        raise
    except AgentAwaitingApproval:
        logger.info("Agent re-suspended during resume: session=%s", session_id)
        raise
    finally:
        current_conversation_id.reset(conv_token)
        self._session_tasks.pop(session_id, None)
        await _safe_flush(ctx_mgr, session_id, timeout=turn.memory_flush_timeout_seconds)
        if turn_clean:
            await _safe_clear_checkpoint(
                ctx_mgr, session_id, timeout=turn.memory_flush_timeout_seconds
            )
        if resume_store:
            await resume_store.delete(session_id)
        await self._approval_manager.clear(session_id)
```

- [ ] **Step 2: Add `turn_resume_store` to Pipeline constructor**

Add parameter:
```python
turn_resume_store: Any | None = None,
```

And store it:
```python
self._turn_resume_store = turn_resume_store
```

- [ ] **Step 3: Simplify `_fill_batch_results` — remove `execute_real=True` branch**

Remove the `if execute_real and resolution_str == "allowed":` branch. The method now only handles `execute_real=False` (DENY/IGNORE error filling).

- [ ] **Step 4: Update `_try_consume_approval` to call new `_resume_agent_turn` signature**

Change:
```python
await self._resume_agent_turn(session_id)
```
to:
```python
await self._resume_agent_turn(session_id, state)
```

- [ ] **Step 5: Run full test suite**

```bash
python -m pytest tests/unit/ -q --tb=short --ignore=tests/unit/plugins 2>&1 | tail -10
```

- [ ] **Step 6: Commit**

```bash
git add framework/pipeline/pipeline.py
git commit -m "refactor: simplify pipeline resume to inject decisions via agent.run()"
```

---

### Task 9: Update BotService integration

**Files:**
- Modify: `examples/bot_project/bot/service/core.py`

- [ ] **Step 1: Create TurnResumeStateStore and pass to Pipeline**

In `initialize()`, after creating `self._state_store`, add:

```python
from framework.agents.react.state import StateStoreTurnResumeStateStore

self._turn_resume_store = StateStoreTurnResumeStateStore(self._state_store)
```

In `_initialize_pipeline()`, pass it to `AgentPipeline`:

```python
self.pipeline = AgentPipeline(
    ...
    turn_resume_store=self._turn_resume_store,
    ...
)
```

- [ ] **Step 2: Set resume_store on ReActGraph**

After creating `self.agent`, set the resume_store on the graph:

```python
self.agent = ReActAgent(provider=provider)
self.agent.graph.resume_store = self._turn_resume_store
```

- [ ] **Step 3: Commit**

```bash
git add examples/bot_project/bot/service/core.py
git commit -m "feat: wire TurnResumeStateStore into BotService pipeline"
```

---

### Task 10: Final cleanup — remove dead code, update exports

**Files:**
- Modify: `framework/agents/react/__init__.py`
- Modify: `framework/pipeline/pipeline.py` — remove `_execute_approved_batch`
- Modify: `framework/approval/builtin/interceptor.py` — remove `_approval_batch_denied` references

- [ ] **Step 1: Update `framework/agents/react/__init__.py`**

```python
"""ReAct Agent 实现模块。"""

from .agent import ReActAgent, ReActEvent
from .builder import ReActAgentBuilder
from .graph import ReActGraph
from .state import (
    InMemoryTurnResumeStateStore,
    StateStoreTurnResumeStateStore,
    TurnResumeState,
    TurnResumeStateStore,
)

__all__ = [
    "InMemoryTurnResumeStateStore",
    "ReActAgent",
    "ReActAgentBuilder",
    "ReActEvent",
    "ReActGraph",
    "StateStoreTurnResumeStateStore",
    "TurnResumeState",
    "TurnResumeStateStore",
]
```

- [ ] **Step 2: Remove `_execute_approved_batch` from pipeline.py**

Delete the method entirely — it's replaced by `_resume_agent_turn(session_id, state)` (ALLOW) and `_fill_batch_results(execute_real=False)` (DENY/IGNORE).

- [ ] **Step 3: Remove `_approval_batch_denied` references from interceptor.py**

Remove the `_approval_batch_denied` check block in `around_tool_call` (lines 92-101) — Gate 1 replaces it. Remove `_handle_denied` and `_handle_timeout` methods that set `_approval_batch_denied`.

- [ ] **Step 4: Run full test suite + lint**

```bash
python -m pytest tests/unit/ -q --tb=short --ignore=tests/unit/plugins 2>&1 | tail -5
echo "---"
ruff check framework/agents/react/ framework/pipeline/pipeline.py framework/approval/ 2>&1 | tail -5
```

- [ ] **Step 5: Commit**

```bash
git add framework/agents/react/__init__.py framework/pipeline/pipeline.py framework/approval/builtin/interceptor.py
git commit -m "chore: cleanup dead code and update exports for ReactGraph"
```

---

### Task 11: Final verification — full test pass + integration check

- [ ] **Step 1: Run the complete unit test suite**

```bash
python -m pytest tests/unit/ -q --tb=short --ignore=tests/unit/plugins 2>&1 | tail -5
```

Expected: All previously passing tests still pass.

- [ ] **Step 2: Run approval-specific tests**

```bash
python -m pytest tests/unit/approval/ tests/unit/control/ -q --tb=short 2>&1 | tail -5
```

- [ ] **Step 3: Run linter on all changed files**

```bash
ruff check framework/agents/react/ framework/pipeline/pipeline.py framework/approval/ examples/bot_project/bot/service/core.py 2>&1
```

Expected: 0 errors.

- [ ] **Step 4: Final commit if any fixes needed**

```bash
git add -A
git commit -m "fix: final adjustments from verification pass"
```
