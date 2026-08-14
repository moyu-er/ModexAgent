# Design Closure — 发现清单

## 高优先级 (contract gaps — 代码会崩)

| # | Finding | 维度 | 位置 | 后果 | 修复 |
|---|---------|------|------|------|------|
| H1 | auto-deliver content 类型不匹配 | data-flow | 05§3.8 L303 | `_extract_auto_deliver_content` 返回 str,直接传给 `deliver()` 但签名已改为 GraphPayload → TypeError | 包裹 `GraphPayload(content=output)` |
| H2 | _format_integrated_input GraphPayload 不匹配 | data-flow | 05§3.6 L337 | `isinstance(payload.content, str)` 永远 False(GraphPayload 不是 str)→ `json.dumps(GraphPayload)` 会 TypeError → agent 收不到上游输入 | 改为 `payload.content.content` |
| H3 | _format_integrated_input 缺 node_id→name 反查 | data-flow | 05§3.6 vs 01§124 | agent 看到 `Message from node 'node_a1b2c3...'` 而非人类可读 name | 加 graph_ref 反查 |
| H4 | GraphDeliverRequest 两个定义冲突 | convergence | 07§2 vs 09§2 | 07 有 graph_instance_id 字段,09 没有(URL path)→ modexctl 和 WebUI 请求体不一致 | 统一为 09 版本(graph_instance_id 在 URL) |
| H5 | deliver 路由 URL 不一致 | convergence | 07§3 vs 09§1 | 07§3 显示 `/api/control/graphs/{id}/deliver` 但文字说统一走 09 的 `/api/graphs/instances/{id}/deliver` → 自相矛盾 | 统一为 `/api/graphs/instances/{id}/deliver` |
| H6 | AgentDescriptor.description 字段不存在 | code-verify | 06§2 L115 | 设计文档声称 `descriptor.description` 已存在(零新依赖),实际字段是 `role_description` → AttributeError | 改为 `descriptor.role_description` |
| H7 | GraphOrchestrator.get_state 不存在 | code-verify | 09§6 L19/231/280 | 设计文档声称"已就绪",实际 GraphOrchestrator 没有此方法(coordinator 有 get_graph_state) | 添加委托方法或标记 TODO |
| H8 | EndNode 写 ctx.state.result 但 GraphState 无 result 字段 | interface | 11§2 L173 | EndNode.execute 写 `ctx.state.result = results`,但 GraphState 基类只有 resume_target + checkpoint → 写入失败(frozen)或创建临时属性(非 frozen) | GraphState 加 `result` 字段 |
| H9 | compiler.validate 引用但未定义 | interface | 09§3 L143 | WebUI PUT handler 调 `compiler.validate(spec)` 但 GraphSpecCompiler 只定义了 compile() → AttributeError | 添加 validate 方法或用 compile 替代 |
| H10 | GraphSpecLoader 引用但未定义 | interface | 08§2 L89 | 装配代码实例化 GraphSpecLoader 但类从未定义 → YAML 无法加载 | 定义 GraphSpecLoader 类 |

## 中优先级 (lifecycle/state gaps — 资源泄漏或状态不一致)

| # | Finding | 维度 | 位置 | 后果 | 修复 |
|---|---------|------|------|------|------|
| M1 | _active_instances 泄漏 | lifecycle | 09§5 | GraphInstance 完成后从不从 _active_instances 移除 → 内存无界增长 | 终态后移除 |
| M2 | SessionInfo 在 session_registry 累积 | lifecycle | 05§3.4 §233 | 每次图运行创建新 session,永久累积在 pool.session_registry → 无界增长 | 定义驱逐策略 |
| M3 | _stop_resources 无 try/finally | lifecycle | 08§3 | pools stop 异常 → graph cleanup + persistence close 被跳过 → 连接泄漏 | 包 try/finally |
| M4 | node_id 恢复时重新赋值机制未定义 | lifecycle | 03§196 vs 05§3.1 | NodeRegistry.create 生成新 node_id,恢复需要旧 node_id → 持久化数据不匹配 | 指定覆盖机制 |
| M5 | create_instance/run_instance 非原子 | state-machine | 09§5 | create 和 run 之间 crash → 实例 RUNNING 但从未启动 | 加 PENDING 状态 |
| M6 | GraphOutput 发射非原子 | state-machine | 11§6 | 构造和 emit 之间 crash → 图完成但输出未推送 | 在 finally 中 emit |
| M7 | 无启动恢复扫描 | state-machine | 08§2 | 重启后 CRASHED 实例在 SQLite 中但无人恢复 | 加 RecoveryScanner |
| M8 | workspace 驱逐时图处理未确认 | lifecycle | 08§4 | asyncio task 取消但状态不更新 → SQLite 中留 RUNNING | 优雅 pause/stop |
| M9 | node CRASHED → graph CRASHED 转换未定义 | state-machine | 01§22 | node max_retry 后 RoutingError,但图级行为未定义 | 定义转换 |
| M10 | CONSUMED_PENDING 非恢复语义(非 END) | state-machine | 01§18 | crash 在 mark_consumed 和 promote 之间 → delivers 卡在 CONSUMED_PENDING | 恢复时 re-promote |

## 低优先级 (ambiguities — 不崩但不一致)

| # | Finding | 维度 | 位置 | 修复 |
|---|---------|------|------|------|
| L1 | version 类型不一致(TEXT vs int) | data-flow | 04§86 vs 09§75 | 统一为 int |
| L2 | NodeStatusInfo.node_id 来源未定义 | data-flow | 09§6 | 从 GraphMetadata.node_id_map 取 |
| L3 | _current_ctx vs graph_context 命名不一致 | convergence | 06§232 | 统一为 graph_context |
| L4 | /api/graphs/validate 端点在 04 提到但 09 未列出 | convergence | 04§10 vs 09§1 | 添加到 09 或移除 04 提及 |
| L5 | resolve_description hasattr 检查 | convergence | 06§2 | 放 Node ABC 上(默认 [not found]) |
| L6 | deliver tool graph_context None guard 是 shim | convergence | 06§4 | 保留作防御性检查或移除 |
| L7 | state.result 完成后不持久化 | data-flow | 11§2 | 持久化或文档标注为内存 |
| L8 | user_input create→run 传输未定义 | data-flow | 09§5 | 存入 GraphInstance |

## 设计原则: 调度统一性

> **原则**: graph 的正常调度、暂停恢复、崩溃恢复,全部依赖同一套 node/deliver 机制。不存在独立的"恢复引擎"或"恢复路径"。恢复 = "找到该继续的 node → 加入协程池 → 走正常调度路径"。详见 `src/modex_graph/AGENTS.md`。

**版本链收敛**: node 的 invocation version 连续递增,不区分正常调用和恢复调用。恢复时 load_latest 读取上次 version,begin_invocation 创建下一 version,与正常连续调用完全等价。不重置、不标记恢复来源、不创建恢复版本。

**持久化策略取舍**:
- **InMemory / Null**: 不持久化,无法支持恢复。适用于 ReAct 等不需要恢复的场景。
- **SQLite**: 持久化 node_states + deliver_states,支持暂停/恢复和崩溃恢复。node 通过 load_latest 从 SQLite 幂等恢复。
