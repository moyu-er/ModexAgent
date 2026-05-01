# ReactGraph — 基于节点图的 ReAct 执行引擎

> **日期**: 2026-05-01
> **范围**: `framework/agents/react/`, `framework/pipeline/pipeline.py`, `framework/approval/builtin/interceptor.py`
> **灵感**: LangGraph 的"函数重放 + 值注入"中断-恢复模式

---

## 一、动机

### 1.1 当前问题

1. **两套 tool 执行路径不一致**：正常路径走 `ReActAgent._execute_tool()`（emitter + interceptor + hook 完整），SuspendResume 审批恢复走 `Pipeline._fill_batch_results()`（直接调 `tool_manager.execute()`，所有 emitter/hook/interceptor 被绕过）
2. **`agent.py` 太臃肿**（~850 行）：单个 `run()` 方法包含了所有逻辑——LLM 调用、流式处理、tool 循环、异常恢复，职责不清
3. **审批恢复是"外部修补"**：Pipeline 手动读 memory、手动 append tool result、手动开新 turn，而不是让 agent 自然恢复

### 1.2 设计目标

- **统一 tool 执行路径**：无论首次执行还是审批后恢复，所有 tool 走 `_execute_tool()` → interceptor chain → emitter → hook
- **节点化 ReAct 循环**：LLM 调用、Tool 执行各自为独立节点，`ReActGraph` 协调节点转换
- **可重入 ToolExecutionNode**：审批暂停时保存 `TurnResumeState`，恢复时从节点起点重入，与 LangGraph 的 `interrupt()` 模式对齐
- **保留全部现有机制**：Hook、Interceptor、Control、Emitter 完全保留，只是调用位置从 `agent.py` 移到节点内

---

## 二、核心数据模型

### 2.1 `TurnResumeState` — 断点状态

```python
# framework/agents/react/state.py

from dataclasses import dataclass, field
from typing import Any

@dataclass
class TurnResumeState:
    """在 ToolExecutionNode 暂停时保存的状态。恢复时从该状态重入。"""
    assistant_message: dict[str, Any]        # 本轮 LLM 返回的含 tool_calls 的 assistant msg
    tool_calls: list[dict[str, Any]]         # 序列化的 tool_calls ([{id, type, function: {name, arguments}}])
    iteration: int                            # 暂停时的迭代编号
    all_new_messages: list[dict[str, Any]]   # 截止暂停时已写入 history 的新增消息
    iteration_messages: list[dict[str, Any]] # 本迭代内已写入的消息
```

### 2.2 `TurnResumeStateStore` — ABC + 两个实现

```python
# framework/agents/react/state.py

from abc import ABC, abstractmethod

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


class StateStoreTurnResumeStateStore(TurnResumeStateStore):
    """基于 StateStore，重启可恢复。SuspendResume 策略使用。
    key 格式: turn_resume_state/{session_id}
    """
```

### 2.3 `TurnResumeStateStore` 与 `ApprovalStateManager` 的关系

两者职责不同，互相独立：
- `ApprovalStateManager`: 管理**审批决策进度**（哪些 tool 已 allow/deny，当前提示哪个 tool）
- `TurnResumeStateStore`: 管理**执行断点**（tool loop 恢复时从哪个位置、哪些消息开始）

Pipeline 在 `_resume_agent_turn` 中从两者分别读取状态，合并后注入 AgentContext。

---

## 三、节点设计

### 3.1 文件结构

```
framework/agents/react/
├── __init__.py               # 导出 ReActAgent, ReActEvent
├── agent.py                  # ReActAgent（薄层，持有 ReActGraph）
├── graph.py                  # ReActGraph（协调器）
├── state.py                  # TurnResumeState + TurnResumeStateStore ABC + 两个实现
├── builder.py                # ReActAgentBuilder（已有，小改）
└── nodes/
    ├── __init__.py
    ├── llm_node.py           # LLMCallNode — LLM 请求 + 流式处理
    └── tool_node.py           # ToolExecutionNode — tool 循环 + 恢复逻辑
```

### 3.2 `LLMCallNode`

**职责**：发起 LLM 请求（流式/非流式），返回 `LLMCallResult`。

```python
@dataclass
class LLMCallResult:
    content: str
    reasoning: str | None
    tool_calls: list[ToolCall]
    finish_reason: str
    assistant_message: dict[str, Any]
    is_error: bool = False
```

**流程图**：
```
接收 AgentContext + emitter
  → call_hooks(BEFORE_ITERATION)
  → drain_injections()
  → build messages (to_messages + governance)
  → call_hooks(AFTER_LLM_CONTEXT)
  → _request_llm (streaming 或 non-streaming, interceptor_chain 包裹)
  → call_hooks(AFTER_LLM_RESPONSE)
  → return LLMCallResult
```

**与旧代码的关系**：提取 `ReActAgent.run()` 中 lines 156-176 的逻辑，以及 `_request_llm()` / `_stream_with_control()` 方法。

### 3.3 `ToolExecutionNode`

**职责**：两阶段 tool 处理——先逐个审批全部 tool，全部通过后批量执行。

**核心原则**：
- **审批-执行分离**：审批阶段不执行任何 tool，执行阶段不走审批
- **全量 or 全否**：全部审批通过才执行；任何一个被拒则剩余全部标记为 preempted
- **NORMAL 免审批**：NORMAL tier 的 tool 不进入审批流程，执行阶段直接执行

**接口**：
```python
class ToolExecutionNode:
    def __init__(self, agent: "ReActAgent"):
        self._agent = agent

    async def execute(
        self,
        context: AgentContext,
        emitter: ContentEmitter[ReActEvent],
        llm_result: LLMCallResult,
        resume_state: TurnResumeState | None = None,
    ) -> ToolExecutionResult:
        ...
```

**`ToolExecutionResult`**：
```python
@dataclass
class ToolExecutionResult:
    all_new_messages: list[dict[str, Any]]
    stop_reason: str  # "completed" | "suspended"
```

**首次执行 — 两阶段流程**（`resume_state is None`）：

```
阶段 1: 审批检查（PRE-CHECK，不执行 tool）
─────────────────────────────────────────────
1. emitter.emit(PROGRESS)
2. call_hooks(BEFORE_TOOL_EXECUTION, context, tool_calls)

3. 遍历 tool_calls，用 interceptor._classify_tier() 分类:
   - HARDLINE   → 直接标记 DENIED（硬阻止，不需用户审批）
   - NORMAL     → 标记 ALLOWED（免审批）
   - DANGEROUS  → 待审批
   - SENSITIVE  → 待审批（除非 YOLO）

4. 如果有待审批 tool:
   a. 构建 ApprovalState(全量 tool_requests，NORMAL 预标记 ALLOWED)
   b. state_manager.save(state)
   c. 构建 TurnResumeState → ctx.metadata["_turn_resume_state"]
   d. ui.render_message(第一个待审批 tool)
   e. wait_strategy.wait()
      ├─ Inline:         阻塞 poll → 收到 APPROVAL_RESPONSE/DENY → 继续第二阶段
      └─ SuspendResume: raise AgentAwaitingApproval
        → 冒泡到 ReActGraph → save TurnResumeStateStore → re-raise to Pipeline
   f. 返回 ToolExecutionResult(stop_reason="suspended")  ← 控制流不进入阶段 2

阶段 2: 批量执行（全部审批通过后才到达）
─────────────────────────────────────────────
5. for tool_call in tool_calls:
     a. emitter TOOL_CALL_START
     b. result = await self._agent._execute_tool(tool_call, context)
        └→ interceptor_chain.around_tool_call()
           └→ TieredToolApprovalInterceptor:
              - Gate0: HARDLINE → error
              - Gate1: 决策已存在(_tool_decisions) → ALLOWED→next_call, DENIED/PREEMPTED→error
              - Gate2: NORMAL tier → next_call（直接执行）
              - Gate3: YOLO → next_call
              - Gate4: 需要审批 → 正常走审批逻辑（首次执行不应到达此处）
                         但如果到达（例如未分类到的 edge case），按正常审批处理
     c. emitter TOOL_CALL_END
     d. _build_tool_message → context.history.append
     e. _save_checkpoint

6. call_hooks(AFTER_TOOL_EXECUTION)
7. drain_injections
8. return ToolExecutionResult(stop_reason="completed")
```

**恢复执行**（`resume_state is not None`）：
```
1. 从 resume_state 恢复 tool_calls, iteration 等
2. 从 context.metadata["_tool_decisions"] 获取审批决策

3. 如果 decisions 中全部审批通过 → 直接进入阶段 2（批量执行）
   - 每个 tool 走完整的: emitter → interceptor gate → executor → emitter → build_tool_message
   - NORMAL: 无决策 but Gate2 passes → next_call 执行
   - ALLOWED: Gate1 hits → next_call 执行
   - DENIED/PREEMPTED: Gate1 hits → error ToolResult
   
4. 如果 decisions 中有拒绝 → 剩余 tool 全部 preempted
   → 也进入阶段 2，被拒/preempted tool 通过 Gate1 返回 error
   → emitter/hook 完整，LLM 看到错误后自然响应
```

**关键 invariant**：首次执行时，在执行阶段 2 之前**没有任何 tool 被执行**。NORMAL tool 也不会被先执行——它们等待审批完成后统一执行。

### 3.3.1 审批拒绝/忽略的退出行为

**原则：全部审批通过的 tool 才执行，被拒/忽略/前序拒绝的 tool 填入 error 结果。NORMAL tier 的 tool 无需审批，自动视为通过。**

**全部通过场景**：
```
LLM tool_calls: [tc1(normal), tc2(sensitive), tc3(sensitive)]
阶段 1 逐个审批: tc2 → /approve, tc3 → /approve → all_approved=True
阶段 2 批量执行:
  tc1(normal): emitter → interceptor Gate1(无决策,NORMAL=next_call) → 真实执行 ✓
  tc2(sensitive): emitter → interceptor Gate1("allowed") → next_call → 真实执行 ✓
  tc3(sensitive): emitter → interceptor Gate1("allowed") → next_call → 真实执行 ✓
  → call_hooks(AFTER_TOOL_EXECUTION) → LLM 看到 3 个成功结果 → 回复用户
```

**拒绝场景（前序拒绝导致后续全部失败）**：
```
LLM tool_calls: [tc1(normal), tc2(sensitive), tc3(sensitive)]
阶段 1: tc2 → /deny
→ ApprovalState: tc2=DENIED
→ Pipeline 补齐 tc3=PREEMPTED（前序拒绝，未审批的自动标记）
→ 不是全部通过 → _resume_agent_turn 注入 _tool_decisions

阶段 2 批量执行:
  tc1(normal): emitter → interceptor Gate1(无决策) → next_call → 真实执行 ✓
  tc2(sensitive): emitter → interceptor Gate1("denied") → error ToolResult
    → _build_tool_message → "Error: Tool 'xxx' was not approved by the user."
  tc3(sensitive): emitter → interceptor Gate1("preempted") → error ToolResult
    → _build_tool_message → "Error: Tool 'xxx' was not executed — prior tool in batch was denied."

  → call_hooks(AFTER_TOOL_EXECUTION)（看到 1 成功 + 2 错误）
  → ReActGraph 继续 LLM 迭代
  → LLM: "抱歉，tc2 被拒绝了，tc3 也因此未执行。tc1 成功完成。需要我换个方式吗？"
```

**IGNORE 场景**：
```
同拒绝，区别是前序 tool 标记为 IGNORED（而非 DENIED）
  tc2: error="Tool 'xxx' was ignored (user sent unrelated message)."
  tc3: error="Tool 'xxx' was not executed — prior tool in batch was ignored."
用户消息随后正常进入新 turn。
```

**NORMAL tier 不变**：NORMAL tool 不参与审批，无论其他 tool 审批结果如何都正常执行。

### 3.3.2 流式/非流式 LLM 兼容

`LLMCallNode` 通过 `emitter.wants_streaming()` + `isinstance(provider, StreamingLLMProvider)` 判断走哪条路径，与当前 `_request_llm` 逻辑完全一致：

```python
class LLMCallNode:
    async def execute(self, context, emitter) -> LLMCallResult:
        wants_streaming = emitter.wants_streaming()
        is_streaming = isinstance(self._agent.provider, StreamingLLMProvider)

        if wants_streaming and is_streaming:
            if context.interceptor_chain and \
               context.interceptor_chain.has_scope(InterceptorScope.LLM_STREAM):
                return await self._stream_with_interceptors(context, emitter)
            return await self._stream_plain(context, emitter)
        else:
            return await self._call_non_streaming(context, emitter)
```

ToolExecutionNode 与 LLM 模式无关——它只处理 tool 执行，不关心 LLM 是流式还是非流式。

### 3.4 `ReActGraph`

**职责**：协调 `LLMCallNode` 和 `ToolExecutionNode` 的转换，管理 ReAct 循环。

```
graph LR
    Start[START] --> LLM[LLMCallNode]
    LLM -->|has tool_calls| Tool[ToolExecutionNode]
    LLM -->|no tool_calls| End[FinalOutput]
    Tool -->|completed| LLM
    Tool -->|suspended| Suspend[⏸ Suspend]
    Suspend -->|resume| Tool
```

**核心方法**：
```python
class ReActGraph:
    def __init__(self, agent: "ReActAgent"):
        self._agent = agent
        self._llm_node = LLMCallNode(agent)
        self._tool_node = ToolExecutionNode(agent)
        self._resume_store: TurnResumeStateStore | None = None

    async def run(
        self,
        context: AgentContext,
        emitter: ContentEmitter[ReActEvent],
    ) -> AgentResult:
        """主循环。检测 metadata 中的 _turn_resume_state 决定是否跳过 LLM。"""
        resume_state = context.metadata.get("_turn_resume_state")
        if resume_state is not None:
            return await self._resume_from(context, emitter, resume_state)
        return await self._run_loop(context, emitter)

    async def _run_loop(self, context, emitter) -> AgentResult:
        """正常 ReAct 循环。"""
        ...

    async def _resume_from(self, context, emitter, resume_state) -> AgentResult:
        """从断点恢复，跳过 LLM 直接进入 ToolExecutionNode。"""
        ...
```

**`_run_loop` 主循环逻辑**：
```python
async def _run_loop(self, context, emitter):
    iteration = 0
    all_new_messages = []
    result = AgentResult(content="", stop_reason="error")

    ctx_token = current_agent_context.set(context)
    await emitter.emit(ReActEvent.START)
    await self._agent._call_hooks(HookPoint.BEFORE_TURN, context)

    try:
        while iteration < context.max_iterations:
            iteration += 1
            await emitter.emit(ReActEvent.ITERATION_START, {"iteration": iteration})

            # --- LLM 节点 ---
            llm_result = await self._llm_node.execute(context, emitter)

            if llm_result.is_error:
                result = AgentResult(error=..., stop_reason="error", messages=all_new_messages)
                await emitter.emit_complete(result)
                return result

            # 写入 assistant message
            assistant_msg = llm_result.assistant_message
            await context.history.append(assistant_msg)
            all_new_messages.append(assistant_msg)
            await self._agent._save_checkpoint(all_new_messages, context)

            # --- Tool 节点 ---
            if llm_result.tool_calls:
                tool_result = await self._tool_node.execute(
                    context, emitter, llm_result,
                )
                all_new_messages.extend(tool_result.iteration_messages)
                # all_new_messages already extended inside ToolExecutionNode

                if tool_result.stop_reason == "suspended":
                    # Save TurnResumeState (already in ctx.metadata)
                    self._save_resume_state(context)
                    return AgentResult(
                        stop_reason="approval_suspended",
                        messages=all_new_messages,
                    )
                # completed → 继续下一轮迭代
                if iteration >= context.max_iterations:
                    result = AgentResult(
                        content="达到最大迭代次数",
                        stop_reason="max_iterations",
                        messages=all_new_messages,
                    )
                    await self._agent._clear_checkpoint(context)
                    await emitter.emit(ReActEvent.MAX_ITERATIONS, result)
                    await emitter.emit_complete(result)
                    return result
            else:
                # 无 tool_calls → 最终输出
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

    except AgentAwaitingApproval as e:
        # ToolExecutionNode 内部抛出的暂停信号
        self._save_resume_state(context)
        raise  # 继续冒泡到 Pipeline

    except asyncio.CancelledError:
        await asyncio.shield(self._agent._save_checkpoint(all_new_messages, context))
        raise

    except AgentControlError as e:
        await asyncio.shield(self._agent._save_checkpoint(all_new_messages, context))
        raise

    except Exception as e:
        await emitter.emit(ReActEvent.ERROR, str(e))
        await self._agent._save_checkpoint(all_new_messages, context)
        result = AgentResult(error=str(e), stop_reason="error", messages=all_new_messages)
        await emitter.emit_complete(result)
        return result

    finally:
        context.metadata.pop("_approval_batch_denied", None)
        context.metadata.pop("_approval_denial", None)
        context.metadata.pop("_cancelled_tool_records", None)
        context.metadata.pop("_injection_cycle_count", None)
        context.metadata.pop("_turn_resume_state", None)
        context.metadata.pop("_tool_decisions", None)
        current_agent_context.reset(ctx_token)
        await self._agent._call_hooks(HookPoint.AFTER_TURN, context, result)
```

### 3.5 `ReActAgent`（薄层）

```python
class ReActAgent(Agent[ReActEvent]):
    event_enum = ReActEvent

    def __init__(self, provider, hook_timeout=..., tool_timeout=...):
        self.provider = provider
        self._hook_timeout = hook_timeout
        self._tool_timeout = tool_timeout
        self._graph = ReActGraph(self)

    @property
    def name(self) -> str:
        return "ReActAgent"

    async def run(self, context: AgentContext, emitter: ContentEmitter[ReActEvent]) -> AgentResult:
        return await self._graph.run(context, emitter)

    # ── 私有方法（供 ToolExecutionNode / LLMCallNode 调用）──
    def _execute_tool(self, tool_call, context) -> ToolResult: ...
    def _execute_tool_raw(self, tool_call, context) -> ToolResult: ...
    def _build_assistant_message(self, content, tool_calls) -> dict: ...
    def _build_tool_message(self, result, call_id) -> dict: ...
    def _call_hooks(self, hook_point, context, *args) -> None: ...
    def _save_checkpoint(self, messages, context) -> None: ...
    def _clear_checkpoint(self, context) -> None: ...
    def _resolve_hook_timeout(self, context) -> float: ...
    def _resolve_tool_timeout(self, context) -> float: ...
    def _drain_injections(self, context) -> list[str]: ...

    # ── 公开属性（供 ToolExecutionNode 引用）──
    @property
    def graph(self) -> ReActGraph:
        return self._graph
```

---

## 四、Interceptor 门控设计

TieredToolApprovalInterceptor 有两个职责：
1. **预检分类**（`_classify_tier`）：给 ToolExecutionNode 提供 tier 分类，用于构建 `ApprovalState`
2. **执行门控**（`around_tool_call`）：在批量执行阶段，根据已有决策放行或拦截

```python
async def around_tool_call(self, ctx, call, next_call) -> ToolResult:
    tool_name = call.tool_name
    tc_id = call.tool_call.call_id or ""

    # ── Gate 0: 硬阻断（始终生效）──
    if self._hardline_matcher and self._hardline_matcher.matches(tool_name):
        return ToolResult(tool_name=tool_name, error="Blocked by safety policy (hardline).")

    # ── Gate 1: 决策已存在（执行阶段：首次执行阶段2 或 恢复阶段2）──
    decisions: dict[str, str] = ctx.metadata.get("_tool_decisions", {})
    decision = decisions.get(tc_id)
    if decision is not None:
        if decision == "allowed":
            return await next_call()   # 审批通过，正常执行
        else:
            # denied / preempted / ignored / timed_out
            return ToolResult(
                tool_name=tool_name, call_id=tc_id,
                error=f"Tool '{tool_name}' was not approved by the user.",
            )

    # ── Gate 2: NORMAL tier（免审批，直接执行）──
    tier = self._classify_tier(tool_name, dict(call.arguments or {}))
    if tier == ApprovalTier.NORMAL:
        return await next_call()

    # ── Gate 3: YOLO 模式（跳过审批）──
    if tier == ApprovalTier.SENSITIVE:
        if await self._store.is_yolo_enabled(ctx.session_id):
            return await next_call()

    # ── Gate 4: 需要审批（首次执行阶段 1 的路由）──
    # 到达此处说明 ToolExecutionNode 的阶段 1 预检未覆盖此 tool，
    # 或调用方直接走了 _execute_tool 没有经过 ToolExecutionNode。
    # 降级为 per-tool 审批模式（兼容直接调用场景）。
    logger.warning("Gate4 fallback: per-tool approval for %s", tool_name)
    return await self._request_approval(ctx, call, next_call, tier)
```

**`_request_approval` 简化**（不再需要 batch_denied 标记和全量 tool 构建）：
1. 构建单 tool `ApprovalRequest`
2. 渲染 IM 提示
3. 调用 `wait()` → 阻塞（Inline）或抛异常（SuspendResume）

**与 ToolExecutionNode 的分工**：
| 职责 | 归属 |
|------|------|
| 全量 tool 分类（NORMAL/DANGEROUS/SENSITIVE/HARDLINE） | ToolExecutionNode 阶段 1 |
| 构建 `ApprovalState`（全量 tool_requests） | ToolExecutionNode 阶段 1 |
| 逐个审批提示（current_index 推进） | Pipeline `_try_consume_approval` |
| 批量执行时按决策放行/拦截 | Interceptor Gate 1 |
| 执行 NORMAL tool | Interceptor Gate 2 |

---

## 五、Pipeline 变更

### 5.1 `_resume_agent_turn` — ALLOW 路径：注入决策 → 批量执行

```python
async def _resume_agent_turn(self, session_id: str, state: ApprovalState) -> None:
    """ALLOW 全部通过后，让 agent 从 ToolExecutionNode 恢复并批量执行所有 tool。"""
    # 1. 加载 resume state
    resume_state = await self._turn_resume_store.load(session_id)

    # 2. 构建 tool_decisions（全部 tool 都有 decision）
    tool_decisions = {}
    for tc_id, resolution in state.resolutions:
        tool_decisions[tc_id] = resolution.value  # "allowed"

    # 3. 加载 context → 构建 AgentContext
    context_state = await ctx_mgr.load_with_metadata(session_id, {})
    agent_context = AgentContext(
        ...,
        metadata={
            "session_id": session_id,
            "_turn_resume_state": resume_state,
            "_tool_decisions": tool_decisions,
        },
        ...
    )

    # 4. agent.run() → ToolExecutionNode 进入阶段 2:
    #    - NORMAL tool: Gate2 passes → next_call 执行 ✓
    #    - ALLOWED tool: Gate1 hits → next_call 执行 ✓
    #    - 所有 tool 走: emitter → interceptor → executor → build_tool_message
    result = await self.agent.run(agent_context, emitter)

    # 5. 清理
    await self._turn_resume_store.delete(session_id)
    await self._approval_manager.clear(session_id)
    await ctx_mgr.save(session_id, user_message=None, assistant_result=result, metadata={})
```

**注意**：`_resume_agent_turn` 仅用于 ALLOW 路径。DENY/IGNORE 路径走 `_fill_batch_results(execute_real=False)` 直接填 error（无需 agent 参与真实的 tool 执行）。

### 5.2 `_try_consume_approval` — 逐个审批 + 全量通过恢复

```python
async def _try_consume_approval(self, text, session_id, input_msg) -> bool:
    """逐个审批 tool。全部通过→agent执行；拒绝/忽略→直接填error。"""
    state = await self._approval_manager.get(session_id)
    if state is None:
        return False

    action = parse_approval_action(text)

    if action == ApprovalAction.ALLOW:
        state = state.apply(state.pending.tool_call_id, ApprovalResolution.ALLOWED)
        if state.all_approved:
            # ── 全量通过 → agent 执行所有 tool ──
            await self._approval_manager.save(state)
            await self._resume_agent_turn(session_id, state)
            return True
        else:
            # 提示用户审批下一个 tool
            await self._approval_manager.save(state)
            if self._im_ui is not None:
                await self._im_ui.render_message(
                    session_id, self._format_approval_message(state.pending))
            return True

    if action == ApprovalAction.DENY:
        # 当前 DENIED，剩余 PREEMPTED
        state = state.apply(state.pending.tool_call_id, ApprovalResolution.DENIED)
        while not state.all_resolved:
            state = state.apply(state.pending.tool_call_id, ApprovalResolution.PREEMPTED)
        await self._fill_batch_results(session_id, state, execute_real=False)
        await self._approval_manager.clear(session_id)
        self._cancel_suspended_task(session_id)
        await self._drain_approval_queue(session_id)
        return True

    # action is None
    if self._is_user_message(input_msg):
        # ── IGNORE: 当前 IGNORED，剩余 PREEMPTED ──
        state = state.apply(state.pending.tool_call_id, ApprovalResolution.IGNORED)
        while not state.all_resolved:
            state = state.apply(state.pending.tool_call_id, ApprovalResolution.PREEMPTED)
        await self._fill_batch_results(session_id, state, execute_real=False)
        await self._approval_manager.clear(session_id)
        self._cancel_suspended_task(session_id)
        await self._drain_approval_queue(session_id)
        return False  # 用户消息继续正常处理
    else:
        await self._enqueue_during_approval(session_id, input_msg)
        return True
```

**`_fill_batch_results(execute_real=False)`** 保留但简化为仅填充 error 结果（DENIED/IGNORED/PREEMPTED 路径）。不再包含 `execute_real=True` 分支（那是 `_resume_agent_turn` 的职责）。

**`_cancel_suspended_task`**：取消当前 session 的 agent task（SuspendResume 下已无运行 task，但 Inline 下协程正阻塞在 wait()）。

```python
class AgentPipeline:
    def __init__(
        self,
        ...,
        turn_resume_store: TurnResumeStateStore | None = None,  # 新增
        ...
    ):
```

---

## 六、Hook / Emitter / Control 保留清单

全部保留，只是调用位置从 `agent.py` 移到对应节点：

| 机制 | 调用位置（新） | 说明 |
|------|---------------|------|
| `HookPoint.BEFORE_TURN` | `ReActGraph._run_loop` 入口 | 不变 |
| `HookPoint.AFTER_TURN` | `ReActGraph._run_loop` finally | 不变 |
| `HookPoint.BEFORE_ITERATION` | `LLMCallNode.execute` | 从 agent.py 移入 |
| `HookPoint.AFTER_LLM_RESPONSE` | `LLMCallNode.execute` | 从 agent.py 移入 |
| `HookPoint.BEFORE_TOOL_EXECUTION` | `ToolExecutionNode.execute` | 从 agent.py 移入 |
| `HookPoint.AFTER_TOOL_EXECUTION` | `ToolExecutionNode.execute` | 从 agent.py 移入 |
| `HookPoint.AFTER_ITERATION` | `ReActGraph._run_loop` | 从 agent.py 移入 |
| `emitter.emit(START/FINAL_OUTPUT/...)` | `ReActGraph` + `ToolExecutionNode` | 分散到对应节点 |
| `interceptor_chain.around_tool_call` | `ReActAgent._execute_tool` | 不变 |
| `interceptor_chain.around_llm_stream` | `LLMCallNode` | 从 `_stream_with_control` 移入 |
| `ControlWaitStrategy` | `TieredToolApprovalInterceptor` | 不变 |
| `ApprovalStateManager` | `TieredToolApprovalInterceptor` + `Pipeline` | 不变 |

---

## 七、移除清单

| 移除项 | 文件 | 原因 |
|--------|------|------|
| `Pipeline._fill_batch_results(execute_real=True)` 路径 | `pipeline.py` | 由 `_resume_agent_turn` → ToolExecutionNode 替代 |
| `Pipeline._read_last_assistant_tool_calls()` | `pipeline.py` | tool_calls 来源变为 TurnResumeState |
| `Pipeline._get_existing_tool_result_ids()` | `pipeline.py` | 不再需要（ToolExecutionNode 统一执行，无"已执行"差异） |
| `Pipeline._append_to_history()` | `pipeline.py` | Agent 的 _build_tool_message + history.append 替代 |
| `ReActAgent.run()` 主循环体（lines 125-307） | `agent.py` | 迁移到 ReActGraph + LLMCallNode + ToolExecutionNode |
| `ReActAgent._stream_with_control()` | `agent.py` | 迁移到 LLMCallNode（保留为节点内部逻辑） |
| `ReActAgent._request_llm()` | `agent.py` | 迁移到 LLMCallNode（保留为节点内部逻辑） |
| `ReActAgent._approval_batch_denied` 补齐逻辑（lines 247-271） | `agent.py` | 审批决策由 ToolExecutionNode 阶段 1 集中处理 |
| `ReActAgent AgentAwaitingApproval` catch 中的硬编码 denial_context（lines 336-364） | `agent.py` | TurnResumeState 替代 |
| `ReActAgent._save_denial_checkpoint()` | `agent.py` | TurnResumeStateStore 替代 |
| `AgentPipeline._execute_approved_batch` | `pipeline.py` | 拆分为 `_resume_agent_turn`（ALLOW）和 `_fill_batch_results(execute_real=False)`（DENY/IGNORE） |

**保留但简化的**：
| 文件 | 保留内容 | 简化 |
|------|---------|------|
| `pipeline.py:_fill_batch_results` | DENY/IGNORE 的 error 填充 | 移除 `execute_real=True` 分支 |

---

## 八、Inline 策略处理

InlineWaitStrategy 与 SuspendResume 的区别仅在 `wait_strategy.wait()` 的行为（阻塞 vs 抛异常）。**Interceptor 和 ToolExecutionNode 不感知策略差异**：

- **Inline**：`wait()` 阻塞 poll channel → `APPROVAL_RESPONSE` 命令到达 → 返回 → interceptor 返回 `await next_call()` → tool loop 自然继续 → **协程未中断，TurnResumeState 不使用**
- **SuspendResume**：`wait()` raise `AgentAwaitingApproval` → ToolExecutionNode → ReActGraph → ReActAgent → Pipeline catch → TurnResumeState 保存 → 用户审批 → `_resume_agent_turn` 注入 decisions → agent.run() 重入 ToolExecutionNode

Inline 路径中创建 `TurnResumeState` 但不是必需的——它在 `ctx.metadata` 中，若协程未中断则被 finally 清理。无副作用。

---

## 九、新增/修改文件清单

| 文件 | 操作 | 说明 |
|------|:----:|------|
| `framework/agents/react/state.py` | **新增** | `TurnResumeState` + `TurnResumeStateStore` ABC + 两个实现 |
| `framework/agents/react/graph.py` | **新增** | `ReActGraph` 协调器 |
| `framework/agents/react/nodes/__init__.py` | **新增** | 空 |
| `framework/agents/react/nodes/llm_node.py` | **新增** | `LLMCallNode` |
| `framework/agents/react/nodes/tool_node.py` | **新增** | `ToolExecutionNode` |
| `framework/agents/react/agent.py` | **修改** | 瘦身 ~850→~350 行，只保留公共接口 + 私有辅助方法 |
| `framework/agents/react/__init__.py` | 修改 | 新增导出 |
| `framework/approval/builtin/interceptor.py` | 修改 | 增加 Gate 1（决策恢复路径）；移除 `_approval_batch_denied` 相关 |
| `framework/pipeline/pipeline.py` | 修改 | 移除 `_fill_batch_results` 及其辅助方法；简化 `_resume_agent_turn`；新增 `turn_resume_store` |
| `examples/bot_project/bot/service/core.py` | 修改 | 装配 `TurnResumeStateStore` |

---

## 十、关键行为验证

| 场景 | 预期行为 |
|------|---------|
| 正常执行（无审批） | LLMCallNode → ToolExecutionNode: 阶段1(全NORMAL)→阶段2(全部执行)→LLMCallNode |
| 有审批+全部通过（SuspendResume） | 阶段1(检测到SENSITIVE)→suspend→逐个/approve→all_approved→resume→阶段2(全部执行: emitter+gate+executor)→LLM继续 |
| 有审批+用户拒绝（SuspendResume） | /deny→当前tool=DENIED,剩余=PREEMPTED→resume→阶段2(NORMAL执行,被拒返回error)→emitter完整→LLM看到error |
| 有审批+用户忽略（SuspendResume） | 用户发无关消息→当前tool=IGNORED,剩余=PREEMPTED→resume→阶段2(error填充)→用户消息正常处理 |
| Inline 逐个审批 | 阶段1→wait()阻塞→/approve→ALLOW→wait()返回→阶段2批量执行（协程未中断） |
| 多tool混合(NORMAL+SENSITIVE+DANGEROUS) | 阶段1: NORMAL预标ALLOWED, SENSITIVE/DANGEROUS进审批→逐个提示→全部通过→阶段2: ALL依次执行 |
| 审批失败后tool结果在记忆中的格式 | `{"role":"tool","tool_call_id":"tc2","content":"Error: Tool 'xxx' was not approved..."}` — 由`_build_tool_message()`统一格式化 |
| 流式LLM | LLMCallNode根据emitter.wants_streaming选路径→ToolExecutionNode不变 |
| 非流式LLM | LLMCallNode调provider.chat()→ToolExecutionNode不变 |
| 审批期间agent消息入队 | Pipeline `_pending_approval_queues`→审批终结后drain→按序处理 |
