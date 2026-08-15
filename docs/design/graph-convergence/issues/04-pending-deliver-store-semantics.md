# 04 — PendingDeliverStore 语义定稿:ABC 化到什么等级

Status: closed
Labels: wayfinder:deliberate
Assignee: GYT
Blocked-by: 01

## Question

用户已明确:`Node._pending_delivers`(节点 execute 期间在内存累加 deliver 的累加器)**绝不能写死只能放内存**,要做成与其他存储一致的持久化 ABC 的一部分。但语义等级需讨论定稿,候选:

- **(a) 仅 ABC 化**:`PendingDeliverStore` ABC + Null/InMemory/Sqlite 三实现,崩溃后仍按现状整节点重执行(语义不变,只消除内存硬编码)。
- **(b) ABC + 断点续传**:crash 发生在 `execute` 返回后、`submit` 派发前时,恢复路径从持久化 pending delivers 恢复 submit,**不重跑 execute**(对 LLM 调用等昂贵副作用节点意义大)。
- **(c) ABC + 事务 outbox**:在 (b) 基础上,deliver 落库与 `complete_invocation` 同数据库事务提交,同时关闭"下游已写入但源 invocation 未完成"的重复 deliver 窗口(建议文档 P0.2 的完整方案)。

需定稿的问题:

1. 等级选择 — 以 01 票审计结论为事实基础:execute→submit 窗口与 route_deliver→complete 窗口的真实代价(LLM agent 节点重执行成本 vs 复杂度)。
2. 接口形态:`PendingDeliverStore` 是否作为第四个 store 并入 `CoordinatorFactory` 装配族;`Node._deliver`/`_collect_delivers` 如何改为经 store 读写;submit 后的清理语义。
3. 与 06 票(deliver 幂等键)的分工:幂等键管"重执行不重复投递",pending store 管"崩溃不丢失未派发输出" — 边界如何切。
4. Null 策略语义(ReAct 每轮 turn coordinator 用 Null — 保持现状无持久化)。

产出:定稿决策(等级+接口草图+与 06 的边界),直接进入 05 实现。

## Comments

**决议(2026-08-15,与 GYT deliberate 定稿):目标侧 STAGED 投递,零新 ABC。**

> ⚠️ 本段为第一轮决议,其中"begin_invocation 作废旧 STAGED""崩溃点 A→作废重建""D1 关闭"已被下方**第二轮修订取代**(绝不作废 + at-least-once)。规范口径以修订段为准。

用户核心立场:deliver 数据持久化是恢复语义的一部分,与 ReAct 记忆持久化对称;必须框架级提供,不能写死内存;但**复用既有 DeliverStore ABC(Null/InMemory/Sqlite)**,不建第四存储族。

**设计**:
1. `Node.deliver()` 在 execute 内即刻路由落库到目标侧 DeliverStore,status=`STAGED`(下游不可见);删除 `_pending_delivers` 内存累加 + submit 搬运两段式。
2. 四态状态机:`STAGED →(上游 complete_invocation)→ PENDING →(mark)→ CONSUMED_PENDING →(promote)→ CONSUMED_COMPLETED`;STAGED 残留由重执行 `begin_invocation` 作废(按 source_node_id)。
3. 时序:execute(内 deliver→STAGED)→ complete_invocation → 刷新 STAGED→PENDING → dispatch 激活。刷新必须在 complete 之后(否则 D1 复活)。
4. 崩溃矩阵:execute 中崩溃(含 agent turn 分钟级窗口)→ STAGED 可观测+重执行作废重建(D4 关闭);complete 后刷新前(微秒)→ bootstrap 对称 auto-promote 兜底(D1 关闭);消费中崩溃 → W3 at-least-once 不变。
5. 装配:ReAct = Null store(STAGED 无操作,deliver 即可见,Linear 下无行为差异,与 02 票一致);bot 图 = SQLite(turn 内每次 deliver 即刻持久)。Null/InMemory/Sqlite 是装配决策。

**实现细节(已定,可翻)**:刷新独立 commit + bootstrap 对称 auto-promote(不搞跨 store 同事务,与 W4 既有模式对称);作废在 begin_invocation;Null 忽略 STAGED。

**对 06 的影响**:D1 在 SQLite 路径关闭 → 06 缩编为 deliver_to_node 外部投递幂等 + 业务纵深防御。

**修订(2026-08-15 第二轮,用户修正刷新/作废语义):**

1. **绝不做废**(框架默认):deliver 是不可撤销的工作产出声明。崩溃重试的旧 STAGED 保留 — 那是 agent 真实产出,作废即丢工作。`begin_invocation` 不做任何清理;框架提供 `void_staged` 能力,业务节点要 exactly-once 自己调用(默认无人调用)。
2. **刷新按来源,全量提升**:complete_invocation 后,对注册的目标 store 执行 `promote_staged_by_source(gid, source_node_id)` — 该来源的**全部** STAGED(本次+历史尝试残留)统一转 PENDING。无投递来源零命中零影响;按 source_node_id 定位,不影响其他节点投递的行。
3. **SQL 无需新列**:现有 `source_node_id`(刷新定位)+ `source_invocation_id`(区分哪次尝试,供业务作废策略)已足够。
4. **多实现收敛**:Sqlite 单表 UPDATE(WHERE gid+source_node_id+status=staged)/ InMemory 遍历同语义 / Null 无操作。
5. **bootstrap 对称 auto-promote 条件按来源节点**:STAGED 行来源节点的最新 invocation 已 COMPLETED 但刷新未发生 → 补刷新。
6. **语义修正**:崩溃重试 N 次 → 完成时 N 份产出全部可见 = **by-design at-least-once**(与既有测试 `test_recovery_delivers_old_and_retried_payload_at_least_once` 断言一致,测试无需改)。D1 回归 by-design;D4 修正为"输出不再丢失"(重执行成本照旧被接受)。框架级去重键被明确拒绝,06 缩编维持。

**输出基数规范口径**(2026-08-15 评审统一,全票适用):可见行数 = 成功 `deliver()` 声明数;崩溃尝试的声明行存活,由后续完成的源 invocation 一并提升。("一次完成可能提升多份历史声明" — 非"N 份⇔N 次成功完成"。)**void_staged 移除**(2026-08-15 定稿):零调用者的投机 API,违背收敛原则;业务 exactly-once 的控制点本就是 integrator seam(按 status/来源过滤重复 payload)— 第二轮修订中"业务可选作废能力"表述作废。
