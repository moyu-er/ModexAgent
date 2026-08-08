# 05 — AgentNode 重设计

Status: ✅ design closed (all 7 items confirmed)
Labels: wayfinder:active
Blocking: 06-graph-deliver-tool, 07-modexctl-deliver-command, 08-bot-graph-factory

## Question

**AgentNode 重设计: 持有结构、emitter 接入、会话管理、deliver tool 集成、auto-deliver、IntegratedInput 消费。**

Ticket 01-04 已闭环 deliver 机制 / taskId / 装配拓扑 / GraphSpec 创作。本 ticket 设计 AgentNode 的完整内部实现——图调度中 agent 节点的业务执行单元。

### 上下文(基于代码探索确认)

**当前 AgentNode**(`src/modex_agent/agents/agent_node.py:85-130`):
- 简单 wrapper: 持有 `agent: Agent` + `agent_context_factory: Callable` + `next_node: str | None`
- execute: 造 `CollectorEmitter`(纯缓冲 sink)→ 覆盖 `agent_ctx.emitter` → `agent.run()` → `self.deliver(result, next_node, ctx)`
- **不回流 WebUI**: CollectorEmitter 跳过 TurnContextBuilder → pool 的 emitter_factory 永不被咨询(ticket 03 §77-82 确认)

**Pool 装配关键路径**(探索确认):
- `PoolInstance`(pool_instance.py:23-54): **没有** `main_agent: Agent` 字段,只有 `main_agent_name: str` + `pool: Any`(AgentPool)
- agent 实例访问: `pool.get(main_agent_name) → AgentInstance → .pipeline.agent`
- `emitter_factory` **不在** PoolInstance 上——它被 `_WorkspaceEmitterFactory` 包裹后注入 `TurnContextBuilder._emitter_factory`(factory.py:256-260)
- `WebBotEmitter`: 双 sink——WebSocketOutputAdapter(直接)+ TranscriptStore(via `_persist`),**不走 broker**
- `AgentPool.get(name)`: 返回 `AgentInstance`,**无锁/无 mutex**;single-flight 是 per-session 的(`InboxPoller.inflight: dict[session_id, asyncio.Task]`)

**Session 机制**(探索确认):
- `SessionInfo`(session_id.py:51-128): frozen Pydantic,`session_id="{prefix}.{agentName}"`
- `SessionIdFactory.create(agent_name, external_id=...)`: 生成 session_id,external_id 编码进 prefix
- `SessionRegistry.register(session)`: 公开方法,write-through 到 SessionStore
- `AgentPool.session_registry` property: 暴露注册器

**deliver tool 参考模式**(探索确认):
- `TaskDispatchTool`(tools.py:481-668): `description` property 每次从 `store.list()` 重建;`get_dynamic_schema()` 绑 enum;`execute()` 校验 target → 委托 `service.send_async()`
- `CommunicationTargetStore`(tools.py:159-342): `add/pop/list/has/get` + description 缓存失效
- `AgentCommunicationSystemPromptProvider`(providers.py:285-326): 复合 provider,`_PeerCommSubProvider` 通过 `tool_manager.get_tool("task") → isinstance → list_targets()` 发现 targets

**auto-deliver 参考模式**(探索确认):
- `SubagentAutoSendHook`(subagent_auto_send.py:88-514): `FINALLY_TURN` 钩子,全路径触发(success/error/cancel)
- `_extract_full_result_text`: 反向扫描 `result.messages` 找最后一条 assistant 消息 → fallback `result.content` → 剥 think 标签
- `_classify`: error/loop_detected = 硬失败;max_iterations/turn_cancelled/timeout = 软失败(native)
- 格式化委托 `build_agent_comm_message`(message_format.py:163-223)

**IntegratedInput 注入参考**(探索确认):
- `InboxFlushHook`(inbox_flush.py:21-79): `BEFORE_TURN` + `BEFORE_ITERATION`,消费 inbox → 构建 `SYSTEM_REMINDER` record → `history.append()`
- `build_agent_reminder_record`: 设 `role=SYSTEM_REMINDER`,sanitize content,wrap in system-reminder

**Node.run 生命周期**(node.py:125-315):
- load_latest → begin_invocation → integrate(collect_consumable_delivers + mark_consumed) → execute(undelivered 检测重试) → submit → complete_invocation + promote_delivers → finalize
- `_pending_delivers` 在 execute 期间是纯内存累积,submit 时统一 dispatch

## Discussion

### 3.1 node_id 注入: NodeRegistry.create 收敛点(对 ticket 03 方式 2 的修正)

> ✅ **confirmed** (2026-08-07)

**ticket 03 原决议**: `NodeFactory.create(spec, node_id: str)` 签名变更(方式 2),影响全部 16 个 NodeFactory 子类。

**探索发现**: `NodeRegistry.create`(node_factory.py:163)是 node 创建的**单一收敛点**——它调 `factory.create(spec)` 后设 `node.name = spec.name`。在 `NodeRegistry.create` 层注入 node_id 不改 ABC 契约:

```python
# NodeRegistry.create 收敛注入(修正方式 2)
def create(self, spec: NodeSpec) -> Node[Any]:
    ...
    node = factory.create(spec)          # ABC 不变
    node.name = spec.name                 # 现有
    node.node_id = generate_id(prefix="node")  # 新增: 收敛注入
    if spec.trigger is not None:
        node.trigger = spec.trigger
    return node
```

**Node 基类加 `node_id: str = ""` 字段**(node.py:75 附近,和 `name: str = ""` 并列)。

**优势**:
- 零 ABC 契约变更(16 个子类不动)
- 单一注入点(收敛规则 1)
- factory 不需要知道 node_id——AgentNode 在 execute() 时从 `self.node_id` 读取(lazy)

**AgentNode 的 node_id 使用**: 不在构造时用,在首次 execute() 时用 `self.node_id` 生成 session_id。lazy 模式,不需要 factory 传参。

**待确认**: 此方案是否可接受?它简化了 ticket 03 的 breaking change 范围(去掉 NodeFactory.create 签名变更),但 node_id 生成时机从 "graph instance 创建时" 变为 "NodeRegistry.create 调用时(编译时)"。两者时机接近(compile 在 create_and_run 中紧邻 graph_instance_id 生成),差异可接受。

### 3.2 AgentNode 持有结构

> ✅ **归属确认** (2026-08-07): 框架层 `AgentNode`(ABC)实现 session 策略 + 映射逻辑;bot 层 `BotAgentNode` 继承,实现 execute 业务。

**框架层 ABC**(modex_agent/agents/agent_node.py):
```python
class SessionStrategy(Enum):
    """AgentNode 的 session 复用策略。"""
    CACHED = "cached"                  # 同 node 多次调用复用 session(上下文连续)
    PER_INVOCATION = "per_invocation"  # 每次 invocation 新 session(未来模式)

class AgentNode(Node[Any], ABC):
    """框架层 AgentNode ABC——实现通用 session 策略 + 映射逻辑。

    session 映射规则: external_id = f"{self.node_id}.{agent_name}"
    → SessionIdFactory 编码进 session_id prefix
    → 同 node 多次调用(react 环形)= 同 session(CACHED 模式)

    框架层提供: session 策略 + 映射 + SessionRegistry.register
    子层提供: agent_name/pool_name/workspace_resolver + execute 业务逻辑
    """

    def __init__(
        self,
        *,
        session_strategy: SessionStrategy = SessionStrategy.CACHED,
    ) -> None:
        self._session_strategy = session_strategy
        self._session: SessionInfo | None = None

    @abstractmethod
    def agent_name(self) -> str:
        """子类提供 agent 名(用于 session 映射)。"""
        ...

    @abstractmethod
    async def _resolve_session_registry(self) -> SessionRegistry:
        """子类提供 pool 的 session_registry。"""
        ...

    async def _ensure_session(self, ctx: GraphContext[Any]) -> SessionInfo:
        """从图信息映射到 session_id。

        CACHED 模式: 缓存在实例上,同 node 多次 execute(react 环形)复用。
        PER_INVOCATION 模式: 每次新建,不读缓存。
        """
        if self._session_strategy == SessionStrategy.PER_INVOCATION:
            return await self._create_session(ctx)
        if self._session is not None:
            return self._session
        self._session = await self._create_session(ctx)
        return self._session

    async def _create_session(self, ctx: GraphContext[Any]) -> SessionInfo:
        """创建 session: external_id = f"{node_id}.{agent_name}"。"""
        external_id = f"{self.node_id}.{self.agent_name()}"
        factory = SessionIdFactory()
        session = factory.create(
            agent_name=self.agent_name(),
            external_id=external_id,
        )
        registry = await self._resolve_session_registry()
        await registry.register(session)
        return session
```

**bot 层实现**(examples/bot_project/bot/graph/agent_node.py):
```python
class BotAgentNode(AgentNode):
    """bot_project 的 AgentNode 实现——图调度中 agent 节点的业务执行单元。"""

    def __init__(
        self,
        agent_name: str,
        pool_name: str,
        workspace_resolver: WorkspaceResolverCell,
        *,
        session_strategy: SessionStrategy = SessionStrategy.CACHED,
    ) -> None:
        super().__init__(session_strategy=session_strategy)
        self._agent_name = agent_name
        self._pool_name = pool_name
        self._workspace_resolver = workspace_resolver
        self._deliver_tool: GraphDeliverTool | None = None

    def agent_name(self) -> str:
        return self._agent_name

    async def _resolve_session_registry(self) -> SessionRegistry:
        pool = self._resolve_pool()
        return pool.pool.session_registry
```

**不直接持有 agent/builder/emitter**——通过 resolver lazy 获取 pool 资源:
```python
def _resolve_pool(self) -> PoolInstance:
    resources = self._workspace_resolver.resolve_workspace()
    pool = resources.pools.get(self._pool_name)
    if pool is None:
        raise RuntimeError(f"Pool {self._pool_name!r} not found in workspace")
    return pool

def _resolve_agent_instance(self) -> AgentInstance:
    pool = self._resolve_pool()
    instance = pool.pool.get(self._agent_name)
    if instance is None or instance.pipeline is None:
        raise RuntimeError(f"Agent {self._agent_name!r} not found in pool {self._pool_name!r}")
    return instance
```

**TurnContextBuilder 访问**(§3.5 确认的路径):
```python
builder = instance.pipeline.turn_context_builder  # public property
agent = instance.pipeline.agent                    # ReActAgent
ctx_mgr = instance.context_manager                # ContextManager
```

### 3.3 emitter 接入: TurnContextBuilder 复用

> ✅ **确认** (2026-08-07): 不需要 PoolInstance 暴露 emitter_factory 字段。emitter 通过 TurnContextBuilder.build_runtime_and_context 获取。

**原问题**: emitter_factory 当前不在 PoolInstance 上,被 `_WorkspaceEmitterFactory` 包裹后埋在 `TurnContextBuilder._emitter_factory` 里。AgentNode 无法访问。

**修正**: §3.5 确认 AgentNode 复用 `TurnContextBuilder.build_runtime_and_context`,该方法在 line 539-540 调 `self._emitter_factory(session.session_id)` 造 emitter。AgentNode 不需要直接访问 emitter_factory——`build_runtime_and_context` 内部处理。

**收敛规则**: 不造第三条 emitter 路径。AgentNode 用 TurnContextBuilder 造 emitter(和 TurnRunner.execute_turn 走同一条路径),WebSocket 流 + transcript 持久化自动复用。

**emitter_factory 可靠性**(验证确认):
- 在 TurnContextBuilder 构造时设置(`__init__` line 168)
- 由 `ReActTurnRunner.set_emitter_factory` 在 pool 装配时注入
- 在 AgentNode.execute() 时已稳定(wiring 在第一个 turn 之前完成)

### 3.4 会话管理: 框架层 AgentNode ABC 实现

> ✅ **确认** (2026-08-07): session 逻辑放 modex_agent 框架层(AgentNode ABC),映射规则 `external_id = f"{node_id}.{agent_name}"`,CACHED 模式为默认。

**设计决策**: 框架层 `AgentNode` ABC 实现 session 策略(§3.2),bot 层 `BotAgentNode` 继承并提供 agent_name + session_registry。

**映射规则**: `external_id = f"{self.node_id}.{self.agent_name()}"` → SessionIdFactory 编码进 session_id prefix。同 node 多次调用(react 环形)= 同 session_id = 上下文连续。

**两种模式**:
- `CACHED`(默认): `_session` 缓存在实例上,同 BotAgentNode 多次 execute 复用。满足当前业务需求——react 环形依赖反复调用时 session 一致。
- `PER_INVOCATION`: 每次 invocation 新建 session,不读缓存。未来模式。

**注**: 当前 session_id 格式是 `nodeId.agentName`,不是最终的统一收敛形式(ticket 03 提到的 node_id 生成形式统一是未来工作)。当前 node 业务层实现够用即可。

**GraphContext 字段**: `ctx.graph_instance_id: int | None`(context.py:133)—— node 能拿到 graph instance ID。但当前映射规则不依赖它(用 node_id + agent_name),如果未来需要 graph-level session 隔离可以加入组合。

**session 生命周期**: 随 BotAgentNode 实例存活。graph 完成后 session 保留在 pool 的 session_registry 中(便于会话历史追溯,ticket 03 §34)。

> **M2 修复** (2026-08-08): session_registry 累积泄漏。定义驱逐策略: graph-created sessions(external_id 以 `node_` 前缀)在 graph instance 从 _active_instances 移除时(ticket 09 M1 修复)一并清理。或依赖现有 session GC(ADR-0018),需确认覆盖 graph sessions。实现时决定具体策略。

### 3.5 execute() 完整流程

> ✅ **执行模型确认** (2026-08-07): 直接调 `await agent.run()`,复用 TurnContextBuilder 构建 AgentContext。不走 pool 的 inbox/InboxPoller 异步模型。

**验证结论**(基于 explore agent bg_fe490026 的代码验证):

`TurnContextBuilder.build_runtime_and_context` 可安全在 `process_locked` 之外调用:
- 14 个实例属性在构造时设置,`process_locked` 不改变它们
- 唯一前置条件: `context_state` 参数(从 `assemble` 来)+ `ContextManager`
- 可跳过: session lock / busy check / slash commands / approval / on_session_start
- 复用获得: hooks / interceptors / governance / turn_store / control_channel / safety / trace_store

**AgentInstance 访问路径**(descriptor.py:122-137):
- `instance.pipeline.turn_context_builder` — public property(pipeline.py:149)
- `instance.pipeline.agent` — ReActAgent
- `instance.context_manager` — ContextManager

**execute() 流程**:

> **修正** (2026-08-08): deliver tool 不再 register/unregister 到共享 ToolManager(会影响其他正在运行的会话)。改为 GraphToolPreset 创建独立 ToolManager 实例(拷贝 base tools + 加入 graph preset tools)。AgentContext 加 `graph_context` 字段标记图调度上下文。

```python
async def execute(self, ctx: GraphContext[Any], integrated_input: IntegratedInput) -> None:
    """AgentNode 业务执行: 构建 context → 注入输入 → 运行 agent → auto-deliver。"""
    # 1. 获取 pool 资源(lazy)
    instance = self._resolve_agent_instance()  # pool.get(agent_name)
    builder = instance.pipeline.turn_context_builder
    ctx_mgr = instance.context_manager
    agent = instance.pipeline.agent

    # 2. 确保 session
    session = await self._ensure_session(ctx)

    # 3. 构建 context_state(唯一前置步骤)
    input_msg = self._build_input_message(integrated_input, session)
    context_state = await builder.assemble(
        session.session_id, input_msg, input_metadata={},
        sanitized_content=input_msg.content, media_blocks=[],
        _media_processor=None, ctx_mgr=ctx_mgr, route_result=None,
        _is_approval_cmd=False, append_user_message=True,
    )

    # 4. 构建完整 AgentContext + emitter
    agent_context, emitter = builder.build_runtime_and_context(
        session, context_state, ctx_mgr,
        input_metadata={}, pool_data=None,
        inline_attachments=[], workspace=None,
    )

    # 5. 注入 IntegratedInput 为 system-reminder(参考 InboxFlushHook)
    if integrated_input.payloads:
        reminder = self._format_integrated_input(integrated_input)
        await agent_context.history.append(reminder)

    # 6. 替换为 graph 专用 ToolManager(拷贝 base + preset,不影响共享 ToolManager)
    self._ensure_deliver_tool(ctx)
    preset = GraphToolPreset(graph_tools=[self._deliver_tool])
    agent_context.tool_manager = preset.build_tool_manager(instance.pipeline.tool_manager)

    # 7. 标记图调度上下文(方案 A)
    agent_context.graph_context = ctx

    # 8. 运行 agent(同步 await 到跑完)
    result = await agent.run(agent_context, emitter)

    # 9. auto-deliver(agent 没用 deliver tool 时的兜底)
    if not self._has_pending_delivers():
        output = self._extract_auto_deliver_content(result)
        self.deliver(GraphPayload(content=output), None, ctx)  # 包裹为 GraphPayload (H1 修复)

    # per-execution ToolManager 随 agent_context 一起 GC,不影响共享 ToolManager
```

**后续需设计的 3 个点**(不阻塞当前决策):
1. ~~**turn task 注册**~~: 不需要——stop/approval 都不在图调度 scope 内
2. ~~**GraphInterrupt 处理**~~: 不需要——approval 不在图调度 scope 内
3. ~~**on_session_end cleanup**~~: 不需要——当前 pool 装配中 `on_session_end=None`(factory.py:232),未被使用。execute_turn finally 的 `unregister_turn` 也不需要(turn task 注册不在 scope 内)。`_safe_flush`(memory flush)是实现细节,实现时决定。

### 3.6 IntegratedInput 消费: system-reminder 注入

> ✅ **确认** (2026-08-07): 本期不做 GraphContextSystemPromptProvider,但保留占位。数据流: IntegratedInput → system-reminder → agent(不同于常规用户输入调用)。

**关键数据流确认**: 图调度的 agent 输入来自 IntegratedInput(上游 delivers 聚合),不是常规用户输入。这意味着:
- 不走 `build_turn_request` / `preprocess`(那是用户输入 sanitize/attachment 处理)
- IntegratedInput 格式化为 system-reminder,直接 append 到 `agent_context.history`
- 后续可能需要 system prompt 补强(GraphContextSystemPromptProvider 占位),让 agent 理解"这是图调度的输入,不是用户对话"

**格式**(ticket 01 §120-137 的设计,基于探索确认的 InboxFlushHook 模式):

```python
def _format_integrated_input(self, integrated_input: IntegratedInput) -> dict[str, Any]:
    """格式化 IntegratedInput 为 system-reminder 消息。

    按 source node 分章节,每个 node 内部按顺序标 part_x。
    """
    if not integrated_input.payloads:
        return {}

    sections: dict[str, list[str]] = {}
    for payload in integrated_input.payloads:
        # node_id → name 反查(H3 修复): source_node 是 node_id,显示用 name
        source_id = payload.source_node
        source_name = self._resolve_source_name(source_id)  # 通过 graph_ref 反查
        parts = sections.setdefault(source_name, [])
        # GraphPayload 提取(H2 修复): payload.content 是 GraphPayload,取 .content
        content = payload.content.content if hasattr(payload.content, 'content') else str(payload.content)
        parts.append(content)

    lines = ["<system-reminder>"]
    for source, parts in sections.items():
        lines.append(f"Message from node '{source}':")
        for i, part in enumerate(parts, 1):
            lines.append(f"  part_{i}: {part}")
        lines.append("")
    lines.append("</system-reminder>")

    return {
        "role": MessageRole.SYSTEM_REMINDER,
        "content": "\n".join(lines),
        "meta_graph_input": True,
        "meta_source_nodes": list(sections.keys()),
    }

def _resolve_source_name(self, node_id: str) -> str:
    """node_id → node_name 反查(H3 修复)。

    通过 graph_ref.nodes 反查:遍历 nodes 找 node_id 匹配的 name。
    全局可能重名所以必须用 node_id 作为标识,显示用 name。
    找不到时返回 node_id 本身(降级)。
    """
    if self._graph_ref is not None:
        for name, node in self._graph_ref.nodes.items():
            if getattr(node, 'node_id', None) == node_id:
                return name
    return node_id  # 降级:找不到 name 时显示 node_id
```

**注入路径**: 直接 append 到 `agent_context.history`(参考 InboxFlushHook 的 `history.append(append_dict)`)。不走 prompt_pipeline 的 SystemPromptProvider——因为 IntegratedInput 是 per-execution 的动态数据,不是静态 system prompt 内容。

**GraphContextSystemPromptProvider(占位,后续增强)**:
- 后续需要一个 system prompt 补强,让 agent 理解图调度上下文(当前节点位置、上下游角色、这是图输入不是用户对话)
- 参考 `AgentCommunicationSystemPromptProvider`(providers.py:285-326)的复合 provider 模式
- 和 IntegratedInput 的 system-reminder 正交(一个是静态图全貌,一个是动态上游内容)
- 本期不实现,保留占位

### 3.7 deliver tool 集成: GraphDeliverTool

**参考**: `TaskDispatchTool`(tools.py:481-668)+ `CommunicationTargetStore`(tools.py:159-342)。

**GraphDeliverTargetStore**: 从图拓扑提取下游节点。
```python
class GraphDeliverTargetStore:
    """从 CompiledGraph 拓扑提取可用 deliver 目标。"""

    def __init__(self, graph_ref: CompiledGraph[Any], current_node: str) -> None:
        self._graph = graph_ref
        self._current = current_node
        self._targets: list[GraphDeliverTarget] | None = None

    def list(self) -> list[GraphDeliverTarget]:
        if self._targets is None:
            edges = self._graph.edges_from(self._current)
            self._targets = [
                GraphDeliverTarget(
                    name=e.target,
                    description=f"Deliver to downstream node '{e.target}'",
                )
                for e in edges
                if e.target != GraphNode.END
            ]
        return list(self._targets)

    def get(self, name: str) -> GraphDeliverTarget | None:
        return next((t for t in self.list() if t.name == name), None)
```

**GraphDeliverTool**: 镜像 TaskDispatchTool 的动态描述 + enum 绑定 + 校验。
- `description` property: 从 store.list() 重建
- `get_dynamic_schema()`: 绑 target_node enum
- `execute()`: 校验 target → 调 `node.deliver(content, target, ctx)` 累积(不是 send_async,是 in-memory 累积)

**关键区别**: TaskDispatchTool 委托 `service.send_async()` 走 broker/bus;GraphDeliverTool 调 `self._node.deliver(content, node_id, ctx)` 累积到 `_pending_delivers`——图调度的 deliver 是临时累积 + submit 统一 dispatch → `deliver_store.accumulate()`(持久化,策略可选)。deliver content 是 `GraphPayload`(ticket 11 §5)。

**node_id 对齐** (2026-08-07): deliver tool 对 agent 暴露 node_name(局部安全:同一 node 的下游不重名),execute 时内部转换为 node_id(通过 `graph_ref.nodes[name].node_id`)→ 调 `node.deliver(GraphPayload, node_id, ctx)`。

**集成路径** (2026-08-08 修正): deliver tool 不再 register/unregister 到共享 ToolManager(并发安全问题:同一 agent 实例被多个会话共享,register/unregister 会影响其他正在运行的会话)。改为通过 `GraphToolPreset` 创建独立 ToolManager 实例(拷贝 base tools + 加入 graph preset tools),替换 `agent_context.tool_manager`。详见 §3.5 execute 流程 + `GraphToolPreset` 类型。

**GraphToolPreset**(modex_agent 框架层):
```python
class GraphToolPreset:
    """图调度专用 tool preset——统一注册 graph 相关 tools。
    
    在 pool 的常规 tool 列表基础上,创建独立 ToolManager 实例,
    加入 graph preset tools(deliver / 后续 graph 查看等)。
    不修改 pool 的共享 ToolManager(并发安全)。
    """

    def __init__(self, graph_tools: list[Tool]) -> None:
        self._graph_tools = graph_tools

    def build_tool_manager(self, base: ToolManager) -> InMemoryToolManager:
        """从 base 拷贝 tool 列表 + 加入 graph preset tools。"""
        tm = InMemoryToolManager(config=base.config)
        for name in base.list_tools():
            tool = base.get_tool(name)
            if tool is not None:
                tm.register(tool)
        for tool in self._graph_tools:
            tm.register(tool)
        return tm
```

**AgentContext.graph_context 字段**(方案 A,区分图调度 vs 常规会话):
```python
class AgentContext:
    ...
    graph_context: GraphContext | None = None  # 图调度时注入,常规会话为 None
```

BotAgentNode.execute() 时设 `agent_context.graph_context = ctx`。任何需要判断上下文的代码检查此字段。

**归属**: GraphDeliverTool + GraphDeliverTargetStore + GraphToolPreset + AgentContext.graph_context 放 **modex_agent 框架层**。bot 层只负责注入 graph_ref。

**ticket 06 细化**: 本 ticket 确认集成路径(GraphToolPreset)。完整设计见 ticket 06。

### 3.8 auto-deliver: AgentResult 提取

> ✅ **确认** (2026-08-07): 接受框架既有行为——`deliver(content, None, ctx)` 的 `None` target 由 `_resolve_default_target` 解析为所有下游。无需改变框架语义。

**验证**(`_resolve_default_target`, node.py:363-379):
```python
def _resolve_default_target(self, ctx: GraphContext[S]) -> list[str]:
    graph = self._graph_ref
    targets = [e.target for e in graph.edges_from(self.name)]
    if targets:
        return targets          # 所有下游边
    return [GraphNode.END]      # 没有下游 → END
```

**含义**: auto-deliver 调 `self.deliver(output, None, ctx)`,submit 时自动 fan-out 到所有下游。agent 如果要选择性投递,应该用 deliver tool 显式选 target。后续 GraphDeliverTool(ticket 06)的 tool description 中需向 agent 说明:不显式调用 deliver tool 时,结果会自动投递到所有下游节点。

**参考**: `SubagentAutoSendHook._extract_full_result_text`(subagent_auto_send.py:286-305)。

**提取逻辑**:
```python
def _extract_auto_deliver_content(self, result: AgentResult | None) -> str:
    """从 AgentResult 提取 auto-deliver 内容。

    优先: result.messages 反向扫描最后一条 assistant 消息。
    兜底: result.content(仅正常完成时有意义)。
    剥除 think 标签。
    """
    raw = ""
    if result is not None and result.messages:
        for msg in reversed(result.messages):
            role = msg.role if hasattr(msg, 'role') else msg.get('role')
            if str(role) == 'assistant':
                content = msg.content if hasattr(msg, 'content') else msg.get('content')
                if content:
                    raw = str(content)
                    break
    if not raw and result is not None:
        raw = result.content or ""
    # 剥 think 标签(复用 SubagentAutoSendHook 的 regex)
    raw = _THINK_PAIRED_RE.sub("", _THINK_TAG_RE.sub("", raw))
    return raw
```

**auto-deliver 触发条件**: execute() 结束后检查 `_pending_delivers`——如果 agent 已经用 deliver tool 投递了,不 auto-deliver(避免重复)。只有 agent 没投递时才兜底。

```python
def _has_pending_delivers(self) -> bool:
    return bool(self._pending_delivers)
```

**内容结构**: 先实现基础版(直接提取文本 deliver)。后续完善加 SubagentAutoSendHook 式的结构化信封(ResultMeta 头 + 格式化 body),用 `build_agent_comm_message` 格式化。结构化信封是增强,不是阻塞项。

### 3.9 agent_context_factory 设计

> ✅ **确认** (2026-08-07): 不需要自定义 agent_context_factory。直接复用 pool 的 TurnContextBuilder。

**原设计**: `AgentNode.__init__` 接受 `agent_context_factory: Callable[[GraphContext], AgentContext]`。

**修正**: AgentNode 通过 `instance.pipeline.turn_context_builder` 获取 pool 已有的 TurnContextBuilder,调 `builder.assemble()` + `builder.build_runtime_and_context()` 构建 AgentContext。不需要自定义 factory。

**理由**(基于 explore agent bg_fe490026 验证):
- TurnContextBuilder 的 14 个实例属性在构造时设置,稳定可复用
- 复用获得完整 turn 基础设施(hooks / interceptors / governance / turn_store / control_channel)
- 自定义 factory 会丢失这些基础设施(或需要手动重建,违反收敛规则)

### 3.10 AgentNodeFactory 重设计

**当前**: `AgentNodeFactory`(agent_node.py:133)持有 `agents: dict[str, Agent]` + `context_factories: dict[str, Callable]`。

**重设计**: 不持有 agent 实例(lazy 从 pool 获取),持有 workspace_resolver + pool/agent 名映射。

```python
class BotAgentNodeFactory(NodeFactory):
    """从 NodeSpec.config(agent/pool 名)创建 BotAgentNode。"""

    def __init__(self, workspace_resolver: WorkspaceResolverCell) -> None:
        self._resolver = workspace_resolver

    def create(self, spec: NodeSpec) -> Node[Any]:
        agent_name = spec.config.get("agent")
        pool_name = spec.config.get("pool", "default")
        if not agent_name or not isinstance(agent_name, str):
            raise ValueError(f"AgentNode requires config['agent'] (string). Got: {agent_name!r}")
        return BotAgentNode(
            agent_name=agent_name,
            pool_name=pool_name,
            workspace_resolver=self._resolver,
        )

    def config_schema(self) -> type[BaseModel] | None:
        return None  # config 在 create() 中校验
```

**收敛**: 一个 factory 服务所有 agent node——不按 agent 名注册多个 factory,而是在 create() 时从 spec.config 读 agent/pool 名。NodeRegistry 只注册一个 `"agent" → BotAgentNodeFactory`。

## 归属汇总

| 层 | 内容 | 归属 |
|----|------|------|
| `Node.node_id: str` 字段 | Node 基类加字段 | **modex_graph** |
| `NodeRegistry.create` 注入 node_id | 收敛注入点 | **modex_graph** |
| `generate_id(prefix, separator)` 工具方法 | opencode 风格短 ID | **modex_agent/utils/id.py** |
| `AgentNode` ABC + `SessionStrategy` + session 映射 + `resolve_description()` | 通用 session 策略(`nodeId.agentName` → SessionIdFactory) + desc 获取(默认 "[not found]") | **modex_agent**(框架层) |
| `AgentContext.graph_context` 字段 | 区分图调度 vs 常规会话(方案 A) | **modex_agent**(框架层) |
| `GraphToolPreset` | 拷贝 base tools + 加入 graph preset,创建独立 ToolManager | **modex_agent**(框架层) |
| `BotAgentNode` | AgentNode 业务实现(execute + pool 资源 lazy 获取) | **bot_project** |
| `BotAgentNodeFactory` | 从 spec.config 创建 BotAgentNode | **bot_project** |
| `GraphDeliverTool` + `GraphDeliverTargetStore` + `GraphDeliverTarget` | agent self-deliver tool | **modex_agent**(框架层,ticket 06) |
| `GraphContextSystemPromptProvider` | 图上下文 system prompt 注入 | **modex_agent**(后续增强,本期不做) |
| auto-deliver 内容提取 | 从 AgentResult 提取 | **bot_project**(BotAgentNode 方法) |
| IntegratedInput 格式化 | system-reminder 格式 | **bot_project**(BotAgentNode 方法) |

## 清理项(同步处理)

在 node_id 改动时一并清理(ticket 01 §149-157):
- C1: `NodeInstance.upstream_payloads` 死数据——存了没人读
- C3: `deliver()` 的 ctx 参数 vestigial——`_deliver` 不用 ctx
- C5: `SchedulerInstanceStatus` 重复枚举——和 `NodeInstanceStatus` 重复

## 待确认(阻塞实现)

1. ~~**§3.1 node_id 注入方案**~~: ✅ confirmed — NodeRegistry.create 收敛注入
2. ~~**§3.4 session 策略**~~: ✅ confirmed — 框架层 AgentNode ABC,CACHED 模式,`nodeId.agentName` 映射
3. ~~**§3.3 emitter_factory 暴露**~~: ✅ confirmed — 通过 TurnContextBuilder 复用,不需要 PoolInstance 加字段
4. ~~**§3.8 auto-deliver 多下游**~~: ✅ confirmed — 接受框架既有行为(None → 所有下游),deliver tool description 中向 agent 说明
5. ~~**§3.6 图上下文 prompt provider**~~: ✅ confirmed — 本期不做,保留占位,后续需要 system 补强

## 后续追踪事项(不阻塞 ticket 05,但需在 ticket 06 中讨论)

- **GraphDeliverTool 的 description 设计**: 如何向 agent 描述下游 targets(包括 auto-deliver 到所有下游的兜底行为说明)。当前在 ticket 06 中有集成路径(§3.7),但 description 的具体措辞和格式需要专门讨论。**检查: ticket 06 是否已创建?** → 未创建,ticket 05 §3.7 只确认了集成路径。需创建 ticket 06 并纳入此项。
- **GraphContextSystemPromptProvider(占位)**: 后续 system prompt 补强,让 agent 理解图调度上下文(节点位置、上下游角色、这是图输入不是用户对话)。参考 `AgentCommunicationSystemPromptProvider` 复合 provider 模式。

## 不做什么

- 不让 AgentNode 走 pool 的 turn 循环(InboxPoller/dispatch_envelope)——图调度独立于 pool
- 不在 AgentNode 中实现 mid-execution deliver——deliver 在 execute 期间累积,submit 统一 dispatch(ticket 01)
- 不自造第三条 emitter 路径——复用 pool 的 emitter_factory(收敛规则)
- 不做 GraphContextSystemPromptProvider(图全貌 prompt 注入)——后续增强
- 不做 auto-deliver 结构化信封——先实现基础版,后续完善
