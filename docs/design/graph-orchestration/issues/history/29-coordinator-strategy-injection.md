# Coordinator 策略注入机制

Status: triage:closed (resolved 2026-08-04)
Blocked by: none

## Question

`GraphOrchestrator` 当前用 `create_null_coordinator` 用于所有路径（`graph_orchestrator.py:221`）。`GraphRecoveryService` 同样（`graph_recovery.py:135,170`）。`ReActAgent.actual_turn` 用 Null coordinator 是有意设计（per-turn，AgentContext 持有状态）。

如何注入 Memory/SQLite coordinator 到 GraphOrchestrator 和 RecoveryService？

**ticket 22 决议更新**: coordinator 现在持有 `_instance_store: GraphInstanceStore`（不再是 `_metadata_store: GraphMetadataStore`）。coordinator factory 需要装配 `NullGraphInstanceStore` / `InMemoryGraphInstanceStore` / `SqliteGraphInstanceStore`。

选项：
1. **`create_coordinator` 工厂函数** — 加 `create_coordinator(graph_instance_id, *, backend: PersistenceBackend, connection: sqlite3.Connection | None) -> GraphPersistenceCoordinator`。根据 `PersistenceBackend` enum（`FILE` / `SQLITE` / `NULL`）选工厂。`GraphOrchestrator.__init__` 接受 `persistence_backend` 参数。
2. **`GraphOrchestrator.__init__` 接受 `PersistenceConfig`** — 定义 `PersistenceConfig` frozen Pydantic（backend enum + db_path + options）。`GraphOrchestrator` 持有 config，`create_and_run` 时用 config 创建 coordinator。
3. **`GraphInstance` 持有 coordinator factory** — `GraphInstance` 加 `coordinator_factory: CoordinatorFactory` 字段。`create_and_run` 时调用 factory 创建 coordinator。Recovery 时从 GraphInstance 获取 factory。
4. **`ConnectionManager` 模式** — 定义 `ConnectionManager` ABC，管理 per-workspace SQLite connection。`GraphOrchestrator` 接受 `ConnectionManager`，create_and_run 时获取 connection 创建 coordinator。

Recovery 路径如何重建 coordinator？需要从 DB 恢复状态，所以必须用同一 `graph_instance_id` + SQLite connection。

## Context

- `create_null_coordinator`（`persistence_coordinator.py:777-816`）: 用 `NullGraphInstanceStore` + `NullNodeStateFactory` + `NullDeliverStoreFactory` 装配（ticket 22 将 `NullGraphMetadataStore` 改为 `NullGraphInstanceStore`）
- `GraphPersistenceCoordinator.__init__`（`persistence_coordinator.py:102-126`）: 接受 `GraphInstanceStore` + `NodeStateFactory` + `DeliverStoreFactory`（ticket 22 将 `GraphMetadataStore` 改为 `GraphInstanceStore`）
- 三种实现已就绪: `Null*Factory` / `Simple*Factory` / `Sqlite*Factory`（后者接受共享 `sqlite3.Connection`）
- `PersistenceBackend` enum 已存在于 `src/modex_agent/persistence/`（`FILE` / `SQLITE`），驱动 factory selection
- `GraphOrchestrator.__init__`（`graph_orchestrator.py:142`）: 接受 `node_registry` / `state_registry` / `spec_store` / `instance_store`
- `GraphRecoveryService`（`graph_recovery.py`）: `recover_crashed` 和 `resume` 都用 `create_null_coordinator`
- `GraphOrchestrator._run_existing_instance`（`graph_orchestrator.py:304`）: 从 recovery 接收 GraphInstance（含 coordinator），调 `register_node`
- 设计文档 §9 todo #1: "Production coordinator 策略选择 — Memory / SQLite 策略的选择与注入机制需要实现，形式是 coordinator factory injection"

### ticket 33 影响（2026-08-04 决议后）

ticket 33 的决议**不影响 coordinator factory 的设计**，但影响 GraphOrchestrator 的 state 创建路径：

- **coordinator `__init__` 签名不变** — DispatchStore / conflict_detector 在 ParallelScheduler 上，不在 coordinator 上。`complete_invocation` 传参变更和 `rebuild_main_state` 简化是 coordinator 内部方法变更，不影响 `__init__` 签名。
- **`GraphOrchestrator._create_state` 变更** — ticket 33 移除 `state_factory.py`，`DynamicStateFactory(schema).create_state()`（graph_orchestrator.py:415）改为 `state_class()`。这是 GraphOrchestrator 的变更，不是 coordinator factory 的变更。coordinator factory 的选项 1-4 不受影响。
- **ReActAgent 路径不变** — ReActAgent 用 `create_null_coordinator()`（agent.py:286），不经过 GraphOrchestrator。

## Resolution（2026-08-04 grilling 裁决）

### 硬约束（用户裁决）

**持久化定义归业务层，框架只操作内存对象。** 框架（`modex_graph` + `modex_agent/orchestration`）不决定后端选择、DB 路径、连接生命周期；运行时（scheduler / coordinator / orchestrator）只面对已装配的 store / coordinator 对象工作，默认 Null / 内存实现。业务层（bot 装配代码）负责定义并装配持久化，把成品注入框架。

### 框架侧决议

1. **注入 seam：选项 3 的精神，挂在 orchestrator 而非 GraphInstance。** `modex_graph` 定义 `CoordinatorFactory` ABC + `NullCoordinatorFactory` 默认实现：
   ```python
   class CoordinatorFactory(ABC):
       @abstractmethod
       def create(
           self, graph_instance_id: int, instance_store: GraphInstanceStore
       ) -> GraphPersistenceCoordinator: ...
   ```
   `instance_store` 由 orchestrator 传入（而非 factory 闭包捕获）——保证 coordinator 与 orchestrator 持有**同一个** instance store 对象，防止 ticket 22 消灭的双权威以另一种形式复活。

   **装配故事（2026-08-04 一致性审计补齐）**：`create()` 的签名只携带 `graph_instance_id` + `instance_store`，coordinator 构造所需的另外两件——`node_state_store_factory`（`NodeStateStoreFactory.create(graph_instance_id)` 创建绑定 gid 的 store）与 `deliver_store_factory`——由 factory 实现**闭包捕获**。业务实现闭包持有 caller-owned `sqlite3.Connection`，`create()` 内装配 `SqliteNodeStateStore(conn)` + `SqliteDeliverStoreFactory(conn)` 后构造 coordinator；`NullCoordinatorFactory` 装配 `NullNodeStateStore` + `NullDeliverStoreFactory`（ReActAgent per-turn 路径）。框架在 `modex_graph/persistence/persistence_coordinator.py` 提供 `CoordinatorFactory` ABC + `NullCoordinatorFactory`（与 `create_null_coordinator` 同模块）。
2. **一个注入点。** `GraphOrchestrator.__init__` 接受 `coordinator_factory`（默认 `NullCoordinatorFactory`）；`create_and_run` 与 `GraphRecoveryService` 重建共用同一 factory——新建 + recovery 两条路径收敛，无分支。
3. **否决选项 1 / 2 / 4。** 选项 1（`PersistenceBackend` 入 orchestrator）与选项 2（`PersistenceConfig`）是「框架内定义持久化」，违反硬约束；选项 4（框架内 `ConnectionManager`）让框架管连接生命周期，同样越界。GraphInstance 持 factory 的原始选项 3 否决：factory 引用不应随实例对象流转（实例是运行时对象，factory 是装配期制品）。
4. **Store 构造契约收敛。** 全部 Sqlite store 统一为「接受 caller-owned `sqlite3.Connection`，store 永不关闭它」（把 `SqliteDeliverStore._owns_conn` 的条件所有权收敛为统一 caller-owned）。`SqliteGraphInstanceStore` 的 path-only/自开自关异类在 ticket 22 实现时一并收敛——它本就被 ticket 22 决议纳入 coordinator 的共享连接集合。
5. **`coordinator.close()` 契约**：只做逻辑清理，永不关闭业务拥有的连接。
6. **线程契约**：store 方法是同步方法、只在 event-loop 线程被调用（asyncio 单线程，同步段不交错）。业务提供的连接无需额外锁。**此契约覆盖 ticket 31c 的主体**——见 [ticket 31](31-state-machine-cas-thread-safety.md)。
7. **ReActAgent per-turn 路径保持 Null coordinator，不经注入**（有意设计，AgentContext 持有跨 turn 状态）。

### 业务层责任（bot 参考实现）

1. **DB 拓扑**：`<workspace>/.modex/graph.db`（用户裁决）。否决共用 `state.db`（stdlib 连接写入绕过 `ConnectionManager` 的 `anyio.Lock`，与 aiosqlite 写同一文件靠 busy_timeout 竞争；`MigrationRunner` 不认识图表，schema 所有权浑浊）。否决全局库（与 ADR-0023 workspace-as-unit 张力大；「GraphSpec 跨 workspace」是 spec 定义库的独立决策，生命周期不同于实例三表，不拖住实例拓扑）。
2. 业务实现自己的 `CoordinatorFactory`（闭包持有连接），连接生命周期挂 workspace materialize / evict。
3. 装配沿用现有 `app_config.persistence.backend` inline-branch 模式；v1 不加新配置字段。
4. `GraphOrchestrator` 的首次生产装配（当前零生产接线，仅测试构造）——装配点与 `WorkspacePersistenceManager` 同级（`bot/workspace/wiring/resources.py`）。

## Resolution criteria

- ✅ coordinator factory 的接口设计 — `CoordinatorFactory` ABC，`create(graph_instance_id, instance_store)`
- ✅ `GraphOrchestrator.__init__` 签名变化 — 加 `coordinator_factory`（默认 Null）
- ✅ `GraphRecoveryService` 如何重建 coordinator — 与 `create_and_run` 共用同一注入 factory
- ✅ 共享 SQLite connection 的管理 — 业务层责任；框架契约 = caller-owned，store/coordinator 永不关闭
- ✅ 与 `PersistenceBackend` enum 的关系 — 业务层 inline-branch 消费，框架不感知
- ✅ ReActAgent 路径保持 Null coordinator（有意设计，不注入）
- ✅ 与 bot 启动序列的集成 — 业务层装配点（resources.py 同级），框架只定义契约
