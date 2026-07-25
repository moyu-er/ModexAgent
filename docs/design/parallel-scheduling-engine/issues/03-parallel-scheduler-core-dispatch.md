# 03 — ParallelScheduler 最小可用:就绪队列 + dispatch 接口 + 单节点快路径

**What to build:**

实现 `ParallelScheduler` 的基本执行循环,使其能跑通最简单的线性图(`A→B→END`)。引入多实例模型(每次执行产生独立 `NodeInstance`)、节点状态机(`NodeInstanceStatus` 枚举)、`ctx.dispatch` 投递接口(`DispatchEvent` 结构体)、出边白名单校验、单节点快路径(无 fork 直接操作 main_state)。`max_iterations` 每实例计数。此阶段不实现 fork 隔离、触发模式、可达性判断、路由编译——节点必须手动调 `ctx.dispatch` 来路由。

**Blocked by:** 02

**Status:** completed

- [x] `NodeInstanceStatus`(`StrEnum`)定义:`DORMANT` / `PENDING` / `READY` / `RUNNING` / `COMPLETED`
- [x] `NodeInstance`(regular class,非 Pydantic——持有运行时状态 per rule 12)定义:字段 `instance_id: str`、`node_name: str`、`seq: int`、`status: NodeInstanceStatus`、`forked_state: S | None`。`instance_id` 格式为 `{node_name}#{seq}`,由全局计数器生成
- [x] `DispatchEvent`(`BaseModel`,`frozen=True`,`extra="forbid"`)定义:字段 `source_instance: str`、`target: str`、`payload: dict[str, Any] | None`
- [x] `ParallelScheduler` 实现基本循环:入口节点创建实例 `entry#0`(READY)→ 执行 → dispatch → 下游实例创建(PENDING→READY)→ 执行 → ... → ready 为空且 active 为空时终止
- [x] 单节点快路径:当 ready 集合只有一个实例且无 RUNNING 实例时,跳过 fork,直接操作 `main_state`
- [x] `GraphContext.dispatch(target: str, state_update: dict[str, Any] | None = None)` 实现:校验 `target` 在当前节点的出边目标集合内(白名单)→ 创建 `DispatchEvent` 记录 → 更新 target 实例状态机。dispatch 立即生效,不等 execute 返回
- [x] 出边白名单校验失败时抛 `RoutingError`,错误信息包含当前节点名和合法 target 列表
- [x] `max_iterations` 每实例执行计数 +1;超限抛 `GraphRecursionError`
- [x] `GraphNode.END` 的 dispatch 标记为终止信号:dispatch 给 END 时不创建实例,而是记录到 `end_sources` 集合
- [x] `modex_graph.__init__` 导出 `ParallelScheduler`、`NodeInstanceStatus`、`NodeInstance`、`DispatchEvent`
- [x] 测试:线性图 `A→B→END` 在 `scheduler="parallel"` 下通过手动 `ctx.dispatch` 正确执行,最终 state 正确
- [x] 测试:单节点快路径行为验证(无 fork 开销,直接操作 main_state)
- [x] 测试:`ctx.dispatch` 向无出边连接的节点投递时抛 `RoutingError`
- [x] 测试:`max_iterations` 在并行模式下正确计数
- [x] 测试:`LinearScheduler` 下调用 `ctx.dispatch` 抛 `RuntimeError`
- [x] 所有枚举值通过 `StrEnum` 定义,框架代码中不出现裸字符串("DORMANT" / "PENDING" 等)
