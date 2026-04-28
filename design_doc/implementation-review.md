# Hook / Interceptor / Control 实现检视报告

> 检视日期：2026-04-29
> 对照文档：`design_doc/hook-interceptor-control-design-plan.md`、`design_doc/hook-interceptor-control-review.md`、`design_doc/intercept-implementation-plan.md`
> 检视范围：`framework/hook/`、`framework/interceptor/`、`framework/control/`、`framework/agents/react/agent.py`、`framework/core/agent.py`、`framework/pipeline/pipeline.py`、`framework/session/agent_session.py`、`examples/bot_project/`

---

## 一、总体判断

**核心架构方向正确，三个包已按设计落地，bot_project 已切换到新导入路径。** 但存在 3 个 P0 级问题（AgentSession 未接入、ReActAgent 散落字符串未消除、on_checkpoint 未迁移到 checkpoint_store）和若干 P1/P2 级缺陷需要修复。

---

## 二、正确实现项

### 2.1 包结构与命名 ✅

- 三个包名符合设计：`framework.hook`、`framework.interceptor`、`framework.control`（单数）
- 不存在被禁止的 `framework.intercept`、`framework.intervention` 命名
- 内置模块位于 `builtin/` 子目录

### 2.2 Hook 系统 ✅

| 组件 | 设计要求 | 实现 |
|------|----------|------|
| HookPoint (StrEnum) | 枚举值 = 方法名 | ✅ 值与现有方法名完全匹配 |
| Hook (Protocol) | 可选方法协议 | ✅ 所有 9 个 hook point 有对应方法签名 |
| HookPayload/HookResult | 结构化数据 | ✅ frozen dataclass，HookResult 支持 pass_through/veto_result |
| HookErrorPolicy | IGNORE/LOG/ABORT | ✅ 默认 LOG |
| HookSpec | hook + on_error | ✅ 默认 LOG |
| HookRunner.dispatch | 统一调度 + 超时 + 错误策略 | ✅ 独立超时、策略分支、聚合 HookResult |
| HookRunner.dispatch_finalize | 同步 finalize_content 链 | ✅ 同步串行调用 |
| 内置 Hook 迁移 | 5 个内置 Hook | ✅ RunLoggingHook、RuntimeContextHook、InboxFlushHook、PeerAutoSendHook、SubagentMemoryCleanupHook |
| 旧 core/hooks.py | 废弃或删除 | ✅ 仅保留废弃说明文档字符串 |
| 旧 multi_agent/hooks.py | 清理 | ✅ 仅保留任务级 Hook（TaskProgressHook、TaskInterventionHook） |

### 2.3 Interceptor 系统 ✅

| 组件 | 设计要求 | 实现 |
|------|----------|------|
| InterceptorScope | 9 个 scope 枚举 | ✅ 全部定义 |
| Interceptor (Protocol) | scopes + around_* 方法 | ✅ 声明 scopes frozenset |
| Scope 上下文类型 | ToolCallContext 等 | ✅ frozen dataclass |
| Next-call 协议 | ToolCallNext 等 Callable 类型 | ✅ |
| InterceptorChain | 洋葱链 + scope 过滤 | ✅ 按注册顺序构建链，scope 过滤 |
| around_tool_call 兜底 | 必须返回合法 ToolResult | ✅ except AgentControlError 透传，其他异常转 ToolResult |
| around_turn/iteration 错误策略 | 普通异常向外抛出 | ✅ 不制造伪结果 |
| 内置 Interceptor | 5 个 | ✅ ControlDrain、ToolApproval、ToolTimeout、TurnTimeout、ResultLimit |
| ToolApproval deny_as_tool_error | 伪错误 ToolResult | ✅ 错误消息明确 |
| ToolApproval deny_as_cancel | 受控退出 | ✅ raise ApprovalDenied |
| ToolTimeout 从 ctx.safety 读取 | 不重复超时 | ✅ _resolve_timeout 优先自身配置，fallback ctx.safety |
| TurnTimeout 从 ctx.safety 读取 | 同上 | ✅ 同理 |

### 2.4 Control 系统 ✅

| 组件 | 设计要求 | 实现 |
|------|----------|------|
| AgentControlError 层级 | 基类 + 4 个子类 | ✅ 含 termination 字段 |
| TerminationReason 枚举 | CANCELLED/TIMEOUT/APPROVAL_DENIED/POLICY_VIOLATION | ✅ 额外 MAX_ITERATIONS/ERROR |
| ControlCommand | 结构化命令 | ✅ command_id、type、scope、source、priority、ttl、correlation_id、idempotency_key |
| ControlScope | session_id + 可选 agent_id/turn_id | ✅ frozen dataclass |
| ControlChannel 协议 | send/drain/peek | ✅ |
| InMemoryControlChannel | 内存实现 | ✅ scope 匹配过滤 |
| ControlEventBus 协议 | emit/subscribe | ✅ |
| CallbackControlEventBus | 回调实现 | ✅ |
| CheckpointStore 协议 | save/load/clear | ✅ |
| JsonFileCheckpointStore | 文件实现 | ✅ 路径安全处理 |
| NoOpCheckpointStore | 占位实现 | ✅ |
| PresetControlRule 协议 | evaluate → ControlCommand \| None | ✅ |
| TokenBudgetControlRule | 预算超限 + 冷却 | ✅ |
| ControlCommandType | 含预留类型 | ✅ PAUSE/RESUME/BACKGROUND 等 |
| ControlEventType | 含预留类型 | ✅ |

### 2.5 AgentContext 增强 ✅

AgentContext 已添加三个可选字段：
- `hook_runner: HookRunner | None`
- `interceptor_chain: InterceptorChain | None`
- `checkpoint_store: CheckpointStore | None`

### 2.6 bot_project 集成 ✅

- 所有导入已切换到新路径（`from framework.hook.builtin import ...`、`from framework.interceptor.builtin import ...`、`from framework.control.channel import ...`）
- 不存在旧路径导入（`framework.core.hooks`、`framework.multi_agent.hooks`）
- Pipeline 模式正确组装：HookRunner、InterceptorChain、InboxFlushHook
- Pool 模式通过 AgentFactory 传递 default_hook_runner / default_interceptor_chain
- InterceptorChain 装配顺序正确：ControlDrain → TurnTimeout → ToolTimeout → ResultLimit

### 2.7 ReActAgent 集成 ✅（部分）

- 已导入 AgentControlError、HookPoint、HookPayload、ToolCallContext
- `_call_hooks` 检测 hook_runner 后走新路径
- `_execute_tool` 检测 interceptor_chain 后走 around_tool_call 包裹
- AgentControlError 捕获后保存 checkpoint 并 re-raise

---

## 三、问题清单

### P0 — 必须修复

#### P0-1. AgentSession 未接入新组件

**位置**: `framework/session/agent_session.py:365-373`

AgentSession 构建 AgentContext 时只传递了 `hooks` 和 `on_checkpoint`，未传递 `hook_runner`、`interceptor_chain`、`checkpoint_store`。Session 模式（HTTP API 风格）的 agent 不会使用新的 HookRunner / InterceptorChain / CheckpointStore。

```python
# 当前（缺失）
agent_context = AgentContext(
    ...
    hooks=self._hooks,
    runtime_context_manager=self._runtime_context_manager,
)
```

**要求**: AgentSession 构造函数接受 `hook_runner`、`interceptor_chain`、`checkpoint_store` 参数，并在构建 AgentContext 时传递。

---

#### P0-2. ReActAgent 调用点仍散落硬编码字符串

**位置**: `framework/agents/react/agent.py:140,146,156,212,228-230,234,309`

设计文档 §3.1 明确要求："调用点必须使用集中定义的 HookPoint，禁止散落字符串"。当前 `_call_hooks` 的所有调用仍使用硬编码字符串：

```python
await self._call_hooks("before_turn", context)          # 应为 HookPoint.BEFORE_TURN
await self._call_hooks("after_llm_response", context, response)  # 应为 HookPoint.AFTER_LLM_RESPONSE
await self._call_hooks("before_tool_execution", context, tool_calls)
# ... 等 7 处
```

虽然 `_call_hooks` 内部用 `HookPoint(method_name)` 转换，但调用方仍使用裸字符串。如果方法名拼错或 HookPoint 枚举值变更，不会在调用方产生编译时错误。

**要求**: 所有调用点改为使用 HookPoint 枚举常量。

---

#### P0-3. on_checkpoint 未迁移到 checkpoint_store

**位置**: `framework/agents/react/agent.py:311-323`

设计文档 §7.2 明确："第一阶段在 ReActAgent 内部直接调用 ctx.checkpoint_store.save(...)"，且 review §C3 确认了这一方案。当前实现仍使用旧的 `context.on_checkpoint` 回调：

```python
async def _save_checkpoint(self, all_new_messages, context):
    if context.on_checkpoint:  # ← 旧回调
        await context.on_checkpoint(list(all_new_messages))
```

`on_checkpoint` 是旧的 Callable 字段，不是新的 `CheckpointStore` 协议。AgentContext 上虽然已声明 `checkpoint_store: CheckpointStore | None`，但 ReActAgent 从未使用它。

**要求**:
- `_save_checkpoint` 改为使用 `context.checkpoint_store.save(checkpoint_id, data)`
- `_clear_checkpoint` 改为 `context.checkpoint_store.clear(checkpoint_id)`
- 确定合理的 checkpoint_id 生成策略（如 `f"{session_id}:latest"`）
- `on_checkpoint` 字段标记废弃或移除

---

### P1 — 显著缺陷

#### P1-1. InMemoryControlChannel TTL 未实现

**位置**: `framework/control/channel.py:53-55`

设计文档 §15.6 要求："channel 实现必须支持 TTL；过期命令 drain 时丢弃并记录事件"。当前 drain() 中 TTL 检查为空注释：

```python
if cmd.ttl_seconds is not None:
    # TTL check：基于创建时间 (假设存储在 payload 中) 简化处理
    # 实际 TTL 可由外部在 send 前设置
    pass
```

**要求**: ControlCommand 需要一个 `created_at: float` 字段（默认 `time.monotonic()`），drain 时检查 `now - cmd.created_at > cmd.ttl_seconds` 则跳过并记录日志。

---

#### P1-2. ControlCommandHandler 注册机制未实现

**位置**: `framework/interceptor/builtin/control_drain.py:76-92`

设计文档 §15.6 建议："不应写成单个巨大 if/elif。建议使用 handler registry"。当前使用硬编码 if/elif：

```python
def _handle_command(self, ctx, cmd):
    if cmd_type in (ControlCommandType.CANCEL_TURN, ControlCommandType.CANCEL_RUN):
        raise AgentCancelled(reason)
    logger.debug("ControlDrain: unhandled command type=%s", ...)
```

后续添加 INJECT_USER_MESSAGE、SET_BUDGET_LIMIT 等处理都需要修改此类。

**要求**: 实现 `ControlCommandHandler` 协议和注册表，ControlDrainInterceptor 持有 handler 列表，通过 `command_type` 匹配分发。

---

#### P1-3. AgentRuntimeConfig 聚合配置未创建

**位置**: 缺失

设计文档 §10 定义了 `AgentRuntimeConfig` 作为结构化代码配置对象，捆绑 hooks、interceptors、control 组件：

```python
runtime = AgentRuntimeConfig(
    hooks=[...],
    interceptors=[...],
    control=RuntimeControl(channel=..., event_bus=..., checkpoint_store=..., preset_rules=[...]),
)
```

当前各组件分别传递给 Pipeline / AgentFactory / AgentPool，没有统一的配置入口。

**要求**: 创建 `AgentRuntimeConfig` dataclass 聚合所有运行时组件。Pipeline / AgentFactory / AgentPool / AgentSession 接受 AgentRuntimeConfig 参数。

---

#### P1-4. Pool 模式下 control_channel 未共享给子 agent

**位置**: `examples/bot_project/bot/service/core.py:626-662`

`_build_interceptor_chain` 创建独立的 `InMemoryControlChannel`，但在 pool 模式下，peer agent 和 subagent 是否共享同一 control_channel 取决于 AgentFactory 如何传递。当前 DefaultAgentFactory 只传递 `default_interceptor_chain`（已含 channel），但 peer agent 有独立的 interceptor_chain（在 `builders.py` 中构建），不含 ControlDrainInterceptor。

**要求**: 确认 pool 模式下 peer/subagent 是否需要接收外部控制命令。如果需要，应在 `_build_peer_tool_manager` 路径中为 peer 也注入 ControlDrainInterceptor。

---

#### P1-5. HookRunner.dispatch 的 payload 解包与 Hook 方法签名存在摩擦

**位置**: `framework/hook/runner.py:87-99`、`framework/agents/react/agent.py:352-368`

HookRunner.dispatch 将 `payload.data` 解包为 `**kwargs` 传给 hook 方法：

```python
hook_kwargs = dict(payload.data) if payload else {}
result = await asyncio.wait_for(method(ctx, **hook_kwargs), timeout=timeout)
```

而 `_call_hooks` 在构造 payload 时使用硬编码键名：

```python
if method_name == "after_turn":
    payload_data = {"result": args[0]}
elif method_name == "after_llm_response":
    payload_data = {"response": args[0]}
elif method_name == "before_tool_execution":
    payload_data = {"tool_calls": args[0]}
```

Hook 协议方法签名是 `after_turn(self, ctx, result)` 和 `after_llm_response(self, ctx, response)` — 参数名恰好匹配 payload key。但这依赖隐式约定，没有编译时保障。

**要求**: 在 HookRunner 或 `_call_hooks` 中增加参数名映射文档，或考虑为不同 HookPoint 定义专用的 payload 类型。

---

#### P1-6. ToolApprovalInterceptor._redact_args 过于简陋

**位置**: `framework/interceptor/builtin/tool_approval.py:196-206`

设计 §15.10 要求 "tool approval request 必须脱敏参数"。当前实现只做一级精确匹配：

```python
sensitive_keys = {"api_key", "secret", "token", "password", "credential", "access_key", "private_key"}
```

不支持：
- 嵌套结构中的敏感字段（如 `config.api_key`）
- 模式匹配（如 `*_token`、`*_secret`）
- 自定义脱敏策略
- 构造函数缺少 `redact_args` 选项（设计文档有 `redact_args=True` 参数）

**要求**: 至少增加大小写不敏感匹配和通配符匹配；构造函数增加 `redact_args: bool = True` 参数。

---

### P2 — 设计改进建议

#### P2-1. TaskProgressHook / TaskInterventionHook 未迁移到 framework.hook.builtin

**位置**: `framework/multi_agent/hooks.py`

这两个 Hook 仍在 `multi_agent` 包内，未迁入 `framework/hook/builtin/`。它们在 `multi_agent/__init__.py` 中被导出，且不实现新的 Hook 协议（没有继承 Hook Protocol）。

**建议**: 如果这两个 Hook 是框架级通用 Hook，应迁入 builtin。如果是 multi-agent 专属，当前放置可接受，但应在文档中说明。

---

#### P2-2. ControlEventBus handler 是同步回调

**位置**: `framework/control/event_bus.py:16`

```python
ControlEventHandler = Callable[[ControlEvent], None]
```

handler 是同步函数，无法执行异步操作（如写数据库、发送 HTTP）。CallbackControlEventBus 实现也是同步调用。

**建议**: 改为 `Callable[[ControlEvent], Awaitable[None]] | Callable[[ControlEvent], None]`，在 CallbackControlEventBus 中检测是否为协程函数并用 await 调用。

---

#### P2-3. ControlEventBus 协议缺少 unsubscribe

**位置**: `framework/control/event_bus.py:19-35`

Protocol 只声明了 `emit` 和 `subscribe`，但 CallbackControlEventBus 实现了额外的 `unsubscribe` 方法。调用方如果想取消订阅，只能依赖具体实现类而非 Protocol。

**建议**: 在 Protocol 中增加 `unsubscribe` 方法声明。

---

#### P2-4. InterceptorChain 不验证返回类型

**位置**: `framework/interceptor/chain.py:63-86`

around_tool_call 的兜底逻辑只在异常时触发。如果某个 interceptor 正常返回了 None 或非法类型（非 ToolResult），链会继续传递 None 直到最终返回。设计 §5.2 说"非法返回值都要补齐 tool_call_id 对应的 tool result"。

**建议**: 在 around_tool_call 返回前增加 `isinstance(result, ToolResult)` 检查，非法时转为错误 ToolResult。

---

#### P2-5. TurnTimeoutInterceptor.NOTIFY 模式返回伪 AgentResult

**位置**: `framework/interceptor/builtin/turn_timeout.py:62-66`

```python
return AgentResult(content="Turn timed out.", stop_reason="timeout")
```

设计 §5.2 明确说 "around_turn / around_iteration 不制造伪结果；普通异常默认向外抛出并终止当前边界"。超时属于控制终止，应通过 AgentControlError 表达，或至少在 NOTIFY 模式下明确说明行为。

**建议**: NOTIFY 模式的语义需要明确。如果是"记录但不终止"，需要返回正常的 AgentResult 让 turn 继续——但 asyncio.wait_for 已经取消了 turn，无法继续。建议移除 NOTIFY 或改为 raise AgentTimeout + emit event。

---

#### P2-6. TokenBudgetControlRule 依赖 metadata.usage

**位置**: `framework/control/preset.py:54-56`

```python
usage = getattr(ctx, "metadata", {}).get("usage", {}) if hasattr(ctx, "metadata") else {}
total = usage.get("total_tokens", 0)
```

没有任何地方在 AgentContext.metadata 中维护 `usage.total_tokens`。ReActAgent 的 token usage 在 AgentResult.usage 中，不在 context.metadata 中。这条规则在实际运行中永远无法触发。

**建议**: 改为从 AgentResult.usage 或 LLMResponse.usage 累积计算，或要求调用方在每轮结束后更新 metadata.usage。

---

#### P2-7. 测试覆盖不足

当前没有针对新模块的独立测试文件：
- 缺少 `tests/unit/hook/` 目录及 HookPoint、HookRunner、HookErrorPolicy 测试
- 缺少 `tests/unit/interceptor/` 目录及 InterceptorChain、内置 interceptor 测试
- 缺少 `tests/unit/control/` 目录及 ControlChannel、ControlEventBus、CheckpointStore、PresetControlRule 测试

旧测试文件 `tests/unit/test_hooks.py`、`tests/unit/multi_agent/test_peer_auto_send_hook.py` 等可能仍测试旧导入路径。

**要求**: 补齐测试矩阵，至少覆盖设计 §15.11 列出的测试项：
- HookPoint 分发命中
- HookErrorPolicy 三种行为
- InterceptorChain tool 兜底
- InterceptorChain control 异常透传
- ToolApproval deny_as_tool_error → session/history 补齐
- ToolApproval deny_as_cancel → 受控退出
- ControlChannel TTL 过期
- ControlDrain iteration check
- RuntimeSafetyPolicy 共存

---

#### P2-8. HookPoint.FINALIZE_CONTENT 在 dispatch 中不适用

**位置**: `framework/hook/abc.py:34`

FINALIZE_CONTENT 列在 HookPoint 枚举中，但它是同步方法，不能通过 HookRunner.dispatch（异步）调用。需要单独走 dispatch_finalize。枚举中的存在可能误导调用方。

**建议**: 在 HookPoint 枚举或 HookRunner 文档中明确说明 FINALIZE_CONTENT 必须通过 dispatch_finalize 调用，不应传入 dispatch。

---

## 四、设计偏差记录

| 偏差 | 说明 | 影响 |
|------|------|------|
| AgentRuntimeConfig 未创建 | 组件分别传递 | 调用方组装负担大，无法统一 snapshot |
| ControlCommandHandler 未实现 | 用 if/elif 替代 | 后续新增命令类型需修改 ControlDrainInterceptor |
| on_checkpoint 未移除 | 旧回调与新 checkpoint_store 并存 | 两套机制可能冲突 |
| TaskProgressHook/TaskInterventionHook 未迁移 | 留在 multi_agent | 不是 Hook 协议实现，hook_runner 无法调度 |
| TurnTimeout NOTIFY 模式不可用 | wait_for 取消后无法返回正常结果 | NOTIFY 实际不会被触发 |
| TokenBudgetControlRule 无法触发 | metadata.usage 未维护 | 规则形同虚设 |

---

## 五、对照验收标准

| 标准 | 状态 | 备注 |
|------|------|------|
| 无未采纳草案命名 `framework.intercept`、`framework.intervention` | ✅ | 全部使用 `framework.hook`/`interceptor`/`control` |
| 无 `Intervention*`、`Intercept*` 类名 | ✅ | |
| ReActAgent 不再散落硬编码 hook point 字符串 | ❌ **P0-2** | 7 处仍使用裸字符串 |
| 现有 hook 行为改造后仍可用 | ✅ | 旧路径作为 fallback 保留 |
| tool 审批拒绝 `deny_as_tool_error` 产生合法 ToolResult | ✅ | |
| `deny_as_cancel` 受控退出并保存终止状态 | ⚠️ | AgentControlError re-raise，但 checkpoint 用旧 on_checkpoint |
| timeout、cancel、approval denied 使用统一终止语义 | ✅ | |
| 相关 unit/integration/example 测试通过 | ❌ **P2-7** | 新模块缺少独立测试 |

---

## 六、修复优先级建议

1. **P0-2**: ReActAgent 调用点改 HookPoint 常量 — 改动小、收益大
2. **P0-3**: on_checkpoint → checkpoint_store — 核心契约对齐
3. **P0-1**: AgentSession 接入 — 覆盖 session 模式
4. **P1-1**: InMemoryControlChannel TTL — 设计明确要求
5. **P1-3**: AgentRuntimeConfig — 统一配置入口
6. **P1-2**: ControlCommandHandler 注册 — 扩展性保障
7. **P2-7**: 测试矩阵 — 验证保障
8. 其余 P1/P2 按实际优先级排列
