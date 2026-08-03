# taskId 的 scope 和语义

Status: triage:closed
Assignee: sisyphus
Started: 2026-08-02
Resolved: 2026-08-02

## Question

用户描述:"它们可以按 taskId 构建所需的共享知识库"。

调研发现:taskId 已在 `external/types.py:174` 定义,通过 `MODEX_TASK_ID` env 注入,但**仅门控 4 个 stub 命令,零业务消费**。modex_graph **零引用** taskId。

需要决策:

1. **taskId 的 scope** — 
   - **图级**:一个图 run 一个 taskId,所有 node 共享。简单,但粒度粗。
   - **节点级**:每个 node 有自己的 taskId。细粒度,但"共享知识库"需要跨 node。
   - **分层**:图有一个 root taskId,每个 node 有子 taskId(如 `root.node_1`)。兼顾共享与隔离。

2. **taskId 与 graph run_id 的关系** — modex_graph 的 ParallelScheduler 有 `run_id`(CheckpointStore 用)。taskId 与 run_id 是同一个?还是独立?如果独立,如何关联?

3. **taskId 的生成** — 谁生成?调用方传入?图调度系统自动生成?复用已有的 `ExternalEnvSpec.taskId`(已定义但未消费)?

4. **taskId 的持久化** — taskId 是否需要持久化(跨进程/跨重启)?存在哪里?复用已有的 persistence(SQLite)?

5. **taskId 与现有 ExternalEnvSpec.taskId 的关系** — 已有的 taskId 定义在 external agent 集成层。图调度系统的 taskId 是复用它?还是重新定义?如果复用,是否需要提升到框架层(从 external 层移到更通用的位置)?

## Context

- 调研发现:taskId 定义在 `src/modex_agent/agents/external/types.py:174`,通过 `MODEX_TASK_ID` env 注入
- 调研发现:taskId 仅门控 4 个 stub 命令(modexctl handoff/patch/inspect/codex-review),零业务消费
- 调研发现:modex_graph 零引用 taskId
- 调研发现:KnowledgeBase RAG 仅文档(ADR-0036 不存在),taskId 是为其预留的
- ADR-0034 D19:CheckpointStore 有 run_id 概念
- graph engineering 概念:graph_id / run_id / node_id 三元组(可观测性 backbone)
- **ticket 10 联动**:run_id 当前 uuid 随机生成、不持久化、不接受外部传入(ticket 10 类别 1 纯实现缺口)。本 ticket 问题 2(taskId 与 run_id 关系)需要与 ticket 10 的 run_id 管理决策联动。建议两个 ticket 在 run_id/taskId 关系上协同决策,或本 ticket 在 ticket 10 的 run_id 决策后推进问题 2。

## Resolution criteria

明确以下决策:
- taskId scope(图级 / 节点级 / 分层)
- taskId 与 run_id 的关系(复用 / 独立 / 关联)
- taskId 生成方式(调用方 / 自动 / 复用 ExternalEnvSpec)
- taskId 持久化策略
- 与现有 ExternalEnvSpec.taskId 的关系(复用 / 重新定义 / 提升)

## Resolution

### 核心决策:图调度场景中 taskId = graph_instance_id(值相同,概念不同)

taskId 和 graph_instance_id 是**不同概念**:
- taskId 是业务层/external agent 集成层的概念,有它自己的注入/使用方式(env 注入 `MODEX_TASK_ID`)
- graph_instance_id 是图调度层的概念(ticket 04/10 决议,Snowflake ID,持久化唯一 key)

**只在图调度场景中值相同**:创建 GraphInstance 时,bot 工厂把 graph_instance_id 作为 taskId 通过 env 注入给 ExternalEnvSpec(与其他 env 注入方式一致),让它们值相同。不统一概念,不改代码结构。

### 5 个问题的回答

| 问题 | 答案 |
|------|------|
| **1. scope** | 图级——一个图实例一个 taskId(=graph_instance_id),所有 node 共享。节点级标识是 node_state_id(node_states 表 PK,Snowflake) |
| **2. 与 graph_instance_id 关系** | 图调度场景中值相同。概念不同:taskId 是业务/external 层概念,graph_instance_id 是图调度层概念 |
| **3. 生成** | 创建 GraphInstance 时生成 graph_instance_id(Snowflake),同时作为 taskId 通过 env 注入给 ExternalEnvSpec |
| **4. 持久化** | graph_instance_id 在 graph_instances 表(PK)。taskId 不额外持久化(值相同,查 graph_instance_id 即可) |
| **5. 与 ExternalEnvSpec.taskId 的关系** | 不统一(路径 C:保持现状,值相同)。ExternalEnvSpec.taskId 保持自己的定义和注入方式(env)。图调度场景中约定 taskId = graph_instance_id,加注释/文档说明 |

### 实现方式

- bot 工厂创建 GraphInstance 时:`os.environ["MODEX_TASK_ID"] = str(graph_instance_id)`(或其他 env 注入方式,与现有 external agent 集成一致)
- ExternalEnvSpec 从 env 读取 taskId,逻辑不变
- 在创建处加注释/文档说明:"图调度场景中,taskId = graph_instance_id,这是业务约定,不是代码统一"

### 不做的

- 不提升 taskId 到框架层(路径 B)——改动大,概念混淆
- 不让 ExternalEnvSpec 直接引用 graph_instance_id(路径 A)——external 层不应该依赖图调度层概念
- 不改 ExternalEnvSpec.taskId 的定义和注入方式——保持现状

### 与 ticket 10 的联动

ticket 10 类别 1 已决议 graph_instance_id(取代 run_id)。本 ticket 确认 taskId 在图调度场景中与 graph_instance_id 值相同。两个概念各自独立,通过值相同实现关联。
