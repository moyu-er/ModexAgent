# 02 — taskId 共享上下文机制

Status: closed
Labels: wayfinder:resolved
Blocking: 06-modexctl-deliver-command, 07-bot-graph-factory

## Question

**taskId=graphInstanceId 作为任务共享上下文的分区键。共享上下文和内容的具体机制是什么？**

### 上下文

- 现有 memory 系统：Session/Archive/Core/Pruned/Experience 五层 + SessionScope/UserScope/GlobalScope
- `ExternalEnvSpec.task_id` 字段已存在（`src/modex_agent/agents/external/types.py:174`）
- `MODEX_TASK_ID` env 注入已实现（`env_builder.py:90-91`）
- bot 工厂不存在（BL-13）——taskId 注入路径未接线
- `docs/design/graph-orchestration/issues/history/06-shared-knowledge-base-interface.md` 已关闭为"业务功能增强"

## Discussion (2026-08-07)

### 修正：agent 不感知 taskId，内部自动注入

ticket 06 历史决议说"agent 通过 modexctl kb 使用时不需自己感知 taskId（参数从 env 读取）"。进一步明确：

- **`modexctl kb --by-task`**：`--by-task` 是 **bool 开关**（不是传 taskId 值），CLI 内部从 `MODEX_TASK_ID` env 读取实际 taskId
- **kb tool**：同理，agent 调 `task_kb(action="get", key="xxx")`，tool 内部从 graph context 拿 taskId，agent 不传 taskId

agent 永远不感知 taskId 的值——它只知道"我要读写 task 知识库"。

### kb 定位（结合 01 结论）

- **deliver** = 即时、定向、节点间数据流（上游 → 下游，有明确目标）
- **modexctl kb** = 持久化、共享、任意节点随时读写（不限上下游，task 私有）

deliver 处理"我完成了，结果给你"；kb 处理"把这个存起来，后面谁需要谁来读"。

### 存什么

agent 通过 kb 存两类内容：
1. **任务知识**：研究 agent 发现的代码库结构/约束、planner 的任务分解、项目上下文——不是投递给特定下游的，而是"谁需要谁来读"
2. **中间产物索引**：编码 agent 写的文件路径、测试结果摘要——供后续节点查阅，不是即时投递

### kb 持久化

当前 bot 用 SQLite 但应可灵活替换/拔插。当前先不打磨现有 kb 机制——先建轻量版（能 get/set），后续替换持久化后端。

**kb 功能当前完全不存在**（探索确认：不在当前分支、不在 git 历史、不在任何分支。现有"knowledge"只是 memory 系统的 Core Memory 层，是 agent 级 in-context 记忆，不是 task 级共享 KV）。从零构建。

### kb tool action 设计

两 action（upsert 语义）：

- `get(key)` — 读单个 key
- `set(key, value)` — 写（insert + update 合并，upsert 语义）

不做 insert/update 分离——agent 不需要先 get 判断存在性；upsert 简单可预测。不做 delete——task kb 是图运行期间的累积知识，图完成后 lifecycle 清理。

### 归属

| 层 | 内容 | 归属 |
|----|------|------|
| kb 持久化 | per-task 分区 KV（SQLite 默认，可替换） | **bot_project**（轻量实现，不做框架 ABC） |
| kb tool（`task_kb` tool） | agent 调用的 tool，action=get/set | **bot_project**（tool 注册，像 ExperienceTool） |
| `modexctl kb --by-task` CLI | 外部 agent 用，bool 开关，taskId 从 env 读 | **bot_project** |
| taskId 注入 | AgentNode context_factory 从 GraphContext 拿 graph_instance_id；`MODEX_TASK_ID` env 注入（基础设施已有，当前永远 None，待接线） | **modex_agent**（已有字段+env 注入，需接线） |

**不做框架 ABC**——kb 是 bot 业务功能，不是框架抽象。不引入 `TaskKvStore` ABC 到 modex_agent。后续如果需要可替换持久化后端，在 bot 层用 ABC 隔离即可。

### 已有可复用基础设施

- `ExternalEnvSpec.task_id` 字段（`src/modex_agent/agents/external/types.py:174`）——已定义，默认 None
- `MODEX_TASK_ID` env 注入（`env_builder.py:90-91`）——已实现，但 task_id 永远 None
- `graph_instance_id`（Snowflake ID）——taskId 的值来源，ticket 05 决议 `taskId = str(graph_instance_id)`
- `DeliverStore` 的三档模式（Null/InMemory/Sqlite）——可作 kb 持久化设计参考

### 优先级

先完成 ticket 03（bot 装配拓扑）确定 GraphOrchestrator 怎么部署——taskId 注入路径依赖装配方式。kb 实现排在 03 之后。

### 不做什么

- 不做向量检索/embedding/RAG——本期是结构化 KV
- 不做 KnowledgeBase ABC——是 modexctl 的 CLI 命令 + tool
- 不做框架级 TaskKvStore ABC——kb 是 bot 业务功能
- 不做 delete action
- 不做 node 级状态抽象——kb 是 task 级共享

### 待确认

- **kb 功能是否已在其他环境实现**：用户可能在另一台环境有 kb 实现（git 历史未合入）。后续换环境查找。如果没有再从零实现。不阻塞图调度 agent 设计——kb 是 bot 业务层功能，框架层设计（deliver/taskId 注入/AgentNode）不依赖 kb 的具体实现。
