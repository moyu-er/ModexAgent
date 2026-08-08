# 04 — 路由编译:transition/Command → dispatch + 多目标静态边

**What to build:**

让节点通过声明式 `NodeResult.transition` / `Command.goto` 表达路由,由 `ParallelScheduler` 自动编译为 `ctx.dispatch` 调用。支持同 reason 多条静态边全部触发(fan-out)。支持 `Command.goto=list[Task]` 并行多目标。节点可混用 `ctx.dispatch` 和 `NodeResult.transition`。节点不 dispatch 也不返回 transition 时静默跳过(合法行为)。fan-out 场景端到端验证。

**Blocked by:** 03

**Status:** completed

- [x] `CompiledGraph` 边查找方法更新:`next_nodes_by_transition(source, transition)` 返回 `list[str]`(所有 reason 匹配的 target,不是第一个);`default_edge_targets(source)` 返回 `list[str]`(所有 reason=None 的 target)
- [x] `ParallelScheduler` 在实例 `execute` 返回后,编译 `NodeResult`:
  - 有 `Command.goto`(str)→ dispatch 到该 target
  - 有 `Command.goto`(list[Task])→ 对每个 Task 调 dispatch;`Task.state=None` 标记为共享快照(此阶段暂不实现 fork,实际 fork 在 05 中实现;此处先记录 state 语义)
  - 有 `transition` 且匹配到静态边 → dispatch 到所有匹配 target
  - `transition=None` → dispatch 到所有默认边 target
  - 无 transition、无 Command、无手动 dispatch → 静默跳过(不报错)
- [x] `transition` 匹配静态边但无匹配且无默认边 → 抛 `RoutingError`
- [x] 混用场景:节点在 execute 内调了 `ctx.dispatch("X")`,返回 `NodeResult(transition="done")` 匹配到 Y → X 和 Y 都被 dispatch。两者独立,不互斥
- [x] `NodeResult.state_update` 作为 dispatch 载荷:transition/Command 编译的 dispatch 携带 `state_update` 作为 payload;手动 `ctx.dispatch` 的 payload 由调用者指定
- [x] 测试:`transition="success"` 匹配两条边(`A→B reason="success"` + `A→C reason="success"`)→ B 和 C 都被 dispatch 并执行
- [x] 测试:`Command(goto=[Task(node="B"), Task(node="C")])` → B 和 C 并行 dispatch
- [x] 测试:节点手动 `ctx.dispatch("D")` + 返回 `transition="done"` 匹配 E → D 和 E 都被 dispatch
- [x] 测试:节点不 dispatch 不返回 transition → 静默跳过,不报错,下游 DORMANT
- [x] 测试:`transition="nonexistent"` 无匹配无默认边 → `RoutingError`
- [x] 测试:fan-out + fan-in 场景(A→[B,C]→D,D `ON_ALL_PREDS`)端到端正确执行(此阶段 D 的触发模式暂用简单逻辑:收到所有入边 source 的 dispatch 后就绪;完整可达性判断在 06 实现)
- [x] 路由编译逻辑中不硬编码 transition 值——所有 transition 值来自用户代码或 `StrEnum`(如 `ReActReason`)
