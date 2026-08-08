# 06 — GraphDeliverTool

Status: ✅ design closed (all 5 items confirmed)
Labels: wayfinder:active
Blocking: 07-modexctl-deliver-command

## Question

**GraphDeliverTool: agent 在 execute() 期间调用的 deliver tool,动态暴露下游 targets,让 agent 显式选择投递目标。**

Ticket 05 §3.7 确认了集成路径。本 ticket 设计完整实现,重点是 description 设计(如何向 agent 描述下游 targets + auto-deliver 兜底行为说明)。

### 上下文(基于代码探索确认)

**参考: TaskDispatchTool**(tools.py:481-668):
- `description` property: 每次调 `_build_description()` 重建(无缓存)
- `_build_description`: 基本说明 + "When NOT to use" + "Usage notes" + 按 kind 分 "Peer Agents" / "Subagents" 章节
- `get_dynamic_schema()`: 绑 target_agent enum 为当前可用 target 名列表
- `execute()`: 校验 target_agent → 委托 `service.send_async()` 走 broker/bus
- `_TASK_PARAMS`: 静态参数定义(target_agent / content / invocation_id)

**参考: CommunicationTargetStore**(tools.py:159-342):
- `add/pop/list/has/get` + description 缓存失效
- `CommunicationTarget`: frozen dataclass(name/kind/description/pool_name/bus_ref/execution_strategy)

**参考: SubagentAutoSendHook**(subagent_auto_send.py):
- auto-deliver 兜底: agent 没用 tool 投递时,从 AgentResult 提取内容 auto-deliver
- ticket 05 §3.8 确认: auto-deliver 到所有下游(None → _resolve_default_target)

**Node._graph_ref**(node.py:198):
- 在 `Node.run(graph=compiled)` 中设置,execute() 之前
- BotAgentNode 可通过 `self._graph_ref` 获取 CompiledGraph 拓扑

**GraphPayload**(ticket 11 §5):
- deliver content 现在是 `GraphPayload`(frozen Pydantic),不是 `Any`
- `content: str` 字段

**node_id 对齐** (2026-08-07):
- deliver tool 对 agent 暴露 node_name(局部安全:同一 node 的下游不重名)
- execute 时内部转换为 node_id(通过 `graph_ref.nodes[name].node_id`)
- 调 `node.deliver(GraphPayload, node_id, ctx)` 累积
- 持久化层/store/IntegratedPayload.source_node 全部用 node_id(全局可能重名)

## Discussion

### 1. 集成路径(✅ confirmed, 2026-08-08 修正)

> ✅ confirmed (from ticket 05, 2026-08-08 修正: GraphToolPreset 替代 register/unregister)

AgentNode.execute() 期间通过 `GraphToolPreset` 创建独立 ToolManager 实例(拷贝 base tools + 加入 deliver tool),替换 `agent_context.tool_manager`。不修改共享 ToolManager(并发安全)。

```python
# BotAgentNode.execute() 中(ticket 05 §3.5)
self._ensure_deliver_tool(ctx)
preset = GraphToolPreset(graph_tools=[self._deliver_tool])
agent_context.tool_manager = preset.build_tool_manager(instance.pipeline.tool_manager)
agent_context.graph_context = ctx  # 标记图调度上下文

result = await agent.run(agent_context, emitter)
# per-execution ToolManager 随 agent_context 一起 GC,不影响共享 ToolManager
```

### 2. GraphDeliverTargetStore + GraphDeliverTarget(✅ confirmed)

从 CompiledGraph 拓扑提取下游节点。对 agent 暴露 name + description。

```python
class GraphDeliverTarget(BaseModel):
    """图 deliver 目标——下游节点描述。"""
    name: str           # 下游节点名(人类可读,agent 可见)
    description: str    # 节点描述(业务层赋予)

    model_config = ConfigDict(frozen=True, extra="forbid")

class GraphDeliverTargetStore:
    """从 CompiledGraph 拓扑提取可用 deliver 目标。

    对 agent 暴露 node_name(局部安全:同一 node 的下游不重名)。
    """

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
                    description=self._resolve_node_description(e.target),
                )
                for e in edges
                if e.target != GraphNode.END
            ]
        return list(self._targets)

    def get(self, name: str) -> GraphDeliverTarget | None:
        return next((t for t in self.list() if t.name == name), None)

    def resolve_node_id(self, name: str) -> str:
        """name → node_id 转换(局部安全:下游不重名)。"""
        node = self._graph.nodes.get(name)
        if node is None:
            raise RoutingError(f"Node {name!r} not found in graph")
        return node.node_id

    def _resolve_node_description(self, node_name: str) -> str:
        """从节点提取业务描述。"""
        node = self._graph.nodes.get(node_name)
        if node is None:
            return "[not found]"
        # AgentNode ABC 提供 resolve_description()(默认 "[not found]")
        # BotAgentNode override: 从 AgentInstance.descriptor.description 获取
        # 非 AgentNode 类型没有此方法 → fallback "[not found]"
        if hasattr(node, "resolve_description"):
            return node.resolve_description()
        return "[not found]"
```

**desc 获取链路**:
- `AgentNode` ABC(modex_agent 框架层)提供 `resolve_description() -> str`,默认返回 `"[not found]"`
- `BotAgentNode` override: 从 `_resolve_agent_instance().descriptor.role_description` 获取(复用已有字段,零新依赖)
- 非 AgentNode 类型(function/delay 等)没有此方法 → fallback `"[not found]"`
- GraphSpec `config.description` 作为可选覆盖(后续增强,同一 agent 在不同图中角色不同时覆盖)
- L5 设计决策: resolve_description 放 AgentNode ABC(不放 Node ABC),非 AgentNode 用 hasattr 检查——用户明确要求

**循环依赖排查**: 无循环。BotAgentNode 本来就持有 pool 引用(通过 resolver),execute 时必然走 `_resolve_agent_instance()` 拿 AgentInstance。desc 顺手从同一个 AgentInstance.descriptor 取——复用已有链路,不引入新依赖。

**END 不作为 deliver target**: `if e.target != GraphNode.END` 过滤掉 END。agent 不应该显式 deliver 到 END——auto-deliver 兜底会处理(ticket 05 §3.8)。

**归属**: GraphDeliverTargetStore + GraphDeliverTarget 放 **modex_agent** 框架层。

### 3. description 设计(✅ confirmed)

> ✅ confirmed (2026-08-07): 修正了"auto-deliver 被跳过"的错误措辞,直接描述行为。

**description 结构**:

```
Deliver your output to a downstream node in the graph.

The content you provide will be forwarded to the target node as input.
Delivers are accumulated — call this tool once or multiple times during your
turn; all delivers are dispatched together when your turn ends.

When NOT to use this tool:
- If you have only one downstream node, you can skip this tool — your final
  output will be automatically delivered to all downstream nodes.
- If you want to send different content to different downstream nodes, use
  this tool to explicitly choose targets.

Usage notes:
1. Call deliver with a specific target to send content only to that node.
2. Omit target to deliver to all downstream nodes (same as not calling deliver).
3. If you don't call deliver, your final output is automatically delivered
   to ALL downstream nodes (fan-out).
4. Content should be self-contained — the downstream node has no access to
   your reasoning, tool calls, or intermediate results.

Available downstream nodes:
- {target_name}: {target_description}
- ...
```

**关键设计点**:
- 明确告诉 agent "不调 deliver tool 时自动投递到所有下游"(auto-deliver 兜底行为)
- 明确告诉 agent "调 deliver 传具体 target 只投递给那个 node"
- 明确告诉 agent "调 deliver 不传 target = 所有下游(和不调一样)"
- 不提"auto-deliver 被跳过"——直接描述行为,不解释内部机制
- 动态列出可用 downstream nodes(从 store.list() 构建)

### 4. execute 路径(✅ confirmed)

> ✅ confirmed: agent 传 target_name → 内部转换为 node_id → 调 `node.deliver(GraphPayload, node_id, ctx)` 累积。

```python
class GraphDeliverTool(Tool):
    """Agent self-deliver tool——图调度中 agent 显式选择投递目标。"""

    def __init__(self, node: AgentNode, store: GraphDeliverTargetStore) -> None:
        self._node = node
        self._store = store
        super().__init__(name="deliver", parameters=_DELIVER_PARAMS, config=ToolConfig())

    @property
    def description(self) -> str:
        return self._build_description()

    def _build_description(self) -> str:
        # 见 §3 description 结构
        ...

    def get_dynamic_schema(self) -> dict[str, Any]:
        """绑 target enum 为当前可用 target 名列表。"""
        ...

    async def execute(self, **kwargs: Any) -> str:
        target_name = kwargs.get("target")       # node_name(agent 可见)
        content_str = str(kwargs.get("content", ""))

        # content 包装为 GraphPayload(ticket 11 §5)
        payload = GraphPayload(content=content_str)

        # 从 agent_context.graph_context 获取图上下文(方案 A)
        ctx = _current_agent_context()
        graph_ctx = ctx.graph_context if ctx is not None else None
        if graph_ctx is None:
            return "Error: deliver tool called outside graph context."

        if target_name is None:
            # 不传 target → deliver 到所有下游(None → _resolve_default_target)
            self._node.deliver(payload, None, graph_ctx)
            return "Delivered to all downstream nodes."

        # 校验 target_name
        target = self._store.get(target_name)
        if target is None:
            available = ", ".join(t.name for t in self._store.list())
            return f"Error: '{target_name}' is not a valid downstream node. Available: {available}"

        # name → node_id 转换(局部安全:下游不重名)
        node_id = self._store.resolve_node_id(target_name)
        self._node.deliver(payload, node_id, graph_ctx)
        return f"Delivered to '{target_name}'."
```

**关键区别**: TaskDispatchTool 委托 `service.send_async()` 走 broker/bus;GraphDeliverTool 调 `node.deliver()` 临时累积到 `_pending_delivers`(execute 期间 in-memory) → submit 后统一 dispatch → `deliver_store.accumulate()`(持久化,策略可选)。

**注**: `_current_agent_context()` 从 contextvar 获取当前 AgentContext(和 TaskDispatchTool 相同模式)。`graph_context` 字段(方案 A,在 AgentContext 上)由 BotAgentNode.execute() 设置——常规会话 `graph_context is None`,deliver tool 不在 ToolManager 中(不会调用)。

**注**: `_current_ctx` 是 BotAgentNode 在 execute 开始时设置的 GraphContext 引用,供 deliver tool 调用 `node.deliver(content, target, ctx)` 时传入。

### 5. GraphPayload 影响(✅ confirmed)

> ✅ confirmed: content 参数是 `str`(LLM 传参),execute 时包装为 `GraphPayload(content=content_str)`。

**参数定义**:
```python
_DELIVER_PARAMS: dict[str, Any] = {
    "type": "object",
    "properties": {
        "target": {
            "type": "string",
            "description": (
                "Target downstream node name. Must be one of the names listed "
                "in the tool description under 'Available downstream nodes'. "
                "Omit to deliver to all downstream nodes."
            ),
        },
        "content": {
            "type": "string",
            "description": (
                "Complete, self-contained content to deliver to the target node. "
                "The downstream node has no access to your reasoning, tool calls, "
                "or intermediate results — include all necessary context."
            ),
        },
    },
    "required": ["content"],
}
```

**注**: `target` 不是 required——omitted 时 deliver 到所有下游。

## 归属

| 层 | 内容 | 归属 |
|----|------|------|
| `GraphDeliverTarget` | deliver 目标结构体 | **modex_agent** |
| `GraphDeliverTargetStore` | 从拓扑提取下游 + name→node_id 转换 | **modex_agent** |
| `GraphDeliverTool` | agent self-deliver tool | **modex_agent** |
| `GraphToolPreset` | 拷贝 base tools + 加入 graph preset(并发安全) | **modex_agent** |
| `_DELIVER_PARAMS` | 静态参数定义 | **modex_agent** |
| BotAgentNode._ensure_deliver_tool + GraphToolPreset 替换 | 注入路径 | **bot_project**(ticket 05) |
| target_description 来源 | AgentDescriptor / GraphSpec node desc | **bot_project**(近期) / **modex_graph**(后续 GraphSpec) |

## 不做什么

- 不走 broker/bus——in-memory 累积 + submit 统一 dispatch
- 不支持 invocation_id / continuation——图节点不是 agent 通信,没有会话续接
- 不暴露 END 作为 deliver target——auto-deliver 兜底处理
- 不在 description 中解释 auto-deliver 内部机制——直接描述行为
- 不做 target_description 的 GraphSpec node 级 desc 覆盖——后续增强
- L3 修复: 统一使用 `agent_context.graph_context` 命名,不使用 `_current_ctx`
