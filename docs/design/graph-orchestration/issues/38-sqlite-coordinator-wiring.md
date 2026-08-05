# SQLite CoordinatorFactory 装配与 orchestrator 生产接线

Status: triage:ready-for-agent
Blocked by: 37
Design: `../external-control.md` §9、§10；`../distributed-persistence.md` §7、§12

## Context

三种 SQLite store（`SqliteGraphInstanceStore` / `SqliteNodeStateStore` / `SqliteDeliverStore`）都已实现，但 `GraphOrchestrator` 默认注入 `NullCoordinatorFactory`，且全项目无生产装配点——持久化恢复能力已建成却不可用。本 ticket 提供框架侧的 SQLite 装配件与业务层使用范式。**恢复扫描器（定时调 `recover_crashed`）是业务层职责**，本 ticket 只交付可复用的参考实现（放 examples），框架不加定时器。

## Tasks

1. **SQLite 装配工具**：在合适的位置（候选：`src/modex_agent/orchestration/` 或复用现有 workspace DB 装配处）提供 `SqliteCoordinatorFactory(CoordinatorFactory)`——构造时接收 caller-owned `sqlite3.Connection`（workspace DB `<workspace>/.modex/state.db`），`create()` 内装配 `SqliteNodeStateStore(graph_instance_id)` + `SqliteDeliverStoreFactory`，`instance_store` 用调用方传入的（契约见 distributed-persistence.md §7.1）。先查现有代码是否已有等价物（`runtime/services.py` 等），有则收敛复用，不造第二份。
2. **GraphInstanceStore 生产接线**：orchestrator 生产路径用 `SqliteGraphInstanceStore`（共享同一 connection），替换默认 Null/Memory。
3. **业务层扫描器参考实现**：在 `examples/`（候选 `examples/bot_project/` 或 `examples/graph_patterns/`）提供恢复扫描的参考装配：启动时 + 定时调 `orchestrator.recover_crashed()`；演示业务侧重试预算（自行计数、超预算经 `update_status` 标 FAILED）。
4. **文档**：`external-control.md` §10 的使用范式补一段最小装配示例（连接创建 → store 装配 → orchestrator 注入 → 扫描循环）。

## MUST NOT

- 不在框架层加定时器/后台任务（扫描时机归业务层）。
- 不在 `GraphMetadata` 加 `recovery_attempts` 等字段（预算归业务层）。
- 不让 store 关闭或拥有 connection（caller-owned 契约，distributed-persistence.md §12.1）。
- 不改 ReActAgent per-turn 路径（继续用 `create_null_coordinator`）。

## Acceptance

- 集成测试：SQLite 档下 `create_and_run` → 进程级崩溃模拟（kill 执行协程 + 重建全部对象，复用既有 E2E 模式）→ `recover_crashed` → 图从断点继续并完成。
- 参考示例可运行：`examples/` 下扫描器示例的 smoke 测试通过。
- `pytest tests/integration/graph_orchestration/ -v -m integration` 全绿，`ruff` + `mypy` 通过。
