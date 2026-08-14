# Graph Scheduling — 后续待办与设计遗留

> 统一登记:图调度子系统的未修复问题、设计决策需求、已知限制、后续增强。
>
> **来源整合**: review-residual-items.md + .divergence-index.md + closure-findings.md + PRD.md "Not yet specified" + 各 issue 文件 "后续增强" / "待确认" 项。
>
> 已修复项不在此列(4 项 Critical/Important 已修复:GraphInterrupt 吞没、resume bypass、persistence teardown、spec 目录不一致;14 项 divergence 已收敛)。

## 优先级定义

| 级别 | 含义 | 判定标准 |
|------|------|----------|
| **P0 极高** | 不修复会导致功能不可用或数据损坏 | 影响核心操作路径(运行/暂停/恢复/停止),用户可观测到功能失效 |
| **P1 高** | 不修复会导致功能退化或边界条件不可靠 | 特定场景下行为不正确,但不影响主流程;长期运行有累积风险 |
| **P2 中** | 不修复不影响功能,但违反规范或增加技术债 | 类型安全/架构规范/代码质量层面;当前行为正确但不合规 |
| **P3 低** | 纯清理项,无功能/规范影响 | 代码整洁度、命名一致性、便利函数收敛等 |
| **P4 远期** | 明确标注为 out-of-scope 的后续增强 | 本期设计已确认不做,记录待未来迭代 |

---

## P0 — 极高优先级(功能直接影响)

### P0-6. Graph 调度中的 subagent 通信走 inbox 机制,缺少 graph 专用配置

**位置**: `src/modex_agent/hook/builtin/subagent_auto_send.py` (发送), `src/modex_agent/hook/builtin/inbox_flush.py` (消费), `examples/bot_project/bot/graph/agent_node.py` (graph-native 路径)

**问题**: 当前存在两套并行的 agent 通信机制,形成架构断层:

- **路径 A (inbox 机制)**: `SubagentAutoSendHook.finally_graph()` → `agent_bus.send(parent_session_id, envelope)` → `InboxMQ` 持久化 → `InboxPoller` 唤醒 → parent 下一轮 turn → `InboxFlushHook.start_node_turn()` → `consume()` → `ctx.history.append()` (作为普通 SYSTEM_REMINDER)
- **路径 B (graph-native deliver)**: `BotAgentNode` 内 agent 调用 `GraphDeliverTool` → `Node.deliver(GraphPayload, target_node, ctx)` → `Node._pending_delivers` 累积 → `submit()` → 下游节点 `execute()` → `_format_integrated_input()` 注入

**断层**: graph 调度中的 agent 通过 `send_to_agent` tool 或 `SubagentAutoSendHook` 发送消息时,走的是路径 A (pool inbox 机制)。但消费方 agent 收到消息后,通过 `InboxFlushHook` 将消息作为普通 `AGENT_MESSAGE` 注入 history —— **缺少 graph 专用配置**:

1. 没有 `GraphDeliverTool` — 消费方 agent 无法向 graph 下游节点 deliver
2. 没有 `graph_context` — agent 不知道自己在 graph 调度上下文中
3. 没有 graph topology context — agent 不知道上下游节点角色
4. 没有 `MAX_TURNS` 设置 — graph 调度中的 turn 限制不生效
5. 没有 approval disable — graph 节点中的工具审批会死锁(无用户审批)

**根因**: `AgentMessageEnvelope` 不携带 graph 调度上下文信息(`graph_instance_id`, `source_node_id`, `graph_spec_id`)。消费方无法区分"这是 graph 调度产生的消息"和"这是普通 agent 间消息"。`BotAgentNode.execute()` 中完整的 graph agent 环境配置逻辑(deliver tool, topology, approval disable, MAX_TURNS)耦合在 BotAgentNode 内部,不是可复用的独立模块。

**影响**: graph 调度中跨 pool/跨 agent 的通信回退为普通会话消息,消费方 agent 缺少 graph 上下文,无法正确执行 graph 工作流语义(deliver 到下游节点、理解拓扑位置等)。当前 graph 内 agent 间通信仅通过路径 B (deliver) 工作;路径 A (inbox) 产生的消息无法激活 graph agent 环境。

**修复方向** (3 层设计):

1. **通信数据扩展**: 在 `AgentMessageEnvelope.metadata` 中添加 graph 调度字段:
   - `graph_instance_id: int` — 来源 graph 实例
   - `graph_spec_id: int` — graph spec ID
   - `source_node_id: str` — 发送方在 graph 中的节点 ID
   - `graph_aware: bool` — 标记这是 graph 调度产生的消息
   
   `SubagentAutoSendHook` 或 `BotAgentNode` 在发送消息时填充这些字段。

2. **消费方检测 + 分流**: 新增 `GraphInboxDispatchHook(StartNodeTurnHook)` (或扩展 `InboxFlushHook`):
   - 检测 inbox 消息的 `graph_aware` metadata 字段
   - graph-aware 消息: 走 graph agent 路径 (配置 graph 专用 tool/context)
   - 普通消息: 走常规 inbox flush 路径 (不变)
   - **收敛规则**: 不是新增第三条并行路径,而是检测到 graph-aware 消息时**替换**常规 flush 行为

3. **graph agent 环境重建**: 从 `BotAgentNode.execute()` 提取可复用的 `GraphAgentContextConfigurator`:
   - 注入 `GraphDeliverTool` (基于 `GraphEngineController.deliver_to_node` 的 deliver 回路)
   - 设置 `agent_context.graph_context` (轻量 proxy,只重建 deliver 回路,不重建完整 graph state)
   - disable approval (graph 节点中的工具审批会死锁)
   - 设置 `MAX_TURNS` (graph 调度的 turn 限制)
   - 注入 topology context (上下游节点角色)

**deliver 回路设计**: 消费方 agent 的 `GraphDeliverTool` 需要回到原 graph 的 deliver store。通过 `GraphEngineController.deliver_to_node(node_name, content)` 传递 — controller 持有 live graph 引用。如果 graph 已结束/paused,降级为普通 inbox 消息 (fallback)。

**风险**:
- `GraphContext` 重建复杂 — 需要轻量 proxy 方案,只重建 deliver 回路
- 生命周期管理 — graph 实例可能已结束,deliver 无处可去 (需状态检查 + 降级)
- 与现有 inbox 机制收敛 — 不能引入第三条路径 (收敛规则 15)

**前置工作**:
- 先写 ADR 记录设计决策 (graph-aware 通信机制)
- 从 `BotAgentNode` 提取 `GraphAgentContextConfigurator` 作为第一步 (纯重构,不改行为)
- 再添加 metadata 扩展 + dispatch hook

**工作量**: 高 — 涉及通信协议扩展 + 新 hook + agent context 配置模块提取 + deliver 回路设计

**状态**: 🔴 未开始 — 设计待确认

---

### P0-1. `run_instance` setup 失败导致实例永久卡 RUNNING — ✅ FIXED

**位置**: `src/modex_agent/orchestration/graph_orchestrator.py:262-281`

**问题**: spec reload、state creation、engine/context 构造、controller 注册发生在 `try` 块之前。任一失败会绕过 crash 持久化、output emission、controller cleanup、terminal eviction → 实例永久 RUNNING,无法恢复。

**修复**: setup 移入 `try` 块;失败走 `except Exception` → 写 CRASHED + 创建 GraphOutput + finally 走 `_finalize_instance`。

---

### P0-2. Output-adapter 失败掩盖图异常,COMPLETED → CRASHED — ✅ FIXED

**位置**: `src/modex_agent/orchestration/graph_orchestrator.py:310-314`

**问题**: `_evict_if_terminal` 在 `await emit` 之后执行。如果 output adapter 抛异常,已 completed/crashed 的实例不会被 evict。

**修复**: `emit` 包 `try/except` 在 `_finalize_instance` 中,失败只记日志;`_evict_if_terminal` 始终执行。

---

### P0-3. Stopping a paused graph 泄漏 active instance — ✅ FIXED

**位置**: `src/modex_agent/control/graph_control.py:218-229`

**问题**: paused run 已经离开 `run_instance`。`stop` 修改 persisted status 为 STOPPED,但没有 finalizer 触发 eviction。

**修复**: `_stop` 无 engine 时调 `_finalize_instance(gid, STOPPED)` — 走与 `run_instance.finally` 相同的 finalization 路径。

---

### P0-4. 生命周期状态转换无集中守卫 — ✅ FIXED

**位置**: `src/modex_agent/orchestration/graph_orchestrator.py:234-262`, `src/modex_agent/control/graph_control.py:195-229`

**问题**: `run_instance` 无条件写 RUNNING — 重复 `start_run` 调用可以并发执行同一 coordinator。

**修复**: `_running_gids: set[int]` guard — `run_instance` 开始时检查并 add,`finally` 中 discard。

---

### P0-5. External delivery 不强制生命周期状态 — ✅ FIXED

**位置**: `src/modex_agent/control/graph_control.py:239-272`

**问题**: `_coordinator_lookup` 对所有 active instance 成功,包括 PENDING。

**修复**: `_deliver` 在 load metadata 后检查 `status in {RUNNING, PAUSED}`,否则 raise ValueError。

---

## P1 — 高优先级(功能退化/边界不可靠)

### P1-1. Graph routes workspace 参数不一致 — ✅ FIXED

`?workspace=` → `?ws=`（graph_routes.py + modexctl deliver CLI 同步修改）。

---

### P1-2. BotAgentNode 绕过 turn lifecycle — ✅ FIXED

`agent.run` → `ReActTurnRunner.execute_turn`（收敛 turn lifecycle: register_task/set_turn_uuid/ctx_mgr.save/unregister_turn/_safe_flush/on_session_end）。approval per-turn 禁用（`AgentRuntimeServices.approval = None`，不改 pool 级别，正常会话不受影响）。

---

### P1-3. Resume after bot restart 缺 evicted 场景测试 — ✅ FIXED

测试验证: evicted resume 通过 `start_resume` → recovery 路径正确重建 instance。R9 修复已覆盖此场景。

---

### P1-4. workspace 驱逐时正在运行的图处理未优雅停止 — ✅ FIXED

`run_instance` 添加 `except asyncio.CancelledError` → 写 STOPPED + re-raise。`finally` 中 `_finalize_instance(STOPPED)` → evict。

---

### P1-5. SessionInfo 在 session_registry 无界累积 — ✅ FIXED

`SessionRegistry` ABC + `InMemorySessionRegistry` 添加 `cleanup(session_id)` 方法（移除 cache + store）。`BotAgentNode.execute` try/finally 包裹，PER_INVOCATION 策略下执行后清理 session。

---

## P2 — 中优先级(规范违反/技术债)

### P2-1. `EndNode.execute` isinstance 分派

**位置**: `src/modex_graph/nodes/end_node.py:37,40`

**问题**: `EndNode` 用 `isinstance(ctx.state, DefaultGraphState)` 和 `isinstance(payload.content, GraphPayload)` 判断结果聚合方式。违反类型安全规则 9(扩展边界外不许 isinstance)。

**根因**: `IntegratedPayload.content: Any` — 故意开放以支持框架扩展。EndNode 需知道 content 是否为 `GraphPayload`(静态图 deliver)以提取 `.content`。

**当前影响**: 无 — bot_project 用 `DefaultGraphState` + `GraphPayload`,isinstance 检查通过。

**修复选项**(需设计决策):
- (a) 在 `GraphState` ABC 上加 `get_result()` 方法,EndNode 多态调用(最干净,但改 ABC 表面)
- (b) 始终用 `GraphPayload` 包装 deliver content(breaking change for custom Node)
- (c) 保持现状(isinstance at real boundary,有 `# ruff: noqa: ANN401` 显式 opt-out)

**工作量**: 中(选 a) / 低(选 c 保持现状)。

---

### P2-2. `deliver_store.py` 手写 JSON 序列化

**位置**: `src/modex_graph/persistence/deliver_store.py:46-81`

**问题**: `_encode_content`/`_decode_content` 用手写 `json.dumps`/`json.loads` + 自定义 `{"__pydantic__": True, "class": ..., "data": ...}` envelope。违反规则 13(序列化用 Pydantic `model_dump`/`model_validate`)。字符串类型分派(`if cls_name == "GraphPayload"`)脆弱。

**当前影响**: 无 — 序列化/反序列化逻辑正确,数据读写正常。

**修复选项**(需设计决策):
- (a) `DiscriminatedUnion` envelope model with type discriminator
- (b) `TypeAdapter[BaseModel]` 序列化(如果所有 content 都是 BaseModel)
- (c) 限制 content 为 `GraphPayload` only(breaking change for custom nodes)

**工作量**: 中。

---

### P2-3. `_make_command` loose dict payload

**位置**: `src/modex_agent/orchestration/graph_orchestrator.py:559`, `src/modex_agent/control/graph_control.py:242`

**问题**: control commands(pause/stop/resume/deliver)用 `dict[str, object]` 而非 typed Pydantic model。`graph_control.py:242` 用 `isinstance(node_name, str)` 验证 — loose dict 的症状。

**当前影响**: 无 — control 命令功能正常,isinstance 验证有效。

**修复方向**: 定义 `GraphDeliverPayload(BaseModel)` + `GraphControlPayload(BaseModel)` discriminated union,替换整个 control command 链。

**工作量**: 高 — 影响 8+ 文件,~20 call sites。

---

### P2-4. `GraphEventItem` 用 `extra="allow"` 而非 `extra="forbid"`

**位置**: `examples/bot_project/bot/webui/routes/graph_models.py:126`

**问题**: 违反模块约定(所有 model `extra="forbid"`)。`kind: str` 而非 `GraphOutputKind` enum。

**当前影响**: 无 — 功能正常,只是模型约束不严格。

**修复方向**: 声明已知字段 + `extra="forbid"`,forward-compatibility 用 `data: dict[str, Any]` 字段。

**工作量**: 低 — 需验证所有 `GraphOutput` 变体确保字段完整。

---

### P2-5. `graph_orchestrator.py` state extraction 绕过 Pydantic

**位置**: `src/modex_agent/orchestration/graph_orchestrator.py:286`

**问题**: `dict(final_state).get("result")` 应该用 `final_state.model_dump(...).get("result")` 或声明的 result-bearing state type。

**当前影响**: 无 — 功能正确。

**工作量**: 低。

---

### P2-6. `GraphAsNodeConfig.graph_spec` 接受 dict

**位置**: `src/modex_graph/nodes/graph_as_node.py:129`

**问题**: `isinstance(config.graph_spec, dict)` — 如果字段类型为 `GraphSpec`,Pydantic 会自动 coerce。规则 9/12 联合违反。

**当前影响**: 无 — 当前用法下 Pydantic 正确处理。

**工作量**: 低 — 改字段类型为 `GraphSpec`,移除 isinstance。

---

### P2-7. `graph_routes.py` 依赖 orchestrator 私有属性

**位置**: `examples/bot_project/bot/webui/routes/graph_routes.py` (多处)

**问题**: 直接访问 `_spec_store`, `_instance_store`, `_compiler` 使 example 依赖实现细节。

**当前影响**: 无 — 功能正常。

**修复方向**: 在 orchestrator 上暴露窄公开方法。

**工作量**: 中 — 影响多个 route handler。

---

### P2-8. Background task 异常丢弃无日志

**位置**: `src/modex_agent/orchestration/graph_orchestrator.py:340-355`

**问题**: REST-started task 失败产生 "Task exception was never retrieved" 但无上下文日志。

**当前影响**: 无功能影响 — 只是日志缺失,调试困难。

**工作量**: 低 — 在 completion callback 中 consume + log task 异常。

---

### P2-9. `_pending_delivers` in-memory 设计(execute 期间 crash 丢失 delivers)

**位置**: `src/modex_graph/node.py` _pending_delivers

**问题**: execute 期间 crash 会丢失 in-memory 累积的 delivers。当前 undelivered detection retry 机制依赖 `_pending_delivers` 状态查询。正确方式是状态机——deliver 直接走 store(带"execute 期间"状态)。

**当前影响**: 低 — crash 恢复时丢失未提交的 delivers,但 node 会重新执行(CRASHED → re-execute),重新 execute 会重新 deliver。

**修复方向**: 后续设计为状态机方案,deliver 直接走 store 带"execute 期间"状态。

**工作量**: 高 — 涉及 Node.run 生命周期和 deliver store schema 变更。

---

### P2-10. node_id schema migration 数据回填策略未定义

**位置**: `src/modex_agent/persistence/migrations/workspace/`

**问题**: `001_initial.sql` 已存在,加 `node_id TEXT NOT NULL` 列的 migration 缺少已有数据回填策略。

**当前影响**: 无 — 项目 under active development,可能可以 DROP+重建。但如有生产数据需回填。

**修复方向**: 确认是否有生产数据;如无,DROP+重建;如有,编写 `002_*.sql` migration 回填。

**工作量**: 低(确认无生产数据) / 中(需回填)。

---

## P3 — 低优先级(代码整洁/收敛)

### P3-1. `node_state_store.py` 用 `json.dumps({})` 空状态占位

**位置**: `src/modex_graph/persistence/node_state_store.py`

**问题**: 空状态用手写 `json.dumps({})`。可用 `"{}"` 常量替代。

**工作量**: 极低。

---

### P3-2. `create_null_coordinator` vs `NullCoordinatorFactory.create`

**位置**: `src/modex_graph/persistence/persistence_coordinator.py:296,361`

**问题**: 两条 Null coordinator 构造路径。便利函数可委托给 factory。

**工作量**: 极低。

---

### P3-3. `_submit_result` 死框架状态(test-only seam)

**位置**: `src/modex_graph/node.py:92,198,429`

**问题**: 60+ test 断言读取它。docstring 已修正为准确描述其为 test-observation seam。移除需更新 7 个 test 文件。

**工作量**: 中(主要是 test 更新)— 收益低,可延后。

---

### P3-4. `SchedulerInstanceStatus` 重复枚举

**位置**: `src/modex_graph/` (closure-findings C5)

**问题**: `SchedulerInstanceStatus` 和 `NodeInstanceStatus` 重复。

**工作量**: 低 — 合并为一个枚举。

---

### P3-5. `deliver()` 的 ctx 参数 vestigial

**位置**: `src/modex_graph/node.py`

**问题**: `_deliver` 不用 ctx,旧设计残留。

**工作量**: 低 — 但影响 Node ABC 签名,需检查所有子类。

---

### P3-6. 统一项目内 ID 生成方式

**位置**: `src/modex_graph/utils/id.py` vs `src/modex_graph/id_generator.py`

**问题**: 项目内有 `generate_id()` (opencode 风格短 ID) 和 `SnowflakeIdGenerator` (64-bit int) 两套 ID 生成。ticket 03 标注为低优先级优化项。

**工作量**: 低 — 但需确认两套 ID 的不同用途(node_id 用 str, graph_instance_id 用 int)是否合理。

---

### P3-7. sessionId 格式统一

**位置**: `examples/bot_project/bot/graph/agent_node.py`

**问题**: 当前 sessionId 用 `uuid+agentName` 格式,应改为 `agentName+id` 格式(如 `main_a1b2c3d4e5f6`),与 node_id 格式一致。ticket 03 标注为低优先级优化项。

**工作量**: 低。

---

## P4 — 远期增强(本期明确 out-of-scope)

以下项在设计阶段已明确标注为"本期不做,后续增强",记录于此供未来迭代参考。

### P4-1. GraphContextSystemPromptProvider(图上下文 system prompt 注入)

**来源**: ticket 05 §3.6

**描述**: 让 agent 理解图调度上下文(当前节点位置、上下游角色、这是图输入不是用户对话)。参考 `AgentCommunicationSystemPromptProvider` 复合 provider 模式。本期保留占位,不做。

### P4-2. WebUI 可视化拖拽编辑器(模式 B)

**来源**: ticket 10

**描述**: 拖拽点/边的可视化编辑器。本期只做 YAML 编辑器(模式 A),模式 B 是前端增强,共享保存 API。

### P4-3. 图执行实时拓扑高亮

**来源**: ticket 10

**描述**: 节点状态变色(运行中/完成/失败)。依赖 WebSocket 推送节点状态变更事件。

### P4-4. GraphSpec POST 创建 / DELETE 删除 REST 端点

**来源**: ticket 09

**描述**: 当前以 YAML 文件管理为主。POST/DELETE 端点为后续增强。

### P4-5. GraphSpec 在线热更新

**来源**: ticket 08

**描述**: 启动时加载,运行时不热更新。热更新为后续增强。

### P4-6. GraphOutputKind 扩展(PAUSED/RESUMED 事件类型)

**来源**: ticket 11

**描述**: 当前只有 `COMPLETED` + `CRASHED`。后续扩展 PAUSED/RESUMED 事件类型 + output adapter 的 progress/node_states 字段。

### P4-7. 真正 WebSocket 推送(替代事件轮询)

**来源**: design-gap-resolutions G7

**描述**: 当前 WebUIGraphOutputAdapter 用内存 event store + REST 轮询。后续增强为 WebSocket 实时推送。

### P4-8. GraphSpec node 级 description 覆盖

**来源**: ticket 06

**描述**: 同一 agent 在不同图中角色不同时,用 GraphSpec `config.description` 覆盖 target_description。后续增强。

### P4-9. auto-deliver 结构化信封

**来源**: ticket 05, ticket 01

**描述**: 当前 auto-deliver 是纯文本提取。后续可参考 `SubagentAutoSendHook` 式结构化信封(ResultMeta 头 + 格式化 body)。

### P4-10. kb(task 共享知识库)— 完整功能未实现

**来源**: ticket 02 (design closed,实现未开始)

**设计决策**(已在 ticket 02 中确认):
- **定位**: deliver = 即时定向节点间数据流;kb = 持久化共享,任意节点随时读写,task 私有
- **taskId = str(graph_instance_id)** — agent 不感知 taskId 值,内部从 `MODEX_TASK_ID` env 或 graph context 自动注入
- **存什么**: ① 任务知识(代码库结构/约束、任务分解、项目上下文) ② 中间产物索引(文件路径、测试结果摘要)
- **API**: 两 action,upsert 语义 — `get(key)` 读,`set(key, value)` 写(insert+update 合并)。不做 delete,不做 insert/update 分离
- **不做框架 ABC**: kb 是 bot 业务功能,不引入 `TaskKvStore` ABC 到 modex_agent。后续如需替换持久化后端,在 bot 层用 ABC 隔离
- **不做向量检索/embedding/RAG**: 本期是结构化 KV

**当前状态**: 完全未实现。探索确认:不在当前分支、不在 git 历史、不在任何分支。现有"knowledge"只是 memory 系统的 Core Memory 层(agent 级 in-context 记忆),不是 task 级共享 KV。从零构建。

**已有可复用基础设施**:
- `ExternalEnvSpec.task_id` 字段(`src/modex_agent/agents/external/types.py:174`)— 已定义,默认 None
- `MODEX_TASK_ID` env 注入(`env_builder.py:90-91`)— 已实现,但 task_id 永远 None(待接线)
- `graph_instance_id`(Snowflake ID)— taskId 的值来源
- `DeliverStore` 三档模式(Null/InMemory/Sqlite)— 可作 kb 持久化设计参考

**需实现的组件**:

| 组件 | 归属 | 说明 |
|------|------|------|
| kb 持久化 | bot_project | per-task 分区 KV(SQLite 默认,可替换)。轻量版先建 get/set,后续替换持久化后端 |
| kb tool(`task_kb` tool) | bot_project | agent 调用的 tool,action=get/set。内部从 graph context 拿 taskId,agent 不传 taskId |
| `modexctl kb --by-task` CLI | bot_project | 外部 agent 用,bool 开关,taskId 从 `MODEX_TASK_ID` env 读 |
| taskId 注入接线 | modex_agent | AgentNode context_factory 从 GraphContext 拿 graph_instance_id → 设置 `ExternalEnvSpec.task_id` → env 注入。基础设施已有,当前永远 None,需接线 |

**待确认**: kb 功能是否已在其他环境实现(用户可能在另一台环境有实现,git 历史未合入)。后续换环境查找。如果没有再从零实现。

**不阻塞图调度**: kb 是 bot 业务层功能,框架层设计(deliver/taskId 注入/AgentNode)不依赖 kb 的具体实现。图调度工作流(worker → reviewer → worker 环)在无 kb 的情况下可通过 deliver 通信完全运作。kb 是增强项,非必需项。

**工作量**: 中 — 持久化层 + tool + CLI + taskId 接线,4 个组件从零构建。

### P4-11. 动态图拓扑 / AdaptiveNode / GraphRAG

**来源**: PRD.md out-of-scope

**描述**: 运行时修改已编译图、LLM 自主生成图、知识图谱。明确远期,不在本期 scope。

### P4-12. Postgres 后端 / 子图独立 checkpoint / 流式事件层

**来源**: PRD.md future-cap

**描述**: Postgres 后端、子图独立 checkpoint + ParentCommand、token 级流式事件层。均为远期增强。

---

## 已知合理分歧(不需要修复)

以下分歧服务于不同的执行模型,是设计意图,不是 bug:

| 分歧 | 原因 |
|------|------|
| PENDING deliver 发现:bootstrap seed 派生 vs `_recheck_pending` instance admission | 不同消费者:LinearScheduler seed list vs ParallelScheduler instance creation |
| promote_delivers 两个调用点:bootstrap(crash recovery) vs Node.run(normal completion) | 正常完成 vs crash-between-mark_consumed-and-promote 恢复窗口 |
| 两个 deliver store:in-memory `_pending_delivers`(pre-resolution buffer) vs persisted `deliver_store`(post-resolution durable) | 不同 pipeline 阶段,不同生命周期 |
| GraphInstance 构造:create_instance(compiled+user_input) vs recovery(metadata+coordinator) | 初始创建 vs 恢复(经 bootstrap 重建状态) |
| query_consumable 语义差异:Null(all) vs InMemory(PENDING only) vs Sqlite(PENDING+CONSUMED_PENDING) | 不同状态机:策略 tradeoff |
| Graph DB 连接:sync sqlite3(graph) vs async aiosqlite(workspace) | modex_graph 是独立 sync 包 |
| `_recover_instances` CRASHED write 看似冗余 | 覆盖 `run_instance` 之前的失败窗口(如 `_load_spec` 失败),防御性 |

---

## 已修复项(存档)

| # | 问题 | 修复内容 | 日期 |
|---|------|---------|------|
| P0-1~5 | 5 项生命周期硬化 | `_finalize_instance` 统一 finalization + setup 移入 try + `_running_gids` guard + `_stop` 无 engine 走 finalize + `_deliver` status 检查 | 2026-08-08 |
| P1-1~5 | 5 项功能退化修复 | `?ws=` 参数收敛 + `execute_turn` turn lifecycle 收敛 + approval per-turn 禁用 + evicted resume 测试 + CancelledError→STOPPED + SessionRegistry.cleanup | 2026-08-08 |
| — | GraphInterrupt 被 `except Exception` 吞没 | `graph_recovery.py` 添加 `except GraphInterrupt: raise` | 2026-08-08 |
| R9 | resume bypass recovery path | 新增 `start_resume()`,route 改调 `start_resume` | 2026-08-08 |
| R10 | persistence teardown 无条件关闭 | `owns_persistence` + `pools_ok` 双门控 | 2026-08-08 |
| R12 | spec 目录不一致 | per-workspace `ctx.target/config/graphs` + 全局模板首次复制 | 2026-08-08 |
| A1-A17 | 14 项 divergence 收敛 | `_dispatch_utils` helper / `FrameworkPayloadSource` enum / `_integrate_upstream` helper / bootstrap single-scan / enum .value / typed event model 等 | 2026-08-08 |

---

## 文档整合说明

本文档整合了以下来源:
- `review-residual-items.md`(已删除) — 20 项 code review 发现
- `.divergence-index.md`(已删除) — 9 项 Flag for Refactor + 15 项 Justified divergence
- `closure-findings.md` — H/M/L 级发现中未修复的清理项(C3/C5/L1-L8 中仍适用的)
- `PRD.md` "Not yet specified" — 8 项 fog-of-war(已按优先级归入 P1-P4)
- 各 issue 文件(01-11)中标注"后续增强"/"待确认"/"不做"的散落项
