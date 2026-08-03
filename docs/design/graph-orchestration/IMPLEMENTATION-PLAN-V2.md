# 分布式持久化实现计划 V2(整合 Design-Closure 修复)

> **基准**: distributed-persistence-design.md §12 Phase 1-5 + §15.5 F1-F11 critical finding 修复
> **约束**: 不修改存量 ticket(issues/02|04|07|10),新建增量 ticket(11-NN)
> **日期**: 2026-08-03

---

## 1. 任务总览

### 1.1 任务来源

| 来源 | 内容 | 整合策略 |
|------|------|----------|
| §12 Phase 1 | 持久化接口与类型定义 | 整合 F4(suspended 字段)/F11(factory required) |
| §12 Phase 2 | 三种实现(Null/Memory/SQLite) | 整合 F13(schema 迁移)/I14(共享 connection) |
| §12 Phase 3 | GraphPersistenceCoordinator | 整合 F1/F2/F3(recovery + promote + 事务)/F8(无 parent_version) |
| §12 Phase 4 | Node.run() 演进 + scheduler 集成 | 整合 F6(current_invocation)/C1/C4 |
| §12 Phase 5 | GraphInstance 演进 + GraphOrchestrator 接线 | 整合 F7/F9/F10(驱逐 + close + GraphControlService 收敛) |
| §15.5 F1-F11 | 11 项 critical finding 修复 | 按影响 Phase 归属(见 §1.3) |

### 1.2 Wide Refactor 识别

| Refactor | 类型 | 策略 |
|----------|------|------|
| GraphInstance: frozen Pydantic → 运行时 class (C2) | wide refactor | expand-contract: 先定义 GraphMetadata(expand,已在设计 §4.3)→ GraphInstance 演进为持有 GraphMetadata 的普通 class → callers 逐步迁移 → 移除旧 frozen Pydantic 形式(contract)。callers 大多访问 graph_instance_id/status(委托 metadata),blast radius 可控(~33 callers) |
| NodeState ABC: read/write/snapshot/restore → save_invocation/load/query_versions | wide refactor | expand-contract: 新接口与旧接口共存(expand)→ 逐步迁移 callers → 移除旧接口(contract)。当前零生产 caller(hypothetical seam),blast radius 极小 |
| DeliverStore ABC: accumulate/query_pending/mark_submitted → accumulate(新签名)/query_consumable/mark_consumed/promote_consumed | wide refactor | expand-contract: 新方法与旧方法共存 → 逐步迁移 callers(ParallelScheduler/GraphControlService)→ 移除旧方法。blast radius 中等(~10 callers) |
| Node.run() 签名: 加 coordinator(通过 ctx) + 删 upstream_payloads + 删 enforce_deliver | wide refactor | expand-contract 不可行(签名变更必须原子)→ 一次性切换,所有 callers 同步更新。blast radius: scheduler 2 处 + 所有 Node 子类(但子类只实现 execute,不改 run) |

### 1.3 F1-F11 归属表

| # | Finding | 影响层 | 归属 Task | 修订位置 |
|---|---------|--------|-----------|----------|
| F1 | SUPERSEDED crash → graph stuck | recovery | T14(coordinator begin_invocation 事务) + T19(recovery 逻辑) | §3.3.2 step 1, §5 |
| F2 | save COMPLETED + promote crash → double-effect | recovery | T14(coordinator complete_invocation 事务) + T19(recovery 自动 promote) | §3.3.2 step 3, §5 |
| F3 | v4 delivers 不被 v5 promote → 孤儿 | coordinator | T14(promote_delivers 改为 promote 所有 CONSUMED_PENDING) | §3.3.2 step 3, §4.4 |
| F4 | suspended 标记未定义 | 数据模型 | T11(NodeInvocationRecord 加 suspended 字段) | §4.2, §3.3.2, §3.4, §5 |
| F5 | pending_dispatches vs deliver_store 不同步 | recovery | T19(recovery _recheck_pending 查 deliver_store) | §5 |
| F6 | route_deliver source 参数不可用 | interface | T15(GraphContext 加 current_invocation) + T16(scheduler dispatch handler 注入) | §4.10, §7.2 |
| F7 | GraphControlService 收敛未接线 | lifecycle | T19(GraphControlService 直接获取 coordinator) | §13.2 |
| F8 | begin_invocation parent_version 来源 | interface | T14(移除参数,内部计算) | §4.4, §3.3.2 |
| F9 | GraphInstance 注册表无驱逐 | lifecycle | T19(unregister_instance) | §13.2 |
| F10 | SQLite connection 无 close | lifecycle | T19(coordinator.close) | §13.2 |
| F11 | factory=None rule 15 违规 | interface | T11(coordinator constructor factory required) — 但实际实现在 T14 | §4.4 |

---

## 2. 任务分解(10 个 task,对应 10 个新建 ticket)

### T11 — 持久化类型定义与 enum 拆分

**Phase**: 1
**Blocked by**: None — 可立即开始
**F-repair**: F4(suspended 字段), F11(factory 类型声明)

**交付**:
- NodeInvocationRecord: frozen Pydantic,加 `suspended: bool = False` 字段(F4)
- GraphMetadata: frozen Pydantic(§4.3 全字段)
- InvocationContext: frozen Pydantic(§4.8)
- RecoveryContext: frozen Pydantic,含 `rebuilt_main_state: dict[str, Any]`(I9/F8)
- GraphStateSnapshot: frozen Pydantic(§2.4)
- SchedulerInstanceStatus enum: DORMANT/READY/RUNNING/COMPLETED(I22 拆分)
- InvocationStatus enum: PENDING/RUNNING/COMPLETED/CANCELED/CRASHED/SUPERSEDED(I22 + I4)
- DeliverConsumptionStatus enum: PENDING/CONSUMED/CONSUMED_PENDING/CONSUMED_COMPLETED(I12)
- NodeState ABC 演进: save_invocation / load_invocation / load_latest / load_latest_completed / query_versions(新接口,与旧共存)
- GraphMetadataStore ABC: save / load / update_status
- NodeStateFactory ABC: create() -> NodeState
- DeliverStoreFactory ABC: create() -> DeliverStore(F11: required 类型,非 Optional)
- DeliverStore ABC 演进: accumulate(新签名)/ query_consumable / mark_consumed / promote_consumed / clear(I10: 移除 DeliverConsumer ABC)
- DeliverRecord 演进: 加 source_node/source_invocation_id/consumed_by_invocation_id/status(DeliverConsumptionStatus)

**验证**: mypy clean;新 ABC 不可直接实例化;现有测试全绿(无行为变更,纯类型定义)

---

### T12 — DeliverStore ABC 演进 + DeliverRecord 演进(独立 ticket,因 blast radius 中等)

**Phase**: 1(与 T11 并行?不 — T12 依赖 T11 的 DeliverConsumptionStatus enum)
**Blocked by**: T11
**F-repair**: I10(移除 DeliverConsumer ABC), I12(DeliverRecord enum)

**交付**:
- DeliverStore ABC: 新方法 query_consumable / mark_consumed / promote_consumed;accumulate 加 source_node/source_invocation_id 参数(新签名)
- 旧方法 mark_submitted / query_pending / query_by_target 保留(expand,待 contract 阶段移除)
- DeliverRecord: 加 source_node/source_invocation_id/consumed_by_invocation_id/status(DeliverConsumptionStatus)
- 移除 DeliverConsumer ABC + DefaultDeliverConsumer(I10: 不引入)
- DeliverStoreFactory ABC: create() -> DeliverStore

**验证**: mypy clean;现有 DeliverStore 实现仍编译(旧方法保留);新方法无实现(abstract)

---

### T13 — Null/Memory/SQLite 三种持久化实现

**Phase**: 2
**Blocked by**: T11, T12
**F-repair**: I13(schema 迁移 ALTER TABLE), I14(共享 SQLite connection)

**交付**:
- NullNodeState + NullGraphMetadataStore + NullNodeStateFactory(全 no-op)
- SimpleNodeState 演进(从 read/write/snapshot/restore → 新接口,memory dict 实现)
- MemoryGraphMetadataStore(dict 实现)
- SimpleNodeStateFactory
- SqliteNodeState 演进(新接口 + parent_version/status/suspended 字段 + schema 迁移: ALTER TABLE ADD COLUMN,幂等)
- SqliteGraphMetadataStore(graph_instances 表演进)
- SqliteNodeStateFactory
- NullDeliverStore(in-memory queue,无状态机,用于 ReActAgent)
- InMemoryDeliverStore(二态 PENDING/CONSUMED,promote 删除)
- SqliteDeliverStore(三态 PENDING/CONSUMED_PENDING/CONSUMED_COMPLETED,promote 升级)
- DeliverStoreFactory 三实现(SqliteDeliverStoreFactory 接受共享 connection 参数,I14)

**验证**: 每种实现的 CRUD 测试;round-trip(save→load→compare);Null 确认 no-op;schema 迁移幂等;共享 connection 测试

---

### T14 — GraphPersistenceCoordinator 完整实现

**Phase**: 3
**Blocked by**: T13
**F-repair**: F1(SUPERSEDED 事务), F2(complete 事务), F3(promote 所有), F8(无 parent_version), F11(factory required)

**交付**:
- GraphPersistenceCoordinator class
- constructor: `default_deliver_store_factory: DeliverStoreFactory` required(F11,非 Optional)
- register_node: factory required,无 `if is not None` guard
- begin_invocation(node_name) -> InvocationContext(F8: 无 parent_version 参数,内部从 load_latest_completed 计算;I18: version=max(已有)+1;F4: 检查 suspended=True → 标记 SUPERSEDED;I17: 内部 try/except 自清理;F1: SUPERSEDED 标记 + 新 invocation 创建包 SQLite 事务)
- complete_invocation(invocation, state) (F2: save COMPLETED + promote_delivers 包事务;F3: promote_delivers 升级该 node 所有 CONSUMED_PENDING)
- cancel_invocation / suspend_invocation(F4: suspended=True) / crash_invocation / finalize_invocation
- 消费方法(I10): collect_consumable_delivers / mark_delivers_consumed / promote_delivers
- load_for_recovery() -> RecoveryContext(I9: 含 rebuilt_main_state;I5: invocation_id 排序)
- rebuild_main_state(I5: 按 invocation_id 遍历 COMPLETED apply;最后 apply SUPERSEDED snapshot)
- load_latest_invocation(node_name)(I16: resume 判断)
- route_deliver(target, content, source_node, source_invocation_id)(I20: END 跳过)
- get_graph_state / finalize_invocation(F4: suspended=True 不动)

**验证**: coordinator 单元测试(mock NodeState + GraphMetadataStore);生命周期转换测试;版本链测试;recovery 测试(含 I16 resume 跳过 re-consume);F1/F2 事务测试(crash-between 模拟)

---

### T15 — Node.run() 演进 + GraphContext coordinator + current_invocation

**Phase**: 4
**Blocked by**: T14
**F-repair**: F6(current_invocation 字段), C1(无 coordinator=None), C4(无 upstream_payloads)

**交付**:
- Node.run(ctx, *, graph) 新签名(无 upstream_payloads,无 coordinator 参数,无 enforce_deliver)
- 统一生命周期调度: begin → integrate(总是从 deliver_store,I16 resume 检查)→ try(execute+retry+submit)→ complete(含 promote)/cancel/suspend/crash → finally
- GraphContext 加 coordinator 字段(always present)
- GraphContext 加 current_invocation: InvocationContext | None 字段(F6: scheduler 在 execute 前设置)
- fork() 传播 coordinator(缺口B: 默认继承父,子图用子 coordinator)
- ⚠️ 缺口C: suspend_invocation 调 ctx.state.checkpoint() 直接(不能用 state_schema().fields 迭代)

**验证**: Node.run 生命周期测试;integrate 从 deliver_store 测试;I16 resume 跳过 re-consume 测试;fork() coordinator 传播测试

---

### T16 — CheckpointData 移除 + scheduler 集成

**Phase**: 4
**Blocked by**: T15
**F-repair**: F6(dispatch handler 注入 source)

**交付**:
- ParallelScheduler: 移除 _build_checkpoint_data / _schedule_checkpoint / _restore_from_checkpoint
- run_async 顶部: ctx.coordinator.load_for_recovery() 替代 checkpoint 恢复
- dispatch handler: coordinator.route_deliver(target, content, source_node=ctx.current_invocation.node_name, source_invocation_id=ctx.current_invocation.invocation_id)(F6)
- _execute_instance: execute 前设 ctx.current_invocation = invocation(scheduler 从 begin_invocation 返回值获取)
- LinearScheduler: dispatch handler 同;Null 默认 coordinator
- GraphEngine: 移除 checkpoint_store 参数
- CheckpointData + CheckpointStore 移除(或保留为 Null/Memory 基类,后删)

**验证**: 现有测试更新后全绿;ParallelScheduler 恢复测试(coordinator + Memory);LinearScheduler 行为不变(Null)

---

### T17 — ReActAgent Null coordinator 接入(可与 T18 并行)

**Phase**: 4
**Blocked by**: T16
**F-repair**: 缺口A(正交层)

**交付**:
- ReActAgent.actual_turn: 创建 NullCoordinator(NullNodeState + NullGraphMetadataStore + NullDeliverStore)→ 注入 ReActGraphContext(coordinator=ctx.coordinator)
- AgentContext 状态管理不变(正交层: coordinator 管 node invocation,AgentContext 管 agent turn state)
- React 4 节点(START/LLM/TOOL/END)+ AgentNode 不需改(只实现 execute,继承 run)

**验证**: ReActAgent GraphInterrupt suspend/resume 测试(Null coordinator + AgentContext: resume_target 存活);React 4 节点 deliver-only 路由测试(NullDeliverStore in-memory queue)

---

### T18 — GraphInstance 演进为运行时 class(wide refactor,expand-contract)

**Phase**: 5
**Blocked by**: T16
**F-repair**: C2(GraphInstance 演进), A7(update_status 签名)

**交付**:
- GraphInstance: 从 frozen Pydantic → 普通 class
- 持有: GraphMetadata(可序列化值对象)+ coordinator + 可扩展字段
- 方法: get_state() / load_for_recovery() / update_status(status)(A7: 签名定义,委托 coordinator/metadata_store)
- GraphInstanceStore → GraphMetadataStore 演进(存 GraphMetadata,不存运行时 GraphInstance)
- InMemoryGraphInstanceStore → InMemoryGraphMetadataStore
- SqliteGraphInstanceStore → SqliteGraphMetadataStore
- ~33 callers 更新(大多访问 graph_instance_id/status → 委托 metadata)

**验证**: GraphInstance 测试;callers 全部更新;现有测试全绿

---

### T19 — GraphOrchestrator 注册表 + coordinator 创建 + 驱逐 + GraphControlService 收敛

**Phase**: 5
**Blocked by**: T18
**F-repair**: F7(GraphControlService), F9(unregister_instance), F10(coordinator.close), I3/I19(register_node 时机), I2(GraphControlService 收敛)

**交付**:
- GraphOrchestrator 加 _active_instances: dict[int, GraphInstance] 注册表
- create_and_run: create GraphMetadata → save → create coordinator(根据策略选 Null/Memory/SQLite,注入 factory)→ create GraphInstance(metadata, coordinator)→ register → _execute
- _execute: 从注册表取 GraphInstance → use coordinator → create GraphContext(coordinator=gi.coordinator)→ GraphEngine → run_async
- register_node 在 GraphInstance 构造时(I3/I19: orchestrator 遍历 compiled.nodes 调 coordinator.register_node)
- F9: unregister_instance(gid) → coordinator.close() → 从 dict 移除;触发: terminal + 显式调用
- F10: coordinator.close() → SQLite connection.close();Null/Memory no-op
- crash recovery: old GraphInstance 先 unregister(关旧 connection)再注册新的
- GraphRecoveryService 接入: load GraphMetadata → reconstruct coordinator → create GraphInstance → register → _execute
- F7/I2: GraphControlService._deliver 从 _active_instances[gid].coordinator 获取,调 route_deliver(source="__external__", source_invocation_id=0);移除共享 deliver_store

**验证**: GraphOrchestrator E2E 测试(创建→执行→恢复→驱逐);GraphControlService deliver 收敛测试;crash recovery(旧 instance unregister + 新创建)

---

### T20 — 集成测试 + 端到端验证

**Phase**: 5
**Blocked by**: T17, T19
**F-repair**: 无(验证 task)

**交付**:
- GraphOrchestrator E2E: 创建→执行→恢复(crash→recover_crashed→验证状态)
- GraphInterrupt suspend/resume E2E(Memory coordinator: 跨 _execute state snapshot 存活)
- GraphControlService deliver 收敛测试
- 自环节点(A→A)调度验证(§10 待办: 现有 scheduler 动态机制验证)
- ReActAgent + GraphOrchestrator 双路径验证(正交层)
- F1/F2 crash-between 事务验证(crash 模拟 → recovery → 状态一致)
- 全量测试套件绿

**验证**: 全量测试套件绿;E2E 场景全部通过

---

## 3. 依赖图

```
T11 (类型定义) ──┬──→ T12 (DeliverStore ABC) ──→ T13 (三实现) ──→ T14 (coordinator) ──→ T15 (Node.run) ──→ T16 (scheduler) ──┬──→ T17 (ReActAgent) ──┐
                │                                                                                                         ├──→ T18 (GraphInstance) ──→ T19 (Orchestrator) ──┤
                └──→ (T11 无 blocker,可立即开始)                                                                                                                              │
                                                                                                                                                                             ↓
                                                                                                                                                                    T20 (集成测试)
```

### 依赖关系明细

| Task | Blocked by | 可并行? |
|------|------------|---------|
| T11 | None | — (起始) |
| T12 | T11 | 否(依赖 T11 的 enum) |
| T13 | T11, T12 | 否(依赖 T11 类型 + T12 ABC) |
| T14 | T13 | 否(依赖三实现) |
| T15 | T14 | 否(依赖 coordinator) |
| T16 | T15 | 否(依赖 Node.run 签名) |
| T17 | T16 | **是,与 T18 并行**(都只依赖 T16) |
| T18 | T16 | **是,与 T17 并行**(都只依赖 T16) |
| T19 | T18 | 否(依赖 GraphInstance 演进) |
| T20 | T17, T19 | 否(最终验证,依赖全部) |

### 关键路径

T11 → T12 → T13 → T14 → T15 → T16 → T18 → T19 → T20(9 步顺序依赖)

### 并行机会

- T17(ReActAgent)与 T18(GraphInstance)在 T16 完成后可并行

---

## 4. 验证策略

### 4.1 每 Task 验证标准

| Task | 验证 | 门禁 |
|------|------|------|
| T11 | mypy clean;新 ABC 不可实例化;现有测试全绿 | ruff + mypy + pytest tests/unit/ |
| T12 | mypy clean;旧实现仍编译(旧方法保留) | ruff + mypy |
| T13 | CRUD + round-trip + Null no-op + schema 迁移幂等 | pytest tests/unit/modex_graph/ |
| T14 | 生命周期/版本链/recovery/resume 单元测试 + F1/F2 事务测试 | pytest tests/unit/ (coordinator) |
| T15 | Node.run 生命周期 + integrate + I16 resume + fork 传播 | pytest tests/unit/modex_graph/ |
| T16 | 现有测试更新后全绿 + ParallelScheduler 恢复 + LinearScheduler 不变 | pytest tests/unit/ + tests/integration/ |
| T17 | ReActAgent suspend/resume + React 4 节点 deliver-only | pytest tests/unit/agents/react/ |
| T18 | GraphInstance 测试 + callers 全更新 | pytest tests/unit/ + mypy |
| T19 | E2E(创建→执行→恢复→驱逐)+ GraphControlService 收敛 | pytest tests/integration/ |
| T20 | 全量测试套件绿 + E2E 场景 | pytest tests/ -v |

### 4.2 集成验证场景

1. **正常执行**: GraphOrchestrator create_and_run → 完成 → 验证状态
2. **GraphInterrupt suspend/resume**: execute → suspend → resume → 完成(Memory coordinator: 跨 _execute state snapshot 存活)
3. **Crash recovery**: execute → crash → recover_crashed → 验证状态(SQLite coordinator: 从 DB 重建)
4. **F1 crash-between**: 模拟 SUPERSEDED 标记后 crash → recovery → 验证 node re-dispatch
5. **F2 crash-between**: 模拟 save COMPLETED 后 promote 前 crash → recovery → 验证自动 promote
6. **自环节点(A→A)**: A execute → deliver to self → A complete → A re-dispatch → 消费自己的 deliver
7. **ReActAgent + GraphOrchestrator 双路径**: 两条路径都能 suspend/resume
8. **GraphControlService deliver**: 外部 DELIVER_TO_NODE → coordinator.route_deliver → target node 消费
9. **GraphInstance 驱逐**: terminal → unregister_instance → coordinator.close → connection closed

---

## 5. 风险与缓解

| 风险 | 影响 | 缓解 |
|------|------|------|
| GraphInstance wide refactor (T18) blast radius ~33 callers | 中 | expand-contract: GraphMetadata 先定义(已在设计 §4.3)→ GraphInstance 演进为持有 GraphMetadata → callers 访问 graph_instance_id/status 委托 metadata |
| Node.run() 签变更不可 expand-contract(必须原子) | 高 | T15 一次性切换,所有 callers 在 T16 同步更新;React 4 节点 + AgentNode 不需改(只实现 execute) |
| F1/F2 事务实现复杂度(SQLite transaction 跨 NodeState + DeliverStore) | 中 | 事务在 coordinator 层(coordinator 持有共享 connection);如事务不可行,退化为 recovery 自动修复(F2 step 6 已设计) |
| 自环节点调度未验证(§10 待办) | 低 | T20 集成测试验证;现有 scheduler 动态机制(_ready + _handle_dispatch + _recheck_pending)已支持 |
| input_integrator.integrate_from_snapshot 未定义(S9) | 低 | T15 实现时定义;可能是 InputIntegrator 的新方法或 coordinator 的辅助方法 |

---

## 6. 与存量 ticket 的关系

| 存量 ticket | 状态 | 与 V2 的关系 |
|-------------|------|-------------|
| issues/02-node-abstraction-design.md | 已实现(有设计修正通知) | V2 不修改;GraphAsNode/FunctionNode 重新定位已在设计文档 §9 |
| issues/04-graph-nesting-execution-model.md | 已实现(有设计修正通知) | V2 不修改;GraphInstance 演进(C2)是增量 |
| issues/07-long-running-node-execution.md | 已实现(有设计修正通知) | V2 不修改;deliver/submit 模型已在设计文档 §2.3 |
| issues/10-graph-lifecycle-management.md | 已实现(有设计修正通知) | V2 不修改;生命周期管理演进已在设计文档 §13 |

**V2 新建 ticket(11-20)**: 增量实现,不修改存量。存量 ticket 的 blockquote 通知指向 distributed-persistence-design.md(权威文档)。
