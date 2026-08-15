# 08 — re-invocation 状态语义与 deliver 账本跨版本定稿

Status: closed
Labels: wayfinder:deliberate
Assignee: GYT
Blocked-by: (无 — frontier)

## Question

ADR-0040 遗留的 re-invocation 语义问题,需定稿两点:

1. **状态继承**:re-invoke(version N+1)时 `bootstrap` 第 1 步 `rebuild_main_state` 会把上一轮的最终 state 恢复进 ctx.state。定稿:re-invocation 应**继承**上轮最终状态(对话延续语义,bot GraphConversation"继续对话"场景)还是**全新开始**(每次 invoke 独立)?若继承,`node_scratch` 中的上轮残留如何处置(与 07 票结论联动);若全新,bootstrap/re-invocation 分支需要什么最小改动。
2. **deliver 账本跨版本作用域**:DeliverStore 按 `graph_instance_id` 键控,re-invoke 后同一 gid 下 v1 的 delivers(PENDING 残留、CONSUMED_COMPLETED 历史)与 v2 的新 delivers 共存 — `query_consumable` 只过滤状态不过滤版本。验证(参考 01 票审计假设 5):v1 失败实例的未消费 PENDING delivers 会不会漏进 v2 执行?若会,定稿作用域方案 — 候选:deliver 记录加 graph version 列(ADR-0040 曾为 node_states 拒绝过此方案,理由是否同样适用于 deliver 需重估)、或 re-invoke 时将残留 PENDING 显式转终态。收敛优先:不加平行机制。

产出:两点定稿 + 最小实现 + re-invoke 回归测试 + ADR-0040 对应段落合并更新。

## 04/05 设计带来的新输入(2026-08-15)

STAGED 行同按 gid+source_node_id 键控:re-invoke v2 中源节点再次完成时,promote_staged_by_source 会把 **v1 残留 STAGED 一并提升**(绝不作废语义的推论)。本票定稿 deliver 账本跨版本作用域时需一并覆盖 PENDING(D2 泄漏,审计已证)与 STAGED(新增)两类残留;作废与否的默认策略已在 04 决议(绝不作废),本票只定作用域。

## Comments

**决议(2026-08-15,与 GYT deliberate 定稿):**

1. **问题 1(状态继承)随 07 关闭**:rebuild_main_state 退役,不存在恢复路径;业务延续在节点自己的持久化(会话/记忆层)。
2. **Q1 意图显式化(核心决议)**:bootstrap 增 mode 参数,由 orchestrator 入口显式传入(start_invoke→FRESH;_run_existing/recover→RECOVERY)。FRESH=[entry] 零扫描零 auto-promote(真·重开);RECOVERY=完整推导。第 5 步意图猜谜启发式(RUNNING→entry/terminal→[]/Null has_any_invocation 兜底)整体退役。ADR-0040 补记:re-invoke = from entry,文档与实现一致。
3. **Q2 撤回 — 账本不动,残留可见=正确设计**:PENDING 残留=不丢弃旧输入(无法判定无用);CONSUMED_PENDING 重读=业务按 IntegratedPayload.status 过滤(04/05 既定);STAGED 跨版本提升=绝不作废推论。不引入 SUPERSEDED、不加版本列。
4. **RECOVERY 种子推导 = 现行确定性 BFS 微调(用户裁定:大概率不用改)**:三处扫描(pending 种子/崩溃种子/auto-promote)+BFS 遍历去 END 跳过(END 纳入初始可执行节点);START 保持跳过(空种子→[entry] 回退已覆盖 START 崩溃,且 START 无 deliver store;promote_staged_by_source 按来源全量提升兜住其 STAGED)。可达性闭合由运行时 _can_reach_active 承担(种子集最小化冗余,撤回);_can_reach_active 第三源同步去 END 跳过。
5. **RECOVERY 空种子规则**:无可作起点节点 → [entry](START-only 崩溃场景,RUNNING 孤儿实例);PAUSED 且答案 deliver 未到达 → 不启动引擎(07 语义:答案到达触发)。
6. **reached_end 补全**:END 作为种子直接执行时无 dispatch 置位 → END 执行完成即置 reached_end(或终态按 END 记录判定),防误判 FAILED。
7. **auto-promote 双补全(用户认可)**:STAGED(源 COMPLETED,complete↔promote 微秒窗)+CONSUMED_PENDING(消费方 COMPLETED,既有 W4)——前置于种子推导。CANCELED 规范定义(2026-08-15 评审修订):节点 CANCELED=尝试级取消(种子跳过但 PENDING 扫描可再触发);"整图不恢复"由**实例终态(STOPPED)**决定,非节点 CANCELED — STOPPED 下残留 STAGED 永不可见=正确。
8. **撤回记录**:本会话曾误判"START 跳过=数据丢失缺陷"与"种子集需最小反链剪枝"——均被用户纠正,空种子回退与运行时可达性检查已分别覆盖。
