# Hook / Interceptor / Control 设计与实施规划

本文是面向当前实际代码实现的新版设计/规划文档。`intercept-design(todo).md` 是未落地的未来设计草案，只作为参考材料；它不是当前实现，也不是被迁移对象。本设计结合 review 结论，对当前 `framework/core/hooks.py`、`framework/agents/react/`、`framework/pipeline/`、`framework/multi_agent/` 与 `examples/bot_project/` 的实际实现提出目标结构和实施步骤。

目标包名统一采用单数：

```text
framework.hook
framework.interceptor
framework.control
```

## 一、目标与原则

### 1.1 目标

- 统一生命周期扩展、调用包裹、运行时控制三类机制。
- 移除当前代码中不符合目标边界的实现和使用方式；对未落地设计草案中的旧命名不采纳、不兼容。
- 保留并增强现有 hook 能力：观察、上下文修改、runtime context、inbox flush、tool call 记录、peer auto send 等。
- 支持 Hook、Interceptor、Control 都能触发受控终止。
- 支持 tool 调用前审批、审批失败伪错误结果、审批失败退出 agent、超时退出、外部取消、代码内预配置策略。
- 可配置能力第一阶段通过代码装配 API 提供，不要求 YAML/配置文件声明式配置。
- 保证 session/history 与 provider tool call 协议一致。

### 1.2 非兼容改造原则

以下分为两类：当前代码中已存在但需要改造的入口，以及原设计草案中未落地但不采纳的命名。二者都不做兼容层。

| 来源 | 当前入口/草案命名 | 目标入口 | 处理方式 |
|------|------------------|----------|----------|
| 当前代码 | `framework/core/hooks.py` 承载通用 hook | `framework.hook` | 改造并迁入目标包 |
| 当前代码 | `framework/multi_agent/hooks.py` 分散 hook | `framework.hook.builtin` | 改造并迁入目标包 |
| 当前代码 | `framework/multi_agent/inbox/hook.py` 独立 hook | `framework.hook.builtin.inbox_flush` | 改造并迁入目标包 |
| 当前代码 | 业务代码散落 `_call_hooks("...")` | `HookPoint` + `HookRunner` | 改为集中常量调度 |
| 未落地草案 | `framework.intercept` | `framework.interceptor` | 不采纳草案命名 |
| 未落地草案 | `framework.intervention` | `framework.control` | 不采纳草案命名 |
| 当前代码 | `multi_agent/intervention.py` 的任务级策略接口 | control / interceptor / multi-agent 职责拆分 | 按目标边界改造或删除 |

## 二、总体架构

```text
Agent / Pipeline / Pool
  |
  |-- framework.hook
  |     HookPoint
  |     HookRunner
  |     builtins: logging, runtime_context, inbox_flush, peer_auto_send
  |
  |-- framework.interceptor
  |     Interceptor
  |     InterceptorChain
  |     around_tool_call / around_llm_call / around_turn / supervise_loop
  |     builtins: tool_approval, tool_timeout, turn_timeout, result_limit, control_drain
  |
  |-- framework.control
        ControlCommand
        ControlChannel
        ControlEventBus
        ControlDrain
        CheckpointStore
        preset rules
```

职责边界：

| 组件 | 定位 | 能做什么 | 不应承担什么 |
|------|------|----------|--------------|
| Hook | 生命周期扩展点 | 观察、轻量修改、上下文注入、策略 veto、受控终止 | 复杂 around-call、长时间审批等待 |
| Interceptor | 调用边界包裹 | 审批、超时、重试、短路、结果改写、调用级终止 | 外部命令存储、长期状态管理 |
| Control | 运行时控制平面 | 外部输入、预配置策略、审批响应、取消、事件、checkpoint | 直接侵入业务调用实现 |

## 三、Hook 设计

### 3.1 HookPoint

`getattr` 可以保留，但调用点必须使用集中定义的 `HookPoint`，禁止散落字符串。

```python
class HookPoint(StrEnum):
    BEFORE_TURN = "before_turn"
    AFTER_TURN = "after_turn"
    BEFORE_ITERATION = "before_iteration"
    AFTER_ITERATION = "after_iteration"
    BEFORE_TOOL_EXECUTION = "before_tool_execution"
    AFTER_TOOL_EXECUTION = "after_tool_execution"
    AFTER_LLM_RESPONSE = "after_llm_response"
    ON_CONTROL_COMMAND = "on_control_command"
    FINALIZE_CONTENT = "finalize_content"
```

枚举值必须等于 hook 对象上的方法名，因为 `HookRunner` 内部会通过 `getattr(hook, hook_point.value, None)` 调度。第一阶段沿用当前实际代码中的 `before_tool_execution` / `after_tool_execution` 命名，不改成 `tool_call`，避免同时重命名所有现有 hook 子类。

### 3.2 HookRunner

```python
class HookRunner:
    async def dispatch(
        self,
        hook_point: HookPoint,
        ctx: AgentContext,
        payload: HookPayload,
    ) -> HookResult:
        ...
```

最小类型定义：

```python
@dataclass(frozen=True)
class HookPayload:
    data: Mapping[str, object] = field(default_factory=dict)


@dataclass(frozen=True)
class HookResult:
    veto: bool = False
    content_override: str | None = None


class HookErrorPolicy(StrEnum):
    IGNORE = "ignore"
    LOG = "log"
    ABORT = "abort"


@dataclass(frozen=True)
class HookSpec:
    hook: Hook
    on_error: HookErrorPolicy = HookErrorPolicy.LOG
```

要求：

- 内部可用 `getattr(hook, hook_point.value, None)`。
- 控制异常必须透传。
- 普通异常按 hook 配置处理：`ignore`、`log`、`abort`；默认 `LOG`。
- 保留现有 hook 的上下文修改能力。
- 支持 hook 返回 `HookResult` 表达轻量决策；受控终止优先用统一异常。

### 3.3 内置 Hook 改造

| 现有能力 | 新位置 | 改造要求 |
|----------|--------|----------|
| `RunLoggingHook` | `framework.hook.builtin.logging` | 保留行为 |
| `RuntimeContextHook` | `framework.hook.builtin.runtime_context` | 保留 tool call 记录 |
| `InboxFlushHook` | `framework.hook.builtin.inbox_flush` | 保证 pipeline/pool 都能生效 |
| `PeerAutoSendHook` | `framework.hook.builtin.peer_auto_send` | 保留 peer final content 自动转发 |
| `SubagentMemoryCleanupHook` | `framework.hook.builtin.subagent_cleanup` | 保留清理时机 |

现有 `core/hooks.py`、`multi_agent/hooks.py`、`multi_agent/inbox/hook.py` 不做 re-export 兼容。改造完成后删除原入口或改为内部不可用状态，所有调用方改新路径。

## 四、统一终止模型

Hook、Interceptor、Control 都使用同一套终止语义。

```python
class AgentControlError(Exception): ...
class AgentCancelled(AgentControlError): ...
class AgentTimeout(AgentControlError): ...
class ApprovalDenied(AgentControlError): ...
class PolicyViolation(AgentControlError): ...

class TerminationReason(StrEnum):
    CANCELLED = "cancelled"
    TIMEOUT = "timeout"
    APPROVAL_DENIED = "approval_denied"
    POLICY_VIOLATION = "policy_violation"
```

规则：

- `AgentControlError` 表示受控退出，不是普通失败。
- `asyncio.CancelledError`、`KeyboardInterrupt`、`SystemExit` 不允许被 hook/interceptor 吞掉。
- tool 调用已产生 assistant tool_call 后，非终止式拒绝或超时必须补齐 tool result。
- 终止式退出必须保存 termination metadata 或 checkpoint，避免恢复时历史不一致。

## 五、Interceptor 设计

Interceptor 是框架级 AOP 机制，不绑定 ReAct，也不只作用于某个 turn。ReAct turn 只是其中一个可拦截边界；同一套机制应能用于 Agent、Pipeline、Pool、LLM、Tool、Memory 等调用点。

第一阶段至少建模以下 scope，实际接入可分阶段完成：

| Scope | 目标边界 | 第一阶段 |
|-------|----------|:--------:|
| `agent_run` | agent 一次完整运行 | 可建模，按需接入 |
| `turn` | 单轮 agent turn | 接入 ReActAgent |
| `iteration` | ReAct/Plan 等循环的一次迭代 | 接入 ReActAgent 的检查点 |
| `llm_call` | LLM 请求/响应 | 可建模，先保留扩展点 |
| `llm_stream` | LLM streaming chunk / finalization | 可建模，后续接入 |
| `tool_call` | 单个工具调用 | 接入 ToolApproval/ToolTimeout |
| `pipeline_step` | pipeline 节点/步骤 | 可建模，后续接入 |
| `pool_task` | AgentPool 调度任务 | 可建模，后续接入 |
| `memory_operation` | memory 读写/压缩/治理 | 可建模，后续接入 |

### 5.1 接口

```python
class InterceptorScope(StrEnum):
    AGENT_RUN = "agent_run"
    TURN = "turn"
    ITERATION = "iteration"
    LLM_CALL = "llm_call"
    LLM_STREAM = "llm_stream"
    TOOL_CALL = "tool_call"
    PIPELINE_STEP = "pipeline_step"
    POOL_TASK = "pool_task"
    MEMORY_OPERATION = "memory_operation"


class Interceptor(Protocol):
    scopes: FrozenSet[InterceptorScope]

    async def around_tool_call(
        self,
        ctx: AgentContext,
        call: ToolCallContext,
        next_call: ToolCallNext,
    ) -> ToolResult:
        ...

    async def around_turn(
        self,
        ctx: AgentContext,
        next_call: TurnNext,
    ) -> AgentRunResult:
        ...

    async def around_iteration(
        self,
        ctx: AgentContext,
        call: IterationContext,
        next_call: IterationNext,
    ) -> IterationResult:
        ...
```

第一阶段优先落地 `tool_call`、`turn`、`iteration` 三个边界；其他 scope 先作为强类型扩展点保留，避免后续把 interceptor 锁死在 ReAct turn。

### 5.2 InterceptorChain

要求：

- 按配置顺序执行，外层先进入、后退出。
- interceptor 可调用 `next_call()`，也可返回替代结果短路。
- 控制异常必须透传。
- 普通异常由具体边界决定是否转换为合法结果或向外抛出。
- `around_tool_call` 必须兜底返回合法 `ToolResult`：普通异常、非法返回值、审批失败伪错误都要补齐 `tool_call_id` 对应的 tool result。
- `around_turn` / `around_iteration` 不制造伪结果；普通异常默认向外抛出并终止当前边界。
- `asyncio.CancelledError`、`KeyboardInterrupt`、`SystemExit`、`AgentControlError` 始终透传。

### 5.3 内置 Interceptor

| Interceptor | 边界 | 作用 |
|-------------|------|------|
| `ToolApprovalInterceptor` | tool | 工具调用前审批 |
| `ToolTimeoutInterceptor` | tool | 工具调用超时 |
| `TurnTimeoutInterceptor` | turn/loop | 单轮或整体运行超时 |
| `ToolResultLimitInterceptor` | tool | 限制 tool result 长度 |
| `ControlDrainInterceptor` | agent_run/turn/iteration | 消费或检查 control command 并转为运行时动作 |

### 5.4 RuntimeSafetyPolicy 共存

当前代码已有 `RuntimeSafetyPolicy`。第一阶段不允许出现两层 timeout 同时争抢同一 owner。

策略：

- `ToolTimeoutInterceptor`、`TurnTimeoutInterceptor` 从 `ctx.safety` 读取默认超时值。
- 如果某个边界已由 interceptor 接管 timeout，该边界不再由旧 safety wrapper 重复包裹。
- LLM request/stream timeout 第一阶段仍由 provider/safety 层负责；`llm_call` interceptor 先保留扩展点。
- 后续若 interceptor 全面接管 timeout，需要同步废弃或重构 `RuntimeSafetyPolicy` 中对应字段。

## 六、Tool 审批设计

`ToolApprovalInterceptor` 是 tool 审批的唯一默认实现位置。

### 6.1 审批请求

审批事件发送到 `ControlEventBus`：

```python
@dataclass
class ToolApprovalRequest:
    agent_id: str
    session_id: str
    turn_id: str
    tool_call_id: str
    tool_name: str
    redacted_arguments: Mapping[str, object]
    correlation_id: str
```

必须脱敏参数，不能把 secret、token、credential 原样发出。

### 6.2 审批结果

| 配置 | 行为 |
|------|------|
| `allow` | 执行 tool |
| `deny_as_tool_error` | 写入伪错误 `ToolResult`，agent 继续运行 |
| `deny_as_cancel` | 保存终止状态，退出当前 run |
| `timeout_as_tool_error` | 写入超时伪错误 `ToolResult`，agent 继续运行 |
| `timeout_as_cancel` | 抛出 `AgentTimeout` |

默认拒绝行为为 `deny_as_tool_error`。

伪错误结果必须保存到 session/history：

```python
ToolResult(
    tool_call_id=tool_call.id,
    tool_name=tool_call.name,
    is_error=True,
    content="Tool execution was not approved. The tool was not run.",
)
```

如果一个 assistant 消息包含多个 tool call，必须保证每个已声明 tool call 都有合法结果、checkpoint 恢复策略或稳定回滚点。

## 七、Control 设计

### 7.1 ControlCommand

```python
@dataclass
class ControlCommand:
    command_id: str
    type: ControlCommandType
    scope: ControlScope
    source: str
    priority: int
    ttl_seconds: float | None
    correlation_id: str | None
    idempotency_key: str | None
    payload: Mapping[str, object]
```

```python
@dataclass(frozen=True)
class ControlScope:
    session_id: str
    agent_id: str | None = None
    turn_id: str | None = None
```

`source` 示例：

```text
preset:token_budget
preset:wall_clock_timeout
external:user
external:admin
system:approval_timeout
```

### 7.2 接口

```python
class ControlChannel(Protocol):
    async def send(self, command: ControlCommand) -> None: ...
    async def drain(self, scope: ControlScope, limit: int) -> Sequence[ControlCommand]: ...
    async def peek(self, scope: ControlScope) -> Sequence[ControlCommand]: ...

class ControlEventBus(Protocol):
    async def emit(self, event: ControlEvent) -> None: ...
    async def subscribe(self, event_type: ControlEventType, handler: ControlEventHandler) -> Subscription: ...

class CheckpointStore(Protocol):
    async def save(self, checkpoint: AgentCheckpoint) -> None: ...
    async def load(self, checkpoint_id: str) -> AgentCheckpoint | None: ...
```

第一阶段支持：

- cancel turn/run
- approval response
- approval timeout
- preset budget cancel
- termination metadata
- 最小 checkpoint

Checkpoint 第一阶段由运行边界的 owner 显式触发：ReActAgent 在保存 assistant message、tool result、终止状态时直接调用 `ctx.checkpoint_store.save(...)`。暂不把 checkpoint 保存抽成 Hook 或 Interceptor，避免第一阶段职责交叉。

pause/resume、stream control、动态热更新后续再做。

## 八、AgentContext 增强

建议在现有 `AgentContext` 上增量加入运行时组件，不引入会削弱现有 hook 类型能力的过度抽象上下文。

```python
@dataclass
class AgentContext:
    hooks: Sequence[Hook]
    hook_runner: HookRunner
    interceptors: Sequence[Interceptor]
    interceptor_chain: InterceptorChain
    control_channel: ControlChannel | None
    control_event_bus: ControlEventBus | None
    checkpoint_store: CheckpointStore | None
    metadata: MutableMapping[str, object]
```

要求：

- public API 使用结构体和协议，不使用裸 `dict`、裸 `list`、裸 `Any`。
- 新增字段应服务于新组件，不保留旧命名字段。

## 九、执行顺序

典型 turn 流程：

```text
HookRunner.dispatch(BEFORE_TURN)
  -> InterceptorChain.around_turn
       -> ControlDrainInterceptor
            -> HookRunner.dispatch(ON_CONTROL_COMMAND) for each command
       -> TurnTimeoutInterceptor
       -> ReAct iteration
            -> HookRunner.dispatch(BEFORE_ITERATION)
            -> LLM call
            -> HookRunner.dispatch(AFTER_LLM_RESPONSE)
            -> tool calls
                 -> HookRunner.dispatch(BEFORE_TOOL_EXECUTION)
                 -> InterceptorChain.around_tool_call
                      -> ToolApprovalInterceptor
                      -> ToolTimeoutInterceptor
                      -> actual tool
                 -> save ToolResult to session/history
                 -> HookRunner.dispatch(AFTER_TOOL_EXECUTION)
            -> HookRunner.dispatch(AFTER_ITERATION)
  -> HookRunner.dispatch(AFTER_TURN)
```

控制命令可以由外部输入或预配置规则产生，但进入运行时后都由 `ControlDrainInterceptor` 转换为统一动作。

`ControlDrainInterceptor` 不只在 turn 开始执行。第一阶段建议：

- `around_turn` 开始时 drain 一次，处理 turn 前已存在的命令。
- `around_iteration` 前执行轻量检查；对 cancel/timeout 这类高优先级命令可以 drain 并转为受控终止。
- 长耗时 tool 内部取消需要由 tool timeout 或后续 supervisor scope 补强，不在第一阶段强行实现后台 watcher。

## 十、代码装配示例

第一阶段的“可配置”指代码层面的可装配、可替换、可组合，不要求 YAML 或配置文件声明式配置。后续如果需要 YAML，可以在稳定的代码配置对象之上再做解析层。

```python
runtime = AgentRuntimeConfig(
    hooks=[
        HookSpec(
            hook=RunLoggingHook(),
            on_error=HookErrorPolicy.LOG,
        ),
        HookSpec(
            hook=RuntimeContextHook(),
            on_error=HookErrorPolicy.ABORT,
        ),
        HookSpec(
            hook=InboxFlushHook(inbox=inbox),
            on_error=HookErrorPolicy.LOG,
        ),
    ],
    interceptors=[
        ControlDrainInterceptor(
            channel=control_channel,
            max_commands=3,
        ),
        TurnTimeoutInterceptor(
            timeout_seconds=180,
            on_timeout=TimeoutAction.CANCEL_TURN,
        ),
        ToolApprovalInterceptor(
            approval_service=approval_service,
            matcher=ToolNameMatcher({"shell", "write_file", "send_message"}),
            redact_args=True,
            approval_timeout_seconds=60,
            on_denied=ApprovalDeniedAction.TOOL_ERROR,
            on_timeout=ApprovalTimeoutAction.CANCEL_TURN,
        ),
        ToolResultLimitInterceptor(max_chars=4000),
    ],
    control=RuntimeControl(
        channel=control_channel,
        event_bus=control_event_bus,
        checkpoint_store=checkpoint_store,
        preset_rules=[
            TokenBudgetControlRule(
                max_tokens=120000,
                action=ControlAction.CANCEL_TURN,
            ),
        ],
    ),
)
```

约束：

- `AgentRuntimeConfig` 是结构化代码配置对象，不是 `dict`。
- matcher、policy、action 使用协议、枚举或结构体，不使用硬编码字符串。
- 字段名使用自然语义：包名保持单数 `framework.hook` / `framework.interceptor`，但集合字段使用复数 `hooks` / `interceptors`。
- bot_project 可以在 builder 中按代码组装默认 runtime，也可以由调用方传入自定义 runtime。
- 暂不要求从 YAML 生成 runtime；如果未来需要，只能作为薄适配层，不能反向污染核心 API。

## 十一、bot_project 适配

`examples/bot_project` 应做以下调整：

- 构建 agent 时装配 `HookRunner` 与改造后的内置 hook。
- tool manager 调用路径接入 `InterceptorChain.around_tool_call`。
- BotService 通过代码 builder 装配默认 `AgentRuntimeConfig`；调用方可用代码传入自定义 hook、interceptor、control。
- tool 审批失败的 `deny_as_tool_error` 必须写入当前 session/history。
- pool 与 pipeline 两种模式都要验证 hook 和 control drain 生效。

## 十二、实施步骤

实施状态以 `intercept-implementation-plan.md` 为准。步骤概览：

1. Hook 改造增强。
2. 统一控制异常。
3. Interceptor 基础链。
4. Tool 审批。
5. Timeout 策略。
6. Control 平面。
7. ReActAgent 接入。
8. bot_project 适配。
9. 清理与验证。

每完成一步必须：

- 刷新 `intercept-implementation-plan.md` 状态。
- 写进展记录。
- 如设计发生变化，同步更新本文。
- 补充或更新测试。

## 十三、验收标准

- 新代码不出现未采纳草案命名 `framework.intercept`、`framework.intervention`；目标包统一为 `framework.hook`、`framework.interceptor`、`framework.control`。
- 新增实现不使用未采纳草案中的 `Intervention*`、`Intercept*` 类名作为主 API。
- ReActAgent 不再散落硬编码 hook point 字符串。
- 现有 hook 行为改造后仍可用。
- tool 审批拒绝时，`deny_as_tool_error` 会产生合法 tool result 并保存到 session/history。
- `deny_as_cancel` 会受控退出，并保存足够恢复或解释的终止状态。
- timeout、cancel、approval denied 都使用统一终止语义。
- 相关 unit/integration/example 测试通过。

## 十四、复核结论

结合 `intercept-design-review.md` 复核后，本设计已覆盖当前必须项：

- 三组件包名采用单数 `hook`、`interceptor`、`control`。
- 原设计文档是未落地草案，不是当前实现；本设计只吸收其合理场景，不采纳其旧命名。
- 非兼容改造当前代码中不符合目标边界的入口。
- HookPoint 枚举值与当前 hook 方法名匹配，补齐 `after_llm_response`。
- Interceptor 是通用 AOP 调用边界，不只作用于 ReAct turn。
- Hook 是基于当前代码的改造 + 增强，不是只支持异常。
- Hook、Interceptor、Control 都能触发受控终止。
- tool 审批支持 `deny_as_tool_error` 与 `deny_as_cancel`。
- `deny_as_tool_error` 必须写入 session/history 中的合法 tool result。
- 预配置策略和外部输入统一为 `ControlCommand`。
- 第一阶段可配置方式为代码装配 API，不要求 YAML。

暂不纳入第一阶段：

- YAML/配置文件解析。
- pause/resume 完整状态机。
- stream interceptor。
- 动态配置热更新。
- async tool 的完整后台执行/恢复协议。

## 十五、原草案覆盖性订正

原 `intercept-design(todo).md` 虽未落地，但覆盖了不少未来场景。本节把其中仍有价值的内容按新命名和多轮 review 结论重新纳入，避免新版设计只停留在高层框架。

### 15.1 覆盖矩阵

| 原草案主题 | 新设计处理 |
|------------|------------|
| 三机制分工 | 保留为 Hook / Interceptor / Control，改用单数包名 |
| 目录结构 | 补充到 15.2 |
| Hook ABC / Runner / Composite | 保留 HookRunner；不单独设计 CompositeHook 第一阶段能力 |
| ExecutionContext | 不采用；直接增强现有 AgentContext |
| InterceptChain 洋葱模型 | 改为 InterceptorChain，补充通用 AOP scope |
| 内置拦截器 | 改为内置 Interceptor，补充审批、超时、结果限制、control drain |
| 干预类型 | 改为 ControlCommandType，补充命令类型和 source/scope |
| Channel / Bus / Checkpoint | 改为 ControlChannel / ControlEventBus / CheckpointStore |
| 外部审批 | 保留，落在 ToolApprovalInterceptor + ControlEventBus |
| 预配置干预 | 改为 PresetControlRule，代码装配，不做 YAML |
| 异步工具 | 第一阶段不实现完整后台执行，但保留边界约束 |
| 执行顺序与冲突规避 | 补充到 15.8 |
| bot_project 适配 | 保留，按代码 builder 装配 |
| nanobot 对照 | 不作为实现约束，只保留“可审计、可插拔、可中止”的设计目标 |

### 15.2 目标目录结构

```text
framework/
  hook/
    __init__.py
    abc.py              # Hook, HookPoint, HookPayload, HookResult, HookSpec
    runner.py           # HookRunner
    builtin/
      logging.py
      runtime_context.py
      inbox_flush.py
      peer_auto_send.py
      subagent_cleanup.py

  interceptor/
    __init__.py
    abc.py              # Interceptor, InterceptorScope, contexts, next-call protocols
    chain.py            # InterceptorChain
    builtin/
      control_drain.py
      tool_approval.py
      tool_timeout.py
      turn_timeout.py
      result_limit.py

  control/
    __init__.py
    types.py            # ControlCommand, ControlScope, ControlEvent, enums
    channel.py          # InMemoryControlChannel
    event_bus.py        # CallbackControlEventBus
    checkpoint.py       # CheckpointStore implementations
    preset.py           # PresetControlRule
```

当前 `framework/core/hooks.py`、`framework/multi_agent/hooks.py`、`framework/multi_agent/inbox/hook.py` 中已有能力迁入 `framework.hook`。不保留 re-export 兼容层。

### 15.3 Hook 方法契约

第一阶段沿用当前 hook 方法名，避免不必要改名：

```python
class Hook(Protocol):
    async def before_turn(self, ctx: AgentContext, prompt: str) -> None: ...
    async def after_turn(self, ctx: AgentContext, result: AgentRunResult) -> None: ...
    async def before_iteration(self, ctx: AgentContext, iteration: int) -> None: ...
    async def after_iteration(self, ctx: AgentContext, iteration: int) -> None: ...
    async def after_llm_response(self, ctx: AgentContext, response: LLMResponse) -> None: ...
    async def before_tool_execution(
        self,
        ctx: AgentContext,
        tool_calls: Sequence[ToolCall],
    ) -> None: ...
    async def after_tool_execution(
        self,
        ctx: AgentContext,
        tool_results: Sequence[ToolResult],
    ) -> None: ...
    async def on_control_command(
        self,
        ctx: AgentContext,
        command: ControlCommand,
    ) -> HookResult: ...
    async def finalize_content(self, ctx: AgentContext, content: str) -> str | None: ...
```

说明：

- Protocol 方法是可选能力，实际分发仍由 `getattr` 判断。
- `HookPayload` 是 HookRunner 的统一承载结构，但现有方法签名不必强制改成单一 payload 参数。
- `on_control_command` 可返回 `HookResult(veto=True)` 拒绝某条 control command。
- 策略类 hook 可通过抛出 `PolicyViolation` 受控终止。

### 15.4 Interceptor 上下文契约

每个 scope 都应有结构化 context，不用裸 `dict`：

```python
@dataclass(frozen=True)
class ToolCallContext:
    tool_call: ToolCall
    tool_name: str
    arguments: Mapping[str, object]
    session_id: str
    turn_id: str


@dataclass(frozen=True)
class TurnContext:
    prompt: str
    turn_id: str
    max_iterations: int


@dataclass(frozen=True)
class IterationContext:
    iteration: int
    turn_id: str


@dataclass(frozen=True)
class LLMCallContext:
    messages: Sequence[ChatMessage]
    model: str | None
    stream: bool
```

第一阶段不要求所有 context 都接入运行路径，但类型要先稳定，避免后续 AOP scope 扩展时再破坏 API。

### 15.5 Control 命令与事件类型

```python
class ControlCommandType(StrEnum):
    CANCEL_RUN = "cancel_run"
    CANCEL_TURN = "cancel_turn"
    INJECT_USER_MESSAGE = "inject_user_message"
    APPROVAL_RESPONSE = "approval_response"
    SET_BUDGET_LIMIT = "set_budget_limit"
    CHECKPOINT_SAVE = "checkpoint_save"
    BACKGROUND_TOOL_RESULT = "background_tool_result"
    BACKGROUND_TOOL_PROGRESS = "background_tool_progress"
    PAUSE_RUN = "pause_run"
    RESUME_RUN = "resume_run"


class ControlEventType(StrEnum):
    TOOL_APPROVAL_REQUESTED = "tool_approval_requested"
    TOOL_APPROVAL_RESOLVED = "tool_approval_resolved"
    BACKGROUND_TOOL_STARTED = "background_tool_started"
    BACKGROUND_TOOL_PROGRESS = "background_tool_progress"
    BACKGROUND_TOOL_COMPLETED = "background_tool_completed"
    RUN_CANCELLED = "run_cancelled"
    RUN_PAUSED = "run_paused"
    RUN_RESUMED = "run_resumed"
    TURN_TIMEOUT = "turn_timeout"
    CHECKPOINT_SAVED = "checkpoint_saved"
```

第一阶段不实现 pause/resume、后台工具结果回灌和 streaming，但不应把 `ControlCommandType` / `ControlEventType` 设计死成只有 cancel。预留命令与事件类型是为了保持 control 平面的可扩展性；具体处理器可后续注册。

### 15.6 Channel / EventBus 语义

`ControlChannel` 负责命令输入，`ControlEventBus` 负责事件输出，两者不能混用：

| 组件 | 方向 | 示例 |
|------|------|------|
| `ControlChannel.send` | 外部/预设策略 -> agent runtime | cancel、approval response、message injection |
| `ControlChannel.drain` | agent runtime 消费命令 | turn/iteration 边界处理 |
| `ControlChannel.peek` | watcher 或检查点非破坏查看 | 高优先级 cancel 检查 |
| `ControlEventBus.emit` | agent runtime -> 外部 | approval request、checkpoint saved、timeout |

要求：

- command 必须有 `command_id` 和 `idempotency_key`，避免重复审批/重复取消。
- channel 实现必须支持 TTL；过期命令 drain 时丢弃并记录事件。
- watcher 不应通过 drain 抢消费命令；需要检查时用 `peek`。
- approval request 发出和 approval response 消费通过 `correlation_id` 关联。

`ControlDrain` 不应写成单个巨大 `if/elif`。建议使用 handler registry：

```python
class ControlCommandHandler(Protocol):
    command_type: ControlCommandType

    async def handle(self, ctx: AgentContext, command: ControlCommand) -> ControlDecision:
        ...
```

这样 pause/resume、后台工具结果、外部消息注入等能力可以后续新增 handler，而不破坏 channel、event bus、drain 的核心接口。

### 15.7 预配置规则

预配置规则不是 YAML，而是代码装配对象：

```python
class PresetControlRule(Protocol):
    name: str

    async def evaluate(self, ctx: AgentContext) -> ControlCommand | None:
        ...
```

示例：

```python
TokenBudgetControlRule(
    max_tokens=120000,
    action=ControlAction.CANCEL_TURN,
    cooldown_seconds=30,
)
```

规则：

- preset rule 只生产 `ControlCommand`，不直接改写 agent 状态。
- 同一 rule 应支持 cooldown，避免每次 iteration 重复发送 cancel。
- preset command 和外部 command 走同一 channel/drain 路径。

### 15.8 执行顺序与冲突规避

| 冲突 | 决策 |
|------|------|
| hook 普通异常 vs control 异常 | control 异常透传；普通异常按 HookErrorPolicy |
| tool 审批拒绝 vs tool result 协议 | `deny_as_tool_error` 必须写入合法 ToolResult |
| tool 审批拒绝退出 vs pending tool calls | `deny_as_cancel` 必须保存 checkpoint 或稳定回滚点 |
| turn timeout vs external cancel 同时发生 | 按优先级处理：external/admin cancel > timeout > preset budget |
| RuntimeSafetyPolicy vs timeout interceptor | 同一边界只能有一个 timeout owner |
| ControlDrain vs watcher | drain 单一消费者；watcher 只能 peek |
| Hook final content vs peer auto send | finalize_content 先确定最终内容，再由 peer auto send 决定是否转发 |

### 15.9 异步工具的暂缓边界

原草案包含同步转异步工具执行。第一阶段不实现完整 async tool，但需要留下约束：

- 不写入“占位 tool result 后再替换”的历史，因为模型可能已经基于占位结果推理。
- 如需后台工具，优先设计为独立能力：`start_background_task` / `poll_background_task`。
- 如果未来要恢复后台 tool result，必须作为新消息或新 tool result 协议处理，不能修改已被 LLM 消费的历史。

### 15.10 安全与审计

- tool approval request 必须脱敏参数。
- control command 需要 source、auth_context、correlation_id。
- EventBus 输出中不能泄露 secret。
- cancel、approval、timeout、policy violation 都应记录 termination reason。
- checkpoint 保存内容应避免写入未脱敏密钥。

### 15.11 测试矩阵

| 测试 | 目标 |
|------|------|
| HookPoint 分发 | `before_tool_execution` / `after_tool_execution` / `after_llm_response` 能命中现有 hook |
| HookErrorPolicy | ignore/log/abort 行为符合预期 |
| InterceptorChain tool 兜底 | 普通异常转合法 ToolResult |
| InterceptorChain control 异常 | AgentControlError 透传 |
| ToolApproval deny_as_tool_error | session/history 补齐 ToolResult |
| ToolApproval deny_as_cancel | run 受控退出并保存 termination metadata |
| ControlChannel TTL | 过期命令不执行 |
| ControlDrain iteration check | iteration 间 cancel 能生效 |
| RuntimeSafetyPolicy 共存 | 同一 timeout owner 不重复触发 |
| bot_project pool/pipeline | hook、interceptor、control 基础路径均生效 |
