# Metadata store 双权威收敛

Status: triage:closed
Blocked by: none
Resolved: 2026-08-04

## Question

`GraphInstanceStore`（列级存储，丰富 query API：`load_by_status` / `load_by_parent` / `delete`）与 `GraphMetadataStore`（JSON 整体存储，全字段保真，简单 API：`save` / `load` / `update_status`）目前是双权威，互不通信。`GraphOrchestrator` / `GraphControlService` / `GraphRecoveryService` 用前者，`coordinator` 用后者。即使接 SQLite coordinator，两者会静默分歧（orchestrator 写 `graph_instances` 表，coordinator 读 `graph_metadata` 表）。

收敛方向是什么？

选项：
1. **合并到 `GraphInstanceStore`** — 给它加全字段存储（`metadata_json` 列），让 coordinator 接受 `GraphInstanceStore` 而非 `GraphMetadataStore`。删除 `GraphMetadataStore` + 3 实现。保留丰富 query API。
2. **合并到 `GraphMetadataStore`** — 给它加 query API（`load_by_status` / `load_by_parent` / `delete`），让 orchestrator/control/recovery 改用 `GraphMetadataStore`。删除 `GraphInstanceStore` + 2 实现。保留全字段保真。
3. **保留两者 + 同步机制** — coordinator 写 `GraphMetadataStore` 后同步到 `GraphInstanceStore`（或反之）。引入同步复杂度但保持各自优势。

收敛后的 store 应该被 coordinator 还是 orchestrator 持有？`GraphOrchestrator` 当前在 `__init__` 接受 `instance_store` — 收敛后是否改为接受 coordinator factory？

## Context

- 设计文档 §9.1 item 1 明确标记此为待办决策
- 原始设计 doc §7.2: "GraphInstanceStore → 演进为 GraphMetadataStore（存可序列化 metadata，不存运行时 GraphInstance）" — 设计意图是合并，但实现保留了两套
- `GraphInstanceStore` 表 `graph_instances`：列级存储（spec_id, parent_instance_id, parent_node, status, created_at, updated_at），scheduler bookkeeping 字段（instance_seq, iteration_count, activated_sources, pending_dispatches）用默认值填充
- `GraphMetadataStore` 表 `graph_metadata`：`metadata_json` 整体序列化（`GraphMetadata.model_dump_json()`），全字段保真
- `SqliteGraphInstanceStore` 和 `SqliteGraphMetadataStore` 各自持有独立 SQLite connection
- `GraphPersistenceCoordinator.__init__` 接受 `GraphMetadataStore`，不感知 `GraphInstanceStore`
- 收敛规则 1: "收敛而非新增并行路径"

## Resolution criteria

明确：
- 收敛到哪个 store（或新合并 store 的设计）
- 被删除的 store + 实现列表
- 收敛后 store 的持有者（coordinator vs orchestrator）
- `GraphOrchestrator.__init__` 签名变化
- 表 schema 变化（是否需要迁移）
- 对 Recovery 路径的影响

## Resolution

### 设计哲学

**GraphInstance 是核心设计概念，GraphMetadata 是它的可序列化投影。** GraphInstance 由 ticket 04 引入为"新增核心抽象"，C2 决策让它从 frozen Pydantic 演进为运行时 class（持有非序列化的 coordinator），才把可序列化部分提取为 GraphMetadata。store 存的永远是 GraphMetadata（因为 GraphInstance 不可序列化），但 store 服务的是 GraphInstance 的生命周期。

### 真实状态（subagent 确认）

不是"双权威互不通信"——是**单写 + 零读**：
- `GraphInstanceStore` 是实际生效的 store（6 个生产 call site：orchestrator 3 + control 2 + recovery 1），承担 create + status 转换 + recovery 查询
- `GraphMetadataStore` 在生产中**完全空转**：`save()` 零 caller，`update_status()` 零 caller（`GraphInstance.update_status()` 方法存在但无人调用），`load()` 永远返回 None（因为从不 save）
- status 写入有两条不收敛的路径：Path A（直接 store，orchestrator/control/recovery 用）是活跃的；Path B（经 coordinator，`GraphInstance.update_status` → `coordinator.update_graph_status` → `metadata_store.update_status`）是死路径

### 决策

**`GraphInstanceStore` 吸收全字段保真能力，`GraphMetadataStore` 删除。**

以 GraphInstance 为核心概念，store 服务于它的生命周期。GraphInstanceStore 是活跃的 store，吸收空转 store 的全字段保真能力。不改名为 GraphMetadataStore——store 命名应反映它服务的核心概念（GraphInstance 生命周期），而非存储格式（GraphMetadata 值对象）。

### 目标 schema

> **2026-08-04 再修订（ticket 32 闭环）**：原「混合存储」决议中的 `bookkeeping_json` 列作废——ticket 32 裁决 bookkeeping 四字段是三层体系的运行时视图，永不持久化。最终 schema 为**纯列存储**：

固有字段（identity + lifecycle）列级存储：

```sql
CREATE TABLE IF NOT EXISTS graph_instances (
    graph_instance_id   BIGINT PRIMARY KEY,
    spec_id             BIGINT NOT NULL,
    parent_instance_id  BIGINT,
    parent_node         TEXT,
    status              TEXT NOT NULL DEFAULT 'running'
                        CHECK (status IN ('running','paused','stopped',
                                          'crashed','completed','failed')),
    created_at          INTEGER NOT NULL,
    updated_at          INTEGER NOT NULL
);
```

- `update_status` 是纯列级单条 UPDATE — 最高频操作最轻量
- `load_by_status` / `load_by_parent` 用普通索引
- 无数据迁移 — 当前无真实数据，直接改表
- ~~`bookkeeping_json` 零 schema 迁移~~ — 作废：无 bookkeeping 字段需要持久化（ticket 32）

### 目标 ABC

```python
class GraphInstanceStore(ABC):
    """Workspace-level store for GraphInstance's serializable projection.
    
    Serves GraphInstance lifecycle: create, status transitions, recovery
    queries. Stores GraphMetadata (the serializable value object), NOT
    the runtime GraphInstance (which holds a non-serializable coordinator).
    """
    def save(self, metadata: GraphMetadata) -> None: ...           # 全字段 UPSERT
    def load(self, graph_instance_id: int) -> GraphMetadata | None: ...  # 全字段读取（改名自 load_by_id）
    def update_status(self, graph_instance_id: int, status: GraphInstanceStatus) -> None: ...  # 纯列级 UPDATE
    def load_by_status(self, status: GraphInstanceStatus) -> list[GraphMetadata]: ...
    def load_by_parent(self, parent_instance_id: int) -> list[GraphMetadata]: ...
    def delete(self, graph_instance_id: int) -> None: ...
```

变化：`save` 存全字段（不再丢弃 bookkeeping）；`load_by_id` 改名 `load`；`update_status` / `load_by_status` 接受 enum（不再 `.value`）；新增 `NullGraphInstanceStore`。

### 三实现

| 实现 | 特征 | 用途 |
|------|------|------|
| `NullGraphInstanceStore` | 全 no-op, `load` 返回 None | ReActAgent per-turn（新增） |
| `InMemoryGraphInstanceStore` | dict, `GraphMetadata` 整体存 | 测试 / 单进程临时图（已存在，改 `load_by_id` → `load`） |
| `SqliteGraphInstanceStore` | 混合存储（列级 + JSON 列） | 生产（已存在，改 schema 加 `bookkeeping_json`） |

### 收敛 status 写入路径

删除 Path B（coordinator 经介的死路径）：
- 删除 `coordinator.update_graph_status()` 方法
- 删除 `GraphInstance.update_status()` 方法
- 所有 status 写入统一走 Path A：orchestrator/control/recovery 直接调 `instance_store.update_status`

### 接线变化

四者（orchestrator / control / recovery / coordinator）共享同一个 `GraphInstanceStore` 实例：
- `GraphOrchestrator.__init__(instance_store: GraphInstanceStore)` — 不变（已接受此类型）
- `GraphControlService.__init__(instance_store: GraphInstanceStore)` — 不变
- `GraphRecoveryService.__init__(instance_store: GraphInstanceStore)` — 不变
- `GraphPersistenceCoordinator.__init__(instance_store: GraphInstanceStore)` — **替代 `graph_metadata_store: GraphMetadataStore`**

coordinator 的 `load_for_recovery` / `get_graph_state` 从同一个 store 读取——数据一致。

### 删除

- `GraphMetadataStore` ABC + `NullGraphMetadataStore` + `MemoryGraphMetadataStore` + `SqliteGraphMetadataStore`
- `graph_metadata_store.py` 文件
- `graph_metadata` 表（无数据迁移）
- `coordinator.update_graph_status()` 方法
- `GraphInstance.update_status()` 方法
- `create_null_coordinator` 中的 `NullGraphMetadataStore` 替换为 `NullGraphInstanceStore`

### 关联 gap（已由 ticket 32 关闭）

~~store 现在能存全字段 GraphMetadata（`bookkeeping_json`），但**谁写** scheduler bookkeeping 仍然是独立 gap~~ — [ticket 32](32-scheduler-bookkeeping-persistence.md) 已裁决：**没有人写**。bookkeeping 四字段是运行时视图（recovery 时从三层 store 重建/派生），永不持久化。`bookkeeping_json` 列与 `GraphMetadata` 的 4 个 bookkeeping 字段一并移除。

### 修正（2026-08-04，ticket 33 决议后）

> **⚠️ 本节关于 bookkeeping 持久化的结论已被 ticket 32 推翻**（见上方「关联 gap」）：4 字段作为 **scheduler 运行时状态**保留（ticket 33 保留 ParallelScheduler 的结论不变），但**不进入持久化 schema**。以下表格的「保留」仅指运行时保留。

ticket 33 决议移除 fork-merge 依赖链，但**保留 ParallelScheduler 的并发调度能力**。`GraphMetadata` 的 bookkeeping 字段**不变**：

| 字段 | 来源 | 是否保留 |
|------|------|---------|
| `instance_seq` | ParallelScheduler 的 instance 序号 | **保留** — ParallelScheduler 并发调度仍需要 |
| `iteration_count` | 两个 scheduler 都用（max_iterations 安全网） | **保留** |
| `activated_sources` | ParallelScheduler ON_ALL_PREDS bookkeeping | **保留** — ParallelScheduler 保留 |
| `pending_dispatches` | ParallelScheduler ON_ALL_PREDS bookkeeping | **保留** — ParallelScheduler 保留 |

~~`bookkeeping_json` schema 不变：`{instance_seq, iteration_count, activated_sources, pending_dispatches}`。~~（ticket 32 作废）

~~ticket 32（bookkeeping 持久化）不变：仍需持久化全部 4 个字段。~~（ticket 32 裁决零持久化，本句作废）

### 补充确认（2026-08-04，ticket 33 最终决议后）

ticket 33 的最终决议（SUPERSEDED 移除 / `complete_invocation` 传参变更 / state_factory 移除 / channel 系统移除）**不进一步影响本 ticket**：

- **SUPERSEDED 移除**：影响的是 `InvocationStatus` enum（per-node invocation 状态），不是 `GraphInstanceStatus`（graph 实例状态）。`GraphInstanceStore` 的 schema 存 `GraphInstanceStatus`（`running/paused/stopped/crashed/completed/failed`），不含 SUPERSEDED。不受影响。
- **`complete_invocation` 传参变更**：从 delta 改为 full snapshot。影响 `NodeState` 的 `state_json` 列内容，不影响 `GraphInstanceStore` 的 schema。
- **state_factory 移除**：影响 `GraphOrchestrator._create_state`（从 `DynamicStateFactory(schema).create_state()` 改为 `state_class()`），不影响 `GraphInstanceStore`。
- **channel 系统移除**：影响 `GraphState` 内部实现，不影响 store 层。

`GraphInstanceStore` 的 ABC、schema、三实现、接线关系全部不变。
