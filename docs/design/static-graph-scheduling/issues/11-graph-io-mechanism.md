# 11 — 图调度输入/输出机制

Status: ✅ design closed (all 6 items confirmed)
Labels: wayfinder:active
Blocking: 08-bot-graph-factory, 09-webui-graph-control-api

## Question

**用户触发图执行时,输入怎么进入图?图执行完成后,结果怎么回流给用户?**

Ticket 01-04 设计了节点间通信(deliver),但**图的初始输入和最终输出**是空白——端到端的入口和出口没设计。

### 上下文(基于代码探索确认)

**当前 create_and_run**:
```python
async def create_and_run(self, spec_id: int, *, initial_state: GraphState | None = None, ...) -> int:
    spec = self._load_spec(spec_id)
    compiled = self._compiler.compile(spec)
    graph_instance_id = default_id_generator().generate()
    ...
    await self._execute(instance, compiled, state)
    return graph_instance_id
```
- `initial_state` 是 GraphState 对象,不是用户输入文本
- 返回 `graph_instance_id`(int),不是图执行结果
- 第一个 node(entry node)的 `IntegratedInput` 是空的——没有上游 deliver

**当前 route_deliver 对 END 的处理**(persistence_coordinator.py:139-175):
```python
def route_deliver(self, target_node, content, source_node, source_invocation_id) -> int | None:
    if target_node == GraphNode.END:
        return None  # ← END 无 deliver_store, delivers 丢失!
```
- 终端节点 deliver 到 `__END__` 的内容被**静默丢弃**
- 图的"输出"目前只能通过 `ctx.state.result`(终端节点写入 state)+ `GraphEngine.run_async(ctx)` 返回 `ctx.state` 读取
- 多终端节点的图(如 map-reduce:多个 worker 都 deliver 到 END)无法聚合输出

**当前 react 的 StartNode / EndNode**(react 特化实现):
- `StartNode`(start.py:14-39): 接收空 IntegratedInput,`deliver(None, ReActNode.LLM, ctx)` 硬编码路由到 LLM node
- `EndNode`(end.py:20-86): 构建 AgentResult,写 `state.result`,`self.deliver(result, GraphNode.END, ctx)` deliver 到 END(被丢弃)

## Discussion

### 1. 图输入: START 节点接收 user_input 并 deliver 分发

> ✅ **方向确认** (2026-08-07) + **修正** (2026-08-08): START/END 始终实例化为 Node,默认用框架基类,GraphSpec 可覆盖。不做向后兼容。

**设计**: 用户输入由 `create_and_run(user_input=...)` 传入,存到 `GraphContext`。START 节点的 `execute()` 从 ctx 读取 user_input,自己决定 deliver 分发。

**START/END 实例化规则**:
- **始终实例化**: 所有 graph 有且仅有一个 START 和一个 END Node 实例
- **默认**: GraphSpec 未显式定义时,用框架基类 `StartNode` / `EndNode`(modex_graph)
- **自定义**: GraphSpec 可显式定义 START/END 的 NodeSpec(node_type + config),业务继承重写
- **不做向后兼容**: sentinel 常量模式(graph.py 当前的 `GraphNode.START` 作为边端点但无 Node 实例)废除

**框架层 START 基类**(modex_graph):
```python
class StartNode(Node[S]):
    """图入口节点——接收 user_input,deliver 分发到下游。

    默认实现: 从 ctx 读取 user_input,deliver(None, ctx) → 所有下游。
    业务可继承重写: 路由到特定下游 / fan-out / 加工内容 / 条件分发。
    """

    def __init__(self) -> None:
        self.name = GraphNode.START

    async def execute(self, ctx: GraphContext[S], integrated_input: IntegratedInput) -> None:
        user_input = ctx.user_input  # GraphPayload | None
        if user_input is not None:
            # 默认 fan-out: None → _resolve_default_target → 所有下游
            self.deliver(user_input, None, ctx)
        return None
```

**GraphContext 加 `user_input` 字段**(context.py):
```python
class GraphContext(Generic[S]):
    def __init__(self, *, ..., user_input: GraphPayload | None = None) -> None:
        ...
        self.user_input: GraphPayload | None = user_input
```

**create_and_run 传参**:
```python
async def create_and_run(
    self,
    spec_id: int,
    *,
    user_input: GraphPayload | None = None,  # 新增
    ...
) -> int:
    ...
    ctx = GraphContext(state=state, runtime=..., coordinator=..., user_input=user_input)
    await self._execute(instance, compiled, ctx)
```

**业务自定义路由示例**:
```python
class RouterStartNode(StartNode):
    """业务层: 根据用户输入内容路由到不同 agent。"""

    async def execute(self, ctx: GraphContext[S], integrated_input: IntegratedInput) -> None:
        user_input = ctx.user_input
        if user_input is None:
            return
        content = user_input.content
        edges = self._graph_ref.edges_from(self.name)
        
        # 根据 content 路由
        if "代码" in content:
            target_name = "coder"
        elif "研究" in content:
            target_name = "researcher"
        else:
            self.deliver(user_input, None, ctx)  # 默认 fan-out
            return
        
        target_id = self._graph_ref.nodes[target_name].node_id
        self.deliver(user_input, target_id, ctx)
```

**GraphSpec 中自定义 START**:
```yaml
nodes:
  - name: __START__
    node_type: router_start
    config:
      routes:
        - keywords: ["代码", "bug"]
          target: coder
        - keywords: ["研究", "分析"]
          target: researcher
  - name: coder
    node_type: agent
    config: { agent: coder, pool: default }
  - name: researcher
    node_type: agent
    config: { agent: researcher, pool: default }
edges:
  - { source: __START__, target: coder }
  - { source: __START__, target: researcher }
  - { source: coder, target: __END__ }
  - { source: researcher, target: __END__ }
```

**优势**:
- START 节点是可继承的——业务可以 fan-out / 加工 / 条件分发
- user_input 存在 GraphContext 中,不注入为 deliver(START 自己决定怎么分发)
- 和 deliver/submit 模型一致(START 是图的第一个 node,用 deliver 分发到下游)

### 2. 图输出: END 节点收集 delivers,可继承,不丢弃

> ✅ **方向确认** (2026-08-07) + **修正** (2026-08-08): END 始终实例化,默认用框架基类,GraphSpec 可覆盖。

**设计**: END 是真正的 Node 实例(有 node_id + deliver_store + execute)。`route_deliver(target=END)` 正常 accumulate,不特殊处理。END 的 `execute()` 消费 delivers,聚合,写 `state.result`。

**框架层 END 基类**(modex_graph):
```python
class EndNode(Node[S]):
    """图终端节点——收集所有 deliver 到 END 的内容,聚合为图结果。

    默认实现: 消费 delivers → 聚合为 list → 写 state.result。
    业务可继承重写: 合并/转换/筛选/格式化。
    """

    def __init__(self) -> None:
        self.name = GraphNode.END

    async def execute(self, ctx: GraphContext[S], integrated_input: IntegratedInput) -> None:
        results = [p.content for p in integrated_input.payloads]
        ctx.state.result = results  # 写 state.result(list 格式)
        return None
```

> **H8 修复** (2026-08-08): `ctx.state.result` 需要 GraphState 基类有 `result` 字段。当前 GraphState 只有 `resume_target + checkpoint()`。需在 GraphState 基类(或 DefaultGraphState)加 `result: list[GraphPayload] | None = None` 字段。这是 ADR-0036 的连带影响。

**route_deliver 不再特殊处理 END**(persistence_coordinator.py):
```python
def route_deliver(self, target_node, content, source_node, source_invocation_id) -> int | None:
    # END 是注册的 Node,有 deliver_store,正常 accumulate
    store = self._deliver_stores.get(target_node)
    if store is None:
        raise RoutingError(f"Node {target_node!r} has no deliver_store registered.")
    return store.accumulate(...)
```

**关键变更**:
- END 节点走 NodeRegistry.create(有 node_id),`coordinator.register_node(end_node.node_id)` 正常注册 deliver_store
- `route_deliver(target=END_node_id)` 正常 accumulate,不再返回 None
- END 节点的 `Node.run()` 在 integrate 阶段调 `collect_consumable_delivers` → 拿到所有 deliver 到 END 的内容 → `execute()` 聚合

**多终端节点**: 多个 node 都 deliver 到 END → END 的 deliver_store accumulate 多条 → END 的 execute 聚合为 list。自然支持 map-reduce 模式。

**GraphSpec 中自定义 END**:
```yaml
nodes:
  - name: __END__
    node_type: custom_end
    config: { format: "summary" }
```

### 2.1 GraphSpecCompiler 修正(START/END 实例化)

> **修正** (2026-08-08): compiler 始终创建 START/END Node 实例。

**当前行为**(spec_compiler.py:92-134): 只遍历 `spec.nodes` 创建 Node,不创建 START/END。START/END 是 sentinel 常量(边的特殊端点),无 Node 实例。

**修正后行为**:
```python
def compile(self, spec: GraphSpec) -> CompiledGraph[Any]:
    graph = Graph(name=spec.name)

    # 1. 确保 START/END 节点存在
    start_spec = self._find_node(spec, GraphNode.START)
    end_spec = self._find_node(spec, GraphNode.END)

    # 未显式定义 → 用默认 NodeSpec
    if start_spec is None:
        start_spec = NodeSpec(name=GraphNode.START, node_type="start")
    if end_spec is None:
        end_spec = NodeSpec(name=GraphNode.END, node_type="end")

    # 2. 创建所有 Node(含 START/END)
    for node_spec in [start_spec, end_spec] + list(spec.nodes):
        if node_spec.name in (GraphNode.START, GraphNode.END):
            node = self._node_registry.create(node_spec)
            graph.add_node(node_spec.name, node)

    for node_spec in spec.nodes:
        node = self._node_registry.create(node_spec)
        graph.add_node(node_spec.name, node)

    # 3. 添加 edges(不变)
    for edge in spec.edges:
        graph.add_edge(edge.source, edge.target)

    # 4. validate + compile(不变)
    ...
```

**NodeRegistry 注册默认 start/end**:
```python
node_registry.register("start", DefaultStartNodeFactory())
node_registry.register("end", DefaultEndNodeFactory())
```

**GraphSpec 结构校验更新**(spec.py):
- 显式定义的 START/END NodeSpec 的 `name` 必须是 `"__START__"` / `"__END__"`
- 显式定义的 START/END 的 `node_type` 必须在 NodeRegistry 中注册
- 不允许 `spec.nodes` 中出现 `name == "__START__"` 或 `"__END__"` 之外的重复

**Graph.compile() 修正**(graph.py):
- START/END 不再是 sentinel 常量,而是注册的 Node 实例
- `entry_node` 检查: `edges_from(GraphNode.START)` 的第一条边的 target
- `terminal` 检查: `edges_to(GraphNode.END)` 的 source
- 不再做 "START/END not in nodes" 的排除(它们现在在 nodes 中)

### 3. END delivers 持久化

> ✅ **确认** (2026-08-07): END 正常走 deliver_store 持久化,和其他 node 完全一样。统一路径,不特殊处理(收敛规则)。

**设计**: END 的 deliver 消费状态机和普通 node 一致(PENDING → CONSUMED_PENDING → CONSUMED)。

**崩溃恢复行为**:
- END 的 invocation 已 COMPLETED → delivers 已 promote(CONSUMED),不会重复消费
- END 的 invocation 是 crashed → 恢复时重新 run,`collect_consumable_delivers` 拿到未消费的 delivers

### 4. 后台执行 + 输出回流

> ✅ **确认** (2026-08-07): 图点击触发,后台异步执行。前端查看图执行状态 + 各节点情况,点 agentNode 可跳转到相关 session。

**执行模型**:
- `create_and_run` 本身是 async,bot 层 REST handler 用 `asyncio.create_task` 调用
- HTTP 请求立即返回 `graph_instance_id`
- GraphOrchestrator 不内置后台机制——由调用方(bot 层)决定
- 图后台异步自己执行,不阻塞 HTTP 请求

**前端交互**(bot 业务层实现):
- 图执行状态: running / completed / crashed,前端可查询
- 各节点情况: 节点状态(PENDING / RUNNING / COMPLETED / CRASHED),前端可视化
- agentNode 跳转: 点击 agentNode → 跳转到该 node 的 session(session_id = `nodeId.agentName`, ticket 05 §3.4)→ 查看 agent 对话历史 / transcript

**HTTP 触发**:
```
POST /api/graphs/{name}/run
  body: { "user_input": { "content": "分析代码库结构" } }
  response: { "graph_instance_id": 123456789, "status": "running" }
```

**图执行中**: 各 node 的 agent 事件通过 WebBotEmitter 推送到 WebSocket(复用 pool 的 emitter 路径,ticket 05)。

**图完成/崩溃**: 通过 GraphOutputAdapter(§6)推送给上游。

**REST 查询**:
```
GET /api/graphs/instances/{graph_instance_id}
  response: { "status": "running|completed|crashed", "result": [...], "nodes": [...] }
```

### 5. user_input 类型: GraphPayload 结构体

> ✅ **确认** (2026-08-07): user_input 和 deliver content 统一用 `GraphPayload` 结构体,不裸 `str`。存量数据类型加强,便于后续增/改字段。

**设计**: 新增 `GraphPayload`(frozen Pydantic BaseModel),当前只有 `content: str`。user_input 和 deliver content 都用它。

```python
# modex_graph/integration.py (或新文件)
class GraphPayload(BaseModel):
    """图调度中的数据载体——user_input 和 deliver content 的统一结构。

    当前只有 content 字段。结构体化以便后续扩展
    (如 metadata / priority / content_type 等),不破坏序列化兼容。
    """
    content: str

    model_config = ConfigDict(frozen=True, extra="forbid")
```

**影响范围**(breaking change):
- `create_and_run(user_input: GraphPayload | None = None)` —— ticket 11
- `Node.deliver(content: GraphPayload, ...)` —— 从 `Any` 改为 `GraphPayload`
- `route_deliver(content: GraphPayload, ...)` —— 同上
- `IntegratedPayload.content` —— 从 `Any` 改为 `GraphPayload`
- `deliver_store.accumulate(content: GraphPayload)` —— 序列化用 `model_dump()`,反序列化用 `model_validate()`

**连带影响记录**: deliver content 类型变更是 ticket 01(已 closed)的 breaking change。作为 ticket 11 的连带影响记录 + 在 ADR-0036(node_id breaking change)中一起追踪,不单独创建 ticket。两者都是 breaking change,可以一起 migration。

**归属**: `GraphPayload` 放 **modex_graph**(deliver 是 modex_graph 的概念)。

**START 节点用法**:
```python
async def execute(self, ctx: GraphContext[S], integrated_input: IntegratedInput) -> None:
    user_input = ctx.user_input  # GraphPayload | None
    if user_input is not None:
        self.deliver(user_input, entry_node, ctx)  # deliver GraphPayload
```

**AgentNode 的 IntegratedInput 消费**: `_format_integrated_input` 从 `payload.content.content`(GraphPayload.content)提取文本格式化为 system-reminder。

### 6. output adapter: GraphOutputAdapter ABC + 结构体化

> ✅ **确认** (2026-08-07): 设计简单 ABC + frozen Pydantic 结构体 + 常量,不硬编码 dict/字符串。

**设计**: 图结果汇报层——END 节点聚合结果到 `state.result` 后,adapter 转换给上游消费方。和输入(GraphPayload)对称。

**结构体 + 常量**(modex_graph):
```python
# modex_graph/output_adapter.py
from enum import StrEnum
from pydantic import BaseModel, ConfigDict
from typing import Any

class GraphOutputKind(StrEnum):
    """图输出事件类型。"""
    COMPLETED = "graph_completed"
    CRASHED = "graph_crashed"

class GraphOutput(BaseModel):
    """图执行结果的结构化载体。

    由 adapter 构造,传递给上游消费方(WebSocket / REST / CLI)。
    """
    kind: GraphOutputKind
    graph_instance_id: int
    result: Any = None         # COMPLETED: END 节点聚合的 state.result
    error: str | None = None   # CRASHED: 错误信息

    model_config = ConfigDict(frozen=True, extra="forbid")

class GraphOutputAdapter(ABC):
    """图结果输出适配器——把图执行结果转换给上游消费方。

    图调度的结果汇报层。GraphOrchestrator 在图完成后构造
    ``GraphOutput``,调用 adapter 转换并传递给上游。
    """

    @abstractmethod
    async def emit(self, output: GraphOutput) -> None:
        """转换图结果并传递给上游。

        Args:
            output: 结构化的图输出(GraphOutput)
        """
        ...
```

**GraphOrchestrator 用法**:
```python
# 图完成
output = GraphOutput(
    kind=GraphOutputKind.COMPLETED,
    graph_instance_id=graph_instance_id,
    result=state.result,
)
await self._output_adapter.emit(output)

# 图崩溃
output = GraphOutput(
    kind=GraphOutputKind.CRASHED,
    graph_instance_id=graph_instance_id,
    error=str(e),
)
await self._output_adapter.emit(output)
```

**bot 层实现**:
```python
class WebUIGraphOutputAdapter(GraphOutputAdapter):
    async def emit(self, output: GraphOutput) -> None:
        await self._ws.broadcast(output.model_dump())
```

**归属**: `GraphOutput` / `GraphOutputKind` / `GraphOutputAdapter` 放 **modex_graph**(和 GraphPayload / StartNode / EndNode 一起构成图的完整 I/O)。

**GraphOrchestrator 持有 adapter**: 构造时注入 `output_adapter: GraphOutputAdapter`,图完成后调 `await adapter.emit(output)`。

**后续扩展提示**: `GraphOutputKind` 可加 `PAUSED` / `RESUMED` 等事件类型;`GraphOutput` 可加 `progress` / `node_states` 等字段。本期只做 `COMPLETED` + `CRASHED`。

## 归属

| 层 | 内容 | 归属 |
|----|------|------|
| `GraphPayload` 结构体 | user_input 和 deliver content 的统一载体 | **modex_graph** |
| `StartNode` / `EndNode` 基类 | 默认实现(fan-out / 聚合) | **modex_graph** |
| GraphSpec 支持自定义 START/END | 显式 NodeSpec(node_type + config) | **modex_graph**(GraphSpec + Compiler) |
| GraphSpecCompiler 始终创建 START/END | 未显式定义时用默认基类 | **modex_graph** |
| `GraphContext.user_input` 字段 | 存 GraphPayload | **modex_graph** |
| `route_deliver` END 不再丢弃 + content 类型改 GraphPayload | 去掉 `if END: return None` + `Any` → `GraphPayload` | **modex_graph**(GraphPersistenceCoordinator) |
| END 节点正常 `register_node` | END 有 deliver_store(用 END 的 node_id 注册) | **modex_agent**(GraphOrchestrator,注册时包含 END) |
| `create_and_run(user_input=)` 签名 | 传 GraphPayload 到 GraphContext | **modex_agent**(GraphOrchestrator) |
| deliver content `Any` → `GraphPayload` | breaking change,连带影响 ticket 01 | **modex_graph**(在 ADR-0036 一起追踪) |
| react StartNode/EndNode | 继承 modex_graph 基类,react 特化 | **modex_agent**(react) |
| 业务自定义 START/END | 继承重写 | **bot_project** |
| `GraphOutput` + `GraphOutputKind` + `GraphOutputAdapter` | 图结果汇报层 ABC + 结构体 + 常量 | **modex_graph** |
| GraphOrchestrator 持有 output_adapter | 构造注入,图完成后 emit | **modex_agent**(GraphOrchestrator) |
| WebUIGraphOutputAdapter | bot 层实现,WebSocket 推送 | **bot_project** |
| 后台执行 + WebSocket 推送 | asyncio.create_task + WebSocket 事件 | **bot_project**(REST handler + WebUI) |
| REST 端点 | POST /api/graphs/{name}/run + GET /api/graphs/instances/{id} | **bot_project**(GraphConfigController) |

## 待确认

1. ~~**§3 END delivers 持久化**~~: ✅ confirmed — END 正常走 deliver_store 持久化,统一路径
2. ~~**§4 后台执行策略**~~: ✅ confirmed — bot 层 asyncio.create_task,图后台异步执行,前端查看状态+跳转 session
3. ~~**§5 user_input 类型**~~: ✅ confirmed — `GraphPayload` 结构体(frozen Pydantic),user_input 和 deliver content 统一,连带影响 ticket 01,在 ADR-0036 一起追踪
4. ~~**§6 output adapter**~~: ✅ confirmed — `GraphOutputAdapter` ABC + `GraphOutput`(frozen Pydantic) + `GraphOutputKind`(StrEnum),放 modex_graph

## 不做什么

- 不做图执行的 HTTP 长轮询——用 WebSocket 推送
- 不在 GraphOrchestrator 内置后台 task 机制——由调用方决定同步/异步
- 不特殊处理 END 的 deliver 消费——统一走 deliver_store 路径(收敛规则)
- 不做 GraphOutputKind 的 PAUSED/RESUMED 事件类型——后续扩展
- 不做 output adapter 的 progress/node_states 字段——后续扩展
