# 图引擎演进方向（Future Capabilities）

Status: **directional**（设计方向记录，非当前实现状态。已定稿即将实现的见 `external-control.md` + `issues/34`~`39`；本文件记录下一波值得设计的引擎能力演进。）
Date: 2026-08-05

本文件是图引擎核心能力的中期演进路线。每项均基于现有架构的自然延伸——数据或钩子已就绪，缺的是公开 API 或装配。目的：在 `issues/34`~`39` 落地后，明确下一批值得排期设计的能力方向，避免散落在 backlog 里失去整体视野。

与 `backlog.md` 的区别：backlog 记录"历史提到但未做"的全部事项（含低优先级与业务侧接线）；本文件只收录**引擎核心层值得主动设计的演进**，且有足够架构基础可承接。

## 1. 时间旅行：版本链状态查询与定点恢复

### 现有基础

`node_states` 表每行存一条 `NodeInvocationRecord`，含 `version` / `parent_version` / `state_json`（full snapshot） / `status` / `suspended`。版本链完整保留了图运行过程中每个节点每次调用的全量状态快照。`GraphPersistenceCoordinator.rebuild_main_state()` 目前只取"全局最新"重建主状态——但它遍历的就是这条版本链，**定点重建任意历史版本的数据已全部在表里**。

### 缺口

无公开 API 暴露版本链的查询与定点恢复能力。当前只能 `get_graph_state()`（最新快照），不能：
- 查某节点某版本的状态快照；
- 从指定历史版本重建主状态（而非最新）；
- 从指定版本 fork 恢复执行（而非从最新恢复）。

### 设计方向

在 `GraphPersistenceCoordinator` / `GraphRecoveryService` 上补三个公开方法：

1. **`get_state_history(graph_instance_id, node_name=None, before_version=None, limit=None)`**：遍历版本链返回状态快照列表。数据来源：`NodeStateStore.query_versions`（已存在，支持 `status_filter`，扩展支持 `before_version`）。可用于审计、回放、调试。
2. **`rebuild_main_state(from_versions: dict[str, int] | None = None)`**：`load_for_recovery` 的参数化版本——指定每节点取哪个版本，而非恒取最新。未指定的节点取最新。用于"从某历史点重建主状态"。
3. **`resume(graph_instance_id, from_versions=None)`**：`GraphRecoveryService.resume` 的定点版本——重建主状态后从指定版本对应的节点重入。需配套：入口集推导规则（`external-control.md` §7）以 `from_versions` 指定的版本为基准判定非终态。

价值：调试（"第 5 轮时状态是什么"）、回放（从历史点 fork 重跑不同分支）、审计（合规追溯）。成本极低——数据已在表里，只补 API 与参数化 `rebuild_main_state`。

## 2. 流式事件层：多模式 + token 级贯穿

### 现有基础

`GraphRuntime`（`runtime.py`）已有 `emit(event_type, data, ctx)` 钩子（ADR-0033 D5），节点可显式调；`before_node` / `after_node` 是引擎自动调用的唯二生命周期钩子。业务层（modex_agent）的 `InterceptorChain` 已实现 LLM token 级流式（`LLM_STREAM` scope），但**不走 graph runtime**——token 事件在 ReAct 节点内部消化，图引擎层无统一 stream 通道。

### 缺口

- 单一 `emit` 通道，无模式区分（消费者无法只订阅"状态更新"或只订阅"token 流"）。
- token 级流式未贯穿到图引擎层——非 ReAct 消费者（未来用 modex_graph 搭的图）拿不到 token 流。
- 无 task 级事件（节点 start/finish 带耗时、token 统计）。

### 设计方向

1. **`StreamMode` 枚举**（`constants.py`）：
   - `VALUES`——每节点完成后全状态快照；
   - `UPDATES`——节点名 + 该节点对 state 的增量（diff 前后 snapshot）；
   - `MESSAGES`——LLM token 级流式（需 LLM callback 桥，见下）；
   - `TASKS`——节点 start/finish 事件（含耗时、token、error）；
   - `CUSTOM`——节点 `ctx.runtime.emit` 自定义事件（现有能力的显式归类）。
2. **`GraphRuntime` 扩展 `stream_modes: set[StreamMode]`**（per-run 配置，默认 `{CUSTOM}`）：`emit` 按 mode 分流，消费者按订阅 mode 过滤。
3. **LLM callback 桥**：在 `GraphRuntime` 实现侧接一个 `BaseCallbackHandler`，`on_llm_new_token` 转发为 `emit(MESSAGES, {token, node, invocation_id})`。这样任何图的 LLM 节点都能产出 token 流，不只 ReAct。
4. **`astream(ctx, modes)` 入口**：`GraphEngine` 加流式入口，返回 `AsyncIterator[StreamPart]`，内部驱动 `run_async` + 从 runtime emit 队列消费。

价值：任何图消费者都能拿到结构化事件流（不只 ReAct）；调试与可观测性显著提升；为后续前端实时渲染铺路。成本中等——LLM callback 桥是主要工作量，mode 枚举与分流是轻量改造。

## 3. 节点级执行策略：retry / timeout / error policy

### 现有基础

`Node.max_retry`（`node.py`）是**框架级未投递检测重试**——节点 `execute()` 返回但没调 `deliver()` 时，框架注入错误反馈重试，超过 `max_retry` 抛 `RoutingError`。这是路由正确性保障，不是业务级错误恢复。`Node.run()` 的异常路径：`GraphInterrupt→suspend`、`GraphBubbleUp→cancel`、`Exception→crash`（`node.py:301-315`）。崩溃恢复靠 `recover_crashed` 扫描重派。

### 缺口

- 无 per-node 业务级重试策略（LLM 超时、工具失败时节点自己 catch 重试，框架不兜底）。
- 无执行超时保护——长时节点（如外部 agent 调用）阻塞 await 期间无 watchdog。
- 无错误分支路由——节点失败只能 crash 图或节点自己吞，不能声明式"失败走 fallback 节点"。

### 设计方向

1. **`NodeExecutionPolicy` 值对象**（`spec.py`，加到 `NodeSpec.config` 或独立字段）：
   ```python
   class NodeExecutionPolicy(BaseModel):
       retry: RetryPolicy | None = None
       timeout: TimeoutPolicy | None = None
       on_error: ErrorStrategy = ErrorStrategy.CRASH  # CRASH / DEFAULT_VALUE / FAIL_BRANCH
   ```
2. **`RetryPolicy`**：`max_attempts` / `initial_interval` / `backoff_factor` / `max_interval` / `jitter` / `retry_on: Callable[[Exception], bool] | None`。`Node.run()` 在 `execute` 外包一层重试循环：catch 匹配异常 → 清空本次 invocation 的 delivers（`_pending_delivers.clear()`）→ sleep backoff+jitter → 重跑 `execute`。重试时 invocation 不新建（同一 invocation 内重试），超出 `max_attempts` 走 `crash_invocation`。
3. **`TimeoutPolicy`**：`run_timeout`（硬墙钟）+ `idle_timeout`（无进展超时，需 progress heartbeat）。用 `asyncio.wait_for` 或 watchdog task 实现；超时抛 `NodeTimeoutError`（`GraphBubbleUp` 子类，可被 retry）。
4. **`ErrorStrategy`**：
   - `CRASH`（默认）——异常走现有 crash 路径；
   - `DEFAULT_VALUE`——节点声明 `default_output`，异常时框架替它 deliver 默认值后正常 complete；
   - `FAIL_BRANCH`——节点声明 fallback 边（`NodeSpec` 加 `on_error_target`），异常时框架替它 deliver 到 fallback 节点后 complete。

价值：生产可靠性。长时外部调用超时、LLM 偶发失败重试、工具失败走 fallback——这些都是业务层目前各自实现的，提到框架层后统一且可声明式配置。成本中等——retry 循环 + timeout watchdog + default/fallback 分支的 deliver 替代。

## 4. 子图独立 checkpoint 与 ParentCommand 双向通信

### 现有基础

`GraphAsNode`（`nodes/graph_as_node.py`）当前是共享 ctx 的轻量 wrapper：`execute()` 调 `compiled.execute(ctx)` 在父 ctx 上跑子图，子图 node invocation 写入**父图的** `node_states`（共享 `graph_instance_id`），然后 deliver `{"subgraph_completed": True}`。`GraphMetadata` 有 `parent_instance_id` / `parent_node` 字段（嵌套原语已就绪）。`ParentCommand(GraphBubbleUp)` 异常类已存在但"Phase c only，从未抛出"（exceptions.py 注释）。

### 缺口

- 子图无独立 `graph_instance_id` + coordinator——崩溃恢复时无法区分父子图边界，子图状态混在父图版本链里。
- `ParentCommand` 未激活——子图不能向父图发命令（如"我完成了，请父图跳转节点 X"）。
- 子图无独立 checkpoint 命名空间——时间旅行（§1）无法按子图层查询。

### 设计方向

1. **`GraphAsNode.execute` 改造**：创建子 `GraphInstance`（`parent_instance_id` = 父 gid，`parent_node` = 本节点名）+ 独立 coordinator（同 factory 装配）+ 独立 `GraphContext`（共享 `ctx.state` 但独立 coordinator/invocation）。子图跑在自己的实例空间里。
2. **状态桥接**：子图完成后，显式把需要暴露给父图的结果 deliver 到父图（而非依赖共享 state 副作用）。父图通过 deliver 消费子图结果。
3. **`ParentCommand` 激活**：子图节点可 `raise ParentCommand(command_dict)`（`exceptions.py`），子图 scheduler 不吞（D7），传播到 `GraphAsNode.execute` 的 except 分支，包装为父图层的 deliver 或 GraphInterrupt。典型用途：子图 HITL 挂起冒泡到父图、子图请求父图跳转。
4. **嵌套恢复**：父图崩溃恢复时，若 `parent_node` 指向 GraphAsNode，递归恢复子图实例（`recover_crashed` 对子图实例同样适用，`parent_instance_id` 链可遍历）。

价值：图套图是核心编排模式（PRD L17），当前"能跑但恢复语义不完整"（backlog BL-10）。独立 checkpoint 让子图可独立恢复/审计/时间旅行；ParentCommand 让子图能表达"我需要父图做什么"。成本中等偏高——GraphAsNode 重写 + ParentCommand 传播链 + 嵌套恢复递归。

## 5. 多后端持久化：Postgres CoordinatorFactory

### 现有基础

`CoordinatorFactory` ABC（`persistence_coordinator.py`）+ 三档 store ABC（`GraphInstanceStore` / `NodeStateStore` / `DeliverStore`）+ SQLite 实现齐全。`external-control.md` §9 + `issues/38` 已设计 SQLite CoordinatorFactory 装配与业务层接线。store 接口全是同步方法（`§12.2 线程契约`：只在 event-loop 线程调用），SQLite 用 `sqlite3.Connection` caller-owned。

### 缺口

只有 SQLite 一种持久化后端。单进程 + SQLite 适合单实例 bot，但多实例部署（多个 bot 进程共享图状态、跨机恢复）需要 Postgres 的事务与连接池。

### 设计方向

1. **`SqliteNodeStateStore` / `SqliteDeliverStore` / `SqliteGraphInstanceStore` 的 SQL 方言抽象**：当前 DDL 是 SQLite 方言（`json_valid` CHECK、`BIGINT PRIMARY KEY`）。提取 dialect 层或直接写 Postgres 对应 DDL（`JSONB` 替代 `TEXT CHECK(json_valid)`、`BIGSERIAL` 或 Snowflake PK 保留）。
2. **`PostgresCoordinatorFactory`**：用 `asyncpg` 或 `psycopg` 连接池，store 方法改为同步包装（`asyncpg` 是异步的，需 `async_to_sync` 或 store 接口异步化——后者改动大，倾向前者配合 `run_in_executor`）。
3. **事务语义**：Postgres 支持事务，可让 `complete_invocation` + `promote_delivers` 在同一事务内提交（SQLite 当前是各自 commit），进一步收紧崩溃窗口。
4. **schema 迁移**：复用 `MigrationRunner` 模式，加 Postgres migration 脚本。

价值：多实例生产部署、跨机崩溃恢复、更高并发。成本中高——dialect 抽象 + 连接池 + 事务语义 + 异步适配。

## 6. 明确不在演进方向内的事

以下能力看似"缺失"，实为刻意的设计边界，不纳入演进：

| 能力 | 不做的理由 |
|---|---|
| channels / reducers 层 | 已在 ADR-0033 2026-08-05 精炼中**刻意移除**，收敛到 deliver + 共享 state。reducer 的并行合并靠 deliver 聚合 + ON_ALL_PREDS 触发门。重新加 channel 层是倒退，增加复杂度无收益。 |
| 内置业务节点库（LLM/Code/HTTP/Iteration 等） | modex_graph 是框架无关图原语层，业务节点归 modex_agent。`FunctionNode` 作为万能适配器可包装任意逻辑；`GraphAsNode` 支持子图；`graph_patterns/` 示例已展示 conditional/retry/map_reduce。内置业务节点破坏框架无关边界。 |
| 变量引用语法（`{{#node.var#}}`） | 用 Pydantic 类型化 `GraphState` 字段——比 selector 更类型安全、IDE 友好、编译期可校验。无类型 selector 是产品便利妥协，不是引擎能力。 |
| 工作流版本/发布/draft-published | 业务层职责（bot 图工厂，backlog BL-13）。`GraphSpecStore` 存图定义，版本管理是上层关注点。 |
| 分布式任务队列（Celery 等） | modex_graph 是单进程 asyncio 引擎。分布式调度是业务层包装（多进程共享 Postgres + 各自跑 `recover_crashed` 即可分布式恢复，无需引擎内建队列）。 |
| 图级 MVCC 轮次 | 当前共享 state + full snapshot 无此概念，backlog BL-05 明确"优先级低，暂不设计"。节点级版本链已提供 per-node 版本，图级轮次收益不明确。 |

## 7. 演进优先级建议

按"价值 × 成本 × 架构就绪度"排序：

1. **时间旅行 API**（§1）——数据全在表里，补 API 即可，成本最低，调试/审计价值高。
2. **流式事件层**（§2）——`emit` 钩子已存在，加 mode + callback 桥，任何图消费者受益。
3. **节点级执行策略**（§3）——生产可靠性刚需，retry/timeout/error 三件套。
4. **子图独立 checkpoint + ParentCommand**（§4）——图套图恢复语义完整化，配合时间旅行形成嵌套可观测。
5. **Postgres 后端**（§5）——多实例部署前提，成本最高但 unlocks 生产规模化。

§1-§3 可在 tickets 34-39 落地后立即排期；§4-§5 视生产需求排期。每项落地时各自开设计 ticket，不在本文件展开实现细节。
