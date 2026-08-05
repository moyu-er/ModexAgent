# LiveGraphEngineController 与 orchestrator GraphDrained 映射

Status: triage:ready-for-agent
Blocked by: 34
Design: `../external-control.md` §5

## Context

`GraphControlService` 的 pause/stop 命令目前只写 `GraphInstanceStore` 状态 + 调 stub（`InMemoryGraphEngineController.pause()` 只设 bool，调度循环无感知）。本 ticket 让控制命令真正作用到运行中的调度循环，并把 `GraphDrained` 上抛映射为正确的实例状态。

## Tasks

1. **`src/modex_agent/control/graph_control.py` 新增 `LiveGraphEngineController`**：注册时持有运行实例的 `ctx.control`（`GraphRunControl`）引用；`pause()` = `control.request_pause(reason)` + 唤醒戳；`stop()` = `control.request_stop(reason)` + 唤醒戳。
2. **注册/注销时机**：`GraphOrchestrator._execute` 构造 `GraphContext` 后，把 `ctx.control` 包装为 `LiveGraphEngineController` 注册进 `GraphControlService._engines`；`_execute` 结束（含所有异常路径）时注销（防止向已结束的 run 发命令）。
3. **orchestrator 异常映射**：`_execute` 增加 `except GraphDrained` 分支——识别为预期内退出，**不写状态**（PAUSED/STOPPED 已由 `GraphControlService._pause`/`_stop` 先行写入），不 re-raise 为引擎错误。与既有 `except GraphInterrupt → PAUSED` 分支并列。
4. **`deliver_to_node` 唤醒**：`GraphControlService.deliver_to_node` 在现有 `route_deliver` 之后，若目标实例在 `_active_instances`（运行中），调对应 controller 的 `notify_deliver(target)`；非运行实例保持现状（deliver 落库，恢复时消费）。
5. 保留 `InMemoryGraphEngineController` 作为测试/无控制场景的 stub 实现（不删除，缩小为测试工具）。

## MUST NOT

- 不在 orchestrator 的 `GraphDrained` 分支里再写一次 PAUSED/STOPPED（双重写入违反单一写入路径；control service 已写）。
- 不给 pause/stop 增加等待在途任务收尾的逻辑（决议：立即取消）。
- 不引入跨进程通道（Redis 等），单进程句柄先行。

## Acceptance

- 单元测试：对运行中实例发 PAUSE_GRAPH → 调度循环退出、实例状态 PAUSED、controller 已注销；之后 resume 可正常恢复（结合 SQLite coordinator）。
- 单元测试：STOP_GRAPH → 实例状态 STOPPED 且后续 `resume` 被拒（配合 ticket 37）。
- 单元测试：运行中 `deliver_to_node` 后目标节点在该 run 内消费到新 deliver。
- `pytest tests/unit/modex_agent/control/` 相关测试全绿，`ruff` + `mypy` 通过。
