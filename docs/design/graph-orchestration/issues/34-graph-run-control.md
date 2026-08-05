# GraphRunControl 与 GraphDrained 激活（调度器安全点）

Status: triage:ready-for-agent
Blocked by: none
Design: `../external-control.md` §3、§4

## Context

运行中的图收不到外部暂停/停止信号：`GraphControlService` 只写状态，调度循环无感知（`InMemoryGraphEngineController` 是 stub）。`GraphDrained(GraphBubbleUp)` 异常类已存在（`src/modex_graph/exceptions.py`）但从不抛出——本 ticket 激活它。

## Tasks

1. **`src/modex_graph/run_control.py`（新文件）**：实现 `GraphRunControl`——
   - `request_pause(reason: str)` / `request_stop(reason: str)`：单向置位，无锁（单属性写入），不可撤销；
   - 只读属性 `pause_requested` / `stop_requested` / `drain_reason`；
   - `notify_deliver(target: str)`：唤醒戳（见任务 4）；
   - `check()`：命中 pause/stop 标志时抛 `GraphDrained(reason)`。形状预留未来命令 deque 扩展（scheduler 调用点不变）。
2. **`GraphContext` 加 `control: GraphRunControl` 字段**：per-run 一个实例，默认自带；不改 `GraphEngine.run_async(ctx)` 签名。
3. **LinearScheduler 安全点**：`run_async` 的 `while` 循环顶部（执行下一节点前）调 `ctx.control.check()`。
4. **ParallelScheduler 安全点**：主循环 launch 新 READY 实例前调 `check()`；命中后停止 launch、`cancel()` 全部在途 task（`asyncio.gather(*running, return_exceptions=True)` 收尾）、抛 `GraphDrained`。`notify_deliver` 需能唤醒阻塞在 `asyncio.wait` 的主循环：control 持有对 `_wakeup` 事件的引用（scheduler 在 `run_async` 顶部注入），`notify_deliver` 时 set 它；主循环醒来后按目标节点 trigger 模式消化新到的 PENDING deliver（复用 `_handle_dispatch` 已有逻辑，不为唤醒另写路径）。
5. **在途节点状态落法验证**：被 cancel 的节点经 `finally: finalize_invocation` 落 CRASHED（`asyncio.CancelledError` 绕过所有 except）——补一个明确测试钉住该行为。

## MUST NOT

- 不改 `Scheduler` ABC（`check()` 是 scheduler 内部实现细节）。
- 不给 `GraphDrained` 拆分子类区分 pause/stop（目标状态由 control service 先行写入，异常只表达预期退出）。
- 不在 `modex_graph` 内 import `modex_agent` 的 control 层（依赖单向）。

## Acceptance

- 单元测试：Linear 图在两节点间收到 pause → 第二节点不执行，`GraphDrained` 抛出，已完成节点 COMPLETED + delivers promoted。
- 单元测试：Parallel 图 pause → 在途节点被取消且 invocation 落 CRASHED，未 launch 的 READY 实例不执行。
- 单元测试：`notify_deliver` 唤醒阻塞主循环，目标节点消费新 deliver。
- `pytest tests/unit/modex_graph/ -v` 全绿，`ruff check` + `mypy src/modex_graph` 通过。
