# 18 — ReActAgent Null coordinator 接入

**What to build:** ReActAgent 路径接入 Null coordinator — actual_turn 创建 NullCoordinator,注入 ReActGraphContext。AgentContext 状态管理不变(正交层: coordinator 管 node invocation,AgentContext 管 agent turn state)。React 4 节点 + AgentNode 不需改(只实现 execute,继承 run)。

**Blocked by:** 17 — CheckpointData 移除 + scheduler 集成(依赖 scheduler 接入 coordinator)

**Status:** ready-for-agent

**Design ref:** distributed-persistence-design.md §3.3.4, §13.2, §12 Phase 4.8

## 交付内容

### ReActAgent.actual_turn 接入(§3.3.4, 缺口A)
- 创建 `NullCoordinator`(GraphPersistenceCoordinator with NullNodeState + NullGraphMetadataStore + NullDeliverStore)
- 注入 `ReActGraphContext(coordinator=null_coordinator)`
- AgentContext 状态管理不变 — AgentContext(含 ReActTurnState)由 AgentPool 持有,跨 turn 存活
- Null coordinator 是 structural pass-through;AgentContext 是 active 状态机制

### 正交层论证(§3.3.4)
- coordinator: node invocation 持久化(版本链 + state_json + deliver 消费)— GraphOrchestrator 路径用 Memory/SQLite
- AgentContext: agent turn 状态持久化(ReActTurnState + resume_target + tool 批次)— ReActAgent 路径用 AgentPool
- 两条路径都用同一 Node.run() 代码路径(always coordinator),分歧在状态持有机制(不同关注点)

### React 4 节点 + AgentNode 不需改
- START / LLM / TOOL / END 节点只实现 `execute(ctx, integrated_input)`,继承 `run()` 不改
- AgentNode 同上
- GraphInterrupt suspend: ToolNode._suspend_for_approval 设 state.resume_target → ctx.interrupt → Node.run() except GraphInterrupt → coordinator.suspend_invocation(Null: no-op)→ AgentContext 持有状态

## Acceptance criteria

- [ ] ReActAgent.actual_turn 创建 NullCoordinator 并注入 ReActGraphContext
- [ ] NullCoordinator 的 begin/complete/suspend/crash_invocation 是 no-op 或 in-memory
- [ ] NullDeliverStore 的 accumulate/query_consumable/mark_consumed/promote_consumed 是 in-memory queue 操作
- [ ] React 4 节点(START/LLM/TOOL/END)不需修改 — 继承 Node.run() 即可
- [ ] AgentNode 不需修改 — 继承 Node.run() 即可
- [ ] ReActAgent GraphInterrupt suspend/resume 测试通过: suspend → AgentContext 持有 resume_target → resume → NEW GraphEngine → StartNode 读 resume_target → 路由到 TOOL
- [ ] React 4 节点 deliver-only 路由测试通过(NullDeliverStore in-memory queue)
- [ ] 现有 ReActAgent 测试更新后全绿
- [ ] mypy clean
