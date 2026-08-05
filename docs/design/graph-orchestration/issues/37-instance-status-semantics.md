# 图实例状态语义精化（STOPPED 终态化）

Status: triage:ready-for-agent
Blocked by: none
Design: `../external-control.md` §2

## Context

现行 `GraphRecoveryService.resume()` 同时接受 PAUSED 和 STOPPED（`graph_recovery.py`）。2026-08-05 决议：STOPPED 是人为终止的**终态**，与 COMPLETED/FAILED 同级；只有 PAUSED 可手动恢复。未捕获异常退出 → CRASHED（可重试）已是现状，无需改动；FAILED 由业务层经 `update_status` 写入，框架不加计数。

## Tasks

1. **`graph_recovery.py`**：`resume()` 状态校验从 `PAUSED or STOPPED` 改为仅 `PAUSED`；对 STOPPED 抛出明确的 `ValueError`（消息说明 STOPPED 是终态）。同步更新 docstring。
2. **`graph_control.py`**：核对 `_stop` 的注释与文档（STOPPED = 终态不可恢复）。
3. **文档联动**：`distributed-persistence.md` §10.3 的计划变更标注随实现落地（移除"计划变更"字样）；`external-control.md` §2 状态表为权威。
4. **测试更新**：现有断言"STOPPED 可 resume"的测试改为断言拒绝；新增 PAUSED→resume 正常、CRASHED→手动 resume 拒绝、COMPLETED/FAILED→拒绝的状态矩阵测试。

## MUST NOT

- 不给框架加重试计数/预算字段（`GraphMetadata` 保持 5 字段）。
- 不改 `recover_crashed()` 的扫描集合（CRASHED + 孤儿 RUNNING 已是正确语义：人为暂停不自动恢复）。
- 不新增 DRAINED 等状态枚举值。

## Acceptance

- 状态机矩阵测试全绿：resume 只认 PAUSED，其余五态各自拒绝路径明确。
- `pytest tests/unit/modex_agent/control/ tests/unit/modex_agent/orchestration/` 全绿，`ruff` + `mypy` 通过。
