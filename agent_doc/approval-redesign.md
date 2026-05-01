# 审批机制重新设计

> **日期**: 2026-05-01
> **范围**: `framework/approval/`, `framework/control/`, `framework/agents/react/`, `framework/pipeline/`, `examples/bot_project/`
> **动机**: 当前设计过于臃肿（4层异常传播、CheckpointStore、SuspendResumeWaitStrategy），且存在致命断点（checkpoint_store 未注入）

---

## 一、设计原则

1. **消息驱动状态机**：用户审批消息（`/approve` `/deny`）走正常 pipeline 队列消费，通过审批状态识别，不走特殊路径
2. **记忆文件为单一真相源**：tool_call 信息从记忆文件末尾读取，不依赖序列化的 Checkpoint
3. **全量 or 全否**：同批所有 tool 要么全部审批通过执行，要么全部不执行（任何一条被拒绝/忽略→全量否定）
4. **零上下文污染**：审批消息（`/approve` `/deny`）绝不写入记忆/LLM 上下文
5. **精简实现**：移除 CheckpointStore、ApprovalStore、SuspendResumeWaitStrategy、AgentAwaitingApproval 异常传播链

---

## 二、核心数据模型

### 2.1 ApprovalSessionState

```python
# framework/approval/state.py

@dataclass
class PendingToolInfo:
    """待审批的单条 tool 信息。"""
    tool_name: str
    tool_call_id: str
    arguments: dict[str, object]
    tier: str                     # ApprovalTier 值


@dataclass
class ApprovalSessionState:
    """一个 session 的审批会话状态。

    生命周期：
    1. interceptor 检测到需审批 → create → 发送 IM 消息
    2. 用户发送 /approve 或 /deny → pipeline 消费 → 更新 resolved
    3. resolved=True → 执行/填充结果 → 清除状态 → 恢复 agent
    """
    session_id: str
    pending_tools: list[PendingToolInfo]   # 本轮所有待审批 tool
    approval_message_id: str = ""          # IM 消息 ID（用于后续 update）
    created_at: float = 0.0
    resolved: bool = False                 # 用户是否已做出最终决策
    approved: bool = False                 # True=全部审批通过, False=拒绝/忽略
```

### 2.2 ApprovalStateStore ABC

```python
# framework/approval/abc.py (追加)

class ApprovalStateStore(ABC):
    """审批会话状态持久化抽象。

    实现：
    - InMemoryApprovalStateStore: 进程内 dict（开发/测试）
    - JsonFileApprovalStateStore: 基于 StateStore（单机 bot，重启可恢复）
    """

    @abstractmethod
    async def get(self, session_id: str) -> ApprovalSessionState | None: ...

    @abstractmethod
    async def save(self, state: ApprovalSessionState) -> None: ...

    @abstractmethod
    async def delete(self, session_id: str) -> None: ...
```

> **与旧 ApprovalStore 的区别**：旧的 `ApprovalStore` 管理的是"审批 pattern / YOLO 开关"等长效偏好。新的 `ApprovalStateStore` 管理的是"当前活跃审批会话"的瞬时状态。两者职责完全不同。

---

## 三、完整时序

### 3.1 正常审批通过流程

```
┌─ Pipeline._process_message_locked() ─────────────────────────────────────┐
│                                                                          │
│ 1. 检查 ApprovalStateStore.get(session_id) → None (无审批状态)           │
│ 2. 用户消息正常存储到记忆                                                │
│ 3. 构建 AgentContext → agent.run()                                       │
│                                                                          │
│   ┌─ ReActAgent.run() ────────────────────────────────────────────────┐ │
│   │                                                                    │ │
│   │ 4. LLM 返回 tool_calls=[shell("rm /tmp/logs"), write_file(...)]   │ │
│   │ 5. assistant message (含 tool_calls) 写入 history ✓               │ │
│   │ 6. 设置 ctx.metadata["_pending_tool_calls"] = tool_calls          │ │
│   │                                                                    │ │
│   │ 7. for tool_call in tool_calls:                                    │ │
│   │      _execute_tool(tc1) → interceptor_chain.around_tool_call()     │ │
│   │                                                                     │ │
│   │      ┌─ TieredToolApprovalInterceptor ──────────────────────────┐  │ │
│   │      │ 8. 从 ctx.metadata["_pending_tool_calls"] 获取全量 tool  │  │ │
│   │      │ 9. 检查 tier: shell=dangerous, write_file=sensitive       │  │ │
│   │      │ 10. 需要审批 →                                            │  │ │
│   │      │     a. 构建 ApprovalSessionState(所有 tool)               │  │ │
│   │      │     b. ApprovalStateStore.save(state)                     │  │ │
│   │      │     c. ui.render_message("⚠️ 审批请求...")  → QQ 用户     │  │ │
│   │      │     d. ctx.metadata["_approval_suspended"] = True         │  │ │
│   │      │     e. return ToolResult(error="suspended")                │  │ │
│   │      └──────────────────────────────────────────────────────────┘  │ │
│   │                                                                     │ │
│   │      ┌─ 回到 ReActAgent ────────────────────────────────────────┐  │ │
│   │      │ 11. 检测 _approval_suspended → break (不追加 result)      │  │ │
│   │      │ 12. save_checkpoint (assistant msg 已在 history)          │  │ │
│   │      │ 13. return AgentResult(stop_reason="approval_suspended")  │  │ │
│   │      └──────────────────────────────────────────────────────────┘  │ │
│   └──────────────────────────────────────────────────────────────────┘ │
│                                                                          │
│ 14. Pipeline 收到 stop_reason="approval_suspended" → turn 正常结束      │
│     (不清理 checkpoint)                                                  │
└──────────────────────────────────────────────────────────────────────────┘

【用户看到 QQ 消息："⚠️ Tool Approval Required: shell(...), write_file(...)"】
【用户回复 "/approve"】

┌─ Pipeline._process_message_locked() ─────────────────────────────────────┐
│                                                                          │
│ 1. 检查 ApprovalStateStore.get(session_id) → ApprovalSessionState!       │
│ 2. 解析消息: "/approve" (strip + lower + 前缀匹配)                       │
│ 3. 设置 state.resolved=True, state.approved=True                         │
│ 4. ApprovalStateStore.save(state)                                        │
│ 5. ui.update_message("✅ Approved")                                      │
│                                                                          │
│ 6. _execute_approved_batch(session_id, state):                           │
│    a. 加载记忆上下文 ctx_mgr.load(session_id)                            │
│    b. 从 history 末尾找到 assistant 消息 (含 tool_calls)                 │
│    c. for tool in state.pending_tools:                                   │
│         tool_result = await tool_manager.execute(name, args)  # 真实执行 │
│         history.append(tool_result.to_message())                         │
│    d. ctx_mgr.flush(session_id)  # 持久化                                │
│                                                                          │
│ 7. ApprovalStateStore.delete(session_id)                                 │
│ 8. spawn _resume_agent_turn(session_id)  # 新建 turn，LLM 继续推理       │
│                                                                          │
│ 9. return None  (审批消息不保存到记忆 ✓)                                 │
└──────────────────────────────────────────────────────────────────────────┘

┌─ _resume_agent_turn(session_id) ───────────────────────────────────────┐
│ 10. 加载上下文 (含 tool results)                                        │
│ 11. 构建 AgentContext → agent.run()                                     │
│ 12. LLM 看到 tool results → 继续推理 → 最终回复                         │
└────────────────────────────────────────────────────────────────────────┘
```

### 3.2 审批拒绝流程

```
【用户发送 "/deny"】

同 3.1 步骤 1-3，区别：
  state.resolved=True, state.approved=False

_execute_denied_batch():
  for tool in state.pending_tools:
      synthetic = ToolResult(error=f"Tool '{name}' was denied by user.")
      history.append(synthetic.to_message())

ApprovalStateStore.delete(session_id)
spawn _resume_agent_turn(session_id)  # LLM 看到错误可自行修正
```

### 3.3 审批忽略流程

```
【用户在审批期间发送普通消息】

同 3.1 步骤 1:
  ApprovalStateStore.get(session_id) → 有审批状态
  但消息不是 /approve /deny → 视为忽略审批

  state.resolved=True, state.approved=False
  (同拒绝流程，填充 synthetic 错误)
```

### 3.4 无审批状态的 /approve 消息

```
Pipeline 入口:
  ApprovalStateStore.get(session_id) → None
  消息是 "/approve" → 但无审批状态 → 按普通用户消息处理
  (LLM 会收到 "/approve" 文本，可自行解释)
```

---

## 四、各组件变更

### 4.1 ReActAgent — 简化暂停逻辑

```python
# framework/agents/react/agent.py

# 在 tool 执行循环前注入全量 tool_calls
if tool_calls:
    context.metadata["_pending_tool_calls"] = tool_calls  # ← 新增

for idx, tool_call in enumerate(tool_calls):
    result = await self._execute_tool(tool_call, context)

    # ── 检测审批暂停 ──
    if context.metadata.get("_approval_suspended"):
        # 不追加当前 tool result 到 history
        # 也不补齐后续 tool
        break  # ← 替换旧的 batch_denied + raise ApprovalDenied

    # 正常流程: 追加 tool result
    tool_message = self._build_tool_message(result, tool_call.call_id)
    await context.history.append(tool_message)
    ...

# 循环后检查
if context.metadata.get("_approval_suspended"):
    await self._save_checkpoint(all_new_messages, context)
    # ⚠️ 不再 raise AgentAwaitingApproval
    return AgentResult(
        stop_reason="approval_suspended",
        messages=all_new_messages,
    )
```

**移除的代码**：
- `AgentAwaitingApproval` catch 块（lines 324-361）
- `_approval_batch_denied` 补齐逻辑（lines 245-268）
- `ApprovalDenied` raise（line 266）
- 硬编码 `"tier": "dangerous"`（line 339）

### 4.2 TieredToolApprovalInterceptor — 简化为状态创建

```python
# framework/approval/builtin/interceptor.py

async def around_tool_call(self, ctx, call, next_call) -> ToolResult:
    tool_name = call.tool_name

    # 已暂停 → 直接返回错误 (不会到达这里，因为 ReActAgent 已 break)
    if ctx.metadata.get("_approval_suspended"):
        return ToolResult(tool_name=tool_name, call_id=..., error="Suspended")

    # 获取全量 tool_calls
    all_tools: list[ToolCall] = ctx.metadata.get("_pending_tool_calls", [])
    if not all_tools:
        return await next_call()

    # 检查是否有任何 tool 需要审批
    needs_approval = False
    pending: list[PendingToolInfo] = []
    for tc in all_tools:
        tier = self._classify(tc.tool_name, dict(tc.arguments or {}))
        if tier != ApprovalTier.NORMAL:
            needs_approval = True
        pending.append(PendingToolInfo(
            tool_name=tc.tool_name,
            tool_call_id=tc.call_id or "",
            arguments=dict(tc.arguments or {}),
            tier=tier.value,
        ))

    if not needs_approval:
        return await next_call()

    # 创建审批状态
    state = ApprovalSessionState(
        session_id=ctx.session_id,
        pending_tools=pending,
        created_at=time.monotonic(),
    )

    # 持久化
    store: ApprovalStateStore = ctx.metadata.get("_approval_state_store")
    if store:
        await store.save(state)

    # 发送 IM 消息
    msg_id = await self._ui.render_message(
        session_id=ctx.session_id,
        content=self._format_batch_approval_message(pending),
    )
    state.approval_message_id = msg_id

    # 标记暂停
    ctx.metadata["_approval_suspended"] = True
    ctx.metadata["_approval_session_state"] = state

    return ToolResult(
        tool_name=tool_name,
        call_id=call.tool_call.call_id or "",
        error="Tool execution suspended — awaiting user approval.",
    )
```

**移除的依赖**：
- `ControlWaitStrategy`（不再需要 wait）
- `SuspendResumeWaitStrategy`（不再需要）
- `ApprovalStore`（不再需要 is_yolo_enabled 等——YOLO 可移到 interceptor 自身的简单标记）
- `ControlEventBus`（事件发射可保留但简化）
- `AgentAwaitingApproval`（不再抛出）

### 4.3 AgentPipeline — 审批状态检查

```python
# framework/pipeline/pipeline.py

async def _process_message_locked(self, input_msg, session_id, route_result):
    ...
    sanitized_content = ...

    # ── 审批状态检查（在保存用户消息之前）──
    approval_store: ApprovalStateStore = self._get_approval_store()
    approval_state = await approval_store.get(session_id) if approval_store else None

    if approval_state is not None:
        text = (input_msg.content or "").strip().lower()

        # 剪枝：只检查短消息（避免超长文本做前缀匹配）
        if len(text) <= 20:
            if text.startswith("/approve"):
                approval_state.resolved = True
                approval_state.approved = True
            elif text.startswith("/deny"):
                approval_state.resolved = True
                approval_state.approved = False

        if not approval_state.resolved:
            # 忽略：等效拒绝
            approval_state.resolved = True
            approval_state.approved = False

        # 更新 IM 消息
        await self._ui.update_message(...)

        # 执行/填充
        await self._complete_approval_batch(session_id, approval_state)

        # 清除状态
        await approval_store.delete(session_id)

        # 恢复 agent turn
        asyncio.create_task(self._resume_agent_turn(session_id))

        return None  # 审批消息不保存到记忆 ✓

    # ── 命令拦截（原有逻辑）──
    if self.command_interceptor is not None:
        ...
```

**移除的代码**：
- `_pending_approvals` dict（lines 197）
- `has_pending_approval()` / `get_pending_approval()`（lines 728-734）
- `resume_after_approval()`（lines 736-878）— 替换为 `_complete_approval_batch` + `_resume_agent_turn`
- `_process_turn_resume()`（lines 880-998）— 替换为 `_resume_agent_turn`
- `except AgentAwaitingApproval` catch 块（lines 677-685, 974-979）

### 4.4 Pipeline 新增方法

```python
async def _complete_approval_batch(
    self, session_id: str, state: ApprovalSessionState,
) -> None:
    """审批完成后：执行 tool 或填充错误，并写入 history。"""
    ctx_mgr = self._get_ctx_mgr(session_id)
    context_state = await ctx_mgr.load(session_id)
    history = context_state.history

    for tool_info in state.pending_tools:
        if state.approved:
            # 真实执行
            result = await self.tool_manager.execute(
                tool_info.tool_name, tool_info.arguments,
            )
        else:
            # 填充错误
            result = ToolResult(
                tool_name=tool_info.tool_name,
                call_id=tool_info.tool_call_id,
                error=f"Tool '{tool_info.tool_name}' was not approved by the user.",
            )
        result.call_id = tool_info.tool_call_id
        await history.append(result.to_message())

    await ctx_mgr.flush(session_id)


async def _resume_agent_turn(self, session_id: str) -> None:
    """审批完成后恢复 agent 推理。"""
    ctx_mgr = self._get_ctx_mgr(session_id)
    context_state = await ctx_mgr.load(session_id)

    agent_context = AgentContext(
        system_prompt=context_state.system_prompt,
        history=context_state.history,
        tool_manager=self.tool_manager,
        session_id=session_id,
        max_iterations=self.max_iterations,
        metadata={"session_id": session_id},
        hooks=self.hooks,
        hook_runner=self.hook_runner,
        interceptor_chain=self.interceptor_chain,
        governance=self.governance,
        safety=self.safety,
        ...
    )

    emitter = self.emitter_factory(session_id) if self.emitter_factory else ...
    result = await self.agent.run(agent_context, emitter)
    await ctx_mgr.save(session_id, user_message=None, assistant_result=result)
```

### 4.5 IMCommandRouter — 简化

```python
# examples/bot_project/bot/command_router.py

class IMCommandRouter:
    def __init__(self, *, channel: ControlChannel) -> None:
        self._channel = channel
        # ❌ 移除 on_approval_response 回调

    async def handle_message(self, session_id: str, raw_text: str) -> bool:
        text = raw_text.strip().lower()
        # ❌ 不再发送 ControlCommand(APPROVAL_RESPONSE)
        # 审批消息由 pipeline 通过 ApprovalStateStore 直接处理
        # 此 router 仅处理 /yolo 等不依赖审批状态的命令
        if text.startswith("/yolo"):
            await self._channel.send(ControlCommand(
                type=ControlCommandType.SET_DYNAMIC_CONFIG,
                scope=ControlScope(session_id=session_id),
                payload={"approval_yolo": True},
            ))
            return True
        return False
```

> `/approve` 和 `/deny` 不再由 CommandRouter 处理，而是由 Pipeline 通过 `ApprovalStateStore` 直接识别。CommandRouter 只处理与审批状态无关的命令（如 `/yolo`）。

### 4.6 BotService — 组装

```python
# examples/bot_project/bot/service/core.py

async def initialize(self):
    ...
    # StateStore: JSON 文件持久化
    data_dir = self._resolve_path("data_dir", "data")
    state_dir = data_dir / "state"
    state_store = JsonFileStateStore(state_dir)

    # ApprovalStateStore
    self._approval_state_store = InMemoryApprovalStateStore(state_store)

    # IM UI (发送审批消息)
    self._im_ui = IMUserInterface(
        output_adapter=self.output_adapter,
        channel=self.control_channel,
    )

    # TieredToolApprovalInterceptor (不再需要 wait_strategy)
    self._approval_interceptor = TieredToolApprovalInterceptor(
        hardline_matcher=...,
        dangerous_matcher=...,
        sensitive_matcher=...,
        approval_ui=self._im_ui,
        event_bus=getattr(self, "event_bus", None),
        approval_timeout=300.0,
        on_denied=DenyAction.TOOL_ERROR,
        on_timeout=TimeoutAction.TOOL_ERROR,
        # ❌ 移除: wait_strategy, approval_store
    )

    # Pipeline 需要 ApprovalStateStore 的引用
    self.pipeline = AgentPipeline(
        ...
        approval_state_store=self._approval_state_store,  # ← 新增
        ...
    )
```

---

## 五、YOLO 模式处理

YOLO 模式不再使用独立的 `ApprovalStore`，而是作为 interceptor 的内存标记：

```python
class TieredToolApprovalInterceptor:
    def __init__(self, ...):
        self._yolo_sessions: set[str] = set()  # 内存标记，重启丢失

    # sensitive tier 检查
    if tier == ApprovalTier.SENSITIVE and session_id in self._yolo_sessions:
        return await next_call()  # 跳过审批
```

如需持久化 YOLO 模式，后续可通过 `StateStore` 实现，不在此次改动范围。

---

## 六、移除清单

| 移除项 | 文件 | 原因 |
|--------|------|------|
| `SuspendResumeWaitStrategy` | `control/wait_strategy.py:48-88` | 不再需要挂起-恢复等待 |
| `AgentAwaitingApproval` 异常 | `control/exceptions.py:74` | 不再通过异常传播暂停信号 |
| `CheckpointStore` ABC + 实现 | `control/checkpoint/` 整个子包 | 审批不再使用 checkpoint 存储 |
| `ApprovalStore` ABC + `StateStoreBackedApprovalStore` | `approval/abc.py` + `approval/builtin/store.py` | 替换为 ApprovalStateStore |
| `pipeline._pending_approvals` dict | `pipeline/pipeline.py:197` | 替换为 ApprovalStateStore |
| `pipeline.resume_after_approval()` | `pipeline/pipeline.py:736-878` | 替换为 _complete_approval_batch + _resume_agent_turn |
| `pipeline._process_turn_resume()` | `pipeline/pipeline.py:880-998` | 替换为 _resume_agent_turn |
| `ReActAgent` 中 `AgentAwaitingApproval` catch | `agents/react/agent.py:324-361` | 替换为 _approval_suspended 标记 |
| `ReActAgent` 中 `_approval_batch_denied` 补齐 | `agents/react/agent.py:245-268` | 替换为 break + Pipeline 统一处理 |
| `IMCommandRouter._on_approval_response` | `bot/command_router.py:28-31,48-61` | 不再需要回调 |
| `IMCommandRouter` 中 `/approve` `/deny` 处理 | `bot/command_router.py:41-62` | 移至 Pipeline 入口 |
| `BotService._handle_approval_response()` | `bot/service/core.py:692-700` | 不再需要 |

---

## 七、与旧设计对比

| 维度 | 旧设计 | 新设计 |
|------|--------|--------|
| **暂停方式** | `SuspendResumeWaitStrategy` 抛 `AgentAwaitingApproval` 跨越 4 层 | interceptor 设 `_approval_suspended=True` → ReActAgent break → 正常返回 |
| **状态存储** | `CheckpointStore` (ABC → StateStoreBackedCheckpointStore) 序列化 AgentCheckpoint | `ApprovalStateStore` (ABC → InMemory/JsonFile) 仅存 tool 列表 |
| **恢复机制** | Pipeline `resume_after_approval` + `_process_turn_resume` 异步任务 | Pipeline `_complete_approval_batch` + `_resume_agent_turn` |
| **审批消息路由** | `IMCommandRouter` 发送 `ControlCommand` + 回调 `_handle_approval_response` | Pipeline 入口通过 `ApprovalStateStore` 直接识别 |
| **Tool 信息源** | `AgentCheckpoint.denial_context` 序列化字段 | 记忆文件末尾 assistant 消息 |
| **YOLO 模式** | `ApprovalStore` ABC + `StateStoreBackedApprovalStore` | interceptor 内存 `_yolo_sessions: set[str]` |
| **组件数量** | 8 个 ABC + 10+ 实现类 | 2 个 ABC + 4 实现类 |
| **跨层耦合** | Interceptor → ReActAgent → Pipeline → CommandRouter (4 层) | Interceptor → ReActAgent → Pipeline (3 层，仅标记传递) |

---

## 八、新增/修改文件清单

| 文件 | 操作 | 说明 |
|------|:----:|------|
| `framework/approval/state.py` | **新增** | `ApprovalSessionState`, `PendingToolInfo`, `ApprovalStateStore` ABC, `InMemoryApprovalStateStore` |
| `framework/approval/abc.py` | 修改 | 追加 `ApprovalStateStore` ABC；`ApprovalStore` 标记 deprecated |
| `framework/approval/builtin/interceptor.py` | 修改 | 移除 wait_strategy/approval_store 依赖；改为创建 ApprovalSessionState；批量审批 |
| `framework/agents/react/agent.py` | 修改 | 注入 `_pending_tool_calls`；`_approval_suspended` 检测 + break；移除 exception catch 块 |
| `framework/pipeline/pipeline.py` | 修改 | 新增 `approval_state_store` 参数；`_process_message_locked` 中审批状态检查；`_complete_approval_batch`；`_resume_agent_turn`；移除 resume_after_approval 等 |
| `framework/control/wait_strategy.py` | 修改 | `SuspendResumeWaitStrategy` 标记 deprecated（保留但不再使用） |
| `framework/control/exceptions.py` | 修改 | `AgentAwaitingApproval` 标记 deprecated |
| `examples/bot_project/bot/service/core.py` | 修改 | 创建 `ApprovalStateStore`；移除 `CheckpointStore`；简化 interceptor 构建 |
| `examples/bot_project/bot/command_router.py` | 修改 | 移除 `/approve` `/deny` 处理 + 回调 |
| `framework/interceptor/builtin/__init__.py` | 修改 | 更新 exports |

---

## 九、关键行为验证

| 场景 | 预期行为 |
|------|---------|
| 用户 `/approve`（有审批状态） | 所有 tool 执行 → tool results 写入 history → agent 继续推理 |
| 用户 `/deny`（有审批状态） | 所有 tool 不执行 → error results 写入 history → agent 继续推理 |
| 用户发送普通消息（有审批状态） | 等效 deny → error results 写入 history → agent 继续推理 |
| 用户 `/approve`（无审批状态） | 按普通用户消息处理 → LLM 可见 "/approve" 文本 |
| 多个 tool_call 同批 | assistant 消息含所有 tool_calls → 审批消息展示所有 tool → 全量通过/拒绝 |
| 审批期间的 IM 消息 | 发送审批请求到 QQ → `/approve` 后 update 原消息为 "✅ Approved" |
| Pool 模式 | 与 Pipeline 模式行为一致（审批状态在 Pipeline 入口统一检查） |
| 重启恢复 | `JsonFileStateStore` 持久化审批状态 → 重启后 `/approve` 仍可恢复 |
