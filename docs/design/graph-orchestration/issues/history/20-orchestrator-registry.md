# 20 — GraphOrchestrator 注册表 + coordinator 创建 + 驱逐 + GraphControlService 收敛

**What to build:** GraphOrchestrator 加 _active_instances 注册表管理 GraphInstance 生命周期;per-GraphInstance 创建 coordinator;register_node 在构造时;crash recovery 重建 GraphInstance;GraphControlService._deliver 收敛到 coordinator.route_deliver;coordinator.close() + unregister_instance 驱逐机制。

**Blocked by:** 19 — GraphInstance 演进为运行时 class(依赖运行时 class 持有 coordinator)

**Status:** ready-for-agent

**Design ref:** distributed-persistence-design.md §13.2, §4.10, §7.2, F7/F9/F10, I2/I3

## 交付内容

### GraphOrchestrator 注册表(§13.2, C2)
- 加 `_active_instances: dict[int, GraphInstance]` 注册表
- `create_and_run`: create GraphMetadata → save → create coordinator(根据策略选 Null/Memory/SQLite,注入 factory)→ create GraphInstance(metadata, coordinator)→ register → _execute
- `_execute`: 从注册表取 GraphInstance → use coordinator → create GraphContext(coordinator=gi.coordinator)→ GraphEngine → run_async

### register_node 在构造时(I3/I19)
- orchestrator 遍历 compiled.nodes 调 coordinator.register_node(编译后,_execute 前)
- GraphSpecCompiler 不改(不持有 coordinator)

### 驱逐机制(F9/F10)
- `GraphOrchestrator.unregister_instance(graph_instance_id)`: 调 `coordinator.close()` → 从 _active_instances 移除
- `coordinator.close()`: SQLite 策略调 connection.close();Null/Memory no-op
- 触发条件: terminal status(COMPLETED/FAILED/CRASHED)+ 显式应用调用
- 不依赖 "natural GC"(dict 强引用阻止 GC)

### crash recovery(§13.2)
- load GraphMetadata from store → reconstruct coordinator(SQLite stores, state recovered from DB)→ create new GraphInstance → register
- **old GraphInstance 先 unregister**(关旧 connection)再注册新的

### GraphControlService 收敛(I2/F7)
- `_deliver` 从 `_active_instances[graph_instance_id].coordinator` 获取 coordinator 引用(F7: 直接获取,不经 controller 中转)
- 调 `coordinator.route_deliver(target_node=node_name, content=content, source_node="__external__", source_invocation_id=0)`
- 移除 GraphControlService 的共享 deliver_store

### GraphRecoveryService 接入
- 恢复流程: load GraphMetadata → reconstruct coordinator → create GraphInstance → register → _execute

## Acceptance criteria

- [ ] GraphOrchestrator 有 _active_instances: dict[int, GraphInstance] 注册表
- [ ] create_and_run 创建 coordinator(根据策略选 Null/Memory/SQLite)+ GraphInstance + register
- [ ] _execute 从注册表取 GraphInstance,用其 coordinator 创建 GraphContext
- [ ] register_node 在 GraphInstance 构造时(遍历 compiled.nodes)
- [ ] unregister_instance(gid) 调 coordinator.close() + 从 dict 移除(F9)
- [ ] coordinator.close() 关 SQLite connection(F10);Null/Memory no-op
- [ ] crash recovery: old GraphInstance 先 unregister 再注册新的
- [ ] GraphControlService._deliver 从 _active_instances[gid].coordinator 获取 coordinator(F7)
- [ ] GraphControlService._deliver 调 coordinator.route_deliver(source="__external__", source_invocation_id=0)
- [ ] GraphControlService 无共享 deliver_store(I2 收敛)
- [ ] GraphRecoveryService 接入: load metadata → reconstruct coordinator → create GraphInstance → _execute
- [ ] GraphOrchestrator E2E 测试通过(创建→执行→恢复→驱逐)
- [ ] GraphControlService deliver 收敛测试通过
- [ ] mypy clean
