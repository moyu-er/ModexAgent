# 06 — 触发模式 + 可达性就绪判断

**What to build:**

实现 `NodeTrigger` 枚举(`ON_ALL_PREDS` / `ON_RECEIVE`)和每节点可配置的触发模式。实现基于可达性的就绪判断:节点就绪需同时满足"至少一个激活上游已完成 dispatch"和"无活跃实例(PENDING∪READY∪RUNNING)能沿出边到达该节点"。实现 `ON_ALL_PREDS` 按 source 去重分组(每组每个 source 一个 dispatch,配对触发一次实例)。实现 `ON_RECEIVE` 每次 dispatch 触发新实例。条件分支跳过一臂不死锁 join。

**Blocked by:** 04, 05

**Status:** completed

- [x] `NodeTrigger`(`StrEnum`)定义:`ON_ALL_PREDS` / `ON_RECEIVE`
- [x] `Node` ABC 新增 `trigger: NodeTrigger = NodeTrigger.ON_ALL_PREDS` 属性(默认值,子类可覆盖)
- [x] `Graph.compile()` 新增 `default_trigger: NodeTrigger = NodeTrigger.ON_ALL_PREDS` 参数;`CompiledGraph` 新增 `default_trigger: NodeTrigger` 字段。节点未显式设置 trigger 时使用图级默认值
- [x] "激活上游"追踪:调度器维护每个节点的"已 dispatch 的 source 集合"。只有实际执行并 dispatch 的上游算激活;声明的入边但上游未被路由选中不算激活
- [x] 可达性 BFS 实现:`can_reach(active_instances: set[str], target: str) -> bool`——从所有 PENDING∪READY∪RUNNING 状态的实例出发,沿 `CompiledGraph` 的出边做 BFS,判断能否到达 target。保守策略:检查所有声明的出边,不考虑实际路由决策
- [x] `ON_ALL_PREDS` 就绪判断:target 节点的所有激活 source 都至少有一个 dispatch 到达 + 无活跃实例能到达 target → 就绪。创建一个实例,消费一组 dispatch(每个 source 一个)
- [x] `ON_ALL_PREDS` 分组:维护 per-target 的 pending dispatch 队列。每个 source 的 dispatch 追加到队列。当队列包含所有激活 source 的至少一个 dispatch 时,从每个 source 消费一个 dispatch 组成一组,触发一次实例
- [x] `ON_RECEIVE` 就绪判断:收到任一 dispatch + 无活跃实例能到达 target → 就绪。每次 dispatch 触发一个新实例
- [x] 条件分支跳过验证:A 有条件路由到 B 或 C,只选了 C(A→B 的边存在但 A 没 dispatch 给 B)→ D(入边 B→D, C→D,`ON_ALL_PREDS`)的激活 source 只有 C → C 完成后 D 就绪(B 永远 DORMANT,不算激活)
- [x] 长链不提前触发验证:A→[B,C], B→D, C→E→F→D。B 先完成 dispatch 给 D,但 E 还在跑(RUNNING)→ E 能到达 D(经 F)→ D 不就绪。E 完成 dispatch 给 F,F 完成 dispatch 给 D → 无活跃实例能到达 D → D 就绪
- [x] 测试:`ON_ALL_PREDS` 两上游都 dispatch 后触发一次实例
- [x] 测试:`ON_ALL_PREDS` 条件分支跳过一臂,join 不死锁
- [x] 测试:`ON_RECEIVE` 两上游先后 dispatch,触发两次实例
- [x] 测试:`ON_RECEIVE` 两上游几乎同时 dispatch,触发两次并发实例
- [x] 测试:长链(A→E→F→D)中 D 不提前触发
- [x] 测试:节点级 trigger 覆盖图级 default_trigger
- [x] 测试:循环自环(body→body,`transition="retry"`)每次执行产生新实例,不重置状态
- [x] 所有触发模式判断逻辑基于 `NodeTrigger` 枚举值,不硬编码字符串比较
- [x] 可达性 BFS 使用 `CompiledGraph` 的边拓扑,不依赖运行时字符串拼接
