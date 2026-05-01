# Agent 审批体系重新设计

## 一、问题诊断

当前实现的根本问题：**审批消息和用户消息的边界模糊**。

1. Pipeline 的 `command_interceptor` 检查在 `_process_message_locked` 内部，但 `busy check` 在外部先执行
2. InlineWaitStrategy 阻塞期间，`/approve` 消息被 busy check 排入 injection queue，随后被当作 `[Injected]` 注入上下文
3. SuspendResumeWaitStrategy 挂起后，`checkpoint_store` 未传递到 Pipeline/AgentContext，checkpoint 从未保存，resume 静默失败
4. 最终结果：`/approve` 作为 user role 消息进入 memory，LLM 产生循环生成

核心原则（借鉴 hermes-agent 和 nanobot）：

> **在审批状态下，所有进入的消息都是审批应答，NEVER 作为 user 消息写入记忆。**

## 二、共享抽象层（两套策略通用）

### 2.1 `parse_approval_action()` — 纯函数

```python
# framework/approval/response.py

from framework.approval.types import ApprovalAction

_APPROVE_ALIASES = frozenset({
    "/approve", "approve", "/allow", "allow", "/yes", "yes", "/ok", "ok",
})
_DENY_ALIASES = frozenset({
    "/deny", "deny", "/reject", "reject", "/no", "no", "/cancel", "cancel",
})

def parse_approval_action(text: str) -> ApprovalAction | None:
    """将用户文本解析为审批动作。纯函数，无副作用，两套策略共享。"""
    if len(text) > 30:       # 剪枝：审批指令不会超过 30 字符
        return None
    cmd = text.strip().lower()
    if cmd in _APPROVE_ALIASES:
        return ApprovalAction.ALLOW
    if cmd in _DENY_ALIASES:
        return ApprovalAction.DENY
    return None
```

### 2.2 `ApprovalState` — 不可变状态快照

```python
# framework/approval/state.py

@dataclass(frozen=True)
class ApprovalState:
    """一次 agent turn 中所有需要审批的 tool_call 的审批进度。
    不可变：每个 apply() 返回新实例。
    """
    session_id: str
    tool_requests: tuple[ApprovalRequest, ...]   # batch 全部 tool（正常 tier 的也在此）
    current_index: int                            # 当前正在提示用户的 tool 索引
    resolutions: tuple[tuple[str, ApprovalResolution], ...]

    @property
    def pending(self) -> ApprovalRequest | None:
        if self.current_index < len(self.tool_requests):
            return self.tool_requests[self.current_index]
        return None

    @property
    def all_resolved(self) -> bool:
        return self.current_index >= len(self.tool_requests)

    @property
    def all_approved(self) -> bool:
        return self.all_resolved and all(
            r == ApprovalResolution.ALLOWED
            for _, r in self.resolutions
        )

    def apply(self, tool_call_id: str, resolution: ApprovalResolution) -> ApprovalState:
        new_resolutions = (*self.resolutions, (tool_call_id, resolution))
        return ApprovalState(
            session_id=self.session_id,
            tool_requests=self.tool_requests,
            current_index=self.current_index + 1,
            resolutions=new_resolutions,
        )
```

### 2.3 `ApprovalStateManager` — ABC

```python
# framework/approval/state.py

class ApprovalStateManager(ABC):
    """审批状态管理器抽象。"""

    @abstractmethod
    async def get(self, session_id: str) -> ApprovalState | None: ...
    @abstractmethod
    async def save(self, state: ApprovalState) -> None: ...
    @abstractmethod
    async def clear(self, session_id: str) -> None: ...
```

两个实现：

```python
class InMemoryApprovalStateManager(ApprovalStateManager):
    """InlineWaitStrategy 使用。进程内 dict，重启丢失。"""

class StateStoreBackedApprovalStateManager(ApprovalStateManager):
    """SuspendResumeWaitStrategy 使用。基于 StateStore，重启可恢复。"""
```

## 三、两套策略

### 3.1 InlineWaitStrategy

- **wait() 行为**：阻塞在 `channel.drain(APPROVAL_RESPONSE)`，直到 pipeline 写入响应
- **状态管理**：`InMemoryApprovalStateManager`。Interceptor 在 wait() 前保存状态
- **ALLOW 唤醒**：Pipeline 写入 APPROVAL_RESPONSE → wait() 返回 → interceptor 继续执行 tool
- **IGNORE 处理**：Pipeline 填充伪结果 → 取消当前协程 → 压制 CancelledError → 处理用户新消息

### 3.2 SuspendResumeWaitStrategy

- **wait() 行为**：立即 raise `AgentAwaitingApproval`。ReActAgent 保存检查点。Pipeline 保存 ApprovalState
- **状态管理**：`StateStoreBackedApprovalStateManager`。Pipeline 在捕获异常后保存状态
- **ALLOW 唤醒**：Pipeline 执行 tool → 填充结果 → 派生 `_resume_agent_turn()`
- **IGNORE 处理**：填充伪结果 → 清理状态 → 处理用户新消息（协程已销毁）

### 3.3 两套策略只有两处差异

| 差异点 | Inline | SuspendResume |
|--------|--------|---------------|
| `ApprovalStateManager` 实现 | `InMemory*` | `StateStoreBacked*` |
| ALLOW: tool 由谁执行 | ReActAgent tool loop（协程苏醒后正常执行） | Pipeline 独立执行（协程已死） |
| ALLOW: tool 结果如何写入 | ReActAgent 的 `_build_tool_message` 正常追加 | Pipeline 读 memory → 执行 → 插入 assistant 消息后方 |
| ALLOW: 如何继续 | interceptor 返回 `next_call()` → agent 自然继续 | `agent.run()` 全新启动，LLM 从带结果的 history 继续 |

其余代码 100% 共享：`parse_approval_action`、`ApprovalState`、`_try_consume_approval`、`_fill_pseudo_results`（DENIED/IGNORED 路径）、`TieredToolApprovalInterceptor`。

## 四、Pipeline 集成

### 4.1 入口检查（唯一的 pipeline 变更点）

```python
# pipeline._process_message()
async def _process_message(self, input_msg):
    session_id = ...
    text = getattr(input_msg, 'content', '') or ''

    # ═══════ 审批状态检查（在 busy check 和 lock 之前） ═══════
    if await self._try_consume_approval(text, session_id):
        return None   # 审批消息 → 永不写入记忆

    # ── busy check ──
    # ── lock → _process_message_locked ──
    ...  # 其他代码不变
```

### 4.2 `_try_consume_approval` — 策略多态入口

```python
async def _try_consume_approval(self, text: str, session_id: str) -> bool:
    state = await self._approval_manager.get(session_id)
    if state is None:
        return False

    action = parse_approval_action(text)

    if action == ApprovalAction.DENY:
        # 明确拒绝 → 全部 tool 不执行，退出 agent run
        await self._deny_approval_batch(session_id, state)
        return True

    if action == ApprovalAction.ALLOW:
        state = state.apply(state.pending.tool_call_id, ApprovalResolution.ALLOWED)
        if state.all_approved:
            await self._execute_approved_batch(session_id, state)
        else:
            await self._approval_manager.save(state)
            await self._im_ui.render_message(
                session_id, self._format_approval_message(state.pending))
        return True

    # action is None → 非审批消息
    if _is_user_message(input_msg):
        # 用户消息但非审批指令 → IGNORED
        await self._ignore_approval_batch(session_id, state)
        return False   # 用户消息继续正常处理
    else:
        # 非用户消息（如 peer/subagent 的 agent 消息）→ 入队阻塞
        await self._enqueue_during_approval(session_id, input_msg)
        return True    # 已拦截，不写入 memory
```

### 4.3 三种终结路径

#### 路径 A: ALLOWED（全部通过）

```
_try_consume_approval → ALLOW → all_approved=YES
  │
  ▼
_execute_approved_batch():
  1. await _approval_manager.clear(session_id)
  2. 策略分叉：

  ┌─ InlineWaitStrategy ─────────────────────────────────────┐
  │ Pipeline 写入 APPROVAL_RESPONSE(action="allow") 到 channel │
  │ → 唤醒 interceptor 的 wait()                               │
  │ → interceptor 返回 await next_call()                       │
  │ → ReActAgent._execute_tool_raw() 正常执行 tool             │
  │ → ReActAgent._build_tool_message() 正常追加到 history      │
  │ → tool loop 继续下一个 tool_call                           │
  │                                                           │
  │ Pipeline 不执行 tool、不填充结果。                          │
  │ 协程一直存活，所有 tool 执行和结果写入走 ReActAgent 标准路径。│
  └──────────────────────────────────────────────────────────┘

  ┌─ SuspendResumeWaitStrategy ──────────────────────────────┐
  │ 协程已销毁。Pipeline 独立完成 tool 执行 + 结果插入：       │
  │                                                           │
  │ a) 从 memory 文件读取最后一个 assistant 消息的 tool_calls  │
  │    （真实来源，格式: [{id, type, function: {name, args}}]）│
  │                                                           │
  │ b) 逐一调用 tool_manager.execute(tc_name, tc_args)        │
  │    注意：不通过 agent、不通过 interceptor。                │
  │    直接调用 ToolManager，绕过审批层。                      │
  │                                                           │
  │ c) 将每个 ToolResult 构建为 tool-role 消息，               │
  │    插入到 history 中对应 assistant 消息后方：             │
  │      assistant(tool_calls=[tc1, tc2, tc3])               │
  │      tool(tool_call_id=tc1.id, content=<result>)          │
  │      tool(tool_call_id=tc2.id, content=<result>)          │
  │      tool(tool_call_id=tc3.id, content=<result>)          │
  │                                                           │
  │ d) _resume_agent_turn(session_id):                        │
  │     → 加载更新后的 context_state（含 tool 结果）           │
  │     → 构建 AgentContext                                   │
  │     → agent.run() 全新启动                                │
  │     → LLM 看到完整 history → 生成文本回复                  │
  └──────────────────────────────────────────────────────────┘
```


#### 路径 B: DENIED（明确拒绝）— 两策略共享

```
_deny_approval_batch():
  1. 所有剩余 tool 标记为 DENIED/PREEMPTED
  2. 遍历 memory 中所有 tool_calls → 填充伪结果:
     ALLOWED（之前批准的）→ ToolResult(error="批次被拒绝，此工具未执行")
     DENIED              → ToolResult(error="工具被用户拒绝")
     PREEMPTED           → ToolResult(error="前序工具拒绝，此工具未执行")
  3. await _approval_manager.clear(session_id)
  4. 退出 agent run:
     Inline:         取消 session task → 等待 CancelledError → 压制
     SuspendResume:  清理检查点 → 不派生新 turn
  5. _drain_approval_queue(session_id)  # 放行排队的 agent 消息
  6. 返回 True（消息已消费）
```

#### 路径 C: IGNORED（用户输入无关内容）— 两策略共享

```
_ignore_approval_batch():
  1. 所有剩余 tool 标记为 IGNORED
  2. 遍历 memory 中所有 tool_calls → 填充伪结果:
     ALLOWED（之前批准的）→ ToolResult(error="用户发送无关消息，批次被忽略")
     IGNORED             → ToolResult(error="用户发送无关消息，工具未执行")
     PREEMPTED           → ToolResult(error="前序工具被忽略，此工具未执行")
  3. await _approval_manager.clear(session_id)
  4. 退出当前 agent run:
     Inline:         取消 session task → 等待 CancelledError → 压制
     SuspendResume:  清理检查点 → 不派生新 turn
  5. _drain_approval_queue(session_id)  # 放行排队的 agent 消息
  6. 返回 False → 用户消息继续走正常 pipeline（保存到记忆 → agent.run() 新 turn）
```

### 4.4 `_fill_batch_results` — 共享的批量结果填充

DENIED/IGNORED 路径共用，SuspendResume 的 ALLOWED 路径也用（执行真实 tool）。

```python
async def _fill_batch_results(
    self, session_id: str, state: ApprovalState, *,
    execute_real: bool = False,
) -> None:
    """为所有 tool_call 填充结果到 history。

    从 memory 读取最后一个 assistant 消息的 tool_calls（真实来源）。
    将结果为 tool-role 消息，按 tool_call_id 顺序插入 assistant 消息后方。

    Args:
        execute_real: True=SuspendResume ALLOWED 路径，执行真实 tool
                      False=DENIED/IGNORED 路径，全部填充伪结果
    """
    all_tool_calls = await self._read_last_assistant_tool_calls(session_id)
    resolution_map = dict(state.resolutions)

    for tc in all_tool_calls:
        tc_id = tc["id"]
        tc_name = tc["function"]["name"]
        tc_args = json.loads(tc["function"].get("arguments", "{}"))
        resolution = resolution_map.get(tc_id, ApprovalResolution.PREEMPTED)

        if execute_real and resolution == ApprovalResolution.ALLOWED:
            # 绕过审批层，直接调用 ToolManager
            result = await self.tool_manager.execute(tc_name, tc_args)
            result.call_id = tc_id
        else:
            error_msg = _ERROR_TEMPLATES[resolution].format(name=tc_name)
            result = ToolResult(tool_name=tc_name, call_id=tc_id, error=error_msg)

        tool_msg = {
            "role": "tool",
            "tool_call_id": tc_id,
            "name": tc_name,
            "content": result.error or str(result.result or " "),
        }
        await self._append_to_history(session_id, tool_msg)
```

**注意**：`execute_real=True` 时直接调用 `tool_manager.execute()`，不经过 interceptor 链。审批已通过，不需要再次审批。

### 4.5 伪结果错误信息

```python
_ERROR_TEMPLATES = {
    ApprovalResolution.DENIED:    "Tool '{name}' was denied by user.",
    ApprovalResolution.IGNORED:   "Tool '{name}' was ignored (user sent unrelated message).",
    ApprovalResolution.PREEMPTED: "Tool '{name}' was not executed — prior tool in batch was denied/ignored.",
    ApprovalResolution.TIMED_OUT: "Tool '{name}' was not executed — approval timed out.",
}
```

## 五、审批期间的 agent 消息入队

### 5.1 问题

SuspendResume 策略下 agent 协程已销毁，但 session 仍可能收到非 user 消息——例如 bot_project 中 peer agent 的回复（role=agent, source_agent=peer）。

若此消息在审批未决时写入 memory，将插入到 assistant(tool_calls) 和 tool result 之间，破坏 OpenAI/Anthropic 协议要求的连续对应关系：

```
assistant(tool_calls=[tc1, tc2])    ← 审批未决
agent(peer_reply)                   ← 插入！破坏了 tool_call 对应
tool(tc1.result)                    ← 错位
tool(tc2.result)                    ← 错位
```

### 5.2 方案：`_pending_approval_queue`

在 `AgentPipeline.__init__` 中新增：

```python
self._pending_approval_queues: dict[str, asyncio.Queue[InputMessage]] = {}
```

在审批未决期间，非 user 消息（`source_agent` 非空 或 role=agent）入队等待。审批终结后按序放行。

### 5.3 `_try_consume_approval` 中的入队分支

```python
# 在 _try_consume_approval 中：
# action is None → 非审批消息
if _is_user_message(input_msg):
    # 用户消息但非审批指令 → IGNORED
    await self._ignore_approval_batch(session_id, state)
    return False
else:
    # agent 消息 → 入队等待审批完成
    queue = self._pending_approval_queues.setdefault(
        session_id, asyncio.Queue(maxsize=50))
    await queue.put(input_msg)
    logger.debug("Queued agent message during approval: %s", session_id)
    return True
```

### 5.4 审批终结后放行

在每个终结路径（ALLOWED / DENIED / IGNORED）的 `_finalize_batch` 或恢复流程完成后，消费并处理队列中的消息：

```python
async def _drain_approval_queue(self, session_id: str) -> None:
    """审批结束后按序消费 pending 队列中的 agent 消息。"""
    queue = self._pending_approval_queues.pop(session_id, None)
    if queue is None:
        return
    while not queue.empty():
        msg = queue.get_nowait()
        # 走正常消息处理流程（此时无审批状态，不会被拦截）
        asyncio.create_task(self._process_message(msg))
```

调用时机：
- SuspendResume ALLOWED: 在 `_resume_agent_turn` 中，tool 结果写入后、`agent.run()` 之前
- DENIED / IGNORED: 伪结果填充完成后立即调用
- Inline ALLOWED: 不需要（协程未中断，history 完整）

### 5.5 为什么 user 消息不排队

user 消息在审批期间有两种情况：
- **审批指令**（`/approve` `/deny`）：由 `parse_approval_action` 识别 → 更新 `ApprovalState`
- **非审批指令**（无关内容）：触发 IGNORED 路径 → 填充伪结果 → 消息正常处理

两者都直接消费审批状态，不需要排队。

## 六、Interceptor 简化

新设计下 interceptor 只做：

1. 从 `context.metadata["_pending_tool_calls"]` 获取本轮全部 tool_calls
2. 为每个 tool 构建 `ApprovalRequest`（标记 tier）
3. 创建 `ApprovalState` → `state_manager.save(state)`
4. 发送第一个审批提示 → `ui.render_message()`
5. 调用 `wait_strategy.wait()` → 阻塞（Inline）或抛异常（SuspendResume）

之后全部逻辑由 Pipeline 的 `_try_consume_approval` 接管。

## 七、ReActAgent 微调

### 6.1 存储批量 tool_calls（一行新增）

```python
# ReActAgent.run() — tool loop 前加一行
if tool_calls:
    context.metadata["_pending_tool_calls"] = tool_calls
    for idx, tool_call in enumerate(tool_calls):
        ...
```

### 6.2 SuspendResume 策略下无需跳过逻辑

SuspendResume 的 ALLOWED 路径：Pipeline 执行所有 tool 并填充结果后，通过 `_resume_agent_turn()` → `agent.run()` 全新启动。不走 tool loop 恢复，所以不需要"跳过已有结果"逻辑。

Inline 的 ALLOWED 路径：channel 信号唤醒 interceptor → `next_call()` 正常执行 → tool loop 自然继续。也不需要跳过逻辑，因为只有当前 tool 需要审批，之前的 tool 已经执行过了。

## 八、全部流程一览

### 场景 1：全部通过（两策略对比）

```
用户: "帮我读 config.yml 然后修改 timeout"
LLM:  assistant(tool_calls=[read_file(config.yml), edit_file(config.yml)])

ReActAgent tool loop:
  tc1(read_file) → normal tier → 直接执行 → ToolResult ✓ → 追加到 history
  tc2(edit_file) → sensitive tier → interceptor._request_approval():
      ApprovalState([tc1✓, tc2?])
      ui.render_message("Approve edit_file? /approve /deny")
      wait_strategy.wait()
        ├─ Inline:         协程阻塞在 channel.drain()
        └─ SuspendResume:  抛 AgentAwaitingApproval → agent 协程销毁, checkpoint 持久化

用户: "/approve"
Pipeline._try_consume_approval → ALLOW → all_approved=YES

  ┌─ InlineWaitStrategy ──────────────────────────────────────┐
  │ 1. channel 写入 APPROVAL_RESPONSE("allow")                │
  │ 2. interceptor.wait() 返回 WaitResult(value="allow")      │
  │ 3. interceptor 返回 await next_call()                     │
  │ 4. ReActAgent._execute_tool_raw() 执行 edit_file          │
  │ 5. ReActAgent._build_tool_message() 追加到 history        │
  │ 6. agent 自然继续 → LLM 生成文本回复                       │
  │                                                           │
  │ 整个过程中 ReActAgent 协程未中断。                          │
  │ tool 执行和结果写入走标准路径，审批层透明。                  │
  └──────────────────────────────────────────────────────────┘

  ┌─ SuspendResumeWaitStrategy ───────────────────────────────┐
  │ 1. _fill_batch_results(execute_real=True):                │
  │    a) 从 memory 读取 assistant.tool_calls                  │
  │       [{id:tc1, name:read_file}, {id:tc2, name:edit_file}]│
  │    b) tc1: 已有真实结果（之前已执行）→ 跳过               │
  │    c) tc2: ALLOWED → tool_manager.execute(edit_file, ...) │
  │       注意: 直接调用 tool_manager，绕过 interceptor 链    │
  │    d) 构建 tool-role 消息插入 history:                    │
  │         assistant(tool_calls=[tc1, tc2])                  │
  │         tool(tool_call_id=tc1, <已存在的真实结果>)         │
  │         tool(tool_call_id=tc2, <刚执行的结果>)             │
  │ 2. _resume_agent_turn(session_id):                        │
  │    → 加载含 tool 结果的 context                           │
  │    → agent.run() 全新启动                                 │
  │    → LLM 看到完整 tool 结果 → 生成文本回复                 │
  └──────────────────────────────────────────────────────────┘

用户收到: "已将 timeout 修改为 60s"
```

> **关键差异**：Inline 的 tool 执行在 ReActAgent 内部（正常路径）；SuspendResume 的 tool 执行在 Pipeline 内部（独立调用，绕过 agent 和 interceptor），执行完毕后通过 agent.run() 全新启动继续。

### 场景 2：明确拒绝

```
用户: "/deny"
Pipeline: _try_consume_approval → DENY
  → 标记剩余 tool 为 DENIED/PREEMPTED
  → _fill_pseudo_results:
      tc1: 已执行 → 跳过 (已有真实结果)
      tc2: DENIED → ToolResult(error="denied by user")
  → 退出 agent run → 清理状态
  → 返回 True (消息已消费)

下次用户消息开始新 turn。
```

### 场景 3：忽略（无关消息）

```
用户: "帮我看看天气"  (而非 /approve)
Pipeline: _try_consume_approval → action=None → IGNORED
  → 标记剩余 tool 为 IGNORED
  → _fill_pseudo_results:
      tc1: 已执行 → 跳过
      tc2: IGNORED → ToolResult(error="ignored")
  → 退出 agent run → 清理状态
  → 返回 False → "帮我看看天气" 走正常流程:
      → 保存 user 消息到 memory
      → agent.run() 新 turn
```

## 九、待移除的冗余

| 当前文件 | 移除内容 | 理由 |
|----------|---------|------|
| `pipeline.py` | `command_interceptor` 参数及 `_process_message_locked` 中的拦截逻辑 | 移至 `_try_consume_approval` |
| `pipeline.py` | `_pending_approvals` dict | 被 `ApprovalStateManager` 取代 |
| `pipeline.py` | `resume_after_approval()` | 被 `_execute_approved_batch` + `_resume_agent_turn` 取代 |
| `pipeline.py` | `_process_turn_resume()` | 简化进 `_resume_agent_turn` |
| — | **新增**: `_pending_approval_queues` + `_drain_approval_queue` | 审批期间 agent 消息入队，终结后放行 |
| `bot/command_router.py` | `/approve` `/deny` 处理 + `on_approval_response` 回调 | 被 `_try_consume_approval` 取代 |
| `bot/service/core.py` | `_handle_approval_response` | Pipeline 内部处理 |
| `bot/service/core.py` | `command_interceptor=self._command_router` 传递 | 不再需要 |
| `wait_strategy.py` | SuspendResume 的 2s quick poll | 不需要 |
| `multi_agent/factory.py` | `command_interceptor` 参数 | 不再需要 |

## 十、新增/修改文件清单

| 文件 | 操作 | 说明 |
|------|:----:|------|
| `framework/approval/response.py` | **新增** | `parse_approval_action()` 纯函数 |
| `framework/approval/state.py` | **新增** | `ApprovalState` + `ApprovalStateManager` ABC + 两个实现 |
| `framework/control/wait_strategy.py` | 修改 | SuspendResume 移除 2s quick poll |
| `framework/approval/builtin/interceptor.py` | 修改 | `_request_approval` 简化：创建状态 → 等待 |
| `framework/pipeline/pipeline.py` | 修改 | `_try_consume_approval` + 三条终结路径 |
| `framework/agents/react/agent.py` | 修改 | 一行: `context.metadata["_pending_tool_calls"] = tool_calls` |
| `examples/bot_project/bot/service/core.py` | 修改 | 装配 `ApprovalStateManager`；移除旧审批回调 |
| `examples/bot_project/bot/command_router.py` | 修改 | 移除 `/approve` `/deny`；保留 `/yolo` |
| `framework/multi_agent/factory.py` | 修改 | 移除 `command_interceptor` 参数 |
| `framework/approval/types.py` | 不变 | 已有枚举足够 |
| `framework/approval/abc.py` | 不变 | 已有 dataclass 足够 |
| `framework/control/channel.py` | 不变 | — |
| `framework/control/ui/*` | 不变 | — |
| `framework/control/exceptions.py` | 不变 | `AgentAwaitingApproval` 已存在 |
| `framework/multi_agent/descriptor.py` | 不变 | — |
| `examples/bot_project/bot/service/builders.py` | 不变 | — |

### 场景 4：审批期间收到 agent 消息（SuspendResume 特有）

```
审批状态: 等待用户对 tc2(shell) 的审批

peer agent 回复到达 (role=agent, source_agent=peer):
Pipeline._try_consume_approval:
  → parse_approval_action("peer的回复内容") → None
  → _is_user_message → False (source_agent 非空)
  → enqueue → 入队等待
  → 返回 True (已拦截，不写入 memory)

用户: "/approve"
Pipeline._try_consume_approval:
  → parse_approval_action("/approve") → ALLOW
  → _execute_approved_batch() → 执行 tool → 填充结果
  → _drain_approval_queue():
      消费 peer 消息 → asyncio.create_task(_process_message(peer_msg))
      → 此时无审批状态 → 正常处理 → 保存到 memory → agent.run()

结果: tool/tool_call 对应关系完整，peer 消息在 tool 结果之后按序处理。
```

## 十一、装配示例（bot_project）

```python
# bot/service/core.py — initialize() 中的审批部分

# StateStore: JSON 文件持久化（重启可恢复）
data_dir = self._resolve_path("data_dir", "data")
state_dir = data_dir / "state"
self._state_store = JsonFileStateStore(state_dir)

# — 策略 A: SuspendResume（两行切换）—
self._approval_manager = StateStoreBackedApprovalStateManager(self._state_store)
self._wait_strategy = SuspendResumeWaitStrategy(
    checkpoint_store=StateStoreBackedCheckpointStore(self._state_store),
    channel=self.control_channel,
)

# — 策略 B: Inline（只需这两行不同）—
# self._approval_manager = InMemoryApprovalStateManager()
# self._wait_strategy = InlineWaitStrategy(channel=self.control_channel)

# — 审批拦截器（共享，不感知策略差异）—
self._approval_interceptor = TieredToolApprovalInterceptor(
    hardline_matcher=ExactNameMatcher({"rm_rf_root", "dd_raw_device"}),
    dangerous_matcher=ExactNameMatcher({"shell", "delete_file"}),
    sensitive_matcher=ArgumentSensitiveMatcher(
        tool_names={"read_file", "write_file", "edit_file", "list_dir", "shell"},
        allowed_dirs={project_dir, data_dir},
        path_arg_names={"path", "file_path", "directory", "dir"},
    ),
    approval_ui=IMUserInterface(output_adapter=self.output_adapter, channel=self.control_channel),
    approval_store=StateStoreBackedApprovalStore(self._state_store),
    wait_strategy=self._wait_strategy,
    state_manager=self._approval_manager,
)

# — Pipeline 接收 ApprovalStateManager —
self.pipeline = AgentPipeline(
    ...
    approval_manager=self._approval_manager,
    im_ui=self._im_ui,
)
```
