# ReAct Graph — 基于抽象图引擎的 ReAct 执行引擎

> **日期**: 2026-05-02
> **状态**: 设计完成，待审核
> **灵感**: LangGraph `interrupt()` / `Command` / checkpoint-replay 中断-恢复模式

---

## 一、动机

### 1.1 当前问题

1. **agent.py 臃肿** (~775行): 单个 `run()` 包含 LLM 调用、流式处理、tool 循环、Hook/Interceptor 调用、异常恢复，职责不清
2. **扩展耦合**: Hook/Interceptor/Approval 与 ReAct 循环紧耦合，难以独立测试、替换或省略
3. **审批恢复链路过长**: 需要 Pipeline 手动读 memory、手动注入 decisions，走外部恢复路径
4. **无通用图抽象**: 未来若新增 PlanReAct / RewooAgent 等 Agent 模式，需重复实现调度逻辑
5. **AgentContext 被 ReAct 污染**: 当前 20+ 固定字段中混入 `max_tools_per_turn`、`hooks: list[Hook]` 等 ReAct 专属字段，`to_messages()` 硬编码了系统提示词注入逻辑
6. **审批状态无抽象**: 当前无独立的状态持久化层，InlineWait 和 SuspendResume 两套策略代码分散在 interceptor 的不同分支中

### 1.2 设计目标

- **通用图引擎**: `framework/core/graph/` 抽象 Node/Edge/Graph/Engine，与 ReAct 无关
- **AgentContext 瘦身**: 核心通用字段 + `extensions: dict` 扩展机制，移除 ReAct 专属字段
- **LLMNode 负责消息组装**: `AgentContext.to_messages()` 只返回历史消息，系统提示词由 LLMNode 显式放入上下文
- **枚举化/常量化**: 所有节点名、路由原因、元数据 key 使用枚举/常量，禁止裸字符串
- **审批状态持久化抽象**: `ApprovalState` + `ApprovalStateStore` ABC + `LocalFileApprovalStateStore` 默认实现，分布式场景可通过 Redis 实现同一 ABC
- **策略可插拔**: `SuspendStrategy` ABC 统一 InlineWait / SuspendResume，ToolNode 不感知具体策略，切换策略不影响其他代码
- **整批审批、中断-恢复**: 所有 tool 先分类，需审批的一起 `interrupt()`；恢复时从 ToolNode 起点重演
- **Clean / Full 双模式**: Clean 纯节点+边 (学习/测试/DIY)，Full 注入 Hook/Interceptor/Approval
- **向后不兼容**: 直接移除旧实现，不做兼容层

---

## 二、枚举与常量

遵循项目类型安全规则: 所有节点名、路由原因、元数据 key 使用枚举/常量。

### 2.1 核心图枚举 (`framework/core/graph/constants.py`)

```python
from enum import StrEnum

class GraphNode(StrEnum):
    """Engine 识别的特殊节点名。"""
    END = "__end__"

class GraphMetaKey:
    """图引擎在 ctx.metadata 中使用的 key。"""
    GRAPH_RESULT = "_graph_result"
```

### 2.2 ReAct 枚举 (`framework/agents/react/constants.py`)

```python
from enum import StrEnum

class ReActNode(StrEnum):
    START = "start"
    LLM   = "llm"
    TOOL  = "tool"
    END   = "end"

class ReActReason(StrEnum):
    NORMAL_START  = "normal_start"
    RESUME_TOOLS  = "resume_tools"
    HAS_TOOLS      = "has_tools"
    NO_TOOLS       = "no_tools"
    MAX_ITERATIONS = "max_iterations"
    LLM_ERROR      = "llm_error"
    TOOLS_DONE     = "tools_done"
    TURN_CANCELLED = "turn_cancelled"
    DONE           = "done"

class ReActMetaKey:
    ITERATION       = "_react_iteration"
    LLM_RESPONSE    = "_llm_response"
    ITERATION_MSGS  = "_iteration_messages"
    RESUME_STATE    = "_turn_resume_state"
    TOOL_DECISIONS  = "_tool_decisions"
    DENY_AS_CANCEL  = "_deny_as_cancel"
    APPROVAL_DENIAL = "_approval_denial"
    INJECTION_CYCLE = "_injection_cycle_count"
```

### 2.3 AgentContext 扩展 key (`framework/core/context_extensions.py`)

```python
class ExtensionKey:
    HOOK_RUNNER         = "hook_runner"
    HOOKS               = "hooks"
    INTERCEPTOR_CHAIN   = "interceptor_chain"
    CHECKPOINT_STORE    = "checkpoint_store"
    RUNTIME_CTX_MGR     = "runtime_context_manager"
    RUNTIME_CTX         = "runtime_context"
    GOVERNANCE          = "governance"
    SAFETY              = "safety"
    INJECTION_QUEUE     = "injection_queue"
    MAX_TOOLS_PER_TURN  = "max_tools_per_turn"
    ON_CHECKPOINT       = "on_checkpoint"
    SUSPEND_STRATEGY    = "suspend_strategy"
```

### 2.4 审批枚举 (`framework/approval/constants.py`)

```python
from enum import StrEnum

class ApprovalDecision(StrEnum):
    ALLOWED   = "allowed"
    DENIED    = "denied"
    PENDING   = "pending"
    PREEMPTED = "preempted"    # 级联拒绝

class ApprovalTier(StrEnum):
    NORMAL    = "normal"
    DANGEROUS = "dangerous"
    SENSITIVE = "sensitive"
    HARDLINE  = "hardline"

class ApprovalStatus(StrEnum):
    PENDING  = "pending"
    APPROVED = "approved"
    DENIED   = "denied"
    PARTIAL  = "partial"       # 部分已决策
```

---

## 三、AgentContext 重新设计

```python
# framework/core/agent.py

@dataclass
class AgentContext:
    """Agent 执行上下文 — 只含通用字段，不含任何 Agent 类型专属字段。"""

    # ── 核心 (必填) ──
    system_prompt: str
    history: MessageHistory
    tool_manager: ToolManager

    # ── 通用配置 ──
    session_id: str = ""
    max_iterations: int = 10
    temperature: float | None = None
    max_tokens: int | None = None
    attachments: list[str] = field(default_factory=list)

    # ── 自由扩展 (可选运行时服务，由上层注入) ──
    extensions: dict[str, Any] = field(default_factory=dict)

    # ── 节点间通信 (图执行期间读写) ──
    metadata: dict[str, Any] = field(default_factory=dict)

    # ── 图执行期间由 Agent 设置 ──
    emitter: ContentEmitter | None = None

    def add_attachment(self, path: str) -> None:
        self.attachments.append(path)

    async def to_messages(self) -> list[dict[str, Any]]:
        """返回历史消息列表 — 不含系统提示词。
        系统提示词由 LLMNode 显式组装到上下文最前面。
        """
        history_list = await self.history.to_list()
        history_list, has_agent_msgs = normalize_agent_messages_for_llm(history_list)
        non_system = [msg for msg in history_list if msg.get("role") != "system"]
        return _strip_none_values(non_system)

    def get_tool_descriptions(self) -> list[dict[str, Any]]:
        return self.tool_manager.get_tool_descriptions()


def ctx_ext(ctx: AgentContext, key: str, default: Any = None) -> Any:
    """从 extensions 中安全取值。"""
    return ctx.extensions.get(key, default)
```

调用方: `ctx_ext(ctx, ExtensionKey.HOOK_RUNNER)` 替代 `ctx.hook_runner`。

---

## 四、核心图抽象 (`framework/core/graph/`)

### 4.1 Node & NodeTransition

```python
class Node(ABC):
    def __init__(self, name: str) -> None:
        self.name = name

    @abstractmethod
    async def execute(self, ctx: "AgentContext") -> "NodeTransition":
        ...

@dataclass(frozen=True)
class NodeTransition:
    target: str     # 下一节点名, 或 GraphNode.END
    reason: str     # 路由 key, 由 Edge 匹配
```

### 4.2 Edge & Graph

```python
@dataclass(frozen=True)
class Edge:
    source: str
    target: str
    reason: str | None = None   # None = 无条件 fallback

class Graph:
    def __init__(self, name: str = "graph") -> None:
        self.name = name
        self._nodes: dict[str, Node] = {}
        self._edges: dict[str, list[Edge]] = {}
        self.entry_node: str = ReActNode.START

    def add_node(self, node: Node) -> None:
        self._nodes[node.name] = node

    def add_edge(self, source: str, target: str, reason: str | None = None) -> None:
        self._edges.setdefault(source, []).append(Edge(source, target, reason))

    def next_node(self, source: str, reason: str) -> str:
        candidates = self._edges.get(source, [])
        for edge in candidates:
            if edge.reason == reason:
                return edge.target
        for edge in candidates:
            if edge.reason is None:
                return edge.target
        raise KeyError(f"No edge from {source} for reason {reason!r}")
```

### 4.3 GraphEngine

```python
class GraphEngine:
    """驱动节点执行 + 沿边路由。不感知 ReAct / Hook / Interceptor / Approval。

    返回类型不固定 — 由 EndNode 写入 ctx.metadata[GraphMetaKey.GRAPH_RESULT]，
    调用方 (Agent) 自行解释。
    """

    def __init__(self, graph: Graph) -> None:
        self.graph = graph

    async def run(self, ctx: "AgentContext") -> Any:
        """单入口。从 entry_node 开始，直到 GraphNode.END。"""
        current: str = self.graph.entry_node
        while current != GraphNode.END:
            node = self.graph._nodes[current]
            transition = await node.execute(ctx)
            current = self.graph.next_node(current, transition.reason)
        except GraphInterrupt:
            raise   # 冲到上层 (Agent → Pipeline)
        return self.build_result(ctx)

    def build_result(self, ctx: "AgentContext") -> Any:
        """从 ctx 提取最终结果。子类可覆写返回特定类型。"""
        return ctx.metadata.get(GraphMetaKey.GRAPH_RESULT)
```

只有 `run()` 一个入口。`build_result()` 是普通方法 (非 private)，子类可覆写返回强类型。

### 4.4 interrupt() & GraphInterrupt

```python
# framework/core/graph/interrupt.py

class GraphInterrupt(Exception):
    """图执行中断。由 interrupt() 抛出，Engine 捕获后冲到上层。"""
    value: Any
    node_name: str
    iteration: int

_current_resume: contextvars.ContextVar[Any] = contextvars.ContextVar("_gr_resume")

def interrupt(value: Any) -> Any:
    """节点内调用。首调 raise GraphInterrupt，恢复时返回注入值。
    由 SuspendStrategy 调用，不应被 Node 直接调用。
    """
    resume = _current_resume.get(None)
    if resume is not None:
        return resume
    raise GraphInterrupt(value=value, ...)
```

---

## 五、审批状态持久化

### 5.1 数据模型 (`framework/approval/state.py`)

```python
from dataclasses import dataclass, field

@dataclass
class ApprovalRequest:
    """单个工具的审批请求。"""
    tool_name: str
    tool_call_id: str
    arguments: dict[str, Any]
    tier: str               # ApprovalTier 值
    iteration: int

@dataclass
class ApprovalState:
    """一轮 ReAct 中的审批状态。一次可能有多个 tool 需要审批。"""
    session_id: str
    requests: list[ApprovalRequest]
    decisions: dict[str, str] = field(default_factory=dict)   # tool_call_id → ApprovalDecision
    status: str = ApprovalStatus.PENDING

    @property
    def every_tool_decided(self) -> bool:
        return all(
            tc_id in self.decisions and self.decisions[tc_id] != ApprovalDecision.PENDING
            for tc_id in (r.tool_call_id for r in self.requests)
        )

    @property
    def unresolved_count(self) -> int:
        return sum(
            1 for r in self.requests
            if r.tool_call_id not in self.decisions
            or self.decisions[r.tool_call_id] == ApprovalDecision.PENDING
        )

    def apply(self, tool_call_id: str, decision: str) -> None:
        if decision == ApprovalDecision.DENIED:
            # 拒绝 → 级联：同批剩余全部 PREEMPTED
            self.decisions = {
                tc_id: (d if d != ApprovalDecision.PENDING else ApprovalDecision.PREEMPTED)
                for tc_id, d in self.decisions.items()
            }
        self.decisions[tool_call_id] = decision

    def final_decisions(self) -> list[str]:
        """返回最终决策列表，与 requests 顺序一致。"""
        return [
            self.decisions.get(r.tool_call_id, ApprovalDecision.PREEMPTED)
            for r in self.requests
        ]
```

### 5.2 ApprovalStateStore ABC + 默认实现

```python
# framework/approval/store.py

from abc import ABC, abstractmethod

class ApprovalStateStore(ABC):
    """审批状态持久化抽象。分布式场景可通过 Redis 等实现同一 ABC。"""

    @abstractmethod
    async def save(self, state: ApprovalState) -> None:
        """保存审批状态。"""
        ...

    @abstractmethod
    async def load(self, session_id: str) -> ApprovalState | None:
        """加载审批状态, 不存在返回 None。"""
        ...

    @abstractmethod
    async def delete(self, session_id: str) -> None:
        """清除审批状态。"""
        ...


class LocalFileApprovalStateStore(ApprovalStateStore):
    """默认实现: JSON 文件持久化。

    key: {workspace}/{session_id}_approval.json
    """

    def __init__(self, workspace: Path) -> None: ...

    async def save(self, state: ApprovalState) -> None:
        path = self._path(state.session_id)
        data = {"session_id": state.session_id,
                "requests": [dataclasses.asdict(r) for r in state.requests],
                "decisions": state.decisions,
                "status": state.status}
        path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")

    async def load(self, session_id: str) -> ApprovalState | None: ...

    async def delete(self, session_id: str) -> None:
        self._path(session_id).unlink(missing_ok=True)


class InMemoryApprovalStateStore(ApprovalStateStore):
    """测试用 / InlineWait 用。进程重启丢失。"""
    def __init__(self) -> None:
        self._store: dict[str, ApprovalState] = {}
    ...
```

### 5.3 TurnResumeState + Store (`framework/agents/react/state.py`)

```python
@dataclass
class TurnResumeState:
    """SuspendResume 策略中断时的执行快照。恢复时从该状态回到 ToolNode。"""
    iteration: int
    tool_calls: list[dict[str, Any]]
    tool_decisions: list[str]
    all_new_messages: list[dict[str, Any]]


class TurnResumeStateStore(ABC):
    """执行快照持久化抽象。"""

    @abstractmethod
    async def save(self, session_id: str, state: TurnResumeState) -> None: ...
    @abstractmethod
    async def load(self, session_id: str) -> TurnResumeState | None: ...
    @abstractmethod
    async def delete(self, session_id: str) -> None: ...


class InMemoryTurnResumeStateStore(TurnResumeStateStore):
    """进程内 dict，InlineWait / 测试用。"""

class StateStoreTurnResumeStateStore(TurnResumeStateStore):
    """基于现有 StateStore 基础设施，SuspendResume 用。"""
```

---

## 六、中断策略抽象 (`framework/agents/react/strategy.py`)

```python
from abc import ABC, abstractmethod

class SuspendStrategy(ABC):
    """中断策略抽象 — ToolNode 通过该接口请求审批决策。

    InlineWait:  阻塞等待，不抛异常，不持久化
    SuspendResume: 保存状态 → raise → 恢复 → 回决策
    """

    @abstractmethod
    async def solicit_approval(
        self, requests: list[ApprovalRequest], ctx: AgentContext
    ) -> list[str]:
        """请求审批并返回最终决策列表 (与 requests 顺序一致)。

        决策值: ApprovalDecision.ALLOWED / DENIED / PREEMPTED
        """
        ...


class InlineWaitStrategy(SuspendStrategy):
    """阻塞式审批 — 通过控制 channel 逐个等待用户决策。

    特点: 不持久化，不抛 GraphInterrupt，全程在一个 engine.run() 内。
    """

    def __init__(self, channel: "ApprovalChannel") -> None:
        self._channel = channel

    async def solicit_approval(
        self, requests: list[ApprovalRequest], ctx: AgentContext
    ) -> list[str]:
        # 使用内存 ApprovalState 跟踪进度
        state = ApprovalState(session_id=ctx.session_id, requests=requests)

        for req in requests:
            # 1. 通过 emitter 提示用户 (具体渲染由 OutputAdapter 负责)
            await ctx.emitter.emit(ReActEvent.APPROVAL_REQUIRED, req)

            # 2. 阻塞等待用户决策
            decision = await self._channel.wait_for_decision(req.tool_call_id)
            state.apply(req.tool_call_id, decision)

            if decision == ApprovalDecision.DENIED:
                break  # 级联拒绝: 剩余 tool 在 state.final_decisions() 中自动变 PREEMPTED

        return state.final_decisions()


class SuspendResumeStrategy(SuspendStrategy):
    """中断恢复式审批 — 持久化状态后 raise GraphInterrupt。

    恢复时 Pipeline 重新 engine.run()，StartNode 路由回 ToolNode，
    solicit_approval() 被二次调用，interrupt() 返回注入的决策。
    """

    def __init__(self, approval_store: ApprovalStateStore,
                 resume_store: TurnResumeStateStore) -> None:
        self._approval_store = approval_store
        self._resume_store = resume_store

    async def solicit_approval(
        self, requests: list[ApprovalRequest], ctx: AgentContext
    ) -> list[str]:
        # 构建并持久化审批状态
        approval_state = ApprovalState(session_id=ctx.session_id, requests=requests)
        await self._approval_store.save(approval_state)

        # 构建并持久化执行快照
        resume_state = TurnResumeState(
            iteration=ctx.metadata[ReActMetaKey.ITERATION],
            tool_calls=[tc_to_dict(tc) for tc in ...],
            tool_decisions=[ApprovalDecision.PENDING] * len(requests),
            all_new_messages=ctx.metadata.get(ReActMetaKey.ITERATION_MSGS, []),
        )
        await self._resume_store.save(ctx.session_id, resume_state)

        # interrupt() → 首调 raise, Pipeline 恢复后二次调用回决策
        return interrupt(requests)
```

**关键: ToolNode 不感知策略**

ToolNode 通过 `ctx.extensions[ExtensionKey.SUSPEND_STRATEGY]` 获取策略实例，调用 `solicit_approval()`。切换 InlineWait ↔ SuspendResume 只需注入不同策略实例，ToolNode 代码完全不变。

---

## 七、ReAct Node 实现 (`framework/agents/react/nodes/`)

### 7.1 StartNode

```python
class StartNode(Node):
    def __init__(self) -> None:
        super().__init__(ReActNode.START)

    async def execute(self, ctx: AgentContext) -> NodeTransition:
        resume_state = ctx.metadata.get(ReActMetaKey.RESUME_STATE)

        if resume_state is not None:
            ctx.metadata[ReActMetaKey.ITERATION] = resume_state.iteration
            ctx.metadata[ReActMetaKey.TOOL_DECISIONS] = resume_state.tool_decisions
            ctx.metadata[ReActMetaKey.ITERATION_MSGS] = resume_state.all_new_messages
            return NodeTransition(ReActNode.TOOL, ReActReason.RESUME_TOOLS)

        ctx.metadata[ReActMetaKey.ITERATION] = 0
        await ctx.emitter.emit(ReActEvent.START)
        return NodeTransition(ReActNode.LLM, ReActReason.NORMAL_START)
```

### 7.2 LLMNode

```python
class LLMNode(Node):
    """构造上下文 (含系统提示词) → 调用 LLM → 写出 assistant message。"""

    def __init__(self, agent: "ReActAgent", *, enable_hooks: bool = True) -> None:
        super().__init__(ReActNode.LLM)
        self._agent = agent
        self._enable_hooks = enable_hooks

    async def execute(self, ctx: AgentContext) -> NodeTransition:
        iteration = ctx.metadata[ReActMetaKey.ITERATION] + 1
        ctx.metadata[ReActMetaKey.ITERATION] = iteration

        if iteration > ctx.max_iterations:
            await ctx.emitter.emit(ReActEvent.MAX_ITERATIONS)
            return NodeTransition(ReActNode.END, ReActReason.MAX_ITERATIONS)

        await ctx.emitter.emit(ReActEvent.ITERATION_START, {"iteration": iteration})

        if self._enable_hooks:
            await self._agent._call_hooks(HookPoint.BEFORE_ITERATION, ctx)
            await self._agent._drain_injections(ctx)

        messages = self._build_messages(ctx)       # ← LLMNode 显式组装
        response = await self._call_llm(messages, ctx)

        if self._enable_hooks:
            await self._agent._call_hooks(HookPoint.AFTER_LLM_RESPONSE, ctx, response)

        if response.finish_reason == FinishReason.ERROR.value:
            return NodeTransition(ReActNode.END, ReActReason.LLM_ERROR)

        assistant_msg = self._agent._build_assistant_message(response)
        await ctx.history.append(assistant_msg)
        ctx.metadata[ReActMetaKey.LLM_RESPONSE] = response
        ctx.metadata[ReActMetaKey.ITERATION_MSGS] = [assistant_msg]

        if response.tool_calls:
            return NodeTransition(ReActNode.TOOL, ReActReason.HAS_TOOLS)
        return NodeTransition(ReActNode.END, ReActReason.NO_TOOLS)

    def _build_messages(self, ctx: AgentContext) -> list[dict[str, Any]]:
        """显式组装: system prompt + history。系统提示词在整个 ReAct 循环中不变。"""
        messages: list[dict[str, Any]] = []
        if ctx.system_prompt:
            messages.append({"role": "system", "content": ctx.system_prompt})
        messages.extend(await ctx.to_messages())

        governance = ctx_ext(ctx, ExtensionKey.GOVERNANCE)
        if governance is not None:
            messages = await governance.apply(messages)
        return messages
```

### 7.3 ToolNode

```python
class ToolNode(Node):
    """两阶段: 分类 → (中断策略介入 | 批量执行)。

    策略通过 ctx.extensions[ExtensionKey.SUSPEND_STRATEGY] 注入。
    """

    def __init__(self, agent: "ReActAgent", *,
                 enable_approval: bool = True,
                 enable_hooks: bool = True) -> None:
        super().__init__(ReActNode.TOOL)
        self._agent = agent
        self._enable_approval = enable_approval
        self._enable_hooks = enable_hooks

    async def execute(self, ctx: AgentContext) -> NodeTransition:
        response = ctx.metadata.pop(ReActMetaKey.LLM_RESPONSE)
        tool_calls = response.tool_calls
        iteration = ctx.metadata[ReActMetaKey.ITERATION]

        # 阶段 1: 分类所有 tool
        decisions = self._classify_all(tool_calls, ctx)

        # 阶段 2: 如有待审批 tool, 委托策略获取决策
        if self._enable_approval and self._any_pending(decisions):
            requests = build_approval_requests(tool_calls, decisions, iteration)
            strategy = ctx_ext(ctx, ExtensionKey.SUSPEND_STRATEGY)
            resolved = await strategy.solicit_approval(requests, ctx)
            decisions = self._merge(decisions, resolved)

        # 阶段 3: 批量执行
        return await self._execute_batch(tool_calls, decisions, ctx)

    async def _execute_batch(
        self, tool_calls: list[ToolCall], decisions: list[str], ctx: AgentContext
    ) -> NodeTransition:
        denied_encountered = False
        for tc, dec in zip(tool_calls, decisions):
            if denied_encountered:
                dec = ApprovalDecision.PREEMPTED

            await ctx.emitter.emit(ReActEvent.TOOL_CALL_START, tc)

            if dec == ApprovalDecision.ALLOWED:
                if self._enable_hooks:
                    await self._agent._call_hooks(HookPoint.BEFORE_TOOL_EXECUTION, ctx, [tc])
                result = await self._agent._execute_tool(tc, ctx)
                if self._enable_hooks:
                    await self._agent._call_hooks(HookPoint.AFTER_TOOL_EXECUTION, ctx, [result])
            else:
                # 拒绝/忽略 → 伪 tool 结果, 告知模型工具未执行
                result = ToolResult(tool_name=tc.tool_name, result=None,
                                    error=f"Error: {dec}")

            await ctx.emitter.emit(ReActEvent.TOOL_CALL_END, (tc, result))

            tool_msg = self._agent._build_tool_message(result, tc.call_id)
            await ctx.history.append(tool_msg)
            ctx.metadata[ReActMetaKey.ITERATION_MSGS].append(tool_msg)

            if dec in (ApprovalDecision.DENIED, ApprovalDecision.PREEMPTED):
                denied_encountered = True

        if self._enable_hooks:
            await self._agent._drain_injections(ctx)

        if denied_encountered and ctx.metadata.get(ReActMetaKey.DENY_AS_CANCEL):
            await self._agent._save_denial_checkpoint(ctx)
            return NodeTransition(ReActNode.END, ReActReason.TURN_CANCELLED)

        return NodeTransition(ReActNode.LLM, ReActReason.TOOLS_DONE)
```

**ToolNode 与策略的关系**: ToolNode 不 import 任何具体策略类。它通过 `ctx_ext(ctx, ExtensionKey.SUSPEND_STRATEGY)` 获取 `SuspendStrategy` 实例，调用 `solicit_approval()`。策略是 InlineWait 还是 SuspendResume，ToolNode 不关心 — 接口相同。

### 7.4 EndNode

```python
class EndNode(Node):
    def __init__(self, agent: "ReActAgent") -> None:
        super().__init__(ReActNode.END)
        self._agent = agent

    async def execute(self, ctx: AgentContext) -> NodeTransition:
        response = ctx.metadata.pop(ReActMetaKey.LLM_RESPONSE, None)
        messages = ctx.metadata.pop(ReActMetaKey.ITERATION_MSGS, [])

        if response and not response.tool_calls:
            result = AgentResult(content=response.content or "",
                                 reasoning=response.reasoning_content,
                                 messages=messages, attachments=ctx.attachments)
            await ctx.emitter.emit(ReActEvent.FINAL_OUTPUT, result)
        else:
            result = AgentResult(content="达到最大迭代次数",
                                 stop_reason="max_iterations",
                                 messages=messages, attachments=ctx.attachments)

        await self._agent._clear_checkpoint(ctx)
        await ctx.emitter.emit_complete(result)
        ctx.metadata[GraphMetaKey.GRAPH_RESULT] = result
        return NodeTransition(GraphNode.END, ReActReason.DONE)
```

---

## 八、ReActGraph — 两套图

```python
class ReActGraph(Graph):

    def __init__(self, agent: "ReActAgent", *,
                 mode: Literal["clean", "full"] = "full") -> None:
        super().__init__(name=f"react_{mode}")
        enable = mode == "full"

        self.add_node(StartNode())
        self.add_node(LLMNode(agent, enable_hooks=enable))
        self.add_node(ToolNode(agent, enable_approval=enable, enable_hooks=enable))
        self.add_node(EndNode(agent))

        self.add_edge(ReActNode.START, ReActNode.LLM,  reason=ReActReason.NORMAL_START)
        self.add_edge(ReActNode.START, ReActNode.TOOL, reason=ReActReason.RESUME_TOOLS)
        self.add_edge(ReActNode.LLM, ReActNode.TOOL, reason=ReActReason.HAS_TOOLS)
        self.add_edge(ReActNode.LLM, ReActNode.END,  reason=ReActReason.NO_TOOLS)
        self.add_edge(ReActNode.LLM, ReActNode.END,  reason=ReActReason.MAX_ITERATIONS)
        self.add_edge(ReActNode.LLM, ReActNode.END,  reason=ReActReason.LLM_ERROR)
        self.add_edge(ReActNode.TOOL, ReActNode.LLM, reason=ReActReason.TOOLS_DONE)
        self.add_edge(ReActNode.TOOL, ReActNode.END, reason=ReActReason.TURN_CANCELLED)
```

| | Clean | Full |
|---|---|---|
| **Hook** | 跳过 `_call_hooks` | agent 注入 HookRunner |
| **Interceptor** | LLM 纯流式/非流式，tool `execute_tool_raw` | LLM `_stream_with_control`，tool `interceptor.around_tool_call` |
| **Approval** | 跳过 classify / 策略 | 完整两阶段 + SuspendStrategy |
| **Emitter** | 仅核心事件 | 全部事件 |
| **用途** | 单元测试 / DIY | bot_project |

---

## 九、Pipeline 集成

### 9.1 策略依赖注入

Pipeline 在构造 `AgentContext` 时注入策略:

```python
# Pipeline._process_message_locked

# SuspendResume 策略
strategy = SuspendResumeStrategy(
    approval_store=self._approval_store,
    resume_store=self._resume_store,
)
ctx.extensions[ExtensionKey.SUSPEND_STRATEGY] = strategy
```

切换策略只需替换注入的实例。

### 9.2 审批命令隔离

```
[1] 内容清洗、附件处理
[2] 命令拦截 (command_interceptor)
[3] 审批消费 (NEW — 仅 SuspendResume 策略触发)
    state = approval_store.load(session_id)
    if state is not None and is_approval_command(input_msg):
        state.apply(input_msg.tool_call_id, input_msg.decision)   # 更新决策
        if state.every_tool_decided:
            # 注入恢复数据
            ctx.metadata[ReActMetaKey.RESUME_STATE] = resume_store.load(session_id)
            ctx.metadata[ReActMetaKey.TOOL_DECISIONS] = state.final_decisions()
            _current_resume.set(state.final_decisions())
            await engine.run(ctx)                                    # start → tool
            _current_resume.reset()
            approval_store.delete(session_id)
            resume_store.delete(session_id)
            await _drain_approval_buffer(session_id)
        else:
            approval_store.save(state)    # 更新进度，等更多决策
        return None                                           # ← 绝不写入 memory
[4] 保存 user 消息到 memory
[5] 构建 context → engine.run(ctx)
```

### 9.3 完整 SuspendResume 流程

```
1. engine.run(ctx) → StartNode → LLMNode → ToolNode
2. ToolNode._classify_all() → 3 个 tool: 1 NORMAL + 2 DANGEROUS
3. ToolNode: strategy.solicit_approval([req_d1, req_d2])
     → SuspendResumeStrategy:
         save ApprovalState(2 pending) → approval_store.save()
         save TurnResumeState(snapshot) → resume_store.save()
         interrupt([req_d1, req_d2]) → raise GraphInterrupt
4. Engine catch → re-raise
5. Pipeline catch → emitter 发审批提示 → return

[用户] /approve tool_d1
6. Pipeline [3]: approval_store.load(session_id) → state
   state.apply(tool_d1, ALLOWED) → 还有 1 个未决定
   approval_store.save(state) → return None

[用户] /approve tool_d2
7. Pipeline [3]: state.apply(tool_d2, ALLOWED) → every_tool_decided = True
   _current_resume.set([ALLOWED, ALLOWED])
   engine.run(ctx) → StartNode → RESUME_TOOLS → ToolNode
8. ToolNode: strategy.solicit_approval([req_d1, req_d2])
     → SuspendResumeStrategy:
         interrupt([req_d1, req_d2]) → _current_resume 非空 → 返回 [ALLOWED, ALLOWED]
9. ToolNode._execute_batch() → ALLOWED × 2 正常执行 + NORMAL 直行
10. ToolNode → LLMNode → ... → EndNode
11. Pipeline: save result → flush → drain buffer
```

### 9.4 InlineWait 流程对比

```
1. engine.run(ctx) → ... → ToolNode
2. strategy.solicit_approval([req_d1, req_d2])
     → InlineWaitStrategy:
         for each req:
            emitter emit → user sees prompt
            channel.wait_for_decision() → 阻塞直到用户返回
         return [ALLOWED, ALLOWED]  # 不抛异常
3. ToolNode._execute_batch() → 全程在一个 engine.run() 内
```

**差异总结**:

| | InlineWait | SuspendResume |
|---|---|---|
| 持久化 | 无 (内存) | ApprovalStateStore + TurnResumeStateStore |
| engine.run() 次数 | 1 次 | 2 次 (首调 + 恢复) |
| ToolNode 代码 | 相同 | 相同 |
| Pipeline 参与 | 无 | 审批消费 + 恢复 |

### 9.5 多Agent消息缓冲

审批暂停期间 peer 回执消息不应触发 engine:

```python
class AgentPipeline:
    _approval_pending: dict[str, list[InputMessage]] = {}

    async def _process_message(self, input_msg: InputMessage) -> AgentResult | None:
        if input_msg.session_id in self._approval_pending:
            self._approval_pending[input_msg.session_id].append(input_msg)
            return None

        try:
            return await self._process_message_locked(input_msg, ...)
        except GraphInterrupt:
            self._approval_pending[session_id] = []
            raise

    async def _drain_approval_buffer(self, session_id: str) -> None:
        pending = self._approval_pending.pop(session_id, [])
        for msg in pending:
            await self._process_message(msg)
```

---

## 十、ReActAgent 薄壳

```python
class ReActAgent(Agent[ReActEvent]):
    def __init__(self, provider, *, mode: Literal["clean", "full"] = "full", ...):
        self.provider = provider
        self.graph = ReActGraph(self, mode=mode)
        self.engine = GraphEngine(self.graph)

    async def run(self, ctx: AgentContext, emitter: ContentEmitter) -> AgentResult:
        ctx.emitter = emitter
        token = current_agent_context.set(ctx)
        try:
            await self._call_hooks(HookPoint.BEFORE_TURN, ctx)
            result = await self.engine.run(ctx)
            await self._call_hooks(HookPoint.AFTER_TURN, ctx, result)
            return result
        except GraphInterrupt:
            raise
        except AgentControlError:
            await asyncio.shield(self._save_checkpoint(...))
            raise
        except asyncio.CancelledError:
            await asyncio.shield(self._save_checkpoint(...))
            raise
        except Exception:
            logger.exception("Agent execution error")
            ...
            return AgentResult(error=str(e), ...)
        finally:
            ctx.metadata.pop(ReActMetaKey.DENY_AS_CANCEL, None)
            ctx.metadata.pop(ReActMetaKey.APPROVAL_DENIAL, None)
            ctx.metadata.pop(ReActMetaKey.INJECTION_CYCLE, None)
            ctx.emitter = None
            current_agent_context.reset(token)
```

---

## 十一、bot_project 适配要点

- `ReActAgent` 用 `ReActGraph(mode="full")`
- Pipeline 选择策略: `SuspendResumeStrategy(approval_store=LocalFileApprovalStateStore(...), resume_store=StateStoreTurnResumeStateStore(...))`
- 策略注入: `ctx.extensions[ExtensionKey.SUSPEND_STRATEGY] = strategy`
- `InterceptorChain` 含 `TieredToolApprovalInterceptor` (tool 分类)
- Main agent 的 `ArgumentSensitiveMatcher` 加入 interceptor chain，peer/subagent 通过 `exclude_interceptor_types` 排除
- 详细适配步骤见实现计划

---

## 十二、文件布局

```
framework/core/
├── graph/
│   ├── __init__.py          # 导出 Node, NodeTransition, Edge, Graph, GraphEngine,
│   │                        #   GraphInterrupt, interrupt, Command, GraphNode, GraphMetaKey
│   ├── constants.py         # GraphNode, GraphMetaKey
│   ├── node.py              # Node ABC, NodeTransition
│   ├── graph.py             # Edge, Graph
│   ├── engine.py            # GraphEngine
│   └── interrupt.py         # GraphInterrupt, interrupt(), Command, _current_resume
├── context_extensions.py    # ExtensionKey
└── agent.py                 # AgentContext (瘦身), ctx_ext()

framework/approval/
├── __init__.py
├── constants.py             # ApprovalDecision, ApprovalTier, ApprovalStatus
├── state.py                 # ApprovalRequest, ApprovalState
└── store.py                 # ApprovalStateStore ABC, LocalFileApprovalStateStore,
                             #   InMemoryApprovalStateStore

framework/agents/react/
├── __init__.py              # 导出 ReActAgent, ReActEvent, ReActAgentBuilder
├── agent.py                 # ReActAgent (~150行)
├── builder.py               # ReActAgentBuilder (已有, 小改)
├── constants.py             # ReActNode, ReActReason, ReActMetaKey
├── graph.py                 # ReActGraph (clean/full)
├── state.py                 # TurnResumeState, TurnResumeStateStore ABC + 实现
├── strategy.py              # SuspendStrategy ABC, InlineWaitStrategy, SuspendResumeStrategy
├── nodes/
│   ├── __init__.py
│   ├── start.py             # StartNode
│   ├── llm.py               # LLMNode (组装消息 + LLM 调用)
│   ├── tool.py              # ToolNode (分类 → 策略 → 执行)
│   └── end.py               # EndNode
└── AGENTS.md

framework/pipeline/
└── pipeline.py              # + 审批消费 + 多agent缓冲 + 策略注入
```

---

## 十三、关键差异 vs 旧设计文档 (2026-05-01)

| | 旧设计 | 新设计 |
|---|---|---|
| **图调度** | 硬编码 `_run_loop` | 抽象 GraphEngine + 枚举 Edge 路由 |
| **引擎入口** | `_run(ctx, emitter, start_node, resume_state)` | `run(ctx)` — emitter 在 ctx 内 |
| **结果返回** | 固定 `AgentResult` | `build_result(ctx)` 可覆写，返回 `Any` |
| **AgentContext** | 20+ 固定字段 | 核心 10 字段 + `extensions` dict |
| **to_messages()** | 含 system_prompt | 不含，LLMNode 显式组装 |
| **字符串** | 裸字符串 | 全枚举化 |
| **审批状态** | 无独立抽象 | ApprovalState + ApprovalStateStore ABC + LocalFile 默认 |
| **中断策略** | 硬编码在 ToolNode / Pipeline | SuspendStrategy ABC，InlineWait + SuspendResume |
| **策略切换** | 代码分散在 interceptor 分支 | 替换 ctx.extensions 中一个实例 |
| **双模式** | 无 Clean | `enable_hooks`/`enable_approval` 开关 |
| **多Agent缓冲** | 无 | 审批中缓冲非审批消息 |
