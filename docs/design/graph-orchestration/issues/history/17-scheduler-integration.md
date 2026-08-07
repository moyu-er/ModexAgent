# 17 — CheckpointData 移除 + scheduler 集成

**What to build:** ParallelScheduler 和 LinearScheduler 接入 coordinator — 移除 checkpoint 逻辑,dispatch handler 调 coordinator.route_deliver,execute 前设 ctx.current_invocation。CheckpointData 完全移除。现有测试更新后全绿。

**Blocked by:** 16 — Node.run() 演进 + GraphContext coordinator(依赖新签名 + ctx 字段)

**Status:** ready-for-agent

**Design ref:** distributed-persistence-design.md §4.10, §7.1, §12 Phase 4

## 交付内容

### ParallelScheduler 集成(§4.10)
- 移除 `_build_checkpoint_data` / `_schedule_checkpoint` / `_restore_from_checkpoint`
- `run_async` 顶部: `ctx.coordinator.load_for_recovery()` 替代 checkpoint 恢复(返回含 rebuilt_main_state 的 RecoveryContext)
- dispatch handler: `ctx.coordinator.route_deliver(target, content, source_node=ctx.current_invocation.node_name, source_invocation_id=ctx.current_invocation.invocation_id)`(F6)
- `_execute_instance`: execute 前设 `ctx.current_invocation = invocation`(从 begin_invocation 返回值获取)

### LinearScheduler 集成
- dispatch handler: 同 ParallelScheduler(调 coordinator.route_deliver)
- Null 默认 coordinator(行为不变)

### GraphEngine 回退
- 移除 `checkpoint_store` 参数(改由 coordinator 持有)

### CheckpointData 移除(§7.1)
- 移除 `CheckpointData` + `CheckpointStore` ABC + `MemoryCheckpointStore` / `SqliteCheckpointStore`
- 移除 ParallelScheduler 的 `_checkpoint_store` 字段

### Recovery 补充(F1/F2/F5)
- Recovery 流程(§5): SUPERSEDED 无后继 → re-dispatch(F1);自动 promote COMPLETED 的 CONSUMED_PENDING(F2);_recheck_pending 查 deliver_store 的 PENDING delivers 给 COMPLETED nodes(F5)

## Acceptance criteria

- [ ] ParallelScheduler 无 _build_checkpoint_data / _schedule_checkpoint / _restore_from_checkpoint
- [ ] ParallelScheduler.run_async 顶部调 ctx.coordinator.load_for_recovery()
- [ ] ParallelScheduler dispatch handler 调 coordinator.route_deliver(F6: 从 ctx.current_invocation 读取 source)
- [ ] _execute_instance 在 node.run() 前设 ctx.current_invocation
- [ ] LinearScheduler dispatch handler 调 coordinator.route_deliver
- [ ] GraphEngine 无 checkpoint_store 参数
- [ ] CheckpointData + CheckpointStore 完全移除
- [ ] Recovery 补充: SUPERSEDED 无后继 → re-dispatch(F1)
- [ ] Recovery 补充: 自动 promote COMPLETED 的 CONSUMED_PENDING delivers(F2)
- [ ] Recovery 补充: _recheck_pending 查 deliver_store PENDING delivers(F5)
- [ ] 现有测试更新后全绿(所有 Node.run() 调用更新:无 upstream_payloads,coordinator via ctx)
- [ ] ParallelScheduler 恢复测试通过(coordinator + Memory 实现)
- [ ] LinearScheduler 行为不变(Null 实现)
- [ ] mypy clean
