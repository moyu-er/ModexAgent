# 07 — 终止条件 + compile 校验

**What to build:**

实现 `GraphNode.END` 哨兵的 `ON_ALL_PREDS` 语义(所有激活 END 上游完成后触发结束)。实现 ready 为空 + active 为空即终止。实现 `Graph.compile(scheduler="parallel")` 的 START 可达性校验和 END 可达性校验。处理有出边但不 dispatch 的静默跳过场景。

**Blocked by:** 06

**Status:** completed

- [x] `GraphNode.END` 哨兵不创建实例、不执行。调度器维护 `end_sources: set[str]`(dispatch 给 END 的 source 实例 ID 集合)
- [x] END 的 `ON_ALL_PREDS` 语义:所有 `end_sources` 中的实例都 COMPLETED 后,图终止。一个分支走到 END 不终止图——等其他分支也完成
- [x] 终止判断:`ready` 为空 AND `active`(PENDING∪READY∪RUNNING)为空 → 图终止。这覆盖 END 的 ON_ALL_PREDS 完成,也覆盖"所有节点都完成了,没有 dispatch 给 END"的场景
- [x] 有出边但不 dispatch 的节点:合法行为。其下游保持 DORMANT,不影响终止判断。compile 不报错
- [x] `Graph.compile(scheduler=SchedulerKind.PARALLEL)` 新增 START 可达性校验:从 entry_node 做 BFS,所有注册节点必须可达。不可达节点抛 `RoutingError`,错误信息列出不可达节点名
- [x] `Graph.compile(scheduler=SchedulerKind.PARALLEL)` 新增 END 可达性校验:对每个节点做反向 BFS(沿入边),判断是否能到达 `GraphNode.END`。不可达 END 的节点抛 `RoutingError`,错误信息列出节点名
- [x] START/END 可达性校验仅在 `scheduler=PARALLEL` 时执行;`LINEAR` 模式不校验(保持现有行为)
- [x] 测试:多分支都走向 END → 所有分支完成后图终止
- [x] 测试:一个分支走向 END,另一个分支走向无出边的普通节点 X → X 完成后 ready 变空,END 的 source 也就绪 → 图终止
- [x] 测试:有出边但不 dispatch 的节点 → 下游 DORMANT,图正常终止
- [x] 测试:compile 时存在从 START 不可达的节点 → `RoutingError`
- [x] 测试:compile 时存在无法到达 END 的节点(旁挂节点)→ `RoutingError`
- [x] 测试:`LINEAR` 模式下不执行 START/END 可达性校验(现有行为不变)
- [x] 测试:ReAct 4 节点图在 `PARALLEL` 模式下通过 START/END 可达性校验
- [x] 可达性校验使用 `CompiledGraph` 的边拓扑数据结构,不依赖字符串硬编码;校验结果通过结构化异常(`RoutingError` 携带节点名列表)报告
