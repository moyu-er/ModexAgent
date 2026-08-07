# Node 业务状态持久化设计

Status: triage:closed
Blocked by: none
Resolved: 2026-08-04

## Question

框架中存在两个声称是"node 状态持久化"的抽象，概念混淆：

- **`NodeStateStore`**（`node_state_store.py`）— per-node 状态快照的持久化抽象。append-only MVCC，schema 简洁（6 列）。零生产调用——从未接入 Node.run()。
- **`NodeState`**（`node_state.py`）— invocation 版本链 + 生命周期状态机 + 旧 in-memory dict API（read/write/snapshot/restore/has）。UPSERT-per-version，schema 丰富（11 列）。被 coordinator 调用。

两者写同名 `node_states` 表，schema 不兼容。

如何收敛？node 应如何管理自己的状态/生命周期/故障恢复？

## Resolution

### 设计哲学

**Node.run() 自管理生命周期/状态/故障恢复。** 各个 node 自己维护自己的持久化策略（in-memory 允许不用故障恢复，SQLite 用于跨进程恢复）。整个图的状态/快照从各个 node 的数据聚合。

### 设计历史：NodeStateStore 是正确设计，NodeState 是混乱叠加

`NodeStateStore` 的设计意图是正确的：per-node 状态持久化，append-only MVCC 快照，node 自己管。但它从未接入 Node.run()。

`NodeState` 是后来叠加的——它把三个不同关注点混在一个 ABC 里：
1. invocation 版本链（version, parent_version, invocation_id）
2. 生命周期状态机（status: PENDING→RUNNING→COMPLETED/CRASHED/...，suspended）
3. 旧 in-memory dict API（read/write/snapshot/restore/has — vestigial）

而且 coordinator 把 lifecycle 逻辑（begin/complete/suspend/crash/finalize）放在自己身上，node 通过 coordinator 间接访问——node 不自管理。

### 收敛方向：一个 ABC（`NodeStateStore`），lifecycle 移入 store

收敛到 `NodeStateStore`——它是正确的名字和正确的抽象。把 `NodeState` 的 invocation 版本链 + lifecycle 方法**移入** `NodeStateStore`，删除 `NodeState` 旧 API，删除 `NodeState` 名字。

**一个 graph instance 一个 store 实例**（不是 per-node）。store 内部按 `node_name` 路由。coordinator 持有一个 `_node_state_store: NodeStateStore`，不再是 `dict[str, NodeState]`。

### 收敛后的 `NodeStateStore` ABC

```python
class NodeStateStore(ABC):
    """Per-node 状态 + 生命周期持久化。一个 graph instance 一个 store 实例。

    Deep module: 版本链 + 生命周期状态机 + crash recovery + 1-active-invocation
    强制，背后是小接口。Node.run() 直接调 lifecycle 方法；coordinator 调 query
    方法做图级 rebuild。

    契约:
    - 一个 invocation 一条记录（(node_name, version) 唯一）。
    - begin_invocation 是 INSERT 新 version 记录；状态转换是**条件更新（CAS，ticket 31）**：
      `UPDATE ... WHERE (graph_instance_id, node_name, version) AND status='running'`。
      失配时 `complete`/`suspend`/`cancel` 严格抛 `InvocationStateError`；
      `crash`/`finalize`/orphan 清理幂等容忍（no-op）。终态 {COMPLETED, CANCELED, CRASHED}
      结构性不可变（无转换以终态为源）。
    - 同一 node 最多 1 个活跃（RUNNING）invocation — begin_invocation 强制。
    - 不用 PENDING — 记录直接创建为 RUNNING。
    - 线程契约（ticket 29/31c）：store 方法是同步方法，只在 event-loop 线程被调用；
      连接 caller-owned，store 永不关闭。
    - node 自己管理持久化策略: NullNodeStateStore (无持久化) /
      InMemoryNodeStateStore (内存, 无故障恢复) / SqliteNodeStateStore (SQLite, 跨进程恢复)。
    """

    # ── Lifecycle (Node.run() 直接调) ──

    def begin_invocation(self, node_name: str) -> InvocationContext:
        """创建新 invocation 记录 (status=RUNNING)。

        内部 recovery (caller 不可见):
        1. load_latest(node_name)
        2. 若 latest 是 RUNNING+suspended → 不动（HITL suspend，保留供 rebuild/resume 用）
        3. 若 latest 是 RUNNING (非 suspended, orphan) → 标记 CRASHED
        4. version = max(all versions) + 1
        5. parent_version = load_latest_completed(node_name).version (或 None)
        6. 生成 invocation_id (Snowflake)
        7. INSERT 新记录 (status=RUNNING)
        返回 InvocationContext(invocation_id, version, parent_version)。
        """

    def complete_invocation(self, invocation: InvocationContext, state: dict[str, Any]) -> None:
        """CAS: RUNNING → COMPLETED, state_json ← state (= ctx.state.model_dump(mode="json"), full snapshot)。
        失配（记录已终态）抛 InvocationStateError（ticket 31）。"""

    def suspend_invocation(self, invocation: InvocationContext, snapshot: dict[str, Any]) -> None:
        """CAS: RUNNING → RUNNING(suspended=True), state_json ← snapshot。
        失配抛 InvocationStateError（ticket 31）。"""

    def crash_invocation(self, invocation: InvocationContext) -> None:
        """CAS: RUNNING → CRASHED, state_json ← {}。
        失配幂等容忍（no-op）——标记 CRASHED 永远安全（ticket 31）。"""

    def cancel_invocation(self, invocation: InvocationContext) -> None:
        """CAS: RUNNING → CANCELED, state_json ← {}。
        失配抛 InvocationStateError（ticket 31）。"""

    def finalize_invocation(self, invocation: InvocationContext) -> None:
        """安全网 (Node.run() finally 调):
        - suspended RUNNING → 不动 (HITL, 等恢复)
        - orphan RUNNING (非 suspended) → CRASHED
        - COMPLETED / CANCELED / CRASHED → 不动
        """

    # ── Queries (Node.run() resume 检查 + coordinator rebuild) ──

    def load_latest(self, node_name: str) -> NodeInvocationRecord | None:
        """该 node 的最高 version 记录。"""

    def load_latest_completed(self, node_name: str) -> NodeInvocationRecord | None:
        """最新 COMPLETED 记录 (parent_version 计算 + recovery 用)。"""

    def query_versions(self, node_name: str, status_filter: set[InvocationStatus] | None = None) -> list[NodeInvocationRecord]:
        """该 node 的所有 version，version DESC。可选 status 过滤。"""

    def list_nodes(self) -> list[str]:
        """有记录的所有 node 名。"""

    def query_all(self, status_filter: set[InvocationStatus]) -> list[NodeInvocationRecord]:
        """所有 node 中匹配 status 的记录 (rebuild_main_state 用)。"""

    # ── Cleanup ──

    def clear(self) -> None:
        """删除本 graph instance 的所有记录。"""
```

**为什么 lifecycle 在 store 上而不是 coordinator 上：** lifecycle 状态 IS state record 上的一个字段。"invocation lifecycle" 和 "node state persistence" 是同一关注点——分开会创建一个浅 pure-persistence store + 一个独立的 lifecycle manager 重复 recovery/version 逻辑。把 lifecycle 放在 store 上让它成为 deep module：`begin_invocation(name) → InvocationContext` 隐藏 recovery + version 计算 + 条件写入（CAS，ticket 31）+ 1-active-invocation 强制。deletion test：删掉 store，所有复杂度重新出现在 node/coordinator 中。

### Schema

一张表。现有 `SqliteNodeState` 的 11 列 schema，**移除 PENDING**：

```sql
CREATE TABLE node_states (
    node_state_id     BIGINT PRIMARY KEY,           -- Snowflake
    graph_instance_id BIGINT NOT NULL,             -- FK -> graph_instances
    node_name         TEXT NOT NULL,
    version           INTEGER NOT NULL,            -- per-node 单调递增 (0,1,2…)
    parent_version    INTEGER,                     -- nullable (NULL = v0)
    invocation_id     BIGINT NOT NULL,             -- Snowflake, 全局时间序
    status            TEXT NOT NULL DEFAULT 'running'
                      CHECK (status IN (
                          'running','completed','canceled','crashed'
                      )),
    state_json        TEXT NOT NULL,               -- full snapshot (ctx.state.model_dump())
    suspended         INTEGER NOT NULL DEFAULT 0,  -- 0/1
    created_at        INTEGER NOT NULL,            -- epoch ms (转换更新时保留)
    updated_at        INTEGER NOT NULL,            -- epoch ms (转换更新时刷新)
    UNIQUE (graph_instance_id, node_name, version)
);
```

**写入语义**（ticket 31 CAS 裁决后）：
- `begin_invocation` = INSERT 新 version 记录（UNIQUE 冲突即框架 bug）
- 状态转换 = 条件 UPDATE：`WHERE (graph_instance_id, node_name, version) AND status='running'`，验证 1 row affected
- `DO UPDATE SET`: `status`, `state_json`, `suspended`, `updated_at`
- **不可变** (创建后不动): `node_state_id`, `graph_instance_id`, `node_name`, `version`, `parent_version`, `invocation_id`, `created_at`

### Node.run() 通过 `ctx.node_state_store` 直接调 store

Node.run() 自管理生命周期——coordinator 不在 lifecycle 路径中：

```python
async def run(self, ctx, ...):
    store = ctx.node_state_store

    # Resume 检查 (只读, begin 之前)
    prev = store.load_latest(self.name)
    is_resume = prev is not None and prev.suspended

    # 自管理 lifecycle: node 直接调 store
    invocation = store.begin_invocation(self.name)
    ctx.current_invocation = invocation
    try:
        # ... integrate, execute, retry (不变) ...
        store.complete_invocation(invocation, ctx.state.model_dump(mode="json"))
        # deliver promotion 解耦 — node 显式调 coordinator
        ctx.coordinator.promote_delivers(self.name, invocation.invocation_id)
        return result
    except GraphInterrupt:
        store.suspend_invocation(invocation, ctx.state.model_dump(mode="json"))
        raise
    except GraphBubbleUp:
        store.cancel_invocation(invocation)
        raise
    except Exception:
        store.crash_invocation(invocation)
        raise
    finally:
        store.finalize_invocation(invocation)
```

### Coordinator 缩小

coordinator 退出 lifecycle 路径。**移除** 6 个 lifecycle 方法 + `_node_states: dict[str, NodeState]`。

**保留** coordinator：`register_node`（deliver only）、`route_deliver`、`collect_consumable_delivers`、`mark_delivers_consumed`、`promote_delivers`、`rebuild_main_state`、`load_for_recovery`。

```python
class GraphPersistenceCoordinator:
    def __init__(self, graph_instance_id, instance_store: GraphInstanceStore,
                 node_state_store_factory, deliver_store_factory):
        self._graph_instance_id = graph_instance_id
        self._instance_store = instance_store  # ticket 22：替代 graph_metadata_store
        # 一个 store for all nodes (不是 dict[str, NodeState]):
        self._node_state_store = node_state_store_factory.create(graph_instance_id)
        self._deliver_stores: dict[str, DeliverStore] = {}

    def register_node(self, node_name, deliver_store=None):
        # 不再注册 per-node NodeState。只注册 deliver store。
        self._deliver_stores[node_name] = deliver_store or self._default_ds_factory.create()

    def node_state_store(self) -> NodeStateStore:
        """暴露 store 给 GraphContext 接线。"""
        return self._node_state_store
```

### Graph 级 state rebuild

`rebuild_main_state` 从 store 取最新 full snapshot（不 replay delta，不做两阶段 apply）：

```python
def rebuild_main_state(self) -> dict[str, Any]:
    store = self._node_state_store
    # 查所有 COMPLETED + suspended RUNNING 记录（full snapshot）
    candidates = store.query_all({InvocationStatus.COMPLETED})
    candidates += store.query_all({InvocationStatus.RUNNING})  # 含 suspended

    # 过滤出有有效 state_json 的记录（crash/cancel 存 {}，跳过）
    candidates = [r for r in candidates if r.state_json and (
        r.status == InvocationStatus.COMPLETED or r.suspended
    )]

    if not candidates:
        return {}

    # 取 updated_at 最大的（完成时间序，不是 invocation_id 开始序）
    # 共享 state 模式下，最新 snapshot 包含所有之前 COMPLETED 的累积结果
    # tiebreaker（ticket 26）：updated_at 极端碰撞时用 invocation_id DESC
    latest = max(candidates, key=lambda r: (r.updated_at, r.invocation_id))
    return dict(latest.state_json)
```

**图状态从最新 full snapshot 重建** — 每个 node 的 `complete_invocation` 存 `ctx.state.model_dump(mode="json")`（full snapshot）。`suspend_invocation` 存同一格式。共享 state 模式下，最新的 COMPLETED/suspended snapshot 包含所有之前的结果。不需要 delta replay，不需要 commit_seq，不需要两阶段 apply。与 ticket 26 决议一致。

**为什么按 `updated_at` 排而不是 `invocation_id`** — `invocation_id` 是开始时间序，`updated_at` 是完成时间序。并发 instance 的开始序 ≠ 完成序。最新完成的 snapshot 包含一切历史（共享 state 累积）。

### ReAct 版本链

同一个 node 被多次调用（ReAct 环）。每次调用 = 新 `begin_invocation` = 新 version：

| 调用 | version | parent_version | invocation_id | status (complete 后) |
|------|---------|----------------|---------------|----------------------|
| 1st  | 0       | None           | snowflake_A   | COMPLETED            |
| 2nd  | 1       | 0              | snowflake_B   | COMPLETED            |
| 3rd  | 2       | 1              | snowflake_C   | COMPLETED            |

- `begin_invocation` 计算 `version = max(all versions) + 1`（per-node）
- `parent_version = load_latest_completed(node_name).version`（链到同 node 上次 COMPLETED）
- `load_latest(node_name)` → 返回 v2（最高 version）
- `load_latest_completed(node_name)` → 返回 v2（最新 COMPLETED）

**crash 场景**：v1 crash（orphan RUNNING），下次 `begin_invocation`（v2）标记 v1 CRASHED。`parent_version = v0.version = 0`（v1 未 COMPLETED，`load_latest_completed` 返回 v0）。链跳过 crash 的 version——正确，因为 crash 的 version 未产出有用状态。

### 1-active-invocation 强制

store 的 `begin_invocation` 内部强制（不是 coordinator）：
1. `load_latest(node_name)`
2. 若 RUNNING+suspended → 不动（HITL suspend，保留供 rebuild/resume 用。新 invocation 直接创建，旧记录保留）
3. 若 RUNNING（非 suspended，orphan）→ 标记 CRASHED
4. 创建新 RUNNING 记录

没有代码路径能为同一 node 创建第二个 RUNNING 记录而不先处理前一个 orphan。suspended RUNNING 保留不影响 1-active-invocation——`load_latest` 返回新记录（更高 version），旧 suspended 记录只供 `rebuild_main_state` 和 resume 检查用。

### 持久化策略（node 自选）

| 实现 | 特征 | 用途 |
|------|------|------|
| `NullNodeStateStore` | 全 no-op | ReActAgent per-turn（无持久化） |
| `InMemoryNodeStateStore` | 内存 dict，无故障恢复 | 测试 / 单进程临时图 |
| `SqliteNodeStateStore` | SQLite upsert-per-version，跨进程恢复 | 生产 |

node 不选择自己的持久化后端——graph engine 选择（通过 coordinator factory）。但不同策略允许不同能力：in-memory 允许不用故障恢复（crash 后从 fresh start），SQLite 提供跨进程恢复。

### 旧 API 处置

**移除 `NodeState` 旧 API**（read/write/snapshot/restore/has）— vestigial，Node.run() 从不调。`SqliteNodeState` 的 in-memory dict shim（read→None, write→pass）是死代码。

**移除 `NodeState` 名字本身** — 被 `NodeStateStore` 取代。

**移除 `NodeStateFactory`** → 替换为 `NodeStateStoreFactory`（创建一个绑定 graph_instance_id 的 store）。

**移除 `PENDING` status** — 记录直接创建为 RUNNING（不两步保存 PENDING→RUNNING）。

**移除 coordinator 的 6 个 lifecycle 方法** — 移入 store。

**移除 coordinator 的 `_node_states: dict[str, NodeState]`** — 改为一个 `_node_state_store: NodeStateStore`。

**移除 coordinator 的 `load_latest_invocation`** — node 直接调 `store.load_latest(self.name)`。

### `deliver promotion` 解耦

当前 `complete_invocation` 内部调 `promote_delivers`。收敛后 store 的 `complete_invocation` **不知道 delivers**（关注点分离）。Node.run() 显式调两者：
```python
store.complete_invocation(invocation, ctx.state.model_dump(mode="json"))
ctx.coordinator.promote_delivers(self.name, invocation.invocation_id)
```

### `GraphContext` 变更

新增 `ctx.node_state_store: NodeStateStore`——**实现为只读 property，委托 `ctx.coordinator.node_state_store()`**（2026-08-04 一致性审计修订；原文「coordinator 在创建 context 时设置」有误——coordinator 不创建 context，orchestrator / ReActAgent 才创建）。

property 委托的理由：coordinator 本来就在 ctx 上（Null coordinator 暴露 `NullNodeStateStore`），`fork()` 继承 coordinator 即自动继承 store，**零构造点改动**（orchestrator、ReActAgent、fork 三处都不需要新参数）。单一事实源在 coordinator，无平行接线。

`ctx.coordinator` 保留（deliver 路由 + promote_delivers）。

## 删除清单

| 删除 | 理由 |
|------|------|
| `NodeState` ABC 旧 API（read/write/snapshot/restore/has） | vestigial，Node.run() 从不调 |
| `NodeState` 名字本身 | 被 `NodeStateStore` 取代 |
| `node_state.py` → 重命名/重构为 `node_state_store.py`（或合并） | 收敛到一个 ABC |
| coordinator 的 6 个 lifecycle 方法 | 移入 store |
| `_node_states: dict[str, NodeState]` | 改为一个 `_node_state_store` |
| `PENDING` status | 直接创建 RUNNING |
| `load_invocation` 方法 | `load_latest` + `query_versions` 覆盖 |
| `NodeStateFactory` | 替换为 `NodeStateStoreFactory` |

**注意**：原来的 `node_state_store.py`（append-only 6 列 schema）**不保留**——它的 ABC 接口被收敛后的 `NodeStateStore` 取代，它的 append-only 写模式被「一 invocation 一记录 + 条件写入」取代。但它的**设计意图**（per-node 状态持久化，node 自管）保留并体现在收敛后的 ABC 中。

## Resolution criteria

- ✅ 两层状态关系定义 — node 业务状态 = `ctx.state` 字段，被 full snapshot 覆盖。node 持久化通过 `NodeStateStore`（invocation 版本链 + lifecycle）
- ✅ `NodeStateStore` 的命运 — 保留并收敛（吸收 NodeState 的 invocation 版本链 + lifecycle 方法）
- ✅ `NodeState` 旧 API 的命运 — 移除（vestigial）
- ✅ 表名冲突解决 — 收敛到一个 ABC，一张表，一套 schema
- ✅ node 业务状态与图级状态的边界 — node 通过 `ctx.state`（GraphState）写业务状态（imperative 唯一写模式），通过 `ctx.node_state_store` 管理 lifecycle/版本链。图级 state 从最新 COMPLETED 的 full snapshot 重建（不 replay delta）
- ✅ 对 GraphInterrupt suspend/resume 的影响 — `suspend_invocation` 存 `ctx.state.model_dump()`（full snapshot），`load_latest` 在 resume 时读取
- ✅ node 自管理 lifecycle/state/fault-recovery — Node.run() 通过 `ctx.node_state_store` 直接调 begin/complete/suspend/crash/finalize
- ✅ 各 node 自己维护持久化策略 — Null/InMemory/Sqlite 三种 store 实现
- ✅ 1-active-invocation — store 的 begin_invocation 强制
- ✅ ReAct 环版本链 — per-node version counter + parent_version 链
- ✅ 与 ticket 26/33 一致 — `complete_invocation` 存 full snapshot（`ctx.state.model_dump()`），不存 delta；`rebuild_main_state` 从最新 COMPLETED snapshot 重建
