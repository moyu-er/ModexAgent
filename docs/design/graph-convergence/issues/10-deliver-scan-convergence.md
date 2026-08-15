# 10 — DeliverStore 重复扫描收敛

Status: closed
Labels: wayfinder:task
Assignee: GYT
Blocked-by: (无 — frontier)

## Question

当前对 deliver store 的 PENDING 扫描存在三处独立查询路径,行为正确但重复:

1. `ParallelScheduler._recheck_pending` — 每轮调度循环扫描所有节点 deliver store;
2. `ParallelScheduler._can_reach_active` 第三 BFS 源(2026-08-11 fan-in 修复)— 再次扫描各节点 PENDING delivers;
3. `bootstrap` — 启动/恢复时扫描(一次性,与前两者的关系需厘清)。

收敛目标:**不改变行为**的前提下,消除每轮循环中的重复 store 查询(如调度器维护单一 consumable 快照/增量失效,两处消费同一数据)。要求:

1. 先测量/推理现状开销(节点数 × 每轮扫描)是否构成实际热点(建议文档 §七指出是调度控制面开销热点 — 以 bot 图规模判断)。
2. 收敛方案不引入新的持久化机制(不动 DeliverStore ABC 语义),是调度器内存层的查询整理。
3. 现有 parallel scheduler 测试(termination/trigger/routing/recovery)全绿为不变式守护。

产出:收敛实现 + 测试绿 + `AGENTS.md` 中调度收敛段落更新。

## Comments

**决议(2026-08-15 关闭):设计收敛由 05/08 完成,扫描合并优化明确不做。**

**已纳入设计(归 05/08 实现票,本票零实现)**:
- bootstrap 扫描:08 定稿 — mode 显式化(FRESH 零扫描/RECOVERY 完整推导)、END 纳入三处扫描+BFS、auto-promote 双补全前置
- `_can_reach_active` 第三源:08 定稿 — 去 END 跳过(对称)
- `_recheck_pending` 与 dispatch 双路径:05 定稿后结构性正确 — dispatch=纯唤醒(控制面),store 扫描=唯一数据面;原票担心的"双数据路径"问题消失,双路径即设计意图,不得"收敛"掉

**明确不做(避免误导)**:
- 扫描合并/共享快照/增量失效层 — bot 图 ≈5 节点,进程内 SQLite 索引查询微秒级,单次运行总查询量不构成热点;加层违背收敛原则
- 触发条件(重估门槛):图规模达百节点级,或 store 后端网络化(分布式)时重估扫描合并
