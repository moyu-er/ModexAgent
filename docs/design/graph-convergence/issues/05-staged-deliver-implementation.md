# 05 — 目标侧 STAGED 投递落地实现

Status: open
Labels: wayfinder:task
Assignee: GYT
Blocked-by: 04 (deliberate 已完成,待实施)

## 实施计划(2026-08-15 定稿)

两阶段落地,每阶段独立提交、独立全绿:
- **阶段 i(store 层)**:四态枚举 + 三实现语义统一(InMemory 认领改 CONSUMED_PENDING/query 可见/promote 翻终态)+ promote_staged_by_source/void_staged + DDL 迁移(CHECK rebuild+新索引)+ 同一状态机测试套件跑三实现。不动 Node.run,旧调度全绿。
- **阶段 ii(机制重排)**:Node.deliver 即刻路由(删 _pending_delivers/submit)、complete→promote→dispatch 时序、UndeliveredError 检测点迁移、bootstrap STAGED auto-promote 前置、调度器/恢复套件按四态语义更新。

已定微决策:promote 失败=raise 走 crash 路径(靠 bootstrap 补刷恢复);deliver() 保持 void;测试冲突按四态新语义改断言。

## 终态整合(2026-08-15 讨论闭环)

全局实施顺序定案:**05 → 07 → 03**(07 先收缩 Node.run/state 模型,03 对终态机制写一遍不返工)。05 与 07 边界:05 只做时序重排,complete_invocation 签名照旧;07 随后删 state 参数/resume 分支/suspend/rebuild — 各自动 Node.run 一次,独立提交可 bisect。阶段 i 承接 InMemory 语义对齐引起的全部既有测试更新;conformance 套件 = 参数化三实现共享用例 + Null 差异契约单列。**本票讨论全部结束,待实施;实施前绝不写代码。**

## Question

按 04 票定稿(含第二轮修订:目标侧 STAGED 投递、绝不做废、按来源全量刷新)实现:

1. **DeliverStore 四态**:`DeliverConsumptionStatus` 增加 `STAGED`;三实现语义对齐 — Sqlite:CHECK 约束 + `query_consumable` 维持 `IN ('pending','consumed_pending')`(STAGED 天然不可见)+ 新方法 `promote_staged_by_source(gid, source_node_id)`(单表 UPDATE,零命中零影响);InMemory:遍历同语义;Null:忽略 STAGED(deliver 即可见,mark=删除语义不变)。~~void_staged~~(已移除,04 定稿:业务 exactly-once 走 integrator 过滤)。
2. **写路径单一化**:`Node.deliver()` 经 coordinator 即刻 route 到目标侧 store(STAGED),含 `_resolve_default_target` 拓扑解析/校验前移到 deliver 调用时点(RoutingError 时点变化记录在案);删除 `_pending_delivers`/`_submit` 搬运逻辑。
3. **Node.run 时序重排**:execute → complete_invocation → `promote_staged_by_source`(本节点全部 STAGED 含历史尝试残留 → PENDING)→ dispatch 激活(内存信号);调度器双路径(`_handle_dispatch` 快路径 / `_recheck_pending` 扫描)核对不产生可见性竞态。
4. **绝不做废**:`begin_invocation` 不清理任何 STAGED;`void_staged` 仅作为业务节点可选能力暴露(store 方法 + coordinator 透传),框架默认路径零调用。崩溃重试 N 次 → 完成时 N 份产出全部可见(at-least-once by design)。
5. **bootstrap 对称 auto-promote**:STAGED 行按 source_node_id 分组,来源节点最新 invocation 已 COMPLETED 但行仍 STAGED → 补 promote(与 CONSUMED_PENDING auto-promote 同模式)。
6. **迁移**:SQLite DDL CHECK 约束变更(轻量迁移路径,与现有 `_init_schema` rebuild 约定一致);workspace migration 与 standalone 双路径。SQL 无需新列(source_node_id/source_invocation_id 已存在)。
7. **装配**:CoordinatorFactory 不变;bot 图 SQLite / ReAct Null 装配验证。
8. **测试**:崩溃矩阵四窗口用例(execute 中崩溃→STAGED 保留可观测+重执行后一并提升;complete-刷新间隙→auto-promote;刷新-激活间隙→store 扫描接管;消费中 at-least-once 不回归);`test_recovery_delivers_old_and_retried_payload_at_least_once` 断言不变(语义正是新设计);`test_deliver_submit.py`、`test_scheduler_recovery.py`、`test_linear_recovery_entry.py`、`test_distributed_persistence_e2e.py` 既有套件全绿(时序重排涉及的用例按新语义更新)。
9. **文档**:`src/modex_graph/AGENTS.md` deliver 机制段落、ADR-0033 持久化契约段落合并更新(留 13 票终局一并做也可)。

关闭标准:三实现语义对齐 + 时序重排落地 + 崩溃矩阵用例绿 + 既有恢复套件绿。

## InMemory 认领语义收敛(2026-08-15 补,审计遗漏的显式分歧)

现状:InMemoryDeliverStore 认领用独立态 `CONSUMED`(query 不可见),promote=删除;Sqlite 用 `CONSUMED_PENDING`(query 可见,at-least-once 重读)。⇒ 同一 ABC 两套认领语义:InMemory 消费方崩溃重试拿到**空输入**(认领行不可见),Sqlite 重读认领行;调度器恢复测试用 InMemory 工厂,测的恢复语义与 SQLite 生产语义不一致。收敛:三实现统一四态机(STAGED/PENDING/CONSUMED_PENDING/CONSUMED_COMPLETED),InMemory 改用 CONSUMED_PENDING 认领+query 可见+promote 翻终态(不再删除),持久化差异只体现在"是否活得过进程重启";Null 维持无状态契约(mark=删除,不承诺恢复)。属本票"三实现语义对齐"的显式化,验收时以"同一状态机测试套件跑三实现"为准。

## 入口分离与终局补全(2026-08-15 第三轮)

1. **两个写入口**:Node.deliver(execute 内)→ stage 路径,写 STAGED;外部 route_deliver(deliver_to_node 控制面)→ 无源 invocation 可等,直写 PENDING(写 STAGED 将永不可见)。Null 两入口等价(均立即可见)。
2. **唤醒寻址**:promote_staged_by_source 返回受影响目标 node_id 集合,Node.run 据此发 dispatch 唤醒 — 旧 `_pending_delivers` 内存累加彻底删除,不留任何内存态。
3. **未完成来源的 STAGED 永不可见**(by design):崩溃循环/CANCELED 的源,其 STAGED 停留不可见 = 未完成工作不应可见;业务可选 void_staged。
4. **CONSUMED 枚举值移除**:InMemory 对齐后旧值 CONSUMED 无使用者,从枚举与 SQL CHECK 中删除(CHECK 变更走 `_init_schema` rebuild 轻量迁移)。附带修复:AgentNode 的 CONSUMED_PENDING 过滤此前在三实现下行为不一致(InMemory 认领行本不可见),统一后三实现一致。

## 实现级约束(2026-08-15 第四轮,精确控制核查发现)

1. **bootstrap 顺序约束(关键)**:STAGED auto-promote 必须在 seed 推导**之前**执行 — 否则"complete 成功 + 刷新失败 + 图 CRASHED"场景恢复时:seeds 空(节点全 COMPLETED)→ 空 seeds 分支 → re-invoke from entry,把 99% 完成的图从头跑。先补刷 STAGED→PENDING 则步骤 3 的 pending-deliver 扫描发现它们,正常续跑。
2. **promote 失败传播**:`promote_staged_by_source` 抛错 → 让 Node.run 整体走 crash 路径(图 CRASHED),依赖约束 1 的 bootstrap 补刷恢复 — 不 log-and-continue(静默吞错 → STAGED 不可见 → UndeliveredError 死端 FAILED,更难诊断)。asyncio 单线程 + caller-owned 单连接,promote 失败实际只剩磁盘级错误(罕见)。
3. **UndeliveredError 检测点迁移**:旧 submit 时判"零 deliver 死端"→ 新时点为 complete 后 promote 返回空受影响集(本节点零 STAGED)→ 语义保持 FAILED,时点后移。
4. **索引**:promote 的 UPDATE 按 (gid, source_node_id, status) 扫描,现有索引不覆盖 — 补一个 `(graph_instance_id, source_node_id, status)` 部分索引(仅 Sqlite,DDL 并入迁移)。

## IntegratedPayload 状态可见性(2026-08-15 第五轮,用户指出 seam 缺口)

现状缺口:`IntegratedPayload` 仅 source_node/content/metadata,**不带消费状态** — 业务无法在 integrate 层按状态自定义(尤其 AgentNode 需丢 CONSUMED_PENDING 防重复重放),只能整个覆写 `_integrate_upstream` 自建 collect(现状 AgentNode 正是如此),InputIntegrator seam 形同虚设。

补强:
1. `IntegratedPayload` 增加 `status: str`(pending/consumed_pending)+ `consumed_by_invocation_id: int | None`(区分"本节点上次崩溃尝试认领"的行)。
2. 框架默认 `_integrate_upstream` 构造 payload 时从 DeliverRecord 带入状态;**DefaultInputIntegrator 不过滤**(全量 at-least-once 语义,框架默许重读 CONSUMED_PENDING)。
3. AgentNode 简化:从"覆写 _integrate_upstream 自建 collect+过滤"改为"按 payload.status 丢 consumed_pending"(或注入自定义 integrator)— 成为业务自定义 integrate 的样板;被过滤行保持原状态不被 mark,由 auto-promote/下次执行收敛。
4. complete_invocation 澄清(非新增):现状既有步骤(RUNNING→COMPLETED CAS+全量快照),是 STAGED 提升/恢复快照/seeds 推导的前提;04/05 未改其语义,只在链路图中补全展示。

**收敛纪律(2026-08-15 补)**:IntegratedPayload.status 落地时,AgentNode 现有的覆写 `_integrate_upstream` 自建 collect+过滤路径**必须删除**,收敛到 integrator seam(框架默认 collect + 按状态过滤的自定义 integrator)— 不允许新旧两条消费路径并存(仓库收敛规则)。bot 图的 envelope 组装(BotAgentNode._build_graph_input_envelope)是节点业务逻辑,不动。**全局 state 快照层由 07 整体删除(非"剥离"),本票不碰快照**。

## 命名消歧(2026-08-15 补,两轴正交勿混淆)

状态机两条轴:**可见性轴**(源侧)— `promote_staged_by_source`:源 node complete 后,其全部 STAGED→PENDING;**消费轴**(消费侧)— 既有 `promote_delivers`:消费方 complete 后,CONSUMED_PENDING→CONSUMED_COMPLETED。Node.run 步序:begin → collect+mark(认领)→ integrate → execute(内 deliver STAGED)→ complete → promote_staged_by_source(本节点产出可见化)→ dispatch(纯调度唤醒,不携带数据;Linear 选路/Parallel 快路径必需,`_recheck_pending` 扫描为兜底)→ promote_delivers(本节点输入消费完成化)。重复消费语义:消费方非 resume 重试重读 PENDING+CONSUMED_PENDING(W3 at-least-once 有界);**可见行数=成功 deliver() 声明数,崩溃尝试的声明行存活并由后续完成的源一并提升**(04 规范口径);AgentNode 过滤 CONSUMED_PENDING 靠会话记忆幂等(ADR-0038 D5)。
