# 共享恢复入口集推导 + LinearScheduler 恢复统一

Status: triage:ready-for-agent
Blocked by: none（注意与 ticket 34 同触 scheduler 文件，建议先后落地或同人完成）
Design: `../external-control.md` §7、§8

## Context

LinearScheduler 崩溃恢复只重建 state 后从 entry_node 重跑（`linear.py:80-86`），不跳过 COMPLETED 节点——崩溃后已完成节点被重复执行。ParallelScheduler 已有完整的入口集推导（`_redispatch_from_recovery` + `_rebuild_pending_from_delivers` + `_recheck_pending`）。本 ticket 把入口集推导收敛为两个 scheduler 共享的规则，Linear 崩溃恢复不再依赖 `resume_target`（它回归 HITL 本职）。

## 入口集推导规则（设计已定稿）

1. **主路径**：每节点取版本链顶端（`load_latest`）：CRASHED / 孤儿 RUNNING / suspended RUNNING → 重入候选；COMPLETED / CANCELED → 跳过；无记录 → 非候选（除非规则 2 命中）。
2. **稀有路径**：扫所有节点 deliver_store 的 PENDING deliver，目标节点纳入候选——即使从未有 invocation。**不做来源过滤、不做入度推断**（deliver 记录是"上游已提交"的唯一凭证，依据是 `submit()` 先于 `complete_invocation()` 的时序不变量）。
3. 入口集为空 = fresh start → entry_node。

## Tasks

1. **ParallelScheduler**：核对现有 `_restore_from_recovery` 与上述规则的一致性，补齐缺口（重点是规则 2 中"从未启动但持有 PENDING deliver 的节点"在 ON_ALL_PREDS 下经 `_recheck_pending` 触发门判定的路径测试）。
2. **LinearScheduler 恢复改造**：`run_async` 顶部 `load_for_recovery` 后，按入口集规则推导起点（拓扑序最早候选），从该节点开始顺序循环；无候选则从 entry_node 正常开始（现状行为，fresh start）。
3. **resume_target 回归 HITL 本职**：`GraphInterrupt` 挂起恢复路径不变（entry node 读 `state.resume_target` 路由）；崩溃恢复路径不再依赖它。更新 `linear.py` docstring 中"always starts from entry_node"的过时表述。
4. **时序不变量防回归测试**：构造"submit 后 complete 前崩溃"场景（可用 fault-injection store 包装），断言恢复后目标节点经 deliver 凭证被纳入入口集。
5. **at-least-once 契约测试**：源节点重跑 + 目标持有旧 PENDING deliver 时，断言目标消费到全部可消费 deliver（行为声明见 `external-control.md` §8），框架不去重。

## MUST NOT

- 不加 deliver 来源 invocation 状态过滤（设计明确否决，见 external-control.md §7）。
- 不入度推断"上游全完成则该执行"。
- 不改 `InvocationStatus` 枚举（READY/PENDING 只存在于 Parallel 运行时实例状态机，不持久化）。
- 不动 `Node.run()` 的 resume 检测与 suspend 路径。

## Acceptance

- Linear 链图 A→B→C：B 完成后崩溃，恢复只从 C 开始，A/B 不重复执行。
- Linear 图"上游完成+deliver 已落库、目标未启动即崩溃"场景：恢复后目标节点被执行。
- Parallel 既有恢复测试（`tests/unit/modex_graph/test_scheduler_recovery.py`）全绿无回归。
- 新增测试覆盖：环形图（ReAct 式 A→B→A）崩溃恢复按版本链顶端判定。
- `pytest tests/unit/modex_graph/ -v` 全绿，`ruff` + `mypy` 通过。
