# 16 — Node.run() 演进 + GraphContext coordinator + current_invocation

**What to build:** Node.run() 接入 coordinator 统一生命周期调度 — 删除 upstream_payloads/coordinator/enforce_deliver 参数,coordinator 通过 ctx.coordinator(always present)。GraphContext 加 coordinator + current_invocation 字段。fork() 传播 coordinator。这是签名变更(wide refactor,不可 expand-contract,一次性切换)。

**Blocked by:** 15 — GraphPersistenceCoordinator 完整实现(依赖 coordinator 方法)

**Status:** ready-for-agent

**Design ref:** distributed-persistence-design.md §3.3.2, §3.3.3, §6, §4.10, §7.2

## 交付内容

### Node.run() 新签名(C1/C4)
- `async def run(self, ctx: GraphContext[S], *, graph: CompiledGraph[S] | None = None) -> NodeResult`
- 删除 `upstream_payloads` 参数(C4: integrate 总是从 deliver_store)
- 删除 `coordinator` 参数(C1: coordinator 在 ctx.coordinator)
- 删除 `enforce_deliver`(always enforce)

### Node.run() 统一生命周期调度(§3.3.2)
- step 1: `ctx.coordinator.begin_invocation(node_name)`(F8: 无 parent_version)
- step 2: integrate — 检查 prev 是 SUPERSEDED + 有 state_json → I16 resume 用 snapshot 跳过 re-consume;否则正常 collect_consumable_delivers + mark_delivers_consumed + integrate
- step 3: try: execute + retry + submit;complete_invocation(含 F3 promote + F2 事务)
- step 4: except GraphBubbleUp → cancel_invocation
- step 5: except GraphInterrupt → **ctx.state.checkpoint() 直接调用(缺口C: 不能用 state_schema().fields 迭代)** → suspend_invocation(suspended=True, F4)
- step 6: except Exception → crash_invocation
- step 7: finally → finalize_invocation

### GraphContext 演进(§4.10, §7.2)
- 加 `coordinator: GraphPersistenceCoordinator` 字段(always present)
- 加 `current_invocation: InvocationContext | None` 字段(F6: scheduler 在 execute 前设置,dispatch handler 读取 source_node + source_invocation_id)
- `fork()` 加 `coordinator` 参数:默认继承父(shared);子图用子 coordinator(缺口B)

## Acceptance criteria

- [ ] Node.run() 签名: `async def run(self, ctx, *, graph=None) -> NodeResult`(无 upstream_payloads, 无 coordinator, 无 enforce_deliver)
- [ ] Node.run() 调用 ctx.coordinator.begin_invocation(node_name)(无 parent_version 参数,F8)
- [ ] Node.run() integrate 总是从 deliver_store(通过 coordinator.collect_consumable_delivers)
- [ ] Node.run() I16 resume 检查: prev 是 SUPERSEDED + 有 state_json → 用 snapshot 跳过 re-consume
- [ ] Node.run() suspend 时调 ctx.state.checkpoint() 直接(缺口C 警告:不能用 state_schema().fields)
- [ ] GraphContext 有 coordinator 字段(always present)
- [ ] GraphContext 有 current_invocation 字段(F6)
- [ ] fork() 传播 coordinator(默认继承父;可覆盖)
- [ ] Node.run 生命周期测试通过: begin → integrate → execute → complete/cancel/suspend/crash → finally
- [ ] I16 resume 跳过 re-consume 测试通过
- [ ] fork() coordinator 传播测试通过
- [ ] mypy clean
