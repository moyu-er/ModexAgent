# 暂停/恢复/崩溃 E2E 测试套件

Status: triage:ready-for-agent
Blocked by: 34, 35, 36, 37
Design: `../external-control.md` 全文

## Context

ticket 34-37 各自带单元测试，本 ticket 补齐跨层 E2E：把"外部控制 + 崩溃恢复 + 持久化档位"的组合行为钉住，防止语义回归。同时把若干当前测试空白（恢复重叠场景、环形恢复）补为契约测试。

## Tasks

1. **暂停-取消-恢复闭环**（SQLite 档，Parallel 图）：运行中 pause → 在途节点被取消（invocation 落 CRASHED）、未启动节点不执行、实例 PAUSED → `resume(gid)` → 被取消节点重派、图跑到 COMPLETED。
2. **停止终态闭环**：运行中 stop → 实例 STOPPED → `resume` 拒绝；`recover_crashed` 不捡 STOPPED。
3. **进程级崩溃恢复**（SQLite 档）：执行中 kill（不 finalize）→ 孤儿 RUNNING → `recover_crashed` → 入口集 = 非终态节点 + PENDING deliver 目标 → 完成；断言已完成节点不重复执行（Linear + Parallel 各一组）。
4. **deliver 重叠契约测试**：源节点在 route_deliver 后 complete 前崩溃（fault-injection），恢复后断言目标消费到全部可消费 deliver（at-least-once，框架不去重）。
5. **环形图恢复**：ReAct 式 A→B→A 环，崩溃于第 N 轮，恢复后按版本链顶端判定，不从头重放已完成轮次。
6. **Null 档 fail-safe**：Null coordinator 下 `recover_crashed()` 返回 `[]`；`resume` 抛 `InstanceNotFoundError`；`load_for_recovery` 回退 fresh start 正常运行。
7. **InMemory 档降级**：进程内崩溃可恢复；验证二态 deliver 状态机下恢复语义与文档一致（`external-control.md` §9 矩阵）。

## MUST NOT

- 不为通过测试修改被测语义（测试钉的是 `external-control.md` 的契约）。
- 不用 MagicMock 替代真实 store（遵守 tests/AGENTS.md：用真实 SQLite/InMemory store，必要时 fault-injection 子类）。

## Acceptance

- 上述场景全部落在 `tests/integration/graph_orchestration/`（标记 `integration`），CI 可跑。
- 全量 `pytest tests/unit/modex_graph tests/integration/graph_orchestration` 绿，`ruff` + `mypy` 通过。
