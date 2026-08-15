# 分布式持久化与 Node 生命周期

Status: **current**（设计权威）。本文档描述 `modex_graph` 分布式持久化层的当前实现状态。目标读者是需要理解、维护或扩展该系统的开发者。

**2026-08-15 refinement（phase 07+09）：** `node_states` schema 瘦身为纯生命周期+版本链事实：`NodeInvocationRecord` 与所有 `NodeStateStore` 实现不再携带 `state_json` / `suspended` 列；`suspend_invocation` 方法退役（`GraphInterrupt` 改走 `cancel_invocation` + 上抛，恢复是全新 re-invocation 重消费 consumable delivers，不读快照）。`rebuild_main_state` 删除——state 不从 store 恢复，由调用方初始化 `ctx.state`，崩溃恢复从 invocation 状态 + 四态 deliver 准入路径派生。`DeliverStore` 消费状态机统一为四态（`STAGED → PENDING → CONSUMED_PENDING → CONSUMED_COMPLETED`，stateful 实现一致）。`GraphMetadata` 增 `attrs` 扩展位（phase 09 ownership seam）。`bootstrap` 改为显式 `*, mode: BootstrapMode`（FRESH 零扫描 / RECOVERY 完整推导）。下文相关段落已同步；保留的历史描述标注 "(历史)"。

配套文档：`external-control.md`（外部控制面与恢复语义权威，2026-08-05）。状态机语义精化、恢复入口集推导、at-least-once 契约、持久化档位降级矩阵以该文档为准。所有配套 ticket（34~39）已落地实现。

Date: 2026-08-05（2026-08-15 更新：phase 07+09 退役 state_json/suspended/suspend_invocation，四态 deliver，bootstrap 显式 mode，attrs 扩展位）

## 1. 概述

分布式持久化用三层 store 加共享 state 加 full snapshot 描述图运行的全部可恢复状态。没有 fork/merge,没有 channel,没有 declarative delta,没有单独的 dispatch 持久化。Nodes 直接 mutate 共享 state,持久化层保存 full snapshot。

### 1.1 三层持久化

| 层 | Store | 内容 | 拥有者 |
|----|-------|------|--------|
| Graph 实例 | `GraphInstanceStore` | `GraphMetadata`(5 字段: identity + status) | `GraphOrchestrator` / recovery / control 共享同一个实例 |
| Node 调用 | `NodeStateStore` | 调用版本链 + lifecycle 状态（无 state_json/suspended） | `GraphPersistenceCoordinator`(per graph instance 一个) |
| Deliver | `DeliverStore` | per-node 投递与消费状态机 | `GraphPersistenceCoordinator.register_node` 注册(per node 一个) |

每层各有 `Null` / `InMemory` / `Sqlite` 三种实现。`Null` 用于 ReActAgent per-turn 路径(无持久化),`InMemory` 用于测试与单进程临时图,`Sqlite` 用于需要 crash recovery 的生产图。三档的恢复能力降级矩阵与 fail-safe 契约见 `external-control.md` §9。

### 1.2 共享 state（per-run 工作区，不持久化）

图状态是单个 `GraphState(BaseModel)` 实例,所有 node 共享同一个 `ctx.state` 引用。Node 在 `execute` 里直接 imperative mutate(`ctx.state.x = y`)。没有 reducer channel,没有 declarative `state_update` delta。

**State 不从 store 恢复**（phase 07）。调用方初始化 `ctx.state`；`node_states` 只存生命周期+版本链事实（无 `state_json`）。崩溃恢复从 invocation 状态 + 四态 deliver 准入路径派生,不重建 business-state snapshot。`GraphState.checkpoint()` / `from_checkpoint()` 仍存在（Pydantic `model_dump` / `model_validate`），供 ReAct turn-state 序列化等场景使用,但 graph 层不再用它持久化 node 状态。

### 1.3 GraphSpec 加 state_class

`GraphSpec` 携带 `state_class: str`(registry name),`GraphSpecCompiler` 通过 `Mapping[str, type[GraphState]]` registry 解析为具体 `GraphState` 子类。编译产物 `CompiledGraph` 持有 resolved `state_class`。调用方用它初始化 `ctx.state`（state 不从 store 恢复——phase 07）。

## 2. 架构

```
┌─────────────────────────────────────────────────────────────────────┐
│                        GraphOrchestrator                            │
│  - CoordinatorFactory (注入,默认 NullCoordinatorFactory)             │
│  - GraphInstanceStore (共享给 recovery / control)                    │
│  - _active_instances: dict[int, GraphInstance]                      │
└───────────┬─────────────────────────────────────────────────────────┘
            │ create_and_run(spec_id, ...)
            ▼
┌─────────────────────────────────────────────────────────────────────┐
│                      GraphInstance (runtime)                        │
│  - metadata: GraphMetadata (frozen Pydantic, 5 字段)                │
│  - coordinator: GraphPersistenceCoordinator                        │
└───────────┬─────────────────────────────────────────────────────────┘
            │
            ▼
┌─────────────────────────────────────────────────────────────────────┐
│                   GraphPersistenceCoordinator                       │
│  - instance_store: GraphInstanceStore (caller-owned)                │
│  - node_state_store: NodeStateStore (scoped to graph_instance_id)   │
│  - deliver_stores: dict[str, DeliverStore] (per node)              │
│                                                                    │
│  方法组:                                                            │
│    注册与路由: register_node / get_deliver_store / route_deliver    │
│    消费:       collect_consumable_delivers / mark_delivers_consumed │
│                / promote_delivers                                  │
│    恢复与查询: get_graph_state                                     │
│                (load_for_recovery/rebuild_main_state 已移除——phase 07+08)│
│    资源清理:   close (no-op,store 不关连接)                         │
│  没有 lifecycle 方法(lifecycle 在 NodeStateStore 上)               │
└───────────┬─────────────────────────────────────────────────────────┘
            │ run_async(ctx)
            ▼
┌─────────────────────────────────────────────────────────────────────┐
│              Scheduler (LinearScheduler | ParallelScheduler)       │
│  - bootstrap(ctx, graph, mode=FRESH|RECOVERY) → seed 节点名        │
│  - 调用方初始化 ctx.state（不从 store 恢复）                        │
│  - 调度循环: before_node → Node.run → after_node                  │
└───────────┬─────────────────────────────────────────────────────────┘
            │ Node.run(ctx, graph=compiled)
            ▼
┌─────────────────────────────────────────────────────────────────────┐
│                          Node.run()                                │
│  1. begin_invocation (NodeStateStore)                              │
│  2. collect_consumable_delivers + integrate                        │
│  3. await execute(ctx, integrated_input)  # async void             │
│  4. complete_invocation(invocation)  # 无 state 参数               │
│  5. promote_staged_by_source + dispatch(target, state_update={})   │
│  6. promote_delivers                                               │
│  except GraphInterrupt: cancel_invocation + re-raise               │
│  except GraphBubbleUp:  cancel_invocation                          │
│  except Exception:      crash_invocation                           │
│  finally:               finalize_invocation                        │
└─────────────────────────────────────────────────────────────────────┘
```

数据流方向: `Node.run` 调 `NodeStateStore` 的 lifecycle 方法(经 `ctx.node_state_store`),调 `coordinator.route_deliver` 投递下游,调 `coordinator.collect_consumable_delivers` 消费上游。Coordinator 不持有 lifecycle,只做路由与查询。Scheduler 不感知 store,只调 coordinator 的恢复与查询接口。

## 3. GraphInstanceStore

### 3.1 GraphMetadata

```python
class GraphMetadata(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    graph_instance_id: int        # Snowflake ID,持久化唯一 key
    spec_id: int                  # FK → graph_specs.spec_id
    version: int = 0              # 版本链（begin_invocation 递增）
    parent_instance_id: int | None  # 嵌套子图时父实例 ID,顶层为 None
    parent_node: str | None       # 父图中创建此实例的 node 名,顶层为 None
    status: GraphInstanceStatus   # running / paused / stopped / crashed / completed / failed
    node_id_map: dict[str, str] = {}        # v0 冻结的 {name: node_id},跨版本复制
    attrs: dict[str, int | str | None] = Field(default_factory=dict)  # phase 09 扩展位（ownership/audit seam）
    created_at: int = 0
    updated_at: int = 0
```

`frozen=True, extra="forbid"`。`attrs` 是 typed exception（跟随 `node_id_map` 先例），供业务层写 per-instance 元数据（如 `executor_process_id`）而不改 schema。Scheduler bookkeeping 字段（`instance_seq` / `iteration_count` / `activated_sources` / `pending_dispatches`）不在这里。这四个全是运行时视图,recovery 时从 `node_states` 与 `deliver_states` 派生,不持久化。

### 3.2 单一 status 写入路径

Status 更新只走一条路: `instance_store.update_status(graph_instance_id, GraphInstanceStatus)`。

- `GraphOrchestrator` 在 `create_and_run` 时写 `RUNNING`,完成时写 `COMPLETED`,`GraphInterrupt` 时写 `PAUSED`,其他异常写 `CRASHED`。
- `GraphDrained`(外部 pause/stop 触发的协作排空,见 `external-control.md` §3-5)上抛时**不覆盖状态**——目标状态(PAUSED/STOPPED)已由 `GraphControlService` 在调 engine 之前写入,orchestrator `except GraphDrained: pass` 识别为预期内退出(ticket 34/35 已落地)。
- `GraphRecoveryService` 恢复时写 `RUNNING`。
- `GraphControlService` 的 pause / stop / resume 命令写对应 status。
- FAILED 不由框架写入:业务层重试预算耗尽后经 `update_status` 写入(见 `external-control.md` §2/§10)。
- `GraphInstance` runtime class 不再有 `update_status` 方法。`GraphInstance.status` 是只读 property,委托到 `metadata.status`。

没有 `GraphMetadataStore`。旧设计的 `GraphMetadataStore` ABC 加三个实现(Null / Memory / Sqlite)已删除,身份与 status 持久化收敛到 `GraphInstanceStore`。

### 3.3 ABC 与三种实现

```python
class GraphInstanceStore(ABC):
    @abstractmethod
    def save(self, graph_instance_id: int, metadata: GraphMetadata) -> None: ...
    @abstractmethod
    def load(self, graph_instance_id: int) -> GraphMetadata | None: ...
    @abstractmethod
    def update_status(self, graph_instance_id: int, status: GraphInstanceStatus) -> None: ...
    @abstractmethod
    def query_by_status(self, status: GraphInstanceStatus) -> list[GraphMetadata]: ...
    @abstractmethod
    def delete(self, graph_instance_id: int) -> None: ...
```

三种实现:

- `NullGraphInstanceStore`: no-op,`load` 返回 None。ReActAgent per-turn 路径。
- `MemoryGraphInstanceStore`: dict 加 `model_copy` 更新 status。
- `SqliteGraphInstanceStore`: `graph_instances` 表,共享 `sqlite3.Connection`。

### 3.4 graph_instances 表

```sql
CREATE TABLE IF NOT EXISTS graph_instances (
    graph_instance_id   BIGINT  PRIMARY KEY,
    spec_id             BIGINT  NOT NULL,
    parent_instance_id  BIGINT,
    parent_node         TEXT,
    status              TEXT    NOT NULL DEFAULT 'running'
                        CHECK (status IN ('running', 'paused', 'stopped',
                                          'crashed', 'completed', 'failed')),
    created_at          INTEGER NOT NULL,
    updated_at          INTEGER NOT NULL
);
```

5 个基本列加 `created_at` / `updated_at`。没有 `bookkeeping_json` 列(曾在 002 添加,004 删除)。索引: `idx_graph_instances_spec`(spec_id)、`idx_graph_instances_parent`(parent_instance_id,partial)、`idx_graph_instances_active`(status,partial,只索引 running / paused / crashed)。`trg_graph_instances_auto_updated_at` trigger 自动维护 `updated_at`。

## 4. NodeStateStore

`NodeStateStore` 是 node 调用生命周期、版本链、CAS 的单一权威。Rule 15: 没有 `NodeState` 平行路径(旧 `NodeState` ABC 已删除),`Node.run()` 通过 `ctx.node_state_store` 直接调这些方法。

### 4.1 scope

每个 store 实例 scoped to 一个 `graph_instance_id`(构造时捕获)。所有方法只取 `node_name`,不再传 `graph_instance_id`。

### 4.2 InvocationStatus enum

```python
class InvocationStatus(StrEnum):
    RUNNING = "running"        # 执行中,或 GraphInterrupt suspend 后待恢复
    COMPLETED = "completed"    # 终态,不可变
    CANCELED = "canceled"      # 终态,GraphBubbleUp 取消
    CRASHED = "crashed"        # 终态,异常
```

没有 `PENDING`。Records 在 `begin_invocation` 时直接 INSERT 为 `RUNNING`。没有 `SUPERSEDED`。**没有 `suspended` 字段**（phase 07 退役）——`GraphInterrupt` 走 `cancel_invocation`（终态 `CANCELED`）+ 上抛，恢复是全新 re-invocation 重消费 consumable delivers，不保留挂起态。`RUNNING` 的语义仅为"执行中"。

### 4.3 Lifecycle 方法

```python
class NodeStateStore(ABC):
    def __init__(self, graph_instance_id: int) -> None: ...

    # Lifecycle
    @abstractmethod
    def begin_invocation(self, node_id: str) -> InvocationContext: ...
    @abstractmethod
    def complete_invocation(self, invocation: InvocationContext) -> None: ...
    @abstractmethod
    def crash_invocation(self, invocation: InvocationContext) -> None: ...
    @abstractmethod
    def cancel_invocation(self, invocation: InvocationContext) -> None: ...
    @abstractmethod
    def finalize_invocation(self, invocation: InvocationContext) -> None: ...

    # Query
    @abstractmethod
    def load_latest(self, node_id: str) -> NodeInvocationRecord | None: ...
    @abstractmethod
    def load_latest_completed(self, node_id: str) -> NodeInvocationRecord | None: ...
    @abstractmethod
    def load_by_invocation_id(self, node_id: str, invocation_id: int) -> NodeInvocationRecord | None: ...
    @abstractmethod
    def query_versions(self, node_id: str, status_filter: set[InvocationStatus] | None = None) -> list[NodeInvocationRecord]: ...
    @abstractmethod
    def list_nodes(self) -> list[str]: ...
    @abstractmethod
    def query_all(self, status_filter: set[InvocationStatus]) -> list[NodeInvocationRecord]: ...
    @abstractmethod
    def clear(self) -> None: ...
```

**没有 `suspend_invocation`**（phase 07 退役）。`complete_invocation(invocation)` 不带 state 参数——`node_states` 只存生命周期+版本链事实，不存 `state_json`。`GraphInterrupt` 走 `cancel_invocation`（终态 `CANCELED`）+ 上抛。

### 4.4 begin_invocation 行为

1. Orphan 清理: 如果存在 prior RUNNING record,标记为 CRASHED。
2. `version = max(所有已有版本号) + 1`(不是 `load_latest_completed + 1`,避免 CRASHED 或 RUNNING 版本号相同导致 UNIQUE 冲突)。
3. `parent_version` 从 `load_latest_completed` 取,无则 None。
4. INSERT 新 record,status = RUNNING,invocation_id = Snowflake ID。（没有 `suspended` 列——phase 07 退役。）
5. 返回 `InvocationContext`(invocation_id + node_id + version + parent_version)。

（历史：旧实现保留 suspended RUNNING record 作为 rebuild 源。phase 07 后无 suspended 态——`GraphInterrupt` 走 `cancel_invocation`，recovery 是全新 re-invocation。）

### 4.5 CAS(compare-and-swap)

严格与容忍分层:

| 方法 | CAS 语义 | 失败行为 |
|------|----------|----------|
| `complete_invocation` | STRICT: `WHERE status='running'` | `rowcount == 0` 抛 `InvocationStateError` |
| `cancel_invocation` | STRICT: 同上 | 抛 `InvocationStateError` |
| `crash_invocation` | TOLERANT: 已终态则 no-op | 不抛 |
| `finalize_invocation` | TOLERANT: 终态不动,orphan RUNNING 转 CRASHED | 不抛 |

**没有 `suspend_invocation`**（phase 07 退役）。CAS 条件不再带 `suspended=0`（列已删除）。`InvocationStateError` 在 `modex_graph/exceptions.py`,ordinary `Exception`,与 `RoutingError` 同级。表示 lost race 或 duplicate transition attempt。

### 4.6 三种实现

- `NullNodeStateStore`: `begin_invocation` 返回有效 `InvocationContext`(generated invocation_id, version=0, parent_version=None),其余 no-op,所有 query 返回 None / empty。
- `InMemoryNodeStateStore`: dict 加 `list[NodeInvocationRecord]`。CAS 通过 status check 实现(`if current.status != RUNNING or current.suspended: raise`)。
- `SqliteNodeStateStore`: `UPDATE ... WHERE status='running' AND suspended=0` 加 `rowcount == 0` 检测。共享 `sqlite3.Connection`。

### 4.7 node_states 表

```sql
CREATE TABLE IF NOT EXISTS node_states (
    node_state_id       BIGINT  PRIMARY KEY,
    graph_instance_id   BIGINT  NOT NULL,
    node_id             TEXT    NOT NULL,
    version             INTEGER NOT NULL DEFAULT 0,
    parent_version      INTEGER,
    status              TEXT    NOT NULL DEFAULT 'running'
                        CHECK (status IN ('running', 'completed',
                                          'canceled', 'crashed')),
    invocation_id       BIGINT  NOT NULL DEFAULT 0,
    created_at          INTEGER NOT NULL,
    updated_at          INTEGER NOT NULL,
    UNIQUE (graph_instance_id, node_id, version)
);
```

CHECK 约束只允许 4 个 status 值。索引: `idx_node_states_latest`(graph_instance_id, node_id, version DESC)、`idx_node_states_node`、`idx_node_states_status`、`idx_node_states_cross`(graph_instance_id, node_id, invocation_id)、`idx_node_states_global`(graph_instance_id, invocation_id DESC)。

**没有 `state_json` / `suspended` 列**（phase 07 退役）。行只存生命周期+版本链事实。`SqliteNodeStateStore._init_schema` 检测到含 `state_json` / `suspended` 列的旧表时重建（保有效行、删旧列、重建索引）。State 不从 store 恢复——调用方初始化 `ctx.state`，恢复从 invocation 状态 + 四态 deliver 准入路径派生。

### 4.8 NodeInvocationRecord(值对象)

```python
class NodeInvocationRecord(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    invocation_id: int
    graph_instance_id: int
    node_id: str
    version: int
    parent_version: int | None
    status: InvocationStatus
    created_at: int
    updated_at: int
```

**没有 `state_json` / `suspended` 字段**（phase 07 退役）。`updated_at` 是最后 transition 时间。

## 5. DeliverStore

`DeliverStore` 是 per-node 投递累积与消费状态机。每个 node 持有自己的 `deliver_store` 引用,表示"我收到的内容"。Coordinator 在 `register_node` 时为每个 node 创建 deliver_store 引用。

### 5.1 ABC

```python
class DeliverStore(ABC):
    @abstractmethod
    def accumulate(self, *, graph_instance_id, target_node, source_node,
                   source_invocation_id, content) -> int: ...
    @abstractmethod
    def query_consumable(self, graph_instance_id, target_node) -> list[DeliverRecord]: ...
    @abstractmethod
    def mark_consumed(self, deliver_ids: list[int], consumed_by_invocation_id: int) -> None: ...
    @abstractmethod
    def promote_consumed(self, consumed_by_invocation_id: int) -> None: ...
```

`accumulate` 是 keyword-only 签名,带 `source_node` + `source_invocation_id`。`query_consumable` 返回可消费的 delivers。`mark_consumed` 标记为已消费。`promote_consumed` 在 invocation COMPLETED 时调用。

### 5.2 消费状态机

`DeliverConsumptionStatus` enum 有 4 个值,不同实现用不同子集:

**NullDeliverStore(无状态机)**:
- `accumulate` 创建 PENDING 记录,存 in-memory queue。
- `mark_consumed` 直接移除记录。
- `promote_consumed` no-op。

**InMemoryDeliverStore(四态,与 SQLite 一致)**:
```
STAGED → PENDING → CONSUMED_PENDING → CONSUMED_COMPLETED
```
- `accumulate` 创建 PENDING（默认 status）。
- `promote_staged_by_source` 把匹配源的 STAGED 记录转 PENDING。
- `mark_consumed` 设 `status = CONSUMED_PENDING`,记 `consumed_by_invocation_id`（frozen model — 经 `model_copy` 替换）。
- `promote_consumed` 把匹配的 CONSUMED_PENDING 转 CONSUMED_COMPLETED（不删除）。
- `query_consumable` 返回 PENDING + CONSUMED_PENDING。

**SqliteDeliverStore(三态)**:
```
PENDING → CONSUMED_PENDING → CONSUMED_COMPLETED
```

| 状态 | 含义 | 恢复时行为 |
|------|------|-----------|
| `PENDING` | 已投递,未被任何 invocation 消费 | 纳入消费 |
| `CONSUMED_PENDING` | 被某 invocation 消费,但该 invocation 未 COMPLETED | 重新纳入消费(上次没完成) |
| `CONSUMED_COMPLETED` | 被某 invocation 消费,且该 invocation 已 COMPLETED | 跳过(已处理完成) |

- `mark_consumed` 转换 PENDING 到 CONSUMED_PENDING,记 `consumed_by_invocation_id`。
- `promote_consumed` 转换 CONSUMED_PENDING 到 CONSUMED_COMPLETED。
- `query_consumable` 返回 PENDING 加 CONSUMED_PENDING,排除 CONSUMED_COMPLETED。

Node 重新进入时(图有环),新 invocation 查 deliver_store: 上次 invocation CONSUMED_COMPLETED 的 delivers 跳过,新投递的 PENDING delivers 纳入消费。Crash 恢复时,crash 的 invocation 消费的 delivers 是 CONSUMED_PENDING,重新纳入消费。

### 5.3 DeliverRecord(值对象)

```python
class DeliverRecord(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    deliver_id: int                              # Snowflake ID (PK)
    graph_instance_id: int
    node_name: str                               # 接收方(this store 的 owner)
    next_node: str                               # 旧字段,保留兼容
    source_node: str                             # 投递方
    source_invocation_id: int                    # 投递方 invocation_id
    consumed_by_invocation_id: int | None        # 消费方 invocation_id
    content: Any                                 # 投递内容(JSON-serializable)
    status: DeliverConsumptionStatus
    created_at: int
    updated_at: int
```

### 5.4 deliver_states 表

```sql
CREATE TABLE IF NOT EXISTS deliver_states (
    deliver_id          BIGINT  PRIMARY KEY,
    graph_instance_id   BIGINT  NOT NULL,
    node_name           TEXT    NOT NULL,
    next_node           TEXT    NOT NULL,
    source_node         TEXT    NOT NULL DEFAULT '',
    source_invocation_id INTEGER NOT NULL DEFAULT 0,
    consumed_by_invocation_id INTEGER,
    content_json        TEXT    NOT NULL CHECK (json_valid(content_json)),
    status              TEXT    NOT NULL DEFAULT 'pending'
                        CHECK (status IN ('accumulated', 'submitted',
                                          'pending', 'consumed',
                                          'consumed_pending', 'consumed_completed')),
    created_at          INTEGER NOT NULL,
    updated_at          INTEGER NOT NULL
);
```

CHECK 约束允许全部 4 个 `DeliverConsumptionStatus` 值加两个旧 `DeliverStatus` 值(`accumulated` / `submitted`)做向后兼容。索引: `idx_deliver_states_node`(graph_instance_id, node_name, status)、`idx_deliver_states_target`(graph_instance_id, next_node, status)。`deliver_id` 用 Snowflake ID(跨进程单调),不用 SQLite AUTOINCREMENT。

## 6. GraphPersistenceCoordinator

### 6.1 角色

Coordinator 是持久化路由与恢复查询的中央编排器。它**不持有 lifecycle 方法**(lifecycle 已移到 `NodeStateStore`)。它持有三个 store 引用,提供路由投递、消费查询、恢复重建三组方法。

```python
class GraphPersistenceCoordinator:
    def __init__(self, graph_instance_id, instance_store,
                 node_state_store, default_deliver_store_factory) -> None: ...

    @property
    def node_state_store(self) -> NodeStateStore: ...

    # 注册与路由
    def register_node(self, node_name, deliver_store=None) -> None: ...
    def get_deliver_store(self, node_name) -> DeliverStore | None: ...
    def route_deliver(self, target_node, content, source_node,
                      source_invocation_id) -> int | None: ...

    # 消费
    def collect_consumable_delivers(self, node_name, invocation_id) -> list[DeliverRecord]: ...
    def mark_delivers_consumed(self, node_name, deliver_ids, invocation_id) -> None: ...
    def promote_delivers(self, node_name, invocation_id) -> None: ...

    # 恢复与查询
    def get_graph_state(self, node_status_filter=None) -> GraphStateSnapshot: ...

    def close(self) -> None: ...  # no-op
```

### 6.2 register_node 只注册 deliver store

`register_node(node_name, deliver_store=None)` 只做一件事: 为 node 注册 `deliver_store`。`None` 表示用构造时注入的 default factory。不再注册 `node_state`(旧设计的 `NodeState` ABC 已删除,所有 node 共享同一个 `NodeStateStore`)。

### 6.3 route_deliver

`route_deliver(target_node, content, source_node, source_invocation_id)` 把一个 deliver 路由到 target node 的 deliver_store。

- `target == GraphNode.END`: 跳过(END 无 deliver_store),返回 None。
- `target` 无注册 store: 抛 `RoutingError`。
- 正常: 调 `store.accumulate(...)`,返回 `deliver_id`。

`GraphControlService` 的外部 deliver 也走这条路: `route_deliver(target_node=node_name, content=content, source_node="__external__", source_invocation_id=0)`。

### 6.4 rebuild_main_state — 已移除（phase 07）

`rebuild_main_state()` 已删除。`node_states` 不再存 `state_json`，state 不从 store 恢复。调用方初始化 `ctx.state`；崩溃恢复从 invocation 状态 + 四态 deliver 准入路径派生，不重建 business-state snapshot。`bootstrap(mode=RECOVERY)` 的空 seed fallback 返回 `[entry_node]`，调度器从入口重跑（fresh state）。

（历史算法：对每个 node 取 newest COMPLETED 或 suspended RUNNING 的 `state_json` snapshot，跨 node 按 `invocation_id` Snowflake 时间序 `dict.update` 合并。phase 07 退役 `state_json`/`suspended` 后此路径消失。）

### 6.5 load_for_recovery — 已移除（phase 07+08）

`load_for_recovery()` 与 `RecoveryContext` 已删除。恢复推导收敛进 `bootstrap(ctx, graph, *, mode=BootstrapMode.RECOVERY)`（`scheduler/bootstrap.py`），直接用 `coordinator.node_state_store` 与各 node 的 deliver_store 派生 seed，不经过 coordinator 的恢复方法。

bootstrap RECOVERY 做两项自动修复（在 seed 推导之前）：
- **auto-promote STAGED**：把 `COMPLETED` 源节点的 STAGED delivers 转 PENDING（修复 crash 在 `complete_invocation` 与 `promote_staged_by_source` 之间）。
- **auto-promote CONSUMED_PENDING**：把消费方 invocation 已 `COMPLETED` 的 CONSUMED_PENDING delivers 转 CONSUMED_COMPLETED（修复 crash 在 `mark_consumed` 与 `promote_delivers` 之间）。

两项前置使提升后的 PENDING 行对 seed 扫描可见。bootstrap 不恢复 `ctx.state`（调用方初始化），不创建 instance，只返回 seed 节点名列表。

### 6.6 close 是 no-op

`coordinator.close()` 不做任何事。Store 接受 caller-owned `sqlite3.Connection`,从不关闭连接。连接生命周期由调用方(业务层)管理。保留 `close` 方法是 safe-to-call lifecycle hook,供 `GraphOrchestrator.unregister_instance` 调用。

## 7. CoordinatorFactory

### 7.1 ABC

```python
class CoordinatorFactory(ABC):
    @abstractmethod
    def create(self, graph_instance_id: int,
               instance_store: GraphInstanceStore) -> GraphPersistenceCoordinator: ...
```

Factory 收到调用方的 `instance_store` 实例(与 orchestrator 和 recovery service 共享),内部组装 `node_state_store` 和 `deliver_store_factory`。这是业务层装配: 框架不规定 store 如何构造(Null / InMemory / Sqlite 加共享 connection)。

### 7.2 NullCoordinatorFactory(框架默认)

```python
class NullCoordinatorFactory(CoordinatorFactory):
    def create(self, graph_instance_id, instance_store) -> GraphPersistenceCoordinator:
        return GraphPersistenceCoordinator(
            graph_instance_id=graph_instance_id,
            instance_store=instance_store,                    # 用调用方的,不替换
            node_state_store=NullNodeStateStore(graph_instance_id),
            default_deliver_store_factory=NullDeliverStoreFactory(),
        )
```

`instance_store` 用调用方的(不替换为 Null),所以 coordinator 与调用方共享 instance store,而 node state 和 deliver 保持 no-op。这保证 `GraphInstanceStore` 的 status 持久化在 ReAct 路径仍生效,而 node invocation 和 deliver 不持久化。

### 7.3 注入点

`CoordinatorFactory` 注入 `GraphOrchestrator.__init__`(默认 `NullCoordinatorFactory()`)。`create_and_run` 与 `GraphRecoveryService` 共用同一个注入点: 都调 `factory.create(graph_instance_id, instance_store)`。

ReActAgent per-turn 路径不走 `GraphOrchestrator`,用 `create_null_coordinator(graph_instance_id=0)` 直接构造 Null coordinator。这是 structural pass-through: AgentContext 是状态载体,coordinator 是 no-op。

业务层需要 SQLite recovery 时,提供自己的 `CoordinatorFactory` 实现,内部用共享 `sqlite3.Connection` 装配 `SqliteNodeStateStore` 和 `SqliteDeliverStoreFactory`。参考实现: workspace DB 在 `<workspace>/.modex/state.db`。

## 8. Node.run() 统一流程

`Node.run(ctx, *, graph)` 是 node 生命周期的唯一入口。它统一处理状态转移、持久化、版本链、deliver 收集与 dispatch。Node 子类只实现 `async execute(ctx, integrated_input) -> None`(async void,无返回值)。

完整流程（phase 07+ 后）:

1. **begin_invocation**: `ctx.node_state_store.begin_invocation(self.node_id)` 创建新 invocation（version = max+1，orphan RUNNING → CRASHED）。设置 execution context 的 invocation。
2. **try 块**: integrate → execute → complete → promote_staged → dispatch → promote_delivers。
   - **integrate**: `coordinator.collect_consumable_delivers(self.node_id, invocation.invocation_id)`（返回 PENDING + CONSUMED_PENDING）→ 若有则 `mark_delivers_consumed` → `input_integrator.integrate(payloads)` 整合为 `IntegratedInput`。
   - **execute**: 调 `await self.execute(ctx, integrated)`。async void,无 NodeResult。
   - **complete**: `node_state_store.complete_invocation(invocation)`（STRICT CAS，无 state 参数——`node_states` 不存 `state_json`）。
   - **promote_staged_by_source**: `coordinator.promote_staged_by_source(gid, self.node_id)` 把本节点 STAGED 输出转 PENDING，返回受影响 target 节点 ID 集。
   - **dispatch**: 对每个受影响 target 调 `ctx.dispatch(target, state_update={})`——纯唤醒信号，内容已在 deliver store（不经 dispatch payload）。
   - **promote_delivers**: `coordinator.promote_delivers(self.node_id, invocation.invocation_id)` 把本节点消费的 CONSUMED_PENDING 输入转 CONSUMED_COMPLETED。
3. **except GraphInterrupt**: `node_state_store.cancel_invocation(invocation)`（终态 CANCELED，无 snapshot）,re-raise。
4. **except GraphBubbleUp**: `node_state_store.cancel_invocation(invocation)`,re-raise。
5. **except Exception**: `node_state_store.crash_invocation(invocation)`,re-raise（并 `emit_output(NODE_CRASHED)`）。
6. **finally**: `node_state_store.finalize_invocation(invocation)`。安全网（orphan RUNNING → CRASHED）。

**没有 `suspend_invocation` / `state_json` snapshot**（phase 07 退役）。`GraphInterrupt` cancel + 上抛，恢复是全新 re-invocation 重消费 consumable delivers。`after_node(ctx, node_name)` 由 scheduler 在 `Node.run` 返回后调用（两参数，不传 result）。

`after_node(ctx, node_name)` 由 scheduler 在 `Node.run` 返回后调用。两参数,不传 result(因为 `execute` 是 async void,无 NodeResult):

```python
async def after_node(self, ctx: GraphContext[Any], node_name: str) -> None: ...
```

## 9. Deliver 路由与消费

### 9.1 投递(生产侧)

Node A 在 execute 中调 `self.deliver(content, next_node, ctx)` 累积到 in-memory `_pending_delivers`。execute 返回后,`_submit` 按 `next_node` 分组,每组调 `ctx.dispatch(target, state_update={"delivered": payload, "_source_node": ..., "_source_inv_id": ...})`。

Scheduler 的 dispatch handler 读 `ctx.current_invocation` 获取 `source_node` 与 `source_invocation_id`,调 `coordinator.route_deliver(target_node, content, source_node, source_invocation_id)`。Coordinator 找到 target node 的 deliver_store,调 `store.accumulate(...)` 生产到下游 deliver_store。

`target == GraphNode.END` 时 `route_deliver` 跳过。

### 9.2 消费(消费侧)

Node B 的 `run()` 在 integrate 阶段从自己的 deliver_store 消费:

1. `coordinator.collect_consumable_delivers(self.name, invocation.invocation_id)` 返回可消费的 delivers。
2. 若有 delivers,`coordinator.mark_delivers_consumed(self.name, [deliver_ids], invocation.invocation_id)` 标记为已消费。
3. 用 `input_integrator.integrate` 整合为 `IntegratedInput`。
4. 传给 `execute(ctx, integrated)`。

（历史：旧 suspend/resume 实现跳过此流程,直接用前一 invocation 的 state snapshot 作为 integrated input,避免 double-effect。phase 07 退役 suspend 后此分支消失——`GraphInterrupt` 走 `cancel_invocation`，恢复是全新 re-invocation，integrate 正常消费 consumable delivers。）

### 9.3 时序不变量(契约)

`Node.run()` 在 `execute()` 期间通过 `deliver()` → `route_deliver(stage=True)` 把内容以 `STAGED` 落目标节点 deliver_store（在 `complete_invocation` 之前）。`complete_invocation` 标记 COMPLETED 后,`promote_staged_by_source` 把 STAGED 转 PENDING,再 `dispatch` 发唤醒。中间无 await。因此:**上游 COMPLETED ⟹ 其 deliver 必然已持久化**（STAGED 态,或条件分支下被有意跳过）。这是恢复入口集推导"deliver 记录 ⟺ 上游已提交"推断成立的根基(见 `external-control.md` §7),不得调换顺序。mid-execute 进程被杀的窗口(STAGED 已落但源未 COMPLETED)由"源节点重派 + at-least-once"兜底——重试完成时新旧 STAGED 行一并提升(W1: 输出 at-least-once by design)。

### 9.4 崩溃窗口处置结论(D6/D7/D8,见 12 票矩阵)

- **D6(stop 协作)= 接受+文档化**:外部 `stop` 触发 `GraphDrained` 协作排空后,节点体在当前 `execute` 完成后才退出(不中断进行中的 LLM/tool 调用);已终态实例的 CAS(`complete_invocation`/`cancel_invocation`)静默幂等(TOLERANT 语义)。这是设计内行为,不修。
- **D7(complete↔IORecord)= 接受+文档化**:`complete_invocation` 与 `promote_staged`/`dispatch` 之间的崩溃窗口,实例状态(lifecycle)是权威,IORecord/`state_json` 的 null 被结果路径容忍(phase 07 后 `node_states` 不存 `state_json`,此窗口进一步收窄——`complete_invocation` 只写 lifecycle 事实,内容已在 STAGED deliver store)。不修。
- **D8(Linear 不支持外部投递准入)= 接受+文档化**:`LinearScheduler` 是 ReAct 内部流调度器,无多源 admission 路径;外部投递(`deliver_to_node`)属 Parallel/bot 图场景。见 `src/modex_graph/AGENTS.md`(D8 disposition)。不修。

## 10. 恢复流程

### 10.1 恢复入口：bootstrap(mode=RECOVERY)

两个 scheduler 在 `run_async` 顶部都调 `bootstrap(ctx, graph, mode=BootstrapMode.RECOVERY)`（phase 08 收敛——旧 `_restore_from_recovery` / `_redispatch_from_recovery` / `_rebuild_pending_from_delivers` 已合并进 `bootstrap` + `_recheck_pending`）。

bootstrap RECOVERY 流程:

1. **auto-promote DOUBLE（seed 推导之前）**：STAGED（COMPLETED 源）→ PENDING；CONSUMED_PENDING（COMPLETED 消费方）→ CONSUMED_COMPLETED。两项前置使提升后的 PENDING 行对 seed 扫描可见。END 纳入提升与扫描（不再跳过）。START 跳过（empty-seed fallback 覆盖）。
2. **seed 推导**：CRASHED / orphan RUNNING invocations + 有 PENDING delivers 的节点，拓扑序（BFS from `entry_node`，END 纳入）。
3. **空 seed fallback**：全 COMPLETED 图、无 PENDING delivers → 返回 `[entry_node]`（re-invoke 从入口重跑，fresh state）。

bootstrap **不恢复 `ctx.state`**（调用方初始化），不创建 instance，只返回 seed 列表。

### 10.2 LinearScheduler 恢复

`LinearScheduler.run_async` 调 `bootstrap(mode=RECOVERY)` 取 seeds，取 `seeds[0]`（或空 seed fallback 的 `entry_node`）开始顺序循环。不调 `load_for_recovery`，不恢复 state。Resume routing 是 graph author 的 concern：entry node 读 `state.resume_target` 路由到目标 node；未设置则从 entry_node 正常开始。

崩溃恢复与 ParallelScheduler 共享同一套**恢复入口集推导**（ticket 36，见 `external-control.md` §7）：版本链顶端非终态（CRASHED/孤儿 RUNNING）重派 ∪ PENDING deliver 凭证扫描（无来源过滤、无入度推断），取拓扑序最早候选起步，之后走正常 deliver 路由。`resume_target` 机制保留，但回归本职：只管 HITL 挂起恢复，崩溃恢复不再依赖它。

### 10.3 GraphRecoveryService

`GraphRecoveryService` 两种恢复类型共享同一流程:

1. 从 `instance_store` 加载 `GraphMetadata`。
2. `factory.create(graph_instance_id, instance_store)` 重建 coordinator(用注入的 factory,不是 `create_null_coordinator`)。
3. 构造 `GraphInstance(metadata, coordinator)`。
4. `instance_store.update_status(gid, RUNNING)`。
5. 调 `engine_factory.create_and_run(instance)`。

`recover_crashed()`: 查 `CRASHED` + `RUNNING` 实例(进程被 kill 时 graph 留在 RUNNING——没有活进程在跑它),全部走共享恢复流程。返回恢复的 `graph_instance_id` 列表。

`resume(graph_instance_id)`: 加载单个实例,校验 status 为 PAUSED(STOPPED 是终态不可恢复;CRASHED 不被手动 resume,COMPLETED / FAILED 是终态),走共享流程。STOPPED 终态化决议见 `external-control.md` §2(ticket 37 已落地)。

## 11. ON_RECEIVE 串行门

### 11.1 语义

`ON_RECEIVE` trigger mode 的 node,其 dispatch 默认立即 fire(创建新 instance,无 reachability 检查)。但有 per-node 串行门: 如果 target node 已有 in-flight instance(DORMANT / READY / RUNNING 任一),dispatch 进入 per-node FIFO queue,等 in-flight instance 完成后才 fire。

```
dispatch 到 ON_RECEIVE node X:
  if X 无 in-flight instance:
    fire 立即(create_task)
  else:
    queue 到 _on_receive_queue[X]
    X 当前 instance 完成后 → _drain_on_receive_queue(X) → fire 下一个
```

`_is_node_running(node_name)` 检查是否有 DORMANT / READY / RUNNING 的 instance。`_drain_on_receive_queue(node_name)` 在 instance 完成后调用,fire 队首的 queued dispatch。

N 个 dispatch 到同一 ON_RECEIVE node → N 个串行执行(instance 一个接一个)。语义保留(每 dispatch 一个 instance),只是不并发。

### 11.2 谨慎使用(已升级为 deprecated)

串行门是 in-memory only,不跨 crash 持久化。Crash 时 queue 里的 dispatch 丢失。恢复时 `_rebuild_pending_from_delivers` 重新扫描 PENDING delivers,但 queued(未 fire)的 dispatch 状态不重建。

这意味着: 如果 ON_RECEIVE node 在有 queued dispatch 时 crash,恢复后这些 dispatch 不会自动重新 fire。它们对应的 delivers 是 PENDING(已 `accumulate` 但未 `mark_consumed`),会被 `_rebuild_pending_from_delivers` 扫到。如果 target 有 in-flight instance(被 re-dispatch),running instance 会消费它们。如果 target 无 in-flight instance,会创建新 instance 处理。但 queue 里的 dispatch 顺序不保留。

**ON_RECEIVE 已降级为 deprecated / experimental**(2026-08-12):`Graph.compile()` 对其发 `DeprecationWarning`,`GraphSpec` 声明式 API 直接拒绝。大多数 node 用 `ON_ALL_PREDS`(默认),它的 pending dispatch 会通过 `_recheck_pending` 重新评估,语义可恢复。新生产图不要使用 ON_RECEIVE。

## 12. 连接与线程契约

### 12.1 连接契约

所有 Sqlite store(`SqliteGraphInstanceStore` / `SqliteNodeStateStore` / `SqliteDeliverStore`)接受 `sqlite3.Connection`(caller-owned)。

- Store 从不关闭连接。
- Store 从不 commit 调用方的 connection(每个 store 内部 `commit` 自己的 DDL / DML,但这与调用方共享同一 connection)。
- `coordinator.close()` 是 no-op。
- Connection 生命周期由调用方(业务层)管理: 创建、共享给所有 store、最终关闭。

业务层装配 CoordinatorFactory 时,创建一个 `sqlite3.Connection`(workspace DB),传给 `SqliteNodeStateStore` 和 `SqliteDeliverStore`。`GraphInstanceStore` 也共享同一 connection(由 orchestrator 持有)。

### 12.2 线程契约

所有 store 方法是同步的,只在 event-loop 线程调用。

- `Node.run` 里的 lifecycle 调用(`begin_invocation` / `complete_invocation` 等)是同步的,在 `execute` 的 await 之前或之后执行。
- `_deliver` / `deliver` 同步运行在 `Node.execute` 内部。
- `route_deliver` 同步运行在 dispatch handler 里(LinearScheduler 的 `_handle_linear_dispatch`,ParallelScheduler 的 `_handle_dispatch`)。
- `collect_consumable_delivers` / `mark_delivers_consumed` 同步运行在 `Node.run` 的 integrate 阶段。

asyncio 单线程模型保证这些同步调用不会 interleave。不需要锁。如果业务层用 `check_same_thread=False` 共享 connection 给其他线程,那是业务层的责任,框架不保证线程安全。

## 13. ParallelScheduler 并发核心

### 13.1 并发执行

`ParallelScheduler` 保留并发调度核心:

- `asyncio.create_task` 启动每个 READY instance。
- `await asyncio.wait(running, return_when=FIRST_COMPLETED)` 等任一完成。
- 完成后处理 dispatch + recheck,新 READY instance 立即启动。

没有 fork/merge。没有 generation-based conflict detection。没有 `WriteConflictDetector` ABC。

### 13.2 共享 state 加 per-task context shell

每个 instance task 用自己的 context shell(`exec_ctx = copy(ctx)`),但**共享 `ctx.state`**:

- `exec_ctx.state` 指向同一个 `GraphState` 实例(不 deep copy)。
- `exec_ctx.current_invocation` 重置为 None(每个 task 独立设置 invocation)。
- Imperative mutation(`exec_ctx.state.x = y`)直接修改共享 state,对所有 task 可见。

没有 fork-based state isolation。所有 instance 看到同一个 state。这是设计选择: agent 框架的 node 通常是串行或弱并发的,state 共享比 fork + merge 简单且足够。

### 13.3 max_iterations 同步预留

```python
# _execute_instance 顶部,在任何 await 之前:
if self._iteration_count >= self.graph.max_iterations:
    raise GraphRecursionError(...)
self._iteration_count += 1
```

Check + increment 是同步代码段,在 `before_node` 的第一个 await 之前执行。asyncio 单线程保证不会有两个 instance 同时通过 check。即使 N 个 instance 同时 READY,它们的 `_execute_instance` 调用串行进入 check,iteration_count 准确递增。

### 13.4 LinearScheduler 与 ParallelScheduler 的差异

| 关注 | LinearScheduler | ParallelScheduler |
|------|-----------------|-------------------|
| 调度 | 顺序:`current` 指针,一个 node 接一个 | 并发:READY set + asyncio.create_task |
| 路由 | `_handle_linear_dispatch` 记录 target,取第一个 | `_handle_dispatch` 按 trigger mode 处理 |
| trigger mode | 不适用(顺序) | `ON_ALL_PREDS`(默认) / `ON_RECEIVE` |
| 恢复 | `bootstrap(mode=RECOVERY)` → seeds[0]（版本链非终态 ∪ PENDING deliver → 拓扑序最早候选,ticket 36） | `bootstrap(mode=RECOVERY)` → 创建 re-execute instance + `_recheck_pending` 扫描 PENDING deliver |
| 外部控制 | 循环顶部 `ctx.control.check()`(ticket 34) | launch 前 `check()`,命中取消在途 task + 抛 `GraphDrained`(ticket 34) |
| max_iterations | 在循环顶部 check | 在 `_execute_instance` 顶部 check + increment(同步) |

两个 scheduler 都调 `bootstrap(mode=FRESH|RECOVERY)`,都从 entry_node 开始(LinearScheduler 直接循环,ParallelScheduler 创建 entry instance)。

### 13.5 upstream_payloads 是 vestigial 字段

`NodeInstance.upstream_payloads`(`scheduler/instance.py`)由 scheduler 在三处写入(`_rebuild_pending_from_delivers` / `_fire_on_receive` / `_try_fire_on_all_preds`),但 **`Node.run()` 从不读它**——节点输入永远来自 `coordinator.collect_consumable_delivers`(查 deliver_store)。该字段是历史遗留记账,不代表节点实际输入。扩展调度器时不要以它为依据;后续可考虑移除(见 `backlog.md` BL-14 同类清理)。

## 14. 迁移历史

Workspace DB 的 migration 序列:

| Migration | 内容 | 影响 |
|-----------|------|------|
| `001_initial.sql` | 初始 schema。创建 `graph_instances`(5 基本列)、`node_states`(lifecycle 列 + CHECK(running/completed/canceled/crashed))、`deliver_states`(6 status 值)三表。 | 全新 workspace 的基线。 |

净结果:

- `graph_instances`: 5 个基本列(graph_instance_id, spec_id, parent_instance_id, parent_node, status)加 created_at / updated_at。无 bookkeeping_json。
- `node_states`: lifecycle 切片(version, parent_version, status, invocation_id, created_at, updated_at)。CHECK(running/completed/canceled/crashed)。（001 初始含 `state_json`/`suspended` 列；phase 07 `SqliteNodeStateStore._init_schema` 检测旧表并重建为纯 lifecycle 列。）
- `deliver_states`: per-node 消费状态机。CHECK 允许 4 个 `DeliverConsumptionStatus` + 2 个旧 `DeliverStatus` 值(向后兼容)。

`SqliteNodeStateStore._init_schema` 和 `SqliteDeliverStore._init_schema` 用 `CREATE TABLE IF NOT EXISTS` 创建表(与 001 DDL 一致)。`modex_graph` standalone 使用时直接创建;`modex_agent` workspace 使用时由 `MigrationRunner` 跑 001。

## 15. 已移除的概念

以下概念在当前实现中已删除,本文档不描述它们。列出供从旧文档迁移的读者参考:

- **Channels**: `BaseChannel` ABC、`LastValue`、`ReducerChannel`。State 字段不再用 `Annotated[T, ChannelSpec]` 声明。`GraphState` 是普通 Pydantic BaseModel,`checkpoint()` = `model_dump(mode="json")`。
- **Fork/merge**: `ctx.fork()` 仍存在但 scheduler 不调用。没有 fork-based state isolation。ParallelScheduler 用 per-task context shell 共享 `ctx.state`。
- **Generation-based conflict detection**: `WriteConflictDetector` ABC、`GenerationWriteTracker`、`InvalidUpdateError`。没有 multi-write 检测。
- **NodeResult / DispatchEvent / Command / Task**: `execute` 是 async void,无返回值。路由通过 `deliver` / `submit` + `ctx.dispatch(target, state_update={deliver payload})`。
- **Declarative deltas**: `state_update` 不再是 state delta。`complete_invocation` 存 full snapshot(`ctx.state.checkpoint()`),不存 delta。
- **PENDING / SUPERSEDED statuses**: `InvocationStatus` 只有 RUNNING / COMPLETED / CANCELED / CRASHED。Records begin as RUNNING。（`suspended=True` 字段已随 phase 07 退役——`GraphInterrupt` 走 `cancel_invocation`，无挂起态。）
- **GraphMetadataStore**: 删除。身份与 status 持久化收敛到 `GraphInstanceStore`。
- **NodeState ABC**: 删除。Lifecycle + 版本链 + CAS 收敛到 `NodeStateStore`。没有旧 in-memory dict API(read / write / snapshot / restore / has)。
- **Bookkeeping 持久化**: `instance_seq` / `iteration_count` / `activated_sources` / `pending_dispatches` 不持久化。Recovery 时从 `node_states` 和 `deliver_states` 派生。`GraphMetadata` 修剪到 5 字段。
- **CheckpointStore ABC + CheckpointData**: 删除。没有单独的 scheduler checkpoint。（历史：State snapshot 曾在 `node_states.state_json`；phase 07 退役后 scheduler 运行时结构是 in-memory，state 不从 store 恢复。）
- **DispatchStore**: 删除。Dispatch 通过 `deliver_store.accumulate` 记录,没有单独的 dispatch event 持久化。
- **SUPERSEDED 两阶段 rebuild**: （历史）`rebuild_main_state` 曾先 apply COMPLETED 再 apply SUPERSEDED，后简化为单条最新 snapshot。phase 07+08 退役 `rebuild_main_state` 后此概念整体消失——恢复收敛进 `bootstrap`。
- **`promote_delivers` 跨 invocation 升级**: 当前 `promote_delivers` 仍升级该 node 的所有 CONSUMED_PENDING delivers(不限 invocation_id),修复 resume 场景。但这是消费状态机的内部逻辑,不是 SUPERSEDED rebuild。
- **`state_json` / `suspended` 列（phase 07 退役）**: `node_states` 表与 `NodeInvocationRecord` 不再携带 `state_json`（full state snapshot）或 `suspended`。行只存生命周期+版本链事实。State 不从 store 恢复——调用方初始化 `ctx.state`，恢复从 invocation 状态 + 四态 deliver 准入路径派生。`SqliteNodeStateStore._init_schema` 检测旧表并重建。
- **`suspend_invocation`（phase 07 退役）**: `NodeStateStore` 不再有 `suspend_invocation`。`GraphInterrupt` 走 `cancel_invocation`（终态 `CANCELED`）+ 上抛；恢复是全新 re-invocation 重消费 consumable delivers，不读挂起快照。
- **`rebuild_main_state` / `load_for_recovery` / `RecoveryContext`（phase 07+08 退役）**: coordinator 不再有这些恢复方法。恢复推导收敛进 `bootstrap(ctx, graph, *, mode=BootstrapMode.RECOVERY)`，直接用 `node_state_store` 与 deliver_store 派生 seed。

## 16. 接口定义汇总

### 16.1 枚举(`constants.py`)

- `InvocationStatus`: RUNNING / COMPLETED / CANCELED / CRASHED
- `DeliverConsumptionStatus`: PENDING / CONSUMED / CONSUMED_PENDING / CONSUMED_COMPLETED
- `GraphInstanceStatus`: RUNNING / PAUSED / STOPPED / CRASHED / COMPLETED / FAILED
- `NodeInstanceStatus`: DORMANT / PENDING / READY / RUNNING / COMPLETED
- `NodeTrigger`: ON_ALL_PREDS / ON_RECEIVE
- `GraphNode`: START / END(sentinel)
- `SchedulerKind`: LINEAR / PARALLEL

### 16.2 异常(`exceptions.py`)

- `GraphBubbleUp`(base): `GraphInterrupt` / `GraphDrained` / `ParentCommand`
- `RoutingError`: 路由无法解析 next node
- `GraphRecursionError`: 超过 `max_iterations`
- `InvocationStateError`: CAS transition 失败(ordinary Exception,与 `RoutingError` 同级)

### 16.3 值对象(`graph_metadata.py`)

全部 `frozen=True, extra="forbid"`:

- `GraphMetadata`: 5 字段(graph_instance_id, spec_id, parent_instance_id, parent_node, status)
- `InvocationContext`: `begin_invocation` 的返回值(invocation_id, node_name, version, parent_version)
- `NodeInvocationRecord`: 一条持久化调用记录
- `GraphStateSnapshot`: `get_graph_state` 的返回值(metadata, nodes)
- (`RecoveryContext`: 已移除——phase 07+08。恢复推导收敛进 `bootstrap`。)

### 16.4 Store ABC 与实现

| ABC | Null | InMemory | Sqlite |
|-----|------|----------|--------|
| `GraphInstanceStore` | `NullGraphInstanceStore` | `MemoryGraphInstanceStore` | `SqliteGraphInstanceStore` |
| `NodeStateStore` | `NullNodeStateStore` | `InMemoryNodeStateStore` | `SqliteNodeStateStore` |
| `DeliverStore` | `NullDeliverStore` | `InMemoryDeliverStore` | `SqliteDeliverStore` |

Factory ABC: `DeliverStoreFactory`(Null / InMemory / Sqlite 三实现)、`CoordinatorFactory`(NullCoordinatorFactory 是框架默认)。

### 16.5 工厂函数

```python
def create_null_coordinator(graph_instance_id: int = 0) -> GraphPersistenceCoordinator: ...
```

用 `NullGraphInstanceStore` 加 `NullNodeStateStore` 加 `NullDeliverStoreFactory` 装配。用于 ReActAgent per-turn、LLMNode 模块级 governance helper。`GraphOrchestrator` 当前默认用 `NullCoordinatorFactory`(注入 `instance_store`,coordinator 共享调用方的 instance store)。
