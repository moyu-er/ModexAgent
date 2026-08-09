# 图编排 Backlog（遗留与未排期事项）

Status: living（2026-08-05 盘点自 `issues/history/` 全部 ticket + `PRD.md` fog 清单，逐项对照代码验证）
用途：记录"历史提到过、当前设计（`external-control.md` + `distributed-persistence.md`）未覆盖、代码未实现"的事项，供后续排期设计。每项含出处与代码验证状态。

与本期设计的关系标注：**无关** / **部分相关** / **已被本期设计覆盖**。

## 高优先级（阻塞端到端可用）

### BL-13 bot 图工厂与 pool config 集成

- 出处：`issues/history/10-graph-lifecycle-management.md` §3.6；`issues/history/wayfinder-map.md` L38
- 内容：bot 启动后加载 GraphSpec → 编译 → 实例化 → 注入依赖 → 执行 → 提供外部控制接口。框架能力已就绪，但 `examples/bot_project` 无 `GraphOrchestrator` / `CoordinatorFactory` 任何装配（grep 零匹配）。
- 关系：**部分被本期覆盖**（ticket 38 交付 SQLite factory 装配件与扫描器参考实现），但 bot 层的图工厂、pool 关联、REST/命令暴露仍是业务侧实现缺口。
- 备注：端到端"用起来"的最后一块拼图，建议紧随 ticket 34-39 之后排期。

## 中优先级（功能缺口）

### BL-18 modexctl deliver --help 缺少动态目标描述

- 出处：2026-08-09 graph agent context injection 设计审查
- 内容：`modexctl deliver` 命令（`examples/bot_project/bot/cli/modexctl/commands/deliver.py`）的 `--help` 输出是静态 typer Option help 文本，仅描述 `--node-name`、`--content`、`--workspace`、`--graph-instance-id` 参数。缺少 `GraphDeliverTool.description`（`src/modex_agent/tools/graph_deliver.py:174-193`）中的动态内容：当前节点名、下游目标列表、每个 target 的 description、deliver 引导语。
- 影响：external agent（Pi/OpenCode CLI）在 graph 节点中通过 `modexctl deliver` 而非 tool call 投递结果时，无法从 `--help` 得知有哪些合法 target 及每个 target 期望什么内容——native agent 的 deliver tool 动态描述对外部 agent 不可见。
- 关系：**部分相关**。`GraphWorkflowProvider`（system prompt 注入）对 external agent 不可见（external agent 不读 system prompt pipeline），所以 deliver 语义引导需要通过 CLI `--help` 或 `current_input` / `AGENTS.md` runtime block 补齐。
- 建议：`modexctl deliver --help` 或 `modexctl deliver --list-targets` 动态查询 graph 实例拓扑，输出当前节点可投递的下游目标 + 每个 target 的 description（与 `GraphDeliverTool.description` 收敛到同一信息源 `GraphDeliverTargetStore.list()`）。

### BL-01 长任务节点超时与取消机制

- 出处：`issues/history/07-long-running-node-execution.md` §5
- 内容：`NodeSpec.config` 声明 `timeout_seconds`，超时触发异常控制链。当前 `NodeSpec`（`spec.py`）无 timeout 字段，`node.py` 的 `max_retry` 是未投递重试上限而非执行超时。
- 关系：**无关**。本期状态机（CRASHED 级联）可承载超时后的状态转换，但超时触发机制未覆盖。
- 场景：ExternalAgent 长时执行（ADR-0022）无超时保护。

### BL-10 子图（GraphAsNode）嵌套恢复

- 出处：`issues/history/04-graph-nesting-execution-model.md`；`distributed-persistence.md` §1.1
- 内容：`GraphMetadata` 有 `parent_instance_id`/`parent_node` 字段（嵌套原语存在），但 `GraphAsNode.execute` 只是 `await compiled.execute(ctx)` 的简单 wrapper——不创建子 GraphInstance、不挂独立 coordinator，子图 node invocation 写入**父图**的 `node_states`，崩溃恢复无法区分父子边界。
- 关系：**部分相关但未覆盖**。本期恢复入口集推导在父子共享 `graph_instance_id` 时能工作；若子图用独立 instance id，跨层级联恢复语义未设计。
- 场景：图套图是 PRD 核心模式，当前"能跑但恢复语义不完整"。

### BL-12 图编排 REST/CLI 端点

- 出处：`issues/history/wayfinder-map.md` L41；`issues/history/distributed-persistence-design.md` §2.4
- 内容：`coordinator.get_graph_state()` 已存在，但无 REST/CLI 暴露（`GET /api/graph/{id}/state` 等）；pause/stop/resume/deliver 命令同样无端点。
- 关系：**部分相关**。本期控制面（GraphRunControl）落地后需要端点才能被外部调用——与 BL-13 同属"最后一层接线"。

## 低优先级（历史明确延后 / fog）

| # | 事项 | 出处 | 状态与备注 |
|---|------|------|-----------|
| BL-02 | 预定义拓扑模板（Pipeline/Star/Supervisor/Swarm） | `history/09` | 决议"ROI 低"，deliver/submit 已能直接构造大部分模式 |
| BL-03 | 共享知识库接口（`modexctl kb --by-task`） | `history/06` | 业务功能增强，非框架级 |
| BL-04 | hook 主动检测"未投递"（after_dispatch 事件） | `history/03` §待办 | `max_retry` 已覆盖核心场景；需新增 `after_dispatch`/`delivery_outcome` hook 点 |
| BL-05 | 图级 MVCC 轮次 | `history/10` §待办 | 明确"优先级低，暂不设计"；当前共享 state + full snapshot 无此概念 |
| BL-09 | taskId 可观测性贯穿（trace/span 按 graph_instance_id 串联） | `PRD.md` L67 | ID 定义已有，观测链路未建 |
| BL-11 | ON_RECEIVE 串行门崩溃后队列顺序丢失 | `history/31`、`distributed-persistence.md` §11.2 | 已知限制：deliver 不丢（at-least-once 覆盖），但内存 FIFO 顺序不保留。默认 ON_ALL_PREDS 规避 |
| BL-14 | `ctx.fork()` 死 API 残留 | `history/33`、`distributed-persistence.md` §15 | scheduler 已不调用，保留可能误导使用者；评估移除或加 deprecation |
| BL-17 | 自环节点（A→A）调度验证 | `history/IMPLEMENTATION-PLAN-V2.md` §10 | 机制应支持，缺专门测试（测试缺口，非设计缺口） |

## 远期（PRD out of scope）

| # | 事项 | 出处 |
|---|------|------|
| BL-06 | AdaptiveNode / LLM 自主生成图 | `PRD.md` L66/L81 |
| BL-07 | 动态图拓扑 v2（运行时修改已编译图） | `PRD.md` L68 |
| BL-08 | 知识库作为图复用仓库（wGraph） | `PRD.md` L69 |

## 本期设计已覆盖（确认项，非 backlog）

- `GraphDrained` 从不抛出 → ticket 34 激活（`external-control.md` §3-4）
- `LiveGraphEngineController` stub → ticket 35（§5）
- LinearScheduler 崩溃恢复重复执行已完成节点 → ticket 36（§7）
- STOPPED/PAUSED 语义含混 → ticket 37（§2）
- SQLite 持久化无生产装配 → ticket 38（§9-10）
- 恢复重叠场景（A 重跑 + B 持旧 deliver）测试空白 → ticket 39 钉为 at-least-once 契约（§8）
- `NodeInstance.upstream_payloads` vestigial 字段 → 已在 `distributed-persistence.md` §13.5 标注
