# 08 — 并行错误处理

**What to build:**

实现并行执行下的错误处理:节点异常(非 `GraphBubbleUp`)取消所有并发实例并传播;`GraphInterrupt` 第一个抛出即传播,其他并发实例取消;`InvalidUpdateError` 作为 `GraphBubbleUp` 子类走同一传播路径。`before_node` / `after_node` 钩子并发调用,`ctx.emit` 并发调用,实现方保证安全。

**Blocked by:** 05

**Status:** completed

- [x] 节点 `execute` 抛出非 `GraphBubbleUp` 异常时:`asyncio.wait(FIRST_COMPLETED)` 行为——第一个异常取消所有正在运行的 Task,异常传播给 `GraphEngine.run_async` 的调用者
- [x] `GraphInterrupt` 抛出时:立即传播(不等其他并发实例完成),其他并发实例通过 `asyncio.cancel` 取消。与当前 `LinearScheduler` 行为一致(引擎不吞 `GraphBubbleUp`)
- [x] `InvalidUpdateError`(在 05 中实现为 `GraphBubbleUp` 子类)抛出时:走同一传播路径——立即传播,取消其他并发实例
- [x] `before_node` / `after_node` 并发调用:`ParallelScheduler` 在 `asyncio.create_task` 执行中对每个实例调用 `ctx.runtime.before_node` / `after_node`,实现方需保证并发安全
- [x] `ctx.emit` 并发调用:`GraphContext.emit` 当前实现是 `loop.create_task(runtime.emit(...))`(fire-and-forget),天然支持并发。验证在并行场景下不阻塞、不竞态
- [x] 审计 `ReactGraphRuntime` 的 `before_node` / `after_node` / `dispatch_hook` / `emit` 实现:检查是否有共享可变状态(计数器、列表等)。如有,加 `asyncio.Lock` 或改为原子操作。审计结果记录在 ticket comments 中
- [x] 测试:两个并发实例,其中一个抛 `RuntimeError` → 另一个被取消,`RuntimeError` 传播给调用者
- [x] 测试:两个并发实例,其中一个 `ctx.interrupt(value)` → `GraphInterrupt` 传播,另一个被取消
- [x] 测试:两个并发实例同时写同一 `LastValue` 字段 → `InvalidUpdateError` 传播,两个实例都终止
- [x] 测试:`before_node` / `after_node` 在并行场景下被并发调用,不崩溃(使用 `TrackingRuntime` 辅助类验证调用记录)
- [x] 测试:`ctx.emit` 在并行场景下并发调用,事件不丢失、不阻塞
- [x] 错误处理逻辑基于异常类型判断(`isinstance(exc, GraphBubbleUp)`),不硬编码异常类名字符串
