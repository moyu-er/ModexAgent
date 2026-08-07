# 图生命周期管理:持久化、恢复、bot 工厂

Status: triage:closed
Assignee: sisyphus
Started: 2026-08-02
Resolved: 2026-08-02(类别1+2+3)

## Question

图调度系统需要完整的生命周期管理。深挖探索确认:大量设计已存在(ADR-0034 D19 CheckpointStore、D7 multi-instance、resume_target、DispatchStore),但实现一半,且图定义持久化/生命周期状态机/bot 层管理完全不存在。

需要决策三大类问题:

### 类别 1:纯实现缺口(设计已有,只需接通)

1. **CheckpointStore.load_latest 接通** — save 已实现(parallel.py:657-703 异步保存),load_latest 零调用(ADR-0034 D19 明确 deferred)。如何接通?
   - ParallelScheduler.run_async 接受外部 run_id(当前 parallel.py:167 硬生成 uuid)
   - 从 checkpoint 恢复 main_state / pending_on_all_preds / completed_instances / dispatch_events
   - 跳过 completed_instances 中的节点

2. **run_id 管理** — 当前 uuid.uuid4().hex 随机生成,不持久化,不接受外部传入。需要:
   - 显式 run_id 传入(run_async 接受 run_id 参数)
   - run_id 持久化(跨重启关联)
   - run_id ↔ 业务实体映射(run_id 属于哪个图/session/task)

### 类别 2:需扩展设计(已有设计但不够)

3. **多节点并行恢复** — CheckpointData 不捕获 READY/RUNNING 实例(只记录 COMPLETED)。
   - 方案 A:CheckpointData 增加 in_flight_instances 字段(捕获 READY/RUNNING 实例 + forked_state)
   - 方案 B:crash 时 in-flight 实例视为未完成,恢复时重新 dispatch(需要节点幂等)
   - 方案 C:crash 时 in-flight 实例视为失败,触发错误恢复路径
   - 选哪个?

4. **节点幂等** — ADR-0033 D7 主动选择 suspend-without-re-execution 回避了幂等。但 crash recovery 场景下,重新 dispatch = 重新调用 execute = 可能重复副作用。
   - 幂等键机制(execute 接受 idempotency_key)?
   - is_retry 信号(execute(ctx, *, is_retry: bool))?
   - 副作用日志/dedup?
   - 还是接受"crash recovery 可能重复副作用"作为已知限制?

5. **从任意入口恢复** — resume_target(已交付)支持进程内 suspend/resume 从中间节点恢复。crash recovery 是否也支持"从指定节点恢复"?
   - 从 checkpoint 恢复时,入口是 START 还是断点节点?
   - 如果断点节点是 ON_ALL_PREDS 且部分上游未完成,如何处理?

### 类别 3:完全不存在,需新设计

6. **图定义持久化** — 当前 Graph builder 是纯编程式(add_node/add_edge/compile),CompiledGraph 只在内存。
   - 图定义如何持久化?(GraphSpec JSON/dict → 数据库?文件?)
   - 图定义 CRUD?(创建/读取/更新/删除)
   - 图定义"只设计不执行"(持久化但不调度)如何表达?
   - 与 ticket 08(GraphSpec)的关系:GraphSpec 既是"配置载体"又是"持久化格式"?

7. **图生命周期状态机** — 当前图要么是内存对象,要么在跑。没有 designed/created/executing/paused/completed/failed 状态。
   - 需要哪些状态?
   - 状态转换规则?
   - 状态持久化在哪里?

8. **bot 层图管理** — bot_project 无图 CRUD、无图列表、无图与 pool 关联。
   - bot 如何管理多个图?(图列表存储在哪里?)
   - 图与 pool 的关系?(一个 pool 多个图?一个图跨 pool?)
   - bot 图工厂的接口?(build_graph(spec, deps) → Graph?build_and_run(spec, deps, run_id?) → Result?)
   - 图调度的入口位置?(新 TurnRunner?ExecutionStrategy?独立服务?CLI?——与 ticket 10 联动)

## Context

### 已有设计(可复用)

- **ADR-0034 D19 CheckpointStore**:CheckpointData(main_state/pending_on_all_preds/completed_instances/dispatch_events)+ save 已实现 + load_latest 零调用 + ADR 明确 "crash recovery deferred"
- **ADR-0034 D7 multi-instance**:DORMANT→PENDING→READY→RUNNING→COMPLETED 状态机,纯内存
- **ADR-0033 D7 resume_target**:进程内 suspend/resume 从中间节点恢复,已交付
- **ADR-0034 D16 DispatchStore**:SQLite 持久化已实现,恢复路径不存在
- **ADR-0034 D17 event model**:instance 完成后触发 _schedule 重新检查
- **ApprovalResumer**(`src/modex_agent/pipeline/approval_resumer.py`):项目里唯一端到端跑通的 resume 实现,模式为 load_pending → apply_resume → restore state。可作 graph resume 参考。
- **ADR-0008/0020**:审批默认关闭 + subagents 永远无审批(长程任务中关闭审批无需新 ADR)
- **ADR-0015 inbox**:InboxFlushHook fold-in 完整实现(AgentNode 内 agent 天然支持)

### 已有缺口(探索确认)

- CheckpointData 不捕获 READY/RUNNING 实例(只 COMPLETED)
- run_id 不持久化,不接受外部传入
- 节点幂等无设计(D7 主动回避)
- 图定义持久化完全不存在(issue 08 open)
- 图生命周期状态机完全不存在
- bot 层图管理完全不存在(issue 10 open)
- ConcurrentWriteTracker state 明确不持久化(假设重启时无 in-flight)

### 用户需求

- 图要有持久化(图定义 + 执行状态)
- 重启时从断点恢复(可能多个节点正在运行)
- 节点要有恢复实现,做好幂等
- 框架层面支持恢复,从挂掉的节点重新执行
- 图定义可以只设计不执行(持久化但不调度)
- 每次调度不论入口是否在起点,都可以由 bot 构建对象并调用图
- bot 有自己的工厂,bot 启动后所有依赖就绪,工厂构建图对象 + 注入依赖 + 调度执行

## Resolution criteria

明确以下决策:
- CheckpointStore load_latest 接通方案(含 run_id 管理)
- 多节点并行恢复策略(方案 A/B/C)
- 节点幂等策略(幂等键 / is_retry / 接受重复 / 其他)
- 从任意入口恢复的支持范围
- 图定义持久化格式与 CRUD
- 图生命周期状态机(状态集 + 转换规则)
- bot 层图管理(图列表 / 图与 pool 关系 / 图工厂接口 / 调度入口)
- 是否需要新 ADR(或扩展现有 ADR-0034 D19)

## Resolution(类别 1+2)

### 核心原则:状态分层 + 职责分离

**modex_graph 的职责(图级)**:
1. 维护图级状态(main_state / pending_dispatches / activated_sources / completed_instances / instance_seq / iteration_count / graph_instance_id)
2. 持久化图级快照(CheckpointStore,已有 save,接通 load_latest)
3. 根据 checkpoint 重建图级状态到内存
4. 重新 dispatch 未完成节点(复用正常调度逻辑)
5. 提供状态查询能力(业务层视情况获取/查看,不推送)

**不做的**:
- 不判断节点内部各步骤如何恢复
- 不设计节点级幂等机制
- 不捕获 in_flight forked_state(crash 时已死,重新执行)

**Node 的职责(业务级,通过 Node 级状态抽象)**:
- 自己维护状态(单状态 / MVCC 版本链 / 无状态,由 node 自己决定)
- 自己的持久化策略(用通用实现或自己重写)
- 自己的恢复实现(execute 被重新调用时如何处理)
- 自己的幂等判断
- 上游输入是否需要重新拿来触发 LLM
- **自己判断如何处理重复调用**:环形重复(下游 submit 重新唤醒上游,正常的新一轮)/ 故障恢复(crash 后 checkpoint 恢复,可能有部分完成的工作)/ 未投递重跑(ticket 03,应看到错误反馈)——这三种场景 node 的处理策略不同,但框架不传递"调用原因"信号。node 通过自己的状态维护(MVCC 版本链 / 单状态 / 自定义)+ 框架提供的原语(instance_id+seq / completed_instances / node_states 表)自行判断。简单的状态维护可能无法满足实际诉求,node 可以实现复杂的状态逻辑(如完整 agent 历史和中间态),通过检查自己的状态判断"上次做到哪了""这是新一轮还是恢复"。

### 类别 1:纯实现缺口(设计已有,接通 load_latest + run_id)

#### 1.1 CheckpointStore load_latest 接通

> **设计修正(2026-08-03)**:本节的 CheckpointData 单一 blob 持久化已被分布式持久化替代。见 `distributed-persistence-design.md`。恢复流程改为 coordinator.load_for_recovery(从 graph metadata + 各 node 版本链重建),不再从单一 blob 恢复。CheckpointData 移除,activated_sources / instance_seq / iteration_count 归入 graph metadata。以下为原始设计(保留作历史参考)。

**恢复流程**:
```
1. 业务层传入 graph_instance_id → load_latest(graph_instance_id) → CheckpointData
2. 重建内存状态(同步段,无 await):
   - main_state = GraphState.from_checkpoint(data.main_state)
   - pending_dispatches = data.pending_on_all_preds
   - completed_instances = data.completed_instances(可信,事务保证)
   - dispatch_events = data.dispatch_events(审计日志)
   - activated_sources = data.activated_sources(**当前未持久化,需新增字段**)
   - instance_seq = data.instance_seq(**当前未持久化,需新增字段**)
   - iteration_count = data.iteration_count(**当前未持久化,需新增字段**)
   - ConcurrentWriteTracker 从零重建(已有设计)
3. 重新计算可执行节点:
   - 不倒推 completed_instances(危险:有向环 + 条件路由 + MVCC)
   - 复用 _recheck_pending 逻辑:根据 activated_sources + pending_dispatches + _can_reach_active 推导
   - 未完成节点(crash 时 RUNNING/READY/PENDING)通过 _recheck_pending 自然触发
4. 正常调度循环执行
```

**CheckpointData 需新增字段**(当前缺失):
- `activated_sources: dict[str, set[str]]` —— ON_ALL_PREDS 节点已激活的上游集合(恢复 ALL_PREDS 判断的必要数据)
- `instance_seq: int` —— 全局 instance 序号(避免恢复后 instance ID 与已完成的冲突)
- `iteration_count: int` —— 全局迭代计数(max_iterations 安全网)

**事务保证**:现有实现已满足——instance COMPLETED 标记 + 下游 dispatch + _mark_ready 在同一同步段(无 await 之间,asyncio 单线程保证原子)。checkpoint 在 dispatch 之后异步保存,即使 crash 在 checkpoint 保存前,恢复时根据实际 completed_instances 推导,状态一致(可能浪费已完成工作,但不矛盾)。

#### 1.2 graph_instance_id 管理(原 run_id,术语修正)

- `graph_instance_id` 是持久化的唯一 key(ticket 04 决议:GraphInstance 抽象)
- 重启后通过 graph_instance_id 关联同一实例
- checkpoint/dispatch/activated_sources/completed_instances 等全部挂在 GraphInstance 上
- 当前的 `self._run_id`(uuid 随机生成)应被 graph_instance_id 取代
- graph_instance_id 由外部传入(创建 GraphInstance 时分配),不是内部随机生成
- graph_instance_id ↔ 业务实体映射(task_id / session_id 等)是业务层的事

### 类别 2:需扩展设计

#### 2.1 多节点并行恢复

> **设计修正(2026-08-03)**:本节的多节点恢复策略已纳入分布式持久化设计。恢复从各 node 最新 invocation 状态推导(COMPLETED 跳过,非 COMPLETED 重新 dispatch,新建 invocation)。见 `distributed-persistence-design.md` §5。以下为原始设计(保留作历史参考)。

**策略:不捕获 in_flight,根据 activated_sources + pending_dispatches + 可达性重新推导**

crash 时 RUNNING/READY/PENDING 实例的状态丢弃(瞬态,不可信)。恢复时:
- 信任 completed_instances(事务保证)
- 信任 activated_sources + pending_dispatches(持久化的调度状态)
- 丢弃 READY/RUNNING 状态,通过 _recheck_pending 重新推导
- 未完成节点被重新 dispatch → execute 被重新调用 → node 自己决定如何处理

**不倒推 completed_instances 的原因**(用户修正):
1. 有向环中,节点可能 completed 多次(循环),单靠 completed 状态无法判断当前轮次
2. 条件路由中,部分上游因路由选择不走,不应视为"未完成需等待"——需通过 activated_sources 判断
3. MVCC 版本链下,每次唤醒是独立状态版本,completed 只是某个版本的状态

**瞬态情况**:crash 可能发生在"上游已 COMPLETED 但下游还没 READY"的窗口。现有实现中 COMPLETED + dispatch + _mark_ready 是同步段(无 await),不会出现此瞬态。但 checkpoint 异步保存可能在 dispatch 后、save 前崩溃——此时恢复看不到这次完成,上游会被重新 dispatch 重新执行(浪费但一致)。

#### 2.2 节点幂等——移出 modex_graph 范围

**节点幂等完全是 node 的责任**。modex_graph 只提供:
- "这个节点上次是否完成"的信息(completed_instances)
- "这是第几次被唤醒"的信息(instance_id + seq)

node 的 execute 被重新调用时如何处理(从头来 / 跳过已完成步骤 / 幂等键去重)由 node 自己决定。

#### 2.3 从任意入口恢复

modex_graph 根据 checkpoint 定位断点:
- 不从 START 重新跑整个图
- 根据 activated_sources + pending_dispatches + completed_instances + 可达性,识别未完成节点
- 重新 dispatch 未完成节点
- 入口是断点节点(不是 START),由 _recheck_pending 自然触发

### 类别 2 新增:Node 级状态抽象(MVCC 版本链)

> **设计修正(2026-08-03)**:本节的 Node 级状态抽象已演进为完整的分布式持久化设计。NodeState ABC 从 read/snapshot/restore 扩展为 save_invocation/load/query_versions + 版本链 + 生命周期统一调度(PENDING→RUNNING→COMPLETED/CANCELED/CRASHED)。见 `distributed-persistence-design.md` §3-§4。以下为原始设计(保留作历史参考)。

#### 设计原则

- **ABC 在 modex_graph**:定义节点状态接口(read/snapshot/restore/状态查询)
- **通用实现在 modex_graph**:SimpleNodeState(单状态 + 简单快照),node 可直接用
- **业务实现在 modex_agent**:AgentNodeState(带 MVCC 版本链 + agent 历史持久化),node 可覆盖
- **node 可选择不维护版本链**(单状态),合法简化
- **内存缓存优先**:状态查询频繁(图遍历查看上游依赖、ALL_PREDS 判断、可达性检查),必须内存缓存,IO 在背后

#### 接口需求(初步)

Node 暴露状态方法,graph 调度时通过这些方法读取:

**读**(graph 调度时需要):
- 恢复时判断节点是否完成
- ALL_PREDS 模式判断上游情况(上游是否完成 / 是否激活)
- 可达性检查时查看节点状态

**写**(graph 调度时需要):
- 节点状态刷新(刷为 READY / COMPLETED 等)
- dispatch 时写入 pending payload

**具体 ABC 设计待 ticket 02 的 NodeFactory/NodeSpec 落地后细化**——Node 级状态抽象是 Node 的子组件,与 Node ABC 的关系需要协同设计。

#### 图级 MVCC 轮次(待办,不设计)

图级 MVCC(每轮循环是一个事务,轮次内所有节点看到同一版本)记为待办,优先级低,暂不设计实现。

### 待新增 ADR

> **设计修正(2026-08-03)**:以下 ADR 扩展建议已被 `distributed-persistence-design.md` 替代。CheckpointData 新增字段改为 graph metadata + NodeState 分布式持久化;load_latest 接通改为 coordinator.load_for_recovery;Node 级状态抽象已演进为完整持久化接口。原 ADR-0034 D19 的 CheckpointStore ABC 被 coordinator + NodeState 替代。

建议新增 ADR 扩展 ADR-0034 D19:(已被替代,见 `distributed-persistence-design.md` §10)
- ~~CheckpointData 新增字段(activated_sources / instance_seq / iteration_count)~~ → 归入 graph metadata
- ~~run_id 外部传入~~ → graph_instance_id(已实现)
- ~~load_latest 接通 + 恢复流程~~ → coordinator.load_for_recovery
- ~~Node 级状态抽象 ABC~~ → NodeState 完整持久化接口
- 不在 modex_graph 层面设计节点幂等(职责分离)— 仍然有效

### 类别 3:完全新设计(图定义持久化 + 生命周期状态机 + bot 工厂 + 外部控制)

#### 3.1 图定义持久化

**GraphSpec 独立表**:多个图实例共享同一 spec,避免重复存储。支持"只设计不执行"(spec 存了但没有 instance)。

**存储格式**:SQLite(与现有持久化一致)。spec 内容以 JSON 序列化存储在 `spec_json` 列。

**跨 workspace**:先跨 workspace 共用(业务层面后续可调整)。

#### 3.2 生命周期状态机

图实例状态枚举(`GraphInstanceStatus`,StrEnum,便于扩展):

```
designed → created → running → completed
                     ↑↓
                   paused ← pause()(手动,不被故障恢复自动捡起)
                   stopped ← stop()(可手动恢复,非终态)
                   crashed ← 故障(可被故障恢复自动捡起)
                   failed ← 执行失败
```

**关键区分**:
- `paused`:用户手动暂停,**不被故障恢复机制自动捡起**,只能手动 resume()
- `stopped`:用户手动停止,**可手动恢复**(非终态),不被故障恢复自动捡起
- `crashed`:故障中断,**可被故障恢复机制自动捡起**
- `completed`/`failed`:终态

**存储**:status 字段在 graph_instances 表中。状态转换由 `__start__`/`__end__`(图自带 sentinel)+ 外部控制触发。

#### 3.3 外部控制接口(异常控制链统一)

**核心**:外部控制(pause/stop/resume/deliver)都走同一条异常控制链。框架提供外部触发接口,异常沿调用链传播,节点决定是否 catch。

**复用现有 ControlCommand 模式**(src/modex_agent/control/types.py):
- 扩展新的 ControlCommandType:PAUSE_GRAPH / STOP_GRAPH / RESUME_GRAPH / DELIVER_TO_NODE
- ControlCommand 已有 scope(session_id/agent_id/turn_id)+ payload + idempotency_key + ttl,可复用

**接口收敛**:REST + CLI 收敛到同一路径(与现有 `modexctl send` → HTTP → `receive` 模式一致)。
- REST endpoint:如 `POST /api/graph/{instance_id}/pause` / `POST /api/graph/{instance_id}/deliver/{node_name}`
- CLI:如 `modexctl graph pause {instance_id}` / `modexctl graph deliver {instance_id} {node_name} {payload}`
- 内部实现收敛到同一条路径(CLI 调 REST 接口)

**submit 针对某个节点**:`deliver(graph_instance_id, node_name, payload)` —— 投递到指定节点。

**异常控制链**:
```
外部触发(pause/stop/deliver)→ 注入异常 → 传播到正在执行的 node._execute(ctx)
  └─ _execute 调用 node.execute(ctx)
    └─ execute 被 异常中断(全部退出,不 catch)
      └─ _execute 框架层清理(标记节点状态 + checkpoint)
        └─ 异常传播到图层面
          └─ InterruptPolicy 处理(ticket 04):
             - CrashPolicy(默认):图实例暂停/停止
             - 业务自定义
```

#### 3.4 node._execute / node.execute 双方法接口

**`_execute`(框架固定,不可覆盖)**:
- 框架的调度入口
- 调用 `node.execute(ctx)`(node 的自定义逻辑)
- 异常处理:全部退出(不判断是否需要退出)
- 清理:标记节点状态 + checkpoint

**`execute`(node 自定义,可覆盖)**:
- node 的实际业务逻辑
- 被异常中断时直接终止(不 catch,全部退出)
- 未来可加自定义字段判断是否需要退出(当前不做)

#### 3.5 恢复的两种类型

| 类型 | 触发 | 行为 | 状态过滤 |
|------|------|------|---------|
| **故障恢复** | crash 后自动 / 重启时 | 从 checkpoint 重建 + 重新 dispatch | 只捡 `crashed` 的,不捡 `paused`/`stopped` |
| **手动恢复** | 外部调 `resume()` | 从 checkpoint 重建 + 重新 dispatch | 适用于 `paused`/`stopped` |

恢复流程相同(从 checkpoint 重建 + 重新 dispatch),区别只是**触发条件和状态过滤**。

#### 3.6 bot 工厂

bot 启动后所有依赖就绪(TurnRunner/AgentPool/Provider 等),bot 图工厂:
1. 从持久化加载 GraphSpec
2. GraphSpecCompiler 编译 → CompiledGraph
3. 实例化 GraphInstance(分配 snowflake_id)
4. 注入依赖(AgentNode 的 TurnRunner 等)
5. 交给 GraphEngine 执行
6. 提供外部控制接口(pause/stop/resume/deliver)

图对象是临时的(每次调度重建),图定义(GraphSpec)和执行状态(checkpoint)是持久的。

#### 3.7 持久化 Schema

**三张表,统一 schema,Snowflake ID(非 UUID)**:

Snowflake ID 优势:递增、有序、索引友好。UUID 不递增可能导致索引慢。后续项目慢慢换用 Snowflake。现成 Python 实现:`snowflake-id`(PyPI,无依赖,Python 3.8+)。

```sql
-- 图定义表(独立,多实例共享)
graph_specs:
  spec_id        BIGINT (PK, Snowflake)
  name           TEXT
  spec_json      TEXT (GraphSpec 的 JSON 序列化)
  version        TEXT
  created_at     INTEGER (timestamp ms)
  updated_at     INTEGER (timestamp ms)

-- 图实例表(运行态,持久化)
graph_instances:
  graph_instance_id   BIGINT (PK, Snowflake)
  spec_id             BIGINT (FK → graph_specs)
  parent_instance_id  BIGINT (FK → graph_instances, nullable)
  parent_node         TEXT (nullable)
  status              TEXT (GraphInstanceStatus 枚举)
  checkpoint_json     TEXT (CheckpointData 的 JSON 序列化)
  created_at          INTEGER (timestamp ms)
  updated_at          INTEGER (timestamp ms)

-- 节点状态表(方式 C:每版本一行 + blob 列,统一 schema)
node_states:
  node_state_id       BIGINT (PK, Snowflake)
  graph_instance_id   BIGINT (FK → graph_instances)
  node_name           TEXT
  version             INTEGER (MVCC 版本号)
  parent_version      INTEGER (nullable, 上一版本,版本链)
  state_json          TEXT (JSON, node 自定义内容)
  status              TEXT (节点状态:pending/running/completed/...)
  created_at          INTEGER (timestamp ms)
  updated_at          INTEGER (timestamp ms)
```

**node_states 表设计**:
- 每版本一行(支持 MVCC 版本链)
- `state_json` blob 列存内容(node 自定义,由 Node 级状态抽象 ABC 管理)
- `version` + `parent_version` 形成版本链
- `graph_instance_id` + `node_name` + `version` 唯一索引
- node 可以选择不维护版本链(单状态,只有 version=0 一行)
- node 可以选择无状态(不写这张表)

**updated_at**:所有表都有,遵循项目规范。

### deliver/submit 修正(来自 ticket 07)

- deliver 持久化:新增 deliver_states 表(Snowflake ID,graph_instance_id FK,node_name,next_node,content_json,status,created_at,updated_at)
- deliver 路由:modexctl deliver → REST → deliver_states 表 → node._deliver 读取
- 外部控制 deliver 的含义修正:不是"外部投递触发异常中断"(原 ticket 11 类别 3 的设计),而是"外部投递累积到当前节点"。异常控制链仍用于 pause/stop/resume,不用于 deliver
- 持久化 Schema 从三表(graph_specs/graph_instances/node_states)扩展为四表(+deliver_states)
