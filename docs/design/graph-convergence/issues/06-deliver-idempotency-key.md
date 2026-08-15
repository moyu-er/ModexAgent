# 06 — Deliver 幂等键:外部投递幂等与纵深防御(缩编版)

Status: closed
Labels: wayfinder:deliberate
Assignee: GYT
Blocked-by: 01

## Question

> ⚠️ 以下缩编 body 写于 04 第一轮决议后,其中"重执行作废旧 STAGED、SQLite 路径不再产生重复"**已被 04 第二轮修订否决**(绝不作废+at-least-once,崩溃重复=by design)。规范决议以 Comments 为准;本票已关闭,零实现遗留。

**范围缩编**(04 票决议):D1(崩溃重执行→下游重复投递)在 SQLite 路径已被目标侧 STAGED 投递关闭(04/05) — 重执行 `begin_invocation` 作废旧 STAGED,刷新只在 complete 后发生,不再产生跨 invocation 重复。InMemory/Null 路径崩溃即全丢、无重复语义。因此本票缩编为:

1. **`deliver_to_node` 外部投递幂等**(核心剩余需求):REST/控制面调用方重试(网络超时重发)会向目标 store 产生重复 PENDING 行 — 调用方拿不到稳定 deliver_id 做去重。定稿:外部投递是否引入调用方幂等键(RestgreSQL 惯例:client-generated request-id + 唯一约束 + 冲突返回既有 deliver_id),以及键的作用域(实例级?节点级?)。以 01 票审计 W13 结论为事实基础。
2. **业务纵深防御(可选)**:节点业务输出键(source_node + 逻辑序号)唯一约束,防御未来非崩溃路径的重复投递(如调度 bug、双写)。评估:是否值得常驻唯一约束,还是按"两用例原则"等第二个真实需求。倾向:不做,记录判断依据。

产出:外部投递幂等定稿 + (可选)纵深防御取舍 + 实现 + 测试。

## 设计联动(2026-08-15)

04/05 已定:外部 route_deliver 维持直写 PENDING(入口分离);框架级去重键明确拒绝(崩溃重复=at-least-once by design)。本票剩余唯一问题:deliver_to_node 调用方重试(REST 重发/WebUI 双击)产生的重复 PENDING 行 — 接受 at-least-once 并文档化,或引入 client request-id 去重。纵深防御项倾向不做(两用例原则,04 决议已记录判断依据)。

## Comments

**决议(2026-08-15):关闭,不再独立讨论。** 框架级去重键被 04 决议明确拒绝(绝不作废 + 按来源全量提升 = at-least-once by design);外部投递(deliver_to_node)调用方重试的重复行同样接受 at-least-once — 手动操作场景、低危、对会话记忆型 agent 影响小,文档化即可(事项并入 13 票 ADR 收口)。纵深防御不做(两用例原则)。
