# 21 — 集成测试 + 端到端验证

**What to build:** 全量集成测试 — 验证分布式持久化设计在正常路径 + crash recovery + suspend/resume 边界场景下端到端闭环。包含 F1/F2 事务验证、自环节点调度验证、双路径(ReActAgent + GraphOrchestrator)验证。

**Blocked by:** 18 — ReActAgent Null coordinator 接入;20 — GraphOrchestrator 注册表 + coordinator 创建 + 驱逐 + GraphControlService 收敛

**Status:** ready-for-agent

**Design ref:** distributed-persistence-design.md §5, §3.3.3, §3.3.4, §10, IMPLEMENTATION-PLAN-V2.md §4.2

## 交付内容

### E2E 验证场景(9 个)

1. **正常执行**: GraphOrchestrator create_and_run → 完成 → 验证 GraphInstance status = COMPLETED
2. **GraphInterrupt suspend/resume**(Memory coordinator): execute → suspend → resume → 完成。验证: 跨 _execute state snapshot 存活;resume 后 StartNode 读 resume_target 路由正确;I16 跳过 re-consume(无 double-effect)
3. **Crash recovery**(SQLite coordinator): execute → crash → recover_crashed → 验证状态。验证: 从 DB 重建 main_state;CRASHED node re-dispatch;CONSUMED_PENDING delivers re-consume
4. **F1 crash-between 验证**: 模拟 SUPERSEDED 标记后 crash(无后继 invocation)→ recovery → 验证 node re-dispatch
5. **F2 crash-between 验证**: 模拟 save COMPLETED 后 promote 前 crash → recovery → 验证自动 promote CONSUMED_PENDING delivers
6. **自环节点(A→A)调度验证**(§10 待办): A execute → deliver to self → A complete → A re-dispatch → 消费自己的 deliver。验证: 现有 scheduler 动态机制(_ready + _handle_dispatch + _recheck_pending)支持自环;串行保证(A 执行中不并行调度)
7. **ReActAgent + GraphOrchestrator 双路径验证**: 两条路径都能 suspend/resume。验证: ReActAgent(Null coordinator + AgentContext)和 GraphOrchestrator(Memory coordinator)正交层正确
8. **GraphControlService deliver 收敛**: 外部 DELIVER_TO_NODE → coordinator.route_deliver(source="__external__")→ target node 消费。验证: 无共享 deliver_store;走统一 coordinator 路径
9. **GraphInstance 驱逐**: terminal → unregister_instance → coordinator.close → SQLite connection closed。验证: 注册表不累积;connection 不泄漏

### 全量测试套件
- `pytest tests/unit/ -v` 全绿
- `pytest tests/integration/ -v -m integration` 全绿
- `ruff check src/modex_graph src/modex_agent tests/` clean
- `mypy src/modex_graph src/modex_agent` clean

## Acceptance criteria

- [ ] 场景 1(正常执行)测试通过
- [ ] 场景 2(suspend/resume, Memory coordinator)测试通过 — state snapshot 跨 _execute 存活 + I16 跳过 re-consume
- [ ] 场景 3(crash recovery, SQLite coordinator)测试通过 — 从 DB 重建 + re-dispatch
- [ ] 场景 4(F1 crash-between)测试通过 — SUPERSEDED 无后继 re-dispatch
- [ ] 场景 5(F2 crash-between)测试通过 — 自动 promote
- [ ] 场景 6(自环节点 A→A)测试通过 — 现有 scheduler 动态机制支持
- [ ] 场景 7(双路径)测试通过 — ReActAgent + GraphOrchestrator 正交
- [ ] 场景 8(GraphControlService deliver 收敛)测试通过
- [ ] 场景 9(GraphInstance 驱逐)测试通过 — 注册表不累积 + connection 不泄漏
- [ ] `pytest tests/unit/ -v` 全绿
- [ ] `pytest tests/integration/ -v -m integration` 全绿
- [ ] `ruff check` clean
- [ ] `mypy` clean
