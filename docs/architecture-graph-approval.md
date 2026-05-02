# Graph Architecture, Hooks, Interceptors & Approval System

## Current Implementation Status

This document should be read with the current runtime summary in
`docs/current-runtime.md`. The active ReAct runtime is graph-based and uses
`StartNode`, `LLMNode`, `ToolNode`, and `EndNode`. `clean` mode is intended to
strip hooks, approval, interceptors/control, suspend/resume, runtime state store,
and injection queues at turn entry, with one log line. `full` mode wires those
services explicitly.

The bot project default interceptor chain currently includes
`ControlDrainInterceptor` and `ToolResultLimitInterceptor` only. `TurnTimeoutInterceptor`
and `ToolTimeoutInterceptor` are not default wiring. Runtime persistence should
prefer `RuntimeStateStore` / `JsonFileRuntimeStateStore` naming; the
`CheckpointStore` names remain compatible for generic control checkpoints.

本文档描述 ModexAgent 从 monolithic `run()` 重构为 LangGraph 风格的图架构后，Hook / Interceptor / Control / Approval 四大横切关注点的设计与协作方式。

---

## 目录

1. [架构总览](#1-架构总览)
2. [图抽象层 (Graph Framework)](#2-图抽象层-graph-framework)
3. [ReAct Agent 图实现](#3-react-agent-图实现)
4. [Hook 系统](#4-hook-系统)
5. [Interceptor 系统](#5-interceptor-系统)
6. [Control 系统](#6-control-系统)
7. [Approval 审批系统](#7-approval-审批系统)
8. [Pipeline 集成](#8-pipeline-集成)
9. [中断/恢复机制](#9-中断恢复机制)
10. [bot_project 集成](#10-bot_project-集成)
11. [关键设计决策](#11-关键设计决策)

---

## 1. 架构总览

```
┌─────────────────────────────────────────────────────────┐
│                      Pipeline                            │
│  · 加载上下文、构建 AgentContext                          │
│  · 管理 ApprovalStore / ResumeStore                      │
│  · 捕获 GraphInterrupt → 发送审批提示                     │
│  · 接收审批命令 → 注入 _current_resume → 重新执行 agent    │
└────────────────────┬────────────────────────────────────┘
                     │ agent.run(ctx, emitter)
┌────────────────────▼────────────────────────────────────┐
│                    ReActAgent                            │
│  · 薄壳: 组装 ReActGraph → GraphEngine                   │
│  · run(): before/after turn hooks, 异常处理, 清理        │
│  · 提供 _execute_tool / _stream_with_control 给节点使用   │
└────────────────────┬────────────────────────────────────┘
                     │ engine.run(ctx)
┌────────────────────▼────────────────────────────────────┐
│                   GraphEngine                            │
│  · 纯粹的图执行循环: execute node → follow edge           │
│  · 对 ReAct / Hook / Interceptor 完全无感知               │
│  · GraphInterrupt 直接 re-raise 给调用方                  │
└────────────────────┬────────────────────────────────────┘
                     │
     ┌───────────────┼───────────────┐
     ▼               ▼               ▼
┌─────────┐   ┌──────────┐   ┌──────────┐
│StartNode│   │ LLMNode   │   │ ToolNode  │   ┌────────┐
│         │   │ · hooks   │   │ · hooks   │   │EndNode │
│ 路由判断│   │ · intercp │   │ · intercp │   │        │
└─────────┘   │ · stream  │   │ · approve │   └────────┘
              └──────────┘   └──────────┘
```

**核心原则**：
- **Pipeline** 负责编排（I/O、持久化、审批生命周期）
- **GraphEngine** 负责节点调度（纯循环，不关心业务）
- **Node** 负责业务逻辑（LLM 调用、工具执行、审批分类）
- **Hook / Interceptor / Control** 以 AOP 方式注入到节点执行过程中

---

## 2. 图抽象层 (Graph Framework)

位置: `framework/core/graph/`

### 2.1 核心类型

```python
# node.py
@dataclass(frozen=True)
class NodeTransition:
    target: str    # 下一节点名
    reason: str    # 路由原因（用于边匹配）

class Node(ABC):
    name: str
    @abstractmethod
    async def execute(self, ctx: AgentContext) -> NodeTransition: ...

# graph.py
@dataclass(frozen=True)
class Edge:
    source: str
    target: str
    reason: str | None = None   # None = 无条件回退边

class Graph:
    entry_node = "start"
    def add_node(node) -> None
    def add_edge(source, target, reason=None) -> None
    def next_node(source, reason) -> str  # 精确匹配 > 回退匹配

# engine.py
class GraphEngine:
    async def run(ctx) -> Any:
        current = graph.entry_node
        while current != GraphNode.END:
            node = graph._nodes[current]
            transition = await node.execute(ctx)
            if transition.target == GraphNode.END:
                break
            current = graph.next_node(current, transition.reason)
        return build_result(ctx)
```

### 2.2 路由规则

边的匹配是**基于字符串的两遍匹配**：
1. 第一遍：精确 `reason` 匹配
2. 第二遍：`reason=None` 的无条件回退边
3. 都不匹配 → 抛出 `KeyError`

### 2.3 中断原语

```python
# interrupt.py
class GraphInterrupt(Exception):
    value: Any          # 载荷（如审批请求列表）
    node_name: str      # 触发中断的节点
    iteration: int      # 中断时的迭代计数

_current_resume: ContextVar[Any] = ContextVar("_gr_resume")

def interrupt(value: Any) -> Any:
    resume = _current_resume.get(None)
    if resume is not None:
        return resume      # 恢复：返回注入的值
    raise GraphInterrupt(value=value)  # 首次：抛出异常
```

**两次调用语义**：
- **首次调用**（`_current_resume` 为 None）→ 抛出 `GraphInterrupt`，栈展开到 Pipeline
- **二次调用**（已通过 `_current_resume.set(value)` 注入）→ 返回注入值，继续执行

---

## 3. ReAct Agent 图实现

位置: `framework/agents/react/`

### 3.1 图拓扑

```
        NORMAL_START          RESUME_TOOLS
start ──────────────→ llm    start ────────────→ tool
        (正常启动)              (审批恢复)

llm ──── HAS_TOOLS ───→ tool
llm ──── NO_TOOLS ────→ end
llm ──── MAX_ITERATIONS → end
llm ──── LLM_ERROR ────→ end

tool ─── TOOLS_DONE ──→ llm    (循环)
tool ─── TURN_CANCELLED → end
```

### 3.2 节点职责

| 节点 | 文件 | 职责 |
|------|------|------|
| `StartNode` | `nodes/start.py` | 迭代计数初始化；检测 `RESUME_STATE` 元数据来路由到 TOOL（恢复）或 LLM（正常） |
| `LLMNode` | `nodes/llm.py` | 调用 LLM，流式输出，运行 before/after iteration hooks，通过 InterceptorChain 包装流式调用 |
| `ToolNode` | `nodes/tool.py` | **三阶段**：分类（`_classify_all`）→ 审批（strategy）→ 批量执行（`_execute_batch`） |
| `EndNode` | `nodes/end.py` | 构建 `AgentResult`，发送完成事件，写入 `_graph_result` |

### 3.3 AgentContext — 共享上下文

```python
@dataclass
class AgentContext:
    system_prompt: str
    history: MessageHistory          # 消息历史（唯一的消息写入路径）
    tool_manager: ToolManager
    session_id: str
    max_iterations: int = 10
    extensions: dict[str, Any] = {}  # 扩展服务包
    metadata: dict[str, Any] = {}    # 跨节点临时状态
    emitter: ContentEmitter | None = None
```

- **`extensions`** — Pipeline 注入的服务（HookRunner、InterceptorChain、SuspendStrategy 等），通过 `ExtensionKey` 常量访问
- **`metadata`** — 节点间传递的临时状态（`LLM_RESPONSE`、`ITERATION`、`RESUME_STATE`、`TOOL_DECISIONS` 等），通过 `ReActMetaKey` 常量访问

### 3.4 ReActAgent.run() — 薄壳

```python
async def run(self, context, emitter):
    context.emitter = emitter
    await self._call_hooks(HookPoint.BEFORE_TURN, context)
    try:
        result = await self.engine.run(context)  # 委托给图引擎
    except GraphInterrupt:
        raise  # 传递给 Pipeline
    finally:
        # 清理 metadata、_current_resume 等
        ...
    await self._call_hooks(HookPoint.AFTER_TURN, context)
    return result
```

### 3.5 两种模式

- **`clean`**: `enable_hooks=False`, `enable_approval=False` — 无横切关注点
- **`full`**: 全部启用 — bot_project 使用的模式

---

## 4. Hook 系统

位置: `framework/hook/`

### 4.1 设计

Hook 是**生命周期回调**：在特定时间点被调用，可以观察、veto 或修改内容，但不包装执行流程。

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

class Hook(Protocol):
    async def before_turn(self, ctx, ...) -> HookResult: ...
    async def after_tool_execution(self, ctx, tool_results) -> HookResult: ...
    # 所有方法都是可选的
```

### 4.2 HookRunner

串行分发 hooks，按注册顺序执行。聚合结果：
- `veto=True` → 阻止当前操作
- `content_override` → 最后一个非 None 值生效

### 4.3 内置 Hooks

| Hook | 触发点 | 用途 |
|------|--------|------|
| `RunLoggingHook` | LLM/Tool 响应 | 记录日志 |
| `RuntimeContextHook` | before_turn / tool_exec | 管理 per-turn RuntimeContext |
| `ToolResultTransformHook` | after_tool_exec | 脱敏、截断过长结果 |
| `DynamicToolFilterHook` | before_iteration | Token 预算梯度降级、错误只读模式 |
| `LLMOutputGuardHook` | after_llm_response | 脱敏输出中的敏感信息 |
| `ToolPolicyGuardHook` | before_tool_exec | 工具 deny list 标记 |
| `ProgressReportHook` | 多个钩子点 | 发出进度事件 |
| `InboxFlushHook` | before_turn / iteration | 消费 inbox 中的待处理消息 |
| `PeerAutoSendHook` | after_turn | 自动转发 peer agent 结果 |
| `SubagentMemoryCleanupHook` | after_turn | 清理 subagent 临时记忆目录 |

### 4.4 Hook vs Interceptor

| | Hook | Interceptor |
|---|---|---|
| **模式** | 生命周期回调 | AOP 洋葱中间件 |
| **控制流** | 观察 + 可选 veto | 包装执行，决定是否调用 `next()` |
| **调用方式** | `HookRunner.dispatch(point, ctx)` | `chain.around_tool_call(ctx, next_call)` |
| **典型用途** | 日志、脱敏、自动转发 | 审批、超时、工具过滤 |

---

## 5. Interceptor 系统

位置: `framework/interceptor/`

### 5.1 设计

Interceptor 是 **AOP 环绕通知**：形成洋葱链，每个 interceptor 决定是继续传递（调用 `next()`）还是短路返回。

```python
class Interceptor(Protocol):
    scopes: frozenset[InterceptorScope]   # 声明关注的 scope

    async def around_tool_call(self, ctx: ToolCallContext, next_call: ToolCallNext) -> ToolResult: ...
    async def around_llm_stream(self, ctx: LLMStreamContext, next_call: LLMStreamNext) -> AsyncIterator: ...
    async def around_turn(self, ctx: TurnContext, next_call: TurnNext) -> AgentResult: ...
```

### 5.2 InterceptorChain

```python
class InterceptorChain:
    interceptors: list[Interceptor]

    async def around_tool_call(self, context, tool_call, tool_executor) -> ToolResult:
        # 构建递归 _next(index) 链
        # 每个 interceptor 调用 next_call() 进入下一层
```

### 5.3 内置 Interceptors

| Interceptor | Scope | 用途 |
|-------------|-------|------|
| `ToolTimeoutInterceptor` | TOOL_CALL | 工具执行超时控制 |
| `TurnTimeoutInterceptor` | TURN | 回合超时控制 |
| `ControlDrainInterceptor` | TOOL_CALL / LLM_STREAM | 在工具/LLM 执行前 drain 控制命令 |
| `ToolResultLimitInterceptor` | TOOL_CALL | 限制工具结果大小 |
| `TieredToolApprovalInterceptor` | TOOL_CALL | **分级审批**（见第 7 节） |

### 5.4 TieredToolApprovalInterceptor — 分级逻辑

```python
def classify_tier(self, tool_call) -> str:
    # 1. Hardline 匹配 → HARDLINE（直接拒绝，不审批）
    if self._hardline and self._hardline.matches(name):
        return ApprovalTier.HARDLINE

    # 2. 路径检查 → 任何工具操作不允许目录 → DANGEROUS
    if self._argument_matcher and not self._argument_matcher.is_allowed(tool_call):
        return ApprovalTier.DANGEROUS

    # 3. 名称匹配 → DANGEROUS
    if self._dangerous and self._dangerous.matches(name):
        return ApprovalTier.DANGEROUS

    # 4. 名称匹配 → SENSITIVE（可 yolo 跳过）
    if self._sensitive and self._sensitive.matches(name):
        return ApprovalTier.SENSITIVE

    return ApprovalTier.NORMAL
```

**关键**：`ArgumentMatcher` 检查**所有**工具的参数中是否包含路径，路径不在允许目录内则升级为 DANGEROUS。这意味着即使 `list_dir` 这种非危险工具，如果操作 `/etc` 也会触发审批。

---

## 6. Control 系统

位置: `framework/control/`

Control 系统提供**带外控制通道**：用户或系统可以在 Agent 运行时发送命令（取消、注入消息、审批响应等）。

### 6.1 核心概念

```
外部输入（用户/系统）
       │
       ▼
┌─────────────────┐     ┌─────────────────┐
│  ControlChannel  │────▶│  Agent Runtime   │
│  (命令输入队列)   │     │  drain() 消费命令 │
└─────────────────┘     └────────┬────────┘
                                 │
                         ┌───────▼───────┐
                         │ ControlEventBus │
                         │ (事件输出总线)   │
                         └─────────────────┘
                                 │
                                 ▼
                          外部订阅者（监控/UI）
```

- **ControlChannel** — 命令输入：`send(command)`, `drain(scope)`, `peek(scope)`
- **ControlEventBus** — 事件输出：`emit(event)`, `subscribe(type, handler)`
- **ControlCommand** — 命令：`CANCEL_RUN`, `CANCEL_TURN`, `APPROVAL_RESPONSE`, `INJECT_USER_MESSAGE` 等
- **ControlEvent** — 事件：`TOOL_APPROVAL_REQUESTED`, `RUN_CANCELLED`, `AGENT_PROGRESS` 等

### 6.2 异常体系

```
AgentControlError (base)
├── AgentCancelled      — 外部取消
├── AgentTimeout        — 超时
├── ApprovalDenied      — 审批拒绝（cancel_turn）
└── PolicyViolation     — 策略违规
```

**规则**：`AgentControlError` 及其子类在 Interceptor 和 Hook 中**始终 re-raise**，确保控制命令可靠终止 agent 循环。

### 6.3 UI 系统

```python
class ControlUserInterface(ABC):
    async def render_message(session_id, content) -> message_id
    async def render_question(session_id, question, options, timeout) -> str | None
    async def update_message(session_id, message_id, content)
```

三种实现：
- **CLIUserInterface** — 终端 `input()` 交互
- **IMUserInterface** — QQ/Discord/Telegram 即时通讯，通过 `OutputAdapter` 发送 + `ControlChannel` 等待响应
- **NoopUserInterface** — 无界面模式，全部丢弃

---

## 7. Approval 审批系统

位置: `framework/approval/`

### 7.1 审批决策

```python
class ApprovalTier(StrEnum):
    NORMAL = "normal"        # 自动允许
    SENSITIVE = "sensitive"  # 可 yolo 跳过
    DANGEROUS = "dangerous"  # 需要审批
    HARDLINE = "hardline"    # 直接拒绝

class ApprovalDecision(StrEnum):
    ALLOWED = "allowed"
    DENIED = "denied"
    PENDING = "pending"
    PREEMPTED = "preempted"  # 因前面的 DENIED 而被级联抢占
```

### 7.2 审批状态

```python
@dataclass
class ApprovalRequest:
    tool_name: str
    tool_call_id: str
    arguments: dict
    tier: str
    iteration: int

class ApprovalState:
    requests: list[ApprovalRequest]
    decisions: dict[str, str]   # tool_call_id → decision

    def apply(self, tool_call_id, decision):
        # DENIED → 级联：所有剩余 PENDING → PREEMPTED
        ...

    def final_decisions(self) -> list[str]:
        # 按请求顺序返回，未决定的默认 PREEMPTED
        ...
```

### 7.3 ApprovalStore — 状态持久化

```python
class ApprovalStateStore(ABC):
    async def save(state: ApprovalState) -> None
    async def load(session_id: str) -> ApprovalState | None
    async def delete(session_id: str) -> None
```

- **InMemoryApprovalStateStore** — 测试用
- **LocalFileApprovalStateStore** — JSON 文件持久化（默认），存储于 `data/approval/`

### 7.4 SuspendStrategy — 审批策略

定义审批的**阻塞/中断行为**：

```python
class SuspendStrategy(ABC):
    async def solicit_approval(requests, ctx, ...) -> list[str]: ...
```

两种实现：

| 策略 | 行为 | 持久化 | 适用场景 |
|------|------|--------|----------|
| `InlineWaitStrategy` | 阻塞轮询 `channel.wait_for_decision()` | 无 | 同步 CLI |
| `SuspendResumeStrategy` | 保存状态 → `interrupt()` → 恢复时返回决策 | 有 | IM Bot（异步） |

**SuspendResumeStrategy 关键逻辑**：

```python
async def solicit_approval(self, requests, ctx, all_tool_calls, llm_content, llm_reasoning):
    resume_val = _current_resume.get(None)
    if resume_val is not None:
        _current_resume.set(None)   # 消费一次后清除
        return resume_val           # 恢复路径：直接返回决策

    # 首次路径：保存状态
    approval_state = ApprovalState(session_id=ctx.session_id, requests=list(requests))
    await self._approval_store.save(approval_state)
    resume_state = TurnResumeState(
        iteration=ctx.metadata[ReActMetaKey.ITERATION],
        tool_calls=all_tool_calls or [],
        tool_decisions=[PENDING] * len(requests),
        all_new_messages=list(ctx.metadata.get(ReActMetaKey.ITERATION_MSGS, [])),
        llm_content=llm_content,
        llm_reasoning=llm_reasoning,
    )
    await self._resume_store.save(ctx.session_id, resume_state)
    return interrupt(requests)  # 首次 → raise GraphInterrupt
```

### 7.5 TurnResumeState — 执行快照

暂停时保存的完整执行上下文，恢复时 `StartNode` 据此重建 `LLMResponse` 并路由到 `ToolNode`：

```python
@dataclass
class TurnResumeState:
    iteration: int
    tool_calls: list[dict]          # OpenAI 格式的 tool_calls
    tool_decisions: list[str]
    all_new_messages: list[dict]
    llm_content: str
    llm_reasoning: str | None
```

---

## 8. Pipeline 集成

位置: `framework/pipeline/pipeline.py`

Pipeline 是**编排层**，负责将所有组件连接起来。

### 8.1 AgentContext 构建

```python
agent_context = AgentContext(
    system_prompt=system_prompt,
    history=history,
    tool_manager=tool_manager,
    session_id=session_id,
    max_iterations=max_iterations,
    extensions={
        ExtensionKey.HOOKS: self.hooks,
        ExtensionKey.HOOK_RUNNER: self.hook_runner,
        ExtensionKey.INTERCEPTOR_CHAIN: self.interceptor_chain,
        ExtensionKey.CHECKPOINT_STORE: self.checkpoint_store,
        ExtensionKey.RUNTIME_CTX_MGR: self.runtime_context_manager,
        ExtensionKey.GOVERNANCE: self.governance,
        ExtensionKey.SAFETY: self.safety,
        ExtensionKey.INJECTION_QUEUE: injection_queue,
        ExtensionKey.SUSPEND_STRATEGY: self._suspend_strategy,  # 条件注入
    },
)
```

### 8.2 审批 Store 懒初始化

```python
def _ensure_approval_stores(self):
    if self._suspend_strategy is not None or self.checkpoint_store is None:
        return
    self._approval_store = LocalFileApprovalStateStore(self._approval_workspace)
    self._resume_store = StateStoreTurnResumeStateStore(self.checkpoint_store)
    self._suspend_strategy = SuspendResumeStrategy(
        self._approval_store, self._resume_store,
    )
```

只初始化一次（`_suspend_strategy is not None` 时跳过），需要 `checkpoint_store` 存在。

### 8.3 审批检测与分发

```python
# 1. 检查是否有待审批状态
approval_state_early = await self._approval_store.load(session_id)

# 2. 解析用户输入是否为审批命令
action = parse_approval_action(input_msg.content)  # 支持 /approve, /deny 等

# 3. 如果是审批命令 → 应用决策
if action is not None and approval_state_early is not None:
    for req in approval_state_early.requests:
        if req.tool_call_id not in approval_state_early.decisions:
            approval_state_early.apply(req.tool_call_id, decision)
            break

# 4. 如果不是审批命令且来自其他 agent → 缓冲（等待审批完成）
elif input_metadata.get("source_agent"):
    self._approval_pending[session_id].append(input_msg)
    return None
```

### 8.4 中断处理 — 两处捕获

**正常路径**：
```python
try:
    result = await self.agent.run(agent_context, emitter)
except GraphInterrupt as interrupt_exc:
    # SuspendResumeStrategy 已保存状态
    # 通过 UI 发送审批提示
    for req in interrupt_exc.value:
        await self._user_interface.render_message(session_id, f"审批: {req.tool_name}")
    return None  # 不保存到记忆
```

**恢复路径**（恢复后可能产生新的工具调用需要审批）：
```python
_current_resume.set(decisions)
try:
    result = await self.agent.run(agent_context, emitter)
except GraphInterrupt as interrupt_exc:
    # 第二轮工具也需要审批
    await self._user_interface.render_message(...)
    return None
finally:
    _current_resume.set(None)
```

### 8.5 恢复后的清理

```python
await self._approval_store.delete(session_id)
await self._resume_store.delete(session_id)
# Drain 缓冲的 agent-to-agent 消息
await self._drain_approval_buffer(session_id)
```

---

## 9. 中断/恢复机制

### 9.1 完整生命周期

```
┌─ 正常执行 ──────────────────────────────────────────────────────┐
│                                                                    │
│  Pipeline                                Graph Engine              │
│  ┌──────────┐                            ┌──────────────────┐     │
│  │agent.run()│ ─────────────────────────▶ │ StartNode        │     │
│  │          │                            │   → LLMNode      │     │
│  │          │                            │   → ToolNode     │     │
│  │          │                            │     classify_all()│     │
│  │          │ ◀── GraphInterrupt ─────── │     strategy     │     │
│  │          │          .solicit_approval()│       interrupt()│     │
│  └──────────┘                            └──────────────────┘     │
│       │                                                            │
│       │ 1. 提取 interrupt_exc.value (审批请求列表)                  │
│       │ 2. 通过 UI 发送审批提示                                    │
│       │ 3. return None                                            │
│                                                                    │
└────────────────────────────────────────────────────────────────────┘

┌─ 用户审批 ────────────────────────────────────────────────────────┐
│                                                                    │
│  用户发送 /approve 或 /deny                                        │
│       │                                                            │
│       ▼                                                            │
│  Pipeline._process_message_locked()                                │
│       │                                                            │
│       │ 1. 加载 ApprovalState                                     │
│       │ 2. parse_approval_action() → ApprovalAction               │
│       │ 3. approval_state.apply(tool_call_id, decision)            │
│       │ 4. 如果 every_tool_decided → 进入恢复                      │
│                                                                    │
└────────────────────────────────────────────────────────────────────┘

┌─ 恢复执行 ────────────────────────────────────────────────────────┐
│                                                                    │
│  Pipeline                                Graph Engine              │
│  ┌──────────┐                            ┌──────────────────┐     │
│  │ 加载      │                            │                  │     │
│  │ ResumeState│                           │                  │     │
│  │          │                            │                  │     │
│  │ 设置      │                            │                  │     │
│  │ _current_ │                            │                  │     │
│  │ resume.set│                            │                  │     │
│  │     (decisions)                        │                  │     │
│  │          │                            │                  │     │
│  │agent.run()│ ─────────────────────────▶ │ StartNode        │     │
│  │          │                            │  检测 RESUME_STATE│     │
│  │          │                            │  → 重建 LLMResp   │     │
│  │          │                            │  → 路由到 ToolNode│     │
│  │          │                            │                  │     │
│  │          │                            │ ToolNode         │     │
│  │          │                            │   interrupt()     │     │
│  │          │                            │   → _current_resume│    │
│  │          │                            │      有值 → 返回  │     │
│  │          │                            │   应用决策        │     │
│  │          │                            │   批量执行工具     │     │
│  │          │ ◀── result ────────────── │   → LLMNode → End │     │
│  └──────────┘                            └──────────────────┘     │
│       │                                                            │
│       │ 1. 清理 _current_resume.set(None)                         │
│       │ 2. 删除 approval_store / resume_store                     │
│       │ 3. Drain 缓冲消息                                          │
│       │ 4. 保存结果到记忆                                          │
│                                                                    │
└────────────────────────────────────────────────────────────────────┘
```

### 9.2 ToolNode 三阶段详解

```
ToolNode.execute(ctx)
│
├─ Phase 1: _classify_all(tool_calls, ctx)
│   └─ 逐个工具调用 _get_tier() → interceptor_chain.interceptors[].classify_tier()
│      NORMAL → ALLOWED, HARDLINE → DENIED, 其他 → PENDING
│
├─ Phase 2: 审批（如有 PENDING）
│   ├─ 构建 ApprovalRequest 列表
│   ├─ strategy.solicit_approval(requests, ctx, all_tc_dicts, llm_content, llm_reasoning)
│   │   ├─ 首次: 保存状态 → interrupt(requests) → raise GraphInterrupt
│   │   └─ 恢复: _current_resume 有值 → 返回决策列表
│   └─ _merge(decisions, resolved) ← 将决策合并回原始列表
│
├─ Guard: 仍有 PENDING？ → TURN_CANCELLED
│
└─ Phase 3: _execute_batch(tool_calls, decisions, ctx)
    ├─ 按顺序执行
    ├─ 遇到 DENIED → 后续全部 PREEMPTED
    ├─ ALLOWED → agent._execute_tool() → 结果追加到 history
    └─ DENY_AS_CANCEL 且被拒绝 → TURN_CANCELLED
       └─ 否则 → TOOLS_DONE（返回 LLMNode 继续）
```

---

## 10. bot_project 集成

位置: `examples/bot_project/`

### 10.1 配置

```yaml
# bot_config.yml
# Approval defaults are assembled by runtime construction.
# Do not duplicate default approval policy in bot_config.yml.
```

### 10.2 初始化流程

```python
# core.py _initialize_bot()

# 1. 构建基础 InterceptorChain（超时、control drain、结果限制）
interceptor_chain = self._build_interceptor_chain()
# 包含: ToolTimeout, TurnTimeout, ControlDrain, ToolResultLimit

# 2. 初始化审批基础设施
self._approval_workspace = project_dir / "data/approval"
self._runtime_state_store = JsonFileRuntimeStateStore(approval_workspace / "checkpoints")
self._im_ui = IMUserInterface(output_adapter, control_channel)

# 3. 创建 ReActAgent（full mode）
agent = ReActAgent(provider, mode="full")

# 4. 构建 main agent 专用 InterceptorChain（含审批）
main_chain = InterceptorChain()
for interceptor in self.interceptor_chain.interceptors:
    main_chain.add(interceptor)  # 复制基础 interceptor
main_chain.add(TieredToolApprovalInterceptor(
    dangerous_matcher=ToolNameMatcher({"shell", "write_file", "edit_file"}),
    argument_matcher=ArgumentMatcher({"."}),  # 允许工作目录内
))
```

### 10.3 主 Agent vs Peer Agent

**主 Agent**：注入含 `TieredToolApprovalInterceptor` 的 `main_chain`
**Peer/Subagent**：使用基础 chain（无审批 interceptor）→ 所有工具直接执行

```python
# Pool 模式下的注入
if agent_id == "main":
    main_instance.pipeline.interceptor_chain = main_chain
    main_instance.pipeline.checkpoint_store = self._checkpoint_store
    main_instance.pipeline._approval_workspace = self._approval_workspace
    main_instance.pipeline._user_interface = self._im_ui
```

### 10.4 完整文件列表

| 组件 | 关键文件 |
|------|----------|
| Graph 框架 | `framework/core/graph/` (5 files) |
| ReAct 实现 | `framework/agents/react/agent.py`, `graph.py`, `constants.py`, `strategy.py`, `state.py`, `nodes/` |
| Hook 系统 | `framework/hook/abc.py`, `runner.py`, `builtin/` |
| Interceptor 系统 | `framework/interceptor/abc.py`, `chain.py`, `builtin/tool_approval.py` |
| Control 系统 | `framework/control/types.py`, `channel.py`, `exceptions.py`, `ui/` |
| Approval 系统 | `framework/approval/constants.py`, `state.py`, `store.py`, `response.py` |
| Pipeline | `framework/pipeline/pipeline.py` |
| AgentContext | `framework/core/agent.py`, `context_extensions.py` |
| bot_project | `examples/bot_project/bot/service/core.py`, `config/bot_config.yml` |
| 测试 | `tests/unit/core/graph/`, `tests/unit/agents/react/`, `tests/unit/approval/`, `examples/bot_project/tests/` |

---

## 11. 关键设计决策

### 11.1 基于字符串的边路由（非条件函数）

边的匹配是精确字符串相等，不使用 lambda/条件函数。这使得图拓扑可声明、可检查、可序列化。

### 11.2 基于异常的暂停（非协程 yield）

使用 `raise GraphInterrupt` + `ContextVar` 注入代替 `yield`/`send`。代价是需要 Pipeline 捕获并重新调用 `agent.run()`，好处是整个调用栈完全展开，状态管理清晰，与 asyncio 生态兼容。

### 11.3 AgentContext.extensions 作为服务包

所有横切服务（Hook、Interceptor、Strategy、Checkpoint 等）统一通过 `extensions` dict 注入，不污染 AgentContext 的类型签名。通过 `ctx_ext(ctx, key)` 安全访问。

### 11.4 ctx.history.append() 作为唯一消息写入路径

消息只在节点执行过程中通过 `ctx.history.append()` 写入。Pipeline 不调用 `ctx_mgr.save()`，避免了双重写入和状态不一致。

### 11.5 checkpoint 与 memory 分离

- `CheckpointStore` — 崩溃恢复（`{session_id}:latest`、`{session_id}:denial`），不写入 memory 文件
- `MessageHistory` — 对话记忆（`messages.jsonl`），由各 Node 在 `execute()` 中写入

### 11.6 工具分类优先级

```
Hardline 名称匹配
  → 路径违规（任何工具操作不允许目录）
    → Dangerous 名称匹配
      → Sensitive 名称匹配
        → NORMAL（自动允许）
```

`ArgumentMatcher` 对**所有工具**生效，不仅限于 dangerous 列表中的工具。例如 `list_dir /etc` 也会触发审批。
