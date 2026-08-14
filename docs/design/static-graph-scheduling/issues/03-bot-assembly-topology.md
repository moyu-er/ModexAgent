# 03 — Bot 装配拓扑

Status: closed
Labels: wayfinder:resolved
Blocking: 06-modexctl-deliver-command, 07-bot-graph-factory, 08-webui-graph-control-api

## Question

**GraphOrchestrator 在 bot 中怎么部署？这决定 pool/graph 关系和 WebUI 架构。**

### 上下文

- 现有 pool 结构：`config/pools/<name>/pool.yml` + `templates/*.yml`；main agent + subagents（star topology）；`AgentPool` + `MessageBroker` + `AgentMessageBus`
- GraphOrchestrator 已实现（`src/modex_agent/orchestration/graph_orchestrator.py`）：`create_and_run(spec_id)` → 返回 graph_instance_id
- SQLite coordinator factory 已实现（`src/modex_agent/orchestration/sqlite_coordinator_factory.py`）
- RecoveryScanner 示例代码在 `external-control.md:147-176`
- bot 层零 GraphOrchestrator 装配（BL-13）
- WebUI pool 结构：`PoolConfigController` → `/api/pools` CRUD → `PoolEditor.tsx` 前端
- 现有 pool 类型：native（ReAct）+ external（Pi/OpenCode CLI）。加 graph pool 是 C 选项的自然扩展。

## Discussion (2026-08-07)

### 修正：图调度是独立于会话/pool 的系统

**不是 pool 内的 execution strategy，是独立系统。** 之前的"graph 作为 execution strategy"建议太细节且定位错误。

整体架构定位：

- **图调度系统独立运行**——不和 InboxPoller/dispatch_envelope/pool 的 turn 循环耦合。GraphOrchestrator 是独立服务，按自己的调度逻辑驱动 node 执行。
- **node 默认是 AgentNode，引用某个 pool 的 main agent**——不同 node 可以共用同一个 pool 体系。AgentNode 不"拥有"agent，它引用 pool 里已注册的 agent 实例。
- **会话管理是 node 的内部业务逻辑**：
  - 同一 node 反复进入（像 ReAct 循环）→ 复用同一会话（避免记忆丢失）
  - 不同 node → 不同会话（即使共用同一 pool/agent）
- **agent 输出回流到 pool 会话**——便于追踪，用户能在 pool 的会话历史里看到各个 node 的 agent 输出
- **大量依赖 node 内部实现和会话管理**——新建/复用是 node 的业务决策，不是框架强制

### 图示

```
┌─────────────────────────────────────────────────────┐
│              图调度系统（独立）                        │
│                                                     │
│  GraphOrchestrator                                  │
│    ├── AgentNode "research" ──引用──→ pool A main   │
│    ├── AgentNode "coder"   ──引用──→ pool B main   │
│    └── AgentNode "review"  ──引用──→ pool A main   │
│                                                     │
│  node 会话管理（内部业务）：                          │
│    research#0 → 会话 R（首次创建）                    │
│    research#1 → 会话 R（复用，记忆连续）              │
│    coder#0    → 会话 C（不同 node，不同会话）         │
│    review#0   → 会话 V                               │
└─────────────────────────────────────────────────────┘
         │ agent 输出回流
         ▼
┌─────────────────────────────────────────────────────┐
│              pool 会话系统（现有）                     │
│                                                     │
│  pool A: main agent + subagents + session history   │
│  pool B: main agent + subagents + session history   │
│                                                     │
│  会话历史中可看到各个 node 的 agent 输出（追踪）       │
└─────────────────────────────────────────────────────┘
```

### 关键设计问题（已确认方向）

1. **图调度系统怎么触发** — ✅ 已有 graph 配置（JSON/YAML，后续 WebUI 可视化配置），通过 API 显式点击触发，后端构建 GraphInstance 然后调度。
2. **AgentNode 怎么引用 pool 的 agent** — ✅ 直接持有引用。AgentNode 内部持有 pool 等内容，完全可以具备 emitter 以及会话持久化。注意 workspace 多工作区设计。
3. **会话管理具体机制** — ✅ node 内部实现设计。每个 node 有唯一标识（nodeId，opencode 风格短 ID str 生成，详见影响 2.1），同 node 反复进入用同一会话（避免记忆丢失），不同 node 不同会话。
4. **GraphOrchestrator 部署在哪** — ✅ per-workspace，和 pool 实例统一分布，被各个 workspace 持有。orchestrator 也这样做。
5. **agent 输出怎么回流到 pool 会话** — ✅ AgentNode 内部持有 pool 引用，具备 emitter + 会话持久化能力，输出天然回流。

### 探索发现（2026-08-07）

#### AgentNode 当前不回流 WebUI（需重设计）

`agent_node.py:126-128` 自造 `CollectorEmitter`（纯缓冲 sink），覆盖 `agent_ctx.emitter`，跳过 TurnContextBuilder → pool 的 `emitter_factory`（WebBotEmitter factory）永不被咨询。

pool 正常路径天然回流：`PoolRouter → AgentPipeline → ReActTurnRunner → TurnContextBuilder.build_runtime_and_context`（`turn_context_builder.py:539-546`）每个 turn 用 `self._emitter_factory(session.session_id)` 造 WebBotEmitter。

**修正方向**：AgentNode 重设计（ticket 05）时，agent_context_factory 内部调 pool 的 `emitter_factory(session_id)` 造 WebBotEmitter + transcript 持久化，不自造 CollectorEmitter。

#### GraphOrchestrator per-workspace 可行路径

GraphOrchestrator 当前完全未接入 workspace 系统（bot_project 0 引用）。workspace 三层结构：
- identity（WorkspaceContext，轻量永远保留）
- resources（PoolWorkspaceResources，重量懒加载+LRU驱逐）—— `pools: dict[str, PoolInstance]` per-workspace
- handle（WorkspaceHandle + WorkspaceResolverCell 晚绑定）

**可行路径**：per-workspace GraphOrchestrator 实例加入 `PoolWorkspaceResources`，在 `_assemble_resources` 中构造。AgentNodeFactory 用 `WorkspaceResolverCell` 晚绑定引用 workspace 的 pools。拆卸时随 R 一起销毁。

#### nodeId 需要加到 schema

当前 `node_states`（表18）和 `deliver_states`（表19）都以 `node_name` 为键：
- `node_states`: `UNIQUE(graph_instance_id, node_name, version)`，所有索引以 `node_name` 为主
- `deliver_states`: `node_name`（积累方）+ `next_node`（目标方）+ `source_node`（投递方）

**用户决议**：直接调整 `001_initial.sql`，增加 `node_id`（opencode 风格可排序短 ID，str 类型，详见影响 2.1），用于区分不同 node。不随版本变化。

### 待深入

#### node_id schema 改动（已决议，详见影响 2.1）

**全部表加 node_id（TEXT 类型），内部使用 id 而非 name 区分 node。** node_name 保留为人类可读标签但不再是主键。

涉及表（`001_initial.sql`）：
- `node_states`：加 `node_id TEXT NOT NULL`；主键改 `UNIQUE(graph_instance_id, node_id, version)`；索引跟着改
- `deliver_states`：加 `node_id`/`next_node_id`/`source_node_id`（全 `TEXT`，对应现有 `node_name`/`next_node`/`source_node`）；索引跟着改

#### node_id 生成时机（已决议，详见影响 2.1）

**node_id 在 graph instance 创建时生成一次，不重新编译生成。** 关键原因：
- graph instance 可能崩溃/暂停后恢复——恢复时不重新构建 node_id
- node 的真正执行可能远晚于 graph 实例化时间——node_id 必须在 instance 化时就绪

路径：`GraphOrchestrator.create_instance()` 创建 GraphInstance 时生成所有 node_id（opencode 风格短 ID str，`generate_id(prefix="node")`），存到 GraphMetadata.node_id_map。恢复时 `_run_existing_instance` 从持久化数据读回 node_id（M4 恢复），传递给 compiled nodes。`bootstrap(ctx, graph)` 查 store 产生 seed nodes，scheduler 不区分 fresh/recovery。NodeSpec 编译时不生成——graph instance 化时生成。

#### AgentNode 持有结构（已确认方向）

**AgentNode 是 bot_project 的自定义实现类**，不是框架的泛型 AgentNode。持有所需部分（emitter_factory + transcript_store + agent 引用 + session 管理），统一实例化入参（多传一点），node 内部维护所需字段。

在 `node.run` 的可自定义方法中使用——但框架固定的方法（如 run/submit 生命周期）不建议重写。可自定义的方法是 `execute()`（节点业务逻辑）。

具体持有字段（待 ticket 05 细化）：
- `emitter_factory: Callable[[str], ContentEmitter]` — pool 的 WebBotEmitter factory
- `transcript_store` — pool 的 transcript 持久化
- `agent: Agent` — pool 的 agent 实例引用
- session 管理逻辑（同 node 复用会话、不同 node 新建会话）
- `node_id` — 从 graph instance 传递下来

归属：bot_project 自定义 AgentNode 子类。框架层 `AgentNode`（`src/modex_agent/agents/agent_node.py`）是基类参考，bot_project 的实现类持有更多业务字段。

### 连带影响（已记录）

#### 影响 1：node_id 改动是 modex_graph 框架层 breaking change

`NodeStateStore`/`DeliverStore`/`GraphPersistenceCoordinator` 的方法签名从 `(node_name)` 改为 `(node_id)`。`Node.run()` 里所有 `self.name` 调用也要改。跨 6 层 ~15 文件，需要走 ADR。

归属：modex_graph 框架层。属于最初设计缺失——node 身份和 name 未分离。

用户确认：可接受，理解属于最初设计缺失。

#### 影响 2：node_id 映射存储方式（已决议）

**方案 A：graph_instances 表加 JSON 列 + GraphMetadata 加 `node_id_map` 字段。**

node_id_map 有三个特征决定它是 JSON 列而非独立表：
1. **总是整体读写**——创建时生成、恢复时读回，从不独立查询某个 node_id
2. **体量小**——图通常 < 50 个节点
3. **创建后不可变**——node_id 生成后不变

独立表会增加 store ABC + 三套实现 + migration 模板，零查询收益。

**三层存储自动统一支持**（不改 Null/InMemory 的非持久化策略）：

| 实现 | node_id_map 行为 | 和当前一致 |
|------|------------------|-----------|
| `NullGraphInstanceStore` | save no-op，load 返回 None → 无 map，fresh start | ✅ 无法故障恢复，和当前一致 |
| `InMemoryGraphInstanceStore` | 直接存 GraphMetadata 对象 → map 随对象保留 | ✅ 进程内可用，重启丢失，和当前一致 |
| `SqliteGraphInstanceStore` | 显式处理 JSON 列（save/load/_row_to_metadata） | 新增——唯一需改的实现 |

**改动范围**：
- `GraphMetadata`：加 `node_id_map: dict[str, str]` 字段（node_name → node_id）
- `graph_instances` 表：加 `node_id_map_json TEXT NOT NULL DEFAULT '{}'` 列
- `SqliteGraphInstanceStore`：save/load 处理 JSON 列（3 个方法：save、load、_row_to_metadata）
- `GraphOrchestrator.create_instance()`：生成 node_ids，填充 map
- `_run_existing_instance()`：恢复时读回 map，重新赋予 compiled nodes 的 node_id（M4 恢复）

归属：modex_graph 框架层（持久化 schema）+ modex_agent（GraphOrchestrator 生成/传递 node_id）。

用户确认：方案 A。

#### 影响 2.1：node_id 身份设计（已决议）

**Node 实例在 graphInstance 初始化创建时构造并赋予 nodeId，node.run 运行时 CRUD 全程贯穿 nodeId。**

完整路径：
```
GraphOrchestrator.create_and_run():
  1. 编译 GraphSpec → CompiledGraph（Node 实例构造）
  2. 生成 graph_instance_id（Snowflake int，保持不变）
  3. 生成 node_id_map: dict[str, str]（每个 node_name → node_id）
  4. Node 实例被赋予 node_id（instance 创建阶段，方式 2：NodeFactory.create(spec, node_id)）
  5. GraphMetadata(graph_instance_id, ..., node_id_map=node_id_map) → 存 InstanceStore

Node.run(ctx):
  6. begin_invocation(self.node_id, ...) → node_states 行带 node_id
  7. coordinator.collect_consumable_delivers(self.node_id, ...) → 按 node_id 查询
  8. coordinator.mark_delivers_consumed(self.node_id, ...)
  9. coordinator.route_deliver(target_node_id, ...) → deliver_states 行带 node_id
  10. complete_invocation(self.node_id, ...) → 按 node_id 更新

恢复 _run_existing_instance() + bootstrap():
  11. _run_existing_instance: 从 GraphMetadata.node_id_map 读回映射，重新赋予 compiled nodes 的 node_id (M4)
  12. bootstrap: 查 store 产生 seed nodes (非终态 invocation + PENDING delivers)，scheduler 不区分 fresh/recovery
```

**身份分层**：
- `node.name`（str）— 人类可读拓扑标识，边定义用 name，dispatch 校验 target 用 name
- `node.node_id`（str）— 机器标识，持久化 CRUD 用，不随版本变化

**node_id 类型用 `str`**，不用 int。理由：
1. agent 友好——deliver tool 的 target 参数是 agent 传的，str 形式更安全
2. 切换自由——str 不绑定具体生成方法

**node_id 生成方式：opencode 风格可排序短 ID**（调研 `.references/opencode/packages/opencode/src/id/id.ts` + `.references/kimi-code` 后确认）。

格式：`[prefix][separator] + timestamp_hex(12) + base62_random(14)`

三个组成部分（均由调用方可控）：
- **前缀**（可选，默认 `None`）：`None` / 空字符串 `""` / 纯空白 `"  "` 都视为无前缀——此时分隔符也不输出。调用方传前缀时工具内部 strip。
- **分隔符**（可选，默认 `"_"`）：仅当前缀存在时才拼接。调用方可传 `""` 等其他分隔符。
- **ID 主体**：`timestamp_hex(12) + base62_random(14)`，固定 26 字符。

示例值：
```
带前缀：node_a1b2c3d4e5f6KmN8pQr4xYz0   （30 字符，prefix="node"）
自定义分隔符：node-a1b2c3d4e5f6KmN8pQr4xYz0  （prefix="node", separator="-"）
无前缀：a1b2c3d4e5f6KmN8pQr4xYz0         （26 字符，prefix=None）
```

- 时间戳部分：`(current_ms - EPOCH) * 0x1000 + counter`，编码为 6 字节 hex（12 字符），同毫秒 counter 递增防碰撞
- 随机部分：14 字符 base62（os.urandom 取模），~83 位熵
- EPOCH 与现有 Snowflake 一致（2024-01-01 UTC）

**工具方法签名**：`generate_id(prefix: str | None = None, separator: str = "_") -> str`

**node_id 接入**：`generate_id(prefix="node")` → `node_a1b2c3d4e5f6KmN8pQr4xYz0`

**工具方法归属**：`src/modex_agent/utils/id.py`，`generate_id(prefix: str | None = None, separator: str = "_") -> str`。

**graph_instance_id 保持 Snowflake int（BIGINT）**——内部持久化主键，不直接给 agent 用。node_id 用 str 短 ID——agent 可见标识。两者类型不同但语义不同，合理。

**SQLite schema**：`node_id TEXT NOT NULL`（SQLite 无原生 UUID 类型，TEXT 存储是标准做法）。

**Breaking change 范围**（已确认可接受，需 ADR-0036）：
- `Node` 基类：加 `node_id: str` 字段
- `NodeRegistry.create`：收敛注入 `node.node_id = generate_id(prefix="node")`（ticket 05 §3.1 修正:不改 NodeFactory.create ABC 签名）
- `NodeStateStore` / `DeliverStore` / `GraphPersistenceCoordinator`：方法签名 `(node_name: str)` → `(node_id: str)`
- `Node.run()` 内部所有 store 调用从 `self.name` 改为 `self.node_id`
- `Node.deliver` / `Node._submit` / `ctx.dispatch`：target 参数语义从 node_name → node_id
- `Node._resolve_default_target`：`edges_from(self.name)` 返回 name 列表 → 转换为 node_id 列表(局部安全:同一 node 的下游不重名)
- `IntegratedPayload.source_node`：语义从 node_name → node_id(全局可能重名:上游可能来自不同子图)
- START/END 节点的 `register_node` 用 node_id(START/END 也走 NodeRegistry.create,有 node_id)
- SQLite schema：`node_states` / `deliver_states` 加 `node_id TEXT` 列，主键/索引改
- deliver content 类型 `Any` → `GraphPayload`（ticket 11 §5 连带影响）
- `node_name` 保留为人类可读标签，不再是主键

#### 实现优化待办（低优先级，非本期 scope）

**TODO(optimization, low-priority): 统一项目内 UUID 生成方式**

当前项目内 24 处 `uuid.uuid4()` 散落调用，格式不统一（hex 32字符/str 36字符/截短 8或16字符 混用）：
- `trace/hooks.py` ×7、`session_id.py`、`multi_agent/envelope.py` ×2
- `pipeline/turn_runner.py`、`pipeline/pipeline.py`
- `workspace/registry.py` ×2、`workspace/store.py`
- `multi_agent/inbox/types.py`、`agents/external/agent.py`
- `agents/experience/review_agent.py`、`hook/builtin/experience_review.py` ×3
- `messaging/broker_bridge.py`、`messaging/broker_memory.py`

后续逐步收敛到 `utils/id.py` 的 `generate_id()` 入口。这是实现优化，非高优先级。

**TODO(optimization, low-priority): sessionId 格式统一**

当前 sessionId 是 uuid+agentName 形式，后续应改为 agentName+id 形式（如 `main_a1b2c3d4e5f6KmN8pQr4xYz0`），与 node_id 风格一致。这是实现优化，非高优先级。

### 不做什么

- 不把 graph 作为 pool 的 execution strategy——图调度是独立系统
- 不让 graph 系统和 InboxPoller/dispatch_envelope 耦合——node 引用 pool agent 但不走 pool 的 turn 循环
- 不让框架强制会话管理策略——新建/复用是 node 的业务决策
