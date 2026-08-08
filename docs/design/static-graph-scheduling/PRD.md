# Static Graph Scheduling — Wayfinder Map

Status: wayfinder:map

## Destination

一份完整的设计 spec，让静态图调度能端到端接入 ModexAgent：框架层 deliver 语义（execute 内累积 + 结束时统一 dispatch + auto-deliver）+ taskId 共享上下文 + bot_project 装配 + WebUI 后端 API + WebUI 前端可视化配置。当所有设计决策明确、实现路径清晰时，map 完成。

**不是**：动态图拓扑（运行时修改已编译图）、AdaptiveNode（LLM 生成图）、GraphRAG/知识图。这些远期，out of scope。

## Notes

- **域**: modex_graph（框架无关图引擎，已完整）+ modex_agent（AgentNode/GraphOrchestrator/控制面，已存在）+ bot_project（装配/REST/WebUI/CLI，缺口）
- **已有设计**: `docs/design/graph-orchestration/` 有 PRD + external-control.md + distributed-persistence.md + issues 34-39。本次"重新规划完整接入"——已有设计作为输入参考但不绑定。
- **已有代码**: `modex_graph` Phase a+c 完整；`GraphOrchestrator`/`GraphControlService`/`AgentNode` 框架层已实现；`examples/graph_patterns/` 4 个非 ReAct 模式验证；bot 层零装配（BL-13）。
- **Skills**: architecture-patterns, codebase-design, domain-modeling, deliberate
- **规则**: 收敛而非新增并行路径；ABCs before implementations；frozen Pydantic；ADR-0007（两个用例才提升 seam）；modex_graph 不能 import modex_agent（架构守卫）
- **调研**: `.research/graph-engineer/` 有 industry-survey + project-capability-assessment + evaluation

## Decisions so far

- [01 — Deliver 机制](issues/01-deliver-vs-state-boundary.md) — ✅ 核心设计已明确。deliver 是唯一节点间通信通道；三条路径（deliver tool / modexctl deliver / auto-deliver）收敛到 `coordinator.route_deliver`；无 mid-execution（deliver 在 execute() 累积，submit() 统一 dispatch）；ON_ALL_PREDS 是核心 fan-in 模式。
- [02 — taskId 共享上下文](issues/02-taskid-shared-context-mechanism.md) — ✅ 核心设计已明确。agent 不感知 taskId（bool 开关，内部注入）；kb = per-task 持久 KV（get/set upsert）；不做框架 ABC；kb 功能当前不存在，待确认是否有他环境实现。
- [03 — Bot 装配拓扑](issues/03-bot-assembly-topology.md) — ✅ 核心设计已明确。图调度独立于 pool/session；per-workspace GraphOrchestrator；AgentNode 是 bot_project 自定义类持有 pool 引用 + emitter + 会话管理；node_id 用 opencode 风格可排序短 ID（str），node_id_map 存 GraphMetadata JSON 列。
- [04 — GraphSpec 创作方式](issues/04-graphspec-authoring.md) — ✅ 核心设计已明确。YAML 是 source of truth（`config/graphs/<name>/graph.yml`），SQLite 是运行时副本（实例化后不依赖 YAML）；spec_id 是 Snowflake PK，version 自动递增，无 UNIQUE 约束；节点类型注册（agent/function/delay/human_input/graph）；校验三阶段（加载/编译/WebUI 实时）。

## Open tickets

### Frontier（draft 已写,待确认关键决策）

- **05 — AgentNode 重设计**（blocked by 01 ✅, 03 ✅）— ✅ **design closed**。7/7 项全部确认。stop/approval/on_session_end 均不在图调度 scope 内。GraphDeliverTool description 设计追踪 → ticket 06。
- **11 — 图调度输入/输出机制**（新发现 P0 盲区）— ✅ **design closed**。6/6 项全部确认。START/END 可继承节点(modex_graph) + route_deliver 不丢弃 END + GraphPayload 结构体(user_input + deliver content 统一,连带影响 ticket 01) + GraphOutputAdapter ABC + 后台异步执行。breaking change 在 ADR-0036 一起追踪。

### Blocked（等 05/11 确认后推进）

- **06 — GraphDeliverTool**（blocked by 01 ✅, 05 ✅）— ✅ **design closed**。5/5 项全部确认。description 修正(直接描述行为,不提"被跳过") + execute 路径(name→node_id 转换) + GraphPayload 包装 + target_description 来源(AgentDescriptor,后续 GraphSpec 可覆盖)。node_id 全面对齐完成。
- **07 — modexctl deliver 命令**（blocked by 03 ✅, 11 ✅）— ✅ **design closed**。Typer closure + GraphDeliverRequest + POST /api/graphs/instances/{id}/deliver(和 09 共享路由)。底层 orchestrator.deliver_to_node 已就绪。
- **08 — Bot graph factory**（blocked by 03 ✅, 05 ✅, 11 ✅）— ✅ **design closed**。PoolWorkspaceResources 加 graph_orchestrator + output_adapter。_assemble_resources wiring(参考 assemble_sqlite_orchestrator)。1 个待确认(workspace 驱逐时图处理,当前简单处理)。
- **09 — WebUI graph control API**（blocked by 03 ✅, 08 ✅, 04 ✅, 11 ✅）— ✅ **design closed**。REST: GraphSpec CRUD(读+YAML 编辑器写+校验)+ 图控制(run/pause/resume/stop/deliver)+ 状态查询。create_and_run 拆分为 create_instance + run_instance(立即返回 instance_id)。结构体化(frozen Pydantic)。
- **10 — WebUI graph visual config**（blocked by 09 ✅, 04 ✅）— ✅ **design closed**。YAML 编辑器(本期) + 可视化拖拽编辑器(后续)。执行查看器(节点状态 + agentNode 跳转 session + 控制按钮)。纯前端 React,无后端新设计。
- **ADR-0036 — node_id + START/END 实例化 + GraphPayload breaking change**（blocked by 05/11 confirmed）— Node 加 `node_id: str` 字段 + NodeRegistry.create 收敛注入 + store 方法签名 `(node_name)` → `(node_id)` + SQLite schema 加 `node_id TEXT` 列 + START/END 从 sentinel 常量改为 Node 实例(GraphSpecCompiler 始终创建,默认框架基类,GraphSpec 可覆盖) + deliver content `Any` → `GraphPayload`。breaking change,需 ADR。

## Not yet specified

<!-- fog of war — in-scope 但还不够 sharp 到能 ticket -->

### 1. 执行可视化
用户提到"整个执行的可视化至少是要有的，但优先级没那么高"。哪些节点跑了、状态、deliver 流——怎么在 WebUI 呈现？这取决于 WebUI 后端 API 设计（ticket 09）。当前作为 fog，等 09 创建时 graduate。

### 2. Framework vs business 归属（✅ ticket 05 已决议）
ticket 05 已明确归属: BotAgentNode + BotAgentNodeFactory + auto-deliver + IntegratedInput 格式化 → bot_project; GraphDeliverTool + GraphDeliverTargetStore → modex_agent 框架层; PoolInstance.emitter_factory 字段 → modex_agent; Node.node_id + NodeRegistry.create 注入 → modex_graph; generate_id 工具方法 → modex_agent/utils/id.py。

### 3. node_id schema migration 数据回填（探索发现的新 gap）
ticket 03 列了 breaking change 范围但没提已有数据迁移策略。`001_initial.sql` 已存在,加 `node_id TEXT NOT NULL` 列是 `002_*.sql` migration。已有行没有 node_id,怎么回填?**待确认: 当前是否有生产数据**(项目 under active development,可能可以 DROP+重建)。

### 4. AgentNode 并发安全（探索发现的新 gap）
AgentNode 引用 pool 的 main agent 实例。如果 pool 正在跑一个 turn(用户 WebUI 聊天),同时 graph 调度触发该 agent 执行一个 node——两个并发使用同一 agent 实例会串状态吗?`ReActAgent` 本身 stateless(state 在 AgentContext/GraphState),但 `ToolManager`/`ContextManager` 等依赖是否有内部状态? **ticket 05 §3.4 的 fresh-session 策略规避了 single-flight 竞争,但 agent 实例级并发(同 agent 不同 session 同时跑)仍需确认安全**。

### 5. workspace 驱逐时正在运行的图处理（探索发现的新 gap）
LRU 驱逐 workspace 时,pool 会 stop,正在运行的 graph instance 怎么办?强制 stop?等完成?后台继续? **ticket 08 装配时需设计 `_stop_resources` 中的 graph_orchestrator 清理步骤**。

### 6. `_pending_delivers` in-memory 设计问题（后续设计,不纳入当前 scope）
当前 `Node._pending_delivers` 是 in-memory 列表(execute 期间临时累积 delivers,submit 后统一 dispatch)。问题: execute 期间 crash 会丢失 delivers,牺牲了持久化能力。正确方式是状态机——deliver 直接走 store(带"execute 期间"状态),retry 通过查询该状态实现 undelivered detection。当前 `_pending_delivers` 存在的理由是 undelivered detection retry 机制(node.py:257-275)。**后续设计为状态机方案,不纳入当前 ticket scope。当前设计文档中 deliver 的描述应明确区分: 累积是临时 in-memory,持久化在 submit 后走 store。**

### 7. external agent 环境变量继承问题（实现时留注释,不扩大设计）
external agent spawn 时 `ExternalEnvBuilder.build_modex_vars` 设置 `MODEX_TASK_ID` + `MODEX_NODE_ID`(env_builder.py:90-93)。如果图节点 external agent 调用 subagent,subagent 会继承这些环境变量——可能误认为自己有 deliver 能力。正确区分: `MODEX_NODE_ID` 只在图节点 agent spawn 时设置,subagent spawn 时不设 node_id(或设为 None)。**实现时在 ExternalEnvSpec / SubagentExternalBuilder 中留注释: subagent 不继承 node_id。ROI 低,不单独设计。**

### 8. 恢复语义设计待办（closure 检视发现,M4/M7/M9/M10）

| # | 问题 | 位置 | 修复方向 |
|---|------|------|---------|
| M4 | node_id 恢复时重新赋值机制未定义 | 03§196 vs 05§3.1 | NodeRegistry.create 生成新 node_id,恢复需用 GraphMetadata.node_id_map 覆盖。**修复**: 恢复路径在 NodeRegistry.create 后 `node.node_id = persisted_id` 覆盖。 |
| M7 | 无启动恢复扫描 | 08§2 | 重启后 CRASHED 实例在 SQLite 但无人恢复。**修复**: assembly 中加 RecoveryScanner 启动扫描(参考 recovery_scanner.py)。 |
| M9 | node CRASHED → graph CRASHED 转换未定义 | 01§22 | node max_retry 后 RoutingError,图级行为未定义。**修复**: RoutingError 传播到 GraphEngine → graph 级 CRASHED → GraphOutput(CRASHED)。 |
| M10 | CONSUMED_PENDING 恢复语义(非 END) | 01§18 | crash 在 mark_consumed 和 promote 之间 → delivers 卡在 CONSUMED_PENDING。**修复**: 恢复时扫描 CONSUMED_PENDING delivers → re-promote。 |

## 关键代码文件索引

| 文件 | 作用 |
|------|------|
| `src/modex_graph/node.py` | Node ABC — deliver/submit/run 完整生命周期 |
| `src/modex_graph/context.py` | GraphContext — dispatch/emitter/state/coordinator |
| `src/modex_graph/scheduler/parallel.py` | ParallelScheduler — 连续调度 + ON_ALL_PREDS/ON_RECEIVE |
| `src/modex_graph/scheduler/linear.py` | LinearScheduler — 顺序调度 |
| `src/modex_graph/persistence/persistence_coordinator.py` | 交付路由 + 恢复 |
| `src/modex_graph/persistence/graph_metadata.py` | NodeInvocationRecord / InvocationContext / GraphMetadata |
| `src/modex_graph/persistence/node_state_store.py` | 版本链存储 |
| `src/modex_graph/persistence/deliver_store.py` | 交付存储 |
| `src/modex_graph/persistence/instance_store.py` | GraphInstanceStore — Null/InMemory/Sqlite |
| `src/modex_graph/integration.py` | IntegratedInput / IntegratedPayload / InputIntegrator |
| `src/modex_graph/state/state.py` | GraphState — 仅 resume_target + checkpoint |
| `src/modex_graph/spec.py` | GraphSpec / NodeSpec / EdgeSpec |
| `src/modex_graph/spec_compiler.py` | GraphSpecCompiler |
| `src/modex_graph/id_generator.py` | SnowflakeIdGenerator — graph_instance_id 生成 |
| `src/modex_agent/orchestration/graph_orchestrator.py` | GraphOrchestrator — 框架级图编排服务 |
| `src/modex_agent/agents/agent_node.py` | AgentNode + AgentNodeFactory（当前简单 wrapper，需重设计） |
| `src/modex_agent/hook/builtin/subagent_auto_send.py` | SubagentAutoSendHook — auto-deliver 参考模式 |
| `src/modex_agent/multi_agent/tools.py` | TaskDispatchTool + CommunicationTargetStore — deliver tool 参考模式 |
| `src/modex_agent/memory/prompt_pipeline/providers.py` | SystemPromptProvider 体系 — 图上下文注入参考 |
| `src/modex_agent/persistence/migrations/workspace/001_initial.sql` | SQLite schema（node_states/deliver_states/graph_instances 等表） |
| `src/modex_agent/agents/react/state.py` | ReActTurnState — GraphState 的业务子类示例 |
| `examples/bot_project/bot/service/pool/factory.py` | pool 装配流程（create_pool） |
| `examples/bot_project/bot/workspace/handle.py` | PoolWorkspaceResources + WorkspaceHandle + WorkspaceResolverCell |
| `examples/bot_project/bot/workspace/wiring/resources.py` | _assemble_resources — per-workspace 构造 |
| `examples/bot_project/bot/webui/emitter/web_bot.py` | WebBotEmitter — agent 事件 → WebSocket + transcript |
| `src/modex_agent/pipeline/turn_context_builder.py` | TurnContextBuilder — emitter_factory 选择路径 |

## 关键设计约束

- **modex_graph 不能 import modex_agent**（架构守卫测试 `tests/architecture/test_modex_graph_isolation.py`）
- **收敛规则**：不新增并行路径，图调度应融入现有 pool/workspace 体系
- **ABCs before implementations**，zero Protocols
- **frozen Pydantic** for config/value objects
- **ADR-0007**：两个用例才提升 seam
- **不破坏现有 pool 模式**——ReAct agent 必须继续工作
- **非图调度的常规使用不受影响**——deliver tool / 图上下文感知等只在图调度中引入

## 实现顺序建议

1. modex_graph 框架层 node_id 改动（ADR + schema + 6 层签名改 + utils/id.py 工具方法） — 阻塞一切
2. Ticket 03 装配（GraphOrchestrator per-workspace + AgentNode 自定义类）
3. Ticket 01 deliver 机制（deliver tool + auto-deliver + IntegratedInput 格式化）
4. Ticket 02 kb（待确认后接线或从零构建）
5. Ticket 04 GraphSpec 创作（YAML + WebUI）
6. Ticket 07-10（modexctl deliver / WebUI API / WebUI 可视化）

## Out of scope

<!-- ruled beyond the destination -->

- **动态图拓扑（运行时修改已编译图）** — BL-07，远期。静态图调度先完成。
- **AdaptiveNode / LLM 自主生成图** — BL-06，远期。GraphSpec 可序列化 + TopologyValidator 确定性校验是基础，但 LLM 生成不在本期。
- **GraphRAG / 知识图谱** — 不同技术栈。modex_graph 是编排图引擎，不是知识图引擎。如需覆盖，在 modex_agent/memory/ 层做。
- **Postgres 后端** — future-cap §5，远期。SQLite 够用。
- **子图独立 checkpoint + ParentCommand** — future-cap §4，中优先级但不在本期静态图调度 scope。
- **流式事件层（token 级贯穿）** — future-cap §2，中优先级。本期关注 deliver/装配/WebUI，流式可后续。
