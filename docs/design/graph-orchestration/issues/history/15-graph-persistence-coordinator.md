# 15 — GraphPersistenceCoordinator 完整实现

**What to build:** coordinator 是分布式持久化的核心 — 统一调度 node 生命周期事件 + 持久化路由。完整实现所有方法,独立可测试(不接入 scheduler)。整合 F1/F2/F3/F8 修复:begin_invocation 事务、complete_invocation 事务、promote 所有 CONSUMED_PENDING、无 parent_version 参数。

**Blocked by:** 14 — Null/Memory/SQLite 三种持久化实现(依赖 ABC 的具体实现)

**Status:** ready-for-agent

**Design ref:** distributed-persistence-design.md §4.4, §3.3.2, §3.3.3, §5

## 交付内容

### coordinator class(§4.4)
- constructor: graph_instance_id, graph_metadata_store, default_node_state_factory, **default_deliver_store_factory(required,F11)**
- `register_node(node_name, node_state=None, deliver_store=None)`: factory required,无 `if is not None` guard

### 生命周期方法(F8/F1/F2/F3/F4)
- `begin_invocation(node_name) -> InvocationContext`: **F8: 无 parent_version 参数**,内部从 load_latest_completed 计算;I18: version=max(已有)+1;F4: 检查 suspended=True → 标记 SUPERSEDED;I17: 内部 try/except 自清理;F1: SUPERSEDED 标记 + 新 invocation 创建包 SQLite 事务
- `complete_invocation(invocation, state)`: save COMPLETED;**F3: promote_delivers 升级该 node 所有 CONSUMED_PENDING**(不限 invocation_id);F2: save COMPLETED + promote 包 SQLite 事务
- `cancel_invocation(invocation)`: save CANCELED
- `suspend_invocation(invocation, state_snapshot)`: save RUNNING + state_json=snapshot + **suspended=True**(F4)
- `crash_invocation(invocation)`: save CRASHED
- `finalize_invocation(invocation)`: 安全网 — orphan PENDING(suspended=False)→ CRASHED;suspended=True 不动;SUPERSEDED 不动

### 消费方法(I10: 替代 DeliverConsumer ABC)
- `collect_consumable_delivers(node_name, invocation_id) -> list[DeliverRecord]`: 委托 deliver_store.query_consumable
- `mark_delivers_consumed(node_name, deliver_ids, invocation_id)`: 委托 deliver_store.mark_consumed
- `promote_delivers(node_name, invocation_id)`: **F3: 委托 deliver_store.promote 该 node 所有 CONSUMED_PENDING**

### 恢复方法(I5/I9/I16)
- `load_for_recovery() -> RecoveryContext`: 加载 metadata + 各 node load_latest + rebuild_main_state;返回含 rebuilt_main_state 的 RecoveryContext(I9)
- `rebuild_main_state()`: 按 **invocation_id 全局排序**(I5)遍历 COMPLETED apply state_update;最后 apply SUPERSEDED 的 state_snapshot
- `load_latest_invocation(node_name)`: 加载最新 invocation(I16 resume 判断)
- `get_graph_state(node_status_filter) -> GraphStateSnapshot`: 收集 metadata + 各 node query_versions

### 路由方法(I20)
- `route_deliver(target_node, content, source_node, source_invocation_id) -> int | None`: target == END 跳过;否则路由到 deliver_store.accumulate
- `get_deliver_store(node_name) -> DeliverStore | None`: 外部查询

## Acceptance criteria

- [ ] begin_invocation 无 parent_version 参数(F8),内部从 load_latest_completed 计算
- [ ] begin_invocation 检查 suspended=True → 标记 SUPERSEDED(F4)
- [ ] begin_invocation 内部 try/except 自清理 PENDING(I17)
- [ ] begin_invocation 的 SUPERSEDED 标记 + 新 invocation 创建在 SQLite 中包事务(F1)
- [ ] complete_invocation 的 save COMPLETED + promote_delivers 在 SQLite 中包事务(F2)
- [ ] promote_delivers 升级该 node 的所有 CONSUMED_PENDING delivers(F3,不限 invocation_id)
- [ ] suspend_invocation 设 suspended=True(F4)
- [ ] finalize_invocation 跳过 suspended=True 的 RUNNING(F4)
- [ ] default_deliver_store_factory 是 required 参数(F11)
- [ ] load_for_recovery 返回含 rebuilt_main_state 的 RecoveryContext(I9)
- [ ] rebuild_main_state 按 invocation_id 排序(I5),最后 apply SUPERSEDED snapshot
- [ ] route_deliver 对 END target 跳过(I20)
- [ ] coordinator 单元测试通过(用 mock stores): 生命周期转换 / 版本链 / recovery / resume 跳过 re-consume(I16)
- [ ] F1 事务测试: 模拟 SUPERSEDED 标记后 crash → recovery → node re-dispatch
- [ ] F2 事务测试: 模拟 save COMPLETED 后 promote 前 crash → recovery → 自动 promote
- [ ] mypy clean
