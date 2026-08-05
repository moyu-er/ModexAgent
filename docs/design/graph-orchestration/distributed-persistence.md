# 分布式持久化与 Node 生命周期

Status: **current**（设计权威）。本文档描述 `modex_graph` 分布式持久化层的当前实现状态。目标读者是需要理解、维护或扩展该系统的开发者。

Date: 2026-08-05

## 1. 概述

分布式持久化用三层 store 加共享 state 加 full snapshot 描述图运行的全部可恢复状态。没有 fork/merge,没有 channel,没有 declarative delta,没有单独的 dispatch 持久化。Nodes 直接 mutate 共享 state,持久化层保存 full snapshot。

### 1.1 三层持久化

| 层 | Store | 内容 | 拥有者 |
|----|-------|------|--------|
| Graph 实例 | `GraphInstanceStore` | `GraphMetadata`(5 字段: identity + status) | `GraphOrchestrator` / recovery / control 共享同一个实例 |
| Node 调用 | `NodeStateStore` | 调用版本链 + lifecycle 状态 + full state snapshot | `GraphPersistenceCoordinator`(per graph instance 一个) |
| Deliver | `DeliverStore` | per-node 投递与消费状态机 | `GraphPersistenceCoordinator.register_node` 注册(per node 一个) |

每层各有 `Null` / `InMemory` / `Sqlite` 三种实现。`Null` 用于 ReActAgent per-turn 路径(无持久化),`InMemory` 用于测试与单进程临时图,`Sqlite` 用于需要 crash recovery 的生产图。

### 1.2 共享 state 加 full snapshot

图状态是单个 `GraphState(BaseModel)` 实例,所有 node 共享同一个 `ctx.state` 引用。Node 在 `execute` 里直接 imperative mutate(`ctx.state.x = y`)。没有 reducer channel,没有 declarative `state_update` delta。

持久化时存 full snapshot:

- `GraphState.checkpoint()` 返回 `model_dump(mode="json")`,得到 JSON 兼容 dict。
- `GraphState.from_checkpoint(data)`(classmethod)等价于 `cls.model_validate(data)`。

`complete_invocation` 把当前 `ctx.state.checkpoint()` 整体写入 `node_states.state_json`。`suspend_invocation` 同样存 full snapshot(`ctx.state.checkpoint()` 的结果)。恢复时 `model_validate(rebuilt_main_state)` 重建 state 对象,不 replay delta。

### 1.3 GraphSpec 加 state_class

`GraphSpec` 携带 `state_class: str`(registry name),`GraphSpecCompiler` 通过 `Mapping[str, type[GraphState]]` registry 解析为具体 `GraphState` 子类。编译产物 `CompiledGraph` 持有 resolved `state_class`。这是 state 反序列化的唯一入口: recovery 时 scheduler 用 `type(ctx.state).model_validate(rebuilt_main_state)` 重建 state。

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
│    恢复与查询: load_for_recovery / rebuild_main_state              │
│                / get_graph_state                                   │
│    资源清理:   close (no-op,store 不关连接)                         │
│  没有 lifecycle 方法(lifecycle 在 NodeStateStore 上)               │
└───────────┬─────────────────────────────────────────────────────────┘
            │ run_async(ctx)
            ▼
┌─────────────────────────────────────────────────────────────────────┐
│              Scheduler (LinearScheduler | ParallelScheduler)       │
│  - load_for_recovery() → RecoveryContext                           │
│  - 重建 state: model_validate(rebuilt_main_state)                  │
│  - 调度循环: before_node → Node.run → after_node                  │
└───────────┬─────────────────────────────────────────────────────────┘
            │ Node.run(ctx, graph=compiled)
            ▼
┌─────────────────────────────────────────────────────────────────────┐
│                          Node.run()                                │
│  1. begin_invocation (NodeStateStore)                              │
│  2. collect_consumable_delivers + integrate                        │
│  3. await execute(ctx, integrated_input)  # async void             │
│  4. submit: ctx.dispatch(target, state_update={deliver payload})   │
│  5. complete_invocation(state=ctx.state.checkpoint())              │
│  except GraphInterrupt: suspend_invocation(snapshot)               │
│  except GraphBubbleUp:  cancel_invocation                          │
│  except Exception:      crash_invocation                           │
│  finally:               finalize_invocation                        │
└─────────────────────────────────────────────────────────────────────┘
```

数据流方向: `Node.run` 调 `NodeStateStore` 的 lifecycle 方法(经 `ctx.node_state_store`),调 `coordinator.route_deliver` 投递下游,调 `coordinator.collect_consumable_delivers` 消费上游。Coordinator 不持有 lifecycle,只做路由与查询。Scheduler 不感知 store,只调 coordinator 的恢复与查询接口。

## 3. GraphInstanceStore

### 3.1 GraphMetadata(5 字段)

```python
class GraphMetadata(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    graph_instance_id: int        # Snowflake ID,持久化唯一 key
    spec_id: int                  # FK → graph_specs.spec_id
    parent_instance_id: int | None  # 嵌套子图时父实例 ID,顶层为 None
    parent_node: str | None       # 父图中创建此实例的 node 名,顶层为 None
    status: GraphInstanceStatus   # running / paused / stopped / crashed / completed / failed
```

`frozen=True, extra="forbid"`。Scheduler bookkeeping 字段(`instance_seq` / `iteration_count` / `activated_sources` / `pending_dispatches`)不在这里。这四个全是运行时视图,recovery 时从 `node_states` 与 `deliver_states` 派生,不持久化。

### 3.2 单一 status 写入路径

Status 更新只走一条路: `instance_store.update_status(graph_instance_id, GraphInstanceStatus)`。

- `GraphOrchestrator` 在 `create_and_run` 时写 `RUNNING`,完成时写 `COMPLETED`,`GraphInterrupt` 时写 `PAUSED`,其他异常写 `CRASHED`。
- `GraphRecoveryService` 恢复时写 `RUNNING`。
- `GraphControlService` 的 pause / stop / resume 命令写对应 status。
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

没有 `PENDING`。Records 在 `begin_invocation` 时直接 INSERT 为 `RUNNING`。没有 `SUPERSEDED`。Suspend 的 invocation 保持 `RUNNING` 加 `suspended=True`(独立字段,不是独立 status)。

### 4.3 Lifecycle 方法

```python
class NodeStateStore(ABC):
    def __init__(self, graph_instance_id: int) -> None: ...

    # Lifecycle
    @abstractmethod
    def begin_invocation(self, node_name: str) -> InvocationContext: ...
    @abstractmethod
    def complete_invocation(self, invocation: InvocationContext, state: dict[str, Any]) -> None: ...
    @abstractmethod
    def suspend_invocation(self, invocation: InvocationContext, snapshot: dict[str, Any]) -> None: ...
    @abstractmethod
    def crash_invocation(self, invocation: InvocationContext) -> None: ...
    @abstractmethod
    def cancel_invocation(self, invocation: InvocationContext) -> None: ...
    @abstractmethod
    def finalize_invocation(self, invocation: InvocationContext) -> None: ...

    # Query
    @abstractmethod
    def load_latest(self, node_name: str) -> NodeInvocationRecord | None: ...
    @abstractmethod
    def load_latest_completed(self, node_name: str) -> NodeInvocationRecord | None: ...
    @abstractmethod
    def load_by_invocation_id(self, node_name: str, invocation_id: int) -> NodeInvocationRecord | None: ...
    @abstractmethod
    def query_versions(self, node_name: str, status_filter: set[InvocationStatus] | None = None) -> list[NodeInvocationRecord]: ...
    @abstractmethod
    def list_nodes(self) -> list[str]: ...
    @abstractmethod
    def query_all(self, status_filter: set[InvocationStatus]) -> list[NodeInvocationRecord]: ...
    @abstractmethod
    def clear(self) -> None: ...
```

### 4.4 begin_invocation 行为

1. Orphan 清理: 如果存在 prior 非 suspended 的 RUNNING record,标记为 CRASHED。
2. `version = max(所有已有版本号) + 1`(不是 `load_latest_completed + 1`,避免 CRASHED 或 RUNNING 版本号相同导致 UNIQUE 冲突)。
3. `parent_version` 从 `load_latest_completed` 取,无则 None。
4. INSERT 新 record,status = RUNNING,suspended = False,invocation_id = Snowflake ID。
5. 返回 `InvocationContext`(invocation_id + node_name + version + parent_version)。

Suspended 的 RUNNING record 不动。它是有效的 rebuild 源,新 invocation 会建立新的 version,不覆盖它。

### 4.5 CAS(compare-and-swap)

严格与容忍分层:

| 方法 | CAS 语义 | 失败行为 |
|------|----------|----------|
| `complete_invocation` | STRICT: `WHERE status='running' AND suspended=0` | `rowcount == 0` 抛 `InvocationStateError` |
| `suspend_invocation` | STRICT: 同上 | 抛 `InvocationStateError` |
| `cancel_invocation` | STRICT: 同上 | 抛 `InvocationStateError` |
| `crash_invocation` | TOLERANT: 已终态则 no-op | 不抛 |
| `finalize_invocation` | TOLERANT: suspended RUNNING 与终态不动,orphan RUNNING 转 CRASHED | 不抛 |

`InvocationStateError` 在 `modex_graph/exceptions.py`,ordinary `Exception`,与 `RoutingError` 同级。表示 lost race 或 duplicate transition attempt。

### 4.6 三种实现

- `NullNodeStateStore`: `begin_invocation` 返回有效 `InvocationContext`(generated invocation_id, version=0, parent_version=None),其余 no-op,所有 query 返回 None / empty。
- `InMemoryNodeStateStore`: dict 加 `list[NodeInvocationRecord]`。CAS 通过 status check 实现(`if current.status != RUNNING or current.suspended: raise`)。
- `SqliteNodeStateStore`: `UPDATE ... WHERE status='running' AND suspended=0` 加 `rowcount == 0` 检测。共享 `sqlite3.Connection`。

### 4.7 node_states 表

```sql
CREATE TABLE IF NOT EXISTS node_states (
    node_state_id       BIGINT  PRIMARY KEY,
    graph_instance_id   BIGINT  NOT NULL,
    node_name           TEXT    NOT NULL,
    version             INTEGER NOT NULL DEFAULT 0,
    parent_version      INTEGER,
    status              TEXT    NOT NULL DEFAULT 'running'
                        CHECK (status IN ('running', 'completed',
                                          'canceled', 'crashed')),
    invocation_id       BIGINT  NOT NULL DEFAULT 0,
    state_json          TEXT    NOT NULL CHECK (json_valid(state_json)),
    suspended           INTEGER NOT NULL DEFAULT 0,
    created_at          INTEGER NOT NULL,
    updated_at          INTEGER NOT NULL,
    UNIQUE (graph_instance_id, node_name, version)
);
```

CHECK 约束只允许 4 个 status 值。索引: `idx_node_states_latest`(graph_instance_id, node_name, version DESC)、`idx_node_states_node`、`idx_node_states_status`、`idx_node_states_cross`(graph_instance_id, node_name, invocation_id)、`idx_node_states_global`(graph_instance_id, invocation_id DESC)。

`state_json` 存 full state snapshot(`GraphState.checkpoint()` 的结果)。不是 delta,不是 per-field channel value。

### 4.8 NodeInvocationRecord(值对象)

```python
class NodeInvocationRecord(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    invocation_id: int
    graph_instance_id: int
    node_name: str
    version: int
    parent_version: int | None
    status: InvocationStatus
    state_json: dict[str, Any]       # full state snapshot
    suspended: bool = False
    created_at: int
    updated_at: int
```

`updated_at` 是最后 transition 时间。`rebuild_main_state` 用它排序。

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

**InMemoryDeliverStore(二态)**:
```
PENDING → CONSUMED
```
- `accumulate` 创建 PENDING。
- `mark_consumed` 设 `status = CONSUMED`,记 `consumed_by_invocation_id`。
- `promote_consumed` 删除该 invocation 消费的记录(已完成,不需要了)。

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
    def rebuild_main_state(self) -> dict[str, Any]: ...
    def load_for_recovery(self) -> RecoveryContext: ...
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

### 6.4 rebuild_main_state

`rebuild_main_state()` 返回重建后的 main_state dict。

算法:

1. 对每个有 state snapshot 的 node,查 `{COMPLETED, RUNNING}` 版本。
2. 过滤到 `COMPLETED` 或 `suspended=True RUNNING`(两者都是 full state snapshot)。
3. 取该 node 的 single newest record: `max(valid, key=lambda r: (r.updated_at, r.invocation_id))`。SQL 等价 `ORDER BY updated_at DESC, invocation_id DESC LIMIT 1`。
4. 跨 node 按 `invocation_id`(全局 Snowflake 时间序)排序,逐个 `dict.update(record.state_json)` 合并。

没有 SUPERSEDED 两阶段 apply。每个 node 只贡献一条最新 snapshot。Snowflake 时间序保证跨 node 因果序。

### 6.5 load_for_recovery

`load_for_recovery()` 返回 `RecoveryContext`:

```python
class RecoveryContext(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    metadata: GraphMetadata
    node_states: dict[str, NodeInvocationRecord | None]   # 各 node 最新 invocation
    rebuilt_main_state: dict[str, Any]                     # 重建后的 main_state
```

内部做一项自动修复: `_auto_promote_completed_invocations`。扫描所有 node 的 deliver_store 的 CONSUMED_PENDING 记录,如果其 `consumed_by_invocation_id` 对应的 invocation 已 COMPLETED(通过 `load_by_invocation_id` 检查),则 `promote_consumed`。修复 crash 在 save COMPLETED 与 `promote_delivers` 之间的状态不一致。

Scheduler 在 `run_async` 顶部调此方法,直接用 `rebuilt_main_state`,无需额外调 rebuild。

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

`Node.run(ctx, *, graph)` 是 node 生命周期的唯一入口。它统一处理状态转移、持久化、版本链、deliver 收集与 submit。Node 子类只实现 `async execute(ctx, integrated_input) -> None`(async void,无返回值)。

完整流程:

1. **resume 检查**(begin 之前,只读): `node_state_store.load_latest(self.name)`。如果最新 invocation 是 suspended,这次是 resume from suspend,用 state snapshot 作为 integrated input base,并追加消费 suspend 后新到的 PENDING delivers(跳过 CONSUMED_PENDING,那些是 suspend 前已消费的)。
2. **begin_invocation**: `ctx.node_state_store.begin_invocation(self.name)` 创建新 invocation。设置 `ctx.current_invocation`。
3. **try 块**: integrate + execute + submit + complete。
   - **integrate**: resume 时用 `prev.state_json` 作 base,追加新 PENDING delivers 的 payload,一起 `input_integrator.integrate`。正常时调 `coordinator.collect_consumable_delivers`,若有 delivers 则 `mark_delivers_consumed`,再用 `input_integrator.integrate` 整合为 `IntegratedInput`。
   - **execute**: 调 `await self.execute(ctx, integrated)`。async void,无 NodeResult。
   - **submit**: `self.submit(ctx)` 调 `_submit`,按 `next_node` 分组,每组调 `ctx.dispatch(target, state_update={"delivered": payload, ...})`。deliver payload,不是 state delta。
   - **complete**: `node_state_store.complete_invocation(invocation, ctx.state.checkpoint())`。保存 COMPLETED + full snapshot。
4. **except GraphInterrupt**: `snapshot = ctx.state.checkpoint()`,调 `node_state_store.suspend_invocation(invocation, snapshot)`,re-raise。
5. **except GraphBubbleUp**: `node_state_store.cancel_invocation(invocation)`,re-raise。
6. **except Exception**: `node_state_store.crash_invocation(invocation)`,re-raise。
7. **finally**: `node_state_store.finalize_invocation(invocation)`。安全网。

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

Resume from suspend 时跳过此流程,直接用前一 invocation 的 state snapshot 作为 integrated input,避免 double-effect。

## 10. 恢复流程

### 10.1 ParallelScheduler 恢复

`ParallelScheduler._restore_from_recovery(ctx, recovery)` 在 `run_async` 顶部调 `load_for_recovery()` 后执行:

1. **重建 state**: `ctx.state = type(ctx.state).model_validate(recovery.rebuilt_main_state)`(若有 prior state)。
2. **派生 iteration_count**: `self._iteration_count = len(ctx.coordinator.node_state_store.query_all({InvocationStatus.COMPLETED}))`。从 COMPLETED invocation 数量派生,不读持久化的 bookkeeping 字段。
3. **重置 instance_seq**: `self._instance_seq = 0`(纯内存临时量)。
4. **清空运行时结构**: `_activated_sources` / `_pending_dispatches` / `_on_receive_queue` / `_instances` / `_active` / `_ready` 全部重置。
5. **`_redispatch_from_recovery(recovery)`**: 基于 node status 重新 dispatch。没有 "COMPLETED + delivers" 快捷路径。CRASHED 的 node 重新 dispatch,suspended 的 RUNNING 走 resume 路径。
6. **`_rebuild_pending_from_delivers(ctx, recovery)`**: 扫描所有 node 的 deliver_store 的 PENDING delivers(无条件扫描,不限于 COMPLETED node)。对每个 target node 解析 trigger mode:
   - `ON_ALL_PREDS`: deliver 进入 pending dispatch queue。`_recheck_pending` 在 group complete + reachability clear 时 fire。
   - `ON_RECEIVE`: 如果 target 无 in-flight instance,创建新 instance 处理 delivers。如果 target in-flight(被 re-dispatch),running instance 会通过 `collect_consumable_delivers` 消费这些 delivers。
7. **`_recheck_pending()`**: fire 任何 ready 的 ON_ALL_PREDS node。
8. **Fresh start**: 如果没有 instance 被 recover 且没有任何 prior invocation,创建 entry instance。

### 10.2 LinearScheduler 恢复

LinearScheduler 的恢复是 4 行:

```python
recovery = ctx.coordinator.load_for_recovery()
has_prior_state = any(v is not None for v in recovery.node_states.values())
if has_prior_state and recovery.rebuilt_main_state:
    ctx.state = type(ctx.state).model_validate(recovery.rebuilt_main_state)
```

之后从 `self.graph.entry_node` 开始顺序循环。Resume routing 是 graph author 的 concern: entry node 读 `state.resume_target` 路由到目标 node。如果 `state.resume_target` 未设置,从 entry_node 正常开始。

### 10.3 GraphRecoveryService

`GraphRecoveryService` 两种恢复类型共享同一流程:

1. 从 `instance_store` 加载 `GraphMetadata`。
2. `factory.create(graph_instance_id, instance_store)` 重建 coordinator(用注入的 factory,不是 `create_null_coordinator`)。
3. 构造 `GraphInstance(metadata, coordinator)`。
4. `instance_store.update_status(gid, RUNNING)`。
5. 调 `engine_factory.create_and_run(instance)`。

`recover_crashed()`: 查 `CRASHED` + `RUNNING` 实例(进程被 kill 时 graph 留在 RUNNING——没有活进程在跑它),全部走共享恢复流程。返回恢复的 `graph_instance_id` 列表。

`resume(graph_instance_id)`: 加载单个实例,校验 status 为 PAUSED 或 STOPPED(CRASHED 不被手动 resume,COMPLETED / FAILED 是终态),走共享流程。

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

### 11.2 谨慎使用

串行门是 in-memory only,不跨 crash 持久化。Crash 时 queue 里的 dispatch 丢失。恢复时 `_rebuild_pending_from_delivers` 重新扫描 PENDING delivers,但 queued(未 fire)的 dispatch 状态不重建。

这意味着: 如果 ON_RECEIVE node 在有 queued dispatch 时 crash,恢复后这些 dispatch 不会自动重新 fire。它们对应的 delivers 是 PENDING(已 `accumulate` 但未 `mark_consumed`),会被 `_rebuild_pending_from_delivers` 扫到。如果 target 有 in-flight instance(被 re-dispatch),running instance 会消费它们。如果 target 无 in-flight instance,会创建新 instance 处理。但 queue 里的 dispatch 顺序不保留。

因此 ON_RECEIVE 标记谨慎使用。大多数 node 用 `ON_ALL_PREDS`(默认),它的 pending dispatch 会通过 `_recheck_pending` 重新评估,语义可恢复。

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
| 恢复 | 4 行:`load_for_recovery` + `model_validate` + entry_node 循环 | `_restore_from_recovery`: redispatch + rebuild pending + recheck |
| max_iterations | 在循环顶部 check | 在 `_execute_instance` 顶部 check + increment(同步) |

两个 scheduler 都调 `load_for_recovery()`,都从 entry_node 开始(LinearScheduler 直接循环,ParallelScheduler 创建 entry instance)。

## 14. 迁移历史

Workspace DB 的 migration 序列:

| Migration | 内容 | 影响 |
|-----------|------|------|
| `001_initial.sql` | 初始 schema。创建 `graph_instances`(5 基本列)、`node_states`(lifecycle 列 + CHECK(running/completed/canceled/crashed))、`deliver_states`(6 status 值)三表。 | 全新 workspace 的基线。 |

净结果:

- `graph_instances`: 5 个基本列(graph_instance_id, spec_id, parent_instance_id, parent_node, status)加 created_at / updated_at。无 bookkeeping_json。
- `node_states`: lifecycle 切片(version, parent_version, status, invocation_id, state_json, suspended, created_at, updated_at)。CHECK(running/completed/canceled/crashed)。
- `deliver_states`: per-node 消费状态机。CHECK 允许 4 个 `DeliverConsumptionStatus` + 2 个旧 `DeliverStatus` 值(向后兼容)。

`SqliteNodeStateStore._init_schema` 和 `SqliteDeliverStore._init_schema` 用 `CREATE TABLE IF NOT EXISTS` 创建表(与 001 DDL 一致)。`modex_graph` standalone 使用时直接创建;`modex_agent` workspace 使用时由 `MigrationRunner` 跑 001。

## 15. 已移除的概念

以下概念在当前实现中已删除,本文档不描述它们。列出供从旧文档迁移的读者参考:

- **Channels**: `BaseChannel` ABC、`LastValue`、`ReducerChannel`。State 字段不再用 `Annotated[T, ChannelSpec]` 声明。`GraphState` 是普通 Pydantic BaseModel,`checkpoint()` = `model_dump(mode="json")`。
- **Fork/merge**: `ctx.fork()` 仍存在但 scheduler 不调用。没有 fork-based state isolation。ParallelScheduler 用 per-task context shell 共享 `ctx.state`。
- **Generation-based conflict detection**: `WriteConflictDetector` ABC、`GenerationWriteTracker`、`InvalidUpdateError`。没有 multi-write 检测。
- **NodeResult / DispatchEvent / Command / Task**: `execute` 是 async void,无返回值。路由通过 `deliver` / `submit` + `ctx.dispatch(target, state_update={deliver payload})`。
- **Declarative deltas**: `state_update` 不再是 state delta。`complete_invocation` 存 full snapshot(`ctx.state.checkpoint()`),不存 delta。
- **PENDING / SUPERSEDED statuses**: `InvocationStatus` 只有 RUNNING / COMPLETED / CANCELED / CRASHED。Records begin as RUNNING。Suspend 用 `suspended=True` 字段,不是独立 status。
- **GraphMetadataStore**: 删除。身份与 status 持久化收敛到 `GraphInstanceStore`。
- **NodeState ABC**: 删除。Lifecycle + 版本链 + CAS 收敛到 `NodeStateStore`。没有旧 in-memory dict API(read / write / snapshot / restore / has)。
- **Bookkeeping 持久化**: `instance_seq` / `iteration_count` / `activated_sources` / `pending_dispatches` 不持久化。Recovery 时从 `node_states` 和 `deliver_states` 派生。`GraphMetadata` 修剪到 5 字段。
- **CheckpointStore ABC + CheckpointData**: 删除。没有单独的 scheduler checkpoint。State snapshot 在 `node_states.state_json`,scheduler 运行时结构是 in-memory。
- **DispatchStore**: 删除。Dispatch 通过 `deliver_store.accumulate` 记录,没有单独的 dispatch event 持久化。
- **SUPERSEDED 两阶段 rebuild**: `rebuild_main_state` 不再先 apply COMPLETED 再 apply SUPERSEDED。每个 node 只贡献一条最新 snapshot(COMPLETED 或 suspended RUNNING)。
- **`promote_delivers` 跨 invocation 升级**: 当前 `promote_delivers` 仍升级该 node 的所有 CONSUMED_PENDING delivers(不限 invocation_id),修复 resume 场景。但这是消费状态机的内部逻辑,不是 SUPERSEDED rebuild。

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
- `RecoveryContext`: `load_for_recovery` 的返回值(metadata, node_states, rebuilt_main_state)
- `GraphStateSnapshot`: `get_graph_state` 的返回值(metadata, nodes)

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
