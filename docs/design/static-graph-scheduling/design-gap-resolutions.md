# Design Gap Resolutions — Static Graph Scheduling

> 2026-08-08: Oracle 高精度 review 发现 7 个设计级差距。本文档记录每个差距的设计决策,已同步到相关设计 ticket (01/04/05/08/11) 和 closure-findings.md。
>
> **实现计划**: `.omo/plans/static-graph-scheduling-impl.md` 已整合所有设计差距决策。

## G1: START/END 调度器语义

**差距**: 设计说 START/END 实例化为 Node,但调度器把 END 当循环退出条件(`linear.py:95`),把 END dispatch 当终止信号(`parallel.py:495`)。

**决策**: 
- START 注册为 Node,`entry_node = GraphNode.START`,StartNode.execute() 读 ctx.user_input deliver 下游
- END 注册为 Node,EndNode.execute() 聚合 delivers → ctx.state.result
- LinearScheduler: `entry_node` 改为 GraphNode.START;循环改 `while True:`,内部 `if current == GraphNode.END: execute; break`
- ParallelScheduler: `_handle_dispatch` 移除 `if target == GraphNode.END: return`;正常 route_deliver 到 END,创建 END instance 执行后终止

**同步到**: ticket 11 §2.2

## G2: GraphSpec/TopologyValidator 验证

**差距**: spec.py 拒绝空 nodes;topology_validator 可能拒绝直接 START→END。

**决策**: 
- GraphSpec.nodes 允许空列表(START/END 由 compiler 自动创建)
- TopologyValidator 允许直接 START→END 边
- compiler.compile() 总是创建 START/END NodeSpec

**同步到**: ticket 04 §12, ticket 11 §2.1

## G3: node_name/node_ID 边界

**差距**: 计划让 ctx.dispatch 传 node_id,但调度器用 name 做 graph.nodes key、验证 edges。

**决策**: **保持 dispatch 传 node_name。转换缝在调度器 dispatch handler:**
1. ctx.dispatch(target_name) — 传 name
2. 调度器验证 target_name 在 edges_from(source_name) 中
3. 调度器转换 target_name → target_node_id via graph.nodes[name].node_id
4. 调度器调 coordinator.route_deliver(target_node_id, ...)

Node._resolve_default_target 返回 name 列表。ctx.dispatch 传 name。route_deliver 接受 node_id。

**同步到**: ticket 01 §G3

## G4: GraphPayload 与 ReAct/built-in 节点兼容

**差距**: Node.deliver(content: Any) → GraphPayload 全局变更破坏所有现有节点。

**决策**: **不改 Node.deliver 签名。GraphPayload 是静态图调度专用。**
- Node.deliver(content: Any, ...) — 签名不变
- IntegratedPayload.content: Any — 不变
- route_deliver(content: Any, ...) — 接受 Any
- GraphPayload 用于:AgentNode(auto-deliver 包装)、StartNode、EndNode
- 创建 DefaultGraphState(state/default_state.py):result: list[GraphPayload] | None = None, frozen=False
- GraphState 基类不加 result 字段(兼容 ReActTurnState.result: AgentResult)
- EndNode.execute() 用 hasattr(content, 'content') 判断 GraphPayload vs 原始 dict
- ReAct EndNode: 移除 vestigial deliver(result, GraphNode.END, ctx)

**同步到**: ticket 11 §5 (G4 修正), ticket 05 (G4 标注), closure-findings H8

## G5: SQLite 连接类型

**差距**: SqliteCoordinatorFactory 需要 sqlite3.Connection(同步),workspace 用 aiosqlite(异步)。

**决策**: **graph 子系统用独立同步 sqlite3.Connection,指向同一数据库文件。**
- _assemble_resources: 从 persistence 获取 db_path,创建 sqlite3.Connection(db_path, check_same_thread=False)
- GraphSpecStore/GraphInstanceStore/SqliteCoordinatorFactory 都用此同步连接
- _stop_resources: graph_connection.close()
- workspace ConnectionManager(aiosqlite)不变

**同步到**: ticket 08 §2

## G6: GraphSpecStore API

**差距**: save 拒绝重复(name,version);list_all() 不返回 spec_id/timestamps;无 update。

**决策**:
- save(spec) → upsert(insert or update by name+version,返回 spec_id)
- list_records() → list[GraphSpecRecord] (新方法)
- get_by_id(spec_id) → GraphSpecRecord | None (新方法)
- GraphSpecRecord: frozen Pydantic, spec_id/name/version/created_at
- InMemory + Sqlite 都实现新 API
- GraphSpecLoader.load_from_dir: save(spec) upsert,启动幂等

**同步到**: ticket 04 §11

## G7: Per-workspace WebUI + WebSocket

**差距**: REST 路由收到单一 orchestrator;ws_broadcaster 不存在。

**决策**:
- REST 路由从请求提取 workspace_id,通过 WorkspaceManager 解析到 PoolWorkspaceResources
- WebUIGraphOutputAdapter: 不用 ws_broadcaster。改为 graph_event_store.append(instance_id, output)
- REST GET /instances/{id}/events 返回事件列表(轮询)
- 后续增强:真正 WebSocket 推送

## 调度统一性 — node/deliver 是唯一调度机制

**原则**: graph 的正常调度、暂停恢复、崩溃恢复,全部依赖同一套 node/deliver 机制。不存在独立的"恢复引擎"或"恢复路径"。

**设计决策**:
- **正常调度**: create_instance → run_instance → engine.run_async → scheduler 逐 node 执行 → node.deliver → route_deliver → 下游 node 被触发 → 直到 END
- **暂停**: pause → 设置 status=PAUSED → node.run 中的 control.check() 检测中断 → 状态持久化到 store (SQLite 策略)
- **恢复 (in-process)**: set status=RUNNING → start_run → run_instance → engine.run_async → 每个 node 在 begin_invocation 前调 load_latest → 从持久化状态恢复 (上次完成的 invocation + 未消费的 delivers) → 继续执行
- **崩溃恢复 (restart)**: recover_crashed → 扫描 CRASHED instances → 对每个: 编译 spec → 从 node_id_map 恢复 node_id → start_run → 同正常调度路径
- **幂等性**: node 自己保证 — load_latest 是幂等的 (读取最新 invocation 状态),deliver 消费是幂等的 (mark_consumed + promote 防重复消费)

**版本链收敛** (2026-08-08 补充):
- node 的 invocation version 链是**连续递增**的,不区分"正常调用"和"恢复调用"
- 正常执行: load_latest(v=1) → begin_invocation(v=2) → execute → complete(v=2) → 下次 load_latest(v=2) → begin_invocation(v=3) → ...
- 恢复执行: load_latest(v=3, 上次 CRASHED) → begin_invocation(v=4) → execute → complete(v=4) — version=4 和正常调用的 v=4 完全等价
- **不做**: 不重置 version 计数器,不创建"恢复版本",不在 version 上标记恢复来源
- scheduler 不区分"正常 node"和"恢复 node" — 它只看到"一个 node,load_latest 后 begin_invocation,execute,submit"

**不做**:
- 不实现独立的 RecoveryEngine — 恢复就是"找到该继续的 node → 加入协程池 → 走正常调度路径"
- 不实现独立的恢复状态机 — node 的 invocation 状态 (PENDING/RUNNING/COMPLETED/CRASHED) 由 node 自己管理, scheduler 只负责触发
- 不为恢复添加特殊调度逻辑 — scheduler 不区分"正常 node"和"恢复 node"

**持久化策略取舍**:
- **InMemory / Null**: 不持久化,进程重启后数据丢失,**无法支持恢复**。这是设计取舍 — 适用于 ReAct 等不需要恢复的场景。
- **SQLite**: 持久化 node_states + deliver_states + graph_instances,**支持暂停/恢复和崩溃恢复**。node 通过 load_latest 从 SQLite 恢复状态。

**归属**: modex_graph (node/deliver 机制) + modex_agent (orchestrator 生命周期管理)

**同步到**: ticket 08 §2 (装配), ticket 11 §6 (WebUIGraphOutputAdapter)
