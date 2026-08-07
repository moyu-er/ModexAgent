# 共享知识库的接口和 scope

Status: triage:closed
Assignee: sisyphus
Started: 2026-08-02
Resolved: 2026-08-02

## Question

用户描述:"它们可以按 taskId 构建所需的共享知识库"。

调研发现:KnowledgeBase RAG **未实现**(仅 CONTEXT.md 前瞻性描述,ADR-0036 不存在)。现有三层记忆(Session/Archive/Core)是 agent 级的,不是 task 级的。

需要决策:

1. **知识库的接口** — node 如何读写知识库?
   - 简单 KV(`get(key) / set(key, value)`)?适合结构化数据传递。
   - 搜索型(`search(query) -> results`)?适合非结构化知识。
   - 两者都要?接口形态是什么(frozen Pydantic ABC)?
   - 通过 `GraphRuntime` 暴露(如 `ctx.runtime.kb_get(...)`)?还是 `GraphContext` 直接访问?还是 `user_data`?

2. **知识库的 scope** — 
   - **图级**:一个图 run 一个知识库,所有 node 共享读写。简单。
   - **task 级**:按 taskId 隔离,不同 task 的知识库独立。需要 taskId scope 先确定(05)。
   - **node 级 + 图级**:每个 node 有私有空间 + 图级共享空间。类似"私有变量 + 全局变量"。

3. **知识库的持久化** — 
   - **run 内**:图 run 结束后知识库消失。简单,但无法跨 run 复用。
   - **持久化**:知识库跨 run 存在,可作为"图复用仓库"(wGraph 概念)。需要持久化后端(SQLite?)。
   - 用户之前说"易用性,后续"——但知识库如果是核心协作机制,可能不能后续。

4. **知识库与现有三层记忆的关系** — 
   - 现有 Session/Archive/Core Memory 是 agent 级的(每个 agent 有自己的记忆)。
   - 知识库是 task 级的(多个 agent/node 共享)。
   - 是新增一个 TaskScope 记忆层?还是独立系统(不复用记忆基础设施)?
   - ADR-0035 把"Knowledge"层重命名为"Core Memory"以"disambiguate from the forthcoming KnowledgeBase RAG module"——暗示 KnowledgeBase 是独立系统。

5. **知识库的内容** — 
   - node 产出物(执行结果、中间数据)?
   - 任务上下文(目标、约束、进度)?
   - agent 间的"交接信息"(node A 给 node B 的消息)?
   - 以上全部?

## Context

- 调研发现:KnowledgeBase RAG 未实现,ADR-0036 不存在
- 调研发现:taskId 是为 KnowledgeBase 预留的
- ADR-0035:把"Knowledge"层重命名为"Core Memory","disambiguate from the forthcoming KnowledgeBase RAG module"
- 现有三层记忆:Session(会话级)、Archive(归档级)、Core Memory(核心级)
- graph engineering 概念:org graph(稳定)vs work graph(临时);work graph 的 stateJson 是自定义流程状态
- openclaw:TaskFlow 的 stateJson + waitJson 是逃逸舱,让 controller 自行管理复杂状态

## Resolution criteria

明确以下决策:
- 知识库接口(KV / 搜索 / 两者;ABC 定义;通过 runtime/context/user_data 暴露)
- 知识库 scope(图级 / task 级 / node+图级)
- 知识库持久化(run 内 / 持久化 / 混合)
- 与现有三层记忆的关系(新增 TaskScope / 独立系统 / 复用)
- 知识库内容类型(产出物 / 上下文 / 交接信息 / 全部)
- 是否需要写 ADR-0036 定义 KnowledgeBase

## Resolution

### 业务功能增强,非框架级设计

知识库是 modexctl kb 的业务功能增强,不是框架级设计。

**本质**:modexctl kb 的 CRUD 加一个 `--by-task`(参数命名参考现有 modexctl 设计)参数,按 taskId 划区。

**行为**:
- 带 `--by-task <taskId>`:kb 的 CRUD 带上 taskId 用于区分,属于该 task 的私有知识库
- 不带 `--by-task`:公共知识库内容,不带 taskId 也可 CRUD

**用途**:补充 submit 内容不够表现上下文的场景。deliver/submit 处理即时数据流(node 间传递),知识库提供持久化共享存储(任意 node 随时读写,不限上下游)。

**与 taskId 的关系**:taskId = graph_instance_id(ticket 05 决议)。`--by-task` 参数的值就是 graph_instance_id,通过 env 注入给 agent,agent 通过 modexctl kb 使用时不需自己感知 taskId(参数从 env 读取)。

### 不是框架级设计

- 不需要 KnowledgeBase ABC
- 不需要 GraphRuntime 暴露
- 不需要 node 级状态抽象
- 不需要 ADR-0036
- 不需要统一持久化 schema(复用 modexctl kb 现有持久化)

### 优先级:低

当前优先级不高。完成框架+图设计实现后,接入 bot 后再做。本 ticket 关闭,实现阶段按需推进。
