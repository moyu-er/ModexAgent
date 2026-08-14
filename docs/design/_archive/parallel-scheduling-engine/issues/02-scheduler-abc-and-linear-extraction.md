# 02 — Scheduler ABC + LinearScheduler 提取

**What to build:**

引入 `Scheduler` ABC 作为 `GraphEngine` 的可插拔调度策略抽象层,将当前 `GraphEngine` 的顺序执行逻辑原样提取为 `LinearScheduler` 实现。`GraphEngine` 变为委托入口,根据 `CompiledGraph` 携带的 `SchedulerKind` 选择调度器。`Graph.compile()` 新增 `scheduler` 参数(默认 `LINEAR`)。零行为变更——所有现有图在默认调度器下行为与之前完全一致。

**Blocked by:** 01

**Status:** completed

- [x] `SchedulerKind`(`StrEnum`)定义:值为 `LINEAR` / `PARALLEL`,放在 `modex_graph` 的枚举模块中(不硬编码字符串)
- [x] `Scheduler` ABC 定义:声明 `run_async(ctx) -> S` 和 `run(ctx) -> S` 两个方法签名(与当前 `GraphEngine` 的公共入口一致),接收 `CompiledGraph` 和 `GraphContext`
- [x] `LinearScheduler` 实现:将 `GraphEngine.run_async` / `run` / `_resolve_next` / `_execute_task` 的逻辑原样搬入,行为不变
- [x] `GraphEngine` 变为委托层:构造时从 `CompiledGraph.scheduler` 读取 `SchedulerKind`,选择对应 `Scheduler` 实现;`run_async` / `run` 委托给选中的 Scheduler
- [x] `Graph.compile()` 新增 `scheduler: SchedulerKind = SchedulerKind.LINEAR` 参数;`CompiledGraph` 新增 `scheduler: SchedulerKind` 字段(`frozen=True` dataclass)
- [x] `Graph.compile()` 的 `scheduler` 参数值通过枚举传入,不接受裸字符串
- [x] `modex_graph.__init__` 导出 `Scheduler`、`LinearScheduler`、`SchedulerKind`
- [x] `GraphContext.dispatch` 方法存根:存在但检查 scheduler kind,在 `LinearScheduler` 下抛 `RuntimeError("dispatch is only available under ParallelScheduler")`
- [x] 所有现有测试在默认 `LINEAR` 调度器下绿色,零行为变更
- [x] 新增测试:`Graph.compile(scheduler=SchedulerKind.LINEAR)` 构建的图行为与不传 `scheduler` 参数完全一致
- [x] 架构守卫:验证 `Scheduler` 是 ABC(非 Protocol),`LinearScheduler` 继承 `Scheduler`
