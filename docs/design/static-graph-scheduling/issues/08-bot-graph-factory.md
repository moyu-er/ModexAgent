# 08 — Bot graph factory

Status: ✅ design closed (直接实现设计,1 个待确认项: workspace 驱逐时图处理)
Labels: wayfinder:active
Blocking: 09-webui-graph-control-api

## Question

**GraphOrchestrator + SQLite coordinator 在 bot 层 per-workspace 装配。**

### 上下文(基于代码探索确认)

**参考: assemble_sqlite_orchestrator**(recovery_scanner.py):
- 现成的 GraphOrchestrator wiring 模板
- SqliteCoordinatorFactory on workspace connection
- GraphSpecStore + GraphInstanceStore on workspace SQLite

**PoolWorkspaceResources**(resources.py):
- `@dataclass`,持有 pools / persistence / broker 等
- `_assemble_resources` 构造顺序: persistence open → PoolStore scan → broker/interceptor → pools 构造 → resolver_cell.set(resources)
- `_stop_resources` 清理顺序

**WorkspaceResolverCell**(handle.py):
- late-binding: `cell.set(resources)` after assembly
- `resolve_workspace()` lazily 获取当前 workspace resources
- BotAgentNodeFactory 用同一模式引用 pools(ticket 05 §3.10)

**BotAgentNodeFactory**(ticket 05 §3.10):
- 持有 `workspace_resolver: WorkspaceResolverCell`
- `create(spec)` 从 `spec.config` 读 agent_name/pool_name → 创建 BotAgentNode
- 需要 pools 已装配才能 resolve

## Discussion

### 1. PoolWorkspaceResources 加字段

```python
@dataclass
class PoolWorkspaceResources:
    ...
    graph_orchestrator: GraphOrchestrator | None = None  # 新增
    graph_output_adapter: GraphOutputAdapter | None = None  # 新增(ticket 11 §6)
```

### 2. 装配顺序

```python
# _assemble_resources 中,pools 构造后:

# ── Graph orchestrator (BL-13) ─────────────────────────────────────────
# NodeRegistry 注册 node types(BotAgentNodeFactory 需要 workspace_resolver)
node_registry = NodeRegistry()
node_registry.register("agent", BotAgentNodeFactory(workspace_resolver_cell))
node_registry.register("function", FunctionNodeFactory())
node_registry.register("delay", DelayNodeFactory())
node_registry.register("human_input", HumanInputNodeFactory())
node_registry.register("graph", GraphAsNodeFactory())

# state_classes: graph state 类型映射
state_classes = {
    "default": DefaultGraphState,  # bot 层默认 state
}

# GraphSpecStore + GraphInstanceStore on workspace SQLite
graph_spec_store = SqliteGraphSpecStore(persistence.connection)
graph_instance_store = SqliteGraphInstanceStore(persistence.connection)

# SqliteCoordinatorFactory on workspace connection
coordinator_factory = SqliteCoordinatorFactory(
    connection=persistence.connection,
    node_state_store_factory=SqliteNodeStateStoreFactory(...),
    deliver_store_factory=SqliteDeliverStoreFactory(...),
)

# output adapter(ticket 11 §6)
output_adapter = WebUIGraphOutputAdapter(ws_broadcaster)

# GraphOrchestrator
graph_orchestrator = GraphOrchestrator(
    node_registry=node_registry,
    state_classes=state_classes,
    spec_store=graph_spec_store,
    instance_store=graph_instance_store,
    coordinator_factory=coordinator_factory,
    output_adapter=output_adapter,  # ticket 11 §6 新增参数
)

# 加载 YAML GraphSpec(ticket 04)
# H10 修复: GraphSpecLoader 需定义——扫描 config/graphs/*.yml,parse 到 GraphSpec,save 到 GraphSpecStore
graph_spec_loader = GraphSpecLoader(graph_spec_store)  # TODO: 定义此类(bot_project)
graph_spec_loader.load_from_dir(config_dir / "graphs")

resources.graph_orchestrator = graph_orchestrator
resources.graph_output_adapter = output_adapter
```

### 3. 清理顺序

> **M3 修复** (2026-08-08): _stop_resources 用 try/finally 保证所有步骤执行。

```python
# _stop_resources 中(M3: try/finally 保证清理):
try:
    # 1. pools 停止
    await _stop_pools(resources)
finally:
    try:
        # 2. graph orchestrator cleanup(M8: 先 pause active)
        if resources.graph_orchestrator is not None:
            await resources.graph_orchestrator.pause_all_active()
            await resources.graph_orchestrator.cleanup()
    finally:
        # 3. persistence 关闭(必须最后,stores 依赖 connection)
        if resources.persistence is not None:
            await resources.persistence.close()
```

### 4. workspace 驱逐时正在运行的图处理(待确认)

> **M8 修复** (2026-08-08): _stop_resources 中优雅 pause 所有 active instances,不直接取消 task。

LRU 驱逐 workspace 时,pool 会 stop,正在运行的 graph instance 怎么办?

**设计**: 在 `_stop_resources` 中,graph_orchestrator.cleanup() 前先 pause 所有 active instances:
```python
# _stop_resources 中:
if resources.graph_orchestrator is not None:
    # M8: 优雅 pause 所有 active instances(不直接取消 task)
    await resources.graph_orchestrator.pause_all_active()
    await resources.graph_orchestrator.cleanup()
```

`pause_all_active()` 遍历 `_active_instances`,对 RUNNING 实例调 `pause()`,让节点完成当前 execute 后暂停。后续 workspace 重新加载时通过 RecoveryScanner 恢复。

### 5. GraphOrchestrator 构造参数变更

ticket 11 §6 新增 `output_adapter: GraphOutputAdapter` 参数。GraphOrchestrator 需要修改构造函数:

```python
class GraphOrchestrator:
    def __init__(
        self,
        *,
        node_registry: NodeRegistry,
        state_classes: Mapping[str, type[GraphState]],
        spec_store: GraphSpecStore,
        instance_store: GraphInstanceStore,
        coordinator_factory: CoordinatorFactory = _NULL_COORDINATOR_FACTORY,
        output_adapter: GraphOutputAdapter | None = None,  # 新增(ticket 11 §6)
    ) -> None:
```

图完成/崩溃时调 `await self._output_adapter.emit(output)`(如果 adapter 非 None)。

### 6. create_and_run 签名变更

ticket 11 §1 新增 `user_input: GraphPayload | None = None` 参数:

```python
async def create_and_run(
    self,
    spec_id: int,
    *,
    user_input: GraphPayload | None = None,  # 新增(ticket 11 §1)
    initial_state: GraphState | None = None,
    parent_instance_id: int | None = None,
) -> int:
```

## 归属

| 层 | 内容 | 归属 |
|----|------|------|
| `PoolWorkspaceResources.graph_orchestrator` | 新字段 | **bot_project** |
| `PoolWorkspaceResources.graph_output_adapter` | 新字段 | **bot_project** |
| NodeRegistry 注册 + BotAgentNodeFactory | node types 注册 | **bot_project** |
| GraphSpecStore + GraphInstanceStore | workspace SQLite | **bot_project**(wiring) |
| SqliteCoordinatorFactory | workspace connection | **bot_project**(wiring) |
| WebUIGraphOutputAdapter | WebSocket 推送 | **bot_project** |
| GraphSpecLoader.load_from_dir | YAML 加载(ticket 04) | **bot_project** |
| GraphOrchestrator 构造参数变更 | 加 output_adapter + user_input | **modex_agent** |
| _assemble_resources / _stop_resources | 装配/清理 | **bot_project** |

## 待确认

1. **workspace 驱逐时图处理**: 当前简单处理(不主动停止,task 被取消),后续增强(优雅 pause)。是否可接受?

## 不做什么

- 不做 workspace 驱逐时的优雅图停止——后续增强
- 不在框架层内置 per-workspace 装配——bot 层 wiring
- 不做 GraphSpec 的在线加载热更新——启动时加载,后续增强
