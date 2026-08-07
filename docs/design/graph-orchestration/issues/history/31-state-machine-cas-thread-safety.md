# 状态机 CAS + SQLite connection 线程安全

Status: triage:closed (resolved 2026-08-04)
Blocked by: none (was: [Fork-merge 依赖链移除](33-fork-merge-removal.md))

## Question

ticket 33 移除 fork-merge 但保留 ParallelScheduler 并发调度。并发安全问题简化：

- **fork-merge 的 conflict detection 消失** — 共享 state，无 fork 写冲突
- **并发 invocation 安全** — 重新评估
- **状态机 CAS** — 仍然需要（终态不可变性不强制）
- **SQLite connection 线程安全** — 仍然存在

## Resolution（并发串行化部分）

> **⚠️ 已被 2026-08-04 grilling 裁决修订**（见下方「Resolution（2026-08-04 闭环）」）：「不加 scheduler gate」翻转为「ON_RECEIVE 加 per-node 串行门」。以下原文保留作决策轨迹。

### 裁决：保留 coordinator 现有机制，不加 scheduler gate

explore 验证的关键事实：

1. **生产图用 LinearScheduler**（顺序执行，无并发）— `Graph.compile()` 默认 `SchedulerKind.LINEAR`（graph.py:117）
2. **ReAct 用 NullCoordinator**（`create_null_coordinator()`，agent.py:286）— 无持久化、无版本链、无 crash recovery
3. **ReAct 图用默认 ON_ALL_PREDS**（无 node 设置 ON_RECEIVE）— grep `ON_RECEIVE` in `src/modex_agent` 返回零匹配
4. **ON_ALL_PREDS 有 reachability BFS 门控**（`_can_reach_active`）— 同 node 的并发 dispatch 在大多数拓扑下被阻止

同 node 并发 dispatch（ON_RECEIVE + ParallelScheduler）只在测试和示例中出现，不是生产场景。

### coordinator 的 crash-on-prior-RUNNING 已足够

coordinator 的 `begin_invocation` 发现同 node 有 prior RUNNING（非 suspended）→ 标记 CRASHED（`persistence_coordinator.py:344-357`）。这是 **crash recovery 机制**，不是并发门控。

对于 true crash（进程重启）：A 已死，标记 CRASHED 正确，B 从 fresh 开始。

对于并发 invocation（A 在 await 点，B 启动）：A 被标记 CRASHED 但 A 的 `node.run()` 仍在执行。当 A 恢复并调 `complete_invocation` 时，UPSERT 覆盖 CRASHED 为 COMPLETED — A 的 ghost completion 泄漏到版本链。

**但这不是生产问题**：
- 生产用 LinearScheduler — 无并发
- 生产用 NullCoordinator — `begin_invocation` 是 no-op（`NullNodeState` 所有方法返回 None/空）
- 只有 ParallelScheduler + SQLite coordinator + ON_RECEIVE 组合下才触发 — 测试/示例场景

### 不加 scheduler gate 的理由

1. **不为测试-only 场景增加生产复杂度** — scheduler gate 是新代码、新测试、新维护负担
2. **coordinator 机制对 true crash 正确** — 这是它的设计目的
3. **ON_ALL_PREDS 的 reachability BFS 已门控大多数场景** — 同 node 并发 dispatch 需要特定拓扑
4. **write-disjoint 契约覆盖跨 node 并发** — 不同 node 写不同字段

### 如果未来生产图用 ParallelScheduler + ON_RECEIVE

那时再加 scheduler gate。gate 的实现是 `_handle_dispatch` ON_RECEIVE 路径加一个 `if node_name in self._running_nodes: queue` 判断 — 小改动。但现在不做（YAGNI）。

## Resolution（2026-08-04 闭环，grilling 裁决）

### (a) 修订：ON_RECEIVE 加 per-node 串行门（翻转原「不加 gate」裁决）

**用户裁决理由**：「同一 node 最多同时 1 次活跃调用」是设计哲学第 5 条，应在**触发条件层做成结构不变量**，而非靠 store 的 crash-on-prior-RUNNING 兜底——兜底把「并发」误判为「crash」，语义本来就是拧的（A 还活着却被标 CRASHED，A 回来后 ghost complete 覆盖版本链）。

**同 node 并发调度的唯一触发路径**（事实核查）：

| 组合 | 同 node 并发 | 原因 |
|---|---|---|
| LinearScheduler | 不可能 | 单指针顺序执行 |
| ParallelScheduler + ON_ALL_PREDS | 不可能 | reachability BFS（`_can_reach_active`）门控，含自环 A→A |
| ParallelScheduler + ON_RECEIVE | **唯一路径** | ADR-0034 D4：ON_RECEIVE 从不做 reachability 检查，收到 dispatch 立即 fire |

**Gate 设计**：

- `ParallelScheduler._handle_dispatch` 的 ON_RECEIVE 路径：目标 node 有 RUNNING instance → dispatch 进入该 node 的 FIFO 队列，不立即 fire
- 排水点挂在 instance 完成事件（ADR-0034 D17 Event 3 现有路径）：instance 完成后若队列非空，取下一个 dispatch fire 新 instance
- **ON_RECEIVE 语义保留**：N 次 dispatch 仍触发 N 次执行，从并发变串行——语义不变，并发消除
- ON_ALL_PREDS 路径不动（BFS 已把关）；LinearScheduler 不动

**ON_RECEIVE 标记谨慎使用 + TODO**（用户裁决）：trigger mode 本身保留，但代码注释/文档标记「谨慎使用」，并添加 TODO——ON_RECEIVE 的语义完善（与 recovery 的交互、排队 dispatch 的持久化需求等）留待后续考虑。

### (b) CAS：不变量断言（gate 落地后简化）

所有 lifecycle 转换统一为条件更新：`UPDATE ... WHERE (graph_instance_id, node_name, version) AND status='running'`（suspend 与 orphan 清理再加 `suspended=0`）。Sqlite 查 `rowcount`，InMemory 做等价检查。**终态 `{COMPLETED, CANCELED, CRASHED}` 结构性不可变**——没有任何转换以终态为源状态。

**失败语义分层**：

- **严格**（失配抛 `InvocationStateError`）：`complete_invocation` / `suspend_invocation` / `cancel_invocation`——它们断言「我的 invocation 还活着且归我」，失配 = 不变量被破坏，响亮失败
- **容忍**（幂等 no-op）：`crash_invocation`（把终态记录标 CRASHED 永远安全）、`finalize_invocation` 与 `begin_invocation` 的 orphan 清理（安全网按定义不抛）

`InvocationStateError`：新增，普通 Exception 子类（与 `RoutingError` 同级；它是状态机违例，不是 `GraphBubbleUp` 控制流），落在 `src/modex_graph/exceptions.py`。Null store 不受影响（全 no-op）。

**定位**：gate 使进程内幽灵场景结构性消失，CAS 降级为**不变量断言 + 跨进程防护**（防 modexctl 在 bot 运行时直写 graph.db、防框架 bug）。

### (c) SQLite connection 线程安全：契约文档化

被 [ticket 29](29-coordinator-strategy-injection.md) 的线程契约实质覆盖，无额外机制：

- store 方法是同步方法、只在 event-loop 线程被调用（asyncio 单线程；`ParallelScheduler` 的 `create_task` 也在同一 loop 线程，同步段不交错）
- 连接 caller-owned（业务层拥有），框架侧不存在跨线程共享点，无需锁
- modexctl CLI 用独立短连接（不同进程/连接，无共享）

落地动作仅是**把此契约写入 `NodeStateStore` ABC 文档**。

## Resolution criteria

- ✅ 并发 invocation 串行化设计 — **修订**：ON_RECEIVE per-node 串行门（触发条件层把关）
- ✅ CAS transitions 实现 — WHERE-clause 条件更新 + rowcount 检查
- ✅ 终态不可变性强制 — 结构性保证（无转换以终态为源），终态集合 = `{COMPLETED, CANCELED, CRASHED}`
- ✅ SQLite connection 线程安全策略 — ticket 29 线程契约，文档化到 ABC
- ✅ coordinator 线程安全契约 — 同左
