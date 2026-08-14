# 05 — Fork 状态隔离 + 多写检测

**What to build:**

实现并发执行时的状态隔离:每个并发实例 fork main_state 的深拷贝快照,实例完成后仅 `state_update` 通过 channel 语义合并回 main_state,命令式 mutation 不传播。实现 `LastValue` 多写检测(`InvalidUpdateError`)和 `ReducerChannel` 并发 fold。单节点快路径跳过 fork。

**Blocked by:** 03

**Status:** completed

- [x] `InvalidUpdateError(GraphBubbleUp)` 异常类定义在 `modex_graph.exceptions`,继承 `GraphBubbleUp`
- [x] `LastValue.update(values: list[T])` 在 `len(values) > 1` 时抛 `InvalidUpdateError`,错误信息包含字段名和写入数量
- [x] `LastValue.update` 的多写检测只在实际并发场景触发(单节点快路径下 `values` 长度始终为 1,不受影响)
- [x] `ParallelScheduler` fork 逻辑:当 ready 集合有多个实例或存在 RUNNING 实例时,每个 READY 实例在执行前 fork `main_state`(`model_copy(deep=True)`);实例的 `forked_state` 字段设为快照
- [x] `GraphContext.state` 在 fork 模式下指向 `forked_state`;在快路径下指向 `main_state`
- [x] 实例完成后,`NodeResult.state_update` 通过 `main_state.apply_state_update()` 合并回主状态(channel 语义);命令式 mutation(`ctx.state.x = y` 在 fork 上的修改)不传播
- [x] `ctx.dispatch` 的 payload(来自 `state_update` 或手动 `ctx.dispatch(state_update=...)`)在投递时合并到 main_state(通过 channel 语义),fork 中的实例看到的是 fork 时刻的快照
- [x] Generation-based conflict detection: `WriteConflictDetector.commit()` detects same-generation `LastValue` field collisions before merge
- [x] 单节点快路径(ready 只有 1 个且无 RUNNING):跳过 fork,直接操作 main_state,零拷贝开销
- [x] 测试:两个并发实例分别 `state_update={"count": 1}` 和 `state_update={"count": 2}` 到同一个 `LastValue` 字段 → `InvalidUpdateError`
- [x] 测试:两个并发实例分别 `state_update={"items": [1]}` 和 `state_update={"items": [2]}` 到 `ReducerChannel(reducer=operator.add)` → 最终 `items=[1,2]`
- [x] 测试:并发实例 A 做 `ctx.state.x = 999`(命令式),实例 B 做 `state_update={"x": 1}` → main_state 的 `x` 为 1(命令式 mutation 不传播)
- [x] 测试:单节点快路径下命令式 mutation 直接生效(因为操作的是 main_state 本身)
- [x] 测试:`InvalidUpdateError` 是 `GraphBubbleUp` 子类,可被 GraphBubbleUp except 捕获
- [x] 测试:map-reduce 模式(`Command(goto=[Task(node="worker", state=...)] * N)`)在 ParallelScheduler 下并行执行,ReducerChannel 正确 fold 所有 worker 贡献
- [x] `InvalidUpdateError` 的错误信息不硬编码字段名——从 channel 的 `_field_type` 或 field name 动态获取
