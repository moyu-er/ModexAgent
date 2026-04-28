# Hook / Intercept / Intervention 三体系设计

> 修订说明：本文早期章节中的 `framework/intercept`、`framework/intervention`、`Intercept`、`Intervention` 是历史草案命名。后续实现以本修订版约束为准：
>
> - 包名统一为 `framework.hooks`、`framework.interceptors`、`framework.control`。
> - 组件名统一为 `Hook`、`Interceptor`、`Control`。
> - `intervention` 只作为历史草案概念出现在旧章节中；实现时不保留兼容别名，不再作为顶层包名。
> - `getattr` 可以保留；需要消除的是散落的硬编码字符串，而不是动态分发本身。
> - Hook、Interceptor、Control 都可以触发受控终止；不能只有 Control/Intervention 才具备退出能力。
> - 本次改造不做向后兼容：旧包名、旧导入、旧错误实现和旧错误使用方式应被移除，而不是 re-export 或 deprecated 保留。

## 零、已确认的修订版目标设计

### 0.1 三组件最终命名

```text
framework/hooks
framework/interceptors
framework/control
```

三者职责如下：

| 组件 | 职责 | 典型能力 |
|------|------|----------|
| Hook | 生命周期扩展点 | 观察、记录、上下文注入、轻量修改、策略 veto、受控终止 |
| Interceptor | 包裹明确调用边界 | around LLM/tool/turn/iteration、审批、超时、短路、重试、结果改写 |
| Control | 运行时控制平面 | 外部输入、预配置策略、cancel、approval response、checkpoint、事件上报 |

Hook 保留当前已有能力，不因引入受控终止而退化成“只能抛异常”的机制。现有 `RuntimeContextHook`、`InboxFlushHook`、`PeerAutoSendHook`、`RunLoggingHook` 等能力都应迁移到 `framework.hooks` 并继续可插拔使用。

### 0.2 HookPoint 与 getattr

`getattr` 可以继续作为 `HookRunner` 的内部多态分发方式。问题不是 `getattr`，而是 `_call_hooks("before_turn", ...)` 这种字符串散落在业务代码里。

建议：

```python
class HookPoint(StrEnum):
    BEFORE_TURN = "before_turn"
    AFTER_TURN = "after_turn"
    BEFORE_ITERATION = "before_iteration"
    AFTER_ITERATION = "after_iteration"
    BEFORE_TOOL_CALL = "before_tool_call"
    AFTER_TOOL_CALL = "after_tool_call"
```

业务代码只传 `HookPoint`，`HookRunner` 内部可以执行：

```python
method = getattr(hook, hook_point.value, None)
```

这样既保留动态扩展能力，也避免硬编码扩散。

### 0.3 统一受控终止模型

Hook、Interceptor、Control 都可以触发退出或抛异常，但必须收敛到统一控制类型。

建议：

```python
class AgentControlError(Exception): ...
class AgentCancelled(AgentControlError): ...
class AgentTimeout(AgentControlError): ...
class ApprovalDenied(AgentControlError): ...
class PolicyViolation(AgentControlError): ...
```

配套原因枚举：

```python
class TerminationReason(StrEnum):
    CANCELLED = "cancelled"
    TIMEOUT = "timeout"
    APPROVAL_DENIED = "approval_denied"
    POLICY_VIOLATION = "policy_violation"
```

处理规则：

1. `HookRunner` 和 `InterceptorChain` 必须透传 `AgentControlError`、`asyncio.CancelledError`、`KeyboardInterrupt`、`SystemExit`。
2. 普通 hook 异常按配置处理：`ignore`、`log`、`abort`。
3. Hook 除异常外仍保留现有功能：可以观察、修改上下文、注入 runtime context、记录调用信息、flush inbox、自动转发 peer 内容。
4. Interceptor 可以通过返回替代结果短路，也可以通过受控异常终止。
5. ControlCommand 经 drain 后转换为同一套受控异常或上下文变更，不另起一套退出语义。

### 0.4 Tool 审批两种拒绝模式

tool 调用前审批建议实现为 `ToolApprovalInterceptor`，因为它需要包裹具体 tool call，并保证 provider 消息链和 session/history 一致。

审批拒绝必须支持两套模式：

| 模式 | 行为 | 使用场景 |
|------|------|----------|
| `deny_as_tool_error` | 生成合法的伪错误 `ToolResult`，保存到 session/history，告诉模型“审批失败，工具未执行”，agent 继续运行 | 默认模式，适合让模型解释、换方案或请求用户补充 |
| `deny_as_cancel` | 写入终止状态或 checkpoint，抛出 `ApprovalDenied`/`AgentCancelled`，退出当前 agent run，等待下一次用户输入或恢复 | 高风险工具、强审批场景、拒绝后不希望模型继续尝试 |

`deny_as_tool_error` 不是静默跳过。它必须补齐 assistant tool_call 对应的 tool result，例如：

```python
ToolResult(
    tool_call_id=tool_call.id,
    tool_name=tool_call.name,
    is_error=True,
    content="Tool execution was not approved. The tool was not run.",
)
```

并且该结果必须进入 session/history，避免下一轮 LLM 请求时出现“assistant 发起了 tool_call，但缺少对应 tool result”的协议错误。

如果一个 assistant 消息里包含多个 tool call，审批拒绝或取消时也必须处理批量一致性：

1. `deny_as_tool_error`：被拒绝的 tool call 写入伪错误 result；未审批/未执行的 tool call 也要按策略补齐 result 或暂停整个 batch。
2. `deny_as_cancel`：保存 pending tool calls 到 checkpoint 或 termination metadata；恢复时必须能补齐合法 tool result，或回滚到 tool_call 前的稳定状态。

### 0.5 预配置与外部输入

预配置策略和外部输入都统一为 `ControlCommand`，只通过 `source`、`scope`、`priority`、`ttl_seconds` 等字段区分来源。

示例：

```text
source = "preset:token_budget"
source = "preset:wall_clock_timeout"
source = "external:user"
source = "external:admin"
source = "system:approval_timeout"
```

`ControlCommand` 建议包含：

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

第一阶段不要求 pause/resume 做齐，优先支持：

- cancel turn/run
- tool approval request/response
- approval timeout
- loop/tool timeout
- preset budget cancel
- checkpoint/termination metadata 的最小一致性

### 0.6 第一阶段落地范围

第一阶段按以下范围实现：

1. 新建或整理 `framework.hooks`，迁移并增强现有 hook 能力，加入 `HookPoint` 和 `HookRunner`。
2. 新建 `framework.interceptors`，先支持 tool 和 turn/loop 边界。
3. 新建 `framework.control`，定义 `ControlCommand`、channel、event bus、drain 的最小接口。
4. 定义 `AgentControlError`、`TerminationReason`。
5. 实现 `ToolApprovalInterceptor`，支持 `deny_as_tool_error` 和 `deny_as_cancel`。
6. 实现 tool/turn timeout 的基础策略。
7. 暂不把 pause/resume、stream interceptor、热更新配置放入第一阶段。
8. 移除旧入口和旧错误实现，不为 `framework.hook`、`framework.intercept`、`framework.intervention` 或旧 `multi_agent/intervention.py` 提供兼容 re-export。

## 一、问题分析

### 1.1 当前问题

| 问题 | 现状 | 目标 |
|------|------|------|
| Hook 调用字符串硬编码 | `_call_hooks("before_turn", ...)` 用 `getattr` 字符串匹配 | `HookPoint` 枚举 + `HookRunner` 类型化调度 |
| Hook 只适用 ReAct | 8 个生命周期点硬编码在 `_call_hooks` | _通用基类 + 每模式定义自己的 Hook 协议_ |
| 旧实现散落各处 | `core/hooks.py`, `multi_agent/hooks.py`, `multi_agent/inbox/hook.py` | 统一到 `framework/hooks/` 独立 package |
| 无 @Around 拦截 | LLM/Tool/Iteration/Loop 无洋葱包裹 | `framework/intercept/` 拦截器体系 |
| 中断/兜底/快照缺失 | 无 checkpoint、阻断后丢状态、审批拒绝缺 tool result | `framework/intervention/` 干预体系 |
| 干预归属错误 | `multi_agent/intervention.py` 仅任务级策略检查 | 移除重写，新建通用干预体系 |
| getattr 不规范 | `getattr(hook, method_name, None)` 无类型检查 | `HookRunner.dispatch(HookPoint.XXX, ...)` |

### 1.2 三机制分工

```
┌──────────────────────────────────────────────────────────────────────┐
│                        Agent / Pipeline 运行时                        │
│                                                                      │
│  ┌─────────────┐   ┌────────────────┐   ┌───────────────────┐        │
│  │    Hook      │   │   Intercept    │   │   Intervention    │        │
│  │  (生命周期切面) │   │  (方法级环绕)   │   │   (外部双向交互)   │        │
│  ├─────────────┤   ├────────────────┤   ├───────────────────┤        │
│  │ HookPoint 枚举│   │ 洋葱模型        │   │ Channel 指令通道   │        │
│  │ HookRunner   │   │ 可阻断/重试/降级 │   │ Bus 事件上报      │        │
│  │ 模式相关协议   │   │ ★ 强制兜底       │   │ Checkpoint 快照   │        │
│  │ 可修改对象    │   │ 模式无关         │   │ Priority 指令调度  │        │
│  └─────────────┘   └────────────────┘   └───────────────────┘        │
│                                                                      │
│  framework/hooks/   framework/intercept/   framework/intervention/   │
└──────────────────────────────────────────────────────────────────────┘
```

- **Hook**：固定横切点触发，由 agent 模式定义生命周期。可观察+修改上下文。每模式独立协议。
- **Intercept**：包裹具体方法调用（LLM/Tool/迭代），与模式无关。可阻断/重试。必须保证兜底。
- **Intervention**：外部系统 ↔ 运行中 agent 通道。推送事件、拉取指令、保存快照。

**三者独立拔插**：可只用 Hook、只用 Intercept、只用 Intervention，或全用。


## 二、目录结构

```
framework/
│
├── hook/                              # ★ 新建：Hook 系统(注意, 不叫hooks)
│   ├── __init__.py
│   ├── abc.py                          # AgentRunHook 基类 + HookPoint 枚举
│   ├── protocols.py                    # ReAct / Plan 等模式的 Hook 协议
│   ├── runner.py                       # HookRunner 调度器
│   ├── composite.py                    # CompositeHook
│   ├── builtin/
│   │   ├── __init__.py
│   │   ├── logging.py                  # RunLoggingHook (← core/hooks.py)
│   │   ├── runtime_context.py          # RuntimeContextHook (← core/hooks.py)
│   │   ├── inbox_flush.py              # InboxFlushHook (← multi_agent/inbox/hook.py)
│   │   ├── peer_auto_send.py           # PeerAutoSendHook (← multi_agent/hooks.py)
│   │   ├── subagent_cleanup.py         # SubagentMemoryCleanupHook (← multi_agent/hooks.py)
│   │   ├── task_intervention.py        # TaskInterventionHook (← multi_agent/hooks.py)
│   │   ├── progress.py                 # TaskProgressHook (← multi_agent/hooks.py)
│   │   └── reporting.py               # ★ 新增: InterventionReportingHook
│   └── README.md
│
├── intercept/                          # ★ 新建：拦截器系统
│   ├── __init__.py
│   ├── abc.py                          # AgentIntercept ABC + LLMCallContext 等结构体
│   ├── chain.py                        # InterceptChain（洋葱调度 + 兜底）
│   ├── builtin/
│   │   ├── __init__.py
│   │   ├── llm.py                      # RetryIntercept / FallbackIntercept / ResponseValidationIntercept
│   │   ├── tool.py                     # ApprovalIntercept / SandboxRoutingIntercept / ToolResultTruncateIntercept
│   │   └── iteration.py               # InterventionDrainIntercept / TimeoutIntercept
│   └── README.md
│
├── intervention/                       # ★ 新建：干预系统
│   ├── __init__.py
│   ├── abc.py                          # InterventionChannel / Bus / CheckpointManager ABC
│   ├── types.py                        # Intervention / InterventionEvent / AgentCheckpoint
│   ├── channel.py                      # InMemoryInterventionChannel
│   ├── bus.py                          # CallbackInterventionBus
│   ├── checkpoint.py                   # MemoryCheckpointManager / SessionMetadataCheckpointManager
│   ├── commands.py                     # PriorityCommandRouter
│   └── README.md
│
├── core/
│   ├── hooks.py                        # 删除内容，改为 from framework.hooks.abc import *
│   ├── agent.py                        # AgentContext 新增 intercepts / intervention_* / checkpoint_manager
│   └── ...
│
├── agents/react/
│   ├── agent.py                        # 用 HookRunner + InterceptChain 替换 _call_hooks
│   └── ...
│
├── multi_agent/
│   ├── hooks.py                        # 删除内容，改为 re-export
│   ├── inbox/hook.py                   # 删除内容
│   ├── intervention.py                 # ★ 整体删除
│   └── ...
```

### 与旧文件的对照

| 旧文件 | 操作 |
|--------|------|
| `core/hooks.py` | 删除 `AgentRunHook`/`RuntimeContextHook`/`CompositeRunHook`/`RunLoggingHook`，改为 `from framework.hooks.abc import *` |
| `multi_agent/hooks.py` | 删除所有类，改为 re-export |
| `multi_agent/inbox/hook.py` | 删除 |
| `multi_agent/intervention.py` | **整体删除** |
| `multi_agent/__init__.py` | 删除 `InterventionAction` 等导出 |


## 三、framework/hooks/ — Hook 系统

Hook 系统**不绑定任何 Agent 模式**。不同模式定义自己的 HookPoint，
HookRunner 通过 `getattr` 调度，未实现的方法静默跳过。

### 3.1 核心 ABC

```python
# framework/hooks/abc.py

from abc import ABC
from typing import Any, TYPE_CHECKING, Protocol, runtime_checkable

if TYPE_CHECKING:
    from framework.core.execution_context import ExecutionContext
    from framework.core.types import LLMResponse
    from framework.intervention.types import Intervention, AgentCheckpoint


class HookPoint:
    """Hook 生命周期点命名空间（可扩展，不限模式）。

    使用方式：
        HookPoint.ON_ERROR           → "on_error"
        HookPoint.ReAct.TURN_START   → "on_turn_start"
        HookPoint.Plan.STEP_START    → "on_step_start"

    任何 agent 模式只需引用 HookPoint 下的常量作为调度 key。
    非枚举设计 — 新 agent 模式只需在类上新增属性即可扩展，
    无需修改现有 HookPoint 定义。
    """

    # ── 通用（所有执行模式共享） ──
    ON_ERROR = "on_error"
    ON_INTERRUPT = "on_interrupt"
    ON_INTERVENTION = "on_intervention"
    FINALIZE_CONTENT = "finalize_content"
    ON_START = "on_start"          # 通用开始
    ON_END = "on_end"              # 通用结束
    ON_LLM_RESPONSE = "on_llm_response"
    ON_TOOL_EXECUTION_START = "on_tool_execution_start"
    ON_TOOL_EXECUTION_END = "on_tool_execution_end"

    # ── ReAct 模式 ──
    class ReAct:
        TURN_START = "on_turn_start"
        ITERATION_START = "on_iteration_start"
        ITERATION_END = "on_iteration_end"
        TURN_END = "on_turn_end"

    # ── Plan 模式（未来扩展示例） ──
    class Plan:
        PLAN_CREATED = "on_plan_created"
        STEP_START = "on_step_start"
        STEP_END = "on_step_end"
        PLAN_COMPLETED = "on_plan_completed"

    # ── Pipeline 模式（非 Agent 场景） ──
    class Pipeline:
        BEFORE_PROCESS = "on_before_process"
        AFTER_PROCESS = "on_after_process"
        BEFORE_FILTER = "on_before_filter"
        AFTER_FILTER = "on_after_filter"


class AgentRunHook(ABC):
    """执行 Hook 基类 — 不限 Agent 模式，不限执行场景。

    所有方法有默认空实现。子类覆盖需要的方法即可。
    HookRunner 通过 getattr 调度，未实现的方法静默跳过。

    可运行在：
    - Agent 内（ReAct/Plan/Chain/Tree-of-Thought 等）
    - Pipeline 步骤中
    - 独立的 Tool/LM 调用中
    - 任何有 ExecutionContext 的上下文中
    """

    # ── 通用（所有模式/场景共享） ──

    async def on_start(self, ctx: "ExecutionContext") -> None: pass
    async def on_end(self, ctx: "ExecutionContext") -> None: pass
    async def on_error(self, ctx: "ExecutionContext", error: Exception) -> None: pass

    async def on_interrupt(
        self, ctx: "ExecutionContext", reason: str, checkpoint: "AgentCheckpoint | None"
    ) -> None: pass

    async def on_intervention(
        self, ctx: "ExecutionContext", intervention: "Intervention"
    ) -> bool | None:
        """返回 False 拒绝该指令。"""
        return None

    def finalize_content(self, ctx: "ExecutionContext", content: str | None) -> str | None:
        return content

    async def on_llm_response(self, ctx: "ExecutionContext", response: "LLMResponse") -> None: pass
    async def on_tool_execution_start(self, ctx: "ExecutionContext", tool_calls: list[Any]) -> None: pass
    async def on_tool_execution_end(self, ctx: "ExecutionContext", results: list[Any]) -> None: pass

    # ── ReAct 模式 ──

    async def on_turn_start(self, ctx: "ExecutionContext") -> None: pass
    async def on_iteration_start(self, ctx: "ExecutionContext") -> None: pass
    async def on_iteration_end(self, ctx: "ExecutionContext") -> None: pass
    async def on_turn_end(self, ctx: "ExecutionContext") -> None: pass

    # ── Plan 模式（未来） ──

    async def on_plan_created(self, ctx: "ExecutionContext", plan: Any) -> None: pass
    async def on_step_start(self, ctx: "ExecutionContext", step: Any) -> None: pass
    async def on_step_end(self, ctx: "ExecutionContext", step: Any, result: Any) -> None: pass
    async def on_plan_completed(self, ctx: "ExecutionContext", final: Any) -> None: pass


@runtime_checkable
class ReActHookProtocol(Protocol):
    """ReAct 模式专用的 hook 方法签名（用于 isinstance 检查）。"""
    async def on_turn_start(self, ctx) -> None: ...
    async def on_iteration_start(self, ctx) -> None: ...
    async def on_llm_response(self, ctx, response) -> None: ...
    async def on_tool_execution_start(self, ctx, tool_calls) -> None: ...
    async def on_tool_execution_end(self, ctx, results) -> None: ...
    async def on_iteration_end(self, ctx) -> None: ...
    async def on_turn_end(self, ctx) -> None: ...
```

### 3.2 HookRunner

```python
# framework/hooks/runner.py

import asyncio
import logging
from typing import Any

logger = logging.getLogger(__name__)
_DEFAULT_TIMEOUT = 10.0


class HookRunner:
    """Hook 调度器 — 不绑定 AgentContext，接受任何有 hooks 属性的对象。

    调度约定：
    1. 读取 ctx.hooks 列表
    2. 对每个 hook，getattr(hook, hook_point) 获取方法
    3. 若存在则调用，不存在则跳过
    4. 每个 hook 独立超时 + 独立错误隔离

    用法:
        runner = HookRunner(ctx.hooks, timeout=10.0)
        await runner.dispatch(HookPoint.ReAct.TURN_START, ctx)
        await runner.dispatch(HookPoint.ON_LLM_RESPONSE, ctx, response)
    """

    def __init__(self, hooks: list["AgentRunHook"], timeout: float = _DEFAULT_TIMEOUT):
        self._hooks = [h for h in (hooks or []) if h is not None]
        self._timeout = timeout

    async def dispatch(
        self, hook_point: str, ctx, *args: Any, **kwargs: Any
    ) -> None:
        """异步调度。ctx 只需实现 agent 无关的基础字段（session_id 等）。"""
        for hook in self._hooks:
            method = getattr(hook, hook_point, None)
            if method is None:
                continue
            try:
                await asyncio.wait_for(
                    method(ctx, *args, **kwargs), timeout=self._timeout
                )
            except asyncio.CancelledError:
                raise
            except asyncio.TimeoutError:
                logger.warning(
                    "Hook %s.%s timed out after %.1fs",
                    type(hook).__name__, hook_point, self._timeout,
                )
            except Exception:
                logger.exception("Hook %s.%s failed", type(hook).__name__, hook_point)

    def dispatch_sync(
        self, hook_point: str, ctx, *args: Any
    ) -> Any:
        """同步调度，后一个 hook 接收前一个返回值。"""
        result = args[0] if args else None
        for hook in self._hooks:
            method = getattr(hook, hook_point, None)
            if method is None:
                continue
            try:
                result = method(ctx, result)
            except Exception:
                logger.exception("Hook %s.%s failed", type(hook).__name__, hook_point)
        return result
```

### 3.3 CompositeHook

```python
# framework/hooks/composite.py

class CompositeHook(AgentRunHook):
    """组合多个 Hook，串行调用，独立错误隔离。"""

    def __init__(self, hooks: list[AgentRunHook] | None = None):
        self._hooks = list(hooks or [])

    async def on_turn_start(self, ctx):
        for h in self._hooks:
            with suppress(Exception): await h.on_turn_start(ctx)

    async def on_iteration_start(self, ctx):
        for h in self._hooks:
            with suppress(Exception): await h.on_iteration_start(ctx)

    # ... 其余方法同理 ...

    def finalize_content(self, ctx, content):
        for h in self._hooks:
            with suppress(Exception):
                content = h.finalize_content(ctx, content)
        return content
```

### 3.4 ExecutionContext — 解耦 Agent/非 Agent 场景

Hook / Intercept / Intervention 不应绑定 `AgentContext`。引入 `ExecutionContext` 作为通用基：

```python
# framework/core/execution_context.py

from dataclasses import dataclass, field
from typing import Any, TYPE_CHECKING

if TYPE_CHECKING:
    from framework.hooks.abc import AgentRunHook
    from framework.intercept.abc import AgentIntercept
    from framework.intervention.abc import InterventionChannel, InterventionBus, CheckpointManager


@dataclass
class ExecutionContext:
    """通用执行上下文 — Hook / Intercept / Intervention 的最小依赖。

    Agent、Pipeline、独立工具调用等场景均可使用。
    AgentContext 继承此类，增加 Agent 专属字段。
    """

    session_id: str = ""
    metadata: dict[str, Any] = field(default_factory=dict)

    # Hook / Intercept / Intervention 基础设施
    hooks: list["AgentRunHook"] = field(default_factory=list)
    intercepts: list["AgentIntercept"] = field(default_factory=list)
    intervention_channel: "InterventionChannel | None" = None
    intervention_bus: "InterventionBus | None" = None
    checkpoint_manager: "CheckpointManager | None" = None
```

```python
# framework/core/agent.py — AgentContext 继承 ExecutionContext

@dataclass
class AgentContext(ExecutionContext):
    """Agent 执行上下文 — 继承通用钩子/拦截/干预能力。"""

    system_prompt: str = ""
    history: "MessageHistory" = field(default_factory=MessageHistory)
    tool_manager: "ToolManager" = field(default_factory=ToolManager)
    max_iterations: int = 10
    max_tools_per_turn: int = 10
    temperature: float | None = None
    max_tokens: int | None = None
    attachments: list[str] = field(default_factory=list)
    runtime_context_manager: "RuntimeContextManager | None" = None
    runtime_context: "RuntimeContext | None" = None
    governance: "ContextGovernance | None" = None

    async def to_messages(self) -> list[dict]: ...
    def get_tool_descriptions(self) -> list[dict]: ...
```

### 3.5 跨模式使用示例

#### ReAct Agent

```python
# framework/agents/react/agent.py

from framework.hooks.runner import HookRunner
from framework.hooks.abc import HookPoint

class ReActAgent(Agent):
    async def run(self, context, emitter):
        hooks = HookRunner(context.hooks, timeout=self._hook_timeout)
        chain = InterceptChain(context.intercepts)

        try:
            await hooks.dispatch(HookPoint.ReAct.TURN_START, context)
            result = await chain.around_loop(context, self._loop_body(...))
            return result
        except CancelledError:
            await hooks.dispatch(HookPoint.ON_INTERRUPT, context, "cancelled", checkpoint)
            raise
        finally:
            await hooks.dispatch(HookPoint.ReAct.TURN_END, context)

    async def _execute_iteration(self, ctx, hooks, chain):
        await hooks.dispatch(HookPoint.ReAct.ITERATION_START, ctx)

        response = await chain.around_llm_call(ctx, self._do_llm, llm_ctx)
        await hooks.dispatch(HookPoint.ON_LLM_RESPONSE, ctx, response)

        if tool_calls:
            await hooks.dispatch(HookPoint.ON_TOOL_EXECUTION_START, ctx, tool_calls)
            for tc in tool_calls:
                await chain.around_tool_call(ctx, self._do_tool, tool_ctx)
            await hooks.dispatch(HookPoint.ON_TOOL_EXECUTION_END, ctx, results)

        await hooks.dispatch(HookPoint.ReAct.ITERATION_END, ctx)
```

#### Plan Agent（未来）

```python
class PlanAgent(Agent):
    async def run(self, context, emitter):
        hooks = HookRunner(context.hooks)
        chain = InterceptChain(context.intercepts)

        await hooks.dispatch(HookPoint.ON_START, context)

        plan = await self._create_plan(context)
        await hooks.dispatch(HookPoint.Plan.PLAN_CREATED, context, plan)

        for step in plan.steps:
            await hooks.dispatch(HookPoint.Plan.STEP_START, context, step)

            response = await chain.around_llm_call(context, self._do_llm, llm_ctx)
            result = await chain.around_tool_call(context, self._do_tool, tool_ctx)

            await hooks.dispatch(HookPoint.Plan.STEP_END, context, step, result)

        await hooks.dispatch(HookPoint.Plan.PLAN_COMPLETED, context, final_result)
        await hooks.dispatch(HookPoint.ON_END, context)
```

#### 非 Agent 场景（独立 Pipeline 步骤）

```python
# Pipeline 中使用 Hook + Intercept，不依赖 AgentContext

class DataProcessingStep:
    def __init__(self, ctx: ExecutionContext):
        self.ctx = ctx

    async def execute(self, data):
        hooks = HookRunner(self.ctx.hooks)
        chain = InterceptChain(self.ctx.intercepts)

        await hooks.dispatch(HookPoint.Pipeline.BEFORE_PROCESS, self.ctx, data)

        # 用 LLM 增强数据（Intercept 包裹）
        llm_ctx = LLMCallContext(messages=[...], tools=None, temperature=0.3, max_tokens=None)
        response = await chain.around_llm_call(self.ctx, self._call_llm, llm_ctx)

        result = self._transform(data, response.content)

        await hooks.dispatch(HookPoint.Pipeline.AFTER_PROCESS, self.ctx, result)
        return result
```


## 四、framework/intercept/ — 拦截器系统

Intercept 系统**不绑定任何 Agent 模式**。`around_llm_call` 和 `around_tool_call`
可用于任何有 LLM/Tool 调用的场景；`around_iteration` 和 `around_loop`
仅由迭代型 Agent 调用。

### 4.1 AgentIntercept ABC

```python
# framework/intercept/abc.py

from dataclasses import dataclass, field
from abc import ABC
from typing import Any, Awaitable, Callable


@dataclass
class LLMCallContext:
    messages: list[dict[str, Any]]
    tools: list[dict[str, Any]] | None
    temperature: float | None
    max_tokens: int | None
    extra: dict[str, Any] = field(default_factory=dict)


@dataclass
class ToolCallContext:
    tool_name: str
    arguments: dict[str, Any]
    call_id: str | None


@dataclass
class IterationContext:
    iteration: int
    max_iterations: int


class AgentIntercept(ABC):
    """环绕拦截器 — 包裹关键方法调用（模式无关）。

    与 Hook 区别：Hook 在固定横切点触发，Intercept 包裹具体方法。
    """

    async def around_llm_call(
        self, ctx, call_next: Callable, llm_ctx: LLMCallContext
    ) -> "LLMResponse":
        return await call_next(llm_ctx)

    async def around_tool_call(
        self, ctx, call_next: Callable, tool_ctx: ToolCallContext
    ) -> "ToolResult":
        return await call_next(tool_ctx)

    async def around_iteration(
        self, ctx, call_next: Callable, iter_ctx: IterationContext
    ) -> Any:
        return await call_next()

    async def around_loop(
        self, ctx, call_next: Callable
    ) -> "AgentResult":
        return await call_next()
```

### 4.2 InterceptChain（洋葱调度 + 兜底）

```python
# framework/intercept/chain.py

class InterceptChain:
    """洋葱模型调度器。★ around_tool_call 强制兜底。"""

    def __init__(self, intercepts: list[AgentIntercept] | None = None):
        self._intercepts = list(intercepts or [])

    async def around_llm_call(self, ctx, fn, llm_ctx):
        return await self._execute("around_llm_call", ctx, fn, llm_ctx)

    async def around_tool_call(self, ctx, fn, tool_ctx):
        from framework.core.tool_manager import ToolResult
        try:
            result = await self._execute("around_tool_call", ctx, fn, tool_ctx)
        except Exception as e:
            result = ToolResult(
                tool_name=tool_ctx.tool_name,
                error=f"Error: Tool intercept failed: {e}",
            )
        if not isinstance(result, ToolResult):
            result = ToolResult(
                tool_name=tool_ctx.tool_name,
                error="Error: Tool intercept returned invalid result.",
            )
        return result

    async def around_iteration(self, ctx, fn, iter_ctx):
        return await self._execute("around_iteration", ctx, fn, iter_ctx)

    async def around_loop(self, ctx, fn):
        return await self._execute("around_loop", ctx, fn)

    async def _execute(self, method_name: str, ctx, final_fn, *args):
        cur_fn = final_fn
        for intercept in reversed(self._intercepts):
            method = getattr(intercept, method_name, None)
            if method is None:
                continue
            prev = cur_fn
            async def _w(*a, _p=prev, _m=method, **kw):
                return await _m(ctx, _p, *a, **kw)
            cur_fn = _w
        return await cur_fn(*args)
```

### 4.3 内置拦截器

```python
# framework/intercept/builtin/llm.py

class RetryIntercept(AgentIntercept):
    def __init__(self, max_attempts=3, backoff_base=1.0,
                 retryable_exceptions: tuple = (Exception,)):
        self._max = max_attempts
        self._backoff = backoff_base
        self._retryable = retryable_exceptions

    async def around_llm_call(self, ctx, call_next, llm_ctx):
        for attempt in range(self._max):
            try:
                return await call_next(llm_ctx)
            except self._retryable as e:
                if attempt == self._max - 1:
                    raise
                await asyncio.sleep(self._backoff * (2 ** attempt))


class FallbackIntercept(AgentIntercept):
    def __init__(self, fallback_provider):
        self._fb = fallback_provider

    async def around_llm_call(self, ctx, call_next, llm_ctx):
        try:
            return await call_next(llm_ctx)
        except Exception:
            return await self._fb.chat(
                llm_ctx.messages, tools=llm_ctx.tools,
                temperature=llm_ctx.temperature,
                max_tokens=llm_ctx.max_tokens,
            )
```

```python
# framework/intercept/builtin/tool.py

class ApprovalIntercept(AgentIntercept):
    """工具审批拦截器。复用 framework/security/handlers.py 的 ApprovalHandler。"""
    def __init__(self, handlers, *, require_approval_for=None,
                 skip_approval_for=None, timeout=30.0, on_denied="error"):
        self._handlers = handlers
        self._require = require_approval_for or set()
        self._skip = skip_approval_for or set()
        self._timeout = timeout
        self._on_denied = on_denied

    async def around_tool_call(self, ctx, call_next, tool_ctx):
        if not self._needs_approval(tool_ctx.tool_name):
            return await call_next(tool_ctx)
        if await self._run_approval(tool_ctx):
            return await call_next(tool_ctx)
        return self._deny_result(tool_ctx)

    def _needs_approval(self, name):
        if name in self._skip or "*" in self._skip:
            return False
        if self._require:
            return name in self._require
        return True

    async def _run_approval(self, tool_ctx):
        reason = f"Tool '{tool_ctx.tool_name}' args={tool_ctx.arguments}"
        for h in self._handlers:
            try:
                if not await asyncio.wait_for(h.approve(tool_ctx.tool_name, reason),
                                               timeout=self._timeout):
                    return False
            except (asyncio.TimeoutError, Exception):
                return False
        return True

    def _deny_result(self, tool_ctx):
        if self._on_denied == "cancel":
            raise asyncio.CancelledError("Tool denied by approval")
        if self._on_denied == "skip":
            return ToolResult(tool_name=tool_ctx.tool_name,
                              result="[Tool execution skipped by policy]")
        return ToolResult(tool_name=tool_ctx.tool_name,
                          error="Error: Tool execution denied by approval policy.")


class ToolResultTruncateIntercept(AgentIntercept):
    def __init__(self, max_chars=20000):
        self._max = max_chars

    async def around_tool_call(self, ctx, call_next, tool_ctx):
        result = await call_next(tool_ctx)
        if result.result and len(str(result.result)) > self._max:
            result.result = str(result.result)[:self._max] + "\n...(truncated)"
        return result
```

```python
# framework/intercept/builtin/iteration.py

class InterventionDrainIntercept(AgentIntercept):
    """迭代边界泄洪干预指令。

    会调用 ctx.hooks 中每个 hook 的 on_intervention() 方法做权限检查。
    任何 hook 返回 False 则该指令被丢弃。
    """
    def __init__(self, channel, max_per_drain=3, drain_on_message=True):
        self._channel = channel
        self._max = max_per_drain
        self._drain_msg = drain_on_message

    async def around_iteration(self, ctx, call_next, iter_ctx):
        for iv in await self._channel.drain(ctx.session_id, limit=self._max):
            # ★ 让 Hook 有机会拒绝该指令
            if not await self._check_hooks(ctx, iv):
                continue

            if iv.type == InterventionType.CANCEL:
                raise asyncio.CancelledError(
                    iv.payload.get("reason", "Task cancelled"))
            if iv.type == InterventionType.MESSAGE and self._drain_msg:
                await ctx.history.append({
                    "role": "user",
                    "content": iv.payload.get("content", ""),
                })
        return await call_next()

    async def _check_hooks(self, ctx, iv) -> bool:
        """Hook 权限检查：任何 hook 返回 False 则拒绝。"""
        for hook in (ctx.hooks or []):
            try:
                result = await hook.on_intervention(ctx, iv) if hasattr(hook, 'on_intervention') else None
                if result is False:
                    return False
            except Exception:
                pass  # hook 异常不影响指令处理
        return True


class TimeoutIntercept(AgentIntercept):
    def __init__(self, timeout_seconds, on_timeout="cancel"):
        self._timeout = timeout_seconds
        self._on_timeout = on_timeout

    async def around_loop(self, ctx, call_next):
        try:
            return await asyncio.wait_for(call_next(), timeout=self._timeout)
        except asyncio.TimeoutError:
            if self._on_timeout == "return_partial":
                return AgentResult(
                    content="[Agent execution timed out]",
                    stop_reason="timeout")
            raise asyncio.CancelledError("Agent loop timed out")
```

### 4.4 兜底保证

| 失败场景 | 兜底 | 责任方 |
|----------|------|--------|
| 审批超时 | `ToolResult(error="...")` | `ApprovalIntercept._deny_result()` |
| 审批拒绝 | `ToolResult(error="...")` | `ApprovalIntercept._deny_result()` |
| 拦截器异常 | `ToolResult(error="Tool intercept failed: {e}")` | `InterceptChain.around_tool_call()` |
| 返回 None/非 ToolResult | 强制转换为 error ToolResult | `InterceptChain.around_tool_call()` |
| LLM 失败 | 重试 / 降级 | `RetryIntercept` / `FallbackIntercept` |

**核心保证：assistant 的每个 tool_call 都有合法的 tool result 消息。**


## 五、framework/intervention/ — 干预系统

### 5.1 旧系统移除

`framework/multi_agent/intervention.py` 整体删除。旧代码的 `TaskSupervisor` / `TaskInterventionPolicy` / `InterventionAction` / `InterventionResult` 不再保留。

新系统不再做"任务级策略"和"运行时交互"的分离，统一为外部 ↔ Agent 的双向通道。

### 5.2 类型定义

```python
# framework/intervention/types.py

class InterventionType(str, Enum):
    MESSAGE = "message"
    CANCEL = "cancel"
    PAUSE = "pause"
    RESUME = "resume"
    MODIFY_SYSTEM = "modify_system"
    PROGRESS = "progress"          # 异步工具进度上报
    TOOL_RESULT = "tool_result"    # 异步工具结果回传


class InterventionEventType(str, Enum):
    CHECKPOINT_SAVED = "checkpoint_saved"
    CHECKPOINT_RESTORED = "checkpoint_restored"
    ITERATION_START = "iteration_start"
    ITERATION_END = "iteration_end"
    TOOL_APPROVAL_GRANTED = "tool_approval_granted"
    TOOL_APPROVAL_DENIED = "tool_approval_denied"
    INTERVENTION_RECEIVED = "intervention_received"
    TASK_CANCELLED = "task_cancelled"
    ERROR = "error"


class CheckpointPhase(str, Enum):
    AWAITING_TOOLS = "awaiting_tools"
    TOOLS_COMPLETED = "tools_completed"
    FINAL_RESPONSE = "final_response"


@dataclass
class Intervention:
    type: InterventionType
    session_id: str
    payload: dict = field(default_factory=dict)
    priority: int = 0
    timestamp: float = 0.0


@dataclass
class InterventionEvent:
    type: InterventionEventType
    session_id: str
    payload: dict = field(default_factory=dict)
    timestamp: float = 0.0


@dataclass
class AgentCheckpoint:
    phase: CheckpointPhase
    iteration: int
    assistant_message: dict
    completed_tool_results: list[dict] = field(default_factory=list)
    pending_tool_calls: list[dict] = field(default_factory=list)
    timestamp: float = 0.0
    metadata: dict = field(default_factory=dict)
```

### 5.3 ABC 接口

```python
# framework/intervention/abc.py

class InterventionChannel(ABC):
    """外部 → Agent 指令通道"""
    @abstractmethod
    async def send(self, session_id: str, intervention: Intervention) -> None: ...
    @abstractmethod
    async def drain(self, session_id: str, limit: int = 5) -> list[Intervention]: ...
    async def cancel(self, session_id: str, reason: str = ""): ...
    async def inject_message(self, session_id: str, content: str): ...


class InterventionBus(ABC):
    """Agent → 外部 事件通道"""
    @abstractmethod
    async def emit(self, event: InterventionEvent) -> None: ...
    @abstractmethod
    async def subscribe(
        self, event_type: InterventionEventType,
        callback: Callable[[InterventionEvent], Awaitable[None]],
    ) -> None: ...


class CheckpointManager(ABC):
    """检查点管理器（参考 nanobot 3 阶段设计）"""
    @abstractmethod
    async def save(self, session_id: str, checkpoint: AgentCheckpoint) -> None: ...
    @abstractmethod
    async def load(self, session_id: str) -> AgentCheckpoint | None: ...
    @abstractmethod
    async def clear(self, session_id: str) -> None: ...
    async def restore_to_messages(
        self, session_id: str, messages: list[dict]
    ) -> bool:
        """物化检查点到消息列表 + 去重。"""
        ...
```

### 5.4 内置实现

```python
# framework/intervention/channel.py
class InMemoryInterventionChannel(InterventionChannel): ...

# framework/intervention/bus.py
class CallbackInterventionBus(InterventionBus): ...

# framework/intervention/checkpoint.py
class MemoryCheckpointManager(CheckpointManager): ...
class SessionMetadataCheckpointManager(CheckpointManager): ...

# framework/intervention/commands.py
class PriorityCommandRouter:
    """优先级指令调度（旁路锁，如 /stop）。参考 nanobot CommandRouter。"""
    def register_priority(self, command: str, handler): ...
    def register(self, command: str, handler): ...
    def is_priority(self, raw: str) -> bool: ...
    async def dispatch_priority(self, ctx) -> Any: ...
    async def dispatch(self, ctx) -> Any: ...
```

### 5.5 外部交互扩展点

所有 ABC 接口可通过自定义实现对接各种外部系统：

```python
# 示例：Redis、HTTP Webhook、WebSocket 实现
class RedisInterventionChannel(InterventionChannel): ...
class WebhookInterventionChannel(InterventionChannel): ...
class WebSocketInterventionBus(InterventionBus): ...
```


## 六、AgentContext 增强

```python
# framework/core/agent.py

@dataclass
class AgentContext:
    # ── 现有字段 ──
    system_prompt: str
    history: MessageHistory
    tool_manager: ToolManager
    session_id: str = ""
    max_iterations: int = 10
    max_tools_per_turn: int = 10
    temperature: float | None = None
    max_tokens: int | None = None
    metadata: dict = field(default_factory=dict)
    hooks: list[AgentRunHook] = field(default_factory=list)
    attachments: list[str] = field(default_factory=list)
    runtime_context_manager: RuntimeContextManager | None = None
    runtime_context: RuntimeContext | None = None
    governance: ContextGovernance | None = None

    # ★ 删除: on_checkpoint（由 CheckpointManager 替代）

    # ── 新增 ──
    intercepts: list["AgentIntercept"] = field(default_factory=list)
    intervention_channel: "InterventionChannel | None" = None
    intervention_bus: "InterventionBus | None" = None
    checkpoint_manager: "CheckpointManager | None" = None
```


## 七、examples/bot_project 适配

### 7.1 现有引用及改动

| 现有 import | 改为 |
|-------------|------|
| `from framework.core.hooks import RunLoggingHook` | `from framework.hooks.builtin.logging import RunLoggingHook` |
| `from framework.multi_agent.inbox.hook import InboxFlushHook` | `from framework.hooks.builtin.inbox_flush import InboxFlushHook` |
| `from framework.multi_agent.hooks import PeerAutoSendHook` | `from framework.hooks.builtin.peer_auto_send import PeerAutoSendHook` |

类本身不变，仅 import 路径迁移。

### 7.2 新写法示例

```python
# bot/service/core.py

from framework.hooks.builtin.logging import RunLoggingHook
from framework.hooks.builtin.inbox_flush import InboxFlushHook
from framework.intercept.chain import InterceptChain
from framework.intercept.builtin.llm import RetryIntercept, FallbackIntercept
from framework.intercept.builtin.tool import ApprovalIntercept, ToolResultTruncateIntercept
from framework.intercept.builtin.iteration import InterventionDrainIntercept, TimeoutIntercept
from framework.intervention.channel import InMemoryInterventionChannel
from framework.intervention.bus import CallbackInterventionBus
from framework.intervention.checkpoint import MemoryCheckpointManager
from framework.security.handlers import ConsoleApprovalHandler


class BotService:

    def __init__(self, config_dir, mode="pool"):
        # 干预基础设施（可选，不构造则对应字段为 None）
        self.intervention_channel = InMemoryInterventionChannel(max_pending=100)
        self.intervention_bus = CallbackInterventionBus()
        self.checkpoint_manager = MemoryCheckpointManager()

    def _collect_run_hooks(self) -> list[AgentRunHook]:
        hooks = self.plugin_integration.collect_hooks()
        if self.config.get("observability", {}).get("run_logging", {}).get("enabled"):
            hooks.append(RunLoggingHook(
                logger_name="bot.run",
                level=logging.INFO,
            ))
        return hooks

    def _build_main_intercepts(self) -> list[AgentIntercept]:
        """为主 agent 组装拦截器链。"""
        return [
            RetryIntercept(max_attempts=3, backoff_base=1.0),
            FallbackIntercept(fallback_provider=self.backup_provider),
            ApprovalIntercept(
                handlers=[ConsoleApprovalHandler()],
                require_approval_for={"exec", "write_file"},
                skip_approval_for={"read_file", "list_dir"},
                on_denied="error",
            ),
            ToolResultTruncateIntercept(max_chars=20000),
            InterventionDrainIntercept(
                channel=self.intervention_channel,
                max_per_drain=3,
            ),
            TimeoutIntercept(timeout_seconds=600, on_timeout="return_partial"),
        ]

    async def _initialize_pipeline(self):
        hooks = self._collect_run_hooks()
        inbox_hook = InboxFlushHook(
            consumer=self.inbox_consumer,
            agent_name="main",
        )

        self.pipeline = AgentPipeline(
            agent=self.agent,
            hooks=[inbox_hook] + hooks,
            intercepts=self._build_main_intercepts(),
            checkpoint_manager=self.checkpoint_manager,
            intervention_channel=self.intervention_channel,
            intervention_bus=self.intervention_bus,
            ...,
        )
```

```python
# bot/service/builders.py

from framework.hooks.builtin.peer_auto_send import PeerAutoSendHook

class AgentBuilderMixin:
    def _setup_peers(self, ...):
        # ... setup peer agents ...
        instance.pipeline.hooks.append(
            PeerAutoSendHook(
                agent_bus=self.agent_bus,
                self_name=peer_name,
                parent_name=parent_name,
            )
        )
```

### 7.3 不使用 Intercept/Intervention 时

```python
# 最小用法：只用 Hook，不用其他
pipeline = AgentPipeline(
    agent=self.agent,
    hooks=[InboxFlushHook(...), RunLoggingHook(...)],
    # intercepts 默认空 → 零开销透传
    # intervention_* 默认 None → 不启用
)
```


## 八、代码组合示例（测试）

```python
# 测试：只测 ApprovalIntercept，无 Hook、无 Intervention 依赖
async def test_approval_denied_still_returns_valid_tool_result():
    intercept = ApprovalIntercept(
        handlers=[ConfigBasedApprovalHandler(default_action=False)],
        require_approval_for={"exec"},
    )
    chain = InterceptChain([intercept])

    async def mock_fn(tctx):
        pytest.fail("should not reach here")

    result = await chain.around_tool_call(
        make_context(),
        mock_fn,
        ToolCallContext(tool_name="exec", arguments={}, call_id="c1"),
    )
    # ★ 关键是返回合法 ToolResult
    assert isinstance(result, ToolResult)
    assert result.error and "denied" in result.error.lower()


# 测试：只测 InterventionChannel，独立拔插
async def test_channel_cancel():
    channel = InMemoryInterventionChannel()
    await channel.cancel("s1", "stop")
    items = await channel.drain("s1")
    assert items[0].type == InterventionType.CANCEL
```


## 九、与 nanobot 的对照

| 模式 | nanobot | ModexAgent |
|------|---------|-----------|
| Hook 接口 | `AgentHook` 6 方法 | `AgentRunHook` + 每模式 Protocol |
| Hook 调度 | 直接 `hook.method()` | `HookRunner.dispatch(HookPoint.XXX)` |
| 检查点 3 阶段 | `awaiting_tools/tools_completed/final_response` | 同样枚举 |
| 检查点恢复 | `_restore_runtime_checkpoint()` 物化+去重 | `CheckpointManager.restore_to_messages()` |
| 消息注入 | `_pending_queues` | `InterventionChannel.drain()` |
| 优先级指令 | `CommandRouter.priority()` 旁路锁 | `PriorityCommandRouter` |
| 取消恢复 | CancelledError → checkpoint restore → leftover re-publish | 同样模式 |
| Tool 兜底 | `_backfill_missing_tool_results()` | `InterceptChain` 强制兜底 |


## 八、三者共存的执行顺序与冲突规避

### 8.1 完整调用链（三者全开）

以单次 ReAct 迭代为例，从上到下是执行时序：

```
┌─ Iteration 级 ──────────────────────────────────────────────────┐
│                                                                  │
│  Intercept.around_iteration                                      │
│  ├─ InterventionDrainIntercept.drain()  ← 泄洪干预指令            │
│  │   └─ 对每条指令调用 hook.on_intervention() 权限检查             │
│  │                                                               │
│  ├─ Hook: on_iteration_start(ctx)                                │
│  │                                                               │
│  │  ┌─ LLM 调用 ───────────────────────────────────┐            │
│  │  │                                               │            │
│  │  │  Intercept.around_llm_call                    │            │
│  │  │  ├─ RetryIntercept (可重试, may call_next多次) │            │
│  │  │  │  ├─ FallbackIntercept (异常降级)            │            │
│  │  │  │  │  └─ call_next → provider.chat()         │            │
│  │  │  │  └─ return LLMResponse                     │            │
│  │  │  └─ return LLMResponse                        │            │
│  │  │                                               │            │
│  │  │  Hook: on_llm_response(ctx, response)          │            │
│  │  │  ↑ hook 可以修改 response.tool_calls 等         │            │
│  │  └───────────────────────────────────────────────┘            │
│  │                                                               │
│  │  if tool_calls:                                               │
│  │                                                               │
│  │  Hook: on_tool_execution_start(ctx, tool_calls)                │
│  │  ↑ hook 可以修改 tool_calls 列表                                │
│  │                                                               │
│  │  for each tool_call:                                          │
│  │    ┌─ Tool 调用 ──────────────────────────────────┐           │
│  │    │                                               │           │
│  │    │  Intercept.around_tool_call                   │           │
│  │    │  ├─ ApprovalIntercept (审批, 可能阻断)          │           │
│  │    │  ├─ SandboxRoutingIntercept (沙箱路由)         │           │
│  │    │  │  └─ call_next → tool_manager.execute()     │           │
│  │    │  ├─ ToolResultTruncateIntercept (结果截断)     │           │
│  │    │  └─ return ★合法 ToolResult★ (兜底保证)        │           │
│  │    └───────────────────────────────────────────────┘           │
│  │                                                               │
│  │  Hook: on_tool_execution_end(ctx, results)                     │
│  │                                                               │
│  ├─ Hook: on_iteration_end(ctx)                                  │
│  │                                                               │
└─ call_next() → return result ───────────────────────────────────┘
```

**关键规则**：

| 规则 | 说明 |
|------|------|
| **Hook 在最外层** | Hook 横跨 Intercept 两侧，观察完整包裹过程 |
| **Intercept 在中间** | 洋葱式包裹实际调用，可阻断/重试/降级 |
| **真实调用在最内层** | provider.chat() / tool_manager.execute() |
| **Hook 后于 Intercept 执行** | Hook 修改的是 Intercept 已处理完毕的结果 |
| **Intercept 先于 Hook 执行** | Intercept 在真实调用前做预处理 |
| **Intervention drain 在迭代最前** | 干预指令在迭代开始前处理，不阻塞工具执行 |
| **on_intervention hook 过滤指令** | Hook 可拒绝干预指令 |

### 8.2 冲突场景分析

#### 场景 A: Hook 修改了被 Intercept 处理过的结果

```
Intercept.around_llm_call → RetryIntercept 重试后返回 response
Hook.on_llm_response → RunLoggingHook 修改 response.content（截断）
```

无冲突：Hook 后执行，修改的是最终版本。Intercept 不需要再看到它。

#### 场景 B: Hook 设置的状态被 Intercept 读取

```
Hook.on_tool_execution_start → RuntimeContextHook 设置 ctx.runtime_context
Intercept.around_tool_call → ApprovalIntercept 读取 runtime_context 判断是否需要审批
```

无冲突：Hook 在 `on_tool_execution_start` 时先执行，Intercept 在 `around_tool_call` 时读取。顺序正确。

#### 场景 C: 多个 Intercept 竞争修改同一数据

```
chain = InterceptChain([ApprovalIntercept, ToolResultTruncateIntercept])
```

ApprovalIntercept 先执行（外层），ToolResultTruncateIntercept 后执行（内层）：
1. ApprovalIntercept 可能返回 **error ToolResult**（阻断）
2. ToolResultTruncateIntercept 的 call_next 不会被调用（被 ApprovalIntercept 短路）

无冲突：如果外层的 ApprovalIntercept 不调用 call_next，内层根本不会执行。符合洋葱模型预期。

#### 场景 D: CancelledError 传播时的清理顺序

```
InterventionDrainIntercept.around_iteration
  → drain → 发现 CANCEL 指令
  → raise CancelledError("Task cancelled")
```

传播路径：
```
1. around_iteration 抛出 CancelledError
2. 穿透 around_loop 的各个 Intercept
   - TimeoutIntercept → 重新抛出
3. 到达 ReActAgent.run() 的 except CancelledError:
   - hook.on_interrupt(ctx, "cancelled", checkpoint)  ← Hook 通知
   - checkpoint 保存 + restore_to_messages
4. finally:
   - hook.on_turn_end(ctx, result)  ← Hook 最后通知
```

无冲突：Intercept 链完全展开后，Hook 才感知中断。确保 Intercept 的清理逻辑（如果有 try/finally）先执行完。

#### 场景 E: Hook 修改了 tool_calls 影响批处理

```
Hook.on_tool_execution_start(ctx, tool_calls)
  → 某个 hook 从 tool_calls 中移除了一个 (tool_calls.pop())
for tool_call in tool_calls:  ← 使用的是修改后的列表
    Intercept.around_tool_call(...)
```

无冲突：但需要文档明确说明 Hook 可以修改 `tool_calls` 列表（增删改），后续循环使用修改后的版本。

#### 场景 F: 干预指令与审批拦截的交互

```
InterventionDrainIntercept.drain()
  → 发现 MESSAGE 类型指令 → append 到 ctx.history
ApprovalIntercept.around_tool_call()
  → 检查是否需要审批 → 用户回复的审批消息通过 history 传递
```

潜在问题：消息注入到 `ctx.history`，但 LLM 调用的 `messages` 是从 `ctx.to_messages()` 构建的。如果注入发生在 `to_messages()` 之后，注入的消息不会立即被 LLM 看到，需要等到下一次迭代。

这是预期行为，与 nanobot 的消息注入模式一致（注入的消息在下一次 LLM 调用时可见）。

### 8.3 流程终止协议

三类机制都可以触发流程终止。**新增并发看门狗**确保长耗时 LLM/Tool
执行中的 CANCEL 指令也能即时生效。

#### 终止来源分类

```
┌──────────────────────────────────────────────────────────────────────────────┐
│                              终止来源                                        │
│                                                                              │
│ 直接中断 (即时)          通道中断 (延迟 ≤1 迭代)      看门狗中断 (即时, 并发)  │
│ ──────────────          ──────────────────────      ───────────────────────  │
│ TimeoutIntercept        外部 /stop                   CancellationWatchIntercept│
│   → wait_for 超时          → channel.send(CANCEL)      → 后台轮询 channel       │
│   → 直接 raise             → 迭代边界 drain            → 长耗时 LLM/Tool 中     │
│                                                       → 即时 cancel(main_task) │
│ ApprovalIntercept           TokenBudget                                                  │
│   → on_denied=cancel        → channel.send(CANCEL)    看门狗解决的核心问题:        │
│   → 直接 raise              → 下一迭代生效            长耗时 LLM/Tool 调用中       │
│                                                       外部 CANCEL 无法被迭代边界   │
│ RetryIntercept             预设策略                     drain 感知 → 延迟过大      │
│   → 重试耗尽                → channel.send(CANCEL)    → 看门狗并发轮询即时处理    │
│   → 直接 raise                                         → LLM/Tool 调用被 asyncio │
│                                                          Task.cancel() 中断      │
└──────────────────────────────────────────────────────────────────────────────┘

所有路径最终汇聚到统一的 CancelledError handler (checkpoint + restore + hook 通知):
```

#### 并发干预看门狗 (CancellationWatchIntercept)

这是解决"长耗时 LLM/Tool 调用中 CANCEL 无效"的关键 Intercept：

```python
# framework/intercept/builtin/iteration.py 新增

class CancellationWatchIntercept(AgentIntercept):
    """Loop 级并发看门狗 — 后台轮询 channel，长耗时中即时响应 CANCEL。

    问题场景：
      LLM 调用耗时 60s → 用户 /stop 在 t=5s → 迭代边界 drain 在 t=60s → 延迟 55s

    解决：
      main_task 执行 LLM/Tool 调用
      watchdog 并发轮询 channel (每 0.5s)
      CANCEL 到达 → watchdog 即时 cancel(main_task) → CancelledError → 统一 handler
    """

    def __init__(self, poll_interval: float = 0.5):
        self._poll_interval = poll_interval

    async def around_loop(self, ctx, call_next):
        channel = getattr(ctx, 'intervention_channel', None)
        if channel is None:
            return await call_next()

        main_task = asyncio.create_task(call_next())
        watchdog = asyncio.create_task(
            self._watch(channel, ctx.session_id, ctx, main_task)
        )

        try:
            done, pending = await asyncio.wait(
                [main_task, watchdog],
                return_when=asyncio.FIRST_COMPLETED,
            )

            if main_task in done:
                # 正常完成
                return await main_task
            else:
                # 看门狗先完成 → CANCEL/PAUSE 被检测到
                main_task.cancel()
                try:
                    await main_task
                except asyncio.CancelledError:
                    pass

                reason = ctx.metadata.get("_termination_reason", "intervention")
                raise asyncio.CancelledError(reason)

        finally:
            watchdog.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await watchdog

    async def _watch(self, channel, session_id, ctx, main_task):
        """后台轮询 channel，检测到 CANCEL/PAUSE 时取消 main_task。"""
        while not main_task.done():
            for iv in await channel.drain(session_id, limit=1):
                if iv.type == InterventionType.CANCEL:
                    ctx.metadata["_termination_reason"] = (
                        TerminationReason.CANCELLED_BY_USER
                        if iv.priority >= 50
                        else TerminationReason.POLICY_VIOLATION
                    )
                    main_task.cancel()
                    return
                elif iv.type == InterventionType.PAUSE:
                    # 暂停：设置暂停事件，由调用方等待恢复
                    ctx.metadata["_paused"] = True
                    return
            await asyncio.sleep(self._poll_interval)
```

**使用方式** — 添加到 InterceptChain 最外层：

```python
chain = InterceptChain([
    CancellationWatchIntercept(poll_interval=0.5),  # ← 最外层, 并发监控
    TimeoutIntercept(timeout_seconds=600),
    InterventionDrainIntercept(channel=channel),
    ApprovalIntercept(handlers=[...]),
    RetryIntercept(max_attempts=3),
])
```

`CancellationWatchIntercept` 放在最外层：即使 LLM 调用中 RetryIntercept 在重试，
看门狗仍然能即时检测到 CANCEL 并取消整个 loop。

### 8.4 干预类型扩展（支持异步场景）

```python
# framework/intervention/types.py

class InterventionType(str, Enum):
    MESSAGE = "message"          # 注入消息到当前 turn
    CANCEL = "cancel"            # 取消当前任务
    PAUSE = "pause"              # 暂停
    RESUME = "resume"            # 恢复
    MODIFY_SYSTEM = "modify_system"
    PROGRESS = "progress"        # ★ 异步工具执行进度上报
    TOOL_RESULT = "tool_result"  # ★ 异步工具结果回传


class TerminationReason(str, Enum):
    CANCELLED_BY_USER = "cancelled_by_user"
    TIMEOUT = "timeout"
    APPROVAL_DENIED = "approval_denied"
    TOKEN_BUDGET = "token_budget"
    POLICY_VIOLATION = "policy_violation"
    MAX_ITERATIONS = "max_iterations"
    EXTERNAL_COMMAND = "external_command"
    RETRY_EXHAUSTED = "retry_exhausted"
    TASK_PAUSED = "task_paused"          # ★ 暂停后恢复
```

### 8.5 同步转异步工具执行

核心思路：将原本阻塞 agent 循环的工具调用转换为后台异步执行，
通过 InterventionChannel 回传进度和结果，agent 在 drain 时注入对话历史。

与之对比：opencode 等主流 coding agent 对耗时 shell 命令有相同的异步执行→回传设计。

#### 整体流程

```
Iteration N:
  LLM: 决定调用 exec("npm install")  (预计耗时 60s)
  
  AsyncToolIntercept.around_tool_call:
    → 检测到工具适合异步执行
    → 后台 asyncio.create_task() 启动真实工具调用
    → 立即返回占位 ToolResult("[Async started: task_abc]")
  
  Agent: 构建 tool_message(call_id="call_1") 含占位内容 → 追加到 history
  Agent: 继续循环（不阻塞 60s）

后台:
  npm install 执行中
  → 每 10s: channel.send(PROGRESS, {call_id: "call_1", progress: "50%"})

Iteration N+1:
  LLM: 可以推理其他事情（不等待 npm install）
  InterventionDrainIntercept.drain():
    → PROGRESS → emit 到 InterventionBus（用户可见）
    → TOOL_RESULT (如已完成) → 更新 history 中 call_1 的 tool 消息

Iteration N+2:
  npm install 完成
  channel.drain() → TOOL_RESULT {call_id: "call_1", result: "..."}
  → InterventionDrainIntercept 更新 history 中对应的 tool 消息内容
  → LLM 下次 to_messages() 时看到完整结果
```

#### AsyncToolIntercept — 同步转异步的拦截器

```python
# framework/intercept/builtin/tool.py 新增

class AsyncToolIntercept(AgentIntercept):
    """将匹配的同步工具调用转为后台异步执行。

    不阻塞 agent 循环：工具在后台运行，通过 InterventionChannel
    回传进度和结果，agent 在迭代边界的 drain 中注入对话历史。

    构造参数：
        async_tools: set[str]    需要异步化的工具名集合
        channel: InterventionChannel | None
        progress_interval: float  进度回传间隔（秒），0 = 不回传进度
        timeout: float | None    异步任务超时（秒），None = 不超时
    """

    def __init__(self, async_tools: set[str] | None = None,
                 channel: "InterventionChannel | None" = None,
                 progress_interval: float = 0.0,
                 timeout: float | None = None):
        self._async_tools = async_tools or set()
        self._channel = channel
        self._progress_interval = progress_interval
        self._timeout = timeout

    async def around_tool_call(self, ctx, call_next, tool_ctx):
        if tool_ctx.tool_name not in self._async_tools:
            return await call_next(tool_ctx)

        channel = self._channel or getattr(ctx, 'intervention_channel', None)
        if channel is None:
            return await call_next(tool_ctx)  # 无 channel 则同步执行

        # 后台启动异步任务
        asyncio.create_task(
            self._run_async(call_next, tool_ctx, ctx, channel)
        )

        # 立即返回占位结果 → agent 继续循环
        return ToolResult(
            tool_name=tool_ctx.tool_name,
            result=f"[Async execution started for {tool_ctx.tool_name}]",
        )

    async def _run_async(self, call_next, tool_ctx, ctx, channel):
        """后台执行工具并回传进度/结果。"""
        start = time.monotonic()
        task = asyncio.create_task(call_next(tool_ctx))

        # 进度回传
        if self._progress_interval > 0:
            while not task.done():
                elapsed = time.monotonic() - start
                await channel.send(ctx.session_id, Intervention(
                    type=InterventionType.PROGRESS,
                    session_id=ctx.session_id,
                    payload={
                        "call_id": tool_ctx.call_id,
                        "tool_name": tool_ctx.tool_name,
                        "elapsed_seconds": round(elapsed, 1),
                    },
                ))
                try:
                    await asyncio.wait_for(
                        asyncio.shield(task), timeout=self._progress_interval
                    )
                except asyncio.TimeoutError:
                    continue
                except asyncio.CancelledError:
                    break

        try:
            if self._timeout:
                result = await asyncio.wait_for(task, timeout=self._timeout)
            else:
                result = await task
            error = result.error
            output = str(result.result) if result.result else ""
        except asyncio.TimeoutError:
            error = f"Async task timed out after {self._timeout}s"
            output = ""
        except asyncio.CancelledError:
            error = "Task cancelled"
            output = ""
        except Exception as e:
            error = str(e)
            output = ""

        # 回传最终结果
        await channel.send(ctx.session_id, Intervention(
            type=InterventionType.TOOL_RESULT,
            session_id=ctx.session_id,
            payload={
                "call_id": tool_ctx.call_id,
                "tool_name": tool_ctx.tool_name,
                "result": output,
                "error": error,
                "elapsed_seconds": round(time.monotonic() - start, 1),
            },
        ))
```

#### InterventionDrainIntercept 处理 TOOL_RESULT

```python
# framework/intercept/builtin/iteration.py 增强

class InterventionDrainIntercept(AgentIntercept):

    # ... preserve existing __init__ and CANCEL handling ...

    async def around_iteration(self, ctx, call_next, iter_ctx):
        if self._preset_manager:
            await self._preset_manager.check_and_fire(ctx, self._channel)

        for iv in await self._channel.drain(ctx.session_id, limit=self._max):
            if not await self._check_hooks(ctx, iv):
                continue

            # ── 取消/暂停 ──
            if iv.type == InterventionType.CANCEL:
                raise asyncio.CancelledError(...)
            if iv.type == InterventionType.PAUSE:
                ctx.metadata["_paused"] = True
                return None

            # ── 消息注入 ──
            if iv.type == InterventionType.MESSAGE and self._drain_msg:
                await ctx.history.append({
                    "role": "user",
                    "content": iv.payload.get("content", ""),
                })

            # ── ★ 异步工具结果注入 ──
            if iv.type == InterventionType.TOOL_RESULT:
                self._inject_tool_result(ctx, iv.payload)

            # ── ★ 进度上报 ──
            if iv.type == InterventionType.PROGRESS:
                self._report_progress(ctx, iv.payload)

        return await call_next()

    def _inject_tool_result(self, ctx, payload: dict):
        """将异步工具结果注入历史。

        策略：通过 call_id 查找已有的占位 tool 消息并替换内容。
        如果未找到占位消息（工具还未被 LLM 调用），则追加新消息。
        """
        call_id = payload.get("call_id")
        tool_name = payload.get("tool_name", "unknown")
        content = payload.get("error") or payload.get("result") or " "

        # 在 history 中查找 call_id 匹配的占位消息
        history = ctx.history
        for i, msg in enumerate(history.messages):
            if (msg.get("role") == "tool"
                    and msg.get("tool_call_id") == call_id
                    and msg.get("name") == tool_name):
                # 替换占位内容为真实结果
                history.messages[i]["content"] = content
                return

        # 未找到占位 → 追加
        history.append({
            "role": "tool",
            "tool_call_id": call_id,
            "name": tool_name,
            "content": content,
        })

    def _report_progress(self, ctx, payload: dict):
        """将进度通过 InterventionBus 上报给用户。"""
        bus = getattr(ctx, 'intervention_bus', None)
        if bus:
            asyncio.ensure_future(bus.emit(InterventionEvent(
                type=InterventionEventType.TOOL_PROGRESS,
                session_id=ctx.session_id,
                payload=payload,
            )))
```

#### 在 BotService 中使用

```python
# 在 InterceptChain 中组合 AsyncToolIntercept
chain = InterceptChain([
    CancellationWatchIntercept(poll_interval=0.5),
    AsyncToolIntercept(
        async_tools={"exec", "shell", "npm_install", "pip_install"},
        channel=channel,
        progress_interval=3.0,  # 每 3 秒回传进度
        timeout=300.0,           # 5 分钟超时
    ),
    ApprovalIntercept(...),
    InterventionDrainIntercept(channel=channel),
    TimeoutIntercept(timeout_seconds=600),
])
```

`AsyncToolIntercept` 在 `ApprovalIntercept` **之前**（外层）：
- 审批检查通过后，才启动异步执行
- 异步执行本身不需要再次审批

#### 关键约束

| 约束 | 说明 |
|------|------|
| **必须有 InterventionChannel** | 无 channel 时 AsyncToolIntercept 回退为同步执行 |
| **call_id 必须正确匹配** | TOOL_RESULT.payload.call_id 必须与原始 tool_call.call_id 一致 |
| **占位消息保证合法** | 占位 ToolResult 有 content → LLM API 不报错 |
| **历史支持 mutation** | ctx.history 需支持通过下标修改已有消息（MessageHistory 已支持） |
| **看门狗对异步工具无效** | CancellationWatchIntercept 无法取消后台 asyncio.create_task — 但可在 TOOL_RESULT 处理时跳过已取消 task 的结果 |

此设计使 Intervention 体系成为 agent 内部组件间通信的总线：
外部指令（CANCEL）、预置策略（TokenBudget）、异步结果（TOOL_RESULT）、
进度上报（PROGRESS）全部走同一 channel，统一 drain 处理。

### 8.6 预置干预 (Preset Intervention)

"预置干预"让 Intercept 可以把中断决策委托给 Intervention 体系，实现与外部干预的统一路径。

#### 核心思路

```
Intercept 检测到条件 → 不直接 raise → 发送 CANCEL 到 Channel
  → 当前迭代正常完成
  → 下一迭代 InterventionDrainIntercept 泄洪 → raise CancelledError
  → 与外部 /stop 完全相同的处理路径
```

#### PresetPresetManager

```python
# framework/intervention/presets.py

@dataclass
class PresetIntervention:
    """预置干预规则：当 condition 满足时自动触发 intervention。"""
    name: str
    condition: Callable[["AgentContext"], Awaitable[bool]]
    intervention: Intervention
    cooldown_seconds: float = 0.0  # 冷却时间，避免频繁触发
    _last_fired: float = 0.0


class PresetInterventionManager:
    """预置干预管理器。

    在各拦截点被调用，检查预置条件，满足时发送干预指令到 channel。

    用法：
        manager = PresetInterventionManager()
        manager.add(PresetIntervention(
            name="token_budget",
            condition=lambda ctx: estimate_tokens(ctx) > threshold,
            intervention=Intervention(
                type=InterventionType.CANCEL,
                session_id="...",
                payload={"reason": "Token budget exceeded"},
            ),
        ))
    """

    def __init__(self):
        self._presets: list[PresetIntervention] = []

    def add(self, preset: PresetIntervention) -> None:
        self._presets.append(preset)

    def remove(self, name: str) -> None:
        self._presets = [p for p in self._presets if p.name != name]

    async def check_and_fire(
        self, ctx: "AgentContext", channel: "InterventionChannel"
    ) -> int:
        """检查所有预置规则，满足条件则发送干预指令。返回触发的指令数。"""
        now = time.monotonic()
        fired = 0
        for preset in self._presets:
            # 冷却检查
            if preset.cooldown_seconds > 0 and now - preset._last_fired < preset.cooldown_seconds:
                continue
            try:
                if await preset.condition(ctx):
                    iv = preset.intervention
                    iv.session_id = iv.session_id or ctx.session_id
                    iv.timestamp = now
                    await channel.send(ctx.session_id, iv)
                    preset._last_fired = now
                    fired += 1
            except Exception:
                logger.exception("PresetIntervention '%s' condition check failed", preset.name)
        return fired
```

#### InterventionDrainIntercept 集成

```python
# framework/intercept/builtin/iteration.py 增强

class InterventionDrainIntercept(AgentIntercept):
    def __init__(self, channel, preset_manager=None, max_per_drain=3,
                 drain_on_message=True, extra_drain_points=("iteration",)):
        self._channel = channel
        self._preset_manager = preset_manager
        self._max = max_per_drain
        self._drain_msg = drain_on_message
        self._extra_drain_points = extra_drain_points  # 额外泄洪点

    async def around_iteration(self, ctx, call_next, iter_ctx):
        # 1. 检查预置策略 → 满足条件的发送到 channel
        if self._preset_manager:
            await self._preset_manager.check_and_fire(ctx, self._channel)

        # 2. 泄洪 channel（预置 + 外部指令统一处理）
        for iv in await self._channel.drain(ctx.session_id, limit=self._max):
            if not await self._check_hooks(ctx, iv):
                continue
            if iv.type == InterventionType.CANCEL:
                ctx.metadata["_termination_reason"] = (
                    TerminationReason.CANCELLED_BY_USER
                    if iv.priority >= 50 else
                    TerminationReason.POLICY_VIOLATION
                )
                raise asyncio.CancelledError(
                    iv.payload.get("reason", "Task cancelled"))
            if iv.type == InterventionType.MESSAGE and self._drain_msg:
                await ctx.history.append({
                    "role": "user",
                    "content": iv.payload.get("content", ""),
                })

        return await call_next()

    # ... _check_hooks 不变 ...
```

### 8.7 边界情况

#### 长耗时执行中 CANCEL 延迟

使用 `CancellationWatchIntercept` 即时处理（见 8.3 并发看门狗）。对于未添加看门狗的 Agent，可在工具间增加 drain 检查点作为轻量替代：

```python
for tool_call in tool_calls:
    if ctx.intervention_channel:
        for iv in await ctx.intervention_channel.drain(ctx.session_id, limit=1):
            if iv.type == InterventionType.CANCEL:
                raise asyncio.CancelledError("Cancelled during tool execution")
    result = await chain.around_tool_call(ctx, _do_tool, tool_ctx)
```

#### 直接中断与通道中断同时触发

```
TimeoutIntercept → 直接 raise CancelledError (TIMEOUT)
同时 channel 中存在外部 CANCEL 指令（未被 drain）
→ agent 已终止，外部 CANCEL 残留
→ 下一 turn 启动时 drain → 又是新一轮
```

**缓解**：CANCEL 指令附加 `timestamp`，drain 时过滤超过 TTL 的过期指令。

#### 审批拒绝=cancel 的批量工具

```
Hook: on_tool_execution_start(ctx, [tool_a, tool_b, tool_c])
tool_a: ApprovalIntercept → on_denied="cancel" → CancelledError
# tool_b, tool_c 未执行
# checkpoint 保存 pending_tool_calls = [tool_b_id, tool_c_id]
# restore_to_messages 为它们注入 Error 消息
```

兜底保证：所有未执行的 tool_call 在恢复时都有对应的错误 tool result。

#### 预置与外部指令优先级

```
TokenBudget → channel.send(CANCEL, priority=50)
外部 /stop → channel.send(CANCEL, priority=100)
drain → 按 priority 降序 → CANCEL(100) 先处理 → raise
CANCEL(50) 残留 → 下一 turn 被 TTL 过滤丢弃
```

### 8.8 交互矩阵

|  | Hook | Intercept | Intervention |
|--|------|-----------|--------------|
| **Hook** | 多 Hook 串行，各自隔离 | Hook 在最外层，横跨 Intercept | Hook.on_intervention() 过滤指令 |
| **Intercept** | Intercept 在内层，Hook 后执行 | 洋葱模型，外层可短路内层 | InterventionDrainIntercept 桥接两者 |
| **Intervention** | 指令经 Hook 过滤后生效 | 指令由 Intercept 泄洪执行 | Channel ↔ Bus 双向通道 |


## 九、整体迁移计划

```
Phase 1：新建三个 package（旧代码不动）
  - framework/hook/     ← abc, runner, composite, protocols, builtin/*
  - framework/intercept/ ← abc, chain, builtin/*
  - framework/intervention/ ← abc, types, channel, bus, checkpoint, commands

Phase 2：迁移 Hook 实现
  - core/hooks.py         → 内容移到 hooks/builtin/*，文件改为 re-export
  - multi_agent/hooks.py  → 内容移到 hooks/builtin/*，文件改为 re-export
  - multi_agent/inbox/hook.py → 内容移到 hooks/builtin/inbox_flush.py

Phase 3：删除旧 intervention
  - 删除 multi_agent/intervention.py
  - 删除 multi_agent/__init__.py 中的 InterventionAction 等导出
  - subagent_manager.py 中的 TaskSupervisor 引用 → 后续用 Intercept 替换

Phase 4：改造 ReActAgent
  - run() 用 HookRunner + HookPoint 替换 _call_hooks
  - run() 加入 InterceptChain 包裹
  - CancelledError 处理中加入 checkpoint 恢复

Phase 5：适配 examples/bot_project
  - 改 import 路径（RunLoggingHook / InboxFlushHook / PeerAutoSendHook）
  - BotService 中可选的 Intercept / Intervention 装配
  - 验证 bot_service.py --mode pool 正常运行
```

## 十、修订后的迁移计划（以本节为准）

旧版 Phase 中出现的 `framework/hook`、`framework/intercept`、`framework/intervention` 命名不再作为目标实现。迁移按下面版本执行，并且不保留向后兼容入口。

### Phase 1：基础契约

- 新建 `framework/hooks`，迁移现有 hook 实现，保留当前 hook 的观察、上下文修改、runtime context、inbox flush、peer auto send 等功能。
- 引入 `HookPoint`，业务代码不再手写 `"before_turn"` 等字符串；`HookRunner` 内部可继续使用 `getattr` 分发。
- 定义 `AgentControlError`、`AgentCancelled`、`AgentTimeout`、`ApprovalDenied`、`PolicyViolation`、`TerminationReason`。
- `HookRunner` 对控制异常直接透传；普通 hook 异常按配置处理。
- 删除旧 hook 错误入口，不通过 re-export 保留 `framework.hook` 或旧散落调用方式。

### Phase 2：Interceptor 调用边界

- 新建 `framework/interceptors`。
- 先支持 `around_tool_call` 和 `around_turn`/`around_loop`。
- 实现 `InterceptorChain`，控制异常必须透传。
- 实现 `ToolTimeoutInterceptor` 和 `TurnTimeoutInterceptor` 的基础版本。
- 避免和 provider retry、runtime safety policy 重复处理同一超时 owner。
- 删除旧 `framework/intercept` 草案命名，不提供兼容包。

### Phase 3：ToolApprovalInterceptor

- 审批请求发出前脱敏 tool args。
- 审批请求携带 `agent_id`、`session_id`、`turn_id`、`tool_call_id`、`correlation_id`。
- 默认拒绝模式为 `deny_as_tool_error`：写入合法伪错误 `ToolResult` 到 session/history，让模型继续运行。
- 支持强拒绝模式 `deny_as_cancel`：保存 checkpoint 或 termination metadata 后退出当前 run。
- 批量 tool calls 必须保持消息链一致：每个已声明的 tool_call 都要有结果、checkpoint 恢复策略或稳定回滚点。

### Phase 4：Control 平面

- 新建 `framework/control`。
- 定义 `ControlCommand`、`ControlChannel`、`ControlEventBus`、`ControlDrain`、`CheckpointStore` 的最小接口。
- 外部输入和预配置策略都转换成 `ControlCommand`。
- 第一阶段只支持 cancel、approval response、approval timeout、preset budget cancel。
- pause/resume 后续再做，不进入第一阶段。
- 删除旧 `multi_agent/intervention.py` 及旧 `Intervention*` 对外入口；能力按新职责迁移，不做兼容别名。

### Phase 5：ReActAgent 与 bot_project 适配

- `ReActAgent` 使用 `HookRunner` 替换散落 `_call_hooks("...")` 字符串。
- tool 调用通过 `InterceptorChain.around_tool_call` 包裹。
- turn/loop 通过 interceptor 支持总超时和 control drain。
- `examples/bot_project` 装配新 hook/interceptor/control 配置。
- 重点验证 tool 审批拒绝后 session/history 中存在对应 tool result，不出现 provider 协议错误。
