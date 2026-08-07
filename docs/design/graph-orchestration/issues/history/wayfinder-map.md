# 分布式持久化设计决策闭环

Status: closed — archived to `history/`. All tickets closed. Implementation complete. See `distributed-persistence.md` for the authoritative design.

## Destination

解决分布式持久化层的全部待定设计决策，产出更新后的 `distributed-persistence.md` + 实现计划。核心转折：经多轮 explore + Oracle 评估确认，fork-merge 模式及其整棵依赖树（channel / declarative delta / conflict detection / DispatchStore / SUPERSEDED 两阶段 rebuild / state_factory / NodeResult）对 agent 框架不必要。移除后回归三层持久化本质：GraphInstanceStore + NodeStateStore(invocation 版本链 + lifecycle) + DeliverStore，加共享 state + imperative 写 + full snapshot 持久化 + 单次 recovery。Node.run() 通过 `ctx.node_state_store` 自管理生命周期/状态/故障恢复。设计决策完成后交给实现阶段。

## Notes

- **Domain**: modex_graph 分布式持久化层 + modex_agent orchestration/control 接线层
- **Skills every session should consult**: `codebase-design`（deep module 设计词汇）、`domain-modeling`（领域术语）、`grill-with-docs`（决策时创建 ADR）
- **权威设计文档**: 当前设计权威 = 本目录 `issues/` 下的已关闭 ticket + 本 map。`distributed-persistence.md` 描述旧设计（fork-merge + channel 体系），已标记 superseded——将在 ticket 28 阶段 D step 20 按新设计重写（三层持久化 + 共享 state + full snapshot），重写后恢复权威地位。
- **设计决策追溯**: `docs/design/graph-orchestration/issues/history/distributed-persistence-design.md`（1453 行，原始设计 + 22 项检视问题 + F1-F11 修复）
- **规则**: convergence rule 1（收敛而非新增并行路径）；type-safety rules（ABCs, frozen Pydantic, no Any）；architecture rules（deep modules, deletion test）；modex_graph 不能 import modex_agent（架构守卫测试）
- **产出约定**: 新建文档全部放到 `docs/design/graph-orchestration/` 统一管理

## Decisions so far

- [Metadata store 双权威收敛](22-metadata-store-convergence.md) — GraphInstanceStore 吸收全字段保真，GraphMetadataStore 删除，status 写入收敛到单路径，coordinator 持有 _instance_store。经 ticket 32 修订：bookkeeping 四字段为运行时视图不持久化，`bookkeeping_json` 列作废，最终 schema 纯列存储，GraphMetadata 修剪 4 字段。
- [LinearScheduler recovery 契约](24-linearscheduler-recovery-contract.md) — LinearScheduler 加 load_for_recovery + state 恢复。recovery 是 coordinator 职责 + node 幂等。两种 scheduler 都支持 recovery。
- [Fork-merge 依赖链移除](33-fork-merge-removal.md) — ✅ CLOSED。移除 fork-merge + channel + declarative delta + conflict detection + DispatchStore + state_factory + NodeResult + SUPERSEDED。保留 ParallelScheduler 并发调度核心 + 三层持久化 + 共享 state + imperative 写。`complete_invocation` 留在 Node.run() 内部（保持自包含生命周期），只改传参从 delta 到 full snapshot。`GraphContext.fork()` 保留原样（scheduler 停止调用）。
- [rebuild_main_state 简化](26-rebuild-main-state-semantics.md) — ✅ CLOSED。移除 SUPERSEDED 两阶段 apply。recovery 取 `max(updated_at)` 中的 `{COMPLETED, suspended RUNNING}` 单条最新 snapshot。排序用 `updated_at`（完成时间），不是 `invocation_id`（开始时间）。
- [Node 业务状态持久化](23-node-business-state-persistence.md) — ✅ CLOSED。收敛到 `NodeStateStore`（正确设计），移除 `NodeState`（混乱叠加）。lifecycle 方法移入 store，coordinator 退出 lifecycle 路径，node 通过 `ctx.node_state_store` 自管理生命周期/状态/故障恢复。一个 graph instance 一个 store 实例。Null/InMemory/Sqlite 三种持久化策略（in-memory 允许不用故障恢复）。移除 PENDING status。
- [Coordinator 策略注入](29-coordinator-strategy-injection.md) — ✅ CLOSED。硬约束：持久化定义归业务层，框架只操作内存对象。`CoordinatorFactory` ABC（`create(graph_instance_id, instance_store)`）注入 `GraphOrchestrator.__init__`（默认 Null），create_and_run 与 recovery 共用单注入点。store 构造契约收敛为 caller-owned 连接（store 永不关闭）。线程契约：同步方法只在 event-loop 线程被调。业务层拍板 DB 拓扑（参考实现：`<workspace>/.modex/graph.db`）。
- [状态机 CAS + 线程安全](31-state-machine-cas-thread-safety.md) — ✅ CLOSED。**修订**原「不加 scheduler gate」裁决：ON_RECEIVE 加 per-node 串行门（同 node 有 RUNNING instance 时 dispatch 排队，instance 完成后排水；N dispatch → N 串行执行，语义保留），「1-active-invocation」在触发条件层成为结构不变量。ON_RECEIVE 标记谨慎使用 + TODO。CAS = WHERE-clause 条件更新（终态结构性不可变），失败语义分层：complete/suspend/cancel 严格抛 `InvocationStateError`，crash/finalize/orphan 清理幂等容忍。线程安全 = ticket 29 契约文档化到 ABC。
- [Scheduler bookkeeping 持久化](32-scheduler-bookkeeping-persistence.md) — ✅ CLOSED。**零持久化：三层体系（GraphInstance + NodeState + Deliver）已足够**。四字段全是运行时视图：pending_dispatches/activated_sources 从 deliver 层 PENDING 记录重建，iteration_count 从 node 层 COMPLETED 计数派生，instance_seq 重置（纯内存临时量）。无第四类信息需要持久化。级联：ticket 22 的 bookkeeping_json 作废、GraphMetadata 修剪 4 字段。recovery 重建是唯一新增代码，顺带修复孤儿 PENDING delivers 现存 bug。

## Active tickets

- [expand-contract 收尾](28-expand-contract-cleanup-plan.md) — 执行计划（非设计 ticket，2026-08-04 定性）：唯一剩余工作项。已整合全部闭环 ticket（22/23/24/26/29/31/32/33）的实现步骤，按 A（纯移除）→ B（store 收敛）→ C（框架接线）→ D（收尾）四阶段排序。实现时逐步验证。

**设计决策全部闭环。** 本 map 的 Destination 已达成——剩余为实现阶段（ticket 28）与 fog 项。

## Not yet specified

- **Node 业务状态与 GraphInterrupt suspend/resume 的交互**: ✅ 已明确 — full snapshot 覆盖，无需独立 snapshot。详见 [ticket 23](23-node-business-state-persistence.md)。
- **Graph orchestration 与 bot pool config 的集成**: [ticket 29](29-coordinator-strategy-injection.md) 已裁决边界（业务层装配 CoordinatorFactory，框架只定义契约）。剩余为业务侧接线实现（装配点、backend 分支），非设计决策。
- **LiveGraphEngineController 设计**: pause/stop/resume 运行中 engine 的机制。当前只有 `InMemoryGraphEngineController`（recording stub）。
- **是否需要新 ADR**: ticket 33 的移除决策涉及大规模架构变更。应创建新 ADR 或大幅重写 `distributed-persistence.md`。
- **前端查询接口 (REST/CLI)**: `GraphStateSnapshot` 查询 API 设计。实现待前端集成阶段。本 map 不涉及。
- **ON_RECEIVE 语义完善**: [ticket 31](31-state-machine-cas-thread-safety.md) 裁决保留 ON_RECEIVE + per-node 串行门，但标记谨慎使用。TODO：与 recovery 的交互、排队 dispatch 是否需持久化（crash 时队列丢失）等留待后续考虑。
- **`distributed-persistence.md` 重写**: ✅ 已排期 — 纳入 ticket 28 阶段 D step 20（重写为新设计权威文档 + ticket 文件移入 history/）。不再是 fog。

## Out of scope

- **实际代码实现**: 本 map 只产出设计决策，不写实现代码。
- **AdaptiveNode / LLM 自主生成图**: PRD out of scope，远期功能。
- **KnowledgeBase RAG**: PRD out of scope，业务功能增强。
- **图级 MVCC 轮次**: PRD not yet specified，优先级低，暂不设计。
- **node 幂等设计扩展**: 框架提供 `invocation_id` + `version` + `parent_version` 原语，node 自行实现幂等逻辑。这是设计语义，不是待办。
- **taskId 知识库接口**: PRD out of scope，业务功能增强。
- ~~ticket 25（complete 在 merge 之前）~~ — 已删除。没有 merge 就没有时序问题。
- ~~ticket 30（SUPERSEDED snapshot 优先级）~~ — 已删除。SUPERSEDED 移除（ticket 33/26）。

## Closing note

All tickets closed. Implementation complete. See `distributed-persistence.md` for the authoritative design.
