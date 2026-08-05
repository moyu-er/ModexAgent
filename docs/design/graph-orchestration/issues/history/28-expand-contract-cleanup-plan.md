# expand-contract 收尾计划

Status: triage:ready-for-agent
Blocked by: [Fork-merge 依赖链移除](33-fork-merge-removal.md)

## Question

ticket 33 移除 fork-merge 依赖链。这扩展了 expand-contract 收尾的范围——channel / declarative delta / conflict detection / state_factory / DispatchStore 体系的移除，加上之前确认的旧 DeliverStore API 移除。

本 ticket 确认全部移除项的零 caller 状态和移除顺序。

## 待移除清单

### 已在 ticket 22 中解决的

- ~~`GraphMetadataStore` ABC + 3 实现 + `graph_metadata_store.py`~~
- ~~`coordinator.update_graph_status()`~~
- ~~`GraphInstance.update_status()`~~

### ticket 33 移除的（此 ticket 确认收尾）

#### Fork-merge 核心

- fork + deep copy state 隔离（`instance.forked_state` / `model_copy(deep=True)` / `need_fork` 逻辑）
- `NodeInstance.fork_version` 字段（ticket 33 新增 — conflict detector 的 generation key）
- merge segment（conflict detection + apply_state_update + advance + complete）
- `_execute_instance` 的 `fork` 参数
- `ParallelScheduler.__init__` 的 `conflict_detector` + `dispatch_store` 参数

#### Conflict detection

- `WriteConflictDetector` ABC + `GenerationWriteTracker` + `_Generation` + `conflict_detector.py`（整个文件）
- `InvalidUpdateError`（exceptions.py — 零 raiser 后成死代码，需确认无外部 catch）

#### Declarative delta + NodeResult

- `NodeResult` 类型 + `NodeResult.state_update` 字段（result.py — `node.execute()` 返回 `None`）
- `DispatchEvent` 类型（result.py — DispatchStore 的记录类型，无消费者）
- `apply_state_update` / `apply_concurrent_updates`（state/state.py）
- `linear.py:110-111` 的 `result.state_update` 读取 + `apply_state_update` 调用
- `parallel.py:492-511` 的 merge segment（`result.state_update` 读取 + conflict detection + apply）

#### Channel 系统

- `BaseChannel` / `LastValue` / `ReducerChannel` / `Codec` / `register_codec` / `encode_value` / `decode_value` + `state/channel.py`（整个文件）
- `_channels` PrivateAttr / `_setup_channels` / `_sync_fields_to_channels` / `_sync_channels_to_fields` / `_find_channel_marker`（state/state.py）

#### state_factory + state_schema

- `StateFactory` ABC / `SimpleStateFactory` / `DynamicStateFactory` / `StateRegistry` + `state_factory.py`（整个文件）
- `_resolve_channel` / `_channel_marker_to_string` / `_find_channel_marker`（state_factory.py 的 helper）
- `StateFieldSpec.channel` 字段（state_schema.py — channel kind 描述，如保留 declarative GraphSpec 路径则只删此字段）
- `ReactStateFactory`（react/state_factory.py — 直接用 `ReActTurnState`）

#### DispatchStore

- `DispatchStore` ABC + `InMemoryDispatchStore` + `SqliteDispatchStore` + `dispatch_store.py`（整个文件）
- `_dispatch_log` property（parallel.py:134-139）
- `query_dispatches_by_target`（parallel.py:141-150）
- `_handle_dispatch` 中的 DispatchEvent 创建 + record（parallel.py:572-578）
- **`now_ms()` 提取**：dispatch_store.py:47 的 `now_ms()` 被 6 个持久化模块导入（`spec_store` / `instance_store` / `node_state` / `deliver_store` / `node_state_store` / `graph_metadata_store`）。**移除 dispatch_store.py 前必须提取 `now_ms` 到共享位置**（如 `persistence/_time.py`）

#### SUPERSEDED 状态

- `InvocationStatus.SUPERSEDED` enum 值（constants.py）
- `begin_invocation` 中的 SUPERSEDED 标记逻辑（persistence_coordinator.py:326-341）
- `rebuild_main_state` 两阶段 apply（persistence_coordinator.py:596-629）— 改为单次查询，详见 [ticket 26](26-rebuild-main-state-semantics.md)
- `finalize_invocation` 中的 SUPERSEDED 跳过逻辑（persistence_coordinator.py:561）

#### ReActTurnState 迁移

- `from modex_graph.state import LastValue` 导入（react/state.py:58）
- 15 个 `Annotated[T, LastValue]` 注解（react/state.py:93-118）— 改为普通 `T`

#### `__init__.py` 导出清理

- `modex_graph/__init__.py`：移除 `BaseChannel` / `LastValue` / `ReducerChannel` / `Codec` / `register_codec` / `JsonValue` / `DispatchStore` / `InMemoryDispatchStore` / `SqliteDispatchStore` / `WriteConflictDetector` / `GenerationWriteTracker` / `NodeResult` / `DispatchEvent` / `InvalidUpdateError` 导出
- `state/__init__.py`：移除 `BaseChannel` / `Codec` / `JsonValue` / `LastValue` / `ReducerChannel` / `register_codec` 导出
- `persistence/__init__.py`：移除 `DispatchStore` / `InMemoryDispatchStore` / `SqliteDispatchStore` 导出

### 旧 DeliverStore API 移除

- `query_pending` / `query_by_target` / `mark_submitted` / `clear`（ABC + 三实现）
- `DeliverStatus` enum（`ACCUMULATED` / `SUBMITTED`）
- `DeliverRecord` 旧字段（`node_name` / `next_node`）
- `deliver_states` 表 CHECK 约束中的旧 status 值

### `NodeStateStore` 收敛（ticket 23 已决议）

ticket 23 已关闭：收敛到 `NodeStateStore`（正确设计），移除 `NodeState`（混乱叠加）。

- `NodeStateStore` ABC：保留并吸收 `NodeState` 的 invocation 版本链 + lifecycle 方法（begin/complete/suspend/crash/cancel/finalize）。原来的 append-only 写模式改为 UPSERT。原来 6 列 schema 升级为 11 列（加 status/invocation_id/parent_version/suspended/updated_at）。
- `NodeState` ABC：删除旧 API（read/write/snapshot/restore/has）。`NodeState` 名字本身被 `NodeStateStore` 取代。
- `NullNodeState` / `SimpleNodeState` / `SqliteNodeState`：重构为 `NullNodeStateStore` / `InMemoryNodeStateStore` / `SqliteNodeStateStore`（收敛后的 ABC 实现）。
- 原 `node_state_store.py`（append-only 6 列）：ABC 接口被取代，append-only 写模式被 UPSERT 取代。文件合并到收敛后的 `node_state_store.py`。
- coordinator 的 6 个 lifecycle 方法（begin/complete/suspend/crash/cancel/finalize）：移入 store。coordinator 退出 lifecycle 路径。
- coordinator 的 `_node_states: dict[str, NodeState]`：改为一个 `_node_state_store: NodeStateStore`。
- coordinator 的 `load_latest_invocation`：移除。node 直接调 `ctx.node_state_store.load_latest(self.name)`。
- `GraphContext`：新增 `ctx.node_state_store: NodeStateStore`。
- `PENDING` status：移除。记录直接创建为 RUNNING。
- `NodeStateFactory`：替换为 `NodeStateStoreFactory`（创建一个绑定 graph_instance_id 的 store）。
- 表名冲突消失——一个 ABC，一张表，一套 schema。

### 旧 SQLite DB 迁移

当前无真实数据。直接改表 schema。

### ticket 22/32 追加的移除（2026-08-04 闭环后）

- `GraphMetadataStore` ABC + `NullGraphMetadataStore` + `MemoryGraphMetadataStore` + `SqliteGraphMetadataStore` + `graph_metadata_store.py`（整个文件）+ `graph_metadata` 表（ticket 22 决议，代码尚未实施）
- `coordinator.update_graph_status()` + `GraphInstance.update_status()`（status 写入收敛单路径，ticket 22）
- `GraphMetadata` 的 4 个 bookkeeping 字段（`instance_seq` / `iteration_count` / `activated_sources` / `pending_dispatches`）+ `graph_orchestrator.py` 构造点的对应初始化（ticket 32 裁决：运行时视图，不持久化）
- `bookkeeping_json` 列**不实施**（ticket 22 原混合存储决议被 ticket 32 作废，最终 schema = 纯列存储）

### ticket 24/29/31/32 追加的新增工作（2026-08-04 闭环后）

- `CoordinatorFactory` ABC + `NullCoordinatorFactory`（ticket 29）；`GraphOrchestrator.__init__` 加 `coordinator_factory`（默认 Null），`create_and_run` 与 `GraphRecoveryService` 共用单注入点；`instance_store` 由 orchestrator 传入 `create()`
- Store 构造契约统一（ticket 29）：全部 Sqlite store 接受 caller-owned `sqlite3.Connection`、store 永不关闭；`SqliteGraphInstanceStore` 的 path-only 异类收敛
- `LinearScheduler` recovery 4 行（ticket 24）：`load_for_recovery` + `from_checkpoint` 恢复 + 从 entry_node 起
- CAS（ticket 31b）：lifecycle 转换改 WHERE-clause 条件更新 + rowcount 检查；`complete`/`suspend`/`cancel` 严格抛 `InvocationStateError`（新增异常类型），`crash`/`finalize`/orphan 清理幂等容忍；线程契约写入 `NodeStateStore` ABC 文档（ticket 29/31c）
- ON_RECEIVE per-node 串行门（ticket 31a 修订）：`_handle_dispatch` 排队 + instance 完成事件排水；ON_RECEIVE 标记「谨慎使用」注释 + TODO
- Recovery 重建（ticket 32）：`_restore_from_recovery` 加 pending 队列重建（scan PENDING delivers → 按 trigger mode 过滤分组）+ `iteration_count` 从 COMPLETED 计数派生；`_redispatch_from_recovery` 删 COMPLETED+delivers 捷径（被重建 + recheck 覆盖）

## 移除顺序

依赖关系决定执行顺序（已整合 2026-08-04 全部闭环 ticket 的工作项）：

```
阶段 A：纯移除（fork-merge 依赖链，ticket 33）
1. 提取 now_ms() 到 persistence/_time.py（6 个模块依赖它）
2. 更新 GraphState.checkpoint/from_checkpoint 为 model_dump/model_validate
3. 更新 ReActTurnState 去掉 Annotated[T, LastValue] 注解
4. 更新 Node.run() complete_invocation 传参（result.state_update → ctx.state.model_dump）
5. 更新 linear.py 删除 state_update 读取 + apply_state_update
6. 更新 parallel.py 删除 fork 逻辑 + merge segment + _execute_instance fork 参数
   （另含：__init__ 的 conflict_detector/dispatch_store 参数、_dispatch_log property、
   query_dispatches_by_target、_handle_dispatch 的 DispatchEvent record、
   NodeInstance.forked_state/fork_version 字段、相关 imports）
7. 删除 channel.py + state_factory.py + conflict_detector.py + dispatch_store.py
   （另含：state.py 的 channel 机械移除——_channels PrivateAttr / _setup_channels /
   _sync_fields_to_channels / _sync_channels_to_fields / _find_channel_marker /
   apply_state_update / apply_concurrent_updates，~100 行）
8. 删除 NodeResult + DispatchEvent（result.py）
   （另含：node.execute()/Node.run() 返回类型改 None、移除 result 变量线程化；
   after_node 签名去 result 参数——4 站点：runtime.py ABC、react/runtime.py 覆写、
   parallel.py 调用点、linear.py 调用点）
9. 移除 coordinator 中的 SUPERSEDED 逻辑 + 更新 rebuild_main_state + begin_invocation
   （单次查询，ticket 26）。注意：SUPERSEDED enum 值本身留到 step 11 删——
   node_state.py 的 SQL CHECK 约束引用它，step 11 重写该文件时一并移除；
   本步另清理 _redispatch_from_recovery 的 SUPERSEDED 注释

阶段 B：store 层收敛（ticket 22 + 23 + 29 契约部分 + 31b）
10. GraphInstanceStore 收敛（ticket 22 修订版）：删 GraphMetadataStore 全家 + graph_metadata 表；
    最终 schema 纯列存储（无 bookkeeping_json）；status 写入单路径
    （删 coordinator.update_graph_status + GraphInstance.update_status）；
    更新 create_null_coordinator 函数体（NullGraphInstanceStore）。
    注意：GraphMetadata 类此步**不修剪**——4 个 bookkeeping 字段保留但 schema 不持久化它们
    （load 填默认值，无害中间态）；修剪移到 step 16 与 recovery 重写原子落地
    （否则 _restore_from_recovery 读 metadata 字段会在中间态炸掉——审计发现的顺序隐患）
11. NodeStateStore 收敛（ticket 23）：
    a. 创建收敛后的 NodeStateStore ABC（吸收 NodeState 的 invocation 版本链 + lifecycle 方法）
    b. 重构 Null/InMemory/Sqlite 三个实现（含 CAS 语义，ticket 31b）；
       同步移除 node_state.py 的 SUPERSEDED/PENDING SQL CHECK 约束 + constants.py 的两个 enum 值
    c. coordinator 移除 6 个 lifecycle 方法 + _node_states dict → 一个 _node_state_store；
       __init__ 改收 instance_store（ticket 22）；更新 create_null_coordinator（NullNodeStateStore）
    d. GraphContext 新增 ctx.node_state_store——property 委托 ctx.coordinator.node_state_store()，
       零构造点改动（ticket 23 修订；orchestrator / ReActAgent / fork 三处自动继承）
    e. Node.run() 改为调 ctx.node_state_store（不再调 ctx.coordinator 的 lifecycle 方法）
    f. 移除 PENDING status
    g. 删除 NodeState 旧 API + NodeState 名字 + NodeStateFactory
12. Store 构造契约统一（ticket 29）：caller-owned connection；SqliteGraphInstanceStore 异类收敛；
    InvocationStateError 新增；线程契约写入 ABC 文档（ticket 31c）

阶段 C：框架接线（ticket 24 + 29 注入部分 + 31a 修订 + 32）
13. CoordinatorFactory ABC + NullCoordinatorFactory；GraphOrchestrator/recovery 单注入点接线（ticket 29）
14. LinearScheduler recovery 4 行（ticket 24）
15. parallel.py ON_RECEIVE per-node 串行门 + 谨慎使用注释 + TODO（ticket 31a 修订）
16. Recovery 重建 + GraphMetadata 修剪（原子落地，ticket 32）：_restore_from_recovery 重写
    （pending 重建**无条件执行**——不挂 has_prior_state 门；iteration_count 从 COMPLETED 计数派生；
    instance_seq 重置）；_redispatch_from_recovery 删 COMPLETED+delivers 捷径；
    **同步**修剪 GraphMetadata 4 字段 + graph_orchestrator.py 构造点修剪

阶段 D：收尾
17. 更新 GraphSpec/GraphSpecCompiler（state_factory → state_class）+ graph_orchestrator.py
    （DynamicStateFactory → state_class()；update_status 调用去 .value，ticket 22）
18. 更新 __init__.py 导出（移除清单见上文；新增：CoordinatorFactory / NullCoordinatorFactory /
    NodeStateStoreFactory / NullNodeStateStore / InMemoryNodeStateStore / SqliteNodeStateStore /
    InvocationStateError / GraphInstanceStore 三实现）
19. 更新测试（~30+ 站点 + NodeStateStore 收敛测试 + CAS 测试 + ON_RECEIVE 门测试 + recovery 重建测试）
20. 重写 distributed-persistence.md 为新设计权威文档（三层持久化 + 共享 state + full snapshot +
    CoordinatorFactory 注入 + CAS + 零 bookkeeping + 双 scheduler recovery）；
    已关闭 ticket 文件移入 issues/history/（决策轨迹保留）
```

每步可独立验证（测试通过）后再进行下一步。

## Resolution criteria

明确：
- ✅ 每项移除的确认（零 caller / 被 ticket 33 取代）— ticket 33 的移除清单已验证
- ✅ 移除顺序和依赖关系 — 上方 13 步
- ⬜ `__init__.py` 导出清理 — 清单已列出，实现时执行
- ⬜ 对测试的影响 — ticket 33 已列出 DELETE/ADAPT 分类
- ✅ 对 ticket 23 决议的依赖 — ticket 23 已关闭，NodeStateStore 收敛设计已确认（保留 NodeStateStore，移除 NodeState，lifecycle 移入 store）
