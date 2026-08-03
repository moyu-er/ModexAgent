# 分布式持久化与 Node 生命周期统一调度

Status: partial — persistence layer implemented and tested; production wiring (coordinator strategy injection, metadata store convergence, scheduler lifecycle ordering) pending (see §9)
Date: 2026-08-04

本文档描述 `modex_graph` 分布式持久化与 Node 生命周期统一调度的当前实现。目标读者是需要理解和维护该系统的开发者。

> **Design rationale provenance**: 本文档的设计决策由 11 项 critical finding(F1-F11)驱动,finding → task 映射见 `issues/history/IMPLEMENTATION-PLAN-V2.md` §1.3。

## 1. 概述

分布式持久化用 per-node 的版本链存储取代了单一的 `CheckpointData` JSON blob。全局图状态由 graph metadata 加上各 node 的 invocation 版本记录拼装而成,不再序列化为一个快照。

`GraphPersistenceCoordinator` 是中央编排器。它把 node 生命周期事件(begin / complete / cancel / suspend / crash / finalize)与持久化路由(route deliver / collect consumable / mark consumed / promote)统一在自己的接口上。scheduler 不感知任何持久化 store,只调 coordinator 的方法。

三种持久化策略,走同一套 ABC:

- **Null**: 全 no-op。无持久化需求时使用,ReActAgent per-turn 路径的默认策略。
- **Memory**: 进程内 dict。测试与单进程临时图。
- **SQLite**: 文件持久化。需要 crash recovery 的生产图。

`GraphInstance` 是运行时对象,持有 coordinator 加上可序列化的 `GraphMetadata`。`GraphOrchestrator` 用 `_active_instances` 注册表管理 GraphInstance 生命周期,coordinator 的存活期与 GraphInstance 绑定,而不是与单次 `_execute` 调用栈绑定。

## 2. 架构

### 2.1 持久化分层

三层持久化,各自有独立的 store 与生命周期:

| 层 | 内容 | 存储位置 | 拥有者 | 生命周期 |
|----|------|----------|--------|----------|
| Graph metadata | `graph_instance_id`, `status`, `instance_seq`, `iteration_count`, `activated_sources`, `pending_dispatches` | `graph_metadata` 表(SQLite) | coordinator(per-GraphInstance) | 随 GraphInstance 对象 GC(Memory) / 持久化(SQLite) |
| Node invocation | `invocation_id`, `node_name`, `version`, `parent_version`, `status`, `state_json`, `suspended` | `node_states` 表 | 各 node 的 `NodeState` | 随 GraphInstance 对象 GC(Memory) / 持久化(SQLite) |
| Deliver | `deliver_id`, `source_node`, `source_invocation_id`, `consumed_by_invocation_id`, `content`, `status` | `deliver_states` 表 | 各 node 的 `DeliverStore`(per-node) | 随 GraphInstance 对象 GC(Memory) / 持久化(SQLite) |

关键点: `DeliverStore` 是 per-node 的。每个 node 持有自己的 `deliver_store` 引用,表示"我收到的内容"。coordinator 在 `register_node` 时为每个 node 创建 deliver_store 引用,node 可复用 graph 默认实现,也可独立自定义策略。

`main_state` 不再单独持久化。恢复时从各 node 的 COMPLETED invocation 记录按全局时间序重建。

### 2.2 GraphPersistenceCoordinator

`GraphPersistenceCoordinator` 是统一调度 node 生命周期事件与持久化路由的中央编排器。它持有:

- `graph_instance_id`: 绑定所有 store 到同一次图运行的持久化 key。
- `graph_metadata_store`: graph 实例级元数据 store。
- `node_states: dict[str, NodeState]`: per-node 的 invocation 版本链。
- `deliver_stores: dict[str, DeliverStore]`: per-node 的 deliver 累积与消费 store。

它的方法分四组:

**注册与路由**

- `register_node(node_name, node_state=None, deliver_store=None)`: 注册一个 node 的持久化策略。`None` 表示用构造时注入的默认工厂。
- `get_deliver_store(node_name)`: 外部查询某 node 的 deliver_store。
- `route_deliver(target_node, content, source_node, source_invocation_id)`: 把一个 deliver 路由到 target node 的 deliver_store。target 为 `GraphNode.END` 时跳过(返回 None),target 无注册 store 时抛 `RoutingError`。

**消费**

- `collect_consumable_delivers(node_name, invocation_id)`: 委托 `deliver_store.query_consumable`,返回 PENDING 加 CONSUMED_PENDING(SQLite)或仅 PENDING(Memory)。
- `mark_delivers_consumed(node_name, deliver_ids, invocation_id)`: 委托 `deliver_store.mark_consumed`。
- `promote_delivers(node_name, invocation_id)`: 升级该 node 的所有 CONSUMED_PENDING delivers,不限当前 invocation_id。这修复了 resume 场景下被取代版本(superseded)的 delivers 不被新 invocation 完成时升级的问题。

**生命周期**

- `begin_invocation(node_name) -> InvocationContext`: 创建新 invocation,PENDING 转 RUNNING。内部计算 `parent_version`(从 `load_latest_completed`)和 `version`(`max(所有已有版本) + 1`)。如果存在 suspended 的 RUNNING invocation,先标记为 SUPERSEDED。如果存在 orphan PENDING 或非 suspended 的 RUNNING,标记为 CRASHED(安全网)。
- `complete_invocation(invocation, state)`: 保存 COMPLETED,然后调 `promote_delivers`。
- `cancel_invocation(invocation)`: 保存 CANCELED(终态)。
- `suspend_invocation(invocation, state_snapshot)`: 保存 RUNNING 加 `suspended=True`,state_snapshot 存入 `state_json`。GraphInterrupt 路径,不走 crash/cancel。
- `crash_invocation(invocation)`: 保存 CRASHED(终态)。
- `finalize_invocation(invocation)`: 安全网。在 `Node.run()` 的 `finally` 块调用。suspended 的 RUNNING 不动,SUPERSEDED 不动,orphan PENDING 或非 suspended RUNNING 标记为 CRASHED。

**恢复与查询**

- `load_for_recovery() -> RecoveryContext`: 加载 metadata 加各 node 最新状态加重建的 main_state。内部自动 promote 那些消费方 invocation 已 COMPLETED 但 delivers 仍为 CONSUMED_PENDING 的记录。
- `rebuild_main_state() -> dict`: 按 `invocation_id`(全局 Snowflake 时间序)排序 COMPLETED 记录并 apply 其 `state_json`,最后 apply SUPERSEDED 记录的 state snapshot。
- `load_latest_invocation(node_name)`: 加载 node 最新 invocation,用于 resume 判断。
- `get_graph_state(node_status_filter=None) -> GraphStateSnapshot`: 收集 metadata 加各 node 版本列表。
- `close()`: 关闭资源(SQLite connection 等)。由 `GraphOrchestrator.unregister_instance` 调用。

### 2.3 持久化策略

三种实现,各自有完整的 Null / Memory / SQLite 变体:

| 策略 | GraphMetadataStore | NodeState | DeliverStore | 适用场景 |
|------|-------------------|-----------|--------------|----------|
| Null | `NullGraphMetadataStore`(no-op) | `NullNodeState`(no-op) | `NullDeliverStore`(in-memory queue,无状态机) | ReActAgent per-turn;不需要持久化的图 |
| Memory | `MemoryGraphMetadataStore`(dict) | `SimpleNodeState`(内存 dict) | `InMemoryDeliverStore`(二态) | 测试;单进程临时图 |
| SQLite | `SqliteGraphMetadataStore` | `SqliteNodeState` | `SqliteDeliverStore`(三态) | 生产;需要 crash recovery 的图 |

能力边界:

- **Null**: 无持久化。`begin_invocation` 仍创建 `InvocationContext`(提供 `invocation_id` 加 `version` 原语),其余 no-op。`NullDeliverStore` 维护一个 in-memory queue,`mark_consumed` 直接移除记录。
- **Memory**: 单次执行流程内的数据流转。deliver 投递到消费,当前执行状态,graph metadata。不保证 crash recovery,内存对象消失数据丢失。这是设计语义,不是缺陷。
- **SQLite**: 完整能力。deliver 投递到消费(跨恢复),node 状态加版本链(完整 MVCC),graph metadata 持久化,crash recovery,消费幂等(跨恢复)。

SQLite 策略的多个 store(NodeState / GraphMetadata / Deliver)共享同一个 `sqlite3.Connection`,避免连接增殖。connection 由调用方拥有,store 不关闭它。

### 2.4 GraphInstance

`GraphInstance` 是运行时 class(不是 frozen Pydantic)。它配对持有:

- `metadata: GraphMetadata`: 可序列化的 frozen Pydantic 值对象,由 `GraphMetadataStore` 存储。携带 identity、status、scheduler bookkeeping 字段。
- `coordinator: GraphPersistenceCoordinator`: 持久化协调器。其生命周期绑定到 GraphInstance。

`graph_instance_id` / `status` / `spec_id` / `parent_instance_id` / `parent_node` 通过 property 委托到 `metadata`,调用方无需感知内部结构。

方法:

- `get_state()`: 委托 `coordinator.get_graph_state`。
- `load_for_recovery()`: 委托 `coordinator.load_for_recovery`。
- `update_status(status)`: 委托 coordinator 的 metadata store 做持久化,同时用 `model_copy` 更新本地 `metadata`(GraphMetadata 是 frozen,替换是唯一更新方式)。

注册表管理由 `GraphOrchestrator` 的 `_active_instances: dict[int, GraphInstance]` 负责。`unregister_instance(graph_instance_id)` 调 `coordinator.close()` 关闭资源并从注册表移除。触发条件是终态 status 加显式应用调用(如关闭、清理 hook),不依赖 natural GC(dict 强引用阻止 GC)。

## 3. Node 生命周期

### 3.1 Node.run() 统一流程

`Node.run(ctx, *, graph)` 是 node 生命周期的唯一入口。它统一处理状态转移、持久化、版本链、deliver 收集与 submit、undelivered retry、异常分类。node 子类只实现 `execute(ctx, integrated_input) -> NodeResult`。

完整流程:

1. **resume 检查**(begin 之前,只读查询): `coordinator.load_latest_invocation(self.name)`。如果最新 invocation 是 suspended 且有非空 `state_json`,这次是 resume from suspend,用 state snapshot 作为 integrated input,跳过 re-consume。
2. **begin_invocation**: `coordinator.begin_invocation(self.name)` 创建新 invocation。设置 `ctx.current_invocation`(供 dispatch handler 读取 `source_node` 加 `source_invocation_id`)。
3. **try 块**: integrate 加 execute 加 submit 加 complete。
   - **integrate**: resume 时用 `prev.state_json` 作为 integrated input。正常时调 `coordinator.collect_consumable_delivers`,若有 delivers 则 `mark_delivers_consumed`,再用 `input_integrator.integrate` 整合为 `IntegratedInput`。无 delivers 时 integrate 空列表。
   - **execute 加 undelivered retry**: 重置 `_pending_delivers`,调 `self.execute(ctx, integrated)`。如果返回 awaitable 则 await。收集 delivers。有 delivers 则 break。无 delivers 且未超 `max_retry` 则构造错误反馈 IntegratedInput 重新 execute。超过 `max_retry` 抛 `RoutingError`。
   - **submit**: `self.submit(ctx)` 调 `_submit`,按 `next_node` 分组,每组调 `ctx.dispatch(target, state_update={...})`。`next_node=None` 通过 `_resolve_default_target` 用 graph topology 解析(默认边 / 下游 / END)。
   - **complete**: `coordinator.complete_invocation(invocation, result.state_update or {})`。保存 COMPLETED,内部调 `promote_delivers`。
4. **except GraphInterrupt**: `snapshot = ctx.state.checkpoint()`,调 `coordinator.suspend_invocation(invocation, snapshot)`,re-raise。注意必须直接调 `ctx.state.checkpoint()`,不能用 `state_schema().fields` 迭代构建 snapshot,后者会跳过继承字段(如 `resume_target`)导致 resume 后路由错误。
5. **except GraphBubbleUp**: `coordinator.cancel_invocation(invocation)`,re-raise。GraphDrained / ParentCommand / InvalidUpdateError 等协作控制异常走此路径。
6. **except Exception**: `coordinator.crash_invocation(invocation)`,re-raise。
7. **finally**: `coordinator.finalize_invocation(invocation)`。安全网确保持久化状态一致。

### 3.2 Invocation 状态机

`InvocationStatus` 是持久化到 `node_states` 表的状态 enum:

```
PENDING → RUNNING → COMPLETED (终态,不可变)
                 ↘ CANCELED (GraphBubbleUp 取消,终态)
                 ↘ CRASHED (异常,可恢复,终态)
RUNNING → SUPERSEDED (suspended 后被新 invocation 取代,终态)
RUNNING (GraphInterrupt suspend,保持 RUNNING,待恢复)
```

- **PENDING**: invocation 已创建,未开始执行。begin 时先存 PENDING 再转 RUNNING,crash 在两次 save 之间留下可恢复的 PENDING 记录。
- **RUNNING**: execute 正在执行,或 GraphInterrupt suspend 后待恢复。
- **COMPLETED**: execute 正常完成,deliver 已 submit,消费已 promote,不可变。
- **CANCELED**: 被 GraphBubbleUp 取消,终态。恢复时跳过,不自动 re-dispatch,需显式 resume。
- **CRASHED**: execute 抛异常,终态。恢复时 re-dispatch。
- **SUPERSEDED**: suspended 的 RUNNING 被新 invocation 取代,终态。state snapshot 仍 apply 到 main_state,但不算活跃行。语义是"被延续取代,非中止"。

`suspended: bool` 字段显式区分 suspended RUNNING(GraphInterrupt,待恢复)与 orphan RUNNING(crash,需 re-dispatch)。`begin_invocation` 检查 `suspended` 决定标记 SUPERSEDED 还是 CRASHED。`finalize` 跳过 `suspended=True` 的 RUNNING。

与 `InvocationStatus` 分离的还有 `SchedulerInstanceStatus`(DORMANT / READY / RUNNING / COMPLETED),那是 scheduler 调度层追踪实例是否 ready 待执行的状态,不持久化到版本链。两个 enum 关注不同维度。

### 3.3 版本链

每次 `begin_invocation` 创建新版本:

- `version = max(所有已有版本号) + 1`。不是 `load_latest_completed + 1`,避免 CRASHED 或 RUNNING 版本号相同导致 UNIQUE 冲突。
- `parent_version` 指向上一个 COMPLETED 版本(从 `load_latest_completed` 取),无则 None。
- COMPLETED 记录不可变。
- 每次 node 最多一个非 COMPLETED 活跃行。新 invocation 开始前,`begin_invocation` 内部清理: suspended 的 RUNNING 标记 SUPERSEDED,orphan PENDING 或非 suspended RUNNING 标记 CRASHED。CANCELED / CRASHED / SUPERSEDED 都是终态,不算活跃行。

### 3.4 两种执行路径

两种执行路径都 always pass coordinator(Null 或 Memory / SQLite),都用同一 `Node.run()` 代码路径。差异在 coordinator 策略与状态持有机制,这是两个正交关注点:

| 层 | 关注 | 机制 | 路径 |
|----|------|------|------|
| Node invocation 持久化(coordinator) | per-node 版本链, state_json, deliver 消费 | coordinator 加 NodeState 加 DeliverStore | GraphOrchestrator(长生命周期图,跨 `_execute`) |
| Agent turn 状态持久化(AgentContext) | ReActTurnState, resume_target, tool 批次 | AgentContext 加 AgentPool | ReActAgent(per-turn,跨 turn) |

**GraphOrchestrator 路径**(Memory / SQLite coordinator): 长生命周期 GraphInstance,多个 `_execute` 调用。coordinator 持有状态,跨 `_execute` 存活。GraphInterrupt suspend 时 `coordinator.suspend_invocation` 持久化 state snapshot。Resume 时同一 GraphInstance 加同一 coordinator,`load_for_recovery` 取回 snapshot。

**ReActAgent 路径**(Null coordinator): per-turn GraphEngine 构造,无 GraphInstance。AgentContext(含 ReActTurnState)由 AgentPool 持有,跨 turn 存活。GraphInterrupt suspend 时 `coordinator.suspend_invocation` 是 no-op,AgentContext 是状态载体。Resume 时新 GraphEngine,但 AgentContext 复用,StartNode 读 `state.resume_target` 路由到 TOOL 节点恢复。Null coordinator 是 structural pass-through,AgentContext 是 active 状态机制。

## 4. Deliver 路由与消费

### 4.1 投递(生产侧)

node A 在 execute 中调 `self.deliver(content, next_node, ctx)` 累积到 in-memory `_pending_delivers`。execute 返回后,`_submit` 按 `next_node` 分组,每组调 `ctx.dispatch(target, state_update={"delivered": payload, "_source_node": ..., "_source_inv_id": ...})`。

scheduler 的 dispatch handler 读 `ctx.current_invocation` 获取 `source_node` 与 `source_invocation_id`,调 `coordinator.route_deliver(target_node, content, source_node, source_invocation_id)`。coordinator 找到 target node 的 deliver_store,调 `store.accumulate(...)` 生产到下游 deliver_store。

target 为 `GraphNode.END` 时 `route_deliver` 跳过(END 无 deliver_store),返回 None。

### 4.2 消费(消费侧)

node B 的 `run()` 在 integrate 阶段从自己的 deliver_store 消费:

1. `coordinator.collect_consumable_delivers(self.name, invocation.invocation_id)` 返回可消费的 delivers。
2. 若有 delivers,`coordinator.mark_delivers_consumed(self.name, [deliver_ids], invocation.invocation_id)` 标记为已消费。
3. 用 `input_integrator.integrate` 把 delivers 整合为 `IntegratedInput`。
4. 传给 `execute(ctx, integrated)`。

resume from suspend 时跳过此流程,直接用前一 invocation 的 state snapshot 作为 integrated input,避免 double-effect(deliver 已在 snapshot 中消费过)。

### 4.3 消费状态机

不同策略用 `DeliverConsumptionStatus` enum 的不同子集:

**Null 与 Memory(二态)**:

```
PENDING → CONSUMED
```

- `accumulate` 创建 PENDING 记录。
- `mark_consumed` 标记为 CONSUMED(Null 是直接移除记录,Memory 是 `model_copy` 替换为 CONSUMED)。
- `promote_consumed` 在 invocation COMPLETED 时调用: Memory 删除该 invocation 消费的记录(已完成,不需要了),Null 是 no-op。

**SQLite(三态)**:

```
PENDING → CONSUMED_PENDING → CONSUMED_COMPLETED
```

| 状态 | 含义 | 恢复时行为 |
|------|------|-----------|
| PENDING | 已投递,未被任何 invocation 消费 | 纳入消费 |
| CONSUMED_PENDING | 被某 invocation 消费,但该 invocation 未 COMPLETED | 重新纳入消费(上次没完成) |
| CONSUMED_COMPLETED | 被某 invocation 消费,且该 invocation 已 COMPLETED | 跳过(已处理完成) |

- `mark_consumed` 转换 PENDING 到 CONSUMED_PENDING,记录 `consumed_by_invocation_id`。
- `promote_consumed` 在 invocation COMPLETED 时转换 CONSUMED_PENDING 到 CONSUMED_COMPLETED。
- `query_consumable` 返回 PENDING 加 CONSUMED_PENDING,排除 CONSUMED_COMPLETED。

node 重新进入时(图有环),新 invocation 查 deliver_store: 上次 invocation CONSUMED_COMPLETED 的 delivers 跳过,新投递的 PENDING delivers 纳入消费。crash 恢复时,crash 的 invocation 消费的 delivers 是 CONSUMED_PENDING,重新纳入消费。

## 5. 恢复流程

### 5.1 load_for_recovery

`coordinator.load_for_recovery()` 返回 `RecoveryContext`,包含:

- `metadata`: GraphMetadata(从 `graph_metadata_store.load` 取,无记录时返回默认 metadata)。
- `node_states`: `dict[str, NodeInvocationRecord | None]`,各 node 的最新 invocation。
- `rebuilt_main_state`: 重建后的 main_state。

scheduler 在 `run_async` 顶部调此方法,直接使用 `rebuilt_main_state`,无需额外调 rebuild。

### 5.2 main_state 重建

`rebuild_main_state` 分两步:

1. **COMPLETED 记录**: 收集所有 node 的 COMPLETED invocation,按 `invocation_id`(全局 Snowflake 时间序)排序,逐个 apply `state_json`(即 `NodeResult.state_update`)到 fresh state。并行分支的 state_updates 独立,无因果依赖,顺序不重要。
2. **SUPERSEDED 记录**: 收集所有 node 的 SUPERSEDED invocation,按 `invocation_id` 排序,最后 apply 其 `state_json`(suspend 时的 state snapshot)。这确保 imperative mutations(如 `resume_target`)对 resumed node 可见。

用 `invocation_id` 全局排序而非 per-node version,因为 Snowflake 时间序无碰撞且保持因果序。

### 5.3 重新调度决策

scheduler 根据 metadata 的 `pending_dispatches` 与 `activated_sources` 加各 node 最新 invocation 状态重建调度状态:

- **COMPLETED**: 跳过,不重新 dispatch。
- **CRASHED 加 orphan PENDING / 非 suspended RUNNING**: 重新 dispatch。新建 invocation,`parent_version` 指向最后 COMPLETED。
- **CANCELED**: 跳过。deliberate cancel,不自动 re-dispatch,需显式 resume。
- **SUPERSEDED**: 检查是否有后继 invocation。有后继则跳过(已被新 invocation 取代)。无后继(crash 在标记 SUPERSEDED 与创建新 invocation 之间)则重新 dispatch,同 CRASHED 处理。
- **suspended 的 RUNNING**: resume。新建 invocation,旧版本标记 SUPERSEDED,新版本用 state snapshot 跳过 re-consume。

两种恢复类型共享同一流程,入口过滤不同:

- **故障恢复**(`recover_crashed`): 只捡 `CRASHED` 的 graph instance,启动时自动触发。
- **手动恢复**(`resume`): 捡 `PAUSED` / `STOPPED` 的 graph instance,外部 `resume()` 触发。`CRASHED` 不被手动 resume,`COMPLETED` / `FAILED` 是终态。

### 5.4 自动修复

`load_for_recovery` 内部做两项自动修复:

1. **auto-promote CONSUMED_PENDING**: 扫描所有 node 的 deliver_store,找出 CONSUMED_PENDING 记录,如果其 `consumed_by_invocation_id` 对应的 invocation 已 COMPLETED,则 `promote_consumed`。修复 crash 在 save COMPLETED 与 promote_delivers 之间的状态不一致。
2. **recheck pending delivers**: 沿用 scheduler 动态机制(`pending_dispatches` 加 `_ready` set)。查询 deliver_store 的 PENDING delivers 给 COMPLETED nodes,如有 pending deliver 则 re-dispatch。修复 crash 在 route_deliver(dispatch 时)与 `pending_dispatches` 更新(node-end)之间的不同步。

## 6. SQLite Schema

三个表,各自的 SQLite 策略 store 在构造时通过 `CREATE TABLE IF NOT EXISTS` 创建。对旧表用 `PRAGMA table_info` 检查列是否存在,缺则 `ALTER TABLE ADD COLUMN` 迁移,幂等。

### 6.1 node_states 表

```sql
CREATE TABLE IF NOT EXISTS node_states (
    node_state_id     BIGINT PRIMARY KEY,        -- Snowflake ID
    graph_instance_id BIGINT NOT NULL,           -- FK -> graph 实例
    node_name         TEXT NOT NULL,             -- 拥有此状态的 node
    version           INTEGER NOT NULL,          -- MVCC 版本号(每版本一行)
    parent_version    INTEGER,                   -- 上一个 COMPLETED 版本(NULL = 首次)
    status            TEXT NOT NULL DEFAULT 'pending'
                      CHECK (status IN ('pending','running','completed',
                                        'canceled','crashed','superseded')),
    invocation_id     BIGINT NOT NULL DEFAULT 0, -- 产生此版本的 invocation
    state_json        TEXT NOT NULL,             -- JSON 序列化的状态 dict
    suspended         INTEGER NOT NULL DEFAULT 0,-- 0/1 bool
    created_at        INTEGER NOT NULL,          -- epoch ms
    updated_at        INTEGER NOT NULL,          -- epoch ms
    UNIQUE (graph_instance_id, node_name, version)
);
```

索引:

- `idx_node_states_latest` (graph_instance_id, node_name, version DESC) 加载最新版本。
- `idx_node_states_status` (graph_instance_id, node_name, status) 按状态过滤。
- `idx_node_states_cross` (graph_instance_id, node_name, invocation_id) 跨版本查 invocation。
- `idx_node_states_global` (graph_instance_id, invocation_id DESC) 全局时间序排序。

`save_invocation` 用 `INSERT ... ON CONFLICT(graph_instance_id, node_name, version) DO UPDATE SET ...`(upsert-per-version)。`created_at` 在 upsert 时保留原值,`updated_at` 更新。旧表迁移加 `invocation_id` / `parent_version` / `status` / `suspended` / `updated_at` 五列。

### 6.2 deliver_states 表

```sql
CREATE TABLE IF NOT EXISTS deliver_states (
    deliver_id                INTEGER PRIMARY KEY,        -- Snowflake ID
    graph_instance_id         INTEGER NOT NULL,
    node_name                 TEXT NOT NULL,              -- 接收方(此 store 的 owner)
    next_node                 TEXT NOT NULL,              -- 旧字段,保留兼容
    source_node               TEXT NOT NULL DEFAULT '',   -- 投递方
    source_invocation_id      INTEGER NOT NULL DEFAULT 0, -- 投递方 invocation_id
    consumed_by_invocation_id INTEGER,                    -- 消费方 invocation_id(NULL = 未消费)
    content_json              TEXT NOT NULL,              -- JSON 序列化的 content
    status                    TEXT NOT NULL DEFAULT 'pending'
                              CHECK (status IN ('pending','consumed',
                                                'consumed_pending','consumed_completed',
                                                'accumulated','submitted')),
    created_at                INTEGER NOT NULL,
    updated_at                INTEGER NOT NULL
);
```

索引:

- `idx_deliver_states_node` (graph_instance_id, node_name, status) 按接收方与状态查询。
- `idx_deliver_states_target` (graph_instance_id, next_node, status) 旧 API 按目标查询。

CHECK 约束允许全部四个 `DeliverConsumptionStatus` 值加两个旧 `DeliverStatus` 值(`accumulated` / `submitted`)做向后兼容。旧表迁移加 `source_node` / `source_invocation_id` / `consumed_by_invocation_id` 三列。`content` 字段写入时 `json.dumps`,读出时 `json.loads`。`deliver_id` 用 Snowflake ID(跨进程单调),不用 SQLite AUTOINCREMENT。

### 6.3 graph_metadata 表

```sql
CREATE TABLE IF NOT EXISTS graph_metadata (
    graph_instance_id BIGINT PRIMARY KEY,
    metadata_json     TEXT NOT NULL,    -- GraphMetadata.model_dump_json()
    created_at        INTEGER NOT NULL,
    updated_at        INTEGER NOT NULL
);
```

`save` 用 `INSERT OR REPLACE`,metadata 序列化为 `model_dump_json()`。`load` 用 `GraphMetadata.model_validate_json` 反序列化。`update_status` 加载现有 row,用 `model_copy` 更新 status,再 save。无单独索引,主键即查询键。

## 7. 接口定义

以下签名取自当前实现源码。

### 7.1 NodeState ABC

`NodeState` 管理一个 node 的私有内部状态。它是 runtime object with mutable state,不是 frozen Pydantic。ABC 含两组方法: 旧 in-memory dict API(read / write / snapshot / restore / has)与 invocation 版本链 API。

```python
class NodeState(ABC):
    # 旧 in-memory API
    @abstractmethod
    def read(self, field: str) -> Any: ...
    @abstractmethod
    def write(self, field: str, value: Any) -> None: ...
    @abstractmethod
    def snapshot(self) -> dict[str, Any]: ...
    @abstractmethod
    def restore(self, data: dict[str, Any]) -> None: ...
    @abstractmethod
    def has(self, field: str) -> bool: ...

    # invocation 版本链 API
    @abstractmethod
    def save_invocation(
        self,
        graph_instance_id: int,
        node_name: str,
        invocation_id: int,
        version: int,
        parent_version: int | None,
        status: InvocationStatus,
        state: dict[str, Any],
        suspended: bool = False,
    ) -> None: ...
    @abstractmethod
    def load_invocation(
        self, graph_instance_id: int, node_name: str, invocation_id: int
    ) -> NodeInvocationRecord | None: ...
    @abstractmethod
    def load_latest(
        self, graph_instance_id: int, node_name: str
    ) -> NodeInvocationRecord | None: ...
    @abstractmethod
    def query_versions(
        self,
        graph_instance_id: int,
        node_name: str,
        status_filter: set[InvocationStatus] | None = None,
    ) -> list[NodeInvocationRecord]: ...
    @abstractmethod
    def load_latest_completed(
        self, graph_instance_id: int, node_name: str
    ) -> NodeInvocationRecord | None: ...
```

三种实现: `NullNodeState`(全 no-op,read 返回 None)、`SimpleNodeState`(内存 dict 加 `list[NodeInvocationRecord]`,upsert-per-version)、`SqliteNodeState`(SQLite 表,旧 in-memory API 是 no-op shim,版本链 API 是真实存储)。

`NodeStateFactory` ABC:

```python
class NodeStateFactory(ABC):
    @abstractmethod
    def create(self) -> NodeState: ...
```

三种实现: `NullNodeStateFactory` / `SimpleNodeStateFactory` / `SqliteNodeStateFactory`(后者接受共享 `sqlite3.Connection`)。

### 7.2 GraphMetadataStore ABC

```python
class GraphMetadataStore(ABC):
    @abstractmethod
    def save(self, graph_instance_id: int, metadata: GraphMetadata) -> None: ...
    @abstractmethod
    def load(self, graph_instance_id: int) -> GraphMetadata | None: ...
    @abstractmethod
    def update_status(self, graph_instance_id: int, status: GraphInstanceStatus) -> None: ...
```

三种实现: `NullGraphMetadataStore`(no-op,load 返回 None)、`MemoryGraphMetadataStore`(dict,`update_status` 用 `model_copy`)、`SqliteGraphMetadataStore`(`INSERT OR REPLACE`,共享 connection)。

### 7.3 DeliverStore ABC

```python
class DeliverStore(ABC):
    # 新 API(per-node 消费状态机)
    @abstractmethod
    def accumulate(
        self,
        *,
        graph_instance_id: int,
        target_node: str,
        source_node: str,
        source_invocation_id: int,
        content: Any,
    ) -> int: ...
    @abstractmethod
    def query_consumable(self, graph_instance_id: int, target_node: str) -> list[DeliverRecord]: ...
    @abstractmethod
    def mark_consumed(self, deliver_ids: list[int], consumed_by_invocation_id: int) -> None: ...
    @abstractmethod
    def promote_consumed(self, consumed_by_invocation_id: int) -> None: ...

    # 旧 API(保留兼容,后续移除)
    @abstractmethod
    def query_pending(self, graph_instance_id: int, node_name: str) -> list[DeliverRecord]: ...
    @abstractmethod
    def query_by_target(self, graph_instance_id: int, next_node: str) -> list[DeliverRecord]: ...
    @abstractmethod
    def mark_submitted(self, deliver_ids: list[int]) -> None: ...
    @abstractmethod
    def clear(self, graph_instance_id: int) -> None: ...
```

三种实现: `NullDeliverStore`(in-memory queue,`mark_consumed` 移除记录,无状态机)、`InMemoryDeliverStore`(二态,`promote_consumed` 删除记录)、`SqliteDeliverStore`(三态,共享 connection,构造接受 path 或 `sqlite3.Connection`)。

`DeliverStoreFactory` ABC:

```python
class DeliverStoreFactory(ABC):
    @abstractmethod
    def create(self) -> DeliverStore: ...
```

三种实现: `NullDeliverStoreFactory` / `InMemoryDeliverStoreFactory` / `SqliteDeliverStoreFactory`(接受共享 connection)。

### 7.4 GraphPersistenceCoordinator

```python
class GraphPersistenceCoordinator:
    def __init__(
        self,
        graph_instance_id: int,
        graph_metadata_store: GraphMetadataStore,
        default_node_state_factory: NodeStateFactory,
        default_deliver_store_factory: DeliverStoreFactory,
    ) -> None: ...

    # 注册与路由
    def register_node(
        self,
        node_name: str,
        node_state: NodeState | None = None,
        deliver_store: DeliverStore | None = None,
    ) -> None: ...
    def get_deliver_store(self, node_name: str) -> DeliverStore | None: ...
    def route_deliver(
        self,
        target_node: str,
        content: Any,
        source_node: str,
        source_invocation_id: int,
    ) -> int | None: ...

    # 消费
    def collect_consumable_delivers(
        self, node_name: str, invocation_id: int
    ) -> list[DeliverRecord]: ...
    def mark_delivers_consumed(
        self, node_name: str, deliver_ids: list[int], invocation_id: int
    ) -> None: ...
    def promote_delivers(self, node_name: str, invocation_id: int) -> None: ...

    # 生命周期
    def begin_invocation(self, node_name: str) -> InvocationContext: ...
    def complete_invocation(self, invocation: InvocationContext, state: dict[str, Any]) -> None: ...
    def cancel_invocation(self, invocation: InvocationContext) -> None: ...
    def suspend_invocation(self, invocation: InvocationContext, state_snapshot: dict[str, Any]) -> None: ...
    def crash_invocation(self, invocation: InvocationContext) -> None: ...
    def finalize_invocation(self, invocation: InvocationContext) -> None: ...

    # 恢复与查询
    def load_latest_invocation(self, node_name: str) -> NodeInvocationRecord | None: ...
    def rebuild_main_state(self) -> dict[str, Any]: ...
    def load_for_recovery(self) -> RecoveryContext: ...
    def get_graph_state(
        self, node_status_filter: set[InvocationStatus] | None = None
    ) -> GraphStateSnapshot: ...

    # 资源清理
    def close(self) -> None: ...
```

工厂函数:

```python
def create_null_coordinator(graph_instance_id: int = 0) -> GraphPersistenceCoordinator: ...
```

用 `NullGraphMetadataStore` 加 `NullNodeStateFactory` 加 `NullDeliverStoreFactory` 装配。用于 ReActAgent per-turn、LLMNode 模块级 governance helper、GraphOrchestrator 当前默认。

### 7.5 值对象

所有值对象都是 frozen Pydantic `BaseModel`,`extra="forbid"`。

**NodeInvocationRecord**(`node_state.py`): 一次 node 调用的持久化记录。

```python
class NodeInvocationRecord(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    invocation_id: int
    graph_instance_id: int
    node_name: str
    version: int
    parent_version: int | None
    status: InvocationStatus
    state_json: dict[str, Any]
    suspended: bool = False
    created_at: int
    updated_at: int
```

**GraphMetadata**(`graph_metadata.py`): graph 实例级元数据。

```python
class GraphMetadata(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    graph_instance_id: int
    spec_id: int
    parent_instance_id: int | None
    parent_node: str | None
    status: GraphInstanceStatus
    instance_seq: int
    iteration_count: int
    activated_sources: dict[str, list[str]]
    pending_dispatches: dict[str, dict[str, list[dict[str, Any] | None]]]
```

**InvocationContext**(`graph_metadata.py`): `begin_invocation` 的返回值。

```python
class InvocationContext(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    invocation_id: int
    node_name: str
    version: int
    parent_version: int | None
```

**RecoveryContext**(`graph_metadata.py`): `load_for_recovery` 的返回值。

```python
class RecoveryContext(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    metadata: GraphMetadata
    node_states: dict[str, NodeInvocationRecord | None]
    rebuilt_main_state: dict[str, Any]
```

**GraphStateSnapshot**(`graph_metadata.py`): `get_graph_state` 的返回值。

```python
class GraphStateSnapshot(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    metadata: GraphMetadata
    nodes: dict[str, list[NodeInvocationRecord]]
```

**DeliverRecord**(`deliver_store.py`): 一条 accumulated deliver。

```python
class DeliverRecord(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    deliver_id: int
    graph_instance_id: int
    node_name: str               # 接收方(旧字段,保留)
    next_node: str               # 目标下游(旧字段,保留)
    source_node: str             # 投递方
    source_invocation_id: int    # 投递方 invocation_id
    consumed_by_invocation_id: int | None  # 消费方 invocation_id
    content: Any                 # 投递内容
    status: DeliverConsumptionStatus       # 消费状态
    created_at: int
    updated_at: int
```

**枚举**(`constants.py`):

- `InvocationStatus`: PENDING / RUNNING / COMPLETED / CANCELED / CRASHED / SUPERSEDED
- `DeliverConsumptionStatus`: PENDING / CONSUMED / CONSUMED_PENDING / CONSUMED_COMPLETED
- `GraphInstanceStatus`: RUNNING / PAUSED / STOPPED / CRASHED / COMPLETED / FAILED
- `SchedulerInstanceStatus`: DORMANT / READY / RUNNING / COMPLETED
- `GraphNode`: START / END(sentinel)

## 8. GraphOrchestrator 与 GraphControlService

### 8.1 GraphOrchestrator 注册表

`GraphOrchestrator` 是框架级图编排服务。它装配 `GraphSpecCompiler`、`GraphRecoveryService`、`GraphControlService`,并用 `_active_instances: dict[int, GraphInstance]` 注册表管理 GraphInstance 生命周期。

`create_and_run(spec_id, *, initial_state=None, parent_instance_id=None) -> int` 流程:

1. 从 `spec_store` 加载 `GraphSpec`,`GraphSpecCompiler` 编译为 `CompiledGraph`。
2. 用 Snowflake 生成 `graph_instance_id`,构造 `GraphMetadata`(status = RUNNING),存入 `instance_store`。
3. `create_null_coordinator(graph_instance_id)` 创建 coordinator。
4. 遍历 `compiled.nodes` 调 `coordinator.register_node(node_name)`(注册时机: GraphInstance 构造时,编译后,`_execute` 前)。
5. 构造 `GraphInstance(metadata, coordinator)`,存入 `_active_instances`。
6. 创建 state,调 `_execute(instance, compiled, state)`。
7. 返回 `graph_instance_id`。

`_execute(instance, compiled, state)`: 用 `instance.coordinator` 构造 `GraphContext(coordinator=instance.coordinator, graph_instance_id=gid)`,创建 `GraphEngine`,`run_async(ctx)`。生命周期: 正常完成设 COMPLETED,GraphInterrupt 设 PAUSED 并 re-raise(instance 留在注册表,coordinator 存活供 resume),其他异常设 CRASHED 并 re-raise。`finally` 注销 engine controller。

`unregister_instance(graph_instance_id)`: 调 `instance.coordinator.close()`(关闭 SQLite connection 等),从 `_active_instances` 移除。crash recovery 时旧 instance 先被驱逐再注册新的。

当前实现: `create_and_run` 与 recovery 路径都用 `create_null_coordinator`。Memory / SQLite 策略的选择与注入机制是后续待办。

### 8.2 GraphControlService deliver 收敛

`GraphControlService` 路由 `ControlCommand` 到 graph 实例动作。pause / stop / resume / deliver 四种命令走同一 `handle` 路径。

deliver 收敛是关键设计点。`GraphControlService.__init__` 接受 `coordinator_lookup: Callable[[int], GraphPersistenceCoordinator | None]` 回调,由 `GraphOrchestrator._lookup_coordinator` 提供。`_deliver` 命令处理:

1. 从 payload 取 `node_name` 与 `content`。
2. `coordinator = self._coordinator_lookup(gid)` 从注册表取 coordinator。
3. `coordinator.route_deliver(target_node=node_name, content=content, source_node="__external__", source_invocation_id=0)`。外部 deliver 走统一 coordinator 路径,`source_node="__external__"` 标记来源。
4. 通知 engine controller `deliver_to_node(node_name, content)`。

无共享 deliver_store。`GraphControlService` 不持有任何 `DeliverStore`,所有 deliver 通过 coordinator 路由到 per-node store。

`GraphEngineController` 是控制运行中 engine 的 ABC。当前实现是 `InMemoryGraphEngineController`(recording stub,设 bool flag 但不实际控制 scheduler loop)。能真正 pause / stop 运行中 engine 的 `LiveGraphEngineController` 是后续待办。

### 8.3 GraphRecoveryService

`GraphRecoveryService` 两种恢复类型共享同一流程:

1. 从 `instance_store` 加载 `GraphMetadata`。
2. `create_null_coordinator(graph_instance_id)` 重建 coordinator。
3. 构造 `GraphInstance(metadata, coordinator)`。
4. 设 status 为 RUNNING(在 `instance_store`)。
5. 调 `engine_factory.create_and_run(instance)`。

`engine_factory` 是 `GraphEngineFactory` ABC,由 `GraphOrchestrator._EngineFactoryAdapter` 实现,委托 `GraphOrchestrator._run_existing_instance`。该方法处理: 旧 instance 驱逐、node 注册、注册表插入、`_execute`。scheduler 的 `run_async` 在顶部调 `coordinator.load_for_recovery()` 恢复状态并 re-dispatch。

`recover_crashed()`: 从 `instance_store` 查 status 为 CRASHED 的实例,对每个构造 GraphInstance 并走共享流程。返回恢复的 `graph_instance_id` 列表。

`resume(graph_instance_id)`: 加载单个实例,校验 status 为 PAUSED 或 STOPPED(CRASHED 不被手动 resume,COMPLETED / FAILED 是终态),走共享流程。

## 9. 后续待办

以下事项在当前实现中未完成,列为未来工作:

- **Production coordinator 策略选择**: `GraphOrchestrator` 当前用 `create_null_coordinator`。Memory / SQLite 策略的选择与注入机制需要实现,形式是 coordinator factory injection。recovery 路径同样需要用真实策略重建 coordinator 才能从 DB 恢复状态。
- **Contract 阶段旧 API 移除**: `DeliverStatus` enum(`ACCUMULATED` / `SUBMITTED`)、`DeliverStore` 旧方法(`query_pending` / `query_by_target` / `mark_submitted`)、`deliver_states` 表 CHECK 约束中的旧 status 值,保留为 expand 阶段遗留。contract 阶段确认无调用方后移除。`DeliverRecord` 的 `node_name` / `next_node` 旧字段同期待移除。
- **LiveGraphEngineController**: `GraphEngineController` ABC 当前只有 `InMemoryGraphEngineController`(recording stub)。需要实现能真正 pause / stop / resume 运行中 scheduler loop 的 controller,在 coordinator 层集成,而非 scheduler 层。
- **前端查询接口**: `GraphStateSnapshot` 查询 API(REST / CLI),如 `GET /api/graph/{instance_id}/state` 默认返回 COMPLETED 历史,按 status 过滤,单 node 版本历史。设计已预留,实现待前端集成阶段。
- **自环节点(A→A)调度验证**: scheduler 的动态机制(`_ready` set 加 `_handle_dispatch` 加 `_recheck_pending` 加 reachability BFS for ParallelScheduler,顺序执行 for LinearScheduler)支持自环。需要端到端验证: A 完成,dispatch 到自己,deliver_store 投递,A 再次 ready,integrate 消费自己的 deliver。串行保证: 旧实例 COMPLETED 后才 recheck,新实例才 READY。
- **node 幂等设计扩展**: 框架提供 `invocation_id` 加 `version` 加 `parent_version` 原语,node 自行实现幂等逻辑(execute 被重新调用时如何处理)。框架不传递"调用原因"信号。这是设计语义: 幂等是 node 的业务责任,框架只提供识别原语。

### 9.1 待办决策事项(代码检视发现)

以下事项需要设计决策后才能实施,不应直接修改:

- **Metadata store 双权威问题**: `GraphOrchestrator` 用 `GraphInstanceStore` 持久化 metadata(scheduler bookkeeping 字段如 `instance_seq` / `iteration_count` / `activated_sources` / `pending_dispatches` 未写入),`coordinator.load_for_recovery()` 读 `GraphMetadataStore`。两个 store 互不通信,production 路径即使注入 SQLite coordinator 也无法恢复。决策: 收敛到单一 metadata 权威(合并 `GraphInstanceStore` 到 `GraphMetadataStore`,或在 coordinator 上暴露统一 metadata API)。
- **Node COMPLETED 在 scheduler commit 之前**: `Node.run()` 在 execute 返回后立即 submit delivers + save COMPLETED,但 `ParallelScheduler._execute_instance` 在 `node.run()` 返回后才做 conflict detection + merge state。如果 `commit()` raise `InvalidUpdateError`,持久化层已记 COMPLETED 但 scheduler 从未接受该状态。决策: 引入 shared post-commit finalizer(execute + integrate 先,scheduler validate/merge,然后原子 submit + complete)。
- **`rebuild_main_state()` 语义不完整**: recovery 用 `dict.update()` 重建 state,丢失 reducer-channel 语义(append/sum)、imperative mutations(无 state_update 的 node)、runtime commit order(begin 时生成 invocation_id,非 commit 时)。决策: persist commit-order 值 + replay typed deltas through `GraphState.apply_state_update()`,或 persist post-commit checkpoint。
- **同一 node 并发 invocation 安全性**: `ON_RECEIVE` 可并发启动同一 node 的多个 instance。coordinator 假设每 node 最多一个活跃 invocation(`begin_invocation` 标记前一个为 CRASHED)。SQLite 的 `CONSUMED_PENDING` delivers 被两个 invocation 共享,`mark_consumed` 覆盖 ownership。决策: 串行化 per-node invocations(限制 `ON_RECEIVE` 并发),或重新设计 consumption claims + version allocation 支持多活跃 invocation。
- **SUPERSEDED snapshot 在 `rebuild_main_state` 中的优先级**: 每个 SUPERSEDED full-state snapshot 在所有 COMPLETED delta 之后 apply。stale suspended branch 可覆盖后续 branch commit 的字段。决策: 定义 suspend snapshot 是 authoritative full checkpoint、per-node delta、还是 resume-only state。
- **状态机转换未强制**: 所有 lifecycle 方法盲目 upsert version,无 CAS(compare-and-set)。`COMPLETED` 可被覆盖为 `CRASHED`,`CANCELED` 可变 `COMPLETED`。决策: 实现 CAS transitions(expected prior status in WHERE clause,verify 1 row affected)。
- **SQLite connection 线程安全**: `check_same_thread=False` 不序列化访问。多个 store 共享一个 connection 需要锁或 event-loop confinement contract。决策: 加 shared lock,或限定 coordinator 在单一 event loop。
- **LinearScheduler 无 recovery 路径**: 只有 `ParallelScheduler.run_async` 调 `load_for_recovery()`。Linear 是默认 scheduler,但 recovery 不完整。决策: 加共享 recovery initialization seam,两个 scheduler 都调。
- **旧 SQLite DB 迁移**: `deliver_states` 表旧 CHECK 约束只允许 `accumulated` / `submitted`,新代码插入 `pending` 会失败。`_migrate_add_columns` 不重建 CHECK 约束,不翻译旧 status 值。决策: 写新编号 migration(rebuild table + copy rows + translate statuses + recreate indexes)。
- **Contract 阶段旧 API 与 rule 15 冲突**: `DeliverStatus` enum、4 个旧 DeliverStore 方法、`DeliverRecord` 旧字段(`node_name` / `next_node`)无内部 production caller 但保留。决策: 如有外部包兼容性需求,显式 deprecation window;否则在本 change 移除。
