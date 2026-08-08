# 01 — 预重构:移除 route_fn、变更 Command.goto 类型、删除 pending 队列

**What to build:**

将 `modex_graph` 的路由模型从五级优先级链清理为两层模型,为 ParallelScheduler 铺路。移除 `route_fn` 条件边机制(`add_conditional_edges` 方法、`ConditionalEdge` 数据类、`CompiledGraph.conditional_for` 查找)。变更 `Command.goto` 的 Pydantic 字段类型,删除 `list[str]` 形式。删除 `GraphEngine._resolve_next` 中的 `pending: list[str]` 顺序队列。将现有使用 `route_fn` 的测试迁移到 `transition` + 静态边模式。新增架构守卫测试验证删除。

**Blocked by:** None — can start immediately

**Status:** completed

- [x] `add_conditional_edges` 方法从 `Graph` 类移除;调用时抛出明确的 `AttributeError` 或 `RuntimeError`,错误信息引导用户改用 `transition` + 静态边
- [x] `ConditionalEdge` 数据类从 `graph.py` 删除;`CompiledGraph.conditional_edges` 字段删除;`CompiledGraph.conditional_for` 方法删除
- [x] `Command.goto` 字段类型从 `str | list[str] | list[Task] | None` 变为 `str | list[Task] | None`;Pydantic `ConfigDict(frozen=True, extra="forbid")` 校验拒绝 `list[str]` 值,报错信息引导用户改用 `Task(node="X", state=None)`
- [x] `GraphEngine._resolve_next` 中的 `pending: list[str]` 队列逻辑删除;`list[str]` 分支移除(如果运行时遇到 `list[str]` 值,抛 `RoutingError` 兜底)
- [x] `tests/unit/modex_graph/test_engine_topologies.py` 中使用 `add_conditional_edges` 的 4 个测试迁移为 `transition` + 静态边模式(节点返回 `NodeResult(transition="high")`,边 `add_edge("decide", "high", reason="high")`)
- [x] `tests/unit/modex_graph/test_routing.py` 中 `route_fn` 相关测试删除;`Command(goto=list[str])` 测试改为期望 Pydantic `ValidationError`
- [x] 新增 `tests/architecture/test_no_route_fn.py`:AST 守卫验证 `add_conditional_edges` / `ConditionalEdge` / `conditional_for` 在 `src/modex_graph/` 中不存在
- [x] 所有现有测试在默认 `LinearScheduler` 下绿色(`pytest tests/unit/modex_graph/ -v`)
- [x] ReAct 相关测试不受影响(`pytest tests/unit/agents/react/ -v`)
- [x] graph_patterns 测试不受影响(`pytest tests/unit/examples/graph_patterns/ -v`)
- [x] 代码中无硬编码字符串——所有路由相关的字符串值通过 `StrEnum`(如 `ReActReason`)或用户自定义的 transition 字符串传递,框架内部不引入新的硬编码字符串
