# LinearScheduler recovery 契约

Status: triage:closed
Blocked by: none
Resolved: 2026-08-04

## Question

`LinearScheduler` 无 recovery 路径（不调 `load_for_recovery`），但它是默认 scheduler。`ParallelScheduler.run_async` 在顶部调 `ctx.coordinator.load_for_recovery()`（`parallel.py:194`），`LinearScheduler.run_async` 从 `entry_node` 顺序执行（`linear.py:80`），零 recovery 代码。

crash 后 `recover_crashed` 会用 Null coordinator 重建，`run_async` 从 `entry_node` 重跑 — 丢失所有进度。但 orchestrator 不阻止 LINEAR 图被 recover。

如何处理？

选项：
1. **Scheduler ABC 加 recovery 契约** — `Scheduler` ABC 加 `load_for_recovery` 调用要求（或 abstract method），两个 scheduler 都实现。LinearScheduler 加 recovery 分支（恢复 state.resume_target + 从断点节点继续）。
2. **编译时强制 recoverable 图用 PARALLEL** — `GraphSpecCompiler` 或 `TopologyValidator` 检查 `scheduler_kind`，如果图声明为 recoverable（或使用了需要持久化的特性），强制 `SchedulerKind.PARALLEL`。LinearScheduler 保持无 recovery。
3. **LinearScheduler 加简化 recovery** — 只恢复 `state.resume_target`（从 `from_checkpoint`），不恢复 scheduler bookkeeping（`_iteration_count` / `_instance_seq` / `_activated_sources` / `_pending_dispatches`）。从 `entry_node` 开始但 `resume_target` 路由到断点节点。
4. **运行时拒绝 recovery for LINEAR** — `GraphRecoveryService` 在 `recover_crashed` / `resume` 时检查 scheduler kind，LINEAR 图 raise 明确错误而非静默重跑。

## Context

- `Scheduler` ABC（`base.py:20`）只有一个 abstract method: `run_async(ctx) -> S`。Recovery 不在 ABC 契约里。
- `LinearScheduler.run_async`（`linear.py:54-130`）: 从 `entry_node` 顺序执行，`while current != GraphNode.END`。re-entry 完全依赖 `state.resume_target`。
- `LinearScheduler` docstring（`linear.py:60-64`）: "Re-entry semantics: always starts from `entry_node`. The scheduler is stateless across `run_async` calls — no internal 'resume context'. Resume routing is driven by `state.resume_target`."
- `GraphRecoveryService` docstring: recovery state loading happens "INSIDE `ParallelScheduler.run_async`" — implying LinearScheduler is not supported。
- LinearScheduler 的 dispatch handler（`_handle_linear_dispatch`）调用 `coordinator.route_deliver`，所以 deliver 持久化是接通的。但 scheduler bookkeeping（哪个节点执行到哪了）不持久化。
- ReActAgent 默认用 LinearScheduler（`build_react_graph` 选 LINEAR），ReActAgent 路径用 Null coordinator + AgentContext 持有状态 — recovery 由 AgentContext 处理，不依赖 scheduler recovery。

## Resolution criteria

明确：
- LinearScheduler 是否需要 recovery（或明确不支持）
- 如果需要：recovery 的范围（完整 / 简化 / 只靠 resume_target）
- 如果不需要：如何防止 LINEAR 图被错误 recover（编译时 / 运行时）
- Scheduler ABC 契约是否变化
- 对 ReActAgent 路径的影响（ReActAgent 用 LINEAR + Null coordinator，不依赖 scheduler recovery）

## Resolution

### 设计哲学

**所有调度方式都支持 recovery，但 recovery 不是 scheduler 的能力。**

三层职责分层：
- **coordinator（GraphInstance 层）**：持久化 state + `load_for_recovery()` 加载数据 + invocation 原语
- **scheduler**：调 `coordinator.load_for_recovery()` → 用恢复的数据恢复 `ctx.state` → 正常调度循环
- **node**：被 dispatch 后，通过 state（含 `resume_target`）+ invocation 原语 + 自身业务状态做幂等处理

scheduler 不"实现 recovery" — 它只是"用恢复的数据正常调度"。InMemory 策略下 `load_for_recovery` 返回空 RecoveryContext，scheduler 从 fresh start — 这是策略语义，不是缺陷。

调度方式与持久化正交：任何 scheduler + 任何 coordinator 策略组合都语义正确（Null = 无持久化，Memory = 单进程，SQLite = 跨进程恢复）。

### 决策

**LinearScheduler 加 recovery，镜像 ParallelScheduler 模式。**

在 `linear.py:76-78` 之后、`line 80` 之前插入：
```python
recovery = ctx.coordinator.load_for_recovery()
has_prior_state = any(v is not None for v in recovery.node_states.values())
if has_prior_state and recovery.rebuilt_main_state:
    state_class = type(ctx.state)
    ctx.state = state_class.from_checkpoint(recovery.rebuilt_main_state)
```

然后不变：从 `entry_node` 开始顺序执行。

### 两种 scheduler 的 recovery 差异是调度模型的自然差异

| | ParallelScheduler | LinearScheduler |
|---|---|---|
| `load_for_recovery()` | ✓（parallel.py:194） | ✓（新增） |
| 恢复 `ctx.state` | ✓（parallel.py:294-298, from_checkpoint） | ✓（新增，同模式） |
| 恢复后调度决策 | `_redispatch_from_recovery`（按 InvocationStatus 决定跳过/re-dispatch — 多实例调度决策） | 从 `entry_node` 开始（顺序调度决策） |
| 恢复 scheduler bookkeeping | ✓（_iteration_count / _instance_seq / _activated_sources / _pending_dispatches） | 不需要（顺序执行无 _ready set / _activated_sources） |

差异在调度决策，不在 recovery 实现。两者都是"用恢复的数据正常调度"。

### 补充确认（2026-08-04，ticket 33 最终决议后）

ticket 33 的最终决议简化了 recovery 的内部实现，但**不改变本 ticket 的 recovery 契约**：

- **SUPERSEDED 移除**：`_redispatch_from_recovery` 的调度决策分支简化——不再有 SUPERSEDED → re-dispatch 分支。suspended RUNNING → re-dispatch（resume）。CRASHED → re-dispatch（fresh）。但 recovery 契约（coordinator 加载数据 + scheduler 用数据调度 + node 幂等）不变。
- **`rebuild_main_state` 简化**：从两阶段 apply（COMPLETED 先 → SUPERSEDED 后）改为单次查询 `max(updated_at)` 中的 `{COMPLETED, suspended RUNNING}`。`load_for_recovery` 返回的 `rebuilt_main_state` 内容更准确（full snapshot 而非 delta），但 `load_for_recovery` 的调用模式和返回类型不变。
- **`complete_invocation` 存 full snapshot**：COMPLETED 记录的 `state_json` 从 `{}`（delta，生产 node 零写入）变为完整状态快照。recovery 重建的 `ctx.state` 现在包含真实状态而非空字典——**修复了 pre-existing bug**（之前 recovery 对不 suspend 的图重建出空状态）。
- **LinearScheduler 不受影响**：LinearScheduler 的 recovery 路径（`load_for_recovery` + `from_checkpoint` + 从 `entry_node` 开始）不变。`resume_target` 约定不变。

### entry node 的 resume_target 约定

`state.resume_target` 定义在 `GraphState` 基类（`state.py:105`），所有图状态都有。框架不提供通用 entry node — 图作者遵循约定：entry node 读 `state.resume_target`，路由到断点节点（如 ReAct StartNode `react/nodes/start.py:28-31`）。如果不读，从头重跑 — node 自己负责幂等。这符合"node 的职责是幂等可恢复"的设计哲学。

### 不做的事

- **不加 Scheduler ABC recovery 方法** — recovery 不是 ABC 契约，是 coordinator 职责。两个 scheduler 都在 `run_async` 顶部调 `coordinator.load_for_recovery()`，但这是调用模式，不是 ABC 强制
- **不加运行时拒绝** — LinearScheduler 支持恢复
- **不加编译时强制** — coordinator 是运行时注入，编译时看不到
- **不提取共享 helper** — `load_for_recovery` + `has_prior_state` + state 恢复是 4 行代码，ParallelScheduler 的 `_restore_from_recovery` 逻辑正确地不同（恢复多实例 bookkeeping），提取 helper 收益不大

### ReActAgent 路径不受影响

ReActAgent 用 Null coordinator（`agent.py:286`）。Null coordinator 的 `load_for_recovery()` 返回空 RecoveryContext（`persistence_coordinator.py:649-672`，`metadata is None` 分支）。`has_prior_state = False`，state 不恢复 — 行为完全不变。AgentContext 持有跨 turn 状态，与 scheduler recovery 正交。

### 客观评价

相比"运行时拒绝 LinearScheduler recovery"的替代方案：
- **更精简**：新增 4 行代码，0 新类型，0 新 ABC 方法（vs 新异常类型 + runtime guard）
- **更收敛**：一条 recovery 模式（两者都调 load_for_recovery → 恢复 state → 正常调度），差异在调度决策（vs 两条路径：有 recovery / 无 recovery + reject）
- **更符合通用框架设计**：所有 scheduler 平等，调度方式与持久化正交（vs LinearScheduler 被标记为"不支持 recovery"的二等公民）
